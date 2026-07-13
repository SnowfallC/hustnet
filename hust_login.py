#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HUST 校园网助手的登录与保活核心逻辑。"""

import configparser
import html
import json
import logging
import math
import os
import re
import sys
import threading
import time
from datetime import datetime
from typing import Dict, Optional
from urllib.parse import parse_qs, urljoin, urlparse

try:
    import requests
except ImportError:
    sys.exit("缺少依赖 requests，请先执行：pip install requests")


logger = logging.getLogger("hust-login")
logger.setLevel(logging.INFO)
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(
    logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
)
logger.addHandler(_handler)
connection_lock = threading.Lock()
last_auth_origin = ""


def load_config(path: str) -> configparser.ConfigParser:
    """读取 UTF-8 编码的配置文件。"""
    if not os.path.isfile(path):
        logger.error("配置文件不存在：%s", path)
        logger.error("请先复制 config.ini 并填写你的账号密码。")
        sys.exit(1)
    cfg = configparser.ConfigParser()
    cfg.read(path, encoding="utf-8")
    return cfg


def now_str() -> str:
    return datetime.now().strftime("%H:%M:%S")


def build_base_url(cfg) -> str:
    """返回界面展示用的配置服务器地址。实际登录地址从认证页动态获取。"""
    proto = "https" if cfg.getboolean("server", "use_https", fallback=False) else "http"
    host = cfg.get("server", "host", fallback="192.168.170.168").strip()
    if host.startswith(("http://", "https://")):
        parsed = urlparse(host)
        return f"{parsed.scheme}://{parsed.netloc}"
    host = host.rstrip("/")
    return f"{proto}://{host}"


def _find_portal_url(response: requests.Response) -> str:
    """从 HTTP 跳转链或认证页 HTML 中提取 ePortal 地址。"""
    candidates = [response.url, response.headers.get("Location", "")]
    candidates.extend(item.headers.get("Location", "") for item in response.history)
    candidates.extend(
        re.findall(r"(?:href|location)\s*=\s*['\"]([^'\"]+)", response.text or "", re.I)
    )
    for candidate in candidates:
        candidate = html.unescape(candidate.strip())
        if not candidate:
            continue
        candidate = urljoin(response.url, candidate)
        if "/eportal/" in candidate.lower():
            return candidate
    return ""


def get_auth_context(session: requests.Session, cfg) -> Optional[Dict[str, str]]:
    """获取本次登录的认证页、服务器公钥和 MAC 参数。"""
    probe_url = cfg.get("network", "probe_url", fallback="http://www.baidu.com")
    probe_urls = [probe_url]
    if probe_url != "http://1.1.1.1":
        probe_urls.append("http://1.1.1.1")

    redirect_url = ""
    for url in probe_urls:
        try:
            response = session.get(url, timeout=8, allow_redirects=True, verify=False)
        except requests.RequestException:
            continue
        redirect_url = _find_portal_url(response)
        if redirect_url:
            break

    if not redirect_url:
        logger.error("未获取到 ePortal 认证页地址，无法发起登录。")
        return None

    parsed = urlparse(redirect_url)
    if not parsed.scheme or not parsed.netloc:
        logger.error("认证页地址格式无效：%s", redirect_url)
        return None
    origin = f"{parsed.scheme}://{parsed.netloc}"

    try:
        response = session.post(
            f"{origin}/eportal/InterFace.do?method=pageInfo",
            data={"queryString": redirect_url},
            timeout=8,
            verify=False,
        )
        page_info = response.json()
    except (requests.RequestException, ValueError) as error:
        logger.error("获取 ePortal 加密参数失败：%s", error)
        return None

    password_encrypt = str(page_info.get("passwordEncrypt", "")).lower() == "true"
    exponent = str(page_info.get("publicKeyExponent", ""))
    modulus = str(page_info.get("publicKeyModulus", ""))
    if password_encrypt and (not exponent or not modulus):
        logger.error("认证服务器未返回完整 RSA 公钥。")
        return None

    mac = parse_qs(parsed.query).get("mac", ["111111111"])[0]
    global last_auth_origin
    last_auth_origin = origin
    logger.info("已获取本次认证参数：%s", origin)
    logger.info("服务器下发的服务配置：%s", page_info.get("service", {}))
    return {
        "origin": origin,
        "redirect_url": redirect_url,
        "query_string": parsed.query,
        "exponent": exponent,
        "modulus": modulus,
        "mac": mac,
        "password_encrypt": "true" if password_encrypt else "false",
    }


def rsa_utils_encrypt_password(password: str, modulus_hex: str, exponent_hex: str, mac: str) -> str:
    """按当前 ePortal RSAUtils 小端序分块规则加密密码。"""
    exponent = int(exponent_hex, 16)
    modulus = int(modulus_hex, 16)
    chunk_size = math.ceil(modulus.bit_length() / 8)
    plain = (f"{password}>{mac}")[::-1]
    blocks = []
    for offset in range(0, len(plain), chunk_size):
        block = plain[offset:offset + chunk_size]
        value = sum(ord(char) << (8 * index) for index, char in enumerate(block))
        blocks.append(pow(value, exponent, modulus).to_bytes(chunk_size, byteorder="big").hex())
    return "".join(blocks)


def is_online(session: requests.Session, cfg) -> bool:
    """检测探针是否仍被 ePortal 劫持。"""
    probe_url = cfg.get("network", "probe_url", fallback="http://www.baidu.com")
    keyword = cfg.get("network", "probe_keyword", fallback="").strip()
    try:
        response = session.get(probe_url, timeout=8, allow_redirects=False)
    except requests.RequestException as error:
        logger.warning("探针请求失败（%s），按离线处理。", error)
        return False

    location = response.headers.get("Location", "").lower()
    if response.status_code in (301, 302, 303, 307, 308):
        return not any(token in location for token in ("eportal", "login", "192.168."))

    text = response.text or ""
    if "eportal" in text.lower() or ("login" in text.lower() and "password" in text.lower()):
        return False
    return keyword in text if keyword else response.status_code == 200


def login(session: requests.Session, cfg) -> bool:
    """使用认证服务器本次下发的参数提交登录。"""
    username = cfg.get("account", "username").strip()
    password = cfg.get("account", "password")
    service = cfg.get("service", "service", fallback="").strip()
    logger.info("本次登录服务参数：%s", service or "<空值>")
    context = get_auth_context(session, cfg)
    if not context:
        return False

    encrypted = context["password_encrypt"] == "true"
    try:
        login_password = rsa_utils_encrypt_password(
            password, context["modulus"], context["exponent"], context["mac"]
        ) if encrypted else password
    except (TypeError, ValueError, OverflowError) as error:
        logger.error("密码加密失败：%s", error)
        return False

    data = {
        "userId": username,
        "password": login_password,
        "service": service,
        "queryString": context["query_string"],
        "operatorPwd": "",
        "operatorUserId": "",
        "validcode": "",
        "passwordEncrypt": "true" if encrypted else "",
    }
    headers = {
        "Origin": context["origin"],
        "Referer": context["redirect_url"],
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }
    try:
        response = session.post(
            f"{context['origin']}/eportal/InterFace.do?method=login",
            data=data,
            headers=headers,
            timeout=10,
            verify=False,
        )
        result = json.loads(response.content.decode("utf-8"))
    except (requests.RequestException, ValueError) as error:
        logger.error("登录请求异常：%s", error)
        return False

    if result.get("result") == "success":
        logger.info("登录成功 ✓  用户：%s", username)
        return True
    logger.warning("登录失败：服务器返回 → %s", result.get("message", result))
    return False


def logout(cfg) -> bool:
    """主动断开当前 ePortal 认证，并保留明确的结果日志。"""
    session = requests.Session()
    session.trust_env = False
    requests.packages.urllib3.disable_warnings()
    origin = last_auth_origin or build_base_url(cfg)
    success_url = f"{origin}/eportal/redirectortosuccess.jsp"
    try:
        response = session.get(success_url, timeout=8, allow_redirects=True, verify=False)
    except requests.RequestException as error:
        logger.error("获取在线会话失败：%s", error)
        return False

    candidates = [response.url, response.headers.get("Location", "")]
    candidates.extend(item.headers.get("Location", "") for item in response.history)
    user_index = ""
    for candidate in candidates:
        params = parse_qs(urlparse(urljoin(origin, candidate)).query)
        user_index = params.get("userIndex", [""])[0]
        if user_index:
            break
    if not user_index:
        logger.error("未获取到当前在线会话，可能已经断开认证。")
        return False

    try:
        response = session.post(
            f"{origin}/eportal/InterFace.do?method=logout",
            data={"userIndex": user_index},
            headers={"Origin": origin, "Referer": response.url},
            timeout=8,
            verify=False,
        )
    except requests.RequestException as error:
        logger.error("断开认证请求失败：%s", error)
        return False

    if response.status_code != 200:
        logger.error("断开认证失败，服务器状态码：%s", response.status_code)
        return False
    try:
        result = response.json()
    except ValueError:
        result = {}
    if result and result.get("result") not in ("success", "ok"):
        logger.error("断开认证失败：%s", result.get("message", result))
        return False
    logger.info("已主动断开校园网认证。")
    return True


def run_once(cfg) -> bool:
    """执行一次检测；未联网时登录并再次确认连通性。"""
    with connection_lock:
        session = requests.Session()
        session.trust_env = False
        requests.packages.urllib3.disable_warnings()

        if is_online(session, cfg):
            logger.info("网络正常，无需登录。")
            return True

        logger.info("检测到未登录，开始登录流程 …")
        max_retries = cfg.getint("check", "max_retries", fallback=3)
        retry_delay = cfg.getint("check", "retry_delay", fallback=5)
        for attempt in range(1, max_retries + 1):
            logger.info("第 %d/%d 次尝试登录 …", attempt, max_retries)
            if login(session, cfg):
                time.sleep(2)
                if is_online(session, cfg):
                    logger.info("登录后网络探测正常 ✓")
                    return True
                logger.warning("登录请求成功但探测仍失败，继续重试。")
            if attempt < max_retries:
                time.sleep(retry_delay)
        return False


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    cfg = load_config(os.path.join(here, "config.ini"))
    if cfg.getboolean("log", "verbose", fallback=True):
        logger.setLevel(logging.DEBUG)

    interval = cfg.getint("check", "interval_minutes", fallback=10)
    logger.info("=" * 56)
    logger.info("HUST 校园网助手已启动")
    logger.info("账号：%s", cfg.get("account", "username").strip())
    logger.info("检查间隔：%d 分钟", interval)
    logger.info("=" * 56)
    run_once(cfg)

    while True:
        time.sleep(interval * 60)
        logger.info("---- 定时检查（%s）----", now_str())
        try:
            run_once(cfg)
        except KeyboardInterrupt:
            raise
        except Exception as error:
            logger.error("本轮检查出现异常（已忽略，%d 分钟后重试）：%s", interval, error)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("收到 Ctrl+C，已退出。")

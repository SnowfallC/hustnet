#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HUST校园网助手 - 登录核心逻辑

功能：
    每隔 N 分钟（默认 10 分钟）检测一次网络连通性，
    若发现未登录，则自动提交一次登录请求。

依赖：
    pip install requests pycryptodome
"""

import configparser
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from urllib.parse import urlparse, parse_qs, urlencode

try:
    import requests
except ImportError:
    sys.exit("缺少依赖 requests，请先执行：  pip install requests")

try:
    from Crypto.Cipher import PKCS1_v1_5
    from Crypto.PublicKey import RSA
except ImportError:
    sys.exit("缺少依赖 pycryptodome，请先执行：  pip install pycryptodome")


# ===================== ePortal 固定 RSA 公钥 =====================
# 取自登录页 security.js，全校统一，一般无需修改。
E_PUB = 65537
# modulus（十六进制）
N_HEX = (
    "b6c3a5e7a3f1c8d2e4f6a8b0c2d4e6f8a0b2c4d6e8f0a2b4c6d8e0f2a4b6c8d0"
    "e2f4a6b8c0d2e4f6a8b0c2d4e6f8a0b2c4d6e8f0a2b4c6d8e0f2a4b6c8d0e2f4"
)
# NOTE：上面的 N_HEX 是占位示例。不同时期、不同校区的 ePortal 下发的公钥
# 可能不同。请按下述方法获取真实公钥后替换本常量（脚本附带自动探测逻辑）：
#   1) 浏览器打开登录页（未登录会被自动重定向到 192.168.170.168/eportal/...）
#   2) F12 → Sources → 找到 security.js（或类似的加密脚本）
#   3) 里面 setPublic(modulus, exponent) 的第一个参数即为公钥 modulus（十六进制）
# 若你不知道怎么拿，脚本会优先尝试从登录页 HTML/JS 里自动抓取，抓不到才用此默认值。


# ===================== 日志 =====================
logger = logging.getLogger("hust-login")
logger.setLevel(logging.INFO)
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(
    logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
)
logger.addHandler(_handler)


# ===================== 工具函数 =====================
def load_config(path: str) -> configparser.ConfigParser:
    if not os.path.isfile(path):
        logger.error("配置文件不存在：%s", path)
        logger.error("请先复制 config.ini 并填写你的账号密码。")
        sys.exit(1)
    cfg = configparser.ConfigParser()
    # 兼容中文/特殊字符，按原样读取
    cfg.read(path, encoding="utf-8")
    return cfg


def now_str() -> str:
    return datetime.now().strftime("%H:%M:%S")


def build_base_url(cfg) -> str:
    proto = "https" if cfg.getboolean("server", "use_https", fallback=False) else "http"
    host = cfg.get("server", "host", fallback="192.168.170.168")
    return f"{proto}://{host}"


# ===================== RSA 加密 =====================
def _hex_to_bytes(h: str) -> bytes:
    h = h.strip().replace(" ", "").replace("\n", "")
    if len(h) % 2:
        h = "0" + h
    return bytes.fromhex(h)


def rsa_encrypt_password(password: str, modulus_hex: str, exponent: int = E_PUB) -> str:
    """用 ePortal 下发的 RSA 公钥加密密码，返回 Base64 字符串。"""
    import base64

    n = int(modulus_hex, 16)
    key = RSA.construct((n, exponent))
    cipher = PKCS1_v1_5.new(key)
    encrypted = cipher.encrypt(password.encode("utf-8"))
    return base64.b64encode(encrypted).decode("ascii")


# ===================== 探测登录页公钥 =====================
_PUBLIC_KEY_PATTERNS = [
    # security.js 风格：setPublic("modulus", "10001") 或 setPublic('modulus','10001')
    re.compile(
        r"""setPublic\s*\(\s*['"]([0-9A-Fa-f]{128,2048})['"]\s*,\s*['"]([0-9A-Fa-f]+)['"]\s*\)"""
    ),
    # Security 类型构造：new RSAUtils.getKeyPair("...10001...","",'modulus')
    re.compile(
        r"""getPublicKey\s*\(\s*['"]([0-9A-Fa-f]{128,2048})['"]\s*"""
    ),
]


def fetch_public_modulus(session: requests.Session, base_url: str) -> str:
    """尝试从登录页相关 JS 中抓取真实的 RSA 公钥 modulus。抓不到则返回默认值。"""
    candidates = [
        f"{base_url}/eportal/InterFace.do?method=pageInfo",
        f"{base_url}/eportal/index.jsp",
        f"{base_url}/eportal/js/security.js",
        f"{base_url}/eportal/InterFace.do?method=login",
    ]
    for url in candidates:
        try:
            r = session.get(url, timeout=5, verify=False)
        except requests.RequestException:
            continue
        if not r.text:
            continue
        for pat in _PUBLIC_KEY_PATTERNS:
            m = pat.search(r.text)
            if m:
                mod = m.group(1)
                logger.info("已从登录页自动获取 RSA 公钥 modulus（前 16 位：%s…）", mod[:16])
                return mod
    logger.debug("未能从登录页自动获取公钥，使用配置/默认 modulus。")
    return N_HEX


# ===================== 网络检测 =====================
def is_online(session: requests.Session, cfg) -> bool:
    """
    判断当前是否已经联网。
    规则：
      - 探针 URL 返回 200 且响应中能找到关键字（或无关键字时只要 200）→ 在线
      - 探针 URL 302/跳转到 ePortal 登录页 → 离线
      - 探针 URL 返回的内容包含 eportal 字样 → 离线
    """
    probe_url = cfg.get("network", "probe_url", fallback="http://www.baidu.com")
    keyword = cfg.get("network", "probe_keyword", fallback="").strip()
    try:
        resp = session.get(
            probe_url,
            timeout=8,
            allow_redirects=False,  # 不跟随跳转，便于判断是否被劫持到登录页
        )
    except requests.RequestException as e:
        logger.warning("探针请求失败（%s），按离线处理。", e)
        return False

    # 被重定向 → 一定是未登录
    if resp.status_code in (301, 302, 303, 307, 308):
        loc = resp.headers.get("Location", "")
        if "eportal" in loc.lower() or "192.168." in loc or "login" in loc.lower():
            return False
        # 其它重定向（如 baidu → https）暂时按在线处理
        return True

    text = resp.text or ""
    if "eportal" in text.lower() or "login" in text.lower() and "password" in text.lower():
        return False

    if keyword:
        return keyword in text
    return resp.status_code == 200


# ===================== 登录流程 =====================
def get_redirect_url(session: requests.Session, cfg) -> str:
    """
    触发一次"未登录重定向"，拿到包含 userip / ac_id 等参数的登录页 URL。
    登录接口需要把这个 URL 的 query 部分原样作为 queryString 提交。
    """
    probe_url = cfg.get("network", "probe_url", fallback="http://www.baidu.com")
    # 这里要让它跟随一次跳转，以便拿到 eportal 的完整 URL
    resp = session.get(probe_url, timeout=8, allow_redirects=True)
    final_url = resp.url
    if "eportal" not in final_url.lower():
        # 没跳到 eportal，可能是已经登录了
        return ""
    return final_url


def login(session: requests.Session, cfg, public_modulus: str) -> bool:
    base_url = build_base_url(cfg)
    username = cfg.get("account", "username").strip()
    password = cfg.get("account", "password")
    service = cfg.get("service", "service", fallback="education").strip()

    # 1) 拿到重定向后的登录页 URL，从中提取 queryString
    redirect_url = get_redirect_url(session, cfg)
    if not redirect_url:
        logger.info("未检测到 ePortal 登录页，可能已经在线或网络异常。")
        return False

    parsed = urlparse(redirect_url)
    qs = parsed.query  # 例如: userip=10.x.x.x&ac_id=...&...
    if not qs:
        qs = ""

    # 2) 加密密码
    try:
        encrypted_pwd = rsa_encrypt_password(password, public_modulus)
    except Exception as e:
        logger.error("密码 RSA 加密失败：%s", e)
        return False

    # 3) 提交登录请求
    login_url = f"{base_url}/eportal/InterFace.do?method=login"
    data = {
        "userId": username,
        "password": encrypted_pwd,
        "service": service,
        "queryString": qs,
        "operatorPwd": "",
        "operatorUserId": "",
        "validcode": "",
        "passwordEncrypt": "true",
    }
    headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Referer": redirect_url,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36",
    }

    try:
        r = session.post(login_url, data=data, headers=headers, timeout=10, verify=False)
    except requests.RequestException as e:
        logger.error("登录请求异常：%s", e)
        return False

    text = (r.text or "").strip()
    logger.debug("登录返回：%s", text[:200])

    # 返回一般是 JSON：{"result":"success","message":"..."} 或 {"result":"fail",...}
    try:
        result = json.loads(text)
        if result.get("result") == "success":
            logger.info("登录成功 ✓  用户：%s", username)
            return True
        logger.warning("登录失败：服务器返回 → %s", result.get("message", text[:120]))
        return False
    except ValueError:
        # 非 JSON，按关键字兜底
        if "success" in text.lower():
            logger.info("登录成功 ✓")
            return True
        logger.warning("登录失败：非预期的返回 → %s", text[:120])
        return False


# ===================== 主循环 =====================
def run_once(cfg) -> bool:
    """执行一次"检测 + 必要时登录"。返回本次是否触发了登录且成功。"""
    base_url = build_base_url(cfg)
    session = requests.Session()
    # 关闭 SSL 警告（ePortal 常用自签名证书）
    requests.packages.urllib3.disable_warnings()

    if is_online(session, cfg):
        logger.info("网络正常，无需登录。")
        return True

    logger.info("检测到未登录，开始登录流程 …")
    modulus = fetch_public_modulus(session, base_url)

    max_retries = cfg.getint("check", "max_retries", fallback=3)
    retry_delay = cfg.getint("check", "retry_delay", fallback=5)

    for attempt in range(1, max_retries + 1):
        logger.info("第 %d/%d 次尝试登录 …", attempt, max_retries)
        ok = login(session, cfg, modulus)
        if ok:
            # 登录后再次探测确认
            time.sleep(2)
            if is_online(session, cfg):
                logger.info("登录后网络探测正常 ✓")
                return True
            logger.warning("登录请求成功但探测仍失败，可能需要等待，继续重试。")
        time.sleep(retry_delay)
    return False


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    cfg_path = os.path.join(here, "config.ini")
    cfg = load_config(cfg_path)

    if cfg.getboolean("log", "verbose", fallback=True):
        logger.setLevel(logging.DEBUG)

    interval = cfg.getint("check", "interval_minutes", fallback=10)
    logger.info("=" * 56)
    logger.info("HUST校园网助手 已启动")
    logger.info("账号：%s", cfg.get("account", "username").strip())
    logger.info("服务器：%s", build_base_url(cfg))
    logger.info("检查间隔：%d 分钟", interval)
    logger.info("=" * 56)

    # 启动时立即检测一次
    run_once(cfg)

    while True:
        # sleep 一个间隔后再检测
        time.sleep(interval * 60)
        logger.info("---- 定时检查（%s）----", now_str())
        try:
            run_once(cfg)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            # 主循环里捕获一切异常，避免某次失败导致整个脚本退出
            logger.error("本轮检查出现异常（已忽略，%d 分钟后重试）：%s", interval, e)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("收到 Ctrl+C，已退出。")
        sys.exit(0)

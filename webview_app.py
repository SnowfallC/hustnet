#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HUST校园网助手 - 桌面窗口版

特性：
    - 双击启动直接弹出原生窗口（用系统 EdgeWebView2 内核，不是浏览器）
    - 关闭窗口 → 缩到托盘后台保活（不退出）
    - 托盘"显示主窗口"→ 窗口回来

启动：
    python webview_app.py
"""
import os
import sys
import logging
import threading
import time
import traceback
import webbrowser
from collections import deque
from datetime import datetime
from urllib.parse import urlparse

# ---- 路径处理：区分「打包资源目录」和「用户数据目录」----
# RES_DIR：只读资源（webview_index.html）。打包后指向 exe 内部 _MEIPASS 或 exe 同级。
# USER_DIR：可写文件（config.ini、日志）。始终用 exe 同级目录，打包后方便用户找到/修改。
def _get_frozen_base():
    """返回 exe 所在目录（打包后）或脚本所在目录（开发时）。"""
    if getattr(sys, "frozen", False):
        # PyInstaller 打包后：sys.executable 是 exe 路径
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _get_resource_dir():
    """只读资源目录：打包后是 _MEIPASS（exe 内部解压目录），开发时是脚本目录。"""
    if getattr(sys, "frozen", False):
        return sys._MEIPASS  # PyInstaller 临时解压目录
    return os.path.dirname(os.path.abspath(__file__))


HERE = _get_frozen_base()       # exe/脚本所在目录（用户可见）
RES_DIR = _get_resource_dir()   # 资源目录（HTML 等）
ERR_LOG = os.path.join(HERE, "启动错误.log")


def _crash(exc_type, exc_value, exc_tb):
    msg = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    with open(ERR_LOG, "a", encoding="utf-8") as f:
        f.write(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}]\n{msg}\n")
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            0, "程序启动失败，详情见：\n" + ERR_LOG + "\n\n" + msg[:500],
            "HUST校园网助手 - 启动错误", 0x10)
    except Exception:
        pass


sys.excepthook = _crash

# ---- 3rd party ----
try:
    import webview
    from webview import Window
    from PIL import Image, ImageDraw
    from pystray import Icon, Menu, MenuItem
except ImportError as e:
    _crash(ImportError, f"缺少依赖：{getattr(e, 'name', e)}。请执行：  "
            f"pip install pywebview pystray pillow requests pycryptodome",
            e.__traceback__)
    sys.exit(1)

# ---- 本项目：登录核心逻辑完全复用 ----
try:
    import hust_login as core
except Exception:
    _crash(*sys.exc_info())
    sys.exit(1)


CFG_PATH = os.path.join(HERE, "config.ini")  # config.ini 放 exe 同级，用户可改
_instance_lock = None  # 单实例锁 socket（全局持有，防 GC）
APP_NAME = "HUST校园网重连助手"
config_write_lock = threading.Lock()


def _ensure_config():
    """首次运行时，从 config.example.ini 复制一份 config.ini。"""
    if os.path.isfile(CFG_PATH):
        return
    example = os.path.join(RES_DIR, "config.example.ini")
    if not os.path.isfile(example):
        example = os.path.join(HERE, "config.example.ini")
    if os.path.isfile(example):
        import shutil
        try:
            shutil.copy(example, CFG_PATH)
        except Exception:
            pass


_ensure_config()

# 注册表开机自启
AUTORUN_KEY = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run"
AUTORUN_NAME = "HUST_AutoLogin"


# ============================================================
#  状态 + 日志收集（前端通过 Api 拉取）
# ============================================================
class AppState:
    def __init__(self):
        self.online = None           # True/False/None
        self.last_check = None
        self.last_login = None
        self.lock = threading.Lock()
        self.logs = deque(maxlen=300)
        self._log_counter = 0

    def add_log(self, level, msg, ts):
        with self.lock:
            self._log_counter += 1
            self.logs.append({
                "idx": self._log_counter, "ts": ts,
                "level": level, "msg": msg,
            })

    def logs_since(self, since):
        with self.lock:
            return [dict(x) for x in self.logs if x["idx"] > since]

    def to_status_dict(self):
        cfg = core.load_config(CFG_PATH)
        return {
            "online": self.online,
            "last_check": self.last_check,
            "last_login": self.last_login,
            "auto_running": keepalive.running,
            "username": cfg.get("account", "username").strip(),
            "interval_minutes": cfg.getint("check", "interval_minutes", fallback=10),
        }


state = AppState()


def sync_dynamic_server(cfg):
    """将本次认证页动态发现的服务器地址写回配置。"""
    origin = core.last_auth_origin
    if not origin:
        return
    parsed = urlparse(origin)
    if not parsed.scheme or not parsed.netloc:
        return
    host = parsed.netloc
    use_https = parsed.scheme == "https"
    current_host = cfg.get("server", "host", fallback="")
    current_https = cfg.getboolean("server", "use_https", fallback=False)
    if current_host == host and current_https == use_https:
        return
    cfg["server"] = {"host": host, "use_https": "true" if use_https else "false"}
    with config_write_lock:
        with open(CFG_PATH, "w", encoding="utf-8") as f:
            cfg.write(f)
    core.logger.info("认证服务器已动态更新为：%s", origin)


class _SinkHandler(logging.Handler):
    """把 core.logger 的日志同步进内存缓冲。"""
    def emit(self, record):
        ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        state.add_log(record.levelname, record.getMessage(), ts)


core.logger.addHandler(_SinkHandler())


# ============================================================
#  保活线程（逻辑与 tray.py 一致，循环调 core.run_once）
# ============================================================
class Keepalive:
    def __init__(self):
        self.stop_event = threading.Event()
        self.thread = None

    @property
    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self):
        if self.running:
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        core.logger.info("自动保活已启动。")

    def stop(self):
        self.stop_event.set()
        core.logger.info("自动保活已请求停止。")

    def _loop(self):
        prev_online = None
        # 启动立即检查一次
        self._do_check(prev_online)

        while not self.stop_event.is_set():
            cfg = core.load_config(CFG_PATH)
            interval = cfg.getint("check", "interval_minutes", fallback=10)
            if self.stop_event.wait(interval * 60):
                break
            prev_online = self._do_check(prev_online)

    def _do_check(self, prev_online):
        core.logger.info("==== 执行连通性检查 ====")
        try:
            cfg = core.load_config(CFG_PATH)
            ok = core.run_once(cfg)
            sync_dynamic_server(cfg)
        except Exception as e:
            core.logger.error("检查过程异常：%s", e)
            ok = False
        state.online = ok
        state.last_check = core.now_str()
        if ok and prev_online in (False, None):
            state.last_login = core.now_str()
        # 通知前端刷新（如果窗口开着）
        _notify_window("status_changed", state.to_status_dict())
        return ok


keepalive = Keepalive()


# ============================================================
#  开机自启 —— 用 winreg 操作注册表（不调子进程，彻底避免死锁）
# ============================================================
try:
    import winreg
    _HAS_WINREG = True
except ImportError:
    _HAS_WINREG = False


_RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _get_self_cmd_for_autorun():
    """返回开机自启要执行的命令行。
    打包后：直接启动 exe（sys.executable 就是 华科校园网.exe）。
    开发时：用 pythonw 静默启动 webview_app.py。
    """
    if getattr(sys, "frozen", False):
        # 打包后：sys.executable 是 exe 本身，直接启动它（HUST校园网助手.exe）
        return f'"{sys.executable}"'
    # 开发时：用 pythonw 静默启动脚本
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        exe = exe[:-len("python.exe")] + "pythonw.exe"
    return f'"{exe}" "{os.path.join(HERE, "webview_app.py")}"'


def is_autorun_enabled():
    """检测注册表里有没有自启项。优先用 winreg（纯 Python，无线程问题）。"""
    if _HAS_WINREG:
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY_PATH, 0, winreg.KEY_READ)
            try:
                winreg.QueryValueEx(key, AUTORUN_NAME)
                return True
            finally:
                winreg.CloseKey(key)
        except FileNotFoundError:
            return False
        except OSError:
            return False
    # 回退：subprocess（已重定向所有流，避免死锁）
    return _autorun_via_subprocess("query") is not None


def enable_autorun():
    cmd = _get_self_cmd_for_autorun()
    if _HAS_WINREG:
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE)
            try:
                winreg.SetValueEx(key, AUTORUN_NAME, 0, winreg.REG_SZ, cmd)
            finally:
                winreg.CloseKey(key)
            core.logger.info("已设置开机自启。")
            return True
        except OSError as e:
            core.logger.error("设置自启失败（winreg）：%s", e)
            # 回退到 subprocess
    ok = _autorun_via_subprocess("add", cmd)
    if ok:
        core.logger.info("已设置开机自启（subprocess）。")
    else:
        core.logger.error("设置自启失败。")
    return ok


def disable_autorun():
    if _HAS_WINREG:
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE)
            try:
                winreg.DeleteValue(key, AUTORUN_NAME)
            finally:
                winreg.CloseKey(key)
            core.logger.info("已取消开机自启。")
            return True
        except FileNotFoundError:
            return True  # 本来就没有，视为成功
        except OSError as e:
            core.logger.error("取消自启失败（winreg）：%s", e)
    ok = _autorun_via_subprocess("delete")
    return ok


# subprocess 回退方案：所有流都显式重定向 + timeout，杜绝 PyWebView 线程死锁
def _autorun_via_subprocess(action, value=None):
    import subprocess
    CREATE_NO_WINDOW = 0x08000000
    args = ["reg"]
    if action == "query":
        args += ["query", AUTORUN_KEY, "/v", AUTORUN_NAME]
    elif action == "add":
        args += ["add", AUTORUN_KEY, "/v", AUTORUN_NAME, "/t", "REG_SZ", "/d", value, "/f"]
    elif action == "delete":
        args += ["delete", AUTORUN_KEY, "/v", AUTORUN_NAME, "/f"]
    else:
        return None
    try:
        r = subprocess.run(
            args,
            stdin=subprocess.DEVNULL,       # 关键：不继承 PyWebView 的 stdin
            stdout=subprocess.PIPE,         # 关键：捕获而非继承
            stderr=subprocess.PIPE,
            timeout=8,                      # 关键：兜底，绝不无限等待
            creationflags=CREATE_NO_WINDOW,
        )
        if action == "query":
            return r.stdout.decode("gbk", "ignore") if r.returncode == 0 else None
        return r.returncode == 0
    except subprocess.TimeoutExpired:
        core.logger.error("reg %s 超时。", action)
        return None if action == "query" else False
    except Exception as e:
        core.logger.error("reg %s 异常：%s", action, e)
        return None if action == "query" else False


# ============================================================
#  图标（绿/红/灰）
# ============================================================
def make_icon(color):
    palette = {"green": "#10b981", "red": "#ef4444", "gray": "#9ca3af"}
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c = palette.get(color, palette["gray"])
    d.ellipse((6, 6, 58, 58), fill=c + "33")
    d.ellipse((14, 14, 50, 50), fill=c)
    try:
        d.text((26, 18), "H", fill="white")
    except Exception:
        pass
    return img


# ============================================================
#  JS Bridge —— 前端通过 pywebview.api.xxx() 调用这些方法
# ============================================================
class Api:
    """暴露给前端 JS 的接口。所有方法返回 dict/list/str/数字/bool。"""

    def __init__(self):
        self._last_click = {}
        self._debounce_ms = 500

    def _debounce(self, key):
        now = time.monotonic() * 1000
        last = self._last_click.get(key, 0)
        if now - last < self._debounce_ms:
            return False
        self._last_click[key] = now
        return True

    # ---- 状态 ----
    def get_status(self):
        return state.to_status_dict()

    # ---- 配置 ----
    def get_config(self):
        cfg = core.load_config(CFG_PATH)
        return {
            "username": cfg.get("account", "username").strip(),
            "password": cfg.get("account", "password"),
            "host": cfg.get("server", "host", fallback="192.168.170.168"),
            "use_https": cfg.getboolean("server", "use_https", fallback=False),
            "probe_url": cfg.get("network", "probe_url", fallback="http://www.baidu.com"),
            "probe_keyword": cfg.get("network", "probe_keyword", fallback=""),
            "interval_minutes": cfg.getint("check", "interval_minutes", fallback=10),
            "max_retries": cfg.getint("check", "max_retries", fallback=3),
            "retry_delay": cfg.getint("check", "retry_delay", fallback=5),
            "service": cfg.get("service", "service", fallback=""),
            "verbose": cfg.getboolean("log", "verbose", fallback=True),
        }

    def save_config(self, data):
        """前端传 dict 过来。PyWebView 会把 JS 对象转成 dict。"""
        import configparser
        current = core.load_config(CFG_PATH)
        cfg = configparser.ConfigParser()
        cfg["account"] = {
            "username": data.get("username", ""),
            "password": data.get("password", ""),
        }
        cfg["server"] = {
            "host": data.get("host", current.get("server", "host", fallback="")),
            "use_https": "true" if data.get("use_https", current.getboolean("server", "use_https", fallback=False)) else "false",
        }
        cfg["network"] = {
            "probe_url": data.get("probe_url", current.get("network", "probe_url", fallback="http://www.baidu.com")),
            "probe_keyword": data.get("probe_keyword", ""),
        }
        cfg["check"] = {
            "interval_minutes": str(data.get("interval_minutes", 10)),
            "max_retries": str(data.get("max_retries", 3)),
            "retry_delay": str(data.get("retry_delay", 5)),
        }
        cfg["service"] = {"service": data.get("service", "")}
        cfg["log"] = {"verbose": "true" if data.get("verbose", True) else "false"}
        with config_write_lock:
            with open(CFG_PATH, "w", encoding="utf-8") as f:
                cfg.write(f)
        core.logger.info("配置已保存。")
        return {"ok": True}

    # ---- 操作 ----
    def check_now(self):
        """手动触发一次检查+登录。同步返回结果。"""
        if not self._debounce("check_now"):
            return {"ok": state.online is True, "online": state.online,
                    "msg": "操作太频繁，已忽略"}
        core.logger.info("（手动）触发检查 …")
        prev = state.online
        try:
            cfg = core.load_config(CFG_PATH)
            ok = core.run_once(cfg)
            sync_dynamic_server(cfg)
        except Exception as e:
            core.logger.error("手动检查异常：%s", e)
            ok = False
        state.online = ok
        state.last_check = core.now_str()
        if ok and prev in (False, None):
            state.last_login = core.now_str()
        _notify_window("status_changed", state.to_status_dict())
        return {"ok": ok, "online": ok}

    def disconnect_now(self):
        """主动断开认证，同时停止保活，避免随后自动重新登录。"""
        if not self._debounce("disconnect_now"):
            return {"ok": False, "msg": "操作太频繁，已忽略"}
        keepalive.stop()
        core.logger.info("（手动）请求断开校园网认证 …")
        try:
            cfg = core.load_config(CFG_PATH)
            with core.connection_lock:
                ok = core.logout(cfg)
        except Exception as e:
            core.logger.error("断开认证异常：%s", e)
            ok = False
        state.online = False if ok else state.online
        state.last_check = core.now_str()
        _notify_window("status_changed", state.to_status_dict())
        return {"ok": ok, "online": state.online}

    def start_keepalive(self):
        if not self._debounce("keep"):
            return {"ok": True, "running": keepalive.running}
        keepalive.start()
        _notify_window("status_changed", state.to_status_dict())
        return {"ok": True, "running": True}

    def stop_keepalive(self):
        if not self._debounce("keep"):
            return {"ok": True, "running": keepalive.running}
        keepalive.stop()
        _notify_window("status_changed", state.to_status_dict())
        return {"ok": True, "running": False}

    # ---- 开机自启 ----
    def get_autorun(self):
        return {"enabled": is_autorun_enabled()}

    def set_autorun(self, enabled):
        """切换开机自启。立即返回，真正的注册表操作丢到子线程，绝不卡 UI。"""
        def _bg():
            if enabled:
                enable_autorun()
            else:
                disable_autorun()
            _notify_window("status_changed", state.to_status_dict())
        threading.Thread(target=_bg, daemon=True).start()
        return {"enabled": enabled, "pending": True}

    # ---- 日志 ----
    def get_logs(self, since=0):
        return state.logs_since(int(since))

    # ---- 窗口控制 ----
    def hide_window(self):
        """前端"最小化到托盘"按钮调用。"""
        if _main_window:
            _main_window.hide()
        return {"ok": True}

    def open_self_service(self):
        """使用系统浏览器打开华科网络自助服务。"""
        webbrowser.open("http://myself.hust.edu.cn")
        return {"ok": True}


# ============================================================
#  窗口引用 + 给前端推送消息
# ============================================================
_main_window: Window = None


def _notify_window(event_name, payload):
    """向当前窗口推送事件（前端通过 window pywebview RPC 监听）。

    PyWebView 没有原生的服务端推送，用 evaluate_js 注入一段 JS 触发前端回调。
    """
    global _main_window
    if _main_window is None:
        return
    try:
        import json as _json
        js = ("window.__hustDispatch && window.__hustDispatch("
              f"{_json.dumps(event_name)}, {_json.dumps(payload)});")
        _main_window.evaluate_js(js)
    except Exception:
        pass  # 窗口可能正忙/已隐藏，忽略


# ============================================================
#  托盘（独立线程，与 PyWebView 主线程并行）
# ============================================================
class TrayController:
    def __init__(self):
        self.icon = None

    def _menu(self):
        # pystray 约定：
        #   - text= / checked= 的 callable 接收单参数 (item, 菜单项自身)
        #   - 点击回调 action 接收两参数 (icon, item)
        return Menu(
            MenuItem("显示主窗口", self.on_show_window, default=True),
            MenuItem("立即检查并登录", self.on_check_now),
            Menu.SEPARATOR,
            MenuItem(
                lambda item: f"自动保活：{'运行中' if keepalive.running else '已停止'}",
                None),
            MenuItem("启动自动保活", lambda icon, item: keepalive.start()),
            MenuItem("停止自动保活", lambda icon, item: keepalive.stop()),
            Menu.SEPARATOR,
            MenuItem("开机自启动",
                     self.on_toggle_autorun,
                     checked=lambda item: is_autorun_enabled()),
            Menu.SEPARATOR,
            MenuItem("退出", self.on_quit),
        )

    def start(self):
        self.icon = Icon(APP_NAME, icon=make_icon("gray"),
                         title=APP_NAME, menu=self._menu())
        threading.Thread(target=self.icon.run, daemon=True).start()

    def refresh(self):
        if not self.icon:
            return
        online = state.online
        color = "green" if online is True else ("red" if online is False else "gray")
        try:
            self.icon.icon = make_icon(color)
            self.icon.update_menu()
        except Exception:
            pass

    def on_show_window(self, icon=None, item=None):
        global _main_window
        if _main_window:
            try:
                _main_window.show()
            except Exception:
                pass

    def on_check_now(self, icon=None, item=None):
        threading.Thread(target=api.check_now, daemon=True).start()

    def on_toggle_autorun(self, icon=None, item=None):
        # 异步执行，避免在托盘回调线程里做阻塞操作
        def _bg():
            if is_autorun_enabled():
                disable_autorun()
            else:
                enable_autorun()
            if self.icon:
                try:
                    self.icon.update_menu()
                except Exception:
                    pass
        threading.Thread(target=_bg, daemon=True).start()

    def on_quit(self, *_):
        global _should_quit
        core.logger.info("通过托盘退出程序。")
        _should_quit = True  # 放行 closing 事件
        keepalive.stop()
        # 先停托盘
        if self.icon:
            try:
                self.icon.stop()
            except Exception:
                pass
        # 再销毁窗口（会触发 closing，因 _should_quit=True 故放行）
        if _main_window:
            try:
                _main_window.destroy()
            except Exception:
                pass
        # 兜底：0.5 秒后强制退出，避免任何残留线程卡住
        def _force():
            time.sleep(0.5)
            os._exit(0)
        threading.Thread(target=_force, daemon=True).start()


api = Api()
tray = TrayController()


# ============================================================
#  窗口关闭拦截：点 X → 隐藏到托盘（绝不退出）；托盘"退出"才真退出
# ============================================================
# 关键：用标志位区分「用户点 X」（隐藏）和「托盘退出」（真退出）。
# EdgeChromium 后端的 closing 事件 cancel 机制不可靠（会触发 Chromium
# 窗口类注销崩溃），所以点 X 时我们：1) 设 args.Cancel=True 阻止 FormClosing
# 2) 调 hide() 隐藏窗口。真正退出走 destroy() + icon.stop()。
_should_quit = False


def _on_closing():
    """点窗口 X 时触发。返回 False → PyWebView 取消关闭（args.Cancel=True）。"""
    global _should_quit
    if _should_quit:
        # 托盘点了"退出"，放行关闭
        core.logger.info("收到退出指令，正在关闭窗口 …")
        return True
    # 用户点 X：只隐藏，不退出
    core.logger.info("窗口已最小化到托盘，后台保活继续运行。")
    try:
        if _main_window:
            _main_window.hide()
    except Exception as e:
        core.logger.error("隐藏窗口失败：%s", e)
    return False  # 取消关闭


# ============================================================
#  主程序
# ============================================================
def main():
    global _main_window

    core.logger.info("=" * 50)
    core.logger.info("HUST校园网助手启动")
    core.logger.info("Python: %s", sys.version.split()[0])

    # 启动保活
    keepalive.start()

    # 启动托盘
    tray.start()
    # 后台定时刷新托盘图标颜色
    def _watchdog():
        while True:
            time.sleep(15)
            tray.refresh()
    threading.Thread(target=_watchdog, daemon=True).start()

    # 创建主窗口
    html_path = os.path.join(RES_DIR, "webview_index.html")
    _main_window = webview.create_window(
        title=APP_NAME,
        url=html_path,
        width=920,
        height=780,
        min_size=(720, 600),
        text_select=False,
        js_api=api,
        frameless=True,
        easy_drag=False,
        # 关闭时隐藏到托盘
        on_top=False,
    )

    # 拦截关闭事件：点 X → 隐藏到托盘
    _main_window.events.closing += _on_closing

    # 启动一个非 daemon 的"哨兵"线程：只要它活着，进程就不会退出。
    # 这样即使 webview.start() 因窗口关闭而返回，进程仍由托盘+保活维持。
    def _sentinel():
        while not _should_quit:
            time.sleep(1)

    threading.Thread(target=_sentinel, daemon=False).start()

    # PyWebView 必须在主线程运行（Windows 要求）。
    # webview.start() 在窗口关闭后会返回，但因为哨兵线程还活着，进程继续。
    webview.start(debug=False)

    # webview.start() 返回后（窗口被关闭/destroy），若用户不是要退出，
    # 就在这里阻塞等托盘的"退出"指令。托盘退出会设 _should_quit=True 并 os._exit。
    core.logger.info("主窗口已关闭，程序转入托盘后台模式。")
    while not _should_quit:
        time.sleep(1)
    core.logger.info("正在退出程序 …")
    os._exit(0)


if __name__ == "__main__":
    # 单实例锁（模块级变量 _instance_lock 持有，防止 GC 释放端口）
    import socket as _sock
    _instance_lock = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
    try:
        _instance_lock.bind(("127.0.0.1", 5150))
    except OSError:
        # 已有实例在跑，提示并退出
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0, "HUST校园网助手已经在运行了（托盘中）。",
                APP_NAME, 0x40)
        except Exception:
            pass
        sys.exit(0)
    _instance_lock.listen(1)

    main()

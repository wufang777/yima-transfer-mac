#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""易码互传 · Windows 版 —— 局域网文件互传（托盘 + 网页控制台）。

与 macOS 版协议完全对齐（互传的前提）：
  - HTTP 服务：端口 8000，端点与 Mac 版一致（/upload /files /download /share /api/peers ...）
  - UDP 发现：端口 58887，广播 {"app":"LanFileTransfer","id","name","port"}，3 秒一次
  - 手机端与电脑端页面复用仓库根目录的 index.html / console.html / settings.html

Windows 专属：
  - 托盘：pystray（打开控制台 / 接收目录 / 二维码 / 重启 / 退出）
  - 通知：托盘气泡
  - 开机自启：启动文件夹快捷方式（无需管理员权限）
  - 选文件对话框：tkinter（浏览器拿不到真实路径，与 Mac 的 osascript 同理）

命令行：
  python yima_win.py                # 正常启动（托盘）
  python yima_win.py --headless     # 无托盘运行（开发/调试/CI 冒烟）
  python yima_win.py --port 8010    # 覆盖端口
  python yima_win.py --install-autostart / --uninstall-autostart
  python yima_win.py --version
"""
import os
import sys
import json
import socket
import ctypes
import subprocess
import threading
import time
import mimetypes
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

IS_WIN = (os.name == "nt")
IS_MAC = (sys.platform == "darwin")

# ---- 品牌与版本（与 Mac 版保持同一口径）----
APP_NAME = "易码互传"
APP_COMPANY = "易码通科技"
APP_VERSION_NUM = "1.0.0"
APP_BUILD = "20260914"
APP_CHANNEL = "stable"
APP_RELEASE_DATE = "%s-%s-%s" % (APP_BUILD[0:4], APP_BUILD[4:6], APP_BUILD[6:8])
APP_VERSION = "v%s" % APP_VERSION_NUM
APP_VERSION_FULL = "v%s (%s)" % (APP_VERSION_NUM, APP_BUILD)
APP_VERSION_LINE = "%s · 构建 %s" % (APP_VERSION, APP_BUILD)
APP_TAGLINE = "%s · Windows 版" % APP_NAME
SERVER_VERSION = "YimaTransfer-Win/1.0"

LOG_FILENAME = "易码互传.log"
QR_FILENAME = "易码互传_连接二维码.png"

# ---- 目录与配置 ----
if getattr(sys, "frozen", False):
    BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    APP_DIR = Path(os.environ.get("APPDATA") or Path.home()) / APP_NAME
    APP_DIR.mkdir(parents=True, exist_ok=True)
else:
    BASE_DIR = Path(__file__).resolve().parent.parent   # 仓库根目录（网页在这里）
    APP_DIR = Path(__file__).resolve().parent

CONFIG_PATH = APP_DIR / "config.json"
DEFAULTS = {
    "port": 8000,
    "receive_dir": str(Path.home() / "Downloads" / APP_NAME),
    "autostart": False,
    "notify_on_receive": True,
    "open_folder_on_receive": False,
    "open_console_on_start": True,
}


def load_config():
    cfg = dict(DEFAULTS)
    try:
        if CONFIG_PATH.exists():
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except Exception:
        pass
    return cfg


def save_config(cfg):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


CONFIG = load_config()


def _as_int(v, dflt):
    try:
        return int(v)
    except Exception:
        return dflt


PORT = _as_int(CONFIG.get("port"), 8000) or 8000


def resolve_receive_dir(raw):
    p = Path(os.path.expandvars(str(raw))).expanduser()
    if not p.is_absolute():
        p = Path.home() / "Downloads" / p
    return p


RECEIVE_DIR = resolve_receive_dir(CONFIG.get("receive_dir") or DEFAULTS["receive_dir"])
try:
    RECEIVE_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    pass

CONSOLE = BASE_DIR / "console.html"
INDEX = BASE_DIR / "index.html"
SETTINGS = BASE_DIR / "settings.html"
ICON_PNG = BASE_DIR / "assets" / "icon.png"
LOGO_PNG = BASE_DIR / "assets" / "logo.png"

CONNECT_URL = "http://localhost:%d" % PORT

# ---- PC→手机 共享文件表 ----
SHARE_LOCK = threading.Lock()
SHARED = {}

# ---- 局域网设备发现（与 Mac 版同一协议）----
DISCOVERY_PORT = 58887
PEER_TIMEOUT = 12
INSTANCE_ID = uuid.uuid4().hex
PEER_LOCK = threading.Lock()
PEERS = {}

TRAY = [None]   # pystray Icon 引用（通知用）


def _log(msg):
    try:
        with open(APP_DIR / LOG_FILENAME, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def _fmt_size(n):
    n = int(n)
    if n < 1024:
        return "%d B" % n
    if n < 1048576:
        return "%.1f KB" % (n / 1024)
    if n < 1073741824:
        return "%.1f MB" % (n / 1048576)
    return "%.2f GB" % (n / 1073741824)


# ---------- 系统交互（Windows 优先，Mac 仅用于开发调试）----------
def _open_path(p):
    try:
        if IS_WIN:
            os.startfile(str(p))                       # noqa
        elif IS_MAC:
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
    except Exception:
        pass


def _copy_to_clipboard(text):
    text = str(text or "")
    try:
        if IS_WIN:
            CF_UNICODETEXT = 13
            GMEM_MOVEABLE = 0x0002
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            if not user32.OpenClipboard(0):
                return False
            try:
                user32.EmptyClipboard()
                data = text.encode("utf-16-le") + b"\x00\x00"
                h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
                if not h:
                    return False
                lp = kernel32.GlobalLock(h)
                if not lp:
                    return False
                ctypes.memmove(lp, data, len(data))
                kernel32.GlobalUnlock(h)
                user32.SetClipboardData(CF_UNICODETEXT, h)
                return True
            finally:
                user32.CloseClipboard()
        elif IS_MAC:
            subprocess.run(["pbcopy"], input=text.encode("utf-8"),
                           capture_output=True, timeout=5)
            return True
    except Exception:
        pass
    return False


def _notify(title, msg):
    """系统通知：优先托盘气泡（Windows），开发机上用 osascript。"""
    icon = TRAY[0]
    if icon is not None:
        try:
            icon.notify(msg, title)
            return
        except Exception:
            pass
    if IS_MAC:
        try:
            def _esc(s):
                return str(s).replace("\\", "").replace('"', "'")
            subprocess.Popen([
                "osascript", "-e",
                'display notification "%s" with title "%s"'
                % (_esc(msg), _esc(title)),
            ])
        except Exception:
            pass


def _reveal_in_folder(path: Path):
    try:
        if IS_WIN:
            subprocess.Popen(["explorer", "/select,", str(path)])
        elif IS_MAC:
            subprocess.Popen(["open", "-R", str(path)])
        else:
            _open_path(path.parent)
    except Exception:
        pass


# ---------- 选文件对话框（专用线程持有一个隐藏 Tk root）----------
class _DialogRunner:
    """tkinter 不允许跨线程混用，专设一条 UI 线程排队执行对话框任务。"""

    def __init__(self):
        self._queue = []
        self._lock = threading.Lock()
        self._started = False
        self._ready = False
        self._tk = None

    def _loop(self):
        try:
            import tkinter as tk
            from tkinter import filedialog
        except Exception as e:
            _log("[对话框] tkinter 不可用: %s" % e)
            self._tk = None
            self._ready = True
            return
        self._tk = tk.Tk()
        self._tk.withdraw()
        self._fd = filedialog
        self._ready = True
        while True:
            job = None
            with self._lock:
                if self._queue:
                    job = self._queue.pop(0)
            if job is None:
                try:
                    self._tk.update()
                except Exception:
                    pass
                time.sleep(0.05)
                continue
            fn, result_box = job
            try:
                result_box["value"] = fn(self._tk)
            except Exception as e:
                result_box["value"] = ("__error__", str(e))

    def start(self):
        if self._started:
            return
        self._started = True
        self._ready = False
        threading.Thread(target=self._loop, daemon=True).start()
        for _ in range(100):          # 最多等 5 秒让 UI 线程完成初始化
            if getattr(self, "_ready", False):
                break
            time.sleep(0.05)

    def run(self, fn, timeout=600):
        self.start()
        if getattr(self, "_tk", None) is None:
            return None, "tkinter 不可用"
        box = {}
        with self._lock:
            self._queue.append((fn, box))
        deadline = time.time() + timeout
        while time.time() < deadline:
            if "value" in box:
                v = box["value"]
                if isinstance(v, tuple) and v and v[0] == "__error__":
                    return None, v[1]
                return v, None
            time.sleep(0.05)
        return None, "选择超时"


_DIALOG = _DialogRunner()


def _pick_files(folder_mode=False):
    """弹出选文件/文件夹对话框，返回 (paths, err)。"""
    def _job(tk):
        if folder_mode:
            p = _DIALOG._fd.askdirectory(parent=tk)
            return [p] if p else []
        ps = _DIALOG._fd.askopenfilenames(parent=tk)
        return list(ps)

    return _DIALOG.run(_job)


# ---------- 局域网 IP ----------
def _bad_lan_ip(ip):
    return (not ip) or ip.startswith("127.") or ip.startswith("169.254.") \
        or ip.startswith("0.") or ip == "::1"


def get_lan_ip():
    """UDP 连接探测本机局域网 IP（Windows 上足够可靠）。"""
    for probe in ("8.8.8.8", "223.5.5.5"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect((probe, 80))
                ip = s.getsockname()[0]
                if not _bad_lan_ip(ip):
                    return ip
            finally:
                s.close()
        except Exception:
            continue
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if not _bad_lan_ip(ip):
            return ip
    except Exception:
        pass
    return "127.0.0.1"


def local_ip_set():
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            ip = info[4][0]
            if "." in ip:
                ips.add(ip)
    except Exception:
        pass
    ips.add(get_lan_ip())
    ips.update({"127.0.0.1", "::1", "::ffff:127.0.0.1"})
    return ips


def rebuild_connect_url():
    global CONNECT_URL
    CONNECT_URL = "http://%s:%d" % (get_lan_ip(), PORT)
    return CONNECT_URL


def common_dirs():
    home = Path.home()
    return {
        "desktop": str(home / "Desktop"),
        "downloads": str(home / "Downloads"),
        "documents": str(home / "Documents"),
        "received": str(RECEIVE_DIR),
    }


def wechat_receive_dirs():
    """Windows 版暂不探测微信目录（后续可接 %USERPROFILE%\\Documents\\WeChat Files）。"""
    return []


# ---------- 局域网设备发现 ----------
def _discovery_beacon():
    msg = json.dumps({
        "app": "LanFileTransfer",
        "id": INSTANCE_ID,
        "name": socket.gethostname(),
        "port": PORT,
    }).encode("utf-8")
    while True:
        for target in ("255.255.255.255", "<broadcast>"):
            s = None
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.settimeout(1.0)
                s.sendto(msg, (target, DISCOVERY_PORT))
            except Exception:
                pass
            finally:
                try:
                    if s:
                        s.close()
                except Exception:
                    pass
        time.sleep(3)


def _discovery_listen():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # SO_REUSEPORT：同机多实例调试时让每个实例都能收到广播（Windows 无此选项）
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except Exception:
                pass
        s.bind(("0.0.0.0", DISCOVERY_PORT))
    except Exception as e:
        _log("[发现] 监听 %d 失败: %s" % (DISCOVERY_PORT, e))
        return
    s.settimeout(2.0)
    last_prune = 0.0
    while True:
        try:
            data, addr = s.recvfrom(4096)
        except Exception:
            data = None
        now = time.time()
        if data:
            try:
                info = json.loads(data.decode("utf-8"))
            except Exception:
                info = None
            if isinstance(info, dict) and info.get("app") == "LanFileTransfer" \
                    and info.get("id") and info["id"] != INSTANCE_ID:
                try:
                    port = int(info.get("port", 0))
                except Exception:
                    port = 0
                if 1 <= port <= 65535:
                    with PEER_LOCK:
                        PEERS[info["id"]] = {
                            "id": info["id"],
                            "name": str(info.get("name") or "未知设备"),
                            "ip": addr[0],
                            "port": port,
                            "last_seen": now,
                        }
        if now - last_prune >= 3:
            last_prune = now
            with PEER_LOCK:
                for pid in [p for p, v in PEERS.items()
                            if now - v.get("last_seen", 0) > PEER_TIMEOUT]:
                    PEERS.pop(pid, None)


def start_discovery():
    threading.Thread(target=_discovery_beacon, daemon=True).start()
    threading.Thread(target=_discovery_listen, daemon=True).start()


def list_peers():
    with PEER_LOCK:
        return [
            {"id": v["id"], "name": v["name"], "ip": v["ip"], "port": v["port"]}
            for v in sorted(PEERS.values(), key=lambda x: (x["name"], x["ip"]))
        ]


# ---------- 开机自启（启动文件夹快捷方式，无需管理员）----------
def _startup_lnk():
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / ("%s.lnk" % APP_NAME)


def is_autostart_installed():
    if IS_WIN:
        return _startup_lnk().exists()
    # 开发机（mac）上按 Mac 版的 LaunchAgents 判断，避免设置页状态错乱
    return (Path.home() / "Library" / "LaunchAgents" / "com.yima.transfer.plist").exists()


def install_autostart():
    if not IS_WIN:
        _log("[自启] 非 Windows 环境跳过")
        return
    lnk = _startup_lnk()
    lnk.parent.mkdir(parents=True, exist_ok=True)
    if getattr(sys, "frozen", False):
        target = sys.executable
    else:
        target = os.path.abspath(sys.argv[0])
    ps = (
        "$ws = New-Object -ComObject WScript.Shell; "
        "$s = $ws.CreateShortcut('%s'); "
        "$s.TargetPath = '%s'; "
        "$s.WorkingDirectory = '%s'; "
        "$s.Description = '%s 局域网文件互传'; "
        "$s.Save()" % (lnk, target, os.path.dirname(target), APP_NAME)
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                   capture_output=True, timeout=30)
    _log("[自启] 已安装: %s" % lnk)


def uninstall_autostart():
    if not IS_WIN:
        return
    lnk = _startup_lnk()
    if lnk.exists():
        try:
            lnk.unlink()
        except OSError:
            pass
    _log("[自启] 已取消")


# ---------- 二维码 ----------
try:
    import segno
    HAS_QR = True
except ImportError:
    HAS_QR = False


def save_qr_image(url):
    if not HAS_QR:
        return None
    try:
        qr_path = APP_DIR / QR_FILENAME
        segno.make(url, error="m").save(str(qr_path), scale=10, border=4)
        return qr_path
    except Exception:
        return None


# ---------- 收到文件 ----------
def _on_file_received(name, size):
    msg = "收到文件：%s（%s）" % (name, _fmt_size(size))
    _log("[接收] %s" % msg)
    if CONFIG.get("notify_on_receive", True):
        _notify(APP_NAME, msg)
    if CONFIG.get("open_folder_on_receive"):
        _open_path(RECEIVE_DIR)


def _restart_and_exit():
    try:
        if getattr(sys, "frozen", False):
            cmd = [sys.executable]
        else:
            cmd = [sys.executable, os.path.abspath(__file__)]
        if IS_WIN:
            subprocess.Popen(cmd, close_fds=True,
                             creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        else:
            subprocess.Popen(cmd)
        time.sleep(0.5)
    except Exception:
        pass
    os._exit(0)


def _safe_path(name: str):
    """基于文件名构造接收目录内的安全路径，杜绝路径穿越。"""
    name = os.path.basename(urllib.parse.unquote(name))
    if not name:
        return None
    candidate = (RECEIVE_DIR / name).resolve()
    try:
        candidate.relative_to(RECEIVE_DIR.resolve())
    except Exception:
        return None
    return candidate


# ---------- HTTP 服务（与 Mac 版同一套端点）----------
class Handler(BaseHTTPRequestHandler):
    server_version = SERVER_VERSION

    def _is_local(self):
        host = self.client_address[0]
        if host in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            return True
        return host in local_ip_set()

    def _deny_remote(self):
        self._json(403, {"error": "该接口仅限本机访问"})

    def _json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, path: Path):
        if path.exists():
            html = path.read_text(encoding="utf-8").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
        else:
            self._json(404, {"error": "%s not found" % path.name})

    def _send_file(self, path: Path, inline: bool = False):
        size = path.stat().st_size
        ascii_name = path.name if path.name.isascii() else "file"
        action = "inline" if inline else "attachment"
        disp = '%s; filename="%s"; filename*=UTF-8\'\'%s' % (
            action, ascii_name, urllib.parse.quote(path.name))
        if inline:
            ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        else:
            ctype = "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", disp)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b"{}"
            return json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            return {}

    # ---- GET ----
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/":
            if self._is_local():
                self._send_html(CONSOLE)
            else:
                self._send_html(INDEX)
            return

        if path == "/console":
            if self._is_local():
                self._send_html(CONSOLE)
            else:
                self._deny_remote()
            return

        if path in ("/phone", "/index.html"):
            self._send_html(INDEX)
            return

        if path in ("/settings", "/settings.html"):
            self._send_html(SETTINGS)
            return

        if path == "/files":
            files = []
            try:
                for f in sorted(RECEIVE_DIR.iterdir()):
                    if f.is_file():
                        files.append({
                            "name": f.name,
                            "size": f.stat().st_size,
                            "mtime": int(f.stat().st_mtime),
                        })
            except Exception:
                pass
            self._json(200, files)
            return

        if path == "/api/shared":
            with SHARE_LOCK:
                items = [
                    {"id": sid, "name": it["name"], "size": it["size"],
                     "mtime": it.get("mtime", 0), "folder": it.get("folder", ""),
                     "path": str(it.get("path", ""))}
                    for sid, it in SHARED.items()
                ]
            items.sort(key=lambda x: -x.get("mtime", 0))
            self._json(200, items)
            return

        if path == "/api/peers":
            self._json(200, list_peers())
            return

        if path.startswith("/share/"):
            sid = path[len("/share/"):]
            with SHARE_LOCK:
                it = SHARED.get(sid)
                fp = it["path"] if it else None
            if it and fp is not None and Path(fp).is_file():
                self._send_file(Path(fp),
                                inline=parsed.query in ("inline=1", "open=1"))
            else:
                self._json(404, {"error": "文件不存在或已被取消分享"})
            return

        if path == "/info":
            self._json(200, {"url": CONNECT_URL, "port": PORT,
                             "dir": str(RECEIVE_DIR)})
            return

        if path == "/api/version":
            self._json(200, {
                "app": APP_NAME,
                "version": APP_VERSION_NUM,
                "build": APP_BUILD,
                "release_date": APP_RELEASE_DATE,
                "channel": APP_CHANNEL,
                "version_line": APP_VERSION_LINE,
                "company": APP_COMPANY,
                "platform": "windows",
            })
            return

        if path == "/api/local/update":
            self._json(200, {"phase": "idle", "message": "", "percent": 0,
                             "manifest": None, "error": ""})
            return

        if path == "/api/local/status":
            if not self._is_local():
                self._deny_remote()
                return
            self._json(200, {
                "url": CONNECT_URL,
                "ip": get_lan_ip(),
                "port": PORT,
                "dir": str(RECEIVE_DIR),
                "autostart": is_autostart_installed(),
                "notify_on_receive": bool(CONFIG.get("notify_on_receive", True)),
                "open_folder_on_receive": bool(CONFIG.get("open_folder_on_receive", False)),
                "peers": list_peers(),
                "version": APP_VERSION_LINE,
                "version_num": APP_VERSION_NUM,
                "build": APP_BUILD,
                "release_date": APP_RELEASE_DATE,
                "update_phase": "idle",
            })
            return

        if path == "/api/settings":
            self._json(200, {
                "port": PORT,
                "receive_dir": str(RECEIVE_DIR),
                "autostart": is_autostart_installed(),
                "notify_on_receive": bool(CONFIG.get("notify_on_receive", True)),
                "open_folder_on_receive": bool(CONFIG.get("open_folder_on_receive", False)),
                "update_feed": "",
                "update_check_on_start": False,
                "connect_url": CONNECT_URL,
                "common_dirs": common_dirs(),
                "wechat_dirs": wechat_receive_dirs(),
                "version": APP_VERSION_NUM,
                "build": APP_BUILD,
                "release_date": APP_RELEASE_DATE,
                "version_line": APP_VERSION_LINE,
            })
            return

        if path == "/logo.png":
            try:
                data = LOGO_PNG.read_bytes()
            except Exception:
                self._json(404, {"error": "logo not found"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=300")
            self.end_headers()
            self.wfile.write(data)
            return

        if path == "/qr.png":
            if not HAS_QR:
                self._json(404, {"error": "segno not available"})
                return
            import io
            buf = io.BytesIO()
            qr = segno.make(CONNECT_URL, error="l")
            qr.save(buf, kind="png", scale=6, border=2)
            data = buf.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
            return

        if path.startswith("/download/"):
            safe = _safe_path(path[len("/download/"):])
            if safe and safe.is_file():
                self._send_file(safe, inline=parsed.query in ("inline=1", "open=1"))
            else:
                self._json(404, {"error": "not found"})
            return

        self._json(404, {"error": "not found"})

    # ---- POST ----
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        p = parsed.path

        if p == "/upload":
            self._handle_upload(parsed)
            return

        if p == "/api/share":
            if not self._is_local():
                self._deny_remote()
                return
            self._handle_share()
            return

        if p == "/api/settings":
            self._handle_save_settings()
            return

        if p == "/api/restart":
            self._json(200, {"ok": True})
            threading.Thread(target=_restart_and_exit, daemon=True).start()
            return

        if p == "/api/local/open-folder":
            if not self._is_local():
                self._deny_remote()
                return
            data = self._read_json_body()
            target = data.get("path") or str(RECEIVE_DIR)
            try:
                tp = Path(target).resolve()
                rec = str(RECEIVE_DIR.resolve())
                base = str(BASE_DIR.resolve())
                if str(tp).startswith(rec) or str(tp).startswith(base):
                    _open_path(tp)
                    self._json(200, {"ok": True})
                else:
                    self._json(403, {"error": "路径不在允许范围内"})
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        if p == "/api/local/reveal":
            if not self._is_local():
                self._deny_remote()
                return
            data = self._read_json_body()
            safe = None
            if data.get("name"):
                safe = _safe_path(str(data["name"]))
            elif data.get("path"):
                try:
                    tp = Path(str(data["path"])).resolve()
                    rec = str(RECEIVE_DIR.resolve())
                    base = str(BASE_DIR.resolve())
                    ok_scope = str(tp).startswith(rec) or str(tp).startswith(base)
                    if not ok_scope:
                        with SHARE_LOCK:
                            ok_scope = any(Path(it["path"]) == tp
                                           for it in SHARED.values())
                    if ok_scope:
                        safe = tp
                except Exception:
                    safe = None
            if safe and safe.is_file():
                _reveal_in_folder(safe)
                self._json(200, {"ok": True})
            else:
                self._json(404, {"error": "not found"})
            return

        if p == "/api/local/copy":
            if not self._is_local():
                self._deny_remote()
                return
            data = self._read_json_body()
            ok = _copy_to_clipboard(str(data.get("text", "")))
            self._json(200, {"ok": ok})
            return

        if p == "/api/local/pick-files":
            if not self._is_local():
                self._deny_remote()
                return
            data = self._read_json_body()
            paths, err = _pick_files(bool(data.get("folder")))
            if err == "cancelled":
                self._json(200, {"ok": False, "cancelled": True})
            elif err:
                self._json(500, {"ok": False, "error": err})
            else:
                self._json(200, {"ok": True, "paths": paths or []})
            return

        if p in ("/api/local/update/check", "/api/local/update/download"):
            self._json(400, {"ok": False, "message": "Windows 版在线升级将在后续版本提供"})
            return

        self._json(404, {"error": "not found"})

    # ---- DELETE ----
    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/api/share/"):
            sid = parsed.path[len("/api/share/"):]
            with SHARE_LOCK:
                existed = SHARED.pop(sid, None) is not None
            self._json(200, {"ok": True, "removed": existed})
            return
        if parsed.path.startswith("/delete/"):
            safe = _safe_path(parsed.path[len("/delete/"):])
            if not safe or not safe.is_file():
                self._json(404, {"error": "not found"})
                return
            try:
                safe.unlink()
                self._json(200, {"ok": True})
            except OSError as e:
                self._json(500, {"error": str(e)})
            return
        self._json(404, {"error": "not found"})

    # ---- 上传 ----
    def _handle_upload(self, parsed):
        query = urllib.parse.parse_qs(parsed.query)
        name = query.get("name", ["upload.bin"])[0]
        name = os.path.basename(urllib.parse.unquote(name)) or "upload.bin"
        # Windows 文件名非法字符兜底（Mac 传来的名字可能带 : 等）
        for ch in '<>:"|?*':
            name = name.replace(ch, "_")

        dest = RECEIVE_DIR / name
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self._json(500, {"error": "目录创建失败: %s" % e})
            return
        written = 0
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            with open(dest, "wb") as out:
                if length > 0:
                    remaining = length
                    while remaining > 0:
                        chunk = self.rfile.read(min(65536, remaining))
                        if not chunk:
                            break
                        out.write(chunk)
                        remaining -= len(chunk)
                        written += len(chunk)
                else:
                    self.close_connection = True
                    while True:
                        chunk = self.rfile.read(65536)
                        if not chunk:
                            break
                        out.write(chunk)
                        written += len(chunk)
        except OSError as e:
            self._json(500, {"error": "写入失败: %s" % e})
            return

        if written == 0:
            self._json(400, {"ok": False, "error": "未接收到数据(0 字节)"})
            return

        self._json(200, {"ok": True, "name": name, "size": written})
        _on_file_received(name, written)

    # ---- PC 分享文件给手机 ----
    def _handle_share(self):
        data = self._read_json_body()
        paths = data.get("paths", [])
        names = data.get("names", [])
        folder_hint = (data.get("folder") or "").strip()
        if isinstance(paths, str):
            paths = [paths]
        if not isinstance(paths, list):
            self._json(400, {"ok": False, "error": "paths 必须为字符串数组"})
            return
        if isinstance(names, str):
            names = [names]
        if not isinstance(names, list):
            self._json(400, {"ok": False, "error": "names 必须为字符串数组"})
            return

        for n in names:
            safe = _safe_path(str(n))
            if safe:
                paths.append(str(safe))

        added, failed = [], []
        with SHARE_LOCK:
            for sp in paths:
                try:
                    fp = Path(sp)
                    if fp.is_file():
                        sid = uuid.uuid4().hex
                        st = fp.stat()
                        SHARED[sid] = {
                            "path": fp.resolve(),
                            "name": fp.name,
                            "size": st.st_size,
                            "mtime": st.st_mtime,
                            "folder": folder_hint,
                        }
                        added.append({"id": sid, "name": fp.name, "size": st.st_size})
                    else:
                        failed.append(sp)
                except Exception:
                    failed.append(sp)
        self._json(200, {"ok": True, "added": added, "failed": failed})

    def _handle_save_settings(self):
        global RECEIVE_DIR, PORT, CONFIG
        data = self._read_json_body()
        try:
            if "port" in data:
                new_port = _as_int(data["port"], PORT)
                if 1 <= new_port <= 65535 and new_port != PORT:
                    CONFIG["port"] = new_port
            if "receive_dir" in data and str(data["receive_dir"]).strip():
                new_dir = resolve_receive_dir(str(data["receive_dir"]).strip())
                new_dir.mkdir(parents=True, exist_ok=True)
                CONFIG["receive_dir"] = str(new_dir)
                RECEIVE_DIR = new_dir
            for key in ("notify_on_receive", "open_folder_on_receive"):
                if key in data:
                    CONFIG[key] = bool(data[key])
            save_config(CONFIG)
            if "port" in data:
                PORT = _as_int(CONFIG.get("port"), PORT)
        except Exception as e:
            self._json(500, {"ok": False, "error": "保存失败: %s" % e})
            return

        if "autostart" in data:
            want = bool(data["autostart"])
            try:
                if want and not is_autostart_installed():
                    install_autostart()
                elif not want and is_autostart_installed():
                    uninstall_autostart()
            except Exception:
                pass

        self._json(200, {"ok": True, "needs_restart": True})


# ---------- 托盘 ----------
def _tray_image():
    try:
        from PIL import Image
        return Image.open(ICON_PNG)
    except Exception:
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (64, 64), (255, 122, 0, 255))
        ImageDraw.Draw(img).text((18, 20), "易", fill="white")
        return img


def run_tray(on_console, on_phone, on_qr, on_folder, on_restart, on_quit):
    try:
        import pystray
    except ImportError:
        _log("[托盘] pystray 未安装，退化为无托盘模式（服务继续运行）")
        return None

    menu = pystray.Menu(
        pystray.MenuItem("打开控制台", lambda *_: on_console(), default=True),
        pystray.MenuItem("打开手机连接页", lambda *_: on_phone()),
        pystray.MenuItem("显示二维码", lambda *_: on_qr()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("打开接收目录", lambda *_: on_folder()),
        pystray.MenuItem("重启服务", lambda *_: on_restart()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", lambda *_: on_quit()),
    )
    icon = pystray.Icon(APP_NAME, _tray_image(), APP_NAME, menu)
    TRAY[0] = icon
    threading.Thread(target=icon.run, daemon=True).start()
    _log("[托盘] 已启动")
    return icon


# ---------- main ----------
def main():
    import argparse
    parser = argparse.ArgumentParser(description="易码互传 · Windows 版")
    parser.add_argument("--headless", action="store_true",
                        help="无托盘运行（开发/调试）")
    parser.add_argument("--port", type=int, help="覆盖服务端口")
    parser.add_argument("--no-open", action="store_true",
                        help="启动时不自动打开控制台")
    parser.add_argument("--install-autostart", action="store_true")
    parser.add_argument("--uninstall-autostart", action="store_true")
    parser.add_argument("--version", action="store_true")
    args = parser.parse_args()

    global PORT
    if args.version:
        print("%s Windows版 %s" % (APP_NAME, APP_VERSION_FULL))
        return
    if args.install_autostart:
        install_autostart()
        print("已安装开机自启。")
        return
    if args.uninstall_autostart:
        uninstall_autostart()
        print("已取消开机自启。")
        return
    if args.port:
        PORT = args.port
        rebuild_connect_url()

    server = None
    for attempt in range(15):
        try:
            ThreadingHTTPServer.allow_reuse_address = False
            server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
            break
        except OSError:
            if attempt == 0:
                _log("[启动] 端口 %d 被占用，重试中…" % PORT)
            time.sleep(1.0)
    if server is None:
        msg = "端口 %d 被占用，无法启动。请修改端口或关闭占用程序。" % PORT
        _log("[FATAL] %s" % msg)
        if IS_WIN:
            try:
                ctypes.windll.user32.MessageBoxW(0, msg, APP_NAME, 0x10)
            except Exception:
                pass
        else:
            print(msg)
        sys.exit(1)

    start_discovery()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    rebuild_connect_url()
    qr_path = save_qr_image(CONNECT_URL)
    _log("[启动] %s Windows版 | frozen=%s | url=%s | dir=%s"
         % (APP_VERSION_FULL, getattr(sys, "frozen", False),
            CONNECT_URL, RECEIVE_DIR))

    def _console():
        _open_path("http://localhost:%d/console" % PORT)

    def _phone():
        _open_path(CONNECT_URL)

    def _qr():
        p = save_qr_image(CONNECT_URL)
        if p and Path(p).exists():
            _open_path(p)

    def _folder():
        _open_path(RECEIVE_DIR)

    def _restart():
        threading.Thread(target=_restart_and_exit, daemon=True).start()

    def _quit():
        _log("[退出] 用户从托盘退出")
        icon = TRAY[0]
        try:
            if icon is not None:
                icon.stop()
        except Exception:
            pass
        os._exit(0)

    if not args.headless:
        run_tray(_console, _phone, _qr, _folder, _restart, _quit)
        _notify(APP_NAME, "服务已启动：%s" % CONNECT_URL)

    if not args.no_open and CONFIG.get("open_console_on_start", True):
        _console()

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        _log("[退出] Ctrl+C")
        print("\n已停止。")


if __name__ == "__main__":
    main()

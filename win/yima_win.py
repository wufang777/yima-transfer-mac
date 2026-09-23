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
import urllib.request
import http.client
import errno as _errno
import uuid
import concurrent.futures
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

IS_WIN = (os.name == "nt")
IS_MAC = (sys.platform == "darwin")


class _NullWriter:
    """PyInstaller --noconsole 下 sys.stdout/stderr 为 None，兜底防崩。"""

    def write(self, *_a, **_k):
        pass

    def flush(self):
        pass


if getattr(sys, "frozen", False):
    if sys.stdout is None:
        sys.stdout = _NullWriter()
    if sys.stderr is None:
        sys.stderr = _NullWriter()

# ---- 品牌与版本（与 Mac 版保持同一口径）----
APP_NAME = "易码互传"
APP_COMPANY = "易码通科技"
APP_VERSION_NUM = "1.8.1"
APP_BUILD = "20260923"
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
    """排除不可用于局域网互传的地址（与 Mac 版一致）。"""
    if (not ip) or ip.startswith("127.") or ip.startswith("169.254.") \
            or ip.startswith("0.") or ip == "::1":
        return True
    try:
        a, b = (int(x) for x in ip.split(".")[:2])
    except Exception:
        return True
    if a == 198 and 18 <= b <= 19:      # 198.18.0.0/15 代理 fake-IP（Surge/Clash 等）
        return True
    if a == 100 and 64 <= b <= 127:     # 100.64.0.0/10 CGNAT（Tailscale 等虚拟网）
        return True
    if a == 198 and b == 51:            # 文档保留段
        return True
    if a == 203 and b == 0:             # 文档保留段
        return True
    return False


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
    # 兜底：解析 ipconfig 输出（主机名解析失败/多网卡/WSL 虚拟网卡等场景）
    if os.name == "nt":
        try:
            out = subprocess.run(
                [r"C:\Windows\System32\ipconfig.exe"], capture_output=True, timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if not out.stdout:
                out = subprocess.run(
                    ["ipconfig"], capture_output=True, timeout=5,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            text = None
            for enc in ("gbk", "utf-8", "cp437"):
                try:
                    text = out.stdout.decode(enc)
                    break
                except Exception:
                    continue
            if text:
                for line in text.splitlines():
                    line = line.strip()
                    if ("IPv4" in line or "IP Address" in line) and ":" in line:
                        ip = line.rsplit(":", 1)[-1].strip()
                        for junk in ("（首选）", "(preferred)", "(首选)"):
                            ip = ip.replace(junk, "").strip()
                        if _bad_lan_ip(ip):
                            continue
                        ips.add(ip)
        except Exception:
            pass
    ips = {ip for ip in ips if not _bad_lan_ip(ip)}
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
        # 全局广播 + 逐网卡定向广播：绑定真实网卡发出，
        # 避免有 VPN/虚拟网卡时广播走默认路由出不去
        jobs = [(None, "255.255.255.255")]
        for ip in local_ip_set():
            b = "%s.255" % ip.rsplit(".", 1)[0]
            if (ip, b) not in jobs:
                jobs.append((ip, b))
        for bind_ip, target in jobs:
            s = None
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                if bind_ip:
                    try:
                        s.bind((bind_ip, 0))
                    except Exception:
                        pass
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
                    is_new = False
                    with PEER_LOCK:
                        is_new = info["id"] not in PEERS
                        PEERS[info["id"]] = {
                            "id": info["id"],
                            "name": str(info.get("name") or "未知设备"),
                            "ip": addr[0],
                            "port": port,
                            "last_seen": now,
                        }
                    if is_new:
                        _log("[发现] 新设备 %s (%s:%s)" % (
                            info.get("name") or "未知设备", addr[0], port))
        if now - last_prune >= 3:
            last_prune = now
            with PEER_LOCK:
                for pid in [p for p, v in PEERS.items()
                            if now - v.get("last_seen", 0) > PEER_TIMEOUT]:
                    PEERS.pop(pid, None)


def start_discovery():
    threading.Thread(target=_discovery_beacon, daemon=True).start()
    threading.Thread(target=_discovery_listen, daemon=True).start()
    threading.Thread(target=_discovery_scanner, daemon=True).start()


# ---------- HTTP 扫描发现（不依赖 UDP 广播，穿透广播被拦的网络）----------
SCANNED = {}                 # "ip:port" -> peer dict（HTTP 探测到的设备）
SCAN_LOCK = threading.Lock()
SCAN_TTL = 300               # 扫描结果保留 5 分钟（每 2 分钟自动重扫刷新）


def _lan_prefixes():
    """本机各网段的 /24 前缀，如 ['192.168.1.', '192.168.10.']。"""
    pres = []
    for ip in local_ip_set():
        if _bad_lan_ip(ip) or ":" in ip:
            continue
        p = ip.rsplit(".", 1)[0] + "."
        if p not in pres:
            pres.append(p)
    return pres


# ---------- 向其它设备发送文件（服务端代为转发）----------
# 为什么不直接在网页里 fetch 对方 IP：浏览器跨域 POST 带文件时会先发
# OPTIONS 预检，而且响应要带 CORS 头才让读；任何一环不满足，XHR 都只会
# 抛出一个笼统的 onerror —— 界面看到的就是「设备不可达」，其实文件可能
# 已经传过去了，也可能根本没发出去。改由本机服务端转发后：同源、错误
# 原因准确（拒绝连接 / 超时 / 防火墙 / 对方拒收）、大文件流式不占内存。
_REFUSED = {61, 111, 10061}            # ECONNREFUSED (macOS / Linux / Windows)
_HOST_UNREACH = {65, 113, 10065}       # EHOSTUNREACH
_NET_UNREACH = {51, 101, 10051}        # ENETUNREACH
_TIMED_OUT = {60, 110, 10060}          # ETIMEDOUT


def _peer_send_timeout(length):
    """按体积给出超时：小文件别等太久，大文件又别中途掐断。"""
    try:
        mb = max(0.0, float(length) / 1048576.0)
    except Exception:
        mb = 0.0
    return max(20.0, min(1800.0, 20.0 + mb * 8.0))


def _peer_send_error(exc, port=None):
    """把底层异常翻译成用户能看懂的一句话。"""
    code = getattr(exc, "errno", None)
    text = str(exc)
    if code in _REFUSED or "refused" in text.lower():
        return "对方程序没有在运行（或端口 %s 未对外开放）" % (port or PORT)
    if isinstance(exc, (socket.timeout, TimeoutError)) or code in _TIMED_OUT:
        return "连接超时 —— 多半是对方防火墙拦住了，请放行一次入站连接"
    if code in _HOST_UNREACH:
        return "对方主机不可达 —— 请确认两台设备连的是同一个局域网"
    if code in _NET_UNREACH:
        return "网络不可达 —— 本机似乎没有接入对方所在的局域网"
    if isinstance(exc, socket.gaierror):
        return "无法解析对方地址：%s" % text
    return text or exc.__class__.__name__


def forward_file_to_peer(ip, port, filename, length, rfile, chunk=262144):
    """把 rfile 中的 length 字节流式转发到对方 /upload。

    返回 (ok: bool, msg: str, sent: int)。msg 成功时是空串，失败时是原因。
    """
    port = int(port or PORT)
    try:
        length = int(length or 0)
    except Exception:
        length = 0
    filename = os.path.basename(filename) or "upload.bin"
    path = "/upload?name=" + urllib.parse.quote(filename)

    conn = None
    sent = 0
    try:
        conn = http.client.HTTPConnection(ip, port, timeout=_peer_send_timeout(length))
        conn.putrequest("POST", path, skip_accept_encoding=True)
        conn.putheader("Content-Type", "application/octet-stream")
        conn.putheader("Content-Length", str(length))
        conn.putheader("X-Yima-From", socket.gethostname())
        conn.endheaders()

        remaining = length
        while remaining > 0:
            buf = rfile.read(min(chunk, remaining))
            if not buf:
                break
            conn.send(buf)
            remaining -= len(buf)
            sent += len(buf)

        resp = conn.getresponse()
        raw = resp.read(8192)
        if resp.status != 200:
            detail = ""
            try:
                detail = json.loads(raw.decode("utf-8", "ignore")).get("error", "")
            except Exception:
                detail = raw.decode("utf-8", "ignore")[:120]
            return False, "对方拒收（HTTP %d）%s" % (resp.status, detail), sent
        if sent == 0:
            return False, "文件是空的，没有内容可发", 0
        return True, "", sent
    except Exception as e:
        _log("[发送] %s:%d 失败: %s: %s" % (ip, port, e.__class__.__name__, e))
        return False, _peer_send_error(e, port), sent
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def forward_text_to_peer(ip, port, text, from_name=None):
    """把一段文字 POST 到对方 /api/text/receive。返回 (ok, msg)。"""
    port = int(port or PORT)
    try:
        body = json.dumps(
            {"text": text, "from": from_name or socket.gethostname()},
            ensure_ascii=False,
        ).encode("utf-8")
        conn = http.client.HTTPConnection(ip, port, timeout=8.0)
        try:
            conn.request(
                "POST", "/api/text/receive", body=body,
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "Content-Length": str(len(body)),
                    "X-Yima-From": socket.gethostname(),
                },
            )
            resp = conn.getresponse()
            resp.read(8192)
            if resp.status != 200:
                return False, "对方拒收（HTTP %d）" % resp.status
        finally:
            conn.close()
        return True, ""
    except Exception as e:
        _log("[文字] 发往 %s:%d 失败: %s: %s" % (ip, port, e.__class__.__name__, e))
        return False, _peer_send_error(e, port)


# ---- 文字互传：收发记录（内存，进程内保留最近 100 条） ----
TEXT_MAX_LEN = 10000          # 单条文字上限（字符）
_TEXT_HISTORY = []
_TEXT_LOCK = threading.Lock()


def _text_record(direction, device, text):
    with _TEXT_LOCK:
        _TEXT_HISTORY.append({
            "dir": direction,          # "sent" / "recv"
            "device": str(device)[:64],
            "text": text,
            "ts": int(time.time()),
        })
        del _TEXT_HISTORY[:-100]


def text_history(limit=100):
    with _TEXT_LOCK:
        return list(_TEXT_HISTORY[-limit:])


def _probe_live_instance(port=None):
    """端口上是否真有活着的易码互传在应答。

    用来区分「真被占用」和「TIME_WAIT 残留」：进程退出后，先前建立的连接
    会在 TIME_WAIT 滞留（Windows 默认长达 120 秒），此时端口没人监听却仍
    bind 不上。旧逻辑一路死等然后报「端口被占用」，用户明明已经关了程序
    却被告知端口冲突。
    """
    return _http_probe_peer("127.0.0.1", port or PORT, timeout=0.6) is not None


def _http_probe_peer(ip, port=None, timeout=0.8):
    """HTTP 探测该地址是否为易码互传实例；是则返回其版本信息。"""
    port = port or PORT
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open("http://%s:%d/api/version" % (ip, port), timeout=timeout) as r:
            info = json.loads(r.read(8192).decode("utf-8", "ignore"))
    except Exception:
        return None
    if not isinstance(info, dict) or info.get("app") != APP_NAME:
        return None
    return info


def scan_lan(port=None, timeout=0.8, workers=128):
    """并发扫描本机各网段，返回发现的易码互传设备列表。"""
    port = port or PORT
    mine = {ip for ip in local_ip_set() if ":" not in ip}
    targets = []
    for pre in _lan_prefixes():
        for i in range(1, 255):
            ip = pre + str(i)
            if ip not in mine:
                targets.append(ip)
    if not targets:
        return []
    found = []
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            for ip, info in ex.map(
                    lambda ip: (ip, _http_probe_peer(ip, port, timeout)), targets):
                if info:
                    found.append((ip, info))
    except Exception as e:
        _log("[扫描] 异常: %s" % e)
    now = time.time()
    with SCAN_LOCK:
        for ip, info in found:
            SCANNED["%s:%d" % (ip, port)] = {
                "id": "http:%s:%d" % (ip, port),
                "name": str(info.get("name") or ip),
                "ip": ip,
                "port": port,
                "platform": info.get("platform", ""),
                "last_seen": now,
            }
        for k in [k for k, v in SCANNED.items()
                  if now - v.get("last_seen", 0) > SCAN_TTL]:
            SCANNED.pop(k, None)
    if found:
        _log("[扫描] %s 网段发现 %d 台设备: %s" % (
            "、".join(_lan_prefixes()), len(found),
            "、".join("%s(%s)" % (i.get("name") or i.get("app"), ip)
                      for ip, i in found)))
    else:
        _log("[扫描] %s 网段未发现其它设备（已探测 %d 个地址）" % (
            "、".join(_lan_prefixes()), len(targets)))
    return [v for v in SCANNED.values()]


def note_scanned_peer(ip, port=None, name=None, platform=None):
    """把一台设备登记进扫描结果（手动添加设备时用）。"""
    port = port or PORT
    with SCAN_LOCK:
        SCANNED["%s:%d" % (ip, port)] = {
            "id": "http:%s:%d" % (ip, port),
            "name": name or ip, "ip": ip, "port": port,
            "platform": platform or "", "last_seen": time.time(),
        }


def _discovery_scanner():
    """开机后先扫一遍，之后每 2 分钟刷新（UDP 广播被拦时的兜底通道）。"""
    time.sleep(3)
    while True:
        try:
            scan_lan()
        except Exception as e:
            _log("[扫描] 后台异常: %s" % e)
        time.sleep(120)


def list_peers():
    """UDP 广播发现的设备 + HTTP 扫描发现的设备，按 ip:port 去重合并。"""
    now = time.time()
    out, seen = [], set()
    with PEER_LOCK:
        for v in sorted(PEERS.values(), key=lambda x: (x["name"], x["ip"])):
            seen.add("%s:%d" % (v["ip"], v["port"]))
            out.append({"id": v["id"], "name": v["name"], "ip": v["ip"],
                        "port": v["port"], "via": "broadcast"})
    with SCAN_LOCK:
        for v in sorted(SCANNED.values(), key=lambda x: (x["name"], x["ip"])):
            key = "%s:%d" % (v["ip"], v["port"])
            if key in seen or now - v.get("last_seen", 0) > SCAN_TTL:
                continue
            out.append({"id": v["id"], "name": v["name"], "ip": v["ip"],
                        "port": v["port"], "via": "http",
                        "platform": v.get("platform", "")})
    return out


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


def connect_code():
    """连线码内容：http://ip:port/connect?name=主机名。

    一码两用：
    - 手机扫码 → 浏览器打开 /connect 欢迎页，一键进入传文件页面；
    - 电脑扫码（控制台「扫码连接」）→ 识别出 ip:port 自动添加为设备。
    """
    return "http://%s:%d/connect?name=%s" % (
        get_lan_ip(), PORT, urllib.parse.quote(socket.gethostname()))


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

    def log_message(self, fmt, *args):
        """--noconsole 下 stderr 是 None，改写文件日志（否则每个请求都会崩）。"""
        try:
            _log("%s - %s" % (self.address_string(), fmt % args))
        except Exception:
            pass

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

        if path == "/api/scan":
            # 主动 HTTP 扫描本机各网段（不依赖 UDP 广播）
            try:
                scan_lan()
            except Exception as e:
                _log("[扫描] 手动触发异常: %s" % e)
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
                "name": socket.gethostname(),
                "port": PORT,
            })
            return

        if path == "/api/text/messages":
            self._json(200, text_history(100))
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

        if path == "/connect-qr.png":
            # 本机「电脑连线码」：对方控制台扫码连接用
            if not HAS_QR:
                self._json(404, {"error": "segno not available"})
                return
            import io
            buf = io.BytesIO()
            qr = segno.make(connect_code(), error="l")
            qr.save(buf, kind="png", scale=6, border=2)
            data = buf.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
            return

        if path == "/api/connect-info":
            self._json(200, {"ip": get_lan_ip(), "port": PORT,
                             "name": socket.gethostname(), "code": connect_code()})
            return

        if path == "/connect":
            # 手机扫「连线码」后打开的欢迎页：一键进入传文件页面
            name = socket.gethostname()
            page = ("""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>已连接 - 易码互传</title>
<style>
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
font-family:-apple-system,"PingFang SC",sans-serif;background:#f3f4f6}
.card{background:#fff;border-radius:16px;padding:36px 28px;max-width:340px;text-align:center;
box-shadow:0 4px 24px rgba(0,0,0,.08)}
.ok{width:64px;height:64px;border-radius:50%%;background:#22c55e;color:#fff;font-size:34px;
line-height:64px;margin:0 auto 16px}
h1{font-size:20px;margin:0 0 8px;color:#111}
p{color:#6b7280;font-size:14px;margin:0 0 24px}
a.btn{display:block;background:#f97316;color:#fff;text-decoration:none;border-radius:10px;
padding:14px;font-size:16px;font-weight:600}
a.btn:active{opacity:.85}
</style></head><body><div class="card">
<div class="ok">&#10003;</div>
<h1>已找到「%s」</h1>
<p>手机与电脑已在同一局域网，点击下方按钮即可互传文件。</p>
<a class="btn" href="/">开始传文件</a>
</div></body></html>""" % name)
            data = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
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

    # ---- OPTIONS（跨域预检兜底）----
    def do_OPTIONS(self):
        """放行跨域预检（兼容旧版控制台，详见 forward_file_to_peer 注释）。"""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---- POST ----
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        p = parsed.path

        if p == "/upload":
            self._handle_upload(parsed)
            return

        if p == "/api/peers/send":
            # 由本机服务端代发到对方设备（绕开浏览器跨域限制）
            if not self._is_local():
                self._deny_remote()
                return
            q = urllib.parse.parse_qs(parsed.query)
            ip = (q.get("ip", [""])[0] or "").strip()
            name = (q.get("name", ["upload.bin"])[0] or "").strip()
            try:
                prt = int(q.get("port", [PORT])[0] or PORT)
            except Exception:
                prt = PORT
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
            except Exception:
                length = 0
            if not ip:
                self._json(400, {"ok": False, "error": "缺少对方 IP"})
                return
            if ip in local_ip_set() or ip in ("127.0.0.1", "localhost"):
                self._json(400, {"ok": False, "error": "不能发给自己"})
                return
            ok, msg, sent = forward_file_to_peer(ip, prt, name, length, self.rfile)
            if ok:
                _log("[发送] 已发往 %s:%d 《%s》 %d 字节" % (ip, prt, name, sent))
            self._json(200, {"ok": ok, "error": msg, "sent": sent,
                             "ip": ip, "port": prt, "name": name})
            return

        if p == "/api/text/send":
            # 由本机服务端把文字转发到对方 /api/text/receive
            if not self._is_local():
                self._deny_remote()
                return
            body = self._read_json_body() or {}
            text = str(body.get("text") or "").strip()
            if not text:
                self._json(400, {"ok": False, "error": "没有内容可发送"})
                return
            if len(text) > TEXT_MAX_LEN:
                self._json(400, {"ok": False,
                                 "error": "文字过长（上限 %d 字，当前 %d 字）" % (TEXT_MAX_LEN, len(text))})
                return
            q = urllib.parse.parse_qs(parsed.query)
            ip = (q.get("ip", [""])[0] or "").strip()
            try:
                prt = int(q.get("port", [PORT])[0] or PORT)
            except Exception:
                prt = PORT
            if not ip:
                self._json(400, {"ok": False, "error": "缺少对方 IP"})
                return
            if ip in local_ip_set() or ip in ("127.0.0.1", "localhost"):
                self._json(400, {"ok": False, "error": "不能发给自己"})
                return
            ok, msg = forward_text_to_peer(ip, prt, text)
            if ok:
                _text_record("sent", q.get("name", [ip])[0] or ip, text)
                _log("[文字] 已发往 %s:%d %d 字" % (ip, prt, len(text)))
            self._json(200, {"ok": ok, "error": msg, "ip": ip, "port": prt})
            return

        if p == "/api/text/receive":
            # 局域网对方设备发来的文字（与 /upload 同信任级别）
            body = self._read_json_body() or {}
            text = str(body.get("text") or "")
            from_name = str(body.get("from")
                            or self.headers.get("X-Yima-From")
                            or self.client_address[0])[:64]
            if not text.strip():
                self._json(400, {"ok": False, "error": "内容为空"})
                return
            if len(text) > TEXT_MAX_LEN:
                self._json(400, {"ok": False, "error": "文字过长，已拒收"})
                return
            _text_record("recv", from_name, text)
            _log("[文字] 收到来自 %s 的 %d 字" % (from_name, len(text)))
            # 自动复制到剪贴板，收到即可直接粘贴
            if CONFIG.get("text_autocopy", True):
                _copy_to_clipboard(text)
            _notify(APP_NAME, "收到来自 %s 的文字：%.60s%s"
                    % (from_name, text.replace("\n", " "),
                       "…" if len(text) > 60 else ""))
            self._json(200, {"ok": True})
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

        if p == "/api/peers/add":
            # 手动添加设备：直接用 HTTP 探测该地址，成功即登记（不依赖 UDP）
            body = self._read_json_body() or {}
            ip = str(body.get("ip") or "").strip()
            try:
                prt = int(body.get("port") or PORT)
            except Exception:
                prt = PORT
            if not ip:
                self._json(400, {"ok": False, "error": "请填写对方 IP"})
                return
            info = _http_probe_peer(ip, prt, timeout=2.0)
            if not info:
                self._json(200, {"ok": False,
                                 "error": "连不上 %s:%d —— 对方程序未运行，或被防火墙拦截" % (ip, prt)})
                return
            note_scanned_peer(ip, prt, str(info.get("name") or ip),
                              info.get("platform"))
            _log("[设备] 手动添加成功: %s (%s:%d)" % (
                info.get("name") or ip, ip, prt))
            self._json(200, {"ok": True, "peers": list_peers()})
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


def run_tray(on_console, on_phone, on_qr, on_folder, on_scan, on_restart, on_quit):
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
        pystray.MenuItem("扫描局域网设备", lambda *_: on_scan()),
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
# ---------- Windows 防火墙自检 ----------
def ensure_firewall_rules():
    """确保防火墙放行本程序（入站 UDP 发现 + TCP 服务）。

    netsh 添加规则需要管理员权限：普通权限下会失败，仅记日志，
    并提示用户右键管理员运行「防火墙放行.bat」。
    """
    if not IS_WIN or not getattr(sys, "frozen", False):
        return
    exe = sys.executable

    def _run(*args):
        try:
            return subprocess.run(
                list(args), capture_output=True, timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:
            return None

    # 已有同名规则就不再重复添加
    q = _run("netsh", "advfirewall", "firewall", "show", "rule",
             "name=%s" % APP_NAME)
    if q is not None and APP_NAME in (q.stdout or b"").decode("gbk", "ignore"):
        _log("[防火墙] 规则已存在，跳过")
        return
    added = True
    for suffix, proto in (("", "UDP"), ("HTTP", "TCP")):
        r = _run("netsh", "advfirewall", "firewall", "add", "rule",
                 "name=%s%s" % (APP_NAME, suffix),
                 "dir=in", "action=allow", "enable=yes", "profile=any",
                 "program=%s" % exe, "protocol=%s" % proto)
        if r is None or r.returncode != 0:
            added = False
    if added:
        _log("[防火墙] 已自动添加放行规则（UDP 发现 + TCP 服务）")
    else:
        _log("[防火墙] 自动放行失败（需要管理员权限）。"
             "请右键「以管理员身份运行」安装目录下的 防火墙放行.bat")


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
    for attempt in range(45):
        try:
            ThreadingHTTPServer.allow_reuse_address = False
            server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
            break
        except OSError:
            if attempt == 0:
                _log("[启动] 端口 %d 被占用，重试中…" % PORT)
            if attempt >= 3 and not _probe_live_instance(PORT):
                # 端口上没有任何易码互传应答 → 是上一次退出留下的 TIME_WAIT
                # 残留（Windows 默认 120 秒），不会造成双实例，允许复用立即启动
                try:
                    ThreadingHTTPServer.allow_reuse_address = True
                    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
                    _log("[启动] 端口 %d 处于 TIME_WAIT 释放期，已安全接管" % PORT)
                    break
                except OSError:
                    pass
            time.sleep(1.0)
    if server is None:
        msg = ("端口 %d 已被占用，无法启动。\n\n"
               "可能原因：\n  • 已有另一个易码互传在运行\n  • 其它软件占用了该端口\n\n"
               "程序已等待约 45 秒仍未成功。请关闭占用程序，或修改服务端口后重启。"
               % PORT)
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
    try:
        ensure_firewall_rules()
    except Exception as e:
        _log("[防火墙] 自检异常: %s" % e)
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

    def _scan():
        def _do():
            try:
                found = scan_lan()
                _notify(APP_NAME, "扫描完成，共发现 %d 台设备" % len(found))
            except Exception as e:
                _log("[扫描] 托盘触发异常: %s" % e)
        threading.Thread(target=_do, daemon=True).start()

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
        run_tray(_console, _phone, _qr, _folder, _scan, _restart, _quit)
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

#!/usr/bin/env python3
"""易码互传 · macOS 原生版 —— 局域网文件互传（菜单栏 + 网页控制台）。

与 Windows 版功能对齐：
  - 手机扫码上传 / 下载（同一 WiFi，无需装 App）
  - PC ↔ PC 局域网自动发现互传（UDP 58887）
  - PC 主动分享文件给手机
  - 设置：接收目录 / 端口 / 通知 / 开机自启（LaunchAgents）

Mac 壳层架构（与 Windows tkinter 版不同）：
  - 菜单栏常驻：rumps（macOS 原生 NSStatusItem）
  - 电脑端控制台：网页 /console（仅本机可访问），启动后自动打开
  - 通知：osascript 系统通知（线程安全）
  - 无 tkinter / 无 pystray —— 壳层零历史包袱

品牌：易码互传 · 易码通科技
"""
import os
import sys
import json
import socket
import subprocess
import threading
import time
import hashlib
import mimetypes
import shutil
import urllib.parse
import urllib.request
import http.client
import errno as _errno
import uuid
import concurrent.futures
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---- 品牌信息 ----
APP_NAME = "易码互传"
APP_COMPANY = "易码通科技"

# ---- 版本号规范（发新版只改这三个常量，或直接跑 release.sh）----
#   APP_VERSION_NUM  语义化版本：主.次.修订  —— 功能新增升次版本，修 bug 升修订号
#   APP_BUILD        构建号：YYYYMMDD      —— 每构建一次即更新，用于区分同日多次构建
#   APP_CHANNEL      发布通道：stable / beta
APP_VERSION_NUM = "1.7.0"
APP_BUILD = "20260922"
APP_CHANNEL = "stable"
APP_RELEASE_DATE = "%s-%s-%s" % (APP_BUILD[0:4], APP_BUILD[4:6], APP_BUILD[6:8])
APP_VERSION = "v%s" % APP_VERSION_NUM
APP_VERSION_FULL = "v%s (%s)" % (APP_VERSION_NUM, APP_BUILD)
APP_VERSION_LINE = "%s · 构建 %s" % (APP_VERSION, APP_BUILD)
APP_TAGLINE = "%s · macOS 原生版" % APP_NAME

AUTOSTART_PLIST_NAME = "com.yima.transfer.plist"
LOG_FILENAME = "易码互传.log"
QR_FILENAME = "易码互传_连接二维码.png"

IS_MAC = (sys.platform == "darwin")

# ---- 程序目录 & 配置 ----
if getattr(sys, "frozen", False):
    # .app 包内只读，可写数据放用户级目录
    APP_DIR = Path.home() / "Library" / "Application Support" / APP_NAME
    APP_DIR.mkdir(parents=True, exist_ok=True)
else:
    APP_DIR = Path(__file__).parent

CONFIG_PATH = APP_DIR / "config.json"
DEFAULTS = {
    "port": 8000,
    "receive_dir": "received",
    "autostart": False,
    "notify_on_receive": True,
    "open_folder_on_receive": False,
    "open_console_on_start": True,
    # 在线升级：更新清单地址（支持 http(s):// 或本机/局域网路径），留空则不自动检查
    "update_feed": "",
    "update_check_on_start": True,
    # 收到对方文字后自动复制到剪贴板
    "text_autocopy": True,
}


def load_config():
    cfg = dict(DEFAULTS)
    try:
        if CONFIG_PATH.exists():
            loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cfg.update(loaded)
    except Exception:
        pass
    try:
        cfg["port"] = int(cfg.get("port", 8000))
    except Exception:
        cfg["port"] = 8000
    return cfg


def save_config(cfg):
    try:
        CONFIG_PATH.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


CONFIG = load_config()


def resolve_receive_dir(raw):
    raw = raw or "received"
    p = Path(raw)
    if not p.is_absolute():
        p = APP_DIR / raw
    return p.resolve()


RECEIVE_DIR = resolve_receive_dir(CONFIG.get("receive_dir", "received"))
RECEIVE_DIR.mkdir(parents=True, exist_ok=True)
PORT = int(CONFIG.get("port", 8000))

# 打包后资源目录
if getattr(sys, "frozen", False):
    BASE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))
else:
    BASE_DIR = Path(__file__).parent
CONSOLE = BASE_DIR / "console.html"
INDEX = BASE_DIR / "index.html"
SETTINGS = BASE_DIR / "settings.html"
ICON_PNG = BASE_DIR / "assets" / "icon.png"
LOGO_PNG = BASE_DIR / "assets" / "logo.png"   # Yimaclaw 马头品牌图标（界面用）

CONNECT_URL = "http://localhost:%d" % PORT   # 启动时按真实局域网 IP 刷新

# ---- PC→手机 共享文件表 ----
SHARE_LOCK = threading.Lock()
SHARED = {}

# ---- 局域网设备发现（PC ↔ PC）----
DISCOVERY_PORT = 58887
PEER_TIMEOUT = 12
INSTANCE_ID = uuid.uuid4().hex
PEER_LOCK = threading.Lock()
PEERS = {}  # id -> {"id","name","ip","port","last_seen"}


def _open_path(p):
    """macOS：用系统默认程序打开文件/目录/URL。"""
    try:
        subprocess.Popen(["open", str(p)])
    except Exception:
        pass


PICK_LOCK = threading.Lock()          # 同一时间只允许一个选文件对话框
PICK_CANCELLED_MARKS = ("User canceled", "用户已取消", "-128", "-600")  # osascript 取消的常见字样


def _pick_files_via_osascript(folder_mode=False):
    """弹出 macOS 原生选文件对话框，返回所选文件的绝对路径列表。

    浏览器的 <input type=file> 出于安全限制拿不到真实磁盘路径，
    而 /api/share 需要绝对路径 —— 所以由后端调 osascript 来选。
    返回 (paths, err)；用户点取消时返回 (None, "cancelled")。
    """
    if folder_mode:
        script = (
            'tell application "Finder" to activate\n'
            "set f to choose folder\n"
            'return POSIX path of f'
        )
    else:
        script = (
            'tell application "Finder" to activate\n'
            "set theFiles to choose file with multiple selections allowed\n"
            'set out to ""\n'
            "repeat with f in theFiles\n"
            '  set out to out & POSIX path of f & linefeed\n'
            "end repeat\n"
            "return out"
        )
    try:
        with PICK_LOCK:
            proc = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=600,
            )
    except subprocess.TimeoutExpired:
        return None, "选择超时，请重试"
    except Exception as e:
        return None, str(e)

    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        if any(m in err for m in PICK_CANCELLED_MARKS):
            return None, "cancelled"
        return None, err or "选择失败"
    paths = [l.strip() for l in (proc.stdout or "").splitlines() if l.strip()]
    if folder_mode:
        paths = [p for p in paths if p]
    return paths, None


def _log(msg):
    try:
        with open(APP_DIR / LOG_FILENAME, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def _notify(title, msg):
    """系统通知（osascript，任意线程可用）。"""
    def _esc(s):
        return str(s).replace("\\", "").replace('"', "'")
    try:
        subprocess.Popen([
            "osascript", "-e",
            'display notification "%s" with title "%s" sound name "Pop"'
            % (_esc(msg), _esc(title)),
        ])
    except Exception:
        pass


def _copy_to_clipboard(text):
    """复制任意文字到剪贴板。pbcopy 走 stdin，多行/引号/中文都不会出问题。"""
    try:
        r = subprocess.run(
            ["pbcopy"], input=str(text).encode("utf-8"),
            capture_output=True, timeout=5,
        )
        if r.returncode == 0:
            return True
    except Exception:
        pass
    try:  # 兜底：osascript（pbcopy 不可用时）
        subprocess.run(
            ["osascript", "-e",
             'set the clipboard to "%s"' % str(text).replace('"', '\\"').replace("\n", " ")],
            capture_output=True, timeout=5,
        )
        return True
    except Exception:
        return False


def _fmt_size(n):
    n = int(n)
    if n < 1024:
        return "%d B" % n
    if n < 1048576:
        return "%.1f KB" % (n / 1024)
    if n < 1073741824:
        return "%.1f MB" % (n / 1048576)
    return "%.2f GB" % (n / 1073741824)


def _safe_path(name: str):
    """基于文件名构造安全路径，杜绝路径穿越。"""
    name = os.path.basename(urllib.parse.unquote(name))
    candidate = (RECEIVE_DIR / name).resolve()
    if str(candidate).startswith(str(RECEIVE_DIR)):
        return candidate
    return None


# ---------- 局域网 IP 识别（macOS 专用，规避代理 fake-IP）----------
def _ifconfig_candidates():
    """解析 ifconfig，返回 [(网卡名, IPv4)]。"""
    out = ""
    for exe in ("/sbin/ifconfig", "/usr/sbin/ifconfig", "ifconfig"):
        try:
            r = subprocess.run([exe], capture_output=True, text=True, timeout=5)
            if r.stdout:
                out = r.stdout
                break
        except Exception:
            continue
    if not out:
        return []
    cands, cur = [], ""
    for line in out.splitlines():
        if line and not line[0].isspace():
            cur = line.split(":", 1)[0].strip()
        s = line.strip()
        if s.startswith("inet ") and cur:
            cands.append((cur, s.split()[1]))
    return cands


def _bad_lan_ip(ip):
    """排除不可用于局域网互传的地址。"""
    if ip.startswith("127.") or ip.startswith("169.254."):
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


def _ip_score(iface, ip):
    """候选地址评分：物理网卡 + 常见家用网段优先。"""
    try:
        parts = [int(x) for x in ip.split(".")]
    except Exception:
        return -1
    score = 0
    if iface.startswith("en"):
        score += 100
    elif iface.startswith(("bridge", "ap", "awdl")):
        score += 20
    if parts[0] == 192 and parts[1] == 168:
        score += 50
    elif parts[0] == 10:
        score += 40
    elif parts[0] == 172 and 16 <= parts[1] <= 31:
        score += 30
    return score


def get_lan_ip():
    """尽力获取真实局域网 IP；失败回退 127.0.0.1。"""
    best, best_score = None, -1
    for iface, ip in _ifconfig_candidates():
        if _bad_lan_ip(ip):
            continue
        sc = _ip_score(iface, ip)
        if sc > best_score:
            best, best_score = ip, sc
    if best:
        return best
    # 兜底：UDP 连接探测（结果同样要过过滤规则）
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if not _bad_lan_ip(ip):
                return ip
        finally:
            s.close()
    except Exception:
        pass
    return "127.0.0.1"


def _local_ipv4s():
    """本机所有可用的局域网 IPv4（排除回环/链路本地/虚拟网段）。

    打包环境下 ifconfig 可能不可用，故再用 get_lan_ip() 兜底。
    """
    seen, out = set(), []
    for _iface, ip in _ifconfig_candidates():
        if ip in seen or _bad_lan_ip(ip):
            continue
        seen.add(ip)
        out.append(ip)
    try:
        fb = get_lan_ip()
        if fb and fb not in seen and not _bad_lan_ip(fb):
            out.append(fb)
    except Exception:
        pass
    return out


def rebuild_connect_url():
    global CONNECT_URL
    ip = get_lan_ip()
    CONNECT_URL = "http://%s:%d" % (ip, PORT)
    return CONNECT_URL


def common_dirs():
    home = os.path.expanduser("~")
    return {
        "desktop": os.path.join(home, "Desktop"),
        "downloads": os.path.join(home, "Downloads"),
        "documents": os.path.join(home, "Documents"),
        "received": str(RECEIVE_DIR),
    }


# macOS 微信「文件接收目录」的探测位置。
#   微信 4.x：~/Library/Containers/com.tencent.xinWeChat/Data/Documents/
#             xwechat_files/<账号ID>_<hash>/msg/file
#   微信 3.x 及更早：目录名不同（.../2.0b4.0.9/<hash>/Message/...），不再维护
WECHAT_ROOT = (Path.home() / "Library" / "Containers" / "com.tencent.xinWeChat"
               / "Data" / "Documents" / "xwechat_files")


def wechat_receive_dirs():
    """探测本机微信的文件接收目录（可能有多个账号），按最近使用倒序返回。

    只做只读探测，探测不到就返回空列表，不影响主流程。
    """
    out = []
    try:
        if not WECHAT_ROOT.is_dir():
            return out
        for acct in sorted(WECHAT_ROOT.iterdir()):
            # all_users / Backup 不是账号目录
            if not acct.is_dir() or acct.name in ("all_users", "Backup"):
                continue
            fdir = acct / "msg" / "file"
            if not fdir.is_dir():
                continue
            try:
                mtime = fdir.stat().st_mtime
            except OSError:
                mtime = 0.0
            # 账号目录名形如 wxid_xxx_7baf / wufang777_0e81，末尾一段是随机 hash
            account = acct.name.rsplit("_", 1)[0] if "_" in acct.name else acct.name
            out.append({
                "account": account,
                "path": str(fdir),
                "mtime": mtime,
            })
    except Exception:
        return []
    out.sort(key=lambda x: x["mtime"], reverse=True)
    for i, item in enumerate(out):
        item["recent"] = (i == 0)          # 最近用过的账号，基本就是当前登录账号
        item["label"] = (time.strftime("%Y-%m-%d", time.localtime(item["mtime"]))
                         if item["mtime"] else "未知")
    return out


try:
    import segno
    HAS_QR = True
except ImportError:
    HAS_QR = False

try:
    import rumps
    HAVE_RUMPS = True
except ImportError:
    HAVE_RUMPS = False


# ---------- 局域网设备发现 ----------
def _discovery_beacon():
    """每 3 秒向全网广播本实例的存在信息。"""
    msg = json.dumps({
        "app": "LanFileTransfer",
        "id": INSTANCE_ID,
        "name": socket.gethostname(),
        "port": PORT,
    }).encode("utf-8")
    while True:
        # 全局广播 + 逐网卡定向广播：绑定真实网卡发出，
        # 避免有 VPN/代理 TUN 时广播走默认路由（虚拟网卡）出不去
        jobs = [(None, "255.255.255.255")]
        for ip in _local_ipv4s():
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
    """监听其它实例的广播，维护在线设备表。"""
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
    except Exception:
        return
    s.settimeout(2.0)
    last_prune = 0.0
    while True:
        try:
            data, addr = s.recvfrom(4096)
        except socket.timeout:
            data = None
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
    for ip in _local_ipv4s():
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
    """把一段文字 POST 到对方 /api/text/receive。

    返回 (ok: bool, msg: str)。msg 成功时为空串。
    """
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

    用来区分「真被占用」和「TIME_WAIT 残留」：进程被杀掉后，先前建立的
    连接会在 TIME_WAIT 状态滞留十几到几十秒，此时端口没人监听却仍 bind
    不上。旧逻辑一律死等 20 秒，超时就弹「端口已被占用」——用户明明已经
    退出了程序，却被提示端口冲突（本机踩过）。
    """
    return _http_probe_peer("127.0.0.1", port or PORT, timeout=0.6) is not None


def _http_probe_peer(ip, port=None, timeout=0.8):
    """HTTP 探测该地址是否为易码互传实例；是则返回其版本信息。

    显式绕过系统代理 —— 否则局域网 IP 会被代理劫持（本机踩过这个坑）。
    """
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
    mine = set(_local_ipv4s())
    targets = []
    for pre in _lan_prefixes():
        for i in range(1, 255):
            ip = pre + str(i)
            if ip not in mine:
                targets.append(ip)
    if not targets:
        _log("[扫描] 未取到本机网段（扫描跳过）—— 本机 IP: %s" % _local_ipv4s())
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
                "name": str(info.get("name") or info.get("host") or ip),
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
            key = "%s:%d" % (v["ip"], v["port"])
            seen.add(key)
            out.append({"id": v["id"], "name": v["name"], "ip": v["ip"],
                        "port": v["port"], "via": "broadcast"})
    with SCAN_LOCK:
        for v in sorted(SCANNED.values(), key=lambda x: (x["name"], x["ip"])):
            key = "%s:%d" % (v["ip"], v["port"])
            if key in seen:
                continue
            if now - v.get("last_seen", 0) > SCAN_TTL:
                continue
            out.append({"id": v["id"], "name": v["name"], "ip": v["ip"],
                        "port": v["port"], "via": "http",
                        "platform": v.get("platform", "")})
    return out


# ---------- 开机自启（macOS LaunchAgents）----------
def _mac_launchagent_path():
    return os.path.join(
        os.path.expanduser("~"), "Library", "LaunchAgents", AUTOSTART_PLIST_NAME
    )


def is_autostart_installed():
    return os.path.exists(_mac_launchagent_path())


def install_autostart():
    import plistlib
    target = os.path.abspath(sys.argv[0])
    if getattr(sys, "frozen", False):
        parts = Path(sys.executable).parts
        if "Contents" in parts:
            idx = parts.index("Contents")
            target = str(Path(*parts[:idx]))
    plist_path = _mac_launchagent_path()
    os.makedirs(os.path.dirname(plist_path), exist_ok=True)
    pl = {
        "Label": "com.yima.transfer",
        "ProgramArguments": ["open", "-a", target],
        "RunAtLoad": True,
    }
    with open(plist_path, "wb") as f:
        plistlib.dump(pl, f)
    try:
        subprocess.run(["launchctl", "unload", plist_path],
                       capture_output=True, timeout=5)
        subprocess.run(["launchctl", "load", plist_path],
                       capture_output=True, timeout=5)
    except Exception:
        pass
    print("已安装开机自启（macOS LaunchAgents）：\n  " + plist_path)


def uninstall_autostart():
    plist_path = _mac_launchagent_path()
    if os.path.exists(plist_path):
        try:
            subprocess.run(["launchctl", "unload", plist_path],
                           capture_output=True, timeout=5)
            os.remove(plist_path)
            print("已取消开机自启。")
        except OSError as e:
            print("删除自启项失败：%s" % e)
    else:
        print("未发现自启项，无需操作。")


# ---------- 单实例（lsof 端口定位 + 进程名匹配，双保险）----------
def _kill_port_occupant(port):
    try:
        out = subprocess.run(
            ["lsof", "-ti", "tcp:%d" % port, "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        me = os.getpid()
        for pid_s in out.split():
            if pid_s.isdigit() and int(pid_s) > 0 and int(pid_s) != me:
                subprocess.run(["kill", "-9", pid_s], capture_output=True)
                return True
    except Exception:
        pass
    return False


def _kill_other_instances():
    """结束旧实例，保证本实例是唯一运行的副本。
    双保险：① lsof 按端口精准定位并结束；
            ② pgrep 按 .app 内可执行路径匹配结束（覆盖端口已被换的情况）。"""
    me = os.getpid()
    killed = []
    if _kill_port_occupant(PORT):
        killed.append("port:%d" % PORT)
    try:
        out = subprocess.run(
            ["pgrep", "-f", "%s.app/Contents/MacOS" % APP_NAME],
            capture_output=True, text=True, timeout=5,
        ).stdout
        for pid_s in out.split():
            if pid_s.isdigit() and int(pid_s) > 0 and int(pid_s) != me:
                subprocess.run(["kill", "-9", pid_s], capture_output=True)
                killed.append("pid:" + pid_s)
    except Exception:
        pass
    if killed:
        _log("[单实例] 已结束旧实例: %s" % "; ".join(killed))
        # 等端口真正释放（最多 3 秒）
        for _ in range(6):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.4)
            try:
                s.connect(("127.0.0.1", PORT))
                s.close()
                time.sleep(0.5)   # 仍可连接 → 继续等
            except Exception:
                s.close()
                break
        time.sleep(0.3)


def _show_port_conflict_dialog(port):
    """窗口化运行时端口冲突，用系统对话框告知。"""
    script = (
        'display dialog "端口 %d 已被占用，无法启动。\\n\\n'
        '可能原因：\\n  • 已有另一个易码互传在运行\\n  • 其它软件占用了该端口\\n\\n'
        '程序已等待约 45 秒仍未成功。可在设置页修改「服务端口」后重启。" '
        'with title "%s" buttons {"好"} default button "好" with icon stop' % (port, APP_NAME)
    )
    try:
        subprocess.Popen(["osascript", "-e", script])
    except Exception:
        pass


# ---------- 二维码 ----------
def save_qr_image(url):
    if not HAS_QR:
        return None
    try:
        qr_path = APP_DIR / QR_FILENAME
        segno.make(url, error="m").save(str(qr_path), scale=10, border=4)
        return qr_path
    except Exception:
        return None


def _print_text_qr(url):
    if not HAS_QR:
        return
    qr = segno.make(url, error="l")
    for row in qr.matrix:
        print("  " + "".join("#" if cell else " " for cell in row))


# ---------- 版本与在线升级 ----------
UPDATE_LOCK = threading.Lock()
UPDATE_STATE = {
    "phase": "idle",        # idle | checking | uptodate | available | downloading | downloaded | installing | error
    "message": "",
    "percent": 0,
    "manifest": None,       # 远端清单
    "file": None,           # 已下载的安装包路径
    "error": "",
    "checked_at": 0,
}
_DEFAULT_FEED = ""   # 出厂更新源；留空表示由用户在设置页填写


def vtuple(s):
    """版本字符串 → 可比较元组：'v1.2.3' → (1,2,3)"""
    s = str(s or "").strip().lstrip("vV")
    main = s.split("-")[0].split("+")[0]
    parts = []
    for seg in main.split("."):
        num = ""
        for ch in seg:
            if ch.isdigit():
                num += ch
            else:
                break
        parts.append(int(num) if num else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def version_newer(remote, local):
    return vtuple(remote) > vtuple(local)


def update_feed_url():
    return (CONFIG.get("update_feed") or _DEFAULT_FEED or "").strip()


def _is_private_host(host):
    """内网 / 回环地址：访问这些地址不走系统代理（代理软件会拦截或返回 502）。"""
    h = (host or "").lower()
    if h in ("localhost", "") or h.endswith(".local"):
        return True
    try:
        parts = [int(x) for x in h.split(".")]
    except Exception:
        return False
    if len(parts) != 4:
        return False
    a, b = parts[0], parts[1]
    return (a == 127 or a == 10 or a == 0
            or (a == 192 and b == 168)
            or (a == 172 and 16 <= b <= 31)
            or (a == 169 and b == 254))


def _urlopen(url, timeout=10):
    """打开 URL；内网地址强制直连，公网地址沿用系统代理设置。"""
    host = urllib.parse.urlparse(url).hostname or ""
    if _is_private_host(host):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(url, timeout=timeout)
    return urllib.request.urlopen(url, timeout=timeout)


def _read_manifest_from(src):
    """从 http(s):// 或本地路径读取更新清单 JSON。"""
    if src.startswith("http://") or src.startswith("https://"):
        url = urllib.parse.quote(src, safe=":/?&=#%") if any(ord(c) > 127 for c in src) else src
        with _urlopen(url, timeout=10) as resp:
            raw = resp.read().decode("utf-8", "replace")
    else:
        p = Path(urllib.parse.unquote(src[7:]) if src.startswith("file://") else src).expanduser()
        raw = p.read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict) or not data.get("latest"):
        raise ValueError("更新清单格式不正确（缺少 latest 字段）")
    return data


def resolve_manifest_url(u):
    """清单里的相对下载地址 → 绝对地址（相对清单所在目录）。"""
    feed = update_feed_url()
    if not u:
        return ""
    if u.startswith("http://") or u.startswith("https://") or u.startswith("file://") or u.startswith("/"):
        return u
    if feed.startswith("http://") or feed.startswith("https://"):
        joined = urllib.parse.urljoin(feed, u)
        # 文件名可能含中文/空格，按 URL 规则编码（中文文件名在各家云存储上兼容性不一）
        return urllib.parse.quote(joined, safe=":/?&=#%")
    base = Path(urllib.parse.unquote(feed[7:]) if feed.startswith("file://") else feed).expanduser()
    return str(base.parent / u)


def check_update():
    """拉取更新清单并与当前版本比对，结果写入 UPDATE_STATE。"""
    feed = update_feed_url()
    with UPDATE_LOCK:
        UPDATE_STATE.update({"phase": "checking", "message": "正在检查更新…", "error": "", "percent": 0})
    if not feed:
        with UPDATE_LOCK:
            UPDATE_STATE.update({
                "phase": "error",
                "error": "尚未配置更新源地址（设置 → 在线升级）",
                "message": "未配置更新源",
            })
        return dict(UPDATE_STATE)

    try:
        m = _read_manifest_from(feed)
        remote = str(m.get("latest", "")).strip()
        newer = version_newer(remote, APP_VERSION_NUM)
        m["_download_url"] = resolve_manifest_url(m.get("url") or m.get("download_url") or "")
        with UPDATE_LOCK:
            UPDATE_STATE.update({
                "phase": "available" if newer else "uptodate",
                "manifest": m,
                "message": ("发现新版本 %s" % remote) if newer else "当前已是最新版本",
                "error": "",
                "checked_at": int(time.time()),
            })
        _log("[更新] 检查完成：本地 %s / 远端 %s / 有新版=%s" % (APP_VERSION_NUM, remote, newer))
    except Exception as e:
        with UPDATE_LOCK:
            UPDATE_STATE.update({
                "phase": "error",
                "error": "检查更新失败：%s" % e,
                "message": "检查更新失败",
                "checked_at": int(time.time()),
            })
        _log("[更新] 检查失败：%s" % e)
    return dict(UPDATE_STATE)


def _update_download_dir():
    dl = Path.home() / "Downloads"
    d = (dl if dl.is_dir() else APP_DIR) / "易码互传更新"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        d = APP_DIR
    return d


def download_update():
    """后台线程下载新版安装包（dmg），带进度与 sha256 校验。"""
    with UPDATE_LOCK:
        m = dict(UPDATE_STATE.get("manifest") or {})
    url = m.get("_download_url") or ""
    if not url:
        with UPDATE_LOCK:
            UPDATE_STATE.update({"phase": "error", "error": "更新清单里没有下载地址"})
        return

    def _worker():
        try:
            name = m.get("file") or (url.split("/")[-1].split("?")[0] or "易码互传-新版.dmg")
            dest = _update_download_dir() / name
            tmp = dest.with_suffix(dest.suffix + ".part")
            with UPDATE_LOCK:
                UPDATE_STATE.update({"phase": "downloading", "percent": 0,
                                     "message": "正在下载 %s…" % name, "error": ""})

            if url.startswith("http://") or url.startswith("https://"):
                with _urlopen(url, timeout=30) as resp:
                    total = int(resp.headers.get("Content-Length") or m.get("size") or 0)
                    done = 0
                    h = hashlib.sha256()
                    with open(tmp, "wb") as fh:
                        while True:
                            chunk = resp.read(262144)
                            if not chunk:
                                break
                            fh.write(chunk)
                            h.update(chunk)
                            done += len(chunk)
                            if total:
                                with UPDATE_LOCK:
                                    UPDATE_STATE["percent"] = int(done * 100 / total)
            else:
                src = Path(urllib.parse.unquote(url[7:]) if url.startswith("file://") else url).expanduser()
                shutil.copy2(src, tmp)
                with UPDATE_LOCK:
                    UPDATE_STATE["percent"] = 100
                h = hashlib.sha256(tmp.read_bytes())

            want = (m.get("sha256") or "").strip().lower()
            got = h.hexdigest().lower()
            if want and want != got:
                tmp.unlink(missing_ok=True)
                raise ValueError("校验失败：安装包 sha256 不匹配（期望 %s…，实际 %s…）" % (want[:12], got[:12]))

            tmp.replace(dest)
            with UPDATE_LOCK:
                UPDATE_STATE.update({
                    "phase": "downloaded", "percent": 100, "file": str(dest),
                    "message": "已下载 %s，可立即升级" % name, "error": "",
                })
            _log("[更新] 下载完成：%s" % dest)
            _notify(APP_NAME, "新版安装包已下载，可在控制台点击「立即升级」")
        except Exception as e:
            with UPDATE_LOCK:
                UPDATE_STATE.update({"phase": "error", "error": "下载失败：%s" % e, "message": "下载失败"})
            _log("[更新] 下载失败：%s" % e)

    threading.Thread(target=_worker, daemon=True).start()


def current_bundle_path():
    """当前运行的 .app 路径（开发模式返回 None）。"""
    if not getattr(sys, "frozen", False):
        return None
    p = Path(sys.executable).resolve()
    for parent in p.parents:
        if parent.suffix == ".app":
            return parent
    return None


def install_update_script(dmg_path):
    """生成升级脚本：等本进程退出 → 挂载 dmg → 替换 .app → 重开。
    替换前先备份，签名校验失败自动回滚。"""
    bundle = current_bundle_path()
    script = r'''#!/bin/bash
# 易码互传自动升级脚本（由应用生成，升级完成后可删除）
set -u
DMG=%(dmg)s
BUNDLE=%(bundle)s
LOG=%(log)s
exec >>"$LOG" 2>&1
echo "===== $(date '+%%Y-%%m-%%d %%H:%%M:%%S') 开始升级 ====="
echo "安装包: $DMG"
echo "目标: $BUNDLE"

if [ ! -f "$DMG" ]; then echo "安装包不存在，中止"; exit 1; fi
if [ ! -d "$BUNDLE" ]; then echo "目标应用不存在，中止"; exit 1; fi

# 1) 等旧进程完全退出（最多 60 秒）
for i in $(seq 1 120); do
  pgrep -f "易码互传.app/Contents/MacOS" >/dev/null 2>&1 || break
  sleep 0.5
done
sleep 1
# 兜底：仍有残留就强制结束（避免替换到一半文件被占用）
pkill -9 -f "易码互传.app/Contents/MacOS" >/dev/null 2>&1 || true
sleep 1
# 等服务端口释放（最多 30 秒），否则新版本起来会绑不上端口
for i in $(seq 1 60); do
  if ! lsof -nP -iTCP:%(port)s -sTCP:LISTEN >/dev/null 2>&1; then break; fi
  sleep 0.5
done

# 2) 挂载 dmg（只读）
MP=$(hdiutil attach "$DMG" -nobrowse -readonly 2>/dev/null | awk -F'\t' '/\/Volumes\//{print $NF}' | tail -1)
if [ -z "${MP:-}" ] || [ ! -d "$MP" ]; then echo "挂载失败，中止"; exit 1; fi
echo "已挂载: $MP"
SRC=$(find "$MP" -maxdepth 1 -name "*.app" | head -1)
if [ -z "${SRC:-}" ] || [ ! -d "$SRC" ]; then
  echo "镜像内未找到 .app，中止"; hdiutil detach "$MP" >/dev/null 2>&1; exit 1
fi

# 3) 备份 → 替换 → 校验（失败自动回滚）
rm -rf "$BUNDLE.old"
if ! mv "$BUNDLE" "$BUNDLE.old"; then echo "备份失败，中止"; hdiutil detach "$MP" >/dev/null 2>&1; exit 1; fi
if ! cp -R "$SRC" "$BUNDLE"; then
  echo "拷贝失败，回滚"; rm -rf "$BUNDLE"; mv "$BUNDLE.old" "$BUNDLE"
  hdiutil detach "$MP" >/dev/null 2>&1; exit 1
fi
xattr -dr com.apple.quarantine "$BUNDLE" >/dev/null 2>&1
if codesign -v "$BUNDLE" >/dev/null 2>&1; then
  echo "新版本已就位，签名校验通过"
  mv "$BUNDLE.old" "$HOME/.Trash/易码互传.app-升级前备份-$(date '+%%Y%%m%%d-%%H%%M%%S')" 2>/dev/null || rm -rf "$BUNDLE.old"
else
  echo "签名校验失败，回滚到原版本"
  rm -rf "$BUNDLE"; mv "$BUNDLE.old" "$BUNDLE"
  hdiutil detach "$MP" >/dev/null 2>&1
  exit 1
fi

# 4) 卸载镜像 → 清理安装包 → 重新打开应用
hdiutil detach "$MP" >/dev/null 2>&1
rm -f "$DMG"
echo "升级完成，重新启动应用"
open "$BUNDLE"
echo "===== 升级结束 ====="
''' % {
        "dmg": _sh_quote(str(dmg_path)),
        "bundle": _sh_quote(str(bundle)) if bundle else '"/Applications/易码互传.app"',
        "log": _sh_quote(str(APP_DIR / "update.log")),
        "port": PORT,
    }
    script_path = Path("/tmp") / ("yima_update_%s.sh" % uuid.uuid4().hex[:8])
    script_path.write_text(script, encoding="utf-8")
    script_path.chmod(0o755)
    return script_path


def _sh_quote(s):
    return "'" + str(s).replace("'", "'\\''") + "'"


def apply_update():
    """执行升级：非打包模式只打开镜像让用户手动替换；打包模式静默替换自身。"""
    with UPDATE_LOCK:
        file_path = UPDATE_STATE.get("file")
        m = dict(UPDATE_STATE.get("manifest") or {})
    if not file_path or not Path(file_path).exists():
        with UPDATE_LOCK:
            UPDATE_STATE.update({"phase": "error", "error": "安装包未就绪，请先下载"})
        return False, "安装包未就绪，请先下载"

    bundle = current_bundle_path()
    if bundle is None:
        # 开发模式（源码运行）：直接打开镜像，避免误替换
        _open_path(file_path)
        return False, "源码运行模式不自动升级，已为你打开安装包"

    try:
        script = install_update_script(file_path)
        subprocess.Popen(["/bin/bash", str(script)], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with UPDATE_LOCK:
            UPDATE_STATE.update({"phase": "installing", "message": "正在升级，应用即将自动重启…"})
        _log("[更新] 已启动升级脚本：%s" % script)
        _notify(APP_NAME, "正在升级到 %s，应用将自动重启" % m.get("latest", "新版本"))

        def _quit_soon():
            time.sleep(1.5)
            try:
                import rumps
                rumps.quit_application()
            except Exception:
                pass
            os._exit(0)

        threading.Thread(target=_quit_soon, daemon=True).start()
        return True, "开始升级，应用将自动重启"
    except Exception as e:
        with UPDATE_LOCK:
            UPDATE_STATE.update({"phase": "error", "error": "升级失败：%s" % e})
        return False, "升级失败：%s" % e


def update_state_payload():
    with UPDATE_LOCK:
        st = dict(UPDATE_STATE)
    m = dict(st.get("manifest") or {})
    st["manifest"] = {
        "latest": m.get("latest", ""),
        "build": m.get("build", ""),
        "release_date": m.get("release_date", ""),
        "notes": m.get("notes") or [],
        "size": m.get("size", 0),
        "channel": m.get("channel", "stable"),
        "min_system": m.get("min_system", ""),
        "url": m.get("_download_url", ""),
    } if m else None
    st["current"] = {
        "version": APP_VERSION_NUM,
        "build": APP_BUILD,
        "release_date": APP_RELEASE_DATE,
        "channel": APP_CHANNEL,
        "version_line": APP_VERSION_LINE,
    }
    st["feed"] = update_feed_url()
    st["can_auto_install"] = current_bundle_path() is not None
    return st


# ---------- HTTP 服务 ----------
_LOCAL_IPS = None


def local_ip_set():
    """本机所有网卡地址集合（含回环）。浏览器访问自身局域网 IP 时，
    TCP 来源地址是局域网 IP 而非 127.0.0.1，需一并视为本机。"""
    global _LOCAL_IPS
    if _LOCAL_IPS is None:
        _LOCAL_IPS = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
        try:
            for _iface, ip in _ifconfig_candidates():
                _LOCAL_IPS.add(ip)
        except Exception:
            pass
    return _LOCAL_IPS


class Handler(BaseHTTPRequestHandler):
    server_version = "YimaTransfer-Mac/1.0"

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

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b"{}"
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

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
            action, ascii_name, urllib.parse.quote(path.name),
        )
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

    # ---- GET ----
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # 根页面按客户端分流：本机 → 控制台，其它设备 → 手机页
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
            if it and fp is not None and fp.is_file():
                self._send_file(fp, inline=parsed.query in ("inline=1", "open=1"))
            else:
                self._json(404, {"error": "文件不存在或已被取消分享"})
            return

        if path == "/info":
            self._json(200, {"url": CONNECT_URL, "port": PORT, "dir": str(RECEIVE_DIR)})
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
                "name": socket.gethostname(),
                "platform": "mac",
                "port": PORT,
            })
            return

        if path == "/api/text/messages":
            self._json(200, text_history(100))
            return

        if path == "/api/local/update":
            if not self._is_local():
                self._deny_remote()
                return
            self._json(200, update_state_payload())
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
                "update_phase": UPDATE_STATE.get("phase", "idle"),
            })
            return

        if path == "/api/settings":
            self._json(200, {
                "port": PORT,
                "receive_dir": str(RECEIVE_DIR),
                "autostart": is_autostart_installed(),
                "notify_on_receive": bool(CONFIG.get("notify_on_receive", True)),
                "open_folder_on_receive": bool(CONFIG.get("open_folder_on_receive", False)),
                "update_feed": update_feed_url(),
                "update_check_on_start": bool(CONFIG.get("update_check_on_start", True)),
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

    # ---- OPTIONS（跨域预检兜底）----
    def do_OPTIONS(self):
        """放行跨域预检。

        新版控制台已改为走本机服务转发的 /api/peers/send，正常不会再触发
        预检；这里保留是为了兼容旧版页面（浏览器会因 Content-Type 不是
        简单请求而先发 OPTIONS，之前没实现该方法是「设备不可达」的原因之一）。
        """
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
            # 由本机服务端代发到对方设备（绕开浏览器跨域限制，见 forward_file_to_peer 注释）
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
            # 由本机服务端把文字转发到对方 /api/text/receive（同源调用，无跨域问题）
            if not self._is_local():
                self._deny_remote()
                return
            body = self._read_json() or {}
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
            body = self._read_json() or {}
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
            # 仅本机可发起分享：若不限制，同网段任意设备都能让电脑分享
            # 任意私有文件并通过 /share/<id> 下载走
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
            try:
                body = self._read_json() or {}
            except Exception:
                body = {}
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

        # ---- 仅本机可用的控制接口 ----
        if p == "/api/local/update/check":
            if not self._is_local():
                self._deny_remote()
                return
            threading.Thread(target=check_update, daemon=True).start()
            self._json(200, {"ok": True, "started": True})
            return

        if p == "/api/local/update/download":
            if not self._is_local():
                self._deny_remote()
                return
            download_update()
            self._json(200, {"ok": True, "started": True})
            return

        if p == "/api/local/update/install":
            if not self._is_local():
                self._deny_remote()
                return
            ok, msg = apply_update()
            self._json(200 if ok else 400, {"ok": ok, "message": msg})
            return

        if p == "/api/local/update/open":
            if not self._is_local():
                self._deny_remote()
                return
            with UPDATE_LOCK:
                fp = UPDATE_STATE.get("file")
            if fp and Path(fp).exists():
                subprocess.Popen(["open", "-R", str(fp)])
                self._json(200, {"ok": True})
            else:
                self._json(404, {"ok": False, "error": "安装包尚未下载"})
            return

        if p == "/api/local/open-folder":
            if not self._is_local():
                self._deny_remote()
                return
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
                raw = self.rfile.read(length) if length else b"{}"
                data = json.loads(raw.decode("utf-8")) if raw else {}
            except Exception:
                data = {}
            target = data.get("path") or str(RECEIVE_DIR)
            # 安全限制：只允许打开接收目录及其父级内
            try:
                tp = Path(target).resolve()
                if str(tp).startswith(str(RECEIVE_DIR)) or str(tp).startswith(str(APP_DIR)):
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
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
                raw = self.rfile.read(length) if length else b"{}"
                data = json.loads(raw.decode("utf-8"))
            except Exception:
                data = {}
            # 两种定位方式：接收目录里的文件按 name 解析；分享中的文件按 path 定位
            # （path 仅允许接收目录内或当前正被分享的路径，防止被用来探测任意文件）
            safe = None
            if data.get("name"):
                safe = _safe_path(str(data["name"]))
            elif data.get("path"):
                try:
                    tp = Path(str(data["path"])).resolve()
                    ok_scope = (str(tp).startswith(str(RECEIVE_DIR))
                                or str(tp).startswith(str(APP_DIR)))
                    if not ok_scope:
                        with SHARE_LOCK:
                            ok_scope = any(
                                it["path"] == tp for it in SHARED.values())
                    if ok_scope:
                        safe = tp
                except Exception:
                    safe = None
            if safe and safe.is_file():
                try:
                    subprocess.Popen(["open", "-R", str(safe)])
                    self._json(200, {"ok": True})
                except Exception as e:
                    self._json(500, {"error": str(e)})
            else:
                self._json(404, {"error": "not found"})
            return

        if p == "/api/local/copy":
            if not self._is_local():
                self._deny_remote()
                return
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
                raw = self.rfile.read(length) if length else b"{}"
                data = json.loads(raw.decode("utf-8"))
                ok = _copy_to_clipboard(str(data.get("text", "")))
                self._json(200, {"ok": ok})
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        if p == "/api/local/pick-files":
            # 弹出原生选文件对话框，返回绝对路径列表（供「发送到手机」使用）
            if not self._is_local():
                self._deny_remote()
                return
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
                raw = self.rfile.read(length) if length else b"{}"
                data = json.loads(raw.decode("utf-8")) if raw.strip() else {}
            except Exception:
                data = {}
            paths, err = _pick_files_via_osascript(bool(data.get("folder")))
            if err == "cancelled":
                self._json(200, {"ok": False, "cancelled": True})
            elif err:
                self._json(500, {"ok": False, "error": err})
            else:
                self._json(200, {"ok": True, "paths": paths})
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
        if not parsed.path.startswith("/delete/"):
            self._json(404, {"error": "not found"})
            return
        safe = _safe_path(parsed.path[len("/delete/"):])
        if not safe or not safe.is_file():
            self._json(404, {"error": "not found"})
            return
        try:
            safe.unlink()
            self._json(200, {"ok": True})
        except OSError as e:
            self._json(500, {"error": "delete failed: %s" % e})

    # ---- 上传（与 Windows 版协议一致：原始 body + ?name=）----
    def _handle_upload(self, parsed):
        query = urllib.parse.parse_qs(parsed.query)
        name = query.get("name", ["upload.bin"])[0]
        name = os.path.basename(urllib.parse.unquote(name)) or "upload.bin"

        dest = RECEIVE_DIR / name
        dest.parent.mkdir(parents=True, exist_ok=True)
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
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b"{}"
            data = json.loads(raw.decode("utf-8"))
            paths = data.get("paths", [])
            names = data.get("names", [])
            folder_hint = (data.get("folder") or "").strip()
        except Exception as e:
            self._json(400, {"ok": False, "error": "请求解析失败: %s" % e})
            return
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

        # names：接收目录里的文件，按文件名解析（限死在接收目录内，杜绝穿越）
        for n in names:
            safe = _safe_path(str(n))
            if safe:
                paths.append(str(safe))

        added, failed = [], []
        with SHARE_LOCK:
            for p in paths:
                try:
                    fp = Path(p)
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
                        failed.append(p)
                except Exception:
                    failed.append(p)
        self._json(200, {"ok": True, "added": added, "failed": failed})

    def _handle_save_settings(self):
        global RECEIVE_DIR, PORT, CONFIG
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b"{}"
            data = json.loads(raw.decode("utf-8"))
        except Exception as e:
            self._json(400, {"ok": False, "error": "请求解析失败: %s" % e})
            return

        changed_port = False
        try:
            if "port" in data:
                new_port = int(data["port"])
                if 1 <= new_port <= 65535 and new_port != PORT:
                    CONFIG["port"] = new_port
                    changed_port = True
            if "receive_dir" in data and str(data["receive_dir"]).strip():
                new_dir = resolve_receive_dir(str(data["receive_dir"]).strip())
                new_dir.mkdir(parents=True, exist_ok=True)
                CONFIG["receive_dir"] = str(new_dir)
                RECEIVE_DIR = new_dir
            for key in ("notify_on_receive", "open_folder_on_receive",
                        "open_console_on_start", "update_check_on_start"):
                if key in data:
                    CONFIG[key] = bool(data[key])
            if "update_feed" in data:
                CONFIG["update_feed"] = str(data["update_feed"] or "").strip()
            save_config(CONFIG)
        except Exception as e:
            self._json(500, {"ok": False, "error": "保存失败: %s" % e})
            return

        # 自启开关
        if "autostart" in data:
            want = bool(data["autostart"])
            if want and not is_autostart_installed():
                try:
                    install_autostart()
                except Exception:
                    pass
            elif not want and is_autostart_installed():
                try:
                    uninstall_autostart()
                except Exception:
                    pass

        if changed_port:
            self._json(200, {"ok": True, "restart": True})
            threading.Thread(target=_restart_and_exit, daemon=True).start()
        else:
            self._json(200, {"ok": True})

    def log_message(self, fmt, *args):
        pass  # 控制台模式下的访问日志由 stdout 负责，避免刷屏


def _on_file_received(name, size):
    """收到文件后的处理：通知 / 自动打开目录。"""
    msg = "收到文件：%s（%s）" % (name, _fmt_size(size))
    if CONFIG.get("notify_on_receive", True):
        _notify(APP_NAME, msg)
    if CONFIG.get("open_folder_on_receive"):
        _open_path(RECEIVE_DIR)


def _restart_and_exit():
    """重新拉起自身后退出当前进程（端口变更 / 手动重启用）。"""
    try:
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--from-restart"]
        else:
            cmd = [sys.executable, os.path.abspath(__file__), "--from-restart"]
        subprocess.Popen(cmd)
        time.sleep(0.5)
    except Exception:
        pass
    os._exit(0)


# ---------- 启动闪屏（双击图标后的视觉反馈）----------
def _splash_enabled():
    """启动闪屏可用性探测：AppKit 在（rumps 依赖 pyobjc，正常必然在）就启用。"""
    try:
        import AppKit  # noqa
        import Foundation  # noqa
        return True
    except Exception:
        return False


class _Splash:
    """启动闪屏：无边框圆角小窗 —— 品牌图标 + 标题 + 状态文案 + 转圈动画。

    生命周期（全部必须在主线程调用，AppKit 约束）：
        s = _Splash(); s.show()
        s.pump() × N          ← 泵事件，驱动转圈动画与淡入
        s.set_text(...)       ← 更新状态文案
        s.finish()            ← 保证最少停留时长后淡出关闭
    """

    W, H = 300, 148

    def __init__(self):
        self._win = None
        self._text = None
        self._spin = None
        self._alpha = 0.0
        self._shown_at = 0.0
        self._build()

    def _build(self):
        from AppKit import (
            NSWindow, NSVisualEffectView, NSTextField, NSImageView, NSImage,
            NSProgressIndicator, NSColor, NSFont, NSMakeRect, NSMakeSize,
            NSBorderlessWindowMask, NSBackingStoreBuffered, NSFloatingWindowLevel,
            NSWindowCollectionBehaviorCanJoinAllSpaces, NSTextAlignmentCenter,
            NSControlSizeSmall, NSProgressIndicatorSpinningStyle,
            NSFontWeightSemibold, NSFontWeightRegular,
            NSVisualEffectMaterialWindowBackground, NSVisualEffectStateActive,
            NSVisualEffectBlendingModeBehindWindow,
        )

        rect = NSMakeRect(0, 0, self.W, self.H)
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, NSBorderlessWindowMask, NSBackingStoreBuffered, False)
        win.setLevel_(NSFloatingWindowLevel)          # 压在普通窗口之上
        win.setOpaque_(False)
        win.setBackgroundColor_(NSColor.clearColor())
        win.setHasShadow_(True)
        win.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces)
        win.setReleasedWhenClosed_(False)
        win.center()

        # 毛玻璃卡片：跟随系统浅色/深色模式
        fx = NSVisualEffectView.alloc().initWithFrame_(rect)
        fx.setMaterial_(NSVisualEffectMaterialWindowBackground)
        fx.setState_(NSVisualEffectStateActive)
        fx.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
        fx.setWantsLayer_(True)
        try:
            fx.layer().setCornerRadius_(14.0)
            fx.layer().setMasksToBounds_(True)
        except Exception:
            pass
        win.setContentView_(fx)

        # 品牌图标
        try:
            if ICON_PNG.exists():
                img = NSImage.alloc().initWithContentsOfFile_(str(ICON_PNG))
                if img is not None:
                    img.setSize_(NSMakeSize(48, 48))
                    iv = NSImageView.alloc().initWithFrame_(NSMakeRect(126, 82, 48, 48))
                    iv.setImage_(img)
                    fx.addSubview_(iv)
        except Exception:
            pass

        fx.addSubview_(self._label(NSMakeRect(0, 58, self.W, 18), APP_NAME, 13.0,
                                   NSFontWeightSemibold, NSColor.labelColor()))
        self._text = self._label(NSMakeRect(0, 38, self.W, 14), "正在启动服务…", 11.0,
                                 NSFontWeightRegular, NSColor.secondaryLabelColor())
        fx.addSubview_(self._text)

        spin = NSProgressIndicator.alloc().initWithFrame_(NSMakeRect(139, 8, 22, 22))
        spin.setStyle_(NSProgressIndicatorSpinningStyle)
        spin.setControlSize_(NSControlSizeSmall)
        spin.setDisplayedWhenStopped_(True)
        spin.startAnimation_(None)
        self._spin = spin
        fx.addSubview_(spin)

        self._win = win

    @staticmethod
    def _label(frame, text, size, weight, color):
        from AppKit import NSTextField, NSTextAlignmentCenter, NSFont
        tf = NSTextField.alloc().initWithFrame_(frame)
        tf.setStringValue_(text)
        tf.setBezeled_(False)
        tf.setDrawsBackground_(False)
        tf.setEditable_(False)
        tf.setSelectable_(False)
        tf.setAlignment_(NSTextAlignmentCenter)
        tf.setFont_(NSFont.systemFontOfSize_weight_(size, weight))
        tf.setTextColor_(color)
        return tf

    def show(self):
        self._shown_at = time.time()
        self._alpha = 0.0
        self._win.setAlphaValue_(0.0)
        self._win.orderFrontRegardless()

    def set_text(self, s):
        if self._text is not None:
            self._text.setStringValue_(s)

    def pump(self, interval=0.05):
        """手动泵一次 AppKit 事件（转圈动画与淡入依赖这里）。仅限主线程。"""
        if self._win is None:
            return
        from AppKit import NSApplication, NSEventMaskAny, NSDefaultRunLoopMode
        from Foundation import NSDate
        app = NSApplication.sharedApplication()
        ev = app.nextEventMatchingMask_untilDate_inMode_dequeue_(
            NSEventMaskAny, NSDate.dateWithTimeIntervalSinceNow_(interval),
            NSDefaultRunLoopMode, True)
        if ev is not None:
            app.sendEvent_(ev)
        if self._alpha < 1.0:
            self._alpha = min(1.0, self._alpha + 0.2)
            self._win.setAlphaValue_(self._alpha)

    def finish(self, min_total=1.2):
        """保证总停留不少于 min_total 秒（让「正在打开浏览器…」能被看到），随后淡出关闭。"""
        if self._win is None:
            return
        left = min_total - (time.time() - self._shown_at)
        while left > 0:
            step = min(0.05, left)
            self.pump(step)
            left -= step
        a = 1.0
        while a > 0.0:
            a = max(0.0, a - 0.12)
            self._win.setAlphaValue_(a)
            self.pump(0.03)
        try:
            self._spin.stopAnimation_(None)
            self._win.orderOut_(None)
            self._win.close()
        except Exception:
            pass
        self._win = None


def _bootstrap_server(from_restart, on_stage=None):
    """准备网络 → 杀旧实例 → 绑端口 → 起服务 → 局域网广播 → 生成二维码。

    返回 (server, qr_path)；端口始终绑不上时返回 (None, None)。
    on_stage(文案)：可选，用于闪屏同步启动阶段（只在后台线程调用）。
    """
    if on_stage:
        on_stage("正在准备网络…")
    rebuild_connect_url()
    _log("[启动] %s | darwin | frozen=%s" % (APP_VERSION_FULL, getattr(sys, "frozen", False)))

    # 单实例：先结束旧实例再绑定
    if from_restart:
        _kill_port_occupant(PORT)
        time.sleep(0.6)
    else:
        _kill_other_instances()

    if on_stage:
        on_stage("正在启动服务…")
    # 绑定 HTTP 端口（旧实例刚杀/刚升级完，端口释放需要时间，重试约 45 秒）
    server = None
    for _attempt in range(45):
        try:
            ThreadingHTTPServer.allow_reuse_address = False
            server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
            break
        except OSError:
            if _attempt == 2:
                # 可能是上一实例/升级残留，再清一次
                _kill_other_instances()
            if _attempt >= 3 and not _probe_live_instance(PORT):
                # 端口上没有任何易码互传应答 → 是上一次退出留下的 TIME_WAIT
                # 残留（macOS 可长达 30 秒），不会造成双实例，允许复用立即启动
                try:
                    ThreadingHTTPServer.allow_reuse_address = True
                    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
                    _log("[启动] 端口 %d 处于 TIME_WAIT 释放期，已安全接管" % PORT)
                    break
                except OSError:
                    pass
            time.sleep(1.0)
    if server is None:
        return None, None

    start_discovery()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, save_qr_image(CONNECT_URL)


# ---------- 控制面板窗口（Dock / 强制退出可见，可一键关闭服务）----------
try:
    from Foundation import NSObject as _NSObject
except ImportError:  # 无 pyobjc 的环境退化为普通类（仅影响面板，菜单栏本就不可用）
    _NSObject = object

_PANEL_CONTROLLER = [None]  # 单例引用（容器避免 global 声明）


class _PanelController(_NSObject):
    """原生控制面板：显示运行状态，提供「打开控制台 / 关闭服务」按钮。"""

    # 属性（_cb/_win）在实例化后由 show_control_panel 赋值 ——
    # 不给 pyobjc 类写自定义 init：零参 super().init() 在此环境不可用。

    def _build(self):
        from AppKit import (
            NSWindow, NSButton, NSColor, NSMakeRect,
            NSTitledWindowMask, NSClosableWindowMask, NSMiniaturizableWindowMask,
            NSBackingStoreBuffered, NSRoundedBezelStyle,
            NSFontWeightSemibold, NSFontWeightRegular,
        )
        W, H = 340, 212
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, W, H),
            NSTitledWindowMask | NSClosableWindowMask | NSMiniaturizableWindowMask,
            NSBackingStoreBuffered, False)
        win.setTitle_("%s · 控制面板" % APP_NAME)
        win.setReleasedWhenClosed_(False)
        win.center()

        title = _Splash._label(NSMakeRect(0, 158, W, 22),
                               "%s · 服务运行中" % APP_NAME, 13.0,
                               NSFontWeightSemibold, NSColor.labelColor())
        info = _Splash._label(NSMakeRect(0, 102, W, 50),
                              "连接地址：%s\n接收目录：%s" % (CONNECT_URL, RECEIVE_DIR),
                              10.5, NSFontWeightRegular, NSColor.secondaryLabelColor())
        ver = _Splash._label(NSMakeRect(0, 80, W, 16), APP_VERSION_FULL, 10.0,
                             NSFontWeightRegular, NSColor.tertiaryLabelColor())
        cv = win.contentView()
        cv.addSubview_(title)
        cv.addSubview_(info)
        cv.addSubview_(ver)

        b_open = NSButton.alloc().initWithFrame_(NSMakeRect(26, 26, 138, 34))
        b_open.setTitle_("打开控制台")
        b_open.setBezelStyle_(NSRoundedBezelStyle)
        b_open.setTag_(0)
        b_open.setTarget_(self)
        b_open.setAction_("panelAction:")
        cv.addSubview_(b_open)

        b_quit = NSButton.alloc().initWithFrame_(NSMakeRect(176, 26, 138, 34))
        b_quit.setTitle_("关闭服务")
        b_quit.setBezelStyle_(NSRoundedBezelStyle)
        b_quit.setTag_(1)
        b_quit.setTarget_(self)
        b_quit.setAction_("panelAction:")
        try:  # 红字提示这是退出操作
            from Foundation import NSMakeRange
            from AppKit import (NSMutableAttributedString,
                                NSForegroundColorAttributeName)
            attr = NSMutableAttributedString.alloc().initWithString_("关闭服务")
            attr.addAttribute_value_range_(NSForegroundColorAttributeName,
                                           NSColor.systemRedColor(),
                                           NSMakeRange(0, 4))
            b_quit.setAttributedTitle_(attr)
        except Exception:
            pass
        cv.addSubview_(b_quit)

        win.setDelegate_(self)
        self._win = win

    def show(self):
        from AppKit import NSApplication
        if self._win is None:
            self._build()
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self._win.makeKeyAndOrderFront_(None)

    def panelAction_(self, sender):
        cb = self._cb or {}
        if sender.tag() == 1:
            cb.get("quit")()
        else:
            cb.get("console")()

    def windowWillClose_(self, notification):
        self._win = None


def show_control_panel(console_cb, quit_cb):
    """打开（或前置）控制面板窗口。"""
    if _PANEL_CONTROLLER[0] is None:
        ctl = _PanelController.alloc().init()
        ctl._cb = {"console": console_cb, "quit": quit_cb}
        ctl._win = None
        _PANEL_CONTROLLER[0] = ctl
    _PANEL_CONTROLLER[0].show()


def _install_dock_reopen(show_fn):
    """rumps 的 NSApplication 委托是它内部的 NSApp 类；
    给它补上 applicationShouldHandleReopen，点击 Dock 图标即弹出控制面板。"""
    try:
        import rumps.rumps as _rr

        def _reopen(self, application, flag):
            try:
                show_fn()
            except Exception as e:
                _log("[面板] Dock 点击打开面板失败：%s" % e)
            return True

        _rr.NSApp.applicationShouldHandleReopen_hasVisibleWindows_ = _reopen
        _log("[面板] Dock 点击已接管 → 控制面板")
    except Exception as e:
        _log("[面板] Dock 点击接管失败：%s" % e)


# ---------- 菜单栏（rumps / NSStatusItem 原生）----------
def run_menu_bar(qr_path):
    import rumps

    app_ref = {}

    class YimaApp(rumps.App):
        def __init__(self):
            super().__init__(
                APP_NAME,
                icon=str(ICON_PNG) if ICON_PNG.exists() else None,
                quit_button=None,
            )
            self.menu = [
                rumps.MenuItem("控制面板…", callback=self.on_panel),
                None,
                rumps.MenuItem("打开控制台", callback=self.on_console),
                rumps.MenuItem("打开手机连接页", callback=self.on_phone),
                rumps.MenuItem("显示二维码", callback=self.on_qr),
                None,
                rumps.MenuItem("打开接收目录", callback=self.on_folder),
                rumps.MenuItem("复制连接地址", callback=self.on_copy),
                rumps.MenuItem("设置…", callback=self.on_settings),
                None,
                rumps.MenuItem("检查更新…", callback=self.on_check_update),
                rumps.MenuItem("当前版本 %s" % APP_VERSION_FULL, callback=None),
                None,
                rumps.MenuItem("重启服务", callback=self.on_restart),
                rumps.MenuItem("退出易码互传", callback=self.on_quit),
            ]
            self._last_url = CONNECT_URL
            # IP 变化看门狗：每 5 秒校验一次，变了就刷新二维码
            rumps.Timer(self._watch_ip, 5).start()
            # 点击 Dock 图标 → 弹出控制面板
            _install_dock_reopen(self.on_panel)

        def on_panel(self, sender=None):
            def _console():
                _open_path("http://localhost:%d/console" % PORT)

            def _quit():
                _log("[退出] 用户从控制面板关闭服务")
                rumps.quit_application()

            try:
                show_control_panel(_console, _quit)
                _log("[面板] 控制面板已打开")
            except Exception as e:
                _log("[面板] 打开控制面板失败：%s" % e)
                _notify(APP_NAME, "控制面板打开失败，可用菜单栏「打开控制台」")
                _open_path("http://localhost:%d/console" % PORT)

        def _watch_ip(self, sender):
            try:
                url = rebuild_connect_url()
                if url != self._last_url:
                    _log("[IP] 连接地址变化: %s -> %s" % (self._last_url, url))
                    self._last_url = url
                    save_qr_image(url)
                    _notify(APP_NAME, "连接地址已更新：%s" % url)
            except Exception:
                pass

        def on_console(self, sender):
            _open_path("http://localhost:%d/console" % PORT)

        def on_phone(self, sender):
            _open_path(CONNECT_URL)

        def on_qr(self, sender):
            p = save_qr_image(self._last_url)
            if p and Path(p).exists():
                _open_path(p)
            else:
                _notify(APP_NAME, "二维码生成失败（未安装 segno）")

        def on_folder(self, sender):
            _open_path(RECEIVE_DIR)

        def on_copy(self, sender):
            if _copy_to_clipboard(self._last_url):
                _notify(APP_NAME, "已复制连接地址：%s" % self._last_url)

        def on_settings(self, sender):
            _open_path("http://localhost:%d/settings" % PORT)

        def on_check_update(self, sender):
            if not update_feed_url():
                _notify(APP_NAME, "尚未配置更新源，请在「设置 → 在线升级」中填写清单地址")
                _open_path("http://localhost:%d/settings" % PORT)
                return
            _notify(APP_NAME, "正在检查更新…")

            def _work():
                st = check_update()
                phase = st.get("phase")
                if phase == "available":
                    m = st.get("manifest") or {}
                    notes = m.get("notes") or []
                    tip = "；".join(notes[:2]) if notes else "可在控制台一键升级"
                    _notify(APP_NAME, "发现新版本 %s：%s" % (m.get("latest", ""), tip))
                    _open_path("http://localhost:%d/console#update" % PORT)
                elif phase == "uptodate":
                    _notify(APP_NAME, "当前已是最新版本 %s" % APP_VERSION_FULL)
                else:
                    _notify(APP_NAME, st.get("error") or "检查更新失败")

            threading.Thread(target=_work, daemon=True).start()

        def on_restart(self, sender):
            threading.Thread(target=_restart_and_exit, daemon=True).start()

        def on_quit(self, sender):
            _log("[退出] 用户从菜单栏退出")
            rumps.quit_application()

    app_ref["app"] = YimaApp()
    app_ref["app"].run()


# ---------- main ----------
def main():
    import argparse
    parser = argparse.ArgumentParser(description="易码互传 · macOS 原生版")
    parser.add_argument("--console", action="store_true", help="终端模式运行（调试用，Ctrl+C 停止）")
    parser.add_argument("--no-open", action="store_true", help="启动时不自动打开控制台页面")
    parser.add_argument("--install-autostart", action="store_true", help="安装开机自启并退出")
    parser.add_argument("--uninstall-autostart", action="store_true", help="取消开机自启并退出")
    parser.add_argument("--from-restart", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--version", action="store_true", help="打印版本号并退出")
    parser.add_argument("--check-update", action="store_true", help="检查更新并打印结果（不启动服务）")
    args = parser.parse_args()

    if args.version:
        print("%s %s" % (APP_NAME, APP_VERSION_FULL))
        return

    if args.check_update:
        print("%s %s" % (APP_NAME, APP_VERSION_FULL))
        print("更新源：%s" % (update_feed_url() or "（未配置）"))
        st = check_update()
        print("状态  ：%s" % st.get("phase"))
        print("说明  ：%s" % st.get("message"))
        m = st.get("manifest") or {}
        if m:
            print("远端版本：%s（构建 %s）" % (m.get("latest", ""), m.get("build", "")))
            for n in (m.get("notes") or []):
                print("  · %s" % n)
        if st.get("error"):
            print("错误  ：%s" % st["error"])
        return

    if args.install_autostart:
        install_autostart()
        return
    if args.uninstall_autostart:
        uninstall_autostart()
        return

    # 启动后自动检查更新（后台，不阻塞；仅在已配置更新源时）
    if CONFIG.get("update_check_on_start", True) and update_feed_url():
        def _auto_check():
            time.sleep(6)
            st = check_update()
            if st.get("phase") == "available":
                m = st.get("manifest") or {}
                _notify(APP_NAME, "发现新版本 %s，可在控制台一键升级" % m.get("latest", ""))
        threading.Thread(target=_auto_check, daemon=True).start()

    if args.console:
        server, qr_path = _bootstrap_server(args.from_restart)
        if server is None:
            _log("[FATAL] 端口 %d 被占用，启动失败" % PORT)
            _show_port_conflict_dialog(PORT)
            sys.exit(1)
        print("=" * 56)
        print("%s 已启动（终端模式）" % APP_VERSION_FULL)
        print("  本机控制台 : http://localhost:%d/console" % PORT)
        print("  手机访问   : %s   (同一 WiFi 下)" % CONNECT_URL)
        print("  接收目录   : %s" % RECEIVE_DIR)
        print("  按Ctrl+C停止")
        print("=" * 56)
        if qr_path:
            print("  二维码图片：%s" % qr_path)
            _print_text_qr(CONNECT_URL)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            print("\n已停止。")
        return

    # 菜单栏模式
    if not HAVE_RUMPS:
        _log("[FATAL] rumps 未安装，无法启动菜单栏。pip install rumps")
        _notify(APP_NAME, "缺少 rumps 组件，无法启动菜单栏。请重新打包或 pip install rumps")
        sys.exit(1)

    open_console = CONFIG.get("open_console_on_start", True) and not args.no_open

    boot = {"done": False, "server": None, "qr": None, "fatal": None, "stage": ""}

    def _boot():
        def _stage(s):
            boot["stage"] = s
        try:
            server, qr = _bootstrap_server(args.from_restart, on_stage=_stage)
            if server is None:
                boot["fatal"] = "port"
            else:
                boot["server"], boot["qr"] = server, qr
        except Exception as exc:
            boot["fatal"] = exc
        finally:
            boot["done"] = True

    # 启动闪屏：起服务期间给用户视觉反馈；创建失败自动回退同步启动，不影响可用性
    splash = None
    if _splash_enabled():
        try:
            splash = _Splash()
            splash.show()
        except Exception as exc:
            _log("[闪屏] 创建失败，回退同步启动：%r" % (exc,))
            splash = None

    if splash:
        threading.Thread(target=_boot, daemon=True).start()
        last_stage = None
        while not boot["done"]:
            stage = boot["stage"]
            if stage and stage != last_stage:
                splash.set_text(stage)
                last_stage = stage
            splash.pump()
    else:
        _boot()

    if boot["fatal"]:
        if splash:
            splash.finish(min_total=0.2)
        if boot["fatal"] == "port":
            _log("[FATAL] 端口 %d 被占用，启动失败" % PORT)
            _show_port_conflict_dialog(PORT)
        else:
            _log("[FATAL] 启动异常：%r" % (boot["fatal"],))
            _notify(APP_NAME, "启动失败：%s" % boot["fatal"])
        sys.exit(1)

    if splash:
        splash.set_text("正在打开浏览器…" if open_console else "启动完成")

    if open_console:
        _open_path("http://localhost:%d/console" % PORT)

    if splash:
        splash.finish(min_total=1.2)   # 「正在打开浏览器…」至少停留一秒多再淡出

    _notify(APP_NAME, "服务已启动：%s" % CONNECT_URL)
    run_menu_bar(boot["qr"])


if __name__ == "__main__":
    main()

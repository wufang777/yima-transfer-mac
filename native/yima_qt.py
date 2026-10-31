#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
易码互传 · Qt 原生客户端（macOS 原生观感 v2）

不依赖浏览器：所有操作走本机 HTTP 服务（与网页版同一套协议），
界面是原生窗口（Apple Human Interface 风格：左侧导航 + 卡片式内容）。
服务端核心直接复用 app.py，因此协议、断点续传、后台发送等能力与网页版完全一致。

用法：
    python3 native/yima_qt.py
    python3 native/yima_qt.py --smoke-all /tmp/ui      # 离屏渲染每个页面，用于自检
"""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import ThreadingHTTPServer as _THS
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

IS_WIN = (os.name == "nt")
IS_MAC = (sys.platform == "darwin")
_win_dir = ROOT / "win"
if IS_WIN and str(_win_dir) not in sys.path:
    sys.path.insert(0, str(_win_dir))

# 服务端核心：Mac 用 app.py，Windows 用同协议的 win/yima_win.py（两端 API 完全对齐）
if IS_WIN:
    import yima_win as core  # noqa: E402
else:
    import app as core  # noqa: E402

from PySide6.QtCore import Qt, QTimer, Signal, QObject, QThread, QSize, QSettings  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QIcon, QAction, QFont, QPixmap, QPainter, QColor,
)
from PySide6.QtSvg import QSvgRenderer  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QListWidget, QListWidgetItem, QFileDialog, QTextEdit,
    QStackedWidget, QProgressBar, QMessageBox, QLineEdit, QFormLayout,
    QStatusBar, QAbstractItemView, QScrollArea, QCheckBox, QComboBox,
    QMenu, QSizePolicy, QFrame, QDialog, QInputDialog, QStyle,
)

APP_NAME = "易码互传"
APP_COMPANY = "易码通科技"
CHUNK = 2 * 1024 * 1024
ACCENT = "#007AFF"

# 侧栏导航图标（24×24 线稿 SVG，按主题着色，避免使用彩色 emoji）
NAV_ICONS = {
    "设备": ('<svg viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="12" rx="2.5"/>'
             '<path d="M1.5 19.2h21"/></svg>'),
    "发送": ('<svg viewBox="0 0 24 24"><path d="M12 16.5V5.2"/>'
             '<path d="M7.4 9.8 12 5.2l4.6 4.6"/>'
             '<path d="M4.5 15v3.2a2 2 0 0 0 2 2h11a2 2 0 0 0 2-2V15"/></svg>'),
    "文字": ('<svg viewBox="0 0 24 24"><path d="M20.5 11.6a7.9 7.9 0 0 1-11.4 7L4 20.2l1.4-3.6'
             'a7.9 7.9 0 1 1 15.1-5z"/></svg>'),
    "文件": ('<svg viewBox="0 0 24 24"><path d="M3.2 7.4a2.4 2.4 0 0 1 2.4-2.4h3.3l2.2 2.2h8.3'
             'a2.4 2.4 0 0 1 2.4 2.4v8.4a2.4 2.4 0 0 1-2.4 2.4H5.6a2.4 2.4 0 0 1-2.4-2.4z"/></svg>'),
    "设置": ('<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3.3"/>'
             '<path d="M12 2.9v2.6M12 18.5v2.6M4.9 4.9l1.8 1.8M17.3 17.3l1.8 1.8'
             'M2.9 12h2.6M18.5 12h2.6M4.9 19.1l1.8-1.8M17.3 6.7l1.8-1.8"/></svg>'),
}


def fmt_size(n):
    try:
        n = float(n)
    except Exception:
        return "-"
    if n < 1024:
        return "%d B" % n
    if n < 1048576:
        return "%.1f KB" % (n / 1024)
    if n < 1073741824:
        return "%.1f MB" % (n / 1048576)
    return "%.2f GB" % (n / 1073741824)


def system_dark_mode():
    """读取系统外观（浅色/深色）。不支持时返回 False。"""
    if sys.platform == "darwin":
        try:
            out = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"],
                                 capture_output=True, text=True, timeout=2).stdout.strip()
            return out.endswith("Dark")
        except Exception:
            return False
    if os.name == "nt":
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                 r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
            val, _t = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return int(val) == 0
        except Exception:
            return False
    return False


def open_path_system(target):
    """用系统默认方式打开路径 / 网址（Mac: open，Windows: startfile）。"""
    target = str(target)
    try:
        if os.name == "nt" and not target.lower().startswith(("http://", "https://")):
            os.startfile(target)  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", target])
        else:
            subprocess.Popen(["xdg-open", target])
    except Exception:
        pass


def open_url(url):
    try:
        webbrowser.open(str(url))
    except Exception:
        open_path_system(url)


def reveal_path(path):
    """在资源管理器 / Finder 中选中该文件。"""
    path = str(path)
    try:
        if os.name == "nt":
            subprocess.Popen(["explorer", '/select,"%s"' % path])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path)])
    except Exception:
        pass


def bootstrap_server():
    """拉起本机服务（成功返回 server 对象，失败返回 None）。

    Mac 的 app.py 有 _bootstrap_server；Windows 的 yima_win.py 没有，
    这里用同一套参数自行绑定，保证两端行为一致。
    """
    fn = getattr(core, "_bootstrap_server", None)
    if callable(fn):
        server, _qr = fn(False)
        return server

    port = int(getattr(core, "PORT", core.CONFIG.get("port", 8000)))
    server = None
    for attempt in range(30):
        try:
            _THS.allow_reuse_address = False
            server = _THS(("0.0.0.0", port), core.Handler)
            break
        except OSError:
            if attempt >= 3 and not core._probe_live_instance(port):
                try:
                    _THS.allow_reuse_address = True
                    server = _THS(("0.0.0.0", port), core.Handler)
                    break
                except OSError:
                    pass
            time.sleep(1.0)
    if server is None:
        return None
    core.rebuild_connect_url()
    core.start_discovery()
    core._resume_pending_jobs()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        if hasattr(core, "ensure_firewall_rules"):
            core.ensure_firewall_rules()
    except Exception:
        pass
    return server


def nav_icon(name, active=False, size=20):
    """用 SVG 线稿生成单色图标（选中态用品牌蓝）。"""
    svg = NAV_ICONS.get(name)
    if not svg:
        return QIcon()
    color = ACCENT if active else ("#FFFFFF" if system_dark_mode() else "#3C3C43")
    body = svg.replace("<svg ", '<svg fill="none" stroke="%s" stroke-width="1.7" '
                                'stroke-linecap="round" stroke-linejoin="round" ' % color, 1)
    renderer = QSvgRenderer(bytearray(body.encode("utf-8")))
    pm = QPixmap(size * 2, size * 2)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    renderer.render(painter)
    painter.end()
    return QIcon(pm)


# ---------------------------------------------------------------- 皮肤 ----
def _qss(dark):
    if dark:
        c_text = "#F2F2F7"
        c_sub = "#98989F"
        c_bg = "#1E1E1E"
        c_sidebar = "#262628"
        c_card = "#2C2C2E"
        c_border = "#3A3A3C"
        c_field = "#1C1C1E"
        c_hover = "#3A3A3C"
        c_groove = "#3A3A3C"
        c_sep = "#3A3A3C"
    else:
        c_text = "#1D1D1F"
        c_sub = "#86868B"
        c_bg = "#F5F5F7"
        c_sidebar = "#EFEFF2"
        c_card = "#FFFFFF"
        c_border = "#D8D8DC"
        c_field = "#FFFFFF"
        c_hover = "#EFEFF2"
        c_groove = "#E5E5EA"
        c_sep = "#E3E3E6"

    btn_top = "#3B3B3D" if dark else "#FFFFFF"
    btn_bot = "#333335" if dark else "#F3F3F5"

    return """
    QWidget { color: %(text)s; font-family: "PingFang SC", "Microsoft YaHei UI", "Microsoft YaHei", "Helvetica Neue", "Segoe UI", sans-serif;
              font-size: 13px; }
    QWidget#root { background: %(bg)s; }
    QWidget#sidebar { background: %(sidebar)s; }
    QWidget#content { background: %(bg)s; }

    QLabel#appname { font-size: 15px; font-weight: 600; }
    QLabel#appver  { font-size: 11px; color: %(sub)s; }
    QLabel#pagetitle { font-size: 19px; font-weight: 600; }
    QLabel#pagesub  { font-size: 12px; color: %(sub)s; }
    QLabel#sec      { font-size: 13px; font-weight: 600; }
    QLabel#hint     { font-size: 12px; color: %(sub)s; }
    QLabel#pill     { background: %(card)s; border: 1px solid %(border)s; border-radius: 10px;
                      padding: 3px 10px; font-size: 12px; color: %(sub)s; }

    QWidget.card { background: %(card)s; border: 1px solid %(border)s; border-radius: 10px; }
    QWidget.sep  { background: %(sep)s; max-height: 1px; }

    QPushButton { background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 %(b_top)s, stop:1 %(b_bot)s);
                  border: 1px solid %(border)s; border-radius: 6px; padding: 5px 14px;
                  min-height: 20px; color: %(text)s; }
    QPushButton:hover { background: %(hover)s; }
    QPushButton:pressed { background: %(groove)s; }
    QPushButton:disabled { color: %(sub)s; background: %(bg)s; border-color: %(border)s; }
    QPushButton[accent="true"] { background: %(accent)s; border-color: %(accent)s; color: #FFFFFF; }
    QPushButton[accent="true"]:hover { background: #0A84FF; }
    QPushButton[accent="true"]:pressed { background: #0060C8; }
    QPushButton[flatBtn="true"] { border: none; background: transparent; color: %(accent)s; padding: 4px 6px; }
    QPushButton[flatBtn="true"]:hover { background: %(hover)s; border-radius: 6px; }

    QLineEdit, QTextEdit, QComboBox { background: %(field)s; border: 1px solid %(border)s;
                  border-radius: 6px; padding: 6px 8px; selection-background-color: %(accent)s; }
    QLineEdit:focus, QTextEdit:focus, QComboBox:focus { border-color: %(accent)s; }
    QComboBox::drop-down { border: none; width: 20px; }
    QComboBox QAbstractItemView { background: %(card)s; border: 1px solid %(border)s;
                  selection-background-color: %(accent)s; selection-color: #FFFFFF; }
    QTextEdit { padding: 10px; }

    QListWidget, QListView { background: %(card)s; border: 1px solid %(border)s;
                  border-radius: 8px; padding: 4px; outline: 0; }
    QListWidget::item { padding: 7px 10px; border-radius: 6px; }
    QListWidget::item:hover { background: %(hover)s; }
    QListWidget::item:selected { background: %(accent)s; color: #FFFFFF; }

    QListWidget#nav { background: transparent; border: none; padding: 4px 8px; }
    QListWidget#nav::item { padding: 8px 10px; border-radius: 6px; margin: 1px 0; color: %(text)s; }
    QListWidget#nav::item:hover { background: %(hover)s; }
    QListWidget#nav::item:selected { background: %(accent)s; color: #FFFFFF; }

    QProgressBar { background: %(groove)s; border: none; border-radius: 4px; max-height: 8px; }
    QProgressBar::chunk { background: %(accent)s; border-radius: 4px; }

    QScrollArea { background: transparent; border: none; }
    QScrollArea > QWidget > QWidget { background: transparent; }
    QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
    QScrollBar::handle:vertical { background: %(border)s; border-radius: 5px; min-height: 24px; }
    QScrollBar::handle:vertical:hover { background: %(sub)s; }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
    QStatusBar { background: %(sidebar)s; color: %(sub)s; font-size: 12px; }
    QMenuBar { background: %(sidebar)s; }
    QMenuBar::item:selected { background: transparent; color: %(accent)s; }
    QMenu { background: %(card)s; border: 1px solid %(border)s; padding: 4px; }
    QMenu::item { padding: 5px 22px 5px 12px; border-radius: 5px; }
    QMenu::item:selected { background: %(accent)s; color: #FFFFFF; }
    QCheckBox::indicator { width: 15px; height: 15px; border-radius: 4px;
                  border: 1px solid %(border)s; background: %(field)s; }
    QCheckBox::indicator:checked { background: %(accent)s; border-color: %(accent)s; }
    QToolTip { background: %(card)s; color: %(text)s; border: 1px solid %(border)s; padding: 4px; }
    """ % {
        "text": c_text, "sub": c_sub, "bg": c_bg, "sidebar": c_sidebar, "card": c_card,
        "border": c_border, "field": c_field, "hover": c_hover, "groove": c_groove,
        "sep": c_sep, "b_top": btn_top, "b_bot": btn_bot, "accent": ACCENT,
    }


def apply_skin(app, mode="auto"):
    """mode: auto / light / dark —— 重新套用自适应皮肤。"""
    dark = system_dark_mode() if mode == "auto" else (mode == "dark")
    app.setStyle("Fusion")
    from PySide6.QtGui import QPalette
    pal = QPalette()
    base = QColor("#1E1E1E" if dark else "#F5F5F7")
    card = QColor("#2C2C2E" if dark else "#FFFFFF")
    text = QColor("#F2F2F7" if dark else "#1D1D1F")
    pal.setColor(QPalette.Window, base)
    pal.setColor(QPalette.WindowText, text)
    pal.setColor(QPalette.Base, card)
    pal.setColor(QPalette.AlternateBase, base)
    pal.setColor(QPalette.Text, text)
    pal.setColor(QPalette.Button, card)
    pal.setColor(QPalette.ButtonText, text)
    pal.setColor(QPalette.Highlight, QColor(ACCENT))
    pal.setColor(QPalette.HighlightedText, QColor("#FFFFFF"))
    pal.setColor(QPalette.ToolTipBase, card)
    pal.setColor(QPalette.ToolTipText, text)
    app.setPalette(pal)
    app.setStyleSheet(_qss(dark))
    if sys.platform != "darwin":
        app.setFont(QFont("Microsoft YaHei UI", 9))
    else:
        app.setFont(QFont("PingFang SC", 13))
    return dark


# ---------------------------------------------------------------- API ----
class Api:
    """本机服务接口封装（全部走 127.0.0.1，不走系统代理）。"""

    def __init__(self, port):
        self.base = "http://127.0.0.1:%d" % int(port)
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _req(self, path, data=None, method="GET", ctype=None, timeout=20.0):
        url = self.base + path
        r = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            r.add_header("Content-Type", ctype or "application/octet-stream")
        return self._opener.open(r, timeout=timeout).read()

    def get_json(self, path, timeout=20.0):
        return json.loads(self._req(path, timeout=timeout).decode("utf-8", "ignore"))

    def get_bytes(self, path, timeout=15.0):
        return self._req(path, timeout=timeout)

    def post_json(self, path, obj, timeout=30.0):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        return json.loads(self._req(path, data=body, method="POST",
                                    ctype="application/json", timeout=timeout)
                          .decode("utf-8", "ignore"))

    def post_bytes(self, path, blob, timeout=120.0):
        raw = self._req(path, data=blob, method="POST",
                        ctype="application/octet-stream", timeout=timeout)
        try:
            return json.loads(raw.decode("utf-8", "ignore"))
        except Exception:
            return {"ok": False, "error": raw.decode("utf-8", "ignore")[:120]}


# ------------------------------------------------------------ 控件工具 ----
def make_card(title=None, subtitle=None):
    """Apple 风格分组卡片：圆角白底 + 可选标题/副标题。"""
    card = QWidget()
    card.setProperty("class", "card")
    v = QVBoxLayout(card)
    v.setContentsMargins(16, 14, 16, 14)
    v.setSpacing(10)
    if title:
        lb = QLabel(title)
        lb.setObjectName("sec")
        v.addWidget(lb)
    if subtitle:
        sb = QLabel(subtitle)
        sb.setObjectName("hint")
        sb.setWordWrap(True)
        v.addWidget(sb)
    return card, v


def hsep():
    line = QWidget()
    line.setProperty("class", "sep")
    line.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    return line


class DirRow(QWidget):
    """接收目录选择行：只读路径 + 常用位置 + 更改… + 在 Finder 中显示。"""

    changed = Signal(str)

    def __init__(self, initial="", parent=None, compact=False):
        super().__init__(parent)
        self._path = initial
        self.edit = QLineEdit(initial)
        self.edit.setReadOnly(True)
        self.edit.setMinimumWidth(300 if not compact else 220)

        self.btn_common = QPushButton("常用位置")
        menu = QMenu(self)
        home = str(Path.home())
        presets = [
            ("桌面", os.path.join(home, "Desktop")),
            ("下载", os.path.join(home, "Downloads")),
            ("文稿", os.path.join(home, "Documents")),
            ("影片", os.path.join(home, "Movies")),
            ("默认接收目录", str(Path(core.APP_DIR) / "received")),
        ]
        for label, path in presets:
            act = QAction(label, self)
            act.triggered.connect(lambda _c=False, p=path: self.set_path(p))
            menu.addAction(act)
        menu.addSeparator()
        act_other = QAction("选择其它目录…", self)
        act_other.triggered.connect(self.choose)
        menu.addAction(act_other)
        self.btn_common.setMenu(menu)

        self.btn_pick = QPushButton("更改…")
        self.btn_pick.clicked.connect(self.choose)
        self.btn_open = QPushButton("在 Finder 中显示")
        self.btn_open.clicked.connect(self.reveal)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(self.edit, 1)
        row.addWidget(self.btn_common)
        row.addWidget(self.btn_pick)
        row.addWidget(self.btn_open)

    def path(self):
        return self._path

    def set_path(self, path):
        path = str(path)
        if path and path != self._path:
            self._path = path
            self.edit.setText(path)
            self.changed.emit(path)

    def choose(self):
        start = self._path if os.path.isdir(self._path) else str(Path.home())
        picked = QFileDialog.getExistingDirectory(
            self, "选择接收文件的目录", start,
            QFileDialog.ShowDirsOnly | QFileDialog.DontResolveSymlinks)
        if picked:
            self.set_path(picked)

    def reveal(self):
        p = self._path
        try:
            if not os.path.isdir(p):
                os.makedirs(p, exist_ok=True)
            open_path_system(p)
        except Exception as e:
            QMessageBox.warning(self, "打不开目录", str(e))


# -------------------------------------------------------- 后台发送线程 ----
class SendWorker(QObject):
    """后台把文件分片缓冲到本机 / 目标设备（不阻塞界面）。"""
    progress = Signal(int, int)          # 已传, 总大小
    finished = Signal(bool, str)

    def __init__(self, api, path, target):
        super().__init__()
        self.api = api
        self.path = path
        self.target = target             # None=发到本机(手机可见)，否则为 peer dict

    def run(self):
        name = os.path.basename(self.path)
        try:
            size = os.path.getsize(self.path)
            if self.target:
                init = self.api.post_json("/api/peers/init", {
                    "name": name, "size": size,
                    "ip": self.target["ip"], "port": self.target["port"]})
                commit_path, commit_body = "/api/peers/commit", {"id": init["id"]}
            else:
                init = self.api.post_json("/upload/init", {"name": name, "size": size})
                if init.get("done"):
                    self.progress.emit(size, size)
                    self.finished.emit(True, "接收目录里已有同名同大小文件，无需重传")
                    return
                commit_path, commit_body = "/upload/complete", {"id": init["id"]}

            if not init.get("ok"):
                self.finished.emit(False, init.get("error", "初始化失败"))
                return

            offset = int(init.get("offset", 0) or 0)
            with open(self.path, "rb") as fh:
                while offset < size:
                    fh.seek(offset)
                    blob = fh.read(min(CHUNK, size - offset))
                    if not blob:
                        break
                    r = self.api.post_bytes(
                        "/upload/chunk?id=%s&offset=%d" % (init["id"], offset), blob)
                    if isinstance(r, dict) and not r.get("ok"):
                        self.finished.emit(False, r.get("error", "分片失败"))
                        return
                    offset = int(r.get("offset", offset))
                    self.progress.emit(offset, size)

            fin = self.api.post_json(commit_path, commit_body)
            if not fin.get("ok"):
                self.finished.emit(False, fin.get("error", "收尾失败"))
                return
            if self.target:
                self.finished.emit(True, "已交给后台发送任务，可关闭窗口")
            else:
                self.finished.emit(True, "已保存到接收目录")
        except urllib.error.HTTPError as e:
            self.finished.emit(False, "HTTP %d" % e.code)
        except Exception as e:
            self.finished.emit(False, str(e))


# ---------------------------------------------------------------- 主窗 ----
class MainWindow(QMainWindow):
    NAV = ["设备", "发送", "文字", "文件", "设置"]

    def __init__(self, app_ref, api, port, attached):
        super().__init__()
        self.app_ref = app_ref
        self.api = api
        self.port = port
        self.attached = attached
        self.jobs = []
        self._settings = {}
        self.setWindowTitle(APP_NAME)
        self.resize(1060, 720)
        self.setMinimumSize(960, 660)
        icon = ROOT / "assets" / "icon.png"
        if icon.exists():
            self.setWindowIcon(QIcon(str(icon)))

        root = QWidget()
        root.setObjectName("root")
        lay = QHBoxLayout(root)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self._build_sidebar())
        lay.addWidget(self._build_content(), 1)
        self.setCentralWidget(root)

        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status.showMessage("服务运行中 · 端口 %d%s" %
                                (port, "（已接入本机现有服务）" if attached else ""))

        self.setMenuBar(self._build_menu())
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1500)
        self.refresh_peers()
        self.refresh_jobs()
        self.load_settings()

    # ---------- 侧栏 ----------
    def _build_sidebar(self):
        side = QWidget()
        side.setObjectName("sidebar")
        side.setFixedWidth(224)
        v = QVBoxLayout(side)
        v.setContentsMargins(16, 18, 16, 14)
        v.setSpacing(10)

        head = QHBoxLayout()
        logo = ROOT / "assets" / "icon.png"
        if logo.exists():
            pm = QPixmap(str(logo)).scaled(40, 40, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            lb = QLabel()
            lb.setPixmap(pm)
            head.addWidget(lb)
        box = QVBoxLayout()
        nm = QLabel(APP_NAME)
        nm.setObjectName("appname")
        ver = QLabel("v%s · 构建 %s" % (getattr(core, "APP_VERSION_NUM", "?"),
                                        getattr(core, "APP_BUILD", "?")))
        ver.setObjectName("appver")
        box.addWidget(nm)
        box.addWidget(ver)
        head.addLayout(box, 1)
        v.addLayout(head)
        v.addSpacing(6)

        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setIconSize(QSize(20, 20))
        self.nav.setSpacing(0)
        for name in self.NAV:
            it = QListWidgetItem(QIcon(), name)
            it.setSizeHint(QSize(0, 34))
            self.nav.addItem(it)
        v.addWidget(self.nav, 1)

        self.side_conn = QLabel("未连接")
        self.side_conn.setObjectName("hint")
        self.side_conn.setWordWrap(True)
        v.addWidget(self.side_conn)
        self.btn_code = QPushButton("连接码")
        self.btn_code.setProperty("accent", True)
        self.btn_code.setProperty("accentRole", "primary")
        self.btn_code.clicked.connect(self.show_connect_code)
        v.addWidget(self.btn_code)
        return side

    def _sync_nav_icons(self):
        cur = self.nav.currentRow()
        for i in range(self.nav.count()):
            name = self.NAV[i]
            self.nav.item(i).setIcon(nav_icon(name, active=(i == cur)))
        self._dark_icons = None

    # ---------- 内容区 ----------
    def _build_content(self):
        content = QWidget()
        content.setObjectName("content")
        v = QVBoxLayout(content)
        v.setContentsMargins(28, 22, 28, 20)
        v.setSpacing(14)

        head = QHBoxLayout()
        box = QVBoxLayout()
        self.page_title = QLabel("设备")
        self.page_title.setObjectName("pagetitle")
        self.page_sub = QLabel("")
        self.page_sub.setObjectName("pagesub")
        box.addWidget(self.page_title)
        box.addWidget(self.page_sub)
        head.addLayout(box, 1)
        self.conn_pill = QLabel("运行中")
        self.conn_pill.setObjectName("pill")
        head.addWidget(self.conn_pill, 0, Qt.AlignRight)
        v.addLayout(head)

        self.stack = QStackedWidget()
        self.pages = {
            "设备": self._build_devices(),
            "发送": self._build_send(),
            "文字": self._build_text(),
            "文件": self._build_files(),
            "设置": self._build_settings(),
        }
        for name in self.NAV:
            w = self.pages[name]
            if isinstance(w, QScrollArea):
                self.stack.addWidget(w)
            else:
                sc = QScrollArea()
                sc.setWidgetResizable(True)
                sc.setFrameShape(QFrame.NoFrame)
                holder = QWidget()
                holder.setObjectName("content")
                hl = QVBoxLayout(holder)
                hl.setContentsMargins(0, 0, 8, 0)
                hl.setSpacing(14)
                hl.addWidget(w)
                hl.addStretch(1)
                sc.setWidget(holder)
                self.stack.addWidget(sc)
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.currentRowChanged.connect(self._on_nav_changed)
        self.nav.setCurrentRow(0)
        v.addWidget(self.stack, 1)
        return content

    def _on_nav_changed(self, row):
        name = self.NAV[row] if 0 <= row < len(self.NAV) else ""
        titles = {
            "设备": ("设备", "同一局域网内的其它电脑；手机请用「连接码」在浏览器打开"),
            "发送": ("发送 / 后台任务", "大文件分片缓冲后由后台任务发出，中断可续传"),
            "文字": ("文字互传", "给其它电脑或本机手机网页推送文字、网址、验证码"),
            "文件": ("接收的文件", "手机上传到本机的文件，以及本机分享给手机的内容"),
            "设置": ("设置", "接收目录、开机启动、通知与更新"),
        }
        t, s = titles.get(name, (name, ""))
        self.page_title.setText(t)
        self.page_sub.setText(s)
        self._sync_nav_icons()
        if name == "文件":
            self.refresh_files()
        elif name == "设置":
            self.load_settings()

    # ---------- 菜单 ----------
    def _build_menu(self):
        bar = self.menuBar()
        m_app = bar.addMenu(APP_NAME)
        about = QAction("关于 %s" % APP_NAME, self)
        about.setMenuRole(QAction.AboutRole)
        about.triggered.connect(self.show_about)
        m_app.addAction(about)
        code = QAction("连接码…", self)
        code.triggered.connect(self.show_connect_code)
        m_app.addAction(code)
        check = QAction("检查更新…", self)
        check.triggered.connect(self.check_update)
        m_app.addAction(check)
        m_app.addSeparator()
        quit_act = QAction("退出 %s" % APP_NAME, self)
        quit_act.setMenuRole(QAction.QuitRole)
        quit_act.setShortcut("Ctrl+Q")
        quit_act.triggered.connect(self.close)
        m_app.addAction(quit_act)

        m_file = bar.addMenu("文件")
        a1 = QAction("选择文件发送到设备…", self)
        a1.triggered.connect(self.send_file_to_peer)
        a2 = QAction("上传文件到本机…", self)
        a2.triggered.connect(self.send_file_local)
        a3 = QAction("分享给手机…", self)
        a3.triggered.connect(self.share_to_phone)
        for a in (a1, a2, a3):
            m_file.addAction(a)

        m_view = bar.addMenu("视图")
        dark = QAction("切换深色/浅色外观", self)
        dark.triggered.connect(self.toggle_skin)
        m_view.addAction(dark)
        return bar

    # ---------- 页①：设备 ----------
    def _build_devices(self):
        card, v = make_card("局域网内的电脑",
                            "选中一台后可向它发文件或发文字；列表每 1.5 秒自动刷新。")
        self.peer_list = QListWidget()
        self.peer_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.peer_list.setMinimumHeight(220)
        v.addWidget(self.peer_list)
        self.dev_hint = QLabel("")
        self.dev_hint.setObjectName("hint")
        v.addWidget(self.dev_hint)

        row1 = QHBoxLayout()
        self.btn_refresh = QPushButton("刷新设备")
        self.btn_scan = QPushButton("重新扫描局域网")
        self.btn_add_peer = QPushButton("手动添加…")
        self.btn_refresh.clicked.connect(self.refresh_peers)
        self.btn_scan.clicked.connect(self.scan)
        self.btn_add_peer.clicked.connect(self.add_peer)
        for b in (self.btn_refresh, self.btn_scan, self.btn_add_peer):
            row1.addWidget(b)
        row1.addStretch(1)
        v.addLayout(row1)

        v.addWidget(hsep())
        row2 = QHBoxLayout()
        self.btn_send_file = QPushButton("发送文件到选中设备")
        self.btn_send_file.setProperty("accent", True)
        self.btn_send_text = QPushButton("发送文字到选中设备")
        self.btn_send_file.clicked.connect(self.send_file_to_peer)
        self.btn_send_text.clicked.connect(lambda: self._goto("文字"))
        row2.addWidget(self.btn_send_file)
        row2.addWidget(self.btn_send_text)
        row2.addStretch(1)
        v.addLayout(row2)
        return card

    def refresh_peers(self):
        try:
            peers = self.api.get_json("/api/peers", timeout=5)
        except Exception as e:
            self.dev_hint.setText("获取设备失败：%s" % e)
            self.side_conn.setText("服务未响应")
            return
        keep = self._current_peer()
        self.peer_list.clear()
        for p in peers:
            item = QListWidgetItem("%s　（%s:%s）" % (p.get("name", "?"), p.get("ip"), p.get("port")))
            item.setData(Qt.UserRole, p)
            self.peer_list.addItem(item)
            if keep and str(p.get("ip")) == str(keep.get("ip")):
                self.peer_list.setCurrentItem(item)
        self.dev_hint.setText("共 %d 台设备" % len(peers))
        self.side_conn.setText("端口 %d · 已发现 %d 台设备" % (self.port, len(peers)))
        self.conn_pill.setText("服务运行中 · 端口 %d" % self.port)

    def scan(self):
        self.status.showMessage("正在扫描局域网…", 4000)
        try:
            self.api.get_json("/api/scan", timeout=60)
        except Exception:
            pass
        self.refresh_peers()
        self.status.showMessage("扫描完成", 3000)

    def add_peer(self):
        text, ok = QInputDialog.getText(self, "手动添加设备", "对方 IP（可加 :端口）：")
        if not ok or not text.strip():
            return
        value = text.strip()
        ip, port = value, None
        if ":" in value:
            ip, _, p = value.partition(":")
            try:
                port = int(p)
            except ValueError:
                port = None
        try:
            body = {"ip": ip}
            if port:
                body["port"] = port
            r = self.api.post_json("/api/peers/add", body, timeout=10)
            self.status.showMessage("已添加" if r.get("ok") else ("失败：" + str(r.get("error"))), 5000)
        except Exception as e:
            QMessageBox.warning(self, "添加失败", str(e))
        self.refresh_peers()

    def _current_peer(self):
        it = self.peer_list.currentItem()
        return it.data(Qt.UserRole) if it else None

    def _goto(self, name):
        if name in self.NAV:
            self.nav.setCurrentRow(self.NAV.index(name))

    # ---------- 页②：发送 / 后台任务 ----------
    def _build_send(self):
        card, v = make_card("发送文件",
                            "文件先分片缓冲到本机，再交给后台任务发出；中断后可用「断点续传」继续。")
        row = QHBoxLayout()
        self.btn_pick = QPushButton("选择文件发送到选中设备…")
        self.btn_pick.setProperty("accent", True)
        self.btn_pick_local = QPushButton("上传文件到本机…")
        self.btn_share = QPushButton("分享给手机…")
        self.btn_pick.clicked.connect(self.send_file_to_peer)
        self.btn_pick_local.clicked.connect(self.send_file_local)
        self.btn_share.clicked.connect(self.share_to_phone)
        for b in (self.btn_pick, self.btn_pick_local, self.btn_share):
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)

        self.up_bar = QProgressBar()
        self.up_bar.setTextVisible(False)
        self.up_label = QLabel("就绪")
        self.up_label.setObjectName("hint")
        v.addWidget(self.up_bar)
        v.addWidget(self.up_label)

        card2, v2 = make_card("后台发送任务", "关闭窗口后仍会继续；失败的任务可从断点重发。")
        self.job_list = QListWidget()
        self.job_list.setMinimumHeight(200)
        v2.addWidget(self.job_list)
        row2 = QHBoxLayout()
        self.btn_job_refresh = QPushButton("刷新")
        self.btn_job_retry = QPushButton("重试选中（断点续传）")
        self.btn_job_refresh.clicked.connect(self.refresh_jobs)
        self.btn_job_retry.clicked.connect(self.retry_job)
        row2.addWidget(self.btn_job_refresh)
        row2.addWidget(self.btn_job_retry)
        row2.addStretch(1)
        v2.addLayout(row2)

        holder = QWidget()
        hl = QVBoxLayout(holder)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(14)
        hl.addWidget(card)
        hl.addWidget(card2)
        return holder

    def _start_send(self, path, target):
        self.up_bar.setValue(0)
        self.up_label.setText("缓冲中：%s" % os.path.basename(path))
        self._thread = QThread(self)
        self._worker = SendWorker(self.api, path, target)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    def _on_progress(self, done, total):
        pct = int(done * 100 / max(1, total))
        self.up_bar.setValue(pct)
        self.up_label.setText("%d%%　（%s / %s）" % (pct, fmt_size(done), fmt_size(total)))

    def _on_finished(self, ok, msg):
        self.up_label.setText(("完成：" if ok else "失败：") + msg)
        if ok:
            self.up_bar.setValue(100)
        self.refresh_jobs()
        self.refresh_files()

    def send_file_to_peer(self):
        p = self._current_peer()
        if not p:
            self._goto("设备")
            QMessageBox.information(self, "提示", "请先在「设备」页选中一台电脑")
            return
        path, _ = QFileDialog.getOpenFileName(self, "选择要发送的文件")
        if path:
            self._start_send(path, p)

    def send_file_local(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择要上传到本机的文件")
        if path:
            self._start_send(path, None)

    def share_to_phone(self):
        """把本机文件加入分享列表，手机端「电脑发来的文件」可见。"""
        paths, _ = QFileDialog.getOpenFileNames(self, "选择要分享给手机的文件")
        if not paths:
            return
        try:
            body = {"paths": paths, "names": [os.path.basename(p) for p in paths], "folder": ""}
            r = self.api.post_json("/api/share", body, timeout=30)
            QMessageBox.information(self, "完成", ("已分享 %d 个文件" % len(paths))
                                    if r.get("ok") else ("失败：" + str(r.get("error"))))
        except Exception as e:
            QMessageBox.warning(self, "失败", str(e))

    def refresh_jobs(self):
        try:
            data = self.api.get_json("/api/peers/jobs", timeout=5)
            self.jobs = data.get("jobs", [])
        except Exception:
            self.jobs = []
        self.job_list.clear()
        state_txt = {"queued": "排队中", "sending": "发送中", "buffering": "缓冲中",
                     "done": "已完成", "failed": "已中断"}
        for j in self.jobs:
            pct = int((j.get("sent", 0) or 0) * 100 / max(1, j.get("size", 1)))
            txt = "%s　·　%s　·　%d%%（%s / %s）%s" % (
                state_txt.get(j.get("state"), j.get("state")), j.get("name"), pct,
                fmt_size(j.get("sent", 0)), fmt_size(j.get("size", 0)),
                ("　→　%s:%s" % (j.get("ip"), j.get("port"))) if j.get("ip") else "")
            if j.get("error"):
                txt += "　【%s】" % j["error"]
            item = QListWidgetItem(txt)
            item.setData(Qt.UserRole, j)
            self.job_list.addItem(item)

    def retry_job(self):
        it = self.job_list.currentItem()
        if not it:
            QMessageBox.information(self, "提示", "先选中一个任务")
            return
        j = it.data(Qt.UserRole)
        try:
            self.api.post_json("/api/peers/retry", {"id": j["id"]})
            self.status.showMessage("已重新排队，从断点继续", 3000)
        except Exception as e:
            QMessageBox.warning(self, "失败", str(e))
        QTimer.singleShot(800, self.refresh_jobs)

    # ---------- 页③：文字 ----------
    def _build_text(self):
        card, v = make_card("发送文字", "网址、验证码、临时笔记 —— 可发到其它电脑，或推送到手机网页。")
        self.text_in = QTextEdit()
        self.text_in.setPlaceholderText("输入要发送的文字…")
        self.text_in.setMinimumHeight(120)
        v.addWidget(self.text_in)
        row = QHBoxLayout()
        self.btn_text_send = QPushButton("发送到选中设备")
        self.btn_text_send.setProperty("accent", True)
        self.btn_text_push = QPushButton("推送到手机")
        self.btn_text_send.clicked.connect(self.send_text_to_peer)
        self.btn_text_push.clicked.connect(self.push_to_phone)
        row.addWidget(self.btn_text_send)
        row.addWidget(self.btn_text_push)
        row.addStretch(1)
        v.addLayout(row)

        card2, v2 = make_card("最近收发的文字")
        self.text_list = QListWidget()
        self.text_list.setMinimumHeight(200)
        v2.addWidget(self.text_list)
        row2 = QHBoxLayout()
        self.btn_text_refresh = QPushButton("刷新")
        self.btn_text_copy = QPushButton("复制选中")
        self.btn_text_refresh.clicked.connect(self.refresh_texts)
        self.btn_text_copy.clicked.connect(self.copy_text_item)
        row2.addWidget(self.btn_text_refresh)
        row2.addWidget(self.btn_text_copy)
        row2.addStretch(1)
        v2.addLayout(row2)

        holder = QWidget()
        hl = QVBoxLayout(holder)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(14)
        hl.addWidget(card)
        hl.addWidget(card2)
        return holder

    def send_text_to_peer(self):
        p = self._current_peer()
        txt = self.text_in.toPlainText().strip()
        if not txt:
            return
        if not p:
            self._goto("设备")
            QMessageBox.information(self, "提示", "请先在「设备」页选中一台电脑")
            return
        try:
            q = urllib.parse.urlencode({"ip": p["ip"], "port": p["port"]})
            r = self.api.post_json("/api/text/send?" + q, {"text": txt})
            self.status.showMessage("已发送" if r.get("ok") else ("失败：" + str(r.get("error"))), 4000)
            if r.get("ok"):
                self.text_in.clear()
        except Exception as e:
            QMessageBox.warning(self, "失败", str(e))

    def push_to_phone(self):
        txt = self.text_in.toPlainText().strip()
        if not txt:
            return
        try:
            r = self.api.post_json("/api/text/phone", {"text": txt})
            self.status.showMessage("已推送到手机" if r.get("ok") else str(r), 4000)
            if r.get("ok"):
                self.text_in.clear()
        except Exception as e:
            QMessageBox.warning(self, "失败", str(e))
        QTimer.singleShot(400, self.refresh_texts)

    def refresh_texts(self):
        try:
            msgs = self.api.get_json("/api/text/messages", timeout=5).get("messages", [])
        except Exception:
            return
        self.text_list.clear()
        for m in msgs[-50:]:
            t = time.strftime("%H:%M", time.localtime(m.get("ts", 0)))
            item = QListWidgetItem("[%s] %s：%s" % (t, m.get("from", "?"), m.get("text", "")))
            item.setData(Qt.UserRole, m.get("text", ""))
            self.text_list.addItem(item)

    def copy_text_item(self):
        it = self.text_list.currentItem()
        if not it:
            QMessageBox.information(self, "提示", "先选中一条文字")
            return
        QApplication.clipboard().setText(it.data(Qt.UserRole) or "")
        self.status.showMessage("已复制到剪贴板", 2500)

    # ---------- 页④：接收的文件 ----------
    def _build_files(self):
        card, v = make_card("接收目录",
                            "手机上传的文件、以及本机缓冲完成后落盘的文件都存放在这里。")
        self.dir_row_files = DirRow(str(core.RECEIVE_DIR), compact=True)
        self.dir_row_files.changed.connect(self.apply_receive_dir)
        v.addWidget(self.dir_row_files)
        hint = QLabel("更改后新收到的文件会存到新位置，已有文件不会自动迁移。")
        hint.setObjectName("hint")
        v.addWidget(hint)

        card2, v2 = make_card("已接收的文件")
        self.file_list = QListWidget()
        self.file_list.setMinimumHeight(240)
        self.file_list.itemDoubleClicked.connect(lambda _i: self.reveal_file())
        v2.addWidget(self.file_list)
        row = QHBoxLayout()
        self.btn_files_refresh = QPushButton("刷新")
        self.btn_open_dir = QPushButton("打开接收目录")
        self.btn_reveal_file = QPushButton("在 Finder 中显示")
        self.btn_files_refresh.clicked.connect(self.refresh_files)
        self.btn_open_dir.clicked.connect(lambda: DirRow(str(self._recv_dir())).reveal())
        self.btn_reveal_file.clicked.connect(self.reveal_file)
        row.addWidget(self.btn_files_refresh)
        row.addWidget(self.btn_open_dir)
        row.addWidget(self.btn_reveal_file)
        row.addStretch(1)
        v2.addLayout(row)

        holder = QWidget()
        hl = QVBoxLayout(holder)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(14)
        hl.addWidget(card)
        hl.addWidget(card2)
        return holder

    def _recv_dir(self):
        """当前生效的接收目录（优先用服务端返回值）。"""
        return self._settings.get("receive_dir") or str(core.RECEIVE_DIR)

    def refresh_files(self):
        try:
            files = self.api.get_json("/files", timeout=8)
        except Exception:
            return
        self.file_list.clear()
        for f in files:
            item = QListWidgetItem("%s　·　%s" % (f.get("name"), fmt_size(f.get("size", 0))))
            item.setData(Qt.UserRole, f.get("name"))
            self.file_list.addItem(item)

    def reveal_file(self):
        it = self.file_list.currentItem()
        name = it.data(Qt.UserRole) if it else None
        if not name:
            QMessageBox.information(self, "提示", "先选中一个文件")
            return
        path = Path(self._recv_dir()) / name
        if not path.exists():
            QMessageBox.information(self, "打不开", "文件不存在：%s" % path)
            return
        try:
            reveal_path(str(path))
        except Exception as e:
            QMessageBox.warning(self, "打不开", str(e))

    # ---------- 页⑤：设置 ----------
    def _build_settings(self):
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 8, 0)
        v.setSpacing(14)

        # 存储
        c1, l1 = make_card("存储", "选择接收文件的位置。")
        self.dir_row = DirRow(str(core.RECEIVE_DIR))
        self.dir_row.changed.connect(self._mark_dirty)
        l1.addWidget(self.dir_row)
        row_dir = QHBoxLayout()
        self.btn_dir_save = QPushButton("保存目录")
        self.btn_dir_save.setProperty("accent", True)
        self.btn_dir_save.clicked.connect(self.save_receive_dir)
        row_dir.addWidget(self.btn_dir_save)
        row_dir.addStretch(1)
        l1.addLayout(row_dir)
        v.addWidget(c1)

        # 通用
        c2, l2 = make_card("通用")
        self.cb_autostart = QCheckBox("登录时自动启动")
        self.cb_notify = QCheckBox("收到文件时显示通知")
        self.cb_open_folder = QCheckBox("收到文件后自动打开接收目录")
        self.cb_update_check = QCheckBox("启动时自动检查更新")
        for cb in (self.cb_autostart, self.cb_notify, self.cb_open_folder, self.cb_update_check):
            l2.addWidget(cb)
        for cb in (self.cb_autostart, self.cb_notify, self.cb_open_folder, self.cb_update_check):
            cb.stateChanged.connect(self._mark_dirty)
        v.addWidget(c2)

        # 外观
        c3, l3 = make_card("外观", "界面外观只影响这个原生窗口。")
        row_skin = QHBoxLayout()
        row_skin.addWidget(QLabel("主题"))
        self.cb_skin = QComboBox()
        self.cb_skin.addItems(["跟随系统", "浅色", "深色"])
        self.cb_skin.setMinimumWidth(160)
        self.cb_skin.currentIndexChanged.connect(self._on_skin_changed)
        row_skin.addWidget(self.cb_skin)
        row_skin.addStretch(1)
        l3.addLayout(row_skin)
        v.addWidget(c3)

        # 服务信息
        c4, l4 = make_card("服务", "与网页版共用同一个本机服务。")
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        form.setHorizontalSpacing(14)
        self.ed_port = QLineEdit(str(self.port))
        self.ed_port.setReadOnly(True)
        self.ed_ip = QLineEdit("-")
        self.ed_ip.setReadOnly(True)
        self.ed_ver = QLineEdit("v%s · 构建 %s" % (getattr(core, "APP_VERSION_NUM", "?"),
                                                   getattr(core, "APP_BUILD", "?")))
        self.ed_ver.setReadOnly(True)
        self.ed_rel = QLineEdit(getattr(core, "APP_RELEASE_DATE", "-"))
        self.ed_rel.setReadOnly(True)
        form.addRow("本机 IP", self.ed_ip)
        form.addRow("服务端口", self.ed_port)
        form.addRow("版本", self.ed_ver)
        form.addRow("发行日期", self.ed_rel)
        l4.addLayout(form)
        row_svc = QHBoxLayout()
        self.btn_console = QPushButton("打开浏览器控制台")
        self.btn_restart = QPushButton("重启服务")
        self.btn_check_update = QPushButton("检查更新")
        self.btn_console.clicked.connect(
            lambda: open_url("http://127.0.0.1:%d/console" % self.port))
        self.btn_restart.clicked.connect(self.restart_service)
        self.btn_check_update.clicked.connect(self.check_update)
        row_svc.addWidget(self.btn_console)
        row_svc.addWidget(self.btn_restart)
        row_svc.addWidget(self.btn_check_update)
        row_svc.addStretch(1)
        l4.addLayout(row_svc)
        v.addWidget(c4)

        # 关于
        c5, l5 = make_card("关于")
        about = QLabel("%s —— 局域网文件 / 文字互传\n%s 出品" % (APP_NAME, APP_COMPANY))
        about.setWordWrap(True)
        about.setObjectName("hint")
        l5.addWidget(about)
        v.addWidget(c5)

        # 保存
        bottom = QHBoxLayout()
        self.btn_save = QPushButton("保存设置")
        self.btn_save.setProperty("accent", True)
        self.btn_save.clicked.connect(self.save_settings)
        self.lb_dirty = QLabel("")
        self.lb_dirty.setObjectName("hint")
        bottom.addWidget(self.btn_save)
        bottom.addWidget(self.lb_dirty)
        bottom.addStretch(1)
        v.addLayout(bottom)

        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setFrameShape(QFrame.NoFrame)
        sc.setWidget(box)
        return sc

    def _mark_dirty(self, *_a):
        self.lb_dirty.setText("有未保存的修改")

    def load_settings(self):
        try:
            self._settings = self.api.get_json("/api/settings", timeout=6)
        except Exception:
            return
        s = self._settings
        cur = str(s.get("receive_dir") or core.RECEIVE_DIR)
        self.dir_row._path = cur
        self.dir_row.edit.setText(cur)
        self.dir_row_files._path = cur
        self.dir_row_files.edit.setText(cur)
        self._server_dir = cur
        self.ed_ip.setText(str(s.get("connect_url", "")).replace("http://", "") or "-")
        self.ed_port.setText(str(s.get("port", self.port)))
        self.cb_autostart.setChecked(bool(s.get("autostart")))
        self.cb_notify.setChecked(bool(s.get("notify_on_receive")))
        self.cb_open_folder.setChecked(bool(s.get("open_folder_on_receive")))
        self.cb_update_check.setChecked(bool(s.get("update_check_on_start")))
        self.lb_dirty.setText("")

    def apply_receive_dir(self, path, _source=None):
        """立即把新接收目录写入服务端配置。"""
        try:
            r = self.api.post_json("/api/settings", {"receive_dir": path}, timeout=15)
            if not r.get("ok"):
                QMessageBox.warning(self, "保存失败", str(r.get("error")))
                return False
            self.load_settings()
            self.refresh_files()
            self.status.showMessage("接收目录已更新：%s" % path, 6000)
            return True
        except Exception as e:
            QMessageBox.warning(self, "保存失败", str(e))
            return False

    def save_receive_dir(self):
        path = self.dir_row.path()
        if path and path != self._server_dir:
            self.apply_receive_dir(path)
        else:
            self.status.showMessage("接收目录未变化", 3000)

    def save_settings(self):
        body = {
            "autostart": self.cb_autostart.isChecked(),
            "notify_on_receive": self.cb_notify.isChecked(),
            "open_folder_on_receive": self.cb_open_folder.isChecked(),
            "update_check_on_start": self.cb_update_check.isChecked(),
        }
        direction = self.dir_row.path()
        if direction and direction != getattr(self, "_server_dir", ""):
            body["receive_dir"] = direction
        try:
            r = self.api.post_json("/api/settings", body, timeout=20)
        except Exception as e:
            QMessageBox.warning(self, "保存失败", str(e))
            return
        if not r.get("ok"):
            QMessageBox.warning(self, "保存失败", str(r.get("error")))
            return
        self.status.showMessage("设置已保存", 4000)
        QTimer.singleShot(600, self.load_settings)

    def restart_service(self):
        ret = QMessageBox.question(
            self, "重启服务", "重启本机服务（端口不变），网页端会短暂断开后自动恢复。是否继续？")
        if ret != QMessageBox.Yes:
            return
        try:
            self.api.post_json("/api/restart", {})
        except Exception:
            pass
        self.status.showMessage("正在重启服务…", 5000)

    def check_update(self):
        try:
            r = self.api.post_json("/api/local/update/check", {}, timeout=60)
            msg = r.get("message") or str(r)
            QMessageBox.information(self, "检查更新", str(msg))
        except Exception as e:
            QMessageBox.warning(self, "检查更新失败", str(e))

    def _on_skin_changed(self, idx):
        mode = ["auto", "light", "dark"][idx] if 0 <= idx < 3 else "auto"
        QSettings(APP_COMPANY, APP_NAME).setValue("skin", mode)
        apply_skin(self.app_ref, mode)
        self._sync_nav_icons()

    def toggle_skin(self):
        idx = self.cb_skin.currentIndex()
        self.cb_skin.setCurrentIndex(1 if idx != 1 else 2)

    # ---------- 连接码 / 关于 ----------
    def show_connect_code(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("连接码")
        dlg.setMinimumWidth(360)
        v = QVBoxLayout(dlg)
        title = QLabel("用手机扫描连接")
        title.setObjectName("sec")
        v.addWidget(title)
        img = QLabel()
        img.setAlignment(Qt.AlignCenter)
        try:
            data = self.api.get_bytes("/connect-qr.png", timeout=15)
            pm = QPixmap()
            pm.loadFromData(data)
            img.setPixmap(pm.scaledToWidth(240, Qt.SmoothTransformation))
            v.addWidget(img)
        except Exception as e:
            img.setText("二维码生成失败：%s" % e)
            v.addWidget(img)
        try:
            info = self.api.get_json("/api/connect-info", timeout=6)
            sub = QLabel("或在手机浏览器打开：%s\n本机：%s" % (
                info.get("code", ""), info.get("ip", "")))
        except Exception:
            sub = QLabel("服务未响应")
        sub.setObjectName("hint")
        sub.setWordWrap(True)
        v.addWidget(sub)
        row = QHBoxLayout()
        b_open = QPushButton("在浏览器打开")
        b_open.setProperty("accent", True)
        b_copy = QPushButton("复制地址")
        b_close = QPushButton("完成")
        row.addWidget(b_open)
        row.addWidget(b_copy)
        row.addStretch(1)
        row.addWidget(b_close)
        v.addLayout(row)

        def _open():
            try:
                info = self.api.get_json("/api/connect-info", timeout=6)
                open_url(info.get("code", ""))
            except Exception as e:
                QMessageBox.warning(dlg, "打不开", str(e))

        def _copy():
            try:
                info = self.api.get_json("/api/connect-info", timeout=6)
                QApplication.clipboard().setText(str(info.get("code", "")))
                self.status.showMessage("连接地址已复制", 2500)
            except Exception:
                pass

        b_open.clicked.connect(_open)
        b_copy.clicked.connect(_copy)
        b_close.clicked.connect(dlg.accept)
        dlg.exec()

    def show_about(self):
        QMessageBox.about(
            self, "关于 %s" % APP_NAME,
            "%s v%s（构建 %s）\n发行日期：%s\n\n%s 出品\n\n"
            "局域网文件 / 文字互传，支持分片断点续传与后台发送。"
            % (APP_NAME, getattr(core, "APP_VERSION_NUM", "?"),
               getattr(core, "APP_BUILD", "?"),
               getattr(core, "APP_RELEASE_DATE", "?"), APP_COMPANY))

    # ---------- 定时刷新 ----------
    def _tick(self):
        self.refresh_peers()
        if self.nav.currentRow() == 1 or self.jobs:
            self.refresh_jobs()
        if self.nav.currentRow() == 2:
            self.refresh_texts()


def _crash_log_path():
    """崩溃日志落盘位置：打包后放在 exe 同目录，源码运行放在仓库根目录。"""
    try:
        base = Path(sys.executable).parent if getattr(sys, "frozen", False) else ROOT
        return base / "ui-crash.log"
    except Exception:
        return Path.cwd() / "ui-crash.log"


def main():
    """外壳：任何异常都写 ui-crash.log（Windows GUI 版看不到终端输出）。"""
    try:
        return _main()
    except BaseException:  # noqa: BLE001
        import traceback
        msg = traceback.format_exc()
        try:
            p = _crash_log_path()
            p.write_text(msg, encoding="utf-8")
            print("崩溃日志已写入: %s" % p)
        except Exception:
            pass
        print(msg)
        try:
            _app = QApplication.instance() or QApplication(sys.argv)
            QMessageBox.critical(None, "%s 启动失败" % APP_NAME, msg[-1500:])
        except Exception:
            pass
        return 1


def _main():
    args = list(sys.argv[1:])
    smoke_out = None
    smoke_all = None
    if "--smoke" in args:
        i = args.index("--smoke")
        smoke_out = args[i + 1] if len(args) > i + 1 else "/tmp/yima_qt_smoke.png"
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    if "--smoke-all" in args:
        i = args.index("--smoke-all")
        smoke_all = args[i + 1] if len(args) > i + 1 else "/tmp/yima_qt_ui"
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setAttribute(Qt.AA_DontShowIconsInMenus, True)
    apply_skin(app, QSettings(APP_COMPANY, APP_NAME).value("skin", "auto", type=str))

    port = int(core.CONFIG.get("port", 8000))
    api = Api(port)

    # 已有实例在跑就接入，否则自己拉起服务（与网页版共用一套逻辑）
    attached = True
    try:
        api.get_json("/api/version", timeout=2)
    except Exception:
        attached = False
        try:
            if bootstrap_server() is None:
                QMessageBox.critical(None, "启动失败", "无法启动本机服务（端口被占用？）")
                return 1
        except Exception as e:
            QMessageBox.critical(None, "启动失败", str(e))
            return 1

    win = MainWindow(app, api, port, attached)
    win.show()

    if smoke_out:
        app.processEvents()
        win.grab().save(smoke_out)
        print("SMOKE OK ->", smoke_out)
        return 0

    if smoke_all:
        outdir = Path(smoke_all)
        outdir.mkdir(parents=True, exist_ok=True)
        for i, name in enumerate(MainWindow.NAV):
            win.nav.setCurrentRow(i)
            app.processEvents()
            win.resize(1060, 760)
            app.processEvents()
            p = outdir / ("%d-%s.png" % (i + 1, name))
            win.grab().save(str(p))
            print("SMOKE PAGE ->", p)
        return 0

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
易码互传 · Qt 原生客户端（v1.14.0+，Mac 版 MVP）

不依赖浏览器：所有操作走本机 HTTP 服务（与网页版同一套协议），
界面是原生窗口。服务端核心直接复用 app.py，因此协议、断点续传、
后台发送等能力与网页版完全一致。

用法：
    python3 native/yima_qt.py
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app as core  # noqa: E402  —— 复用现有服务端核心（配置、发现、上传协议、后台任务）

from PySide6.QtCore import Qt, QTimer, Signal, QObject, QThread  # noqa: E402
from PySide6.QtGui import QIcon, QAction  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QListWidget, QListWidgetItem, QFileDialog, QTextEdit,
    QTabWidget, QProgressBar, QMessageBox, QLineEdit, QFormLayout,
    QStatusBar, QSplitter, QAbstractItemView,
)

APP_NAME = "易码互传"
CHUNK = 2 * 1024 * 1024


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


class SendWorker(QObject):
    """后台把文件分片缓冲到本机（不阻塞界面）。"""
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
                    self.finished.emit(True, "电脑上已有同文件，无需重传")
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
                self.finished.emit(True, "已交给后台发送，可关闭窗口")
            else:
                self.finished.emit(True, "已保存到接收目录")
        except urllib.error.HTTPError as e:
            self.finished.emit(False, "HTTP %d" % e.code)
        except Exception as e:
            self.finished.emit(False, str(e))


class MainWindow(QMainWindow):
    def __init__(self, api, port, attached):
        super().__init__()
        self.api = api
        self.port = port
        self.attached = attached
        self.jobs = []
        self.setWindowTitle("%s · 原生客户端" % APP_NAME)
        self.resize(880, 600)
        icon = ROOT / "assets" / "icon.png"
        if icon.exists():
            self.setWindowIcon(QIcon(str(icon)))

        tabs = QTabWidget()
        tabs.addTab(self._build_devices(), "设备")
        tabs.addTab(self._build_send(), "发送 / 后台任务")
        tabs.addTab(self._build_text(), "文字互传")
        tabs.addTab(self._build_files(), "接收的文件")
        tabs.addTab(self._build_settings(), "设置")
        self.setCentralWidget(tabs)

        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status.showMessage("服务运行中 · 端口 %d%s" %
                                (port, "（已接入现有服务）" if attached else ""))

        self.setMenuBar(self._build_menu())
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1500)
        self.refresh_peers()

    # ---------- 菜单 ----------
    def _build_menu(self):
        bar = self.menuBar()
        m = bar.addMenu("操作")
        act_scan = QAction("重新扫描局域网", self)
        act_scan.triggered.connect(self.scan)
        m.addAction(act_scan)
        act_quit = QAction("退出", self)
        act_quit.triggered.connect(self.close)
        m.addAction(act_quit)
        return bar

    # ---------- 页①：设备 ----------
    def _build_devices(self):
        w = QWidget()
        v = QVBoxLayout(w)
        self.peer_list = QListWidget()
        self.peer_list.setSelectionMode(QAbstractItemView.SingleSelection)
        v.addWidget(QLabel("局域网内的其它电脑（手机用「连接码」在浏览器打开，不走本界面）："))
        v.addWidget(self.peer_list)
        row = QHBoxLayout()
        self.btn_refresh = QPushButton("刷新设备")
        self.btn_scan = QPushButton("重新扫描局域网")
        self.btn_send_file = QPushButton("发文件到选中设备")
        self.btn_send_text = QPushButton("发文字到选中设备")
        self.btn_refresh.clicked.connect(self.refresh_peers)
        self.btn_scan.clicked.connect(self.scan)
        self.btn_send_file.clicked.connect(self.send_file_to_peer)
        self.btn_send_text.clicked.connect(self.send_text_to_peer)
        for b in (self.btn_refresh, self.btn_scan, self.btn_send_file, self.btn_send_text):
            row.addWidget(b)
        v.addLayout(row)
        self.dev_hint = QLabel("")
        self.dev_hint.setStyleSheet("color:#86868b")
        v.addWidget(self.dev_hint)
        return w

    def refresh_peers(self):
        try:
            peers = self.api.get_json("/api/peers", timeout=5)
        except Exception as e:
            self.dev_hint.setText("获取设备失败：%s" % e)
            return
        self.peer_list.clear()
        for p in peers:
            item = QListWidgetItem("%s  （%s:%s）" % (p.get("name", "?"), p.get("ip"), p.get("port")))
            item.setData(Qt.UserRole, p)
            self.peer_list.addItem(item)
        self.dev_hint.setText("共 %d 台设备 · 每 1.5 秒自动刷新" % len(peers))

    def scan(self):
        try:
            self.api.get_json("/api/scan", timeout=60)
        except Exception:
            pass
        self.refresh_peers()

    def _current_peer(self):
        it = self.peer_list.currentItem()
        return it.data(Qt.UserRole) if it else None

    # ---------- 页②：发送 / 后台任务 ----------
    def _build_send(self):
        w = QWidget()
        v = QVBoxLayout(w)
        row = QHBoxLayout()
        self.btn_pick = QPushButton("选择文件发送到选中设备…")
        self.btn_pick_local = QPushButton("选择文件上传到本机（手机可见）…")
        self.btn_share = QPushButton("分享给手机…")
        self.btn_pick.clicked.connect(self.send_file_to_peer)
        self.btn_pick_local.clicked.connect(self.send_file_local)
        self.btn_share.clicked.connect(self.share_to_phone)
        for b in (self.btn_pick, self.btn_pick_local, self.btn_share):
            row.addWidget(b)
        v.addLayout(row)

        self.up_bar = QProgressBar()
        self.up_label = QLabel("就绪")
        v.addWidget(self.up_bar)
        v.addWidget(self.up_label)

        v.addWidget(QLabel("后台发送任务（关掉窗口也会继续）："))
        self.job_list = QListWidget()
        v.addWidget(self.job_list)
        row2 = QHBoxLayout()
        self.btn_job_refresh = QPushButton("刷新任务")
        self.btn_job_retry = QPushButton("重试选中（断点续传）")
        self.btn_job_refresh.clicked.connect(self.refresh_jobs)
        self.btn_job_retry.clicked.connect(self.retry_job)
        row2.addWidget(self.btn_job_refresh)
        row2.addWidget(self.btn_job_retry)
        v.addLayout(row2)
        return w

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
        self.up_label.setText("%d%%  （%s / %s）" % (pct, fmt_size(done), fmt_size(total)))

    def _on_finished(self, ok, msg):
        self.up_label.setText(("完成：" if ok else "失败：") + msg)
        if ok:
            self.up_bar.setValue(100)
        self.refresh_jobs()

    def send_file_to_peer(self):
        p = self._current_peer()
        if not p:
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
            txt = "%s · %s · %d%%（%s/%s）%s" % (
                state_txt.get(j.get("state"), j.get("state")), j.get("name"), pct,
                fmt_size(j.get("sent", 0)), fmt_size(j.get("size", 0)),
                (" → " + str(j.get("ip")) + ":" + str(j.get("port"))) if j.get("ip") else "")
            if j.get("error"):
                txt += "  【%s】" % j["error"]
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
        w = QWidget()
        v = QVBoxLayout(w)
        self.text_in = QTextEdit()
        self.text_in.setPlaceholderText("输入要发送的文字：网址、验证码、笔记…")
        v.addWidget(self.text_in)
        row = QHBoxLayout()
        self.btn_text_send = QPushButton("发送到选中设备")
        self.btn_text_push = QPushButton("推送到手机")
        self.btn_text_send.clicked.connect(self.send_text_to_peer)
        self.btn_text_push.clicked.connect(self.push_to_phone)
        row.addWidget(self.btn_text_send)
        row.addWidget(self.btn_text_push)
        v.addLayout(row)

        v.addWidget(QLabel("最近收发的文字（点「复制」取走）："))
        self.text_list = QListWidget()
        v.addWidget(self.text_list)
        self.btn_text_refresh = QPushButton("刷新")
        self.btn_text_refresh.clicked.connect(self.refresh_texts)
        v.addWidget(self.btn_text_refresh)
        return w

    def send_text_to_peer(self):
        p = self._current_peer()
        txt = self.text_in.toPlainText().strip()
        if not txt:
            return
        if not p:
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

    def refresh_texts(self):
        try:
            msgs = self.api.get_json("/api/text/messages", timeout=5).get("messages", [])
        except Exception:
            return
        self.text_list.clear()
        for m in msgs[-50:]:
            t = time.strftime("%H:%M", time.localtime(m.get("ts", 0)))
            self.text_list.addItem("[%s] %s：%s" % (t, m.get("from", "?"), m.get("text", "")))

    # ---------- 页④：接收的文件 ----------
    def _build_files(self):
        w = QWidget()
        v = QVBoxLayout(w)
        self.file_list = QListWidget()
        v.addWidget(self.file_list)
        row = QHBoxLayout()
        self.btn_files_refresh = QPushButton("刷新")
        self.btn_open_dir = QPushButton("打开接收目录")
        self.btn_files_refresh.clicked.connect(self.refresh_files)
        self.btn_open_dir.clicked.connect(self.open_dir)
        row.addWidget(self.btn_files_refresh)
        row.addWidget(self.btn_open_dir)
        v.addLayout(row)
        self.dir_label = QLabel("接收目录：%s" % core.RECEIVE_DIR)
        self.dir_label.setStyleSheet("color:#86868b")
        v.addWidget(self.dir_label)
        return w

    def refresh_files(self):
        try:
            files = self.api.get_json("/files", timeout=8)
        except Exception:
            return
        self.file_list.clear()
        for f in files:
            self.file_list.addItem("%s  ·  %s" % (f.get("name"), fmt_size(f.get("size", 0))))

    def open_dir(self):
        try:
            subprocess.Popen(["open", str(core.RECEIVE_DIR)])
        except Exception as e:
            QMessageBox.warning(self, "打不开", str(e))

    # ---------- 页⑤：设置 ----------
    def _build_settings(self):
        w = QWidget()
        v = QVBoxLayout(w)
        form = QFormLayout()
        self.ed_port = QLineEdit(str(self.port))
        self.ed_port.setReadOnly(True)
        self.ed_dir = QLineEdit(str(core.RECEIVE_DIR))
        self.ed_dir.setReadOnly(True)
        self.ed_ver = QLineEdit("%s · 构建 %s" % (core.APP_VERSION_NUM, core.APP_BUILD))
        self.ed_ver.setReadOnly(True)
        form.addRow("服务端口", self.ed_port)
        form.addRow("接收目录", self.ed_dir)
        form.addRow("版本", self.ed_ver)
        v.addLayout(form)
        self.btn_console = QPushButton("在浏览器打开控制台（排错用）")
        self.btn_console.clicked.connect(
            lambda: subprocess.Popen(["open", "http://127.0.0.1:%d/console" % self.port]))
        v.addWidget(self.btn_console)
        v.addStretch(1)
        return w

    # ---------- 定时刷新 ----------
    def _tick(self):
        self.refresh_peers()
        self.refresh_jobs()


def main():
    args = [a for a in sys.argv[1:]]
    smoke_out = None
    if "--smoke" in args:
        i = args.index("--smoke")
        smoke_out = args[i + 1] if len(args) > i + 1 else "/tmp/yima_qt_smoke.png"
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)

    port = int(core.CONFIG.get("port", 8000))
    api = Api(port)

    # 已有实例在跑就接入，否则自己拉起服务（与网页版共用一套逻辑）
    attached = True
    try:
        api.get_json("/api/version", timeout=2)
    except Exception:
        attached = False
        try:
            server, _qr = core._bootstrap_server(False)
            if server is None:
                QMessageBox.critical(None, "启动失败", "无法启动本机服务（端口被占用？）")
                return 1
        except Exception as e:
            QMessageBox.critical(None, "启动失败", str(e))
            return 1

    win = MainWindow(api, port, attached)
    win.show()

    if smoke_out:
        # 离屏自检：渲染一次界面并存图，用于无显示器环境下验证 UI 构建
        win.resize(880, 620)
        app.processEvents()
        win.grab().save(smoke_out)
        print("SMOKE OK ->", smoke_out)
        return 0

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

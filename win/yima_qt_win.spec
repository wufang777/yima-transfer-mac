# -*- mode: python ; coding: utf-8 -*-
# 易码互传 · 原生界面（Qt）Windows 打包配置（onefile，无控制台窗口）
# 用法（仓库根目录，Windows 上）：pyinstaller win/yima_qt_win.spec
# 产物：dist/yima-transfer-ui.exe
import os

SPEC_DIR = SPECPATH            # win/
ROOT = os.path.abspath(os.path.join(SPEC_DIR, ".."))
ASSETS = os.path.join(ROOT, "assets")
NATIVE = os.path.join(ROOT, "native")

QT_EXCLUDES = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebChannel",
    "PySide6.QtWebSockets", "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic", "PySide6.Qt3DAnimation", "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQuickWidgets",
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtDesigner", "PySide6.QtHelp",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtSerialPort", "PySide6.QtPositioning",
    "PySide6.QtRemoteObjects", "PySide6.QtSensors", "PySide6.QtStateMachine",
    "PySide6.QtUiTools", "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtTextToSpeech",
    "PySide6.QtNetworkAuth", "PySide6.QtAxContainer", "PySide6.QtHttpServer",
    "PySide6.QtScxml", "PySide6.QtGraphs", "PySide6.QtGraphsWidgets", "PySide6.QtSpatialAudio",
    # Windows 原生客户端不用的东西（Mac 专属依赖）
    "rumps", "AppKit", "Foundation", "CoreFoundation", "objc",
]

a = Analysis(
    [os.path.join(NATIVE, "yima_qt.py")],
    pathex=[SPEC_DIR, NATIVE, ROOT],     # 让 yima_win / app / native 三者都能被 import
    binaries=[],
    datas=[
        (os.path.join(ROOT, "console.html"), "."),
        (os.path.join(ROOT, "index.html"), "."),
        (os.path.join(ROOT, "settings.html"), "."),
        (os.path.join(ASSETS, "icon.png"), "assets"),
        (os.path.join(ASSETS, "logo.png"), "assets"),
    ],
    hiddenimports=["PySide6.QtSvg", "pystray", "winreg"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=QT_EXCLUDES,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="yima-transfer-ui",     # ASCII 文件名：与 CI / URL 编码口径一致
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,                 # 临时开启：便于 CI 抓启动报错（定位后会改回 False）
    icon=os.path.join(SPEC_DIR, "icon.ico"),
)

# -*- mode: python ; coding: utf-8 -*-
# 易码互传 · 原生界面（Qt）macOS 打包配置
# 用法（仓库根目录）：pyinstaller native/yima_qt.spec --noconfirm
# 产物：dist/易码互传.app
import os

SPEC_DIR = SPECPATH            # native/
ROOT = os.path.abspath(os.path.join(SPEC_DIR, ".."))
ASSETS = os.path.join(ROOT, "assets")

# 不用的 Qt 模块（显著缩小体积，且这里都用不到）
QT_EXCLUDES = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebChannel",
    "PySide6.QtWebSockets", "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic", "PySide6.Qt3DAnimation", "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQuickWidgets",
    "PySide6.QtQml", "PySide6.QtSpatialAudio", "PySide6.QtSql", "PySide6.QtTest",
    "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtOpenGLWidgets", "PySide6.QtConcurrent",
    "PySide6.QtPrintSupport", "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtSerialPort",
    "PySide6.QtPositioning", "PySide6.QtRemoteObjects", "PySide6.QtSensors", "PySide6.QtStateMachine",
    "PySide6.QtUiTools", "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtTextToSpeech",
    "PySide6.QtNetworkAuth", "PySide6.QtAxContainer", "PySide6.QtHttpServer",
    "PySide6.QtScxml", "PySide6.QtGraphs", "PySide6.QtGraphsWidgets",
]

a = Analysis(
    ["yima_qt.py"],
    pathex=[SPEC_DIR, os.path.join(ROOT, "win")],   # win/ ：Windows 版服务端核心目录
    binaries=[],
    datas=[
        (os.path.join(ROOT, "console.html"), "."),
        (os.path.join(ROOT, "index.html"), "."),
        (os.path.join(ROOT, "settings.html"), "."),
        (os.path.join(ASSETS, "icon.png"), "assets"),
        (os.path.join(ASSETS, "logo.png"), "assets"),
    ],
    hiddenimports=["PySide6.QtSvg"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=QT_EXCLUDES,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="易码互传",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                       # 无终端黑框，走 Dock 图标
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[os.path.join(ASSETS, "icon.icns")],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="易码互传",
)
app = BUNDLE(
    coll,
    name="易码互传.app",
    icon=os.path.join(ASSETS, "icon.icns"),
    bundle_identifier="com.yima.transfer.ui",
    version="1.15.0",
    info_plist={
        "CFBundleName": "易码互传",
        "CFBundleDisplayName": "易码互传",
        "CFBundleShortVersionString": "1.15.0",
        "CFBundleVersion": "20261030",
        "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": False,   # 允许跟随系统深色模式
        "LSUIElement": False,                      # 在 Dock 显示图标
        "NSPrincipalClass": "NSApplication",
    },
)

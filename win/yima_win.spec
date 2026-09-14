# -*- mode: python ; coding: utf-8 -*-
# 易码互传 · Windows 版 PyInstaller 打包配置（onefile，无控制台窗口）
# 用法（在仓库根目录）：pyinstaller win/yima_win.spec
import os

SPEC_DIR = SPECPATH            # win/
ROOT = os.path.abspath(os.path.join(SPEC_DIR, ".."))

a = Analysis(
    ["yima_win.py"],
    pathex=[SPEC_DIR],
    binaries=[],
    datas=[
        (os.path.join(ROOT, "console.html"), "."),
        (os.path.join(ROOT, "index.html"), "."),
        (os.path.join(ROOT, "settings.html"), "."),
        (os.path.join(ROOT, "assets", "icon.png"), "assets"),
        (os.path.join(ROOT, "assets", "logo.png"), "assets"),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="yima-transfer",        # ASCII 文件名：避免 CI/URL 编码坑（与 dmg 命名口径一致）
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,               # 无黑色控制台窗口
    icon=os.path.join(SPEC_DIR, "icon.ico"),
)

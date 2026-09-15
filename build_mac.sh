#!/bin/bash
# ============================================================
# 易码互传 · macOS 原生版 一键打包
#
# 用法：bash build_mac.sh
# 产物：dist/易码互传.app + dist/易码互传.dmg
#
# 依赖（脚本自动安装）：rumps（含 pyobjc）、segno
# 注意：不使用 --clean（会触发系统安全拦截清理缓存）
#      每次打包后需重新覆盖 Info.plist 并重签名（脚本已处理）
# ============================================================
set -e
cd "$(dirname "$0")"

# 规避部分环境下 pip 解压 sdist 的 EEXIST 问题
export TMPDIR="${TMPDIR:-/tmp/yima-build}"
mkdir -p "$TMPDIR" 2>/dev/null || true

echo "==> [1/6] 检查 python3"
PY=python3
if ! $PY -c "import sys; assert sys.version_info >= (3,9)" 2>/dev/null; then
  echo "  [错误] 需要 python3 >= 3.9"
  exit 1
fi
echo "  python3 OK: $($PY -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"

echo "==> [2/6] 安装/检查依赖（rumps segno Pillow pyobjc-framework-Cocoa）"
$PY -m pip install -q --user rumps segno Pillow pyobjc-framework-Cocoa 2>/dev/null || \
  $PY -m pip install -q rumps segno Pillow pyobjc-framework-Cocoa || \
  echo "  (pip 安装失败，若依赖已装好可忽略)"
$PY -c "import rumps, segno, AppKit; print('  依赖 OK')" 2>/dev/null || \
  echo "  [警告] 依赖不完整（缺 rumps/segno/pyobjc），打包仍会继续但需检查"

echo "==> [3/6] 生成 icns 图标（多尺寸）"
mkdir -p assets/icon.iconset
$PY - <<'PYEOF'
from PIL import Image
src = "assets/icon.png"
im = Image.open(src).convert("RGBA")
w, h = im.size
side = max(w, h)
canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
canvas.paste(im, ((side - w) // 2, (side - h) // 2))
for s in [16, 32, 64, 128, 256, 512, 1024]:
    canvas.resize((s, s), Image.LANCZOS).save(f"assets/icon.iconset/icon_{s}x{s}.png")
    if s <= 512:
        canvas.resize((s * 2, s * 2), Image.LANCZOS).save(f"assets/icon.iconset/icon_{s}x{s}@2x.png")
print("iconset OK")
PYEOF
iconutil -c icns assets/icon.iconset -o assets/icon.icns 2>/dev/null || \
  echo "  (iconutil 失败，将使用 png 图标兜底)"

echo "==> [4/6] PyInstaller 打包"
# 关键：每次都用全新的构建目录，避免 PyInstaller 复用上次的缓存模块
# （复用会导致「改了代码但二进制还是旧的」，出现版本号与代码不符的假包）
STAMP=$(date +%Y%m%d-%H%M%S)
BUILD_TMP="build/pyi-$STAMP"
DIST_TMP="dist/_build-$STAMP"
rm -rf "$BUILD_TMP" "$DIST_TMP" 2>/dev/null || true
ICON_OPT=""
if [ -f assets/icon.icns ]; then ICON_OPT="--icon assets/icon.icns"; fi
$PY -m PyInstaller --onedir --windowed --name 易码互传 $ICON_OPT -y \
  --workpath "$BUILD_TMP" --distpath "$DIST_TMP" \
  --add-data "console.html:." \
  --add-data "index.html:." \
  --add-data "settings.html:." \
  --add-data "assets/icon.png:assets" \
  --add-data "assets/logo.png:assets" \
  --hidden-import rumps --hidden-import segno \
  --hidden-import AppKit --hidden-import Foundation \
  app.py 2>&1 | tail -3

if [ ! -d "$DIST_TMP/易码互传.app" ]; then
  echo "  [错误] PyInstaller 未产出 .app，打包中止"
  exit 1
fi
# 用新包替换旧包（旧包先移走，避免残留已签名文件挡住后续重签）
if [ -d "dist/易码互传.app" ]; then
  OLD_TS=$(date +%H%M%S)
  mkdir -p build/old 2>/dev/null || true
  mv "dist/易码互传.app" "build/old/易码互传.app-$OLD_TS" 2>/dev/null || rm -rf "dist/易码互传.app" 2>/dev/null || true
fi
mv "$DIST_TMP/易码互传.app" "dist/易码互传.app"

echo "==> [5/6] 覆盖 Info.plist / 图标并重签名"
APP="dist/易码互传.app"
cp assets/icon.icns "$APP/Contents/Resources/AppIcon.icns" 2>/dev/null || true

# 版本号从 app.py 读取（单一来源，避免 plist 与程序版本不一致）
VER_NUM=$($PY -c "import re;print(re.search(r'APP_VERSION_NUM = \"([^\"]+)\"',open('app.py',encoding='utf-8').read()).group(1))")
VER_BUILD=$($PY -c "import re;print(re.search(r'APP_BUILD = \"([^\"]+)\"',open('app.py',encoding='utf-8').read()).group(1))")
echo "  版本：v$VER_NUM  构建：$VER_BUILD"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>              <string>易码互传</string>
    <key>CFBundleDisplayName</key>       <string>易码互传</string>
    <key>CFBundleExecutable</key>        <string>易码互传</string>
    <key>CFBundleIdentifier</key>        <string>com.yima.transfer</string>
    <key>CFBundleVersion</key>           <string>$VER_BUILD</string>
    <key>CFBundleShortVersionString</key><string>$VER_NUM</string>
    <key>CFBundlePackageType</key>       <string>APPL</string>
    <key>LSMinimumSystemVersion</key>    <string>10.13</string>
    <key>NSHighResolutionCapable</key>   <true/>
    <key>CFBundleIconFile</key>          <string>AppIcon</string>
    <key>NSLocalNetworkUsageDescription</key>
    <string>易码互传需要在局域网内与手机和其它电脑传输文件。</string>
</dict>
</plist>
PLIST
codesign --force --sign - "$APP"
echo "  Info.plist 已覆盖（常规应用：Dock 与强制退出列表均可见）"
plutil -lint "$APP/Contents/Info.plist"

# ---- 关键校验：二进制内的版本号必须与 app.py 一致 ----
# 防止 PyInstaller 复用缓存导致「plist 新版本 + 代码还是旧的」的假包
BIN_VER=$("$APP/Contents/MacOS/易码互传" --version 2>/dev/null | sed -E 's/.*v([0-9.]+).*/\1/')
if [ "$BIN_VER" != "$VER_NUM" ]; then
  echo "  [错误] 二进制版本($BIN_VER) 与源码版本($VER_NUM) 不一致 —— 打包缓存可能未生效"
  echo "         已中止发布，请删除 build/ 目录后重试。"
  exit 1
fi
echo "  校验通过：二进制版本 v$BIN_VER 与源码一致"

echo "==> [6/6] 生成 dmg（含 Applications 拖拽入口）"
# 先删掉上一版 dmg：一旦本步失败，绝不能让旧 dmg 冒充新版被发出去
rm -f "dist/易码互传.dmg"

# 卸载所有残留同名卷（含「易码互传 1」这类重名卷）：
# 残留卷存在时 hdiutil 可能写不进去或产出旧包，是假包事故的头号原因
for v in /Volumes/易码互传*; do
  [ -d "$v" ] || continue
  hdiutil detach "$v" -force >/dev/null 2>&1 && echo "  已卸载残留卷：$v"
done

# 暂存目录也用唯一名，避免上次残留的目录/挂载点被复用
STAGE="/tmp/yima-dmg-stage-$STAMP"
rm -rf "$STAGE" && mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"

DMG_TMP="/tmp/yima-dmg-$STAMP.dmg"
rm -f "$DMG_TMP"
# 保留 hdiutil 原始输出：失败时能直接看到真实原因
# （最常见的失败是沙箱/权限拦截对 /Volumes 的写入——hdiutil create 需要内部挂载临时镜像）
CREATE_LOG="/tmp/yima-dmg-create-$STAMP.log"
if ! hdiutil create -volname "易码互传" -srcfolder "$STAGE" -format UDZO "$DMG_TMP" > "$CREATE_LOG" 2>&1; then
  echo "  [错误] dmg 生成失败，发布中止。hdiutil 输出："
  sed 's/^/    /' "$CREATE_LOG" | tail -12
  echo "    提示：若出现 sandbox / Operation not permitted，说明当前环境"
  echo "          拦截了对 /Volumes 的写入（hdiutil create 需内部挂载临时镜像）。"
  echo "          请在无沙箱/已授权终端中重跑本脚本。"
  rm -rf "$STAGE"
  exit 1
fi

# ---- 成品自检：挂载到「独占挂载点」，双重确认版本号 + 二进制字节一致 ----
# 用 -mountpoint 指定独占路径，杜绝挂到已存在的同名卷上（那会读到旧包内容）
MNT="/tmp/yima-dmg-mnt-$STAMP"
rm -rf "$MNT" && mkdir -p "$MNT"
INNER_VER=""; INNER_SHA=""
if hdiutil attach "$DMG_TMP" -nobrowse -readonly -mountpoint "$MNT" >/dev/null 2>&1; then
  INNER_VER=$("$MNT/易码互传.app/Contents/MacOS/易码互传" --version 2>/dev/null | sed -E 's/.*v([0-9.]+).*/\1/')
  INNER_SHA=$(shasum -a 256 "$MNT/易码互传.app/Contents/MacOS/易码互传" 2>/dev/null | awk '{print $1}')
  hdiutil detach "$MNT" -force >/dev/null 2>&1
fi
rm -rf "$MNT"
SRC_SHA=$(shasum -a 256 "$APP/Contents/MacOS/易码互传" | awk '{print $1}')

if [ "$INNER_VER" != "$VER_NUM" ]; then
  echo "  [错误] dmg 内二进制版本(${INNER_VER:-空}) 与目标版本($VER_NUM) 不一致，发布中止"
  rm -f "$DMG_TMP"; rm -rf "$STAGE"
  exit 1
fi
if [ "$INNER_SHA" != "$SRC_SHA" ]; then
  echo "  [错误] dmg 内二进制与本次构建产物不一致（sha256 不符），发布中止"
  rm -f "$DMG_TMP"; rm -rf "$STAGE"
  exit 1
fi
mv -f "$DMG_TMP" "dist/易码互传.dmg"
echo "  dmg OK 且自检通过（内含 v${INNER_VER}，sha256 与构建产物一致）"
rm -rf "$STAGE"

echo ""
echo "============================================"
echo "打包完成："
echo "  $APP   ← 双击运行（菜单栏常驻）"
echo "  dist/易码互传.dmg   ← 分发镜像"
echo ""
echo "首次打开（未签名）：右键 .app → 打开 → 再点「打开」"
echo "首次运行请在防火墙弹窗里点「允许」，局域网互传才能生效。"
echo "============================================"

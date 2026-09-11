#!/bin/bash
# ============================================================
# 易码互传 · 发布新版本（一条命令完成）
#
# 用法：
#   bash release.sh 1.1.0 "新增设备分组;修复二维码刷新" [--channel beta]
#   bash release.sh --build-only            # 不改版本，只重新构建
#
# 流程：改版本号 → 打包 .app/.dmg → 生成更新清单 version.json（含 sha256）
#       → 输出 dist/release/ 目录，把里面的文件传到服务器即完成发布
#
# 版本号规则（语义化版本）：
#   修 bug       → 第三位 +1，如 1.0.0 → 1.0.1
#   加功能       → 第二位 +1，如 1.0.1 → 1.1.0
#   大改版/不兼容 → 第一位 +1，如 1.1.0 → 2.0.0
# 构建号自动取当天日期（YYYYMMDD），同日多次构建也能区分。
# ============================================================
set -e
cd "$(dirname "$0")"

PY=python3
APP_NAME="易码互传"
NEW_VER=""
NOTES=""
CHANNEL="stable"
BUILD_ONLY=0

# ---- 解析参数 ----
while [ $# -gt 0 ]; do
  case "$1" in
    --build-only) BUILD_ONLY=1; shift ;;
    --channel)    CHANNEL="$2"; shift 2 ;;
    --notes)      NOTES="$2"; shift 2 ;;
    -h|--help)    sed -n '3,18p' "$0"; exit 0 ;;
    *)            if [ -z "$NEW_VER" ]; then NEW_VER="$1"; else NOTES="$1"; fi; shift ;;
  esac
done

if [ "$BUILD_ONLY" = "0" ] && [ -z "$NEW_VER" ]; then
  echo "用法：bash release.sh <新版本号> \"更新说明（用分号分隔）\""
  echo "例如：bash release.sh 1.1.0 \"新增设备分组;修复二维码刷新\""
  exit 1
fi

CUR_VER=$($PY -c "import re;print(re.search(r'APP_VERSION_NUM = \"([^\"]+)\"',open('app.py',encoding='utf-8').read()).group(1))")
TODAY=$(date +%Y%m%d)
BUILD="$TODAY"

if [ "$BUILD_ONLY" = "0" ]; then
  # 版本号合法性：必须比当前版本大
  $PY - "$CUR_VER" "$NEW_VER" <<'PYEOF' || exit 1
import sys

def vt(s):
    s = s.strip().lstrip("vV").split("-")[0]
    p = [int(x) if x.isdigit() else 0 for x in s.split(".")]
    while len(p) < 3:
        p.append(0)
    return tuple(p[:3])

cur, new = sys.argv[1], sys.argv[2]
if vt(new) <= vt(cur):
    print("  [错误] 新版本号 %s 必须大于当前版本 %s" % (new, cur))
    sys.exit(1)
print("  版本变更：v%s → v%s" % (cur, new))
PYEOF

  # 写入 app.py（单一来源）
  $PY - "$NEW_VER" "$BUILD" <<'PYEOF'
import re, sys
ver, build = sys.argv[1], sys.argv[2]
p = "app.py"
s = open(p, encoding="utf-8").read()
s = re.sub(r'APP_VERSION_NUM = "[^"]+"', 'APP_VERSION_NUM = "%s"' % ver, s, count=1)
s = re.sub(r'APP_BUILD = "[^"]+"', 'APP_BUILD = "%s"' % build, s, count=1)
open(p, "w", encoding="utf-8").write(s)
print("  app.py 版本号已更新：v%s / 构建 %s" % (ver, build))
PYEOF
else
  echo "  仅构建模式：沿用当前版本 v$CUR_VER"
fi

FINAL_VER=$($PY -c "import re;print(re.search(r'APP_VERSION_NUM = \"([^\"]+)\"',open('app.py',encoding='utf-8').read()).group(1))")
FINAL_BUILD=$($PY -c "import re;print(re.search(r'APP_BUILD = \"([^\"]+)\"',open('app.py',encoding='utf-8').read()).group(1))")
RELEASE_DATE=$(echo "$FINAL_BUILD" | sed -E 's/(....)(..)(..)/\1-\2-\3/')

echo "==> 打包 v$FINAL_VER（构建 $FINAL_BUILD）"
mkdir -p dist/release
BUILD_LOG="dist/release/build.log"
# 关键：不能用 `bash build_mac.sh | tail` —— 管道的退出码取自 tail，
# 构建失败会被吞掉，脚本会拿着上一版的 dmg 继续发布（假包事故的根源）
set +e
bash build_mac.sh > "$BUILD_LOG" 2>&1
BUILD_RC=$?
set -e
tail -8 "$BUILD_LOG"
if [ "$BUILD_RC" != "0" ]; then
  echo "  [错误] 构建失败（退出码 $BUILD_RC），发布中止。完整日志：$BUILD_LOG"
  exit 1
fi

DMG="dist/$APP_NAME.dmg"
if [ ! -f "$DMG" ]; then
  echo "  [错误] 未生成 dmg，发布中止"
  exit 1
fi

# ---- 发布前终检：dmg 内的二进制必须是本次构建的产物 ----
# 只信「挂载后实测」，不信文件时间戳或文件名
MNT="/tmp/yima-rel-verify-$$"
rm -rf "$MNT" && mkdir -p "$MNT"
if ! hdiutil attach "$DMG" -nobrowse -readonly -mountpoint "$MNT" >/dev/null 2>&1; then
  echo "  [错误] dmg 无法挂载校验，发布中止"
  rm -rf "$MNT"
  exit 1
fi
DMG_VER=$("$MNT/易码互传.app/Contents/MacOS/易码互传" --version 2>/dev/null | sed -E 's/.*v([0-9.]+).*/\1/')
DMG_SHA=$(shasum -a 256 "$MNT/易码互传.app/Contents/MacOS/易码互传" 2>/dev/null | awk '{print $1}')
hdiutil detach "$MNT" -force >/dev/null 2>&1
rm -rf "$MNT"
SRC_SHA=$(shasum -a 256 "dist/易码互传.app/Contents/MacOS/易码互传" | awk '{print $1}')

if [ "$DMG_VER" != "$FINAL_VER" ]; then
  echo "  [错误] dmg 内版本(${DMG_VER:-空}) ≠ 目标版本($FINAL_VER)，发布中止"
  exit 1
fi
if [ "$DMG_SHA" != "$SRC_SHA" ]; then
  echo "  [错误] dmg 内二进制与构建产物 sha256 不一致，发布中止"
  exit 1
fi
echo "  终检通过：dmg 内含 v$DMG_VER，且与构建产物逐字节一致"

# ---- 生成更新清单 ----
# 安装包用 ASCII 文件名：避免中文名在 HTTP 服务器 / URL 编码上的兼容问题
OUT_DMG="dist/release/yima-transfer-${FINAL_VER}-${FINAL_BUILD}.dmg"
cp "$DMG" "$OUT_DMG"

$PY - "$FINAL_VER" "$FINAL_BUILD" "$RELEASE_DATE" "$CHANNEL" "$NOTES" "$OUT_DMG" <<'PYEOF'
import hashlib, json, os, sys
ver, build, date, channel, notes, path = sys.argv[1:7]

def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

notes_list = [n.strip() for n in notes.replace(";", "\n").splitlines() if n.strip()]
manifest = {
    "app": "易码互传",
    "latest": ver,
    "build": build,
    "release_date": date,
    "channel": channel,
    "min_system": "10.13",
    "notes": notes_list or ["常规更新与优化"],
    "file": os.path.basename(path),
    "url": os.path.basename(path),      # 与清单同目录，客户端会解析成绝对地址
    "size": os.path.getsize(path),
    "sha256": sha256(path),
}
with open("dist/release/version.json", "w", encoding="utf-8") as fh:
    json.dump(manifest, fh, ensure_ascii=False, indent=2)
print("  更新清单已生成：dist/release/version.json")
print("  sha256: %s…" % manifest["sha256"][:16])
PYEOF

echo ""
echo "============================================"
echo "发布物已就绪：dist/release/"
ls -la dist/release/ | grep -v '^total'
echo ""
echo "上线步骤（把这两个文件传到任意 HTTP 静态目录，例如阿里云 OSS / 公司服务器）："
echo "  1) $(basename "$OUT_DMG")"
echo "  2) version.json"
echo ""
echo "然后在应用的「设置 → 在线升级」里填入清单地址，例如："
echo "  https://你的域名/易码互传/version.json"
echo "之后所有客户端点「检查更新」即可自动升级到 v$FINAL_VER。"
echo "============================================"

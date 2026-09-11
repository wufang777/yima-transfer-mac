#!/bin/bash
# ============================================================
# 易码互传 · 把发布物上传到 GitHub Release（一条命令）
#
# 用法：
#   bash publish_github.sh               # 上传 dist/release 里的当前版本
#   bash publish_github.sh --token XXX   # 用指定令牌，跳过浏览器授权
#
# 前置：先执行 bash release.sh <新版本号> "更新说明"
#       生成 dist/release/yima-transfer-<版本>-<构建>.dmg 与 version.json
#
# 网络说明（本机特有，换机需注意）：
#   代理工具的 fake-IP 把 github.com 映射到了失效节点，所以「设备授权」这
#   一段必须走本地转发（~/yima-github-relay.py，监听 127.0.0.1:7899；
#   双击 ~/启动GitHub转发.command 启动）。
#   api.github.com 与 uploads.github.com 可以直连，因此建 Release、传包
#   不受 fake-IP 影响，无需额外配置。
#
# 令牌缓存：默认存 ~/.yima-github-token（权限 600），失效会自动重新授权。
#          不想落盘就加 --token，或授权后手动删除该文件。
# ============================================================
set -u
cd "$(dirname "$0")"

CLIENT_ID="178c6fc778ccc68e1d6a"
REPO="wufang777/yima-transfer-mac"
RELAY="http://127.0.0.1:7899"
TOKEN_FILE="$HOME/.yima-github-token"
PY=python3
BROWSER="Google Chrome"

API_TOKEN=""
while [ $# -gt 0 ]; do
  case "$1" in
    --token) API_TOKEN="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,21p' "$0"; exit 0 ;;
    *) echo "未知参数：$1（用 --help 看用法）"; exit 1 ;;
  esac
done

command -v gh >/dev/null 2>&1 || { echo "[错误] 未安装 gh CLI，请先执行：brew install gh"; exit 1; }

# ---- 1. 读取发布物并自检 ----
JSON="dist/release/version.json"
[ -f "$JSON" ] || { echo "[错误] 找不到 $JSON"; echo "        请先运行：bash release.sh <新版本号> \"更新说明\""; exit 1; }

VER=$($PY -c "import json;print(json.load(open('$JSON',encoding='utf-8'))['latest'])")
BUILD=$($PY -c "import json;print(json.load(open('$JSON',encoding='utf-8'))['build'])")
SHA=$($PY -c "import json;print(json.load(open('$JSON',encoding='utf-8'))['sha256'])")
SIZE=$($PY -c "import json;print(json.load(open('$JSON',encoding='utf-8'))['size'])")
DMG="dist/release/yima-transfer-${VER}-${BUILD}.dmg"
TAG="v${VER}"

[ -f "$DMG" ] || { echo "[错误] 找不到安装包 $DMG"; exit 1; }
REAL_SHA=$(shasum -a 256 "$DMG" | awk '{print $1}')
if [ "$REAL_SHA" != "$SHA" ]; then
  echo "[错误] 安装包实际 sha256 与清单不一致，拒绝上传（清单可能不是本次构建产出的）"
  echo "        清单：$SHA"
  echo "        实际：$REAL_SHA"
  exit 1
fi
echo "==> 待发布 $TAG（构建 $BUILD）"
echo "    安装包：$(basename "$DMG")  ${SIZE} 字节"

# ---- 2. 取得令牌 ----
token_ok() {
  [ -n "${1:-}" ] || return 1
  curl -s -o /dev/null -w "%{http_code}" --max-time 15 \
    -H "Authorization: token $1" https://api.github.com/user 2>/dev/null | grep -q 200
}

TOKEN="$API_TOKEN"
if token_ok "$TOKEN"; then
  echo "==> 使用命令行传入的令牌"
elif [ -f "$TOKEN_FILE" ] && token_ok "$(cat "$TOKEN_FILE")"; then
  TOKEN="$(cat "$TOKEN_FILE")"
  echo "==> 复用缓存令牌 $TOKEN_FILE"
else
  if ! nc -z 127.0.0.1 7899 2>/dev/null; then
    echo "[错误] 本地 GitHub 转发未启动（127.0.0.1:7899）"
    echo "        请先双击运行：~/启动GitHub转发.command"
    exit 1
  fi
  echo "==> 需要 GitHub 授权，正在申请设备码"
  RESP=$(curl -s -x "$RELAY" --max-time 25 -X POST https://github.com/login/device/code \
    -H "Accept: application/json" -d "client_id=${CLIENT_ID}&scope=repo")
  DEVICE_CODE=$(printf '%s' "$RESP" | sed -n 's/.*"device_code":"\([^"]*\)".*/\1/p')
  USER_CODE=$(printf '%s' "$RESP" | sed -n 's/.*"user_code":"\([^"]*\)".*/\1/p')
  if [ -z "$DEVICE_CODE" ]; then
    echo "[错误] 获取设备码失败：$RESP"
    exit 1
  fi
  echo ""
  echo "    请在浏览器里输入验证码：  $USER_CODE"
  echo ""
  open -a "$BROWSER" "https://github.com/login/device" 2>/dev/null || open "https://github.com/login/device"

  TOKEN=""
  printf "    等待授权"
  for _ in $(seq 1 170); do
    sleep 5
    R=$(curl -s -x "$RELAY" --max-time 25 -X POST https://github.com/login/oauth/access_token \
      -H "Accept: application/json" \
      -d "client_id=${CLIENT_ID}&device_code=${DEVICE_CODE}&grant_type=urn:ietf:params:oauth:grant-type:device_code")
    if printf '%s' "$R" | grep -q '"access_token"'; then
      TOKEN=$(printf '%s' "$R" | sed -n 's/.*"access_token":"\([^"]*\)".*/\1/p')
      break
    fi
    E=$(printf '%s' "$R" | sed -n 's/.*"error":"\([^"]*\)".*/\1/p')
    case "$E" in
      authorization_pending|slow_down) printf "." ;;
      "") printf "?" ;;
      *) echo ""; echo "[错误] 授权失败：$E"; exit 1 ;;
    esac
  done
  echo ""
  [ -n "$TOKEN" ] || { echo "[错误] 授权超时（验证码 15 分钟有效），请重跑本脚本"; exit 1; }
  printf '%s' "$TOKEN" > "$TOKEN_FILE"
  chmod 600 "$TOKEN_FILE"
  echo "==> 授权成功，令牌已缓存到 $TOKEN_FILE"
fi

export GH_TOKEN="$TOKEN"
ACCOUNT=$(gh api user --jq .login 2>/dev/null)
echo "==> 账号：${ACCOUNT:-未知}  仓库：$REPO"

# ---- 3. 生成 Release 说明（从 version.json 派生，避免手写出错）----
NOTES=$(mktemp /tmp/yima-notes.XXXXXX)
$PY - "$JSON" "$NOTES" <<'PYEOF'
import json, sys
m = json.load(open(sys.argv[1], encoding="utf-8"))
L = ["# %s %s（构建 %s）" % (m.get("app", ""), m.get("latest", ""), m.get("build", "")), ""]
L.append("> 发布日期：%s · 渠道：%s · 最低系统：macOS %s"
         % (m.get("release_date", ""), m.get("channel", ""), m.get("min_system", "")))
L += ["", "## 本次更新", ""]
L += ["- %s" % n for n in m.get("notes", [])]
L += ["", "## 安装包", "",
      "| 文件 | 大小 | SHA-256 |", "| --- | --- | --- |",
      "| `%s` | %s 字节 | `%s` |" % (m.get("file", ""), m.get("size", ""), m.get("sha256", ""))]
L += ["", "## 安装说明", "",
      "1. 下载 `.dmg`，把「%s」拖入「应用程序」" % m.get("app", ""),
      "2. **首次打开**：本版本未做苹果开发者签名，请右键图标 → 打开 → 再点「打开」（直接双击会被 Gatekeeper 拦截）",
      "3. 首次运行系统会弹防火墙授权，请点「允许」，局域网互传才能生效",
      "4. 启动后应用常驻**菜单栏**，点菜单栏图标即可打开控制台"]
open(sys.argv[2], "w", encoding="utf-8").write("\n".join(L) + "\n")
PYEOF

# ---- 4. 创建或更新 Release ----
if gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1; then
  echo "==> $TAG 已存在，覆盖上传安装包"
  gh release upload "$TAG" "$DMG" --repo "$REPO" --clobber
else
  echo "==> 创建 Release $TAG"
  gh release create "$TAG" "$DMG" --repo "$REPO" \
    --title "$($PY -c "import json;m=json.load(open('$JSON',encoding='utf-8'));print('%s %s · macOS 原生版（构建 %s）' % (m['app'], m['latest'], m['build']))")" \
    --notes-file "$NOTES" --latest
fi
rm -f "$NOTES"

# ---- 5. 回传校验：把上传的包下载回来比对，杜绝上传损坏 ----
DL=$(mktemp -d /tmp/yima-verify.XXXXXX)
echo "==> 回传校验中（要把 17MB 的包下载回来比对，视网速可能等几分钟，请勿中断）"
if gh release download "$TAG" --repo "$REPO" -D "$DL" --clobber >/dev/null 2>&1; then
  GOT=$(shasum -a 256 "$DL/$(basename "$DMG")" | awk '{print $1}')
  rm -rf "$DL"
  if [ "$GOT" = "$SHA" ]; then
    echo ""
    echo "============================================"
    echo "  发布完成，远程包与本地逐字节一致"
    echo "  https://github.com/$REPO/releases/tag/$TAG"
    echo "============================================"
  else
    echo "[错误] 回传校验失败！本地 $SHA / 远程 $GOT"
    exit 1
  fi
else
  rm -rf "$DL"
  echo "[警告] 回传校验跳过（下载失败），Release 已创建，请手动打开页面确认安装包在不在"
fi

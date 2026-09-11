#!/bin/bash
# ============================================================
# 易码互传 · 发布物上传到阿里云 OSS 静态目录（一条命令）
#
# 用法：
#   bash publish_oss.sh            # 上传 dist/release 里的当前版本
#   bash publish_oss.sh --verify   # 上传后把 dmg 完整下载回来比对 sha256（慢，17MB）
#   bash publish_oss.sh --check    # 只做权限与连通性自检，不上传
#
# 前置（一次性）：
#   1) 配好凭据：ossutil config
#        → 依次填 endpoint / accessKeyID / accessKeySecret
#        凭据存在 ~/.ossutilconfig，属于机密，**不要**放进任何仓库。
#   2) 同目录放一份 oss.conf（从 oss.conf.example 复制改）。
#   3) 先执行 bash release.sh <新版本号> "更新说明" 生成 dist/release/。
#
# 与 publish_github.sh 的关系：
#   GitHub Release 留作代码版本存档（私有，需要登录才能下）；
#   OSS 是给用户下载的正式分发通道（公网匿名可下，自带 HTTPS）。
# ============================================================
set -u
cd "$(dirname "$0")"

PY=python3
CONF="oss.conf"
JSON="dist/release/version.json"
VERIFY=0
CHECK_ONLY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --verify) VERIFY=1; shift ;;
    --check)  CHECK_ONLY=1; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "未知参数：$1（用 --help 看用法）"; exit 1 ;;
  esac
done

# ---- ossutil 定位 ----
OSSUTIL="$(command -v ossutil 2>/dev/null || true)"
if [ -z "$OSSUTIL" ] && [ -x "$HOME/.local/bin/ossutil" ]; then
  OSSUTIL="$HOME/.local/bin/ossutil"
fi
[ -n "$OSSUTIL" ] || { echo "[错误] 找不到 ossutil。安装方法见脚本头部注释。"; exit 1; }

# ---- 读取配置 ----
if [ ! -f "$CONF" ]; then
  echo "[错误] 找不到 $CONF"
  echo "        先复制模板：cp oss.conf.example $CONF  然后按里面的说明改。"
  exit 1
fi
# shellcheck disable=SC1090
source "./$CONF"
: "${BUCKET:?oss.conf 里没配 BUCKET}"
: "${ENDPOINT:?oss.conf 里没配 ENDPOINT}"
PREFIX="${PREFIX:-yima}"

if [ ! -f "$HOME/.ossutilconfig" ]; then
  echo "[错误] ossutil 还没配置凭据（找不到 ~/.ossutilconfig）"
  echo "        请先执行：ossutil config"
  echo "        依次填 endpoint / accessKeyID / accessKeySecret，语言选 CH。"
  exit 1
fi

# 公网访问前缀：优先自定义域名（可挂 CDN），否则用 OSS 默认域名
if [ -n "${PUBLIC_DOMAIN:-}" ]; then
  HOST="${PUBLIC_DOMAIN%/}"
else
  HOST="https://${BUCKET}.${ENDPOINT}"
fi
FEED_URL="${HOST}/${PREFIX}/version.json"

echo "==> Bucket：$BUCKET   地域：$ENDPOINT"
echo "    目录前缀：$PREFIX/"
echo "    公网前缀：$HOST/$PREFIX/"

# ---- --check：只自检 ----
if [ "$CHECK_ONLY" = "1" ]; then
  echo ""
  echo "==> 自检 1/3：凭据是否可用（列 bucket）"
  "$OSSUTIL" ls "oss://${BUCKET}/" -e "$ENDPOINT" -s 2>&1 | tail -5
  echo ""
  echo "==> 自检 2/3：Bucket 读取权限（匿名拉 version.json）"
  CODE=$(curl -s -o /dev/null -w "%{http_code}" -L --max-time 20 "$FEED_URL" 2>/dev/null)
  case "$CODE" in
    200) echo "    200 ✓ 公开可读，匿名客户端能拉到清单" ;;
    404) echo "    404 —— 权限没问题，但清单还没上传（首次发布属正常）" ;;
    403) echo "    403 ✗ Bucket 不是公共读，客户端会拉不到！"
         echo "          到 OSS 控制台把 Bucket 读写权限改成「公共读」，或执行："
         echo "          $OSSUTIL set-acl oss://${BUCKET} public-read -e $ENDPOINT" ;;
    "")  echo "    连接失败 —— 检查网络或 ENDPOINT 是否写对（${ENDPOINT}）" ;;
    *)   echo "    HTTP $CODE —— 需要人工看一眼" ;;
  esac
  echo ""
  echo "==> 自检 3/3：客户端要填的更新源地址"
  echo "    $FEED_URL"
  exit 0
fi

# ---- 读取发布物 ----
[ -f "$JSON" ] || { echo "[错误] 找不到 $JSON"; echo "        请先运行：bash release.sh <新版本号> \"更新说明\""; exit 1; }

VER=$($PY -c "import json;print(json.load(open('$JSON',encoding='utf-8'))['latest'])")
BUILD=$($PY -c "import json;print(json.load(open('$JSON',encoding='utf-8'))['build'])")
SHA=$($PY -c "import json;print(json.load(open('$JSON',encoding='utf-8'))['sha256'])")
SIZE=$($PY -c "import json;print(json.load(open('$JSON',encoding='utf-8'))['size'])")
DMG="yima-transfer-${VER}-${BUILD}.dmg"
DMG_PATH="dist/release/${DMG}"
TAG="v${VER}"

[ -f "$DMG_PATH" ] || { echo "[错误] 找不到安装包 $DMG_PATH"; exit 1; }

REAL_SHA=$(shasum -a 256 "$DMG_PATH" | awk '{print $1}')
if [ "$REAL_SHA" != "$SHA" ]; then
  echo "[错误] 安装包实际 sha256 与清单不一致，拒绝上传（清单可能不是本次构建产出的）"
  echo "        清单：$SHA"
  echo "        实际：$REAL_SHA"
  exit 1
fi
echo ""
echo "==> 待发布 ${TAG}（构建 ${BUILD}）"
echo "    $DMG  ${SIZE} 字节"

# ---- 上传 ----
# 安装包带版本号、内容永不变 → 长缓存；清单会被覆盖 → 必须禁缓存
echo ""
echo "==> 上传安装包（17MB，视网速几十秒）"
"$OSSUTIL" cp -f "$DMG_PATH" "oss://${BUCKET}/${PREFIX}/${DMG}" -e "$ENDPOINT" \
  --meta "Cache-Control:public,max-age=31536000" 2>&1 | tail -3

echo ""
echo "==> 上传更新清单"
"$OSSUTIL" cp -f "$JSON" "oss://${BUCKET}/${PREFIX}/version.json" -e "$ENDPOINT" \
  --meta "Content-Type:application/json#Cache-Control:no-cache" 2>&1 | tail -3

# ---- 公网校验：不看 ossutil 的返回，只认真实拉取结果 ----
echo ""
echo "==> 校验 1/3：匿名拉取更新清单"
LISTED=$(curl -s -L --max-time 25 -H "Cache-Control: no-cache" "$FEED_URL?t=$(date +%s)" 2>/dev/null)
if [ -z "$LISTED" ]; then
  echo "    ✗ 拉不到 $FEED_URL"
  echo "      最常见原因：Bucket 不是公共读。执行下面这条改权限后重试："
  echo "      $OSSUTIL set-acl oss://${BUCKET} public-read -e $ENDPOINT"
  exit 1
fi
REMOTE_VER=$(printf '%s' "$LISTED" | $PY -c "import sys,json;print(json.load(sys.stdin).get('latest',''))" 2>/dev/null)
if [ "$REMOTE_VER" = "$VER" ]; then
  echo "    200 ✓ 清单可读，远端版本 $REMOTE_VER 与本地一致"
else
  echo "    ✗ 清单内容对不上：远端 latest=${REMOTE_VER:-解析失败} / 本地 $VER"
  echo "      OSS 可能有 CDN/边缘缓存，稍等重试；或确认没传错目录前缀。"
  exit 1
fi

echo ""
echo "==> 校验 2/3：安装包大小"
DMG_URL="${HOST}/${PREFIX}/${DMG}"
REMOTE_SIZE=$(curl -sIL --max-time 25 "$DMG_URL" 2>/dev/null | awk 'BEGIN{IGNORECASE=1}/^content-length:/{v=$2}END{gsub(/\r/,"",v);print v}')
if [ "$REMOTE_SIZE" = "$SIZE" ]; then
  echo "    ✓ 远端 $REMOTE_SIZE 字节，与本地一致"
else
  echo "    ✗ 远端 ${REMOTE_SIZE:-取不到} / 本地 $SIZE —— 包可能没传完，或 Bucket 非公共读"
  exit 1
fi

echo ""
if [ "$VERIFY" = "1" ]; then
  echo "==> 校验 3/3：完整下载比对 sha256（17MB，耐心等）"
  TMP=$(mktemp /tmp/yima-oss-verify.XXXXXX)
  if curl -sL --max-time 900 -o "$TMP" "$DMG_URL"; then
    GOT=$(shasum -a 256 "$TMP" | awk '{print $1}')
    rm -f "$TMP"
    if [ "$GOT" = "$SHA" ]; then
      echo "    ✓ 逐字节一致"
    else
      echo "    ✗ 校验失败！本地 $SHA / 远端 $GOT"
      exit 1
    fi
  else
    rm -f "$TMP"
    echo "    ! 下载失败，跳过完整校验（大小校验已通过）"
  fi
else
  echo "==> 校验 3/3：跳过完整 sha256 校验（加 --verify 可开启）"
fi

echo ""
echo "============================================"
echo "  分发完成 $TAG"
echo "  安装包：$DMG_URL"
echo "  清单：  $FEED_URL"
echo ""
echo "  把下面这行填到应用「设置 → 在线升级 → 更新源」："
echo "  $FEED_URL"
echo "============================================"

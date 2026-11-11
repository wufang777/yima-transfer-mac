#!/bin/bash
# 易码互传 鸿蒙版 —— 命令行构建脚本（无需打开 DevEco Studio）
#
# 用法：
#   ./build.sh            # 编译未签名 hap
#   ./build.sh signed     # 编译并用签名配置打包（需先配好 signing 材料）
#
# 依赖：本机已安装 DevEco Studio（默认路径 /Applications/DevEco-Studio.app
#       或挂载的镜像 /Volumes/DevEco-Studio/DevEco-Studio.app）

set -e

# ---------- 1. 定位 DevEco Studio ----------
DEVECO=""
for p in "/Applications/DevEco-Studio.app" "/Volumes/DevEco-Studio/DevEco-Studio.app"; do
  if [ -d "$p/Contents/tools/hvigor" ]; then DEVECO="$p"; break; fi
done
if [ -z "$DEVECO" ]; then
  echo "✗ 未找到 DevEco Studio，请先安装到 /Applications"
  exit 1
fi
echo "→ 使用工具链：$DEVECO"

V="$DEVECO/Contents"
export DEVECO_SDK_HOME="$V/sdk"
export JAVA_HOME="$V/jbr/Contents/Home"
export PATH="$V/tools/node/bin:$JAVA_HOME/bin:/usr/bin:/bin:/usr/sbin:/sbin"
unset NODE_OPTIONS     # 避免宿主的 node 注入干扰 hvigor

PROJ_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJ_DIR"

# ---------- 2. 准备 hvigor 依赖（首次或缺失时自动从 IDE 拷贝） ----------
NEED_COPY=0
[ -d node_modules/@ohos/hvigor ] || NEED_COPY=1
[ -d node_modules/@ohos/hvigor-ohos-plugin ] || NEED_COPY=1
if [ "$NEED_COPY" = "1" ]; then
  echo "→ 首次构建：拷贝 hvigor 与 ohos 插件（约 280MB，稍等）"
  mkdir -p node_modules/@ohos
  rm -rf node_modules/@ohos/hvigor node_modules/@ohos/hvigor-ohos-plugin
  cp -R "$V/tools/hvigor/hvigor" node_modules/@ohos/hvigor
  cp -R "$V/tools/hvigor/hvigor-ohos-plugin" node_modules/@ohos/hvigor-ohos-plugin
fi

# ---------- 3. 构建 ----------
MODE="assembleHap"
if [ "$1" = "signed" ]; then
  MODE="assembleApp"     # 需要 build-profile.json5 中配置 signingConfigs
fi

echo "→ 开始构建（$MODE）"
node ./node_modules/@ohos/hvigor/bin/hvigor.js --mode module -p product=default --no-daemon "$MODE"

# ---------- 4. 输出结果 ----------
HAP=$(find entry/build -name "*.hap" 2>/dev/null | head -1)
if [ -n "$HAP" ]; then
  mkdir -p dist
  cp "$HAP" "dist/$(basename "$HAP")"
  echo "✓ 构建成功：$PROJ_DIR/dist/$(basename "$HAP")  ($(du -h "$HAP" | cut -f1))"
else
  echo "✗ 未找到 hap 产物"
  exit 1
fi

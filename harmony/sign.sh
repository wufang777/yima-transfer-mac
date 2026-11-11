#!/bin/bash
# 易码互传 HarmonyOS 签名脚本
#   发布签名（上架用）：./sign.sh <未签名.hap>
#   调试签名（hdc 侧载用）：SIGN_MODE=debug ./sign.sh <未签名.hap>
set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"
SIGN_DIR="$ROOT/signing"
DEVECO="${DEVECO_HOME:-/Volumes/DevEco-Studio/DevEco-Studio.app/Contents}"
JAVA="$DEVECO/jbr/Contents/Home/bin/java"
JAR="$DEVECO/sdk/default/openharmony/toolchains/lib/hap-sign-tool.jar"

MODE="${SIGN_MODE:-release}"
if [ "$MODE" = "debug" ]; then
  ALIAS="yima-debug"
  APP_CERT="$SIGN_DIR/yima-debug-chain.pem"
  PROFILE="$SIGN_DIR/yima-debug-profile.p7b"
  KEYSTORE="$SIGN_DIR/yima-debug.p12"
  SUFFIX="调试签名"
else
  ALIAS="yima"
  APP_CERT="$SIGN_DIR/yima-release-chain.pem"
  PROFILE="$SIGN_DIR/yima-release-profile.p7b"
  KEYSTORE="$SIGN_DIR/yima.p12"
  SUFFIX="发布签名"
fi

KEY_PWD="${YIMA_KEY_PWD:-Yima@2026}"
STORE_PWD="${YIMA_STORE_PWD:-Yima@2026}"

IN="${1:-$ROOT/../dist/harmony/易码互传-harmony-1.15.0-未签名.hap}"
OUT="${2:-${IN%-未签名.hap}-$SUFFIX.hap}"

[ -f "$IN" ] || { echo "找不到输入 hap: $IN"; exit 1; }
[ -x "$JAVA" ] || { echo "找不到 java: $JAVA"; exit 1; }
[ -f "$JAR" ] || { echo "找不到签名工具: $JAR"; exit 1; }

echo "模式: $MODE   别名: $ALIAS"
echo "输入: $IN"
echo "输出: $OUT"

"$JAVA" -jar "$JAR" sign-app \
  -keyAlias "$ALIAS" \
  -signAlg "SHA256withECDSA" \
  -mode "localSign" \
  -appCertFile "$APP_CERT" \
  -profileFile "$PROFILE" \
  -inFile "$IN" \
  -keystoreFile "$KEYSTORE" \
  -outFile "$OUT" \
  -keyPwd "$KEY_PWD" \
  -keystorePwd "$STORE_PWD"

echo "--- 校验 ---"
"$JAVA" -jar "$JAR" verify-app -inFile "$OUT" \
  -outCertChain /tmp/yima_vf_chain.cer -outProfile /tmp/yima_vf_profile.p7b | tail -4
echo "完成: $OUT"

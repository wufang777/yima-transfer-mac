#!/bin/bash
# 双击即可启动「易码互传」原生桌面界面。
# 会自动挑选一个装有 PySide6 的 Python；本机服务没在跑时，客户端会自行拉起。
cd "$(dirname "$0")" || exit 1

pick_python() {
  for c in "/Users/mac/.workbuddy/binaries/python/envs/default/bin/python" \
           "$(command -v python3)" "/usr/bin/python3"; do
    [ -n "$c" ] && [ -x "$c" ] || continue
    if "$c" -c "import PySide6" >/dev/null 2>&1; then
      echo "$c"
      return 0
    fi
  done
  return 1
}

PY="$(pick_python)"
if [ -z "$PY" ]; then
  echo ">>> 未找到带 PySide6 的 Python，正在安装（约 1-3 分钟）…"
  python3 -m pip install --user PySide6 || { echo "!!! 安装失败，请手动执行: python3 -m pip install --user PySide6"; exit 1; }
  PY="$(pick_python)"
fi
[ -z "$PY" ] && { echo "!!! 仍然找不到可用的 Python"; exit 1; }

echo ">>> 使用解释器: $PY"
exec "$PY" native/yima_qt.py "$@"

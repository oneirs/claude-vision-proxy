#!/bin/bash
# 启动 vision-proxy。前台运行,Ctrl-C 停止。
# 后台跑: ./start.sh & 或用 nohup ./start.sh > proxy.log 2>&1 &
cd "$(dirname "$0")" || exit 1

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

exec python3 vision_proxy.py

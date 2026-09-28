#!/bin/bash
# fork 专属：容器入口包装（上游没有这个文件）。
#
# 做两件事：
#   1. 若 SELF_UPDATE=1 且能看到 docker socket，则在后台拉起自更新守护
#   2. exec 主程序，让 wb2api 保持 PID 1（信号、退出码语义与原来完全一致）
#
# 不启用自更新时（SELF_UPDATE=0 或没挂 socket）行为与上游入口点等价。
set -u

SOCK="${DOCKER_SOCK:-/var/run/docker.sock}"
SCRIPT="${SELF_UPDATE_SCRIPT:-/app/scripts/selfupdate.py}"
CONFIG="${WB2API_CONFIG:-/app/config.json}"
BIN="${WB2API_BIN:-/app/wb2api}"

if [ "${SELF_UPDATE:-0}" = "1" ]; then
  if [ ! -S "$SOCK" ]; then
    echo "[selfupdate] 已设 SELF_UPDATE=1，但看不到 ${SOCK} —— 自更新被跳过。" >&2
    echo "[selfupdate] 请确认用 docker-compose.fork.yml 启动，且其中挂载了 /var/run/docker.sock。" >&2
  elif [ ! -r "$SCRIPT" ]; then
    echo "[selfupdate] 已设 SELF_UPDATE=1，但读不到 ${SCRIPT} —— 自更新被跳过。" >&2
    echo "[selfupdate] 请确认 docker-compose.fork.yml 里的 scripts/selfupdate.py 挂载还在。" >&2
  else
    echo "[selfupdate] 启动自更新守护：镜像=${SELF_UPDATE_IMAGE:-ghcr.io/tarocats/workbuddy2api-panel:latest} 间隔=${SELF_UPDATE_INTERVAL:-21600}s"
    python3 "$SCRIPT" --loop &
  fi
fi

exec "$BIN" -config "$CONFIG"

#!/usr/bin/env bash
set -u
for ip in 8.155.133.216 8.155.131.183; do
  printf '%s 的 22 端口: ' "$ip"
  timeout 6 bash -c "echo > /dev/tcp/$ip/22" 2>/dev/null && echo "【开着】（机器还在运行）" || echo "【探不通】（已停机或未开机）"
done

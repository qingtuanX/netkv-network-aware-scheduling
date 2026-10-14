#!/usr/bin/env bash
set -u
K="/mnt/d/Documents/API/_网络感应 (1).pem"
SO=(-i "$K" -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/tmp/kh_net -o BatchMode=yes -o ConnectTimeout=15 -o LogLevel=ERROR)
echo "=== 本地产物 ==="
ls -la /mnt/e/代码项目文件/论文/sim/results/b1/b1_4c3_20261008T134807Z/ | grep -v '^d'
echo
echo "=== record.csv 尾部 20 行 ==="
tail -20 /mnt/e/代码项目文件/论文/sim/results/b1/b1_4c3_20261008T134807Z/record.csv
echo
echo "=== 两台机器服务残留检查 ==="
for ip in 8.155.133.216 8.155.131.183; do
  echo "--- $ip ---"
  for t in 1 2 3; do
    out=$(ssh "${SO[@]}" root@$ip 'pgrep -a -f "[v]llm serve" || echo 无vllm进程; nvidia-smi --query-gpu=memory.used --format=csv,noheader' 2>&1) && { echo "$out"; break; }
    echo "(第 $t 次无输出，重试)"; sleep 3
  done
done

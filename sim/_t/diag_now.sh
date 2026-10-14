#!/usr/bin/env bash
# 当场状态：拆分臂重启后，decode 为什么 11 分钟还没就绪（只读，带重试）
set -u
K="/mnt/d/Documents/API/_网络感应 (1).pem"
SO=(-i "$K" -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/tmp/kh_net -o BatchMode=yes -o ConnectTimeout=15)
A=8.155.133.216
B=8.155.131.183

echo "=== B: decode 进程 / 健康 / 日志 ==="
for i in 1 2 3; do
  ssh "${SO[@]}" root@$B 'echo "--- vllm 进程 ---"; pgrep -a -f "[v]llm serve" || echo none; echo "--- 本机探 health ---"; curl -s -m 5 -o /dev/null -w "http_code=%{http_code}\n" http://172.29.31.51:8200/health || echo curl失败; echo "--- GPU ---"; nvidia-smi --query-gpu=memory.used --format=csv,noheader; echo "--- decode.log 尾部 30 行 ---"; tail -30 /root/b1/logs/decode.log' && break
  echo "(第 $i 次 ssh 无输出，重试)"; sleep 3
done
echo
echo "=== A: prefill 进程 / 健康 ==="
for i in 1 2 3; do
  ssh "${SO[@]}" root@$A 'echo "--- vllm 进程 ---"; pgrep -a -f "[v]llm serve" | head -3 || echo none; echo "--- health ---"; curl -s -m 5 -o /dev/null -w "http_code=%{http_code}\n" http://172.29.31.52:8100/health || echo curl失败; echo "--- GPU ---"; nvidia-smi --query-gpu=memory.used --format=csv,noheader' && break
  echo "(第 $i 次 ssh 无输出，重试)"; sleep 3
done
echo
echo "=== 后台输出尾部 10 行（有没有新进展） ==="
tail -10 '/mnt/c/Users/qiafe/.local/share/opencode/shell/58c197d4a7dd50e2468de80049440b7e709d01f4/sh_11c10c7da0012vZ1tr3gt71rAJ.out' 2>/dev/null
date -u +'现在 UTC: %H:%M:%SZ'

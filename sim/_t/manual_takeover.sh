#!/usr/bin/env bash
# 人工接管：杀掉卡住的驱动 → 手动清理两台机器 → 记录偏差（全部显式留痕）
set -u
K="/mnt/d/Documents/API/_网络感应 (1).pem"
SO=(-i "$K" -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/tmp/kh_net -o BatchMode=yes -o ConnectTimeout=15 -o LogLevel=ERROR)
A=8.155.133.216
B=8.155.131.183
R=/mnt/e/代码项目文件/论文/sim/results/b1/b1_4c3_20261008T134807Z

echo "=== 1) 找到并杀掉卡住的驱动进程 ==="
ps -eo pid,pid1,etimes,cmd | grep -E 'run_prereg3_clou[d]' || echo "(没找到，可能已退出)"
pkill -f 'run_prereg3_clou[d]\.sh' 2>/dev/null && echo "已发 SIGTERM" || echo "pkill 返回非零（可能已不在）"
sleep 2
ps -eo pid,etimes,cmd | grep -E 'run_prereg3_clou[d]' || echo "确认：驱动已不在"
echo
echo "=== 2) 手动 stop-all @ A（带重试） ==="
okA=0
for t in 1 2 3 4; do
  ssh "${SO[@]}" root@$A 'bash /root/b1/b1_remote_serve.sh stop-all' > "$R/stop_manual_A.log" 2>&1
  if [ -s "$R/stop_manual_A.log" ] && ! grep -q '未知子命令' "$R/stop_manual_A.log"; then okA=1; break; fi
  echo "(A 第 $t 次无有效输出，重试)"; sleep 3
done
echo "A 清理 ok=$okA"; tail -12 "$R/stop_manual_A.log"
echo
echo "=== 3) 手动 stop-all @ B（带重试） ==="
okB=0
for t in 1 2 3 4; do
  ssh "${SO[@]}" root@$B 'bash /root/b1/b1_remote_serve.sh stop-all' > "$R/stop_manual_B.log" 2>&1
  if [ -s "$R/stop_manual_B.log" ] && ! grep -q '未知子命令' "$R/stop_manual_B.log"; then okB=1; break; fi
  echo "(B 第 $t 次无有效输出，重试)"; sleep 3
done
echo "B 清理 ok=$okB"; tail -12 "$R/stop_manual_B.log"
echo
echo "=== 4) 两台机器终态复核 ==="
ssh "${SO[@]}" root@$A 'echo "[A]"; pgrep -a -f "[v]llm serve" || echo "  无 vllm 进程"; nvidia-smi --query-gpu=memory.used --format=csv,noheader'
ssh "${SO[@]}" root@$B 'echo "[B]"; pgrep -a -f "[v]llm serve" || echo "  无 vllm 进程"; nvidia-smi --query-gpu=memory.used --format=csv,noheader'
echo
echo "=== 5) 偏差记录进 record.csv（append-only） ==="
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ),manual-note,1,拆分臂第1轮 decode 末位参数笔误 A_IN 应为 B_IN 导致 ZMQ 绑定失败装死；wait_ready 空转过久 人工终止并手动 stop-all 后重跑" >> "$R/record.csv"
tail -3 "$R/record.csv"

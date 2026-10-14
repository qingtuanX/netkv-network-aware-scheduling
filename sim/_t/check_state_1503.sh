#!/usr/bin/env bash
# 一次性状态勘查：拆分臂失败后（只读）
set -u
K="/mnt/d/Documents/API/_网络感应 (1).pem"
SO=(-i "$K" -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/tmp/kh_net -o BatchMode=yes -o ConnectTimeout=15)
A=8.155.133.216
B=8.155.131.183
R=/mnt/e/代码项目文件/论文/sim/results/b1/b1_4c3_20261008T134807Z
OUT=/mnt/c/Users/qiafe/.local/share/opencode/shell/58c197d4a7dd50e2468de80049440b7e709d01f4/sh_11bfa5b24001eYYOY2ePQUxkZH.out

echo "=========== 0) 后台输出全文件尾部 80 行 ==========="
tail -80 "$OUT" 2>/dev/null || echo "(读不到)"
echo
echo "=========== 1) 本地 record.csv 尾部 ==========="
tail -30 "$R/record.csv"
echo
echo "=========== 2) 本地 stop 日志 ==========="
for f in "$R"/stop_*.log; do echo "--- $(basename "$f") ($(wc -c < "$f") 字节) ---"; head -20 "$f"; done
echo
echo "=========== 3) A 机：进程 / GPU / 产物 ==========="
ssh "${SO[@]}" root@$A 'echo "--- vllm 进程 ---"; pgrep -a -f "[v]llm serve" || echo none; echo "--- toy_proxy ---"; pgrep -a -f "[t]oy_proxy_server" || echo none; echo "--- GPU 显存 ---"; nvidia-smi --query-gpu=memory.used --format=csv,noheader; echo "--- prereg3_out ---"; ls -la /root/b1/prereg3_out/'
echo
echo "=========== 4) B 机：进程 / GPU / decode.log 死因 ==========="
ssh "${SO[@]}" root@$B 'echo "--- vllm 进程 ---"; pgrep -a -f "[v]llm serve" || echo none; echo "--- GPU 显存 ---"; nvidia-smi --query-gpu=memory.used --format=csv,noheader; echo "--- decode.log 尾部 50 行 ---"; tail -50 /root/b1/logs/decode.log; echo "--- decode.log 报错关键词 ---"; grep -n -i -E "error|traceback|failed|abort|exception" /root/b1/logs/decode.log | tail -20'
echo
echo "=========== 5) 对照臂数据抽检（文件大小） ==========="
ssh "${SO[@]}" root@$A 'ls -la /root/b1/prereg3_out/ | grep -E "baseline|smoke"'

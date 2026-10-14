#!/usr/bin/env bash
# 诊断五：定位"驱动里静默失败"的真正差异
set -u
K="/mnt/d/Documents/API/_网络感应 (1).pem"
SO=(-i "$K" -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/tmp/kh_net -o BatchMode=yes -o ConnectTimeout=20 -o LogLevel=ERROR)
A=8.155.133.216
BIN=/mnt/e/代码项目文件/论文/sim/_t
RD=/mnt/e/代码项目文件/论文/sim/results/b1/b1_4c3_20261008T134807Z

echo "=== 0) 驱动文件的字节体检（CRLF？不可见字符？） ==="
file /mnt/e/代码项目文件/论文/sim/k8s_test/run_prereg3_cloud.sh
echo "CR 字符计数: $(grep -c $'\r' /mnt/e/代码项目文件/论文/sim/k8s_test/run_prereg3_cloud.sh || true)"
sed -n '60,64p' /mnt/e/代码项目文件/论文/sim/k8s_test/run_prereg3_cloud.sh | od -c | tail -12
echo
echo "=== 1) A 的 auth.log 22:36–22:39（diag3 重放窗口） ==="
ssh "${SO[@]}" root@$A "grep -aE 'T22:3[6-9]:' /var/log/auth.log | grep -av 'session opened' | grep -av 'CRON' | tail -30"
echo
echo "=== 2) diag3 留下的 569 字节日志 raw 检查 ==="
ls -la "$RD/_debug/"
od -c "$RD/_debug/smoke_baseline_replay.log" | tail -6
echo
echo "=== 3) 完整复刻 diag3 场景重放（sourced strip + set -x，产物到 _debug2） ==="
SIM=/mnt/e/代码项目文件/论文/sim
STRIP=/tmp/prereg3_driver_stripped.sh
sed '/^for ph in/,$d' "$SIM/k8s_test/run_prereg3_cloud.sh" > "$STRIP"
source "$STRIP"
R="$R/_debug2"; mkdir -p "$R"
echo "--- 重放开始 $(date -u +%H:%M:%S) ---"
set -x
smoke_and_gate "http://172.29.31.51:8300" replay2
RC=$?
set +x
echo "--- 重放结束 $(date -u +%H:%M:%S) rc=$RC ---"
echo "--- 本地 log: $(wc -c < "$R/smoke_replay2.log") 字节 ---"
head -30 "$R/smoke_replay2.log"
echo
echo "=== 4) 对照：同命令、直接 ssh（无 set -x 残影） ==="
CMD="mkdir -p /root/b1/prereg3_out && cd /root/b1 && ./venv/bin/python b1_client.py --url 'http://172.29.31.51:8300' --model 'Qwen/Qwen2.5-1.5B-Instruct' --prompts-file /root/b1/prereg3_prompts.json --logprobs 20 --only-ids q01_rep6 q02_rep10 q03_rep14 --out /root/b1/prereg3_out/t_e.json"
ssh "${SO[@]}" root@$A "$CMD" > "$BIN/t_e.log" 2>&1
echo "T-E rc=$?  $(wc -c < "$BIN/t_e.log") 字节"
tail -6 "$BIN/t_e.log"
echo
echo "=== 5) 用 rsh 函数再发一次（抓 rc；--out t_f.json） ==="
CMDF="mkdir -p /root/b1/prereg3_out && cd /root/b1 && ./venv/bin/python b1_client.py --url 'http://172.29.31.51:8300' --model 'Qwen/Qwen2.5-1.5B-Instruct' --prompts-file /root/b1/prereg3_prompts.json --logprobs 20 --only-ids q01_rep6 q02_rep10 q03_rep14 --out /root/b1/prereg3_out/t_f.json"
rsh $A "$CMDF" > "$BIN/t_f.log" 2>&1
echo "T-F rc=$?  $(wc -c < "$BIN/t_f.log") 字节"
tail -6 "$BIN/t_f.log"
echo
echo "=== 6) 远端产物 ==="
ssh "${SO[@]}" root@$A 'ls -la /root/b1/prereg3_out/'

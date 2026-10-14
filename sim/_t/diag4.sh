#!/usr/bin/env bash
# 诊断四：bisect 那条静默失败的 ssh（加 -v 看它死在哪一步）
set -u
K="/mnt/d/Documents/API/_网络感应 (1).pem"
A=8.155.133.216
BIN=/mnt/e/代码项目文件/论文/sim/_t
SO=(-i "$K" -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/tmp/kh_net -o BatchMode=yes -o ConnectTimeout=20 -o LogLevel=ERROR)
SO_V=(-i "$K" -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/tmp/kh_net -o BatchMode=yes -o ConnectTimeout=20 -v)

CMD_A="mkdir -p /root/b1/prereg3_out && cd /root/b1 && ./venv/bin/python b1_client.py --url 'http://172.29.31.51:8300' --model 'Qwen/Qwen2.5-1.5B-Instruct' --prompts-file /root/b1/prereg3_prompts.json --logprobs 20 --only-ids q01_rep6 q02_rep10 q03_rep14 --out /root/b1/prereg3_out/t_a.json"
CMD_B="mkdir -p /root/b1/prereg3_out && cd /root/b1 && ./venv/bin/python b1_client.py --url http://172.29.31.51:8300 --model Qwen/Qwen2.5-1.5B-Instruct --prompts-file /root/b1/prereg3_prompts.json --logprobs 20 --only-ids q01_rep6 q02_rep10 q03_rep14 --out /root/b1/prereg3_out/t_b.json"

for i in 1 2 3; do
  echo "=== T-1.$i: 原样失败命令 + ssh -v 调试 ==="
  date -u +%H:%M:%S
  ssh -v "${SO_V[@]}" root@$A "$CMD_A" > "$BIN/t1_$i.log" 2>&1
  echo "T-1.$i rc=$?  $(date -u +%H:%M:%S)  log=$(wc -c < "$BIN/t1_$i.log") 字节, $(wc -l < "$BIN/t1_$i.log") 行"
  echo "--- 日志尾部 25 行 ---"
  tail -25 "$BIN/t1_$i.log"
  echo
done

echo "=== T-2: 去单引号版（对照） ==="
date -u +%H:%M:%S
ssh "${SO[@]}" root@$A "$CMD_B" > "$BIN/t2.log" 2>&1
echo "T-2 rc=$?  $(date -u +%H:%M:%S)  log=$(wc -c < "$BIN/t2.log") 字节"
tail -12 "$BIN/t2.log"
echo
echo "=== T-3: 原样命令、换输出文件、直接重定向文件 ==="
date -u +%H:%M:%S
ssh "${SO[@]}" root@$A "${CMD_A//t_a.json/t_c.json}" > "$BIN/t3.log" 2>&1
echo "T-3 rc=$?  $(date -u +%H:%M:%S)  log=$(wc -c < "$BIN/t3.log") 字节"
tail -12 "$BIN/t3.log"
echo
echo "=== 远端产物一览 ==="
ssh "${SO[@]}" root@$A 'ls -la /root/b1/prereg3_out/'

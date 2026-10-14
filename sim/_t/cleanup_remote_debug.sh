#!/usr/bin/env bash
# 一次性清理：把冒烟/调试残留挪进 _debug/，保证正式产物目录干净
set -u
K="/mnt/d/Documents/API/_网络感应 (1).pem"
SO=(-i "$K" -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/tmp/kh_net -o BatchMode=yes -o ConnectTimeout=20 -o LogLevel=ERROR)
ssh "${SO[@]}" root@8.155.133.216 'mkdir -p /root/b1/prereg3_out/_debug && cd /root/b1/prereg3_out && for f in smoke_baseline.json smoke_baseline_raw.jsonl smoke_replay2.json smoke_replay2_raw.jsonl t_a.json t_a_raw.jsonl t_b.json t_b_raw.jsonl t_c.json t_c_raw.jsonl t_e.json t_e_raw.jsonl t_f.json t_f_raw.jsonl; do [ -e "$f" ] && mv -f "$f" _debug/; done; echo "--- prereg3_out 顶层 ---"; ls -la /root/b1/prereg3_out/; echo "--- _debug/ ---"; ls /root/b1/prereg3_out/_debug/'

#!/usr/bin/env bash
# 一次性核对：两台机器的模型目录都在（只读）
K="/mnt/d/Documents/API/_网络感应 (1).pem"
SO=(-i "$K" -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/tmp/kh_net -o BatchMode=yes -o ConnectTimeout=15)
for ip in 8.155.133.216 8.155.131.183; do
  echo "== $ip =="
  ssh "${SO[@]}" "root@$ip" 'ls /root/b1/models/Qwen2.5-1.5B-Instruct/ | head -5; du -sh /root/b1/models/Qwen2.5-1.5B-Instruct 2>/dev/null'
done

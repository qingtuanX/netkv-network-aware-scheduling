#!/usr/bin/env bash
set -u
K="/mnt/d/Documents/API/_网络感应 (1).pem"
SO=(-i "$K" -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/tmp/kh_net -o BatchMode=yes -o ConnectTimeout=15 -o LogLevel=ERROR)
for ip in 8.155.133.216 8.155.131.183; do
  for t in 1 2 3; do
    out=$(ssh "${SO[@]}" root@$ip 'hostname' 2>&1); rc=$?
    if [ -n "$out" ]; then echo "$ip → $out (rc=$rc)"; break; fi
    echo "$ip 第 $t 次空输出 (rc=$rc)，重试"; sleep 3
  done
done

#!/usr/bin/env bash
set -u
K="/mnt/d/Documents/API/_网络感应 (1).pem"
SO=(-i "$K" -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/tmp/kh_net -o BatchMode=yes -o ConnectTimeout=15 -o LogLevel=ERROR)
for ip in 8.155.133.216 8.155.131.183; do
  printf '%s → ' "$ip"
  ssh "${SO[@]}" root@$ip 'hostname; uptime' 2>&1 | tr "\n" " " || printf 'ssh 失败'
  echo
done

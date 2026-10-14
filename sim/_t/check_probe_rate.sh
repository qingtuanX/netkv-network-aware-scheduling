#!/usr/bin/env bash
set -u
K="/mnt/d/Documents/API/_网络感应 (1).pem"
SO=(-i "$K" -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/tmp/kh_net -o BatchMode=yes -o ConnectTimeout=15 -o LogLevel=ERROR)
echo "=== B 机最近 12 次登录（时间戳） ==="
ssh "${SO[@]}" root@8.155.131.183 "grep -a 'Accepted' /var/log/auth.log | tail -12 | cut -c1-33"
echo
echo "=== A 机最近 6 次登录 ==="
ssh "${SO[@]}" root@8.155.133.216 "grep -a 'Accepted' /var/log/auth.log | tail -6 | cut -c1-33"
echo
date -u +'现在 UTC: %H:%M:%SZ'

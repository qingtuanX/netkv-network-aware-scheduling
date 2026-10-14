#!/usr/bin/env bash
# 一次性诊断：B 机（8.155.131.183）为什么连不上——只读，不做任何改动
K="/mnt/d/Documents/API/_网络感应 (1).pem"
SO=(-i "$K" -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/tmp/kh_net -o BatchMode=yes -o ConnectTimeout=15)
R=/mnt/e/代码项目文件/论文/sim/results/b1/b1_4c3_20261008T134807Z

echo "== check 阶段记下的错误 =="
cat "$R/check_B.txt" 2>/dev/null
echo
echo "== 重试 SSH 到 B 公网 =="
ssh "${SO[@]}" root@8.155.131.183 hostname 2>&1 | tail -3
echo
echo "== B 公网 22 端口 TCP 探测（本机侧）=="
timeout 5 bash -c 'echo > /dev/tcp/8.155.131.183/22' && echo "22 通（说明机器活着，是 key/host 问题）" || echo "22 不通（IP 变了或没起来）"
echo
echo "== 从 A 内网 ping B（172.29.31.51）=="
ssh "${SO[@]}" root@8.155.133.216 "ping -c 2 -W 2 172.29.31.51 2>&1 | tail -3"
echo
echo "== 从 A 内网探测 B 的 22 端口 =="
ssh "${SO[@]}" root@8.155.133.216 "timeout 5 bash -c 'echo x > /dev/tcp/172.29.31.51/22' && echo B内网22通 || echo B内网22不通"

#!/usr/bin/env bash
# 一次性诊断：对照臂服务启动失败的死因（只读）
K="/mnt/d/Documents/API/_网络感应 (1).pem"
SO=(-i "$K" -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/tmp/kh_net -o BatchMode=yes -o ConnectTimeout=15)
R=/mnt/e/代码项目文件/论文/sim/results/b1/b1_4c3_20261008T134807Z

echo "== 本地记录的启动输出（start_baseline.log）=="
cat "$R/start_baseline.log"
echo
echo "== B 上的服务日志尾部 /root/b1/logs/baseline.log =="
ssh "${SO[@]}" root@8.155.131.183 'tail -n 40 /root/b1/logs/baseline.log'
echo
echo "== B 上的报错关键词 =="
ssh "${SO[@]}" root@8.155.131.183 'grep -n -i -E "error|usage|unrecognized|invalid|failed" /root/b1/logs/baseline.log | head -15'

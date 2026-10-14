#!/usr/bin/env bash
# 一次性诊断（只读）：对照臂冒烟失败的现场勘查 v2
K="/mnt/d/Documents/API/_网络感应 (1).pem"
SO=(-i "$K" -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/tmp/kh_net -o BatchMode=yes -o ConnectTimeout=15)
R=/mnt/e/代码项目文件/论文/sim/results/b1/b1_4c3_20261008T134807Z

echo "== 本地 run 目录 =="
ls -la "$R/"
echo "== smoke_baseline.log 字节数 =="
wc -c "$R/smoke_baseline.log" 2>&1
echo "== verdict 全文 =="
cat "$R/smoke_baseline.verdict" 2>&1 | head -40
echo
echo "== B: baseline 服务日志头 25 行（确认最后一次启动的时间/pid/参数）=="
ssh "${SO[@]}" root@8.155.131.183 'head -25 /root/b1/logs/baseline.log'
echo
echo "== B: 服务日志尾 60 行 =="
ssh "${SO[@]}" root@8.155.131.183 'tail -n 60 /root/b1/logs/baseline.log'
echo
echo "== B: 报错/死亡线索 =="
ssh "${SO[@]}" root@8.155.131.183 'grep -n -i -E "traceback|error|killed|oom|abort" /root/b1/logs/baseline.log | tail -25'
echo
echo "== B: dmesg 尾部 =="
ssh "${SO[@]}" root@8.155.131.183 'dmesg -T 2>/dev/null | tail -12 || echo no-dmesg'
echo
echo "== B: 进程与内存 =="
ssh "${SO[@]}" root@8.155.131.183 'ps aux | grep -E "vllm|toy_proxy" | grep -v grep | head -6; echo ---; free -g | head -2'
echo
echo "== A: ssh 会话日志（找 22:2x 的会话，CST=UTC+8）=="
ssh "${SO[@]}" root@8.155.133.216 'grep -a "Accepted" /var/log/auth.log 2>/dev/null | tail -18'
echo
echo "== A: prereg3_out 现状 =="
ssh "${SO[@]}" root@8.155.133.216 'ls -la /root/b1/prereg3_out/ 2>&1'
echo
echo "== A: /tmp/kh_net 是否存在、几个条目 =="
ssh "${SO[@]}" root@8.155.133.216 'wc -l /tmp/kh_net 2>/dev/null || echo 无此文件'

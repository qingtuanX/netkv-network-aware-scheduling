#!/usr/bin/env bash
# 诊断三：重放驱动里真实的 smoke_and_gate（服务已停，只看管道层）+ 空窗期 auth.log 全查
set -u
K="/mnt/d/Documents/API/_网络感应 (1).pem"
SO=(-i "$K" -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/tmp/kh_net -o BatchMode=yes -o ConnectTimeout=15)

echo "########## 0) 原 run.log（失败现场的日志） ##########"
cat /mnt/e/代码项目文件/论文/sim/results/b1/b1_4c3_20261008T134807Z/run.log
echo
echo "########## 1) A 的 auth.log：22:22–22:25 全部行（去掉 session opened/closed） ##########"
ssh "${SO[@]}" root@8.155.133.216 "grep -aE 'T22:2[2-5]:' /var/log/auth.log | grep -av 'session opened' | grep -av 'session closed' | tail -30"
echo
echo "########## 2) B 的 auth.log：同窗口（对照） ##########"
ssh "${SO[@]}" root@8.155.131.183 "grep -aE 'T22:2[2-5]:' /var/log/auth.log | grep -av 'session opened' | grep -av 'session closed' | tail -30"
echo
echo "########## 3) 摘出驱动函数（去掉尾部执行段）后 source ##########"
SIM=/mnt/e/代码项目文件/论文/sim
STRIP=/tmp/prereg3_driver_stripped.sh
sed '/^for ph in/,$d' "$SIM/k8s_test/run_prereg3_cloud.sh" > "$STRIP"
wc -l "$STRIP" "$SIM/k8s_test/run_prereg3_cloud.sh"
source "$STRIP" || { echo "source 失败"; exit 1; }
echo "source 完成（未执行任何阶段）"
echo
echo "########## 4) bash -x 重放 smoke_and_gate（服务已停；本地产物进 _debug 子目录） ##########"
R="$R/_debug"; mkdir -p "$R"
set -x
smoke_and_gate "http://172.29.31.51:8300" baseline_replay
echo "smoke rc=$?"
set +x
echo
echo "########## 5) 重放的本地产物 ##########"
ls -la "$R/"
echo "--- 客户端日志（前25行） ---"
cat "$R/smoke_baseline_replay.log" 2>/dev/null | head -25
echo "--- verdict（前15行） ---"
cat "$R/smoke_baseline_replay.verdict" 2>/dev/null | head -15
echo
echo "########## 6) 远端 prereg3_out ##########"
ssh "${SO[@]}" root@8.155.133.216 'ls -la /root/b1/prereg3_out/'

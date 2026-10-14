#!/usr/bin/env bash
# 一次性体检：对照臂正式数据（只读 + 上传一个只读检查脚本）
set -u
K="/mnt/d/Documents/API/_网络感应 (1).pem"
SO=(-i "$K" -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/tmp/kh_net -o BatchMode=yes -o ConnectTimeout=20 -o LogLevel=ERROR)
A=8.155.133.216

echo "=== 语法检查 ==="
bash -n /mnt/e/代码项目文件/论文/sim/k8s_test/run_prereg3_cloud.sh && echo SYNTAX_OK
echo
echo "=== 上传检查脚本 ==="
scp -q "${SO[@]}" /mnt/e/代码项目文件/论文/sim/_t/vercheck.py root@$A:/tmp/vercheck.py && echo scp-ok
echo
echo "=== 请求计数与步数 ==="
ssh "${SO[@]}" root@$A '/root/b1/venv/bin/python /tmp/vercheck.py'
echo
echo "=== C1 逐词校验：正式 r1/r2/r3 ==="
for k in 1 2 3; do
  ssh "${SO[@]}" root@$A "cd /root/b1 && ./venv/bin/python b1_4c3_analyze.py smoke --file /root/b1/prereg3_out/arm_baseline_r$k.json"
done
echo
echo "=== r1..r3 哈希（看重复性） ==="
ssh "${SO[@]}" root@$A 'sha256sum /root/b1/prereg3_out/arm_baseline_r*.json'

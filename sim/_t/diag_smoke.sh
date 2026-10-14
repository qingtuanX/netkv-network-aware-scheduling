#!/usr/bin/env bash
# 一次性诊断：手工重放冒烟客户端命令（服务已停，故意——看客户端能否照常落盘报错记录）
K="/mnt/d/Documents/API/_网络感应 (1).pem"
SO=(-i "$K" -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/tmp/kh_net -o BatchMode=yes -o ConnectTimeout=15)

echo "== 手工重放客户端（对照服务已停，预期 http_status=连接失败，但 JSON 文件应该落盘）=="
ssh "${SO[@]}" root@8.155.133.216 "mkdir -p /root/b1/prereg3_out && cd /root/b1 && ./venv/bin/python b1_client.py --url http://172.29.31.51:8300 --model Qwen/Qwen2.5-1.5B-Instruct --prompts-file /root/b1/prereg3_prompts.json --logprobs 20 --only-ids q01_rep6 q02_rep10 q03_rep14 --out /root/b1/prereg3_out/smoke_baseline.json; echo CLIENT_RC=\$?"
echo
echo "== 产物目录 =="
ssh "${SO[@]}" root@8.155.133.216 "ls -la /root/b1/prereg3_out/ 2>&1; echo ---; tail -c 400 /root/b1/prereg3_out/smoke_baseline.json 2>&1"

# PREREG_3 上云 runbook —— processed 分的 4c 解释性诊断（执行单）

> **本文件只是执行单**：判据、指标、停止条件全部冻结在 `B1_4C_PREREG_3.md`，
> 这里不新增、不修改任何判定规则。跑之前先读预注册 §2/§4/§8。
> 预演已完成（`b1_4c3_local_20261008T130152Z`）：对照臂 + C1 冒烟（154 步全过）+ E4/E1/E2/E3
> 分析链路全绿；真拆分拓扑本机跑不了（8 GB 显存 + 无 nixl，记为偏离 D5/D6），由本轮云端覆盖。
> **4c 判定（6/15、未通过）本轮不动；两台机器跑完立即停机。**
>
> **【状态 2026-10-09】已执行完毕**：run `b1_4c3_20261008T134807Z`，两臂 C1 冒烟 PASS、
> E3 双臂逐位相同、E4/E1/E2 已落盘；结果记录见 `B1_4C3_REPORT.md`。
> 两台已按「节省停机模式」停机。本执行单保留作历史，**不要再照单重跑**（复跑需另写预注册）。

## 0. 你要先做的两件事（人工，约 5 分钟）

1. **控制台开机两台实例**（阿里云 `cn-heyuan`，就是 B1 那两台 `i-f8z4ggqwhyqz0r6lp1mj/mi`）。
   注意控制台原文："启动时可能因库存不足导致启动失败"——失败就换可用区时段重试。
   **私网 IP 不变**（172.29.31.52 / .51），**公网 IP 可能变** ⇒ 开机后把新的公网 IP 记下来。
2. **看一眼账单**（节省停机期间系统盘/数据盘仍在计费）。预算口径沿用 B1：
   单次连续 ≤ 4 h/台、累计 ≤ 16 卡时；**本轮预计 30–60 分钟墙钟 ≈ 1–2 卡时**。

## 1. 连通与环境核对（开机后第一件事）

在 Git Bash 里（与 `run_b1.sh` 同一套 SSH 参数；key 路径直接从脚本里取，避免手抄中文路径）：

```bash
cd /e/代码项目文件/论文/sim
SSH_KEY=$(grep -m1 '^SSH_KEY=' k8s_test/run_b1.sh | cut -d'"' -f2)
SSH_OPTS=(-i "$SSH_KEY" -o IdentitiesOnly=yes -o StrictHostKeyChecking=no \
          -o UserKnownHostsFile=/tmp/kh_net -o BatchMode=yes -o ConnectTimeout=20 -o LogLevel=ERROR)
A_PUB='<控制台确认的新公网 IP（原 8.155.133.216）>'
B_PUB='<控制台确认的新公网 IP（原 8.155.131.183）>'
A_IN=172.29.31.52; B_IN=172.29.31.51

# 探活 + 环境三件套（vllm 0.31.0 / nixl 1.4.1 / 显卡空闲）
for h in "$A_PUB" "$B_PUB"; do
  ssh "${SSH_OPTS[@]}" "root@$h" 'hostname; nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv,noheader; \
    /root/b1/venv/bin/python -c "import vllm,nixl,torch;print(vllm.__version__,torch.__version__)"; \
    ls /root/b1/venv/bin/vllm /root/b1/out/toy_proxy_server.py >/dev/null && echo tools-ok'
done
```

**判定**：两台都是 A10 22 GB、vllm=0.31.0、tools-ok。任何一条不符 ⇒ 停，先修环境再继续
（模型/venv 在磁盘上，节省停机**不会**丢；缺了才需要重下，见 B1_REPORT §5.4 的下载速度）。

## 2. 同步本轮脚本（云端副本是旧版，必须换）

本轮新增/改动：`b1_remote_serve.sh`（`B1_LOGPROBS_MODE`/`B1_GPU_UTIL` 两个开关）、
`b1_client.py`、`b1_4c3_analyze.py`。同步并核对哈希：

```bash
for h in "$A_PUB" "$B_PUB"; do
  scp "${SSH_OPTS[@]}" k8s_test/b1_remote_serve.sh k8s_test/b1_client.py "root@$h:/root/b1/"
done
scp "${SSH_OPTS[@]}" k8s_test/b1_4c3_analyze.py "root@$A_PUB:/root/b1/"
sha256sum k8s_test/b1_remote_serve.sh k8s_test/b1_client.py k8s_test/b1_4c3_analyze.py > /tmp/prereg3_scripts.sha256
for h in "$A_PUB" "$B_PUB"; do ssh "${SSH_OPTS[@]}" "root@$h" 'sha256sum /root/b1/b1_remote_serve.sh /root/b1/b1_client.py'; done
# 逐行比对上面两台的哈希与本地 /tmp/prereg3_scripts.sha256
```

## 3. 本轮采集参数（冻结，别改）

| 项 | 值 | 说明 |
|---|---|---|
| logprobs 口径 | **`--logprobs-mode processed_logprobs`** | ★ 本轮唯一新增；C1 冒烟把关 |
| 显存预算 | **0.85**（云端不降档） | 本机预演的 D1/D2 偏离**不带上来** |
| 其余启动参数 | `--enforce-eager --no-enable-prefix-caching --max-model-len 8192` | B1 同款 |
| 请求参数 | `temperature=0 / seed=0 / max_tokens=64 / return_token_ids=true / logprobs=20` | B1 同款 |
| 提示词 | `b1_client.py gen-prompts --preset v2`（q01_rep6…q05_rep22） | 预注册 §3；sha256 留档 |
| 部署形态 | P=A:8100、D=B:8200、代理=A:8192（只绑回环）、对照=B:8300 | B1 同款 |

## 4. 阶段一：对照臂（单机完整服务，B:8300）

```bash
# 4.1 起服务（在 B 上；B1_BIND_IP=B 内网 IP，沿用 B1 的绑定纪律）
ssh "${SSH_OPTS[@]}" "root@$B_PUB" "B1_BIND_IP=$B_IN B1_LOGPROBS_MODE=processed_logprobs \
  bash /root/b1/b1_remote_serve.sh start-baseline 8300 /root/b1/venv/bin/vllm \
  Qwen/Qwen2.5-1.5B-Instruct Qwen/Qwen2.5-1.5B-Instruct"

# 4.2 等就绪
ssh "${SSH_OPTS[@]}" "root@$B_PUB" "for i in \$(seq 1 90); do curl -s -o /dev/null -w '%{http_code}' http://$B_IN:8300/health | grep -q 200 && break; sleep 2; done"

# 4.3 ★ C1 冒烟（q01-q03；在 A 上打 B:8300）——不过就停，不采数（预注册 §8）
ssh "${SSH_OPTS[@]}" "root@$A_PUB" "cd /root/b1 && ./venv/bin/python b1_client.py gen-prompts --preset v2 --out /root/b1/prereg3_prompts.json >/dev/null; \
  ./venv/bin/python b1_client.py --url http://$B_IN:8300 --model Qwen/Qwen2.5-1.5B-Instruct \
    --prompts-file /root/b1/prereg3_prompts.json --logprobs 20 --only-ids q01_rep6 q02_rep10 q03_rep14 \
    --out /root/b1/smoke_baseline.json && \
  ./venv/bin/python b1_4c3_analyze.py smoke --file /root/b1/smoke_baseline.json"
# 输出「[C1 冒烟] 步数=… 不合=0 → PASS」才继续；FAIL ⇒ 记录后停机，回校准

# 4.4 预热 ×3 + 正式 ×3（串行）
for k in 1 2 3; do
  ssh "${SSH_OPTS[@]}" "root@$A_PUB" "cd /root/b1 && ./venv/bin/python b1_client.py --url http://$B_IN:8300 \
    --model Qwen/Qwen2.5-1.5B-Instruct --prompts-file /root/b1/prereg3_prompts.json --logprobs 20 \
    --out /root/b1/warmup_baseline_r$k.json"
done
for k in 1 2 3; do
  ssh "${SSH_OPTS[@]}" "root@$A_PUB" "cd /root/b1 && ./venv/bin/python b1_client.py --url http://$B_IN:8300 \
    --model Qwen/Qwen2.5-1.5B-Instruct --prompts-file /root/b1/prereg3_prompts.json --logprobs 20 \
    --out /root/b1/arm_baseline_r$k.json"
done

# 4.5 /metrics 快照（含前缀缓存关闭证明；t0/t1 各一份）
ssh "${SSH_OPTS[@]}" "root@$B_PUB" "curl -s http://$B_IN:8300/metrics > /root/b1/metrics_baseline_t1.txt; \
  grep -o 'enable_prefix_caching=\"[^\"]*\"' /root/b1/metrics_baseline_t1.txt | sort -u"

# 4.6 停服务（按 PID，不用 pkill）
ssh "${SSH_OPTS[@]}" "root@$B_PUB" "bash /root/b1/b1_remote_serve.sh stop baseline"
```

## 5. 阶段二：拆分臂（P=A:8100 → KV → D=B:8200，代理=A:8192）

```bash
# 5.1 起两端 + 代理
ssh "${SSH_OPTS[@]}" "root@$A_PUB" "B1_BIND_IP=$A_IN B1_LOGPROBS_MODE=processed_logprobs \
  bash /root/b1/b1_remote_serve.sh start-prefill 8100 5600 /root/b1/venv/bin/vllm \
  Qwen/Qwen2.5-1.5B-Instruct Qwen/Qwen2.5-1.5B-Instruct $A_IN"
ssh "${SSH_OPTS[@]}" "root@$B_PUB" "B1_BIND_IP=$B_IN B1_LOGPROBS_MODE=processed_logprobs \
  bash /root/b1/b1_remote_serve.sh start-decode 8200 5600 /root/b1/venv/bin/vllm \
  Qwen/Qwen2.5-1.5B-Instruct Qwen/Qwen2.5-1.5B-Instruct $A_IN"
ssh "${SSH_OPTS[@]}" "root@$A_PUB" "bash /root/b1/b1_remote_serve.sh start-proxy 8192 $A_IN:8100 $B_IN:8200 \
  /root/b1/venv/bin/python /root/b1/out/toy_proxy_server.py"
# 注：NIXL 侧信道两端各用 5600（不同主机不冲突，B1 同款）；
#     若 P/D 握手失败，第一个要试的是 B1_UCX_NET_DEVICES=all（b1_remote_serve.sh 顶部注释）

# 5.2 等就绪（P 的 health、D 的 health、代理的 /healthcheck）
# 5.3 ★ C1 冒烟（经代理 A:8192；q01-q03）——判定同 4.3
ssh "${SSH_OPTS[@]}" "root@$A_PUB" "cd /root/b1 && ./venv/bin/python b1_client.py --url http://127.0.0.1:8192 \
  --model Qwen/Qwen2.5-1.5B-Instruct --prompts-file /root/b1/prereg3_prompts.json --logprobs 20 \
  --only-ids q01_rep6 q02_rep10 q03_rep14 --out /root/b1/smoke_disagg.json && \
  ./venv/bin/python b1_4c3_analyze.py smoke --file /root/b1/smoke_disagg.json"

# 5.4 预热 ×3 + 正式 ×3（同 4.4，--url 换 http://127.0.0.1:8192，输出名 warmup_disagg_r$k / arm_disagg_r$k）
# 5.5 /metrics 快照（两端都要：B 端 nixl 计数顺带留档，证明 KV 仍在正常传）
# 5.6 按 PID 停全部：prefill / decode / proxy
```

## 6. 拉回产物 + 本地分析

```bash
mkdir -p results/b1/b1_4c3_<UTC>
scp "${SSH_OPTS[@]}" "root@$A_PUB:/root/b1/{arm,warmup,smoke}_*.json*" results/b1/b1_4c3_<UTC>/
scp "${SSH_OPTS[@]}" "root@$B_PUB:/root/b1/metrics_*.txt" results/b1/b1_4c3_<UTC>/
.venv/Scripts/python.exe k8s_test/b1_4c3_analyze.py analyze \
  --run-dir results/b1/b1_4c3_<UTC> --out-dir results/b1/b1_4c3_<UTC>
```

产物按预注册 §9 清单核对：arm JSON、metrics 原文、settings、record、E4 表、汇总。

## 7. 收尾（跑完立即执行，别拖到明天）

1. 两台实例在控制台选**「节省停机模式」**停机（**不是**默认的"普通停机"——那个继续收钱）；
2. 从本机对两个公网 IP 的 22 端口各做 2 次 TCP 探测，**全部超时**才算停机确认；
3. 记录：起止 UTC、卡时（2 台 × 小时）、是否触线（≤4 h/台、≤16 卡时）。

## 8. 停止条件（预注册 §8 的执行版，触发就停）

| 触发 | 动作 |
|---|---|
| C1 冒烟 FAIL | **不采数**，直接进第 7 步收尾；结论写"该环境 processed 口径不成立" |
| 同臂 r2 vs r3 不逐位相同 | 先修测量，不解读 E1/E2/E4 |
| token_ids / 分词对不上 | 停，报告"无法对账" |
| 记录缺字段 / 新的静默错误信号 / 同一前置检查连败 3 次 | 按 B1 的 S6/S7/S8 办 |
| 单次连续 > 4 h/台 或 台账算不出时间 | 停（后者保守不放行） |

## 9. 本轮不做的事

不改 4c 判定（6/15、未通过）；不改 B1 记录；不测任何性能；不开新题组；
**不把本机预演的 D1/D2 偏离带上来**（云端显存 0.85、max-model-len 8192）。

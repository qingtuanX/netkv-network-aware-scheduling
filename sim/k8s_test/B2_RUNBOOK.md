# B2 执行手册与台账（B2_RUNBOOK.md）——**命令级写死，照抄执行**

> 性质：这不是新实验设计。它把 `B2_PREREG.md`（v2，已批准的设计）落实成
> **不会跑错的执行顺序**：每轮顺序、每格 `--reps 10`、每步留痕。
> 与协议冲突时以协议为准；本手册的命令参数与协议 §1–§4 逐项一致。
> 裁定记录：作者 2026-10-11——token ID 兜底禁止入正式结论（工具 v2.1 已改并
> 拦截测试通过）、执行台账本文件、服务端指标改称"汇总分布"（协议已改）。

## 0. 固定值（抄自 `run_prereg4_cloud.sh`，别改）

| 项 | 值 |
|---|---|
| A 机（前处理+代理+客户端） | 私网 `172.29.31.52`；公网 `<A_PUB>`（开机后填） |
| B 机（对照臂/decode） | 私网 `172.29.31.51`；公网 `<B_PUB>`（开机后填） |
| 模型 | `Qwen/Qwen2.5-1.5B-Instruct`（本地目录 `/root/b1/models/Qwen2.5-1.5B-Instruct`） |
| vLLM / python | `/root/b1/venv/bin/vllm`；`python3`（venv 在 `/root/b1/venv`） |
| 代理 | `/root/b1/official/toy_proxy_server.py` |
| SSH | `ssh -i ~/.ssh/b1_key.pem root@<A_PUB>`（参数照 `run_b1.sh` 顶部） |
| **两臂服务参数（完全一致）** | `--generation-config vllm --dtype bfloat16 --no-enable-prefix-caching`（**不设** `B1_LOGPROBS_MODE`，无任何概率记录设置） |

## 1. 四轮固定顺序（执行顺序表，不许改）

| 轮 | 先测形态 | 后测形态 | 每形态内档顺序 | 题序文件（种子） |
|---|---|---|---|---|
| 1 | base | disagg | **串行 → 并发** | `prompts_r1.json`（101） |
| 2 | disagg | base | **并发 → 串行** | `prompts_r2.json`（102） |
| 3 | disagg | base | **串行 → 并发** | `prompts_r3.json`（103） |
| 4 | base | disagg | **并发 → 串行** | `prompts_r4.json`（104） |

题序字面值（已冻结在 `sim/k8s_test/b2_prompt_orders/`，同轮两形态用**同一文件**）：

| 轮 | 题序 |
|---|---|
| 1 | q01_rep6 q04_rep18 q03_rep14 q02_rep10 q05_rep22 |
| 2 | q05_rep22 q04_rep18 q01_rep6 q03_rep14 q02_rep10 |
| 3 | q05_rep22 q01_rep6 q03_rep14 q02_rep10 q04_rep18 |
| 4 | q04_rep18 q05_rep22 q03_rep14 q02_rep10 q01_rep6 |

## 2. 开机前准备（本地，一次）

```bash
# ① 同步工具与题序到 A/B（sha256 三端一致才继续）
scp -i ~/.ssh/b1_key.pem sim/k8s_test/b2_perf_client.py root@<A_PUB>:/root/b1/b2/
scp -i ~/.ssh/b1_key.pem -r sim/k8s_test/b2_prompt_orders root@<A_PUB>:/root/b1/b2/
# ② 记录 sha256：本地 / A / B 三处 b2_perf_client.py 与 prompts_r*.json
# ③ 本机验收记录抄进台账：工具 v2.1 拦截测试 ✓（严格模式退出码 2）、
#    正常回归 ✓（logprobs_tokens、64/64、fallback=0）
```

## 3. 每个形态会话的固定九步

下面 `形态=base/disagg`、`轮=s`、`档序`按 §1 表。**九步顺序不许变**。
所有 client 命令在 **A 机**上执行（ssh 进 A 后跑）。

### 3.1 起服务（base 会话在 B；disagg 会话 A+B+代理）

```bash
# base：B 机（⚠️ B1_BIND_IP 必须给——服务绑私网地址，A 机才够得着；与 PREREG_4 同款。
#        2026-10-11 执行时实测：漏了它服务只绑 127.0.0.1，A 连 B_IN:8300 会被拒。）
ssh -i ~/.ssh/b1_key.pem root@<B_PUB> \
  'B1_BIND_IP=172.29.31.51 bash /root/b1/b1_remote_serve.sh start-baseline 8300 /root/b1/venv/bin/vllm /root/b1/models/Qwen2.5-1.5B-Instruct Qwen/Qwen2.5-1.5B-Instruct'

# disagg：A 先起前处理 → B 起 decode（⚠️ start-decode 末位必须是 B 自己的内网 IP）→ A 起代理
ssh -i ~/.ssh/b1_key.pem root@<A_PUB> \
  'B1_BIND_IP=172.29.31.52 bash /root/b1/b1_remote_serve.sh start-prefill 8100 5600 /root/b1/venv/bin/vllm /root/b1/models/Qwen2.5-1.5B-Instruct Qwen/Qwen2.5-1.5B-Instruct 172.29.31.52'
ssh -i ~/.ssh/b1_key.pem root@<B_PUB> \
  'B1_BIND_IP=172.29.31.51 bash /root/b1/b1_remote_serve.sh start-decode 8200 5600 /root/b1/venv/bin/vllm /root/b1/models/Qwen2.5-1.5B-Instruct Qwen/Qwen2.5-1.5B-Instruct 172.29.31.51'
ssh -i ~/.ssh/b1_key.pem root@<A_PUB> \
  'bash /root/b1/b1_remote_serve.sh start-proxy 8192 172.29.31.52:8100 172.29.31.51:8200 /root/b1/venv/bin/python /root/b1/official/toy_proxy_server.py'
```

### 3.2 健康检查（不过就停，不采数）

base：`curl http://127.0.0.1:8300/v1/models`（B 机）；
disagg：prefill `8100` + decode `8200` + 代理 `8192/healthcheck`（各自机上 curl）。

### 3.3 冒烟（1 条，不进统计）——退出码必须 0

```bash
# A 机执行；base 用 http://172.29.31.51:8300，disagg 用 http://127.0.0.1:8192
python3 /root/b1/b2/b2_perf_client.py --url <目标> --arm <形态> \
  --prompts-file /root/b1/b2/b2_prompt_orders/prompts_r<s>.json \
  --only-ids q01_rep6 --max-tokens 64 --reps 1 --concurrency 1 \
  --out /root/b1/b2/out/smoke_<形态>_s<s>.json
```
判定：退出码 0、64/64、`token_count_source=logprobs_tokens`。不过 ⇒ 停，不采该格。

### 3.4 热身（10 条，不进统计）

```bash
# 串行 5 条 + 并发 5 条（--reps 1 × 5 题）
python3 /root/b1/b2/b2_perf_client.py --url <目标> --arm <形态>_warm \
  --prompts-file /root/b1/b2/b2_prompt_orders/prompts_r<s>.json \
  --max-tokens 64 --reps 1 --concurrency 1 \
  --out /root/b1/b2/out/warm_<形态>_s<s>_c1.json
python3 /root/b1/b2/b2_perf_client.py --url <目标> --arm <形态>_warm \
  --prompts-file /root/b1/b2/b2_prompt_orders/prompts_r<s>.json \
  --max-tokens 64 --reps 1 --concurrency 4 \
  --out /root/b1/b2/out/warm_<形态>_s<s>_c4.json
```

### 3.5 /metrics t0 快照

```bash
# base：B 机 curl http://127.0.0.1:8300/metrics  > metrics_base_s<s>_t0.txt
# disagg：B 机 curl http://127.0.0.1:8200/metrics > metrics_disagg_s<s>_t0.txt
```

### 3.6 采数两格（**每格 --reps 10**，档序按 §1 轮表）

```bash
# 档=串行（--concurrency 1）；档=并发（--concurrency 4）。两行都必须带 --reps 10。
python3 /root/b1/b2/b2_perf_client.py --url <目标> --arm <形态> \
  --prompts-file /root/b1/b2/b2_prompt_orders/prompts_r<s>.json \
  --max-tokens 64 --reps 10 --concurrency 1 \
  --out /root/b1/b2/out/perf_<形态>_s<s>_c1.json
python3 /root/b1/b2/b2_perf_client.py --url <目标> --arm <形态> \
  --prompts-file /root/b1/b2/b2_prompt_orders/prompts_r<s>.json \
  --max-tokens 64 --reps 10 --concurrency 4 \
  --out /root/b1/b2/out/perf_<形态>_s<s>_c4.json
```
**每格判定（不满足即停，不继续采该格）**：退出码 0；`n_token_source_fallback=0`、
`n_token_source_invalid=0`（= 全部 `logprobs_tokens`）；`n_length_mismatch=0`。
退出码 2 = 严格模式拦截（token ID 缺失）⇒ **该格作废、先检查、不补跑同格**。

### 3.7 /metrics t1 快照（同 3.5，文件名 `_t1`）

### 3.8 停服务

```bash
# base 会话：ssh <B_PUB> 'bash /root/b1/b1_remote_serve.sh stop baseline'
# disagg 会话：A 与 B 各 'bash /root/b1/b1_remote_serve.sh stop-all'
```

### 3.9 台账记一行（见 §5 表）：起止时刻 / 冒烟 / 预热 / 采数文件 / token 校验 / 异常

## 4. KV 账目自洽（每轮 disagg 会话后）

`nixl_bytes_transferred_sum 增量 ÷ prompt_tokens_by_source{external_kv_transfer} 增量`
应 ≈ **28,796 B/token**（Qwen2.5-1.5B KV 大小 28,672 + 描述符开销）；
`nixl_num_failed_*` 增量应为 0；base 臂 nixl 增量应为 0。

## 5. 执行台账（每会话一行，边跑边填）

| 轮 | 形态 | 档序 | 题序文件 | 起 | 止 | 冒烟 | 预热 | 采数文件 | fallback/invalid | 异常 |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | base | 串→并 | r1 | 08:17 | 08:22 | ✓ | ✓ | perf_base_s1_c1/c4 | 0/0 | 首次冒烟漏 B1_BIND_IP，修正后过 |
| 1 | disagg | 串→并 | r1 | 08:23 | 08:31 | ✓ | ✓ | perf_disagg_s1_c1/c4 | 0/0 | — |
| 2 | disagg | 并→串 | r2 | 08:32 | 08:39 | ✓ | ✓ | perf_disagg_s2_c4/c1 | 0/0 | — |
| 2 | base | 并→串 | r2 | 08:40 | 08:46 | ✓ | ✓ | perf_base_s2_c4/c1 | 0/0 | — |
| 3 | disagg | 串→并 | r3 | 08:47 | 08:52 | ✓ | ✓ | perf_disagg_s3_c1/c4 | 0/0 | ssh 断 1 次，未产生测量，重试 |
| 3 | base | 串→并 | r3 | 08:53 | 09:03 | ✓ | ✓ | perf_base_s3_c1/c4 | 0/0 | ssh 断 2 次，均未产生测量，重试 |
| 4 | base | 并→串 | r4 | 09:15 | 09:20 | ✓ | ✓ | perf_base_s4_c4/c1 | 0/0 | **首轮误路由到 B（套接字 bug）→ 整轮 R4 重跑**；误路由产物归档 *.misrouted.json |
| 4 | disagg | 并→串 | r4 | 09:21 | 09:28 | ✓ | ✓ | perf_disagg_s4_c4/c1 | 0/0 | 同上（重跑）；toy_proxy 用本地副本恢复（sha256 1210e552…） |

> 时区 UTC（约数）；fakedown/invalid 指正式采数的 `n_token_source_fallback` / `n_token_source_invalid`，全程为 0。

## 6. 收工

1. `stop-all` A、B 两台（确认无 vllm 进程）；
2. 拉产物到 `sim/results/b2/<run_id>/`（out/ + metrics + 台账）；
3. 写 `decision.json`（协议 §7 七条逐条勾选 + §0 结论；**手工誊写工具输出**，
   不写新分析代码）；
4. **控制台节省停机**两台（普通停机继续收计算费；公网 IP 会变）；
5. 费用记账：开机/停机时刻 × 控制台单价。

## 7. 中断与止损（协议 §6 的执行版）

- 单请求失败**不重试**，计入失败率（性能采数的规矩，与正确性采数刻意不同）。
- 退出码 2（token ID 缺失）⇒ 该格作废、先检查；**不**降级用块数继续。
- ssh/机器中断 ⇒ 该会话标 `infra_abort`、整轮重跑（含两形态，保住配对），
  原始产物保留。
- 预算不够按协议砍：轮 4→2 → 重复 10→5 → 砍并发档；manifest 记录砍了什么。

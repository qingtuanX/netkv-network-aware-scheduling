# C 阶段执行手册（C_RUNBOOK.md）——命令级写死，照抄执行

> 性质：`C_PREREG.md`（冻结）的执行展开，不是新设计。冲突时以协议为准。
> 教训来源：B2 的 `B2_RUNBOOK.md`（命令写死 + 台账逐格填 + 每主机独立 SSH 套接字）。
> **铁律**：`/tmp/cma` 只配 A、`/tmp/cmb` 只配 B、`/tmp/cmt` 只配 T（ControlPath 含主机
> 区分，2026-10-11 B2 的误路由 bug 就是没做这条）。

## 0. 固定值

| 项 | 值 |
|---|---|
| A（阿里云，前处理/客户端 D1） | 公网 8.155.133.216 / wg **10.10.10.1** / 私网 172.29.31.52 |
| B（阿里云，生成 D2） | 公网 8.155.131.183 / wg **10.10.10.2** / 私网 172.29.31.51 |
| T（腾讯云，生成 D1 / 前处理+客户端 D2） | 公网 1.14.249.136 / wg **10.10.10.3**；登录 `ubuntu@` + sudo |
| 密钥 | A/B：`~/.ssh/b1_key.pem`；T：`~/.ssh/tencent_key.pem`；搬迁互信：`migrate_key` |
| 服务参数 | 与 B2 完全一致（`--generation-config vllm --dtype bfloat16 --no-enable-prefix-caching`，无概率设置） |
| **C 的地址约定** | **服务与 nixl 一律用 wg 地址**（10.10.10.x）：`start-prefill`/`start-decode` 末位各自传 **自己的 wg IP**（nixl 对端靠它回连）；**并加 `B1_UCX_NET_DEVICES=wg0`**（否则 UCX 把内网地址通告给对端，跨云不可达 ⇒ `NIXL_ERR_BACKEND`，D1 冒烟实测踩过） |
| 客户端 | D1 在 A、D2 在 T（协议 §4b：两方向客户端位置不同，进解释边界） |

## 1. 每个测量会话的固定九步（同 B2 纪律）

**D1（阿里→腾讯）**：prefill=A:8100（wg 10.10.10.1）+ decode=T:8200（wg 10.10.10.3）+ proxy=A:8192
**D2（腾讯→阿里）**：prefill=T:8100（wg 10.10.10.3）+ decode=B:8200（wg 10.10.10.2）+ proxy=T:8192

```bash
# ① 起服务（示例 = D1；末位是"本机自己的 wg IP"）
ssh A 'B1_BIND_IP=10.10.10.1 bash /root/b1/b1_remote_serve.sh start-prefill 8100 5600 /root/b1/venv/bin/vllm /root/b1/models/Qwen2.5-1.5B-Instruct Qwen/Qwen2.5-1.5B-Instruct 10.10.10.1'
ssh T 'sudo -n bash -c "B1_BIND_IP=10.10.10.3 bash /root/b1/b1_remote_serve.sh start-decode 8200 5600 /root/b1/venv/bin/vllm /root/b1/models/Qwen2.5-1.5B-Instruct Qwen/Qwen2.5-1.5B-Instruct 10.10.10.3"'
ssh A 'bash /root/b1/b1_remote_serve.sh start-proxy 8192 10.10.10.1:8100 10.10.10.3:8200 /root/b1/venv/bin/python /root/b1/official/toy_proxy_server.py'

# ② 健康检查（prefill 8100 / decode 8200 / proxy 8192/healthcheck）
# ③ 冒烟 1 条（--only-ids q01_rep6 --reps 1，退出码必须 0）
# ④ 热身 10 条（--reps 1 串行 + --reps 1 并发）
# ⑤ /metrics t0 快照（decode 端）
# ⑥ 采数两格（每格 --reps 10，档序按轮表）：
#    D1/D2 各：--concurrency 1 一格 + --concurrency 4 一格，--prompts-file prompts_r1.json
#    （协议 §4：两方向同用 r1，同序可比；每方向 100 条正式）
# ⑦ /metrics t1 快照
# ⑧ 停服务（A/T 或 B/T 各 stop-all）
# ⑨ 台账记一行（§3 表）
```

客户端命令模板（在**前处理机**上执行）：

```bash
python3 /root/b1/b2/b2_perf_client.py --url http://127.0.0.1:8192 --arm <d1|d2> \
  --prompts-file /root/b1/b2/b2_prompt_orders/prompts_r1.json \
  --max-tokens 64 --reps 10 --concurrency 1 --out /root/b1/out/perf_<d1|d2>_c1.json
# 并发档同上 --concurrency 4 --out ..._c4.json
```

**每格判定**：退出码 0；`n_token_source_fallback=0`、`n_token_source_invalid=0`、
`n_length_mismatch=0`；退出码 2（严格拦截）⇒ 该格作废、先检查、不补采同格。

## 2. Q1 KV 账目（每方向会话后立即核）

decode 端 `/metrics` t0→t1（协议 §4 冻结判据）：
① 字节÷token ∈ **28,799 ±1%**；② 传输次数 = 有效请求数（100）；
③ 失败传输/失败通知/KV 过期全 0；④ `local_compute` = 0。

## 3. 执行台账（边跑边填）

| 方向 | 会话 | 档序 | 起 | 止 | 冒烟 | 热身 | 采数文件 | fallback/invalid | KV 账目 | 异常 |
|---|---|---|---|---|---|---|---|---|---|---|
| D1 阿→腾 | 1 | 串→并 | | | | | perf_d1_c1/c4 | / | | |
| D2 腾→阿 | 1 | 并→串 | | | | | perf_d2_c4/c1 | / | | |

（协议 §4：每方向 1 个测量会话；档序 D1 串→并、D2 并→串，反平衡。）

## 4. 收工

拉产物 → `sim/results/c/<run_id>/` → `decision.json`（Q1/Q2/Q3 各 通过/失败/未测 +
费用对账：腾讯 ¥10.90/时×时长 + 流量 + 阿里云带宽调整差价若有）→ **三台全部节省停机**。

## 5. 中断与止损

- 单请求失败不重试（计入失败率）；退出码 2 ⇒ 该格作废先查；
- ssh 断连 ⇒ "无 target= 未产生测量 ⇒ 重试 ≤3；有测量绝不重试"；
- NIXL 端口收不进隧道 / KV 传输不通 ⇒ **Q1 记"失败"即为合法负结果**，停，不硬凑。

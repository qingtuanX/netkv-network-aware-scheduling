# 交接说明（关机重启前写的，重开后从这里继续）

> 写入时间：**2026-09-23 01:30 本地**（UTC+8）。机器即将关机重启，
> 三批 v2 矩阵正在运行，会被中断。本文记录"哪些已完成、哪些要重跑、怎么重跑"。

## 0. 一句话状态

- **v1 证据链：完整且已过全部门**（三批 × 9 mode，`results/matrix{,_trace,_burstgpt}/`，
  验证记录在 `results/verification/`）。它是"链路不排队"模型的产物，按 append-only 保留，
  **结论只作为 v1 模型下的结果**，不与 v2 混引。
- **v2 证据链：跑到约七成，需要补 3–4 个 mode**（下表）。v2 是"链路 FIFO 排队 +
  争用感知策略 NetworkAware2"的模型，是冲 B会/SCI 2区 的主线。

## 1. 重启后要做的第一件事：补跑 v2 缺失的 mode

三批 v2 的产物目录：`results/matrix_v2/`、`results/matrix_trace_v2/`、`results/matrix_burstgpt_v2/`
（v2 要求 `--net-set v2`；`results/matrix_v2` 与旧目录并列，互不覆盖）。

**已有完整 manifest 的 mode（不用重跑）**：

| 批次 | 已完整 |
|---|---|
| 合成 | convergence / scale / ablation / datavolume / fallback / bandwidth / rho / staleness |
| Azure | convergence / scale / ablation / datavolume / fallback |
| BurstGPT | convergence / scale / ablation / datavolume / fallback / bandwidth / rho |

**需要重跑（本次被中断，无 manifest、`p_holm` 缺失）**：

| 批次 | 要重跑 |
|---|---|
| 合成 | `main`（只写了 220/600 行） |
| Azure | `bandwidth`（185/300）、`rho`、`staleness`、`main` |
| BurstGPT | `staleness`（405/480）、`main` |

重跑命令（`sim/` 目录下；**用无超时模式**，600 s 默认超时会截断矩阵）：

```bash
# 合成：只差 main
.venv/Scripts/python.exe experiments/run_matrix.py --mode main     --source synthetic --net-set v2 --out results/matrix_v2
# Azure：4 个 mode
for m in bandwidth rho staleness main; do
  .venv/Scripts/python.exe experiments/run_matrix.py --mode $m --source trace --net-set v2 \
    --trace data/raw/azure_code_1week.csv --out results/matrix_trace_v2
done
# BurstGPT：2 个 mode
for m in staleness main; do
  .venv/Scripts/python.exe experiments/run_matrix.py --mode $m --source trace --net-set v2 \
    --trace data/raw/burstgpt_1.csv --out results/matrix_burstgpt_v2
done
```

跑完（每个 mode 都有 `_manifest.json`）再分析：

```bash
for p in "synthetic results/matrix_v2 results/analysis_v2 figures_v2" \
         "azure results/matrix_trace_v2 results/analysis_trace_v2 figures_trace_v2" \
         "burstgpt results/matrix_burstgpt_v2 results/analysis_burstgpt_v2 figures_burstgpt_v2"; do
  set -- $p
  .venv/Scripts/python.exe analysis/analyze.py --matrix-dir $2 --table-dir $3 --fig-dir $4
done
```

⚠️ 中途被中断的 run（无 manifest）**不要删**：analyze 只取每个 mode 最新的 run_id，
它们不会影响结论，但按项目约定要留作证据（异常运行只标注原因）。

## 2. v2 的关键设计事实（继续工作必须知道）

1. **链路争用模型（V2-1）**：`src/simulator.py` 里每个节点的链路是一条
   `simpy.Resource(capacity=1)` FIFO 管道；`serve()` 的进出两段传输都要先占管道，
   传输时间 = 字节数 / **开始传输那一刻**的真实带宽；`link_pending[node]` 记录链路积压字节，
   通过 `NodeStateHistory` 的第三个字段进入观测（走**队列通道**的陈旧）。
   【一阶简化】一条管道兼管进出两个方向、排队规则是 FIFO 而非处理器共享。
2. **争用感知策略（V2-2，机制贡献）**：`NetworkAware2`，评分 =
   `算力backlog + w_net × (link_drain_time + t_in + t_out)`，
   其中 `link_drain_time = 链路积压字节 / 带宽`（与算力 backlog 对称）。
   与 v1 的 `NetworkAware` **只差 `link_drain_time` 一项**，所以增益可干净归因。
3. **网络档位（v2）**：`NETWORK_CONDITIONS_V2` = 400/100/25 Gbps（快/中/慢，1:4:16），
   时延 1/1.5/2 µs；`--net-set v2` 启用。**v1 的 0.1–10 Gbps 在 v2 模型下不可用**
   （链路直接变成容量瓶颈，p95 冲到 76 万秒）。
4. **到达率标定（门 1 的 v2 形式，重要）**：按**多资源可行域**标定，不能用聚合容量——
   `R = Σ_i min(算力_i / E[工作量], 带宽_i / E[字节数])`，`λ = rho_target × R`。
   实现在 `run_matrix.calibrate_target_rate()`，两个 `build_*_cell_workload()` 通过
   `node_compute` / `node_bandwidth` 参数接收每节点容量。实测 `load_ratio = λ/R = 0.600` 精确成立。
   **曾经的错误**：用聚合容量标定，19 个组合里 5 个 `load_ratio` 达 1.0–2.46（队列无限增长、
   p95 只是有限时长假象）——那批产物已作废，不要把 `20260922T16xxxxZ` 之前
   `results/matrix_v2` 里的 run 当结果用（它们无 manifest 的居多，但 `scale/ablation/…` 有
   两套 run，**只用最新那套**）。
5. **新增的分析指标（`analyze.py` §0b）**：`rho_compute`、`rho_network`（聚合）、
   `rho_slow_link`（λ×E[字节]/最慢链路带宽，实测最高 13.8，说明热点链路被打满）、
   `load_ratio`（可行域判据，必须 ≤ 0.9）。判据：`max(rho_compute, rho_network) ≤ 0.9` 且 `load_ratio ≤ 0.9`。

## 3. v2 已验证的门（重启后不必重验，但改代码后必须重验）

| 门 | 结果 |
|---|---|
| 门 1（两资源 + 可行域） | `load_ratio = 0.600`，rho_compute 0.600、rho_network 0.26–0.33 |
| 门 2（消融严格子集） | `NetworkAware(w=0)` 与 `NetworkAware2(w=0)` **都与 LeastLoad 逐位相同 ✓**（degraded/unstable/normal 三个网络） |
| 门 3 | 同种子两次运行 `(node_id, finish)` 逐位相同 ✓ |
| 门 5 | 回退触发语义与观测年龄一致；`fresh` 档 Δp95 精确为 0 ✓ |
| 白盒 | 单请求 `finish = arrival + net_in + service + net_out`，绝对差 **0.000e+00** ✓ |

**机制方向（修正前那批的初步观察，幅度待重测）**：`scale` mode、p95（秒）——
`degraded` 4 节点：LeastLoad 320.4 / LeastQueue 146.4 / NetworkAware(v1) 153.5 /
**NetworkAware2 58.6**；8 节点：51.8 / 26.2 / 40.2 / **15.5**；16 节点：31.4 / 14.2 / 22.5 / **8.1**。
→ **只看链路快慢（v1）反而比纯队列感知更差；把链路队列一起看才在每档都最优。**
这条要在修正后的数据上重新确认。

## 4. v2 还没实现的（`论文规划.md` §6.6 的剩余项）

- **V2-3 观测噪声**：在"陈旧(lag)"之外增加**估计噪声**通道（对观测到的时延/带宽加乘性噪声），
  作为独立因子；要能证明两因子可分离。
- **V2-5 分层拓扑**：机架内快链路 + 跨机架**共享**慢上行（节点间链路不再独立）；
  实现方式：在现有 per-node 管道之外再加一条 rack 级共享管道，传输同时占用两者。
- **V2-6 强基线**：Power-of-two-choices(least-load) 与 Sia 风格异构感知基线，
  建议放在**独立 mode**（例如 `--mode baselines`），避免改动已在跑的 main 策略表。
- **V2-8 回退重设计**：把触发条件从"只看年龄"改为"年龄 **且** 观测到链路抖动"
  （策略自己记住上一次观测，检测到相对变化超过阈值才算 volatile）。
  这样静态网络下永不触发——正是 v1 回退失效的根因（v1 实测 `degraded` 档触发时
  p95 抬升 +30%~+45%）。

启动前先读 `AGENTS.md` §6（门 1–5 与保真度对照表）与 §10（已踩过的坑，共 33 条），
以及 `.kimi-code/skills/research-experiment/SKILL.md`。

## 5.【2026-09-23 02:00 更新】v2 门检查抓到的三个问题与处置

用 `verify_gates.py --matrix-dir results/matrix_v2 --source synthetic --net-set v2
--label synthetic_v2 --out results/analysis_v2` 跑 v2 合成批：**8/11 通过**。

1. **门 1 有 210 项越界，全部来自 `bandwidth` 模式**（`rho_compute` 最低 0.0023、
   `load_ratio_max` 0.964）。原因：`BANDWIDTH_PROFILES` 是 v1 时代为"链路不排队"设计的
   （0.01–800 Gbps）；在 v2 的可行域标定下，慢档的可持续到达率极小，λ 随之极小，
   **算力利用率掉到 0.0023，违反门 1 下界**。
   本质结论：kv_cache 的 ~900 MB/请求下，慢链路 < ~23 Gbps 时"两条资源都达到 rho≥0.3"不可行。
   **处置**：给 v2 单独定义落在可行带内的档位（慢端 25/50/100/200 Gbps，保持 1:4:16 形状），
   `NET_SET=="v2"` 时启用；改完只需重跑三批的 `bandwidth`（每批约 5 分钟）。
2. **白盒时间分解差 1.0e-2**：`src/simulator.py` 的 v2 `serve()` 把传输时间写成
   `字节数 / 带宽`，**漏掉了链路单程时延**（v1 用 `network_time()` = 时延 + 字节/带宽）。
   影响量级：v2 时延 1–2 µs vs 传输 18–292 ms → 相对误差 < 1e-4，对在跑的批次**无实质影响**，
   但这是 bug，必须修（改用 `network_time()`，在"真正开始传输那一刻"求值）。
   修完重跑白盒检查；是否重跑三批由用户决定（按 1e-4 可只在文档声明，严格无缝则重跑 ~90 分钟）。
3. **黑盒 p95 单调性**：检查脚本自身没传每节点容量 → 退回 v1 聚合标定 → 链路被压爆
   （p95 7.4 万秒、非单调）。**已修**（verify_gates.py 现在传 node_compute/node_bandwidth），
   修后需重跑该项。

**共同教训**：v2 的链路争用模型让一切"聚合容量"口径失效——标定与检查代码都必须拿到
**每节点**的算力与带宽。

**修 1、2 的前置条件**：必须等 Azure（4 个 mode，约 03:00 前）与 BurstGPT（staleness+main）
两条链**全部结束**再改 `src/*` 与 `run_matrix.py`，否则会让批次内部混版本。

---

## 6.【2026-09-23 02:55 更新】两个待修项已修，三批 v2 正在用同一版代码重跑

1. **时延项已修**：`src/simulator.py` 的 `serve()` 现在用
   `network_time(..., trajectory.state(node, env.now))`（= 单程时延 + 字节/带宽），
   不再是裸的 `字节/带宽`。白盒门此前报告的差值 **4.0e-6 s 恰好等于 2×单程时延**，
   精确印证了这个诊断。
2. **v2 专用带宽档位已加**：`BANDWIDTH_PROFILES_V2` = 慢端 25/50/100/200 Gbps
   （比例保持与 v2 默认 degraded 一致），`NET_SET=="v2"` 时由
   `active_bandwidth_profiles()` 自动切换。v1 那套 0.01–800 Gbps 保留给 v1 批次。
   根因是硬约束：~900 MB/请求下慢链路 < ~23 Gbps 时"两条资源都达到 rho≥0.3"不可行。
3. **为了"同一批数字来自同一版代码"，三批 v2 全部 9 个 mode 正在重跑**
   （旧 run 按 append-only 保留，analyze 只取最新 run_id）。
   重跑完成后要做：
   ```bash
   # 三批分析与门记录（应达到 11/11）
   .venv/Scripts/python.exe analysis/analyze.py --matrix-dir results/matrix_v2 \
       --table-dir results/analysis_v2 --fig-dir figures_v2
   .venv/Scripts/python.exe analysis/verify_gates.py --matrix-dir results/matrix_v2 \
       --source synthetic --net-set v2 --label synthetic_v2 --out results/analysis_v2
   # Azure / BurstGPT 同理（--trace 分别指 azure_code_1week.csv / burstgpt_1.csv）
   # 最后做一次干净的备份
   .venv/Scripts/python.exe backup_artifacts.py
   ```

## 7. 实验数据保留（已完成）

- 备份在 **`D:/论文实验数据备份_20260923/`**（另一块盘），含 `results/` + `figures*/` +
  `data/raw/`（两份真实 trace）+ **代码快照**（`src`/`experiments`/`analysis`/`configs`/入口脚本）+ 关键文档。
- 清单 `sim/results/ARTIFACTS.csv`（537 个文件 / 817 MB，含 sha256），逐个文件校验过。
- 可重复：`sim/.venv/Scripts/python.exe backup_artifacts.py`（`--verify-only` 只校验）。
- **为什么连代码一起备份**：仓库零提交、`code_version` 一直是 `uncommitted`，
  只存 CSV 等于存了数字丢了程序。

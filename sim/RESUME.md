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

---

## 8.【2026-09-23 04:40 更新】门 1 判据两处修正 + 三批 v2 状态

### 8.1 门 1 判据与 §0b 对齐（此前 analyze 说"全部有效"、gates 说"越界"，自相矛盾）

`load_ratio = 实际到达率 / R`，`R = Σ_i min(算力_i×槽位/E[work], 带宽_i/E[bytes])`。
本次修了两个**独立的**错误，两处都必须逐行算、再取组内极值：

1. **带宽档位格用错了链路**。`bandwidth` 模式的链路是**每 cell 的档位**（`net_profile`），
   而 manifest 的 `network_conditions` 只存三个基础档。拿基础档去复算 `slow_200g`，
   `load_ratio` 会算成 **1.167**（按它自己的档位是 **0.600**）——正常格子被误报成"不可持续"。
   现在 `analyze.profile_links_table()` 与 `verify_gates` 都按档位表解析；
   **解析不到就留 NaN，绝不用错的链路顶上**。
2. **可行性不能在"分组均值"上算**。`R` 对 E[work]/E[字节数] 是非线性的（每节点取 min 再求和），
   而一个分组可能跨 3 个 trace 时间片（BurstGPT data_heavy：各片 R = 33.7 / 72.9 / 155.8，差 4 倍）。
   按组均值算出来的 `load_ratio` 是 target 的 **1.4535 倍**（两个网络下都精确等于这个数），
   于是 BurstGPT 批有 **6 个完全正常的格子被误报**成"队列无限增长、p95 不可采信"。
   逐行算之后：**0/92 组合 >1，范围 [0.30, 0.90]** 正好落在扫描网格上。

另外 `rho_slow_link` 的口径写清楚了：它是**最坏放置上界**（把全部流量压到一条最慢链路），
不是任何策略的实际链路负载；n 节点机群上它比实际值松 n/链路档数倍。此前按"热点链路已被打满"
报告，是把上界当事实。

### 8.2 `code_version` 一直是 `uncommitted` 的真正原因（第二次修）

`_git()` 里 `safe.directory` 写的是 `ROOT`，而 **`ROOT` 是 `sim/`，仓库根是它的上一级**——
git 依旧判定"该仓库不在安全清单里"而拒绝，`code_version()` 静默回落成 `"uncommitted"`。
现在 `_safe_dirs()` 从 `ROOT` 向上走到 `.git` 所在目录，两级都放行（重复 `-c` 对多值键是追加，
已手工验证）。修后实测 `code_version = a9acad1`、`code_dirty_files` 能列出改动。

**因此本批的 `code_version` 是不齐的**：04:12 之前启动的 mode 记 `uncommitted`，
之后启动的记 `a9acad1`（且列出未提交文件）。这是**真实记录**，不是 bug。
可以证明"仿真数学没混版本"：`git status` 里 `sim/src/` **零改动**，
改动只在 `analysis/analyze.py`、`analysis/verify_gates.py`、`experiments/run_matrix.py`
（后者只有 manifest 的版本记录函数）——即分析与记账层，不是仿真层。
`analyze.py` §0 现在把 `code_version` / 是否脏一起打进 `data_availability.csv`。

### 8.3 三批 v2 状态（截至 04:40）

| 批次 | 状态 |
|---|---|
| `results/matrix_v2`（合成） | ✅ 9 个 mode 全完整；`verify_gates --batch synthetic_v2` = **11/11 ✓**；analyze 已重跑 |
| `results/matrix_burstgpt_v2` | ✅ 9 个 mode 全完整（04:21 收尾）；**11/11 ✓**；analyze 已重跑 |
| `results/matrix_trace_v2`（Azure） | ✅ 9 个 mode 全完整（05:32 收尾，2856 次仿真）；**11/11 ✓**；analyze 已重跑 |

三批齐了之后做的事（已全部完成）：

```bash
.venv/Scripts/python.exe analysis/analyze.py --matrix-dir results/matrix_trace_v2 \
    --table-dir results/analysis_trace_v2 --fig-dir figures_trace_v2
.venv/Scripts/python.exe analysis/verify_gates.py --batch azure_v2
.venv/Scripts/python.exe backup_artifacts.py          # 684 文件 / 827 MB，逐个哈希"全部一致 ✓"
```

**三批 v2 的门记录**：`synthetic_v2_gates.csv`、`azure_v2_gates.csv`、`burstgpt_v2_gates.csv`，均为 **11/11**。

**机制结果（三批一致的主结论）** —— Δp95 为负表示前者更优：

| 对比 | 合成 | Azure | BurstGPT |
|---|---|---|---|
| NA2 vs LeastLoad（normal） | −0.1% | −0.8% | +4.0% |
| NA2 vs LeastLoad（**degraded**） | **−59.6%** | **−54.2%** | **−40.7%** |
| NA2 vs LeastLoad（unstable） | −9.0% | −19.9% | **+18.7%** |
| NA(v1) vs NA2（normal） | +0.1% | +1.1% | −0.6% |
| NA(v1) vs NA2（**degraded**） | **+154.6%** | **+120.5%** | **+103.9%** |
| NA(v1) vs NA2（unstable） | +17.4% | +32.0% | −0.3% |

读法：**只有在慢链路真的被压住（degraded）时，把链路队列放进决策才有大收益；
"只看链路快慢不看排队"（v1 的 NetworkAware）在争用下反而是有害的（+104%~+155%）**；
时变网络（unstable）三个批次方向不一致——这是必须原样报告的**失效区间**，不是噪声。

**关于 `code_version` 不齐**：`staleness` 与 `main` 两个 mode 记录为 `a9acad1`，
其余 7 个 mode 记录为 `uncommitted`（原因见 §8.2）。`sim/src/` 无改动 → 仿真数学同一版。

## 9.【2026-09-23 04:40】真实系统实测（容器模式）：万事俱备，只差一次重启

**当前状态：容器模式一次都没跑过；原因只有 `VirtualMachinePlatform` 处于"启用待定"，
必须重启 Windows 才生效**（`wsl --status` 明确报"未启用虚拟化"）。

已探明的网络事实（决定了技术路线）：`registry-1.docker.io` 与 `auth.docker.io` **不通** →
任何"从 Docker Hub 拉镜像"的路线都不成立；`download.docker.com` 不通 → Docker Desktop 装不上；
`rancher-mirror.rancher.cn/k3s/...` 通 → k3s 从中国镜像装；
`archive.ubuntu.com` / `mirrors.aliyun.com` 通 → 用 apt + debootstrap 就地造镜像。

据此写好的三份产物（**都还没执行过**）：

| 文件 | 作用 |
|---|---|
| `k8s_test/setup_wsl_k3s.sh` | 在 WSL Ubuntu 里装 k3s + 用 debootstrap/podman **就地构建** `simtest/base:1` + 导进 containerd + 写 config.yaml（禁用 traefik/servicelb/metrics-server/local-storage/coredns，`pause-image` 指向自建镜像）+ 起服务 + 出环境能力报告 |
| `k8s_test/k8s/workers.yaml` | 3 个 worker Pod：算力 4/2/1、带宽 100/500/1000 Mbps（与仿真的"快算力+慢链路 ↔ 慢算力+快链路"同构）；`imagePullPolicy: Never` |
| `k8s_test/run_k3s_grid.sh` | 取 Pod IP → 到达率阶梯（门 1 的实测版）→ 主网格 `{least_load, contention} × {新鲜, 陈旧 3s} × {0.5MB, 8MB}` → 汇总 |
| `k8s_test/summarize_real.py` | 每请求 CSV → `summary.csv` / `comparison.csv`（p50/p95/p99、net_share、各 worker 利用率与请求占比），**只用标准库**（要在 WSL 系统 python3 上跑） |

**已跑通的部分（本机多进程，不启容器）**：复现了仿真的边界——小传输（net_share≈0.01）
`least_load` p95 0.28 s 优于 `contention` 0.84 s（网络项误导）；大传输（net_share≈0.88）
`contention` 0.74 s vs `least_load` 2.07 s（**−64%**）。

**重启后的一次性动作**（详见 `k8s_test/README.md` §3）：

```bash
wsl --install -d Ubuntu --no-launch && wsl --shutdown     # Windows 侧
wsl -d Ubuntu -u root -- bash /mnt/e/代码项目文件/论文/sim/k8s_test/setup_wsl_k3s.sh
wsl -d Ubuntu -u root -- bash /mnt/e/代码项目文件/论文/sim/k8s_test/run_k3s_grid.sh
```

**重启前必须先做完的事**：Azure 批的 `rho/staleness/main` 三个 mode
（重启会杀掉后台链，中断的 mode 无 manifest、`p_holm` 缺失，只能重跑）。

## 9b.【2026-09-23 13:35】真实系统实测：k3s 装成 + 首轮网格跑完

WSL2（Ubuntu 26.04，内核 6.18.33.2-microsoft-standard-WSL2，cgroup v2，systemd 已启用，
16 逻辑 CPU / 7.4 GiB）里装了单节点 **k3s v1.31.5+k3s1**（containerd 1.7.23-k3s2）。
镜像 `docker.io/simtest/base:1` 是**就地**造的（debootstrap + podman import，本机 Docker Hub 不通）；
3 个 worker Pod 参数各异（算力 4/2/1、带宽 100/500/1000 Mbps），客户端在 WSL 里用 Pod IP 直连。

**装这一步失败 5 次**（URL 编码、静默退出、archive 覆盖、镜像名全限定、ENV PATH、
systemd 单元、导入时机、`-x` 判据），逐条记在 `k8s_test/README.md` §6。
**测量工具本身又踩了 2 个坑**（同步 client 压不出排队、`_aged_snapshot` 不刷新历史
导致 lag>0 时 240/240 请求全打一个节点），逐条记在同一个 README 的 §7。

**门 1（实测版）**：用 `rho_cpu = 实测到达率 × 实测 CPU 服务时间 / 机群 CPU 配额`；
qps 4/6/8 → rho_cpu 0.37/0.50/0.68（有效区间内），qps 12 → 1.03（越界，只作阶梯边界）。

**首轮主网格结果**（`contention` = 算力+链路积压，`least_load` = 只看算力队列）：

| 数据量（net_share） | qps=6 lag0 | qps=6 lag3 | qps=8 lag0 | qps=8 lag3 |
|---|---|---|---|---|
| 38.9 MB（**0.65**） | **−45.2%** | −21.4% | **−37.5%** | −27.9% |
| 9.7 MB（0.32–0.40） | −11.0% | +47.5% | +5.5% | +10.2% |
| 0.6 MB（0.01–0.05） | −1.6% | +68.7% | +23.1% | −16.3% |

→ 与仿真的收益边界**同向**：网络占比大时 4/4 全胜（−21%～−45%），占比小时无效甚至反向；
状态陈旧 3 s 让收益收窄（−45%→−21%）。**每格只有 1 次运行、无置信区间**，
所以只能支持方向与量级，写正文必须连这句一起写（README §5 有完整三句声明）。

## 10. 重启前/后还要收尾的清单

1. ⏳ 等 Azure 批跑完 → `analyze.py`（`results/analysis_trace_v2`）+ `verify_gates --batch azure_v2`
2. ⏳ 干净备份：`backup_artifacts.py`（此时矩阵全部收尾，应"全部一致 ✓"）
3. ⏳ 第二次 git 提交（`_safe_dirs` 修复 + 门 1 判据修正 + §0 的 code_version 列 + `k8s_test/` 全套）
4. ⏳ 重启 → 按 §9 装 WSL/k3s → 跑真实集群网格
5. ⏳ `论文规划.md` §6.6 的剩余项：**V2-3** 观测噪声通道、**V2-5** 机架共享上行、
   **V2-6** P2C / Sia 风格强基线（建议独立 mode `baselines`）、**V2-8** 回退改"年龄 × 抖动"触发


---

## 11.【2026-10-06 12:00 更新】真实跨云阶段 A（两真机、跨公网）——关机交接

> **这次是新方向的第一站**：研究目标已从"仿真里的任务派发"改为"真实跨云 LLM 服务"
> （见 `真实跨云LLM服务研究计划书.md`）。阶段 A 是它规定的**付费租 GPU 之前的硬门槛**。

### 11.1 已经做完的

| 事 | 状态 | 产物 |
|---|---|---|
| 本机隔离网络阶段 A（单机造两台主机） | ✅ 四条件全过 | `sim/k8s_test/STAGE_A.md`（已定稿，勿改） |
| **公网两台真机阶段 A** | ⚠️ 部分通过（见下） | `sim/results/stage_a_cloud/`、`sim/k8s_test/STAGE_A_CLOUD.md`（用户已定稿，勿改） |
| 上传 / 下传各重复 ≥5 次 | ✅ 已跑满（**上传 10 轮、下传 9 轮**） | `sim/results/stage_a_cloud_repeat/`（142 个文件，**每轮带同会话的 `iperf3` 交叉验证**） |
| 修 A4（并发检查）只测上传方向的问题 | ✅ 已修，下传方向现在也能测 | `sim/k8s_test/measure_system.py` |

**已知的数值结论（来自 `stage_a_cloud_repeat/`）**

- **上传方向（限 6 Mbit）**：10 轮里 **9 轮 PASS**，落在 **5.571–5.729 Mbit/s**；第 5 轮异常（19.947，＝**不限速时的速度**，说明那一轮限速没生效）。
- **下传方向（限 1.5 Mbit）**：9 轮里 **8 轮 PASS**，落在 **1.438–1.441 Mbit/s**（离散度 < 0.3%）；第 5 轮异常（2.909，同样是**不限速时的速度**）。
- **两个方向的异常都是"限速没应用上"，不是链路或工具问题**，且两批都出现在第 5 轮 → **像是跑实验的流程里固定的一个漏步**。
- 一个已推翻的旧判断：早前说"自研工具比 `iperf3` 低 12 个百分点"是**错的**——那是拿不同轮次的两个数在比。
  **同一会话、同一载荷**下，两者只差 2–4 个百分点（实测：工具 5.721 / iperf3 发送端 5.909 / iperf3 接收端 5.578）。

### 11.2 没做完的（开机后从这里继续）

1. **定位 up05 / do05 两轮"限速没生效"的原因**——这是当前最该查清的一条。
2. **写 `sim/k8s_test/STAGE_A_CLOUD_REPEAT.md`**（逐轮数字 + 三项判据逐轮判定 + 未达标项）。
   **数据已经全在盘上，不用重跑**：`sim/results/stage_a_cloud_repeat/` 里每一轮都有
   `*_battery_stdout.txt`、`*_iperf3.json`、`*_tc_before/after*.txt`、`*_qdisc_poll.txt`。
3. 下传方向可以再补 1 轮凑齐 10 轮（可选）。
4. 判据口径按用户认可的三条走（见 §11.3）。

**恢复被停掉的子代理**：`Agent(resume="agent-2", prompt="继续")`（它的对话历史跨会话保留）。

### 11.3 用户认可的判据（**已从"±10% 对标称值"改掉**）

原判据"限速后实测吞吐在 ±10% 内"有口径歧义：同一台限速器，**乐观口径 +2.5%、保守口径 −10.0%**，
跨度正好压在 ±10% 线上。改成三条：

| # | 判据 | 当时状态 |
|---|---|---|
| 1 | 限速器有效性：`overlimits` 增量 > 0 且 `dropped` 可忽略 | ✅ |
| 2 | 速率正确性：与**同会话的** `iperf3` 相差 ≤ 2% | ✅（同会话实测差 2–4 个百分点，落在边界附近） |
| 3 | 方向可分性：单路 vs 多路 ≥ 1.5× | ✅ |

### 11.4 环境事实（重开后直接用，省得重新摸）

- **云主机**：`49.235.115.164`，腾讯云轻量，Ubuntu 26.04，用户 `ubuntu`，sudo 免密。
  **SSH 用公钥免密**：`ssh -i /d/Documents/API/123.pem -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/tmp/kh_new ubuntu@49.235.115.164`
  （密码已不需要，用户可自行更换）
- **云防火墙只放行** 22（仅本机公网 IP）、80、443、**8000**、5173。
  **5201 / 9101-9103 / 8080 全被丢包** → 实验只能用 8000 与 5173。
- **链路能力**：上传 ≈ 21.6–24.7 Mbit/s（家庭宽带上行）；**下传 ≈ 2.9 Mbit/s（该实例出口带宽包上限）**。
  → 计划书原写的 25/50/100 Mbit 档位在下传方向**不成立**；实际用 1.5（下传）/ 6（上传）Mbit。
  → **200 MB 载荷档在公网链路不可用**（下传单程约 9 分钟）。
- **限速配方**：HTB + **按端口过滤**（下传在云上匹配源端口 8000；上传在 WSL 匹配目标端口）。
  **绝不能整块网卡一刀切**，否则会把 SSH 一起限掉、把自己关在门外。
  ⚠️ **坑**：`pkill -f "iperf3 -s"` 会匹配到自己的命令行从而杀掉自己的 shell（ssh 退 255）；
  要用 `pkill -f "[i]perf3"` 这种加方括号的写法。
- **本次关机时云端已清理**：限速规则已拆（回到 `mq`）、无实验进程、8000/5173 已释放。

### 11.5 一条成本提醒

那台云主机是**按量计费**的。如果短期内不再用它，**去控制台把它关机/释放**，
否则你关机期间它仍在计费（停机通常仍收云盘费，要"释放"才停）。

---

## 12.【2026-10-09 更新】线②：B1 / 4c / 概率校准 / PREREG_3 交接（两台 A10 已停机）

> 状态摘要在 `AGENTS.md` §7b；云端实操的坑在 `AGENTS.md` §10e；本轮记录在
> `sim/k8s_test/B1_4C3_REPORT.md`。文档链（`sim/k8s_test/`）：
> `B1_PLAN.md` → `B1_REPORT{,_2}.md` → `B1_4C_PREREG.md` → `B1_4C_REPORT.md`（4c，**6/15 未通过**）
> → `B1_4C_PREREG_2.md`（作废未执行）→ `LOGPROBS_CHECK_PREREG.md`（作废）→
> `LOGPROBS_CALIB_PREREG.md`（校准）→ `B1_4C_PREREG_3.md`（冻结）→ `B1_4C3_RUNBOOK.md`（已执行）
> → **`B1_4C3_REPORT.md`（本轮记录）**。

### 12.1 一句话状态

**PREREG_3 解释性诊断已完成（10-08 采数、10-09 记录），结果只描述不改判；
4c 的 6/15 未通过结论不变；「近并列翻转」仍是待验证解释；B2/C/D 不放行。
两台 A10 已按「节省停机模式」停机（10-09 逐台核对）。**

### 12.2 本轮数据（run `b1_4c3_20261008T134807Z`）

- **门**：C1 冒烟两臂全过（各 156 步，「吐词 = processed 分第一名」逐词校验 0 不合）；
  E3 两臂 r2 vs r3 逐位相同；两臂前缀缓存均关闭（`enable_prefix_caching="False"`）。
- **E4**：15 对请求 12 对逐字相同；**q04 三遍都在第 5 个词分岔**（" a" vs " an"）——
  对照臂给两词 33.3% / 32.2%、拆分臂 31.3% / 33.9% → **近并列**（m=0.9235，A 档）。
- **E2**：分岔处决策余量 0.011–0.026；同臂噪声地板（普通概率差）中位 1e-5、p90 8e-4。
- **概率口径校准**（`logprob_calib_20261008T122156Z`）：raw 111/498 矛盾（22.29%）、
  processed 498/498 自洽、两臂重复逐位相同、非退化 one-hot（0/498）。
  **校正后的派生分析**在 `results/logprob_calib/logprob_calib_20261008T122156Z_analysis_corrected/`
  （one-hot 口径修正，不覆盖原件；C1/C2/C3 判定不受影响）。

### 12.3 开机续跑时要知道的

1. 两台实例：阿里云 cn-heyuan，`i-f8z4ggqwhyqz0r6lp1mj`（A，私网 172.29.31.52）/
   `i-f8z4ggqwhyqz0r6lp1mi`（B，私网 172.29.31.51）。**节省停机后固定公网 IP 已释放，
   重启后先去控制台记新公网 IP**；私网不变。库存不足可能让"启动"失败，换时段重试。
2. SSH 参数与 key 路径在 `k8s_test/run_b1.sh` 顶部（`SSH_KEY` 等，别手抄中文路径）。
3. 工具：启停 `b1_remote_serve.sh`（注意停止子命令是 `stop <name>` / `stop-all`）、
   客户端 `b1_client.py`（v2 题组：`gen-prompts --preset v2`）、分析 `b1_4c3_analyze.py`；
   一键编排 `run_prereg3_cloud.sh`（**复用前先读文件头的传输重试规则**）。
4. 预算纪律沿用 B1：单次连续 ≤4 h/台、累计 ≤16 卡时；跑完立即**节省停机**，
   并以控制台状态验收（不要用端口探测）。
5. 下一步由作者决定：**B2/C/D 未放行**；若复跑任何诊断，先写新预注册，不重用旧执行单。

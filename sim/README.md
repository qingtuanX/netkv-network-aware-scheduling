# sim —— 最小仿真实验台

这是论文《面向 AI 推理服务负载的网络感知异构算力任务调度》的实验工程目录。
当前版本只实现 **Day 1–3** 的目标：跑通 `trace → 仿真 → 图` 的最小闭环。

**当前已实现**：Random、LeastQueue、LeastLoad 三个策略；异构节点；SimPy 离散事件仿真；per-request 原始记录；manifest 归档。

**当前未实现**（按计划在后续周加入）：网络传输时间、NetworkAware、staleness、自适应回退、kind/k3d 验证。

---

## 快速开始

```bash
cd sim

# 1. 环境（已建好则可跳过）
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt

# 2. 用合成负载打通链路，不依赖任何外部数据
.venv/Scripts/python.exe run_min.py --source synthetic

# 3. 看结果
#    results/raw/<run_id>_summary.csv      每次运行的汇总指标
#    results/raw/<run_id>_*_requests.csv   每请求级原始记录
#    results/raw/<run_id>_manifest.json    配置哈希、代码版本、种子
#    figures/sojourn_distribution.png      完成时间分布 + 尾部分布
```

## 接真实 trace

Azure trace 按时间升序排列，所以用 HTTP Range 截前 50 MB 就等于拿到最早那段数据，
不必下完整 660 MB：

```bash
mkdir -p data/raw
curl -L -C - --retry 3 -A "Mozilla/5.0" -r 0-51800516 \
  -o data/raw/azure_code_first50MB.csv \
  https://github.com/Azure/AzurePublicDataset/releases/download/dataset-llm-2024/AzureLLMInferenceTrace_code_1week.csv
```

先体检，再仿真：

```bash
.venv/Scripts/python.exe inspect_trace.py data/raw/azure_code_first50MB.csv
.venv/Scripts/python.exe run_min.py --source trace
```

体检报告要回答四个问题，这四个答案直接决定实验因子怎么设：

1. 这段数据覆盖多长时间
2. 到达率多少，突发有多强（看 CV）
3. `ContextTokens` / `GeneratedTokens` 的分布形状
4. 异常值比例（token=0、时间戳乱序、截断行）

---

## 目录

```text
sim/
├── run_min.py           主实验入口（三策略对照）
├── inspect_trace.py     trace 体检
├── configs/baseline.yaml 冻结的实验配置
├── src/
│   ├── model.py         请求、节点、服务时间模型
│   ├── trace_loader.py  trace 加载 + 合成负载
│   ├── strategies.py    Random / LeastQueue / LeastLoad
│   ├── simulator.py     SimPy 事件模型
│   ├── metrics.py       指标与 bootstrap 置信区间
│   └── config.py        配置加载
├── data/raw/            trace 原始文件（不进 git）
├── results/raw/         原始记录 + manifest
└── figures/             论文图
```

---

## 模型假设（写论文时必须原样声明）

| 项目 | 当前假设 | 状态 |
|---|---|---|
| 工作量 | `work = prefill_coef·ContextTokens + decode_coef·GeneratedTokens` | 工程估算，待标定 |
| 节点并发 | 每节点 `slots` 个独立槽位，槽位间不争抢算力 | 一阶简化 |
| 服务时间 | `work / compute_rate`，单槽位独立推进 | 一阶简化 |
| 网络 | **未建模** | 待加入 |
| 状态新鲜度 | 观测快照恒为新（`staleness = 0`） | 待加入 |
| deadline / SLO | trace 不提供，暂无 | 待设定并声明 |
| 到达率缩放 | `rate_scale` 是实验杠杆，不是数据原貌 | 须声明 |

---

## 公平性铁律

四种策略必须共享**逐位相同**的负载：

- 到达时间、context、generated 全部由 `workload.seed` 决定，**与策略无关**；
- 策略自身的随机性只来自 `seeds` 里的种子（`Random` 用得上）；
- 节点规格、暖机比例、终止条件对所有策略一致。

如果哪天发现某个策略跑的是另一份请求流，那次实验全部作废。

---

## 每次运行必须留下

`run_min.py` 自动写入 `results/raw/<run_id>_manifest.json`，包含：

- 代码版本（git short hash）+ 未提交文件数
- 配置全文 + 配置哈希
- 随机种子列表
- Python 版本、操作系统
- 负载体检结果
- 墙钟耗时与仿真加速比

**原始数据 append-only，异常运行不删除**，只标注原因。

---

## 性能预警

`run_min.py` 每次都会打印仿真加速比（`sim_speedup`，例如 `8000x` 表示 1 秒真实时间算完 8000 秒仿真时间）。
这个数字决定 480 次主实验要跑多久——**先测速，再决定负载切片多大**。

若太慢，按以下顺序调整：

1. 调小 `workload.max_requests`
2. 调小 `workload.duration_s`
3. 减少 seeds（先跑 1 个测速）
4. 再考虑并行化

不要为了跑得快而丢掉 trace 的突发结构——那是本研究的负载特征之一。

---

## 负载强度 rho —— 最容易踩的坑

每次运行都会打印：

```text
系统容量      :      112,000 token-units/s (3 节点, 12 槽位)
单请求平均工作量:        3,505 token-units
负载强度 rho  :        0.370
```

定义：

```text
rho = 单请求平均工作量 × 到达率 / 系统总容量
```

**rho 决定这次实验有没有意义：**

| rho | 系统状态 | 后果 |
|---|---|---|
| < 0.3 | 基本空闲，几乎不排队 | 四种策略差异极小，结论会变成"网络感知没用"，而真相是"没跑到有压力的区间" |
| 0.3 – 0.95 | 有排队压力但稳定 | 这是要观察的区间 |
| > 0.95 | 过饱和，队列无限增长 | p95 只在测队列排空时间，是另一个问题，必须明确声明 |

实测过的对照（当前节点配置，合成负载）：

| 到达率 | Random p95 | LeastQueue p95 | LeastLoad p95 | 说明 |
|---:|---:|---:|---:|---|
| 5 QPS | 3.4s | 1.3s | 1.3s | rho≈0.15，系统空闲，LeastQueue 与 LeastLoad **结果完全相同** |
| 20 QPS | 268s | 16.0s | 13.2s | 出现真实排队，策略开始分化 |
| 100 QPS | 1561s | 1052s | 551s | 过饱和，LeastLoad 明显优于 LeastQueue |

结论：**必须先把 rho 标定到 0.3–0.9，再讨论策略差异。**
当前节点总容量 = `16000×4 + 8000×4 + 4000×4 = 112000` token-units/s，
合成负载平均工作量约 3500 token-units，故 rho=1 约对应 32 QPS。

接真实 trace 后，第一件事就是把 `nodes[].compute_rate` 按 trace 的真实 QPS 反推标定，
使 rho 落在 0.3–0.9；否则整篇论文的实验都建立在无压力的退化区间上。

---

## 已知的一阶简化（待升级）

- 槽位之间**不争抢算力**：4 个槽位各按 `compute_rate` 满载推进，总吞吐是线性叠加的。
  真实 GPU 上并发请求会互相拖慢，这会显著改变最优调度策略。
- 没有 batch 合并效应：真实推理服务把多个请求合批会提高吞吐、拉长单请求延迟。
- 没有网络传输时间。
- 没有 deadline / SLO。

这四条都会影响 RQ1 的答案，必须在论文的 Threats to Validity 里写明。

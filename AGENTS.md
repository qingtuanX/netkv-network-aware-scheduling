# AGENTS.md —— 本仓库的工作说明（给 AI 编码代理）

> 本文件描述仓库**当前真实状态**，不是愿景文档。凡是"尚未实现/未跑通"的地方都明确标注。
> 仓库内所有文档、注释、报告、技能文件都是**中文**；论文正文最终为英文投稿，代码标识符用英文。
> 记录时间：**2026-09-23**（仓库位于 `E:\代码项目文件\论文`，Windows）。注意实验矩阵可能在后台串行运行，`results/` 的内容会随后变化。
> **2026-10-09 补记**：线②真实跨云（`sim/k8s_test/` 的 B0/B1/4c/概率校准/PREREG_3）状态见 §7b 与 `sim/RESUME.md` §12；**4c 收口计划与 10-09→10-18 时间表见 §7c**（计划已冻结、尚未执行）。
> 本次相对 09-22 的实质变化：① v2 模型（链路 FIFO 争用 + `NetworkAware2`）三批矩阵全部跑完并通过 11/11 门检查；
> ② 真实系统实测从"未开始"变成"k3s 已装成、脚本齐备"（见 §5 入口⑩）；③ 门 1 的判据修了两处实现错误（见 §10 第 38–39 条）。

## 0. 汇报对象与用语（先读这条）

**项目作者是本课题的学生，不是资深工程师。** 向他汇报时必须按下面的要求写，
否则他看不懂——这不是客气话，是他明确提过的要求：

1. **用大白话讲结论**，先给"这件事是好是坏、下一步做什么"，再给细节；
2. **专有名词第一次出现必须解释**，或者干脆换成日常比喻。例如：
   - 调度策略 → "把请求派给哪台机器的规则（像餐厅领位员决定带客人坐哪桌）"
   - p95 / 分位数 → "最慢的那一小撮情况有多慢"（不要只写 "p95"）
   - 负载强度 rho → "店里的拥挤程度"
   - net_share → "这一趟里花在搬数据上的时间占了多大比例"
   - staleness / 陈旧观测 → "调度员看着几分钟前的老报表在做决定"
   - 门（gate）→ "开工前的体检项，不过就不许写进论文"
3. **表格要配"怎么读"**：一行说明"数字小的那列代表谁更好"；
4. **不要堆术语**：显著性、效应量、置信区间这类词出现时，必须同时给一句人话解释；
5. **负结果和坑要照实说**，别只报好消息；说清"这个结论不能用到什么场合"；
6. 中文排版，短句、短段，别用多层嵌套列表；
7. **⚠️ 他明确说过"专业术语我都看不懂"（2026-09-23）。所以：任何回答里都不许出现未经翻译的术语，
   包括他问技术问题的时候。**哪怕他觉得问题本身很技术，也先用人话讲清楚，需要时在括号里附上术语原词。
   **违反这一条等于汇报失败。**

### 用词对照表（出现左边任何一个词，必须就地给出右边那句大白话）

| 术语 | 必须同时说的人话 |
|---|---|
| p95 / p99 / 分位数 | "最慢的那一小撮请求有多慢"（倒霉的时候有多慢） |
| 平均值 / 中位数 | "一般情况有多慢" |
| rho / 负载强度 | "店里有多忙" |
| net_share / 网络占比 | "一趟里花在搬数据上的时间占多少" |
| staleness / 陈旧度 / lag | "看到的是几秒前的消息" |
| 队列状态 | "机器前面堆了多少活" |
| 链路状态 | "网线堵不堵、有多宽" |
| 通道（channel） | "两种不同的消息"（说清是哪两种） |
| 效应量 / Cliff's delta | "差别有多大"（0 附近＝几乎没差别） |
| p 值 / 显著性 / p_holm | "'这是碰巧'的可能性有多大"；**必须同时说明：请求数一多，这个数必然很小，所以不能拿它当有效果的证据** |
| 置信区间 | "这个数字的波动范围" |
| 消融 / ablation | "把某个功能关掉再跑一遍，看少了它差多少" |
| 权重 w_net | "给网线消息多大的话语权"（0＝完全不听） |
| 回退 / fallback | "发现消息太旧就不听它，退回只看排队" |
| 判据 / 阈值 | "一条判断规则 / 那条线" |
| 离散度 / 方差 | "请求大小是不是忽大忽小" |
| 相关时间 / 自相关 | "网线状况自己多久变一次" |
| 无量纲 / 归一化 | "用比例来说，而不是用秒数"（因为不同机器快慢不一样） |
| 部署 | "真的拿去用" |
| 分布式 / 算力网络 | "很多台机器分散在不同地方、靠网络连起来一起算" |
| 预印本 | "作者自己挂上网、还没经过审稿的论文" |
| 顶会 / CCF-A / SCI 分区 | "这一行公认最权威的会议 / 期刊档次" |

（面向论文正文的写法不受此限：正文是英文投稿，按 §6/§8 的学术口径来。）

## 1. 项目概览

这是一个**论文项目仓库**，不是一个软件产品。唯一有效的论文规划在根目录 `论文规划.md`：

- **论文题目方向**：面向 AI 推理服务负载的网络感知异构算力任务调度——收益边界与失效条件。
- **研究形态**：以 SimPy 离散事件仿真为主，单节点 k3s（WSL2 里）小规模实测为辅（**已装成并跑通首轮**，见 §5 入口⑩）。实测只用于**趋势对照**，不得包装成生产级集群性能。
- **四个被比较的策略**：`Random`、`LeastQueue`、`LeastLoad`、`NetworkAware`。
- **贡献口径**：empirical study / simulator / benchmark characterization；重点刻画网络感知的**收益边界和失效区间**，允许并必须报告"无收益"与"负收益"结果。

仓库根目录 `E:\代码项目文件\论文` 只有下面这几类内容：

| 路径 | 内容 | 是否进 git |
|---|---|---|
| `文献/` | 52 篇文献 PDF（48 篇已下载，4 篇需机构访问）+ `MANIFEST.md` + 数据集索引 | ❌ 被根 `.gitignore` 排除（约 90 MB；要版本化得用 git-lfs） |
| `sim/` | **全部代码**：Python 仿真实验工程 | ✅ |
| `paper/` | **论文 LaTeX 工程**：`main.tex`（IEEEtran journal）+ `sections/01..10` + `refs.bib` + `figures/`（英文标签图，`make_paper_figures.py` 生成）+ `check_tex.py`（无编译器时的静态自检） | ✅（`figures/*.png` 可能被忽略） |
| `论文草稿.md` | 中文批注版的写作工作稿（§0 标题/摘要/贡献已起草） | ✅ |
| `论文证据索引.md` | **编写正文时的唯一取数来源**：每条主张 → 具体数字 → 出自哪个 CSV 的哪一列 | ✅ |
| `*.md`（根目录） | 中文规划文档：`论文规划.md`、`论文写作与从零学习规划.md`、`学习什么.md` | ✅ |
| `.kimi-code/skills/research-experiment/SKILL.md` | 本项目的**实验纪律**（四道门、禁止的写法、已知模型简化） | ✅ |

### 取数与写作的绑定关系（改论文前必读）

论文里**每一个数字**都必须来自 `sim/results/paper_numbers.txt`，该文件由
`sim/analysis/export_paper_numbers.py` 从三批分析产物自动导出（620+ 行，按论文小节编号排列）。
**禁止手打数字**：手抄必然出错，且出错后没人能快速发现。流程是

```
results/matrix_*        ← 原始实验数据（append-only 证据）
      ↓ analysis/analyze.py
results/analysis_*/     ← 论文表格 + report.txt
      ↓ analysis/export_paper_numbers.py
results/paper_numbers.txt  ← 正文里所有数字的唯一来源
      ↓
paper/sections/*.tex    ← 正文（禁止手改数字，只能从上一行复制）
```

论文图由 `sim/analysis/make_paper_figures.py` 从同一批 CSV 生成到 `paper/figures/`，
**必须英文标签**（`analyze.py` 生成的图是中文标签，不能进英文投稿；该脚本会断言图上无 CJK）。
改完 tex 后跑 `sim/.venv/Scripts/python.exe paper/check_tex.py` 做静态自检（引用键、交叉引用、
环境配对、图片是否存在、括号计数）——本机若没装 LaTeX，这是唯一的自动化防线。

**动手改实验之前必须先读两份文件**：`论文规划.md` §6（实验矩阵与统计协议）和 `.kimi-code/skills/research-experiment/SKILL.md`（实验纪律）。它们规定了因子、指标、统计方法和禁止的报告方式，是需求口径的上游。

### 关于 git（重要）

仓库已 `git init`，**但 master 分支没有任何提交**（`git log` 报 "your current branch 'master' does not have any commits yet"）。后果：

- 脚本里 `git rev-parse --short HEAD` 会失败并回落到字符串 `"uncommitted"`，所以 manifest 里的 `code_version` 现在就是 `"uncommitted"`，`code_dirty_files` 为 `null`。这是**预期行为，不是 bug**。
- 在本机跑 git 命令需绕过 "dubious ownership" 提示，例如：
  `git -c safe.directory='E:/代码项目文件/论文' status --porcelain`
- 未经用户明确要求，不要 `git commit` / `git push` / `git reset`。

## 2. 技术栈与运行环境

- **Python 3.12.10**，虚拟环境在 `sim/.venv`。Windows 下解释器路径固定为 `sim/.venv/Scripts/python.exe`（`sim/README.md` 与各脚本 docstring 都用这个写法）。
- **依赖**（`sim/requirements.txt`）：`simpy>=4.1`、`pyyaml>=6.0`、`pandas>=2.0`、`numpy>=1.26`、`matplotlib>=3.8`。
  - ⚠️ **`scipy` 没有被写进 requirements.txt**，但 `probe_rq1.py` 与 `experiments/run_matrix.py` 直接 `from scipy import stats`。venv 里已装 `scipy 1.18.1`。新建环境时必须补装 scipy，或者顺手把它加进 requirements.txt（改动需与用户确认）。
  - 实测已装版本：numpy 2.5.3、pandas 3.0.6、matplotlib 3.11.2、simpy 4.1.2、PyYAML 6.0.3。
- **没有** `pyproject.toml` / `setup.py` / `package.json` / `Cargo.toml` / `Makefile` / CI 配置。**没有构建步骤**：所有入口都是直接跑的脚本，`src/` 是普通包（`sim/src/__init__.py` 为空），通过入口脚本里的 `sys.path.insert(0, ROOT)` 导入。没有任何 service、API、后台进程。
- **平台**：Windows 11 + Git Bash。控制台默认可能是 GBK，**每个入口脚本顶部都有一段 `sys.stdout.reconfigure(encoding="utf-8")`**，新增入口脚本请照抄，否则中文输出乱码。
- **绘图字体**：`analysis/analyze.py` 会自动挑选 `Microsoft YaHei / SimHei / Noto Sans SC / DengXian`（当前实际用 Microsoft YaHei），因此它的图带中文标题；其它脚本的图是英文标签。

## 3. 目录结构与模块划分

```text
sim/
├── run_min.py              入口①：最小闭环 —— 三策略对照 + 第一张分布图
├── inspect_trace.py        入口②：trace 体检（覆盖时长/QPS/CV/token 分位/异常行）
├── probe_rq1.py            入口③：RQ1 探索性探针（合成负载，3 种网络场景 × 4 种数据量）
├── experiments/
│   └── run_matrix.py       入口④：主实验矩阵运行器（8 种 mode，负载可选 synthetic/trace）
├── analysis/
│   ├── analyze.py          入口⑤：结果汇总 → 论文级表格 + 图 + report.txt
│   ├── characterize_load.py 入口⑥：把 rho 拆成均值/瞬时两部分（多时间窗），门 1 的补充证据
│   └── verify_gates.py     入口⑦：四道门 + 白盒/黑盒实现验证的可复现检查记录
├── src/                    仿真核心库
├── configs/baseline.yaml   run_min.py 用的冻结配置（节点、仿真系数、负载、种子）
├── data/raw/               trace 原始文件（不进 git）：
│                           azure_code_1week.csv（691,989,454 B）、burstgpt_1.csv（50,853,373 B）
├── data/processed/         预留，空
├── results/raw/            run_min / probe_rq1 的每请求级与汇总产物
├── results/matrix/         **合成负载**的 run_matrix 产物（runs/paired/manifest）
├── results/matrix_trace/   **Azure trace** 的产物
├── results/matrix_burstgpt/ **BurstGPT** 的产物
├── results/matrix{,_trace,_burstgpt}_v2/  **v2 模型**（链路 FIFO 争用 + NetworkAware2）的三批产物
├── results/analysis{,_trace,_burstgpt}{,_v2}/  表格 + report.txt（含 v2 三批）
├── results/verification/   四道门的检查记录（{synthetic,azure,burstgpt}{,_v2}_gates.{csv,json}）
├── results/k8s_test/       真实系统实测的每请求 CSV（本机多进程 + 容器两档）
├── figures{,_trace,_burstgpt}{,_v2}/  各批次的图（*.png 被 gitignore）
├── backup_artifacts.py     入口⑧：把 results/figures/data/code 备份到另一块盘并逐个哈希校验
├── rerun_missing_v2.sh     入口⑨：只补跑缺 manifest 的 mode（矩阵被中断后用；`DRY_RUN=1` 先看清单）
├── k8s_test/               入口⑩：真实系统实测（详见 §5 入口⑩ 与 k8s_test/README.md）
│   ├── measure_system.py     4 个角色：worker / scheduler / client / selftest（同一份代码）
│   ├── setup_wsl_k3s.sh      在 WSL2 里装单节点 k3s + 就地构建镜像（不依赖任何镜像仓库）
│   ├── check_workers.sh      体检：3 个 worker 是否真的按不同档位在跑
│   ├── run_k3s_grid.sh       到达率阶梯 + 策略×新鲜度×数据量 对照网格
│   ├── summarize_real.py     汇总成论文级表格（只用标准库，跑在 WSL 的系统 python3 上）
│   └── k8s/workers.yaml      3 个异构 worker Pod（算力 4/2/1、带宽 100/500/1000 Mbps）
└── requirements.txt
```

`sim/README.md` 是最早写下的工程说明（Day 1–3 口径）：它的**「当前已实现 / 未实现」清单已经过时**（现在已有 `NetworkAware`、时变网络、staleness 双通道、6 种实验 mode），但**「模型假设」「公平性铁律」「负载强度 rho」「每次运行必须留下」「性能预警」几节仍然有效**，改动实验前值得一读。

### `src/` 各模块职责

| 模块 | 职责 | 关键符号 |
|---|---|---|
| `src/config.py` | 读 YAML、把 `nodes:` 段构造成 `NodeSpec` | `load_config()`、`build_nodes()` |
| `src/model.py` | 数据模型与**全部公式**（服务时间、网络时间、字节数、deadline、SLO） | `Request`、`NodeSpec`、`NodeView`、`compute_work()`、`service_time()`、`network_time()`、`request_bytes()`、`compute_deadline()` |
| `src/network.py` | 链路状态：静态 / 时变 | `LinkState`、`NetworkTrajectory`、`StaticNetwork`、`UnstableNetwork`（对数带宽的 OU 过程，预生成轨迹 + 线性插值） |
| `src/state_history.py` | 节点运行状态历史，供"回看过去"构造过期观测 | `NodeStateHistory.record()` / `.at(t)`（bisect 精确查找）、`make_histories()` |
| `src/strategies.py` | 策略接口与四个策略 | `Observation`、`Strategy`、`RandomStrategy`、`LeastQueueStrategy`、`LeastLoadStrategy`、`NetworkAwareStrategy`、`STRATEGIES` 注册表 |
| `src/simulator.py` | SimPy 离散事件模型 | `SimConfig`、`RunResult`、`run()` |
| `src/metrics.py` | 指标 | `request_records()`（每请求长表）、`summarize()`、`bootstrap_ci()` |
| `src/trace_loader.py` | 负载来源 | `load_azure_trace()`、`generate_synthetic()`、`WorkloadStats` |
| `src/workload.py` | 4 类推理负载的定义与切片 | `ClassSpec`、`WORKLOAD_CLASSES`（`compute_heavy`/`data_heavy`/`low_latency`/`mixed`）、`build_from_trace()`、`build_synthetic()` |

### 端到端数据流

```text
trace / 合成负载  →  src/workload|trace_loader
                 →  src/simulator.run(workload, nodes, strategy, SimConfig, seed, trajectory)
                       ├─ src/network      真实链路状态（算传输时间）
                       ├─ src/state_history 过去某时刻的节点状态（构造过期观测）
                       └─ src/strategies   基于 Observation 选节点
                 →  src/metrics.summarize / request_records
                 →  results/{raw,matrix,matrix_trace}/*.csv + *_manifest.json
                 →  analysis/analyze.py  →  results/analysis*/*.csv + report.txt + figures*/*.png
                    （目录由 --matrix-dir / --table-dir / --fig-dir 决定；
                      trace 批：matrix_trace → analysis_trace + figures_trace）
```

## 4. 核心设计约定（改代码必须遵守）

1. **公平性：负载与策略完全解耦。** 到达时间、context、generated 全部由负载种子决定，与策略无关；同一 cell 下所有策略共用**逐位相同**的请求流，因此配对比较（按 `req_id` 对齐）是合法的。四种策略的节点规格、暖机比例、终止条件也必须一致。若发现某个策略跑的是另一份请求流，那批实验全部作废。
2. **策略只能看到"调度器以为的状态"。** `Strategy.choose()` 只接收 `Observation`（内含 `NodeView`），拿不到 `NodeSpec` 或真实 `pending`。这是 staleness 的着力点：策略代码**无法绕过观测层偷看真实状态**。
3. **真实状态与观测状态严格分离。** 仿真算传输时间用 `trajectory.state(node_id, env.now)`；策略用 `trajectory.state(node_id, now - lag)`。
4. **staleness 必须拆成两条通道。** `SimConfig.staleness_queue` 与 `staleness_network` 分开控制（队列是亚秒/秒级，链路是几十秒级）；`Observation.observed_at = min(t_q, t_n)`，`staleness = true_time - observed_at`。用一个 lag 同时控制两者会让队列陈旧淹没链路陈旧的真实效应（已实测：30 s 队列陈旧让 p95 涨约 36 倍）。另有 `stale_queue` / `stale_network` 开关用于拆分来源。
5. **消融必须是严格超集。** `NetworkAwareStrategy(w_net=0)` 的排序必须与 `LeastLoad` **逐位相同**（评分函数只多一项 `w_net × (t_in + t_out)`，权重为 0 时 `min` 的 key 退化为 `(backlog_time, node_id)`）。任何"网络感知的收益"都必须能干净归因到网络项。验证方式：`np.array_equal` 比较每请求完成时间数组，**不许用"p 值不显著"代替逐位比较**。
6. **参数来源分四类，写法不能混**：公开 trace（可写数据事实，注明许可）／公开论文实测值（可引用，注明出处）／工程估算（必须标注"工程估算"并进 Threats to Validity）／合成占位（**严禁**写成 trace 结果）。当前已知的工程估算：`prefill_coef`、`decode_coef`、`bytes_per_context_token`、`staleness` 量级、SLO 定义、`compute_rate` 绝对值。
7. **SLO 不能是固定秒数。** 主实验用 `slo_mode="relative"`，`deadline = slo_multiplier × 该请求在最理想节点上的完成时间`（当前 `slo_multiplier=3.0`，理想值用真实当前链路 + 空队列假设算）。固定 SLO 会让 miss rate 饱和在 0/1，失去信息量（已实测）。
8. **指标口径统一在 `metrics.summarize()`**，列名以 `analyze.METRIC_COLS` 为准：`n_requests, sojourn_mean, sojourn_p50/p95/p99/max, wait_mean, wait_p95, net_mean, net_share, slo_miss_rate, throughput_qps, node_utilization`。`net_share`（网络时间占端到端时间的比例）是 RQ1 的**自变量**，不是可选辅助指标。统计前一律先按 `warmup_fraction=0.1` 去掉暖机期的前 10% 请求。
9. **原始数据 append-only。** `results/raw`、`results/matrix`、`results/matrix_trace` 是证据：不删除、不手工修改、不覆盖；异常运行只标注原因。图表只能由脚本从原始数据生成，禁止手工改数（SKILL.md 明确禁止"在电子表格里改数"）。
10. **每个 run 都要留下 manifest**：`run_id`（`{source 或 mode}_{UTC 时间戳}`）、UTC 时间、代码版本、配置全文与哈希、随机种子、Python/平台版本、负载统计、墙钟耗时、仿真加速比。`run_matrix.py` 额外记录每 cell 的耗时；`--source trace` 时另有 `data_source`、`trace_file`、`trace_stats`、`target_rho`、`n_trace_segments`。
11. **接真实 trace 的两条硬规则（`--source trace`）**：
    (a) **到达率必须按"真正被仿真的那一段"标定到目标 rho**，不能用整片 trace 的平均到达率。trace 有强烈昼夜突发，用整片平均标定会让高峰时段的前缀冲到 rho>1（实测第 2 片到 1.20，门 1 失守）。`build_trace_cell_workload()` 先按 qps=1 探一次前缀的局部到达率，再反解 qps；因为到达时间只乘一个 scale，"前缀到达率"与 qps 成正比，反解是精确的。
    (b) **每个种子必须对应不同的 trace 时间片**（种子 s 用第 s 片）。trace 只有一条时间轴，若所有种子共用同一份请求流，确定性策略的种子间方差恒为 0（门 3 的推论），加种子就是浪费算力。
    合成路径**曾经没有**这两条保障：`build_synthetic()` 原来给四类负载同一个 `target_qps=30`，而四类负载的每请求工作量差一个量级，于是 `data_heavy` 跑到 rho=1.83、`low_latency` 压到 0.054，那 6 个组合的对比不可采信。**现在两条路径统一**：`build_synthetic_cell_workload()` 用与 trace 完全同构的两步标定（先探前缀的平均工作量与局部到达率，再反解 qps），实测 12/12 组合落在目标 rho 上。
    注意标定会**整体缩放时间轴**：为把 rho 固定住，不同负载类/不同时间片得到的 qps 能差几个数量级（实测 Azure 16–137 QPS、BurstGPT 2–2005 QPS）。这是刻意的设计——保持 rho 恒定才能把"负载强度"与"负载形状"两个因子解耦；代价是**突发的时间尺度随 qps 变化**，所以突发性必须多窗口报（`analysis/characterize_load.py`），不能只看一个窗口。
12. **观测年龄是两条通道里更旧的那个。** `Observation.observed_at = min(t_q, t_n)`，因此年龄 = `max(队列 lag, 链路 lag)`，不是 `min`。回退判据、staleness 标注、阈值网格全都建在这个量上；把它当成 `min` 会让"阈值 45 s 也触发了回退"这种结论凭空出现（已实际发生）。另外**阈值不能等于任何一档的年龄**：相等时触发与否由浮点误差决定（实测年龄与阈值都是 2.0 时，`now-(now-2.0)` 算出 `2.0000000000000004`，回退被意外触发，Δp95 从 0 跳到 +105%）。详见门 5。
13. **变体策略必须在同一个 cell 内跑参考臂。** `--mode fallback` 的 cell 用 `strategies` 携带 6 个策略规格（`LeastLoad` + 不回退的 `NetworkAware` + 4 个阈值变体），用 `pair_candidates` / `pair_baselines` 指定配对关系——这样"回退 vs 不回退"是**逐请求配对**检验（Wilcoxon + Holm + Cliff's delta），而不是跨 cell 的聚合对比。cell 里只服务于调度的键列在 `CELL_META_KEYS` 中，不写进 CSV（否则会变成字符串化的列表污染证据）。新增变体臂时不要另起 mode 目录，否则配对关系会失效。
14. **自适应策略只能改"网络项"，绝不能改"队列项"。** 三次失败的共同点都是动了队列项的语义（把队列项归零、或把整体排序换成另一套）。队列信息是**唯一的负载信号**，一旦削弱，请求会涌向"快网线 + 慢算力"的节点并把它压爆（实测 p95 57 s → 522 s）。自适应只允许输出两个权重：`w_dyn`（"此刻谁在挤链路"那一块）与 `w_stat`（"哪条网线粗"那一块），两者同时为 0 时评分才退化成 `backlog_time`（此时与 `LeastLoad` 逐位相同，门 2 成立）。
15. **陈旧场景下单次仿真的轨迹是混沌的，只有多种子配对比较才算证据。** 实测：只改**前 2.5% 请求**的决策（4000 请求里约 100 个），就能让 p95 相差 17%。因此"1 个种子、看一张表谁快"得到的差异基本都是噪声；`method` / `align` / `staleness2d` 这些 mode 一律 10 个种子，统计单位是**种子**（＝独立的负载实现），不是请求。
16. **新增策略时，参数必须能真正传到实例上。** `run_matrix.make_strategy()` 用**白名单**把 dict 规格里的键传下去；键名写错（例如策略类改名后仍写旧名）会一路走默认值，**网格静默失效**（不报错）。改策略构造函数签名时，必须同步改 `make_strategy` 的白名单并加一次"参数真的生效了吗"的核对（例如把阈值改到极端值看 `describe()` 与落盘指标是否变）。

## 5. 构建 / 运行命令

**所有命令都在 `sim/` 目录下执行**（脚本用相对 `ROOT` 定位数据与产物）。

### 环境

```bash
cd sim
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe -m pip install scipy        # requirements.txt 里漏了，但 run_matrix/probe_rq1 需要
```

### 入口①：最小闭环（不依赖任何外部数据）

```bash
.venv/Scripts/python.exe run_min.py --source synthetic
.venv/Scripts/python.exe run_min.py --source synthetic --seeds 1 --max-requests 5000   # 测速用
```

参数：`--source {synthetic,trace}`、`--trace <csv>`、`--config <yaml>`、`--max-requests`、`--seeds`。
产物：`results/raw/{run_id}_{策略}_seed{n}_requests.csv`、`{run_id}_summary.csv`、`{run_id}_manifest.json`、`figures/sojourn_distribution.png`。
它会打印**负载强度 rho**（门 1 的第一个检查点）与相对 `LeastQueue` 的 p50/p95 变化率 + bootstrap CI。

### 入口②：接真实 trace

**GitHub 直连在本机不通**（`github.com:443` 连接超时），必须走反代；另外 `curl -C -` 与 `-r` 互斥（curl 8.21 直接报 `--continue-at is mutually exclusive with --range`），所以早期文档里那条 `-C - ... -r 0-51800516` 的命令**是坏的**，跑不通。

```bash
mkdir -p data/raw
# 走反代取完整文件（692 MB / 86 s，实测 7.7 MB/s）。gh-proxy.com 会忽略 Range 而返回整份；
# ghproxy.net 支持 Range（206）但只有约 8 KB/s，别用。
curl -L --retry 5 --retry-delay 3 --retry-all-errors -A "Mozilla/5.0" \
  -o data/raw/azure_code_1week.csv \
  "https://gh-proxy.com/https://github.com/Azure/AzurePublicDataset/releases/download/dataset-llm-2024/AzureLLMInferenceTrace_code_1week.csv"

# 体检（覆盖时长/到达率与突发/token 分位/异常行）。全量 1680 万行，加载约 81 s。
.venv/Scripts/python.exe inspect_trace.py data/raw/azure_code_1week.csv
.venv/Scripts/python.exe run_min.py --source trace \
  --trace data/raw/azure_code_1week.csv
```

该文件在本机的实测轮廓（2026-09-22）：**16,803,695 请求 / 168.0 小时**，均值 27.78 QPS，p95 秒级窗口 77.05 QPS（昼夜突发明显）；`ContextTokens` p50=1930 / p95=7674 / max=7743；`GeneratedTokens` p50=8 / p95=79 / max=5000；丢弃 0 行、截断尾行 0、时间戳单调。注意这里的输出极短（p50 只有 8 token），与合成负载的形状差一个量级。

本机网络出路（实测）：可达 `raw.githubusercontent.com`、`hf-mirror.com`、`modelscope.cn`、`cnb.cool`；**不可达** `github.com`、`huggingface.co`、`mirror.ghproxy.com`。

`inspect_trace.py` 回答四个决定实验因子怎么设的问题：覆盖时长、到达率与突发强度（CV）、token 分布形状、异常值比例（token=0、时间戳乱序、截断尾行）。数据集的来源/许可/URL/字段见 `文献/07-数据集与trace/README.md`。

### 入口③：RQ1 探针（探索性，合成负载）

```bash
.venv/Scripts/python.exe probe_rq1.py            # 自动把 rho 标定到 0.55
.venv/Scripts/python.exe probe_rq1.py --qps 25 --seeds 1 2 3
```

3 种网络场景（`aligned`/`anti`/`uniform`）× 4 种数据量（4 B/token → 330 KB/token）。结尾会打印 **`NetworkAware(w=0)` 与 `LeastLoad` 是否逐位相同**（门 2）。
产物：`results/raw/{run_id}_summary.csv`、`{run_id}_manifest.json`、`figures/rq1_probe.png`。

### 入口④：主实验矩阵

```bash
.venv/Scripts/python.exe experiments/run_matrix.py --mode main
.venv/Scripts/python.exe experiments/run_matrix.py --mode main --limit 4 --seeds 1   # 先测速
```

**两代模型并存（务必分清）**：

| 代 | 链路模型 | 网络档位 | 策略表 | 产物目录 |
|---|---|---|---|---|
| **v1** | 链路**不排队**（网络时间 = 链路状态的确定性函数） | 10 Gbps / 1 Gbps / 100 Mbps | Random / LeastQueue / LeastLoad / NetworkAware | `results/matrix{,_trace,_burstgpt}/` |
| **v2** | 链路是**FIFO 管道**，共用链路的传输互相排队；`link_pending_bytes` 进入观测 | 400 / 100 / 25 Gbps（真实 AI 集群量级） | v1 四策略 + **NetworkAware2**（算力队列 + 链路队列） | `results/matrix{,_trace,_burstgpt}_v2/` |

`--net-set {v1,v2}` 选择档位集合（**默认 v1**，因为 v2 需要两资源 rho 标定配套）。
manifest 里 `link_queue_model: true` 标记 v2 模型产物；**两代结论不得混引**。

⚠️ **v1 的带宽档位在 v2 模型下不可用**：给链路加上排队后，0.1–10 Gbps 会直接变成容量瓶颈
（实测 λ 掉到 0.91 QPS、rho_compute 只剩 0.03、p95 冲到 76 万秒）。v2 必须配 25–400 Gbps。

`--mode` 可选：`main | staleness | datavolume | convergence | ablation | scale | rho | fallback`（默认 `main`）。其他参数：`--out`（默认 `results/matrix`）、`--requests`（默认 20000）、`--seeds`、`--save-requests`（额外存每请求明细，体积大）、`--limit`（只跑前 N 个 cell），以及负载来源 **`--source {synthetic,trace}`**（默认 `synthetic`）、`--trace <csv>`（**Azure / BurstGPT 按表头自动识别**）、`--target-rho`（默认 0.6，两条路径都生效；`rho` 模式逐 cell 覆盖）。

**三个批次各自独立跑与分析**（互不覆盖任何证据）：

```bash
# 合成负载（默认目录）
.venv/Scripts/python.exe experiments/run_matrix.py --mode main --source synthetic --out results/matrix
# Azure LLM Inference Trace 2024
.venv/Scripts/python.exe experiments/run_matrix.py --mode main --source trace \
  --trace data/raw/azure_code_1week.csv --out results/matrix_trace
# BurstGPT（第二条 trace，字段名不同，靠表头自动识别）
.venv/Scripts/python.exe experiments/run_matrix.py --mode main --source trace \
  --trace data/raw/burstgpt_1.csv --out results/matrix_burstgpt

# 每批各自分析（表/图/报告都分目录，否则会互相覆盖）
.venv/Scripts/python.exe analysis/characterize_load.py --source trace \
  --trace data/raw/azure_code_1week.csv --table-dir results/analysis_trace
.venv/Scripts/python.exe analysis/analyze.py --matrix-dir results/matrix_trace \
  --table-dir results/analysis_trace --fig-dir figures_trace
```

trace 模式下：每类负载按 token 分位从 trace 切片、到达率按 `--target-rho` 标定（§4 第 11 条），种子 s 用第 s 个时间片；`--requests` 是每 cell 的请求数，时间片数 = 本轮最大种子号，因此**加大种子数会摊薄每个时间片**（Azure 的 16.8M 行支持 5 个种子有余量，`--mode convergence` 的 20 片也够；BurstGPT 只有 1.4M 行，20 片就会很紧）。切不出足够请求时脚本**直接报错**，不会静默缩小请求量。

各 mode 的 cell 规模：

| mode | 规模 | 说明 |
|---|---|---|
| `main` | 4 负载 × 3 网络 × 2 新鲜度 × 5 种子 = 120 cells × 4 策略 = **480 次仿真** | 论文主矩阵（`论文规划.md` §6.1） |
| `staleness` | 8 个链路 lag × 3 网络 × 3 种子 = 72 cells，加 8 个队列 lag × 3 种子 = 24 cells，合计 **96 cells** | RQ2 完整曲线；链路扫描时队列 lag 固定 ≤1 s，另有只动队列 lag 的对照通道 |
| `datavolume` | 2 数据量 × 4 负载 × 2 网络 × 3 种子 = 48 cells | 共置（4 B/token）vs 分离式/KV（330 KB/token） |
| `convergence` | 20 个种子 × 1 cell | 事后按 3/5/10/20 切片看收敛 |
| `ablation` | `w_net ∈ {0,0.5,1,1.5,2}` × 2 负载 × 2 网络 × 3 种子 = 60 cells | 含 `w_net=0` 与 LeastLoad 的逐位验证 |
| `scale` | 4/8/16 节点 × 2 网络 × 3 种子 = 18 cells | 节点规模敏感性 |
| `rho` | `RHO_GRID=[0.30,0.45,0.60,0.75,0.90]` × 2 负载 × 3 网络 × 3 种子 = 90 cells | 到达压力扫描：收益边界必须在这条轴上也有形状 |
| `fallback` | 3 陈旧档 × 2 负载 × 2 网络 × 3 种子 = 36 cells × **6 策略**（LeastLoad + 不回退的 NA + 4 个阈值变体）= 216 次仿真 | RQ2 回退机制；阈值在同一个 cell 内跑，所以"回退 vs 不回退"是逐请求配对比较 |
| `bandwidth` | `BANDWIDTH_PROFILES` 四档 × 2 数据量 × 2 负载 × 3 种子 = 48 cells | **保真度驱动的带宽敏感性**：保持 degraded 的 100:10:1 异构形状，只把链路绝对尺度从机内（800/80/8 Gbps）换到广域（1/0.1/0.01 Gbps），检验"收益是不是慢链路的产物" |
| `avewindow` | 窗口网格 `WINDOW_GRID=[0,0.5,2,10,30]s` × 2 负载 × {degraded,unstable} × 3 种子（队列通道） + 同网格 × 2 负载 × 3 种子（链路通道，仅 unstable） = **90 cells** | **【细变量②】观测的聚合窗口**：真实监控报的是"过去 W 秒的平均"而不是瞬时值。与 `lag` 的区别是本质性的：`lag` 是看到**旧**值（延迟失真），`window` 是看到**被平均过**的值、尖峰被抹平（信息失真，方向相反）。`lag` 固定为 0 以隔离变量；两条通道各自扫。新增配置项 `SimConfig.window_queue_s` / `window_network_s`（默认 0＝退化成原来的瞬时值，不影响既有结论） |
| `staleness2d` | `STALENESS2D_QUEUE=[0,1,5]s` × `STALENESS2D_NETWORK=[0,1,15,60]s` × 2 负载（mixed/data_heavy）× 10 种子 = **240 cells**；网络固定 `unstable` | **【细变量①】两条通道年龄的二维网格**：回答"排队消息旧但网线消息新"和反过来是不是一回事。种子数由 `ALIGN_SEEDS=10` 控制（统计单位是"独立的负载实现"） |
| `align` | 3 档对齐 × 2 负载 × 2 网络 × 10 种子 = **120 cells** | **【细变量③】对齐度**：`anti`（算力最强配最慢链路）／`rot1`／`aligned`。只改"谁配哪条链路"，链路本身不变；落盘 `align_index`（旧指标）与 `align_rank_corr`（Spearman） |
| `alignscale` | 4/8/16 节点 × 3 档对齐 × 10 种子 = **90 cells** | 对齐度 × 规模。**节点数一变，"算得快"与"网线粗"的配对结构自己就变了**（算力周期 4 / 链路周期 3） |
| `method` | 6 个新鲜度配置 × 2 负载 × 2 网络 × 10 种子 = **240 cells × 5 策略** = 1200 次仿真 | **【V3 方法】降权规则对照**，五条臂在同一个 cell 内跑（逐请求配对）：`LeastLoad` / `NetworkAware2`（完全信）/ `AdaptSplit`（主方法）/ `AdaptLinear` / `AdaptHard`。新鲜度水平见 `METHOD_FRESHNESS`；策略自报的权重落在 `w_dyn_mean` / `w_stat_mean` / `net_share_obs_mean` / `w_gated_frac` 四列 |
| `method_ablate` | 3 个新鲜度 × 2 负载 × 2 网络 × 10 种子 = **120 cells × 11 臂** | **【V3 方法】阈值消融（第一版规则）**（一次只动一个参数，见 `METHOD_ABLATE_ARMS`）：`tau_q∈{1,2,8}`、`tau_n_dyn∈{5,30}`、`net_share_floor∈{0.05,0.20}`、`drift_gate=on` + `primary` 与两条基线 |
| `method_ch` | 与 `method` **逐字段相同**的格子 × 3 臂 = 720 次仿真 | **【V3 方法·修订版】按通道降权**（`decay="channel"`）。格子与 `method` 相同 ⇒ 两批的请求流**逐位相同**（已用 `np.array_equal` 逐格核对完成时间数组），因此可**跨批按 (条件, 种子) 配对**；`analyze` §19 就是把两批并起来读的。坏批/旧批按 append-only 保留，取最新 run_id |
| `baselines` | 2 个新鲜度 × 2 负载 × 2 网络 × 10 种子 = **80 cells × 8 臂** | **补强基线**：`Random`/`LeastQueue`/`LeastLoad` + `P2C`（经典二选一）/`P2CNet`（二选一+网络项）/`CongestionOnly`（Aegis 式拥塞项）/`Bottleneck`（Helix 式瓶颈最小化）/`NetworkAware2`。分析在 `analyze.py` §22 |
| `method_ch_ablate` | 3 个新鲜度 × 2 负载 × 2 网络 × 10 种子 = **120 cells × 9 臂** | **【V3 方法·修订版】阈值消融**（见 `METHOD_CH_ABLATE_ARMS`）：`tau_q∈{2,8}`、`tau_n∈{20,120}`、`link_floor∈{0.5,0.9}` + `primary` 与两条基线。**注意**：这两个键必须在 `make_strategy()` 白名单里，否则会**静默失效**（已加 `raise` 防护） |

⚠️ **`--mode` 的 `choices` 与 `analyze.MODES` 必须同步**：新增 mode 要同时改
`run_matrix.build_matrix()` 的分支、argparse 的 `choices`、`analyze.MODES` 与 `analyze.MODE_KEYS`，
否则产物会被分析脚本**静默忽略**（不会报错，只是那一节显示"没有产物"）。

**实测成本**（20000 请求/次，1.60 s/次/策略）：整套 6 个基础 mode 在 Azure trace 上共约 **47 min 仿真 + 6 次 trace 解析（每次约 81 s）**；BurstGPT 的 trace 小得多（50 MB / 1.4M 行，解析约 5 s），同样规模约 12–15 min。合成负载 0.85 s/次，8 个 mode 合计约 30 min。**三批可以并行跑**（不同 `--out` 目录，互不干扰；本机 16 核、16 GB 内存，实测三批同时跑不换页）。

⚠️ **后台跑矩阵时不要用默认超时**：600 s 的默认超时会在 100/120 cells 处把进程杀掉，只留下没有 manifest 和 `p_holm` 的残片（已实际发生两次）。长任务要用无超时模式，或至少把超时设到 1800 s 以上。

产物（**增量落盘**，每个 cell 追加一次，进程中断也会留下已完成部分）：`results/matrix/{mode}_{时间戳}_runs.csv`（每个 cell×策略一行聚合指标）、`_paired.csv`（配对检验行）、`_manifest.json`。`--save-requests` 时另有 `results/matrix/requests/{run_id}/`。
manifest 与 `p_holm` 列只在**全部 cell 跑完后**写入/回填，因此中断的运行会缺少 manifest 和 `p_holm`。

### 入口⑤：结果汇总分析

```bash
.venv/Scripts/python.exe analysis/analyze.py
.venv/Scripts/python.exe analysis/analyze.py --matrix-dir results/matrix --table-dir results/analysis --fig-dir figures
```

实测约 6 s 跑完（当前数据量）。设计约束（写在文件头 docstring 里，改脚本要维持）：**容忍数据不完整**（缺 mode / paired / manifest / `p_holm` 都不崩，只在可用性报告里标出缺什么）、**零硬编码结论**（所有数字只来自 CSV/JSON）、**幂等**（可重复运行、覆盖输出、无手工中间步骤）、**不做数据挑选**（负结果与零结果照原样进表）。

它按 §0…§9 分节输出，并以 `Tee` 同时写 `results/analysis/report.txt`。
⚠️ **`report.txt` 与 `figures/` 是"读取时那一刻 matrix 产物"的快照**：某 mode 的矩阵还在追加写盘时跑 analyze，就会得到该 mode 的不完整结论（§0 可用性表会显示缺 manifest / 行数偏少）。引用报告数字前，先确认对应 mode 的 `_manifest.json` 已存在（它只在矩阵跑完后才写）。矩阵跑完后再跑一次 `analyze.py` 即可得到完整结果（幂等）。

| 节 | 内容 | 主要产物 |
|---|---|---|
| §0 / §0b | 数据可用性；**门 1 负载强度 rho**（`RHO_LO, RHO_HI = 0.3, 0.9`，越界组合被点名） | `data_availability.csv`、`load_intensity_rho.csv` |
| §1 | RQ1 收益边界（主矩阵，NA vs 三基线） | `rq1_effect_workload_network_staleness.csv`、`fig_rq1_dp95_bars.png`、`fig_rq1_dp95_heatmap{,_zoom}.png` |
| §2 | RQ2 陈旧度曲线（链路通道 vs 队列对照通道） | `rq2_staleness_curve.csv`、`rq2_channel_separation.csv`、`rq2_critical_staleness.csv`、`fig_rq2_p95_vs_network_staleness.png`、`fig_rq2_p95_vs_queue_staleness.png`、`fig_rq2_slo_vs_staleness.png` |
| §3 | RQ3 数据量边界（`datavolume`） | `rq3_datavolume_effect.csv`、`rq3_zero_crossings.csv`、`fig_rq3_net_share_vs_dp95.png` |
| §4 | RQ4 权重消融（含门 2 逐位验证） | `ablation_w0_vs_leastload.csv`、`ablation_effect_by_w_net.csv`、`fig_ablation_w_net.png` |
| §5 | RQ5 节点规模敏感性 | `rq5_scale_effect.csv`、`fig_rq5_scale_nodes.png` |
| §6 | RQ6 种子收敛性 | `convergence_variance.csv`、`convergence_curve.csv`、`convergence_k_summary.csv`、`fig_rq6_convergence_seeds.png` |
| §7 | RQ7 可行性/退化汇总（哪些组合四策略无区分度、哪些逐位相同） | `feasibility_degeneracy.csv`、`feasibility_pairwise_identity.csv`、`fig_rq7_degeneracy_heatmap.png` |
| §8 | 到达压力扫描（`rho` 模式）：Δp95 随 rho 的形状 + 收益消失的 rho 交点 | `rq8_rho_sweep_effect.csv`、`rq8_zero_crossings.csv`、`fig_rq8_rho_sweep.png` |
| §9 | 回退机制（`fallback` 模式）：回退 vs 不回退的逐请求配对 | `rq9_fallback_effect.csv`、`rq9_fallback_summary.csv`、`fig_rq9_fallback.png` |
| §10 / §11 | 墙钟耗时；关键发现汇总 | `timing_summary.csv` |
| §12 | **验证与保真度记录**：门/白盒/黑盒检查结果 + 参数来源档案（读 `*_gates.json` 与 `fidelity_anchors.csv`） | 报告内联展示，文件在分析目录 |
| §13 | **带宽敏感性**：Δp95 与 `net_share` 的关系、变号点 | `rq13_bandwidth_effect.csv`、`rq13_bandwidth_summary.csv`、`rq13_zero_crossings.csv`、`fig_rq13_bandwidth.png` |

⚠️ **代码里的 RQ 编号与论文的 RQ 编号不是一套。** `analyze.py` 内部分成 RQ1–RQ9（收益边界 / 陈旧度 / 数据量 / 权重消融 / 规模 / 收敛 / 退化 / 到达压力 / 回退）；`论文规划.md` §0.2 明确**只做一篇论文、只保留 RQ1（收益边界）与 RQ2（状态新鲜度与回退）**。写正文时不要把代码里的 RQ3–RQ9 当论文 RQ 引用，它们只是内部实验编号；其中 §8（到达压力）支撑论文 RQ1 的边界，§9（回退）支撑论文 RQ2 的第三条贡献。

阈值常量（`analyze.py` 顶部，改判定口径就改这里）：`DEGENERATE_TOL_PCT = 0.5`（p95 跨策略相对极差 < 0.5% 视为数值上无差别）、`CLIFFS_NEGLIGIBLE/SMALL/MEDIUM = 0.147/0.33/0.474`、`NEGLIGIBLE_DP95_PCT = 1.0`（|Δp95%| < 1% 且 |cliffs| < 0.147 才算可忽略——因为 18000 请求 × 5 种子会让 0.04% 的差异也"显著"）。

### 入口⑥：负载强度刻画（把 rho 拆成均值与瞬时）

```bash
.venv/Scripts/python.exe analysis/characterize_load.py --source trace \
  --trace data/raw/azure_code_1week.csv --table-dir results/analysis_trace
.venv/Scripts/python.exe analysis/characterize_load.py --source synthetic --table-dir results/analysis
```

**为什么必须有这个入口**：主矩阵把平均 rho 标定在 0.600，但 p95 能到服务时间的几百倍。
原因不是实现错误，而是 trace 的到达过程有强突发——按 10 s 窗口看，局部 rho 会超过 1
（瞬态过载），队列在突发期排不空。只报平均 rho 既解释不了尾部，也答不上审稿人一定会问的
"rho=0.6 为什么有几百倍的服务时间尾巴"。

它用**与矩阵完全相同**的负载构造代码（直接导入 `experiments/run_matrix.py`，不复制公式），
按 1 s / 10 s / 60 s 三个窗口输出局部 rho 的分位、最大值与"过载窗口占比"，
产物 `{table_dir}/load_burstiness.csv` 会被 `analyze.py` §0b 自动并入。
多窗口是必要的：标定会整体缩放时间轴，固定宽度窗口看到的突发量随 qps 变化，
只报一个窗口无法区分"真突发"与"窗口选得巧"。

### 入口⑦：门与实现验证（把四道门变成产物）

```bash
.venv/Scripts/python.exe analysis/verify_gates.py --batch synthetic
.venv/Scripts/python.exe analysis/verify_gates.py --batch azure
.venv/Scripts/python.exe analysis/verify_gates.py --batch burstgpt
```

**为什么需要它**：门 1–3 反反复复抓住过真 bug（cell 级 `w_net` 被策略工厂丢掉、观测年龄写成 min、回退阈值与年龄重合导致触发由浮点决定……），但早先这些检查是"需要时手写一段对比"，只存在于会话记录里、**不是产物**。这个脚本把它们固定成一个入口：

| 检查 | 内容 |
|---|---|
| 门 1 | 从各 mode 的 runs.csv **复算** rho（不依赖 analyze 的中间产物），统计越界行数与每个 mode 的 rho 区间 |
| 门 2（产物层） | ablation 模式里 `NetworkAware(w_net=0)` 与 `LeastLoad` 的 13 个指标逐位比较（12 个 cell） |
| 门 2（请求层） | 内存内跑同一 cell 的两种策略，比 `(node_id, finish)` 数组；并给"w_net=1 时有多少请求决策不同"作对照 |
| 门 3 | 同一配置同一负载跑两次，`(node_id, finish)` 必须完全一致 |
| 白盒 | 静态网络、单请求、无排队时 `finish == arrival + net_in + service + net_out`（要求绝对差 < 1e-9） |
| 黑盒 | p95 随 rho 单调不减；交付工作量/容量的量级合理性 |
| 产物完整性 | 每个 mode 的 runs+paired+manifest 是否齐全、paired 是否含 `p_holm` |

产物：`results/verification/<batch>_gates.{csv,json}`（含 UTC 时间戳、参数与每项实测值）。
⚠️ 白盒检查**必须在静态网络**上做：时变网络里回传时间用的是**回传时刻**的链路状态（调度时无法预知未来带宽），拿到达时刻的状态算期望值会差 1e-4 量级——那是模型语义不是 bug。

### 入口⑧：组件级实测（真实进程，验证模型的两个核心公式）

```bash
.venv/Scripts/python.exe analysis/measure_components.py all --out results/verification
.venv/Scripts/python.exe analysis/measure_components.py queue --servers 4 --rho 0.85 --tasks 600
```

**为什么需要它**：同类仿真论文（Vidur）会用真实系统给出误差百分比（典型 <5%、最高 12.65%）。本机**做不了集群实测**（没有 docker/kind/k3d，WSL 也没有发行版，见 §10 第 28 条），所以退一步做**组件级**实测，逐条给出误差：

| 组件 | 做法 | 实测结果（2026-09-22） |
|---|---|---|
| 传输时间 `bytes/带宽` | 真实 loopback socket + 软件限速器（令牌桶），3 档速率 × 4 种载荷 × 3 次 | **≥1 MB 载荷的最大误差 1.5%，中位 −0.04%**；<64 KB 的载荷限速器限不住（小于一个 chunk），不构成结论 |
| 排队与服务 | k 个**真实 CPU 进程**做服务端，泊松到达，**把实测的到达时刻与服务时间原样喂回 SimPy**，比较 sojourn 分布 | 三个负载点（实测 rho 0.55/0.80/0.88）：均值误差 +6.5%/+8.5%/+9.8%，p50 +2.0%/+5.5%/+8.5%，p95 +12.2%/+11.8%/+5.4%；p99 在低载时噪声大（+60.6%） |
| **服务时间对负载的依赖** | 空闲单发 vs 持续满载两种标定 | 空闲 83 ms → 持续满载 **162 ms（×1.95）**：这就是"槽位之间不争抢算力"这条简化的**实测误差**，仿真在饱和区把吞吐**高估约 2 倍** |

产物：`{out}/measured_{transfer,queue}.csv`、`measured_components.json`，并复制进各分析目录由 `analyze.py` §12 展示。

⚠️ **两个必须避开的坑**（都实际踩过，见 §10 第 29–30 条）：
1. **派发必须开放环**。单派发线程同步 send/recv 会被 worker 占满而阻塞，到达过程被自我限流（标称 rho=0.9 实测 1.155 却几乎不排队），再把这套被限流的到达喂给仿真就成了同义反复。现在每台服务器一个消费者线程、派发只入队。
2. **标定必须在持续负载下做**。空闲标定（83 ms）比持续满载（162 ms）低约 2 倍，用它算到达率会把实测 rho 推到 1.07/1.48/1.65（标称 0.6/0.85/0.95），标签直接失真。

### 入口⑨：实验数据清点 / 备份 / 校验

```bash
.venv/Scripts/python.exe backup_artifacts.py                 # 清点 + 备份到 D 盘 + 逐个校验
.venv/Scripts/python.exe backup_artifacts.py --verify-only   # 只校验已有备份
```

**为什么需要它**：仓库**没有任何 git 提交**，`manifest.code_version` 一直是 `"uncommitted"`，
代码没有任何版本记录；只备份 CSV 等于存了数字丢了程序。本入口把
`results/` + `figures*/` + `data/raw/` + **代码快照（`src`/`experiments`/`analysis`/`configs`/入口脚本）** + 关键文档
一起复制到**另一块盘**（默认 `D:/论文实验数据备份_<日期>/`），并对副本逐个重新哈希比对。

- 清单：`sim/results/ARTIFACTS.csv`（路径/大小/UTC 修改时间/sha256/是否仍在被写）
- 被矩阵正在追加写的文件标 `in_progress=True`，校验会报"大小不符"，属预期；矩阵收尾后再跑一次即可
- **清点自身会跳过敏清单 `ARTIFACTS.csv`**（它一边写一边算哈希，必然自相矛盾，第一版就踩过这个坑）

### 入口⑩：真实系统实测（WSL2 里的单节点 k3s）

**必须在 WSL 里跑，不是 Windows 侧**。首次安装顺序（只有第一次需要，之后 k3s 由 systemd 常驻）：

```bash
wsl -d Ubuntu -u root -- bash /mnt/e/代码项目文件/论文/sim/k8s_test/setup_wsl_k3s.sh
wsl -d Ubuntu -u root -- bash /mnt/e/代码项目文件/论文/sim/k8s_test/check_workers.sh
wsl -d Ubuntu -u root -- bash -lc "cd /mnt/e/代码项目文件/论文/sim && \
  REQUESTS=240 PILOT_QPS='1 2 4 8' QPS_LIST='4' TAG=k3s bash k8s_test/run_k3s_grid.sh"
```

已经装成的环境（2026-09-23）：Ubuntu 26.04 + k3s v1.31.5+k3s1 + containerd 1.7.23-k3s2，
内核 6.18.33.2-microsoft-standard-WSL2，cgroup v2，systemd 已启用，16 逻辑 CPU / 7.4 GiB。
镜像 `docker.io/simtest/base:1` 是**就地**用 `debootstrap` + `podman import` 造的（本机 Docker Hub 不通）。
3 个 worker Pod 参数各异：算力 4/2/1、带宽 100/500/1000 Mbps。

**这套东西的诚实边界（写正文必须照抄）**：单节点不是多机集群 → 只能比方向；
worker 的算力是**应用层抽象系数**（同一段忙等除以 `compute`），不是真实异构硬件、
也不是 GPU；worker 是线程化 HTTP 服务且并发不设上限，"排队"体现为 CPU 争抢而非固定槽位排队，
所以绝对延迟/吞吐不可引用。
**⚠️ 2026-10-06 起"带宽"这条要改**：传输已从"按带宽档位睡眠的假装传输"改成
**真实字节收发**（POST /work 的 body 就是载荷、响应体也是载荷、worker 逐块计数并回校验和），
并且**已经跑通 WSL2 network namespace + 内核 `tc` 的真限速路径**——
阶段 A 的四项通过条件全部达标（字节一致性、±10% 吞吐、`overlimits` 增量 > 0、单路 vs 多路可分）。
详见 `sim/k8s_test/STAGE_A.md`。老行为只在显式加 `--fake-transfer` 时出现，且只作对照臂。

**阶段 A 新增入口⑪**：`sim/k8s_test/measure_system.py transfer`（阶段 A 验证电池，五项检查逐项
给 PASS/FAIL，产物落 `sim/results/stage_a/transfer_<UTC>_battery.{csv,json}`，append-only）
+ `sim/k8s_test/setup_netns_tc.sh {up [速率Mbit]|down|show}`（重建"第二台主机"与双向 `tc` 限速）。
注意：**k3s 镜像里 bake 的是老协议副本**（`setup_wsl_k3s.sh` 拷贝进 `/opt/measure_system.py`），
要用新 client 打 Pod 必须先重跑 `setup_wsl_k3s.sh` 重建镜像并重启 Pod，否则老 worker 会把二进制
body 当 JSON 解析而报 500。

- 装机踩过的 8 个坑（每条都有真实报错）见 `sim/k8s_test/README.md` §6，新增脚本前先读
- 集群禁用了 `traefik/servicelb/metrics-server/local-storage/coredns`（都要从 Docker Hub 拉镜像），
  Pod 之间用 **Pod IP** 直连，客户端在 WSL 里跑
- 产物落在 `results/k8s_test/`：每请求 CSV + `*_workers.json`；汇总用
  `.venv/Scripts/python.exe k8s_test/summarize_real.py --dir results/k8s_test`（该脚本也只用标准库，
  因为要在 WSL 的系统 python3 上跑）

## 6. 测试与验证策略

**本项目没有测试框架，没有 `test_*.py`，没有 pytest 依赖。** 验证靠三条路：脚本自带的自检输出、门 1–5 的显式检查、以及逐位比较。
**现在这些检查有了可复现入口**：`analysis/verify_gates.py`（入口⑦）把门 1 / 门 2（产物层 + 请求层）/ 门 3 / 白盒（时间分解）/ 黑盒（单调性）/ 产物完整性一次性跑完，结果落在 `results/verification/{synthetic,azure,burstgpt}_gates.{csv,json}`——**引用"门通过了"时请引用这两份文件，不要引用会话里的临时输出**。三批当前均为 8/8 项通过。
新增自动化测试前仍请先与用户确认（项目没有既有测试约定）。

### 门 1：负载强度必须落在有效区间

```
rho = 单请求平均工作量 × 到达率 / 系统总容量
```

- `rho < 0.3`：系统几乎不排队，四策略差异极小。"网络感知没用"是假结论，真相是没跑到有压力的区间。
- `rho > 0.9`（`analyze.py` 口径）/ `> 0.95`（`run_min.py` 的警告口径）：过饱和，p95 只在测队列排空时间，是另一个问题，必须明确声明。
- 有效区间：`0.3 ≤ rho ≤ 0.9`。
- 检查点：`run_min.py` 会打印 rho；`analyze.py` §0b 输出 `load_intensity_rho.csv` 并点名越界组合（判定用跨种子的 **min/max**，不是均值）。
- 两条路径的到达率都按目标 rho 标定：`build_synthetic_cell_workload()` / `build_trace_cell_workload()`，且**必须按真正被仿真的那一段前缀标定**（trace 有昼夜突发，用整片均值标定会让高峰前缀冲到 rho>1，实测到 1.20）。
- **只报平均 rho 不够**：`analysis/characterize_load.py` 把 rho 拆成均值与瞬时（1/10/60 s 三个窗口的局部 rho 与过载窗口占比），产物 `load_burstiness.csv` 会被 §0b 自动并入。实测在平均 rho=0.600 下仍有 10–30% 的 10 s 窗口局部过载（rho>1），**尾部指标主要由它决定**——审稿人一定会问"rho=0.6 为什么 p95 是服务时间的几百倍"。
- **收益边界必须有一根到达压力轴**（`--mode rho`）：只在单一 rho=0.6 上量到的 Δp95 不能叫"边界"。
- **没报 rho 的策略对比结果，不许写进论文。**

### 门 2：消融必须是严格子集

`NetworkAware(w_net=0)` 与 `LeastLoad` 的每请求完成时间数组必须 `np.array_equal` 为真。检查点：`probe_rq1.py` 结尾逐场景逐数据量逐种子比较；`run_matrix.py --mode ablation` + `analyze.py` §4 输出 `ablation_w0_vs_leastload.csv`。

### 门 3：同种子可复现

同一配置、同一种子跑两次，每请求的 `(node_id, finish)` 序列必须完全一致。
**重要推论**：`LeastQueue`/`LeastLoad`/`NetworkAware` 是确定性策略，多种子对它们**不产生方差**——配对比较里真正的方差来源必须是**负载实现**（trace 切片、到达抖动），而不是策略内部随机数。若某策略在 N 个种子下结果完全相同，继续加种子是浪费算力。`Random` 用得上种子。
当前**没有自动化脚本**做门 3 检查，需要时手写对比。

### 门 5：回退机制的触发语义必须可验证

`NetworkAwareStrategy.fallback_staleness` 的判据是 `观测年龄 > 阈值`，而**观测年龄 = max(队列 lag, 链路 lag)**——策略看到的是两条通道里更旧的那个快照（`Observation.observed_at = min(t_q, t_n)`）。2026-09-22 这里踩过一个会静默产出错误结论的坑：把年龄当成 `min`，于是"阈值 45 s 也触发了回退"这种结论凭空出现（其实是 5 s 的年龄 > 0.25 s 阈值）。

检查方式（两件事都要做）：

1. **触发与年龄一致**：对每个（陈旧档 × 阈值）格子，比对"期望触发"与"实际 Δp95 是否为 0"。期望触发却 Δ=0、或期望空操作却 Δ≠0，都是 bug。
2. **阈值不能等于任何一档的年龄**：相等时触发与否由浮点误差决定（实测年龄与阈值都是 2.0 时，`now-(now-2.0)` 算出 `2.0000000000000004`，回退被意外触发，Δp95 一下变成 +105%）。当前三档年龄是 0 / 3 / 30 s，阈值取 0.5 / 1.5 / 5 / 45 s。
3. `fresh` 档是天然负对照：年龄 0 s，任何正阈值都不触发，四个变体应当与"不回退"**逐位相同**（Δp95 精确为 0，`p=1`、`cliffs=0`）。

### 门 4：模型假设与实测事实分离

每个参数都要能回答"它从哪来"（SKILL.md 的四类来源）。**下面是 2026-09-22 实际做过的保真度对照**，写正文时直接引用这一节：

| 模型量 | 代码取值 | 对照锚点（可复核的算术） | 结论与写法 |
|---|---|---|---|
| KV cache 传输量（`kv_cache` 档） | `bytes_per_context_token = 3.3e5`（330 KB/token） | 70B 级 GQA 模型的 KV cache：2(K,V) × 80 层 × 8 KV heads × 128 head_dim × 2 B(fp16) = **327,680 B/token ≈ 320 KiB** | 同一量级、相差约 3%，属于【工程估算】但算术可复核。**必须**在正文声明这是模型配置推算，不是实测 |
| `prompt_only` 档 | 4 B/token | 只传 token id，代表共置/同机场景 | 【工程估算】，用于给出边界曲线的另一侧 |
| `compute_rate` 绝对值 | 节点 0 = 16000 units/s，按 2 的幂递减 | **抽象算力单元，不是真实 GPU 吞吐**。只有节点间比例（1 : 1/2 : 1/4）有语义 | 正文只讨论相对异构性；禁止把 units/s 写成 tokens/s |
| `prefill_coef` / `decode_coef` | 1.0 / 4.0 | decode 每 token 的墙钟代价显著高于 prefill 每 token（decode 受显存带宽限制） | 方向正确；取 4 相对保守（真实差距常更大），会在 Threats 里声明 |
| 链路带宽 | `normal` 10 Gbps / `mid` 1 Gbps / `degraded` 100 Mbps | **MegaScale 的生产网络是 400 Gbps 端口的三层 CLOS 拓扑**（arXiv:2402.15627 全文，2026-09-22 核对；本地 PDF 在 `文献/02-数据中心网络/MegaScale-NSDI2024.pdf`） | 本模型比 AI 集群机内网络低 1–3 个数量级，对应**跨域/算力网络或被强竞争的链路**；这句场景声明必须写进正文，否则带宽取值会被质疑 |
| 单程时延 | 0.5 ms / 2 ms / 5 ms | 同 DC 内 RDMA 是 µs 量级 | **建模选择**（刻意取 ms 级刻画跨域通信），不依赖具体文献引用 |
| `staleness` 量级 | 队列 0/2 s、链路 0/30 s（回退档另加 3 s） | 队列状态在亚秒~秒级就失效；监控周期常 10–60 s | 【工程估算】；已在 `staleness` 模式里扫成曲线，不靠单点 |
| SLO 定义 | `slo_mode="relative"`，`slo_multiplier=3.0` | trace 不提供 deadline | 【实验假设】。注意它会在高网络占比的格子上饱和（见 §7 限制） |

**实现验证（Sargent V&V 的白盒 / 黑盒检查）**，2026-09-22 实测：

| 检查 | 做法 | 结果 |
|---|---|---|
| 白盒：时间分解精确性 | 只放 1 个请求（`max_requests=1`，无排队），比对 `finish` 与 `arrival + net_in + service + net_out` | **绝对差 0.000e+00**（两种策略、degraded + kv_cache 档）✓ |
| 黑盒：容量口径 | 过饱和（rho=1.2）下交付工作量 / 系统容量 | 0.898（<1 是因为 `makespan` 含尾部排空），未饱和时 0.27/0.54/0.80 分别对应 rho=0.3/0.6/0.9 ✓ |
| 黑盒：单调性 | `mixed` 负载 p95 随 rho 变化（0.30→0.90） | 1.40 → 2.93 → 8.07 → 13.03 → 17.89 s，单调不减 ✓ |
| 门 2 逐位验证 | `w_net=0` vs `LeastLoad`（合成、Azure、BurstGPT 三种负载构造下各验一遍） | 全部 `np.array_equal` = True ✓ |


按 §4 第 6 条的四类来源写法归档。已知工程估算：`prefill_coef=1.0`、`decode_coef=4.0`、`bytes_per_context_token`、`staleness` 量级、SLO 定义、`compute_rate` 绝对值。

### 我已实际跑过的验证（可作为基线证据）

| 命令 | 结果 |
|---|---|
| `.venv/Scripts/python.exe -c "import simpy, yaml, pandas, numpy, matplotlib, scipy"` | ✅ 全部可导入（numpy 2.5.3 / pandas 3.0.6 / simpy 4.1.2） |
| `.venv/Scripts/python.exe analysis/analyze.py` | ✅ 跑完，§0–§11 全部输出，无异常 |
| 内存内冒烟：300 请求、3 节点、`LeastLoad` vs `NetworkAware(w_net=0)` | ✅ 正常出指标；**`np.array_equal` = True**（门 2 在当前配置下成立） |
| 三种负载构造（合成 / Azure / BurstGPT）下各做一次门 2 逐位比较 | ✅ 全部 True |
| 门 1 标定复核：`build_*_cell_workload` 的实测 rho 与目标 rho 之差 | ✅ 合成 12/12、Azure 20/20、BurstGPT 20/20 组合的 rho 与目标一致（误差 < 0.005） |
| 门 5 触发语义：三档陈旧 × 四档阈值逐格比对"期望触发 / 实际 Δp95" | ✅ 全部一致（`fresh` 档 Δ 精确为 0） |

### 统计方法（`论文规划.md` §6.3，不要在结果小节临时改）

- 同一场景同一种子下的配对比较用 **Wilcoxon signed-rank**（`run_matrix.paired_test`，按 `req_id` 对齐）；非配对用 Mann–Whitney U。
- 多重比较用 **Holm-Bonferroni**（`run_matrix.holm_bonferroni`），族定义为 `(cell_id, candidate)`。
- 效应量用 **Cliff's delta**；必须同时呈现统计显著性与实际效应大小。
- 每个组合报告均值/中位数、离散度、95% 置信区间，不能只报最好的一次运行。
- 汇报格式强制五要素：**方向 + 效应量**、**条件**、**不确定性**、**适用范围**、以及**负结果/零结果**（NetworkAware 无显著差异、劣化、回退无效，三者都是有效发现，必须写进正文）。

### 已知的失效 / 退化情形（要在实验设计和正文里说明）

- 静态网络（`normal` / `degraded`）下，链路陈旧**精确无效**（Δ=0），因此 3 种网络条件里有 2 种的 stale 档是退化 cell。
- `normal` 网络下 `NetworkAware` 与 `LeastLoad` 经常逐位相同（网络无信号，是**负对照**，这是预期结果不是 bug）。
- **合成**主矩阵曾经有 6 个组合 rho 越界（`data_heavy` 1.832 = 过饱和，`low_latency` 0.054 = 几乎空闲），那批对比不可采信，根因是 `build_synthetic()` 给四类负载同一个 `target_qps=30` 而四类负载的每请求工作量差一个量级。**已修复**：两条路径现在共用同一套"按真正被仿真的前缀标定"的算法（`build_synthetic_cell_workload()` / `build_trace_cell_workload()`），三批共 64+64+46 个组合全部落在 `[0.3, 0.9]`（旧产物 `main_20260922T043007Z` 按约定保留但不引用）。

## 7. 当前进度（截至本文件写入时的仓库真实状态）

> ⚠️ **先读 `sim/RESUME.md`**：那里有 v2 批次被中断后的"哪些已完成 / 哪些要补跑 / 可复制的续跑命令"，
> 以及 v2 链路争用模型、多资源可行域标定、已验证的门这些继续工作必需的设计事实。

**已实现**：四策略；异构节点；SimPy 事件模型；静态 + 时变（OU）网络；staleness 双通道（队列/链路）；relative SLO；**8 种实验 mode**（`main / staleness / datavolume / convergence / ablation / scale / rho / fallback`）；**两条真实 trace 接入**（Azure 2024 与 BurstGPT，`--trace` 按表头自动识别）；**两条负载路径统一的 rho 标定**；`NetworkAware` 的自适应回退（`fallback_staleness` / `fallback_w_net`，已由 `--mode fallback` 扫过）；`analysis/characterize_load.py`（多时间窗突发性）；`analyze.py` 的 §0–§11 表图（含 §8 到达压力、§9 回退机制）。

**已跑的数据**：三个批次**各自独立落盘与分析**，任何数字都必须注明来自哪一批。

| 批次 | 矩阵目录 | 分析产物 | 状态 |
|---|---|---|---|
| 合成（`build_synthetic`） | `results/matrix/` | `results/analysis/` + `figures/` | ✅ **8/8 mode 完整** |
| Azure LLM trace 2024（code 子集） | `results/matrix_trace/` | `results/analysis_trace/` + `figures_trace/` | ✅ **8/8 mode 完整** |
| BurstGPT（KDD'25） | `results/matrix_burstgpt/` | `results/analysis_burstgpt/` + `figures_burstgpt/` | ✅ **8/8 mode 完整** |

当前 run_id（`analyze.py` 取每个 mode 的**最新**一个）：

| mode | 合成 | Azure | BurstGPT |
|---|---|---|---|
| `main` | `20260922T072905Z`（480 runs / 120 cells，5 seeds） | `20260922T051125Z`（480/120） | `20260922T072905Z`（480/120） |
| `staleness` | `20260922T070409Z`（384/96，3 seeds） | `20260922T061308Z`（384/96） | `20260922T074132Z`（384/96） |
| `datavolume` | `20260922T071244Z`（192/48） | `20260922T063040Z`（192/48） | `20260922T074827Z`（192/48） |
| `ablation` | `20260922T073848Z`（240/60） | `20260922T060452Z`（240/60） | `20260922T075033Z`（240/60） |
| `scale` | `20260922T071752Z`（72/18） | `20260922T063633Z`（72/18） | `20260922T075311Z`（72/18） |
| `convergence` | `20260922T071904Z`（80/20） | `20260922T064026Z`（80/20） | `20260922T075414Z`（80/20） |
| `rho` | `20260922T072107Z`（360/90） | `20260922T065021Z`（360/90） | `20260922T070612Z`（360/90） |
| `fallback` | `20260922T072531Z`（216/36，每 cell 6 策略） | `20260922T070751Z`（216/36） | `20260922T071412Z`（216/36） |

**门与实现验证的记录在 `results/verification/{synthetic,azure,burstgpt}_gates.{csv,json}`**（由 `analysis/verify_gates.py` 生成）：三批各 8/8 项通过——门 1 共 2024 行零越界、门 2 产物层 13/13 指标逐位相同 + 请求层 `(node_id, finish)` 逐位相同、门 3 可复现、白盒时间分解绝对差 0.000e+00、黑盒单调性、产物完整性。

**被取代 / 异常留下的产物（按约定不删，但引用时不要用）**：

- `results/matrix/main_20260922T043007Z`：合成主矩阵旧版（`target_qps=30` 时代），6 个组合 rho 越界，已被 `20260922T072905Z` 取代。
- `results/matrix/convergence_20260922T044529Z`：只有 7/20 cells，被后台 600 s 超时杀掉，无 manifest。
- `results/matrix_trace/main_20260922T050118Z`：Azure 主矩阵同样被 600 s 超时杀掉（102/120 cells），无 manifest / `p_holm`。
- `main_20260922T065021Z`（合成与 BurstGPT 各一份）：180 cells 版本，因为把回退实验专用的 `mild` 档泄漏进了主矩阵；已按 `论文规划.md` §6.1 的 2 档重跑（见 §10 第 21 条）。
- `results/matrix/ablation_20260922T071457Z`：`w_net` 网格静默失效的那一轮（所有 cell 都按 w_net=1.0 跑），已被 `20260922T073848Z` 取代（见 §10 第 24 条）。

**当前最重要的实证结论**（方向与条件在这里，**具体数字与置信区间一律看三份 `report.txt`**）：

1. **两批真实 trace 的结论方向不一致——这是当前最重要的发现。** Azure 批：NA 相对 `LeastLoad` 在 60/72 条里显著更优、3 条劣化（全部落在 `unstable`+`stale`）、`normal` 网络的 8 条逐位相同。**BurstGPT 批：只有 46/72 更优、17 条劣化，而且在 `degraded` 网络里也劣化**（`low_latency` 最严重）。"网络感知有收益"**不能跨 trace 外推**。
2. **收益随到达压力单调收窄，且在 BurstGPT 上会变号**：Azure/合成的 rho 扫描里 Δp95 从 rho=0.30 的 −60%~−77% 收窄到 rho=0.90 的 −8%~−32%，但**始终为负**；BurstGPT 的 `mixed/degraded` 在 **rho≈0.84** 处由负转正，`data_heavy/unstable` 在 0.66 处转正。
3. **`normal` 网络是稳定的负对照**：三批 × 五个 rho 档 × 两种新鲜度下，NA 与 `LeastLoad` 逐位相同，无例外。
4. **回退机制（`论文规划.md` §0.3 的第 3 条贡献）按当前设计基本无效甚至有害**：三批合计"有效 8 / 过度保守 22 / 无效 42"（`fresh` 档是空操作对照）。它在 `degraded` 这类**静态**网络里稳定把 p95 抬升 30%+（链路信息本来就没失真，回退等于自愿放弃有用信息），只在 `unstable`+`stale` 的少数格子上有小幅收益。**该贡献的写法必须改成"条件性回退"，或如实报告为失效结果**（§6.3 第 8 条允许后者）。
5. **SLO 违约率在高网络占比的格子上饱和**（四策略 miss rate 全 >0.98），那里只有 p95 有信息量；两个主指标会给出不同结论，必须都报。
6. **两条 staleness 通道被验证为可分离**（SKILL.md 要求的设计检查）：queue-lag 扫描下 `Random` 逐位不变、三个状态型策略都变；net-lag 扫描（队列 lag 固定在 1 s）下 `LeastQueue`/`LeastLoad`/`Random` 逐位不变、只有 NA 变。三批都成立。
7. **临界陈旧度差一个数量级**：`unstable` 网络上 NA 的收益消失点（Δp95 由负转正）实测为 **合成 5.8 s / Azure 44.2 s / BurstGPT 1.9 s**。"staleness 多大算过期"没有跨负载的通用答案，必须按负载报。
8. **数据量边界**：NA 与 `LeastLoad` 的 Δp95 零点在 `net_share` ≈ 0.083（Azure degraded）/ 0.0022（合成）/ 0.00034（BurstGPT）——网络占比低于约 0.1% 时两者没有区别。`datavolume` 只采 prompt_only 与 kv_cache 两档，零点落在两档之间。
9. **节点规模：收益在 `degraded` 上三批都为正但非单调**（4/8/16 节点：合成 −34%/−38%/−62%，Azure −27.5%/−1.7%/−35.4%，BurstGPT −0.6%/−18.5%/−37.2%），`normal` 在所有规模上逐位相同。
10. **5 个种子对 BurstGPT 不够**：`convergence` 的 k 切片显示 BurstGPT 的时间片间离散度远大于 Azure（20 片下 `LeastLoad` 的 Δp95 是 +10.6% ± 7.0，Azure 是 +1.3% ± 0.7）。写 BurstGPT 的结论必须给 k=20 与区间，不能拿 5 片当充分。
11. **带宽敏感性（§13）给出了保真度威胁的定量答案，三批同向**：保持 `degraded` 的 100:10:1 异构形状、只换链路绝对尺度后，Δp95 随 `net_share` 单调变化——机内档（100/200/800 Gbps，net_share 0.001–0.08）是 **+3.3% / +4.8% / +0.0%**（合成/Azure/BurstGPT，即收益消失或小幅劣化），到广域档（0.01/0.1/1 Gbps，net_share 0.21–0.30）才是 **−34.4% / −29.3% / −24.5%**。而真实机内 KV 传输占比是 1–5% 量级（DistServe：90 Gbps 才让开销"不可见"）。
    → **本课题的结论适用区间是"带宽受限/跨域"（算力网络），不是机内推理服务**。这句话必须进 Introduction 与 Threats，它是把"链路太慢"这个质疑转成边界贡献的关键证据。
    §13 还内建了一项自检：`wan_10g` 档与主矩阵 `degraded` 是同一组链路，合成批必须**逐位相同**（实测 24 行 ✓）；trace 批会跳过，因为时间片数随种子数变化（5 片 vs 3 片），同一 seed 落在不同时间片上，**这不是管道错误**。
12. **组件级实测给出了模型两条核心公式的误差（论文的验证章节可以直接引用）**：
    · 传输时间：真实 socket + 软件限速下，≥1 MB 载荷与 `bytes/带宽` 的最大误差 **1.5%**、中位 −0.04%；
    · 排队机制：k=4 个真实 CPU 进程在实测 rho 0.55/0.80/0.88 三点上，sojourn 的均值/p50/p95 与 SimPy 相差 **+2%~+12%**（p99 在低载时受 OS 抖动影响可达 +60%，样本量不足以分离）；
    · **服务时间随负载上升**：空闲 83 ms → 持续满载 162 ms（**×1.95**）。这是"槽位之间不争抢算力"这条简化的实测误差，**仿真在饱和区把吞吐高估约 2 倍**。
    → 这三条合起来，把原先"无任何实测"的空白变成了"组件级已验证 + 一条已量化的简化误差"。注意边界：loopback + 软件限速、CPU 而非 GPU、非集群，**不能**当成集群实测。
13. **竞争态势（2026-10-01 复核更新，直接决定论文怎么写）**：
    · **已正式发表**：**Aegis（SIGCOMM 2026）**——加速器集群按实时网络状态放置，
      把**遥测延迟扫到 60 秒**，结论是"收益照样成立"（p99 −27.3%、SLO 违约 −3.6×）。**这条推翻了
      "没人扫过陈旧遥测"的说法**，是本课题最重要的引用与最直接的对照。
    · **预印本**：**NetKV（arXiv:2606.03910，2026-06）**——同样做"网络感知的 decode 实例选择"，
      建模动态拥塞，有陈旧遥测的理论界与刷新扫描（结论同样"不敏感"），64-GPU fat-tree 仿真 + Mooncake trace。
    · **结论口径（必须照此写）**：❌ **禁止**写"首次/没人做过"任何与网络感知 LLM 调度相关的表述；
      ✅ 正确写法是"**已有两篇工作各自扫过网络侧遥测延迟、结论同为不敏感，但都只覆盖一条通道；
      本课题补上机器排队状态那条通道，给出两侧临界值，并解释为什么链路通道看起来耐旧**"。
      ——这个更正反而**加强**了论证：它解释了文献里"遥测延迟无所谓"为何反复出现
      （被测通道本来就耐旧：`rq16` 显示链路通道旧到 60 s 仍有约 −13%，而队列通道旧 5 s 只剩四分之一）。
    · 我们剩下可站住的三处差异（按强度）：① **分通道陈旧度**（含二维联合扫描与两侧临界值）；
      ② **观测聚合窗口**（"延迟让人反应慢、窗口让人看错"，两者是不同失真）；③ **算力-带宽排名相关性**
      （2026-10-01 检索：训练与推理场景**均无人**把它当解释变量）。
    · 已发表先例（不得声称首创）：CASSINI（NSDI'24）、VClos（Comput. Netw.'25）、Helix（ASPLOS'25）、
      GenSched（INFOCOM'26）、PSP（LCN'26，DCN 里的陈旧状态）、JITServe（NSDI'26，不完美的**请求**信息）。
    · 负结果已有跨领域同构证据可引：**Stale Does Not Mean Unsafe**（arXiv:2609.29522，按新鲜度的守卫
      拦住 92–95% 无害情况）、**Decode-Latency Feedback Prefill**（arXiv:2609.38386，控制器时间尺度与
      机器时间尺度不一致导致不泛化）。新增 12 篇 2025–2026 文献见 `文献/MANIFEST.md`，
    详细定位与本次复核（含 Aegis 全文级核对、检索渠道局限）见 `文献/08-竞争定位（2025-2026）.md` §8。

14. **【细变量②】观测的"聚合窗口"与"延迟(lag)"是两种不同的失真，实测结构如下（`--mode avewindow`，合成批 90 cells / 450 次仿真）**：
    真实监控报的是"过去 W 秒的平均"（Prometheus `avg_over_time(metric[30s])`），不是瞬时值。
    · **绝对值：窗口同时抬高双方。** 队列通道窗口 0→30 s，NA2 的 p95 从 16.0 s 涨到 23.1 s（+44%，
    `mixed/degraded`）、10.8→15.7 s（+46%，`mixed/unstable`）；LeastLoad 同样 +52%。**大家都变慢。**
    · **相对收益：静态网络上几乎不变**（`degraded` 的 Δp95 在 w=0/0.5/2/10/30 s 上稳定在 −67%～−73%），
    **但时变网络上被削掉约三分之一**：`mixed/unstable` 从 w=0 的 **−20.4%** 掉到 w=30 s 的 **−14.4%**；
    链路通道窗口同向（−22.4% → −15.0%）。
    · **即：平滑掉的不只是噪声，还有"某节点刚涌进一批活"这个尖峰**；`lag` 是让人反应慢，
      `window` 是让人**看错**——所以两者必须分开建模（`SimConfig.window_queue_s` / `window_network_s`，默认 0）。
    · 两条通道的窗口效应量级相近（不像 `lag` 那样差两个数量级），这是**与 `lag` 结论不同的一点**，照实写。

15. **【细变量①】两条通道年龄的二维联合扫描（`--mode staleness2d`，合成批，unstable 网络，mixed 负载，3 种子）**：
    这是**已发表文献里没有人做过的一格**——把"两种被观测对象各自的年龄"当成二维因子。
    Δp95%（负 = NetworkAware2 更优），行 = 排队消息年龄，列 = 网线消息年龄：

    | 队列\链路 | 0 s | 1 s | 15 s | 60 s |
    |---|---|---|---|---|
    | **0 s** | **−23** | −21 | −12 | −13 |
    | **1 s** | −17 | −13 | −8 | −12 |
    | **5 s** | **−6** | −3 | **−0** | −2 |

    三条读数：
    · **两个方向都单调收缩**：队列 0→5 s 把优势从 −23% 压到 −6%（约剩 1/4）；链路 0→60 s 压到 −13%（约剩 1/2）。
    · **不对称**：**"排队消息旧 5 秒" ≈ "网线消息旧 15–60 秒"的伤害** → 队列通道更娇气。
      这给出一条可写的判据雏形：两条通道**不能共用同一个年龄阈值**。
    · **次可加**：最坏格 (队列 5 s, 链路 15 s) 的优势**精确为 0**（p95 22.09 = 22.09，效应量 0.049，
      判定"无实质差异"）。两个方向同时陈旧不是乘性放大，而是**共享同一个失效机制 → 优势一起归零**。
    · 效应量同步衰减（Cliff's −0.267 → −0.05），p95 绝对值双方都涨 2.1 倍（10.39 → 22.09 s）。
    · 成本提示：**陈旧度越大单次仿真越慢**（旧状态 → 请求堆积 → 事件数暴增）：满网格 150 cells 要 ~41 分钟，
      收窄到 36 cells 只要 4.8 分钟。要铺开就得接受这个成本。
    · 边界（初版）；**已按计划铺开补齐**，见下表。

    **铺开后的跨批结果（合成 360 + Azure 360 + BurstGPT 360 次仿真，每批 72 cells）**——
    关键格是"都新鲜"(0,0)、"只队列旧"(5,0)、"只链路旧"(0,60)、"两者都旧"(5,60)：

    | 批次/负载 | 都新鲜 | 只队列旧 5 s | 只链路旧 60 s | 两者都旧 |
    |---|---|---|---|---|
    | 合成 / mixed | −23% | −6% | −13% | **−2%（归零）** |
    | 合成 / data_heavy | −25% | −17% | −20% | −11% |
    | Azure / mixed | −38% | −5% | −24% | **+1%（反向）** |
    | Azure / data_heavy | −36% | −3% | −25% | **+2%（反向）** |
    | BurstGPT / mixed | +3% | +36% | −1% | +15% |
    | BurstGPT / data_heavy | −10% | +24% | +12% | +43% |

    三条**跨批成立**的结论（写成正文的口径）：
    1. **排队消息的陈旧度是主导因素**：队列 0→5 s 让优势大幅收缩（合成 −23→−6；Azure −38→−5），
       而**网线消息即使旧到 60 秒，优势仍剩 −13%～−25%**（合成/Azure）。
       **两条通道的"耐旧度"不同，这个不对称在两批里都成立**（合成 data_heavy 上差异最弱，属例外，照实写）。
    2. **两者同时陈旧 ⇒ 优势归零（合成 → −2%）甚至反向（Azure → +1%/+2%）**。
       这是比"收益变小"更值得写的**失效模式**：不是变差一点点，而是**回到无差别/更差**。
    3. **BurstGPT 上优势本来就不存在**（表格整体为正，只有 (0,0) 的 data_heavy 是 −10%），
       所以"陈旧度吃掉优势"在它身上无从观察——这**再次说明结论不能跨 trace 外推**，
       也是当前最需要正面处理的负结果（不是噪声，是方向不同）。

    · 成本提示（重复一次，避免下次重复踩）：**陈旧度越大单次仿真越慢**——
      满网格 150 cells 要 ~41 分钟，收窄到 12 组合×2 负载 的 72 cells 才 8.8–14.2 分钟。

16. **【细变量③】"谁算得快"与"谁网线粗"的对齐度（`--mode align`，合成批 36 cells / 180 次仿真）**：
    这是**机制侧的解释变量**——回答"网络信息为什么可能有价值"。做法只是重排"哪台机器配哪条链路"，
    不改链路本身；一致比例由 `alignment_index()` 量化（anti=0.00 / rot1=0.67 / aligned=1.00）。
    **预测在跑之前就写死在 `build_matrix()` 注释里**：aligned（两个排名一致）收益≈0、anti（相反）最大。

    | 对齐档（一致比例） | mixed/degraded | mixed/unstable | data_heavy/degraded | data_heavy/unstable |
    |---|---|---|---|---|
    | **anti（0.00）** | **−71.7%** | −23.0% | **−67.0%** | −25.0% |
    | rot1（0.67） | −35.4% | −25.8% | −29.7% | −28.6% |
    | **aligned（1.00）** | **−24.1%** | −8.5% | **−2.2%** | −6.4% |

    三条读数：
    · **预测基本成立**：`data_heavy` 上收益从 −67%（相反）塌到 **−2.2%**（一致）；
      `mixed/unstable` 从 −23.0% 塌到 −8.5%。**"两个排名一致 ⇒ 网络信息几乎没有附加价值"得到证实。**
    · **一个诚实的例外**：`mixed/degraded` 在 aligned 下仍剩 **−24.1%**。原因可解释：即使**标称排名**一致，
      "哪条链路此刻压着多少字节"仍是**动态且与排名无关**的信息（快算力+快链路的那台此刻可能正好拥堵）。
      → 所以正确说法是"**对齐度解释了网络信息价值的大部分，但不是全部**"。
    · **配置比调度更重要（值得单列一句）**：同一负载/网络下，
      `aligned + NA2` 的 p95 = **6.44 s**，而 `anti + NA2` = 16.02 s（2.5×）、`anti + LeastLoad` = 56.56 s（**8.8×**）。
      也就是说 **"把机器和网线配对好"带来的提升大于"把调度算法换聪明"**——
      这给网络感知的收益划出了上界：它只能找回错误配对造成损失的一部分。
    · 边界：3 节点、单一链路档位家族；4/8/16 节点**还没验**（`scale` 模式下节点数变化本身就会改变配对结构）。

    **★ 统计版本（2026-09-29 补齐，三批 × 10 种子）**——"种子的作用是**独立的负载实现**"，
    所以统计的单位是**种子**，不是请求：`--seeds 1..10`（trace 模式下一个种子＝一个时间片），
    每格报"跨种子的均值 ± 95% CI（t 分布）"，并做**组间 Mann–Whitney**与**趋势 Spearman**。

    | 批次 | 对齐档 | mixed/degraded | mixed/unstable | data_heavy/degraded | data_heavy/unstable |
    |---|---|---|---|---|---|
    | 合成 | anti | −70.3±1.2 | −26.3±5.2 | −68.2±1.1 | −27.0±3.9 |
    | 合成 | aligned | −22.0±2.8 | −11.7±3.1 | −3.3±2.4 | −9.5±2.6 |
    | Azure | anti | −76.8±0.8 | −41.3±3.0 | −76.7±0.7 | −39.0±3.5 |
    | Azure | aligned | −33.4±1.4 | −21.4±2.3 | −22.4±1.7 | −21.9±3.4 |
    | BurstGPT | anti | −60.1±9.9 | −20.9±11.2 | −65.3±4.0 | −3.7±15.6 |
    | BurstGPT | aligned | −10.9±11.1 | −6.2±10.0 | −29.1±9.7 | −0.1±6.5 |

    组间检验（anti vs aligned，单位＝每个种子的 Δp95%）：**合成 4/4、Azure 4/4 全部 p ≤ 4.4e-4，
    且 Cliff's δ = 0.94–1.00（完全分离：anti 的每个种子都比 aligned 的每个种子更优）**。
    趋势检验（Δp95 ~ 对齐度）三批都显著为正：**ρ = +0.72（p=2.6e-20, n=120）/ +0.68（p=2.2e-34, n=240）
    / +0.36（p=7.2e-09, n=240）** → "**对齐度越高、网络信息的收益越小**"这条机制预测在统计上成立。
    **诚实的反例**：BurstGPT 有两格不显著（`data_heavy/unstable` p=0.91 δ=0.04；`mixed/unstable` p=0.12 δ=0.42），
    原因是**它在 anti 下本来就没有优势**（−3.7%±15.6）→ 正确写法是"优势本身不存在时，
    对齐度效应无从观察"，而不是"对齐度效应不成立"。BurstGPT 的区间普遍最宽（±10~18），
    与 `convergence` 那节的发现一致（它的时间片间波动远大于 Azure）。
    · 工程记录：10 片时 BurstGPT 的 data_heavy 类有一片只剩 7,667 个请求 < 12,000，
      程序按设计**直接报错而不是偷偷缩小请求数**；该批改为 `--requests 6000` 重跑（manifest 里有记录）。

17. **【细变量③ × 规模】对齐度在 4/8/16 节点上（`--mode alignscale`，合成批，450 次仿真，10 种子）**：
    问的是"对齐度效应是不是 3 节点的小样本巧合"。**结论：效应在规模上仍在，但强度衰减，
    而且我们那个"一致比例"标量在 4 台以上失效**——后者是本次最有价值的负面发现。

    | 节点数 | 一致比例(anti/rot1/aligned) | anti | rot1 | aligned | anti−aligned 差 | Mann-Whitney p | Cliff δ |
    |---|---|---|---|---|---|---|---|
    | 4 | 0.42 / 0.58 / 0.58 | −63.1±1.6 | −51.8±2.2 | −11.4±1.2 | **51.7** | 1.8e-4 | 1.00 |
    | 8 | 0.52 / 0.52 / 0.48 | −60.7±4.2 | −57.3±2.2 | −46.8±2.2 | **13.9** | 1.7e-3 | 0.84 |
    | 16 | 0.49 / 0.52 / 0.51 | −57.0±2.5 | −54.4±1.0 | −48.5±2.6 | **8.5** | 1.0e-3 | 0.88 |

    · **成立的部分**：三档规模上 anti 与 aligned 的差都显著（p ≤ 1.7e-3，δ = 0.84–1.00），
      所以"配对结构决定网络信息的价值"**不是 3 节点的巧合**。
    · **衰减**：差值从 4 台的 **51.7 个百分点**降到 16 台的 **8.5 个百分点**。可能的解释是
      **节点越多、调度器的回旋余地越大**（有更多替代节点可以绕过坏配对）。
      **但不能排除另一种解释**：节点一多，"算力阶梯(周期 4) × 链路斜坡"出现算力并列的对，
      一致比例能操纵到的范围本身在收窄（3 台 1.00 → 8 台 0.43 → 16 台 0.20）。
      **两种解释目前没分开**，要分开得改操纵方式（例如让算力阶梯不重复，使 index 在任何 n 都到 0/1）。
    · **★ 更正：上面那版"标量失效"是假象，根因是一个真 bug（已修，见下）**。
      修完后趋势检验**在每一档规模上都单调且极显著**（用新指标"算力排名与带宽排名的 Spearman ρ"）：

      | 节点数 | 趋势 ρ | p | n |
      |---|---|---|---|
      | 4 | **+0.94** | 6.1e-15 | 30 |
      | 8 | **+0.82** | 2.8e-08 | 30 |
      | 16 | **+0.78** | 3.2e-07 | 30 |
      | 合并 | +0.79 | 1.1e-20 | 90 |

      → **"排名相关越高、网络信息的收益越小"在 4/8/16 三档上都成立**，机制结论稳了。

    · **★ 第二次更正（2026-10-01）：上面那句"强度衰减"的说法要改。** 新增独立探针
      `analysis/measure_link_choice.py`（产物 `results/analysis_v2/link_choice_by_scale.csv`，
      3 档对齐 × 3/4/8/16 台 × 3 种子 = 72 行）直接测了那个替代解释：

      | LeastLoad（完全不看网络）挑中机器的**带宽排名**（1=最快） | 3 台 | 4 台 | 8 台 | 16 台 |
      |---|---|---|---|---|
      | 随机水平 (n+1)/2 | 2.0 | 2.5 | 4.5 | 8.5 |
      | anti 档实测 | 2.08 | 2.88 | 4.57 | 7.93 |
      | aligned 档实测 | 1.44 | 1.61 | 3.37 | 6.59 |

      · **anti 档**（＝真实部署形态）的偏离平均只有 0.28 名 → **不看网络的策略并没有"碰巧挑到好网线"**，
        而 anti 档的收益在三档规模上是 **−63.1% / −60.7% / −57.0%**，**基本不衰减**。
      · **aligned 档**：LeastLoad 自己就挑到了好网线（偏离 −0.56 → −1.91 名，随规模变大）。
        **"收益随规模变小"的来源在 aligned 这一档**——该档下网络信息本来就没有额外区分度。
      · 同时仍有**操纵范围收窄**的成分：可构造的 ρ 区间从 3 台的 ±1.00 收到 16 台的 ±0.24
        （算力按周期 4 循环、链路按节点数铺开，节点一多就造不出强对齐的配置）。
      · **结论改写**：不要写"对齐度效应随规模衰减"；要写**"anti 配置下的收益对规模稳健
        （−57%~−63%），规模变大时衰减的是 aligned 档的对照强度，而且它一半来自操纵范围收窄"**。
        要真正分离这两件事，得让算力取值脱离周期 4（留作未来工作）。

    · **bug 记录（值得单列，因为它是"静默错误"的典型）**：`build_matrix()` 里算"对齐度指标"时，
      `apply_alignment()` 需要 `cond["_n_nodes"]` 才能按节点数铺链路斜坡，但**那一处没传**——
      于是**落盘的指标是按"3 条链路循环"算的，而 `run_cell()` 里真正跑的是"按节点数铺的斜坡"**，
      两者根本不是同一个配置。症状是 n=8 时 anti/rot1 的指标完全相同、趋势检验符号反转；
      很容易被误读成"标量度量失效"这种科学结论。**修法**：新增唯一入口 `cell_nodes(cell)`，
      cell 构造与仿真都调它，从结构上杜绝不一致（同时把指标换成 Spearman ρ，并列值用平均秩处理）。
      **旧产物按 append-only 保留**，analyze 只取最新 run_id。

    · **仍然成立的限制**：差值随规模衰减（51.7 → 13.9 → 8.5 个百分点），且**两种解释未分离**——
      "节点越多调度回旋余地越大" vs "操纵范围本身在收窄"（ρ 的可用区间 2.0 → 0.98 → 0.49）。
      要分离得让算力阶梯不重复（使 ρ 在任何 n 都能到 ±1）。
      **★ 已用独立探针分离（2026-10-01）**：见上表——anti 档的偏离平均只有 0.28 名，
      即"回旋余地变大"**不成立**；衰减主要来自 aligned 档对照强度下降 + 操纵范围收窄。

18. **【V3 方法】"该信陈旧状态多少"——四条降权规则的对照与两次设计失败（`--mode method` /
    `method_ch` / `method_ablate` / `method_ch_ablate`，三批各 1200+ 次仿真，10 种子）**：

    【为什么要做】§16 把"两条通道的耐旧度"量出来了（队列 5 s 掉到 1/4，链路 60 s 几乎不掉），
    那就该有一个**判据**决定"看到的状态旧到什么程度时少信多少"。这里把四条规则与两个端点放在
    **同一个 cell 内**跑（逐请求配对）：
    `LeastLoad`（完全不信）/ `NetworkAware2`（完全信）/ `AdaptSplit`（第一版）/
    `AdaptChannel`（第二版）/ `AdaptLinear` / `AdaptHard`。

    · **第一版 `AdaptSplit` 被实测否掉（重要负结果）**：它按"链路信息内部的两块内容"分别降权
      （"忙闲"快速打折、"粗细"不打折）。实测在链路陈旧的格子里它把收益从 −76.7% 削到 −40.2%
      （Azure，链路旧 60 s），甚至掉到 ≈0（合成）。原因：**单独压低"拥塞惩罚"这一项，
      会让"粗网线 + 慢算力"那台机器重新被堆满**——和更早那次"把队列项归零"的失败是同一个坑，
      只是换到了更细的层级上。**教训：降权的层级要选在"信息源"（通道）上，不能选在"打分的某一项"上。**
    · **第二版 `AdaptChannel` 成立**：按通道降权，两个分量同权——
      `w = exp(−年龄_队列/4 s) × max(0.7, exp(−年龄_网线/60 s))`，参数由 §16 曲线定出。
      实测在链路陈旧的格子里保留 **90–99%** 的收益（Azure 链路旧 60 s：NA2 −76.7% 对 Channel −76.2%），
      而第一版与线性版只剩 0–40%。
    · **但"打折"本身只在一种情况下真的有用**：三批里唯一**稳健**的"完全信网络反而有害"
      的格子是 **BurstGPT 的 mixed/unstable（队列旧 5 s + 链路旧 60 s）**：
      中位数 +23.4%、均值 +32.5%，`AdaptChannel` 把它修到 ≈0（−0.1%）。
      其余格子（合成全部、Azure 大部分）**完全信任网络仍然最好**，打折只是白白丢掉 0–40% 的收益。
    · **没解释成功的地方（照实写）**：事先假设"负载越突发、排队消息过期越害人"，
      三批按 1 秒窗到达 CV 排序是 Azure 0.23–0.30 < 合成 0.60–0.65 < BurstGPT 1.30–2.11，
      但"收益被陈旧吃掉的幅度"是 Azure 最大（保留率约 21%）≈ BurstGPT（30%）> 合成（60%），
      **不单调** → 突发性（CV）不是那个决定变量。推测真正的变量是状态变化的**持续性/自相关**
      （Azure 有昼夜爬坡、BurstGPT 有持续几十秒的洪峰、合成的泊松噪声是瞬间的），**尚未测**。
    · **第二个解释也没成立（2026-10-01 补测）**：新增 `analysis/measure_persistence.py` 量了
      "速率变化的持续性"（`rate_change_5s`、`autocorr_1s`、`fano_1s`，三批都算过）：
      合成的 `autocorr_1s` **最高**（0.73–0.85）却**最不受伤**，Azure 最低（0.17–0.28）却伤得最重，
      **仍然不单调**。→ 三批之间"收益被陈旧吃掉多少"的差异**不是**由已测的三个负载属性解释的，
      写成明确的未解问题（候选：陈旧窗口与队列排空时间的**尺度匹配**，对应文献里
      Decode-Latency Feedback Prefill 那条"控制器时间尺度 ≠ 机器时间尺度"）。
      顺带纠正一个既有描述：**合成负载不是平稳泊松**——`generate_synthetic()` 用
      `burstiness=1.5` 的**慢变正弦调制**速率（周期 = n/8 个请求），所以它自带持续性。
    · **⚠️ 口径警告：必须同时报均值与中位数。** 真实 trace 不同时段的 p95 可以差 5–8 倍
      （实测单种子相对差最高 +265%），"逐种子相对差均值"会被极端种子主导：
      BurstGPT 的 `data_heavy/unstable (5,0)` 均值 +27.8% 而中位数只有 −0.5%（＝其实没差别）。
      两者符号不一致的格子必须在正文里写出来。三批的**稳健**口径是"10 个种子里有几个更好"：
      | 场景 | 合成 | Azure | BurstGPT |
      |---|---|---|---|
      | 静态网络 + 队列旧 5 s（NA2 vs LeastLoad 赢几个种子） | 10/10 | 10/10 | 9/10 |
      | 时变网络 + 队列旧 5 s | 6/10 | 4/10 | 5/10 |
      | 链路旧 60 s（队列新鲜） | 9–10/10 | 9–10/10 | 9–10/10 |
      → **只有"时变网络 + 队列旧"这一格需要打折**；其余格子打折纯粹在丢收益。
    · **§21 参数消融（第二版规则，合成批，120 cells × 9 臂）**：结论**不敏感**——
      网线时间常数 60→20/120 s：p95 平均变 +1.3%/−1.2%；权重下限 0.7→0.5/0.9：0.0%/−1.6%；
      队列时间常数 4→2/8 s：+6.3±8.4%（区间跨 0）/−3.2±3.9%。
      12 个条件里结论方向翻转 0–2 个 → **这三个数不是精调出来的**。
    · **★ 一个静默失效的 bug（本次真踩到，防护已加）**：`make_strategy()` 用**白名单**把 dict 规格里的
      键传给策略构造函数。我给 `method_ch_ablate` 的臂写了 `tau_n` / `link_floor` 两个键，
      **但白名单里没有它们** → 它们被**默默丢掉**，四档变体与主方法**逐位相同**，
      §21 的表里整整四行是"0.0±0.0"。**这是最容易骗过人的一种输出**：看起来像
      "参数完全不敏感"这个科学结论，其实是参数根本没生效。
      → 修复：白名单补全，且**白名单之外的键直接 `raise ValueError`**（不再静默丢弃）。
      照此办：**新增可调参数时，必须同时改 `make_strategy()` 的白名单**；
      验证办法是把阈值改到极端值，看 `w_stat_mean` / `describe()` 有没有变。
      **坏批次产物按 append-only 保留**（`method_ch_ablate_20261001T150916Z`、`_152552Z`），
      analyze 取最新 run_id，所以结论不受影响，但引用前要确认 run_id。

1. **三批结果互不可混用**：合成、Azure、BurstGPT 的 token 形状 / `mean_work` / 标定出的 qps 差一个到几个量级，任何数字都必须说明来自哪一批（报告分别是 `results/analysis*/report.txt`）。
2. **两条真实 trace 都只代表一类服务**：Azure 是 **code** 子集（一周、1680 万请求、`GeneratedTokens` p50=8 —— 输出极短），BurstGPT 是 ChatGPT/GPT-4 会话日志（输入短、输出长、**突发远强于 Azure**）。四类负载都是按 token 分位切出来的，不是原生负载标签。
3. 槽位之间**不争抢算力**（4 个槽位各按 `compute_rate` 满速推进，总吞吐线性叠加）→ **高估**算力；真实 GPU 并发会互相拖慢。
4. 链路**没有排队建模**（多请求共用一条链路不互相拖慢）→ **低估**高负载下的网络代价，报告的收益偏保守。
5. 没有 batch 合并效应；网络状态观测延迟是常数，未区分"估计误差"与"陈旧"；节点间链路独立，未建模共享上行/汇聚链路的相关性。
6. 没有 deadline/SLO 的原生来源，SLO 是实验假设；且它会在高网络占比的格子上饱和（见上面第 5 条结论）。
7. 参数里 `compute_rate` 绝对值、`prefill_coef` / `decode_coef`、带宽/时延档位都是【工程估算】；`bytes_per_context_token = 3.3e5` 有可复核算术支撑（70B GQA ≈ 320 KiB/token），其余必须按 §6 门 4 的对照表声明。
8. **观测周期的保真度（2026-09-23 补充核对，写 Threats 必用）**：必须把两种"慢"分开——
   **① 网络传一次要多久**：数据中心 RTT ≈ **12 µs**（`文献/02-数据中心网络/OSCAR-NSDI2026.pdf` 原文），
   跨地域几十毫秒、跨国几百毫秒；**所以"网络本身慢到几十秒"是错的**。
   **② 消息多久更新一次（采集/监控周期）**：这是"几十秒"的唯一来源，而且是**配置**不是物理极限。
   本仓库 PDF 原文里实测到的周期，跨度 **50 ms → 60 s**（差 3 个数量级）：
   | 周期 | 出处（均在 `文献/` 内，可 grep 复核） |
   |---|---|
   | 50 ms | TopoOpt（NSDI 2023） |
   | 100 ms | Sparrow（SOSP 2013） |
   | 10 s | CacheGen（SIGCOMM 2024）、PPD-Disaggregation（2026） |
   | **30 s** | **Mooncake（FAST 2025，Kimi 生产系统记录 KV 缓存状态）**、GORGO（2026） |
   | 60 s | Pollux（OSDI 2021） |
   → **"30 秒"不是极端假设，它就是生产系统里在用的值**；但**结论不能写成"30 秒会坏"**，
   因为"旧不旧"要与**请求处理时间**比：LLM 单请求 0.1–10 s，15 s 刷新 = 几十个请求的时间；
   而对分钟级的训练作业（CASSINI 那类），15 s 还很新——**这正是训练场景与推理场景结论不同的原因**。
   本项目的 `staleness` 网格（0/0.25/1/2/5/15/30/60 s）本来就是把它当参数扫，方向是对的。
   **已知偏差（必须声明）**：本项目把观测年龄建成**常数**，未建模 ①监控周期抖动与丢样、
   ②"过去 30 秒平均值"这类**聚合窗口**（把尖峰抹平可能比"旧"更伤人——这是一个可加的细变量）、
   ③不同子系统周期不同（K8s 指标 ~15 s、网卡计数器 ~1 s、云监控 ~60 s），目前只扫了两条通道。

**尚未开始 / 下一轮该做的**：

- BurstGPT 批缺 `staleness` / `datavolume` / `ablation` / `scale` / `convergence`（`ablation` 是门 2 在整轮上的正式验证点；跨 trace 结论目前只靠 `main`/`rho`/`fallback` 支撑）。
- 回退机制的**重新设计**：数据指向"只在时变网络 + 陈旧时才回退"，当前的"只看年龄"规则太粗。改规则必须作为新的独立实验，不能在看完结果后调参再报收益。
- ~~Kubernetes 小规模实测完全没开始~~ → **2026-09-23 已完成首轮**：WSL2 + 单节点 k3s +
  自建镜像，3 个异构 worker Pod，客户端用 Pod IP 直连。门 1 的实测版用
  `rho_cpu = 实测到达率 × 实测 CPU 服务时间 / 机群 CPU 配额`（qps 6/8 → 0.50/0.68，在区间内）。
  首轮结论：**网络占比大（38.9 MB，net_share 0.65）时带链路积压项的策略 p95 低 21%–45%（4/4 全胜）；
  占比小（0.6 MB，0.01–0.05）时无效甚至反向；状态陈旧 3 s 让收益从 −45% 收窄到 −21%**——
  与仿真的收益边界同向。**每格只有 1 次运行、无置信区间**，只能支持方向与量级。
  装机与测量工具的 10 个坑记在 `sim/k8s_test/README.md` §6/§7；下一轮可做：多节点（k3d 多节点需要
  Docker Hub 不通→改用多 WSL 发行版）、内核 `tc` 整形（需要 worker 真的发字节）、P2C/最大权重基线。
- 仓库没有任何 git 提交，`manifest.code_version` 仍是 `"uncommitted"`；投稿前必须提交一次，否则结论钉不到代码版本。

**产物清单**（均可由脚本重新生成）：
`results/{matrix,matrix_trace,matrix_burstgpt}/`：上表的 runs+paired+manifest，外加三种残片。
`results/analysis/`、`results/analysis_trace/`、`results/analysis_burstgpt/`：各含 `report.txt`（§0–§11）、`data_availability.csv`、`load_intensity_rho.csv`、`load_burstiness.csv`、`rq1_effect_*.csv`、`rq2_*`、`rq3_*`、`rq8_rho_sweep_effect.csv`、`rq8_zero_crossings.csv`、`rq9_fallback_effect.csv`、`rq9_fallback_summary.csv`、`ablation_*`、`rq5_*`、`rq6_*`、`feasibility_*`、`timing_summary.csv`（某个 mode 没跑时对应表为空或缺失，属正常）。
`figures/`、`figures_trace/`、`figures_burstgpt/`：各含该批的 RQ1 柱状/热图、退化热图，以及（若有数据）RQ2/RQ5/RQ6/RQ8/RQ9 的图；合成批另有 `sojourn_distribution.png`、`rq1_probe.png` 等早期产物。
`results/raw/`：早期 `synthetic_*` 与 `rq1probe_*` 的汇总/每请求/manifest（含被中断运行留下的 manifest，按约定不删除）。

### 7b.【2026-10-09 补记】线②真实跨云 LLM 服务（`sim/k8s_test/`，截至 10-09）

> 计划书在根目录 `真实跨云LLM服务研究计划书.md`（阶段 A→D 闸门）。这里只记状态；
> 判据与结论细节以 `k8s_test/` 下各自的报告/预注册为准。交接细节见 `sim/RESUME.md` §12。

**已完成**：阶段 A（两真机 tc 真限速）→ B0（vLLM 组件级）→ B1（跨云双 A10 拆分服务）→
4c 分歧诊断（**6/15、未通过，不改判**）→ 概率口径校准 → **B1_4C_PREREG_3 解释性诊断（10-08 完成）** →
**B1_4C_PREREG_4 收口验证（10-09 完成：六闸门 6/6 PASS，B2 放行，见 §7c 与 `k8s_test/B1_4C4_REPORT.md`）** →
**B2 同云性能基线（10-11 完成并验收）** → **C 跨云可行性试跑（10-11/12 完成并验收）** →
**真实云实验正式收口（10-12：D-mini 裁定不执行，列"计划但未执行"）**。
结案总账：`k8s_test/REAL_CLOUD_CLOSEOUT.md`（三层证据/可写不可写口径/速查数字/费用对账/异常总账）。

- **概率口径结论**：旧"21.7% 吐词不是第一名"是 raw/processed 两套分的口径差
  （raw 矛盾率 22.29%、processed 498/498 自洽、且非退化 one-hot 0/498），
  **不是 vLLM 贪婪解码违规**。raw 分不再用于 4c 诊断。
- **PREREG_3 结果**（描述性，不改判）：v2 题组 15 对请求 12 对逐字相同；q04 三遍都在第 5 个词
  分岔（" a" vs " an"），分岔处两词近并列（m=0.92，A 档）；决策余量 0.011–0.026 vs 同臂噪声
  地板中位 1e-5。E3 重复性两臂逐位相同。**"近并列翻转"仍只是待验证解释。**
- **B2 已放行**（2026-10-09 `B1_4C_PREREG_4` 六闸门 6/6 通过，依据
  `k8s_test/B1_4C4_REPORT.md` 与 `results/b1/b1_4c4_20261009T080426Z/decision.json`；
  此前"B2/C/D 不放行"的旧状态作废）。C/D 未开始，各有自己的前置（C 需第二家云）。

**产物入口**：`sim/k8s_test/B1_4C3_REPORT.md`（本轮记录）、
`sim/results/b1/b1_4c3_20261008T134807Z/`（run 全套 + E4 表）、`sim/results/logprob_calib/`（校准 + 校正版派生分析）。

**环境（10-09 状态）**：阿里云 cn-heyuan 两台 A10（`i-f8z4ggqwhyqz0r6lp1mj/mi`）已按
「节省停机模式」停机；磁盘保留（venv / 模型 / toy_proxy 都在）；**重启后公网 IP 可能变、
私网不变**（172.29.31.52 / .51）；SSH 参数在 `k8s_test/run_b1.sh` 顶部。云端实操的坑见 §10e。

### 7c.【2026-10-09 冻结计划】4c 收口方案与实验时间表（融合版，**PREREG_4 已执行**）

> 来源：`B1_4C_SOLUTION_PLAN.md` + 文献调研（vLLM 官方文档、arXiv:2506.09501、arXiv:2609.25624）
> 的融合结论，作者已认可方向。**状态（2026-10-10 更新）**：PREREG_4 已于 10-09 执行——
> 六闸门 6/6 通过、**B2 已放行**（依据 `k8s_test/B1_4C4_REPORT.md` 与
> `results/b1/b1_4c4_20261009T080426Z/decision.json`）；4c=6/15 不改判仍然有效。
> 旧"B2/C/D 不放行"表述作废；B2 采数协议另见 `k8s_test/B2_PREREG.md`。

**口径转向（一句话）**：4c 不再用"最终输出逐字相同"当**唯一**判据，改成三层验证 +
干预实验；但**旧 6/15 不改判**，"近并列翻转"仍是待验证解释；B2 的放行按 §9 六闸门
决定（已于 10-09 通过）。

**1. 三层验证（替代"只比整段文字"）**：

| 层 | 做什么 | 要点 |
|---|---|---|
| ① 生成参数写死 | 两臂同一份：`temperature=0, top_p=1, top_k=-1, repetition_penalty=1`，`--generation-config vllm`，关前缀缓存/投机解码 | ⚠️ **`top_k=1` 是陷阱**：会让处理后分数退化成 one-hot，"top-20 比较"直接做不成。贪心本来就取最高分，不需要 top_k=1。评分一律用 **processed 分**（raw 分已废，见 §7b） |
| ② 固定前缀下一词测试 | 先由对照臂生成完整序列；把"prompt+第 k 步前缀"喂给两臂，每次只生成 1 个词，比较下一词评分分布（top-20 重合度、同词分差） | 这是核心武器：避免"一个词的差 → 越走越远"；直接检验"KV 传过去后，相同前缀下下一步判断是否一致"（即 teacher forcing） |
| 三类判定 | 见下表 | 判据是"**翻词那步两个词本来差多少**"（并列翻转阈值，tie-flip threshold） |

| 类型 | 判断方式 | 结论 |
|---|---|---|
| 明显一致 | 两边选出同一个词 | 通过 |
| 并列翻转 | 翻词那步**两词差距 < 跨路径数值差上界**（10 条校准题实测，不拍脑袋） | 记"执行路径敏感"，不算 KV 错误 |
| 明显不一致 | 两词差距远大于数值差却仍翻；或本地重算/传输失败/KV 字节异常 | 真错误 → 查 KV/精度，停止性能实验 |

⚠️ **判据为什么这样写（重要，别改回去）**：E2 实测跨臂分布差 0.01–0.03 > 同臂噪声中位 1e-5，
所以"近似并列"**不能**定义成"分布差 ≤ 同臂噪声"——那样 q04 会被误判成"明显不一致"。
正确问法是"翻词处两个候选词的差距（q04 实测 0.011–0.026）是否小于跨路径数值差"。
同机波动上界只作辅助证据与校准，**不再当唯一分类线**；且**每种硬件各测一次**（4060 的不能判 A10）。

**2. 干预实验（把"数值抖动"从猜测变成因果证据，各约一次服务重启）**：

| 干预 | 做法 | 假设成立 ⇒ | 假设不成立 ⇒ |
|---|---|---|---|
| 批不变性 `VLLM_BATCH_INVARIANT=1`（官方开关：让计算顺序不随批排队方式变） | 两臂都开，重跑② | 分歧消失 | 分歧照旧 |
| 32 位精度 `--dtype float32`（噪声小几百倍；1.5B ≈ 6 GB 显存放得下） | 两臂都开，重跑② | 分歧消失 | 分歧照旧 |

- 可行性已核对：**vLLM 0.31.0 版本文档含批不变性**；Qwen2.5 系列在官方验证清单；
  硬件门槛 CC≥8.0，A10=8.6、本机 4060=8.9 都满足。
- 边界：beta 功能、开了变慢、**"拆分传输+批不变性"组合无文献验证过**，只能实测。
- 它**不属于**"新增概率解释路线"（不违反范围冻结）：就是"数值路径敏感"这条已有假设的干预臂。
  干预结果只作诊断，**不进**正式性能实验。

**3. 文献锚点（写论文用；引用前逐篇复核）**：

- arXiv:2506.09501（2025）：greedy 解码跨硬件/批次不保证确定；根源浮点非结合性；近并列处翻词。
- arXiv:2609.25624（2026-09）：同上归因；"tie-flip threshold（并列翻转阈值）"一词出自此；固定归约顺序内核做到跨卡逐位相同。
- vLLM 官方 reproducibility + batch invariance 文档（0.31.0）；issue #9567 / #58899（**未重新在线核对**，引前必查）。
- DistServe（OSDI'24）/ Mooncake（FAST'25）：拆分式服务以首字等待/逐字间隔/goodput 为评价，**不用逐 token 一致当正确性判据**。

**4. 时间表（10-09 → 10-18 收口；只有两次上云机会）**：

| 日期 | 做什么 | 硬门槛 |
|---|---|---|
| 10-09 | 冻结范围；写 `B1_4C_PREREG_4.md` 草稿（判据用并列翻转版；生成参数按上表写死） | 草稿经作者审后冻结 |
| 10-10 | 本机最终验收 6 项：①重复运行稳定 ②服务起停重启正常 ③模拟传输端字节/次数/失败计数正确 ④固定前缀测试能比两路径下一词分布 ⑤旧 B1 数据重分析仍得 6/15 与 12/15 ⑥冻结参数干跑一遍出完整记录。**加：本机预演批不变性开关真存在、真生效（vLLM 须同 0.31.0）** | 6 项不全过 ⇒ **不租云** |
| 10-11 | 云端最后一次 4c（两台 A10，≤4h）：起服务→健康检查→10 条同机校准题→固定前缀测试→两干预臂→KV 指标→下载→节省停机。**不跑性能、不改代码、不加题组** | 优先级：固定前缀基线 > 批不变性 > fp32 |
| 10-12 | B2 同云性能基线（单机 vs 拆分，串行+低并发，TTFT/逐字间隔/总耗时/失败率/KV 量/费用） | 仅当 4c 达可接受口径 |
| 10-13~14 | C 跨云连通：A→B 与 B→A 双向 KV 传输，延迟/失败/传输量/费用 | 任一方向不稳 ⇒ 停，不进 D |
| 10-15~17 | D 正式比较：同云单机/同云拆分/跨云 A→B/跨云 B→A × 3 输入规模 × 2 并发 × ≥3 次重复；保留全部失败超时 | 组合固定，不临时加模型/云厂商/指标 |
| 10-18 | 关闭实验：记录齐全、缺失有因、机器全停、费用对账、结论分"通过/失败/未测/仅描述"四类 | 之后不再有"下一轮诊断" |

10-11 的四个结局（都有价值，负结果也算完成）：

| 结果 | 后续 |
|---|---|
| KV 正常 + 高余量位置一致 | 关闭 B1，进 B2 |
| KV 正常 + 只有并列翻转 | 记"执行路径敏感"，按冻结口径决定是否进 B2 |
| ★ 开批不变性/fp32 后分歧消失 | **因果实锤**：是数值路径不是 KV；4c 以"数值路径敏感"关闭 |
| 高余量位置明显分歧 / 指标异常 | 关闭真实服务线，负结果收口，不再租云排查 |

**5. 本机/云端分工铁律**：客户端格式、生成参数、服务起停、KV 字节核对、固定前缀分析、
失败恢复、B2/D 统计脚本**全部本机验通**；云端只验证真实性（配置哈希一致、真实指标增长、
真实云间网络、最终采数）。**上云后不改代码，只按冻结版本采数据。** 历史教训：阶段 A 模拟
工作进程、B0 缺 /metrics、停止命令/模型路径/解码端 IP 笔误、生成参数没写死、raw/processed
混用——这些全都该在本机抓住，详见 §10e。

**6. 范围冻结**：1.5B 跑完 B/C/D 全程（4c 验证的是"状态与服务逻辑保真"，不是规模效应）；
7B 仅作后续可选规模敏感性；不加新云厂商、新协议、新概率解释路线；不为正收益改阈值；
不把同云结果写成跨云结果；旧 4c 结果不再修改。

**7. 论文写法（英文段，按 §6/§8 学术口径）**：

> KV transfer was verified through receiver-side byte counters, transfer counts, and the absence
> of local prompt recomputation. Exact end-to-end token equality was not achieved in the initial
> test (6/15). All divergences occurred at near-tie decision points where the margin between
> competing tokens (0.011–0.026) was smaller than the measured cross-path numerical difference —
> the "tie-flip" regime known from floating-point non-associativity studies (Yuan et al., 2025;
> Cooper et al., 2026). A teacher-forced next-token evaluation under pinned generation parameters,
> with batch-invariant kernels as an intervention, distinguishes numerical path sensitivity from
> state-transfer inconsistency. Following DistServe and Mooncake, we report transfer integrity and
> distribution-level consistency as separate acceptance criteria rather than requiring token-exact
> equality.

**禁止写**："4c 已通过（因为 12/15 相同）"、"vLLM 贪婪解码失效"（已证伪，见 §7b）、
任何"逐字一致是拆分服务正确性唯一标准"的表述。

## 8. 代码风格与新增代码的约定

- **注释与 docstring 一律中文**，重点解释**"为什么这么建模"和"这个假设从哪来"**，而不是复述代码。`src/model.py`、`src/network.py`、`src/simulator.py` 的 docstring 就是范本：每个简化都写明"这是【一阶简化】"以及它会让结果偏保守还是偏高。
- **标识符英文**：模块/函数/变量 `snake_case`，类 `PascalCase`，常量 `UPPER_SNAKE`。
- **类型标注 + `from __future__ import annotations`**，用 `X | None` 而不是 `Optional[X]`。
- **dataclass** 用于配置与数据模型（`SimConfig`、`Request`、`NodeSpec`、`NodeView`、`LinkState`、`WorkloadStats`、`Observation`）；**纯函数**用于公式（`compute_work` / `service_time` / `network_time` / `request_bytes` / `compute_deadline`）。模块顶部保持 `from __future__ import annotations` → 标准库 → 第三方 → 本包 的导入顺序。
- **入口脚本统一骨架**（照抄现有脚本）：
  1. 模块 docstring 写用法 + 产物路径；
  2. `ROOT = Path(__file__).resolve().parent`（`experiments/`、`analysis/` 下是 `.parent.parent`）；
  3. `sys.path.insert(0, str(ROOT))`；
  4. UTF-8 `reconfigure` 循环；
  5. `matplotlib.use("Agg")`（必须在 `import matplotlib.pyplot` 之前）；
  6. `argparse`；
  7. 延迟导入本包模块（放在 `sys.path` 注入之后，配 `# noqa: E402`）；
  8. `run_id = f"{...}_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"`；
  9. `if __name__ == "__main__": raise SystemExit(main())`。
- **YAML 配置**（`configs/baseline.yaml`）中的数值必须标注来源：`【工程估算】` 之类；该文件在**主实验开始前冻结**，任何改动都要记录原因并单独成一次运行。
- **输出目录与命名**：`results/raw/`（每请求级 + summary + manifest）、`results/matrix/`（runs/paired/manifest）、`results/analysis/`（论文表格 + report.txt）、`figures/`（论文图）。文件名模式 `{run_id}_{kind}.{csv,json}`，`analyze.py` 用正则 `RUN_RE_TMPL` 解析，**改名会静默地让分析脚本找不到数据**。
- **新增一个实验 mode 要动三处**：`experiments/run_matrix.py` 的 `build_matrix()`（加分支 + 在 argparse `choices` 里加名字，并把新因子写进 cell）、`analysis/analyze.py` 的 `MODES` 列表 + 对应的 `section_*()` + `MODE_KEYS`（§7 退化表按它取键，漏了会 `KeyError`）。若新 mode 引入新的列（`target_rho` / `fallback_staleness`），记得在 manifest 里记下网格，并在 `analyze.load_mode` 里确认它不会被误转成字符串。
- **随机性一律显式传种子**：`np.random.default_rng(seed)` / `random.Random(seed)`，不要用全局 `np.random`。链路轨迹的种子派生是 `seed * 1000003 + node_id`，改动会改变所有时变网络结果。
- **增量落盘优于内存攒批**：`run_matrix.py` 曾因把结果全攒在内存、会话退出后整个矩阵无产物，现已改为每个 cell 追加写盘（注释里保留了这次事故记录）。新增长任务请沿用这个模式。

## 9. 安全、不可逆操作与红线

- **不要删除或改写已产出的实验数据**（`results/raw/`、`results/matrix/`、`results/matrix_trace/`、`results/analysis/`、`results/analysis_trace/`）。这些是 append-only 证据；异常运行只标注原因，不删除。
- **不要为了提高速度而牺牲负载真实性**：不要用固定 QPS 替代真实的突发到达过程，不要为了跑得快而把 rho 调进无压力的退化区间。若仿真太慢，按 `run_min.py` 打印的建议顺序调整：`workload.max_requests` → `workload.duration_s` → 减少 seeds（先 1 个测速）→ 最后才考虑并行化。
- **不要在门 1–4 未通过前启动 480 次主实验**（SKILL.md 明文规定）；默认顺序是：确认负载强度 → 确认实现正确性 → 跑主矩阵 → 消融/敏感性/收敛 → 最后才写数字。
- **不要"看完结果再调 `w_net` 并报收益"**：调参必须作为单独消融，权重在看结果前冻结。也不要用"训练/验证集"话术掩盖结果导向的参数选择。
- **不要把单节点 kind/k3d 结果包装成生产级集群性能**；不要把 `compute_rate` 说成真实 GPU 吞吐。
- **不要编造数字**。没有数据支持的结论写成待验证问题。文献引用未逐篇核对前不写成确定事实。
- **`文献/` 的 PDF 不进 git 对象库**（根 `.gitignore` 已排除）；确实要版本化请用 git-lfs。
- **大文件下载**：GitHub 直连在本机**不通**，一律走反代（`https://gh-proxy.com/<原始 URL>`，见 §5 入口② 和 §10 第 12–13 条）。断点续传用 `curl -L -C - --retry 5 --retry-all-errors -A "Mozilla/5.0"`，但 `-C -` **不能与 `-r` 同时用**。Borg/ClusterData2019 只能走 BigQuery，本机 `*.google.com` 不可达。
- 未经用户明确要求，不执行 `git commit` / `push` / `reset` / `rebase`。

### 10c. 两个"看起来没问题、其实静默出错"操作坑（2026-09-29）

1. **不要把 `analyze.py` 的输出接给 `head`/`sed` 截断**：管道提前关闭会让 analyze 收到 **SIGPIPE**
   在中途死掉，`results/analysis_v2/report.txt` 被**截断在某一节中间**（看起来像"这一节没内容"）。
   症状极像"代码 bug"，实际是操作问题。要看片段请**先重定向到文件再读**。
2. **`§12 验证与保真度记录` 读的是分析目录里的门记录副本**（`results/analysis_v2/*_gates.json`），
   不是 `results/verification/` 里的原件。只跑 `verify_gates.py --batch X`（默认写到 verification/）
   时，§12 会继续显示**上一次写进分析目录的旧记录**——本次就因此显示成"10/11、旧判据"，
   而磁盘原件其实已经是 11/11。**处置**：改成判据后要**两处都刷新**：
   `verify_gates.py --batch synthetic_v2 --out results/analysis_v2`。

### 10b. 仿真代码的两个新坑（2026-09-29，做"观测聚合窗口"时踩到）

1. **局部 `import` 会让同名变量在整个函数里变成"局部未赋值"**。`src/simulator.py` 的 `run()` 里
   原来有一句 `from .network import LinkState, StaticNetwork`（写在 `if trajectory is None:` 分支里），
   结果**整个函数**里的 `LinkState` 都被 Python 当成局部名，走"轨迹已给定"这条分支时直接抛
   `NameError: cannot access free variable 'LinkState'`。
   症状很有迷惑性：**只有新增的那条分支会炸**（链路窗口 > 0 时才走到），队列窗口那条分支不经过它，
   所以冒烟测试通过、正式跑 205/450 行才崩。**处置**：统一在模块顶部导入，函数内不再 import。
2. **观测窗口的平均值查询必须先做等价性核对再替换实现**。第一版 `avg_over` 是"遍历窗口内每条记录逐段累加"，
   30 秒窗口 × 20 QPS ≈ 600 条 × 3 节点 × 2 万请求 → 单次运行慢到 30 分钟以上。
   改成"前缀积分（cumulative integral）+ 两次二分查找"后快约 6 倍以上，**但必须证明与旧实现等价**：
   用 4000 个随机历史 × 随机窗口逐位比对（容差 1e-9），结果 0 处不一致才敢替换。
   这类"为了性能重写数值路径"的改动，不验证等价就会**静默改变所有窗口结果**。

## 10. 已知的坑（都真实发生过）

### 10d. WSL2 里做 `tc` 真限速的三个坑（2026-10-05 实测）

1. **同一台机器上两个 IP 之间的流量不走 `veth`**，于是完全绕过 `tc`。
   实测：设了 `tbf rate 25mbit`，测出来 **18827 Mbit/s**——看起来"限速没生效"，其实是流量没经过该设备。
   必须把一端放进 **network namespace**（`ip netns`）才会真正过 veth。
2. **WSL2 空闲会自动关机，`netns` / `veth` / `tc` 规则全部丢失**（不跨会话保留）。
   症状：下一步执行时报 `Cannot open network namespace "hostB": No such file or directory`。
   处置：把命令放进**同一个会话**，或先 `wsl -d Ubuntu -u root -- sleep 7200` 挂住。
3. **`modprobe sch_netem` 会报失败**，但 `netem`/`tbf` 实际**编译进内核**了，直接 `tc` 就能用。
   真正的判据不是 modprobe，而是 `tc qdisc add` 是否成功 + **实测吞吐是否等于设定值**。

**验收标准**：`tc -s qdisc show` 里 `overlimits` 不为 0，且实测吞吐 ≈ 设定带宽（±10%）。
本机验证值：设 25 Mbit → 实测 **24.0 Mbit/s**，`overlimits 17594`。

完整步骤见 `sim/k8s_test/MULTIHOST.md` §1（含方案 B/C 的多机组网、`tc` 异构、
跨机时钟同步要求、以及"多机之后新增了哪些仿真给不了的证据"）。



1. **Windows 控制台编码**：不 `reconfigure("utf-8")` 会中文乱码。
2. **`code_version` 是 `"uncommitted"`**：仓库无提交；`git status --porcelain` 在有版本时才用来数脏文件。
3. **git dubious ownership**：本机需要 `git -c safe.directory='E:/代码项目文件/论文' ...`。
4. **`scipy` 不在 requirements.txt**：新环境装完 requirements 后 `run_matrix.py` / `probe_rq1.py` 会 `ModuleNotFoundError`。
5. **Holm 校正必须按索引回填**：`run_matrix.py` 里用 `pd.Series` + `adj_series.loc[g.index]`，因为 `groupby` 会重排组顺序，按位置赋值会把校正值配到错误的行——**这类错误不报错，只静默产生错误 p 值**。
6. **矩阵还在跑或中途中断时，产物是不完整的**：会只有 `runs`+`paired` 而没有 `manifest` 和 `p_holm`（manifest 与 Holm 校正只在全部 cell 跑完后写）。`analyze.py` 会检测到 `p_holm` 缺失并回退为"本轮按同族定义重算"，同时在可用性报告（§0）里注明。本次探索期间 `staleness` 就处于过这种中间态。
7. **截断下载的 trace 最后一行不完整**：`load_azure_trace()` 会先剥掉疑似截断的尾行，再用 `engine="python", on_bad_lines="skip"` 跳坏行，并单独统计 `n_truncated_tail` / `n_dropped`。
8. **时间格式两版不同**：Azure 2024 版 `2024-05-10 00:00:00.009930+00:00`（带时区），2023 版 `2023-11-16 18:17:03.9799600`（无时区、7 位小数）。加载器用 `utc=True, format="mixed"`。
9. **`staleness` 在静态网络上精确无效**：不要把它当成"staleness 没用"的证据，要用 `unstable` 网络。
10. **只加种子不扰动负载是浪费算力**：确定性策略的种子间方差为零（见门 3 推论）。
11. **rho 容易被忽略**：合成主矩阵里 `data_heavy` 过饱和（1.83）、`low_latency` 空闲（0.054），这两类负载的参数必须按 rho 重新标定后才可引用其对比结论。
12. **`curl -C -` 与 `-r` 互斥**（curl 8.21 报错退出）：早期文档里"断点续传 + Range 截前 50 MB"那条命令两个参数一起用，实际**一个字节都下不下来**，而且因为 `-s` 静默还看不到错误。要用 Range 就别加 `-C -`。
13. **GitHub 直连不通时要换反代**：本机 `github.com:443` 连接超时（约 21 s 后失败），release 资产自然也拿不到。可用的反代是 `https://gh-proxy.com/<原始 GitHub URL>`（692 MB / 86 s）；`ghproxy.net` 支持 Range 但只有约 8 KB/s，`hub.gitmirror.com` / `gh.llkk.cc` 不可用。
14. **整片 trace 的到达率标定会让前缀越界**：`build_from_trace()` 把选中的**整片**时间跨度缩放到目标 qps，但仿真只跑前 `--requests` 个请求。trace 昼夜突发强，高峰片的前缀局部到达率远高于整片均值——实测第 2 片四类负载的 rho 都冲到 0.97–1.20（门 1 失守）。标定必须针对**真正被仿真的前缀**（`build_trace_cell_workload()` 已按此实现），修好后 20 个组合的 rho 精确落在 0.600。
15. **真实 trace 的 token 形状与合成负载差一个量级**：Azure code trace 的 `GeneratedTokens` p50 只有 8（合成负载是 10^4.5≈90），`ContextTokens` p50=1930。四类负载的 `mean_work` 因此从合成的 230–7874 变成 601–4109，标定出的 qps 也完全不同（16–137 QPS）。所以合成与 trace 的结论**不可混着引用**。
16. **观测年龄写成 `min` 而不是 `max`**（已实际发生）：`Observation.observed_at = min(t_q, t_n)` 取的是**更旧**的那个快照，所以年龄是 `max(队列 lag, 链路 lag)`。写成 `min` 之后，阈值 3 s 在年龄 5 s 的档位上"不该触发却触发了"，而且**不报错**——只是让"回退过头"的结论凭空出现。检查方式见门 5。
17. **回退阈值与观测年龄重合**（已实际发生）：判据是 `年龄 > 阈值`，两者相等时结果由浮点误差决定（`now-(now-2.0) = 2.0000000000000004`），同一个配置在语义上应该"不触发"却触发了，Δp95 从 0 跳到 +105%。当前三档年龄 0/3/30 s，阈值取 0.5/1.5/5/45 s，刻意错开。
18. **BurstGPT 的 `Timestamp` 是秒，不是毫秒**：按毫秒解读会把 61 天的数据当成 88 分钟。判别方法是看 24 h 折叠曲线有没有昼夜形态（按秒解读时白天/夜间 ≈ 2.7）。另外本仓库拿到的 `BurstGPT_1.csv` **没有 README 里提到的 `Session ID` / `Elapsed time`**，所以不能做会话结构分析，`load_burstgpt()` 也不返回这些字段。
19. **`analyze.py` 的负载来源靠三级回退判定**：manifest 的 `data_source` → runs.csv 的 `data_source` 列 → paired.csv 的 `workload_stats` 列（旧产物只有这一列，靠 `"SYNTHETIC"` 这个词区分）。改动 `SYNTHETIC_STATS_LABEL` 会静默改变来源判定；早先 §9 把"负载为合成负载"写死，导致 trace 结果的报告带着错误的免责声明。
20. **`--mode convergence` 的种子不走 `--seeds`**：`build_matrix` 里写死 `range(1, 21)`。所以 trace 模式下它会切成 20 个时间片（每个 16.8M/20 ≈ 84 万行），而 `--seeds` 对它无效。BurstGPT 只有 140 万行时，20 片会让每类负载切不出 2 万请求而**直接报错**（这是预期行为，不要改成静默缩小）。
21. **`mild` 陈旧档泄漏进主矩阵**（已实际发生）：为了回退实验在 `STALENESS` 里加了 `mild` 档之后，`build_matrix` 的 `main` 分支用的是 `STALENESS.keys()`，于是主矩阵**静默**从 120 cells 变成 180 cells（3 档新鲜度），与 `论文规划.md` §6.1 冻结的 480 次运行不一致，而且三批之间不再可比。现在 `main` 显式用 `MAIN_STALENESS_LEVELS = ["fresh", "stale"]`；新增档位时**只加进 cell 字典，不要加进 `STALENESS`**，或者照 `MAIN_STALENESS_LEVELS` 的写法显式列出每个 mode 用哪几档。
22. **`effect_table()` 的 `baselines` 参数曾经没被用上**：函数签名加了参数，内层循环却仍写 `for base in BASELINES`，于是回退那一节传入 `baselines=["NetworkAware"]` 完全无效、整节静默返回空（不报错）。同时 `section_fallback` 传进去的子集里没有 `LeastLoad`，两者叠加让 §9 输出"没有可用的配对结果"。**教训**：新增参数后必须检查所有使用点，且"整节输出空"要当成错误信号去查，不要当成"没有数据"。
23. **`paired.csv` 缺分组列会让整份报告崩**：`effect_table` 用 `group_cols` 去筛 paired 行，老产物的 paired.csv 没有 `w_net`（当年只写进 runs.csv），于是 `KeyError: 'w_net'`——异常发生在 §4 之后，**报告只写到 §4 就结束**，而用 `sed`/`tail` 看输出会把 traceback 截掉，看起来像"跑成功了"。现在两处都修了：effect_table 缺列时整组跳过（不退化成分组），§4 按 `cell_id` 从 runs 把 `w_net` 补回，run_matrix 也把 `w_net` 写进 paired 行。
24. **cell 级参数在策略工厂里被丢掉（最危险的一次）**：给 `make_strategy()` 加规格分派后，字符串分支写成 `strategy_factory(spec)(seed)`，漏了把 cell 级 `w_net` 传下去。后果：`--mode ablation` 的**整个 `w_net` 网格静默失效**（所有 cell 都按 w_net=1.0 跑），而 runs.csv 的 `w_net` 列仍然写着 0/0.5/1/1.5/2 —— **产物会谎报实际执行的参数**。抓住它的是门 2：`w_net=0` 与 `LeastLoad` 的逐位比较立刻变成 1/13。修复后 13/13。教训有两条：① 加参数时把"所有使用点"过一遍；② 门 2 这种"必须恒等的硬约束"是不可省略的哨兵，能抓住功能性偏差，而 p 值/效应量抓不住。
25. **门 2 的检查本身也可能写错**：`section_ablation` 里 `sub0 = runs[np.isclose(runs["w_net"], 0.0)]` 忘了限定 `strategy == "NetworkAware"`，而 `w_net` 是 cell 级参数（同 cell 的 Random/LeastQueue 行也带这个值），于是拿 Random 的行去和 `LeastLoad` 比，门 2 会**稳定误报"不通过 ✗ 实现可能有 bug"**。看到门 2 失败时先核对检查逻辑与产物语义，别急着改实现。
26. **`BANDWIDTH_PROFILES` 的链路顺序被写反过一次**（2026-09-22）：约定是 `links[0]` 配 `node 0`（16000 units/s，算力最强）并且**必须是那条最慢的链路**——"快算力+慢链路 vs 慢算力+快链路"这个权衡才是网络感知调度唯一有用武之地的地方。写反之后节点 0 在两个维度上都最强，权衡消失，整组带宽敏感性实验作废重跑。**内建交叉验证**：`wan_10g` 档用的链路与 `NETWORK_CONDITIONS["degraded"]` 逐位相同，因此 §13 会拿它与主矩阵的 degraded 单元对比并打印"逐位相同 ✓"；这个检查若报不一致，说明档位管道或顺序又错了。
27. **保真度核对依赖 `pypdf`**（与 `scipy` 一样不在 `requirements.txt` 里）：`文献/` 下的 PDF 必须先抽成文本才能检索，抽出的缓存放在 `data/processed/pdf_text/*.txt`，可以直接用 `Grep` 复核任何一条引用。当前已抽 12 篇（vLLM / DistServe / Splitwise / Vidur / Llumnix / Orca / MegaScale / IsNetworkBottleneck / DCQCN / TIMELY / OSCAR / Sparrow）；`文献/01-集群调度/` 下**没有** Omega 与 DynamoLLM 这两个文件（MANIFEST 里登记了但库中缺失）。
28. **本机做不了 Kubernetes 实测**：`docker` / `kubectl` / `kind` / `k3d` / `minikube` / `helm` / `podman` **全部未安装**，WSL 已启用但**没有任何 Linux 发行版**。因此论文第 7 章的 kind/k3d 趋势验证在当前环境下无法进行；`论文规划.md` §8.1 的降级条款适用（论文降级为"仿真 + 明确 Threats，不把容器部署写成已完成验证"）。要补这一步需要先装 Docker Desktop / WSL2 发行版（系统级改动，需用户授权）。**替代方案已落地**：组件级实测（`analysis/measure_components.py`，见 §5 入口⑧）给出了传输公式与排队机制的实测误差。
29. **排队实测的派发必须开放环**（已实际发生）：单派发线程同步 `send/recv` 会被 worker 占满而阻塞，到达过程被"回压"自我限流——标称 rho=0.9 实测到 1.155 却几乎不排队，而我又把被限流后的到达喂给 SimPy，两边当然一致，**这是同义反复而不是验证**。正确做法：每台服务器一个消费者线程负责阻塞收发，派发线程只按标称时刻入队后立即返回；并且把**实测的提交时刻**与服务时间一起喂给仿真，两侧共用同一到达过程。
30. **服务时间必须在持续负载下标定**（已实际发生）：同一份工作量，空闲单发 83 ms、持续满载 162 ms（×1.95）。用空闲值算到达率会把实测 rho 推到 1.07/1.48/1.65（标称 0.6/0.85/0.95），**rho 标签失真**。现在先让 k 个 worker 满载跑 2 s 热身、再统计稳态每任务耗时。这个 1.95 倍同时就是"槽位之间不争抢算力"这条简化的实测误差——仿真在饱和区把吞吐高估约 2 倍，**必须进 Threats to Validity**。
31. **v2 链路争用模型与 v1 带宽档位不兼容**（已实际发生）：给每个节点的链路加上 FIFO 管道后，链路容量成为硬约束。v1 的 0.1–10 Gbps 档位下 λ 被压到 0.91 QPS、rho_compute 只剩 0.03、p95 冲到 76 万秒——**系统变成纯网络容量瓶颈**。v2 必须配 25–400 Gbps（真实 AI 集群量级），并且到达率要按**两条资源里先饱和的那条**标定（`calibrate_target_rate`），否则 rho 这个门形同虚设。判据推广为 `max(rho_compute, rho_network) ≤ 0.9`。
32. **`run_cell` 的配对候选列表写死了策略标签**（已实际发生）：加了 `NetworkAware2` 之后，候选仍是 `("NetworkAware", "LeastQueue")`，于是 NA2 只当基线、`paired.csv` 里没有它的效应行，§14 的机制对比失去配对证据。**教训与第 22 条同源**：策略表一旦可配置，任何写死标签的地方都要跟着改成从策略表推导。
33. **`--net-set` 的编辑事故**：新增参数时把 `--save-requests` 当作锚点却没有写回，参数被静默删掉，直到 `main` 里访问 `args.save_requests` 才报 `AttributeError`。改 argparse 后要跑一次真实入口（不能只 `py_compile`）。
34. **v2 的传输时间漏掉链路单程时延**（已修）：`serve()` 里写成裸的 `字节数/带宽`，
   而 v1 用的是 `network_time()` = **时延 + 字节/带宽**。被白盒门精确抓出：
   差值 4.0e-6 s 正好等于 2×单程时延。量级上对结论无影响（v2 时延 1–2 µs vs 传输 18–292 ms，
   相对误差 < 1e-4），但这是实现 bug，且**只要改了传输公式就必须重跑白盒检查**。
35. **v1 的带宽扫描档位在 v2 下不成立**（已修）：`BANDWIDTH_PROFILES` 覆盖 0.01–800 Gbps，
   是"链路不排队"时代的设计；v2 加上链路排队 + 可行域标定后，慢档会把算力利用率压到
   **0.0023**（门 1 下界失守，210 项越界）。硬约束是：**~900 MB/请求下慢链路 < ~23 Gbps 时，
   "两条资源都达到 rho≥0.3"不可行**。现在 v2 用 `BANDWIDTH_PROFILES_V2`（慢端 25/50/100/200 Gbps），
   由 `active_bandwidth_profiles()` 按档位集合切换。
36. **`analyze.py` §0b 的分组键不含 `n_nodes`**（已修）：`scale` 模式的 4/8/16 节点被混在一起算
   `load_ratio`，把 0.600 算成 1.69 并误判 2 个格子"不可持续"（手算 λ/R = 0.600 精确）。
   **教训**：只要某个 mode 引入了新因子（这里是节点数），所有"按场景聚合"的分析都必须把它加进分组键——
   与第 22、32 条同源。
37. **备份脚本的两个自伤**：① `ROOT` 取成 `sim/` 而不是仓库根，于是去找 `sim/sim/...`、
   0 个文件却报"全部一致 ✓"（**空集天真地通过校验**——校验逻辑本身要能发现"清点为 0"这种异常）；
   ② 清单文件被自己包含进清单，写的时候算哈希，复制后大小就变 → 必然自相矛盾。
   现在 `backup_artifacts.py` 两者都处理了（`ROOT = parent.parent`、跳过 `ARTIFACTS.csv`）。
   **注意**：`E:\代码项目文件\论文\sim'` 与 `D:/...` 之间的备份必须**换盘**才有意义。

### 10e. 线②真实跨云（k8s_test / 云端实操）踩过的坑（2026-10-08，详见 `sim/k8s_test/B1_4C3_REPORT.md` §6）

1. **云端 vLLM 启动的 model 参数必须是本地目录路径**（`/root/b1/models/Qwen2.5-1.5B-Instruct`）；
   写 repo id 且 `HF_HUB_OFFLINE=1` 时会 `LocalEntryNotFoundError` 启动即死。
2. **`b1_remote_serve.sh` 停对照臂是 `stop baseline`（两个词）**；写成 `baseline` 会落到
   "未知子命令"分支——**错误输出非空，反而被"输出非空=停成功"的判据放行**：服务没死、
   显存被占，坑死后来的解码实例（实测只剩 2.95/23.55 GiB）。凡按输出判成败的地方，
   必须同时排除错误语义（现已加"含『未知子命令』不算成功"的护栏）。
3. **`start-decode` 末位参数必须是 B 机自己的内网 IP**（对端靠它回连）；传成 A 的 IP 会让
   nixl 握手线程 `ZMQError: Cannot assign requested address`——**服务装死**（进程活着、
   显存占着、健康检查永远不过），比直接崩溃难查。
4. **WSL→云端 ssh 会偶发"静默死亡"**：进程零输出、远端零痕迹、几秒后的下一次连接又正常
   （实测约 2/10）。处置规则："**日志里没有 `target=`（客户端没跑起来）⇒ 未产生测量 ⇒ 重试 ≤3 次；
   一旦有 `target=` ⇒ 已测量 ⇒ 绝不重试**"；stop/metrics 按"无输出=重试"，收尾兜底 stop-all。
   已写进 `run_prereg3_cloud.sh` 文件头。
5. **PowerShell 里跑 wsl 命令的转义坑**：`\"`、`$?` 会被 PowerShell 先吃掉；
   带嵌套引号的命令一律写成 `.sh` 文件再执行（本次因此误诊了两轮）。
6. **省钱的停法是「节省停机模式」**（普通停机继续收计算费）；节省停机会**释放固定公网 IP**
   （重启后公网 IP 可能变、私网不变）。验收用控制台状态，**不要用端口探测**——IP 释放后探测无意义。

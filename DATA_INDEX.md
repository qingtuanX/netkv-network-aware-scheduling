# 论文数据索引（DATA INDEX）

> 定稿：`paper/submission/paper_CN_final_redraw_single.docx`
> 整理日期：2026-10-29　│　机器可读清单：`sim/results/data_inventory.csv`

## 0. 一句话

论文里每一个数字，都只能来自 `sim/results/paper_numbers.txt`；它由三批矩阵 → 三个 `analysis_*_v2/` → 自动导出（620+ 行，按论文章节编号）。**没有一个是手打的。**

```
results/matrix*_v2        ← 原始实验（append-only，不删不改）
        ↓ analysis/analyze.py
results/analysis_*_v2/    ← 论文表格 + report.txt
        ↓ analysis/export_paper_numbers.py
results/paper_numbers.txt ← 正文数字的唯一来源
        ↓
paper/sections/*.tex  →  paper/submission/*.docx
```

## 1. 主实验批次（三源 × 两代模型）

| 批次 | 原始数据 | 分析产物 | 说明 |
|---|---|---|---|
| 合成 v2 | `results/matrix_v2/`（128 文件 / 14.3 MB / 46k 行） | `results/analysis_v2/`（41 CSV） | 主矩阵，链路 FIFO 争用 + NetworkAware2 |
| Azure v2 | `results/matrix_trace_v2/`（82 / 8.1 MB / 28k 行） | `results/analysis_trace_v2/`（37 CSV） | Azure LLM 推理 trace（code 子集） |
| BurstGPT v2 | `results/matrix_burstgpt_v2/`（94 / 9.4 MB / 32k 行） | `results/analysis_burstgpt_v2/`（37 CSV） | ChatGPT/GPT-4 对话 trace |
| 合成 v1 | `results/matrix/` | `results/analysis/` | 早期模型（链路不排队），仅对照，正文不引其结论 |
| Azure v1 | `results/matrix_trace/` | `results/analysis_trace/` | 同上 |
| BurstGPT v1 | `results/matrix_burstgpt/` | `results/analysis_burstgpt/` | 同上 |

## 2. 正文图 → 数据（逐张对应）

| 图 | 数据文件（三个 `analysis_*_v2/` 里同名） |
|---|---|
| 图 2 收益 vs 网络占比 | `rq13_bandwidth_effect.csv` |
| 图 3 对齐度分组柱 | `rq17_alignment_effect.csv` |
| 图 4 对齐度 × 规模 | `rq18_alignment_scale_effect.csv` + `link_choice_by_scale.csv` |
| 图 5 双通道陈旧网格 | `rq16_staleness2d_effect.csv` |
| 图 6 降权规则对比 | `rq19_decay_rule_effect.csv`（+ `rq19_decay_rule_verdict.csv`） |
| 图 7 单因素消融 | `rq21_channel_threshold_ablation.csv` |
| 图 8 真实集群六次重复 | `results/k8s_test/k3s_*.csv`（逐请求，重算 p95） |
| 图 9 真实云锚点 | `paper_numbers.txt` 的 `B2.descriptive.*` / `C.D1.*` / `C.D2.*` / `C.bg.*` |

## 3. 后面补上的实验（按加入时间）

单列的原因：它们不在最初的 8 个 mode 里，是看完结果、补审稿口径时陆续加的。目录都在 `sim/results/` 下。

| 补做什么 | 目录 | 产出 | 支撑论文哪里 |
|---|---|---|---|
| 对齐度扫描 | `matrix_v2` 的 align 批 | `rq17_alignment_effect.csv` | §5.3、图 3、表 4（−68.2% vs −3.3%） |
| 对齐度 × 规模 | 同上 | `rq18_alignment_scale_effect.csv`、`link_choice_by_scale.csv` | §5.3 后段、图 4 |
| 双通道陈旧二维网格 | `matrix_v2` 的 staleness2d 批 | `rq16_staleness2d_effect.csv` | §6、图 5 |
| 到达压力扫描（rho 轴） | 同上 | `rq8_rho_sweep_effect.csv`、`rq8_zero_crossings.csv` | §5 收益边界、Threats |
| 回退机制 | 同上 | `rq9_fallback_effect.csv`、`rq9_fallback_summary.csv` | RQ2 第三条贡献 |
| 带宽/保真度扫描 | 同上 | `rq13_bandwidth_effect.csv` | §5.2、图 2 |
| 观测聚合窗口 | 同上 | `rq15_obs_window_effect.csv` | §6.2（平均 ≠ 延迟） |
| 降权规则对照（method） | `matrix_v2` | `rq19_decay_rule_effect.csv` | §7、图 6 |
| 按通道降权修订（method_ch） | `matrix_eqU_method_ch_synthetic` 等 | 同上（跨批配对） | §7.2（保留 90–99%） |
| 阈值消融 | `matrix_v2` | `rq20_method_threshold_ablation.csv`、`rq21_channel_threshold_ablation.csv` | §7.4、图 7 |
| 补强基线（P2C / NetKV / Aegis 式） | `matrix_v2` | `rq22_baseline_effect.csv`、`rq22_baseline_verdict.csv` | §5.4 |
| link-only 降权检验 | `matrix_linkonly_{synthetic,azure,burstgpt}` | `rq14_mechanism_effect.csv` | §7.5 |
| 一致性/概率校准批 | `matrix_consL*`、`matrix_consP*` | `analysis_consL*`、`analysis_consP*` | 补充材料 S2/S3 |
| 等利用率对照 | `matrix_equtil*`、`matrix_eqU_*` | `analysis_equtil`、`analysis_rq2_cal` | 口径校准，正文不直接引 |
| 真实 k3s 六次重复 | `results/k8s_test/`（218 文件 / 17.5k 行） | 逐请求 CSV → 表 11、图 8 | §8.2 |
| 真实云 B2（同云拆分） | `results/b2/` | → `paper_numbers.txt` 的 `B2.*` | §8.4、表 12、图 9(a) |
| 真实云 C（跨云双向） | `results/c/` | → `C.D1.*`、`C.D2.*`、`C.bg.*` | §8.4、表 12、图 9 |
| 传输层阶段 A 验证 | `results/stage_a*/` | 验证电池 CSV | §8 方法可信度 |
| 组件级实测 | `results/b0/`、`results/verification/measured_*.csv` | `measured_transfer.csv`、`measured_queue_rho*.csv` | Threats、§4.5 |

## 4. 验证与保真度

| 东西 | 位置 |
|---|---|
| 四道门 + 白盒/黑盒 | `results/verification/{synthetic,azure,burstgpt}[_v2]_gates.{csv,json}` |
| 参数来源档案 | `results/verification/fidelity_anchors.csv` |
| 组件实测 | `results/verification/measured_*.csv` |
| 产物清点与哈希 | `results/ARTIFACTS.csv`（80 万行清单） |

## 5. 体积

| 目录 | 文件数 | 大小 |
|---|---|---|
| `sim/results` 全量 | — | 约 160 MB |
| 其中 `raw/`（早期每请求明细） | 28 | 41 MB |
| 三个 `matrix_*_v2` | 304 | 32 MB |
| 三个 `analysis_*_v2` | 115 | 4 MB |
| `k8s_test` | 218 | 2.3 MB |
| `b0/b1/b2/c` | — | 约 6 MB |

逐目录明细见 `sim/results/data_inventory.csv`（文件数、CSV 行数、大小、最新日期）。

## 6. 重跑命令（都在 `sim/` 下）

```bash
# 原始矩阵（三批，各自独立目录）
.venv/Scripts/python.exe experiments/run_matrix.py --mode main --source synthetic --out results/matrix_v2
.venv/Scripts/python.exe experiments/run_matrix.py --mode main --source trace --trace data/raw/azure_code_1week.csv --out results/matrix_trace_v2
.venv/Scripts/python.exe experiments/run_matrix.py --mode main --source trace --trace data/raw/burstgpt_1.csv --out results/matrix_burstgpt_v2

# 分析 → 表图 → 导出正文数字
.venv/Scripts/python.exe analysis/analyze.py --matrix-dir results/matrix_v2 --table-dir results/analysis_v2
.venv/Scripts/python.exe analysis/export_paper_numbers.py

# 门与验证
.venv/Scripts/python.exe analysis/verify_gates.py --batch synthetic
```

## 7. 备份

- 本地：`sim/backup_artifacts.py`（复制到另一块盘 + 逐个哈希校验）。
- GitHub：代码、分析 CSV、原始矩阵 CSV 进仓库；原始 trace（692 MB）与大压缩包不进（.gitignore 已排除，GitHub 单文件上限 100 MB）。
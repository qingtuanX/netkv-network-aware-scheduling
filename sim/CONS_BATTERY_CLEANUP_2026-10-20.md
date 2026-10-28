# 保守模型电池清理与重跑记录（2026-10-20）

## 1. 为什么删

2026-10-12 的"保守服务时间模型"电池（matrix_consL / matrix_consP）**全部没有生效**：
--service-slowdown 只被写进了 cell 字典和 runs.csv 的列，但 experiments/run_matrix.py 的
run_cell() 构造 SimConfig 时漏传，仿真器一直用默认的 "none"（理想服务时间模型）。

后果：约 8,600 次仿真按理想模型跑完，产物却在 service_slowdown 列声称 linear/pressure。
这些数据与既有理想模型结果**逐位相同**，没有任何新信息，且会让 M1 稳健性检验被误认为"已做"。

### 证据（2026-10-20 逐行比对，pandas）

| 批次 | cons 产物 | 旧理想模型产物 | 匹配行 | p50/p95/wait_p95 最大差 |
|---|---|---|---|---|
| 合成 | matrix_consL/main_20261012T084545Z | matrix_v2/main_20260922T194958Z | 600/600 | **0.0** |
| Azure | matrix_consL/main_20261012T124624Z | matrix_trace_v2/main_20260922T210019Z | 600/600 | **0.0** |
| BurstGPT | matrix_consL/main_20261012T135520Z | matrix_burstgpt_v2/main_20260922T195947Z | 600/600 | **0.0** |
| 合成 P | matrix_consP/main_20261012T143640Z | matrix_v2/main_20260922T194958Z | 600/600 | **0.0** |

## 2. 删除清单（用户明确要求删除白跑记录，属于 append-only 约定的显式例外）

| 路径 | 文件数 | 大小 |
|---|---:|---:|
| sim/results/matrix_consL | 57 | 5.86 MB |
| sim/results/matrix_consP | 20 | 2.16 MB |
| sim/results/matrix_consL_v1set_superseded | 35 | 2.18 MB |
| sim/results/analysis_consL_v1set_superseded | 14 | 0.27 MB |
| sim/results/cons_battery_20261012T064115Z.log | 1 | 237 B |
| sim/results/cons_battery_20261012T064500Z.log | 1 | 6,755 B |
| sim/results/cons_battery_20261012T084543Z.log | 1 | 4,266 B |
| sim/results/cons_battery_20261012T124614Z.log | 1 | 11,629 B |

合计 126 个文件、约 10.7 MB。删除前的清单与证据见上表；本记录保留。
（日志必须删：run_cons_battery.py 的断点续跑会扫描 cons_battery_*.log 并跳过"已完成"的组合，
留着旧日志会让新一轮直接跳过全部任务。）

## 3. 修复内容

1. sim/experiments/run_matrix.py 的 run_cell()：给 SimConfig 补上
   service_slowdown=str(cell.get("service_slowdown", "none"))。
2. sim/analysis/regtest_slowdown.py：新增 real_path_check()，不再直接构造 SimConfig，
   而是走真实 run_cell 路径，要求 p95(linear) > p95(none) 且 p95(pressure) >= p95(linear)。
   该检查已并入 selftest。
3. sim/experiments/run_cons_battery.py：启动前先跑 regtest_slowdown.py selftest 作为 preflight，
   不过就拒绝启动整批。
4. 验证：selftest 通过（none 1.864 s / linear 2.061 s / pressure 2.300 s）；
   默认关闭回归仍逐位一致；CLI 小规模冒烟（2000 请求 × 2 cells）确认
   linear 与 none 的 8/8 行 p50/p95 全部不同。

## 4. 重跑

    cd sim
    .venv/Scripts/python.exe experiments/run_cons_battery.py linear
    .venv/Scripts/python.exe experiments/run_cons_battery.py pressure

- 两个变体并行跑（各自独立目录/日志；只改服务时间倍率，不影响确定性）。
- 产物：results/matrix_consL/、results/matrix_consP/；每批 6 个 mode × 3 个负载源 = 18 次 mode-run。
- 跑完后：analysis/analyze.py --matrix-dir results/matrix_consL --table-dir results/analysis_consL
  （P 同理），再决定是否把 M1 小节写进正文。

## 5. 不受影响的证据

paper_numbers.txt 与三批既有分析（analysis_v2 / analysis_trace_v2 / analysis_burstgpt_v2）
均不引用 cons 产物（0 命中），删除它们不改变任何已写进论文的数字。

## 6. 2026-10-20 补记：三批混目录导致 analyze 只取最后一批

- 原驱动器把三批负载写进同一个 matrix_consL / matrix_consP；analyze.py 每个 mode 只取
  最新 run_id，因此首次生成的 analysis_consL / analysis_consP **实际只代表 BurstGPT**。
- 已按 manifest 的 trace_file 把产物拆成
  results/matrix_cons{L,P}_{synthetic,azure,burstgpt}/（原目录保留，未删任何证据），
  并生成 6 份按批次的分析报告：
    results/analysis_consL_synthetic / _azure / _burstgpt
    results/analysis_consP_synthetic / _azure / _burstgpt
  每份都含完整 6 个 mode（main / rho / align / alignscale / datavolume / bandwidth）。
- analysis_consL / analysis_consP 的 report.txt 保留但**不要引用**；各自目录下已放
  README_WARNING.md 说明它只代表 BurstGPT。
- 驱动器已改为按批输出（results/matrix_cons{L,P}_{src}/），以后不会重演。

### 保守模型下必须先声明的口径问题

标称 rho 仍是 0.6，但并发降速让实际利用率大幅上升（main 矩阵均值）：

| 批次 | 理想模型 | L 线性 | P 压力 |
|---|---:|---:|---:|
| 合成 | 0.475 | 0.726 | 0.735 |
| Azure | 0.458 | 0.716 | 0.725 |
| BurstGPT | 0.368 | 0.510 | 0.516 |

因此“保守模型下收益收缩”**有一部分来自实际负载变高**，不能全部归因于服务时间模型本身。
写 M1 小节时必须同时报这张利用率表，并说明这是同标称 rho、不同实际负载的比较。

# `_dev_superseded/`：B0 开发过程中的中间稿产物（**保留、不引用**）

> 记录时间：2026-10-06（UTC）
> 为什么有这个目录：`measure_serving.py` 在写出最终版之前，被真实运行过 10 次以上，
> 每一次都留下了成套的 CSV/JSON。按本仓库"原始产物 append-only、异常运行只标注原因不删除"
> 的纪律，它们**从这里移动到本目录、一个都没删**；但**引用 B0 的结论时不要用它们**。

## 权威产物在上一级目录

`results/b0/` 顶层只保留**最终代码**跑出来的那几份：

| 文件 | 说明 |
|---|---|
| `battery_20261006T172400328Z_1144-1_{battery.json,checks.csv}` | B0 验收电池（25 项检查：24 PASS / 1 INFO / 0 FAIL） |
| `serving_20261006T172400343Z_1144-2_*` | 电池臂 A `overlap0`（KV 传输不与前处理重叠） |
| `serving_20261006T172411288Z_1144-3_*` | 电池臂 B `overlap50`（KV 传输一半落在前处理尾部内） |
| `serving_...172422239Z_1144-4_*` … `...172427666Z_1144-8_*` | 5 个故障注入臂（http_500 / disconnect_mid_stream / truncate_clean / reset_mid_stream / hang） |
| `serving_20261006T172437218Z_22648-1_*` | **两进程模式**的自测：`fake-backend` 单独起，`client` 打它；含每 token 的 `tokens.csv` |

## 本目录里为什么会有这些中间稿（按发现的问题分类）

| 中间稿特征 | 当时的问题 | 后果 |
|---|---|---|
| 请求的 `error_class` 全是 `stream_broken`，`ValueError: too many values to unpack` | 分析代码把三元组 `(名字, 起, 止)` 当二元组解包 | 120/120 请求被误判成"服务端断流"——**假故障**，看的人会去追一个不存在的网络问题 |
| 失败请求全是 `internal_error`，`UnboundLocalError: t_end` | 用 `0.0` 当"流没读完"的哨兵，失败路径上 `t_end` 根本没赋值 | 失败请求又被叠加了一层自己的错误 |
| `Σ语义阶段 − 服务端窗口` 在重叠臂上小于预期 | 我自己把对照的期望值算错了（用容差推的，不是用配方推的） | 一条本应通过的对照判成 FAIL |
| `disconnect_mid_stream` 组 6/6 被判成**成功** | 假后端"断流"时规矩地写了分块终止符 ⇒ 对端看到的是**正常结束**的流 | 那条故障注入根本没在测断流 |
| `reset_mid_stream` 组 6/6 被判成 `timeout` | `socket.close()` 时 `makefile()` 出来的读写包装还持着 fd 引用，fd 没真关 | RST 没发出去，对端只能干等到超时 |
| summary 里 `cov_gap_total_s_max` 是 `null`、CSV 里却是 `0` | 空列表求和返回**整数** 0，而汇总按 `isinstance(x, float)` 过滤 | "恰好为 0 的正常值"被当成"缺数据" |

这些坑都写进了 `measure_serving.py` 的注释与 `B0_REPORT.md` §9，自检里也加了对应的负对照，
所以同样的错误不会静默重来。

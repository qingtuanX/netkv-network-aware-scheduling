# 真实云实验线 —— 最终结案报告（REAL_CLOUD_CLOSEOUT.md）

> 结案日期：2026-10-12。裁定：**B2 完成 / C 完成 / D 不执行（计划但未执行）/
> 真实云端实验正式收口**。本文件是整条真实云线（B0→B1→4c→B2→C）的归档总账，
> 论文写作阶段的引用入口。

## 1. 证据三层（论文的引用结构）

| 层 | 内容 | 统计性质 | 产物入口 |
|---|---|---|---|
| **仿真主线**（论文统计脊梁） | 三批负载（合成/Azure/BurstGPT）× 多模式矩阵 × 多种子 × 门检查；收益边界、失效区间、陈旧度、消融、带宽/数据量扫描 | 种子为独立单位，含 CI/效应量 | `sim/results/analysis*/report.txt`、`论文证据索引.md` |
| **B2 同云真实基线** | 800 条正式（4 配对轮 × 2 形态 × 2 档 × 5 题 × 10 重复）；拆分 vs 单机 | 轮为独立单位（n=4），逐轮配对差+范围 | `sim/results/b2/b2_20261011T081400Z/`、`k8s_test/B2_REPORT.md` |
| **C 跨云真实可行性** | 双向 KV 各 100 条；D1/D2；WireGuard 公网隧道 | 方向为单位（n=2），描述性 | `sim/results/c/b2_c_20261012T031500Z/`、`k8s_test/C_REPORT.md` |

## 2. 可写 / 不可写（作者核定口径）

**可写**：
> We demonstrate bidirectional KV transfer across two public clouds and characterize the
> resulting end-to-end overhead under a real two-cloud deployment. The measured overhead is
> dominated by pre-generation KV movement, while the token generation phase remains
> comparatively stable.

**不可写**："证明跨云代价随 KV 大小线性增长"、"适用于所有输入长度"、
"跨云部署普遍没有价值"、"跨云一定比同云差多少"、"真实生产环境已验证"。

**限制部分必写**：真实云端仅固定输入规模；仅 1.5B + A10；未做真实云端多输入扫描；
数据量扩展规律主要由仿真支持；D-mini 计划但未执行（不伪造其结果）。

## 3. 关键数字速查（写论文从这里取，再回源文件核）

- **B2**（同云拆分 vs 单机，4 轮配对中位差）：首字 +207%（串行）/+97%（并发4）；
  token 间隔 +1.3%/−1.5%；总耗时 +16.0%/+9.8%；KV 每轮 4.29GB/100 次/失败 0。
- **C Q1**（双向 KV 账目）：字节/token=28,799（±1% 判据内）、100=100、失败 0、
  本地重算 0；每方向 4.294GB。
- **C Q2**：200/200 完成，零失败，全 64 词。
- **C Q3**（描述性，vs B2）：串行首字 0.211→4.16/3.84s、总 1.111→5.26/4.78s；
  并发4 首字 0.280→15.09/14.87s；生成阶段 D2 接近同云、D1 慢 18–24%（不全归因于 KV）。
- **带宽实测**（走隧道）：A→T 8.6MB/s、T→A 1.7MB/s、T→B 11.7MB/s（不对称，进边界）。
- **背景图素材**（q01–q05 按题长分组，事后描述）：首字随输入线性增长，
  两方向斜率近似（+0.473/+0.472 秒/千字符）——**只作背景，不作结论**。

## 4. 费用对账（待作者填实际账单）

| 项 | 估算 | 实际（作者填） |
|---|---|---|
| 腾讯云 ins-iwl380xu（¥10.90/时 × ~3.5 时 + 流量 + 系统盘） | ≈¥40–45 | ¥____ |
| 阿里云 A/B（原实例 ~6 时 + 少量流量 + 带宽调至 100Mbps） | 原预算外 | ¥____ |
| 预算闸门 | 第二家云硬上限 ¥500，估用 ~9% | ✅/✗ |

## 5. 异常事件总账（全部零正式数据损失，append-only）

1. SSH 连接复用套接字无主机区分（B2 误路由 → R4 整轮重跑，误路由件归档 `*.misrouted.json`）；
2. UCX 网卡钉 eth0 跨云不可达（`NIXL_ERR_BACKEND` → `B1_UCX_NET_DEVICES=wg0`，工具零改动）；
3. 代理把 KV 失败包装成 200+空内容（→"零 token 即无效"闸门，v2.1 兜住 2 次）；
4. 阿里云公网带宽上限 ~10Mbps（→ 作者调 100Mbps，A→T 提速 9 倍；A 入向仍 ~10Mbps）；
5. 腾讯云原机释放（→ 裁定不做 D-mini，无需重建）；
6. 杂项：venv python 链接指向系统 3.10、t0 快照权限、T→B 密钥缺授权、sshd 限流断连——均当场修复。

## 6. 归档清单

- 数据：`sim/results/b2/b2_20261011T081400Z/`（63 文件 + decision.json）、
  `sim/results/c/b2_c_20261012T031500Z/`（14 文件 + decision.json）；
- 报告：`k8s_test/B2_REPORT.md`、`C_REPORT.md`、协议/手册/价格记录
  （B2_PREREG/B2_RUNBOOK/C_PREREG/C_RUNBOOK/C_PRICE_RECORD/D_MINI_PREREG）；
- 台账：`k8s_test/B1_4C_EXEC_LEDGER.md`（全程裁定与事件）；
- 全库备份：`backup_artifacts.py`（D 盘，逐文件哈希）。

## 7. 下一步

论文最终写作与图表整理（仿真主线出统计图表；B2/C 出真实锚点图与表）。
引用一律先取 `论文证据索引.md` 与本文件 §3 速查，再回源 CSV/JSON 核对。

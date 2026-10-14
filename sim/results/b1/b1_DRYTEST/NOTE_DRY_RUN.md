# 这里**不是**实验数据，是脚本自检的残留（保留原样，别误读）

## 它是什么

`sim/results/b1/b1_DRYTEST/` 是 **2026-10-07T13:24Z** 用

```bash
RUN_ID=b1_DRYTEST ATTEMPT=1 B1_DRY=1 bash sim/k8s_test/run_b1.sh {baseline,precheck,measure,compare,cleanup-check,stop}
```

跑的一次**干跑（dry run）**留下的目录。

## 为什么会有它

正式测量要等模型下载完（约 3 小时）才能开始。为了不在昂贵的时刻才发现脚本有 bug，
先在这条**不执行任何远端命令**的路径上把 `baseline → precheck → measure → compare →
cleanup-check → stop` 全流程走一遍。

它还顺手验证了一个真实问题：`cleanup_check` 里的"残留进程 / 端口 / 显存"三项检查
在**两台都还没起过服务**时能正常输出（A/B 都是"无 vllm 进程 / 端口未监听 / 显存 0 MiB"）。

## 处置

- **不删**（与记录纪律一致：产物只增不减）。
- 但 **`b1_DRYTEST` 绝不参与任何结论**：里面的 `arm_*.json` / `budget.txt` / `consistency_diff.txt`
  都是干跑的空壳，`consistency_diff.txt` 写的就是"无法比对：缺输入文件"。
- 真实产物只在 `${CURRENT_RUN_ID}/attempt{3,4,...}/` 下。
- 正式跑的时候用的是 `RUN_ID=b1_20261007T125547Z`，与这里无关。

## 干跑通过的项目（说明脚本这些地方是通的）

| 阶段 | 干跑结果 |
|---|---|
| `baseline` | 提示词生成 → 起单机服务 → 3 次重复臂 → 稳定性报告，流程完整 |
| `precheck` | 停对照臂 → 起 P/D → 等就绪 → 抓 t0 → 起代理（`/healthcheck`）→ netproof → 冒烟 → 抓 t1 → 直方图增量，流程完整 |
| `measure` | t0 快照 → `disagg_r1`（单请求）→ `disagg_r2..r3` → t1 快照 → 两端直方图增量 → 4b 证据汇总，流程完整 |
| `compare` | 缺输入时**正确报错并列出候选文件数**（而不是崩掉） |
| `cleanup-check` | 三项检查 + 公网不可达 + 私钥泄漏扫描，流程完整 |

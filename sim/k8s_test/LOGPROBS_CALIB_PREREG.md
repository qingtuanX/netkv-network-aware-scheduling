# 概率口径校准方案（预注册）——先分清"选词前的分"与"选词后的分"

> 冻结时间：**2026-10-08（UTC+8）**，写在**发出任何一条新请求之前**。
> 性质：**测量仪口径校准**，零云费用（两台云机器保持停机）。
> **不改** 4c 判定（6/15 不同、未通过）、**不改** B1 任何记录、**不改写** `B1_4C_PREREG_2.md`。
> 取代 `LOGPROBS_CHECK_PREREG.md`（该文件采数前作废，见其头部状态块；未采过任何数据）。
> 依据：作者 2026-10-08 的口径更正（raw vs processed）。

## 0. 术语（人话）

| 文中写法 | 人话 |
|---|---|
| 原始分 raw | 选词规则处理**之前**每个候选词的分（惩罚、温度、top_k/top_p 都还没施加） |
| 处理后分 processed | 选词规则处理**之后**的分；**贪婪解码选的是这份分的第一名** |
| 选词规则 | 除了客户端显式传的 `temperature=0`，还可能继承 `repetition_penalty` / `top_k` / `top_p` |
| 口径 | 同一个词的"分"有两种定义；比大小之前必须先确认比的是哪一种 |
| 矛盾步 | 实际吐出的词**不是**上报分布里分最高的那个步（此前被误读成"服务自相矛盾"） |

## 1. 一个更正（本方案的出发点）

上一轮把"吐出的词不是上报分布第一名"解释成**"服务自己矛盾"——这个解释不成立**：

1. vLLM 0.31.0 默认上报的是 **raw_logprobs（选词前的分）**；
   要拿到选词所用的处理后分，必须用 `--logprobs-mode processed_logprobs`（证据 §2-1/2-2）。
   **贪婪解码只保证"从处理后的分里选第一名"，不保证它是原始分里的第一名。**
2. B1 三条臂的启动日志明确记录：模型 `generation_config.json` 覆盖了默认采样参数
   `{'repetition_penalty': 1.1, 'temperature': 0.7, 'top_k': 20, 'top_p': 0.8}`（证据 §2-3）。
   客户端只显式把温度改成 0，**其他选词规则一条都没关**（证据 §2-4）。
   `repetition_penalty=1.1` 会改变第一名的归属——这足以解释"吐出的词≠原始分第一名"。
3. 因此 **21.7% 的现象是真的，但它是"两种口径相比"的产物**；目前**没有**证据表明
   vLLM 上报损坏或存在程序缺陷。此前三条排除（非采样随机、非数组错位、非两字段不同分布）
   都只排除了窄解释，也不构成"服务矛盾"的证据。

## 2. 证据（每条都能点开复核）

| # | 证据 | 内容 |
|---|---|---|
| 1 | 本机 vLLM **0.31.0** 源码 `vllm/config/model.py` 的 `logprobs_mode` 字段说明（原文） | "Raw means the values **before** applying any logit processors… Processed means the values **after** applying all processors, **including temperature and top_k/top_p**"；默认 `raw_logprobs`；四种模式 `raw_logprobs / processed_logprobs / raw_logits / processed_logits` |
| 2 | 同版本 `vllm/engine/arg_utils.py:970` | `--logprobs-mode` 是 `vllm serve` 的**启动参数**（模型级，不是请求级） |
| 3 | 云端 `sim/results/b1/b1_20261007T163708Z/attempt4/baseline_server.log`（约第 45 行） | "Default vLLM sampling parameters have been overridden by the model's `generation_config.json`: `{'repetition_penalty': 1.1, 'temperature': 0.7, 'top_k': 20, 'top_p': 0.8}`" |
| 4 | `sim/k8s_test/b1_client.py` 请求构造处 | 只显式传 `temperature=0.0`、`seed=0`、`max_tokens`、`return_token_ids`、`logprobs=N` |
| 5 | 本机模型 `generation_config.json`（revision `989aa798…` 快照） | 与云端逐字相同：`repetition_penalty=1.1, temperature=0.7, top_p=0.8, top_k=20` |

## 3. 一个问题（冻结）

> **Q：在同一组完整生成设置下（含继承的选词规则），`processed_logprobs` 返回的分里，
> 实际输出的词是不是每一步的第一名？**

## 4. 判据（冻结，采数后不得放宽、不得换口径）

- **C1（主判据）**：processed 臂上，**全部**步满足 `argmax(该步上报分布) == 服务吐出的词`。
  100% 命中才判"**口径解释成立**"（选词用的就是这份分）。
- **C2（对照分支，raw 臂）**：raw 臂统计同一检查的矛盾率（应能复现旧现象）：
  - raw 矛盾率 **> 0** 且 C1 通过 → 结论：**此前 21.7% 是 raw/processed 口径差**；
  - raw 矛盾率 **= 0** → 本地未复现旧现象，**不能**用本地校准解释云端现象，如实报告、另想办法；
  - C1 **不通过** → "口径解释"在本地不成立，如实报告（此时才轮到查别的解释）。
- **C3（测量自检）**：同臂 r2 vs r3 逐 token 逐位相同；不相同先修测量，**不解读** C1/C2。

**描述性输出（不产生判定，但必须报告）**：processed 分的"形态"——每步 top1/top2 的分值与
差距、是否退化成 one-hot（若退化，说明这份分虽口径正确但**不能**当诊断原料，留给下一步决策）。

## 5. 采数协议（冻结）

- **环境**：本机 RTX 4060 Laptop / WSL2 / vLLM **0.31.0**（与云端同版本）/ 同 revision 模型
  （`989aa7980e4cf806f80c7fef2b1adb7bc71aa306`，本机 `generation_config.json` 与云端逐字相同）。
- **启动参数**（与 B1 逐条相同）：`--enforce-eager --no-enable-prefix-caching
  --max-model-len 8192 --gpu-memory-utilization 0.85`，环境变量
  `HF_HOME=/root/hf-cache HF_HUB_OFFLINE=1 VLLM_USE_FLASHINFER_SAMPLER=0`。
  **两臂只差一个参数**：`--logprobs-mode raw_logprobs`（默认，等价于 B1）vs
  `--logprobs-mode processed_logprobs`。
- **请求参数**（与 B1 相同）：`temperature=0`、`seed=0`、`max_tokens=64`、
  `return_token_ids=true`、`logprobs=20`、`stream=false`。
- **提示词**：`b1_client.py gen-prompts --preset v1` 确定性生成的 5 条
  （`p01_rep8 … p05_rep24`，即产生旧现象的同一批输入），sha256 留档。
  用途**只限仪器口径校准**，不用于任何 4c 假设的验证（遵守裁定 6）。
- **每臂**：预热 1 遍（不进统计）+ 正式 3 遍，串行；顺序固定 **raw 臂先、processed 臂后**。
- **落盘**：两臂逐请求原始响应 JSONL（**原始分与处理后分各存各的，同一输入对照保存**）；
  另存"生效生成设置清单"＝`generation_config.json` 全文 + 请求显式参数 + 服务启动日志的
  覆盖警告行。**不只记录 temperature=0。**

## 6. 校准通过之后（现在只写分支，不预写内容）

- C1 通过 → **另立** `B1_4C_PREREG_3.md`：用处理后分（若形态可用）重新开展解释性诊断。
  **不改写** `B1_4C_PREREG_2.md`；**不改变**已有 4c 结论。
- 本方案**不回答**输出差异的原因、**不证明**差异由 KV 传输造成、**不改判**任何已有结果。
  概率口径校准最多解释"此前的 logprob 诊断为什么失效"，与"输出差异本身"是两件事。

## 7. 边界与不做

- 零云费用；两台云机器保持停机，本步骤**不重启**它们。
- 不追根因（不读采样器实现细节、不做最小复现、不写 bug 报告）；若现象顺手指向根因只记"观察"。
- 小样本（5 题 × 3 遍），不是统计推断。

## 8. 停止条件（触发就停）

| # | 条件 | 停下后做什么 |
|---|---|---|
| 1 | C3 不过（同臂重复不同） | 先修测量，不解读 C1/C2 |
| 2 | token_ids / token 文本对不上（长度不一致） | 停，报告"无法对账" |
| 3 | 出现新的静默错误信号 | 停，如实记录 |
| 4 | 同一前置检查连败 3 次 | 停，写清卡在哪一步（沿用 B1 的 S8） |

## 9. 产物（append-only）

`sim/results/logprob_calib/<run_id>/`：

| 文件 | 内容 |
|---|---|
| `prompts_v1.json` + sha256 | 冻结输入 |
| `arm_{raw,processed}_r{1,2,3}.jsonl` | 逐请求原始响应（含两套分） |
| `record.csv` | run_id / UTC 起止 / 退出码 / 步骤（失败不重跑顶替） |
| `settings_{raw,processed}.txt` | 生效生成设置清单 |
| `verdict.txt` + `verdict.json` | C1/C2/C3 判定 + 逐步明细 + 形态描述 |

派生分析（2026-10-09 校正 one-hot 口径后**另存**，不覆盖上表原件）：
`sim/results/logprob_calib/logprob_calib_20261008T122156Z_analysis_corrected/`
（见其 `ANALYSIS_CORRECTION.md`：原脚本把 logprob 差当成普通概率差统计 one-hot；
校正后 processed 的普通概率 top1−top2 差中位 0.3644、疑似 one-hot 0/498。
C1/C2/C3 判定不受该校正影响。）

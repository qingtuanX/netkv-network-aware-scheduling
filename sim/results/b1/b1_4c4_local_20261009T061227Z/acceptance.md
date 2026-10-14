# 本机验收记录（B1_4C_PREREG_4.md §2.1 / §11；作者 8 步顺序，前一项失败即停）

> 目录：`sim/results/b1/b1_4c4_local_20261009T061227Z/`
> 环境：WSL2 Ubuntu，RTX 4060 Laptop 8188 MiB，驱动 617.42；
> vLLM = `/root/vllm-env/bin/vllm`（**0.31.0**，与预注册冻结版本一致）；
> 模型 `Qwen/Qwen2.5-1.5B-Instruct`（HF 缓存 `/root/hf-cache`，离线加载）。
> 纪律：每步 PASS/FAIL 留证；失败即停；不改预注册判据，只修启动管道。

## 步骤 1：`--generation-config vllm` 是否被 vLLM 0.31.0 接受 —— **PASS**

证据（`vllm serve --help=generation-config` / `--help=dtype`，2026-10-09）：

- `--generation-config` 存在，接受 `vllm`。语义核实：
  - **默认值是 `auto` ＝ 从模型路径加载 generation config**；
    `vllm` ＝ 不加载、用 vLLM 默认生成配置。
  - ⇒ **B1 当年没传它 ⇒ 用的是模型文件里的生成配置**（"悄悄覆盖实验参数"的来源，
    PREREG_4 §2 要排除的正是它）。本条写进论文 Threats 的素材。
  - 另有 `--override-generation-config`（JSON 显式覆盖），备用，本轮不用。
- `--dtype` 合法值 = `{auto,bfloat16,float,float16,float32,half}`。
  ❌ **抓到管道 bug**：`b1_remote_serve.sh` 原写 `--dtype bf16`（不在合法值里），
  argparse 会拒绝、服务起不来。已修：`B1_DTYPE` 默认 `bfloat16`；
  `B1_4C_PREREG_4.md` §2/§2.1 同步**勘误**（只改拼写，语义仍是 bf16 精度，判据未动）。
  干预臂 I2 的 `--dtype float32` 本来就是合法值，不用动。

## 步骤 2：服务启动 / 停止 / 重启 —— **PASS**

- 启动 1：`start-baseline` 起在 8300，**5 s 就绪**，`/v1/models` 正常应答（含 `allow_logprobs: true`）。
  启动命令行（launch 回显，§2.1 证据）含 `--generation-config vllm --dtype bfloat16
  --no-enable-prefix-caching`。
- 停止：`stop baseline` → SIGTERM 进程组 → 已退出，显存归还（812 MiB 系统占用）。✓
- 重启：带 `B1_LOGPROBS_MODE=processed_logprobs` 重启，**20 s 就绪**，`/v1/models` 正常。✓
- **抓到的坑（记入 Threats 素材）**：第一次启动漏了 `B1_LOGPROBS_MODE` 环境变量，
  启动日志里就没有 logprobs_mode——而它的**默认值是 `raw_logprobs`**（被判不可用的口径）。
  这就是"预注册写了 ≠ 生效"的活例子：没有 §2.1 的留证要求，这会带病上云。

## 步骤 3：启动日志确认实际参数 —— **PASS**（evidence_start2.txt）

启动日志 `non-default args` 逐项核对：

| 要求 | 日志实际值 | 判定 |
|---|---|---|
| generation_config=vllm | `'generation_config': 'vllm'` | ✓ |
| dtype=bfloat16 | `'dtype': 'bfloat16'`（engine 侧 `torch.bfloat16`） | ✓ |
| logprobs_mode=processed_logprobs | `'logprobs_mode': 'processed_logprobs'` | ✓ |
| prefix_caching=False | `'enable_prefix_caching': False` + /metrics `enable_prefix_caching="False"` | ✓ |
| （附加）模型 revision | 快照目录 `989aa7980e4cf806f80c7fef2b1adb7bc71aa306` | ✓ 与冻结值一致 |
| （附加）vLLM 版本 | `/version` = `0.31.0` | ✓ 与冻结值一致 |

## 步骤 4：生成并校验 cal10 —— **PASS**

- Windows（venv python）与 WSL（系统 python3）各生成一次：
  **sha256 逐字节一致** = `CD75EDEEEDE584A747FF9FD99124B799086A22C4EE9E1CFCF263790F944912F6`
  （兑现"事后重跑与本轮文件逐字节相同"的承诺）。
- 校验：n=10 ✓；id = `c01_rep4 … c10_rep31` ✓；reps 阶梯 (4,7,10,13,16,19,22,25,28,31) ✓；
  模板首尾正确 ✓；长度递增（2159→16091 字符）✓；无 CRLF ✓。
- 产物：`cal10.json`（Windows 产物，正式用）+ `cal10_wsl.json`（跨平台一致性对照）。

## 步骤 5：写并本机运行 b1_prefix_4c.py —— **PASS**

- 新工具 `sim/k8s_test/b1_prefix_4c.py`（纯标准库）：`collect`（参考生成 + E3 自检 + 逐步回放）
  与 `analyze`（Δ_gap + A/B/C/D + 稳健性重判 + 样本量闸门）。
- `py_compile` 通过；本机实跑见步骤 6。

## 步骤 6：固定前文请求 / token 对账 / top-20 分布解析 —— **PASS**（smoke/ 目录）

对 `c01_rep4`（441 个 prompt token）× 8 步回放 × 两臂（本机自比对）实测：

| 检查 | 结果 |
|---|---|
| 固定前文请求（token-id 数组直接作 prompt） | ✓ 服务接受，`usage.prompt_tokens` 逐步对账 **8/8** |
| token 对账（前缀长度 = prompt_tokens 期望值） | ✓ 8/8（无文本回拼、无分词漂移） |
| top-20 分布解析 + 非退化检查 | ✓ 8/8 步有 ≥2 个非零候选（`top_k=-1` 真的生效） |
| E3 自检（同臂同前缀 2 次逐位相同） | ✓ identical=True |
| 参考臂一致（回放 top1 == 自由生成下一词） | ✓ 7/7（第 8 步是参考序列尽头，无比可比，如实记 None） |
| analyze 全链 | ✓ 自比对 8/8 全 A、Δ_gap=0.0（同服务预期值）；**样本量闸门正确触发**（8<30 ⇒ estimable=false） |

## 步骤 7：legacy 与显式参数两条路径不混淆 —— **PASS**

同一服务各发一条（`step7_legacy.json` / `step7_explicit.json`）：

- legacy：`{temperature 0.0, seed 0, stream false, return_token_ids true, logprobs 20}`
  ——与 B1 历史参数集逐字一致，**不带** top_p/top_k/repetition_penalty；
- explicit：上述 + `top_p 1.0, top_k -1, repetition_penalty 1.0`；
- 两条路径输出与 token_ids 一致（同服务同参数语义），`区分正确 = True`。

## 步骤 8：批不变性开关测试 —— **PASS（开关验证生效，I1 可作因果证据）**

前提核对：`VLLM_BATCH_INVARIANT` 在 vLLM 0.31.0 源码里注册（`envs.py` 默认 0，`=1` 开；
注意力后端与采样路径都有分支）——**支持属实**。

探针协议（PREREG_4 §6）：同一提示词（c01_rep4）单独跑 2 次 vs 混进 3 个并发伴跑里跑 2 次，
比较输出 token 与逐位置 top-20 processed 分布（交集内最大概率绝对差）。开关关/开各一遍：

| 对比 | 关开关（batchinv_off） | 开开关（batchinv_on） |
|---|---|---|
| 单跑 rep1 vs rep2 | 逐位相同（diff=0） | 逐位相同（diff=0） |
| 混批 rep1 vs rep2 | 逐位相同（diff=0） | 逐位相同（diff=0） |
| **单跑 vs 混批** | **diff=0.00688457，第 2 步起偏** | **diff=0.0，逐位相同** |

⇒ **混批敏感性被开关完全消除 ⇒ "真的生效"验证通过**（不是只看环境变量在）。
旁证：开开关后引擎行为还有一处可见变化（`disable_custom_all_reduce` False→True）。

**两个必须带走的事实**：

1. **量级呼应**：仅仅"批里有没有别人"就造成约 **0.007** 的概率偏移，而 q04 竞争词差距是
   0.011–0.026——**同量级**。近并列翻转的机制猜想（执行路径数值差 → 翻词）第一次有了
   本机实测支撑。这是**支撑证据**，不是证明（云端两臂才是 PREREG_4 的正式判定）。
2. **成本**：开开关后 32 词生成从 0.75 s 变 **20–30 s**（慢约 30–40 倍）。
   云端 I1 臂的时间预算要按这个打折（PREREG_4 §7 优先级：基线 > I1 > I2，I2 先砍）。

产物：`step8/batchprobe_batchinv_{off,on}.json`（含逐位置 diff 明细）；
`evidence_batchinv_on.txt`（开开关后的启动日志 + /metrics）。

## 总判定（作者 8 步顺序，2026-10-09 一天内完成）

| # | 步骤 | 结论 |
|---|---|---|
| 1 | `--generation-config vllm` 被 0.31.0 接受 | **PASS**（顺带抓到 `--dtype bf16` 拼写 bug，已修 `bfloat16`） |
| 2 | 服务启动/停止/重启 | **PASS**（5 s / 20 s 就绪，显存归还正常） |
| 3 | 启动日志确认实际参数 | **PASS**（四项全有；顺带抓到漏传 `B1_LOGPROBS_MODE` 会静默落回 raw 口径） |
| 4 | cal10 生成与校验 | **PASS**（跨平台 sha256 一致） |
| 5 | b1_prefix_4c.py 写成并本机运行 | **PASS** |
| 6 | 固定前文/token 对账/top-20 解析 | **PASS**（8/8、8/8、8/8 非退化、E3 逐位相同、参考臂 7/7） |
| 7 | legacy 与显式参数不混淆 | **PASS** |
| 8 | 批不变性开关测试 | **PASS（验证生效）** |

## 步骤 ⑤：旧 B1 数据重分析（6/15 与 12/15 复算）—— **PASS**（reanalysis/ 目录）

| 项 | 做法 | 结果 |
|---|---|---|
| 原 4c 的 6/15 | `b1_compare_4c.py` 重跑 `b1_4c_20261008T002222Z/attempt1/` 的 r1/r2/r3 三对 | 每遍 2/5 逐 token 相同，**合计 6/15** = 历史值 ✓ |
| PREREG_3 的 12/15 | `b1_4c3_analyze.py analyze` 重跑 `b1_4c3_20261008T134807Z/` | **12/15**（15 请求、12 逐位相同、3 分歧）= 历史值 ✓，且 E3 双臂逐位相同、E1 m=0.92354、E2 Δ 中位 1e-5 / M 0.011–0.026 全部与历史报告一致 |

⇒ 分析工具链没有被本轮改动破坏（b1_client.py 的改动是加可选参数，默认行为未变，此处实证）。

## 步骤 ③：模拟传输端字节/次数/失败计数 —— **PASS（9/9）**（mock_transfer/ 目录）

**形态说明（Ruling R10）**：本机无 nixl（8 GB 单卡，B1 偏差 D5），真 KV 传输端只能在云端。
本机验收的正确切法是"计数源 + 读数管道"两端分开验：
模拟传输端 = 按已知传输计划生成与 vLLM `/metrics` 同构的快照（nixl 直方图 `_count/_sum`、
失败计数器、分桶行、无关族、"出现但未动"族）；读数管道 = **一字未改**的
`b1_metrics_delta.py` + `b1_kv_table.py`；然后逐项对真值。

| 检查（真值 → 解析读数） | 结果 |
|---|---|
| decode 冒烟段传输次数 2 → 2 | PASS |
| decode 冒烟段传输字节 655,360 → 655,360 | PASS |
| decode 正式段传输次数 4 → 4 | PASS |
| decode 正式段传输字节 1,310,720 → 1,310,720 | PASS |
| decode 失败传输 2 → 2（注入的失败读得出来） | PASS |
| "出现但未动"族判读正确 | PASS |
| kv_table 五检查点 + 终值（6 次 / 1,966,080 B） | PASS ×3 |

⚠️ 过程中第一版**检查器**正则误报 9 项 FAIL（要求带符号增量，报告是无符号）——
按 AGENTS.md §10 第 25 条"先核对检查逻辑再改实现"处理，解析链无辜，修的是检查器。
修复后 9/9。工具：`sim/k8s_test/b1_mock_transfer_check.py`（可复现回归检查）。

---

# 最终判定：本机验收 6+1 项 **全部 PASS**

① 重复运行稳定（E3 逐位相同）② 服务起停重启 ③ 模拟传输端计数 ④ 固定前缀测试工具
⑤ 旧数据重分析（6/15、12/15 复现）⑥ 冻结参数干跑 ★批不变性开关（验证生效）
——**租云闸门（"没有本机服务启动证据，不租云机器"）已满足**。
下一步 = 10-11 云端收口（作者开云、查账单/预算闸后执行 `B1_4C_PREREG_4.md`）。

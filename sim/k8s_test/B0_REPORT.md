# B0 记录：阶段 B 的测量框架 + 候选引擎核对 + 开跑前清单

> 记录时间：**2026-10-06（UTC）**，本机 Windows 11 + Git Bash，Python 3.12.10（`sim/.venv`）
> 对应计划：`真实跨云LLM服务研究计划书.md` §5「阶段 B」、§7 第 5–6 周、§6 质量门槛
> 对应代码：`sim/k8s_test/measure_serving.py`（本次新增；**未修改任何已有文件**）
> 对应产物：`sim/results/b0/`（权威 run 见 §1 表；开发中间稿在 `sim/results/b0/_dev_superseded/`）

## 0. 先说人话（这一节给不看术语的人）

**B0 是"不租机器的那部分准备工作"。** 它做三件事：

1. **造一把尺子**——一个能对着任意"OpenAI 兼容接口"逐条请求量首字等待、逐字间隔、
   各阶段起止时刻和 KV 缓存真实字节数的工具（`measure_serving.py`），
   并且**自带一个假后端**：这个假后端不跑任何模型，只按设定"假装"吐出各个阶段的时刻，
   所以在**没有显卡的这台电脑上就能把整条测量链路走通**。
2. **把尺子自己校一遍**——用假后端这个"已知答案"去对：量出来的首字等待对不对、
   阶段时间有没有漏记、五种失败能不能分得清。**这一步是 B0 的真正价值**：
   它保证我们到了 GPU 机器上，量出来的数字是尺子的问题还是系统的问题能分得清。
3. **核对候选引擎**——查 vLLM 的"拆分式前处理"能不能给我们想要的四类东西
   （是否实验性、能不能给 KV 字节/耗时/失败数、两端内部时刻能不能采、两端必须匹配哪些项）。

**B0 完全不能替代阶段 B。** 它没跑过任何模型、没有 GPU、没有跨机、没有真实 KV 传输。
见 §3。

关于几个词：
- **拆分式前处理（disaggregated prefilling）**＝把"读完整段输入并算中间状态"和
  "一个字一个字往外吐"这两件事**放到两台机器上**分别做，中间要把算好的中间状态（KV 缓存）传过去。
- **首字等待（TTFT）**＝从请求发出到第一个字回来的时间，用户感觉"它开始理我了"。
- **逐字间隔（TPOT）**＝两个字之间隔多久，用户感觉"它说得快不快"。
- **KV 缓存**＝模型为了接着往下生成而保留的中间计算结果；拆到两台机器时这一坨要真的传过去。
- **门/判据**＝开工前的体检项，不过就不许把数字写进论文。

## 1. 本次交付了什么

| 交付物 | 路径 | 说明 |
|---|---|---|
| 测量工具（含假后端） | `sim/k8s_test/measure_serving.py` | 1400+ 行，**只用标准库**（无 numpy/pandas/scipy），因此云主机的系统 `python3` 也能直接跑 |
| 本记录 | `sim/k8s_test/B0_REPORT.md` | 本文 |
| 权威产物（假后端跑通） | `sim/results/b0/battery_20261006T172400328Z_1144-1_{battery.json,checks.csv}` | B0 验收电池：25 项检查，**24 PASS / 0 FAIL / 1 INFO** |
| 两臂客户端的逐请求明细 | `sim/results/b0/serving_20261006T172400343Z_1144-2_*`（不重叠）、`serving_20261006T172411288Z_1144-3_*`（50% 重叠） | 每请求一行，41 个字段 |
| 5 个故障注入臂 | `serving_...172422239Z_1144-4_*` … `...172427666Z_1144-8_*` | 逐项验证错误分类 |
| 两进程模式自测（含每 token 时刻） | `serving_20261006T172437218Z_22648-1_*` | `fake-backend` 与 `client` 分开起，`_tokens.csv` 有每 token 到达时刻 |
| 开发中间稿（保留、不引用） | `sim/results/b0/_dev_superseded/`（142 个文件 + `README.md`） | 每次真实运行都留了产物；按仓库纪律一个都没删 |

工具支持四个角色：
`selftest`（判据自检，不开网络）、`fake-backend`（假后端）、`client`（对着任意 OpenAI 兼容 URL 量）、
`battery`（前面的合起来，逐项给 PASS/FAIL）。

## 2. B0 证明了什么

**每一条都有落盘产物，不是"跑起来了"的口头结论。**

### 2.1 测量链路能跑通，且量出来的数与"配方"对得上

假后端的配方（`battery.json` 的 `arms[].backend`）是**公开可查**的，所以量出来的数可以逐条对账：

| 量 | 配方（假后端设定） | 实测（120 请求） | 对不对得上 |
|---|---|---|---|
| 逐字间隔（TPOT） | `token_interval_s = 0.02` | 单请求内 p50 的中位 **0.019997 s**、p95 **0.020050 s** | ✅ 差 0.015% |
| 首字等待（TTFT） | `queue 0.01 + prefill 0.10 + kv 0.0629`（不重叠臂） | p50 **0.1749 s** | ⚠️ 比"服务端前置段"多约 2 ms，见下 |
| KV 字节数 | `kv_mb = 24` → 25 165 824 B | 每请求 **25 165 824.0 B** | ✅ 逐字节一致 |
| KV 有效速率 | `kv_gbps = 3.2` | **3200.0 Mbit/s** | ✅ 一致 |
| 重叠量（臂 B） | `kv_overlap_frac 0.5 × kv_xfer_s 0.062915` = **0.031457 s** | 实测中位 **0.031457 s** | ✅ 到小数点后 6 位 |
| 内容片段数 | `token_count = 32` | 每请求 **32** 个 | ✅ 一个不漏 |

**TTFT 那约 2 ms 的差要照实说**：实测 TTFT 比"服务端 queue+prefill+kv"多
**2.014 ms**（臂 A）、**1.971 ms**（臂 B）、**2.284 ms**（两进程自测），
来源是**客户端建连/组请求头 + 服务端收请求 + 首次写出的 flush**。
这不是工具错，而是"外部观测"与"内部标记"天然有差；跨机时这段会变成网络往返加两端处理，
所以它**必须被单独看**（本工具把它放在 `send` 与 `wait_first_byte` 两段里，不藏进任何语义阶段）。

### 2.2 阶段时间能被**完整归属**，而且判据不是空判据

| 检查 | 实测 |
|---|---|
| 客户端可观测分解的并集是否铺满 `[请求开始, 请求结束]` | 两臂 **各 120/120 PASS**，最大空档与合计空档都是 **0.0 s**（容差 0.005 s） |
| 服务端 `queue→prefill→kv_xfer→decode` 在服务端窗口内是否铺满 | 两臂 **各 120/120 PASS**，最大空档 **0.0 s** |
| leave-one-out（拿掉任意一段必露出空档） | 最小空档 **0.313 ms**（臂 A）／0.503 ms（臂 B），远大于下限 1 µs ⇒ **每一段都真的被记下来了** |
| 服务端区间伸出客户端窗口多少（时钟对齐体检） | 中位 **0.0 s**（同机，符合预期） |

leave-one-out 这条**是判据自己的哨兵**：如果拿掉任何一段都仍然"覆盖"，说明那段根本没被记，
或者被别的段错误地吞掉了。有了它，"覆盖通过"才是有内容的结论。

### 2.3 五种失败能被分开识别（这直接对应阶段 C 的排查动作）

| 造出来的失败 | 期望分类 | 实测 |
|---|---|---|
| 直接回 HTTP 500 | `http_5xx` | 6/6 ✅ |
| 200 之后发 FIN 掐断（不写分块终止符） | `stream_incomplete` | 6/6 ✅ |
| 200 之后"规矩地"少收尾（有终止符、但没有 `[DONE]` 也没有 `finish_reason`） | `stream_incomplete` | 6/6 ✅ |
| 200 之后发 RST（真的断） | `stream_broken` | 6/6 ✅ |
| 服务端只接单不回答 | `timeout` | 6/6 ✅ |

**分类里有一条是实现层面的大发现，值得单列**：

> **不能靠"抛异常"来发现流被截断。** CPython 的 `HTTPResponse._peek_chunked()` 在遇到
> 不完整的分块流时**把 `IncompleteRead` 当成正常 EOF 返回**，而 `readline()` 走的正是 `peek`。
> 也就是说，一个被掐断的流在逐行读取下**看起来就是"正常读完了"**。
> 本工具因此改为**核对语义终止信号**（有没有 `[DONE]`、有没有 `finish_reason`），
> 并把这两个标志也写进了每行 CSV（`saw_done` / `saw_finish_reason`），便于事后复核。
> 实测："FIN 掐断"与"规矩截断"在行式读取下**无法区分**，两者都只能靠语义信号抓；
> 只有 RST 才会真的抛异常。

### 2.4 并发是开环的，在途数有记录

两臂各 120 请求、目标到达率 12 req/s，实测吞吐 **11.2 req/s**、**在途峰值 10**、
**`overloaded` 标记 0 次**。开环的意思是：到点就发，**不等上一个请求完成**；
如果系统被压住，在途数会上去并被如实记录，而不是偷偷降速——降速会让"系统在压力下的表现"
这个测量对象消失（阶段 A 已经踩过一次这个坑，见 `sim/k8s_test/README.md` §7 第 9 条）。

### 2.5 工具只依赖标准库，且路径可移植

`measure_serving.py` 不 import `numpy`/`pandas`/`scipy`/`matplotlib`。
理由与 `summarize_real.py` 相同：阶段 C 要在云主机的系统 `python3` 上跑同一份脚本，
那时不一定有 venv。

## 3. B0 **没**证明什么（这一节比 §2 重要）

**B0 不能用来写任何一条关于"系统好不好"的结论。** 具体地：

| 没证明 | 为什么 |
|---|---|
| **任何性能结论 / 收益结论** | 假后端不跑模型。它的"前处理 100 ms""KV 3.2 Gbps"是**按配方伪造的常数**，与任何真实系统无关。 |
| **真实 KV 缓存的字节数与传输时间** | 25 165 824 B 是 `--kv-mb 24` 乘出来的，不是传出来的。真实字节数只能从引擎（vLLM 的 NIXL 指标）取，见 §6.2。 |
| **拆分式服务能不能跑起来** | 没装 vLLM、没有 GPU、没连任何云主机。B0 里"前处理端/生成端"是**同一个进程里的同一个假后端**。 |
| **跨机时间能不能对齐** | 客户端与服务端在**同一台机器、同一个时钟**上。`srv_outside_s` 中位 0.0 s 只说明"没有跨机问题"，**不说明跨机时也没有**。跨机必须靠 NTP，且计划书 §6 要求时钟误差小于最小档位的 1/10。 |
| **绝对量级有意义** | Windows 回环 + CPython 线程化 HTTP 服务，本身就有毫秒级开销（实测 TTFT 比服务端前置段多 ~2 ms）。这里的绝对值**不能外推**到 GPU 机器。 |
| **亚毫秒阶段能分辨** | 本机 `time.time()` 的实测分辨率是 **0.507 / 1.004 / 1.309 / 1.504 ms**（四次运行各自量到）。也就是说**短于约 1 ms 的阶段这一段量不出来**。TTFT/TPOT 是 100 ms 量级，不受影响；但"KV 传输里的 post 时间"这种亚毫秒量，本机的钟分辨不了。 |
| **判据能直接搬到真实引擎上** | 覆盖容差 5 ms 的取值依据是"本机钟分辨率 ~1 ms"与"假后端的阶段都 ≥10 ms"。真实 vLLM 的四个语义阶段之间**可能有真实的、非零的空档**（例如前处理结束到 KV 传输开始之间未归属的一段），那时 5 ms 是否合适要**重新定**，不能照抄。 |
| **拆出来的一定是"纯"阶段** | 假后端的阶段边界是**我们按定义切出来的**。真实引擎的阶段边界来自引擎自己的事件（见 §6.3），语义未必一一对应；两者混着报会误导。 |
| **"加总等于端到端"可以用** | 恰恰相反，见 §5。 |
| **错误分类覆盖了真实世界的全部失败方式** | 只造了 5 种。真实系统还有：TLS 握手失败、代理返回 502/504、DNS 超时、服务端限流（429）、网关超时后重试…… **`429` 目前会被归到 `http_4xx`，没有单独类别**，真正开跑前要决定是否细分。 |

**一句话**：B0 把"尺子"和"体检流程"备齐了，**没有**、也不能给出任何关于系统的数字。

## 4. 实测输出样例（真实运行产出，不是编的）

### 4.1 电池两臂的聚合数（`battery_...172400328Z_1144-1_battery.json`）

配方（两臂只差一个数）：`token_count=32`、`token_interval_s=0.02`、`queue_s=0.01`、
`prefill_s=0.1`、`kv_mb=24`、`kv_gbps=3.2` ⇒ `kv_xfer_s = 0.062915 s`、`kv_bytes = 25165824`。

| | 臂 A `overlap0`（KV 不与前处理重叠） | 臂 B `overlap50`（KV 一半落在前处理尾部内） |
|---|---|---|
| 请求数 / 成功 / 失败 | 120 / 120 / 0 | 120 / 120 / 0 |
| 墙钟 / 吞吐 / 在途峰值 | 10.713 s / 11.2 req/s / 10 | 10.681 s / 11.2 req/s / 10 |
| **TTFT** p50 / p95 / p99 | **0.174929** / 0.191732 / 0.196731 s | **0.143429** / 0.143977 / 0.160138 s |
| **TPOT**（单请求 p50 口径）中位 / p95 | **0.019997** / 0.020050 s | **0.019987** / 0.020045 s |
| **端到端** p50 / p95 / p99 | **0.794734** / 0.811541 / 0.817478 s | **0.763068** / 0.763951 / 0.779948 s |
| 覆盖判定（客户端分解） | 120 PASS / 0 FAIL | 120 PASS / 0 FAIL |
| 覆盖判定（服务端阶段） | 120 PASS / 0 FAIL | 120 PASS / 0 FAIL |
| 客户端最大空档 / 合计空档 | 0.0 / 0.0 s | 0.0 / 0.0 s |
| leave-one-out 最小空档 | 0.313 ms | 0.503 ms |
| 服务端语义阶段**重叠量**中位 | **0.0 s** | **0.031457 s** |
| **`Σ语义阶段 − 端到端`** 中位 | **−0.00182 s** | **+0.029846 s** |
| `Σ语义阶段 − 服务端窗口` 中位 | **0.0 s** | **0.031457 s** |
| 服务端区间伸出客户端窗口 | 0.0 s | 0.0 s |

**怎么读这张表**：数字小的那一列不代表"更好"——两臂**不是**在比谁快，臂 B 的 TTFT 更小
只是因为 KV 传输提前开始了、生成也就提前开始。这张表的用途是**看最后三行**：
重叠存在时"加总"必然大于端到端，而"覆盖"照样通过。见 §5。

### 4.2 一条请求的原始行（臂 B `overlap50`，`r00000`）

```
req_id              = r00000
e2e_s               = 0.7639989852905273      ← 端到端（客户端窗口）
n_content_chunks    = 32
ttft_s              = 0.14380145072937012     ← 首字等待
tpot_p50_s          = 0.01996612548828125     ← 该请求逐字间隔的中位数
cov_max_gap_s       = 0.0                     ← 客户端分解最大空档
cov_loo_min_gap_s   = 0.0005071163177490234   ← leave-one-out 最小空档
cov_loo_stage       = teardown
cov_verdict         = PASS
srv_span_s          = 0.7614572048187256      ← 服务端窗口（queue起→decode止）
srv_stage_sum_s     = 0.7929143905639648      ← 四段简单相加（重叠被重复计入）
srv_overlap_s       = 0.03145718574523926     ← 重叠总量
srv_gap_max_s       = 0.0
srv_outside_s       = 0.0
srv_verdict         = PASS
sum_minus_e2e_s     = 0.0289154052734375      ← 0.7929 − 0.7640，**> 0**
sum_minus_srv_span_s= 0.03145718574523926     ← 0.7929 − 0.7615，**恰好等于重叠量**
kv_bytes            = 25165824.0
kv_mbps             = 3200.0000000000005
```

### 4.3 每 token 的到达时刻（两进程模式，`--save-tokens`）

`serving_20261006T172437218Z_22648-1_tokens.csv`，请求 `r00000` 的前几行
（配方 `token_interval_ms=15`）：

```
req_id,token_index,t_rel_s,gap_s
r00000,0,0.144953,0.000000
r00000,1,0.159451,0.014498
r00000,2,0.174526,0.015076
r00000,3,0.189727,0.015201
r00000,4,0.204325,0.014598
r00000,5,0.219326,0.015001
```

间隔在 14.5–15.2 ms 之间抖动，中位贴着配方的 15 ms——**这就是"逐字间隔"最原始的样子**，
每请求一行（`tpot_p50/p95/p99/mean/max`）都是从这一列算出来的。

### 4.4 两进程模式（`fake-backend` 与 `client` 分开跑）

`serving_20261006T172437218Z_22648-1_summary.json`：
请求 60 / 成功 60 / 失败 0，墙钟 6.395 s，吞吐 9.382 req/s，在途峰值 6；
TTFT p50 **0.134227** / p95 0.156510 s；端到端 p50 **0.478968** / p95 0.501028 s；
覆盖判定客户端 60 PASS、服务端 60 PASS。

这条路径重要，因为它就是**阶段 B 真正要用到的拓扑**：服务在机器 A 上（将来是 vLLM），
测量客户端在机器 B 上（或同一台机的另一个进程）；`--url` 换成真地址即可。

## 5. 为什么阶段时间**不能**用"加总等于端到端"验收

### 5.1 阶段 A 的那条恒等式在这里为什么不成立

阶段 A（`measure_system.py --role transfer`）有一条恒等式，记录在 `sim/k8s_test/STAGE_A.md`：
"入口 + 上传 + 服务端段 + 下传 = 端到端"，残差精确为 0。
那是对的，因为那是**同一条串行时间轴上切出来的四刀**，它是恒等式。

阶段 B 不能照抄，三条理由（本文件 §0 已用大白话说过，这里给依据）：

1. **阶段会重叠。** vLLM 官方拆分式前处理文档写明 worker 连接器是
   "layer-by-layer KV cache store and load"（逐层存/取 KV）——即某一层的 KV 正在传的同时，
   下一层还在算。NIXL 的指标也把 "post time（提交给网卡的时间）" 与
   "xfer time（真正的数据搬运）"**分开报**，正说明这两件事在时间上是可以在不同位置计量的。
2. **两端时钟域不同。** vLLM 官方 metrics 设计文档明确写：
   "monotonic clocks differ between processes - each process has its own reference point.
   So it is meaningless to compare monotonic timestamps from different processes."
   跨进程/跨机相减必须先解决时钟对齐。
3. **有"无归属"的残差是正常的。** 网络往返、TCP 落地、客户端自身开销，本来就不属于
   任何一个服务阶段。强行要求它们之和为 0，等于逼标记者把网络时间摊派给某个阶段——
   那会让"KV 传输耗时"这种关键量被污染。

### 5.2 换成了什么判据（本工具实现的三条）

| 判据 | 内容 | 落盘字段 |
|---|---|---|
| **(A) 覆盖** | 某一区间集合的并集必须铺满它自己的窗口；未被覆盖的空档总长与单段最大值都 ≤ 容差（默认 **5 ms**） | `cov_gap_total_s` / `cov_max_gap_s` / `cov_verdict`（客户端）与 `srv_gap_max_s` / `srv_verdict`（服务端） |
| **(B) 每段都必要** | leave-one-out：拿掉任意**有时长**的一段后，必须立刻露出 > 1 µs 的空档 | `cov_loo_min_gap_s` / `cov_loo_stage` |
| **(C) 加总只报不判** | `Σ阶段 − 端到端` 照实打印，**预期在有重叠时 > 0** | `sum_minus_e2e_s` / `sum_minus_srv_span_s` |

容差 5 ms 的取值依据是**两个已知量级**，不是随手挑的：下界是本机 `time.time()` 约 1 ms 的
分辨率（再小就分不清"空档"与"时钟刻度"），上界是任何一个阶段都在 10 ms 以上
（再大就会放过真的漏记）。**它与任何一档阶段时长都不相等**——相等时判定会由浮点误差决定，
这与本仓库 SKILL.md 里"回退阈值不能等于任何一档 lag"是同一个坑。

### 5.3 实测对照（这就是"换判据"的直接证据）

两臂**唯一的差别**是 KV 传输有没有和前处理重叠（`kv_overlap_frac` 0.0 vs 0.5）：

| 口径 | 臂 A 不重叠 | 臂 B 50% 重叠 | 这个口径能不能当判据 |
|---|---|---|---|
| `Σ语义阶段 − 服务端窗口` | **0.0 s** | **+0.031457 s** | 与重叠量**恒等**，所以它测的是"有没有重叠"，不是"记没记全" |
| `Σ语义阶段 − 端到端` | **−0.00182 s** | **+0.029846 s** | **同一个量在两臂上符号都不同** ⇒ 被重叠污染，**不能当判据** |
| **并集覆盖的最大空档** | 0.0 s | 0.0 s | ✅ 两臂都通过，与重叠无关 |
| **leave-one-out 最小空档** | 0.313 ms | 0.503 ms | ✅ 两臂都通过，证明每段都真的被记了 |

**结论**：如果按阶段 A 那条"加总等于端到端"来验收，臂 B 会**FAIL**——
而臂 B 的标记其实是完全正确的，它只是**诚实地记下了一段真实重叠**。
所以：**阶段 A 的加总口径不适用于有并发阶段的 B，必须换成"并集覆盖 + 空档不超容差"。**

## 6. 候选引擎核对：vLLM 的拆分式前处理

> **本节只核对，不定版本。** 显卡型号、模型大小、传输方式都还没定，所以下面
> 只写"候选区间/条件"，**没有任何版本被选定或锁定**，也**不含任何性能收益结论**。
>
> **取数与访问方式（照实说明）**：官方文档取 `docs.vllm.ai`。源码与依赖清单取
> `raw.githubusercontent.com/vllm-project/vllm/main/...`——本机 GitHub 直连不通
> （见根 `AGENTS.md`），`raw.githubusercontent.com` 也连不上，因此源码是**经
> `https://gh-proxy.com/<原始 URL>` 反代**取的。反代只做转发，正文未改动；
> 但"经反代获取"这件事本身应被视为**渠道局限**（详见 §8）。
> 文档页面是 `en/latest`，即**会随主干变化的版本**，下文的行文以"取数当日的主干"为准。

### 6.1 Q1：是否仍标注为实验性？——**是，仍明确标注**

出处：[Disaggregated Prefilling (experimental) — vLLM 官方文档](https://docs.vllm.ai/en/latest/features/disagg_prefill/)
（`en/stable` 的同一页面同样标注：[stable 版](https://docs.vllm.ai/en/stable/features/disagg_prefill/)）

原文要点（逐句核对）：

- 页面标题即带 **"(experimental)"**；正文开头有 Note：
  **"This feature is experimental and subject to change."**
- 同一页里"复用前处理端的 token id"那一节也单独标注：
  "It is experimental and subject to change."
- NixlConnector 使用指南里，**异构 KV 布局**（`enable_permute_local_kv`）一节标题就是
  **"Experimental Feature"**。
- 该页还写明：**"Disaggregated prefilling is highly related to infrastructure, so vLLM
  relies on third-party connectors for production-level disaggregated prefilling"**——
  即**生产级**要靠第三方连接器；这解释了为什么"能不能上线"要自己验。

**对 B0 的影响**：这条直接支持"阶段 B 的定位是**能不能正确运行与测量**，不是更快"。

### 6.2 Q2：能不能拿到 KV 缓存传输的字节数、传输耗时、失败次数？——**能，而且比预期更全**

出处：[NixlConnector Usage Guide — vLLM 官方文档](https://docs.vllm.ai/en/latest/features/nixl_connector_usage/)
（"Metrics Reference" 与 "Prometheus metrics" 两节）

官方写明连接器支持 **"fully async send/recv"**（异步收发），并周期性打一行
**KV Transfer metrics** 汇总日志，例如：

```
KV Transfer metrics: Num successful transfers=4, Avg xfer time (ms)=1.381,
P90 xfer time (ms)=2.601, Avg post time (ms)=0.672, P90 post time (ms)=0.801,
Avg MB per transfer=2.25, Throughput (MB/s)=1629.549, Avg number of descriptors=72.0,
Num failed transfers=0, Num KV expired reqs=0
```

同时在 `/metrics` 端点导出下列 **Prometheus** 指标（官方表格逐条列出）：

| 指标名 | 类型 | 对应我们要的哪个字段 |
|---|---|---|
| `vllm:nixl_bytes_transferred` | Histogram | **KV 传输的真实字节数**（每次传输一个样本） |
| `vllm:nixl_xfer_time_seconds` | Histogram | **传输耗时**（官方说 `xferDuration`，含 posting 与真正搬运） |
| `vllm:nixl_post_time_seconds` | Histogram | 提交给 RDMA 后端的耗时（`postDuration`，同步开销） |
| `vllm:nixl_num_descriptors` | Histogram | 每次传输的散列表项数 |
| `vllm:nixl_num_failed_transfers` | Counter | **失败次数**（含握手与 `send_notif` 失败，官方说这三类合并统计） |
| `vllm:nixl_num_failed_notifications` | Counter | 通知失败（官方明确写：**已包含在上一条里，不要重复相加**） |
| `vllm:nixl_num_kv_expired_reqs` | Counter | KU 租约到期、生成端还没读的请求数（**是调租约的信号，不是传输故障**） |
| `vllm:nixl_num_notifications_after_expiry` | Counter | 过期后才到的完成通知（**按通知计而非按请求计，TP>1 时会重复计数，不可与上一条比较**） |

**要点（这几条都会影响我们怎么写代码去采）**：

1. **有 histogram 就有分位数**——但也有代价：histogram 是**聚合**的，
   **不带请求 ID 标签**。也就是说，我们能拿到"所有传输的耗时分布"，
   **不能**直接从 Prometheus 拿到"第 137 号请求的那次传输耗时"。
   （官方也明确：那行汇总日志是"**for the last reporting interval**"的区间统计。）
2. 失败是**多种底层事件合并**统计的（传输、握手、通知），所以"失败次数"这个数
   不能直接当成"KV 没传成功几次"来解读，要看配套日志。
3. 官方给了**租约**参数 `kv_lease_duration`（默认 30 s）：前处理算完后 KV 块会保留这么久等
   生成端来读；租约太短会让 `num_kv_expired_reqs` 升高——这正好是阶段 C 里"跨云慢链路"
   必须调的一类参数。
4. 失败策略 `kv_load_failure_policy` 有两档：`fail`（默认，立刻让请求失败）与
   `recompute`（在生成端重算）。官方**警告 `recompute` 会导致性能退化**（生成端本是为低延迟
   优化的配置，去做前处理很低效，还会抬高其他解码请求的尾部延迟）。
   这条对我们的"失败率"口径很关键：**两档下的失败率不可比**，manifest 必须记下用的是哪档。

**未核实**：这些 Prometheus 指标在不同 vLLM 版本上的**名字与存在性**没有逐版本核对
（只核了"取数当日的主干"）。真实开跑前必须在**目标版本**上 `curl /metrics | grep nixl` 实地确认。

### 6.3 Q3：前处理端/生成端的内部时间点能不能采？——**能采，但"单请求精确时刻"要另想办法**

官方给了**引擎内部的逐请求事件序列**与由它算出的区间。出处：
[Metrics — vLLM 官方设计文档](https://docs.vllm.ai/en/latest/design/metrics.html)（"Engine Core Events" 一节），
以及源码 `vllm/v1/metrics/stats.py`（`RequestStateStats`，经 gh-proxy 取自主干）。

引擎核心会为**每一个请求**记录这些**单调钟**时间戳：

| 内部时间点 | 源码字段 | 由它算出的区间 |
|---|---|---|
| 请求进队列 | `queued_ts`（`QUEUED` 事件） | — |
| 第一次被调度 | `scheduled_ts`（`SCHEDULED` 事件） | 排队区间 = `scheduled_ts − queued_ts` |
| **生成出第一个 token**（＝前处理算完） | `first_token_ts`（`NEW_TOKENS`） | **前处理区间 = `first_token_ts − scheduled_ts`** |
| 最后一个 token | `last_token_ts` | 生成区间 = `last_token_ts − first_token_ts` |
| 请求进入前端 | `arrival_time`（墙钟） | 端到端 = `arrival_time → 最后一个 token` |

**这四个时间点分别能从哪拿到**（逐项回答题目问的"响应头 / 日志 / Prometheus / 取不到"）：

| 渠道 | 能不能拿到阶段时刻 | 依据 |
|---|---|---|
| **响应头** | **没有**（至少官方参考实现没有） | 我们核对了官方拆分式前处理的参考代理源码 `tests/v1/kv_connector/nixl_integration/toy_proxy_server.py`：它只做"把请求转发给前处理端、把 `kv_transfer_params` 附到生成端请求上、把 chunk 原样透传"，**没有加任何计时头**。<br>⚠️ **未核实**：vLLM 服务端自身（`vllm/entrypoints/launchers/` 下的 middleware）是否在响应头里放阶段时刻——**没有逐行核对**。 |
| **日志** | **有，但是区间聚合、不是逐请求** | KV 传输那行是"上一报告周期"的汇总（§6.2）。其它是运行时的周期性统计。 |
| **Prometheus `/metrics`** | **有，但是分布（histogram）、不带请求 ID** | `vllm:request_queue_time_seconds`、`vllm:request_prefill_time_seconds`、`vllm:request_decode_time_seconds`、`vllm:e2e_request_latency_seconds`、`vllm:time_to_first_token_seconds`、`vllm:inter_token_latency_seconds`、`vllm:request_time_per_output_token_seconds`，以及 §6.2 的 NIXL 一族。 |
| **单请求精确时刻** | **有，但要开 OpenTelemetry tracing** | 官方文档写明：配 `--otlp-traces-endpoint` 与 `--collect-detailed-traces=all/model/worker` 时，导出 `gen_ai.latency.time_in_scheduler`、`gen_ai.latency.time_in_model_forward`、`gen_ai.latency.time_in_model_execute`。官方同时提醒这个选项"**可能有性能影响**"。 |
| **KV 传输的起止时刻** | **只能从连接器遥测来** | NIXL 报的是 `postDuration`（提交耗时）与 `xferDuration`（端到端传输耗时），**是时长不是时刻**；引擎的逐请求事件里**没有**"KV 开始传/传完了"这两个点。所以"KV 传输在整条时间轴上的位置"**不能直接读到**，只能用估算（见下）。 |

**两条对设计的直接影响，必须写进正文的方法部分**：

1. **官方明确警告不要把两个进程的单调钟相减**（原文见 §5.1 第 2 条）。
   所以"前处理端的 `first_token_ts`"与"生成端的 `first_token_ts`"**不能直接相减**得到
   传输耗时。要么在同一进程内比较，要么用墙钟 + 时钟同步（计划书 §6 已要求时钟误差
   小于最小档位 1/10）。
2. **"生成开始"这件事在我们口径里是"客户端收到第一个内容片段"**（TTFT 锚点），
   与引擎内部的 `first_token_ts`（生成端真的产出第一个 token）**不是同一个事件**，
   中间隔着网络回传。两者要**分别报**，不能混成一个"TTFT"。

**未核实**：
- 上面这些指标在**目标版本**上的确切名字与开关（`--collect-detailed-traces` 的具体取值、
  OTel 导出的具体字段名）没有逐版本核对，只核了取数当日的主干。
- OTel 那条路径**是否覆盖 KV 传输阶段**（还是只有调度/前向/执行三段）**未核实**。

### 6.4 Q4：官方要求的兼容性条件（前处理端 vs 生成端）

出处：[NixlConnector Compatibility Matrix — vLLM 官方文档](https://docs.vllm.ai/en/latest/features/nixl_connector_compatibility/)

官方说：握手时会做一次**兼容哈希（compatibility hash）**校验。**必须一致**的有：

| # | 必须一致 | 为什么 |
|---|---|---|
| 1 | **vLLM 版本** 与 **NIXL 连接器版本** | 官方原文："P and D instances must agree on vLLM version and NIXL connector version"。版本不同直接过不了握手。 |
| 2 | **模型**：架构、dtype、KV 头数、head size、hidden 层数 | 两端的"块形状"必须能对上，否则按字节搬运的 KV 会错位。 |
| 3 | **注意力后端（attention backend）** | 不同后端的 KV 排布不同。 |
| 4 | **KV cache dtype**（`cache_dtype`，例如 FP8） | 量化 KV 那一节写明：两端必须用同一个 `cache_dtype`，不一致会在握手时失败。 |
| 5 | **投机解码的方法与草稿模型配置**（EAGLE/MTP 等） | 官方脚注 1：两端必须用**兼容的**投机配置。 |
| 6 | **NIXL 传输模式：push（WRITE）还是 pull（READ）** | 官方原文：push 与 pull "use incompatible transfer protocols and **must never be paired**"。 |

**可以安全不同**的（官方列出）：

| 项 | 说明 |
|---|---|
| `tensor-parallel-size`（异构 TP） | 受模型类型限制（见下） |
| `block-size`（异构 block size） | 只支持 **P 的 block size < D 的 block size**，且自动重映射 block id |
| KV 块的数量 | 由两端各自可用显存决定 |
| `num_speculative_tokens` | 前处理与生成可以用不同的草稿深度 |
| 草稿模型的 attention backend | 两端各自自动选择，KV 排布在握手时校验而不是过兼容哈希 |

**额外的限制条款（写实验方案时必须逐条对照）**：

- **KV 缓存布局**：NixlConnector 默认 **LBHNC**（head-major，非 MLA 模型）；**LBNHC**
  也支持但**不允许异构 TP 的头切分**。LBHNC ↔ LBNHC 的互相转换
  （`enable_permute_local_kv`）标为**实验性**，且**与 HMA 不兼容**。
- **流水线并行（PP）**：默认的 pull 连接器**不支持** `pipeline-parallel-size > 1`
  与混合 KV 布局（HMA）同时使用，会在启动时报错；要用 push 连接器
  （`NixlPushConnector`）。当前 push+PP+HMA 的限制还包括：**只有前处理端可以是 PP 切分的**、
  **混合 SSM/Mamba 布局不支持 PP**、**非 MLA 的 HMA 层路由要求"生成端 TP ≤ 前处理端 TP"**。
- **模型类型 × 能力矩阵**：Dense / MLA（DeepSeek-V2/V3）/ Sparse MLA / MoE 都支持基础 PD；
  **Hybrid SSM / Mamba** 的异构 TP 是 🚧（未支持），跨层块是 ❌；
  **Encoder-Decoder 是 ❌ 完全不支持**；**Multimodal 全是 ❔（未验证）**。
- **量化 KV**：静态量化（尺度从检查点加载）✅；**动态量化（运行时算尺度）❌ 不支持**
  ——每块的尺度不会随 KV 一起传。
- **异构 TP + Mamba**：要求 **P 的 TP == D 的 TP**。
- **`kv_load_failure_policy="recompute"`**：官方警告会导致性能退化并抬高其他请求的尾部延迟
  （见 §6.2 第 4 点）。

**旁路开关（要不要用是个决定，不是默认）**：
官方给了 `enforce_handshake_compat: false` 来**关掉兼容哈希校验**，原文是
**"at your own risk"**。B0 的建议：**不要关**——它是我们最便宜的"两端是不是配错了"的哨兵。

### 6.5 候选版本区间/条件（**刻意不锁定任何版本**）

按任务约束，这里**只写条件**，不写"最终用哪个版本"：

| 项 | 候选条件（怎么写都行，但必须逐个落实） | 依据 |
|---|---|---|
| vLLM 版本 | **必须两端完全相同**；具体号在**确定 GPU 型号与模型之后**再定；建议取一个**已发布 tag**而不是主干，并在 manifest 里记 commit/tag | §6.4 第 1 条 |
| NIXL 版本 | 随所选 vLLM 的依赖清单走；取数当日主干 `requirements/kv_connectors.txt` 写的是 **`nixl == 1.5.0`**（另含 `lmcache >= 0.3.9`、`mooncake-transfer-engine >= 0.3.12`） | [requirements/kv_connectors.txt @ main](https://gh-proxy.com/https://raw.githubusercontent.com/vllm-project/vllm/main/requirements/kv_connectors.txt) |
| 连接器 | 若要**异步收发 + 完整 KV 传输指标**，候选是 **`NixlConnector`**（官方称其支持 fully async send/recv，并给出上表的 Prometheus 指标）；`LMCacheConnectorV1` 走 NIXL 底层、`MooncakeConnector` 是另一条路。**选哪个要在阶段 B1 实测后再定** | §6.2 出处 |
| 传输后端 | NIXL 的插件（默认 **UCX**；官方示例里还有 `LIBFABRIC`、`GDS` 等）。注意：用 UCX 时 **NCCL 的环境变量不适用**，要配 `UCX_TLS` / `UCX_NET_DEVICES` | [NixlConnector Usage Guide](https://docs.vllm.ai/en/latest/features/nixl_connector_usage/) |
| 传输模式 | **push 或 pull 二选一，两端必须同一种**（`kv_role`：前处理端 `kv_producer`、生成端 `kv_consumer`；`kv_both` **已弃用**） | §6.4 第 6 条 |
| 握手端口 | `VLLM_NIXL_SIDE_CHANNEL_PORT`（默认 5600；同一台机上每个 worker 要不同）与 `VLLM_NIXL_SIDE_CHANNEL_HOST`（**两端在不同机器上时必须设**） | §6.2 出处 |
| 租约 | `kv_lease_duration`（默认 **30 s**）——跨云慢链路很可能要调大，并用 `vllm:nixl_num_kv_expired_reqs` 验证 | §6.2 第 3 点 |
| 模型/精度 | 两端**完全一致**的权重与 dtype（`cache_dtype` 也要一致）。Encoder-Decoder **不可选**；Multimodal **未验证**，先别选 | §6.4 |
| 并行度 | 先用 **TP 两端相同、PP=1** 的最简配置跑通，再考虑异构 TP / PP（PP+HMA 有一串限制） | §6.4 |

## 7. B1 开跑前必须逐项确认的清单

> 性质：**每一行都要在 manifest 里留下实测值或文件哈希**，不能只打勾。
> "为什么必须一致"那一列就是**这一项错了会以什么形式爆出来**。

### 7.1 两端必须匹配的项（vLLM 会校验，或错了会静默算错）

| # | 项目 | 为什么必须一致 | 怎么确认 |
|---|---|---|---|
| 1 | **vLLM 版本**（精确 tag + 镜像 digest） | 兼容哈希的一部分，不一致直接握手失败 | 两端 `vllm --version` + 镜像 digest，写进 manifest |
| 2 | **NIXL 连接器版本** | 同上 | `pip show nixl` / 依赖清单快照 |
| 3 | **模型权重**（同一 repo、同一 revision、逐文件 sha256） | 架构/KV 形状/层数不一致会**静默传错数据**（形状对得上但权重不同 ⇒ 结果错而不是报错） | 两端对模型目录整体算哈希并比对 |
| 4 | **权重 dtype**（fp16/bf16/fp8…） | 每元素的字节数决定 KV 块大小 | 启动日志 + `cache_config_info` 指标 |
| 5 | **KV cache dtype**（`cache_dtype`） | 官方：不一致会在握手时失败 | 两端 `/metrics` 的 `vllm:cache_config_info` |
| 6 | **注意力后端** | KV 排布不同 | 启动参数 + 日志 |
| 7 | **KV 缓存布局**（LBHNC / LBNHC，或开 permute） | 布局不同则按字节搬运会错位；permute 是实验性且与 HMA 互斥 | 两端环境变量 `VLLM_KV_CACHE_LAYOUT` |
| 8 | **投机解码配置**（是否用、方法、草稿模型） | 官方要求"兼容的投机配置" | 启动参数（B1 建议**先全部关掉**，减少变量） |
| 9 | **NIXL 传输模式**（push/pull） | 官方：两者协议不兼容，**绝不能一边 push 一边 pull** | `kv_role` 与连接器类名 |
| 10 | **分词器** | 两端都要能对同一段文本切出同样的 token。注意官方提到：`--tokens-only` 的前处理端**没有分词器**，`output_mode:"text"` 会被拒（400）；生成端若要回文本必须带分词器 | 两端各自对同一段文本 tokenize，比对结果长度与 id |
| 11 | **block size** | 可以不同，但**只支持 P < D**，且自动重映射；用错方向会失败 | 两端启动参数 |
| 12 | **TP / PP 配置** | 受模型类型限制（Mamba 要求 TP 相等；PP+HMA 有一串限制） | 启动参数 |

### 7.2 环境与基础设施（不匹配会让测量本身失真）

| # | 项目 | 为什么必须先确认 |
|---|---|---|
| 13 | **GPU 型号与数量 + 显存** | 决定模型能不能放得下、KV 块有多少；两端不同会变成"硬件差异"这个混淆因子（计划书 §4.2 要求：硬件不同时把差异当成单独因素，不能归因于云厂商） |
| 14 | **网卡型号/速率/拓扑** | 决定 KV 传输走 RDMA 还是 TCP。同一台机内的 NVLink/RDMA 与跨机 TCP 是完全不同的量级，混着报没有意义 |
| 15 | **容器镜像**（CUDA 版本、vLLM 镜像 tag/digest） | CUDA/NCCL/驱动不匹配会在初始化时报错，或更糟——在长跑中途报错 |
| 16 | **系统时钟同步（NTP/chrony）** | **本项目的核心自变量是"状态有多旧"**。计划书 §6 明确要求：机器时钟误差要小于最小状态年龄档位的 1/10。不确认这条，"陈旧 1 秒"这种档位根本不可信 |
| 17 | **观测抓取周期**（Prometheus scrape interval、日志级别） | 这决定了我们看到的"状态"有多旧——它本身就是被研究的量，必须记下来 |
| 18 | **并发档位**（开环的目标到达率） | 计划书 §5 建议先试可持续能力的约 40%/65%/85% 三档。**在试跑确认稳定前不冻结**；标称值必须用实测服务时间反算（不能拿空闲速度算，阶段 A 的组件实测已证明空闲 vs 满载差 ~2 倍） |
| 19 | **输入/输出长度**（prompt tokens / max_tokens） | 它们决定 KV 大小与请求时长；两端、各策略之间必须逐字段相同 |
| 20 | **前缀缓存开关** | 计划书 §4.3：主实验**先关闭**前缀缓存，避免命中效果混进"网络派发"的结论 |
| 21 | **`kv_load_failure_policy`**（fail / recompute） | 两档下的失败率与延迟**不可比**（官方警告 recompute 会退化并抬高尾部） |
| 22 | **`kv_lease_duration`** | 太短会让 KV 在生成端读到之前就过期（`num_kv_expired_reqs` 会涨），表现成"莫名其妙的失败" |
| 23 | **握手端口开放**（`VLLM_NIXL_SIDE_CHANNEL_PORT/HOST`） | 跨云时这往往是安全组/隧道最容易漏的一条；不确认就会卡在"连不上" |
| 24 | **测量客户端所在位置与时钟** | 客户端若在第三台机上，它也参与端到端口径；它的时钟与两端的关系要一起记 |
| 25 | **许可**：模型权重许可 + trace 许可 | 计划书 §7 第 1–2 周的出口项；投稿前必须能说清 |

### 7.3 费用与安全（不是技术项，但漏了会中断实验）

| # | 项目 | 为什么 |
|---|---|---|
| 26 | **预算上限 + 单次试跑最长时长 + 告警** | 计划书 §6：费用按账单实测、事前设预算提醒、结束释放资源 |
| 27 | **管理端口只允许指定登录地址；服务端口仅经实验隧道开放** | 计划书 §5 阶段 C 明文要求 |
| 28 | **日志不得含密钥/认证信息/真实用户文本；测试输入用合成文本** | 计划书 §5 阶段 C 明文要求 |
| 29 | **跨云流量的计费口径** | 计划书 §8 的贡献之一是"费用边界"，所以费用必须能从账单里**分离出来**，否则整条贡献没法写 |

## 8. 未验证项（**单列，一条都不许含糊**）

### 8.1 与 vLLM 有关

| # | 未验证的事 | 现状 |
|---|---|---|
| V1 | **具体版本号的行为** | 只核对了 `docs.vllm.ai/en/latest`（主干）与主干源码。**没有在任何一个已发布 tag 上实地验证过**，因此"指标名字/开关/兼容哈希条款"都可能随版本变。**开跑前必须在目标版本上实地 `curl /metrics`**。 |
| V2 | **vLLM 服务端是否在响应头里放阶段时刻** | 只核对了**官方参考代理**（`toy_proxy_server.py`）——它不加计时头。服务端自身的 middleware **没有逐行核对**。 |
| V3 | **OpenTelemetry 是否覆盖 KV 传输阶段** | 官方文档只举了 `time_in_scheduler` / `time_in_model_forward` / `time_in_model_execute` 三个字段，**没有看到 KV 传输的 span**。未核实。 |
| V4 | **`vllm:nixl_*` 指标的逐版本可用性** | 见 V1。 |
| V5 | **能不能把某一次 KV 传输与某个请求对上** | 从官方文档看：histogram 不带请求 ID，那行日志是区间汇总。**没有找到"逐请求 KV 传输耗时"的现成出口**。要在阶段 B1 实测确认，也许要靠连接器自己的日志或改代码。 |
| V6 | **多连接器（MultiConnector）与 push 连接器的实际行为** | 只读了文档，没跑。 |
| V7 | **异构 TP / PP / 量化 KV 的实际可用性** | 只读了兼容性矩阵，没跑。矩阵本身也说部分条目标 ❔/🚧。 |

### 8.2 与本工具的假后端、测量口径有关

| # | 未验证的事 | 现状 |
|---|---|---|
| M1 | **真实模型下的 TTFT/TPOT 能不能按预期采到** | 假后端是"按配方到点就吐"，真 vLLM 的吐字节奏受批处理影响，可能出现一个 chunk 含多个 token。官方文档明确说：`vllm:inter_token_latency_seconds`（逐输出间隔）与 `vllm:request_time_per_output_token_seconds`（每请求 TPOT）**在"一个输出含多个 token"时会不一样**（例如投机解码）。**本工具目前按"一个 chunk = 一个片段"计**，真机上要先确认每个 chunk 是不是恰好一个 token。 |
| M2 | **服务端阶段区间与 vLLM 的语义阶段是不是一一对应** | 假后端的 `queue/prefill/kv_xfer/decode` 是我们按定义切的；真机要把 `queue time`/`prefill time`/`decode time`（§6.3）映射过来，且**KV 传输那段的起止时刻拿不到**（V5）。映射关系未验证。 |
| M3 | **5 ms 的覆盖容差在真机上是否合适** | 真机的四个语义阶段之间**可能有真实的、非零的空档**。真机上必须重新定容差，并把"为什么取这个值"写下来。 |
| M4 | **跨机时间对齐** | 本机同进程/同机器，`srv_outside_s` 中位 0.0 s 只证明"同机没问题"。跨机的时钟误差如何处理**没验**。 |
| M5 | **亚毫秒阶段** | 本机 `time.time()` 分辨率 0.5–1.5 ms，短于约 1 ms 的阶段量不出来。要量 KV 的 post time 这类量，得换更高分辨率的计时方案。 |
| M6 | **错误分类的完整性** | 只造了 5 种。**`429`（限流）目前会落到 `http_4xx`**，没有单独类别；TLS 失败、代理 502/504、DNS 超时都没造过。 |
| M7 | **非流式（`stream:false`）路径** | 假后端实现了，客户端**没有**走这条路测过（工具只为流式做 TTFT/TPOT）。 |
| M8 | **长跑稳定性** | 单次最多 120 请求 × ~0.8 s。**没有做过小时级长跑**，所以连接复用/端口耗尽/内存这类长跑问题没暴露。 |
| M9 | **`--arrival poisson` 路径** | 代码里有，但 B0 的两臂与自测都用 `uniform`。**没验过**。 |
| M10 | **"两个独立时段"的重复性** | B0 只跑了一轮。计划书 §5 要求至少 3 个独立时段——那是阶段 B/C 的事，B0 不算。 |
| M11 | **本工具在 WSL / Linux 上的行为** | 只在 Windows + CPython 3.12 上跑过。代码里为跨平台做了处理（SO_LINGER 两种打包、只用标准库），但**在 Linux 上没实测**。 |

### 8.3 与阶段 A 那条恒等式有关

| # | 未验证的事 | 现状 |
|---|---|---|
| A1 | **阶段 A 的字段与新判据能否并存** | 本工具是**新文件**，没有改 `measure_system.py`。两者现在是两套口径、两套产物。要不要在阶段 B 里同时保留阶段 A 的加总口径（作为对照）**还没决定**。 |
| A2 | **真实引擎上会不会真的出现重叠** | B0 证明了"有重叠时加总口径会误判"，重叠本身是**在假后端里按官方文档的描述造出来的**（逐层传 KV）。真 vLLM 上到底重叠多少**没测**。 |

## 9. 复现命令

全部在 `sim/` 目录下执行（解释器用 `sim/.venv/Scripts/python.exe`）。

```bash
# 1) 判据自检（不开网络、不需要后端；里面含必须 FAIL 的负对照）
.venv/Scripts/python.exe k8s_test/measure_serving.py selftest

# 2) 一条命令跑完整套 B0 验收（自带假后端；约 30 秒）
.venv/Scripts/python.exe k8s_test/measure_serving.py battery --out-dir results/b0

# 3) 两进程模式：一个终端起假后端，另一个终端量它
.venv/Scripts/python.exe k8s_test/measure_serving.py fake-backend --port 9520 \
    --token-count 24 --token-interval-ms 15 --prefill-ms 80 --kv-mb 16 --kv-gbps 3.2
.venv/Scripts/python.exe k8s_test/measure_serving.py client \
    --url http://127.0.0.1:9520 --label smoke --requests 60 --qps 10 --concurrency 12 \
    --save-tokens --out-dir results/b0

# 4) 将来打真 vLLM：只把 --url 换成拆分式服务的入口（代理/生成端），其余不变
.venv/Scripts/python.exe k8s_test/measure_serving.py client \
    --url http://<host>:<port> --requests 200 --qps <N> --concurrency <C> --out-dir results/b0
```

本工具**不含任何 vLLM 依赖**，所以第 4 步在有 GPU 的机器上也能直接跑——
把脚本拷过去，用那边的 `python3` 即可。

### 本次修掉、并已写进注释与自检的实现坑（都不是假想）

| # | 现象 | 真因 | 现在怎么防 |
|---|---|---|---|
| 1 | 120/120 请求被记成 `stream_broken`（假故障） | 分析代码把 `(名字, 起, 止)` 三元组当二元组解包，异常被最外层的网络兜底抓住 | 采样与分析拆开，分析错记 `internal_error`，**绝不与网络错混** |
| 2 | 失败请求全被叠加成 `internal_error` | 用 `0.0` 当"流没读完"的哨兵，失败路径上 `t_end` 未赋值 | 哨兵改 `None`；只在流真的读完时才做覆盖分析 |
| 3 | 最后一片 token 被静默漏掉（32 数成 31） | 服务端报告挂在该片上，客户端解析到它就 `continue`，没先取文本 | **先取文本、再处理报告** |
| 4 | "断流"组 6/6 被判成成功 | 假后端断流时规矩地写了分块终止符 ⇒ 对端看到正常结束 | 断流改成不写终止符；并新增"规矩截断"作为独立对照 |
| 5 | RST 组 6/6 被判成 `timeout` | `socket.close()` 时 `makefile()` 的包装还持着 fd 引用，fd 没真关 | 先关读写包装再关 socket；用 `_DeadIO` 顶掉 socketserver 的收尾 flush |
| 6 | 汇总里"恰好为 0"的值变成 `null` | `sum([])` 返回**整数** 0，汇总按 `isinstance(x, float)` 过滤 | `check_coverage` 里每个量显式转 `float`，汇总也接受 int；自检里加了类型负对照 |

## 10. 一句话总结

**B0 备齐了尺子并把它校过了（24/24 检查通过），也把候选引擎能不能提供我们要的数据核清了；
但它没有、也不能给出任何关于"系统好不好"的数字。**
下一步（阶段 B1，需要租 GPU）要做的，是把本工具的 `--url` 指向真实的拆分式服务，
把 §6.2 的 NIXL 指标接进来，并逐条落实 §7 的 29 项清单。

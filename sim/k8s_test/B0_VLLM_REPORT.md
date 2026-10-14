# B0 追加记录：把测量尺子接到**真 vLLM** 上

> 记录时间：**2026-10-07（UTC）**；环境：Windows 11 + Git Bash，服务跑在 **WSL2 的 Ubuntu 26.04** 里。
> 对应计划：`真实跨云LLM服务研究计划书.md`
> 对应上一轮：`sim/k8s_test/B0_REPORT.md`、`B0_LINUX_REPORT.md`（含 22 条未验证项）
> 对应代码：`sim/k8s_test/measure_serving.py`（**改了一处，见 §5**）、`sim/k8s_test/probe_vllm_chunking.py`（**本次新增**）
> 对应产物：`sim/results/b0_vllm/`
>
> ⚠️ **本文件末尾另有一轮追加：`§口径修正`（2026-10-07 第二轮）**——那一轮**真的改了代码**：
> 把 §6 提出的"字段必须改名"落到了字段上（`chunk_interval_*` 等新字段 + 服务端 token 数字段），
> 旧字段一个没删；§11 末尾那句"必须按 §6 改名"以**末尾那一节**为准。

---

## 0. 先说人话（不看术语也能读）

**这一轮补的是 B0 最大的一个窟窿：那把尺子从来没量过真的推理服务。**

B0 造了一把尺子（`measure_serving.py`），但它只对着**自己做的假后端**量过——
假后端"按配方吐字"，所以量得准是必然的。真模型不一样：它是**一小批一小批**地算，
字什么时候出来、一次出来几个，都由引擎自己决定。**尺子对不对，只有接到真服务上才知道。**

这一轮做了三件事：

1. **在 WSL 里真装了一个 vLLM，真加载了一个小模型（Qwen2.5-1.5B），真起了 HTTP 服务。**
   全程只用镜像源，装完连模型一共下了约 12 GB。
2. **用那把尺子量了它 120 次**，回答四个问题：能不能流式量、结束标志认得准不准、
   错误分得对不对、**一个输出片是不是等于一个 token**。
3. **顺手看了一眼 `/metrics`**，看那些"KV 传输"指标到底在不在。

**三条最要紧的结论（先说结果）**：

- **好消息：尺子能用。** 120 次请求 **120 次成功、0 次失败**，首字等待和逐片间隔都正常量到了，
  正常结束也一次都没认错。
- **关键结论：一个输出片≈一个 token，但只在不冷不热的稳态下成立。** 平时每一片正好装 1 个 token
  （用三条互相独立的证据对过账）；**可是服务刚起来后的第一条流式请求，一次就塞了 31 个 token**，
  那次"片数"完全不能当"token 数"用。
- **另一个容易踩的坑：有内容的片 ≠ token。** 中文和 emoji 会因为"一个字的字节被劈成两半"而出现
  **文本是空的片**；尺子只数有文本的片，所以**会把 token 数少算**（中文那例少算 2 个，245 那例少算 1 个）。

关于几个词：
- **输出片（chunk）**＝流式响应里一次 `data: {...}` 推送，用户感觉是"又蹦出来一小段"。
- **token**＝模型自己分词的最小单位，一个字/一个词根/半个汉字都可能是 1 个 token。
- **首字等待（TTFT）**＝从发出请求到第一个字回来要多久。
- **逐片间隔**＝两片之间隔多久。**如果一片不等于一个 token，这个数就不能叫"逐 token 间隔"**——
  这就是本报告 §6 要处理的事。

---

## 1. 装了什么（版本 / 模型 / 环境 / 参数）

### 1.1 环境一览（全部在 WSL2 里，不在 Windows 侧）

| 项 | 值 | 说明 |
|---|---|---|
| 发行版 | Ubuntu 26.04.1 LTS | WSL2 内 |
| 内核 | `6.18.33.2-microsoft-standard-WSL2` | |
| 解释器 | **CPython 3.14.4** | WSL 系统 python3 |
| venv | **`/root/vllm-env`** | 放在 **WSL 原生 ext4**，**不是** `/mnt/e` |
| 模型缓存 | **`/root/hf-cache`**（`HF_HOME`） | 同上，原生文件系统 |
| GPU | **RTX 4060 Laptop，8188 MiB，驱动 617.42，算力 8.9** | WSL 里 `nvidia-smi` 可见 |
| vLLM | **0.31.0** | 从**阿里云 PyPI 镜像**装 |
| torch | **2.13.0+cu130**（CUDA 13.0） | vLLM 0.31.0 硬性要求 `torch==2.13.0` |
| transformers | 5.17.0 | |
| huggingface_hub | 1.33.0 | |
| 系统编译器 | **gcc / g++ 15.2.0**、**python3-dev（`Python.h`）** | 见 §1.3，**不装就起不来** |

⚠️ **版本不是"最终锁定版"**。阶段 B 的最终版本要等"显卡型号 / 模型大小 / 传输方式"定了再定。
这里记的只是**本次实际装在机器上的版本号**。

### 1.2 模型：为什么选 `Qwen/Qwen2.5-1.5B-Instruct`

- fp16 权重 **2.88 GiB**，8 GB 显存绰绰有余（本机 Windows 侧桌面已占约 1.1 GiB）；
- 是标准的 instruct 模型，OpenAI 兼容接口行为规范，适合当"第一块试金石"；
- 1.5B 让"改一个参数→跑一轮"只要几秒，方便这次要做的**几十次小实验**；
- 走 **`hf-mirror.com`** 下载（本机直连 `huggingface.co` 不通）。落到
  `/root/hf-cache/hub/models--Qwen--Qwen2.5-1.5B-Instruct/snapshots/989aa7980e4cf806f80c7fef2b1adb7bc71aa306`。

### 1.3 装机路上踩到的坑（每一条都真实发生过，照实记）

| # | 现象（报错原文） | 根因 | 处置 |
|---|---|---|---|
| 1 | `ERROR: Could not install packages due to an OSError: [Errno 28] No space left on device` | WSL 的 **`/tmp` 是 3.7 GiB 的 tmpfs**，pip 把几 GB 的 wheel 全解包在那儿 | 把 `TMPDIR` 指到 ext4（`export TMPDIR=/root/tmp`）后重跑 |
| 2 | 大 wheel 下载只有 **约 78 KB/s** | `pip.conf` 里有 `extra-index-url = pypi.org`，pip 对大包挑了 pypi.org（国内慢） | **只留阿里源**（实测同一文件 **4.7 MB/s**） |
| 3 | `torch._inductor.exc.InductorError: RuntimeError: Failed to find C compiler` | WSL 里**没有任何 C 编译器** | `apt install gcc g++ make` |
| 4 | `gcc /tmp/.../cuda_utils.c ... -o ....so ... returned non-zero exit status 1` | 装完 gcc 仍失败，真因是 **`Python.h` 不存在** | `apt install python3-dev` |
| 5 | `RuntimeError: Could not find nvcc and default cuda_home='/usr/local/cuda' doesn't exist` | FlashInfer 的**采样器**要现场 JIT 编译 CUDA 核，需要 nvcc | 关掉那个采样器：**`VLLM_USE_FLASHINFER_SAMPLER=0`**（另配 `CUDA_HOME`/`CPATH`/`LIBRARY_PATH` 指向 pip 装出来的 `nvidia/cu13` 目录） |
| 6 | `ValueError: No available memory for the cache blocks` | 显存预算调得太高（0.80 / 0.85），**CUDA graph 抓图**吃掉的内存没算进去 | 用 **`--gpu-memory-utilization 0.70`**，成功 |

另外两条**操作层面**的坑（不是模型问题，但会咬人）：

- **apt 源**默认是 `archive.ubuntu.com`，国内很慢；本次换成了 `mirrors.aliyun.com`
  （**系统级改动，已备份原文件到 `/root/b0vllm/ubuntu.sources.orig`**，要还原直接拷回去）。
- **WSL 会在最后一个会话退出后杀掉所有用户进程**。用 `nohup ... &` 起的服务会**静默消失**，
  日志停在半路且没有任何报错（本次实际发生过一次）。要么让命令跑在前台会话里，
  要么用 `systemd-run` 起成系统服务。

### 1.4 服务端最终配置（**这是本次所有数字的前提**）

```
/root/vllm-env/bin/vllm serve Qwen/Qwen2.5-1.5B-Instruct \
  --host 127.0.0.1 --port 8090 \
  --max-model-len 4096 --dtype float16 \
  --gpu-memory-utilization 0.70
环境变量：HF_HOME=/root/hf-cache  HF_HUB_OFFLINE=1  VLLM_USE_FLASHINFER_SAMPLER=0
```

启动日志里读出来的关键事实（原文见 `vllm_server_keylines_*.txt`）：

| 量 | 值 |
|---|---|
| 引擎 | vLLM **V1** LLM engine (v0.31.0)，`enforce_eager=False` |
| 默认开的特性 | `enable_prefix_caching=True`、`enable_chunked_prefill=True` |
| 编译 | mode `VLLM_COMPILE`，backend `inductor`，CUDA graph `FULL_AND_PIECEWISE`，抓图尺寸到 512 |
| 权重加载 | **2.98 GiB / 5.63 s** |
| **可用 KV cache 显存** | **1.84 GiB** |
| **KV cache 容量** | **68,992 tokens**（对 4096-token 请求的**最大并发 16.84×**） |

---

## 2. 用测量工具对接它：四个问题逐条回答

**这次对真服务的正式测量**（尺子 = `measure_serving.py client`，解释器 = WSL 的系统 `python3`）：

```
python3 k8s_test/measure_serving.py client \
  --url http://127.0.0.1:8090 --model Qwen/Qwen2.5-1.5B-Instruct \
  --label vllm_real --requests 120 --qps 12 --concurrency 16 \
  --prompt-tokens 64 --max-tokens 32 --save-tokens --out-dir results/b0_vllm
```

产物：`serving_20261007T061821151Z_5377-1_{requests,tokens,summary,manifest}`（每请求一行 41 字段 + 每片一行）。

> ⚠️ **先说一个会让人白干十分钟的坑（不是 bug，是用法）**：
> `--url` 必须填**不带 `/v1` 的基地址**，因为脚本自己会拼 `/v1/completions`。
> 填成 `http://127.0.0.1:8090/v1` 就会请求 `/v1/v1/completions` → **404**。
> 本次实测：`POST /v1/completions` → **200**，`POST /v1/v1/completions` → **404**。

### 2.1 流式输出能用吗？逐片到达时刻能不能正常记下来？

**能用，而且量得很干净。**

| 量 | 实测（120 请求） |
|---|---|
| 成功 / 失败 | **120 / 0** |
| 墙钟 | 10.449 s（吞吐 11.49 req/s，在途峰值 7） |
| **首字等待（最慢的那撮有多慢 → p95）** | 一般 **45.6 ms**，倒霉时 **53.1 ms**（p99 55.7 ms，最坏 58.9 ms） |
| **逐片间隔** | 单请求 p50 口径：一般 16.05 ms、p95 ≤16.34 ms；逐片 p95 口径：一般 17.17 ms |
| 端到端 | 一般 **546 ms**，p95 562 ms，最坏 578 ms |
| **逐片时刻落盘** | **3840 行**（120 请求 × 32 片），一行不缺 |
| 片段间隔（直接对 3840 行算） | p50 **16.1 ms**、p95 17.4 ms、最小 14.0 ms、最大 26.0 ms |

**"时间都被记下来了"这条判据也在真服务上过了**：
客户端五段分解（`send / wait_first_byte / to_first_content / decode_stream / teardown`）
的**并集覆盖判定 120/120 PASS**，最大空档 **0.0 s**；
leave-one-out 最小空档 **5.13e-05 s**（远大于 1 µs）→ **每一段都真的被记下来了**，
不是靠"并集碰巧覆盖"混过去的。

**一条重要的"拿不到"**：服务端语义阶段（排队 / 前处理 / KV 传输 / 生成）在真 vLLM 上
**一个都没采到** —— `stage_report_seen = 0/120`。
真 vLLM **不会**在流里附带 `x_stage_report` 这种东西（B0 用假后端时是我们自己伪造的）。
所以那 4 个阶段目前是 **N/A**，要拿到得靠 `/metrics`（见 §4）或别的出口。

### 2.2 正常结束标记认得准吗？有没有把"正常结束"误判成"流被截断"？

**认得准，零误判。**

| 检查项 | 实测 |
|---|---|
| 看到 `data: [DONE]` | **120 / 120** |
| 看到带 `finish_reason` 的片段 | **120 / 120** |
| 被归为"流不完整"（`stream_incomplete`）的请求 | **0 / 120** |
| 任何错误分类非空的请求 | **0 / 120** |
| 每请求的内容片段数 | **全部 = 32**（与 `--max-tokens 32` 一致，无一例外） |

也就是说：`saw_done` 与 `saw_finish_reason` 这两个独立信号在真服务上**都有效**，
B0 那条"不能靠异常发现截断、必须核对语义终止信号"的设计在真服务上**成立**。

### 2.3 错误处理：能造出哪些真实的错误场景？分类对不对？

用新增的探针（`probe_vllm_chunking.py errors`）对着真服务造了 12 个场景，逐个记原始响应：

| # | 场景 | HTTP | 尺子的分类 | 服务端原文（截断） |
|---|---|---|---|---|
| 1 | 正对照（合法请求） | **200** | — | 正常生成 |
| 2 | 请求体根本不是 JSON | **400** | `http_4xx` | `json_invalid` |
| 3 | `max_tokens` 传成字符串 | **400** | `http_4xx` | `int_parsing` |
| 4 | `max_tokens = -1` | **400** | `http_4xx` | `max_tokens must be at least 1, got -1` |
| 5 | `max_tokens = 0` | **400** | `http_4xx` | `max_tokens must be at least 1, got 0` |
| 6 | `temperature = -1` | **400** | `http_4xx` | `temperature must be non-negative, got -1.0` |
| 7 | `n = -1` | **400** | `http_4xx` | `n must be at least 1, got -1` |
| 8 | **模型名不存在** | **404** | `http_4xx` | `The model 'x' does not exist.` |
| 9 | 提示词超长（20 万 token） | **400** | `http_4xx` | `maximum context length is 4096 tokens` |
| 10 | 多传一个不存在的字段 | **200** | — | **被忽略，不报错** |
| 11 | 客户端超时设成 1 ms | **0** | **`timeout`** | `socket timeout after 0.001s` |
| 12 | 空请求体 | **400** | `http_4xx` | 校验错误 `missing` |

**分类正确性**：12 个场景**全部归对**（4xx 归 `http_4xx`、404 也归 `http_4xx`、超时归 `timeout`）。

**第 10 条特别重要**：真 vLLM **接受并忽略多余字段**（`extra="allow"` 行为）。
所以尺子往请求体里塞的 `x_req_id` / `_hang_s` **不会**把真服务打挂——
这一点必须先确认，否则整把尺子都打不了真服务。

**过载（限流）试了，但造不出来**：

| 压力 | 结果 |
|---|---|
| 40 个并发、`max_tokens=128` | **40 × HTTP 200** |
| 80 个并发、`max_tokens=256` | **80 × HTTP 200** |
| 服务端访问日志里出现 `429` / `503` 的次数 | **0** |

**结论：vLLM 默认是"排队"而不是"拒绝"**，压力上去只是变慢，不会回限流码。
所以 **`429` 这条路径本次仍然没能验证**（B0 的 M6 项依旧未闭合），
而且尺子里 **429 没有独立类别，会落到 `http_4xx`** 这件事**依然成立**。

### 2.4 ★ 一个输出片到底是不是一个 token？（本任务最要紧的一条）

**结论：在"不冷不热的稳态"下，一片正好等于一个 token——
但这不是无条件的，而且有两个会破坏它的明确情形。**

#### 证据 A：三条互相独立的对账（都指向同一个数）

对每个"（提示词，采样参数）"组合，同时取三个互不依赖的 token 数：

- `n_tokens_ids`：请求里带 `return_token_ids=true` 时**每个片段自报**的 `token_ids` 长度之和；
- `n_usage_tokens`：请求里带 `stream_options.include_usage=true` 时**服务端自报**的 `completion_tokens`；
- `n_tokenize_tokens`：把生成出来的文本送回服务端 `/tokenize`，用**同一个分词器**再数一遍。

同时数"有多少片"（`n_chunks`）和"每片装了几个 token"的直方图。

**关键读数：每片装几个 token 的直方图，在 12 个组合里 11 个是 `{1: N}`**（每个组合重复两次）。
下表是主表（完整合并表见 `chunk_vs_token_all_runs.csv`）：

| 组合 | 片数 | 带文本的片 | **文本为空的片** | Σ`token_ids` | 服务端 `usage` | `/tokenize` | **每片 token 数直方图** |
|---|---|---|---|---|---|---|---|
| ASCII 贪心 32（**冷**，第一条流式） | **2** | 2 | 0 | 32 | 32 | 32 | **`{1:1, 31:1}`** ← 异常 |
| ASCII 贪心 32（热，重跑） | 32 | 32 | 0 | 32 | 32 | 32 | `{1:32}` |
| ASCII 贪心 128 | 126 | 125 | **1** | 126 | 126 | 125 | `{1:126}` |
| **中文** 贪心 32 | 32 | **30** | **2** | 32 | 32 | 32 | `{1:32}` |
| **emoji** 贪心 32 | 32 | **31** | **1** | 32 | 32 | 32 | `{1:32}` |
| ASCII 采样 32（temp=1, top_p=0.95, seed） | 32 | 32 | 0 | 32 | 32 | 32 | `{1:32}` |
| 中文 采样 32 | 32 | **30** | **2** | 32 | 32 | 31 | `{1:32}` |

再补一组"换个 `max_tokens` 值"的扫描（每种值连跑两次）：

| `max_tokens` | 1 | 7 | 64 | 256 | 512 |
|---|---|---|---|---|---|
| 实际片数 | 1 | 7 | 64 | **245** | **245** |
| 每片 token 数直方图 | `{1:1}` | `{1:7}` | `{1:64}` | `{1:245}` | `{1:245}` |

（256/512 都停在 245 片 = 模型自己吐了结束符提前停；两次结果一致。）

**还有最硬的一条**：正式那 120 个请求，**每个请求 32 片、共 32 个 token**，
`n_content_chunks` 全部等于 32，一次例外都没有。

#### 证据 B：唯一一次异常——服务刚起来后的**第一条流式请求**

```
写进 2 片，但一共 32 个 token：
  片 0  t=0.806 s   1 个 token   文本 ' Paris'
  片 1  t=0.820 s  31 个 token   文本 '. The capital of Italy is Rome. What is the c...'   finish_reason=length
  （之后还有一片只带 usage 的收尾片）
```

**这片里塞了 31 个 token，不是 1 个。** 而且能看到它为什么：

- 那次的首字等待是 **0.806 s**，而热起来之后同一个组合只要 **0.028 s**（**差 29 倍**）；
- 那 31 个 token 是在 **14 ms 内一起到达**的 → **0.45 ms/token**；
- 而稳态的逐片间隔是 **16.05 ms/token**。
- **0.45 比 16.05 快 35 倍** → 这批 token 不可能是那 14 ms 里真算出来的，
  它们是**在 0.8 s 那次停顿期间就生成好了、然后被一次性冲出来**。

**这一次之后无论怎么重跑都不再出现**：热重跑 1 次、120 个正式请求、10 次换 `max_tokens` 的新形状请求，
全部都是严格的一片一个 token。

**可核实的事**：这次异常发生在 `14:17:09`；在此之前这台服务收到过的**成功生成请求都是我两条
非流式的 curl 冒烟**，而并行会话的流式请求全部因为路径写错而 404、**根本没走到引擎**。
所以这很可能是"**这台服务的第一条真正走到引擎的流式请求**"——
机制上像是流式输出通路的一次性初始化，停顿期间输出攒着、结束时一起推。
**这一条是推测，不是结论**：要坐实必须重启服务专门复现一次，而本次没有（见 §6 的说明）。

#### 结论（**必须照这个口径引用**）

1. **稳态、本版本本配置下：一个输出片 = 一个 token。** 三条独立证据一致，几十次请求零例外。
2. **但有两类明确的例外，都不能忽略**：
   - **服务刚启动后的第一条流式请求**可能把多个 token 挤进一片（实测一次塞了 31 个）；
   - **文本为空的片**：中文 / emoji 会因为"一个字符的字节被切成两半、要等下一片才凑齐"而出现
     `text=""` 的片。**这类片仍然是一个 token，但尺子数不到**（见 §2.4 的"带文本的片"一列）。
3. **本次没有测**：`--stream-interval` 改成非默认值、投机解码/多 token 一步产出
   （官方明确说过这些会让"逐 token 间隔"和"输出块间隔"不一致）。所以在这些模式下
   **不能假设一片一个 token**。

---

## 3. `/metrics` 实测：那些 KV 传输指标到底在不在？

抓取方式（原样存盘，未做任何加工）：

```
curl -s http://127.0.0.1:8090/metrics -o metrics_raw.txt     # 92,703 字节
```

| 产物 | 位置 |
|---|---|
| 原始全文 | `sim/results/b0_vllm/metrics_20261007T061845Z_raw.txt` |
| 按关键词过滤后的输出 | `sim/results/b0_vllm/metrics_20261007T061845Z_filtered.txt` |

**实测结果（只报"在这个版本上看到了什么"）：**

| 关键词 | 命中行数 | 说明 |
|---|---|---|
| **`nixl`** | **0** | **B0 报告里列的那些 KV 传输指标，一个都没有出现** |
| `kv_transfer` | 2 | **只作为一个标签值**出现：`vllm:prompt_tokens_by_source_total{source="external_kv_transfer"} 0.0` |
| `kv_cache` | 4 | 本机 KV cache 的**配置与占用**，不是跨机传输 |
| `transfer` | 3 | 其中 1 行在另一条指标的**帮助文字**里（"等待原因之一：KV 传输"） |
| `prefix_cache` | 25 | 前缀缓存命中/查询计数（本机行为） |
| `kv_connector` / `motor` | 0 | 未出现 |

**所以明确地回答**：`vllm:nixl_bytes_transferred`、`vllm:nixl_xfer_time_seconds`、
`vllm:nixl_post_time_seconds`、`vllm:nixl_num_descriptors`、`vllm:nixl_num_failed_transfers`、
`vllm:nixl_num_failed_notifications`、`vllm:nixl_num_kv_expired_reqs`、
`vllm:nixl_num_notifications_after_expiry` ——
**在这个版本、这个配置上全部未出现。**

**唯一沾边的东西是** `vllm:prompt_tokens_by_source_total{source="external_kv_transfer"}`，
它是一个**按 token 计数的计数器**，值 **0.0**。它和我们真正想要的
**"传了多少字节 / 传了多久 / 失败几次"是三样不同的东西**，粒度也完全不同
（它是一个累计计数，不带请求 ID，**不能**拿来算某个请求的 KV 传输耗时）。

**要看到 `nixl_*` 还需要什么条件**（照实写，本次没做）：

1. 装 KV 连接器相关的依赖（`nixl`、以及 `lmcache` / `mooncake-transfer-engine` 等）；
2. **以拆分模式启动**：两端各一个 vLLM 实例，配上 `--kv-transfer-config`（指定 `NixlConnector`
   之类的连接器）+ 前置的参考代理，让 KV 真的从一台传到另一台；
3. 传输后端（默认 UCX）与网络设备按 `UCX_TLS` / `UCX_NET_DEVICES` 配好。

**本次是单实例、没开拆分模式**，所以这些指标**未出现是符合预期的**，不是"指标没了"。

> ⚠️ **不写任何性能结论。** 官方的拆分功能仍是**实验性**，且**并不直接提高总吞吐**。
> 本节只回答"看到了什么"，不回答"好不好"。

---

## 4. 顺带读到的一组有用事实（来自 `/metrics` 的配置信息）

`vllm:cache_config_info` 这一条把服务端的缓存配置整个暴露出来了，可以直接引用：

| 字段 | 值 |
|---|---|
| `num_gpu_blocks` | 4312 |
| `kv_cache_size_tokens` | 68,992 |
| `block_size` | 16 |
| `enable_prefix_caching` | `True` |
| `gpu_memory_utilization` | `0.7` |

---

## 5. 对 `measure_serving.py` 改动的完整记录

**硬性约束要求：不改现有文件；若必须改，先写清原因，并保证原有的自检与假后端结论不被破坏。**
本次实际发生了一次改动 + 一次修补，两条都记在这里。

### 5.1 改动一：加 `--model`（**不是我加的**，但必须记）

改动的**原因**是硬的：尺子原来把请求体里的模型名**写死**成 `"fake-model"`，
而**真 vLLM 会校验模型名并返回 404**：

```
POST /v1/completions  {"model": "fake-model", ...}
→ HTTP 404  {"error":{"message":"The model `fake-model` does not exist."}}
```

**实证**：改动前直接拿尺子打真服务，**8 个请求 8 个 `http_4xx`**（产物
`serving_20261007T061807163Z_5304-1_*`）。改动后同一命令 **120/120 成功**。

**改动内容**（最小、向后兼容）：客户端参数组加 `--model`，**默认值仍是 `"fake-model"`**，
并透传到 `MeasureCfg.model` / `run_client`。因为默认值没变，**打假后端的既有路径逐字节不变**。

> 记录一句实话：这一处改动是**并行的另一个会话**在同一台 WSL 里做的（该会话同时在
> `/root/b0vllm` 下起了一个 vLLM 实例，日志名 `vllm_serve_kimi*.log`，端口也是 8090）。
> 本次测量复用了那台实例，**改动本身我核对过**，并做了下面的回归验证。

### 5.2 改动二：修补被这个改动**弄坏**的 `battery`（**这次是我改的**）

**发现的问题**：`--model` 只在 `run_client` 的**命令行路径**上接通了，
而 `run_battery()` 里手搓的两处 `argparse.Namespace`（臂 A/B 与故障注入臂）**没有 `model` 键**。
后果是 `battery` 角色一跑就死：

```
File "measure_serving.py", line 1035, in run_client
    model=args.model,
AttributeError: 'Namespace' object has no attribute 'model'
```

**这正是硬性约束要防的那种破坏**：`battery` 是 B0 的验收电池，它坏了 = 原有结论没法复验。

**修补**：给那两处 `Namespace` 补上 `model="fake-model"`。**刻意写死字面量、不跟 `--model` 走**——
电池永远打自己的假后端，不该因为用户传了一个给真服务用的模型名而改变行为。
（两处都加了注释说明，避免下次再漏。）

**回归证据（三条，都是本次实跑）**：

| 检查 | 结果 |
|---|---|
| `selftest`（含会 FAIL 的负对照） | **全部通过**（末行 `selftest 全部通过`） |
| `battery` 全套 | **PASS 24 / FAIL 0 / INFO 1** —— 与 `B0_REPORT.md` §1 记录的 **24 PASS / 0 FAIL / 1 INFO 完全一致** |
| 对真服务的 120 请求 | **120/120 成功** |

产物：`battery_20261007T061928857Z_6035-1_{checks.csv,battery.json}` 与 `serving_20261007T06193*_*`。

**没有被改的东西**：URL 拼法一行未动。§2 开头那条"`--url` 不要带 `/v1`"是**用法说明**，不是 bug。

---

## 6. 结论怎么影响已有指标命名（**这一节是硬要求，不能含糊**）

**答案：不能确认"一片恒等于一个 token"，所以下面这些名字必须改。**

| 现有名字（`measure_serving.py` 里的字段） | ❌ 不能叫 | ✅ 应该叫 / 引用时的正确说法 |
|---|---|---|
| `n_content_chunks` | "token 数"、"输出 token 数" | **"输出块数"**（且它只数**文本非空**的块，中文/emoji 下会**偏小**） |
| `tpot_p50_s` / `tpot_p95_s` / `tpot_p99_s` / `tpot_mean_s` / `tpot_max_s` | "逐 token 间隔"、"每 token 耗时" | **"输出块间隔"**；**不得当作"逐 token 间隔"使用** |
| `_tokens.csv` 的 `gap_s` / `t_rel_s` | "每个 token 的到达时刻" | **"每个输出块的到达时刻"** |
| 覆盖率里的 `decode_stream` | —（这个没问题） | 仍然成立：它是"首个内容块→末个内容块"的区间 |

**并且要同时写清适用条件（这是本次实测给的边界）**：

1. 在**稳态**且**没有特别配置**时，本版本（vLLM 0.31.0，`stream_interval` 用默认值、
   没开投机解码）实测**输出块数 = token 数**，所以"块间隔"与"逐 token 间隔"**数值上是同一个东西**——
   但**名字仍要按上面的改**，否则一旦换配置就会静默出错。
2. **两种已知会破坏 1:1 的情形**必须写进任何使用说明：
   - 服务刚起来的**第一条流式请求**（实测一片塞了 31 个 token）；
   - **文本为空的块**（中文/emoji 实测存在，块仍是 1 个 token，但 `n_content_chunks` 数不到）。
3. **本次未验证**：非默认 `--stream-interval`、投机解码等"一步出多个 token"的模式。
   官方明确说过这些模式下逐 token 间隔与输出块间隔会不一致，**所以绝不能默认它们也 1:1**。

**给 B0 两处文字的具体改法建议**（不改文档，只给口径）：
`B0_REPORT.md` §2.1 表格里那一行 "逐字间隔（TPOT）" 与 "内容片段数"，
在引用到**真服务**时必须改成 "输出块间隔" 与 "输出块数"，
并在脚注里带上 §2.4 的两条例外。

---

## 7. 我没做到的 / 没验证到的（一条都不含糊）

**关于环境与服务**

1. **这台 8090 上的 vLLM 不是我用本会话启动的。** 它由**并行的另一个会话**在同一台 WSL 里启动
   （日志 `vllm_serve_kimi*.log`、venv 与模型缓存与本会话共用）。我**核对了它的完整命令行、
   环境变量与日志**，本次测量全部复用它。我**没有**杀掉它（见 §8）。
2. **我自己起服务的一路没走通**：因为 `/tmp` 是 tmpfs、缺 gcc、缺 `Python.h`、
   缺 nvcc、显存预算取 0.70/0.80/0.85 试过 —— 失败原因与修复都记在 §1.3 的日志里
   （`vllm_serve_attempt1..4_*.log`）。**最终生效的那套参数组合没有由我独立复现到"起得来"。**
3. **WSL2 不是裸机 Linux**：时钟由 Hyper-V 同步、回环是虚拟网络栈。**真 Ubuntu 服务器上必须再跑一遍。**

**关于四个问题**

4. **"冷启动一次塞 31 个 token"没有专门复现**。要坐实必须**重启服务**再打第一条流式请求，
   而那台服务不是我的（§1），所以只做了"热态无法复现"的反向验证。
   机制解释是**推测**，请按 §2.4 的口径引用。
5. **`nixl_*` 指标没看到，是因为没开拆分模式**——这点符合预期，但**本次也确实没做**：
   没装 `nixl`、没配 `--kv-transfer-config`、没起两端实例、没做任何真实 KV 传输。
   所以"拆分模式下这些指标会不会出现"**仍然未验证**（B0 的 V1/V4 依旧未闭合）。
6. **服务端语义阶段（排队/前处理/KV 传输/生成）全部拿不到**：真 vLLM 不发 `x_stage_report`
   （`stage_report_seen = 0/120`）。**逐请求的 KV 传输耗时仍然没有出口**（B0 的 V5 依旧未闭合）。
7. **`429` / `503` 造不出来**：40、80 并发下全是 200，vLLM 排队不拒绝。
   所以"限流分类对不对"**本次仍然没验**，且尺子里 **429 没有独立类别**这件事依旧成立。
8. **只测了 `/v1/completions`（旧式补全接口）**。`/v1/chat/completions` 只做了冒烟（200），
   **没有用尺子量**；两者的分块行为是否一致**未验证**。
9. **非流式路径没量**（B0 的 M7 依旧未闭合）。
10. **`--arrival poisson` 没量**，本次两次正式跑都是 `uniform`（B0 的 M9 依旧未闭合）。
11. **没有跨机、没有两个时钟域**：全程同机回环，所以 `srv_outside_s` 这类"时钟对齐体检量"
    在真网络下是什么量级**完全没有数**（B0 的 M4 依旧未闭合）。
12. **没有长跑**：单次最长 120 请求、约 10 秒。
13. **只有一个模型、一块卡、一个进程生命周期**。1.5B + 单卡 + 4096 上下文以外的情况没验。
14. **采样侧只测了 6 个组合**：`temperature ∈ {0, 1}`、`top_p ∈ {默认, 0.95}`。
    其它截断参数、`stop`、`repetition_penalty` 等**没测**。
15. **两次正式跑都在同一台"共享服务"上做**（并行会话也在打它）。虽然结果很干净
    （0 失败、片数整齐），但**不能排除**共享带来的扰动；结论的方向不受影响，绝对时延值要谨慎。

**关于工具**

16. **我没有独立验证并行会话对 `measure_serving.py` 的那处改动是否还有其它副作用**。
    我只验了三条回归（selftest / battery 全套 / 真服务 120 请求），
    **Windows 侧没跑**（本次全在 WSL）。
17. **Windows 侧的假后端 + battery 没有重跑**。改动是向后兼容的（默认值未变），
    但"Windows 上也一样"是**推断**，不是本次实测。

---

## 8. 服务与端口的收尾状态

| 项 | 状态 |
|---|---|
| vLLM 服务 | **仍在运行**（`pid 3916`，`127.0.0.1:8090`） |
| 8090 端口 | **仍被占用**（不是本次会话启动的进程） |
| 处置 | **没有杀**。理由见下 |

**为什么没停**：这台服务是**并行的另一个会话**启动的（§5.1、§7.1），
它当时可能仍在做自己的测量。杀掉别人正在量着的服务会**静默毁掉对方的数据**，
这比"留一个占着显存的服务"更糟。本次遵守"不做跨会话的不可逆动作"。

**要停的话，一条命令**（按进程号，不用 `pkill`；本次约束明令不许用宽泛的 `pkill`）：

```bash
wsl -d Ubuntu -u root -- bash -lc "kill 3916"     # 或 systemctl stop b0vllm（若是我用 systemd 起的那个单元）
# 确认端口释放：
wsl -d Ubuntu -u root -- bash -lc "ss -ltn | grep 8090 || echo '8090 已释放'"
```

---

## 9. 复现命令（在 `sim/` 目录下，解释器用 WSL 的系统 `python3`）

```bash
# 0) 环境一次到位（本机已经装好了；换机器时按 §1.3 的六条坑依次处理）
#    重点：venv 与 HF_HOME 放 WSL 原生盘、TMPDIR 别落在 tmpfs、装 gcc/g++/python3-dev
export HF_HOME=/root/hf-cache HF_HUB_OFFLINE=1 VLLM_USE_FLASHINFER_SAMPLER=0

# 1) 起服务（本次生效的那套参数）
/root/vllm-env/bin/vllm serve Qwen/Qwen2.5-1.5B-Instruct \
  --host 127.0.0.1 --port 8090 --max-model-len 4096 --dtype float16 \
  --gpu-memory-utilization 0.70

# 2) 用尺子量它（注意 --url 不要带 /v1，模型名必须填对）
python3 k8s_test/measure_serving.py client --url http://127.0.0.1:8090 \
  --model Qwen/Qwen2.5-1.5B-Instruct --label vllm_real \
  --requests 120 --qps 12 --concurrency 16 --prompt-tokens 64 --max-tokens 32 \
  --save-tokens --out-dir results/b0_vllm

# 3) 片 vs token 的核对 + 错误场景
python3 k8s_test/probe_vllm_chunking.py chunking --url http://127.0.0.1:8090 \
  --model Qwen/Qwen2.5-1.5B-Instruct
python3 k8s_test/probe_vllm_chunking.py errors --url http://127.0.0.1:8090 \
  --model Qwen/Qwen2.5-1.5B-Instruct

# 4) /metrics
curl -s http://127.0.0.1:8090/metrics -o results/b0_vllm/metrics_raw.txt

# 5) 回归：证明 `--model` 那处改动没破坏既有结论
python3 k8s_test/measure_serving.py selftest
python3 k8s_test/measure_serving.py battery --out-dir results/b0_vllm
```

---

## 10. 产物清单（全部在 `sim/results/b0_vllm/`）

| 文件 | 内容 |
|---|---|
| `serving_20261007T061821151Z_5377-1_{requests,tokens,summary,manifest}` | **对真服务的正式测量**：120 请求逐请求明细 + 3840 行逐片时刻 |
| `serving_20261007T061807163Z_5304-1_*` | **改动前**的对照：8/8 `http_4xx`（模型名写死导致 404） |
| `battery_20261007T061928857Z_6035-1_{checks.csv,battery.json}` | 回归用：验收电池 **PASS 24 / FAIL 0 / INFO 1** |
| `serving_20261007T06193*_6035-{2..8}_*` | 上面那轮电池的各臂明细 |
| `probe_20261007T061709Z_{compare.csv,chunks.jsonl,summary.json}` | **片 vs token 第一轮**（含那次冷启动异常，`chunks.jsonl` 有每片原文） |
| `probe_20261007T061736Z_*` | 片 vs token 第二轮（热态，异常不复现） |
| `probe_20261007T061758Z_errors.csv` | **12 个错误场景**的请求、状态码、服务端原文、尺子的分类 |
| `chunk_vs_token_all_runs.csv` | 把上面两轮合并成一张**片/token 对照总表**（12 行） |
| `metrics_20261007T061845Z_raw.txt` | `/metrics` **原始全文**（92,703 字节） |
| `metrics_20261007T061845Z_filtered.txt` | 按关键词过滤后的输出（含 `nixl` 命中 0 行） |
| `env_snapshot_20261007T062050Z.txt` | 环境快照（OS/内核/GPU/Python/包版本/服务端命令行） |
| `vllm_server_log_20261007T062138Z_copy.log` | 服务端日志副本（**只读复制，未改动原件**） |
| `vllm_server_keylines_20261007T062138Z.txt` | 日志里的关键行（参数、权重加载、KV cache 容量） |

在 WSL 内（不属于仓库）：`/root/b0vllm/logs/` 下有 5 份**失败尝试**的完整日志
（`pip_install_attempt1_osp_error.log`、`vllm_serve_attempt1..4_*.log`），§1.3 的每条坑都能在里面找到原文。

---

## 11. 一句话总结

**尺子接到真 vLLM 上是通的**：120 请求零失败、首字等待 45.6 ms、逐片 16.1 ms、
结束标志零误判、错误分类 12/12 全对。
**但"一个片就是一个 token"只在稳态成立**——服务刚起来的第一条流式请求实测把 31 个 token 塞进了一片，
中文/emoji 还会产生"文本为空的片"让尺子少算 token。
**因此 `n_content_chunks` 与 `tpot_*` 必须按 §6 改名**，否则这两个数会在某些条件下静默说谎。
`/metrics` 里 `nixl_*` **一个都没有**（本次没开拆分模式，符合预期），
**要看到它们必须真的搭起拆分式传输，本次没做。**

---

# §口径修正（2026-10-07 的第二轮，追加在本文件末尾）

> 上一轮（本文件 §6）只写了"字段必须改名"的**口径建议**，**代码一行没动**。
> 这一轮把代码也改了，并且补上"改完之后两套口径各自等于多少"的实测对照。
> 只改了一个**代码**文件：`sim/k8s_test/measure_serving.py`（其余代码/脚本按要求未动）。
> 新增产物在 `sim/results/b0_vllm/`（Linux 侧那一批在 `sim/results/b0_vllm_linux/`，见 §12.8）。

## 12.0 一页话（不看术语也能读）

**这一轮要修的问题**：工具里有一组字段名字叫 `tpot_*`（读作"每个 token 要多久"），
还有一个计数叫 `n_content_chunks`。但它们其实数的是**输出块**，不是 token。
真 vLLM 实测下来这两者**不总相等**。

打个比方：原来那把尺子上贴着"每根筷子有多长"，实际量的却是"每次上菜间隔多久"——
**大多数时候一口菜正好一根筷子，看起来没错；但只要有一次端上来一盘（几个 token 挤一块），
或者有一次只端了个空盘子（有个 token 的文本是空的），这个数就悄悄错了。**

这一轮做了四件事：

1. **旧字段一个都没删、没改名**（`tpot_*`、`n_content_chunks` 原样还在），
   只在它们的注释和文档里写清楚："这是**块口径**的数，**不能当逐 token 间隔用**"。
2. **新增一组块口径字段**：`chunk_interval_*`（块的间隔）、`n_output_chunks`（块数）、
   `n_empty_text_chunks`（"有 token 但文本是空的"那些块）。
3. **新增服务端 token 数字段**：`srv_completion_tokens` 等。
   **服务端报了才填，没报就留空——绝不拿块数去顶替。**
4. **把根因用一次单请求实测钉死**：中文提示词下 32 个 token 里有 5 个"文本是空的块"，
   每一个这样的块里**确实带着 1 个 token 号**（原始输出见 §12.5 的取证文件）。

**三条结论**：

- **口径修好了**：块间隔与旧 tpot 在"没有空文本块"时**逐位相同**（英文那组），
  有空文本块时才分开，方向也说得清（旧口径偏大）。
- **服务端 token 数这次真取到了**：4 组真服务共 80 个请求，
  `srv_completion_tokens` 全部等于 32，来源都记为 `usage`。
- **回归全过**：`selftest` 通过、`battery` **30 PASS / 0 FAIL / 1 INFO**（Windows 与 WSL 两侧一致），
  真 vLLM 4 组各 20 请求 **80/80 成功**。

**照实说没做到的**：

- **"服务刚起来第一条流式请求把 31 个 token 挤进一块"这次没复现**——要复现必须**重启服务**，
  而 8090 上那台 vLLM **不是我起的**（见 §8），所以我没重启、也没杀它。
  这一条仍然只有上一轮的记录（§2.4），机制解释仍是推测。
- 单块里到底装了几个 token，**本次只在"空文本块"这一种情形上逐块核对过**
  （核对结果：每个空文本块带 1 个 token）。其它情形（如冷启动那种一块塞 31 个）没逐块核。

## 12.1 改了哪些字段（行号是改完之后的 `measure_serving.py`；行号会随以后编辑漂移，名字不会）

**新增的字段**（都写在 `ReqResult` 上，定义在 774–803 行）：

| 字段 | 行号 | 它是什么 | 怎么读 |
|---|---|---|---|
| `n_output_chunks` | 777 | **输出块数**：服务端一次推过来的那一小段，都算一块 | 这才是"块数"的正确名字 |
| `n_empty_text_chunks` | 778 | 其中**文本是空的**块数（中文/emoji 下会出现） | 这类块**也是块**，旧口径直接漏掉 |
| `chunk_interval_p50_s` / `p95` / `p99` / `mean` / `max` | 793–797 | **输出块之间的间隔**（含空文本块） | 这才是"块间隔"的正确名字 |
| `usage_report_seen` | 799 | 这次流里有没有出现过服务端自报的用量 | 0/1 |
| `srv_completion_tokens` | 800 | **服务端自己报的生成了几个 token**（`usage.completion_tokens`） | 拿不到就是**空**，不是 0 |
| `srv_token_ids_total` | 801 | 服务端**逐块**自报的 token 个数之和（vLLM 的 `return_token_ids`） | 与上一行是两个独立来源 |
| `srv_token_count_source` | 802 | 上面两个里**哪个被采用**：`usage` / `token_ids` / 空（都没拿到） | 空＝不可用 |
| `token_interval_mean_s` | 803 | **逐 token 平均间隔**（代理量） | 只在拿到服务端 token 数且 >1 时有值；**不是**逐 token 时间戳的分位数 |

**保留不改的旧字段**（同样在 774–803 行内）：

| 旧字段 | 行号 | 现在的说法 | 动过什么 |
|---|---|---|---|
| `n_content_chunks` | 774 | **内容块数**（文本非空的块），**不是 token 数** | 只改了注释与文档；值算法不变 |
| `tpot_p50_s` / `p95` / `p99` / `mean` / `max` | 787–791 | **历史命名**，按"文本非空的内容块"算的间隔 | **没删、没改名**；注释里写明"**不得当作逐 token 间隔**" |

> 为什么不干脆删掉旧字段：旧 CSV（`results/b0*/serving_*`）就是按这些名字存的，
> 删了或改名会让**已经落盘的产物无法复核**。所以旧字段留着当"历史口径"，
> 新字段用正确名字，两个并排出现，谁也不会误认。

**同时改的非字段位置**（供复核用）：

| 位置 | 行号 | 改了什么 |
|---|---|---|
| 文件头 docstring | 46–64 | 新增"口径：输出块 ≠ token"一节 |
| 纯函数 `interval_stats()` | 265 | 新增：把一串块时刻换成间隔分位（selftest 直接验它） |
| 假后端配方 `empty_text_every` | 427 | 新增：掺"文本为空"的输出块（**不用显卡也能验块口径**） |
| 假后端回包 | 574–575、688–717 | 收到请求才回 `usage` / 逐块 `token_ids` |
| 纯函数 `classify_event()` | 865 | 新增：一条 SSE 归成 `output` / `usage_only` / `other` |
| `MeasureCfg` | 902、907–908 | 新增 `prompt_text` / `include_usage` / `return_token_ids` |
| 请求体与流式循环 | 945–957、1006–1020 | 加两个取证开关；每个**输出块**都记时刻（含空文本块） |
| `analyze_samples()` | 1098–1149 | 两套间隔分别计算；新增服务端 token 数与前缀判断 |
| `summarize_batch()` | 1337、1346、1402–1430 | 新增 `chunks` / `server_tokens` 两个汇总块、`tpot_mean_s` |
| `print_summary()` | 1459 起 | 打印时把两套口径并排打出来 |
| `selftest` 第 4 节 | 1598 起 | 新增块口径的纯逻辑自检（含"两种口径何时相同"的钉桩） |
| `battery` 新臂 | 1672、1784–1860 | 新增参数模板 `client_args()` 与 **C1–C6** 六项检查 |
| 命令行 | 1999–2005 | 新增 `--prompt-text` / `--no-include-usage` / `--no-return-token-ids` / `--empty-text-every` |

**字段之间的一个恒等式**（看数的时候很有用）：

```
n_output_chunks = n_content_chunks + n_empty_text_chunks
```
（块总数 = 有文本的块 + 文本为空的块。旧口径只给了中间那一项，所以既不等于块数、也不等于 token 数。）

## 12.2 新旧口径为什么不同

三条原因，从"最常见"到"最罕见"：

1. **文本为空的块**（中文/emoji，最常见）。
   一个中文字的字节被劈成两半时，vLLM 会先推一个**文本是 `""`、但带着 1 个 token 号**的块，
   下一个块才把字凑出来。
   **旧口径只数"文本非空"的块**，于是：块数被数小；而且它算"间隔"时会跳过这个块，
   把**两段间隔并成一段**，所以**旧 tpot 会偏大**。
2. **一个块里塞多个 token**（服务刚启动的第一条流式请求，上一轮实测过一次：31 个 token 一块）。
   这时块数 < token 数，块间隔也**没有**逐 token 的含义（那 31 个 token 是"一起冲出来"的，
   中间并没有 31 次间隔）。
3. **服务端自己决定怎么分块**（`--stream-interval` 非默认值、投机解码等）。
   官方明确说过这些模式下"逐 token 间隔"与"块间隔"会不一致。**本次没测这些模式**
   （与本文件 §2.4 第 3 条同一条限制）。

正因为第 1 条，**连"块数"这个词都得重新定义**：块数要含空文本块，而旧字段不含。
两者的关系就是 §12.1 末尾那条恒等式。

## 12.3 三个例子（都用本次实测）

### 例① 中文「中国的首都是」——32 个 token，只有 27 个块有文本

一次单请求取证（原始输出存成 `chunk_probe_20261007T091901Z_text_empty_with_token_ids.txt`）：

```
输出块 32，其中文本为空 5，token_ids 合计 32
  空文本块 #11: token_ids=[10236] finish=None
  空文本块 #12: token_ids=[118]   finish=None
  空文本块 #18: token_ids=[66521] finish=None
  空文本块 #31: token_ids=[8908]  finish=None
  空文本块 #32: token_ids=[232]   finish='length'
usage: {'prompt_tokens': 3, 'total_tokens': 35, 'completion_tokens': 32}
```

**怎么读**：5 个"文本为空"的块，**每个块里都明明白白带着 1 个 token 号**
——这就把"有 token 但文本为空"从推测变成了事实。
注意 #11 和 #12 是**连着**的两个空块，这也是为什么后面看到的偏差不是简单的 32÷27。

**这一组在 20 个请求上的汇总数**（就是上面那位"27 片"的来源）：

| 口径 | 值 | 说明 |
|---|---|---|
| 输出块数（新的正确口径） | **32** | 与 token 数一致 |
| 内容块数（`n_content_chunks`，旧口径） | **27** | **旧口径看到的数**；当成 token 数就少算了 5 个 |
| 空文本块数（新字段） | **5** | 这类块旧口径完全看不见 |

### 例② emoji 与另一条中文提示词——各只差 1 个块

| 提示词 | 输出块 | 内容块 | 空文本块 |
|---|---|---|---|
| `Repeat this exactly: 🚀🌏🔥✨ ` | 32 | 31 | 1 |
| 「你好，请介绍一下你自己」 | 32 | **31** | 1 |

**怎么读**：这两条各只差 1 个块，所以旧口径的偏差很小（见 §12.4 的比值 ≈1.00–1.03）。
**这说明偏差有多大取决于文本内容，事前不可能知道**——这正是"不能无条件信任旧名字"的理由。

### 例③ 启动期：一个块里塞 31 个 token（**上一轮的记录，本轮没复现**）

本文件 §2.4 记录过：服务刚起来后的第一条流式请求写成 **2 个块**，但里面是 **32 个 token**
（第 2 块一次装了 31 个）。**这次没有复现**：复现要重启服务，而 8090 上那台不是本会话起的
（§8、§12.6 第 1 条）。

**如果它再出现，新字段会怎么暴露它**：`n_output_chunks` = 2 而 `srv_completion_tokens` = 32
——两个数**差一个数量级**，一眼就能看出"这次不能按块报 token"。
而旧字段只能看到"32 个内容块/32 片"这种**看起来完全正常**的数，
**这就是静默出错**。这也是为什么新字段必须**两个都留着、并排看**。

### 反例：英文提示词——两套口径**逐位相同**

英文那组 32 个块里一个空文本块都没有，于是
`chunk_interval_mean` 与历史 `tpot_mean` 的中位数**都是 0.015610 s**（20 个请求全部如此）。
**这条很重要**：它说明新口径不是在"换个数"，而是"在旧口径会错的地方才不同"。

## 12.4 ★ 新旧口径并排对照（真 vLLM，每组 20 个请求，全部 `max_tokens=32`）

对照总表：`sim/results/b0_vllm/chunk_vs_token_summary_20261007T091830Z.csv`

| 提示词 | 输出块 | 内容块 | 空文本块 | **块间隔**均值 | **历史 tpot** 均值 | 旧/新 | **块间隔** p95 | **历史 tpot** p95 | 旧/新 | `srv_completion_tokens` |
|---|---|---|---|---|---|---|---|---|---|---|
| 英文（`x `×64） | 32 | 32 | 0 | **0.015610** | **0.015610** | **×1.000** | 0.016851 | 0.016851 | ×1.000 | 32 |
| 中文「中国的首都是」 | 32 | **27** | **5** | **0.015882** | **0.017652** | **×1.111** | 0.020363 | **0.028254** | **×1.388** | 32 |
| 中文「你好，请介绍一下你自己」 | 32 | 31 | 1 | 0.015803 | 0.015825 | ×1.001 | 0.020321 | 0.020150 | ×0.992 | 32 |
| emoji `🚀🌏🔥✨` | 32 | 31 | 1 | 0.015871 | 0.016400 | ×1.033 | 0.020524 | 0.020834 | ×1.015 | 32 |

**怎么读这张表（三句话）**：

1. **第一行是基准线**：没有空文本块时，两列**完全一样**（×1.000）——说明两套口径只在旧口径出错时分开。
2. **第二行是要紧的那一行**：中文那条提示词下，旧口径把"块间隔"报成 **0.017652 s**、
   最慢那一撮报成 **0.028254 s**；而正确的块口径是 **0.015882 / 0.020363 s**。
   也就是说：**旧口径把"最慢那一撮"高估了约 39%（×1.388），把均值高估了约 11%**。
   方向**一定是偏大**（跳掉块 ⇒ 把两段并成一段），这一点与理论预期一致。
3. **第三、四行说明偏差不是常数**：只差 1 个块时，偏差只有 0.1%–3%（甚至有 ×0.992 这种噪声级的反向），
   所以在没有空文本块的数据上，旧值与新值**几乎可以互认**；差多少完全取决于文本内容。

**单位提醒**：表里所有间隔单位都是**秒**。真 vLLM 这台机器上，稳态下一个块约 16 ms（块口径）。

**同一批请求的 token 口径读数**（结论都指向 32 个 token）：

| 口径 | 是什么 | 英文 | 中文（中都） | 中文（你好） | emoji |
|---|---|---|---|---|---|
| `srv_completion_tokens` | 服务端自报生成 token 数（`usage`） | 32 | 32 | 32 | 32 |
| `srv_token_ids_total` | 服务端逐块自报 token 数之和 | 32 | 32 | 32 | 32 |
| `token_interval_mean_s` | 逐 token 平均间隔（代理量） | 0.015610 | 0.015882 | 0.015803 | 0.015871 |
| `srv_token_count_source` | 上面哪个被采用 | `usage` | `usage` | `usage` | `usage` |

**逐请求层面也有一个例子**（中文「中国的首都是」的 `r00000`，同一行里的两套数）：

```
n_output_chunks = 32     n_content_chunks = 27     n_empty_text_chunks = 5
tpot_mean_s        = 0.0173534   ← 旧口径（跳过 5 个空文本块）
chunk_interval_mean_s = 0.0155477  ← 块口径（32 个块全算）
tpot_p95_s         = 0.0237262   ← 旧口径
chunk_interval_p95_s  = 0.0204812  ← 块口径
srv_completion_tokens = 32.0   srv_token_count_source = "usage"
```

## 12.5 回归结果（三项 + 一项附加）

| # | 检查 | 结果 | 证据（产物） |
|---|---|---|---|
| 1 | `measure_serving.py selftest`（纯逻辑，含会 FAIL 的负对照） | **PASS**（Windows 与 WSL 都跑，末行都是"selftest 全部通过"，**含新增的第 4 节**） | 控制台输出；新增检查在 selftest 4/4（14 项） |
| 2 | `measure_serving.py battery`（假后端两臂 + 故障注入 + **新臂**） | **PASS 30 / FAIL 0 / INFO 1**（Windows `battery_20261007T091847106Z_37248-1`；WSL `battery_20261007T091933017Z_24853-1`，两侧逐项一致） | `results/b0_vllm/battery_*_{checks.csv,battery.json}`、`results/b0_vllm_linux/battery_*` |
| 3 | 真 vLLM 小样本（4 组 × 20 请求 = 80） | **80/80 成功，0 失败**；新字段有值、旧字段仍在、`srv_completion_tokens` 全部取到（20/20 per 组） | `results/b0_vllm/serving_20261007T0917*`、`0918*` |
| 4 | 附加：CLI 路径打**假后端**（确认 `--model` 那处改动没破坏假后端路径） | **12/12 成功**，块间隔与 tpot **逐位相同**（都是 0.019985 s），覆盖 PASS 12/12 | `results/b0_vllm/serving_20261007T092027218Z_6704-1_*` |

**battery 的账目对得上**：上一轮记录的是 **24 PASS / 0 FAIL / 1 INFO**（本文件 §5.2），
这一轮 **30 PASS / 0 FAIL / 1 INFO** = 原来那 24 项**一项没丢**（S0、B1–B13）+ 新增 **C1–C6 六项全过**。

**新增的 C1–C6 是什么**（第三节"臂 emptychunks"：假后端每 3 个内容块掺 1 个空文本块，
并且**故意不发 `usage`**，只留 `return_token_ids`）：

| 检查 | 实测（Windows / WSL） | 判据 |
|---|---|---|
| C1 成功率与覆盖 | 120/120 成功，覆盖 PASS 120 | 空文本块不能丢请求、不能破坏覆盖 |
| C2 **块数 ≠ token 数** | 输出块中位 **32**（预期 32）、内容块 **21**（预期 21）、空文本块 **11**（预期 11） | 块口径把三类块分得开 |
| C3 块序列落盘 | `tokens.csv` **3840 行**（预期 120×32），其中 `text_empty=1` 的 **1320 行**（预期 120×11） | 每个块都留痕，空文本块也留痕 |
| C4 **块间隔 vs 历史 tpot** | 块间隔中位 **0.019986 s**（配方 0.02 s）；历史 tpot 中位 **0.030002 s** | 块口径 ≈ 配方；旧口径被跳过动作抬高（实测 ×1.50，与"32 块里只有 21 个内容块"的 32/21 ≈ 1.52 同量级） |
| C5 **服务端不给 token 数时必须留空** | 有 `usage` 的请求 **0/120**，`srv_completion_tokens` 中位 **None**，`srv_token_ids_total` 中位 **32.0**，来源 `{"token_ids": 120}` | 拿不到就留空，**没有拿块数冒充** |
| C6 请求了 `usage` 就必须取到，且收尾片不算块 | 有 `usage` 的请求 **120/120**，`srv_completion_tokens` 中位 **32.0**，空文本块合计 **0** | 只带 `usage` 的收尾片（`choices: []`）**不能**被算成"空文本块" |

（WSL 那侧只有 C4 的数值略有差别：块间隔 0.020005 s、历史 tpot 0.03001 s，属于平台计时噪声。）

> **为什么必须在电池里加这一臂**：如果没有它，"块口径修好了没有"就只能靠**有显卡的机器**验；
> 而这一臂把假后端当"已知答案"，在没有显卡的 Windows 上也能**逐项对账**——
> 这就是本轮唯一能确定性复现"块数 ≠ token 数"的地方。

## 12.6 本次没验证到的（一条都不含糊）

1. **"冷启动一块塞 31 个 token"没有复现**。复现必须重启 8090 上的服务，
   而那台服务**不是本会话起的**（本文件 §8；进程 `pid 3916`），
   我**没有重启、没有杀它**。所以这一条仍然只有上一轮的观测与推测，没有新证据。
2. **没有核对"每个块里到底装几个 token"（除空文本块以外）**。
   本次只对"空文本块"逐块核过（结果：每个带 1 个 token 号，见 §12.3 例①）。
   冷启动那种"一块 31 个 token"的形态**没有做过逐块核对**。
3. **只测了 `/v1/completions`**，`/v1/chat/completions` 仍然只做过冒烟（本文件 §7.8 的未闭合项照旧）。
   两种接口的分块行为是否一致**未验证**。
4. **没测非默认 `--stream-interval`、投机解码**等"一步出多个 token"的配置
   （与 §2.4 第 3 条同一限制）。**在这些模式下绝不能假设块数≈token 数。**
5. **`token_interval_mean_s` 是代理量，不是真实逐 token 间隔**。
   它由"服务端报的 token 数 + 解码窗口跨度"推出，**假设 token 在窗口内均匀分布**，
   而且服务端只给总数、不给每个 token 的时刻。**它没有分位数**（分位数要逐 token 时间戳，
   本次拿不到）。本次实测里它与块口径均值恰好在打印精度内相同，但这是**巧合于"每块 1 token"的稳态**，
   不是它的定义性质。
6. **只用了一台服务、一个模型（Qwen2.5-1.5B）、一块卡**，而且是**共享服务**（可能有别的会话在打它）。
   绝对时延值要谨慎看待；口径本身与共享无关。
7. **没有重跑历史批次的旧产物**。旧 CSV 里的 `tpot_*`、`n_content_chunks` 仍是旧口径的数，
   **不要**拿它们与新 CSV 的 `chunk_interval_*` 直接混着比。
8. **Windows 侧的真服务测量没做**（真服务绑在 WSL 的 127.0.0.1，Windows 连不上）；
   Windows 侧只跑了 selftest / battery / 假后端 CLI。

## 12.7 复现命令（在 `sim/` 目录下）

```bash
# ① 纯逻辑自检（Windows 与 WSL 各跑一次）
.venv/Scripts/python.exe k8s_test/measure_serving.py selftest
wsl -d Ubuntu -u root -- bash -lc "cd '/mnt/e/代码项目文件/论文/sim' && \
  python3 k8s_test/measure_serving.py selftest"

# ② 验收电池（含新臂 emptychunks；约 42 s）
.venv/Scripts/python.exe k8s_test/measure_serving.py battery --out-dir results/b0_vllm
wsl -d Ubuntu -u root -- bash -lc "cd '/mnt/e/代码项目文件/论文/sim' && \
  python3 k8s_test/measure_serving.py battery --out-dir results/b0_vllm_linux"

# ③ 真 vLLM 小样本（服务在 WSL 的 127.0.0.1:8090；--url 不要带 /v1；--model 必须填对）
wsl -d Ubuntu -u root -- bash -lc "cd '/mnt/e/代码项目文件/论文/sim' && \
  python3 k8s_test/measure_serving.py client --url http://127.0.0.1:8090 \
    --model Qwen/Qwen2.5-1.5B-Instruct --label vllm_ascii20 \
    --requests 20 --qps 8 --concurrency 8 --prompt-tokens 64 --max-tokens 32 \
    --save-tokens --out-dir results/b0_vllm"
# 中文/emoji 三组：把 --prompt-text 换成 "中国的首都是" / "你好，请介绍一下你自己" /
# "Repeat this exactly: 🚀🌏🔥✨ "，label 相应改成 vllm_zh_capital / vllm_zh_intro / vllm_emoji

# ④ 单请求取证：空文本块里到底有没有 token（输出存进 chunk_probe_*.txt）
#    见 §12.3 例①，脚本正文就是把 request body 加上 return_token_ids / logprobs /
#    stream_options.include_usage，然后逐块打印 text 与 len(token_ids)

# ⑤ 并排对照表（读 summary.json 与 manifest.json，落盘 chunk_vs_token_summary_*.csv）
.venv/Scripts/python.exe - <<'PY'
import csv, glob, json
from datetime import datetime, timezone
meta = {}
for p in glob.glob("results/b0_vllm/serving_*_manifest.json"):
    m = json.load(open(p, encoding="utf-8")); meta[m["run_id"]] = m
WANT = ("vllm_ascii20", "vllm_zh_capital", "vllm_zh_intro", "vllm_emoji")
rows = []
for p in sorted(glob.glob("results/b0_vllm/serving_*_summary.json")):
    d = json.load(open(p, encoding="utf-8")); m = meta.get(d["run_id"])
    if not m or m.get("label") not in WANT:
        continue
    lat, ck, st = d["latency"], d["chunks"], d["server_tokens"]
    rows.append({
        "label": m["label"], "run_id": d["run_id"],
        "prompt_text": m["params"].get("prompt_text") or "(默认 'x '×64)",
        "include_usage": m["params"]["include_usage"],
        "return_token_ids": m["params"]["return_token_ids"],
        "n_requests": d["n_requests"], "n_ok": d["n_ok"],
        "coverage_verdicts": json.dumps(d["coverage_verdicts"], ensure_ascii=False),
        "n_output_chunks_median": ck["n_output_chunks_median"],
        "n_content_chunks_median": ck["n_content_chunks_median"],
        "n_empty_text_chunks_median": ck["n_empty_text_chunks_median"],
        "n_empty_text_chunks_total": ck["n_empty_text_chunks_total"],
        "chunk_interval_mean_median_s": lat["chunk_interval_mean_s"]["p50"],
        "chunk_interval_p95_median_s": lat["chunk_interval_p95_s"]["p50"],
        "legacy_tpot_mean_median_s": lat["tpot_mean_s"]["p50"],
        "legacy_tpot_p95_median_s": lat["tpot_p95_s"]["p50"],
        "ratio_tpot_mean_over_chunk_mean": round(lat["tpot_mean_s"]["p50"] / lat["chunk_interval_mean_s"]["p50"], 4),
        "ratio_tpot_p95_over_chunk_p95": round(lat["tpot_p95_s"]["p50"] / lat["chunk_interval_p95_s"]["p50"], 4),
        "srv_completion_tokens_median": st["srv_completion_tokens_median"],
        "srv_token_ids_total_median": st["srv_token_ids_total_median"],
        "srv_token_count_source": json.dumps(st["srv_token_count_source_counts"], ensure_ascii=False),
        "token_interval_mean_median_s": st["token_interval_mean_s_median"],
        "requests_csv": d.get("requests_csv"), "tokens_csv": d.get("tokens_csv"),
    })
rows.sort(key=lambda r: WANT.index(r["label"]))
ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
out = f"results/b0_vllm/chunk_vs_token_summary_{ts}.csv"
with open(out, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
print("wrote", out)
PY

# ⑥ 手动看"块 ≠ token"（不用显卡，任何机器都能跑）
.venv/Scripts/python.exe k8s_test/measure_serving.py fake-backend --port 9511 \
  --token-count 32 --empty-text-every 3
.venv/Scripts/python.exe k8s_test/measure_serving.py client --url http://127.0.0.1:9511 \
  --label fake_emptychunks --requests 12 --qps 6 --out-dir results/b0_vllm
```

## 12.8 本轮新增产物（全部 append-only，未覆盖任何旧文件）

**`sim/results/b0_vllm/`**：

| 文件 | 内容 |
|---|---|
| `serving_20261007T091747798Z_24506-1_*` | 真服务 · 英文提示词 · 20 请求（两套口径逐位相同的基准线） |
| `serving_20261007T091805363Z_24618-1_*` | 真服务 · 中文「中国的首都是」· 20 请求（**27 个内容块 / 32 个块**） |
| `serving_20261007T091808561Z_24644-1_*` | 真服务 · 中文「你好，请介绍一下你自己」· 20 请求 |
| `serving_20261007T091811757Z_24670-1_*` | 真服务 · emoji · 20 请求 |
| `serving_20261007T092027218Z_6704-1_*` | 假后端 CLI 冒烟（确认 `--model` 没破坏假后端路径） |
| `battery_20261007T091602434Z_25784-1_*` | Windows 电池（改完即跑的第一版） |
| `battery_20261007T091847106Z_37248-1_*` | **Windows 电池（最终代码）** |
| `battery_rerun_win_console.txt` | 上面那一轮的完整控制台输出 |
| `chunk_vs_token_summary_20261007T091830Z.csv` | **新旧口径并排对照总表**（§12.4 的数据源） |
| `chunk_probe_20261007T091901Z_text_empty_with_token_ids.txt` | **单请求取证**：空文本块里确实带 token 号 |
| `chunk_vs_token_summary_20261007T091823Z.csv` | *空文件（0 字节）*：一次失败脚本的残留（它先建文件后报错），**不要引用**，有效的那个是 `…091830Z` |

**`sim/results/b0_vllm_linux/`**（本轮新目录：WSL 侧的 selftest/battery 与假后端各臂产物）：

| 文件 | 内容 |
|---|---|
| `battery_20261007T091655552Z_23606-1_*` | WSL 电池（改完即跑的第一版） |
| `battery_20261007T091933017Z_24853-1_*` | **WSL 电池（最终代码）** |
| `serving_20261007T0916*`、`serving_20261007T0917*`、`serving_20261007T0919*`、`serving_20261007T0920*` | 两轮电池各自的内层客户端产物 |

## 12.9 一句话总结（口径修正）

**旧名字不能再当"逐 token"用了，但旧字段一个没删**——`tpot_*` / `n_content_chunks` 原样保留、
只在注释与文档里标成"块口径的历史命名"；新增 `chunk_interval_*` / `n_output_chunks` /
`n_empty_text_chunks` / `srv_completion_tokens` 等把话说清楚。
**实测证据**：中文提示词下 32 个 token 只出 27 个有文本的块，那 5 个空文本块**每个都真的带着 1 个 token**；
旧口径因此把最慢那一撮的间隔高估了约 **39%**（×1.388），而英文提示词下两套口径**逐位相同**。
**回归**：selftest 通过、battery **30 PASS / 0 FAIL / 1 INFO**（旧 24 项一项没丢 + 新增 6 项全过）、
真 vLLM **80/80 成功**且 `srv_completion_tokens` 全部取到。
**没做到的**：冷启动"一块塞 31 个 token"这次**没复现**（那台服务不是本会话起的，没重启没杀）。

---

## 12.10 metrics 汇总快照采集（任务 4c）：四种状态，以及"缺项 ≠ 0"

### 12.10.0 一页话（不看术语也能读）

新增了一个**可选**开关 `--metrics-url`：配了它，工具会在一次运行期间按**固定间隔**去抓服务端的
`/metrics`（服务自己报的一堆累计计数），把**原始正文 + 带时间戳的记录**存到硬盘；
**不配它，采集这条路一行都不走**，行为与加这个开关之前一模一样（有机械证据：同一目录下
再跑一次不带开关的运行，**一个多余的文件都没多**）。

采集结果只有四种可能，**必须分得清**——最要紧的一条是：**"指标里没有 NIXL 项"要记成"未出现"，
绝不能记成 0**。
真机上（vLLM 0.31.0、单实例、没开 KV 连接器）采到的**就是**这一种：**成功抓到指标、但没有 NIXL 项
→ 记"未出现"**。

术语先解释清楚（后面全部按这些说法写）：

- **NIXL 项**：vLLM 里负责"把 KV 缓存从一台机器搬到另一台"的那个连接器报的指标
  （传了多少字节、花了多久、失败几次）。**要开拆分式部署（两个实例配连接器）才会出现**；
  单实例的普通服务看不到它们，是**预期现象，不是服务坏了**。
- **汇总快照**：整个服务进程的**累计计数**，是"这台机器一共干了多少"，不是"某个请求干了什么"。
- **缺项**：指标列表里**根本没有这一条**。**数值为零**：这一条**在**，值是 0。
  **这是两件完全不同的事**，混起来就会写出凭空的结论。

### 12.10.1 新增参数（不配就完全不走这条路径）

| 参数 | 默认 | 干什么用 |
|---|---|---|
| `--metrics-url URL` | 空（**不采集**） | `/metrics` 的地址，例如 `http://127.0.0.1:8090/metrics`。空＝整条采集路径不生效 |
| `--metrics-interval-s 秒` | 5.0 | 固定抓取间隔。**开局先抓一次 `start`、结束后再抓一次 `end`**，中间每这么久抓一次 `periodic`——"整次运行前后"的对比靠的就是首末两点 |
| `--metrics-timeout-s 秒` | 5.0 | 单次抓取超时。抓取在**独立线程**里做，超时不会拖慢请求；超时按"抓取失败"记，并留下错误原文 |
| `--version-url URL` | 空 | 取服务版本的地址。空则自动由 `--metrics-url` 推成 `scheme://host/version`（vLLM 的版本端点就是它）。**取不到就记"未取到"，不许猜** |

另外新增了一个角色 `fake-metrics`（假指标服务，**本机就能跑、不需要显卡**）和电池里的一个新臂
（`M1`–`M11`）。假指标服务的作用只有一个：真机在这个配置下看不到 NIXL，所以
**"抓到 NIXL 项"这一种状态只能用假数据验证**；假服务里的指标值**全部是伪造的**，
只用来验状态机，**不作为任何测量结论**。

### 12.10.2 落盘格式（产物都在 `sim/results/b0_metrics/`，append-only）

配了 `--metrics-url` 时**额外**产出三个东西（文件名都带唯一 `run_id`，不覆盖任何旧文件）：

| 文件 | 内容 |
|---|---|
| `<run_id>_metrics_scrapes.csv` | **每次抓取一行**（含失败的行） |
| `<run_id>_scrape_<序号>_<阶段>.json` | **每次抓取的完整记录**：元数据 + `raw_body`（抓到的**原样正文**） |
| `<run_id>_summary.json` 里的 `metrics_snapshot` 段、`<run_id>_manifest.json` 里的 `metrics` 段 | 该次运行的**四态汇总**（同一个 `state` 字段，两处一致） |

`_metrics_scrapes.csv` 的列（也是快照 JSON 的字段名，两处同名同义，可以互相核对）：

| 列 | 含义 |
|---|---|
| `run_id` / `seq` / `phase` | 哪次运行 / 第几次抓取 / 阶段（`start` / `periodic` / `end`） |
| `utc` / `t_rel_s` / `t_utc_epoch` | 抓取时刻：UTC 文本、**相对本次运行起点**的秒数、绝对 Unix 秒。两套都留，是为了既能与请求时刻对齐，又能跨机器对时 |
| `planned_t_rel_s` | 原计划在哪一秒抓（`phase=end` 时它就等于实际停机时刻，不是排班点） |
| `scrape_wall_s` | 这次抓取本身花了多久（**采集的开销就靠它**） |
| `url` / `ok` / `http_status` | 抓哪、成没成、HTTP 状态码 |
| `error_class` / `error_text` | 失败时的分类与**错误原文**（超时/连不上/5xx/空正文…） |
| **`state` / `state_zh`** | **四种状态**：代码值 + 中文说法（见 12.10.3） |
| **`nixl_presence_zh`** | NIXL 的**字面值**：`未配置地址` / `抓取失败` / **`未出现`** / `出现` |
| `nixl_note` | 一句话说明这一格为什么这么记（含"这不是 0"的提醒） |
| `nixl_matched_series_count` | 匹配到的 **NIXL 序列条数**——注意：**这是"条数"，不是任何指标的值**（列名之后另有一列专门写这句） |
| `nixl_series_values` | NIXL 的数值表（JSON）。**没有 NIXL 项时这里是空的**（快照 JSON 里是 `null`），不是 `{}`、更不是 0 |
| `nixl_help_or_type_mentions` | 只在注释行（`# HELP` / `# TYPE`）里提到 NIXL 的行数——用来点出"声明了但没有样本"这种半拉子情形 |
| `n_series` / `n_sample_lines` / `body_bytes` / `body_sha256` | 解析出多少条序列 / 正文多少行 / 正文多少字节 / 正文指纹（**可以事后重算核对**） |
| `kv_transfer_label_hits` / `kv_connector_hits` | **沾边但不同的东西**的命中数（见 12.10.4 的坑），只作参考，不参与 NIXL 判定 |
| `target_version` / `version_source` / `version_error` | 被采集服务的版本 / 从哪取的 / 取不到的原因 |
| `snapshot_file` | 对应的完整快照 JSON 文件名 |

快照 JSON 比 CSV 多两样：`raw_body`（原样正文）与 `scope_note`（那条"不能分摊给单个请求"的限制，
见 12.10.6）。

### 12.10.3 四种状态各怎么记、怎么区分（附实测样例）

| 状态（代码值） | 落盘里怎么记 | 实测样例 run_id | 实测到的样子 |
|---|---|---|---|
| **① `not_configured` 未配置地址** | `state=not_configured`；NIXL 字面值 **`未配置地址`**；NIXL 数值列**空** | `serving_20261007T102848542Z_22220-1` | 抓取 **0** 次；该次运行只多了它本来就有的 3 个产物（manifest / requests.csv / summary.json），**没有** `_metrics_scrapes.csv`、**没有** `_scrape_*.json` |
| **② `scrape_failed` 抓取失败** | `state=scrape_failed`；NIXL 字面值 **`抓取失败`**；`error_class` + **`error_text` 存错误原文** | 挂住不回应 `serving_20261007T102757683Z_30172-1`；连接被拒 `serving_20261007T102800617Z_37988-1`；HTTP 500（电池）`serving_20261007T102722698Z_3740-7` | 错误原文分别是 `TimeoutError: timed out`（3 次尝试各花 0.81–0.82 s）、`TimeoutError: timed out`（2 次尝试各 0.80–0.81 s）、`HTTP 500（正文前 200 字符：'boom\n'）`；**请求仍然全部成功**（8/8 或 6/6） |
| **③ `nixl_absent` 抓到指标但没有 NIXL 项** | `state=nixl_absent`；NIXL 字面值 **`未出现`**；`nixl_series_values = null`（CSV 里**空**） | **真 vLLM**：`serving_20261007T102816211Z_36921-1`；假服务：`serving_20261007T102755004Z_8764-1` | 真机：**517** 条序列 / 92,761 字节正文 / NIXL 匹配 **0** 条；假服务：11 条序列 / 1,796 字节 |
| **④ `nixl_present` 抓到 NIXL 项** | `state=nixl_present`；NIXL 字面值 **`出现`**；`nixl_series_values` 是**真的数值表** | `serving_20261007T102756361Z_8848-1`（假服务 `--metrics-variant nixl`） | 共 18 条序列、其中 **7** 条 NIXL；例如 `vllm:nixl_bytes_transferred_total{…write}=1.25e9`、`vllm:nixl_num_failed_transfers=0.0` |

**怎么读这张表**：**只有第四行才谈得上"传了多少"**。前三行（未配置 / 抓取失败 / 未出现）
**都没有数值可言**——它们不是"传输量 0"，而是"这一次我们不知道传输量"。

### 12.10.4 "缺项 ≠ 0"为什么重要（本任务最要紧的一条）

三种"零"长得像，含义完全不同：

| 情形 | 该记成 | 为什么 |
|---|---|---|
| 服务**报了**这条指标，值是 0 | **`0.0`**（照实记） | 实测对照：假服务 nixl 变体里的 `vllm:nixl_num_failed_transfers{…} 0.0` 落盘就是 `0.0`——它的意思是"**确实一次都没失败**" |
| 指标列表里**根本没有**这条序列 | **`未出现`**（数值列留空/`null`） | 我们**不知道**任何数值。写成 0 就等于替服务编了一个它没说的数 |
| 这次**没抓到** `/metrics` 正文 | **`抓取失败`** | 没拿到正文＝**无从判断**有没有 NIXL 项。**失败既不是"未出现"也不是 0** |

**如果第三行写成 0，会得出什么错误结论**：读的人会以为"这台服务确实传了 KV，只是量是 0"，
于是顺手写下"单实例也不会传 KV"。真相是**这套配置根本没启用那条通路**——
报文里连这条指标都没有。这是本任务里最容易被无声犯下的错，所以：
① 判定只输出"出现 / 未出现"两个词；② 数值字段在缺项时是 `null`（CSV 里是**空单元格**），
不是 0；③ 快照 JSON 里紧跟着一列文字专门说"这里的 0 是**序列条数**，含义是『未出现』，不是『传输量为 0』"。

还有一个**半拉子情形**也被单独记了：指标名只在 `# HELP` / `# TYPE` 注释行里出现、但没有样本行
（`nixl_help_only` 变体，selftest 里验过）。它仍按"未出现"记，但 `nixl_note` 会明确写出
"注释里出现了 N 行 NIXL 字样，但没有样本行"——**不静默**。

**另一个必须分开的坑**：真机的 `/metrics` 里有
`vllm:prompt_tokens_by_source_total{source="external_kv_transfer"}`——名字里有 `kv_transfer`，
看起来像"KV 传输指标"，但它是**按 token 计数的累计计数器**，和"传了多少字节 / 多久 / 失败几次"
**是三样不同的东西**，**不能**算成 NIXL。实测真机 `kv_transfer_label_hits = 2`、
而 NIXL 匹配数 = **0**：两个数分开记，就是为了不让它悄悄混进去
（假服务的正文里也故意放了这一条，电池 `M2` 会专门检查它**没有**被算成 NIXL）。

### 12.10.5 版本是怎么记的（以及一个差点踩进去的坑）

优先级只有两级，取不到就记 **`未取到`** + 原因：

1. `--version-url`（默认由 `--metrics-url` 推成 `scheme://host/version`）；
2. 退一步：`/metrics` 里 **`build_info` 这类"服务构建信息"指标**的 `version` 标签；
3. 都没有 → `target_version = 未取到`，并在 `version_error` 里写清原因。

**真机实测**：`/version` 回 `{"version":"0.31.0"}` → 记下 `0.31.0`，
来源写明 `GET /version（JSON 字段 version）`。真机的 `/version` **可用（HTTP 200），不需要重启服务**。

**差点踩进去的坑（真机上确实存在）**：真机 `/metrics` 里有一条
`python_info{implementation="CPython",…,version="3.14.4"}`——那是 **Python 解释器的版本**，
不是 vLLM 的版本。如果规则写成"见到 `version=` 就拿来当服务版本"，就会把 **3.14.4** 记成
"被采集服务的版本"——**这就是编数**。所以实现里只接受 `build_info` 这一类明确表示服务构建的指标，
其余一律只记成"候选"并写明为什么不用。这件事**落盘可复核**：真机快照的
`version_label_probe` 里写着 `accepted: null`、`candidates: [python_info 的 3.14.4]`、
以及那句拒绝理由。电池 `M4` 会在每次回归时重查这条（"记录里不许出现 3.12.10"）。

另外两种 `/version` 形态也验了（都是真实会遇到的情形）：

| `/version` 回什么 | 落盘结果（实测） |
|---|---|
| JSON 但**没有** version 字段（`{"detail":"no version field"}`） | `target_version = 未取到`，原因写明"JSON 里没有 version/vllm_version/build_version 字段（字段名：['detail']）"，并附一句"`/metrics` 里有 1 个 version 标签候选，但它们不是服务构建信息，**未采用**" |
| 纯文本单行（`plain-text-v9.9.9`） | 记 `plain-text-v9.9.9`，来源写明"GET /version（正文原样，单行短文本）"——正文原样就是版本，**不算编** |

### 12.10.6 这些快照**不能**分摊给单个请求（写进字段，不只写在文档里）

三条理由（**原文存在每一份快照 JSON 的 `scope_note` 字段、summary/manifest 的同名字段里，
CSV 里也有 `nixl_note` 提醒**）：

1. vLLM 的 NIXL 日志本身是**按一段时间汇总**的，官方指标**按传输统计**，都不带请求标识，
   无法自动匹配到本工具里的 `req_id`；
2. `/metrics` 是**进程级累计量**，从两个采样点之差里**解不出**"每个请求占多少"；
3. 抓取时刻与请求边界**不对齐**（一次抓取本身还占用一次 HTTP 往返）。

**能做什么**：比较**整次运行前后**的汇总变化。真机实测样例（20 个请求、每个提示词 64 token）：
首末两次快照之间 `http_request_size_bytes_sum{handler="/v1/completions"}` **+6880**
（= 20 × 344 字节，正好是一次请求体的大小量级）、`vllm:prompt_tokens_total` **+1300**、
`vllm:prefix_cache_hits_total` **+1280**。
这些数和"我们发了 20 个请求"**量级一致**，但**仍然不能**说成"这 6880 字节是第 3 个请求贡献的"——
服务上如果同时还有别的客户端，我们分不出来（本轮没有别的客户端在跑，**但这一点无法由指标本身证明**）。

**消失/新增的序列不计差、也不当 0**（两次抓取之间服务可能重启或注销指标）：实现会给
`only_start` / `only_end` 两份名单，而不是把"消失的序列"写成 −(它上次的值)。本轮真机两侧
517 条序列**完全相同**（`only_start = only_end = 0`），但这只是这次碰巧，字段本身留了位置。

### 12.10.7 真 vLLM 上的实测结果：**成功抓到指标、但没有 NIXL 项 → 记"未出现"**

服务：WSL2 里**已在运行**的普通 vLLM（`127.0.0.1:8090`，`Qwen/Qwen2.5-1.5B-Instruct`，
vLLM **0.31.0**）。**没有重启它，也没有另起第二个实例**。

| 项目 | 实测（`serving_20261007T102816211Z_36921-1`） |
|---|---|
| 抓取 | 4 次（`start` / `periodic` / `periodic` / `end`），**全部 HTTP 200**，时刻 0.0008 s → 2.915 s |
| 正文 | **92,761 字节**（另一次独立运行是 92,748 字节——同一服务，只差十几个计数器的位数） |
| 序列条数 | **517** |
| **NIXL 匹配到的序列数** | **0** → `nixl_series_values = null`，字面值 **`未出现`** |
| 注释行里提到 NIXL | **0** 行（`nixl_help_or_type_mentions = 0`） |
| 沾边的 `kv_transfer` | 2 条（含 `vllm:prompt_tokens_by_source_total{source="external_kv_transfer"}`）——**没有**被算成 NIXL |
| `kv_connector` | 0 条 |
| 目标版本 | **`0.31.0`**，来源 `GET /version（JSON 字段 version）` |
| 请求侧 | 20/20 成功，覆盖判定 20 PASS |

**与 §3 那次 curl 独立交叉核对**：§3 用 `curl … | grep -c nixl` 数到 **0** 行；本次工具独立抓到的
517 条序列里 NIXL 匹配数同样是 **0**。两条互不相干的路径给出同一个结论，所以这一格是可信的。

**顺带补一条新事实**：WSL2 的 localhost 转发**现在是通的**——从 Windows 侧直接打
`127.0.0.1:8090` 成功（12/12 请求 + 3 次抓取，状态同样是"未出现"，产物
`serving_20261007T102821544Z_34108-1`）。这与 §12.6 第 8 条（"Windows 连不上 WSL 的 127.0.0.1"）
**相反**；本次只当作一条环境事实记下来，不改任何已有结论。

### 12.10.8 采集本身的开销（不能悄悄改变被测量的东西）

- 抓取在**独立线程**里做，**不经过请求路径**；任何失败都只记状态，**不让测量跟着失败**
  （电池 `M9` 专门守这条：`/metrics` 返 500 时请求仍 6/6 成功）。
- 实测每次抓取耗时 **4–53 ms**（真机三次运行合起来看：最终那次 26/7/8/8 ms，另两次 4–53 ms；
  92 KB 正文，第一次最慢）。一次 2.915 s 的运行抓 4 次，合计约 49 ms ≈ 该次运行墙钟的 1.7%，
  而且是旁路线程。这些数每次都落盘（`scrape_wall_s`），读的人自己判断。
- **参数配合要注意**：`--metrics-timeout-s` 明显大于 `--metrics-interval-s` 时，一次"挂住"的抓取
  会**挤掉后面几次周期性抓取**（实测 `hang` 那次：间隔 0.3 s、超时 0.8 s，0.85 s 的运行只抓到 2 次，
  有效抓取次数变少）。要密采就把超时压到间隔以下。
- **占用磁盘**：每次抓取会把**整份原样正文**存一个 JSON（真机 92 KB/次）。按 5 s 间隔跑 10 分钟
  大约 120 个文件、~11 MB。要长时间跑就先把间隔放大，或定期把旧 run 的快照归档。

### 12.10.9 回归结果

| 项目 | 结果 |
|---|---|
| `selftest`（Windows） | **全部通过**（全套 **85** 项，其中新增第 5 组 **41 项**：四态、缺项≠0、真 0 照实记、版本白名单、首末对比、未配置不产文件、抓取失败留原文、末次失败时状态与计数自洽） |
| `selftest`（WSL，系统 `python3` 3.14.4） | **全部通过** |
| `battery`（Windows） | **41 PASS / 0 FAIL / 1 INFO**（`battery_20261007T102646748Z_3740-1`） |
| `battery`（WSL） | **41 PASS / 0 FAIL / 1 INFO**（`battery_20261007T102556862Z_35735-1`） |
| 旧检查项 | **逐条比对过：一项没丢、一项判定都没变**。与上一轮（口径修正）的最终电池 `results/b0_vllm/battery_20261007T091847106Z_37248-1_checks.csv`（31 项 = 30 PASS + 1 INFO）按"检查编号 + 项目名"逐条对照：旧 31 项**全部在、判定全部不变**，本次只多出 11 项（M1–M11，全 PASS） |
| 采集臂 | `M1` 未配置不产文件 / `M2` 未出现≠0 / `M3` 定间隔+时间戳 / `M4` 版本没记错 / `M5` 正文与指纹可核 / `M6` 限制写进产物 / `M7` 真 0 照实记 / `M8` 5xx 留原文 / `M9` 采集失败不影响测量 / `M10` 连接失败留原文 / `M11` 版本取不到记"未取到" |
| 真服务 | 20/20 成功，`srv_completion_tokens` 全部取到（与 §12 口径修正那轮一致） |

**没配开关就不生效的机械证据**：同一目录下再跑一次不带 `--metrics-url` 的运行
（`serving_20261007T102848542Z_22220-1`），只多了该次运行本来就有的 3 个产物文件，
**没有** `_metrics_scrapes.csv`、**没有** `_scrape_*.json`。

### 12.10.10 本次**没**验证到的（一条都不含糊）

1. **"抓到 NIXL 项"这一态只用假指标服务验过。** 假服务里的 NIXL 指标名是从 §3 的名单**抄**的，
   不是从真机抓下来的；真机要出现它们需要**拆分式部署**（两个实例 + KV 连接器），
   本任务明确不做。所以"判定规则（指标名里含 `nixl`，大小写无关）在**真** NIXL 文本上对不对"
   **没验**。
2. **真 NIXL 数值的含义没验**：`nixl_bytes` 是"累计字节"还是"按传输统计"、单位是不是字节，
   都没在真机上核过。本工具只做"存在性 + 原样存盘"，**不做任何单位换算**。
3. **连接失败的错误分类跨系统不一致**：Windows 上连一个没人监听的端口报
   `TimeoutError: timed out`（1.0 s 后超时），Linux 上则**立刻被拒**——同一档运行
   （电池里那 6 个请求、抓取间隔 0.25 s）Windows 只抓到 **2** 次、Linux 抓到 **4** 次。
   两种都记"抓取失败" + 原文，但**"连接被拒"这个词在 Windows 上其实没出现**（分类偏笼统）。
4. **没有做"开采集 vs 关采集"的逐请求时延对照**：抓取在独立线程、开销已量（4–53 ms），
   但**没有**一个 A/B 臂来证明"开了采集以后 p95 不变"。要确证得专门跑一组对照。
5. **多实例服务的聚合语义没验**：本项目是单实例，抓一次只拿到一个进程的计数；
   如果服务是多副本，一次抓取只代表一个副本（或被负载均衡随机分到），这条**没验**。
6. **`build_info` 版本的标签路径只用构造文本验过**：真机 0.31.0 的 `/metrics` 里**没有**
   `build_info`（只有 `python_info`），所以"从标签取版本"这条路在真文本上**没走过**。
7. **首末对比只在一个 2.9 s 的运行上验过**，没在长运行（几分钟、几千请求）上验；
   长运行下"序列集合变化"（重启、指标注销）会更容易出现，那种情形**没实测**。
8. **没做任何下游分析**：本次范围限定为"采集"，没有把快照接进汇总或绘图脚本，
   也没有做逐请求归因（那是另一项工作）。
9. **一个**既有**的坑（不是本次引入，也没修）**：`k8s_test/probe_stage_durations.py` 自己拼
   `argparse.Namespace` 调 `run_client()`，缺 `prompt_text` 等键，**在当前代码上会直接
   `AttributeError` 崩**（实测报错原文：`AttributeError: 'Namespace' object has no attribute
   'prompt_text'`）。这是早先加 `--prompt-text` 时就留下的，**与本次改动无关**；
   本次新增的四个参数全部用 `getattr(args, …, 默认值)` 取值，**不会再给这类调用方添新的崩溃点**。

### 12.10.11 复现命令（在 `sim/` 目录下）

```bash
# ① 纯逻辑自检（全套 85 项，含"四态 / 缺项≠0 / 版本白名单"41 项）；Windows 与 WSL 各跑一次
.venv/Scripts/python.exe k8s_test/measure_serving.py selftest
wsl -d Ubuntu -u root -- bash -lc "cd '/mnt/e/代码项目文件/论文/sim' && \
  python3 k8s_test/measure_serving.py selftest"

# ② 验收电池（含 metrics 四态臂 M1–M11；约 46–50 s）
.venv/Scripts/python.exe k8s_test/measure_serving.py battery --out-dir results/b0_metrics
wsl -d Ubuntu -u root -- bash -lc "cd '/mnt/e/代码项目文件/论文/sim' && \
  python3 k8s_test/measure_serving.py battery --out-dir results/b0_metrics"

# ③ 真 vLLM（服务已在 WSL 的 127.0.0.1:8090 跑着；**不要重启它、不要另起实例**）
wsl -d Ubuntu -u root -- bash -lc "cd '/mnt/e/代码项目文件/论文/sim' && \
  python3 k8s_test/measure_serving.py client --url http://127.0.0.1:8090 \
    --model Qwen/Qwen2.5-1.5B-Instruct --label vllm_metrics_real3 \
    --requests 20 --qps 8 --concurrency 8 --prompt-tokens 64 --max-tokens 32 \
    --metrics-url http://127.0.0.1:8090/metrics --metrics-interval-s 1.0 \
    --metrics-timeout-s 3.0 --out-dir results/b0_metrics"
# 预期：state = nixl_absent（字面值"未出现"）、target_version = 0.31.0

# ④ 假指标服务（本机即可；三种配方覆盖四种状态）
.venv/Scripts/python.exe k8s_test/measure_serving.py fake-backend --port 9711 --token-count 4
.venv/Scripts/python.exe k8s_test/measure_serving.py fake-metrics --port 9712 --metrics-variant nixl
#   --metrics-variant {nonixl|nixl|nixl_help_only}
#   --metrics-fail    {none|http_500|http_404|empty_200|hang}
#   --version-mode    {ok|plain|missing|http_500|garbage}
.venv/Scripts/python.exe k8s_test/measure_serving.py client --url http://127.0.0.1:9711 \
  --label fake_nixl --requests 8 --qps 10 --concurrency 4 --prompt-tokens 8 --max-tokens 4 \
  --metrics-url http://127.0.0.1:9712/metrics --metrics-interval-s 0.4 \
  --out-dir results/b0_metrics
# 把 --metrics-url 换成一个没人监听的端口（例如 http://127.0.0.1:9799/metrics）
# 就复现"抓取失败"那一态
```

### 12.10.12 本次新增产物清单（全部 append-only，未覆盖/删除任何旧文件）

新目录 **`sim/results/b0_metrics/`**（截至本次收尾：**548** 个文件 / 137 MB，含 7 轮电池运行的全部内层产物）。代表性的：

| 文件 | 内容 |
|---|---|
| `battery_20261007T102646748Z_3740-1_{checks.csv,battery.json}` | **Windows 最终电池**（41 PASS / 0 FAIL / 1 INFO） |
| `battery_20261007T102556862Z_35735-1_{checks.csv,battery.json}` | **WSL 最终电池**（同上） |
| `final2_win_console.txt` / `final2_linux_console.txt` | 上面两轮的完整控制台输出（WSL 那份里还有一次 selftest） |
| `battery_final_{win,linux}_console.txt`、`battery_{rerun_win,rerun_linux}_console.txt` | 更早两轮的中间产物（保留备查；都是 append-only） |
| `battery_20261007T100946673Z_22256-1_*` | **第一轮电池：1 项 FAIL**（M7 的判据把序列数门槛写成 ≥8，实际 7 条）——**判据写错、实现没错**，已改对并重跑；按 append-only 保留 |
| `serving_20261007T102816211Z_36921-1_*` | **真 vLLM · WSL 侧（最终代码）**：4 次抓取，状态"未出现"，版本 0.31.0，517 条序列 |
| `serving_20261007T102821544Z_34108-1_*` | 真 vLLM · **从 Windows 侧**打过去（localhost 转发，3 次抓取，同样"未出现"） |
| `serving_20261007T101247608Z_32275-1_*`、`serving_20261007T101302956Z_32384-1_*`、`serving_20261007T101455843Z_27308-1_*` | 更早三次真服务抓取（同一服务，结论一致；差别只是后来多落了 `version_label_probe` 字段与一处判据修正） |
| `serving_20261007T102755004Z_8764-1_*` | 假指标服务 `nonixl` → **③ 未出现** |
| `serving_20261007T102756361Z_8848-1_*` | 假指标服务 `nixl` → **④ 出现**（7 条 NIXL 序列，含真 0） |
| `serving_20261007T102757683Z_30172-1_*` | **② 抓取失败**（`/metrics` 挂住不回应，超时 0.8 s） |
| `serving_20261007T102800617Z_37988-1_*` | **② 抓取失败**（连接被拒） |
| `serving_20261007T102803512Z_6544-1_*` | `/version` 是 JSON 但没有 version 字段 → **未取到**（原因里连字段名都写上） |
| `serving_20261007T102804783Z_24612-1_*` | `/version` 是纯文本单行 → 正文原样当版本 |
| `serving_20261007T102848542Z_22220-1_*` | **① 未配置地址**（该目录里没多出任何采集文件） |
| `cli_metrics_states_final_console.txt` | 上面六种状态/形态的 CLI 复现输出（最终代码那一次） |
| `final2_realvllm_{wsl,win}_console.txt`、`final2_nometricsurl_console.txt` | 真服务两次、未配置一次的控制台输出 |
| `cli_metrics_states_console.txt`、`cli_metrics_edge_console.txt` | 更早一轮的同类 CLI 输出（保留备查） |

### 12.10.13 一句话总结（4c）

**加了，但不配就不生效**：`--metrics-url` 配了才按固定间隔抓 `/metrics`，原始正文 + 带时间戳的
元数据（含目标版本）落盘；**四种状态分得清**——未配置地址 / 抓取失败（留错误原文）/
**成功抓到但没有 NIXL 项（记"未出现"，不是 0）** / 抓到 NIXL 项。
**真实服务上采到的就是第三种：`未出现`**（vLLM 0.31.0、单实例、517 条序列里 NIXL 匹配 0 条，
版本 0.31.0 取自 `/version`），这与 §3 用 curl 独立数到的 0 行**一致**。
**回归**：selftest 全过（Windows + WSL），battery **41 PASS / 0 FAIL / 1 INFO**（Windows + WSL，
旧 31 项一项没丢、新增 11 项全过）。
**没做到的**：真的 NIXL 指标长什么样、数值含义、多实例聚合语义——都没验（需要拆分式部署，
本任务明确不做），另外"开/关采集对时延的影响"也没有 A/B 对照，只是把每次抓取耗时落盘了。

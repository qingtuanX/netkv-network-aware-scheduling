# B1 报告（第二轮·新机器）：拆分式（前处理—生成）服务在同云两台 A10 上的功能验证

> **这是第二份 B1 报告。** 第一份 `B1_REPORT.md` 记的是**另一批机器**（阿里云 `cn-wulanchabu`）
> 在下载 7B 模型途中**两台同时失联**、四件事一件都没验成 —— 那份是**有效证据，没有删、没有覆盖**。
> 本文件只记**第二轮**：换了机器、换了更小的模型，把四件事跑完。

- **run_id**：`b1_20261007T163708Z`
- **证据所在的 attempt**：**4**（正式测量）、**5**（收尾停服务）、**6**（抓包补证 + 再收尾）
- 报告写成时刻（UTC）：2026-10-07T17:24Z
- 执行环境：阿里云 `cn-heyuan`（华南3·河源）/ `cn-heyuan-b`，两台 `ecs.gn7i-c8g1.2xlarge`（NVIDIA A10 24 GB）
- 模型：**Qwen2.5-1.5B-Instruct**（revision `989aa7980e4cf806f80c7fef2b1adb7bc71aa306`）
- **四件事的结论**：**4a ✅ / 4b ✅ / 4c ❌（3/5 逐位相同）/ 4d ✅（含 3 处已知缺口）** —— 逐条见 §3

---

## 0. 怎么读这份报告（术语先翻译成人话）

| 词 | 人话 |
|---|---|
| **拆分式服务 / PD 拆分** | 把一次对话拆成两台机器干：一台先"读题"（前处理 prefill），把读出来的记忆交给另一台"写答案"（生成 decode）。 |
| **KV 缓存** | 上面说的"记忆"。它是模型注意力里的键值对，是拆分式服务真正要在两台机器之间搬的东西。 |
| **传没传到** | 不看"程序说成功了"，而看**接收端自己报出来的、一共收到了多少字节**，并且这个数在请求前后**变大了**。 |
| **`/metrics`** | vLLM 开的一个网页，把运行中的各种计数器打成纯文本，可以直接抓下来。 |
| **直方图（histogram）** | 一种特殊的计数器。它在 `/metrics` 里展开成三行：`_bucket`（分桶）、`_count`（**样本数**）、`_sum`（**总和**）。**它统计的是"通知次数"，不是请求次数**。 |
| **冒烟检查** | 正式测之前先发一条请求，确认"装起来了"。它**不算正式数据**。 |
| **逐位相同 / 逐 token 相同** | 两台机器吐出的**词编号序列一个不差**（不是"意思差不多"，是编号完全一样）。 |
| **门 / 判据** | 开工前的体检项，不过就不许写进论文。 |
| **释放 vs 关机** | **释放**＝把机器删掉、停止收机器费；**关机**＝机器还在，很多云仍然继续收钱（尤其云盘）。 |

**这份报告**只回答四件事（§1），**不含任何性能结论** ——
vLLM 官方明确说拆分式前处理**仍是实验功能、且不直接提高总吞吐**，所以"快了多少"不是本阶段的问题。

---

## 1. 只验四件事（别的都不写）

| # | 要验的问题 | 通过的判据 | 结论 |
|---|---|---|---|
| **4a** | 拆分式服务**能不能跑起来** | 前处理端与生成端两个 vLLM 进程都起来、端口在听、代理能路由、端到端能返回 | ✅ 见 §3.1 |
| **4b** | **KV 是否可证实地传到了另一端** | 两端 `/metrics` 里传输直方图的 **`_count`（样本数）与 `_sum`（字节总和）在请求前后都变大** | ✅ 见 §3.2 |
| **4c** | **输出是否正确** | 同一 prompt、`temperature=0`、同一 `max_tokens` 下，拆分式与单机完整的输出在 **token 编号**上逐位相同 | ❌ **3/5 相同**，见 §3.3 |
| **4d** | **记录是否完整** | 每次尝试都有 run_id / attempt / UTC 起止 / 退出码 / 失败原因 / 两端版本 / 模型 revision 与哈希 / 连接器版本 | ✅ 有 3 处已知缺口，见 §3.5 与 §6 |

---

## 2. 本次用的机器与身份核实（要求第 6 条：ping 不够）

### 2.1 两台机器（**核实方式是云元数据服务**，不是猜、不是控制台截图）

取法：在实例内部执行 `curl http://100.100.100.200/latest/meta-data/<key>`。
`100.100.100.200` 是**阿里云专属**的实例元数据服务地址；另有 `/usr/local/aegis/…/AliYunDun`
与 `/usr/local/share/aliyun-assist/aliyun-service` 两个阿里云 agent 进程、镜像名以 `…_alibase_….vhd` 结尾。

| 项 | 机器 A（前处理 prefill） | 机器 B（生成 decode） |
|---|---|---|
| 厂商 | 阿里云（Alibaba Cloud） | 同左 |
| 地域 Region | `cn-heyuan` | 同左 |
| 可用区 Zone | `cn-heyuan-b` | 同左 |
| 私有网络 VPC | `vpc-f8zxd5ciman5fejiit9gx`（`172.16.0.0/12`） | **同一个 VPC** |
| 子网 vSwitch | `vsw-f8zmfknugi51fr1hn7b5h`（`172.29.16.0/20`） | **同一个子网** |
| 实例 ID / 主机名 | `i-f8z4ggqwhyqz0r6lp1mj` / `iZf8z4ggqwhyqz0r6lp1mjZ` | `i-f8z4ggqwhyqz0r6lp1mi` / `iZf8z4ggqwhyqz0r6lp1miZ` |
| 机型 | `ecs.gn7i-c8g1.2xlarge`（8 vCPU / 29.7 GB） | 同左 |
| GPU | NVIDIA A10 23028 MiB ×1，驱动 580.126.09 | 同左 |
| 内网 IP | `172.29.31.52` | `172.29.31.51` |
| 公网 IP | `8.155.133.216` | `8.155.131.183` |
| 镜像 / 账号 | `ubuntu_24_04_x64_100G_…_alibase_20260519.vhd` / `1135257242649102` | 同左 |

**结论**：两台**同厂商、同地域、同可用区、同 VPC、同子网** ⇒ 属于**同云（B）**，
**不构成跨云证据**。原始元数据逐字段见 `attempt4/cloud_meta_{A,B}.log` 与 `cloud_identity.txt`。

### 2.2 内网：**只用 ping 不算证据**，我们有五层证据

| 层 | 证据 | 结果 |
|---|---|---|
| ① 身份 | 元数据服务（上表） | 同 VPC、同子网 ✓ |
| ② 可达性 | 在 B 上**真起监听**、从 A 连（`net_probe_from_A.log`） | 内网端口可达；**同一端口经 B 的公网 IP 不可达** ✓ |
| ③ 路由 | `ip route get 172.29.31.51` | `dev eth0 src 172.29.31.52` ⇒ 走内网网卡、源地址是本机内网 IP ✓ |
| ④ 链路容量 | `netbw.txt`：A 从内网 IP 发 512 MiB 给 B | A 侧 **2410.5 MiB/s = 20.22 Gbit/s**、B 侧 **2398.1 MiB/s = 20.12 Gbit/s**（0.212 s 传完）；接收端自己打印的 peer 是 **`('172.29.31.52', 48814)`** ✓ |
| ⑤ **抓包（最强）** | 在 **B 的 eth0 上真抓包**（`netcap_kv_path.txt`），期间发 1 条请求 | 见下 |

**第 ⑤ 层（tcpdump，attempt6）**：过滤器 `tcp and not port 22`，3,409 个包、内核丢包 0：

| 流（按字节降序） | 字节数 | 它是什么 |
|---|---|---|
| **`172.29.31.52.47565 > 172.29.31.51.51533`** | **27,136,415（25.88 MiB）** | **KV 传输本体**：从 A 的内网地址搬进 B，全程在 eth0 上 |
| `172.29.31.51.38744 > 100.100.30.25.80` | 26,588 | 阿里云自己的内网服务（aegis/aliyun-assist），与本次无关 |
| `172.29.31.52.46178 > 172.29.31.51.8200` | 9,827 | 代理 → 生成端的 HTTP 请求（走内网） |
| `172.29.31.51.8200 > 172.29.31.52.46178` | 8,338 | 生成端的 HTTP 响应 |
| 出现过的对端地址 | `172.29.31.51` ×3409、`172.29.31.52` ×3359 | **只有内网地址** |
| **公网地址（8.155.x）出现次数** | **0** | 没有任何流量走公网 ✓ |

> **口径（别读错）**：抓包这张表证明的是**"KV 走的是哪张网卡、哪些地址"**，**不是**"KV 有多少字节"——
> 它含 UCX 协议头，且这一条请求的 KV 被前缀缓存吃掉了一部分（见 §3.2 的说明）。
> **KV 的确切字节数以 `/metrics` 为准**，两者分工不同，不能互相顶替。
>
> 20 Gbit/s 是**链路原始容量**（环境事实），**不是 vLLM 的吞吐**。

---

## 3. 逐条结论

> 本节每条只写"测到了什么"。**没有测到的写在 §6，单列。**

### 3.1 4a 拆分式服务能不能跑起来 —— ✅ **跑起来了**

| 组件 | 位置 | 端口 | 绑定地址 | 证据文件 |
|---|---|---|---|---|
| 前处理（`kv_role=kv_producer`） | A | 8100 | `172.29.31.52`（**内网 IP**） | `prefill_server.log` |
| 生成（`kv_role=kv_consumer`） | B | 8200 | `172.29.31.51`（**内网 IP**） | `decode_server.log` |
| 代理（官方 `toy_proxy_server.py`） | A | 8192 | **仅 `127.0.0.1`** | `proxy_server.log` |
| 对照臂（单机完整服务） | B | 8300 | `172.29.31.51` | `baseline_server.log` |
| NIXL 握手侧信道 | A / B | 5600 | 各自内网 IP | `netproof.txt` |

- 就绪判定：`READY prefill after 28s (http://172.29.31.52:8100/v1/models)`、
  `READY decode after 1s`、`READY proxy after 1s (http://127.0.0.1:8192/healthcheck)`；
- NIXL 起来了：启动日志里 `NIXL is available`、`Backend UCX was instantiated`、
  `Creating v1 connector with name: **NixlPullConnector**`（**pull 模式**）、
  `NixlConnector setting KV cache layout to LBHNC`、`Transfer plan: TransferTopology(...)`；
- **绑定地址合规**：`ss -ltnp` 里**没有任何 `0.0.0.0`**；从本机经**真实公网**连 8100/8192/8200/8300
  **四个端口全部连不上**（`cleanup_check.txt`）；
- 端到端能返回：**安装冒烟**头一条请求 `http=200 prompt_tokens=636 completion_tokens=64 token_ids=有`。

**本轮通过拆分式路径发出的请求总数**：
`attempt4` = 冒烟 1 条 + 正式 11 条（r1 单请求 1 条 + r2/r3 各 5 条）= **12 条，全部 HTTP 200，0 条失败**；
`attempt6`（抓包补证）= 冒烟 1 条 + 补诊断 1 条。**没有任何一条请求失败**。

### 3.2 4b KV 是否可证实地传输 —— ✅ **可证实（证据在接收端）**

**口径（最容易读错的一条）**：`vllm:nixl_bytes_transferred` 与传输耗时是**直方图**，
样本按**"通知次数"**统计（**不等于请求次数**）；**"指标名出现过"不算证据**（进程一起来名字就有）。
所以判据是**`_count` / `_sum` 的增量**，且**两端都抓、保留全部标签、落盘原始文本**。

**四个检查点都在（含"运行中"）**：`pre_t0`（拆分服务起来、无请求）→ `pre_t1`（冒烟后）→
`t0`（正式测量前）→ `mid`（**运行中**：第一条正式请求之后、重复请求之前）→ `t1`（结束）。
逐序列原文见 **`kv_series_table.txt`**（由 `b1_kv_table.py` 从原始快照搬运，不做任何判断），
原始快照 10 份见 §3.4。

#### D（生成端 B:8200）—— **传输的正面证据在这里**

| 序列（标签 `engine="0", model_name="Qwen/Qwen2.5-1.5B-Instruct"`） | pre_t0 | pre_t1（冒烟后） | t0 | **mid（运行中）** | **t1（结束）** |
|---|---|---|---|---|---|
| `vllm:nixl_bytes_transferred_count` | 0 | 1 | 1 | 2 | **12** |
| `vllm:nixl_bytes_transferred_sum` | 0 | 18,350,080 | 18,350,080 | 18,808,832 | **58,720,256** |
| `vllm:nixl_num_descriptors_count` | 0 | 1 | 1 | 2 | **12** |
| `vllm:nixl_num_descriptors_sum` | 0 | 28 | 28 | 56 | **336** |
| `vllm:nixl_xfer_time_seconds_sum`（秒） | 0 | 0.045910 | 0.045910 | 0.047464 | 0.129755 |
| `vllm:nixl_post_time_seconds_sum`（秒） | 0 | 0.000310 | 0.000310 | 0.000382 | 0.001165 |
| `vllm:nixl_num_failed_transfers_total` | 0 | 0 | 0 | 0 | **0** |
| `vllm:nixl_num_failed_notifications_total` | 0 | 0 | 0 | 0 | **0** |
| `vllm:nixl_num_kv_expired_reqs_total` | 0 | 0 | 0 | 0 | **0** |

**三条独立的自洽核对**（都能手算复核）：

1. `_sum` **恰好**是理论单 token KV 的整数倍：`58,720,256 ÷ 28,672 = 2048.0`（精确整除）；
   冒烟那一条单独看：`18,350,080 ÷ 28,672 = 640.0`（精确整除）。
   其中 28,672 B/token = `2(K,V) × 28 层 × 2 KV 头 × 128 头维度 × 2 B(fp16)`（Qwen2.5-1.5B 的结构）。
2. **640 与这条 prompt 的长度对得上**：冒烟的 prompt 是 **636 token**，而
   `resolved block_size = 16` ⇒ 向上取整到 `640` ⇒ **KV 就是这条 prompt 的、按块补齐后的全部量**。
3. **28 个 descriptor / 次**：`336 ÷ 12 = 28` = **模型的 28 层**，每层一份描述符。

#### 第二个独立计数器：`vllm:prompt_tokens_by_source_total`（同为 D 端）

| 检查点 | `external_kv_transfer` | `local_cache_hit` | `local_compute` |
|---|---|---|---|
| pre_t0 | 0 | 0 | 0 |
| pre_t1（冒烟后） | **636** | 0 | **0** |
| t0 | 636 | 0 | 0 |
| mid | 648 | 624 | 0 |
| t1 | **1984** | 11808 | **0** |

**怎么读**：`local_compute` **自始至终是 0** ⇒ **生成端从来没有自己重算这段 prompt**；
它的 prompt token 要么来自**外部 KV 传输**（1984），要么来自**它自己的前缀缓存**（11808）。

#### P（前处理端 A:8100）—— **指标名都在，但数值没动**

| 序列 | pre_t0 → t1 |
|---|---|
| `vllm:nixl_bytes_transferred_count` / `_sum` | 0 → 0（**没动**） |
| `vllm:nixl_num_descriptors_count` / `_sum` | 0 → 0（**没动**） |
| `vllm:nixl_xfer_time_seconds_count` / `_sum` | 0 → 0（**没动**） |
| `vllm:prompt_tokens_by_source_total{source="local_compute"}` | 0 → **636**（P 自己算的那 636 个 token） |
| `vllm:prompt_tokens_by_source_total{source="external_kv_transfer"}` | 0 → 0（P 不从外部收 KV，合理） |

**这是本轮必须写清楚的一件事**：**P 端没有字节增量，不是"没传"，而是"这个连接器不在生产端计数"**。
日志显示连接器是 **`NixlPullConnector`（拉模式）**：由**生成端发起拉取**，
所以"搬了多少字节"这个观测**只落在拉取方（D）**。
—— 这也正是要求里说的"要退就退到**能明确记录 KV 接收端实际收到多少字节**的连接器计数"，
我们拿到的就是这一种。

> **一个需要如实写出的细节**：`_sum`（块粒度）折合 **2048 个 token 当量**，
> 而 token 计数器报的 `external_kv_transfer` 是 **1984**，差 **64**。
> 两者口径不同（前者按 **16 token 的块**补齐后才搬，后者按真实 prompt token 计数），
> 差值恰好是各次请求的块内填充之和。**我们不把它当成误差或巧合，只把它当成两个不同粒度的计数器。**

> **为什么"运行中"这一张快照重要**：只有"开始/结束"两张的话，`t0→t1` 的增量只能说
> "这段时间里传过"，分不清是哪一条请求传的。有了 `mid`，
> 增量能落到具体请求上：`mid − t0 = 458,752 B = 16 token 当量`（正好是 **r1 那条单请求**）。

### 3.3 4c 输出是否正确 —— ❌ **不通过：5 条里 3 条逐位相同，2 条不同**

**判据是**（**在发出任何正式请求之前就已冻结**，见 §4.4）：同一模型 revision / 分词器 / 精度 /
随机种子 / 生成参数（`temperature=0`、`seed=0`、`max_tokens=64`、`return_token_ids=true`）下，
拆分式与单机完整服务的输出**在 token 编号上逐位相同**。

**结果（对照臂 `baseline_r3` vs 拆分臂 `disagg_r3`，`consistency_diff.txt`）**：

| 请求 | prompt token | 对照臂 completion | 拆分臂 completion | 逐 token |
|---|---|---|---|---|
| p01_rep8 | 636 | 35 | 35 | ✅ 相同 |
| **p02_rep12** | 944 | 22 | **24** | ❌ **在第 20 个 token 处开始不同** |
| p03_rep16 | 1252 | 35 | 35 | ✅ 相同 |
| p04_rep20 | 1560 | 28 | 28 | ✅ 相同 |
| **p05_rep24** | 1868 | 64（撞上限） | 64（撞上限） | ❌ **在第 57 个 token 处开始不同** |

差异的具体样子（**原文照抄，不做美化**）：

- `p02_rep12`
  - 对照臂（22 token）：`… based on the compute backlog and network path states for workers.`
  - 拆分臂（24 token）：`… based on the compute backlog and network path states for workers executing requests.`
- `p05_rep24`（两条都撞到 64 上限）
  - 对照臂尾部：`… No specific details are provided in this summary.`
  - 拆分臂尾部：`… No specific information or instructions are carried in this`

**这两条差异"像"同义改写，但本报告不据此判"等价"** —— 我们没有取 logprobs，
没有证据说这是"概率并列处的翻转"。**结论就是：4c 未通过。**

> **【追加·2026-10-08】读这一节之前必须先知道：这是一条已确认的「方案偏离」，本轮的 4c 不是在方案冻结的配置下跑的。**
>
> 方案要求**关闭前缀缓存**（`B1_PLAN.md` 第 725 行原文：「…B1 期间**关闭投机解码**、
> **前缀缓存按计划书 §4.3 先关闭**」），**但实际是开着的**。三处硬证据，都能点开复核：
>
> | # | 证据 | 内容 |
> |---|---|---|
> | 1 | `k8s_test/b1_remote_serve.sh` **第 52–57 行** `COMMON_ARGS` | 只有 `--host` / `--max-model-len 8192` / `--gpu-memory-utilization 0.85` / `--enforce-eager` —— **没有任何前缀缓存开关**（全脚本 grep `prefix` 零命中） |
> | 2 | 同上，`cmd_start_prefill` / `cmd_start_decode` / `cmd_start_baseline` | 三条臂**共用**同一个 `COMMON_ARGS` ⇒ 不是"哪条臂被开错了"，而是**三条臂一起落到了 vLLM 的默认值** |
> | 3 | `attempt4/prefill_server.log`、`decode_server.log`、`baseline_server.log` 第 15–17 行，以及 `metrics_*_raw.txt` 里的 `vllm:cache_config_info{…}` | 启动配置与指标原文**都写着 `enable_prefix_caching=True`** |
>
> **准确的说法是**：不是"故意开了前缀缓存"，而是**方案冻的这条配置从来没有被写进启动脚本**
> ——一次**静默的方案偏离**。它不报错、不让 4a/4b/4d 失败，只让 **4c 的对照多出一个未受控的混淆项**。
>
> **对结论的影响（只改解释，不改判定）**：
> - **4c 仍然不通过**，`3/5` 一个数字都不动；**不重跑、不覆盖、不事后放宽**。
> - 但**不能**把这次失败归因于"拆分式服务本身"或"KV 传错了"——
>   前缀缓存开着会让**复用块与重算块走不同的数值路径**；而请求名里的 `_rep8/_rep12/…`
>   说明同一段题被**重复发过**，重复时后几次会命中缓存（`local_cache_hit` 实测升到 **11,808** token）。
>   这既解释 §3.3 表里的**首次运行效应**，也是 p02/p05 分歧的**候选原因之一**。
> - **定性更正**：本节末尾原来把 `enable_prefix_caching=True` 只列为"若干未验证候选原因之一"，
>   **这个定性偏轻**——它不是"顺手开的一个选项"，是**没有执行方案要求**。本节其余文字原文保留，**以这段为准**。
>
> **要分离它必须另立实验（新 run_id），且先写规则再采数**：
> ① 三条臂都显式加 `--no-enable-prefix-caching`；② 预热顺序**事先写定**（不再拿 r1 去比）；
> ③ 对同一批输入**逐条串行**比较；④ 记录**首次出现分歧的那个 token 编号**，以及
> **两边模型给它及邻近 token 的概率**（需 `logprobs`）。
> **新结果只能作为后续诊断，不能替换本轮的 B1 记录。**
> 若关掉缓存后仍有 token 差异，**就把它当成真实限制写出来**；
> 若要另设"结果正确"的业务判据，**必须先定义、再采新数据**。

#### 补充诊断（**在正式测量之后做的、不替换任何已有结果**）

按冻结的判据 4c 已经"不通过"，但**为什么**不通过值得记清楚，免得后人误读成"KV 传坏了"：

| 诊断（逐 token 编号口径） | 结果 | 说明 |
|---|---|---|
| 对照臂 r1 vs r2 | **2 / 5 相同**（3 条不同） | **首次运行与后续运行不同** |
| 对照臂 r1 vs r3 | **2 / 5 相同** | 同上 |
| 对照臂 r2 vs r3 | **5 / 5 相同** | 预热之后**稳定** |
| 拆分臂 r2 vs r3 | **5 / 5 相同** | 拆分臂自己重复时也**稳定** |
| 拆分臂 r1 vs r2（只 p01 可比） | p01 **不同** | 拆分臂的**首次**运行也与后续不同 |
| 冒烟（拆分式 p01）vs 对照臂 r1（p01） | **不同** | 同上 |
| **对照臂 r3 vs 拆分臂 r3**（正式 4c） | **3 / 5 相同** | **这就是 4c 的结论** |

**三条读数**：

1. **"先重复几次单机完整服务，确认基准自身稳定"这条前置规则抓到了真问题**：
   **单机对照臂自己的第 1 次运行就与第 2/3 次不同**（3/5 条不同）。
   也就是说，**连"基准"都没有在第一次就稳定下来**，r1 那一轮本来就不该用来做比对。
   → 这说明这台机器上存在一个**首次运行效应**（首次走完一遍模型/内核/缓存的路径与之后不同）；
2. **预热之后**：拆分臂**自比 5/5 相同**、对照臂**自比 5/5 相同** ⇒ 两臂各自都是**确定性的**，
   **差异不是随机噪声**；但**跨臂比较仍只有 3/5**；
3. **因此 4c 的失败是"确定性、但依赖计算路径"的差异**，不是"传输随机出错"——
   后者会被 `nixl_num_failed_transfers` 记到（实测 **全程 0**），也会让 KV 字节数对不上理论值
   （实测**精确整除**，见 §3.2）。

**必须同时声明的反方向证据**：以上只能说明"KV **完整地**搬过去了"，
**不能** 推出"这两条差异无害"。**我们没有证据判定拆分式输出与单机输出等价。**
可能的解释（**都未验证**，列入 §6）：两臂的注意力内核/批处理形状不同、
生成端开着前缀缓存（`enable_prefix_caching=True`）导致复用块与重算块的数值路径不同、
首轮内核自调优等。**这些都需要单独设计实验才能分离，本轮没做。**

### 3.4 `/metrics` 两端实测结果（原始快照清单 + 读法）

全部原样落盘、**不加工**（`grab_metrics` 只 `curl` 一次，原样写文件；`.err` 是同一时刻的 stderr）：

| 快照（`attempt4/`） | 内容 | 大小 |
|---|---|---|
| `metrics_precheck_prefill_t0_raw.txt` / `metrics_precheck_decode_t0_raw.txt` | 拆分服务刚起来、**还没有任何请求** | 见文件 |
| `metrics_precheck_prefill_t1_raw.txt` / `metrics_precheck_decode_t1_raw.txt` | **冒烟请求之后** | 66 KB / 67 KB 量级 |
| `metrics_measure_prefill_t0_raw.txt` / `metrics_measure_decode_t0_raw.txt` | 正式测量**之前** | — |
| `metrics_measure_prefill_mid_raw.txt` / `metrics_measure_decode_mid_raw.txt` | **运行中** | — |
| `metrics_measure_prefill_t1_raw.txt` / `metrics_measure_decode_t1_raw.txt` | 正式测量**结束** | — |
| `metrics_measure_proxy_t1_raw.txt` | 代理端（它没有 KV 指标，属正常） | — |
| `metrics_baseline_idle_t0_raw.txt` / `metrics_baseline_after_raw.txt` | 对照臂（单机完整服务） | 56 KB |

增量判读（直方图口径，`b1_metrics_delta.py`）：`kv_delta_smoke_{A,B}.txt`、
`kv_delta_measure_{A,B}.txt`、`kv_delta_measure_{A,B}_r1.txt`、`kv_delta_measure_{A,B}_r2r3.txt`；
汇总页 `kv_transfer_evidence.txt`；逐序列四点对照 `kv_series_table.txt`。

**命中情况（名字层面的实况，不作为证据）**：两端 `/metrics` 里命中 `nixl` 的行各有 **101 行**；
实际族名与 vLLM v0.31.0 源码一致：`vllm:nixl_bytes_transferred`（直方图，桶从 2 KiB 到 8 GiB）、
`vllm:nixl_xfer_time_seconds`、`vllm:nixl_post_time_seconds`、`vllm:nixl_num_descriptors`、
`vllm:nixl_num_failed_transfers`、`vllm:nixl_num_failed_notifications`、
`vllm:nixl_num_kv_expired_reqs`。**全部标签原样保留**（`engine="0"`、`model_name=…`、
直方图还带 `le=…`），例如：

```
vllm:nixl_bytes_transferred_count{engine="0",model_name="Qwen/Qwen2.5-1.5B-Instruct"} 12.0
vllm:nixl_bytes_transferred_sum{engine="0",model_name="Qwen/Qwen2.5-1.5B-Instruct"} 5.8720256e+07
```

> **三样东西不许混为一谈**（本报告里出现这几个数必须对号入座）：
>
> | 东西 | 本轮实测值 | 它是什么 | **不能**当什么 |
> |---|---|---|---|
> | `kv_cache_size_tokens`（`vllm:cache_config_info`） | `574944`（另有 `num_gpu_blocks=35934`、`block_size=16`） | 缓存**容量**（最多装多少 token 的 KV） | ❌ 某次请求实际传的字节 |
> | 理论 KV 估算 | 4096 token ≈ **117.4 MB**；本轮 640 token ≈ **17.5 MiB** | 量级参考（算术） | ❌ 实测传输量 |
> | 显存占用（`nvidia-smi`） | 服务运行时约 **19,307 MiB**（`gpu_memory_utilization=0.85`） | 资源消耗 | ❌ 传输量 |
>
> **备用证据的纪律**（本轮实际遵守）：**通用网卡字节数没有当 KV 字节数用**。
> §2.2 的 27,136,415 B 是**抓包流量**（含 UCX 协议头），我们**只用它证明"走哪张网卡"**，
> **没有**用它当"KV 传了多少"——后者一律以 `/metrics` 的 `_sum` 为准。

### 3.5 4d 记录是否完整 —— ✅ **主体齐全，3 处缺口如实列出**

| R7 要求的字段 | 状态 | 证据 |
|---|---|---|
| run_id / attempt / UTC 起止 / 退出码 / 失败原因 | ✅ | `attempt4/record.csv`（另有 attempt1→6 各自的 record.csv） |
| 两端版本 + wheel sha256 | ✅ | `dependency_digests.txt`（**两端逐字节相同**） |
| 依赖清单 | ✅ | `requirements_locked_A.txt` / `requirements_locked_B.txt`（sha256 相同） |
| 模型 revision | ✅ | `model_revision.log`（Hub API 原文）+ 脚本里写死的 `MODEL_REV_EXPECT` |
| 模型**两端目录哈希** | ✅ | `model_identity.txt`：A=B=`0ee375c9…1c45`，33 个文件逐文件一致 |
| 连接器版本 / 模式 | ✅ | `nixl==1.4.1`（+`nixl-cu12`/`nixl-cu13` 1.4.1）；模式 `NixlPullConnector` |
| 运行精度 / 注意力后端 / 缓存 dtype | ✅ | `dtype=torch.bfloat16`、`FLASH_ATTN`、`kv_cache_dtype=auto`（三份启动日志） |
| 每请求记录 + 原始响应 | ✅ | `arm_{baseline,disagg}_r{1,2,3}.json` + `*_raw.jsonl`；冒烟单独在 `smoke/` |
| `/metrics` 原始快照 | ✅ | §3.4 的 10+ 份 |
| 收尾（服务已停、无残留） | ✅ | `attempt5/cleanup_check.txt`、`attempt6/cleanup_check.txt` |
| **两端时钟偏差的实测值** | ❌ **没有** | 只拿到各自的 NTP 状态（两端 `System clock synchronized: yes`、`NTP service: active`、同 `Asia/Shanghai`），**没做偏差测量** |
| **R7 里的"镜像 digest"** | ⚠️ 等价替代 | 本任务**不用容器**，等价物是 wheel sha256 + 依赖清单 sha256（已给） |
| **`record.csv` 的行结构** | ⚠️ 有缺陷 | note 字段里带**多行命令**时没有转义换行，CSV 行会被打断（后果见 §6） |

---

## 4. 依赖与模型锁定（要求第 2 条：把版本钉死）

### 4.1 两端依赖（**逐字节相同**才有意义）

| 项 | A | B | 判据 |
|---|---|---|---|
| vLLM | 0.31.0 | 0.31.0 | wheel sha256 `39132b43…3433` **两端相同** ✓ |
| NIXL | 1.4.1（`nixl-cu12`/`nixl-cu13` 同为 1.4.1） | 同 | wheel sha256 `40168c1f…44df` **两端相同** ✓ |
| `requirements_locked.txt` | — | — | sha256 `e5fedd97…b47f9` **两端相同** ✓ |
| torch | 2.13.0+cu130 | 同 | `torch.cuda.is_available() = True`，设备 `NVIDIA A10 cap (8,6)` ✓ |
| transformers / huggingface_hub / flashinfer | 5.17.0 / 1.33.0 / 0.7.0.post1 | 同 | 见 `requirements_locked_{A,B}.txt` |

> **一个跨批次的巧合（值得记）**：本轮的 vLLM wheel、NIXL wheel、`requirements_locked.txt`
> 三个 sha256 **与上一批（已释放的那两台）完全一致** ⇒ 两批机器用的是同一份内网镜像、同一组版本。

### 4.2 模型

| 项 | 值 |
|---|---|
| 模型 | `Qwen/Qwen2.5-1.5B-Instruct` |
| 官方 revision（Hub API `sha`） | `989aa7980e4cf806f80c7fef2b1adb7bc71aa306` |
| **权重文件原始精度** | `config.json` 的 `torch_dtype = bfloat16` |
| **实际运行精度** | 三个实例的启动日志都是 `dtype=torch.bfloat16` ⇒ **与原始精度一致** |
| 文件数 / 总字节 | 33 / ≈3.099 GB（`model.safetensors` 单文件 3,087,467,144 B） |
| **两端整目录哈希** | A = B = `0ee375c91c0e1d424eba4369a39addbfa21150cd566b67035d469e60bc4e1c45` ⇒ **逐文件一致** ✓ |

> **为什么两端哈希比 API 的 revision 更重要**：API 的 sha 只说明"官方那一边是哪一版"；
> 万一两台下到的不是同一份（形状对得上但权重不同），vLLM **不会报错**，
> KV 还是按字节搬 ⇒ **结果静默算错**。两条都要有。
>
> **一处 1 字节的差异（照实记）**：`du -sb` 报的总字节 A=3,098,975,922、B=3,098,975,923，
> 差 1 字节；而**逐文件 sha256 与整目录总哈希完全相同、文件数都是 33**。
> 差值来自 `du` 把目录项本身也计入，与文件内容无关。

### 4.3 为什么本轮不用 7B

① **B1 只验流程通不通，不验性能** —— 模型大小与四件事的结论无关；
② 这两台内存只有 **29.7 GB**，7B 权重 15 GB、**加载峰值约 30 GB**，**太紧**；
③ 公网慢（实测 hf-mirror **1.82 MB/s(A) / 0.66 MB/s(B)**），7B 要下 **3.5–4 小时**，纯白烧卡时。

**1.5B 够不够用**：够。单 token KV 是 28,672 B（可复核算术），
冒烟那条 640 token 当量 = **17.5 MiB**，实测 `_sum` **精确等于**这个数 ⇒ 可观测、可核对。

### 4.4 4c 的对照规则（**在正式请求之前冻结**）

1. 两端**同一模型 revision** + **同一分词器** + **同一运行精度** + **同一随机种子** + **同一生成参数**；
2. **先重复几次"单机完整服务"，确认基准自身稳定**，再比拆分式（**这一条本轮真的起了作用**，见 §3.3）；
3. 比对口径是 **token 编号逐位相同**，不是比显示文本；
4. 官方配置标 **BF16**；**"权重原始精度"与"实际运行精度"分开写**。

---

## 5. 费用口径与上限

### 5.1 "机器已租 ⇒ 增量成本为 0" 是错的

这两台**正在计费**。继续跑就是继续花钱；"不用重新采购" ≠ "继续运行不收费"。

```
继续运行的费用 = 单价 P(元/卡/小时) × 每台卡数(1) × 台数(2) × 从现在到释放的小时数 H
```

### 5.2 查到的与没查到的

| 要查的项 | 状态 | 为什么 |
|---|---|---|
| 计费方式（按量 / 包月） | ❌ **未查到** | 云元数据服务不暴露；实例内无 `aliyun` CLI；无 RAM 角色（`…/meta-data/ram/security-credentials/` 返回 **404**）。**本轮在新机器上又查了一遍，结论相同。** |
| 单价 P | ❌ **未查到** | 阿里云定价页需登录且 JS 渲染；`DescribePrice` 需 AK/SK。逐条证据见 `sim/results/b1_pricing/NOT_FOUND_20261007.md` |
| 今天的实时账单 | ❌ **未查到** | 同上，需要控制台会话。**实例内反复试过元数据与本地接口，均无此字段。** |

→ **本报告不编单价**。金额只写公式；**能不能跑由"时间上限"决定**。

### 5.3 上限与本轮实际用量

| 项 | 上限 | 本轮实际 |
|---|---|---|
| 单次连续运行 | **4 小时/台** | 最大 **0.77 小时**（远未触线） |
| B1 累计（本轮） | **16 卡时**（= 2 台 × 8 小时） | **≈1.54 卡时**（0.77 h 墙钟 × 2 台） |
| 金额 | `≤ 16 × P`（**P 未查到**） | 按公式 `≈ 1.54 × P`，**P 未知 ⇒ 金额无法计算** |

- 计量口径：起点 = 本轮第一次 `init` 的 UTC 时间戳 **2026-10-07T16:37:09Z**（`attempt1/record.csv` 首行），
  终点 = 最后一次远端收尾检查 **17:22:57Z** ⇒ 0.76 h（写报告时刻 17:24Z 为 0.77 h）；
- **`budget_gate()` 全程未触线**（脚本在 `precheck` / `measure` 开头都会检查）；
- ⚠️ **一处口径偏差要说明**：`budget.txt` 里显示的"计量起点"是 **17:18:09Z**，偏晚，
  原因是 `record.csv` 的 note 字段含**多行命令**、没转义换行，把后续行结构打断了（见 §6 第 3 条）。
  **上表的 0.77 h 是我用真实时间戳手算的**，不是从 `budget.txt` 抄的。

> **【追加·2026-10-08】上面那条"原因"归错了，真因已查明并修好，旧文字保留不改。**
>
> **真因不是换行，是列号少算一列。** `budget_report()` / `budget_gate()` 取时间戳写的是
> `awk -F, 'NR>1 && $4 ~ /T/ {print $4}'`，但 `record.csv` 的列是
> `run_id,attempt,host,step,utc_start,utc_end,...` ⇒ **utc_start 是 `$5`**，而 `$4` 是 **step**
> （一般不含字母 T）⇒ **一个都匹配不到** ⇒ `first` 恒为空 ⇒ 回退成"现在" ⇒ **已历时恒为 0**。
>
> **因此这两条要重新表述**：
> - 上表「最大 0.77 小时」「≈1.54 卡时」**是我按真实时间戳手算的，数字本身没错**；
> - 但「**`budget_gate()` 全程未触线**」这句**没有信息量** ——
>   那道闸因为上面的 bug **根本不会触发**，它既没拦住也没放行，谈不上"未触线"。
>   正确写法是：「**闸门在本轮因 bug 失效，未起到保护作用；用量由人工按时间戳核算**」。
>
> **修复与验证**见 `B1_4C_REPORT.md` 附录 B（新增 `record_ts()`，列号修正为 5/6，
> 取不到就报警，闸门改为保守不放行；修复后实测实验窗口 0.097 h / 0.194 卡时，与独立算法一致）。
> **算错的那份 `budget.txt` 在本轮目录下改名保留为 `budget.FAILED_wrong_column_index.txt`。**

> **¥400 的口径**：它只是方案里给的一个**建议档位**，**作者没有确认过**；
> 本报告**不把它当作已定预算**叙述。

### 5.4 本轮实测到的、会直接影响成本的事实

| 源 | 速率 | 说明 |
|---|---|---|
| `hf-mirror.com` | **1.82 MB/s（A）/ 0.66 MB/s（B）** | 下模型走这条 |
| 阿里云内网镜像 `mirrors.aliyun.com` | 约 **17 MB/s** | 装 pip 包走这条 |
| A↔B 内网 | **≈2.36 GB/s（20.2 Gbit/s）** | §2.2 实测 |

**后果**：模型 2.88 GiB，实测 **A 约 5 分钟、B 约 12 分钟**下完（合计 17 分钟，
两台都在计费）；换成 7B（15.2 GB）在同一出口速度下要 **3.5–4 小时**。
→ **B2 及以后要么先解决带宽，要么按"下载时间 × 单价"倒推模型大小**，不能默认"下模型不要钱"。

---

## 6. 没验证到的项（**单列，不藏在正文里**）

| # | 没做到的事 | 原因 | 能不能补 |
|---|---|---|---|
| 1 | **4c 通过** | 实测 3/5 逐位相同（§3.3），**这是负结果，不重跑顶替** | 需另立实验分离"内核/批形状/前缀缓存/首轮自调优"这几个候选原因 |
| 2 | **拆分式输出与单机输出等价** | 没取 logprobs，无法判定那两处差异是否为概率并列处的翻转 | 需要 `logprobs` + 固定 `logit_bias` 或逐层对齐实验 |
| 3 | **两端时钟偏差的实测值** | 只拿到各自的 NTP 状态（都已同步），**没有做偏差测量** | 可用 `chronyc tracking` 的 offset 或一来一回打点 |
| 4 | **正式测量规模 ≥ 100 对请求** | 本轮正式请求只有 **11 条**（先单请求、再 5×2 重复），**小样本是刻意的**（B1 验流程不验性能）；并且**首次运行效应**说明前几条本身还带预热污染 | 若要做"A 与 B 输出是否等价"的统计结论，需先消除首轮效应、再加种子维度 |
| 5 | **关闭前缀缓存后的 4c** | 生成端开着 `enable_prefix_caching=True`，是"两处差异"的候选原因之一，**本轮没有做这个对照** | 加一轮 `--no-enable-prefix-caching` 的对照即可 |
| 6 | **计费方式 / 单价 / 实时账单** | **查不到**（元数据服务不暴露、无 CLI、无 RAM 角色、控制台需登录） | **只能由作者登录控制台查** |
| 7 | **`NixlPushConnector`（推模式）下的现象** | 本轮只跑了默认的 pull 模式 | 换 `kv_connector` 即可对照；届时"字节计数落在哪一端"会反过来 |
| 8 | **更大的 prompt / 更多并发** | 本轮 prompt 最长 1868 token、**完全串行**（刻意如此，避免混入性能变量） | 属于 B2/D 的范围 |
| 9 | **抓包层面的 KV 字节精确核对** | 抓包含 UCX 协议头、且受前缀缓存影响（27,136,415 B ≠ 该请求的 KV 理论量）⇒ 抓包只用来定"走哪张网卡" | 需要按 NIXL 报文结构解析 pcap，本轮不做 |

**没有验证到 ≠ 验证失败**：第 1、4 条是**实测到的负结果**，第 2、3、5、6、7、8、9 条是**本轮没有覆盖的范围**。
两者在报告里必须区分开。

---

## 7. 每一次尝试与失败（**R1/R2：只增不减、不覆盖**）

| attempt | 做到哪一步 | 结果 | 留档位置 |
|---|---|---|---|
| **1** | `env` 的环境安装 | ❌ 失败：镜像**没有 `python3.12-venv`**，`python3 -m venv` 在 `ensurepip` 那步失败 | `attempt1/NOTE_ATTEMPT1_VENV_FAIL.md` |
| **2** | `env`→`digests`→`model`→`netbw` | ✅ 这四步全成功；❌ 随后 **`baseline` 失败**（健康检查地址写错） | `attempt2/` |
| **3** | 重跑 `env`→`digests`→`model`→`netbw`→`baseline` | ❌ `baseline` 又失败：**客户端没推到 B 上**，而且**失败被记成 OK** | `attempt3/` |
| **4** | 全身重跑 → `baseline`✅ `precheck`✅ `measure`✅ `compare`（**4c 不通过**）✅ | ✅ **本报告的主要证据** | `attempt4/` |
| **5** | 收尾：`stop` + `cleanup-check` | ✅ | `attempt5/` |
| **6** | 重起服务做**抓包补证** + 再收尾（`stop` + `cleanup-check`） | ✅ | `attempt6/` |

### 7.1 attempt1：venv 失败（原始报错）

```
ensurepip is not available.  On Debian/Ubuntu systems, you need to install the python3-venv
package using the following command.
    apt install python3.12-venv
Failing command: /root/b1/venv/bin/python3
```

- 根因（实测）：`python3 -c "import ensurepip"` → `ModuleNotFoundError`；
  `/usr/lib/python3.12/ensurepip` 不存在；`dpkg -l python3.12-venv` = **未安装**；
  但 `dpkg -l python3-virtualenv` = **`20.25.0+ds-2` 已经装了**。
- 修法（**没有装任何系统包**）：先试 `python3 -m venv`，失败就 `rm -rf` 后改用镜像里本来就有的
  `/usr/bin/virtualenv`；判据改成"`bin/python` **和** `bin/pip` 都在"（半成品 venv 也要能被识别并重建）。
- **顺带修掉一个"静默成功"**：原来 venv 失败时 `b1_remote_setup.sh` 仍以 **rc=0** 结束、
  `run_b1.sh` 打印"A 装好了 / B 装好了"，**直到下一个阶段才发现什么都没装**。
  现在加了"安装结果自检"：`vllm --version`、`import nixl`、`torch.cuda.is_available()` 三条任一不过就 `exit 1`。

### 7.2 attempt2：`baseline` 失败 —— **服务其实起来了，脚本判它没起来**

`waitready_baseline.log` 报 `NOT READY baseline after 240s`，但同一次 SSH 里看到的实况是：

```
LISTEN 0 2048 172.29.31.51:8300 0.0.0.0:*      ← 端口在听
INFO: Application startup complete.            ← vLLM 自己的日志
```

原因：服务**按设计只绑内网 IP**（不许绑 0.0.0.0），而脚本里的健康检查与 `/metrics` 抓取
**写死了 `curl 127.0.0.1`** ⇒ 连不上。**这是脚本 bug，不是服务失败** ——
本次从未向拆分式服务发出过正式测量请求（按 R3 属"还没开测"）。
修法：`wait_ready()` 与 `grab_metrics()` 改成**先试本机内网 IP，再退回 `127.0.0.1`**
（代理是唯一**故意只绑回环**的组件，回环这条退路必须保留）。

### 7.3 attempt3：客户端没推到 B 上 + **失败被记成 OK**

- `run_arm B "baseline_r1" …` 让客户端**在 B 上跑**，但 `b1_client.py` 只推到了 A ⇒
  三次全部 `/root/b1/venv/bin/python: can't open file '/tmp/b1_client.py'`。
  修法：对照臂也**从 A 发起**（指向 B 的内网 IP），两份臂的客户机/提示词/参数完全一致。
- **更要紧的是第二件事**：远程命令写成 `cmd; echo client_exit=$?`，ssh 的退出码永远是 `echo` 的 0，
  于是 **`record.csv` 把失败记成了 OK**。修法：`… ; rc=$?; echo client_exit=$rc; exit $rc`。
  **记录说谎比测试失败更糟**，这条口径已经写进脚本注释。

### 7.4 另外三个本轮才暴露的坑（都修了，都值得记）

| # | 坑 | 症状 | 修法 |
|---|---|---|---|
| 1 | **在阶段运行中改 `run_b1.sh`** | 阶段正常跑完、最后却多出一行 `syntax error near unexpected token` **（踩了两次）**——bash 按**文件偏移**重读脚本，改文件把偏移错开了 | 纪律：**阶段运行期间绝不改脚本**；本轮后段已按此执行 |
| 2 | **停下载进程的文案里出现字面量 `hf download`，`ps \| grep` 匹配到自己** | `stop_model_download_A` 返回 **exit=255**，日志里"被停的进程"赫然是本命令自己 | 文案里去掉那个字面量 + 用 `$$` 把自己排除；仍然**只按 PID 停**，不用宽泛 `pkill` |
| 3 | **`record.csv` 的 note 含多行命令时不转义换行** | CSV 行被拆断 ⇒ `budget.txt` 的"计量起点"取偏（显示 17:18:09Z，真实起点 16:37:09Z） | **本轮没改**（避免在测量期间动脚本）；已在 §5.3 手算真实时长，并在此登记 |

### 7.5 记录纪律的机械保障（本轮实际生效）

- **每次尝试独立编号**：`no_clobber` 发现目标产物已存在就**拒绝写入**，逼调用者换 `ATTEMPT`。
  本轮它拒绝过：attempt2/3/4 里重复的 `stop`、`baseline` 等（这正是它该干的事）；
- **失败不自动重跑**：`run_b1.sh` 里**没有**任何"检测到失败 → 自动重跑"的分支（硬约束，不许加）；
- **冒烟 ≠ 正式**：拆分服务起来后的**第一条请求**落在 `smoke/` 子目录、**单独留档**，
  失败算"还没开测"；**`measure` 阶段发出的才算正式测量请求**，从那条起失败即最终结果、**不重跑顶替**；
- **每一个远端后台任务都记了 PID**（防孤儿）：
  `pids_model_download_{A,B}.txt`、`pids_*_{A,B}_<UTC时刻>.txt` 构成
  "我起了什么 → 什么时候还在 → 什么时候停了"的时间线；收尾**按 PID 精确停**（§8）。
  **本轮没有出现过"不是我起的"进程或文件改动**。

---

## 8. 收尾：服务已停、无残留

`attempt6/cleanup_check.txt`（最后一次，17:22:5xZ）：

| 检查项 | 机器 A | 机器 B |
|---|---|---|
| 残留进程（vllm / 代理 / 我们的客户端） | **无** ✓ | **无** ✓ |
| 监听端口（8100 / 8192 / 8200 / 8300 / 5600） | **全部未监听** ✓ | **全部未监听** ✓ |
| 显存占用 | **0 MiB / 23028 MiB，利用率 0%** ✓ | **0 MiB / 23028 MiB** ✓ |
| 磁盘 | 197 G，剩 146 G | 同 |

**另外三项**：

- **公网不可达**：从本机经**真实公网**连 `8.155.133.216:8100`、`8.155.133.216:8192`、
  `8.155.131.183:8200`、`8.155.131.183:8300` —— **四个全部连不上** ✓
  （只能证明"此刻连不上"；服务运行期间的暴露面由 `netproof.txt` 里"绑的是内网 IP"证明）
- **私钥泄漏扫描**：产物中**没有**私钥内容、**没有**私钥路径字样 ✓
  （私钥内容自始至终没有被打印，也没有写进任何文件）
- **按 PID 精确停**：`pids_after_stop_*` 显示本会话起过的全部 PID 都 `alive=no`，
  包括 `prefill`、`decode`、`proxy`、`baseline`、两个 `model_download` 与抓包用的 `tcpdump`；
  **没有使用宽泛 `pkill`**。

> **两台机器目前仍在运行（只是没有 B1 进程）**。按方案要求，
> **要不要释放（而不是关机）由作者决定** —— 关机通常仍会计费，详见 §5。

> **【追加·2026-10-08】上面这段写于 B1 结束当时，之后状态已改变，旧文字保留不改。**
> 经作者授权，已用**阿里云控制台**把两台实例**停机**，且**必须**选「**节省停机模式**」：
>
> | 实例 | 实例 ID | 停机后状态 | 模式 |
> |---|---|---|---|
> | A `8.155.133.216`（内网 172.29.31.52） | `i-f8z4ggqwhyqz0r6lp1mj` | **已停止** | 节省停机模式 |
> | B `8.155.131.183`（内网 172.29.31.51） | `i-f8z4ggqwhyqz0r6lp1mi` | **已停止** | 节省停机模式 |
>
> **为什么要选「节省停机」而不是默认的「普通停机」**（对话框原文，两次核对）：
> - 「普通停机模式」＝"停止实例后**保留实例的资源并继续收费**" ← **照样烧钱**
> - 「节省停机模式（原停机不收费）」＝"计算资源（CPU 和内存）、镜像 License 费用、
>   固定公网 IP 的固定带宽模式**暂停计费**；系统盘、数据盘、弹性公网 IP 的固定带宽模式**持续计费**"
>
> **独立复核（不只看控制台）**：从本机对两个公网地址的 **22 端口**做 TCP 探测，
> 各 2 轮共 4 次**全部超时**；SSH 到 A 也报 `Connection timed out`。
>
> **一条未复现的异常，如实记录**：停机过程中 SSH 到 B 曾**一次**报
> `WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!`（本机 `known_hosts` 里 B 的原指纹是
> `SHA256:Hxeqfs/QBIQMHEpEcHvCz8eWkzNmy9FZyKIKWuZqjLE`）。随后连做 4 次探测
> **全部超时**、`ssh-keyscan` 也取不到指纹，**无法复现**。两种解释未分开：
> ① 停机瞬间地址/路由在切换；② 该公网 IP 被回收后短暂分给了别的租户。
> **处置**：**不再重试连这个地址**，以控制台状态为准；**下次开机前必须先看控制台确认新公网 IP**。
>
> **重启前必读**（同样来自对话框注意事项原文）："由于计算资源被回收，
> **启动时可能因库存不足导致启动失败**……如果实例停止前绑定了弹性公网 IP 地址，重启后 IP 地址不变；
> **否则，固定公网 IP 地址可能会变，但私网 IP 地址不会变**。"
> ⇒ **私网 IP 不变**（`172.29.31.5x`，B1 脚本里的内网地址可继续用），
> **公网 IP 可能变**（SSH 前重新确认）。

---

## 9. 产物清单（每条都能点开复核）

全部落在 `sim/results/b1/b1_20261007T163708Z/`（append-only），本轮主要看 `attempt4/`（132 个文件）：

| 类别 | 文件 |
|---|---|
| 记录骨架 | `attempt{1..6}/record.csv`；`budget.txt`（**注意 §5.3 里那条口径偏差**） |
| 云身份 / 清点 | `cloud_meta_{A,B}.log`、`cloud_identity.txt`、`inventory_{A,B}.log`、`clock_{A,B}.log` |
| 内网证据 | `net_internal_A.log`、`net_probe_{listen,from_A,cleanup}.log`、`netbw*.log` + **`netbw.txt`** |
| **KV 走哪张网卡（抓包）** | `attempt6/netcap_{start,request,stop}.log` + **`netcap_kv_path.txt`** |
| 依赖与模型锁定 | **`dependency_digests.txt`**、`dep_digests_{A,B}.log`、`model_revision.log`、**`model_identity.txt`**、`model_hash_{A,B}.log`、`requirements_locked_{A,B}.txt` |
| 服务启停与日志 | `start_{prefill,decode,proxy,baseline}.log`、`waitready_*.log`、`{prefill,decode,proxy,baseline}_server.log`、`stop_*.log` |
| **4b 证据** | 10 份 `metrics_*_raw.txt`（含 `_mid` 运行中）；**`kv_series_table.txt`**；`kv_delta_*.txt`；`kv_transfer_evidence.txt` |
| **4c 证据** | `arm_{baseline,disagg}_r{1,2,3}.json` + `_raw.jsonl`；**`consistency_diff.txt`**（结论）**`baseline_stability.txt`**（基准稳定性）；`smoke/smoke_disagg.json`；补充诊断 `_diag_*.txt`（7 份） |
| 收尾 | `attempt5/cleanup_check.txt`、`attempt6/cleanup_check.txt`、`pids_*_<时刻>.txt`、`remote_pidfiles_{A,B}.txt` |
| 本次新增/更新的脚本 | `sim/k8s_test/run_b1.sh`（机器信息→本轮两台；新增 `digests`/`netbw`/`netcapture`/`pids`；修 5 处 bug）、`b1_remote_setup.sh`（venv 兜底 + 安装自检 + 1.5B）、`b1_client.py`（默认模型名）、**新增 `b1_kv_table.py`** |

**怎么复核本报告的关键数字**（三条命令级的路径）：

1. **KV 字节** → 打开 `attempt4/kv_series_table.txt`，看 D 端 `vllm:nixl_bytes_transferred_sum` 的
   `pre_t0=0 … t1=5.8720256e+07`，再手算 `÷28,672` 是否为整数；
2. **4c** → 打开 `attempt4/consistency_diff.txt` 看"逐 token 相同的请求数 : 3 / 5"，
   以及它下面逐条列出的第一个不同位置；
3. **走没走内网** → 打开 `attempt6/netcap_kv_path.txt` 看那条 27,136,415 B 的流两端是不是 `172.29.31.x`，
   以及"公网地址出现的次数 = 0"。

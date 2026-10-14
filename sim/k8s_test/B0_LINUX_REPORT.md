# B0 在 Linux 上重跑的记录：Windows ↔ WSL2/Ubuntu 的逐项对照

> 记录时间：**2026-10-07（UTC 03:44–03:56）**，仓库 `E:\代码项目文件\论文`。
> 这一轮的对象是 `sim/k8s_test/measure_serving.py`（B0 的测量工具，只用标准库）。
> Linux 侧 = **WSL2 的 Ubuntu**，系统 `python3` **3.14.4**，内核
> `6.18.33.2-microsoft-standard-WSL2`，16 逻辑 CPU。
> Windows 侧 = 同一台机器，`sim/.venv/Scripts/python.exe`（CPython **3.12.10**）。
> 权威产物在 **`sim/results/b0_linux/`**（新增，未覆盖 `sim/results/b0/` 的任何文件）。

## 0. 先说人话（不看术语也能读）

**一句话结论：B0 那套检查在 Linux 上原封不动全部通过，24 项判定与 Windows 上一模一样，
没有任何一条从 PASS 变成 FAIL。** Linux 没有暴露出任何功能缺陷，所以
**`measure_serving.py` 一行都没有改**（理由见 §5）。

但"同一套检查都通过"不等于"两边量出来的是同一个东西"。有 **3 处实质差异**，必须知道：

1. **Windows 上有两个阶段是量不出来的。** 客户端时间轴一共切了 5 段，Windows 的钟
   每 **0.5–1 毫秒**才走一格，于是"等第一个字节"这段在 **85–104/120 个请求里恰好是 0**、
   "收尾"这段在 **110–119/120 个请求里恰好是 0**——不是这两段真的不花时间，而是**尺子太粗**。
   Linux 上这两段分别是 **0.27 毫秒**和 **0.11 毫秒**，**120/120 个请求全都量得出来**。
2. **Linux 的钟比 Windows 细约 2000–4000 倍**（实测：Windows 在 50 毫秒内只有 **50 个**不同的时间值，
   Linux 有 **156,869 个**）。所以代码里"容差取 5 毫秒，因为本机 `time.time()` 约 1 毫秒"这条依据，
   **在 Linux 上不成立**（Linux 的刻度是 0.238 微秒）。
3. **但总时长两边几乎一样**：端到端 p50 差 **0.2 毫秒**、首字等待（TTFT）差 **0.9 毫秒**，
   服务端自报的阶段量（服务端窗口、KV 字节数、流量字节数）**逐位相同**。
   ⇒ 上面 1、2 两条**不影响任何结论**，只影响"这批数据能分辨到什么量级"。

另外还发现一个**两个平台行为相反**的坑：在**同一个端口上再起一个服务**，
**Windows 会静默成功**（后面的实例偷偷抢走新连接），**Linux 直接报 `Errno 98` 拒绝**。
也就是说：**残留的假后端在 Windows 上会让你量错对象还不报错，在 Linux 上会立刻失败**。

**总结一句**：**Linux 上的 B0 不是"更弱"，而是"更能分辨、也更容易发现错误"。**
阶段 B 把服务器放到 Linux 上是对的，而且比在 Windows 上更安全。

---

## 1. 这一轮做了什么 / 没做什么（安全边界）

**做了**（全部在回环 `127.0.0.1` 上，不涉及任何真实网络）：

| # | 动作 | 命令要点 |
|---|---|---|
| 1 | 判据自检，两侧各 1 次 | `measure_serving.py selftest` |
| 2 | 完整电池（两臂 + 5 种故障注入），**Linux 跑 2 次**、Windows 今天补跑 1 次 | `battery --out-dir …`；Linux `:9500–9506`，Windows 补跑 `:9600–9606` |
| 3 | 两进程模式（后端与客户端分属两个进程），各 1 次 | `fake-backend --port 9520` + `client --label … --save-tokens` |
| 4 | 单段时长探针（只读，见 §4），两侧各 5–8 次 | `probe_stage_durations.py` |
| 5 | 时钟分辨率、时钟漂移、端口重复绑定等平台探测 | 见 §3.1 / §3.7 / §3.9 |
| 6 | 逐项对照脚本（新增，两侧都能跑） | `compare_b0_os.py` |

**没做（硬性约束，逐条确认）**：

- **没有执行任何 `tc qdisc` / `tc class` / `tc filter` 的增删改**；只执行过一条只读的
  `ip -br link show eth0`（确认 `eth0` 处于 `UP`，未改动）。**没有碰 `eth0` 的地址、路由、qdisc。**
- **没有连接云主机 `49.235.115.164`**，**没有碰 WSL 上的 `iperf3`**。
- **没有杀任何不是我自己起的进程**（假后端都是我自己 `&` 起的，用完只 `kill` 自己记录的那个 PID；
  没有用过任何宽泛的 `pkill`）。
- 假后端一律绑 `127.0.0.1`，端口集合 = `9500–9506 / 9520 / 9530–9544 / 9600–9606 / 9701–9702`，
  **全部避开 `8000`、`5173`、`9101–9103`**。收工后复查：Windows 与 WSL 上这些端口
  **都没有 `LISTENING` 残留**（只剩 `TIME_WAIT`），WSL 上没有遗留的 `measure_serving` 进程。
- **没有写入/覆盖 `sim/results/b0/`**（收工后复查：该目录所有文件 mtime 仍是 `2026-10-07 01:24`）。
- **没有修改**：`measure_system.py`、`STAGE_A*.md`、`AGENTS.md`、`sim/RESUME.md`、
  `真实跨云LLM服务研究计划书.md`、`sim/k8s_test/B0_REPORT.md`。**`measure_serving.py` 也未修改**
  （文件仍是 83,606 字节 / mtime `2026-10-07 01:23`）。

---

## 2. 逐项检查的 PASS/FAIL 汇总

### 2.1 判定结果：两侧完全一致，不一致 0 项

| 检查 | Windows（10-06 原始） | Windows（10-07 重跑） | Linux（第 1 次） | Linux（第 2 次） |
|---|---|---|---|---|
| `selftest` 逐项 | **30 PASS / 0 FAIL**，返回码 0 | 同 | **30 PASS / 0 FAIL**，返回码 0 | — |
| `battery` 汇总 | **PASS 24 / FAIL 0 / INFO 1** | **PASS 24 / FAIL 0 / INFO 1** | **PASS 24 / FAIL 0 / INFO 1** | **PASS 24 / FAIL 0 / INFO 1** |
| 逐项判定（check+item 做键） | 基准 | 与基准**逐项相同** | **与基准逐项相同（0 项不一致）** | 同上 |
| 两进程模式 | 60/60 成功 | — | 60/60 成功 | — |

**怎么读这张表**：`battery` 的 25 行里，**24 行是 PASS、1 行是 INFO**（`B11` 是"只报不判"的
朴素口径对照，本来就不该有 PASS/FAIL）。**判定不一致 0 项** = 同一套检查在两个操作系统上
给出的结论**逐项相同**。

对照用的三份 `checks.csv`（可直接 diff）：
`results/b0/battery_20261006T172400328Z_1144-1_checks.csv`、
`results/b0_linux/battery_20261007T034431614Z_4394-1_checks.csv`、
`results/b0_linux/win_rerun/battery_20261007T034941871Z_15640-1_checks.csv`。

### 2.2 两侧的关键数字对照（同一天，剔除了机器状态的干扰）

| 量 | Windows（10-07） | Linux（10-07） | 差 |
|---|---|---|---|
| 臂 `overlap0` 成功率 | 120/120 | 120/120 | 0 |
| 臂 `overlap0` 墙钟 / 吞吐 / 在途峰值 | 10.731 s / 11.182 req·s⁻¹ / 10 | 10.712 s / 11.203 req·s⁻¹ / 10 | 墙钟 −19 ms |
| 臂 `overlap0` TTFT p50 | 0.174932 s | 0.174021 s | **−0.91 ms** |
| 臂 `overlap0` 端到端 p50 | 0.794532 s | 0.794330 s | **−0.20 ms** |
| 臂 `overlap0` 端到端 **max** | 0.818771 s | 0.795143 s | **−23.6 ms**（Windows 尾巴长） |
| 臂 `overlap0` TPOT p50 | 0.019993 s | 0.020000 s | +7 µs |
| 臂 `overlap50` 服务端重叠量中位 | 0.0314572 s | 0.0314572 s | **逐位相同** |
| 服务端窗口 / Σ服务端阶段 | 0.792914 / 0.792914 s | 0.792914 / 0.792914 s | **逐位相同** |
| KV 字节数 / 流字节数 | 25165824 / 4993 | 25165824 / 4993 | **逐位相同** |
| 覆盖判定（客户端 / 服务端） | 120 PASS / 120 PASS，最大空档 0.0 s | 同 | 相同 |
| LOO 最小空档（p50） | 1.17 ms | 0.103 ms | **−1.07 ms** |
| LOO 最小空档（max） | 174.9 ms（个别请求） | 0.466 ms | −174 ms |
| 两进程模式 TTFT p50 / e2e p50 | 0.134227 / 0.478968 s | 0.133013 / 0.478209 s | −1.2 ms / −0.76 ms |
| 两进程模式在途峰值 | 6 | 5 | −1（边界效应，不影响判定） |

> **怎么读**：最后一列**小不代表"谁更好"**——这两个操作系统不是在比谁快。
> 这张表要看的只有两件事：①**总时长**的差都在 1 毫秒以内（除了 Windows 的长尾）；
> ②**服务端自报量逐位相同**（说明"配方向管道"这条链路两边一模一样，差异只出现在
> 客户端本地的计时上）。
>
> 完整表（含每列 p50/p95/max 与 run_id）在 `results/b0_linux/summary_table.csv` 与
> `results/b0_linux/os_diff.json`。

---

## 3. 差在哪（每条都有实测数字或原文报错）

### 3.1 ★ 时钟：`time.time()` 的有效刻度差约 2000–4000 倍（这是所有差异的根）

| 项 | Windows 11 | Linux（WSL2） |
|---|---|---|
| `time.get_clock_info('time').implementation` | `GetSystemTimeAsFileTime()` | `clock_gettime(CLOCK_REALTIME)` |
| `get_clock_info('time').resolution`（*声称*值） | **0.015625 s**（15.625 ms） | **1e-09 s** |
| `monotonic` 实现 | `GetTickCount64()` | `clock_gettime(CLOCK_MONOTONIC)` |
| **实测有效刻度**（50 ms 紧循环里不同取值的个数） | 50 ms → **50 个** ⇒ 约 **1.0 ms** 一格 | 50 ms → **156,869 个** ⇒ 约 **0.238 µs** 一格 |
| 实测有效刻度（探针进程内，看相邻时间戳差） | 约 **0.502–0.514 ms** | 相邻时间戳差最小 **238 ns** |
| 工具 `_clock_resolution()` 实测（8 次运行） | 0.506 / 0.998 / 0.999 / 1.000 / 1.000 / 1.309 / 1.504 ms，中位 **1.0 ms** | 1.91 / 2.38 / 2.38 / 2.62 / 2.86 / 3.10 / 3.58 / 3.58 µs，中位 **2.62 µs** |
| `time.perf_counter()` 有效刻度 | 0.3–0.5 µs | 0.35–1.2 µs |

三条读法：

1. **Windows 声称 15.625 ms、实际约 1 ms**：两者都不对，所以 manifest 里记的**实测值**是有用的。
   另外 Windows 的"一格"不是固定的——紧循环里约 **1.0 ms**，探针进程里约 **0.5 ms**
   （系统定时器精度会随进程状态变），所以同一台机器上两次运行的 `time_time_resolution_s`
   可以差 3 倍（0.506 ms vs 1.504 ms）。
2. **Linux 的 0.238 µs 不是钟的极限，是浮点数的极限**：`time.time()` 返回的是
   "从 1970 年起的秒数"这个 float64。2026 年的秒值约 1.79×10⁹，float64 在这个量级的
   最小步长恰好是 `2^-22 s = 0.2384 µs`——实测到的最小增量就是 0.238 µs，
   工具测到的 1.9 / 2.4 / 2.9 / 3.6 µs 全是它的 8 / 10 / 12 / 15 倍。
   ⇒ **在 Linux 上想再细，只能改用 `time.time_ns()`**（实测 411 ns / 872 ns 这种整数纳秒），
   但那会牺牲与其它语言/工具的对齐，**不建议在阶段 B 随便改**。
3. **工具记的那个"分辨率"是单样本估计、会偏高**：`_clock_resolution()` 取的是紧循环里
   **第一个**正增量，只能给出上界。所以**不要拿两侧的 `time_time_resolution_s` 直接相除
   得出"精度差 380 倍"**——按有效刻度算，实际差是 **约 2000–4000 倍**。

### 3.2 ★ 客户端五段：Windows 有两段**量不出来**，Linux 五段全能量

探针（`probe_stage_durations.py`）把客户端时间轴的 5 段逐段量了出来。
**下面是 3 次独立 Windows 运行 + 1 次 Linux 运行的原始统计**（`_probe/*.intervals.json`）：

| 阶段 | Windows「零长」的请求数 | Windows 最小非零值 | Linux「零长」的请求数 | Linux 最小非零值 | Linux 中位 |
|---|---|---|---|---|---|
| `send`（组请求+写出去） | 12–21 / 120 | 502 µs | **0 / 120** | 229 µs | 978 µs |
| `wait_first_byte`（等首字节） | **85–104 / 120** | 504–512 µs | **0 / 120** | **67 µs** | 273 µs |
| `to_first_content`（首字节→首个内容） | 0 / 120 | 171.5 ms | 0 / 120 | 172.2 ms | 172.7 ms |
| `decode_stream`（逐字生成） | 0 / 120 | 618.1 ms | 0 / 120 | 619.6 ms | 620.2 ms |
| `teardown`（收尾） | **110–119 / 120** | 504–515 µs | **0 / 120** | **57 µs** | 116 µs |

**怎么读**：这张表左边三列是 Windows，右边三列是 Linux。
"零长的请求数"越多，说明这一段越量不出来。**所有 Windows 非零值都不小于 502 µs**——
这正好是它的时钟刻度，说明"0"不是真的 0，而是"比一格还短，被吞掉了"。
Linux 上**零长次数是 0/120，五段全部可分辨**，最短的一段是 **57 µs**（收尾）。

> 表里 Windows 的区间是 3 次独立运行的实测范围；Linux 那一列取自**唯一一次带原始区间落盘**的运行
> （`_probe/stage_dbg_lin.intervals.json`），所以它和 §3.3 用的"5 次运行中位数"会有零点几毫秒的出入，
> 两者都是实测值，不是矛盾。

### 3.3 端到端总量几乎一致（所以 3.2 的差异不改变结论）

| | Windows | Linux | 差 |
|---|---|---|---|
| `send + wait_first_byte + to_first_content`（≈TTFT） | 1206.8 + 0 + 173782.9 = **174,989.7 µs** | 1022.3 + 292.5 + 172765.1 = **174,079.9 µs** | **+0.91 ms** |
| 五段合计（≈端到端） | **794,502.5 µs** | **794,342.5 µs** | **+0.16 ms** |
| 电池实测端到端 p50 | 0.794532 s | 0.794330 s | +0.20 ms |

（探针与电池是两条独立路径，**两边的结论对得上**，差 0.04 ms。）
本表的逐段数字用的是 **5 次运行各自中位数**，与 §3.2 单次运行的中位略有出入，属正常。

有意思的是**内部切分不一样**：Windows 的 `to_first_content` 比 Linux **大 1.02 ms**，
而 `decode_stream` 比 Linux **小 0.64 ms**。这 **1.02 ms 与 Windows 的时钟刻度同量级，
因此分不清是"真差异"还是"每次边界都被向上取整到下一格"造成的量化偏差**。
写正文时不要把这一条当结论——它只说明**Windows 上的"分段耗时"不能与 Linux 的逐段对比**。

### 3.4 判据本身的余量：两边都过，但"余量"的含义不同

| | Windows | Linux |
|---|---|---|
| LOO 最小空档 p50 | 1.01–1.35 ms | 0.096–0.123 ms |
| LOO 最小空档 min / max | 0.313 ms / 174.9 ms | 0.056 ms / 0.466 ms |
| 判定下限 `LOO_EPS_S` | 1 µs | 1 µs |
| 表面余量 | ~1000× | ~100× |
| 覆盖最大空档（120 请求） | 0.0 s | 0.0 s |

**怎么读**：**两边的 LOO 判据都通过**（这正是 B0 的结论之一）。
但 Windows 那个"1000× 余量"是**假的大**：因为它看不见小于 0.5 ms 的段，
LOO 必然被抬到一格以上。**Linux 的 100× 才是真实余量**——它量到了 Windows 完全看不到的
57–120 µs 段落，仍然稳稳高于 1 µs 的下限。所以：
**B0 那句"每拿掉一段都应立刻露空档"在 Linux 上被检验得比 Windows 上更充分，
不是在 Linux 上变弱。**

### 3.5 错误分类一致，但 `error_detail` 是**平台相关的自由文本**

5 种故障注入**全部归到期望类别，两侧一致**。唯一差别是"重置连接"那一条的**原文**：

| 故障模式 | 期望类别 | Windows 的 `error_detail` | Linux 的 `error_detail` |
|---|---|---|---|
| `reset_mid_stream` | `stream_broken` | `ConnectionResetError(10054, '远程主机强迫关闭了一个现有的连接。', None, 10054, None)` | `ConnectionResetError(104, 'Connection reset by peer')` |

- `http_500` / `hang` / `disconnect_mid_stream` / `truncate_clean` 四条的 `error_detail`
  两侧**逐字符相同**（`HTTP 500`、`socket timeout after 2.0s`、空、空）。
- 数字码不同（**10054 vs 104**）是 WSA 错误码与 errno 的区别；Windows 的文本还是**本地化**的
  （中文系统上就是中文，这也是为什么它在 GBK 控制台上会显示成乱码）。
- ⇒ **`error_class` 可以做跨平台的判定依据；`error_detail` 绝对不可以做字符串匹配**
  （既不跨平台，也不跨语言环境）。

### 3.6 文件格式：**完全一致**（逐字节查过）

| 产物 | Windows | Linux |
|---|---|---|
| `*_requests.csv` / `*_tokens.csv` / `*_checks.csv` 行尾 | CRLF（121 / 1441 / 26 个） | CRLF（121 / 1441 / 26 个） |
| 单独 LF | 0 | 0 |
| `*_summary.json` / `*_manifest.json` 行尾 | LF | LF |
| BOM | 无 | 无 |
| 中文路径（`/mnt/e/代码项目文件/论文/…`）写入 summary | 合法 UTF-8、未转义 | 合法 UTF-8、未转义 |

**补充一句**：在 Windows 上用 GBK 控制台 `print` 这些中文路径会显示成乱码，
但那是**显示问题不是数据问题**——直接读原始字节是合法的 UTF-8
（`e4 bba3 e7 a0 81 …` = "代码"）。CSV 行尾两边都是 CRLF，是因为 Python 的
`csv` 模块默认就写 `\r\n`，与操作系统无关。

### 3.7 ★ 端口重复绑定：两个平台**行为相反**（重要，且危险方向是 Windows）

同一个端口上起第二个服务（用的就是 `measure_serving.py` 里的
`ThreadingHTTPServer`，其 `allow_reuse_address = 1`）：

| | 结果 |
|---|---|
| Windows | **第二个实例也绑定成功**（`SO_REUSEADDR` 在 Windows 上允许"抢"端口），后续新连接会**静默**被第二个实例接走 |
| Linux | **`OSError: [Errno 98] Address already in use`** ——立刻失败，不会静默 |

复现命令在 `results/b0_linux/console_os_diff*.txt` 之外，用 3 行 Python 即可（见 §7 第 5 条）。
**含义**：在 Windows 上，一个残留的假后端会导致你**量错对象而且不报错**；
在 Linux 上同样的错误会**马上炸给你看**。所以：
① **阶段 B 在 Linux 上跑更安全**；
② 这条也说明"B0 的 Windows 数据有没有被残留后端污染"是一个**该被排除的可能**——
排除依据是 §2.2 里**服务端自报量逐位相同**（服务端窗口 0.792914 s、
重叠量 0.0314572 s、KV 字节数 25165824、流字节数 4993 全对得上配方），
如果量到的是别的进程，这些数对不上。

### 3.8 假后端的启动横幅：**两个平台都会丢**（不是 Linux 特有）

`fake-backend` 起手会打印一行 `[fake-backend] 监听 http://127.0.0.1:9520 …`。
把 stdout 重定向到文件、几秒后 `kill` 掉，**两侧产物都是 0 字节**
（`results/b0_linux/console_fakebackend_9520.txt` 与 `_banner_win_probe.txt` 都实测为 0 字节）。
原因是 Python 在非终端下用块缓冲、进程被信号打断时缓冲区没 flush。

**⇒ 阶段 B 的启动脚本不要 grep 这行横幅来判断"后端就绪"**，
要改成"反复尝试连端口，连上就算就绪"。这条**不是 Linux 缺陷**（Windows 一样），
但在 Linux 上更容易踩，因为启动脚本都是 bash。我**没有**为此改
`measure_serving.py`（它不是 Linux 专属问题，改了就等于动一份已经在 Windows 上
验证过的文件，收益为零、风险非零）。

### 3.9 WSL2 的时钟稳定性（做个初查）

`time.time()` 在 Linux 上是 `CLOCK_REALTIME`（**可被调整**），WSL2 又会与宿主同步时钟。
20 秒、40 个样本实测：

```
(time.time() - time.monotonic()) 偏移：首 = 末 = 1791343371.637489
相邻偏移变化：min = −0.24 µs，max = +0.48 µs；整段漂移 = −0.24 µs
```

⇒ **20 秒内没有发生跳变**。但**只测了 20 秒**，不能据此说"长跑不会跳"。
阶段 B 跨机测量（客户端与服务端在两个时钟域）时，`srv_outside_s`
（服务端区间伸出客户端窗口的量，就是"时钟对齐体检量"）应当被**持续监控**——
它突然变大就是时钟被同步/跳变的信号。

---

## 4. 这一轮为"只读取证"新写的两个工具

两个都**只用标准库**、都**不修改被测代码**，都在两侧都能跑：

| 文件 | 作用 | 关键做法 |
|---|---|---|
| `sim/k8s_test/probe_stage_durations.py` | 把客户端 5 段**各自的时长**量出来（逐请求 CSV 里只有判定结果，没有分段时长） | 把模块里的 `check_coverage` 换成一层记录器（monkeypatch 只作用于本进程），拿到客户端真实的 5 段区间后再原样转调原函数；`--dump-intervals` 可把每个请求的原始区间落盘 |
| `sim/k8s_test/compare_b0_os.py` | 把两侧产物**按 label 配对**、逐列算分位数并给出差值；并直接比两份 `checks.csv` 的判定 | 配对键用 **label**（臂名/故障名），不用 run_id 里的序号——序号只在一次电池内唯一，一个目录里跑两次就会撞；同时登记了"两进程模式两侧标签不同"的别名（`standalone_smoke` ↔ `smoke_linux`） |

**自检**：同一份 `compare_b0_os.py` 分别在 Windows 与 WSL 上运行，
输出的 JSON **除路径分隔符字符串外逐位相同**（`os_diff.json` vs `os_diff_from_linux.json`）——
这本身也是一条"对照脚本没有平台偏差"的证据。

> 新写的脚本在 Windows 上**真实踩到过一次坑**：中文与 `∅` 这类符号直接 `print` 到
> GBK 控制台会抛 `UnicodeEncodeError: 'gbk' codec can't encode character '\u2205'`，
> 脚本跑到一半中断。已按本仓库入口脚本的既有约定补上 UTF-8 `reconfigure`
> （`measure_serving.py` 第 96–100 行就是范本）。

---

## 5. 发现的缺陷与处置

### 5.1 没有发现 Linux 专属的功能缺陷 ⇒ **`measure_serving.py` 一行未改**

判断依据（这四条合起来才敢下这个结论）：

1. 24 项判定与 Windows **逐项相同**，0 项不一致；
2. `selftest` 30 项全通过，返回码 0；
3. 两进程模式（就是阶段 B 要用的拓扑）在 Linux 上跑通，60/60；
4. 5 种故障注入全部归到期望类别，与 Windows 相同。

**不改的理由**：任务规定只允许为"Linux 兼容"而改。既然 Linux 上没有坏掉的东西，
改代码的收益是零，而风险不是零——它会让**已经在 Windows 上验证过的那批产物**
（`results/b0/`）对应的代码版本发生变化，而本仓库**还没有任何 git 提交**，
一旦改了就无法把"旧产物"和"旧代码"对上。所以**保持不动**是正确的处置。

### 5.2 必须带进阶段 B 的四条（不是缺陷，但会在 Linux 上咬人）

| # | 事项 | 具体该怎么做 |
|---|---|---|
| 1 | **覆盖容差 5 ms 的取值依据在 Linux 上不成立**（代码注释与 `B0_REPORT` §5.2 都写着"下界是本机 `time.time()` 约 1 ms"；Linux 实测 0.238 µs） | 结论不受影响（实测空档在两侧都**恒为 0**，容差根本没被用到）。但**阶段 B 的预注册里要写明"容差是平台相关的参数"**：Linux 上 5 ms 相对于钟刻度宽松了约 2 万倍，抓不住"漏记零点几毫秒的一段"这类问题。若要在 Linux 上保持同样的鉴别力，应把 `--cov-tol-s` 收到 `0.0005`；真正卡住鉴别力的仍是 `LOO_EPS_S = 1 µs` |
| 2 | **"每段都必要"这条在 Windows 上被检验得比字面弱** | Linux 上 5 段全有非零时长（最短 57 µs），Windows 上 `wait_first_byte`（85–104/120）与 `teardown`（110–119/120）恒为 0、且非零值不小于 502 µs（时钟一格）。**写正文时不要拿 Windows 的 LOO 表宣称"五段都验过"**，只可以说"三段"；Linux 的数据才支持"五段" |
| 3 | **`error_detail` 不能做字符串匹配** | 只用 `error_class`。Windows 是 `(10054, '远程主机强迫关闭了一个现有的连接。', …)`，Linux 是 `(104, 'Connection reset by peer')`，且 Windows 文本会随系统语言变 |
| 4 | **就绪判断不能靠日志横幅** | 横幅在重定向时两侧都丢（0 字节）。启动脚本要"探端口"而不是"grep 日志"。另：**残留后端在 Windows 上会静默抢端口**，Linux 上会报 `Errno 98`——在 Linux 上跑反而更安全 |

### 5.3 被剔除的一次异常样本（照实记录）

第一次跑 `probe_stage_durations.py`（脚本文件刚落盘、立即执行的那一次）出现了唯一一次
异常：`send` 的中位数是 **6.09 ms**（其余 4 次是 1.01 / 1.06 / 1.21 / 1.25 ms），
且出现了 **25.36 µs** 这种与该次时基（~0.5 ms）不一致的值，
`send` 的 p95 冲到 24.5 ms。
后续 **3 次干净复跑都没有再出现**（最小非零值稳定在 502–514 µs）。
判断：**受干扰样本**（新脚本首次执行 + 文件系统冷缓存 / 实时扫描），
**已从结论中剔除**，只在此处记录，产物保留在 `_probe/` 里不删。

---

## 6. 我没能验证到的项（一条都不含糊）

1. **只有 WSL2，不是裸机 Linux。** 内核是 `6.18.33.2-microsoft-standard-WSL2`，
   时钟由 Hyper-V 与宿主同步、回环走的是虚拟网络栈。**真 Ubuntu 服务器上必须再跑一遍**，
   尤其是 §3.1 的时钟与 §3.9 的时钟稳定性。
2. **操作系统与 Python 版本混在一起，没有分离。** Linux 侧是 **CPython 3.14.4**，
   Windows 侧是 **CPython 3.12.10**；本机 Windows 只装了 3.12、WSL 里只装了 3.14，
   所以**做不成"OS × 解释器"的四格对照**。可以分开说的只有：§3.1 的时钟差异是**平台 API 层面**
   的（`GetSystemTimeAsFileTime()` vs `clock_gettime(CLOCK_REALTIME)`），与解释器版本无关；
   而 §3.2 的逐段时长差里，**有多少来自解释器版本（3.12 vs 3.14 的线程/IO 开销）无法排除**。
3. **跨机器（两个时钟域）没测。** 这一轮全部在同机回环上。`srv_outside_s` 这个
   "时钟对齐体检量"在有真实网络与两个独立时钟时是什么量级，**完全没数**——
   这正是阶段 B1 第一件要量的事。
4. **真 vLLM 没测**（本机无 GPU），沿用 `B0_REPORT` §8.1 的未验证项清单。
5. **错误类别仍只有 5 种**（`429`、`502/504`、TLS 握手失败、DNS 超时未覆盖），
   沿用 `B0_REPORT` §3 的声明。
6. **WSL2 时钟只测了 20 秒**（§3.9），不足以说"长跑不跳变"。
7. **没测非回环的绑定路径**（例如 `--bind 0.0.0.0`）、也没测高并发（>16）下的行为——
   按硬性约束，不碰 `eth0` 就做不了。
8. **没有做"同一端口被占用时电池的行为"这一类破坏性实验**（§3.7 只做了最小复现，
   没有去验证"battery 撞端口时会怎样"），因为在别人跑实验的机器上不该制造这种干扰。

---

## 7. 复现命令

```bash
# —— Linux（WSL2）侧，解释器用系统 python3 ——
# 1) 判据自检
wsl -d Ubuntu -u root -- bash -c 'cd "/mnt/e/代码项目文件/论文/sim" && \
  python3 k8s_test/measure_serving.py selftest'

# 2) 完整电池（自带假后端，约 31 s；产物写进 b0_linux，绝不写 b0）
wsl -d Ubuntu -u root -- bash -c 'cd "/mnt/e/代码项目文件/论文/sim" && \
  python3 k8s_test/measure_serving.py battery \
    --out-dir "/mnt/e/代码项目文件/论文/sim/results/b0_linux"'

# 3) 两进程模式：先起假后端（绑回环，端口避开 8000/5173/9101-9103），再量它
wsl -d Ubuntu -u root -- bash -c 'cd "/mnt/e/代码项目文件/论文/sim" && \
  python3 k8s_test/measure_serving.py fake-backend --port 9520 \
    --token-count 24 --token-interval-ms 15 --prefill-ms 80 --kv-mb 16 --kv-gbps 3.2 & \
  sleep 2 && python3 k8s_test/measure_serving.py client \
    --url http://127.0.0.1:9520 --label smoke_linux --requests 60 --qps 10 \
    --concurrency 12 --save-tokens \
    --out-dir "/mnt/e/代码项目文件/论文/sim/results/b0_linux"'

# 4) 逐段时长探针（只读；--dump-intervals 会额外落盘每请求的原始区间）
python3 k8s_test/probe_stage_durations.py --port 9531 --dump-intervals \
  --out results/b0_linux/_probe/stage_dbg_lin.json
# 5) 端口重复绑定的最小复现（Linux 应报 Errno 98；Windows 上会静默成功）
python3 -c "from http.server import ThreadingHTTPServer as S; \
  a=S(('127.0.0.1',9702),type('H',(),{})); print('first ok'); S(('127.0.0.1',9702),type('H',(),{}))"

# —— 对照 ——
# Windows 侧同一套（解释器换成 sim/.venv/Scripts/python.exe，约 31 s）
.venv/Scripts/python.exe k8s_test/measure_serving.py battery --port 9600 \
  --out-dir results/b0_linux/win_rerun
# 两侧产物逐列对照（同一份脚本，两侧都能跑）
.venv/Scripts/python.exe k8s_test/compare_b0_os.py \
  --win results/b0 --linux results/b0_linux --out results/b0_linux/os_diff.json
python3 k8s_test/compare_b0_os.py --win results/b0 --linux results/b0_linux
```

---

## 8. 产物清单（全部在 `sim/results/b0_linux/`，除了两个新脚本）

| 文件 | 内容 |
|---|---|
| `battery_20261007T034431614Z_4394-1_checks.csv` / `…_5783-1_checks.csv` | **Linux 两次电池的 25 项判定**（权威） |
| `battery_*_battery.json` | 两次电池的完整明细（含两臂配方、每次运行的 summary） |
| `win_rerun/battery_20261007T034941871Z_15640-1_checks.csv` | **Windows 当天补跑的 25 项判定**（同一天对照用） |
| `serving_*_{requests.csv,summary.json,manifest.json}` | Linux 两次电池 + 两进程模式的逐请求级产物（121 行 ≈ 120 请求） |
| `serving_20261007T034512266Z_5032-1_tokens.csv` | 两进程模式 `--save-tokens` 的每 token 到达时刻（1441 行） |
| `summary_table.csv` | Windows(10-06) / Windows(10-07) / Linux 的关键数字并排表 |
| `os_diff.json`、`os_diff_from_linux.json`、`os_diff_sameday.json` | 逐列分位数对照（两份不同侧生成的，用来核对脚本自身无平台偏差） |
| `console_selftest.txt`、`console_selftest_windows.txt`、`console_battery.txt`、`console_battery_win_rerun.txt`、`console_twoprocess.txt`、`console_os_diff*.txt` | 各次运行的原始控制台输出（报告里所有数字的出处） |
| `stage_durations_{windows,linux}.json` | 客户端 5 段时长的 5/2 次采样 |
| `_probe/` | 探针的额外运行（含 `*.intervals.json` 原始区间、被剔除的异常样本、Windows 的 3 次干净复跑） |
| `_banner_win_probe.txt`、`console_fakebackend_9520.txt` | §3.8 横幅丢失的两侧证据（均 0 字节） |
| `sim/k8s_test/probe_stage_durations.py`、`sim/k8s_test/compare_b0_os.py` | 本轮新写的两个只读工具 |

`sim/results/b0/` 与 `sim/k8s_test/B0_REPORT.md` **未被写入**（只读取）。

---

## 9. 一句话总结

**B0 的 24 项判定在 Linux 上原封不动全部通过、与 Windows 逐项相同，没有暴露任何功能缺陷，
因此 `measure_serving.py` 一行未改；真正的差异是"尺子的粗细"——Windows 的 `time.time()`
每 0.5–1 毫秒才走一格，导致客户端 5 段里有 2 段在大多数请求上量不出来（恒为 0），
而 Linux 的刻度是 0.238 微秒、5 段全部可分辨（最短 57 微秒）；两边端到端总量只差 0.2 毫秒，
所以结论不变、分辨率变好。附带发现：残留服务抢端口在 Windows 上静默成功、在 Linux 上直接报错。**

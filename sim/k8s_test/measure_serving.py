"""阶段 B0：面向"OpenAI 兼容接口"的**引擎无关**逐请求测量工具（本机可自足跑通）。

为什么单独写这一个文件，而不改 `measure_system.py`：
    `measure_system.py` 的 worker 是 **CPU 忙等的假 worker，根本不跑模型**——它证明的是
    "真实字节传输 + 真实内核限速"（阶段 A），不是"模型服务的阶段分界"。阶段 B 要量的是
    **首字等待（TTFT）、逐字间隔（TPOT）、前处理/KV 传输/生成各阶段起止时刻、KV 真实字节数**，
    这些量在阶段 A 的工具里根本不存在。所以新写一个，且**不改动任何已有文件**。

引擎无关是什么意思：
    本工具只认 **HTTP + 流式 SSE** 这种"OpenAI 兼容"的对外行为，不 import 任何推理引擎
    （不 import vllm，不 import transformers），因此：
      · 对**假后端**（本文件自带，本机就能跑）有效；
      · 对**真 vLLM / 其它 OpenAI 兼容服务**同样有效——只要你把 URL 指过去。
    这正是阶段 B/C 需要的：先在本机把测量链路跑通，再去租 GPU 的机器上量同一套字段。

================= 关键设计决定：为什么**不用**"加总等于端到端"验收 =================
阶段 A（`measure_system.py --role transfer`）有一条恒等式：
    "入口 + 上传 + 服务端段 + 下传 = 端到端"，残差精确为 0。
那是对的，因为那四段是**同一条串行时间轴切出来的四刀**，数学上就是恒等式。

**阶段 B 不能这么验收。** 三条理由：
  1. **阶段会互相重叠。** vLLM 的拆分式前处理官方文档写明 worker 连接器是
     "layer-by-layer KV cache store and load"（逐层存/取 KV），即某一层的 KV 正在传的同时，
     下一层还在算；NIXL 指标也把 "post time（提交给网卡）" 与 "xfer time（真正的数据搬运）"
     分开报。把重叠的时间直接相加，和必然大于端到端。
  2. **两端时钟域不同。** vLLM 官方 metrics 文档写明："monotonic clocks differ between
     processes ... it is meaningless to compare monotonic timestamps from different processes"。
  3. **有归属不明的残差是正常的**：网络往返、TCP 落地、客户端自身开销本来就不属于任何一个
     "服务阶段"。强行要求和为 0，等于逼着标记者把网络时间摊派给某个阶段。

因此本工具把时间轴**分成两套互不混加的区间集合**：
  · **客户端可观测分解**（`send / wait_first_byte / to_first_content / decode_stream / teardown`）
    ——从外面看得见、首尾相接铺满 `[请求开始, 请求结束]`。这是"时间都被记了"的承担者。
  · **服务端语义阶段**（`queue / prefill / kv_xfer / decode`）——概念上对应 vLLM 的
    排队/前处理/KV 传输/生成。它们**只铺满服务端自己的窗口**，并且**可以互相重叠**。

对应两条判据（`check_coverage()`）：
  (A) **覆盖**：某一区间集合的并集必须铺满它自己的窗口；未被任何区间覆盖的空档
      （"无归属时间"）总和与单段最大值都要 ≤ `--cov-tol-s`。
  (B) **每段都是必要的**（leave-one-out）：把任意一个**有时长**的区间拿掉后，必须立刻露出
      一段 > 1 µs 的空档。否则那段根本没被记进去（或完全被别段遮住）。这条是给判据本身
      做的哨兵，防止"并集覆盖"退化成一个永远通过的空判据。
  (C) `Σ阶段 − 端到端` **不参与判定**，只作为信息照实打印。有重叠时它**应当** > 0；
      阶段 A 的恒等式在这里预期会失败，失败才是对的。

================= 口径：输出块（chunk）≠ token =================
拿真 vLLM（Qwen2.5-1.5B）实测后发现，**一个输出块不总等于一个 token**（本文件第五版修正）：
  · 中文 / emoji 提示词下会出现"带 choices、但 text 为空"的块（实测 32 个 token 只出 30 个
    有文本的块）。**这类块也是输出块**，但旧口径只数文本非空的块，于是连"块数"都偏小。
  · 服务刚启动后的第一条流式请求，实测一次把 31 个 token 挤进一个块。
服务端自报的 token 数有两个出口：`usage.completion_tokens`（OpenAI 兼容的
`stream_options.include_usage`）与逐块 `token_ids`（vLLM 扩展 `return_token_ids`）。
**两个都拿不到时就不报 token 口径的量**，绝不拿块数冒充 token 数。

因此本文件里两套口径**并存、不混用**（字段含义以 `ReqResult` 上的注释为准）：
  · `chunk_interval_*`   —— **输出块**间隔（含文本为空的块）。块口径的正确名字。
  · `tpot_*`             —— **历史命名**，按"文本非空的内容块"间隔算，只留作旧产物复核；
                             **不得当作"逐 token 间隔"**（它既不认空文本块，也不知道一块装几个 token）。
  · `n_content_chunks`   —— 文本非空的块数（**不是 token 数**）。
  · `n_output_chunks` / `n_empty_text_chunks` —— 全部输出块数 / 其中文本为空的块数。
  · `srv_completion_tokens` / `srv_token_ids_total` —— 服务端自报的 token 数；拿不到就留空。
  · `token_interval_mean_s` —— **逐 token 平均间隔（代理量）**，只在拿到服务端 token 数时才有值；
                             它由"token 数 + 解码窗口跨度"推出，**不是**逐 token 时间戳的分位数。

================= 可选的 `/metrics` 汇总快照采集 =================
配了 `--metrics-url` 就在一次运行期间按**固定间隔**抓一次 `/metrics`，把**原始正文 +
带时间戳的元数据**落盘（append-only）。**不配这个参数时，整条采集路径一行都不走**，
现有行为与本参数引入前逐位相同。

它只回答一个问题：**整次运行前后，服务端自报的汇总计数变了多少。**
它**不**回答"某个请求花了多少 KV 传输时间"——理由（vLLM 的 NIXL 日志本身按一段时间汇总、
官方指标按传输统计、不带请求标识，无法自动匹配到本工具里的 req_id）已经写进
`SNAPSHOT_SCOPE_NOTE`，并逐条进快照 JSON、进 summary.json、进报告，避免以后被误用。

采集结果有**四种状态**，必须分得清（缺项 ≠ 数值为零）：
    `未配置地址`                    没给 `--metrics-url`，本次没有采集；
    `抓取失败`                      配了地址但抓不到（超时/拒绝/HTTP 错误），**记下错误原文**；
    `成功抓到指标但没有 NIXL 项`    抓到了 `/metrics`，但里面没有 NIXL 序列 → **记"未出现"**；
    `抓到 NIXL 项`                  里面确实有 NIXL 序列。
**`没有 NIXL 项` 一律记成"未出现"，绝不记成 0**：vLLM 0.31.0 的 NIXL 指标要启用相应
KV 连接器才会出现，单实例普通服务看不到它们是**预期现象而不是故障**（B0_VLLM_REPORT §3）。

================= 角色与用法 =================
    selftest      纯逻辑自检（区间并集/空档/重叠/覆盖判据/metrics 四态的负对照），不开网络。
    fake-backend  **假后端**：本机 HTTP 服务，按设定吐 N 个 token、按设定间隔，
                  并**伪造**各阶段时刻与 KV 字节数。没有显卡也能把整条链路跑通。
    fake-metrics  **假指标服务**：本机 HTTP 服务，按配方吐一份 `/metrics` 正文
                  （有 NIXL / 没有 NIXL / 只在注释里提 NIXL）与一个 `/version`。
                  真机在这个配置下看不到 NIXL 项，所以"抓到 NIXL 项"这一种状态
                  **只能靠它验证**（值全部是伪造的，只用来验状态机，不作为任何结论）。
    client        逐请求测量（开环并发），对任意 OpenAI 兼容 URL 有效，落盘 CSV。
    battery       **B0 的验收电池**：自带假后端与假指标服务，跑两臂（无重叠 / 有重叠）+
                  口径对照臂 + metrics 四态臂 + 一次故障注入，逐项给 PASS/FAIL/INFO，
                  是不需要 GPU 的那部分证据。

用法（在 `sim/` 目录下执行；解释器用 `sim/.venv/Scripts/python.exe` 或 WSL 的 python3）：
    python k8s_test/measure_serving.py selftest
    # 只起假后端（另开一个终端跑 client）
    python k8s_test/measure_serving.py fake-backend --port 9500 --token-count 32
    python k8s_test/measure_serving.py client --url http://127.0.0.1:9500 \
        --requests 120 --qps 12 --concurrency 16 --out-dir results/b0
    # 一条命令跑完整套 B0 验收（推荐）
    python k8s_test/measure_serving.py battery --out-dir results/b0

产物（append-only：文件名带 UTC run_id，不覆盖）：
    results/b0/<run_id>_requests.csv   每请求一行的全部字段
    results/b0/<run_id>_tokens.csv     每**输出块**一行的到达时刻（--save-tokens；
                                       text_empty 列标出"有 token 但文本为空"的块）
    results/b0/<run_id>_checks.csv     电池逐项检查（check,item,measured,verdict,note）
    results/b0/<run_id>_summary.json   聚合分位数、错误分类、覆盖判定汇总
    results/b0/<run_id>_manifest.json  配置全文、环境、代码版本、负载参数

配了 `--metrics-url` 时**额外**落盘（同样 append-only，run_id 唯一）：
    <out>/<run_id>_metrics_scrapes.csv   每次抓取一行：状态 / 时间戳 / 错误原文 / 版本
    <out>/<run_id>_scrape_<seq>_<phase>.json
                                         每次抓取的**完整记录**：元数据 + `raw_body` 原文
                                         （phase ∈ start / periodic / end）
    四态汇总另进 <run_id>_summary.json 的 `metrics_snapshot` 与 manifest 的 `metrics`。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import http.client
import json
import math
import os
import platform
import random
import socket
import statistics
import struct
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Windows 控制台默认可能是 GBK，不重设会在中文输出上乱码（本仓库所有入口脚本都这么做）
# ---------------------------------------------------------------------------
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:
        pass

DEFAULT_OUT_DIR = "results/b0"

# 错误分类：**必须分类记录**。否则"超时"和"连不上"会被混成一个 "failed"，
# 而这两者在阶段 C 里意味着完全不同的动作（一个要调超时/查负载，一个要查隧道与安全组）。
ERR_NONE = ""
ERR_TIMEOUT = "timeout"                 # socket 超时
ERR_CONNECT = "connect_error"           # 连不上（拒绝/不可达/DNS）
ERR_HTTP_5XX = "http_5xx"               # 服务端 5xx
ERR_HTTP_4XX = "http_4xx"               # 客户端 4xx（多半是请求构造问题）
ERR_STREAM_BROKEN = "stream_broken"     # 200 之后传输层真的断了（分块协议不完整/连接被掐）
ERR_STREAM_INCOMPLETE = "stream_incomplete"  # 流"语法上正常结束"但内容不完整（没有 [DONE]、也没有 finish_reason）
ERR_BAD_JSON = "bad_json"               # SSE 里的 JSON 解不开
ERR_NO_CONTENT = "no_content"           # 流正常结束但一个内容片段都没有
ERR_INTERNAL = "internal_error"         # **本工具自己算错**（不是网络/服务端的问题）

# 客户端时间轴上的**可观测阶段**。它们首尾相接铺满整条请求时间轴，所以"并集覆盖"
# 在这条链上应当是构造性成立的——也正因如此，LOO 检查必须存在，否则判据没有信息量。
CLIENT_STAGES = (
    "send",             # 组请求 + 写出去（到 body 全部交给内核）
    "wait_first_byte",  # 等响应头/首字节
    "to_first_content", # 首字节 → 第一个**内容**块（这段含服务端排队+前处理+KV+生成首字）
    "decode_stream",    # 第一个内容块 → 最后一个**输出块**（空文本块也属于解码期）
    "teardown",         # 最后一个输出块 → 连接关闭/读完
)
# 服务端自报的**语义阶段**（与 vLLM 的 queue / prefill / KV 传输 / decode 对应）
SERVER_STAGES = ("queue", "prefill", "kv_xfer", "decode")

# 假后端能造的失败方式。**每一种都要能被单独识别**：它们在阶段 C 里对应不同的排查动作
# （5xx 查服务端、FIN 断流与规矩截断查对端收尾、RST 查中间层与连接、hang 查隧道与负载）。
FAIL_MODES = ("http_500", "disconnect_mid_stream", "truncate_clean", "reset_mid_stream", "hang")

# leave-one-out 的判定下限。刻意取一个"明显大于浮点噪声、又小于任何真实阶段时长"的值：
# 不能用 --cov-tol-s（默认 5 ms），因为 loopback 上 `send` 段可能只有零点几毫秒，
# 拿 5 ms 当 LOO 下限会让"每段都真的被记了"这条永远判 FAIL——判据本身要能站得住。
LOO_EPS_S = 1e-6

# battery 的"块 ≠ token"对照臂用：每几个内容块掺 1 个**文本为空**的输出块。
# 取 3 是为了让空文本块既足够多（能被判出来），又不至于把内容块压到接近 0
# （一个内容块都没有会触发 no_content，那是另一条路径，不是本对照要测的东西）。
BATTERY_EMPTY_EVERY = 3

# 覆盖容差（秒）。取值依据是**两个已知量级**，不是随手挑的：
#   · 下界：本机 `time.time()` 的实测分辨率约 1 ms（见 manifest 的 time_time_resolution_s），
#     容差必须明显大于它，否则"空档"里全是时钟量化误差；
#   · 上界：任何一个阶段时长都在 10 ms 以上（假后端的 queue/prefill/kv/decode 与真实服务的
#     排队/前处理/传输/生成同量级），容差必须明显小于它，否则覆盖判据会放过真的漏记。
# 5 ms 落在中间，并且与任何一档阶段时长都不相等——相等时判定会由浮点误差决定
# （与 SKILL.md 里"回退阈值不能等于任何一档 lag"同一个坑）。
DEFAULT_COV_TOL_S = 0.005

_run_seq_lock = threading.Lock()
_run_seq = [0]


def _new_run_id(prefix: str) -> str:
    """生成唯一 run_id。

    为什么要到毫秒：`battery` 会在**同一个进程、同几秒内**连跑三次客户端。
    若 run_id 只到秒，三次运行会撞成同一个文件名 → **后一次覆盖前一次**，
    直接违反 append-only。加上进程内序号与毫秒，既唯一又仍可按时间排序。
    """
    with _run_seq_lock:
        _run_seq[0] += 1
        seq = _run_seq[0]
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")[:-3] + "Z"
    return f"{prefix}_{ts}_{os.getpid()}-{seq}"


# ===========================================================================
# 一、区间代数（纯函数，先被 selftest 验证）
# ===========================================================================
def normalize(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """合并重叠或相接的区间，返回按左端点排序的不相交列表。"""
    ivs = sorted((float(a), float(b)) for a, b in intervals if b > a)
    out: list[tuple[float, float]] = []
    for a, b in ivs:
        if out and a <= out[-1][1]:
            if b > out[-1][1]:
                out[-1] = (out[-1][0], b)
        else:
            out.append((a, b))
    return out


def union_length(intervals: list[tuple[float, float]]) -> float:
    return sum(b - a for a, b in normalize(intervals))


def clamp(intervals: list[tuple[float, float]], lo: float,
          hi: float) -> list[tuple[float, float]]:
    """把区间裁剪到窗口 [lo, hi] 内。"""
    return [(max(a, lo), min(b, hi)) for a, b in intervals if min(b, hi) > max(a, lo)]


def gaps(intervals: list[tuple[float, float]], lo: float,
         hi: float) -> list[tuple[float, float]]:
    """窗口 [lo,hi] 里**没有任何区间覆盖**的空档（"无归属时间"）。"""
    cov = normalize(clamp(intervals, lo, hi))
    out: list[tuple[float, float]] = []
    cursor = lo
    for a, b in cov:
        if a > cursor:
            out.append((cursor, a))
        cursor = max(cursor, b)
    if cursor < hi:
        out.append((cursor, hi))
    return out


def overlap_length(intervals: list[tuple[float, float]]) -> float:
    """所有区间两两重叠部分的总长度（O(n²)；本工具的区间是十位数量级，够用）。

    它回答的是"重叠有多重"。注意 Σ区间长 = 并集长 + 重叠总量 **只在两两重叠、无三重重叠时**严格成立，
    所以本工具不把它当分解式用，只当指标量。
    """
    total = 0.0
    for i in range(len(intervals)):
        a1, b1 = intervals[i]
        for j in range(i + 1, len(intervals)):
            a2, b2 = intervals[j]
            total += max(0.0, min(b1, b2) - max(a1, a2))
    return total


def percentile(values: list[float], q: float) -> float:
    """线性插值分位数（与 numpy.percentile 默认口径一致，避免为此引入 numpy 依赖）。

    刻意不依赖第三方库：阶段 C 要在云主机的系统 python3 上跑同一个脚本，
    那时不一定有 venv（`summarize_real.py` 也是这个理由只用标准库）。
    """
    xs = sorted(float(v) for v in values if v is not None and math.isfinite(float(v)))
    if not xs:
        return float("nan")
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return xs[lo]
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def interval_stats(times: list[float]) -> tuple[float, float, float, float, float]:
    """把一串**按到达顺序排列的块时刻**换成相邻间隔的分位：(p50, p95, p99, mean, max)。

    设计成纯函数是为了让 selftest 能直接验它：块口径与 token 口径的差别最终就落在
    "喂进来的时刻序列含不含那些空文本块"上，把这一步抽出来才谈得上"判据可自检"。

    **少于 2 个时刻时五个值全返回 nan**，不返回 0.0：只有一个块根本谈不上"间隔"，
    回 0.0 会让下游把"无可测间隔"误读成"间隔为零"（另一种说谎方式）。
    """
    xs = [float(t) for t in times if t is not None and math.isfinite(float(t))]
    gaps_s = [b - a for a, b in zip(xs, xs[1:])]
    nan = float("nan")
    if not gaps_s:
        return (nan, nan, nan, nan, nan)
    return (percentile(gaps_s, 0.50), percentile(gaps_s, 0.95), percentile(gaps_s, 0.99),
            statistics.fmean(gaps_s), max(gaps_s))


# ===========================================================================
# 二、覆盖判据（本文件的核心；阶段 A 的"加总等于端到端"在这里被替换掉）
# ===========================================================================
@dataclass
class CoverageReport:
    span_s: float                 # 该区间集合自己的窗口长度
    sum_stage_s: float            # 各区间时长**简单相加**（重叠会被重复计入）
    union_s: float                # 并集覆盖长度
    overlap_s: float              # 两两重叠总量
    gap_total_s: float            # 无归属空档总长
    max_gap_s: float              # 最大单段空档
    outside_s: float              # 有区间伸到窗口之外的部分（时钟没对齐的信号）
    loo_min_gap_s: float          # leave-one-out：拿掉某一段后露出的最小空档
    loo_stage: str                # 那个"最不必要"的段名
    n_stages: int                 # 参与判定的、时长 > 0 的区间个数
    verdict: str                  # PASS / FAIL / N-A


def check_coverage(named: list[tuple[str, float, float]], lo: float, hi: float,
                   tol: float, loo_stages: tuple[str, ...] | None = None) -> CoverageReport:
    """对一个区间集合做"并集覆盖"验收。

    参数
      named     : [(区间名, 起, 止)]，同一名字出现多次会被并集自然合并
      lo, hi    : 该集合自己的窗口（客户端集合用请求区间；服务端集合用服务端窗口）
      tol       : 允许的最大空档（秒）
      loo_stages: 做 leave-one-out 的名字名单；None 表示对 named 里所有名字做
    """
    if hi <= lo:
        return CoverageReport(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, "", 0, "N-A")
    positive = [(n, a, b) for n, a, b in named if b > a]
    ivs = [(a, b) for _, a, b in positive]
    # 每个量都**显式转成 float**：`sum([])` 会返回**整数** 0，而 dataclass 上声明的是 float，
    # 于是下游按 `isinstance(x, float)` 过滤时会把"恰好为 0 的正常值"当成缺数据丢掉
    # （本文件第四版踩过：cov_gap_total_s 明明是 0，summary 里却成了 null）。
    span = float(hi - lo)
    sum_stage = float(sum(b - a for a, b in ivs))
    union = float(union_length(clamp(ivs, lo, hi)))
    ov = float(overlap_length(clamp(ivs, lo, hi)))
    gp = gaps(ivs, lo, hi)
    gap_total = float(sum(b - a for a, b in gp))
    max_gap = float(max((b - a for a, b in gp), default=0.0))
    outside = float(union_length(ivs) - union)

    # leave-one-out：逐个拿掉一个**有时长**的区间，看是否立刻露出空档。
    # 只对有时长的区间做——零长区间本来就无法承担覆盖，拿它去判"必要性"没有意义。
    names = [n for n in (loo_stages if loo_stages is not None else
                         tuple(dict.fromkeys(n for n, _, _ in positive)))
             if any(nn == n for nn, _, _ in positive)]
    loo_min = float("inf")
    loo_stage = ""
    for name in names:
        kept = [(a, b) for n, a, b in positive if n != name]
        worst = max((b - a for a, b in gaps(kept, lo, hi)), default=0.0)
        if worst < loo_min:
            loo_min, loo_stage = worst, name
    if not math.isfinite(loo_min):
        loo_min = 0.0

    ok_cover = max_gap <= tol
    ok_outside = outside <= tol
    ok_loo = (loo_min > LOO_EPS_S) if names else True
    verdict = "PASS" if (ok_cover and ok_outside and ok_loo) else "FAIL"
    return CoverageReport(span, sum_stage, union, ov, gap_total, max_gap, outside,
                          loo_min, loo_stage, len(positive), verdict)


# ===========================================================================
# 三、环境与运行标识
# ===========================================================================
def host_info() -> dict:
    return {
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "cpu_count": os.cpu_count(),
        "time_time_resolution_s": _clock_resolution(),
    }


def _clock_resolution() -> float:
    """量一下 `time.time()` 的实际分辨率。

    这不是可有可无的花边：本工具的阶段边界全部用 `time.time()` 记录
    （只有它是**跨进程可比**的钟），如果它的分辨率是十几毫秒（老 Windows 曾经如此），
    那么亚毫秒的阶段会被记成 0 长，覆盖判据和 TPOT 都会失真。
    把实测值写进 manifest，读的人才知道本批数据能分辨到什么量级。
    """
    best = 1.0
    prev = time.time()
    for _ in range(200000):
        cur = time.time()
        d = cur - prev
        if d > 0:
            best = min(best, d)
            break
        prev = cur
    else:
        return float("nan")
    return best


def code_version() -> dict:
    """代码版本。本仓库**尚无 git 提交**，所以这里预期回落到 "uncommitted"。

    按 AGENTS.md 的约定，这是预期行为不是 bug：`git rev-parse` 会失败时
    manifest 里 `code_version` 记成字符串 "uncommitted" 即可，不要"修"它。
    """
    import subprocess
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        ver = out.stdout.strip() or "uncommitted"
    except Exception:
        ver = "uncommitted"
    return {"code_version": ver, "note": "本仓库无 git 提交时恒为 uncommitted（预期）"}


# ===========================================================================
# 四、假后端（本机 HTTP 服务，伪造一个 OpenAI 兼容的流式接口）
# ===========================================================================
@dataclass
class FakeBackendConfig:
    """假后端的"配方"。**它伪造的是阶段时刻与 KV 字节数，不是模型输出**。

    为什么必须自带它：本机没有显卡，装 vLLM 也跑不起来。但"测量工具本身对不对"
    （TTFT 抓没抓准、SSE 分块有没有漏、覆盖判据会不会误判、错误分类准不准）
    必须在**不租机器**的前提下先验证一遍。假后端就是那个"已知答案"的对照物：
    它按固定配方生成各阶段时刻，我们把它的自报值与客户端的观测值对齐，
    就能看出测量链路哪一环掉了时间。
    """
    token_count: int = 32             # 吐多少个内容片段
    token_interval_s: float = 0.02    # 逐字间隔（块间隔）
    queue_s: float = 0.01             # 排队等待调度
    prefill_s: float = 0.10           # 前处理计算
    kv_mb: float = 24.0               # KV 传输量（MB）
    kv_gbps: float = 3.2              # KV 链路速率（Gbit/s）→ 传输时长由二者推出
    kv_overlap_frac: float = 0.0      # KV 传输与"前处理尾部"的重叠比例（逐层传 KV 的写照）
    fail_rate: float = 0.0            # 故障注入比例
    fail_mode: str = "http_500"       # http_500 | disconnect_mid_stream | truncate_clean | reset_mid_stream | hang
    seed: int = 1
    # **口径修正用**：每 `empty_text_every` 个内容块后面插一个"文本为空"的输出块
    # （真 vLLM 在中文/emoji 下会这么干）。0＝不插。有了它，"块数 ≠ 内容块数"
    # 以及"块间隔 ≠ 旧 tpot"这两件事就能在**没有显卡**的情况下被确定性地验证。
    empty_text_every: int = 0

    @property
    def kv_bytes(self) -> int:
        return int(self.kv_mb * 1024 * 1024)

    @property
    def kv_xfer_s(self) -> float:
        """由"字节数 ÷ 速率"推出传输时长——刻意让二者**互相自洽**。
        否则报告里的"字节数"和"传输时间"各自独立编造，看的人无法交叉核对。"""
        return (self.kv_bytes * 8.0) / (self.kv_gbps * 1e9) if self.kv_gbps > 0 else 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["kv_bytes"] = self.kv_bytes
        d["kv_xfer_s"] = round(self.kv_xfer_s, 6)
        return d


def _sse(obj: dict) -> bytes:
    return ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8")


class _FakeState:
    def __init__(self, cfg: FakeBackendConfig):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.n = 0
        self.rng = random.Random(cfg.seed)


class _DeadIO:
    """占位 I/O：连接已被我们主动打死，而 `socketserver` 收尾时还会再 flush/close 一次。
    用吞掉全部操作的占位对象顶掉那一次，免得把"预期内的失败"刷成一屏 traceback——
    那看起来像 bug，会误导后来的人去追一个不存在的问题（本文件第四版真被误导过）。"""
    closed = False

    def write(self, *a, **k):
        return 0

    def flush(self):
        return None

    def close(self):
        return None

    def readline(self, *a, **k):
        return b""

    def read(self, *a, **k):
        return b""


def _abort_socket(sock, *wrappers) -> None:
    """**强制 RST 关闭**（而不是优雅的 FIN），用来造"传输层真的断了"。

    为什么需要单独一种是"真正的断"：FIN 只是"我说完了"，对端读到的是正常的 EOF；
    RST 才会让对端的读操作直接报错。两者的区别正是 `stream_incomplete` 与
    `stream_broken` 的区别，所以要能把它们分别造出来。

    两个平台坑，都真的踩过：
      · SO_LINGER 的结构在 Windows 上是 (u_short, u_short)、在 Linux 上是 (int, int)，
        所以两种打包都试一遍——只写一种会在另一个平台上**静默退化成优雅关闭**，
        这个对照组就名存实亡了。
      · **必须先放掉 `makefile()` 出来的读写包装**：它们各持一份 fd 引用
        （`socket._io_refs`），不释放的话 `sock.close()` 只是标记"已关闭"、
        **并不会真的关掉 fd**，于是对端既收不到 RST 也收不到 FIN，只能一直等到自己的超时。
        本文件第四版就被这个坑住：RST 那一组 6/6 全被判成 `timeout`。
    """
    for fmt in ("ii", "hh"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack(fmt, 1, 0))
            break
        except OSError:
            continue
    for f in wrappers:
        try:
            f.close()
        except Exception:
            pass
    try:
        sock.close()
    except OSError:
        pass


def make_fake_handler(state: _FakeState):
    cfg = state.cfg

    class FakeHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        # 关掉每请求一行的访问日志：默认写 stderr，批跑时噪音很大，且与计时窗口无关。
        def log_message(self, fmt, *args):  # noqa: A003
            return

        # ---- 一个最小的 /v1/models，让"OpenAI 兼容"这件事可自证 ----
        def do_GET(self):  # noqa: N802
            if self.path.rstrip("/").endswith("/v1/models"):
                body = json.dumps({"object": "list", "data": [
                    {"id": "fake-model", "object": "model"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_error(404)

        def do_POST(self):  # noqa: N802
            t_req = time.time()
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            try:
                req = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                req = {}
            req_id = str(req.get("x_req_id") or self.headers.get("X-Request-Id") or "")
            with state.lock:
                state.n += 1
                seq = state.n
                fail = state.rng.random() < cfg.fail_rate
            if not req_id:
                req_id = f"auto-{seq}"

            if fail:
                mode = cfg.fail_mode
                if mode == "http_500":
                    body = b'{"error":{"message":"injected failure"}}'
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if mode == "hang":
                    # 故意不回任何东西：用来验证客户端把 socket 超时归到 timeout 而不是别的类。
                    time.sleep(float(req.get("_hang_s") or 30.0))
                    return
                # disconnect_mid_stream / truncate_clean 走下面正常分支，但在中途掐断

            want_stream = bool(req.get("stream", True))
            max_tokens = int(req.get("max_tokens") or cfg.token_count)
            n_tok = max(1, min(cfg.token_count, max_tokens))
            # 两个"取证开关"：请求里带了才回（与真服务的行为一致）。**缺了不影响任何别的行为**，
            # 所以关掉它们只影响"服务端 token 数"能不能取到，不影响块口径。
            _so = req.get("stream_options")
            want_usage = bool(_so.get("include_usage")) if isinstance(_so, dict) else False
            want_token_ids = bool(req.get("return_token_ids"))
            _p = req.get("prompt")
            n_prompt = len(_p.split()) if isinstance(_p, str) else 0

            # ---------- 伪造各阶段时刻（全部用墙钟，便于与客户端对齐） ----------
            # 服务端内部当然可以用单调钟算时长；但**跨进程**比较必须用墙钟 + 时钟同步
            # （vLLM 官方 metrics 文档明确说不同进程的 monotonic 时间戳不可相减）。
            # 同机时墙钟直接可比；跨机要靠 NTP，见 B0_REPORT 的"未验证项"。
            queue_a = t_req
            queue_b = queue_a + cfg.queue_s
            pre_a, pre_b = queue_b, queue_b + cfg.prefill_s
            kv_s = cfg.kv_xfer_s
            if kv_s > 0:
                # 逐层传 KV 的写照：传输区间的**起点前移**到前处理尾部之内，
                # 前移量 = overlap_frac × 传输时长。overlap=0 时退化成"前处理完才开始传"。
                kv_a = max(queue_b, pre_b - cfg.kv_overlap_frac * kv_s)
                kv_b = kv_a + kv_s
            else:
                kv_a = kv_b = pre_b
            dec_a = max(pre_b, kv_b)                    # 生成必须等 KV 到位
            dec_b = dec_a + max(0, n_tok - 1) * cfg.token_interval_s

            report = {
                "req_id": req_id,
                "server_req_start": queue_a,
                "server_req_end": dec_b,
                "stages": [{"name": nm, "start": a, "end": b} for nm, a, b in
                           (("queue", queue_a, queue_b), ("prefill", pre_a, pre_b),
                            ("kv_xfer", kv_a, kv_b), ("decode", dec_a, dec_b))],
                "kv_bytes": cfg.kv_bytes,
                "kv_mbps": (cfg.kv_bytes * 8 / 1e6 / kv_s) if kv_s > 0 else None,
                "fake": True,
            }

            # ---------- 非流式：一次性返回（确认工具对非流式也没崩） ----------
            if not want_stream:
                now = time.time()
                if dec_b > now:
                    time.sleep(dec_b - now)
                body = json.dumps({
                    "id": f"cmpl-{req_id}", "object": "text_completion",
                    "choices": [{"index": 0, "text": "tok " * n_tok,
                                 "finish_reason": "length"}],
                    "x_stage_report": report,
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            # ---------- 流式（SSE）：**逐 token 打时间戳的关键** ----------
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Request-Id", req_id)
            # 用 chunked 传输：每条 SSE 事件一到就能被对端读到。若用 Content-Length
            # 一次写完再发，很多中间层会缓冲到结束才推送，TTFT 就量不准了。
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            def wchunk(payload: bytes) -> bool:
                try:
                    self.wfile.write(b"%x\r\n" % len(payload) + payload + b"\r\n")
                    self.wfile.flush()
                    return True
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return False

            # 等前处理 + KV 传输做完，再吐第一个 token。这段等待正是 TTFT 的主体。
            now = time.time()
            if dec_a > now:
                time.sleep(dec_a - now)

            for i in range(n_tok):
                target = dec_a + i * cfg.token_interval_s
                now = time.time()
                if target > now:
                    time.sleep(target - now)
                if fail and i == max(1, n_tok // 2):
                    if cfg.fail_mode == "disconnect_mid_stream":
                        # **不写分块终止符**，直接把连接掐掉（发 FIN）。用来验证
                        # "对面说完了但内容不齐"能被识别。
                        # 注意：这与 truncate_clean 在**行式读取**下表现完全一样
                        # （CPython 把不完整的分块流当成 EOF 返回），所以两者都只能靠
                        # 语义终止信号识别——见 ERR_STREAM_INCOMPLETE 的说明。
                        try:
                            self.close_connection = True
                            self.connection.shutdown(socket.SHUT_RDWR)
                        except Exception:
                            pass
                        return
                    if cfg.fail_mode == "truncate_clean":
                        # 与上一条正好互补：**规规矩矩结束**，但既没有 `[DONE]`、
                        # 也没有任何 finish_reason。用来验证"语法完整但内容不完整"能被识别。
                        try:
                            self.wfile.write(b"0\r\n\r\n")
                            self.wfile.flush()
                        except Exception:
                            pass
                        self.close_connection = True
                        return
                    if cfg.fail_mode == "reset_mid_stream":
                        # 发 RST。**这是唯一能真正触发 `stream_broken` 的方式**：
                        # 优雅 FIN 与"规矩截断"在行式读取下都只表现为干净的 EOF
                        # （CPython 的 `HTTPResponse._peek_chunked` 把 `IncompleteRead`
                        # 当成 EOF 返回，而 `readline()` 走的正是 `peek`）。
                        # 所以工具**不能靠异常发现截断**，必须查语义终止信号——见 ERR_STREAM_INCOMPLETE。
                        _abort_socket(self.connection, self.wfile, self.rfile)
                        self.wfile = self.rfile = _DeadIO()
                        return
                chunk_choice = {"index": 0, "text": "tok ", "finish_reason": None}
                if (cfg.empty_text_every > 0 and i % cfg.empty_text_every == 0
                        and i != n_tok - 1):
                    # 造一个"**有 token 但文本为空**"的输出块：真 vLLM 在中文/emoji 下会这样
                    # （一个字的字节被劈成两半，先来一块空文本）。它**仍然是一个生成步骤、
                    # 占一个 token**，所以 token_ids 照给、下一块照等一个间隔——
                    # 于是两种口径的数会不一样：块口径数到 n_tok 个间隔，旧 tpot 口径
                    # 跳过这些块、把两个间隔并成一个（这正是要修的那件事）。
                    chunk_choice["text"] = ""
                if want_token_ids:
                    chunk_choice["token_ids"] = [i]   # 逐块自报 token（vLLM 的 return_token_ids）
                chunk = {"id": f"cmpl-{req_id}", "object": "text_completion",
                         "choices": [chunk_choice]}
                if i == n_tok - 1:
                    chunk["choices"][0]["finish_reason"] = "length"
                    # 服务端自报的阶段报告放在**最后一个内容块**里：客户端解析到
                    # x_stage_report 就存下来，且**不把它算作一个 token**，
                    # 于是它既不影响 TTFT/TPOT，也不需要额外一次往返。
                    chunk["x_stage_report"] = report
                if not wchunk(_sse(chunk)):
                    return
            if not wchunk(_sse({"x_stage_report": report})):
                return
            if want_usage:
                # 只在请求里带了 `stream_options.include_usage` 时才发（与真服务一致）。
                # **choices 是空列表**：它是 SSE 片段但**不是输出块**——这一点必须与
                # "有 choices 但文本为空"的块区分开，否则"块数"会凭空多 1。
                if not wchunk(_sse({
                        "id": f"cmpl-{req_id}", "object": "text_completion", "choices": [],
                        "usage": {"prompt_tokens": n_prompt, "completion_tokens": n_tok,
                                  "total_tokens": n_prompt + n_tok}})):
                    return
            wchunk(b"data: [DONE]\n\n")
            wchunk(b"")

    return FakeHandler


def start_fake_backend(cfg: FakeBackendConfig, port: int, bind: str = "127.0.0.1"):
    state = _FakeState(cfg)
    srv = ThreadingHTTPServer((bind, port), make_fake_handler(state))
    srv.daemon_threads = True
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    return srv, state


def run_fake_backend(args) -> int:
    cfg = FakeBackendConfig(
        token_count=args.token_count, token_interval_s=args.token_interval_ms / 1000.0,
        queue_s=args.queue_ms / 1000.0, prefill_s=args.prefill_ms / 1000.0,
        kv_mb=args.kv_mb, kv_gbps=args.kv_gbps, kv_overlap_frac=args.kv_overlap_frac,
        fail_rate=args.fail_rate, fail_mode=args.fail_mode, seed=args.seed,
        empty_text_every=args.empty_text_every)
    srv, _st = start_fake_backend(cfg, args.port, args.bind)
    print(f"[fake-backend] 监听 http://{args.bind}:{args.port}  "
          f"配方={json.dumps(cfg.to_dict(), ensure_ascii=False)}")
    print("               这是**假后端**：不跑任何模型，KV 字节数与各阶段时刻都是按配方伪造的。")
    print("               它的作用是让测量链路在没有显卡的机器上先跑通。Ctrl-C 退出。")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    srv.shutdown()
    return 0


# ===========================================================================
# 五、客户端：开环并发、逐 token 打时间戳
# ===========================================================================
@dataclass
class ReqResult:
    """一个请求的全部落盘字段。算不出的字段留空（CSV 里就是空串，pandas 读成 NaN）。"""
    run_id: str = ""
    label: str = ""
    req_id: str = ""
    scheduled_arrival_s: float = float("nan")   # 计划到达时刻（相对批次起点）
    start_s: float = float("nan")               # 实际开始发送
    inflight_at_submit: int = 0
    overloaded: int = 0
    ok: int = 0
    error_class: str = ERR_NONE
    error_detail: str = ""
    http_status: int = 0
    e2e_s: float = float("nan")
    # ---- 输出块口径（见文件头"输出块 ≠ token"一节）----
    n_content_chunks: int = 0
    # ↑ **内容块数**（文本非空的块）。**不是 token 数**：中文/emoji 下服务端会发"有 token
    #   但文本为空"的块，这类块这里数不到。保留旧名是因为已有产物与脚本按它取列。
    n_output_chunks: int = 0         # **输出块数**（带 choices 的块，含文本为空的块）
    n_empty_text_chunks: int = 0     # 其中文本为空的块数（"有 token 但文本为空"那类）
    n_stream_bytes: int = 0
    saw_done: int = 0                # 流里出现过 data: [DONE]
    saw_finish_reason: int = 0       # 流里出现过带 finish_reason 的片段
    ttft_s: float = float("nan")                # 请求开始 → 第一个内容片段（主口径）
    ttft_from_arrival_s: float = float("nan")   # 计划到达 → 第一个内容片段（含客户端自身延迟）
    # ---- 历史命名（**不得当作"逐 token 间隔"**）----
    # 按"文本非空的**内容块**"间隔算，保留它是为了复核旧产物里同名列的值。
    # 块口径的正确字段是下面的 chunk_interval_*；两者只在"存在空文本块"时不同。
    tpot_p50_s: float = float("nan")
    tpot_p95_s: float = float("nan")
    tpot_p99_s: float = float("nan")
    tpot_mean_s: float = float("nan")
    tpot_max_s: float = float("nan")
    # ---- 输出块间隔（块口径的正确名字；含文本为空的块）----
    chunk_interval_p50_s: float = float("nan")
    chunk_interval_p95_s: float = float("nan")
    chunk_interval_p99_s: float = float("nan")
    chunk_interval_mean_s: float = float("nan")
    chunk_interval_max_s: float = float("nan")
    # ---- 服务端自报的 token 数（**拿不到就留空，绝不拿块数冒充**）----
    usage_report_seen: int = 0                  # 流里出现过带 usage 的片段
    srv_completion_tokens: float = float("nan")  # usage.completion_tokens
    srv_token_ids_total: float = float("nan")    # 逐块 token_ids 长度之和（vLLM 扩展）
    srv_token_count_source: str = ""             # "usage" / "token_ids" / ""（都拿不到）
    token_interval_mean_s: float = float("nan")  # 逐 token 平均间隔（代理量，见文件头）
    # --- 客户端可观测分解的覆盖结论 ---
    cov_gap_total_s: float = float("nan")
    cov_max_gap_s: float = float("nan")
    cov_loo_min_gap_s: float = float("nan")
    cov_loo_stage: str = ""
    cov_overlap_s: float = float("nan")
    cov_verdict: str = "N-A"
    # --- 服务端语义阶段 ---
    stage_report_seen: int = 0
    srv_span_s: float = float("nan")
    srv_stage_sum_s: float = float("nan")
    srv_gap_max_s: float = float("nan")
    srv_overlap_s: float = float("nan")
    srv_loo_min_gap_s: float = float("nan")
    srv_outside_s: float = float("nan")
    srv_verdict: str = "N-A"
    sum_minus_e2e_s: float = float("nan")       # Σ语义阶段 − 端到端（**不参与判定**）
    sum_minus_srv_span_s: float = float("nan")  # Σ语义阶段 − 服务端窗口（干净的对照量）
    kv_bytes: float = float("nan")
    kv_mbps: float = float("nan")


REQ_COLS = list(ReqResult().__dataclass_fields__.keys())


class _Stop(Exception):
    """内部控制流：HTTP 状态码已经决定了这是失败请求，不必再读流。"""


def _extract_text(obj: dict) -> str:
    """从一块 SSE JSON 里取出内容文本。兼容 completions 与 chat 两种形状。"""
    ch = obj.get("choices")
    if not isinstance(ch, list) or not ch:
        return ""
    c0 = ch[0] or {}
    v = c0.get("text")
    if isinstance(v, str):
        return v
    d = c0.get("delta")
    if isinstance(d, dict):
        v = d.get("content")
        if isinstance(v, str):
            return v
    return ""


def _extract_finish_reason(obj: dict) -> str:
    """取出这一块的 finish_reason（没有就返回空串）。

    为什么需要它：判断"流是不是完整结束"不能只看有没有 `[DONE]`。有些 OpenAI 兼容
    实现在截断时**语法上收尾得很规矩**（分块终止符照写），只是少了结尾那一片。
    所以完整性要用两个独立信号（`[DONE]` 与 `finish_reason`）一起判，缺一即视为不完整。
    """
    ch = obj.get("choices")
    if not isinstance(ch, list) or not ch:
        return ""
    c0 = ch[0] or {}
    fr = c0.get("finish_reason")
    return fr if isinstance(fr, str) else ""


def classify_event(obj) -> tuple[str, str, int, str, dict | None]:
    """把一条 SSE JSON 归成 `(kind, text, n_token_ids, finish_reason, usage)`。

    kind 的三种取值是本文件"输出块 ≠ token"口径的落点，**必须区分干净**：
      · `"output"`     —— 带一个 choice 的块，也就是**一次生成步骤**。它的 `text` 可能是空串
                          （中文/emoji 实测会出现），那种块**仍然是输出块**，不能当收尾片丢掉
                          ——旧口径就是这样把"块数"数小的。
      · `"usage_only"` —— 只带 usage、`choices` 为空的收尾块（`stream_options.include_usage`
                          打开时服务端会发）。它是 SSE 片段但**不是输出块**：算进块数就会凭空多 1。
      · `"other"`      —— 两者都不是（例如假后端挂在末尾的 `x_stage_report` 片）。

    `n_token_ids` 只在服务端逐块自报 token 时非零（vLLM 的 `return_token_ids`）；
    拿不到就是 0，**不能用块数去顶替**。
    """
    if not isinstance(obj, dict):
        return ("other", "", 0, "", None)
    usage = obj.get("usage")
    usage = usage if isinstance(usage, dict) else None
    choices = obj.get("choices")
    if not isinstance(choices, list) or not choices:
        return ("usage_only" if usage is not None else "other", "", 0, "", usage)
    c0 = choices[0] if isinstance(choices[0], dict) else {}
    tid = c0.get("token_ids")
    n_tid = len(tid) if isinstance(tid, list) else 0
    return ("output", _extract_text(obj), n_tid, _extract_finish_reason(obj), usage)


@dataclass
class MeasureCfg:
    """一批请求共用的固定参数。收成一个对象，是因为要在每个工作线程里传同一份配置，
    六个散参数很容易传错位（错位后不报错，只是量出来的东西不对）。"""
    url: str
    prompt_tokens: int
    max_tokens: int
    timeout_s: float
    cov_tol_s: float
    batch_t0: float
    prompt_text: str = ""     # 非空则用它当提示词（测中文/emoji 时用；空则沿用 "x "×prompt_tokens）
    model: str = "fake-model"
    keep_tokens: bool = False
    # 两个"取证开关"：默认都开。关掉任意一个只影响**服务端 token 数**能不能取到，
    # 不影响块口径的任何字段——所以它们是可选的，缺了不会让度量失真。
    include_usage: bool = True      # stream_options.include_usage → usage.completion_tokens
    return_token_ids: bool = True   # return_token_ids（vLLM 扩展）→ 逐块 token_ids


def measure_one(cfg: MeasureCfg, req_id: str, arrival_epoch: float
                ) -> tuple[ReqResult, list[tuple[int, float, float, bool]]]:
    """测一个请求。返回 (结果, [(输出块序号, 绝对时刻, 与上一个输出块的间隔, 文本是否为空)])。

    为什么必须**流式**并**逐片打时间戳**：TTFT 与输出块间隔是阶段 B 的头两个主指标，
    只能从"每个片段到达的时刻"算出来。等整个响应收完再算，就只剩端到端一个数了
    ——这正是阶段 A 那个 worker 做不到的地方。

    注意返回的是**输出块**（含文本为空的块），不是 token：一个块可能装多个 token，
    也可能一个 token 的文本要跨两块才凑齐。token 口径的量只在服务端自报 token 数时才有（见文件头）。
    """
    u = urlparse(cfg.url)
    host = u.hostname or "127.0.0.1"
    port = u.port or (443 if u.scheme == "https" else 80)
    path = (u.path.rstrip("/") or "") + "/v1/completions"

    r = ReqResult(req_id=req_id)
    tok_rows: list[tuple[int, float, float, bool]] = []
    t_start = time.time()
    r.start_s = t_start - cfg.batch_t0
    t_send = t_first_byte = t_first_content = t_last_output = 0.0
    n_bytes = 0
    report: dict | None = None
    # 块口径的原始观测：**输出块**时刻（含空文本块）、空文本块数、服务端自报的 token 数。
    # 收成一个 dict 传下去，是为了不再往 analyze_samples 的签名上继续加散参数。
    obs: dict = {"output_ts": [], "n_empty_text": 0, "usage_completion_tokens": None,
                 "token_ids_total": 0, "token_ids_seen": 0}
    # `t_end` 用 None 表示"流没有正常读完"。**不能用 0.0 当哨兵**：0.0 会被
    # `t_end - t_start` 算成一个大负数，还会让后续分析在失败路径上拿到无意义的窗口。
    t_end: float | None = None

    conn = None
    try:
        conn = http.client.HTTPConnection(host, port, timeout=cfg.timeout_s)
        prompt = cfg.prompt_text if cfg.prompt_text else "x " * max(1, cfg.prompt_tokens)
        body_obj: dict = {
            "model": cfg.model, "prompt": prompt,
            "max_tokens": cfg.max_tokens, "stream": True, "temperature": 0.0,
            "x_req_id": req_id, "_hang_s": cfg.timeout_s * 4,
        }
        # 两个取证开关：都按"服务端支持就回、不支持就忽略"来设计——它们**只增加信息**，
        # 不改变请求语义。真 vLLM 对多余字段是 `extra=allow`（B0_VLLM_REPORT §2.3 第 10 条实测），
        # 但其它 OpenAI 兼容实现未必如此，所以留了开关。
        if cfg.include_usage:
            body_obj["stream_options"] = {"include_usage": True}
        if cfg.return_token_ids:
            body_obj["return_token_ids"] = True
        body = json.dumps(body_obj).encode()
        conn.putrequest("POST", path)
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Accept", "text/event-stream")
        conn.putheader("X-Request-Id", req_id)
        conn.putheader("Content-Length", str(len(body)))
        conn.endheaders()
        conn.send(body)
        t_send = time.time()

        resp = conn.getresponse()
        t_first_byte = time.time()   # 响应头到手 ≈ 首字节（http.client 不暴露更细的边界）
        r.http_status = resp.status
        if resp.status >= 500:
            r.error_class = ERR_HTTP_5XX
            r.error_detail = f"HTTP {resp.status}"
            resp.read()
            raise _Stop()
        if resp.status >= 400:
            r.error_class = ERR_HTTP_4XX
            r.error_detail = f"HTTP {resp.status}"
            resp.read()
            raise _Stop()

        while True:
            line = resp.readline()
            if not line:
                break
            n_bytes += len(line)
            s = line.strip()
            if not s.startswith(b"data:"):
                continue
            payload = s[5:].strip()
            if payload == b"[DONE]":
                r.saw_done = 1
                break
            t_now = time.time()
            try:
                obj = json.loads(payload.decode("utf-8"))
            except Exception:
                r.error_class = ERR_BAD_JSON
                continue
            if isinstance(obj, dict) and obj.get("x_stage_report"):
                # 服务端自报的阶段报告：存下来。**必须先取文本、再处理它**——假后端把
                # 报告挂在最后一个内容块上，若这里直接 continue，那个 token 就被静默
                # 漏掉（本文件第一版踩过：32 个 token 只数到 31 个，TTFT 却照常出数）。
                report = obj["x_stage_report"]
                r.stage_report_seen = 1
            kind, text, n_tid, finish_reason, usage = classify_event(obj)
            if usage is not None:
                r.usage_report_seen = 1
                ct = usage.get("completion_tokens")
                if isinstance(ct, (int, float)) and not isinstance(ct, bool):
                    obs["usage_completion_tokens"] = float(ct)
            if kind != "output":
                # 只带 usage 的收尾片 / x_stage_report 片：是 SSE 片段，**不是输出块**。
                continue
            if finish_reason:
                r.saw_finish_reason = 1
            if n_tid:
                obs["token_ids_seen"] += 1
                obs["token_ids_total"] += n_tid
            # ---- 每个**输出块**都要记时刻、记进 tok_rows；文本为空的块也要记 ----
            # （这才是块口径。旧口径只记文本非空的块，于是"块序列"里少了那些块。）
            obs["output_ts"].append(t_now)
            t_last_output = t_now
            text_empty = not text
            if text_empty:
                obs["n_empty_text"] += 1
            elif t_first_content == 0.0:
                t_first_content = t_now
            gap_s = 0.0 if not tok_rows else t_now - tok_rows[-1][1]
            tok_rows.append((len(tok_rows), t_now, gap_s, text_empty))

        # 把剩下的读完再关连接：否则半关的连接会让服务端下一次写踩 BrokenPipe
        try:
            resp.read()
        except Exception:
            pass
        t_end = time.time()
    except _Stop:
        r.e2e_s = time.time() - t_start
        r.ok = 0
    except socket.timeout:
        r.error_class = r.error_class or ERR_TIMEOUT
        r.error_detail = f"socket timeout after {cfg.timeout_s}s"
        r.ok = 0
    except ConnectionRefusedError as e:
        # 只有"连都没连上"才是 connect_error。
        r.error_class = r.error_class or ERR_CONNECT
        r.error_detail = repr(e)
        r.ok = 0
    except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError) as e:
        # **已经拿到响应（http_status != 0）之后再被 RST ⇒ 是断流，不是连不上。**
        # 这条区分很重要：阶段 C 里"连不上"要查隧道/安全组，"断流"要查中间层与对端收尾。
        r.error_class = r.error_class or (ERR_STREAM_BROKEN if r.http_status
                                          else ERR_CONNECT)
        r.error_detail = repr(e)
        r.ok = 0
    except (http.client.HTTPException, http.client.IncompleteRead) as e:
        r.error_class = r.error_class or ERR_STREAM_BROKEN
        r.error_detail = repr(e)
        r.ok = 0
    except OSError as e:
        r.error_class = r.error_class or (ERR_STREAM_BROKEN if r.http_status
                                          else ERR_CONNECT)
        r.error_detail = repr(e)
        r.ok = 0
    except Exception as e:  # noqa: BLE001 —— 兜底，但必须留原文，绝不静默吞掉
        r.error_class = r.error_class or ERR_STREAM_BROKEN
        r.error_detail = f"{type(e).__name__}: {e}"
        r.ok = 0
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass

    # ---- 采样完成之后的分析：**单独包一层 try**，且只在"流真的读完"时才做 ----
    # 为什么单独包：分析代码里的 bug（例如把三元组当二元组解包）若被最外层的网络异常
    # 兜底抓住，会被记成 `stream_broken`，看起来像"服务端中途断流"——一个自己造出来的
    # 假故障。本文件第一版就发生过一次（120/120 全被误判成断流）。分析错归 internal_error。
    # 为什么只在 t_end 非 None 时做：超时/连不上/HTTP 错误这些路径上根本没有完整的请求窗口，
    # 硬去做覆盖分析只会用一个未定义的窗口算出荒谬结果（本文件第二版踩过：失败请求全被记成
    # `UnboundLocalError`）。**失败请求就照实记失败，不硬凑分析。**
    if t_end is not None:
        try:
            analyze_samples(r, tok_rows, n_bytes, t_start, t_end, t_send, t_first_byte,
                            t_first_content, t_last_output, arrival_epoch, cfg, report, obs)
        except Exception as e:  # noqa: BLE001
            r.error_class = ERR_INTERNAL
            r.error_detail = f"{type(e).__name__}: {e}"
            r.ok = 0

    if not cfg.keep_tokens:
        tok_rows = []
    return r, tok_rows


def analyze_samples(r: ReqResult, tok_rows: list[tuple[int, float, float, bool]], n_bytes: int,
                    t_start: float, t_end: float, t_send: float, t_first_byte: float,
                    t_first_content: float, t_last_output: float, arrival_epoch: float,
                    cfg: MeasureCfg, report: dict | None, obs: dict) -> None:
    """把采样结果换算成落盘字段（TTFT / 两套间隔口径 / 两套覆盖判据）。

    单独抽成函数的理由见调用点：分析错与网络错必须能被分开归因。
    `tok_rows` 里**每个输出块一行**（含文本为空的块），`obs` 带服务端自报的 token 数。
    """
    r.n_content_chunks = sum(1 for _, _, _, empty in tok_rows if not empty)
    r.n_output_chunks = len(tok_rows)
    r.n_empty_text_chunks = int(obs.get("n_empty_text", 0))
    r.n_stream_bytes = n_bytes
    r.e2e_s = t_end - t_start
    # ---- 服务端自报的 token 数：**有就用，没有就留空**（绝不拿块数顶替）----
    n_srv_tokens: float | None = None
    if obs.get("usage_completion_tokens") is not None:
        r.srv_completion_tokens = float(obs["usage_completion_tokens"])
    if obs.get("token_ids_seen"):
        r.srv_token_ids_total = float(obs["token_ids_total"])
    if math.isfinite(r.srv_completion_tokens):
        n_srv_tokens, r.srv_token_count_source = r.srv_completion_tokens, "usage"
    elif math.isfinite(r.srv_token_ids_total):
        n_srv_tokens, r.srv_token_count_source = r.srv_token_ids_total, "token_ids"
    if t_first_content == 0.0:
        r.error_class = r.error_class or ERR_NO_CONTENT
        r.ok = 0
    elif not (r.saw_done or r.saw_finish_reason):
        # 流"读完了"，但既没有 [DONE] 也没有 finish_reason ⇒ 内容不完整。
        # 这一条**不能靠异常发现**：截断的连接可能收尾得很规矩（分块终止符照写），
        # 只有从语义上核对终止信号才抓得住。假后端的 truncate_clean 模式就是造这个情形。
        r.error_class = r.error_class or ERR_STREAM_INCOMPLETE
        r.ok = 0
    else:
        r.ok = 1
        r.ttft_s = t_first_content - t_start
        r.ttft_from_arrival_s = t_first_content - arrival_epoch
        # ---- 两套间隔口径，**从两个不同的时刻序列算**，不共用一份数据 ----
        # ① 块口径（正确名字）：全部输出块，含文本为空的那些。
        (r.chunk_interval_p50_s, r.chunk_interval_p95_s, r.chunk_interval_p99_s,
         r.chunk_interval_mean_s, r.chunk_interval_max_s) = interval_stats(
             list(obs.get("output_ts") or []))
        # ② 历史命名 tpot_*：只取文本非空的**内容块**，与旧产物的同名列逐位可比。
        #    没有空文本块时两者必然相同（这一条在 selftest 里钉住）。
        (r.tpot_p50_s, r.tpot_p95_s, r.tpot_p99_s, r.tpot_mean_s,
         r.tpot_max_s) = interval_stats([t for _, t, _, empty in tok_rows if not empty])
        # ---- 逐 token 平均间隔（代理量）：只在拿到服务端 token 数且 >1 时有值 ----
        # 它是"解码窗口跨度 ÷ (token 数 − 1)"，**不是**逐 token 时间戳的分位数；
        # 服务端只给总数、不给每个 token 的时刻，所以这里只能给平均。
        if (n_srv_tokens is not None and n_srv_tokens >= 2
                and math.isfinite(t_last_output) and t_last_output > t_first_content):
            r.token_interval_mean_s = ((t_last_output - t_first_content)
                                       / (n_srv_tokens - 1.0))

    # ---- 集合①：客户端可观测分解（应当铺满 [t_start, t_end]）----
    # `decode_stream` 的右端用**最后一个输出块**（不是最后一个内容块）：空文本块也属于
    # 解码期，落在它之后的只剩收尾。两者只差零点几毫秒，但口径上应当以"块"为准。
    client_iv = [
        ("send", t_start, t_send),
        ("wait_first_byte", t_send, t_first_byte),
        ("to_first_content", t_first_byte, t_first_content or t_first_byte),
        ("decode_stream", t_first_content or t_first_byte,
         t_last_output or t_first_content or t_first_byte),
        ("teardown", t_last_output or t_first_content or t_first_byte, t_end),
    ]
    cov = check_coverage(client_iv, t_start, t_end, cfg.cov_tol_s, CLIENT_STAGES)
    r.cov_gap_total_s = cov.gap_total_s
    r.cov_max_gap_s = cov.max_gap_s
    r.cov_loo_min_gap_s = cov.loo_min_gap_s
    r.cov_loo_stage = cov.loo_stage
    r.cov_overlap_s = cov.overlap_s
    r.cov_verdict = cov.verdict

    # ---- 集合②：服务端语义阶段（只铺满服务端自己的窗口，且可互相重叠）----
    if not report:
        return
    srv_iv = [(str(s["name"]), float(s["start"]), float(s["end"]))
              for s in report.get("stages", [])]
    srv_iv2 = [(a, b) for _, a, b in srv_iv]   # 区间代数只吃二元组
    srv_lo = float(report["server_req_start"])
    srv_hi = float(report["server_req_end"])
    srv = check_coverage(srv_iv, srv_lo, srv_hi, cfg.cov_tol_s, SERVER_STAGES)
    r.srv_span_s = srv.span_s
    r.srv_stage_sum_s = srv.sum_stage_s
    r.srv_gap_max_s = srv.max_gap_s
    r.srv_overlap_s = srv.overlap_s
    r.srv_loo_min_gap_s = srv.loo_min_gap_s
    r.srv_verdict = srv.verdict
    r.sum_minus_srv_span_s = srv.sum_stage_s - srv.span_s
    if math.isfinite(r.e2e_s):
        r.sum_minus_e2e_s = srv.sum_stage_s - r.e2e_s
    r.kv_bytes = float(report.get("kv_bytes") or float("nan"))
    r.kv_mbps = float(report.get("kv_mbps") or float("nan"))
    # 服务端区间伸出客户端窗口多少：**时钟对齐的体检量**。
    # 同机时它应当只有零点几毫秒（＝网络往返 + 两侧处理头尾）；跨机时它会随时钟误差变大
    # ——这正是计划书质量门槛里"机器时钟误差要小于最小档位 1/10"那条要防的东西。
    r.srv_outside_s = union_length(srv_iv2) - union_length(clamp(srv_iv2, t_start, t_end))
    return r, tok_rows


def run_client(args, run_id: str | None = None) -> str:
    """跑一批请求并落盘。返回 run_id（`battery` 用它去读回落盘产物做判定）。"""
    run_id = run_id or _new_run_id("serving")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    results: list[ReqResult] = []
    tok_all: list[tuple[str, int, float, float, bool]] = []
    lock = threading.Lock()
    inflight = [0]
    inflight_peak = [0]
    t0 = time.time()
    threads: list[threading.Thread] = []
    rng = random.Random(args.seed)
    # ---- 可选的 /metrics 汇总快照采集（**不配 --metrics-url 时 sampler 恒为 None**）----
    # 用 getattr 取新参数：别的脚本（如 probe_stage_durations.py）会自己拼 Namespace
    # 来调 run_client，缺键不该让它崩——新参数一律"缺键即默认值"。
    metrics_url = getattr(args, "metrics_url", "") or ""
    metrics_interval_s = float(getattr(args, "metrics_interval_s",
                                       DEFAULT_METRICS_INTERVAL_S))
    metrics_timeout_s = float(getattr(args, "metrics_timeout_s",
                                      DEFAULT_METRICS_TIMEOUT_S))
    version_url = getattr(args, "version_url", "") or ""
    sampler: MetricsSampler | None = None
    if metrics_url:
        sampler = MetricsSampler(metrics_url=metrics_url, interval_s=metrics_interval_s,
                                 timeout_s=metrics_timeout_s, version_url=version_url,
                                 out_dir=out_dir, run_id=run_id, t0=t0)
        print(f"[metrics] 采集已开启：{metrics_url}  间隔 {metrics_interval_s} s  "
              f"超时 {metrics_timeout_s} s  版本地址 {sampler.version_url or '(无)'}")
        sampler.start()
    cfg = MeasureCfg(url=args.url, prompt_tokens=args.prompt_tokens,
                     max_tokens=args.max_tokens, timeout_s=args.request_timeout,
                     cov_tol_s=args.cov_tol_s, batch_t0=t0,
                     prompt_text=args.prompt_text,
                     model=args.model,
                     keep_tokens=args.save_tokens,
                     include_usage=args.include_usage,
                     return_token_ids=args.return_token_ids)

    def worker(idx: int, arrival_epoch: float):
        with lock:
            inflight[0] += 1
            cur = inflight[0]
            if cur > inflight_peak[0]:
                inflight_peak[0] = cur
        r, toks = measure_one(cfg, f"r{idx:05d}", arrival_epoch)
        with lock:
            inflight[0] -= 1
            r.run_id = run_id
            r.label = args.label
            r.scheduled_arrival_s = arrival_epoch - t0
            r.inflight_at_submit = cur
            # 开环：超了**不阻塞**，只标记。阻塞会退化成"client 自己限流"，
            # 那样测量的对象（系统在压力下的表现）就消失了（阶段 A 踩过一次，见 README §7 第 9 条）。
            r.overloaded = 1 if cur > args.concurrency else 0
            results.append(r)
            for ti, tt, gap, empty in toks:
                tok_all.append((r.req_id, ti, tt - t0, gap, empty))

    # 到达时刻：按计划生成。**先算好整个时刻表**，与运行结果无关，
    # 这样即使系统被压住，到达过程也不会被"自我限流"改变。
    arrivals: list[float] = []
    acc = 0.0
    for i in range(args.requests):
        if i == 0:
            acc = 0.0
        elif args.arrival == "poisson":
            acc += rng.expovariate(args.qps)
        else:
            acc += 1.0 / args.qps
        arrivals.append(t0 + acc)

    for i, arrival_epoch in enumerate(arrivals):
        now = time.time()
        if arrival_epoch > now:
            time.sleep(arrival_epoch - now)
        # 每个请求一个线程：只有这样才能"不等上一个完成"。
        th = threading.Thread(target=worker, args=(i, arrival_epoch), daemon=True)
        th.start()
        threads.append(th)

    for th in threads:
        th.join(timeout=args.request_timeout * 4 + 30)

    elapsed = time.time() - t0
    results.sort(key=lambda r: r.req_id)

    # 停机采样（收尾那一次在 stop 里）——**必须在写 summary/manifest 之前**，它们要用结果
    metrics_snapshot: dict
    if sampler is not None:
        sampler.stop()
        metrics_snapshot = build_metrics_summary(sampler, metrics_url, metrics_interval_s,
                                                metrics_timeout_s, version_url)
    else:
        metrics_snapshot = build_metrics_summary(None, "", metrics_interval_s,
                                                metrics_timeout_s, version_url)

    # ---------- 落盘（文件名带唯一 run_id，append-only） ----------
    req_csv = out_dir / f"{run_id}_requests.csv"
    with req_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=REQ_COLS)
        w.writeheader()
        for r in results:
            w.writerow({k: ("" if isinstance(v, float) and not math.isfinite(v) else v)
                        for k, v in asdict(r).items()})

    tok_csv = None
    if args.save_tokens and tok_all:
        tok_csv = out_dir / f"{run_id}_tokens.csv"
        with tok_csv.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            # 列名与旧产物兼容（`token_index` / `t_rel_s` / `gap_s` 原样保留），但**语义是
            # "输出块"不是 token**：这里是每**输出块**一行，`text_empty=1` 的行就是
            # "有 token 但文本为空"那类块。旧产物读这个文件时按块理解同样成立。
            w.writerow(["run_id", "req_id", "token_index", "t_rel_s", "gap_s", "text_empty"])
            for rid, ti, tt, gap, empty in tok_all:
                w.writerow([run_id, rid, ti, f"{tt:.6f}", f"{gap:.6f}", int(empty)])

    summary = summarize_batch(results, elapsed, inflight_peak[0], args)
    summary.update({"run_id": run_id, "requests_csv": str(req_csv),
                    "tokens_csv": str(tok_csv) if tok_csv else None,
                    "metrics_snapshot": metrics_snapshot})
    (out_dir / f"{run_id}_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    manifest = {
        "run_id": run_id,
        "utc": datetime.now(timezone.utc).isoformat(),
        "kind": "measure_serving/client",
        "host": host_info(),
        **code_version(),
        "url": args.url,
        "label": args.label,
        "params": {
            "requests": args.requests, "qps": args.qps, "arrival": args.arrival,
            "concurrency": args.concurrency, "prompt_tokens": args.prompt_tokens,
            "prompt_text": args.prompt_text,
            "max_tokens": args.max_tokens, "request_timeout_s": args.request_timeout,
            "cov_tol_s": args.cov_tol_s, "save_tokens": args.save_tokens,
            "seed": args.seed, "loo_eps_s": LOO_EPS_S,
            # 两个取证开关：关掉不影响块口径，只是拿不到服务端 token 数。
            "include_usage": args.include_usage,
            "return_token_ids": args.return_token_ids,
        },
        "n_ok": summary["n_ok"], "n_err": summary["n_err"],
        "error_counts": summary["error_counts"],
        "coverage_verdicts": summary["coverage_verdicts"],
        "wall_clock_s": round(elapsed, 3),
        # ---- 可选的 /metrics 汇总快照（未配置时 state=未配置地址，形状不变）----
        "metrics": {
            "enabled": bool(metrics_url),
            "state": metrics_snapshot["state"],
            "state_zh": metrics_snapshot["state_zh"],
            "url": metrics_snapshot["url"],
            "version_url": metrics_snapshot["version_url"],
            "interval_s": metrics_snapshot["interval_s"],
            "timeout_s": metrics_snapshot["timeout_s"],
            "n_attempts": metrics_snapshot["n_attempts"],
            "n_ok": metrics_snapshot["n_ok"],
            "n_failed": metrics_snapshot["n_failed"],
            "nixl_presence_zh": metrics_snapshot["nixl_presence_zh"],
            "nixl_note": metrics_snapshot["nixl_note"],
            "target_version": metrics_snapshot["target_version"],
            "version_source": metrics_snapshot["version_source"],
            "version_error": metrics_snapshot["version_error"],
            "scrapes_csv": metrics_snapshot["scrapes_csv"],
            "first_snapshot": metrics_snapshot["first_snapshot"],
            "last_snapshot": metrics_snapshot["last_snapshot"],
            "scope_note": metrics_snapshot["scope_note"],
        },
        "what_this_does_not_prove": [
            "本工具量的是**某一次运行**的逐请求时延，不评模型质量，也不含任何性能收益结论。",
            "若后端是假后端，则各阶段时刻与 KV 字节数都是伪造的，只证明测量链路可用。",
            "覆盖判据证明的是'时间被完整归属'，**不是**'各阶段时长量得准'。",
            "`chunk_interval_*` 是**输出块**间隔；`tpot_*` 是历史命名（按内容块算），"
            "**两者都不是逐 token 间隔**（一个块可能装多个 token）。逐 token 的量只有"
            "`token_interval_mean_s`（代理量，且要求服务端自报 token 数）。",
            "`n_content_chunks` 是**内容块数**（文本非空），不是 token 数；"
            "`srv_completion_tokens` 才是服务端自报的 token 数，拿不到时该列为空。",
            "`metrics` 里记的是**服务端自报的汇总快照**（如果配了 --metrics-url）。它只用于"
            "比较整次运行前后的汇总变化，**不能分摊给单个请求**——理由见 metrics.scope_note。"
            "另外：'成功抓到指标但没有 NIXL 项'记作**未出现**，不是 0；"
            "抓取失败也**不等于**没有 NIXL 项（那是两回事，状态字段分得清）。",
        ],
    }
    (out_dir / f"{run_id}_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print_summary(summary, run_id, out_dir)
    return run_id


def _count_str(vals: list[str]) -> dict[str, int]:
    """把一串字符串按出现次数汇总（空串记成 "unavailable"，免得看起来像键名丢了）。"""
    out: dict[str, int] = {}
    for v in vals:
        k = v or "unavailable"
        out[k] = out.get(k, 0) + 1
    return out


def summarize_batch(results: list[ReqResult], elapsed: float, inflight_peak: int,
                    args) -> dict:
    ok = [r for r in results if r.ok]
    err = [r for r in results if not r.ok]
    err_counts: dict[str, int] = {}
    for r in err:
        k = r.error_class or ERR_NONE
        err_counts[k] = err_counts.get(k, 0) + 1
    verdicts: dict[str, int] = {}
    for r in results:
        verdicts[r.cov_verdict] = verdicts.get(r.cov_verdict, 0) + 1
    srv_verdicts: dict[str, int] = {}
    for r in ok:
        if r.stage_report_seen:
            srv_verdicts[r.srv_verdict] = srv_verdicts.get(r.srv_verdict, 0) + 1

    def q(name: str, p: float) -> float | None:
        vals = [getattr(r, name) for r in ok]
        vals = [x for x in vals
                if isinstance(x, (int, float)) and not isinstance(x, bool)
                and math.isfinite(x)]
        if not vals:
            return None
        v = percentile(vals, p)
        return None if not math.isfinite(v) else round(v, 6)

    def med(name: str, only_srv: bool = False) -> float | None:
        pool = [r for r in ok if (r.stage_report_seen or not only_srv)]
        vals = [getattr(r, name) for r in pool]
        vals = [x for x in vals
                if isinstance(x, (int, float)) and not isinstance(x, bool)
                and math.isfinite(x)]
        return round(statistics.median(vals), 6) if vals else None

    def mx(name: str) -> float | None:
        vals = [getattr(r, name) for r in ok]
        vals = [x for x in vals
                if isinstance(x, (int, float)) and not isinstance(x, bool)
                and math.isfinite(x)]
        return round(max(vals), 6) if vals else None

    cover = [r for r in ok if r.stage_report_seen]
    return {
        "n_requests": len(results),
        "n_ok": len(ok),
        "n_err": len(err),
        "error_counts": err_counts,
        "wall_clock_s": round(elapsed, 3),
        "throughput_qps": round(len(results) / elapsed, 3) if elapsed > 0 else None,
        "inflight_peak": inflight_peak,
        "latency": {
            "ttft_s": {p: q("ttft_s", vv) for p, vv in
                       (("p50", 0.5), ("p95", 0.95), ("p99", 0.99), ("max", 1.0))},
            "e2e_s": {p: q("e2e_s", vv) for p, vv in
                      (("p50", 0.5), ("p95", 0.95), ("p99", 0.99), ("max", 1.0))},
            # 块口径（正确名字，含空文本块）
            "chunk_interval_mean_s": {"p50": q("chunk_interval_mean_s", 0.5),
                                      "p95": q("chunk_interval_mean_s", 0.95)},
            "chunk_interval_p95_s": {"p50": q("chunk_interval_p95_s", 0.5),
                                     "p95": q("chunk_interval_p95_s", 0.95)},
            # 历史命名（按内容块算，保留供旧产物对照）
            "tpot_p50_s": {"p50": q("tpot_p50_s", 0.5), "p95": q("tpot_p50_s", 0.95)},
            "tpot_p95_s": {"p50": q("tpot_p95_s", 0.5), "p95": q("tpot_p95_s", 0.95)},
            # 单请求内的**均值**间隔。要跟 chunk_interval_mean_s 并排看，只能用同为均值的量
            # （拿 tpot_p50_s 去比 chunk_interval_mean_s 是在比中位数与均值，口径不齐）。
            "tpot_mean_s": {"p50": q("tpot_mean_s", 0.5), "p95": q("tpot_mean_s", 0.95)},
        },
        # ---- 块口径与 token 口径的分别汇总（**两组数不混用**）----
        "chunks": {
            "n_output_chunks_median": med("n_output_chunks"),
            "n_content_chunks_median": med("n_content_chunks"),
            "n_empty_text_chunks_median": med("n_empty_text_chunks"),
            "n_empty_text_chunks_total": sum(r.n_empty_text_chunks for r in ok),
            "n_requests_with_empty_text_chunks": sum(1 for r in ok
                                                     if r.n_empty_text_chunks > 0),
        },
        "server_tokens": {
            "n_requests_with_usage": sum(1 for r in ok if r.usage_report_seen),
            "n_requests_with_token_ids": sum(1 for r in ok
                                             if math.isfinite(r.srv_token_ids_total)),
            "n_requests_without_token_count": sum(
                1 for r in ok if not r.srv_token_count_source),
            "srv_completion_tokens_median": med("srv_completion_tokens"),
            "srv_token_ids_total_median": med("srv_token_ids_total"),
            "srv_token_count_source_counts": _count_str([r.srv_token_count_source for r in ok]),
            "token_interval_mean_s_median": med("token_interval_mean_s"),
        },
        "coverage_verdicts": verdicts,
        "srv_verdicts": srv_verdicts,
        "coverage": {
            "n_with_server_report": len(cover),
            "cov_max_gap_s_max": mx("cov_max_gap_s"),
            "cov_gap_total_s_max": mx("cov_gap_total_s"),
            "cov_loo_min_gap_s_min": (min((r.cov_loo_min_gap_s for r in ok
                                           if math.isfinite(r.cov_loo_min_gap_s)),
                                          default=None) if ok else None),
            "srv_gap_max_s_max": mx("srv_gap_max_s"),
            "srv_overlap_s_median": med("srv_overlap_s", only_srv=True),
            "sum_minus_e2e_s_median": med("sum_minus_e2e_s", only_srv=True),
            "sum_minus_e2e_s_max": (round(max(
                r.sum_minus_e2e_s for r in cover
                if math.isfinite(r.sum_minus_e2e_s)), 6) if cover else None),
            "sum_minus_srv_span_s_median": med("sum_minus_srv_span_s", only_srv=True),
            "srv_outside_s_median": med("srv_outside_s", only_srv=True),
            "cov_tol_s": args.cov_tol_s,
            "loo_eps_s": LOO_EPS_S,
        },
        "kv_bytes_per_request": (round(statistics.fmean(
            [r.kv_bytes for r in cover
             if math.isfinite(r.kv_bytes)]), 1) if cover else None),
    }


def print_summary(s: dict, run_id: str, out_dir: Path) -> None:
    lat = s["latency"]
    print(f"\n=== {run_id} ===")
    print(f"请求 {s['n_requests']}  成功 {s['n_ok']}  失败 {s['n_err']}  "
          f"墙钟 {s['wall_clock_s']} s  吞吐 {s['throughput_qps']} req/s  "
          f"在途峰值 {s['inflight_peak']}")
    if s["n_err"]:
        print(f"错误分类：{json.dumps(s['error_counts'], ensure_ascii=False)}")

    def fmt(d):
        return "  ".join(f"{k}={v}" for k, v in d.items() if v is not None)

    print(f"TTFT(s)   {fmt(lat['ttft_s'])}")
    print(f"端到端(s) {fmt(lat['e2e_s'])}")
    ck = s["chunks"]
    print(f"输出块    每请求中位 {ck['n_output_chunks_median']} 块"
          f"（其中有文本 {ck['n_content_chunks_median']} 块、"
          f"文本为空 {ck['n_empty_text_chunks_median']} 块）；"
          f"空文本块合计 {ck['n_empty_text_chunks_total']} 个，"
          f"涉及 {ck['n_requests_with_empty_text_chunks']} 个请求")
    print(f"  块间隔(s)【正确名字，含空文本块】均值口径 {fmt(lat['chunk_interval_mean_s'])}"
          f"   p95 口径 {fmt(lat['chunk_interval_p95_s'])}")
    print(f"  tpot(s)  【历史命名，只按内容块算】均值口径 {fmt(lat['tpot_mean_s'])}"
          f"   p95 口径 {fmt(lat['tpot_p95_s'])}")
    st = s["server_tokens"]
    print(f"服务端 token 数  有 usage 的请求 {st['n_requests_with_usage']} 个，"
          f"有 token_ids 的请求 {st['n_requests_with_token_ids']} 个，"
          f"一个都拿不到的请求 {st['n_requests_without_token_count']} 个"
          f"  来源={json.dumps(st['srv_token_count_source_counts'], ensure_ascii=False)}")
    print(f"  srv_completion_tokens 中位 {st['srv_completion_tokens_median']}，"
          f"srv_token_ids_total 中位 {st['srv_token_ids_total_median']}，"
          f"逐 token 平均间隔中位 {st['token_interval_mean_s_median']} s"
          f"（**代理量**，不是逐 token 时间戳的分位数）")
    print(f"覆盖判定（客户端分解）{json.dumps(s['coverage_verdicts'], ensure_ascii=False)}")
    if s["srv_verdicts"]:
        print(f"覆盖判定（服务端阶段）{json.dumps(s['srv_verdicts'], ensure_ascii=False)}")
    c = s["coverage"]
    print(f"  空档：客户端单段最大 {c['cov_max_gap_s_max']} s，合计最大 {c['cov_gap_total_s_max']} s "
          f"(容差 {c['cov_tol_s']} s)")
    print(f"        leave-one-out 最小空档 {c['cov_loo_min_gap_s_min']} s "
          f"(> {c['loo_eps_s']} 才说明每段都真的被记了)")
    print(f"  重叠：服务端语义阶段重叠中位 {c['srv_overlap_s_median']} s")
    print(f"        (Σ语义阶段 − 端到端) 中位 {c['sum_minus_e2e_s_median']} s，"
          f"最大 {c['sum_minus_e2e_s_max']} s  ← **不参与判定**，有重叠时它本来就该 > 0")
    print(f"  时钟对齐体检：服务端区间伸出客户端窗口的中位 {c['srv_outside_s_median']} s")
    mss = s.get("metrics_snapshot")
    if mss:
        print(f"metrics 快照  {mss['state_zh']}")
        if mss["state"] == MET_NIXL_ABSENT:
            print(f"  NIXL 项：{NIXL_UNSEEN_TEXT} ← **不是「传输量为 0」**："
                  f"本次 /metrics 里没有 NIXL 相关序列（这是状态，不是数值）")
        elif mss["state"] == MET_NIXL_PRESENT:
            print(f"  NIXL 项：出现（{len(mss['nixl_series_values'] or {})} 条序列；"
                  f"数值是按传输汇总的，见快照 raw_body）")
        else:
            print(f"  NIXL 项：{mss['nixl_presence_zh']}（这一格**没有**数值可言）")
        print(f"  目标版本：{mss['target_version']}"
              + (f"（来源 {mss['version_source']}）" if mss["version_source"] else "")
              + (f"  取不到的原因：{mss['version_error']}" if mss["version_error"] else ""))
        print(f"  抓取 {mss['n_attempts']} 次（成功 {mss['n_ok']} / 失败 {mss['n_failed']}）"
              + (f"，首末快照 {mss['first_snapshot']['t_rel_s']} s → "
                 f"{mss['last_snapshot']['t_rel_s']} s"
                 if mss["first_snapshot"] and mss["last_snapshot"] else ""))
        print("  ⚠️ 快照只用于比较**整次运行前后**的汇总变化，**不能分摊给单个请求**")
    print(f"产物 -> {out_dir}")


# ===========================================================================
# 六、metrics 汇总快照采集（**可选**：不配 --metrics-url 时整条路径一行都不走）
# ===========================================================================
# 这一节只做一件事：**在一次运行期间按固定间隔抓 /metrics，把原始正文与带时间戳的元数据
# 落盘**，并让"抓没抓到、里面有没有 NIXL 项"这四个状态**分得清**。
#
# 为什么"缺项"必须与"数值为零"分开记（本文件最容易犯的错，也是最要紧的一条）：
#   vLLM 0.31.0 单实例、没开 KV 连接器时，`/metrics` 里**一条 NIXL 序列都没有**
#   （B0_VLLM_REPORT §3 实测：`nixl` 命中 0 行）。这是**没出现**，不是"传了 0 字节"。
#   若把它记成 0，读的人会以为"这个配置下确实传过数据、只是没有量"——那是凭空的结论。
#   反过来，当序列**存在**时，它的 0 就是真的 0（实测对照：假指标服务里
#   `vllm:nixl_num_failed_transfers 0.0` 表示"确实一次都没失败"）。两种 0 的含义天差地别。
SNAPSHOT_SCOPE_NOTE = (
    "这些快照只用于比较**整次运行前后**的汇总变化；**不能分摊给单个请求**："
    "(1) vLLM 的 NIXL 日志本身按一段时间汇总，官方指标按传输统计，都不带请求标识，"
    "无法自动匹配到本工具里的 req_id；"
    "(2) /metrics 是**进程级累计量**，从两个采样点之差里解不出'每个请求占多少'；"
    "(3) 抓取时刻与请求边界不对齐（一次抓取本身还占用一次 HTTP 往返）。"
    "要逐请求的传输量，得由服务端提供带请求标识的日志或事件——那是另一项工作（逐请求归因），"
    "不在本工具范围内。"
)

MET_NOT_CONFIGURED = "not_configured"   # 未配置地址：本次没有采集
MET_SCRAPE_FAILED = "scrape_failed"     # 抓取失败：配了地址但抓不到
MET_NIXL_ABSENT = "nixl_absent"         # 抓到了指标，但没有 NIXL 项 → 记"未出现"
MET_NIXL_PRESENT = "nixl_present"       # 抓到了 NIXL 项
METRICS_STATES = (MET_NOT_CONFIGURED, MET_SCRAPE_FAILED, MET_NIXL_ABSENT, MET_NIXL_PRESENT)
METRICS_STATE_ZH = {
    MET_NOT_CONFIGURED: "未配置地址（本次没有采集）",
    MET_SCRAPE_FAILED: "抓取失败（配了地址但抓不到）",
    MET_NIXL_ABSENT: "成功抓到指标但没有 NIXL 项（记作：未出现）",
    MET_NIXL_PRESENT: "抓到 NIXL 项",
}
# NIXL 的"存在性"用的是**字面上的词**，不是数字。缺项那一格的字面值就是 "未出现"。
NIXL_PRESENCE_ZH = {
    MET_NOT_CONFIGURED: "未配置地址",
    MET_SCRAPE_FAILED: "抓取失败",
    MET_NIXL_ABSENT: "未出现",
    MET_NIXL_PRESENT: "出现",
}
NIXL_UNSEEN_TEXT = "未出现"             # 缺项的字面值（**不是 0**）
VERSION_UNAVAILABLE = "未取到"           # 版本取不到时的字面值（**不编**）
DEFAULT_METRICS_INTERVAL_S = 5.0
DEFAULT_METRICS_TIMEOUT_S = 5.0
METRICS_DIFF_TOP_N = 10
# 假指标服务自报的版本串。**它是伪造的**（名字里就写着 fake），只用于验证
# "版本被如实记进元数据"这条链路；假服务正文里另有一条 python_info{version="3.12.10"}，
# 专门用来验证"解释器版本不会被当服务版本采用"。
FAKE_METRICS_VERSION = "fake-vllm-0.0.1-test"

# 每次抓取落盘的 CSV 列（顺序即列序）。`raw_body` 不进 CSV（它是快照 JSON 的字段），
# 其余字段与快照 JSON 同名同义，两处可以互相核对。
SCRAPE_COLS = [
    "run_id", "seq", "phase", "utc", "planned_t_rel_s", "t_rel_s", "scrape_wall_s",
    "url", "ok", "http_status", "error_class", "error_text",
    "state", "state_zh", "nixl_presence_zh", "nixl_note",
    "nixl_matched_series_count", "nixl_series_values", "nixl_help_or_type_mentions",
    "n_series", "n_sample_lines", "body_bytes", "body_sha256",
    "kv_transfer_label_hits", "kv_connector_hits",
    "target_version", "version_source", "version_error", "snapshot_file",
]


def _split_sample_line(line: str) -> tuple[str, str, str] | None:
    """把一行 Prometheus 样本拆成 (指标名, 标签串, 数值串)。注释行/空行/解不开的行返回 None。

    手写扫描而不是用正则，是因为**标签值里可以出现空格和花括号**
    （例如 `f{path="a b}"} 1`），一个通用正则去切会把这类行切坏；
    这里按"引号配对"扫描，遇到层号归零的 `}` 之后的第一段才算数值。
    """
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    depth = 0
    in_q = False
    esc = False
    name_end = -1
    for i, ch in enumerate(s):
        if in_q:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_q = False
            continue
        if ch == '"':
            in_q = True
        elif ch == "{":
            if depth == 0:
                name_end = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                rest = s[i + 1:].strip().split()
                if not rest or name_end <= 0:
                    return None
                return (s[:name_end], s[name_end:i + 1], rest[0])
        elif ch in " \t" and depth == 0:
            parts = s.split()
            if len(parts) < 2:
                return None
            return (parts[0], "", parts[1])
    return None


def _labels_of(labels_str: str) -> dict[str, str]:
    """把 `{a="1",b="2"}` 解成 dict。引号内的逗号/等号不算分隔符；解不开的片段直接跳过
    ——**宁可少记一个标签，也不要把标签记错**（标签记错会让序列键对不上，两个快照就没法比）。"""
    if not labels_str or not labels_str.startswith("{"):
        return {}
    inner = labels_str[1:-1] if labels_str.endswith("}") else labels_str[1:]
    out: dict[str, str] = {}
    i, n = 0, len(inner)
    while i < n:
        eq = inner.find("=", i)
        if eq < 0:
            break
        key = inner[i:eq].strip()
        j = eq + 1
        if j < n and inner[j] == '"':
            j += 1
            buf: list[str] = []
            while j < n:
                ch = inner[j]
                if ch == "\\" and j + 1 < n:
                    buf.append(inner[j + 1])
                    j += 2
                    continue
                if ch == '"':
                    j += 1
                    break
                buf.append(ch)
                j += 1
            val = "".join(buf)
        else:
            k = inner.find(",", j)
            if k < 0:
                k = n
            val = inner[j:k].strip()
            j = k
        if key:
            out[key] = val
        k = inner.find(",", j)
        if k < 0:
            break
        i = k + 1
    return out


def parse_metrics_text(text: str) -> dict:
    """解析一次 `/metrics` 正文，并判定"有没有 NIXL 项"。

    **判定只回答"出现 / 未出现"，不回答"是多少"**：没有 NIXL 序列时我们并不知道任何数值，
    此时填任何数字（尤其 0）都是编的。有序列时数值照实记，包括真实的 0。
    数值解不出来的样本（`NaN` / `+Inf` / 形态不认识）一律留 `None`，不填 0。
    """
    series: dict[str, float | None] = {}
    nixl: dict[str, float | None] = {}
    nixl_help = 0
    kv_transfer_hits = kv_connector_hits = 0
    version_candidates: list[dict] = []
    n_sample = n_comment = n_unparsable = 0
    lines = text.splitlines()
    for raw in lines:
        st = raw.strip()
        if st.startswith("#"):
            n_comment += 1
            if "nixl" in st.lower():
                nixl_help += 1
            continue
        if not st:
            continue
        got = _split_sample_line(raw)
        if got is None:
            n_unparsable += 1
            continue
        n_sample += 1
        name, labels_str, val_str = got
        labels = _labels_of(labels_str)
        try:
            val: float | None = float(val_str)
        except ValueError:
            val = None
        if val is not None and not math.isfinite(val):
            # `NaN` / `+Inf` 解得出数但**没有数值含义**：留 None（不填 0，也不拿去算差）。
            val = None
        key = name + labels_str if labels_str else name
        series[key] = val
        low = name.lower()
        if "nixl" in low:
            nixl[key] = val
        if "kv_transfer" in low or any("kv_transfer" in v for v in labels.values()):
            # 实测里唯一"沾边"的东西就是 `vllm:prompt_tokens_by_source_total
            # {source="external_kv_transfer"}`——它是**按 token 计数的累计量**，
            # 和"传了多少字节/多久/失败几次"是三样不同的东西，**不能**当成 NIXL。
            kv_transfer_hits += 1
        if "connector" in low or "connector" in labels_str.lower():
            kv_connector_hits += 1
        if "version" in labels and ("info" in low or "build" in low):
            version_candidates.append({"metric": name, "labels": labels,
                                      "version": labels["version"]})
    if nixl:
        nixl_note = (f"出现：本次 /metrics 里有 {len(nixl)} 条 NIXL 相关序列"
                     "（数值是按传输汇总的量，原文见快照 raw_body）")
    else:
        nixl_note = ("未出现：本次 /metrics 里没有任何 NIXL 相关序列。"
                     f"**这一项记作「{NIXL_UNSEEN_TEXT}」，不是「0」**——没有序列时我们并不知道任何数值。")
        if nixl_help:
            nixl_note += f"（注意：注释行里出现了 {nixl_help} 行 NIXL 字样，但没有样本行）"
    # 只有"服务自身构建信息"类指标才被当作版本来源。**这个白名单是刻意收窄的**：
    # 实测的 /metrics 里还有一个 `python_info{...version="3.14.4"}`——那是**解释器的版本**，
    # 拿它当被采集服务的版本就是编数。所以其余候选只记录、不采用。
    accepted = next((c for c in version_candidates
                     if "build_info" in c["metric"].lower()), None)
    return {
        "n_lines": len(lines),
        "n_sample_lines": n_sample,
        "n_comment_lines": n_comment,
        "n_unparsable_lines": n_unparsable,
        "n_series": len(series),
        "series": series,
        # 没有 NIXL 序列时这里是 **None**（不是 {}、更不带任何 0 值）
        "nixl_series": nixl or None,
        "nixl_matched_series_count": len(nixl),
        "nixl_help_or_type_mentions": nixl_help,
        "nixl_state": MET_NIXL_PRESENT if nixl else MET_NIXL_ABSENT,
        "nixl_presence_zh": NIXL_PRESENCE_ZH[MET_NIXL_PRESENT if nixl else MET_NIXL_ABSENT],
        "nixl_matched_series_count_meaning": ("只表示**匹配到的序列条数**，不是任何指标的值；"
                                             "为 0 时含义是「未出现」，不是「传输量为 0」"),
        "nixl_note": nixl_note,
        "kv_transfer_label_hits": kv_transfer_hits,
        "kv_connector_hits": kv_connector_hits,
        "version_probe": {
            "accepted": accepted,
            "version": accepted["version"] if accepted else None,
            "source": f"/metrics 标签 {accepted['metric']}" if accepted else "",
            "candidates": version_candidates,
            "why_other_candidates_rejected": (
                "只有 build_info 这类**明确表示服务构建**的指标才被采用；"
                "其余 version 标签（例如 python_info 的 version 是**解释器版本**）"
                "只记录成候选。拿它们当服务版本就是编数。"),
        },
    }


def derive_version_url(metrics_url: str) -> str:
    """由 `/metrics` 地址推出 `/version` 地址（vLLM 的版本端点就是 `/version`）。

    只在 path 以 `/metrics` 结尾时保留前缀，其余情形一律退回站点根——
    版本端点的位置在服务里是固定的，凭猜测拼路径比"取不到"更糟。
    """
    u = urlparse(metrics_url or "")
    if u.scheme not in ("http", "https") or not u.hostname:
        return ""
    prefix = u.path[:-len("/metrics")] if u.path.endswith("/metrics") else ""
    return f"{u.scheme}://{u.netloc}{prefix}/version"


def _http_get(url: str, timeout_s: float, accept: str) -> dict:
    """一次 GET。**所有异常都变成可落盘的字段，不抛给调用方**（采集失败不该让测量失败）。"""
    out = {"http_status": None, "body": "", "body_bytes": 0, "body_sha256": "",
           "error_class": "", "error_text": "", "wall_s": 0.0}
    t0 = time.time()
    try:
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ValueError(f"不是合法的 http(s) 地址：{url!r}")
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        cls = (http.client.HTTPSConnection if u.scheme == "https"
               else http.client.HTTPConnection)
        conn = cls(u.hostname, u.port or (443 if u.scheme == "https" else 80),
                   timeout=timeout_s)
        try:
            conn.request("GET", path, headers={"Accept": accept,
                                               "User-Agent": "measure_serving/metrics-snapshot"})
            resp = conn.getresponse()
            body = resp.read()
            out["http_status"] = resp.status
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as e:
        # 注意顺序：socket.timeout 是 OSError 的子类，必须先判它，否则会被归成"连不上"。
        if isinstance(e, (socket.timeout, TimeoutError)):
            out["error_class"] = ERR_TIMEOUT
        elif isinstance(e, (ConnectionError, OSError, socket.gaierror)):
            out["error_class"] = ERR_CONNECT
        else:
            out["error_class"] = ERR_INTERNAL
        out["error_text"] = f"{type(e).__name__}: {e}"
        out["wall_s"] = round(time.time() - t0, 6)
        return out
    out["body"] = body.decode("utf-8", errors="replace")
    out["body_bytes"] = len(body)
    out["body_sha256"] = hashlib.sha256(body).hexdigest()
    out["wall_s"] = round(time.time() - t0, 6)
    if out["http_status"] is not None and out["http_status"] >= 500:
        out["error_class"] = ERR_HTTP_5XX
        out["error_text"] = f"HTTP {out['http_status']}（正文前 200 字符：{out['body'][:200]!r}）"
    elif out["http_status"] is not None and out["http_status"] >= 400:
        out["error_class"] = ERR_HTTP_4XX
        out["error_text"] = f"HTTP {out['http_status']}（正文前 200 字符：{out['body'][:200]!r}）"
    elif out["http_status"] is not None and not out["body"].strip():
        # 200 但正文为空：**不能**当成"抓到了、只是没有 NIXL"——什么都没抓到，
        # 归到"抓取失败"才诚实（否则第四态会凭空多出一批"未出现"）。
        out["error_class"] = ERR_HTTP_5XX
        out["error_text"] = "HTTP 200 但正文为空（0 字节）：没有拿到任何指标，按抓取失败记"
    return out


def fetch_metrics_once(metrics_url: str, timeout_s: float) -> dict:
    """抓一次 `/metrics`，返回可落盘的结果（成功或失败的字段形状一致）。"""
    r = _http_get(metrics_url, timeout_s, accept="text/plain;charset=utf-8")
    r["ok"] = bool(r["http_status"] is not None
                   and 200 <= r["http_status"] < 300 and not r["error_class"])
    return r


def fetch_version_once(version_url: str, timeout_s: float) -> dict:
    """取被采集服务的版本。**取不到就记"未取到"并留下错误原文，绝不编。**"""
    out = {"version": None, "source": "", "raw": "", "error": ""}
    if not version_url:
        out["error"] = "没有可用的版本地址（未给 --version-url，也无法从 --metrics-url 推导）"
        return out
    r = _http_get(version_url, timeout_s, accept="application/json")
    out["raw"] = r["body"][:400]
    if r["error_class"] or not (r["http_status"] and 200 <= r["http_status"] < 300):
        out["error"] = (r["error_text"] or f"HTTP {r['http_status']}"
                        if r["error_text"] or r["http_status"] else "未知错误")
        return out
    text = r["body"].strip()
    try:
        obj = json.loads(text)
    except Exception:
        obj = None
    if isinstance(obj, dict):
        for k in ("version", "vllm_version", "build_version"):
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                out["version"] = v.strip()
                out["source"] = f"GET {urlparse(version_url).path or '/version'}（JSON 字段 {k}）"
                return out
        out["error"] = (f"HTTP 200，但 JSON 里没有 version/vllm_version/build_version 字段"
                        f"（字段名：{sorted(obj)[:12]}）")
        return out
    if text and "\n" not in text and len(text) <= 64:
        # 有的实现直接回一行纯文本版本号。正文原样就是版本，记下来**不算编**。
        out["version"] = text
        out["source"] = f"GET {urlparse(version_url).path or '/version'}（正文原样，单行短文本）"
        return out
    out["error"] = f"HTTP 200，但正文里没有可识别的版本（前 200 字符：{text[:200]!r}）"
    return out


def diff_snapshots(start_text: str, end_text: str, top_n: int = METRICS_DIFF_TOP_N) -> dict:
    """比较首末两次快照的**汇总**变化（整次运行前后）。

    · 只在**两侧都在、且两侧都能解出数值**的序列上算 delta；解不出数值的进
      `n_skipped_non_numeric`，**不拿 0 顶替**。
    · 只出现在一侧的序列进 `only_start` / `only_end` 名单，**不当 0**
      （两次抓取之间服务可能重启或注册新指标，把"消失"写成 0 就是编数）。
    · 这些差是**进程级累计量**在两点之间的变化，覆盖整段运行，**不能拆给单个请求**
      （见 SNAPSHOT_SCOPE_NOTE）。
    """
    a = parse_metrics_text(start_text)["series"]
    b = parse_metrics_text(end_text)["series"]
    common = sorted(set(a) & set(b))
    deltas = []
    n_skipped = 0
    for k in common:
        va, vb = a[k], b[k]
        if va is None or vb is None:
            n_skipped += 1
            continue
        deltas.append({"series": k, "start": va, "end": vb, "delta": vb - va})
    deltas.sort(key=lambda d: -abs(d["delta"]))
    only_start = sorted(set(a) - set(b))
    only_end = sorted(set(b) - set(a))
    return {
        "n_series_start": len(a), "n_series_end": len(b), "n_common": len(common),
        "n_only_start": len(only_start), "n_only_end": len(only_end),
        "n_deltas": len(deltas), "n_skipped_non_numeric": n_skipped,
        "top_deltas": deltas[:top_n],
        "only_start_examples": only_start[:top_n],
        "only_end_examples": only_end[:top_n],
        "only_in_one_snapshot_note": ("只出现在一侧的序列**不计差、也不当 0**："
                                      "两次抓取之间服务可能重启或新增/注销指标。"),
        "note": ("这些差是**两个时间点之间的进程级累计量变化**，覆盖整段运行；"
                 "**不能**拆给单个请求（见 scope_note）。"),
    }


class MetricsSampler:
    """按固定间隔抓 `/metrics`，把**原始正文 + 带时间戳的元数据**落盘的后台线程。

    三个设计点，都是为了"采集不能改变被测量的东西"：
      · 抓取在**独立线程**里做 —— HTTP 往返不进测量主路径，请求时延不受影响；
      · 每次抓取的耗时 `scrape_wall_s` 与尝试次数都落盘 —— 读的人能判断这份开销有多大；
      · **任何失败都不抛给主流程** —— 采集失败只记状态，不让测量跟着失败。

    时间轴用"相对本次运行起点 `t0`"的秒数（`t_rel_s`）**加**绝对 UTC 时刻两套：
    相对量方便与请求时刻对齐，绝对量跨机器可比（阶段 C 要在多台机器上对时）。
    """

    def __init__(self, metrics_url: str, interval_s: float, timeout_s: float,
                 version_url: str, out_dir: Path, run_id: str, t0: float):
        self.metrics_url = metrics_url
        self.interval_s = max(float(interval_s), 0.05)
        self.timeout_s = float(timeout_s)
        self.version_url = version_url or derive_version_url(metrics_url)
        self.out_dir = out_dir
        self.run_id = run_id
        self.t0 = t0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.attempts: list[dict] = []
        self.snapshots: list[dict] = []
        self.first_body: str = ""
        self.last_body: str = ""
        self.version: dict = {"version": None, "source": "", "raw": "", "error": ""}
        self._version_done = False
        self.csv_path: str | None = None

    # ---------------- 线程控制 ----------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="metrics-sampler",
                                        daemon=True)
        self._thread.start()

    def stop(self, join_timeout_s: float | None = None) -> None:
        """停机并写 CSV 汇总。**必须在写 summary/manifest 之前调用**（它们要用这里的结果）。"""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=join_timeout_s if join_timeout_s is not None
                              else self.timeout_s + 5.0)
        self.csv_path = self._write_csv()

    def _loop(self) -> None:
        seq = 0
        while not self._stop.is_set():
            # 第 0 次是 start（开局基线），之后每 interval_s 一次 periodic
            self._scrape(seq, "start" if seq == 0 else "periodic", seq * self.interval_s)
            seq += 1
            deadline = self.t0 + seq * self.interval_s
            while not self._stop.is_set():
                remain = deadline - time.time()
                if remain <= 0:
                    break
                self._stop.wait(min(remain, 0.25))
        # 收尾再抓一次：**"整次运行前后"的对比靠的就是这最后一次**
        self._scrape(seq, "end", time.time() - self.t0)

    # ---------------- 单次抓取 ----------------
    def _resolve_version(self, parsed: dict | None) -> tuple[str, str, str]:
        """(版本字面值, 来源, 取不到时的原因)。`parsed` 为 None 表示这次没抓到正文。"""
        if not self._version_done:
            self.version = fetch_version_once(self.version_url, self.timeout_s)
            self._version_done = True
        if self.version["version"]:
            return self.version["version"], self.version["source"], ""
        err = self.version["error"] or "未知原因"
        if parsed is not None:
            vp = parsed["version_probe"]
            if vp["version"]:
                return vp["version"], vp["source"], err
            if vp["candidates"]:
                err += (f"；/metrics 里有 {len(vp['candidates'])} 个 version 标签候选，"
                        f"但它们不是服务构建信息（例如 python_info 的 version 是解释器版本），"
                        f"**未采用**")
        return VERSION_UNAVAILABLE, "", err

    def _scrape(self, seq: int, phase: str, planned_t_rel_s: float | None) -> None:
        t_call = time.time()
        res = fetch_metrics_once(self.metrics_url, self.timeout_s)
        t_done = time.time()
        parsed = parse_metrics_text(res["body"]) if res["ok"] else None
        target, vsrc, verr = self._resolve_version(parsed)

        if res["ok"] and parsed is not None:
            state = parsed["nixl_state"]
            presence = parsed["nixl_presence_zh"]
            nixl_values = parsed["nixl_series"]      # 缺项时是 None（不是 {}、更不是 0）
            nixl_count = parsed["nixl_matched_series_count"]
            nixl_note = parsed["nixl_note"]
            n_series = parsed["n_series"]
            n_sample = parsed["n_sample_lines"]
            help_mentions = parsed["nixl_help_or_type_mentions"]
            kvt, kvc = parsed["kv_transfer_label_hits"], parsed["kv_connector_hits"]
        else:
            state = MET_SCRAPE_FAILED
            presence = NIXL_PRESENCE_ZH[MET_SCRAPE_FAILED]
            nixl_values = None
            nixl_count = None
            nixl_note = ("抓取失败：这次没有拿到 /metrics 正文，因此**无从判断**有没有 NIXL 项。"
                         f"**不能写成「{NIXL_UNSEEN_TEXT}」，更不能写成 0**——失败与缺项是两回事。")
            n_series = n_sample = None
            help_mentions = None
            kvt = kvc = None

        rec = {
            "run_id": self.run_id, "seq": seq, "phase": phase,
            "utc": datetime.now(timezone.utc).isoformat(),
            "planned_t_rel_s": round(planned_t_rel_s, 6) if planned_t_rel_s is not None else None,
            "t_rel_s": round(t_call - self.t0, 6),
            "t_utc_epoch": round(t_call, 6),
            "scrape_wall_s": round(t_done - t_call, 6),
            "url": self.metrics_url,
            "ok": bool(res["ok"]),
            "http_status": res["http_status"],
            "error_class": res["error_class"],
            "error_text": res["error_text"],
            "body_bytes": res["body_bytes"],
            "body_sha256": res["body_sha256"],
            "state": state,
            "state_zh": METRICS_STATE_ZH[state],
            "nixl_presence_zh": presence,
            "nixl_note": nixl_note,
            "nixl_matched_series_count": nixl_count,
            "nixl_matched_series_count_meaning": (
                "只表示**匹配到的序列条数**，不是任何指标的值；"
                f"为 0 时含义是「{NIXL_UNSEEN_TEXT}」，不是「传输量为 0」"),
            "nixl_series_values": nixl_values,
            "nixl_help_or_type_mentions": help_mentions,
            "n_series": n_series,
            "n_sample_lines": n_sample,
            "kv_transfer_label_hits": kvt,
            "kv_connector_hits": kvc,
            "target_version": target,
            "version_source": vsrc,
            "version_error": verr,
            # 版本标签的**完整取证**：被采用了哪条、以及哪些候选被拒（例如 python_info
            # 的 version 是解释器版本）。落盘是为了让"没拿错版本"这件事可事后复核。
            "version_label_probe": (parsed["version_probe"] if parsed is not None else None),
            "scope_note": SNAPSHOT_SCOPE_NOTE,
            "raw_body": res["body"],
        }
        if phase == "end":
            rec["planned_t_rel_s"] = rec["t_rel_s"]
            rec["planned_t_rel_s_note"] = "phase=end 的 planned 就是实际停机时刻：它不是排班点"
        path = self.out_dir / f"{self.run_id}_scrape_{seq:03d}_{phase}.json"
        try:
            path.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
            rec["snapshot_file"] = str(path)
        except Exception as e:
            rec["snapshot_file"] = ""
            rec["error_text"] = (str(rec["error_text"]) +
                                 f" | 快照落盘失败: {type(e).__name__}: {e}").strip(" |")
        self.attempts.append(rec)
        if rec["ok"]:
            self.snapshots.append(rec)
            if not self.first_body:
                self.first_body = res["body"]
            self.last_body = res["body"]
        print(f"  [metrics] seq={seq} phase={phase} t={rec['t_rel_s']}s "
              f"state={rec['state_zh']} "
              f"NIXL={rec['nixl_presence_zh']} version={rec['target_version']}"
              + (f"  错误：{rec['error_text']}" if rec["error_text"] else ""), flush=True)

    def _write_csv(self) -> str:
        path = self.out_dir / f"{self.run_id}_metrics_scrapes.csv"
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=SCRAPE_COLS, extrasaction="ignore")
            w.writeheader()
            for a in self.attempts:
                w.writerow({k: _csv_cell(a.get(k)) for k in SCRAPE_COLS})
        return str(path)


def _csv_cell(v) -> str:
    """CSV 单元格：None 写空、复合类型写 JSON、其余原样。**None 写空而不是写 0**。"""
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def build_metrics_summary(sampler: MetricsSampler | None, metrics_url: str,
                          interval_s: float, timeout_s: float, version_url: str) -> dict:
    """把一次运行的采集结果收成一个 summary 块：**四个状态在这里被显式标注**。

    未配置时也返回同样形状的块（state=未配置地址），这样下游读 `state` 一个字段就够了，
    不必靠"这个键在不在"来猜——**缺键与缺项一样容易被误读成 0**。
    """
    base = {
        "state": MET_NOT_CONFIGURED,
        "state_zh": METRICS_STATE_ZH[MET_NOT_CONFIGURED],
        "nixl_presence_zh": NIXL_PRESENCE_ZH[MET_NOT_CONFIGURED],
        "nixl_matched_series_count": None,
        "nixl_series_values": None,
        "nixl_note": ("未采集：本次没有给 --metrics-url，整条采集路径没有生效。"
                      f"**这一项记作「未配置地址」，不是「服务端没有 NIXL 指标」，也不是 0。**"),
        "n_attempts": 0, "n_ok": 0, "n_failed": 0,
        "url": "", "version_url": "", "interval_s": interval_s, "timeout_s": timeout_s,
        "target_version": VERSION_UNAVAILABLE, "version_source": "", "version_error": "",
        "first_snapshot": None, "last_snapshot": None,
        "scrapes_csv": None, "snapshot_files": [], "diff_start_end": None,
        "scope_note": SNAPSHOT_SCOPE_NOTE,
    }
    if sampler is None or not metrics_url:
        return base
    ok = [a for a in sampler.attempts if a["ok"]]
    failed = [a for a in sampler.attempts if not a["ok"]]
    has_nixl = any(a.get("nixl_series_values") for a in ok)
    if not ok:
        state = MET_SCRAPE_FAILED
    else:
        state = MET_NIXL_PRESENT if has_nixl else MET_NIXL_ABSENT
    last_ok = ok[-1] if ok else None
    first_ok = ok[0] if ok else None
    diff = None
    if sampler.first_body and sampler.last_body and len(ok) >= 2:
        diff = diff_snapshots(sampler.first_body, sampler.last_body)
    # 取哪一次的记录来填 NIXL 字段：**必须是最新一次"成功"的抓取**，否则会出现
    # "state 说未出现 / 计数却是空的"这种自相矛盾（末次抓取失败、之前成功时就会发生）。
    # 全部失败时才退回最后一次尝试（此时 state 是"抓取失败"，NIXL 字段本来就该是空的）。
    detail = last_ok if last_ok is not None else (
        sampler.attempts[-1] if sampler.attempts else {})
    return {
        **base,
        "state": state,
        "state_zh": METRICS_STATE_ZH[state],
        "nixl_presence_zh": NIXL_PRESENCE_ZH[state],
        "nixl_matched_series_count": (detail.get("nixl_matched_series_count")
                                      if state in (MET_NIXL_ABSENT, MET_NIXL_PRESENT) else None),
        "nixl_series_values": (last_ok or {}).get("nixl_series_values"),
        "nixl_note": (detail.get("nixl_note") or base["nixl_note"]),
        "n_attempts": len(sampler.attempts), "n_ok": len(ok), "n_failed": len(failed),
        "url": metrics_url,
        "version_url": sampler.version_url,
        "interval_s": sampler.interval_s, "timeout_s": sampler.timeout_s,
        "target_version": detail.get("target_version", VERSION_UNAVAILABLE),
        "version_source": detail.get("version_source", ""),
        "version_error": detail.get("version_error", ""),
        "first_snapshot": ({"seq": first_ok["seq"], "phase": first_ok["phase"],
                            "t_rel_s": first_ok["t_rel_s"], "body_bytes": first_ok["body_bytes"],
                            "n_series": first_ok["n_series"]} if first_ok else None),
        "last_snapshot": ({"seq": last_ok["seq"], "phase": last_ok["phase"],
                           "t_rel_s": last_ok["t_rel_s"], "body_bytes": last_ok["body_bytes"],
                           "n_series": last_ok["n_series"]} if last_ok else None),
        "scrapes_csv": sampler.csv_path,
        "snapshot_files": [a.get("snapshot_file", "") for a in sampler.attempts],
        "diff_start_end": diff,
        "error_examples": [{"seq": a["seq"], "phase": a["phase"],
                            "error_class": a["error_class"], "error_text": a["error_text"]}
                           for a in failed][:5],
    }


# ---------------------------------------------------------------------------
# 假指标服务：**四种状态里"抓到 NIXL 项"这一种在真机上验不了**
# （vLLM 0.31.0 单实例、未开 KV 连接器时没有 NIXL 序列），所以按配方造一份正文。
# 这里所有数值都是**伪造的**，只用来验证状态机与解析器，不构成任何测量结论。
# ---------------------------------------------------------------------------
@dataclass
class MetricsBackendConfig:
    variant: str = "nonixl"        # nonixl | nixl | nixl_help_only（只在注释里提 NIXL）
    fail_mode: str = "none"        # none | http_500 | http_404 | empty_200 | hang
    version_mode: str = "ok"       # ok | plain | missing(http_404) | http_500 | garbage
    version: str = FAKE_METRICS_VERSION
    hang_s: float = 3.0

    def to_dict(self) -> dict:
        return asdict(self)


def fake_metrics_text(variant: str = "nonixl",
                      version: str = "fake-vllm-0.0.1-test") -> str:
    """一份**形如 vLLM 0.31.0** 的 `/metrics` 正文（字段名照抄 B0_VLLM_REPORT §3 的实测）。"""
    lines = [
        "# HELP python_gc_objects_collected_total Objects collected during gc",
        "# TYPE python_gc_objects_collected_total counter",
        'python_gc_objects_collected_total{generation="0"} 4766.0',
        'python_gc_objects_collected_total{generation="1"} 42875.0',
        "# HELP python_info Python platform information",
        "# TYPE python_info gauge",
        # ⚠️ 这一行的 version 是**解释器版本**：它专门用来验证版本标签的白名单
        #    （拿它当服务版本就是编数，实现里必须只把它记为"候选"）。
        'python_info{implementation="CPython",major="3",minor="12",patchlevel="10",'
        'version="3.12.10"} 1.0',
        "# HELP process_resident_memory_bytes Resident memory size in bytes.",
        "# TYPE process_resident_memory_bytes gauge",
        "process_resident_memory_bytes 9.4814208e+08",
        "# HELP process_start_time_seconds Start time of the process since unix epoch.",
        "# TYPE process_start_time_seconds gauge",
        "process_start_time_seconds 1.79135360062e+09",
        "# HELP process_cpu_seconds_total Total user and system CPU time spent in seconds.",
        "# TYPE process_cpu_seconds_total counter",
        "process_cpu_seconds_total 43.59",
        "# HELP vllm:cache_config_info Information of the LLMEngine CacheConfig",
        "# TYPE vllm:cache_config_info gauge",
        'vllm:cache_config_info{block_size="16",enable_prefix_caching="True",'
        'num_gpu_blocks="4312",kv_cache_size_tokens="68992"} 1.0',
        "# HELP vllm:num_requests_running Number of requests currently running.",
        "# TYPE vllm:num_requests_running gauge",
        'vllm:num_requests_running{model_name="fake-model"} 2.0',
        "# HELP vllm:kv_cache_usage_perc KV-cache usage.",
        "# TYPE vllm:kv_cache_usage_perc gauge",
        'vllm:kv_cache_usage_perc{model_name="fake-model"} 0.13',
        # 唯一"沾边"的那条：名字里有 kv_transfer，但它是**按 token 计数的累计量**，
        # 不是 NIXL。实现里必须**不**把它算成 NIXL（这条专门用来抓这个错）。
        "# HELP vllm:prompt_tokens_by_source_total prompt tokens by source",
        "# TYPE vllm:prompt_tokens_by_source_total counter",
        'vllm:prompt_tokens_by_source_total{source="external_kv_transfer"} 0.0',
        "# HELP vllm:request_success_total Count of successfully processed requests.",
        "# TYPE vllm:request_success_total counter",
        'vllm:request_success_total{finished_reason="stop",model_name="fake-model"} 12.0',
    ]
    if variant == "nixl_help_only":
        lines += [
            "# HELP vllm:nixl_bytes_transferred Number of bytes transferred by NIXL",
            "# TYPE vllm:nixl_bytes_transferred counter",
        ]
    if variant == "nixl":
        lines += [
            "# HELP vllm:nixl_bytes_transferred_total Number of bytes transferred by NIXL",
            "# TYPE vllm:nixl_bytes_transferred_total counter",
            'vllm:nixl_bytes_transferred_total{model_name="fake-model",transfer_type="write"}'
            " 1.25e+09",
            'vllm:nixl_bytes_transferred_total{model_name="fake-model",transfer_type="read"}'
            " 2.5e+08",
            "# HELP vllm:nixl_xfer_time_seconds_total Total time spent in NIXL transfers",
            'vllm:nixl_xfer_time_seconds_total{model_name="fake-model",transfer_type="write"}'
            " 12.5",
            'vllm:nixl_post_time_seconds_total{model_name="fake-model"} 3.5',
            'vllm:nixl_num_descriptors{model_name="fake-model"} 4096',
            # **真 0**：序列存在时 0 就是"确实一次都没失败"。它与"缺项"必须分开读。
            'vllm:nixl_num_failed_transfers{model_name="fake-model"} 0.0',
            'vllm:nixl_num_failed_notifications{model_name="fake-model"} 0.0',
        ]
    return "\n".join(lines) + "\n"


class _MetricsState:
    def __init__(self, cfg: MetricsBackendConfig):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.n_metrics = 0
        self.n_version = 0


def make_metrics_handler(state: _MetricsState):
    cfg = state.cfg

    class MetricsHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # noqa: A003
            return

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            try:
                path = self.path.split("?")[0].rstrip("/")
                if path.endswith("/metrics"):
                    with state.lock:
                        state.n_metrics += 1
                    if cfg.fail_mode == "http_500":
                        return self._send(500, b"boom\n", "text/plain")
                    if cfg.fail_mode == "http_404":
                        return self._send(404, b"no metrics here\n", "text/plain")
                    if cfg.fail_mode == "empty_200":
                        return self._send(200, b"", "text/plain")
                    if cfg.fail_mode == "hang":
                        time.sleep(cfg.hang_s)   # 让客户端的抓取超时先到（本线程是 daemon）
                    body = fake_metrics_text(cfg.variant, cfg.version).encode()
                    return self._send(200, body,
                                      "text/plain; version=0.0.4; charset=utf-8")
                if path.endswith("/version"):
                    with state.lock:
                        state.n_version += 1
                    if cfg.version_mode == "http_500":
                        return self._send(500, b"boom\n", "text/plain")
                    if cfg.version_mode == "missing":
                        return self._send(404, b"no such endpoint\n", "text/plain")
                    if cfg.version_mode == "plain":
                        return self._send(200, cfg.version.encode(), "text/plain")
                    if cfg.version_mode == "garbage":
                        return self._send(200, b'{"detail":"no version field"}',
                                          "application/json")
                    return self._send(200, json.dumps({"version": cfg.version}).encode(),
                                      "application/json")
                return self._send(404, b"not found\n", "text/plain")
            except Exception:
                # hang 模式下客户端先超时断开，这里再写就会 BrokenPipe——
                # 那是**预期内**的收尾，不该刷一屏 traceback。
                return

    return MetricsHandler


def start_fake_metrics(cfg: MetricsBackendConfig, port: int, bind: str = "127.0.0.1"):
    state = _MetricsState(cfg)
    srv = ThreadingHTTPServer((bind, port), make_metrics_handler(state))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, state


def run_fake_metrics(args) -> int:
    cfg = MetricsBackendConfig(variant=args.metrics_variant, fail_mode=args.metrics_fail,
                              version_mode=args.version_mode,
                              version=args.metrics_version)
    srv, _st = start_fake_metrics(cfg, args.port, args.bind)
    print(f"[fake-metrics] 监听 http://{args.bind}:{args.port}  "
          f"配方={json.dumps(cfg.to_dict(), ensure_ascii=False)}")
    print("               这是**假指标服务**：正文里的指标值全部是伪造的，"
          "只用来验证'四种状态'与解析器，不作为任何测量结论。Ctrl-C 退出。")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    srv.shutdown()
    return 0


# ===========================================================================
# 七、自检（纯逻辑，不开网络）
# ===========================================================================
def run_selftest() -> int:
    """验证区间数学与覆盖判据本身。**这是给判据做的哨兵**。

    为什么判据也要自检：本工具的结论是"覆盖通过"。如果区间并集算错了，
    它会**永远通过**（永远通过 = 没有信息量）。所以这里用几组已知答案的输入反查它，
    并且**必须包含会 FAIL 的负对照**——只验通过路径等于没验。
    """
    fails: list[str] = []

    def expect(name: str, got, want, tol=1e-9):
        if isinstance(want, float):
            ok = (abs(float(got) - want) <= tol) if not math.isnan(want) else True
        else:
            ok = (got == want)
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: got={got} want={want}")
        if not ok:
            fails.append(name)

    print("== selftest 1/5：区间并集 / 空档 / 重叠 / 分位数 ==")
    expect("并集：重叠两段", union_length([(0, 2), (1, 3)]), 3.0)
    expect("并集：相接两段", union_length([(0, 1), (1, 2)]), 2.0)
    expect("并集：分离两段", union_length([(0, 1), (3, 4)]), 2.0)
    expect("空档：中间一段", round(gaps([(0, 1), (3, 4)], 0, 4)[0][0], 9), 1.0)
    expect("空档：合计（[0,1]+[2,4] 在窗口 [0,4] 内）",
           sum(b - a for a, b in gaps([(0, 1), (2, 4)], 0, 4)), 1.0)
    expect("空档：完全覆盖时无空档", len(gaps([(0, 5)], 0, 5)), 0)
    expect("重叠：两段重叠 1", overlap_length([(0, 2), (1, 3)]), 1.0)
    # 逐对：(0,3)&(1,2)=1、(0,3)&(2,3)=1、(1,2)&(2,3)=0 ⇒ 合计 2，
    # 而"三重重叠"的总量只有 2（并集长 3、Σ长 6 ⇒ 重复计入 3）。
    # 这里故意把两者写成不同的数，用来钉住"本函数算的是两两重叠总量"这个语义。
    expect("重叠：按两两计", overlap_length([(0, 3), (1, 2), (2, 3)]), 2.0)
    expect("分位数：中位", percentile([1, 2, 3, 4], 0.5), 2.5)
    expect("分位数：单值", percentile([7.0], 0.95), 7.0)
    expect("裁剪：越界部分被切掉", union_length(clamp([(0, 10)], 2, 5)), 3.0)

    print("== selftest 2/5：覆盖判据的正确路径 ==")
    lo, hi, tol = 100.0, 101.0, 0.02
    tiled = [("send", 100.0, 100.1), ("wait_first_byte", 100.1, 100.3),
             ("to_first_content", 100.3, 100.6), ("decode_stream", 100.6, 100.9),
             ("teardown", 100.9, 101.0)]
    c = check_coverage(tiled, lo, hi, tol, CLIENT_STAGES)
    expect("铺满、无重叠 => PASS", c.verdict, "PASS")
    expect("铺满、无重叠：空档 0", c.max_gap_s, 0.0)
    expect("铺满、无重叠：Σ≈窗口", round(c.sum_stage_s - c.span_s, 9), 0.0)
    expect("铺满、无重叠：重叠 0", c.overlap_s, 0.0)
    expect("铺满、无重叠：LOO 最小空档 = 最小段长 0.1",
           round(c.loo_min_gap_s, 9), 0.1)
    expect("铺满、无重叠：窗口外部分 0", c.outside_s, 0.0)

    print("== selftest 3/5：负对照（判据必须抓得住错）==")
    # 负对照 1：漏记一段 => 必须 FAIL。这正是判据存在的意义。
    missing = [x for x in tiled if x[0] != "wait_first_byte"]
    c2 = check_coverage(missing, lo, hi, tol, CLIENT_STAGES)
    expect("漏记 wait_first_byte => FAIL", c2.verdict, "FAIL")
    expect("漏记 wait_first_byte：露出空档 0.2", round(c2.max_gap_s, 6), 0.2)

    # 负对照 2：**有重叠**的集合 => 仍然 PASS，但 Σ区间 > 窗口。
    # 这就是"阶段 A 的加总口径在阶段 B 不适用"的最小复现。
    over = [("queue", 100.00, 100.05), ("prefill", 100.05, 100.40),
            ("kv_xfer", 100.25, 100.55),   # 与前处理尾部重叠 0.15 s（逐层传 KV 的写照）
            ("decode", 100.55, 100.95)]
    c3 = check_coverage(over, 100.00, 100.95, tol, SERVER_STAGES)
    expect("有重叠 => 仍 PASS", c3.verdict, "PASS")
    expect("有重叠：Σ − 窗口 = 0.15（>0）", round(c3.sum_stage_s - c3.span_s, 9), 0.15)
    expect("有重叠：重叠量 = 0.15", round(c3.overlap_s, 9), 0.15)
    expect("有重叠：LOO 最小空档 > eps", c3.loo_min_gap_s > LOO_EPS_S, True)

    # 负对照 3：某段伸到窗口之外（时钟没对齐的信号）=> outside > 0 且 FAIL
    skew = tiled + [("prefill", 99.5, 99.9)]
    c4 = check_coverage(skew, lo, hi, tol, CLIENT_STAGES)
    expect("区间伸到窗口外 => outside > 0", c4.outside_s > 0.0, True)
    expect("区间伸到窗口外 => FAIL", c4.verdict, "FAIL")

    # 负对照 4：完全空集 => 空档 = 整个窗口 => FAIL（不是"没数据所以通过"）
    c5 = check_coverage([], lo, hi, tol, CLIENT_STAGES)
    expect("空集 => 空档 = 全窗", round(c5.max_gap_s, 6), 1.0)
    expect("空集 => FAIL", c5.verdict, "FAIL")

    # 负对照 5（类型）：所有汇总量必须是 float。`sum([])` 返回的是**整数** 0，
    # 而下游按 isinstance(x, float) 过滤时会把"恰好为 0 的正常值"当成缺数据丢掉。
    # 这不是假想的问题：本文件第四版 summary 里的 cov_gap_total_s 就因此成了 null。
    expect("无空档时 gap_total_s 仍是 float", isinstance(c.gap_total_s, float), True)
    expect("空集时 gap_total_s 仍是 float", isinstance(c5.gap_total_s, float), True)
    expect("sum_stage_s 是 float", isinstance(c5.sum_stage_s, float), True)

    # ================= 4/4：输出块 ≠ token 的口径 =================
    # 这一节是**为口径修正加的哨兵**。它不联网、不依赖显卡：把"一条 SSE 事件"直接喂给
    # 分类函数，把"一串块时刻"直接喂给间隔函数，两处都是纯逻辑，所以能在这里钉死。
    print("== selftest 4/5：输出块口径（块 ≠ token）==")
    # ① 分类：三种片段必须分清。最容易写错的是"带 choices 但文本为空"——
    #    它**是**输出块（真 vLLM 在中文/emoji 下会发），而"choices 为空、只有 usage"
    #    的收尾片**不是**输出块。混起来的后果是块数悄悄多 1 或少几块。
    k1 = classify_event({"choices": [{"index": 0, "text": "tok ", "finish_reason": None}]})
    expect("分类：普通内容块 => output", k1[0], "output")
    expect("分类：普通内容块的文本", k1[1], "tok ")
    k2 = classify_event({"choices": [{"index": 0, "text": "", "finish_reason": None,
                                      "token_ids": [7]}]})
    expect("分类：有 choices 但文本为空 => 仍是 output", k2[0], "output")
    expect("分类：空文本块的 token_ids 个数", k2[2], 1)
    k3 = classify_event({"choices": [], "usage": {"completion_tokens": 32}})
    expect("分类：只有 usage 的收尾块 => usage_only（**不算输出块**）", k3[0], "usage_only")
    expect("分类：只有 usage 的收尾块带出 usage", k3[4]["completion_tokens"], 32)
    k4 = classify_event({"x_stage_report": {"a": 1}})
    expect("分类：阶段报告片 => other", k4[0], "other")
    k5 = classify_event({"choices": [{"delta": {"content": "hi"}}]})
    expect("分类：chat 形状的 delta 也能取到文本", k5[1], "hi")

    # ② 间隔：块口径与历史 tpot 口径**只在存在空文本块时不同**，且差异方向可算。
    #    造 5 个输出块、间隔都是 0.02 s。把第 1、3 块标成"文本为空"，
    #    历史口径就会把它们跳过去，于是把两个间隔并成一个。
    def two_views(times: list[float], empty_idx: set[int]):
        return (interval_stats(times),
                interval_stats([t for i, t in enumerate(times) if i not in empty_idx]))

    ts_all = [0.00, 0.02, 0.04, 0.06, 0.08]
    out_a, con_a = two_views(ts_all, set())
    expect("间隔：没有空文本块时，两种口径逐位相同", out_a == con_a, True)
    expect("间隔：5 个块 => 4 个间隔，均值 0.02", round(out_a[3], 9), 0.02)
    out_b, con_b = two_views(ts_all, {1, 3})
    expect("间隔：块口径仍数全部块（4 个间隔，均值 0.02）", round(out_b[3], 9), 0.02)
    expect("间隔：历史口径跳过空文本块 => 把两个间隔并成一个（0.04 = 2×0.02）",
           round(con_b[3], 9), 0.04)
    expect("间隔：只有一个块时不给数（返回 nan，不是 0.0）",
           math.isnan(interval_stats([1.0])[3]), True)
    expect("间隔：零个块时不给数", math.isnan(interval_stats([])[0]), True)

    # ================= 5/5：metrics 快照的四态与解析（**哨兵：缺项 ≠ 0**）=================
    # 这一节把"四种状态"与"缺项不许写成 0"钉死在纯逻辑层：解析器、状态机、版本白名单、
    # 首末对比，四件都不联网就能验。**联网的那一段（真抓取）由 battery 的 metrics 臂验**，
    # 因为那一段必须真的发一次 HTTP 才有意义。
    print("== selftest 5/5：metrics 四态与解析（缺项 ≠ 0）==")
    p_no = parse_metrics_text(fake_metrics_text("nonixl"))
    p_yes = parse_metrics_text(fake_metrics_text("nixl"))
    p_help = parse_metrics_text(fake_metrics_text("nixl_help_only"))

    expect("缺项：状态 = nixl_absent", p_no["nixl_state"], MET_NIXL_ABSENT)
    expect("缺项：字面值就是「未出现」", p_no["nixl_presence_zh"], NIXL_UNSEEN_TEXT)
    expect("缺项：序列字典是 None（**不是 {} 也不是 0**）", p_no["nixl_series"] is None, True)
    expect("缺项：匹配到的序列条数为 0（这是**条数**，不是任何指标的值）",
           p_no["nixl_matched_series_count"], 0)
    # 这条是本任务最关键的一处陷阱：`external_kv_transfer` 名字里有 kv_transfer，
    # 但它是按 token 计数的累计量，**不是** NIXL（B0_VLLM_REPORT §3 实测里唯一沾边的东西）。
    expect("缺项：沾边的 kv_transfer 序列被单独计数、且**没被算成 NIXL**",
           (p_no["kv_transfer_label_hits"], p_no["nixl_matched_series_count"]), (1, 0))

    expect("有项：状态 = nixl_present", p_yes["nixl_state"], MET_NIXL_PRESENT)
    expect("有项：字面值 = 出现", p_yes["nixl_presence_zh"], NIXL_PRESENCE_ZH[MET_NIXL_PRESENT])
    expect("有项：序列数 > 0", p_yes["nixl_matched_series_count"] > 0, True)
    expect("有项：**真 0 照实记成 0.0**（序列存在时 0 就是'一次都没失败'）",
           p_yes["nixl_series"][
               'vllm:nixl_num_failed_transfers{model_name="fake-model"}'], 0.0)
    expect("有项：字节数是解析出来的数（不是 0）",
           p_yes["nixl_series"][
               'vllm:nixl_bytes_transferred_total{model_name="fake-model",'
               'transfer_type="write"}'], 1.25e+09)

    # 只在 HELP/TYPE 注释里出现 → 仍然算"未出现"，但要在说明里点出来（不静默）
    expect("只在注释里提 NIXL：仍按'未出现'记", p_help["nixl_state"], MET_NIXL_ABSENT)
    expect("只在注释里提 NIXL：注释命中数 > 0", p_help["nixl_help_or_type_mentions"] > 0, True)
    expect("只在注释里提 NIXL：说明里点出了这件事（不静默）",
           "没有样本行" in p_help["nixl_note"], True)

    # 解析器本身：空标签与带标签两种形态都要认；数值解不出时留 None（**不填 0**）
    expect("解析：无标签行", p_no["series"]["process_cpu_seconds_total"], 43.59)
    expect("解析：带标签行的键含标签原文",
           "vllm:kv_cache_usage_perc" in p_no["series"] or
           'vllm:kv_cache_usage_perc{model_name="fake-model"}' in p_no["series"], True)
    p_nan = parse_metrics_text('m{a="1"} NaN\nm2 3.5\nbroken{a="1" 5\n')
    expect("解析：NaN 留 None（不填 0）", p_nan["series"]['m{a="1"}'] is None, True)
    expect("解析：不带标签的普通行", p_nan["series"]["m2"], 3.5)
    expect("解析：解不开的行计入 n_unparsable（不静默丢）", p_nan["n_unparsable_lines"], 1)

    # 版本白名单：python_info 的 version 是**解释器版本**，绝不能当服务版本
    vp = p_no["version_probe"]
    expect("版本：python_info 的 version 只进候选、不被采用",
           (vp["version"], len(vp["candidates"])), (None, 1))
    expect("版本：解释器版本确实在候选里（3.12.10）", vp["candidates"][0]["version"], "3.12.10")
    p_build = parse_metrics_text('vllm:build_info{version="0.31.0"} 1.0\n')
    expect("版本：build_info 的 version 被采用",
           p_build["version_probe"]["version"], "0.31.0")
    expect("版本：来源写明是 /metrics 的哪条指标",
           p_build["version_probe"]["source"].endswith("vllm:build_info"), True)
    expect("版本地址推导：/metrics → /version",
           derive_version_url("http://127.0.0.1:8090/metrics"), "http://127.0.0.1:8090/version")
    expect("版本地址推导：非法地址 → 空串（**不瞎猜路径**）",
           derive_version_url("not-a-url"), "")
    expect("抓取失败时版本也必须给字面值'未取到'（用空地址直接验）",
           fetch_version_once("", 0.5)["version"] is None, True)

    # 首末对比：只在两侧都有且都能解出数值时才算差；消失的序列**不当 0**
    d = diff_snapshots("a 1\nb 5\nc 3\nnixl_x 7\n",
                       "a 4\nb 5\nd 9\nnixl_x 7\n")
    got = {x["series"]: x["delta"] for x in d["top_deltas"]}
    expect("对比：a 的差 = 3", got.get("a"), 3.0)
    expect("对比：b 的差 = 0（真 0 照实报）", got.get("b"), 0.0)
    expect("对比：消失的 c 进 only_start、**不被当成 -3**",
           (d["n_only_start"], "c" in d["only_start_examples"]), (1, True))
    expect("对比：新增的 d 进 only_end、**不被当成 +9**",
           (d["n_only_end"], "d" in d["only_end_examples"]), (1, True))
    d2 = diff_snapshots("x 1\ny NaN\n", "x 2\ny 3\n")
    expect("对比：有一侧解不出数值 ⇒ 跳过、不拿 0 顶替",
           (d2["n_skipped_non_numeric"], len(d2["top_deltas"])), (1, 1))

    # 采样器：**未配置地址时一次抓取都不做**（这就是"整条路径不生效"的机械证据）
    with tempfile.TemporaryDirectory() as _td:
        s_none = build_metrics_summary(None, "", DEFAULT_METRICS_INTERVAL_S,
                                       DEFAULT_METRICS_TIMEOUT_S, "")
        expect("未配置：状态 = not_configured（**不是 nixl_absent**）",
               s_none["state"], MET_NOT_CONFIGURED)
        expect("未配置：NIXL 字段字面值 = 未配置地址（**不是'未出现'、更不是 0**）",
               s_none["nixl_presence_zh"], NIXL_PRESENCE_ZH[MET_NOT_CONFIGURED])
        expect("未配置：NIXL 序列值 None、尝试次数 0",
               (s_none["nixl_series_values"], s_none["n_attempts"]), (None, 0))
        expect("未配置：scope 限制写进了 summary（不只在文档里）",
               "不能分摊给单个请求" in s_none["scope_note"].replace("**", ""), True)
        smp = MetricsSampler(metrics_url="", interval_s=0.1, timeout_s=0.2,
                             version_url="", out_dir=Path(_td), run_id="t", t0=time.time())
        expect("未配置：采样器不会自己启动", smp._thread is None, True)
        expect("未配置：列表为空（没有产生任何快照文件）",
               (len(smp.attempts), len(list(Path(_td).glob("*")))), (0, 0))
        # 末次抓取失败、之前成功：NIXL 字段必须来自**最近一次成功的抓取**，
        # 否则会出现"状态=未出现、计数却是空的"这种自相矛盾（本文件真踩过类似的边界）。
        smp2 = MetricsSampler(metrics_url="http://x.invalid/metrics", interval_s=1.0,
                              timeout_s=0.2, version_url="", out_dir=Path(_td),
                              run_id="t2", t0=time.time())
        ok_rec = {"seq": 1, "phase": "periodic", "t_rel_s": 0.2, "body_bytes": 10,
                  "n_series": 11, "ok": True, "nixl_series_values": None,
                  "nixl_matched_series_count": 0, "nixl_note": "未出现：…",
                  "target_version": "v-test", "version_source": "s", "version_error": "",
                  "snapshot_file": "", "error_class": "", "error_text": ""}
        bad_rec = {"seq": 2, "phase": "end", "t_rel_s": 0.9, "ok": False,
                   "nixl_series_values": None, "nixl_matched_series_count": None,
                   "nixl_note": "抓取失败：…", "target_version": VERSION_UNAVAILABLE,
                   "version_source": "", "version_error": "boom", "error_class": ERR_TIMEOUT,
                   "error_text": "boom", "body_bytes": 0, "n_series": None,
                   "snapshot_file": ""}
        smp2.attempts = [ok_rec, bad_rec]
        smp2.snapshots = [ok_rec]
        s2 = build_metrics_summary(smp2, "http://x.invalid/metrics", 1.0, 0.2, "")
        expect("末次失败但之前成功：状态仍是未出现，且计数与状态自洽（不是空的）",
               (s2["state"], s2["nixl_matched_series_count"], s2["n_failed"], s2["n_ok"]),
               (MET_NIXL_ABSENT, 0, 1, 1))
        expect("末次失败但之前成功：失败原因照样留在 error_examples 里",
               len(s2["error_examples"]), 1)
    # 抓取失败那一态：拿一个**肯定连不上**的地址（保留端口用完立刻关掉）验一次真抓取。
    with socket.socket() as _s:
        _s.bind(("127.0.0.1", 0))
        _free = _s.getsockname()[1]
    r_fail = fetch_metrics_once(f"http://127.0.0.1:{_free}/metrics", 0.8)
    expect("抓取失败：ok=False、状态归到抓取失败那一类",
           (r_fail["ok"], bool(r_fail["error_class"])), (False, True))
    expect("抓取失败：留下错误原文（不是空串）", len(r_fail["error_text"]) > 5, True)
    r_empty = parse_metrics_text("")
    expect("空正文：解析不炸，且按'未出现'处理（调用方另按 200+空 归为抓取失败）",
           (r_empty["n_series"], r_empty["nixl_state"]), (0, MET_NIXL_ABSENT))

    print(f"\nselftest {'全部通过' if not fails else '有失败：' + ','.join(fails)}")
    return 0 if not fails else 1


# ===========================================================================
# 八、B0 验收电池
# ===========================================================================
def run_battery(args) -> int:
    """B0 的验收电池：本机假后端 + 客户端 + 覆盖判据，逐项给结论。

    两臂是**对照**：
      臂 A `overlap0`  —— KV 传输不与前处理重叠（"前处理完再传"）
      臂 B `overlap50` —— KV 传输有一半落在前处理尾部之内（逐层传 KV 的写照）
    预测（在跑之前就写下）：两臂都应当 **覆盖 PASS**；臂 A 的 `Σ语义阶段 − 端到端`
    接近 0，臂 B 的**明显 > 0**。若臂 B 仍然 ≈ 0，说明"重叠"这个设定在假后端里
    根本没生效（管道问题），而不是"重叠不存在"——所以这是一条真的对照，不是装饰。
    另加一次故障注入臂，只验证**错误分类**对不对（不参与覆盖判定）。

    第三臂 `emptychunks` 是**为"输出块 ≠ token"口径修正新增的对照**（检查项 C1–C6）：
    假后端掺进"有 token 但文本为空"的输出块，于是"块数 ≠ 内容块数"、"块间隔 ≠ 历史 tpot"
    这两件事在没有显卡的机器上也能被确定性地验证；这一臂还故意关掉 usage，
    用来验证"服务端不给 token 数时字段必须留空、不许拿块数冒充"。
    """
    run_id = _new_run_id("battery")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    checks: list[dict] = []

    def record(check: str, item: str, measured: str, verdict: bool | None, note: str = ""):
        checks.append({"check": check, "item": item, "measured": measured,
                       "verdict": ("PASS" if verdict else "FAIL") if verdict is not None
                       else "INFO", "note": note})
        tag = "PASS" if verdict else ("FAIL" if verdict is not None else "INFO")
        print(f"  [{tag:4}] {check} / {item}: {measured}" + (f"  ({note})" if note else ""),
              flush=True)

    def client_args(**over) -> argparse.Namespace:
        """电池里所有客户端调用的参数模板。**新增客户端参数必须加在这里**——
        漏一个键会让 `run_client` 直接 `AttributeError`（本文件为 `--model` 踩过一次，
        为口径修正加 `--include-usage` / `--return-token-ids` / `--prompt-text` 时又差点踩第二次）。

        `model` 写死 `"fake-model"`：电池永远打自己的假后端，不该因为用户给真服务传的
        模型名而改变行为。两个取证开关默认打开（与 `client` 角色的默认一致）。
        """
        base = dict(out_dir=str(out_dir), requests=args.requests, qps=args.qps,
                    arrival="uniform", concurrency=args.concurrency,
                    prompt_tokens=args.prompt_tokens, prompt_text="",
                    max_tokens=args.max_tokens, request_timeout=args.request_timeout,
                    cov_tol_s=args.cov_tol_s, save_tokens=args.save_tokens,
                    seed=args.seed, model="fake-model",
                    include_usage=True, return_token_ids=True,
                    # metrics 采集默认**关**：只有专门的 metrics 臂才打开它，
                    # 这样"不配就不生效"这件事在电池里也是默认路径。
                    metrics_url="", metrics_interval_s=DEFAULT_METRICS_INTERVAL_S,
                    metrics_timeout_s=DEFAULT_METRICS_TIMEOUT_S, version_url="")
        base.update(over)
        return argparse.Namespace(**base)

    print(f"=== B0 验收电池 {run_id} ===")
    print("\n--- S0：先自检判据本身（含会 FAIL 的负对照）")
    rc_self = run_selftest()
    record("S0", "selftest（判据自身）", f"返回码 {rc_self}（0=全通过）", rc_self == 0,
           "只验通过路径等于没验，所以自检里必须含负对照")

    arms: list[tuple[str, FakeBackendConfig, dict, str]] = []
    for label, overlap in (("overlap0", 0.0), ("overlap50", 0.5)):
        cfg = FakeBackendConfig(
            token_count=args.token_count, token_interval_s=args.token_interval_ms / 1000.0,
            queue_s=args.queue_ms / 1000.0, prefill_s=args.prefill_ms / 1000.0,
            kv_mb=args.kv_mb, kv_gbps=args.kv_gbps, kv_overlap_frac=overlap, seed=args.seed)
        port = args.port + (0 if overlap == 0.0 else 1)
        srv, _st = start_fake_backend(cfg, port)
        print(f"\n--- 臂 {label}：假后端 :{port}  "
              f"配方={json.dumps(cfg.to_dict(), ensure_ascii=False)}")
        try:
            rid = run_client(client_args(url=f"http://127.0.0.1:{port}", label=label))
        finally:
            srv.shutdown()
            srv.server_close()
        s = _read_summary(out_dir, rid)
        arms.append((label, cfg, s, rid))

    # ---- 逐项判定（只读落盘产物，不用内存态） ----
    tol = args.cov_tol_s
    print("\n=== 判定 ===")
    all_pass = True
    for label, cfg, s, rid in arms:
        cov = s["coverage"]
        ok_cov = all(v == 0 for k, v in s["coverage_verdicts"].items() if k != "PASS")
        all_pass &= ok_cov
        record("B1", f"{label} 成功率", f"{s['n_ok']}/{s['n_requests']}",
               s["n_ok"] == s["n_requests"] and s["n_requests"] > 0,
               "假后端不应丢请求；丢了说明心跳/连接复用有问题")
        record("B2", f"{label} TTFT 可测",
               f"p50={s['latency']['ttft_s']['p50']} s  p95={s['latency']['ttft_s']['p95']} s",
               s["latency"]["ttft_s"]["p50"] is not None,
               "TTFT = 请求开始 → 第一个内容片段")
        record("B3", f"{label} TPOT 可测",
               f"单请求p50口径：中位={s['latency']['tpot_p50_s']['p50']} s  "
               f"p95={s['latency']['tpot_p50_s']['p95']} s",
               s["latency"]["tpot_p50_s"]["p50"] is not None,
               "逐片段间隔的全量分位在 tokens.csv（--save-tokens）")
        record("B4", f"{label} 覆盖：客户端分解最大空档",
               f"{cov['cov_max_gap_s_max']} s（容差 {tol} s）",
               cov["cov_max_gap_s_max"] is not None and cov["cov_max_gap_s_max"] <= tol,
               "并集必须铺满 [请求开始, 请求结束]")
        record("B5", f"{label} leave-one-out 最小空档",
               f"{cov['cov_loo_min_gap_s_min']} s（下限 {cov['loo_eps_s']}）",
               cov["cov_loo_min_gap_s_min"] is not None
               and cov["cov_loo_min_gap_s_min"] > cov["loo_eps_s"],
               "每拿掉一段都应立刻露空档 ⇒ 每一段都真的被记了（判据不是空判据）")
        record("B6", f"{label} 服务端语义阶段覆盖",
               f"最大空档 {cov['srv_gap_max_s_max']} s，判定 {json.dumps(s['srv_verdicts'], ensure_ascii=False)}",
               cov["srv_gap_max_s_max"] is not None and cov["srv_gap_max_s_max"] <= tol,
               "queue→prefill→kv→decode 在服务端窗口内也要铺满")
        record("B7", f"{label} KV 字节数可取",
               f"{s['kv_bytes_per_request']} B/请求（配方 {cfg.kv_bytes} B）",
               s["kv_bytes_per_request"] is not None
               and abs(s["kv_bytes_per_request"] - cfg.kv_bytes) < 1.0,
               "假后端自报；真机要换成 vLLM 的 NIXL 指标（阶段 B1 待做）")

    sm0 = arms[0][2]["coverage"]["sum_minus_srv_span_s_median"]
    sm1 = arms[1][2]["coverage"]["sum_minus_srv_span_s_median"]
    se0 = arms[0][2]["coverage"]["sum_minus_e2e_s_median"]
    se1 = arms[1][2]["coverage"]["sum_minus_e2e_s_median"]
    ov1 = arms[1][2]["coverage"]["srv_overlap_s_median"]
    # 臂 B 的重叠量有**可算的预期值**：KV 传输时长 = kv 字节×8/速率，重叠量 = frac × 该时长。
    # 于是这条对照不是"看个符号"，而是"实测的重叠量对不对得上配方"——那才是真的对照。
    cfg_b = arms[1][1]
    predicted_overlap = cfg_b.kv_overlap_frac * cfg_b.kv_xfer_s
    # 让这条对照**在覆盖容差的分辨力之上**：若重叠量比 2×容差还小，
    # 就算它存在也会被 20.7 ms 的容差吞掉，看起来像"重叠不存在"——那是对照设计失败，不是结论。
    readable = predicted_overlap > 2 * tol
    record("B8", "对照①：无重叠臂 Σ语义阶段 − 服务端窗口", f"{sm0} s",
           sm0 is not None and abs(sm0) <= max(tol, 0.005),
           "无重叠时它本来就该接近 0（这是对照臂，不是验收判据）")
    record("B9", "对照②：50% 重叠臂 Σ语义阶段 − 服务端窗口",
           f"实测中位 {sm1} s，配方预期 {predicted_overlap:.6f} s",
           readable and sm1 is not None
           and abs(sm1 - predicted_overlap) <= max(1e-3, 0.05 * predicted_overlap),
           f"★ 关键对照：实测重叠量必须**对得上配方**（预期 {predicted_overlap:.6f} s）。"
           "重叠存在时'加总'必然大于窗口，所以阶段 A 的'加总等于端到端'"
           "在阶段 B 会**误判**——这正是本工具换判据的理由")
    record("B10", "对照③：两臂的覆盖结论都通过（与重叠无关）",
           f"臂A {arms[0][2]['coverage_verdicts']} / 臂B {arms[1][2]['coverage_verdicts']}",
           all_pass, "重叠只改变'之和'，不破坏'覆盖'——两类判据成功分离")
    record("B11", "对照④：Σ阶段 − 端到端（朴素口径，只报不判）",
           f"臂A 中位 {se0} s / 臂B 中位 {se1} s", None,
           "同一个量在两个臂上不同：说明它就是被重叠污染的，不能当判据")
    record("B12", "服务端语义阶段重叠量（臂 B）", f"中位 {ov1} s",
           ov1 is not None and ov1 > 0, "重叠确实发生了（否则 B9 的对照无意义）")

    # ================= 臂 C：输出块 ≠ token 的口径对照（为口径修正新增）=================
    # 假后端按 `--empty-text-every` 掺进"**有 token 但文本为空**"的输出块——真 vLLM 在
    # 中文/emoji 下就是这么干的。于是"块数 ≠ token 数"和"块间隔 ≠ 旧 tpot"这两件事，
    # **在没有显卡的机器上也能被确定性地验证**（配方是已知的，所以可以逐项对账）。
    # 这一臂还特意**关掉** usage（保留 return_token_ids），用来验证"服务端不给 token 数时
    # 那个字段必须留空，不许拿块数冒充"。
    n_tok_c = max(1, min(args.token_count, args.max_tokens))
    n_empty_expected = sum(1 for i in range(n_tok_c)
                           if i % BATTERY_EMPTY_EVERY == 0 and i != n_tok_c - 1)
    n_content_expected = n_tok_c - n_empty_expected
    print(f"\n--- 臂 emptychunks：假后端 :{args.port + 10}  每 {BATTERY_EMPTY_EVERY} 个内容块"
          f"掺 1 个空文本块（预期：输出块 {n_tok_c} 个 = 内容块 {n_content_expected}"
          f" + 空文本块 {n_empty_expected}）")
    cfg_c = FakeBackendConfig(
        token_count=args.token_count, token_interval_s=args.token_interval_ms / 1000.0,
        queue_s=args.queue_ms / 1000.0, prefill_s=args.prefill_ms / 1000.0,
        kv_mb=args.kv_mb, kv_gbps=args.kv_gbps, kv_overlap_frac=0.0, seed=args.seed,
        empty_text_every=BATTERY_EMPTY_EVERY)
    srv_c, _st_c = start_fake_backend(cfg_c, args.port + 10)
    try:
        rid_c = run_client(client_args(url=f"http://127.0.0.1:{args.port + 10}",
                                      label="emptychunks", save_tokens=True,
                                      include_usage=False))
    finally:
        srv_c.shutdown()
        srv_c.server_close()
    sc = _read_summary(out_dir, rid_c)
    ckc, stc = sc["chunks"], sc["server_tokens"]
    lat_c = sc["latency"]
    record("C1", "emptychunks 成功率与覆盖",
           f"成功 {sc['n_ok']}/{sc['n_requests']}，覆盖 {json.dumps(sc['coverage_verdicts'], ensure_ascii=False)}",
           sc["n_ok"] == sc["n_requests"] and sc["n_requests"] > 0
           and all(v == 0 for k, v in sc["coverage_verdicts"].items() if k != "PASS"),
           "空文本块必须被当成正常输出块：既不能丢，也不能破坏覆盖判据")
    record("C2", "★ 块数 ≠ token 数（块口径数得全）",
           f"输出块中位 {ckc['n_output_chunks_median']}（预期 {n_tok_c}）、"
           f"内容块中位 {ckc['n_content_chunks_median']}（预期 {n_content_expected}）、"
           f"空文本块中位 {ckc['n_empty_text_chunks_median']}（预期 {n_empty_expected}）",
           ckc["n_output_chunks_median"] == n_tok_c
           and ckc["n_content_chunks_median"] == n_content_expected
           and ckc["n_empty_text_chunks_median"] == n_empty_expected,
           "输出块 = 内容块 + 空文本块；旧口径只数后者，所以连'块数'都会数小")
    tok_csv_c = out_dir / f"{rid_c}_tokens.csv"
    n_rows = n_rows_empty = 0
    if tok_csv_c.exists():
        with tok_csv_c.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                n_rows += 1
                n_rows_empty += int(row.get("text_empty") or 0)
    record("C3", "块序列落盘（tokens.csv 每块一行）",
           f"行数 {n_rows}（预期 {sc['n_ok'] * n_tok_c}），其中 text_empty=1 的 {n_rows_empty} 行"
           f"（预期 {sc['n_ok'] * n_empty_expected}）",
           n_rows == sc["n_ok"] * n_tok_c
           and n_rows_empty == sc["n_ok"] * n_empty_expected,
           "块序列必须与 chunk_interval_* 对得上；空文本块也要留痕，否则事后没法核")
    ci = lat_c["chunk_interval_mean_s"]["p50"]
    tp = lat_c["tpot_mean_s"]["p50"]
    interval_s = args.token_interval_ms / 1000.0
    record("C4", "★ 块间隔 vs 历史 tpot（两种口径必须不同）",
           f"块间隔中位 {ci} s（配方 {interval_s} s）；历史 tpot 中位 {tp} s",
           ci is not None and abs(ci - interval_s) <= 0.25 * interval_s
           and tp is not None and tp > ci,
           "块口径数全部输出块 ⇒ 接近配方间隔；历史口径跳过空文本块 ⇒ 把两个间隔并成一个，偏大")
    record("C5", "★ 服务端不给 token 数时必须留空（不拿块数冒充）",
           f"有 usage 的请求 {stc['n_requests_with_usage']}/{sc['n_ok']}，"
           f"srv_completion_tokens 中位 {stc['srv_completion_tokens_median']}，"
           f"srv_token_ids_total 中位 {stc['srv_token_ids_total_median']}，"
           f"来源 {json.dumps(stc['srv_token_count_source_counts'], ensure_ascii=False)}",
           stc["n_requests_with_usage"] == 0
           and stc["srv_completion_tokens_median"] is None
           and stc["srv_token_ids_total_median"] == n_tok_c
           and stc["srv_token_count_source_counts"] == {"token_ids": sc["n_ok"]},
           "这一臂故意不发 stream_options.include_usage：usage 路径必须为空，"
           "而逐块 token_ids（服务端真的给了）照常可用")
    st_a = arms[0][2]["server_tokens"]
    record("C6", "臂 A：请求了 usage 就必须取到，且收尾片不算输出块",
           f"有 usage 的请求 {st_a['n_requests_with_usage']}/{arms[0][2]['n_ok']}，"
           f"srv_completion_tokens 中位 {st_a['srv_completion_tokens_median']}"
           f"（配方 token 数 {n_tok_c}），空文本块合计 {arms[0][2]['chunks']['n_empty_text_chunks_total']}",
           st_a["n_requests_with_usage"] == arms[0][2]["n_ok"]
           and st_a["srv_completion_tokens_median"] == n_tok_c
           and st_a["srv_token_count_source_counts"] == {"usage": arms[0][2]["n_ok"]}
           and arms[0][2]["chunks"]["n_empty_text_chunks_total"] == 0,
           "只有 usage 的收尾片带 choices=[]，**不是**输出块；把它算成'空文本块'就会凭空多 1")

    # ================= 臂 metrics：汇总快照的**四种状态**（为 4c 新增）=================
    # 这一臂回答"四种状态分不分得清"。四种都是必须能区分的：
    #   ① 未配置地址  ② 抓取失败  ③ 抓到指标但没有 NIXL 项（记"未出现"）  ④ 抓到 NIXL 项
    # 真 vLLM 0.31.0 在这个配置下看不到 NIXL（B0_VLLM_REPORT §3），所以 ④ **只能靠假指标服务验**；
    # 假服务里的指标值全是伪造的，只用来验状态机，不作为任何测量结论。
    # ① 直接复用臂 A 的产物：它的 client_args 没给 metrics_url —— 那就该什么都没有。
    s_a = arms[0][2]
    m_a = s_a["metrics_snapshot"]
    stray = sorted(p.name for p in out_dir.glob(f"{arms[0][3]}_scrape_*.json"))
    stray += sorted(p.name for p in out_dir.glob(f"{arms[0][3]}_metrics_scrapes.csv"))
    record("M1", "①未配置 --metrics-url：状态=未配置地址，且不多产任何文件",
           f"state={m_a['state']} / NIXL={m_a['nixl_presence_zh']} / 额外文件 {len(stray)} 个",
           m_a["state"] == MET_NOT_CONFIGURED
           and m_a["nixl_presence_zh"] == NIXL_PRESENCE_ZH[MET_NOT_CONFIGURED]
           and m_a["nixl_series_values"] is None and not stray,
           "★ 缺项不写成 0 的第一层：连'未出现'都不能写——**没采集就说'未配置地址'**；"
           "这条同时是'不配就不生效'的机械证据（一个文件都没多）")

    metric_cases = [
        # (标签, /metrics 变体, /metrics 故障, /version 形态, 期望状态, 是否起假指标服务)
        ("nonixl", "nonixl", "none", "ok", MET_NIXL_ABSENT, True),
        ("nixl", "nixl", "none", "ok", MET_NIXL_PRESENT, True),
        ("http500", "nonixl", "http_500", "ok", MET_SCRAPE_FAILED, True),
        ("refused", "nonixl", "none", "ok", MET_SCRAPE_FAILED, False),
    ]
    cfg_m = FakeBackendConfig(token_count=4, token_interval_s=0.005, queue_s=0.005,
                              prefill_s=0.01, kv_mb=1.0, kv_gbps=1.0, kv_overlap_frac=0.0,
                              seed=args.seed)
    srv_m, _st_m = start_fake_backend(cfg_m, args.port + 20)
    met_rows: list[dict] = []
    try:
        for k, (label, variant, fail_mode, vmode, expect_state, use_fake) in \
                enumerate(metric_cases):
            if use_fake:
                mcfg: MetricsBackendConfig | None = MetricsBackendConfig(
                    variant=variant, fail_mode=fail_mode, version_mode=vmode,
                    version=FAKE_METRICS_VERSION)
                mport = args.port + 30 + k
                msrv, _mst = start_fake_metrics(mcfg, mport)
                murl = f"http://127.0.0.1:{mport}/metrics"
            else:
                # 端口用完立刻关掉：这个地址**肯定没人监听**，用来造"抓取失败"（连接被拒）。
                with socket.socket() as _s:
                    _s.bind(("127.0.0.1", 0))
                    free = _s.getsockname()[1]
                mcfg, msrv, murl = None, None, f"http://127.0.0.1:{free}/metrics"
            print(f"\n--- 臂 metrics[{label}]：metrics-url={murl}  "
                  f"配方={json.dumps(mcfg.to_dict(), ensure_ascii=False) if mcfg else '(无服务)'}")
            try:
                rid_m = run_client(client_args(
                    url=f"http://127.0.0.1:{args.port + 20}", label=f"metrics_{label}",
                    requests=6, qps=10, concurrency=4, prompt_tokens=8, max_tokens=4,
                    metrics_url=murl, metrics_interval_s=0.25, metrics_timeout_s=1.0))
            finally:
                if msrv is not None:
                    msrv.shutdown()
                    msrv.server_close()
            sm = _read_summary(out_dir, rid_m)
            snap_files = sorted(out_dir.glob(f"{rid_m}_scrape_*.json"))
            snaps = [json.loads(p.read_text(encoding="utf-8")) for p in snap_files]
            csv_path = out_dir / f"{rid_m}_metrics_scrapes.csv"
            n_csv = 0
            if csv_path.exists():
                with csv_path.open(encoding="utf-8") as f:
                    n_csv = sum(1 for _ in csv.DictReader(f))
            met_rows.append({"label": label, "run_id": rid_m, "metrics_url": murl,
                             "backend": mcfg.to_dict() if mcfg else None,
                             "expect_state": expect_state, "summary": sm["metrics_snapshot"],
                             "snapshot_files": [p.name for p in snap_files], "n_csv_rows": n_csv})
            first = snaps[0] if snaps else {}
            ts = [x["t_rel_s"] for x in snaps]
            if label == "nonixl":
                record("M2", "③抓到指标但没有 NIXL 项 → 记「未出现」（不是 0）",
                       f"state={sm['metrics_snapshot']['state']} / "
                       f"NIXL={sm['metrics_snapshot']['nixl_presence_zh']} / "
                       f"序列值={json.dumps(sm['metrics_snapshot']['nixl_series_values'], ensure_ascii=False)}",
                       sm["metrics_snapshot"]["state"] == MET_NIXL_ABSENT
                       and sm["metrics_snapshot"]["nixl_presence_zh"] == NIXL_UNSEEN_TEXT
                       and sm["metrics_snapshot"]["nixl_series_values"] is None
                       and all(x["nixl_series_values"] is None for x in snaps),
                       "★ 本任务最关键的一条：假服务正文里**一条 NIXL 序列都没有**，"
                       "此时任何数字（尤其 0）都是编的")
                record("M3", "抓取是**按固定间隔**做的、带时间戳（含 start 与 end）",
                       f"尝试 {sm['metrics_snapshot']['n_attempts']} 次（成功 "
                       f"{sm['metrics_snapshot']['n_ok']} / 失败 {sm['metrics_snapshot']['n_failed']}），"
                       f"phase={[x['phase'] for x in snaps]}，t_rel={ts}",
                       len(snaps) >= 2 and ts == sorted(ts)
                       and snaps[0]["phase"] == "start" and snaps[-1]["phase"] == "end"
                       and all(x["utc"] for x in snaps) and n_csv == len(snaps),
                       "开局基线 + 收尾各一次 —— '整次运行前后'的对比靠的就是首末两点；"
                       "CSV 行数必须与快照文件数一致")
                record("M4", "目标版本被记进快照元数据，且**没把解释器版本当服务版本**",
                       f"target_version={sm['metrics_snapshot']['target_version']} "
                       f"来源={sm['metrics_snapshot']['version_source']}",
                       sm["metrics_snapshot"]["target_version"] == FAKE_METRICS_VERSION
                       and "/version" in sm["metrics_snapshot"]["version_source"]
                       and "3.12.10" not in str(sm["metrics_snapshot"]["target_version"]),
                       "假正文里故意放了一条 python_info{version=\"3.12.10\"}——"
                       "那是**解释器版本**，拿它当服务版本就是编数，实现里只记成候选")
                body = first.get("raw_body", "")
                sha_ok = bool(body) and hashlib.sha256(body.encode()).hexdigest() == \
                    first.get("body_sha256")
                record("M5", "原始正文与指纹都落了盘（可事后复核）",
                       f"首快照 {first.get('body_bytes')} 字节，sha256 复核"
                       f"{'一致' if sha_ok else '不一致'}；"
                       f"n_series={first.get('n_series')}，nixl_note={first.get('nixl_note')[:24]}…",
                       sha_ok and first.get("body_bytes") == len(body.encode())
                       and (first.get("n_series") or 0) > 0,
                       "快照存的是**原样正文**＋指纹：以后谁都可以重算一遍，不用信我们的转述")
                record("M6", "★「快照不能分摊给单个请求」这条限制写进了产物",
                       f"summary 与快照 JSON 都带 scope_note（{len(first.get('scope_note') or '')} 字符），"
                       f"CSV 列里也有 nixl_note",
                       "不能分摊给单个请求" in (first.get("scope_note") or "").replace("**", "")
                       and "不能分摊给单个请求"
                       in (sm["metrics_snapshot"]["scope_note"] or "").replace("**", ""),
                       "写在文档里会被忘掉；写进产物，读数的人绕不过去")
            elif label == "nixl":
                vals = sm["metrics_snapshot"]["nixl_series_values"] or {}
                zero_key = 'vllm:nixl_num_failed_transfers{model_name="fake-model"}'
                record("M7", "④抓到 NIXL 项 → 记「出现」，且**真 0 照实记成 0**",
                       f"state={sm['metrics_snapshot']['state']} / NIXL={sm['metrics_snapshot']['nixl_presence_zh']} / "
                       f"{len(vals)} 条序列，其中 num_failed_transfers={vals.get(zero_key)}",
                       sm["metrics_snapshot"]["state"] == MET_NIXL_PRESENT
                       and sm["metrics_snapshot"]["nixl_presence_zh"]
                       == NIXL_PRESENCE_ZH[MET_NIXL_PRESENT]
                       and vals.get(zero_key) == 0.0 and len(vals) >= 7,
                       "★ 与 M2 正好成对照：序列**存在**时 0 就是'确实一次都没失败'；"
                       "序列**不存在**时只能写'未出现'。两种 0 含义完全不同，所以必须分开记")
            elif label == "http500":
                errs = sm["metrics_snapshot"].get("error_examples") or []
                record("M8", "②抓取失败（HTTP 500）→ 状态=抓取失败，记下错误原文",
                       f"state={sm['metrics_snapshot']['state']}，"
                       f"错误原文={((errs[0]['error_text'] if errs else '') or '')[:60]!r}",
                       sm["metrics_snapshot"]["state"] == MET_SCRAPE_FAILED
                       and sm["metrics_snapshot"]["nixl_series_values"] is None
                       and bool(errs) and "500" in errs[0]["error_text"],
                       "★ 失败**不等于**'未出现'：没抓到正文时我们根本无从判断有没有 NIXL 项。"
                       "原文必须留下，否则事后只能猜是超时还是 5xx")
                record("M9", "采集失败**不影响测量**（请求照常全成功）",
                       f"成功 {sm['n_ok']}/{sm['n_requests']}，"
                       f"覆盖 {json.dumps(sm['coverage_verdicts'], ensure_ascii=False)}",
                       sm["n_ok"] == sm["n_requests"] and sm["n_requests"] > 0,
                       "采集是旁路：抓不到指标是服务端的事，不该让测量跟着失败")
            elif label == "refused":
                record("M10", "②抓取失败（连接被拒）→ 状态=抓取失败，错误原文非空",
                       f"state={sm['metrics_snapshot']['state']} / "
                       f"{json.dumps(sm['metrics_snapshot'].get('error_examples') or [], ensure_ascii=False)[:90]}",
                       sm["metrics_snapshot"]["state"] == MET_SCRAPE_FAILED
                       and bool(sm["metrics_snapshot"].get("error_examples"))
                       and bool(sm["metrics_snapshot"]["error_examples"][0]["error_text"]),
                       "连不上与 5xx 都归'抓取失败'，但原文不同——原文比分类更能定位问题")
                record("M11", "版本取不到 → 记「未取到」＋原因（**不编**）",
                       f"target_version={sm['metrics_snapshot']['target_version']!r} "
                       f"原因={sm['metrics_snapshot']['version_error'][:60]!r}",
                       sm["metrics_snapshot"]["target_version"] == VERSION_UNAVAILABLE
                       and bool(sm["metrics_snapshot"]["version_error"]),
                       "这条路径在真机上会遇到（/version 不存在、被网关挡掉），"
                       "所以宁可记'未取到'也不许拿别的字段顶替")
    finally:
        srv_m.shutdown()
        srv_m.server_close()

    # ---- 故障注入：只验错误分类（**三种失败方式都要过**，因为它们在阶段 C 里
    #      对应完全不同的排查动作：5xx 查服务端、断流查中间层、超时查隧道/负载） ----
    fail_results: list[dict] = []
    for k, mode in enumerate(args.fail_modes):
        print(f"\n--- 臂 failinject[{mode}]：故障注入（只验错误分类，不参与覆盖判定）")
        cfg = FakeBackendConfig(token_count=8, token_interval_s=0.02, queue_s=0.005,
                                prefill_s=0.01, kv_mb=1.0, kv_gbps=1.0,
                                fail_rate=1.0, fail_mode=mode, seed=args.seed)
        port = args.port + 2 + k
        srv, _st = start_fake_backend(cfg, port)
        try:
            rid = run_client(client_args(url=f"http://127.0.0.1:{port}",
                                        label=f"failinject_{mode}", requests=6, qps=6,
                                        concurrency=4, prompt_tokens=8, max_tokens=8,
                                        request_timeout=args.fail_timeout_s,
                                        save_tokens=False))
        finally:
            srv.shutdown()
            srv.server_close()
        fs = _read_summary(out_dir, rid)
        got = fs["error_counts"]
        expect_class = {"http_500": ERR_HTTP_5XX,
                        "disconnect_mid_stream": ERR_STREAM_INCOMPLETE,
                        "truncate_clean": ERR_STREAM_INCOMPLETE,
                        "reset_mid_stream": ERR_STREAM_BROKEN,
                        "hang": ERR_TIMEOUT}[mode]
        record("B13", f"错误分类（{mode} → 期望 {expect_class}）",
               f"失败 {fs['n_err']}/{fs['n_requests']}，分类={json.dumps(got, ensure_ascii=False)}",
               fs["n_err"] == fs["n_requests"] and got.get(expect_class, 0) == fs["n_requests"],
               "必须全部失败且归到期望类别；空类别或串类说明分类逻辑没生效")
        fail_results.append({"mode": mode, "expected_class": expect_class,
                             "run_id": rid, "summary": fs})

    # ---- 落盘 ----
    checks_csv = out_dir / f"{run_id}_checks.csv"
    with checks_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["check", "item", "measured", "verdict", "note"])
        w.writeheader()
        w.writerows(checks)
    detail = {
        "run_id": run_id, "utc": datetime.now(timezone.utc).isoformat(),
        "kind": "measure_serving/battery",
        "host": host_info(), **code_version(),
        "cov_tol_s": args.cov_tol_s, "loo_eps_s": LOO_EPS_S,
        "checks": checks,
        "arms": [{"label": lbl, "run_id": rid, "backend": cfg.to_dict(), "summary": s}
                 for lbl, cfg, s, rid in arms],
        "arm_emptychunks": {"label": "emptychunks", "run_id": rid_c,
                            "backend": cfg_c.to_dict(), "summary": sc,
                            "expected": {"n_output_chunks": n_tok_c,
                                         "n_content_chunks": n_content_expected,
                                         "n_empty_text_chunks": n_empty_expected,
                                         "empty_text_every": BATTERY_EMPTY_EVERY}},
        "failinject": fail_results,
        "arm_metrics": met_rows,
        "client_summaries": [f"{rid}_summary.json" for _, _, _, rid in arms] +
                            [f"{rid_c}_summary.json"] +
                            [f"{fr['run_id']}_summary.json" for fr in fail_results] +
                            [f"{row['run_id']}_summary.json" for row in met_rows],
    }
    (out_dir / f"{run_id}_battery.json").write_text(
        json.dumps(detail, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    n_pass = sum(1 for c in checks if c["verdict"] == "PASS")
    n_fail = sum(1 for c in checks if c["verdict"] == "FAIL")
    n_info = sum(1 for c in checks if c["verdict"] == "INFO")
    print(f"\n=== 电池结果：PASS {n_pass} / FAIL {n_fail} / INFO {n_info} ===")
    print(f"产物 -> {checks_csv}")
    return 0 if n_fail == 0 else 1


def _read_summary(out_dir: Path, run_id: str) -> dict:
    """按 run_id 读回落盘的 summary（判定只用落盘产物，避免依赖内存里的临时对象）。"""
    p = out_dir / f"{run_id}_summary.json"
    if not p.exists():
        raise RuntimeError(f"没找到 {p}（客户端没有留下 summary）")
    return json.loads(p.read_text(encoding="utf-8"))


# ===========================================================================
# 九、入口
# ===========================================================================
def main() -> int:
    ap = argparse.ArgumentParser(
        description="阶段 B0：引擎无关的 OpenAI 兼容服务逐请求测量（含假后端）")
    ap.add_argument("role", choices=["selftest", "fake-backend", "fake-metrics", "client",
                                     "battery"])

    g = ap.add_argument_group("假后端配方（--role fake-backend / battery）")
    g.add_argument("--port", type=int, default=9500)
    g.add_argument("--bind", default="127.0.0.1")
    g.add_argument("--token-count", type=int, default=32, help="每个请求吐多少个内容块")
    g.add_argument("--token-interval-ms", type=float, default=20.0,
                   help="相邻输出块的间隔（毫秒）；假后端按它逐块吐")
    g.add_argument("--empty-text-every", type=int, default=0,
                   help="每 N 个内容块里掺 1 个'文本为空'的输出块（0＝不掺）。"
                        "真 vLLM 在中文/emoji 下会这么干；掺上它就能在**没有显卡**的情况下"
                        "验证'块数 ≠ 内容块数'与'块间隔 ≠ 旧 tpot'这两件事")
    g.add_argument("--queue-ms", type=float, default=10.0, help="排队等待（伪）")
    g.add_argument("--prefill-ms", type=float, default=100.0, help="前处理计算（伪）")
    g.add_argument("--kv-mb", type=float, default=24.0, help="KV 传输量（伪，MiB）")
    g.add_argument("--kv-gbps", type=float, default=3.2,
                   help="KV 链路速率（伪，Gbit/s）；传输时长由字节÷速率推出，二者互相自洽")
    g.add_argument("--kv-overlap-frac", type=float, default=0.0,
                   help="KV 传输与前处理尾部的重叠比例（0＝前处理完再传；0.5＝逐层传的写照）")
    g.add_argument("--fail-rate", type=float, default=0.0)
    g.add_argument("--fail-mode", choices=FAIL_MODES, default="http_500")
    g.add_argument("--fail-modes", nargs="+", choices=FAIL_MODES, default=list(FAIL_MODES),
                   help="battery 角色要逐项验证的失败方式（四种都要过：它们在阶段 C 里"
                        "对应不同的排查动作——5xx 查服务端、断流查中间层/连接、"
                        "截断查服务端收尾逻辑、超时查隧道与负载）")
    g.add_argument("--fail-timeout-s", type=float, default=2.0,
                   help="故障注入臂的 socket 超时（hang 模式靠它触发 timeout 分类）")

    fm = ap.add_argument_group("假指标服务（--role fake-metrics）")
    fm.add_argument("--metrics-variant", choices=["nonixl", "nixl", "nixl_help_only"],
                    default="nonixl",
                    help="正文里放不放 NIXL 序列。nonixl＝一条都没有；nixl＝有一组；"
                         "nixl_help_only＝只在 HELP/TYPE 注释里出现（用来验证"
                         "'声明了但没有样本'不会被当成'抓到 NIXL 项'）")
    fm.add_argument("--metrics-fail", choices=["none", "http_500", "http_404", "empty_200",
                                               "hang"], default="none",
                    help="/metrics 的故障注入（验证'抓取失败'那一态）")
    fm.add_argument("--version-mode", choices=["ok", "plain", "missing", "http_500",
                                              "garbage"], default="ok",
                    help="/version 的形态：ok=JSON 带 version 字段；plain=纯文本；"
                         "missing=404；http_500=5xx；garbage=JSON 但**没有** version 字段"
                         "（用来验证'取不到就记未取到'，不许猜）")
    fm.add_argument("--metrics-version", default="fake-vllm-0.0.1-test",
                    help="假服务自报的版本字符串（**是伪造的**，只用于验证记录链路）")

    c = ap.add_argument_group("客户端（--role client / battery）")
    c.add_argument("--url", default="http://127.0.0.1:9500")
    c.add_argument("--model", default="fake-model",
                   help="请求体里的 model 名。**打真服务时必须填对**：vLLM 对不存在的模型名"
                        "返回 HTTP 404（'The model `x` does not exist.'），而假后端不看这个字段，"
                        "所以默认值只在打假后端时可用。")
    c.add_argument("--label", default="run", help="本次运行的标签（写进每行 CSV）")
    c.add_argument("--requests", type=int, default=120)
    c.add_argument("--qps", type=float, default=12.0, help="到达率（开环：不等上一个完成）")
    c.add_argument("--arrival", choices=["uniform", "poisson"], default="uniform")
    c.add_argument("--concurrency", type=int, default=16,
                   help="在途数的**参考上限**：超了不阻塞，只把 overloaded 标 1，"
                        "以保住开环语义（阻塞会变成'client 自己限流'，测量对象就消失了）")
    c.add_argument("--prompt-tokens", type=int, default=64)
    c.add_argument("--prompt-text", default="",
                   help="用这段字面提示词代替 'x '×--prompt-tokens。测中文/emoji 提示词时用"
                        "（那正是'输出块 ≠ token'最容易出现的地方）")
    c.add_argument("--max-tokens", type=int, default=32)
    c.add_argument("--request-timeout", type=float, default=30.0)
    c.add_argument("--no-include-usage", dest="include_usage", action="store_false",
                   help="不发 stream_options.include_usage（默认发）。关掉只是**拿不到**"
                        "服务端自报的 completion_tokens，块口径的字段一个都不受影响")
    c.add_argument("--no-return-token-ids", dest="return_token_ids", action="store_false",
                   help="不发 return_token_ids（默认发）。关掉只是**拿不到**逐块 token 数，"
                        "块口径的字段一个都不受影响")
    c.add_argument("--cov-tol-s", type=float, default=DEFAULT_COV_TOL_S,
                   help="覆盖判据允许的最大空档（秒）。默认 5 ms：明显大于本机 "
                        "time.time() 的 ~1 ms 分辨率，又明显小于任何阶段时长（≥10 ms）")
    c.add_argument("--save-tokens", action="store_true",
                   help="另存每**输出块**的到达时刻（文件名沿用 _tokens.csv，字段含义见文件头）")
    c.add_argument("--seed", type=int, default=1)
    c.add_argument("--metrics-url", default="",
                   help="**可选**的 /metrics 地址（例如 http://127.0.0.1:8090/metrics）。"
                        "不配它就完全不走采集路径、行为与加这个参数之前一字不差；"
                        "配了就按 --metrics-interval-s 固定间隔抓取，原始正文与带时间戳的"
                        "元数据落盘到 --out-dir（append-only）。"
                        "采集结果分四态：未配置地址 / 抓取失败 / 成功抓到但没有 NIXL 项"
                        "（记作'未出现'，**不是 0**）/ 抓到 NIXL 项。"
                        "⚠️ 快照只用于比较整次运行前后的汇总变化，**不能分摊给单个请求**。"
                        "注意：`battery` 角色会忽略这个参数——它用自己的假指标服务跑四态臂")
    c.add_argument("--metrics-interval-s", type=float, default=DEFAULT_METRICS_INTERVAL_S,
                   help="固定抓取间隔（秒）。开局先抓一次(start)、结束后再抓一次(end)，"
                        "中间每隔这么久抓一次(periodic)——'运行前后'的对比靠首末两点")
    c.add_argument("--metrics-timeout-s", type=float, default=DEFAULT_METRICS_TIMEOUT_S,
                   help="单次抓取的超时（秒）。抓取在独立线程里，超时不会拖慢请求；"
                        "失败按'抓取失败'记，并留下错误原文")
    c.add_argument("--version-url", default="",
                   help="取服务版本的地址；默认由 --metrics-url 推导成 scheme://host/version"
                        "（vLLM 的版本端点就是 /version）。取不到就记'未取到'并留下错误原文，"
                        "**不许猜**——/metrics 里的 python_info 版本是解释器版本，不算服务版本")
    c.add_argument("--out-dir", default=DEFAULT_OUT_DIR,
                   help="产物目录（append-only：文件名带 run_id，不覆盖）")
    args = ap.parse_args()

    if args.role == "selftest":
        return run_selftest()
    if args.role == "fake-backend":
        return run_fake_backend(args)
    if args.role == "fake-metrics":
        return run_fake_metrics(args)
    if args.role == "client":
        run_client(args)
        return 0
    return run_battery(args)


if __name__ == "__main__":
    raise SystemExit(main())

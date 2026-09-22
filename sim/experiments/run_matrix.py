"""主实验矩阵运行器 —— 论文规划 §6.1 的完整实验。

矩阵：
    4 类负载 × 3 种网络 × 2 种状态新鲜度 × 4 种策略 × N 个种子
    = 4 × 3 × 2 × 4 × 5 = 480 次运行

关键效率设计：
    同一个 cell（负载×网络×陈旧×种子）下的 4 种策略共用【同一份负载】，
    在内存里一次跑完，立刻做配对检验并只把统计量写盘。
    这样既保证配对合法性（req_id 一一对应），
    又不需要为 480 次运行各存一份每请求 CSV（那样是 GB 级）。

产出：
    runs.csv          每个 (cell, 策略) 一行的聚合指标
    paired.csv        每个 (cell, 基线) 一行的配对检验结果
    manifest.json     完整配置、代码版本、计时

额外实验（论文规划 §6.3）：
    --mode convergence   3/5/10/20 种子收敛性
    --mode ablation      权重消融 ±50%，同时是门 2 的整轮验证点
    --mode scale         节点规模 4/8/16
    --mode rho           到达压力（负载强度）扫描：收益边界必须在这条轴上也成立
    --mode fallback      RQ2 回退机制：同 cell 内同时跑"不回退"参考臂与各阈值变体

负载来源（--source）：
    --source synthetic   合成负载（manifest 的 workload_stats 标为 SYNTHETIC）
    --source trace       真实 trace（--trace 指向的文件按表头自动识别）
                         Azure LLM Inference Trace 2024 / BurstGPT 都支持

    接真实 trace 时有三处必须与合成路径口径一致，否则结果不可用：

    1. **到达率按目标 rho 标定**（门 1）。合成路径原先给四类负载同一个
       target_qps=30，而四类负载的每请求工作量差一个量级，于是 data_heavy
       跑到 rho=1.83（过饱和）、low_latency 跑到 0.054（近乎空闲），
       这两类的策略对比不可采信（results/analysis/load_intensity_rho.csv 已记录）。
       现在两条路径都改为：先量出【真正被仿真的那一段】的平均工作量与局部到达率，
       再反解 qps，使四类负载、所有 mode 都落在同一个目标 rho 上，
       负载强度不再成为混杂因子。到达时间只整体乘一个 scale，突发结构保留。

    2. **每个种子必须对应不同的 trace 时间片**。trace 只有一条时间轴，
       若所有种子共用同一份请求流，确定性策略的种子间方差恒为 0
       （门 3 的推论），加种子是纯粹的算力浪费。这里把 trace 按时间等分，
       第 s 个种子用第 s 片，让方差来源回到真实的负载实现上。

    3. **合成负载的 token 形状与 trace 差一个量级**（trace 的 GeneratedTokens
       p50 只有 8，合成是 10^4.5≈90），两批结论不可混着引用。

产物（两批完全隔离，互不覆盖）：
    --out results/matrix_trace      trace 的 runs/paired/manifest
    analysis/analyze.py --matrix-dir results/matrix_trace \
        --table-dir results/analysis_trace --fig-dir figures_trace
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

from src.metrics import summarize                      # noqa: E402
from src.model import NodeSpec, compute_work, request_bytes  # noqa: E402
from src.network import LinkState, StaticNetwork, UnstableNetwork  # noqa: E402
from src.simulator import SimConfig, run               # noqa: E402
from src.strategies import (                           # noqa: E402
    LeastLoadStrategy,
    LeastQueueStrategy,
    NetworkAwareContentionStrategy,
    NetworkAwareStrategy,
    RandomStrategy,
)
from src.trace_loader import load_azure_trace, load_burstgpt  # noqa: E402
from src.workload import (                             # noqa: E402
    WORKLOAD_CLASSES,
    build_from_trace,
    build_synthetic,
)

# --- 网络条件（论文规划 §6.1 的 3 类）--------------------------------------
# 每条链路是 (单程时延 s, 带宽 B/s)。带宽换算：1 Gbps = 1.25e8 B/s。
NORMAL = (0.0005, 1.25e9)     # 0.5 ms, 10 Gbps
MID = (0.002, 1.25e8)         # 2 ms,   1 Gbps
DEGRADED = (0.005, 1.25e7)    # 5 ms,   100 Mbps

NETWORK_CONDITIONS = {
    # 正常：全网同构且快。网络感知在这里【应该没有信号】—— 这是负对照。
    "normal": {"kind": "static", "links": [NORMAL, NORMAL, NORMAL]},
    # 退化：高时延低带宽，且节点间异构。
    "degraded": {"kind": "static", "links": [DEGRADED, MID, NORMAL]},
    # 抖动/不稳定：时变，且节点间异构。RQ2 只有在这类网络上才有研究对象。
    "unstable": {
        "kind": "unstable",
        "links": [MID, MID, NORMAL],
        "params": {"theta": 0.5, "sigma_b": 0.8, "sigma_l": 0.4, "dt": 0.2},
    },
}

# --- v2 网络档位：真实 AI 集群量级 -------------------------------------------
# 为什么必须换：v1 的 10 Gbps / 1 Gbps / 100 Mbps 比机内网络低 80–8000×
# （DistServe：OPT-66B 在 10 rps 下需要 90 Gbps 才让 KV 传输开销"不可见"；
#  机内是 800 Gbps InfiniBand；MegaScale 是 400 Gbps 端口）。v1 那样"链路不排队"
# 时这只是"网络很贵"；一旦按 v2 给链路加上 FIFO 排队，0.1–10 Gbps 会直接变成
# **容量瓶颈**——实测 p95 冲到 76 万秒、系统根本跑不动。
# v2 取 25/100/400 Gbps（慢/中/快，比例 1:4:16），配合 kv_cache 的 330 KB/token：
# 27 QPS 下网络利用率约 0.26、算力利用率 0.6 → **两条队列同时有意义**，
# 这才是"算力与网络联合调度"真正的问题形态。时延取 µs 级（DCQCN 实测 RDMA 1.7–2.8 µs）。
V2_FAST = (1.0e-6, 5.0e10)      # 400 Gbps, 1 µs
V2_MID = (1.5e-6, 1.25e10)      # 100 Gbps, 1.5 µs
V2_SLOW = (2.0e-6, 3.125e9)     # 25 Gbps, 2 µs
NETWORK_CONDITIONS_V2 = {
    "normal": {"kind": "static", "links": [V2_FAST, V2_FAST, V2_FAST]},
    # 异构顺序与 v1 一致：node 0（算力最强）配最慢链路 → 保留"快算力/慢链路"的权衡
    "degraded": {"kind": "static", "links": [V2_SLOW, V2_MID, V2_FAST]},
    "unstable": {
        "kind": "unstable",
        "links": [V2_MID, V2_MID, V2_FAST],
        "params": {"theta": 0.5, "sigma_b": 0.8, "sigma_l": 0.4, "dt": 0.2},
    },
}
# 当前生效的网络档位集合，由 --net-set 覆盖
NET_SET = "v1"

# --- 状态新鲜度（论文规划 §6.1 的 2 类）------------------------------------
# 两条通道分别设定：队列状态刷新快（亚秒级监控），链路状态刷新慢（几十秒级）。
# "陈旧"档取 queue=2s / network=30s，是工程上可辩护的量级；
# 更细的曲线由 --mode staleness 扫描给出。
STALENESS = {
    "fresh": {"queue": 0.0, "network": 0.0},
    "stale": {"queue": 2.0, "network": 30.0},
    # 回退机制那一节专用：加一档"轻微陈旧"。回退的代价是"在还有信号时把它关掉"，
    # 只有在陈旧度还没大到让网络项失效的档位上，才可能观察到"回退过头损失收益"。
    # 注意观测年龄 = max(队列 lag, 链路 lag)（调度器看到的是两条通道里更旧的那个快照），
    # 所以这一档的年龄是 3 s 而不是 1 s。
    "mild": {"queue": 1.0, "network": 3.0},
}

# 到达压力扫描的档位（rho 模式）。下端接近门 1 的下沿（几乎不排队 → 四策略必然趋同），
# 上端接近过饱和。收益边界必须在这条轴上给出形状，而不是一个点。
RHO_GRID = [0.30, 0.45, 0.60, 0.75, 0.90]

# 主矩阵的新鲜度档位：**只取 fresh / stale**，与 `论文规划.md` §6.1 冻结的设计一致
# （4 负载 × 3 网络 × 2 新鲜度 × 5 种子 = 120 cells = 480 次运行）。
# `mild` 档是回退机制实验（--mode fallback）专用的中间档，**不要**让它进主矩阵：
# 它会悄悄把主矩阵变成 180 cells，三个批次之间也就不可比了（已实际发生一次）。
MAIN_STALENESS_LEVELS = ["fresh", "stale"]

# 回退触发阈值（秒）。判据是 `观测年龄 > 阈值`，注意两条：
#   1. **观测年龄 = max(队列 lag, 链路 lag)**——策略看到的是两条通道里更旧的那个快照
#      （`Observation.observed_at = min(t_q, t_n)`），不是更新鲜的那个。
#      按 min 理解会把阈值网格整个取错，让"阈值 45 s 也触发回退"这种结论凭空出现。
#   2. 阈值不能正好等于某一档的年龄，否则触发与否由浮点误差决定
#      （实测：阈值与年龄都是 2.0 时，`now-(now-2.0)` 算出 2.0000000000000004，回退被意外触发）。
# 三档年龄分别是 0 / 3 / 30 s，因此取 0.5 / 1.5 / 5 / 45：
#   fresh（0 s）：全部不触发 → 天然负对照（回退应是空操作）
#   mild （3 s）：0.5、1.5 触发；5、45 不触发
#   stale（30 s）：0.5、1.5、5 触发；45 不触发
FALLBACK_THRESHOLDS = [0.5, 1.5, 5.0, 45.0]
FALLBACK_STALENESS_LEVELS = ["fresh", "mild", "stale"]

# --- 带宽敏感性档位（--mode bandwidth）---------------------------------------
# 保真度核对（AGENTS.md §6 门 4）发现：本模型的链路比真实 AI 集群机内网络低 80×–8000×
# （DistServe：OPT-66B 在 10 rps 下需要 90 Gbps 才能让 KV 传输开销"不可见"，
#  并指出现代 GPU 集群用 800 Gbps InfiniBand；DCQCN 实测 RDMA 单程 1.7–2.8 µs）。
# 后果是本批 net_share 达 0.35–0.76，而机内 KV 传输占比是 1–5% 量级。
#
# 【顺序约定，写错就毁掉整组实验】links[0] 配 node 0（算力最强、16000 units/s），
# 而 links[0] 必须是**最慢**的那条链路——"快算力 + 慢链路 vs 慢算力 + 快链路"这个
# 权衡正是网络感知调度唯一的用武之地。与 `degraded` 保持完全相同的顺序与 100:10:1 比例，
# 只改绝对尺度与配套时延，这样变的只有"网络有多贵"这一个因子。
# （2026-09-22 这里写反过一次：档位里 links[0] 给了最快链路，让节点 0 在两个维度上
#  都最强、权衡消失，整组敏感性实验作废重跑。）
BANDWIDTH_PROFILES = {
    # 全 ≥100 Gbps 的真实机内档：慢链路 100 / 中 200 / 快 800 Gbps，时延 10/5/2 µs
    "ib_fast": [(1.0e-5, 1.25e10), (5e-6, 2.5e10), (2e-6, 1.0e11)],
    # InfiniBand 级但慢端只有 8 Gbps：8 / 80 / 800 Gbps，时延 5/2/0.5 µs
    "ib_800g": [(5e-6, 1.0e9), (2e-6, 1.0e10), (5e-7, 1.0e11)],
    # 以太机内档：1 / 10 / 100 Gbps，时延 10/5/2 µs
    "eth_100g": [(1.0e-5, 1.25e8), (5e-6, 1.25e9), (2e-6, 1.25e10)],
    # 当前模型所用的"跨域/算力网络"档：0.1 / 1 / 10 Gbps，时延 5/2/0.5 ms
    # ——与 NETWORK_CONDITIONS["degraded"] 逐位相同，可用来交叉验证档位管道是否正确
    "wan_10g": [(5e-3, 1.25e7), (2e-3, 1.25e8), (5e-4, 1.25e9)],
    # 更极端的广域档：0.01 / 0.1 / 1 Gbps，时延 50/20/5 ms
    "wan_1g": [(5e-2, 1.25e6), (2e-2, 1.25e7), (5e-3, 1.25e8)],
}

# --- v2 专用的带宽敏感性档位 ------------------------------------------------
# v1 的 BANDWIDTH_PROFILES 覆盖 0.01–800 Gbps，是为"链路不排队"设计的；在 v2 的
# 链路争用 + 可行域标定下，慢档会让算力利用率掉到 0.0023（违反门 1 下界）。
# 根本约束：~900 MB/请求下，慢链路 < ~23 Gbps 时"两条资源都达到 rho≥0.3"不可行。
# 所以 v2 只扫**可行带内**的档位：慢端 200/100/50/25 Gbps（比例保持 1:2:4 与 v2 默认一致），
# 观察"慢链路从宽裕到成为热点"这一段里机制增益如何变化。
V2_SLOW25 = (2.0e-6, 3.125e9)
BANDWIDTH_PROFILES_V2 = {
    "slow_200g": [(1.0e-5, 2.5e10), (5e-6, 5.0e10), (2e-6, 1.0e11)],
    "slow_100g": [(1.0e-5, 1.25e10), (5e-6, 2.5e10), (2e-6, 5.0e10)],
    "slow_50g": [(1.5e-5, 6.25e9), (7e-6, 1.25e10), (2e-6, 2.5e10)],
    "slow_25g": [V2_SLOW25, (1.5e-6, 1.25e10), (1e-6, 5.0e10)],
}


def active_bandwidth_profiles() -> dict:
    """当前档位集合下的带宽扫描档位（v2 用可行带内的那一套）。"""
    return BANDWIDTH_PROFILES_V2 if NET_SET == "v2" else BANDWIDTH_PROFILES


# cell 里这些键只服务于调度，不写进 runs.csv（否则会变成字符串化的列表，污染证据）
CELL_META_KEYS = {"strategies", "pair_candidates", "pair_baselines"}

# 陈旧度扫描的水平（秒），用于给出 RQ2 的完整曲线而不是两个点
STALENESS_SWEEP = [0.0, 0.25, 1.0, 2.0, 5.0, 15.0, 30.0, 60.0]

# --- 数据量：决定网络感知值不值的量级开关 ----------------------------------
# 论文必须报告两条曲线：共置架构（只传 prompt）与分离式架构（传 KV cache）。
DATA_VOLUME = {
    "prompt_only": 4.0,        # 4 B/token：只传 token id
    "kv_cache": 3.3e5,         # 330 KB/token：70B 量级 KV cache
}
DEFAULT_DATA_VOLUME = "kv_cache"

# --- 真实 trace（--source trace）-------------------------------------------
# 完整 1 周文件约 110 MB / 144 万请求；只下了前 50 MB 也能跑，只是覆盖时长变短。
TRACE_PATH = ROOT / "data/raw/azure_code_1week.csv"

# 目标负载强度：四类负载共用同一个值，避免"类间差异"被"负载强度差异"混淆（门 1）。
# 0.6 取在有效区间 [0.3, 0.9] 的中段。合成路径不参与这个标定（它的 target_qps=30
# 会把 data_heavy 推到 rho=1.83、low_latency 压到 0.054，见模块 docstring）。
TARGET_RHO = 0.6

# 负载来源：main() 按 --source 覆盖，cell() 把它写进每个 cell，
# 使合成与 trace 的行在 runs.csv 里可区分。
SOURCE = "synthetic"

# 合成路径写进 paired.csv 的 workload_stats 标签。analyze.detect_data_source 靠
# "SYNTHETIC" 这个词判断负载来源，改这个字符串会静默改变来源判定。
SYNTHETIC_STATS_LABEL = "SYNTHETIC (非真实数据，仅用于打通管线)"

# trace 的时间片与其体检结果，由 main() 在 --source trace 时填充。
_TRACE_SEGMENTS: list[list] = []
_TRACE_STATS = None

# 主实验的策略表：v2 加入争用感知策略 NetworkAware2（机制贡献）。
# v1 的四策略表保留，使 v1/v2 两批可对照。
BASE_STRATEGIES_V1 = ["Random", "LeastQueue", "LeastLoad", "NetworkAware"]
BASE_STRATEGIES_V2 = BASE_STRATEGIES_V1 + ["NetworkAware2"]
BASE_STRATEGIES = BASE_STRATEGIES_V1      # 兼容旧调用点


def base_strategies() -> list[str]:
    """当前档位集合下每个 cell 要跑的策略（v2 多一个 NetworkAware2）。"""
    return BASE_STRATEGIES_V2 if NET_SET == "v2" else BASE_STRATEGIES_V1


SEEDS = [1, 2, 3, 4, 5]
N_REQUESTS = 20000


def make_nodes(n_nodes: int, links: list[tuple[float, float]]) -> list[NodeSpec]:
    """构造 n_nodes 个异构节点，算力按 2 的幂递减，链路按 links 循环。

    节点规模敏感性用得到这个函数：4/8/16 节点时算力比例保持一致。
    """
    rates = [16000.0 / (2 ** (i % 4)) for i in range(n_nodes)]
    nodes = []
    for i in range(n_nodes):
        lat, bw = links[i % len(links)]
        nodes.append(NodeSpec(node_id=i, compute_rate=rates[i], slots=4,
                              latency=lat, bandwidth=bw))
    return nodes


def make_trajectory(cond: dict, n_nodes: int, seed: int):
    links = cond["links"]
    link_map = {
        i: LinkState(*links[i % len(links)])
        for i in range(n_nodes)
    }
    if cond["kind"] == "static":
        return StaticNetwork(link_map, label="static")
    return UnstableNetwork(link_map, seed=seed, label="unstable", **cond["params"])


def split_trace_segments(workload: list, n_segments: int) -> list[list]:
    """把 trace 按时间等分成 n_segments 片，每片归零时间轴后作为一个种子的负载实现。

    归零时间轴是必须的：仿真内部按相对时间推进，而每一片覆盖的是
    trace 里完全不同的时段（不同的小时/天），绝对时间戳没有意义。
    """
    if n_segments < 1:
        raise ValueError("n_segments 必须 >= 1")
    n = len(workload)
    bounds = [round(i * n / n_segments) for i in range(n_segments + 1)]
    segments = []
    for i in range(n_segments):
        seg = workload[bounds[i]:bounds[i + 1]]
        if not seg:
            raise ValueError(f"第 {i} 个 trace 时间片为空")
        t0 = seg[0][0]
        segments.append([(a - t0, c, g) for a, c, g in seg])
    return segments


def build_trace_cell_workload(spec, segment: list, n_requests: int,
                              capacity: float,
                              target_rho: float | None = None,
                              net_capacity_bps: float | None = None,
                              bytes_per_ctx_token: float | None = None,
                              bytes_per_gen_token: float = 4.0,
                              node_compute: list[float] | None = None,
                              node_bandwidth: list[float] | None = None,
                              ) -> tuple[list, float, float]:
    """从 trace 时间片里按 token 分位切出一类负载，并把到达率标定到目标 rho。

    标定分两步（门 1）：
      1. 先按 qps=1 切一遍，量出【真正会被仿真的那一段】的平均工作量与局部到达率。
         必须用这一段而不是整片 trace：trace 有强烈的昼夜突发，
         整片平均下来的到达率会让高峰时段的前缀冲到 rho>1（实测第 2 片到 1.20）；
      2. 到达时间是整体乘一个 scale，因此"前 n 个请求的局部到达率"与 qps 成正比，
         反解 qps = (目标 rho × 容量 / 平均工作量) / 探测到的局部到达率。

    间隔的相对结构被完整保留（只乘同一个 scale），所以 trace 的突发性还在。
    返回 (workload, qps, mean_work)。切不够请求就直接报错，不静默缩小请求量——
    那会让不同 cell 的统计功效不一致，却看不出来。
    """
    rho_target = TARGET_RHO if target_rho is None else float(target_rho)
    probe = build_from_trace(segment, spec, target_qps=1.0)
    if len(probe) < n_requests:
        raise ValueError(
            f"负载类 {spec.name} 在该 trace 时间片里只切出 {len(probe):,} 个请求，"
            f"不足 {n_requests:,}；请减少 --requests、减少种子数（=时间片数），"
            f"或换更长的 trace 文件")
    head = probe[:n_requests]
    mean_work = float(np.mean([compute_work(c, g, 1.0, 4.0)
                               for _, c, g in head]))
    if mean_work <= 0:
        raise ValueError(f"负载类 {spec.name} 的平均工作量为 0，无法标定 rho")
    probe_span = head[-1][0] - head[0][0]
    if probe_span <= 0:
        raise ValueError(f"负载类 {spec.name} 的探测段跨度为 0，无法标定 rho")
    qps_at_1x = (len(head) - 1) / probe_span
    # 到达率标定：v2 用多资源可行域（每节点 min(算力, 链路) 再求和），v1 用聚合容量
    mean_bytes = None
    if bytes_per_ctx_token:
        mean_bytes = float(np.mean([
            sum(request_bytes(c, g, bytes_per_ctx_token, bytes_per_gen_token))
            for _, c, g in head]))
    if node_compute:
        qps_needed = calibrate_target_rate(mean_work, mean_bytes, node_compute,
                                           node_bandwidth or [], rho_target)
    else:
        qps_needed = rho_target * capacity / mean_work
    qps = qps_needed / qps_at_1x
    workload = build_from_trace(segment, spec, target_qps=qps)[:n_requests]
    return workload, qps, mean_work


def build_synthetic_cell_workload(spec, n_requests: int, seed: int,
                                  capacity: float,
                                  target_rho: float | None = None,
                                  net_capacity_bps: float | None = None,
                                  bytes_per_ctx_token: float | None = None,
                                  bytes_per_gen_token: float = 4.0,
                                  node_compute: list[float] | None = None,
                                  node_bandwidth: list[float] | None = None,
                                  ) -> tuple[list, float, float]:
    """合成负载也按目标 rho 标定到达率，口径与 trace 路径完全一致。

    以前这里直接用 `spec.target_qps`（四类负载都是 30），而四类负载的每请求工作量
    差一个量级：`data_heavy` 因此跑到 rho=1.83（过饱和，p95 只在测队列排空）、
    `low_latency` 压到 0.054（几乎不排队，四策略必然趋同）。那 6 个组合的对比
    不可采信，而且"类间差异"与"负载强度差异"混在一起，无法归因。

    标定方法与 trace 路径同构：先按 base_qps=1 生成一次（token 抽样只依赖 seed，
    与到达率无关，所以这一步量到的平均工作量就是最终值），再按
    qps = (目标 rho × 容量 / 平均工作量) / 探测到的局部到达率 重建。
    """
    rho_target = TARGET_RHO if target_rho is None else float(target_rho)
    probe, _ = build_synthetic(spec, n_requests=n_requests, seed=1000 + seed,
                               target_qps=1.0)
    head = probe[:n_requests]
    mean_work = float(np.mean([compute_work(c, g, 1.0, 4.0) for _, c, g in head]))
    if mean_work <= 0:
        raise ValueError(f"负载类 {spec.name} 的平均工作量为 0，无法标定 rho")
    probe_span = head[-1][0] - head[0][0]
    if probe_span <= 0:
        raise ValueError(f"负载类 {spec.name} 的探测段跨度为 0，无法标定 rho")
    qps_at_1x = (len(head) - 1) / probe_span
    mean_bytes = None
    if bytes_per_ctx_token:
        mean_bytes = float(np.mean([
            sum(request_bytes(c, g, bytes_per_ctx_token, bytes_per_gen_token))
            for _, c, g in head]))
    if node_compute:
        qps_needed = calibrate_target_rate(mean_work, mean_bytes, node_compute,
                                           node_bandwidth or [], rho_target)
    else:
        qps_needed = rho_target * capacity / mean_work
    qps = qps_needed / qps_at_1x
    workload, _ = build_synthetic(spec, n_requests=n_requests, seed=1000 + seed,
                                 target_qps=qps)
    return workload[:n_requests], qps, mean_work


def load_trace_auto(path: str | Path):
    """按表头自动选 trace 加载器（Azure 2024 / BurstGPT），避免多一个命令行开关。"""
    p = Path(path)
    head = p.read_text(encoding="utf-8", errors="replace")[:400]
    if "TIMESTAMP" in head:
        return load_azure_trace(p)
    if "Request tokens" in head or "Timestamp" in head:
        return load_burstgpt(p)
    raise ValueError(
        f"无法识别 trace 格式（表头既不像 Azure 也不像 BurstGPT）：{p}\n"
        f"前 200 字符: {head[:200]!r}")


def strategy_factory(name: str, w_net: float = 1.0):
    if name == "Random":
        return lambda seed: RandomStrategy(seed=seed)
    if name == "LeastQueue":
        return lambda seed: LeastQueueStrategy(seed=seed)
    if name == "LeastLoad":
        return lambda seed: LeastLoadStrategy(seed=seed)
    if name == "NetworkAware":
        return lambda seed: NetworkAwareStrategy(seed=seed, w_net=w_net)
    if name == "NetworkAware2":
        # V2 机制贡献：算力队列 + 链路队列两条积压一起看
        return lambda seed: NetworkAwareContentionStrategy(seed=seed, w_net=w_net)
    raise ValueError(f"未知策略: {name}")


def label_of(spec) -> str:
    """策略规格 → 结果表里的 strategy 列。字符串就是标签；dict 取 label（缺省用 name）。"""
    if isinstance(spec, str):
        return spec
    return str(spec.get("label") or spec.get("name") or "strategy")


def active_network_conditions() -> dict:
    """当前生效的网络档位集合（`--net-set` 切换 v1 / v2）。"""
    return NETWORK_CONDITIONS_V2 if NET_SET == "v2" else NETWORK_CONDITIONS


def cell_network_condition(cell: dict) -> dict:
    """解析 cell 的网络条件。

    默认取当前档位集合里 `cell["network"]` 的那一项；若 cell 带 `net_profile`，
    则用 `BANDWIDTH_PROFILES` 里的档位（带宽敏感性实验，只改绝对尺度不改异构形状）。
    """
    prof = cell.get("net_profile")
    if prof:
        table = active_bandwidth_profiles()
        if prof not in table:
            raise ValueError(f"未知带宽档位: {prof}（当前档位集合: {sorted(table)}）")
        return {"kind": "static", "links": table[prof]}
    conds = active_network_conditions()
    return conds[cell["network"]]


def calibrate_target_rate(mean_work: float, mean_bytes: float,
                          node_compute: list[float], node_bandwidth: list[float],
                          rho_target: float) -> float:
    """按**多资源可行域**标定到达率（门 1 在 v2 的形式）。

    每节点取两条资源的较小者再求和，得到"完全均衡放置"时可承载的最大到达率：

        R = Σ_i min( 算力_i / E[工作量],  带宽_i / E[字节数] )
        λ = rho_target × R

    【为什么不能用聚合容量】聚合 rho 合格**不等于**运行点可行。实测（19 个 v2 组合）：
    用聚合容量标定时有 5 个组合的实际到达率超过可持续到达率 1.0–2.46 倍
    （`scale/mixed/degraded/4 节点` 最严重），那些格子的队列会无限增长、
    p95 只是有限时长的假象。原因是异构节点的瓶颈不同：25 Gbps 的慢节点上，
    912 MB 的请求只需要 3.4 QPS 就能把它打到满，而聚合口径完全看不到这一点。

    这个 R 也是 §0b 里 `load_ratio = 实际到达率 / R` 的分母，判据是 load_ratio ≤ 0.9。
    """
    if not node_compute:
        return float("inf")
    total = 0.0
    for i, c in enumerate(node_compute):
        cap_i = c / mean_work if mean_work > 0 else float("inf")
        if node_bandwidth and mean_bytes and mean_bytes > 0:
            cap_i = min(cap_i, node_bandwidth[i % len(node_bandwidth)] / mean_bytes)
        total += cap_i
    return rho_target * total


def make_strategy(spec, seed: int, w_net: float = 1.0):
    """按规格实例化策略。spec 是策略名，或带参数与标签的 dict。

    `w_net` 是 cell 级参数（消融实验用它把 NetworkAware 的权重逐档改掉），
    **字符串规格也必须把它传下去**：曾经漏传，导致消融模式里所有 cell 的
    NetworkAware 都跑成 w_net=1.0，整轮 `w_net` 网格静默失效，而门 2 立刻报"不通过"。
    dict 规格里的 `w_net` 优先于 cell 级参数（回退机制与消融都靠它）。

    dict 形式用于回退机制那一节：
        {"name": "NetworkAware2", "label": "NA2_fb2",
         "w_net": 1.0, "fallback_staleness": 2.0, "fallback_w_net": 0.0}
    同一个 cell 内同时跑"不回退"参考臂与多个阈值变体，这样
    "回退 vs 不回退"就是**逐请求配对**比较，而不是跨 cell 的聚合对比。
    """
    if isinstance(spec, str):
        return strategy_factory(spec, w_net)(seed)
    name = str(spec.get("name", "NetworkAware"))
    w = float(spec.get("w_net", w_net))
    # 两个 NetworkAware 变体都支持回退参数；其它策略走通用工厂
    if name in ("NetworkAware", "NetworkAware2"):
        cls = (NetworkAwareStrategy if name == "NetworkAware"
               else NetworkAwareContentionStrategy)
        return cls(seed=seed, w_net=w,
                   fallback_staleness=spec.get("fallback_staleness"),
                   fallback_w_net=float(spec.get("fallback_w_net", 0.0)))
    return strategy_factory(name, w)(seed)


def code_version() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
            text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return "uncommitted"


def paired_test(a: np.ndarray, b: np.ndarray) -> dict:
    """配对比较：a = 候选策略，b = 基线。同一 req_id 一一对应。

    Wilcoxon signed-rank 为主（论文规划 §6.3），Cliff's delta 为效应量。
    """
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]
    d = a - b
    finite = np.isfinite(d)
    d = d[finite]
    if len(d) == 0:
        return {"p_value": float("nan"), "cliffs_delta": float("nan"),
                "median_diff": float("nan"), "n_pairs": 0}
    if np.allclose(d, 0.0):
        # 逐位相同：配对检验没有可检验的差异
        return {"p_value": 1.0, "cliffs_delta": 0.0,
                "median_diff": 0.0, "n_pairs": len(d)}
    try:
        p = float(stats.wilcoxon(a[finite], b[finite]).pvalue)
    except Exception:
        p = float("nan")
    gt, lt = int((d > 0).sum()), int((d < 0).sum())
    delta = float((gt - lt) / len(d))
    return {"p_value": p, "cliffs_delta": delta,
            "median_diff": float(np.median(d)), "n_pairs": len(d)}


def holm_bonferroni(pvals: list[float]) -> list[float]:
    """Holm-Bonferroni 校正（论文规划 §6.3）。返回校正后的 p 值。"""
    idx = np.argsort(pvals)
    m = len(pvals)
    adj = np.full(m, np.nan)
    running_max = 0.0
    for rank, i in enumerate(idx):
        val = pvals[i] * (m - rank)
        running_max = max(running_max, val)
        adj[i] = min(1.0, running_max)
    return list(adj)


def run_cell(cell: dict, strategies: list, n_requests: int,
             w_net: float, save_requests: Path | None = None) -> tuple[list, list]:
    """跑一个 cell 的所有策略，返回 (聚合行, 配对检验行)。

    strategies 的元素既可以是策略名，也可以是 `make_strategy` 认的 dict 规格
    （回退机制那一节需要在一个 cell 内同时跑参考臂与多个阈值变体）。
    """
    cond = cell_network_condition(cell)
    nodes = make_nodes(cell["n_nodes"], cond["links"])
    bpc = DATA_VOLUME[cell["data_volume"]]
    cfg = SimConfig(
        prefill_coef=1.0, decode_coef=4.0, warmup_fraction=0.1,
        max_requests=n_requests,
        bytes_per_context_token=bpc, bytes_per_generated_token=4.0,
        staleness_queue=cell["staleness_queue_s"],
        staleness_network=cell["staleness_network_s"],
        stale_queue=cell.get("stale_queue", True),
        stale_network=cell.get("stale_network", True),
        slo_mode="relative", slo_multiplier=3.0,
    )

    spec = WORKLOAD_CLASSES[cell["workload"]]
    capacity = sum(n.compute_rate * n.slots for n in nodes)
    # v2：链路总容量（B/s）也是硬约束，参与到达率标定与 rho_net 的复算
    net_capacity = float(sum(n.bandwidth for n in nodes))
    rho_target = float(cell.get("target_rho") or TARGET_RHO)
    if cell.get("data_source") == "trace":
        # 真实 trace：请求流由 trace 切片决定，到达率按目标 rho 标定，
        # 种子决定用哪一片（不同种子 = 不同时段的真实流量实现）。
        workload, target_qps, _ = build_trace_cell_workload(
            spec, _TRACE_SEGMENTS[cell["trace_segment"]], n_requests, capacity,
            target_rho=rho_target, net_capacity_bps=net_capacity,
            bytes_per_ctx_token=bpc,
            bytes_per_gen_token=cfg.bytes_per_generated_token,
            node_compute=[n.compute_rate * n.slots for n in nodes],
            node_bandwidth=[n.bandwidth for n in nodes])
        wstats_source = _TRACE_STATS.source
    else:
        # 合成负载同样按目标 rho 标定（以前直接用 target_qps=30，会让
        # data_heavy 过饱和、low_latency 空闲，那批对比不可采信）
        workload, target_qps, _ = build_synthetic_cell_workload(
            spec, n_requests, cell["seed"], capacity, target_rho=rho_target,
            net_capacity_bps=net_capacity, bytes_per_ctx_token=bpc,
            bytes_per_gen_token=cfg.bytes_per_generated_token,
            node_compute=[n.compute_rate * n.slots for n in nodes],
            node_bandwidth=[n.bandwidth for n in nodes])
        wstats_source = SYNTHETIC_STATS_LABEL
    mean_bytes = float(np.mean([
        sum(request_bytes(c, g, bpc, cfg.bytes_per_generated_token))
        for _, c, g in workload]))
    trajectory = make_trajectory(cond, cell["n_nodes"], seed=cell["seed"])
    # 观测年龄：策略看到的是两条通道里【更旧】的那个快照
    # （simulator.build_observation 里 observed_at = min(t_q, t_n)），
    # 因此年龄是 max(队列 lag, 链路 lag)。回退的触发判据就建在它上面。
    staleness_lag_s = max(cell["staleness_queue_s"], cell["staleness_network_s"])

    rows, per_req = [], {}
    # 只服务于调度的键（strategies / pair_*）不进 CSV，否则会写成字符串化的列表
    cell_cols = {k: v for k, v in cell.items() if k not in CELL_META_KEYS}
    for s in strategies:
        sname = label_of(s)
        fb_s = s.get("fallback_staleness") if isinstance(s, dict) else None
        row_w_net = float(s.get("w_net", w_net)) if isinstance(s, dict) else w_net
        t0 = time.perf_counter()
        result = run(workload, nodes, make_strategy(s, cell["seed"], w_net),
                     cfg, cell["seed"], trajectory)
        elapsed = time.perf_counter() - t0
        measured = result.measured(cfg.warmup_fraction)
        row = summarize(measured, result.makespan, cell["n_nodes"],
                        sum(n.slots for n in nodes))
        # 这些键只服务于调度（不要写进 CSV 变成字符串化的列表）
        row.update({**cell_cols, "strategy": sname,
                    "w_net": row_w_net,
                    "fallback_staleness": fb_s,
                    "staleness_lag_s": staleness_lag_s,
                    "target_rho": rho_target,
                    "target_qps": target_qps,
                    "wall_clock_s": round(elapsed, 3),
                    "mean_work": float(np.mean([cfg.prefill_coef * c + cfg.decode_coef * g
                                                for _, c, g in workload])),
                    "mean_bytes": mean_bytes,
                    "net_capacity_Bps": net_capacity,
                    "rho_net": (len(workload) / max(workload[-1][0], 1e-9))
                               * mean_bytes / net_capacity if net_capacity > 0 else float("nan"),
                    "qps_realized": len(workload) / max(workload[-1][0], 1e-9)})
        rows.append(row)

        rec = pd.DataFrame([{"req_id": r.req_id, "sojourn": r.sojourn,
                             "node_id": r.node_id, "slo_met": r.slo_met}
                            for r in measured]).sort_values("req_id")
        per_req[sname] = rec
        if save_requests is not None:
            rec.to_csv(save_requests / f"{cell['cell_id']}_{sname}.csv", index=False)

    # --- 配对检验：候选策略 vs 每个基线（默认 NA 与 LeastQueue 对三个基线）---
    tests = []
    labels = [label_of(s) for s in strategies]
    if cell.get("pair_baselines") is not None:
        baselines = list(cell["pair_baselines"])
    else:
        baselines = [l for l in labels if l != "NetworkAware"]
    if cell.get("pair_candidates") is not None:
        candidates = list(cell["pair_candidates"])
    else:
        # 候选策略：v2 的两个网络感知策略都要参与配对检验，
        # 否则 NA2 只会当基线、`paired.csv` 里没有它的效应列，机制对比就没了配对证据。
        # 同理别忘了 LeastQueue（它相对基线的效应也是论文要报的）。
        candidates = [l for l in ("NetworkAware", "NetworkAware2", "LeastQueue")
                      if l in per_req]
    for cand in candidates:
        for base in baselines:
            if cand == base:
                continue
            a = per_req[cand]["sojourn"].to_numpy(dtype=float)
            b = per_req[base]["sojourn"].to_numpy(dtype=float)
            t = paired_test(a, b)
            t.update({**cell_cols, "candidate": cand, "baseline": base,
                      "w_net": w_net})
            tests.append(t)

    for t in tests:
        t["workload_stats"] = wstats_source
    return rows, tests


def build_matrix(mode: str, extra: dict) -> list[dict]:
    """构造要跑的 cell 列表。"""
    cells = []

    def cell(wl, net, fresh, seed, dv=None, n_nodes=3, **kw):
        st = STALENESS[fresh]
        base = {
            "workload": wl, "network": net, "staleness": fresh,
            "staleness_queue_s": st["queue"], "staleness_network_s": st["network"],
            "seed": seed, "data_volume": dv or DEFAULT_DATA_VOLUME,
            "n_nodes": n_nodes,
            # trace 模式下种子退化为时间片编号：第 s 个种子用第 s 片 trace。
            "data_source": SOURCE,
            "trace_segment": (seed - 1) if SOURCE == "trace" else None,
            # 到达压力：默认取全局目标，rho 模式会逐 cell 覆盖
            "target_rho": TARGET_RHO,
            # 网络档位集合（v1/v2），使不同档位批次的产物可区分
            "net_set": NET_SET,
        }
        base.update(kw)
        return base

    if mode == "main":
        # 只用 MAIN_STALENESS_LEVELS（fresh/stale），与 论文规划.md §6.1 的 480 次运行一致
        for wl, net, fresh, seed in itertools.product(
                WORKLOAD_CLASSES.keys(), NETWORK_CONDITIONS.keys(),
                MAIN_STALENESS_LEVELS, SEEDS):
            cells.append(cell(wl, net, fresh, seed))
    elif mode == "staleness":
        # RQ2 核心：只扫链路陈旧，队列陈旧固定在 1 秒（模拟常见监控周期）
        for lag_n in STALENESS_SWEEP:
            for net in ["normal", "degraded", "unstable"]:
                for seed in SEEDS[:3]:
                    c = cell("mixed", net, "fresh", seed)
                    c["staleness"] = f"net{lag_n:g}"
                    c["staleness_queue_s"] = min(1.0, lag_n)
                    c["staleness_network_s"] = lag_n
                    cells.append(c)
        # 对照：只让队列陈旧，链路实时，验证两条通道效应可分离
        for lag_q in STALENESS_SWEEP:
            for seed in SEEDS[:3]:
                c = cell("mixed", "unstable", "fresh", seed)
                c["staleness"] = f"queue{lag_q:g}"
                c["staleness_queue_s"] = lag_q
                c["staleness_network_s"] = 0.0
                cells.append(c)
    elif mode == "datavolume":
        # 数据量扫描：报告"共置 vs 分离式"的完整边界曲线
        for dv, wl, net, seed in itertools.product(
                DATA_VOLUME.keys(), WORKLOAD_CLASSES.keys(),
                ["normal", "degraded"], SEEDS[:3]):
            cells.append(cell(wl, net, "fresh", seed, dv=dv))
    elif mode == "convergence":
        # 种子收敛性：同一 cell 上跑 20 个种子，事后按 3/5/10/20 切片
        for seed in range(1, 21):
            cells.append(cell("mixed", "unstable", "stale", seed))
    elif mode == "ablation":
        # 权重消融：v2 下同一个权重同时作用在两个网络感知策略上，
        # 这样门 2 一次就能验两个策略的"严格子集"性质（w_net=0 必须退化为 LeastLoad）。
        for w_net in [0.0, 0.5, 1.0, 1.5, 2.0]:
            for wl, net, seed in itertools.product(
                    ["mixed", "data_heavy"], ["normal", "degraded"], SEEDS[:3]):
                c = cell(wl, net, "fresh", seed, w_net_override=w_net)
                if NET_SET == "v2":
                    c["strategies"] = ["LeastLoad", "NetworkAware", "NetworkAware2"]
                cells.append(c)
    elif mode == "scale":
        for n_nodes in [4, 8, 16]:
            for net, seed in itertools.product(["normal", "degraded"], SEEDS[:3]):
                cells.append(cell("mixed", net, "fresh", seed, n_nodes=n_nodes))
    elif mode == "rho":
        # 到达压力扫描：收益边界必须在这条轴上给出形状，而主矩阵把 rho 固定成了一点。
        # 下端贴近门 1 的下沿（几乎不排队 → 四策略必然趋同），上端贴近过饱和。
        for rho in RHO_GRID:
            for wl, net, seed in itertools.product(
                    ["mixed", "data_heavy"],
                    ["normal", "degraded", "unstable"], SEEDS[:3]):
                c = cell(wl, net, "fresh", seed)
                c["target_rho"] = rho
                cells.append(c)
    elif mode == "fallback":
        # RQ2 回退机制：同一个 cell 内同时跑"不回退"参考臂与各阈值变体，
        # 这样"回退 vs 不回退"是逐请求配对比较，而不是跨 cell 的聚合对比。
        specs = ["LeastLoad", "NetworkAware"] + [
            {"name": "NetworkAware", "label": f"NetworkAware_fb{t:g}",
             "w_net": 1.0, "fallback_staleness": t, "fallback_w_net": 0.0}
            for t in FALLBACK_THRESHOLDS]
        for fresh in FALLBACK_STALENESS_LEVELS:
            for wl, net, seed in itertools.product(
                    ["mixed", "data_heavy"], ["degraded", "unstable"], SEEDS[:3]):
                c = cell(wl, net, fresh, seed)
                c["strategies"] = specs
                c["pair_candidates"] = [label_of(s) for s in specs[1:]]
                c["pair_baselines"] = ["LeastLoad", "NetworkAware"]
                cells.append(c)
    elif mode == "bandwidth":
        # 带宽敏感性（对应保真度对照里"链路比机内低 80×–8000×"这条威胁）：
        # 保持 degraded 的异构形状与全部负载/策略不变，只把链路的绝对尺度换档，
        # 观察 Δp95 与 net_share 的关系——即"网络有多贵"这一条轴上收益在哪消失。
        for prof, dv, wl, seed in itertools.product(
                active_bandwidth_profiles().keys(), DATA_VOLUME.keys(),
                ["mixed", "data_heavy"], SEEDS[:3]):
            c = cell(wl, "degraded", "fresh", seed, dv=dv)
            c["net_profile"] = prof
            cells.append(c)
    else:
        raise ValueError(f"未知模式: {mode}")

    for i, c in enumerate(cells):
        c["cell_id"] = f"{mode}_{i:04d}"
    return cells


def main() -> int:
    # 这几个全局量由命令行覆盖，必须在函数体最前面声明（argparse 的默认值就会读它们）
    global SEEDS, SOURCE, TARGET_RHO, _TRACE_SEGMENTS, _TRACE_STATS, NET_SET

    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="main",
                    choices=["main", "staleness", "datavolume", "convergence",
                             "ablation", "scale", "rho", "fallback", "bandwidth"])
    ap.add_argument("--out", default=str(ROOT / "results/matrix"))
    ap.add_argument("--requests", type=int, default=N_REQUESTS)
    ap.add_argument("--seeds", type=int, nargs="*", default=None,
                    help="覆盖随机种子列表（调试用）")
    ap.add_argument("--source", choices=["synthetic", "trace"], default="synthetic",
                    help="负载来源：合成负载，或真实 trace（Azure / BurstGPT 按表头自动识别）")
    ap.add_argument("--trace", default=str(TRACE_PATH),
                    help="trace CSV 路径（仅 --source trace 使用）")
    ap.add_argument("--target-rho", type=float, default=TARGET_RHO,
                    help="到达率标定的目标负载强度（门 1，两条路径都生效；rho 模式逐 cell 覆盖）")
    ap.add_argument("--net-set", choices=["v1", "v2"], default="v1",
                    help="网络档位集合：v1 = 10 Gbps/1 Gbps/100 Mbps（跨域/算力网络）；"
                         "v2 = 400/100/25 Gbps（真实 AI 集群量级，配合按两条资源标定的 rho）")
    ap.add_argument("--save-requests", action="store_true",
                    help="额外保存每请求明细（体积大，只在代表性 cell 上用）")
    ap.add_argument("--limit", type=int, default=None, help="只跑前 N 个 cell（测速用）")
    args = ap.parse_args()

    if args.seeds:
        SEEDS = args.seeds
    SOURCE = args.source
    TARGET_RHO = args.target_rho
    NET_SET = args.net_set

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    run_id = f"{args.mode}_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"

    cells = build_matrix(args.mode, {})
    if args.limit:
        cells = cells[: args.limit]

    n_segments = 0
    if SOURCE == "trace":
        trace_path = Path(args.trace)
        if not trace_path.exists():
            print(f"找不到 trace 文件：{trace_path}")
            return 2
        print(f"读取 trace（百 MB 级文件首次解析需 1–3 分钟）：{trace_path}", flush=True)
        # 按表头自动识别 Azure / BurstGPT，避免多一个开关记错
        trace_workload, _TRACE_STATS = load_trace_auto(trace_path)
        # 时间片数 = 本轮用到的最大种子号，保证每个种子都有自己的负载实现
        n_segments = max(c["trace_segment"] for c in cells) + 1
        _TRACE_SEGMENTS = split_trace_segments(trace_workload, n_segments)
        print(f"  来源 {_TRACE_STATS.source}")
        print(f"  {len(trace_workload):,} 请求   覆盖 {_TRACE_STATS.duration_s/3600:.2f} 小时   "
              f"均值 {_TRACE_STATS.qps_mean:.2f} QPS   p95 窗口 {_TRACE_STATS.qps_p95:.2f} QPS")
        print(f"  ContextTokens p50={_TRACE_STATS.context_p50:.0f} "
              f"p95={_TRACE_STATS.context_p95:.0f}   "
              f"GeneratedTokens p50={_TRACE_STATS.generated_p50:.0f} "
              f"p95={_TRACE_STATS.generated_p95:.0f}")
        print(f"  切成 {n_segments} 个时间片（种子 s 用第 s 片）   目标 rho={TARGET_RHO}")

    n_sims = sum(len(c.get("strategies") or base_strategies()) for c in cells)
    print(f"模式={args.mode}  来源={SOURCE}  run_id={run_id}")
    print(f"cells={len(cells)}  共 {n_sims} 次仿真  每 cell {args.requests} 请求")

    all_rows, all_tests = [], []
    t_start = time.perf_counter()

    # --- 增量落盘 ---
    # 原实现把全部结果攒在内存里最后一次性写盘，进程一旦中断就全丢
    # （已实际发生：会话退出导致整个矩阵无产物）。改为每个 cell 追加一次。
    runs_path = out / f"{run_id}_runs.csv"
    paired_path = out / f"{run_id}_paired.csv"

    def append_rows(path: Path, rows: list[dict]) -> None:
        if not rows:
            return
        pd.DataFrame(rows).to_csv(
            path, mode="a", header=not path.exists(), index=False)

    for i, cell in enumerate(cells):
        w_net = cell.get("w_net_override", 1.0)
        strategies = cell.get("strategies") or base_strategies()
        rows, tests = run_cell(
            cell, strategies, args.requests, w_net,
            save_requests=(out / "requests" / run_id) if args.save_requests else None)
        append_rows(runs_path, rows)
        append_rows(paired_path, tests)
        all_rows.extend(rows)
        all_tests.extend(tests)

        if (i + 1) % 10 == 0 or i == len(cells) - 1:
            el = time.perf_counter() - t_start
            done = i + 1
            eta = el / done * (len(cells) - done)
            print(f"  {done}/{len(cells)} cells  "
                  f"已用 {el/60:.1f} min  预计剩余 {eta/60:.1f} min  "
                  f"({el/done:.2f} s/cell)", flush=True)

    total_el = time.perf_counter() - t_start

    df = pd.DataFrame(all_rows)
    df_t = pd.DataFrame(all_tests)
    # Holm-Bonferroni 校正必须等全部 cell 跑完才能做（秩依赖全族），
    # 因此先读回增量落盘的完整结果，再整体校正后覆写。
    if runs_path.exists():
        df = pd.read_csv(runs_path)
    if paired_path.exists():
        df_t = pd.read_csv(paired_path)
    if len(df_t):
        # 必须按索引回填。groupby 会重排组顺序，直接按位置赋值会把校正值
        # 配到错误的行上（这类错误不会报错，只会静默产生错误的 p 值）。
        adj_series = pd.Series(np.nan, index=df_t.index, dtype=float)
        for _, g in df_t.groupby(["cell_id", "candidate"]):
            vals = holm_bonferroni(list(g["p_value"].fillna(1.0)))
            adj_series.loc[g.index] = vals
        df_t["p_holm"] = adj_series

    df.to_csv(runs_path, index=False)
    df_t.to_csv(paired_path, index=False)

    manifest = {
        "run_id": run_id,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "mode": args.mode,
        "n_cells": len(cells),
        "n_runs": len(df),
        "n_requests_per_run": args.requests,
        "strategies": base_strategies(),
        "seeds": SEEDS,
        "network_conditions": active_network_conditions(),
        "staleness_levels": STALENESS,
        "staleness_sweep_s": STALENESS_SWEEP,
        "data_volume_bytes_per_ctx_token": DATA_VOLUME,
        "data_source": SOURCE,
        # 版本标记：v1 的产物（results/matrix* 里的旧 run）是在**链路不排队**的模型下跑的；
        # 现在仿真器给每个节点的链路加了 FIFO 管道，同一份数据里这两类产物必须能区分开，
        # 否则会把两个模型的结果混着引用。
        "link_queue_model": True,
        "net_set": NET_SET,
        "trace_file": str(Path(args.trace).resolve()) if SOURCE == "trace" else None,
        "target_rho": TARGET_RHO,
        "rho_grid": RHO_GRID if args.mode == "rho" else None,
        "fallback_thresholds_s": FALLBACK_THRESHOLDS if args.mode == "fallback" else None,
        "n_trace_segments": n_segments or None,
        "trace_stats": ({
            "source": _TRACE_STATS.source,
            "n_requests": _TRACE_STATS.n_requests,
            "duration_h": round(_TRACE_STATS.duration_s / 3600.0, 3),
            "qps_mean": round(_TRACE_STATS.qps_mean, 4),
            "qps_p95": round(_TRACE_STATS.qps_p95, 4),
            "context_p50": _TRACE_STATS.context_p50,
            "context_p95": _TRACE_STATS.context_p95,
            "context_max": _TRACE_STATS.context_max,
            "generated_p50": _TRACE_STATS.generated_p50,
            "generated_p95": _TRACE_STATS.generated_p95,
            "generated_max": _TRACE_STATS.generated_max,
            "n_dropped": _TRACE_STATS.n_dropped,
            "n_truncated_tail": _TRACE_STATS.n_truncated_tail,
            "timestamp_monotonic": _TRACE_STATS.timestamp_monotonic,
        } if _TRACE_STATS is not None else None),
        "n_nodes_by_mode": sorted(df["n_nodes"].unique().tolist()),
        "wall_clock_total_s": round(total_el, 2),
        "wall_clock_per_run_s": round(total_el / max(len(df), 1), 4),
        "code_version": code_version(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "slo_note": "SLO 为实验假设（slo_base=1.0s + 0.35s/千token），trace 不提供 deadline",
        "workload_note": (
            "真实 Azure LLM Inference Trace（AzurePublicDataset, CC-BY），"
            "按 ContextTokens/GeneratedTokens 分位切成四类负载；"
            "到达率按目标 rho 标定，种子对应不同的 trace 时间片"
            if SOURCE == "trace" else
            "合成负载占位（SYNTHETIC，不是 trace 结果）；"
            "四类负载共用 target_qps=30，data_heavy/low_latency 的 rho 会越界"),
    }
    with open(out / f"{run_id}_manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2, default=str)

    print()
    print(f"完成：{len(df)} 次仿真，用时 {total_el/60:.2f} min "
          f"（{manifest['wall_clock_per_run_s']:.3f} s/次）")
    print(f"runs:   {out / (run_id + '_runs.csv')}")
    print(f"paired: {out / (run_id + '_paired.csv')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

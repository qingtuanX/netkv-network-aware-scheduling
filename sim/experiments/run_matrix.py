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
    BottleneckStrategy,
    CongestionOnlyStrategy,
    LeastLoadStrategy,
    LeastQueueStrategy,
    NetworkAwareAdaptiveStrategy,
    NetworkAwareContentionStrategy,
    NetworkAwareHardSwitchStrategy,
    NetworkAwareStrategy,
    P2CNetStrategy,
    P2CStrategy,
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
# 【细变量②】观测聚合窗口的扫描网格（秒）。0 = 瞬时值（现状）。
# 0.5/2 对应"秒级监控"，10/30 对应"分钟级监控"的等效平滑。
WINDOW_GRID = [0.0, 0.5, 2.0, 10.0, 30.0]

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

# --- 【细变量①】两条通道年龄的二维网格 --------------------------------------
# 为什么要二维：现有两条 1D 扫描回答不了"搭配"问题——
#   「排队消息很新、网线消息很旧」和「排队消息很旧、网线消息很新」是不是一回事？
# 已发表文献里没有人把"两种被观测对象各自的年龄"作为一个二维因子做（见
# `文献/08-竞争定位（2025-2026）.md` §6.4：最接近的只是无线网络里的"测量频率"）。
# 只扫 **unstable** 网络：静态网络的链路状态不随时间变，"链路 lag" 精确无效（实测 Δ=0），
# 扫它只会得到一整排零——那是设计性质，不是发现。
# 网格大小是成本换来的：**陈旧度越大、单次仿真越慢**（策略看到旧状态 → 请求堆积 →
# 事件数暴增）。实测 25×2×3=150 cells 的满网格要 ~41 分钟，而 3×4 的第一遍只要 ~10 分钟。
# 所以先跑收窄版看结构（对角线 vs 非对角线是否有差别），结构成立再铺开。
# 收窄版保留了三个关键对比：都新鲜 / 只有队列旧 / 只有链路旧 / 两者都旧。
# 对齐度实验的种子数：**种子的作用是"独立的负载实现"**（确定性策略本身无随机性）。
# 3 个种子只能看方向，给不出跨实现的置信区间；要谈统计就得把"实现的个数"提上来。
# 取 10：trace 模式下一个种子=一个时间片，10 片时每片仍有 ~140k 请求（data_heavy 类约 16%，
# 即 ~22k，够 12000 请求/次的切片），再往上分片就有切不出足够请求的风险。
ALIGN_SEEDS = 10

STALENESS2D_QUEUE = [0.0, 1.0, 5.0]
STALENESS2D_NETWORK = [0.0, 1.0, 15.0, 60.0]

# --- V3 方法（降权规则）对照 -------------------------------------------------
# 新鲜度水平 = 四种真实运维配置 + 全新鲜对照。选它们而不是均匀网格的理由：
#   (0,0)   全新鲜：控制组，降权规则在这里不该有损失；
#   (1,0)   队列监控 1 s：接近"理想监控"；
#   (5,0)   队列监控 5 s：§16 实测优势在这里只剩四分之一，是降权最该起作用的地方；
#   (0,15)  链路监控 15 s：最常见的监控周期（Prometheus 默认就是 15 s）；
#   (5,60)  两条通道都慢：真机实测的悲观组合（采集 15 s + 上报延迟）。
METHOD_FRESHNESS = [(0.0, 0.0), (1.0, 0.0), (5.0, 0.0), (0.0, 15.0), (0.0, 60.0),
                    (5.0, 60.0)]

# 五条臂：两条基线 + 三种降权规则。顺序即结果表里的顺序。
METHOD_STRATEGIES = [
    "LeastLoad",
    "NetworkAware2",
    {"name": "NetworkAware2Adapt", "label": "AdaptSplit"},                     # 主方法
    {"name": "NetworkAware2Adapt", "label": "AdaptLinear", "decay": "linear"},
    {"name": "NetworkAware2AdaptHard", "label": "AdaptHard"},                  # 硬开关
]

# 【修订版】按**通道**降权的规则（`decay="channel"`），2026-10-01 在看到第一批结果后加。
# 它不是为了"再凑一个更好的结果"，而是因为原设计的**层级错了**：
#   原设计按"链路信息内部的两块内容（忙闲 / 粗细）"分别降权，实测（BurstGPT 批）证明这样更差——
#   把"拥塞惩罚"单独压低，会让"粗网线 + 慢算力"那台机器重新堆满活。
#   修订版改为按**通道**降权（队列快、链路慢且带下限），两个分量同权。
# **旧的 split 臂继续留在表里一起报**，这样"设计选择值多少钱"是可查的。
# 见 `src/strategies.py` 的类 docstring 修订记录与 `文献/08` 的写作口径。
METHOD_CH_STRATEGIES = [
    "LeastLoad",
    "NetworkAware2",
    {"name": "NetworkAware2Adapt", "label": "AdaptChannel", "decay": "channel"},
    # 【修正版】只按链路状态年龄降权（2026-10-23 校准模型实验后加）：
    # channel 版在 q5_n0 时把网络项压到 28%，丢掉了新鲜链路的有用信息。
    # linkonly 版链路新鲜时保持满权重，只在链路陈旧时降权到下限。
    {"name": "NetworkAware2Adapt", "label": "AdaptLinkOnly", "decay": "linkonly"},
]

# --- 第二版规则的参数消融（同样的 OFAT 口径）--------------------------------
# 三个参数各扫两档：队列时间常数 4→2/8 s、网线时间常数 60→20/120 s、权重下限 0.7→0.5/0.9。
# 问的不是"哪档最好"，而是"把参数挪开，结论动多少"——如果挪一两倍结果几乎不动，
# 说明这个数不是精调出来的；一动就翻，就必须承认结论依赖它。
METHOD_CH_ABLATE_ARMS = [    {"name": "NetworkAware2Adapt", "label": "primary", "decay": "channel"},
    {"name": "NetworkAware2Adapt", "label": "Tq2", "decay": "channel", "tau_q": 2.0},
    {"name": "NetworkAware2Adapt", "label": "Tq8", "decay": "channel", "tau_q": 8.0},
    {"name": "NetworkAware2Adapt", "label": "Tn20", "decay": "channel", "tau_n": 20.0},
    {"name": "NetworkAware2Adapt", "label": "Tn120", "decay": "channel", "tau_n": 120.0},
    {"name": "NetworkAware2Adapt", "label": "floor0.5", "decay": "channel", "link_floor": 0.5},
    {"name": "NetworkAware2Adapt", "label": "floor0.9", "decay": "channel", "link_floor": 0.9},
    "LeastLoad",
    "NetworkAware2",
]

# --- 阈值消融：**独立实验**，不是"调好再报" ---------------------------------# 主方法的四个参数各扫几档，一次只动一个（OFAT）。放在同一个 cell 里跑，
# 于是"改阈值"与"不改阈值"是**逐请求配对**比较（回退机制那一节同样的口径）。
# 为什么要在意的不是"哪个档最好"，而是"结论对参数有多敏感"：
# 如果 TAU_Q 从 2 s 到 8 s 结果几乎不动，说明这个数不是精调出来的；如果一动就翻，
# 就必须承认结论依赖这个选择。
# 消融只用三个新鲜度档（控制 / 队列旧 / 链路旧），省算力且不损失信息——
# "两条通道都旧"那一档的机制是前两档的叠加，已在主对照里测过。
METHOD_ABLATE_FRESHNESS = [(0.0, 0.0), (5.0, 0.0), (0.0, 15.0)]

# --- 补强基线：回答"是不是只赢了弱基线" -------------------------------------
# 之前只有 Random / LeastQueue / LeastLoad 三条对照，审稿人第一个问题就是
# "为什么不跟 P2C 和已发表的网络感知方法比"。这 8 条臂一次跑齐：
#   Random / LeastQueue / LeastLoad —— 原有下界与朴素基线
#   P2C                             —— 经典"二选一"强基线（只需要局部视野）
#   P2CNet                          —— 二选一 + 网络项（把"视野大小"与"看不看网络"解耦）
#   CongestionOnly                  —— Aegis(SIGCOMM'26) 式"拥塞压力"项（不含静态带宽）
#   Bottleneck                      —— Helix(ASPLOS'25) 式瓶颈最小化（取大而非求和）
#   NetworkAware2                   —— 本文的完全信任网络参照臂
BASELINE_STRATEGIES = ["Random", "LeastQueue", "LeastLoad", "P2C", "P2CNet",
                       "CongestionOnly", "Bottleneck", "NetworkAware2"]

METHOD_ABLATE_ARMS = [
    {"name": "NetworkAware2Adapt", "label": "primary"},                       # TAU_Q=4, TAU_N_DYN=15
    {"name": "NetworkAware2Adapt", "label": "Tq1", "tau_q": 1.0},
    {"name": "NetworkAware2Adapt", "label": "Tq2", "tau_q": 2.0},
    {"name": "NetworkAware2Adapt", "label": "Tq8", "tau_q": 8.0},
    {"name": "NetworkAware2Adapt", "label": "Tn5", "tau_n_dyn": 5.0},
    {"name": "NetworkAware2Adapt", "label": "Tn30", "tau_n_dyn": 30.0},
    {"name": "NetworkAware2Adapt", "label": "floor0.05", "net_share_floor": 0.05},
    {"name": "NetworkAware2Adapt", "label": "floor0.20", "net_share_floor": 0.20},
    {"name": "NetworkAware2Adapt", "label": "drift_gate", "drift_gate": True},
    "LeastLoad",
    "NetworkAware2",
]

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


def apply_alignment(cond: dict, align: str | None) -> dict:
    """【细变量③】按"对齐模式"重排链路列表——只改**哪台机器配哪条链路**，不改链路本身。

    为什么它是关键变量（也是"网络信息为什么可能有价值"的机制解释）：
      调度要同时权衡两件事——"哪台机器算得快"和"哪台机器网线粗"。
      · **两者一致**（算得快的机器网线也最粗）→ 按"谁闲"挑和按"网线好"挑，
        挑出来往往是同一台，**网络信息不提供任何额外区分度**，收益应当≈0；
      · **两者相反**（算得最快的机器网线最细）→ 两个标准吵架，网络信息才有用武之地。
    现有全部实验其实都落在"相反"这一档（`make_nodes` 把 `links[0]`（最慢链路）
    配给 node 0（算力最强）），也就是说**从没测过一致的情形**——所以"收益有多大"
    一直缺少一个机制侧的解释变量。

    实现：只是把链路列表重新排序。`anti`（默认）＝保持现状；
    `aligned` ＝反转（快算力配快链路）；`rot1`/`rot2` ＝错位一/两格（部分对齐）。
    """
    if not align:
        return cond
    out = dict(cond)
    # 长度=节点数的平滑斜坡（见 alignment_links：固定 3 条链路的反转在多节点下会失效）
    out["links"] = alignment_links(int(cond.get("_n_nodes", 3)), align, cond)
    return out


def alignment_links(n_nodes: int, align: str, base_cond: dict) -> list[tuple[float, float]]:
    """为 n 个节点生成**长度等于节点数**的链路表，并按"对齐档"排序。

    【为什么不能沿用"反转固定 3 条链路"】算力按 2 的幂循环（周期 4）、链路按 3 条循环（周期 3），
    节点一多两者就互相错位：实测 8 节点时 anti 与 aligned 的**全局一致比例都只有 0.518 / 0.482**，
    操纵本身失效——那样跑出来的"没差别"是假的（不是机制不存在，是没操纵成）。
    （这条是先在 `alignscale` 的构造检查里发现的，避免了白跑 225 次仿真。）

    做法：把基础档位里的**最慢**与**最快**两端之间按几何插值铺成 n 档
    （带宽递增、时延递减，与原档位的方向一致），然后按档排序：
      · `aligned` → 算力第 1 的节点拿最宽链路（一致比例 1.0）
      · `anti`    → 算力第 1 的节点拿最窄链路（一致比例 0.0）
      · `rot1`    → 整条斜坡错开 1/3（部分一致）
    带宽的取值范围与总和都不变（只是**重新配对**），所以变的是"配对"，不是"网络有多贵"。
    3 节点时几何插值的中点恰好等于原档位的中间档（√(3.125×50)=12.5 Gbps），
    因此它与已有 `--mode align`（3 节点）的配对方式是**同一套定义**。
    """
    links = list(base_cond.get("links", []))
    if len(links) < 2 or n_nodes < 2:
        return links
    bw_lo, bw_hi = min(b for _, b in links), max(b for _, b in links)
    # 时延与带宽反向：最慢链路时延最大
    lat_slow = max(l for l, b in links if b == bw_lo)
    lat_fast = min(l for l, b in links if b == bw_hi)
    ramp = []
    for i in range(n_nodes):
        f = i / (n_nodes - 1)
        ramp.append((lat_slow * (lat_fast / lat_slow) ** f,
                     bw_lo * (bw_hi / bw_lo) ** f))
    # 注意命名方向（这里写反过一次，靠构造检查才发现）：
    #   ramp[0] 是最**窄**的链路；节点 0 是算力**最强**的。
    #   所以 `anti`（两排名相反）= 节点 0 拿 ramp[0] → order 递增；
    #   `aligned`（两排名一致）= 节点 0 拿 ramp[-1] → order 递减。
    if align == "aligned":
        order = list(range(n_nodes - 1, -1, -1))     # 算力第 1 ↔ 带宽最宽（一致比例 1.0）
    elif align in (None, "", "anti"):
        order = list(range(n_nodes))                 # 算力第 1 ↔ 带宽最窄（一致比例 0.0）
    elif align == "rot1":
        k = n_nodes // 3
        order = list(range(k, n_nodes)) + list(range(k))
    else:
        raise ValueError(f"未知对齐模式: {align}")
    return [ramp[j] for j in order]


def rank_corr(nodes: list) -> float:
    """算力排名与带宽排名的 **Spearman 相关系数**：+1 = 完全一致，−1 = 完全相反。

    【为什么换掉原来的"两两一致比例"】原指标在节点数 >3 时失效：算力阶梯周期是 4 台、
    链路是斜坡，节点一多就出现"算力并列"的对，这些对被记 0.5 分，把信号稀释掉——
    实测 8 台时 anti 与 rot1 的原指标**完全相同**（0.518），而两者实际收益差 3.4 个百分点；
    于是"按指标排序"的趋势检验在 8 台上符号都反了（ρ=−0.75）。
    Spearman 用**平均秩**处理并列值，并且衡量的是整体单调关系，不会把
    "每台机器上两条排名是否同向"这件事抹平。两个指标都保留在产物里，便于对照。
    """
    n = len(nodes)
    if n < 2:
        return float("nan")
    comp = [x.compute_rate for x in nodes]
    bw = [x.bandwidth for x in nodes]
    try:
        from scipy import stats as st      # noqa: PLC0415
        return float(st.spearmanr(comp, bw).statistic)
    except Exception:                       # noqa: BLE001
        # 兜底：手算 Spearman（平均秩 + Pearson）
        def _ranks(v):
            order = sorted(range(len(v)), key=lambda i: v[i])
            r = [0.0] * len(v)
            i = 0
            while i < len(order):
                j = i
                while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                    j += 1
                avg = (i + j) / 2.0 + 1.0
                for k in range(i, j + 1):
                    r[order[k]] = avg
                i = j + 1
            return r
        rc, rb = _ranks(comp), _ranks(bw)
        mc, mb = sum(rc) / n, sum(rb) / n
        num = sum((a - mc) * (b - mb) for a, b in zip(rc, rb))
        den = (sum((a - mc) ** 2 for a in rc) * sum((b - mb) ** 2 for b in rb)) ** 0.5
        return num / den if den else float("nan")


def cell_nodes(cell: dict) -> list:
    """按 cell 构造节点（含"对齐档"的重排）——**cell 构造与仿真必须都走这一个入口**。

    【为什么要有它】踩过一次：`build_matrix()` 里算"对齐度指标"时没有把节点数传进
    `apply_alignment()`（该函数靠 `cond["_n_nodes"]` 决定斜坡长度），于是
    **记录下来的指标是按"3 条链路循环"算的，而 run_cell 里真正跑的是"按节点数铺的斜坡"**——
    两者不是同一个配置，趋势检验的横轴因此全错（n=8 的符号都反了）。
    现在两处都调这一个函数，不可能再不一致。
    """
    cond = cell_network_condition(cell)
    cond["_n_nodes"] = cell["n_nodes"]
    cond = apply_alignment(cond, cell.get("align"))
    return make_nodes(cell["n_nodes"], cond["links"])


def alignment_index(nodes: list) -> float:
    """两个排名的"一致比例"：0.5 = 无关，1.0 = 完全一致，0.0 = 完全相反。

    对每一对节点，比较"算力谁大"与"链路谁粗"；方向相同的对占比就是它。
    这个数只由节点配置决定（与负载、策略无关），用来把"对齐度"这个机制变量**量化**，
    这样论文里可以说"收益随一致比例如何变化"，而不是只说"配置不同"。
    """
    n = len(nodes)
    if n < 2:
        return float("nan")
    agree = total = 0
    for i in range(n):
        for j in range(i + 1, n):
            dc = nodes[i].compute_rate - nodes[j].compute_rate
            db = nodes[i].bandwidth - nodes[j].bandwidth
            total += 1
            if dc * db > 0:
                agree += 1
            elif dc * db == 0:
                agree += 0.5
    return agree / total if total else float("nan")


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
    if name == "NetworkAware2Adapt":
        # V3 方法贡献：双通道【连续降权】+ 漂移门控 + 按请求网络占比的自适应权重
        return lambda seed: NetworkAwareAdaptiveStrategy(seed=seed, w_net=w_net)
    if name == "NetworkAware2AdaptHard":
        # 方法消融的对照臂：同一套因子但阈值是硬开关（过度回退那一版）
        return lambda seed: NetworkAwareHardSwitchStrategy(seed=seed, w_net=w_net)
    # ——— 补强基线（对应已发表工作的机制，见各策略类的 docstring）———
    if name == "P2C":
        return lambda seed: P2CStrategy(seed=seed)
    if name == "P2CNet":
        return lambda seed: P2CNetStrategy(seed=seed, w_net=w_net)
    if name == "CongestionOnly":
        return lambda seed: CongestionOnlyStrategy(seed=seed, w_net=w_net)
    if name == "Bottleneck":
        return lambda seed: BottleneckStrategy(seed=seed)
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
    if name in ("NetworkAware2Adapt", "NetworkAware2AdaptHard"):
        # 阈值消融用：每个变体可以在同一个 cell 里各带一套阈值/门控参数，
        # 于是"改阈值"是逐请求配对比较，而不是跨 cell 的聚合对比。
        # 【坑】键名必须与 NetworkAwareAdaptiveStrategy.__init__ 认的名字**逐字一致**：
        # 认不出的键会被透传给父类并抛 TypeError（不会静默），但写错**名字**（例如
        # 用旧的 tau_q_soft）会一路走默认值——参数网格静默失效，这类错不报错。
        cls = (NetworkAwareHardSwitchStrategy if name == "NetworkAware2AdaptHard"
               else NetworkAwareAdaptiveStrategy)
        kw: dict = {}
        for key in ("decay", "tau_q", "tau_n", "tau_n_dyn", "tau_q_hard",
                    "tau_n_hard", "tau_q_lin", "tau_n_lin", "link_floor",
                    "vol_drift", "net_share_floor",
                    "drift_gate"):
            if key in spec:
                kw[key] = spec[key]
        # 【静默失效防护】白名单之外的键一律报错，而不是默默丢掉。
        # 踩过：给 `AdaptChannel` 的消融写了 `tau_n` / `link_floor` 两个键，但白名单里没有它们，
        # 于是四档变体**跑出来和主方法逐位相同**、消融表里整整四行是"0.0±0.0"——
        # 看上去像"参数不敏感"这个科学结论，其实是参数没生效。**不报错的错最贵。**
        allowed = set(kw) | {"name", "label", "w_net", "fallback_staleness",
                             "fallback_w_net"}
        unknown = set(spec) - allowed
        if unknown:
            raise ValueError(
                f"策略规格里有无法传递的键 {sorted(unknown)}（策略={name}）。"
                f"要么它是白名单里的参数（{sorted(kw)}），要么是元数据 "
                f"（name/label/w_net/fallback_*）。这是为了防止参数网格静默失效。")
        return cls(seed=seed, w_net=w, **kw)
    return strategy_factory(name, w)(seed)


def _safe_dirs() -> list[str]:
    """从 ROOT 向上走到仓库根，返回这一串目录，用作 `safe.directory`。

    为什么不是只放行 ROOT：**ROOT 是 `sim/`，仓库根是它的上一级**。只写
    `safe.directory=<sim>` 时 git 依旧判定"这个仓库不在安全清单里"而拒绝，
    于是 `code_version()` 静默回落成 "uncommitted"——第一次提交做完后发现字段
    没变，就是这个原因（第二次修复）。手工验证过：重复 `-c` 对多值键是追加，
    两级同时放行即可读到 HEAD。
    """
    dirs, p = [], ROOT
    for _ in range(8):
        dirs.append(p.as_posix())
        if (p / ".git").exists() or p.parent == p:
            break
        p = p.parent
    return dirs


def _git(args: list[str]) -> str | None:
    """调用 git，并显式放行本仓库为安全目录。

    本机 git 会因 "dubious ownership"（仓库所在文件系统不记录属主）拒绝读 HEAD，
    裸调用 `git rev-parse` 直接 fatal，manifest 里的 code_version 会白写成
    "uncommitted"。
    """
    cmd = ["git"]
    for d in _safe_dirs():
        cmd += ["-c", f"safe.directory={d}"]
    try:
        return subprocess.check_output(
            [*cmd, *args], cwd=ROOT, text=True,
            stderr=subprocess.DEVNULL).strip()
    except Exception:
        return None


def code_version() -> str:
    """当前代码版本（短哈希）。没有提交或读不到时回落为 "uncommitted"。"""
    return _git(["rev-parse", "--short", "HEAD"]) or "uncommitted"


def code_dirty_files() -> str:
    """工作树里未提交的文件清单（换行分隔）。用于判断"这批结果对应的是干净版本还是改了一半"。"""
    out = _git(["status", "--porcelain"])
    return out if out else ""


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
    # 【细变量③】链路与节点的配对本可以按"对齐模式"重排（默认 anti = 保持现状）。
    # 必须在建节点与建轨迹之前重排，两者用的链路列表才会一致。
    nodes = cell_nodes(cell)          # 唯一入口：构造方式与记录指标时用的完全一致
    cond = cell_network_condition(cell)
    cond["_n_nodes"] = cell["n_nodes"]
    cond = apply_alignment(cond, cell.get("align"))
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
        # 【细变量②】观测的聚合窗口：真实监控报的是"过去 W 秒的平均"而不是瞬时值
        window_queue_s=float(cell.get("window_queue_s", 0.0)),
        window_network_s=float(cell.get("window_network_s", 0.0)),
        # 【必修】保守模型的并发降速倍率必须在这里进 SimConfig。
        # 2026-10-12 的电池只把 --service-slowdown 写进了 cell 字典/CSV 列，
        # 没传进 SimConfig，于是 8,600 次仿真全部按理想模型跑、产物却声称
        # 用了 linear/pressure（见 CONS_BATTERY_CLEANUP_2026-10-20.md）。
        # regtest_slowdown.py 的 real_path_check 现在用真实 run_cell 路径钉住它。
        service_slowdown=str(cell.get("service_slowdown", "none")),
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
        strat = make_strategy(s, cell["seed"], w_net)
        result = run(workload, nodes, strat, cfg, cell["seed"], trajectory)
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
        # 自适应策略会额外报告"实际施加了多大权重"（见 Strategy.stats_columns）
        row.update(strat.stats_columns())
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
    elif mode == "align":
        # 【细变量③】"谁算得快"与"谁网线粗"的对齐度：网络信息有没有价值的机制解释。
        # 预测（跑之前定好）：aligned（两者一致）时收益≈0 甚至有害——因为按"谁闲"挑
        # 与按"网线好"挑挑到同一台；anti（现状，两者相反）时收益最大；rot1 居中。
        # 这个预测如果成立，就把"收益从哪来"说清楚了；不成立也要照实报。
        # 种子数由 ALIGN_SEEDS 控制（见常量注释：种子的作用是"独立的负载实现"）
        for align, wl, net, seed in itertools.product(
                ["anti", "rot1", "aligned"], ["mixed", "data_heavy"],
                ["degraded", "unstable"], SEEDS[:ALIGN_SEEDS]):
            c = cell(wl, net, "fresh", seed)
            c["align"] = align
            # 把"对齐度"量化后随 cell 落盘：这样分析侧不必硬编码 0/0.67/1.0，
            # 换节点数（4/8/16）时这个数会自己变，趋势检验仍然成立。
            _nodes = cell_nodes(c)
            c["align_index"] = alignment_index(_nodes)      # 旧指标（保留对照）
            c["align_rank_corr"] = rank_corr(_nodes)        # 新指标：Spearman 秩相关
            cells.append(c)
    elif mode == "alignscale":
        # 【细变量③ × 规模】对齐度在 4/8/16 节点上是否仍然成立。
        # 为什么这一组特别自然：**节点数一变，"谁算得快"与"谁网线粗"的配对结构自己就变了**——
        # 算力按 2 的幂循环（周期 4）、链路按 3 条循环（周期 3），两者的相位差随节点数变化，
        # 于是 `alignment_index()` 在各档上取到不同的值（不再只是 0 / 0.67 / 1 三档）。
        # 这一组回答的是："对齐度效应是 3 节点的小样本巧合，还是在规模上仍在？"
        for n_nodes, align, seed in itertools.product(
                [4, 8, 16], ["anti", "rot1", "aligned"], SEEDS[:ALIGN_SEEDS]):
            c = cell("mixed", "degraded", "fresh", seed, n_nodes=n_nodes)
            c["align"] = align
            _nodes = cell_nodes(c)
            c["align_index"] = alignment_index(_nodes)      # 旧指标（保留对照）
            c["align_rank_corr"] = rank_corr(_nodes)        # 新指标：Spearman 秩相关
            cells.append(c)
    elif mode == "staleness2d":
        # 【细变量①】两条通道年龄的二维联合扫描。
        # 现有两条 1D 扫描（`staleness` 模式的 queue 扫描 / net 扫描）无法回答"搭配"问题，
        # 而且 net 扫描把队列 lag 钉在 1 秒，导致那条曲线其实主要在测队列（已实测确认）。
        # 这里两个 lag 都显式设定，网络固定 unstable（静态网络的链路 lag 精确无效）。
        # 负载：mixed + data_heavy。种子数同样升到 10（统计单位是"独立负载实现"，
        # 3 个种子给不出跨实现的置信区间——与 align 实验同一口径）。
        for lag_q, lag_n, wl, seed in itertools.product(
                STALENESS2D_QUEUE, STALENESS2D_NETWORK,
                ["mixed", "data_heavy"], SEEDS[:ALIGN_SEEDS]):
            c = cell(wl, "unstable", "fresh", seed)
            c["staleness"] = f"q{lag_q:g}_n{lag_n:g}"   # 档位标签，便于分组
            c["staleness_queue_s"] = lag_q
            c["staleness_network_s"] = lag_n
            cells.append(c)
    elif mode == "avewindow":
        # 【细变量②】观测的**聚合窗口**：真实监控报的是"过去 W 秒的平均"而不是瞬时值。
        # 与 staleness 的区别是本质性的，这是本 mode 存在的全部理由：
        #   lag  = 看到的是**旧**值，但那个值是当时真实的（延迟失真，越旧越钝）；
        #   window = 看到的是**被平均过**的值，尖峰被抹平（信息失真，**方向相反**：
        #            忙的节点看起来不忙，于是活继续往它身上派）。
        # 设计：lag 固定为 0（隔离变量，不让两种失真混在一起）；窗口分两条通道各自扫。
        #   ① 队列通道窗口（最可能有效，因为队列是秒级变化的）
        #   ② 链路通道窗口（只在时变网络上有意义——静态网络平均=原值）
        # 注意：两条窗口列**每个 cell 都写全**（另一条显式写 0.0），
        # 否则缺的那一列在 CSV 里是 NaN，分组统计时要额外兜底（踩过一次）。
        for w_q, wl, net, seed in itertools.product(
                WINDOW_GRID, ["mixed", "data_heavy"], ["degraded", "unstable"], SEEDS[:3]):
            c = cell(wl, net, "fresh", seed)
            c["window_queue_s"] = w_q
            c["window_network_s"] = 0.0
            cells.append(c)
        for w_n, wl, seed in itertools.product(WINDOW_GRID, ["mixed", "data_heavy"], SEEDS[:3]):
            c = cell(wl, "unstable", "fresh", seed)      # 时变网络才有链路窗口可谈
            c["window_queue_s"] = 0.0
            c["window_network_s"] = w_n
            cells.append(c)
    elif mode == "method":
        # 【V3 方法贡献】降权规则对照：同一个 cell 里跑满五条臂，比较是**逐请求配对**的。
        #   ① LeastLoad   —— 完全不信网络（下界）
        #   ② NetworkAware2 —— 完全信（不管多旧）
        #   ③ AdaptHard   —— 硬开关：年龄超阈值就整块关掉
        #   ④ AdaptLinear —— 线性降权：到尺度就归零
        #   ⑤ AdaptSplit  —— 主方法：指数降权，且把链路信息拆成"忙闲"与"粗细"两块
        # 为什么必须放同一个 cell：这五条臂的差别在"状态有多旧"时才显现，而陈旧场景下
        # 单次仿真的**轨迹是混沌的**（实测：只改前 2.5% 请求的决策就能让 4000 请求的
        # p95 差 17%）。跨 cell 比会把这种噪声当成效应，所以配对比较 + 10 个种子。
        # 新鲜度水平覆盖四种真实运维配置：全新鲜 / 队列监控 1 s / 队列监控 5 s /
        # 链路监控 15 s / 两条通道都慢（队列 5 s + 链路 60 s）。
        for (lag_q, lag_n), wl, net, seed in itertools.product(
                METHOD_FRESHNESS, ["mixed", "data_heavy"],
                ["degraded", "unstable"], SEEDS[:ALIGN_SEEDS]):
            c = cell(wl, net, "fresh", seed)
            c["staleness"] = f"q{lag_q:g}_n{lag_n:g}"
            c["staleness_queue_s"] = lag_q
            c["staleness_network_s"] = lag_n
            c["strategies"] = METHOD_STRATEGIES
            c["pair_candidates"] = ["AdaptSplit", "AdaptLinear", "AdaptHard",
                                    "NetworkAware2"]
            c["pair_baselines"] = ["LeastLoad", "NetworkAware2"]
            cells.append(c)
    elif mode == "method_ch":
        # 【修订版规则】按通道降权（`decay="channel"`）的独立一批。
        # 格子与 `method` 模式**逐字段相同**（同负载/网络/两条 lag/种子/请求数），
        # 而负载与轨迹都只由 (工作负载类, 种子, 请求数) 决定，所以两批的请求流是**逐位相同**的，
        # 跨批按 (cell 条件, 种子, req_id) 配对是合法的——这正是"同一 cell 内配对"的等价形式。
        # 之所以不直接改 `method` 模式重跑：那批产物已经落盘成证据了，append-only 不覆盖它。
        for (lag_q, lag_n), wl, net, seed in itertools.product(
                METHOD_FRESHNESS, ["mixed", "data_heavy"],
                ["degraded", "unstable"], SEEDS[:ALIGN_SEEDS]):
            c = cell(wl, net, "fresh", seed)
            c["staleness"] = f"q{lag_q:g}_n{lag_n:g}"
            c["staleness_queue_s"] = lag_q
            c["staleness_network_s"] = lag_n
            c["strategies"] = METHOD_CH_STRATEGIES
            c["pair_candidates"] = ["AdaptChannel", "AdaptLinkOnly"]
            c["pair_baselines"] = ["LeastLoad", "NetworkAware2"]
            cells.append(c)
    elif mode == "method_ch_ablate":
        # 第二版规则的参数消融：格子与 `method_ablate` 同构（3 档新鲜度，省算力且不丢信息）。
        for (lag_q, lag_n), wl, net, seed in itertools.product(
                METHOD_ABLATE_FRESHNESS, ["mixed", "data_heavy"],
                ["degraded", "unstable"], SEEDS[:ALIGN_SEEDS]):
            c = cell(wl, net, "fresh", seed)
            c["staleness"] = f"q{lag_q:g}_n{lag_n:g}"
            c["staleness_queue_s"] = lag_q
            c["staleness_network_s"] = lag_n
            c["strategies"] = METHOD_CH_ABLATE_ARMS
            c["pair_candidates"] = [a["label"] for a in METHOD_CH_ABLATE_ARMS
                                    if isinstance(a, dict)]
            c["pair_baselines"] = ["LeastLoad", "NetworkAware2"]
            cells.append(c)
    elif mode == "baselines":
        # 补强基线对照：8 条臂放在**同一个 cell 内**跑，因此两两比较是逐请求配对的。
        # 新鲜度只取 fresh / stale 两档（P2C 类基线的意义在于"看得少能不能同样好"，
        # 不需要再扫一遍完整陈旧度网格；陈旧度的影响在 §16 已单独刻画）。
        for fresh, wl, net, seed in itertools.product(
                MAIN_STALENESS_LEVELS, ["mixed", "data_heavy"],
                ["degraded", "unstable"], SEEDS[:ALIGN_SEEDS]):
            c = cell(wl, net, fresh, seed)
            c["strategies"] = BASELINE_STRATEGIES
            # 配对关系：每个新基线 × 两条参照臂（不看网络的 、看网络的）
            c["pair_candidates"] = ["P2C", "P2CNet", "CongestionOnly", "Bottleneck",
                                    "NetworkAware2"]
            c["pair_baselines"] = ["LeastLoad", "NetworkAware2"]
            cells.append(c)
    elif mode == "method_ablate":
        # 阈值消融：见 METHOD_ABLATE_ARMS 的注释（一次只动一个参数）。
        for (lag_q, lag_n), wl, net, seed in itertools.product(
                METHOD_ABLATE_FRESHNESS, ["mixed", "data_heavy"],
                ["degraded", "unstable"], SEEDS[:ALIGN_SEEDS]):
            c = cell(wl, net, "fresh", seed)
            c["staleness"] = f"q{lag_q:g}_n{lag_n:g}"
            c["staleness_queue_s"] = lag_q
            c["staleness_network_s"] = lag_n
            c["strategies"] = METHOD_ABLATE_ARMS
            c["pair_candidates"] = [a["label"] for a in METHOD_ABLATE_ARMS
                                    if isinstance(a, dict)]
            c["pair_baselines"] = ["LeastLoad", "NetworkAware2"]
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
                             "ablation", "scale", "rho", "fallback", "bandwidth",
                             "avewindow", "staleness2d", "align", "alignscale",
                             "method", "method_ablate", "method_ch",
                             "method_ch_ablate", "baselines"])
    ap.add_argument("--out", default=str(ROOT / "results/matrix"))
    ap.add_argument("--requests", type=int, default=N_REQUESTS)
    # 保守模型：并发降速倍率（CONS_MODEL_PREREG 冻结，2026-10-12）。
    # none（默认）= 理想服务时间（与旧产物一致）；linear/pressure = 冻结的 L/P 两版。
    ap.add_argument("--service-slowdown", choices=["none", "linear", "pressure"],
                    default="none")
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
    # 服务时间倍率是 run 级旋钮：写进每个 cell（runs.csv 自动带列，作为产物溯源）
    for _c in cells:
        _c["service_slowdown"] = args.service_slowdown
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
        # 保守模型倍率（none = 理想服务时间 = 与旧产物同一模型）
        "service_slowdown": args.service_slowdown,
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

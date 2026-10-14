"""节点运行状态的历史记录，用于构造"过期"的观测快照。

staleness 的实现方式：
  调度器在时刻 t 决策时，看到的是 t-lag 时刻的状态。
  要做到这一点必须能回看历史，因此这里按事件记录每次状态变化。

记录点是状态真正改变的时刻（派发、计算结束），不是定时采样，
因此查找是精确的，不存在采样误差。lag=0 时退化为"完全实时的状态"。
"""

from __future__ import annotations

import bisect
import random
import threading
from dataclasses import dataclass, field

from .model import NodeSpec


@dataclass
class NodeStateHistory:
    """单个节点的 (时刻, 未完成请求数, 未完成工作量, 链路积压字节) 序列。

    `link_pending_bytes` 是 V2 链路争用模型引入的：节点链路是一条 FIFO 管道，
    共用它的传输互相排队，因此"链路上还压着多少字节"成了一个与算力 backlog 并列的
    状态量。它走的是**队列通道**的陈旧（和 pending 一样按事件记录），
    因为它是本节点侧的排队状态，不是链路本身的带宽/时延。
    """

    node_id: int
    times: list[float] = field(default_factory=list)
    pendings: list[int] = field(default_factory=list)
    works: list[float] = field(default_factory=list)
    link_bytes: list[float] = field(default_factory=list)
    # 前缀积分（cumulative integral）：cum_x[i] = 从 times[0] 积分到 times[i] 的 ∫x dt。
    # 为什么需要：聚合窗口每次决策都要算窗口内的平均值，而窗口里有几百条记录
    # （30 秒窗口 × 20 QPS ≈ 600 条 × 3 节点 × 2 万请求 → 上千万次操作）。
    # 有了前缀积分，任意窗口的平均值只需两次二分查找 + 两段线性外推，O(log n)。
    cum_p: list[float] = field(default_factory=list)
    cum_w: list[float] = field(default_factory=list)
    cum_b: list[float] = field(default_factory=list)
    # 每个节点自己的锁：窗口查询与记录可能同时发生（多策略共享同一份历史）
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, t: float, pending: int, pending_work: float,
               link_pending_bytes: float = 0.0) -> None:
        with self._lock:
            if self.times:      # 上一条记录的值一直持续到 t，先把它积分进去
                dt = max(0.0, t - self.times[-1])
                self.cum_p.append(self.cum_p[-1] + self.pendings[-1] * dt)
                self.cum_w.append(self.cum_w[-1] + self.works[-1] * dt)
                self.cum_b.append(self.cum_b[-1] + self.link_bytes[-1] * dt)
            else:
                self.cum_p.append(0.0)
                self.cum_w.append(0.0)
                self.cum_b.append(0.0)
            self.times.append(t)
            self.pendings.append(pending)
            self.works.append(pending_work)
            self.link_bytes.append(link_pending_bytes)

    def _integral_at(self, t: float) -> tuple[float, float, float]:
        """∫ 从 times[0] 到 t（阶梯函数：某条记录的值持续到下一次记录）。t 早于首条记录时为 0。"""
        if not self.times or t <= self.times[0]:
            return 0.0, 0.0, 0.0
        i = bisect.bisect_right(self.times, t) - 1
        dt = t - self.times[i]
        return (self.cum_p[i] + self.pendings[i] * dt,
                self.cum_w[i] + self.works[i] * dt,
                self.cum_b[i] + self.link_bytes[i] * dt)

    def at(self, t: float) -> tuple[int, float, float]:
        """取 t 时刻（或之前最近一次变化）的状态。

        t 早于第一条记录时返回 (0, 0.0, 0.0)：仿真开始时节点是空的、链路上没有积压。
        """
        if not self.times:
            return 0, 0.0, 0.0
        idx = bisect.bisect_right(self.times, t) - 1
        if idx < 0:
            return 0, 0.0, 0.0
        return self.pendings[idx], self.works[idx], self.link_bytes[idx]

    def avg_over(self, t_start: float, t_end: float) -> tuple[float, float, float]:
        """窗口 [t_start, t_end] 内的**时间加权平均**状态。

        这是真实监控系统报的东西：`avg_over_time(metric[30s])` 给的是窗口内的平均，
        而不是某一瞬间的值。**它会把尖峰抹平**——而调度要防的恰恰是尖峰：
        某节点在 1 秒内涌进一批活，窗口平均后看起来只是"略忙"，
        于是网络/排队信息被"平滑掉了"，决策照旧把活派过去。
        这正是"窗口"和"延迟(lag)"的关键区别：
        延迟是**看到旧的恒定值**，窗口是**看到一个已经不准的平均值**（方向相反的风险）。

        【一阶简化】真实遥测的聚合算子可能是 min/mean/p95/rate 等，这里只实现算术平均
        （时间加权，因为状态是阶梯函数）。换算子是一个可后续扫描的细变量。
        窗口 ≤ 0 或没有历史时退化成瞬时值 `at(t_end)`。
        """
        if t_end <= t_start or not self.times:
            p, w, b = self.at(t_end)
            return float(p), float(w), float(b)
        span = t_end - t_start
        # 前缀积分之差 = 窗口内的精确积分；再除以窗长就是时间加权平均。
        # 等价性有单测式核对：与"逐段累加"的朴素实现逐位比对（见本文件底部说明与
        # `verify_gates` 的窗口检查）。
        i1 = self._integral_at(t_start)
        i2 = self._integral_at(t_end)
        return ((i2[0] - i1[0]) / span, (i2[1] - i1[1]) / span, (i2[2] - i1[2]) / span)


def make_histories(nodes: list[NodeSpec]) -> dict[int, NodeStateHistory]:
    return {n.node_id: NodeStateHistory(node_id=n.node_id) for n in nodes}

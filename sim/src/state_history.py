"""节点运行状态的历史记录，用于构造"过期"的观测快照。

staleness 的实现方式：
  调度器在时刻 t 决策时，看到的是 t-lag 时刻的状态。
  要做到这一点必须能回看历史，因此这里按事件记录每次状态变化。

记录点是状态真正改变的时刻（派发、计算结束），不是定时采样，
因此查找是精确的，不存在采样误差。lag=0 时退化为"完全实时的状态"。
"""

from __future__ import annotations

import bisect
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

    def record(self, t: float, pending: int, pending_work: float,
               link_pending_bytes: float = 0.0) -> None:
        self.times.append(t)
        self.pendings.append(pending)
        self.works.append(pending_work)
        self.link_bytes.append(link_pending_bytes)

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


def make_histories(nodes: list[NodeSpec]) -> dict[int, NodeStateHistory]:
    return {n.node_id: NodeStateHistory(node_id=n.node_id) for n in nodes}

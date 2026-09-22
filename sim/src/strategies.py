"""调度策略。

四个策略共用同一个接口：给一份【调度器可见的状态】，返回一个 node_id。

关键设计：策略只看到 NodeView（"调度器以为的状态"），
看不到 NodeSpec（"真实状态"）。因此 staleness 是观测层的事，
策略代码完全不需要知道它存在，也不可能绕过它偷看真实状态。
"""

from __future__ import annotations

import random
from abc import ABC, abstractmethod
from dataclasses import dataclass

from .model import NodeView, Request, network_time, request_bytes


@dataclass
class Observation:
    """调度器在一次决策时能看到的状态快照。

    注意语义：这不是"真实状态"，而是"调度器以为的状态"。
    当 lag > 0 时，这个快照来自过去，并且它自己知道这一点
    （staleness 属性），因此可以据此设计回退逻辑。
    """

    nodes: list[NodeView]
    observed_at: float              # 快照对应的仿真时刻
    true_time: float                # 当前真实时刻
    bytes_per_context_token: float = 4.0
    bytes_per_generated_token: float = 4.0

    @property
    def staleness(self) -> float:
        return self.true_time - self.observed_at


class Strategy(ABC):
    """所有策略的共同基类。"""

    name: str = "strategy"

    def __init__(self, seed: int = 0):
        self.rng = random.Random(seed)
        # 决策日志：论文里的"决策解释记录"，也是调试 staleness 的唯一手段
        self.decisions: list[dict] = []
        self.log_decisions = False

    @abstractmethod
    def choose(self, req: Request, obs: Observation) -> int:
        """返回选中的 node_id。"""

    def describe(self) -> str:
        return self.name

    def _log(self, req: Request, obs: Observation, chosen: int,
             scores: dict[int, float] | None = None) -> None:
        if self.log_decisions:
            self.decisions.append({
                "req_id": req.req_id,
                "true_time": obs.true_time,
                "observed_at": obs.observed_at,
                "staleness": obs.staleness,
                "chosen": chosen,
                "scores": scores,
            })


class RandomStrategy(Strategy):
    """随机选择。作为下界参考，不是强基线。"""

    name = "Random"

    def choose(self, req: Request, obs: Observation) -> int:
        node = self.rng.choice(obs.nodes)
        self._log(req, obs, node.node_id)
        return node.node_id


class LeastQueueStrategy(Strategy):
    """选择当前未完成请求数最少的节点。"""

    name = "LeastQueue"

    def choose(self, req: Request, obs: Observation) -> int:
        best = min(obs.nodes, key=lambda n: (n.pending, n.node_id))
        self._log(req, obs, best.node_id)
        return best.node_id


class LeastLoadStrategy(Strategy):
    """选择积压排空时间估计最短的节点。

    load 的定义见 NodeView.backlog_time。
    注意这是"资源余量"视角，不等于"这个请求会最快完成"——
    Sia (SOSP'23) 正是靠这个区别说明朴素负载感知会损失 goodput。
    """

    name = "LeastLoad"

    def choose(self, req: Request, obs: Observation) -> int:
        best = min(obs.nodes, key=lambda n: (n.backlog_time, n.node_id))
        self._log(req, obs, best.node_id)
        return best.node_id


class NetworkAwareStrategy(Strategy):
    """LeastLoad + 网络代价项。

    评分函数：
        score(node) = backlog_time(node) + w_net * network_cost(node, req)

    其中 network_cost = 输入传输时间 + 输出传输时间，用【观测到的】链路状态计算。

    【关键设计】w_net = 0 时，这个策略的排序与 LeastLoad 完全一致。
    因此消融实验里可以直接验证这一点：如果 w_net=0 的结果与 LeastLoad
    不是逐位相同，说明实现里有 bug。任何"网络感知的收益"也就都能
    干净地归因到网络项，而不是被别的差异污染。
    """

    name = "NetworkAware"

    def __init__(self, seed: int = 0, w_net: float = 1.0,
                 fallback_staleness: float | None = None,
                 fallback_w_net: float = 0.0):
        super().__init__(seed=seed)
        self.w_net = w_net
        # RQ2 的自适应回退：观测过期超过 fallback_staleness 秒时，
        # 把网络项权重降到 fallback_w_net（默认 0，即退回纯 LeastLoad）。
        self.fallback_staleness = fallback_staleness
        self.fallback_w_net = fallback_w_net

    def effective_w_net(self, obs: Observation) -> float:
        if self.fallback_staleness is None:
            return self.w_net
        return (self.fallback_w_net if obs.staleness > self.fallback_staleness
                else self.w_net)

    def choose(self, req: Request, obs: Observation) -> int:
        bpc = obs.bytes_per_context_token
        bpg = obs.bytes_per_generated_token
        in_bytes, out_bytes = request_bytes(
            req.context_tokens, req.generated_tokens, bpc, bpg)
        req.in_bytes, req.out_bytes = in_bytes, out_bytes

        w = self.effective_w_net(obs)
        scores: dict[int, float] = {}
        for n in obs.nodes:
            t_in, t_out = network_time(in_bytes, out_bytes, n)
            scores[n.node_id] = n.backlog_time + w * (t_in + t_out)

        chosen = min(scores, key=lambda k: (scores[k], k))
        self._log(req, obs, chosen, scores)
        return chosen

    def describe(self) -> str:
        if self.fallback_staleness is not None:
            return (f"{self.name}(w={self.w_net:g},fallback@{self.fallback_staleness:g}s"
                    f"->w={self.fallback_w_net:g})")
        return f"{self.name}(w_net={self.w_net:g})"


class NetworkAwareContentionStrategy(NetworkAwareStrategy):
    """V2 机制贡献：把**算力队列**与**链路队列**两条积压放在一起看。

    评分函数（**看结果前冻结**）：
        score(node) = backlog_time(node)                  # 算力队列的排空时间
                    + w_net × (link_drain_time(node)      # 链路队列的排空时间
                             + t_in(node, req) + t_out(node, req))

    其中 `link_drain_time = 链路上积压的字节 / 带宽`，与 `backlog_time = 未完成工作量 /
    算力×槽位` **完全对称**——一个看算力队列，一个看链路队列。

    与 v1 的 `NetworkAware` 唯一的区别就是多了 `link_drain_time` 这一项。
    这不是调参：它对应一个可解释的机制（"链路也是被争用的资源，别人已经压在这条
    链路上的字节同样会推迟我这次传输"），而且它用的是**观测到**的链路积压，
    因此天然会被陈旧污染——这正好是 RQ2 要研究的对象。

    【门 2】w_net = 0 时评分退化为 backlog_time，排序与 LeastLoad 逐位相同。
    """

    name = "NetworkAware2"

    def choose(self, req: Request, obs: Observation) -> int:
        bpc = obs.bytes_per_context_token
        bpg = obs.bytes_per_generated_token
        in_bytes, out_bytes = request_bytes(
            req.context_tokens, req.generated_tokens, bpc, bpg)
        req.in_bytes, req.out_bytes = in_bytes, out_bytes

        w = self.effective_w_net(obs)
        scores: dict[int, float] = {}
        for n in obs.nodes:
            t_in, t_out = network_time(in_bytes, out_bytes, n)
            scores[n.node_id] = n.backlog_time + w * (
                n.link_drain_time + t_in + t_out)

        chosen = min(scores, key=lambda k: (scores[k], k))
        self._log(req, obs, chosen, scores)
        return chosen


STRATEGIES: dict[str, type[Strategy]] = {
    RandomStrategy.name: RandomStrategy,
    LeastQueueStrategy.name: LeastQueueStrategy,
    LeastLoadStrategy.name: LeastLoadStrategy,
    NetworkAwareStrategy.name: NetworkAwareStrategy,
    NetworkAwareContentionStrategy.name: NetworkAwareContentionStrategy,
}

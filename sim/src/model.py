"""核心数据模型：请求、节点、服务时间。

本文件里的每一个公式都是【模型假设】，不是实测事实。
凡是估算出来的系数，必须在论文的 Threats to Validity 里说明来源。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Request:
    """一个 AI 推理请求。"""

    req_id: int
    arrival: float          # 到达时刻（仿真秒，相对第一个请求）
    context_tokens: int     # trace 的 ContextTokens，prefill 工作量代理
    generated_tokens: int   # trace 的 GeneratedTokens，decode 工作量代理

    # 运行时填充
    node_id: int = -1
    dispatch: float = -1.0
    transfer_in_end: float = -1.0   # 输入传输完成、可以进入队列的时刻
    service_start: float = -1.0     # 真正开始计算
    compute_end: float = -1.0       # 计算结束、开始回传
    finish: float = -1.0            # 回传结束，端到端完成
    work: float = 0.0               # token-units，见 compute_work()
    in_bytes: float = 0.0           # 需要传到节点上的字节数
    out_bytes: float = 0.0          # 需要传回来的字节数
    net_in: float = 0.0             # 输入传输耗时（秒）
    net_out: float = 0.0            # 输出回传耗时（秒）
    deadline: float = float("inf")  # SLO 目标（秒，相对到达时刻）

    @property
    def wait(self) -> float:
        """排队时间：输入传完到开始计算。（不含网络时间）"""
        if self.service_start < 0 or self.transfer_in_end < 0:
            return float("nan")
        return max(0.0, self.service_start - self.transfer_in_end)

    @property
    def network_time(self) -> float:
        """本次请求花在网络传输上的总时间（输入 + 输出）。"""
        if self.finish < 0 or self.compute_end < 0:
            return float("nan")
        t_in = self.transfer_in_end - self.dispatch
        t_out = self.finish - self.compute_end
        return max(0.0, t_in) + max(0.0, t_out)

    @property
    def sojourn(self) -> float:
        """端到端完成时间：到达 -> 完成。这是主指标。"""
        if self.finish < 0:
            return float("nan")
        return self.finish - self.arrival

    @property
    def slo_met(self) -> bool:
        """是否在 deadline 内完成。未完成的请求一律算 miss。"""
        if self.finish < 0:
            return False
        return (self.finish - self.arrival) <= self.deadline


@dataclass
class NodeSpec:
    """一个异构计算节点。

    这是【一阶简化模型】：
      - 节点有 slots 个并发槽位；
      - 每个槽位按 compute_rate（token-units/秒）独立推进；
      - 超过 slots 的请求排队，不共享算力（即不建模槽位间的算力争抢）；
      - 网络用单节点上的 (latency, bandwidth) 表示，不建模链路共享与拥塞排队。
    共享算力、链路拥塞和显存约束是后续版本要加的，现在故意留简单。
    """

    node_id: int
    compute_rate: float     # token-units / 秒 / 槽位
    slots: int = 1
    latency: float = 0.0005     # 单程基础时延（秒）。默认 0.5 ms
    bandwidth: float = 1.25e9   # 链路带宽（字节/秒）。默认 10 Gbps

    # 运行时状态
    pending: int = 0            # 已派发未完成的请求数（排队 + 执行）
    pending_work: float = 0.0   # 已派发未完成的 token-units 总量

    @property
    def backlog_time(self) -> float:
        """把当前积压全部排空的乐观估计（秒）。

        定义：pending_work / (compute_rate * slots)。
        这是一个上界式估计，因为它把正在执行的请求按剩余全量工作量计入。
        """
        return self.pending_work / (self.compute_rate * self.slots)


# ---------------------------------------------------------------------------
# 服务时间模型
# ---------------------------------------------------------------------------

def compute_work(context_tokens: int, generated_tokens: int,
                 prefill_coef: float, decode_coef: float) -> float:
    """把 token 数折算成与算力可比的"工作量"（token-units）。

    假设：prefill 与 decode 的工作量对 token 数近似线性，系数不同。
    这是工程估算，不是从 GPU 实测标定出来的；正式实验前应尽量用
    公开数据或小规模实测校准 prefill_coef / decode_coef。
    """
    return prefill_coef * context_tokens + decode_coef * generated_tokens


def service_time(work: float, node: NodeSpec) -> float:
    """单个请求在该节点上占用一个槽位所需的执行时间（秒）。"""
    rate = node.compute_rate
    if rate <= 0:
        raise ValueError(f"node {node.node_id} 的 compute_rate 必须为正")
    return work / rate


@dataclass
class NodeView:
    """调度器在一次决策时"以为"的某个节点状态。

    这就是 staleness 的着力点：View 的字段来自 t - lag 时刻，
    而仿真用的是 t 时刻的真实状态。两者之差就是信息落后的代价。

    刻意与 NodeSpec 保持同名字段，使策略代码完全不感知 staleness 的存在。
    """

    node_id: int
    pending: int
    pending_work: float
    compute_rate: float
    slots: int
    latency: float
    bandwidth: float
    # V2 链路争用：链路上还压着多少字节（走队列通道的陈旧，与 pending 同源）。
    # 默认 0 使 v1 的观测构造方式保持可用。
    link_pending_bytes: float = 0.0

    @property
    def backlog_time(self) -> float:
        return self.pending_work / (self.compute_rate * self.slots)

    @property
    def link_drain_time(self) -> float:
        """链路排空时间：链路上积压的字节按当前带宽排空需要多久。

        与 `backlog_time` 完全对称——一个是算力队列的排空时间，一个是链路队列的排空时间。
        这就是 V2 机制贡献能被解释的部分：调度器同时看两条队列的积压。
        """
        if self.bandwidth <= 0:
            return float("inf")
        return self.link_pending_bytes / self.bandwidth

    @classmethod
    def from_spec(cls, spec: NodeSpec, latency: float, bandwidth: float) -> "NodeView":
        return cls(
            node_id=spec.node_id,
            pending=spec.pending,
            pending_work=spec.pending_work,
            compute_rate=spec.compute_rate,
            slots=spec.slots,
            latency=latency,
            bandwidth=bandwidth,
        )


# ---------------------------------------------------------------------------
# SLO / deadline —— trace 不提供，必须作为实验假设声明
# ---------------------------------------------------------------------------

def compute_deadline(
    context_tokens: int,
    generated_tokens: int,
    slo_base: float,
    slo_per_1k_tokens: float,
) -> float:
    """给请求设定一个 deadline（秒）。

    【这是一个实验假设，不是 trace 原生事实。】
    形式：deadline = 固定额度 + 按 token 规模线性放宽。

    为什么用"每千 token"而不是固定值：
    长请求天然需要更久，固定 deadline 会变成"惩罚长请求"，
    miss rate 就只是在量 token 分布，而不是在量调度质量。
    """
    return slo_base + slo_per_1k_tokens * (context_tokens + generated_tokens) / 1000.0


# ---------------------------------------------------------------------------
# 网络模型 —— 决定 RQ1 有没有答案的关键参数在这里
# ---------------------------------------------------------------------------

def request_bytes(
    context_tokens: int,
    generated_tokens: int,
    bytes_per_context_token: float,
    bytes_per_generated_token: float,
) -> tuple[float, float]:
    """请求需要跨网络搬运的字节数。

    【这是整篇论文最敏感的建模选择】，因为它直接决定网络代价的量级：

      - bytes_per_context_token = 4      ：只传 prompt 的 token id（int32）。
                                           1 万 token 只有 40 KB，网络代价可忽略。
      - bytes_per_context_token = 3.3e5  ：传 KV cache（约 70B 模型量级，
                                           每 token 约 0.33 MB）。
                                           1 万 token 是 3.3 GB，网络代价主导。

    也就是说：同样的调度器，在"共置服务"下网络感知毫无价值，
    在"分离式/KV 传输"架构下网络感知是决定性的。
    这个参数必须作为实验因子扫描，而不是拍一个数当成事实。
    """
    return (
        context_tokens * bytes_per_context_token,
        generated_tokens * bytes_per_generated_token,
    )


def network_time(
    in_bytes: float,
    out_bytes: float,
    node,
    link=None,
) -> tuple[float, float]:
    """输入传输时间、输出传输时间（秒）。

    模型：t = 单程时延 + 字节数 / 带宽，输入输出各算一次单程时延。

    link 参数就是 staleness 的关键接入点：
      - 传真实链路状态 -> 得到真实传输时间（仿真用）
      - 传观测到的链路状态 -> 得到调度器以为的传输时间（策略用）
    link 为 None 时退回用 node 自带的 latency/bandwidth。

    简化之处：没有对链路做排队建模，多个请求同时使用同一节点链路时不互相拖慢。
    这个简化会低估高负载下的网络代价，必须在 Threats to Validity 里声明。
    """
    if link is None:
        link = node
    if link.bandwidth <= 0:
        raise ValueError(f"node {node.node_id} 的 bandwidth 必须为正")
    t_in = link.latency + in_bytes / link.bandwidth
    t_out = link.latency + out_bytes / link.bandwidth
    return t_in, t_out


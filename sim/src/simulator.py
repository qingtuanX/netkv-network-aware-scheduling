"""SimPy 离散事件仿真器。

事件顺序（每个请求）：
    到达 -> [用过期状态做调度决策] -> 输入传输 -> 排队 -> 计算 -> 输出回传 -> 完成

公平性前提：请求流由 workload_seed 决定，与策略无关。
因此四种策略看到的是逐位相同的到达时间和 token 数。

staleness 的实现：
    仿真用 t 时刻的【真实】链路状态算传输时间；
    策略用 t-lag 时刻的【观测】状态做决策。两者严格分离。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import simpy

from .model import (
    NodeSpec,
    NodeView,
    Request,
    compute_deadline,
    compute_work,
    network_time,
    request_bytes,
    service_slowdown_factor,
    service_time,
)
from .network import LinkState, NetworkTrajectory, StaticNetwork
from .state_history import NodeStateHistory, make_histories
from .strategies import Observation, Strategy


@dataclass
class SimConfig:
    prefill_coef: float = 1.0     # 每个 context token 的工作量权重
    decode_coef: float = 4.0      # 每个 generated token 的工作量权重
    warmup_fraction: float = 0.1  # 前 10% 请求不计入统计（暖机）
    max_requests: int | None = None
    bytes_per_context_token: float = 4.0    # 见 model.request_bytes 的说明
    bytes_per_generated_token: float = 4.0

    # --- 保守模型：并发越高、单请求越慢（CONS_MODEL_PREREG 冻结，2026-10-12）---
    # "none"（默认）= 理想服务时间模型，与注入前逐位一致（regtest_slowdown.py 钉住）；
    # "linear" / "pressure" = 冻结的 L/P 两版（model.service_slowdown_factor）。
    # 只改服务时间倍率：不加策略、不改网络/负载/统计规则；SLO 定义保持不变。
    service_slowdown: str = "none"

    # --- RQ2 因子 ---
    # 两条陈旧通道的时间尺度【完全不同】，必须分开：
    #   队列状态：服务时间是亚秒到秒级，队列在 1 秒内就可能完全变样；
    #   链路状态：拥塞/带宽变化是几十秒量级，监控周期通常 10–60 秒。
    # 用一个 lag 同时控制两者，会把"调度器看不到实时队列"这个更致命的问题
    # 掩盖掉网络陈旧的真实效应（实测已确认：30 秒队列陈旧能让 p95 涨 36 倍）。
    staleness_queue: float = 0.0    # 队列/负载观测落后真实状态的秒数
    staleness_network: float = 0.0  # 链路状态观测落后真实状态的秒数
    stale_queue: bool = True        # 是否启用队列陈旧（用于拆分来源）
    stale_network: bool = True      # 是否启用链路陈旧

    # --- 【细变量②】观测的**聚合窗口**：真实监控报的是"过去 W 秒的平均"，不是瞬时值 ---
    # 与 lag 的区别（这是本变量存在的意义）：
    #   lag  = 看到的是**旧**值，但那个值是当时真实的（确定性失真）；
    #   window = 看到的是**被平均过**的值——尖峰被抹平，于是"某节点刚涌进一批活"
    #            这件事在观测里变得看不见（信息失真，方向与 lag 不同的是：
    #            lag 让人反应慢，window 让人**看错**）。
    # 真实系统里两者同时存在：Prometheus 每 15 s 采一次、每次报 avg_over_time(metric[30s])。
    # 两条通道各有各的窗口，默认 0 = 退化成当前的瞬时值（不影响既有结论）。
    window_queue_s: float = 0.0     # 队列/链路积压的聚合窗口秒数
    window_network_s: float = 0.0   # 链路带宽/时延的聚合窗口秒数
    # 网络通道窗口的采样点数：轨迹是连续函数，这里按等距采样做算术平均。
    # 【一阶简化】真实遥测可能报 min/p95/rate 等不同算子，这里只做算术平均。
    window_samples: int = 8

    # --- SLO（实验假设，不是 trace 原生事实）---
    # 绝对不能用一个固定秒数当 deadline：网络代价由节点选择决定，
    # 固定值会让 miss rate 饱和在 0/1，失去信息量（实测已确认饱和）。
    # relative 模式把 SLO 定义为"该请求在最理想节点上的完成时间 × 倍数"，
    # 含义是"允许比理论最优慢几倍"，这样 miss rate 才在量调度质量。
    slo_mode: str = "relative"          # "relative" | "absolute"
    slo_multiplier: float = 3.0         # relative 模式：相对理论最优的容忍倍数
    slo_base: float = 1.0               # absolute 模式参数
    slo_per_1k_tokens: float = 0.35


@dataclass
class RunResult:
    strategy: str
    seed: int
    requests: list[Request]
    makespan: float
    node_count: int
    staleness_queue: float = 0.0
    staleness_network: float = 0.0

    def measured(self, warmup_fraction: float) -> list[Request]:
        """排除暖机期与未完成的请求。"""
        done = [r for r in self.requests if r.finish >= 0]
        done.sort(key=lambda r: r.arrival)
        cut = int(len(done) * warmup_fraction)
        return done[cut:]


def run(
    workload: list[tuple[int, int, int]],
    nodes: list[NodeSpec],
    strategy: Strategy,
    config: SimConfig,
    seed: int,
    trajectory: NetworkTrajectory | None = None,
) -> RunResult:
    """跑一次仿真。

    workload: [(arrival_s, context_tokens, generated_tokens), ...]
    trajectory: 真实链路状态随时间的变化；None 表示用节点的标称链路（静态）。
    返回的 requests 带完整时间分解，可直接做 per-request 审计。
    """
    env = simpy.Environment()
    lag_q = config.staleness_queue
    lag_n = config.staleness_network

    if trajectory is None:
        # 【坑】这里原来有一句 `from .network import LinkState, StaticNetwork`（局部 import）。
        # 一旦函数里出现对 LinkState 的**局部**赋值/导入，Python 就把整个函数里的 LinkState
        # 都当成局部名 → 走别的分支（trajectory 非空）时那句引用会抛
        # `NameError: cannot access free variable 'LinkState'`。
        # 已改为在模块顶部统一导入，函数里不再 import。
        trajectory = StaticNetwork(
            {n.node_id: LinkState(n.latency, n.bandwidth) for n in nodes},
            label="nominal",
        )

    for n in nodes:
        n.pending = 0
        n.pending_work = 0.0

    histories: dict[int, NodeStateHistory] = make_histories(nodes)
    resources = {n.node_id: simpy.Resource(env, capacity=n.slots) for n in nodes}
    by_id = {n.node_id: n for n in nodes}

    # --- V2 链路争用：每个节点的链路是一条 FIFO 管道（容量 1）---
    # 共用同一条管道的传输互相排队。这是 v1 缺的一环：链路不排队时，网络时间只是
    # 链路状态的确定性函数，"网络感知"退化成"按本请求大小挑最快链路"，
    # 既没有可供陈旧的链路负载状态，`normal`（静态）网络也必然逐位相同。
    # 【一阶简化】一条管道同时承担进出两个方向（不区分全双工），
    # 且排队规则是 FIFO 而非处理器共享——两者都会让网络代价偏保守。
    link_pipes = {n.node_id: simpy.Resource(env, capacity=1) for n in nodes}
    link_pending = {n.node_id: 0.0 for n in nodes}

    all_requests: list[Request] = []
    done_flag = {"makespan": 0.0}

    def build_observation(now: float) -> Observation:
        """构造调度器看到的快照。

        两条通道各用各的 lag：队列看 now-lag_q，链路看 now-lag_n。
        observed_at 取两者中较早的时刻，作为"这份快照有多旧"的统一口径，
        自适应回退据此判断可信度。
        """
        t_q = max(0.0, now - lag_q)
        t_n = max(0.0, now - lag_n)
        t_obs = min(t_q, t_n)
        w_q = config.window_queue_s
        w_n = config.window_network_s

        views: list[NodeView] = []
        for spec in nodes:
            if w_q > 0:
                # 聚合窗口：报的是窗口内的平均（时间加权），不是瞬时值。
                # 注意即使 lag=0 也要走历史，因为"平均"必须有历史才能算。
                pending, work, link_bytes = histories[spec.node_id].avg_over(t_q - w_q, t_q)
                pending = int(round(pending))
            elif config.stale_queue and lag_q > 0:
                pending, work, link_bytes = histories[spec.node_id].at(t_q)
            else:
                pending, work = spec.pending, spec.pending_work
                link_bytes = link_pending[spec.node_id]

            if w_n > 0:
                # 链路通道的窗口：对轨迹做等距采样的算术平均（带宽与时延分别平均）。
                # 静态网络下平均 = 原值（精确无影响），只有时变网络才起作用。
                ns = max(2, int(config.window_samples))
                t_from = max(0.0, t_n - w_n)
                lat_sum = bw_sum = 0.0
                for k in range(ns):
                    tk = t_from + (t_n - t_from) * k / (ns - 1)
                    st = trajectory.state(spec.node_id, tk)
                    lat_sum += st.latency
                    bw_sum += st.bandwidth
                link = LinkState(lat_sum / ns, bw_sum / ns)
            else:
                link = trajectory.state(spec.node_id,
                                        t_n if (config.stale_network and lag_n > 0) else now)
            views.append(NodeView(
                node_id=spec.node_id,
                pending=pending,
                pending_work=work,
                compute_rate=spec.compute_rate,
                slots=spec.slots,
                latency=link.latency,
                bandwidth=link.bandwidth,
                link_pending_bytes=link_bytes,
            ))

        return Observation(
            nodes=views,
            observed_at=t_obs,
            true_time=now,
            # 【细变量①】两条通道各自的快照时刻：队列用 t_q、链路用 t_n。
            # 策略据此可以用**不同的年龄阈值**门控两条通道（实测两者耐旧度差约一个数量级）。
            observed_at_queue=t_q,
            observed_at_network=t_n,
            bytes_per_context_token=config.bytes_per_context_token,
            bytes_per_generated_token=config.bytes_per_generated_token,
        )

    def serve(req: Request, node: NodeSpec) -> "simpy.events.Process":
        # --- 阶段 1：把输入传到节点上（占链路管道，不占算力槽位）---
        # 传输时间 = 单程时延 + 字节数/带宽，用【真正开始传输那一刻】的真实链路状态。
        # 注意必须用 network_time() 而不是自己写 字节/带宽：v2 第一版漏了时延项，
        # 被白盒门精确抓出（差值恰好等于 2×单程时延：Azure v2 下 4 µs）。
        with link_pipes[node.node_id].request() as pipe:
            yield pipe
            req.net_in = network_time(req.in_bytes, req.out_bytes, node,
                                      trajectory.state(node.node_id, env.now))[0]
            yield env.timeout(req.net_in)
            link_pending[node.node_id] -= req.in_bytes
            histories[node.node_id].record(env.now, node.pending, node.pending_work,
                                           link_pending[node.node_id])
        req.transfer_in_end = env.now

        # --- 阶段 2：排队 + 计算（占算力槽位）---
        with resources[node.node_id].request() as slot:
            yield slot
            req.service_start = env.now
            st = service_time(req.work, node)
            if config.service_slowdown != "none":
                # 并发计数 = 此刻占用该节点槽位的请求数（含自己）；冻结定义见 model.py
                st *= service_slowdown_factor(resources[node.node_id].count,
                                              config.service_slowdown)
            yield env.timeout(st)
            req.compute_end = env.now
            node.pending -= 1
            node.pending_work -= req.work
            histories[node.node_id].record(env.now, node.pending, node.pending_work,
                                           link_pending[node.node_id])

        # --- 阶段 3：把输出传回来（同样占链路管道，用回传时刻的真实链路状态）---
        with link_pipes[node.node_id].request() as pipe:
            yield pipe
            true_out = network_time(req.in_bytes, req.out_bytes, node,
                                    trajectory.state(node.node_id, env.now))[1]
            req.net_out = true_out
            link_pending[node.node_id] -= req.out_bytes
            yield env.timeout(true_out)
            histories[node.node_id].record(env.now, node.pending, node.pending_work,
                                           link_pending[node.node_id])
        req.finish = env.now
        done_flag["makespan"] = max(done_flag["makespan"], env.now)

    def dispatcher():
        for i, (arrival, ctx, gen) in enumerate(workload):
            if config.max_requests is not None and i >= config.max_requests:
                break
            if env.now < arrival:
                yield env.timeout(arrival - env.now)

            req = Request(
                req_id=i,
                arrival=float(arrival),
                context_tokens=int(ctx),
                generated_tokens=int(gen),
            )
            req.work = compute_work(ctx, gen, config.prefill_coef, config.decode_coef)
            req.in_bytes, req.out_bytes = request_bytes(
                ctx, gen, config.bytes_per_context_token,
                config.bytes_per_generated_token)

            if config.slo_mode == "relative":
                # 该请求在最理想节点上的完成时间：取所有节点里最快的一个，
                # 用【真实当前】链路状态与空队列假设计算，作为 SLO 的下界。
                ideal = min(
                    network_time(req.in_bytes, req.out_bytes, n,
                                 trajectory.state(n.node_id, env.now))[0]
                    + network_time(req.in_bytes, req.out_bytes, n,
                                   trajectory.state(n.node_id, env.now))[1]
                    + service_time(req.work, n)
                    for n in nodes)
                req.deadline = config.slo_multiplier * ideal
            else:
                req.deadline = compute_deadline(
                    ctx, gen, config.slo_base, config.slo_per_1k_tokens)

            node_id = strategy.choose(req, build_observation(env.now))
            node = by_id[node_id]

            req.node_id = node_id
            req.dispatch = env.now
            # 真实传输时间在 serve() 里按"真正开始传输那一刻"的带宽算——
            # V2 引入链路争用后，开始时刻不再等于派发时刻。这里只占位。
            req.net_in = 0.0
            req.net_out = 0.0

            node.pending += 1
            node.pending_work += req.work
            # 输入字节立刻计入链路积压：调度器据此看到"这条链路上还压着多少字节"
            link_pending[node_id] += req.in_bytes
            histories[node_id].record(env.now, node.pending, node.pending_work,
                                      link_pending[node_id])
            all_requests.append(req)
            env.process(serve(req, node))

    env.process(dispatcher())
    env.run()

    return RunResult(
        strategy=strategy.describe(),
        seed=seed,
        requests=all_requests,
        makespan=done_flag["makespan"],
        node_count=len(nodes),
        staleness_queue=lag_q,
        staleness_network=lag_n,
    )

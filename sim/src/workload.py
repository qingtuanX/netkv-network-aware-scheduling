"""四类 AI 推理负载。

论文规划要求 4 类负载：计算密集、数据密集、低时延在线、混合。

优先从 Azure trace 里**按 token 规模切片**得到这四类——这样负载特征来自
公开数据，而不是我编的参数。trace 未就绪时退回合成负载，并明确标注。

切片的做法：按 context/generated token 的分位数把请求分到四类。
例如"数据密集"= context token 在上四分位、"计算密集"= generated token 在上四分位。
具体阈值写在配置里，主实验前冻结。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .trace_loader import _summarize, generate_synthetic


@dataclass
class ClassSpec:
    """一类负载的切分规则与目标到达率。

    ctx_q / gen_q 是分位区间 (lo, hi]，用于从真实 trace 里筛请求。
    synthetic 参数用于 trace 未就绪时的替代负载。
    """

    name: str
    ctx_q: tuple[float, float]
    gen_q: tuple[float, float]
    target_qps: float
    synthetic: dict


# 四类负载的定义。分位数阈值来自对 token 分布的划分，主实验前冻结。
WORKLOAD_CLASSES: dict[str, ClassSpec] = {
    # 计算密集：输出长（decode 主导），输入短
    "compute_heavy": ClassSpec(
        name="compute_heavy",
        ctx_q=(0.0, 0.5),
        gen_q=(0.5, 1.0),
        target_qps=30.0,
        synthetic=dict(context_mu=6.2, context_sigma=1.0,
                       generated_mu=5.6, generated_sigma=0.8),
    ),
    # 数据密集：输入长（prefill 与传输主导），输出短
    "data_heavy": ClassSpec(
        name="data_heavy",
        ctx_q=(0.5, 1.0),
        gen_q=(0.0, 0.5),
        target_qps=30.0,
        synthetic=dict(context_mu=8.4, context_sigma=1.2,
                       generated_mu=3.0, generated_sigma=0.7),
    ),
    # 低时延在线：两者都小，到达率高，对调度开销最敏感
    "low_latency": ClassSpec(
        name="low_latency",
        ctx_q=(0.0, 0.35),
        gen_q=(0.0, 0.35),
        target_qps=30.0,
        synthetic=dict(context_mu=5.0, context_sigma=0.7,
                       generated_mu=2.2, generated_sigma=0.6),
    ),
    # 混合：不做筛选，保持 trace 原貌
    "mixed": ClassSpec(
        name="mixed",
        ctx_q=(0.0, 1.0),
        gen_q=(0.0, 1.0),
        target_qps=30.0,
        synthetic=dict(context_mu=7.0, context_sigma=1.4,
                       generated_mu=4.5, generated_sigma=1.3),
    ),
}


def build_from_trace(
    workload: list[tuple[float, int, int]],
    spec: ClassSpec,
    target_qps: float | None = None,
) -> list[tuple[float, int, int]]:
    """从真实 trace 里按 token 分位区间筛出一类负载，并把到达率缩放到目标值。

    返回 (arrival, context_tokens, generated_tokens) 列表。
    """
    if not workload:
        return []

    arr = np.array([w[0] for w in workload], dtype=float)
    ctx = np.array([w[1] for w in workload], dtype=float)
    gen = np.array([w[2] for w in workload], dtype=float)

    ctx_lo, ctx_hi = np.quantile(ctx, spec.ctx_q)
    gen_lo, gen_hi = np.quantile(gen, spec.gen_q)

    # 边界包含在中位数一侧，避免同一个请求落进两类
    ctx_lo = ctx_lo + (1e-9 if spec.ctx_q[0] > 0 else 0)
    gen_lo = gen_lo + (1e-9 if spec.gen_q[0] > 0 else 0)
    mask = ((ctx > ctx_lo) & (ctx <= ctx_hi)
            & (gen > gen_lo) & (gen <= gen_hi))
    if not mask.any():
        return []

    sel = np.flatnonzero(mask)
    t = arr[sel]
    t = t - t[0]

    qps = target_qps if target_qps is not None else spec.target_qps
    if qps and len(t) > 1:
        span = t[-1]
        if span > 0:
            # 目标到达率下重排到达时间，保留原始间隔的相对结构（含突发）
            scale = (len(t) / qps) / span
            t = t * scale

    return [(float(t[i]), int(ctx[sel[i]]), int(gen[sel[i]]))
            for i in range(len(sel))]


def build_synthetic(spec: ClassSpec, n_requests: int, seed: int,
                    target_qps: float | None = None):
    """trace 未就绪时的替代负载。返回 (workload, stats)。

    【标记为 synthetic】不能在论文里当作 Azure trace 的结果。
    """
    qps = target_qps if target_qps is not None else spec.target_qps
    # 用每请求工作量反推所需到达率，使各类负载的 rho 可比，
    # 这样"策略差异"不会被"负载强度不同"混淆。
    return generate_synthetic(
        n_requests=n_requests,
        base_qps=qps,
        burstiness=1.5,
        seed=seed,
        **spec.synthetic,
    )

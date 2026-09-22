"""指标计算。

主指标：p50 / p95 / p99 端到端完成时间、吞吐、平均等待时间。
辅助：节点利用率、按节点分布、时间分解。

统计原则：先给分布和分位数，再给区间估计；不给"最好的一次运行"。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .model import Request


def request_records(requests: list[Request]) -> pd.DataFrame:
    """把请求列表转成长表 —— 原始证据，一次都不许手改。"""
    rows = [{
        "req_id": r.req_id,
        "node_id": r.node_id,
        "arrival": r.arrival,
        "dispatch": r.dispatch,
        "service_start": r.service_start,
        "finish": r.finish,
        "context_tokens": r.context_tokens,
        "generated_tokens": r.generated_tokens,
        "work": r.work,
        "in_bytes": r.in_bytes,
        "out_bytes": r.out_bytes,
        "net_in": r.net_in,
        "net_out": r.net_out,
        "net_total": r.network_time,
        "deadline": r.deadline,
        "slo_met": r.slo_met,
        "wait": r.wait,
        "sojourn": r.sojourn,
    } for r in requests]
    return pd.DataFrame(rows)


def summarize(requests: list[Request], makespan: float,
              node_count: int, node_slots_total: int) -> dict:
    """汇总指标。requests 应已排除暖机期。"""
    if not requests:
        return {}

    sojourn = np.array([r.sojourn for r in requests], dtype=float)
    wait = np.array([r.wait for r in requests], dtype=float)
    net = np.array([r.network_time for r in requests], dtype=float)
    sojourn = sojourn[np.isfinite(sojourn)]
    wait = wait[np.isfinite(wait)]
    net = net[np.isfinite(net)]

    busy = float(np.sum([r.compute_end - r.service_start for r in requests
                         if r.compute_end >= 0 and r.service_start >= 0]))
    capacity = makespan * node_slots_total if makespan > 0 and node_slots_total > 0 else 0.0

    return {
        "n_requests": len(requests),
        "sojourn_mean": float(sojourn.mean()),
        "sojourn_p50": float(np.percentile(sojourn, 50)),
        "sojourn_p95": float(np.percentile(sojourn, 95)),
        "sojourn_p99": float(np.percentile(sojourn, 99)),
        "sojourn_max": float(sojourn.max()),
        "wait_mean": float(wait.mean()),
        "wait_p95": float(np.percentile(wait, 95)),
        "net_mean": float(net.mean()) if len(net) else float("nan"),
        "net_share": float(net.mean() / sojourn.mean()) if len(net) and sojourn.mean() > 0 else float("nan"),
        "slo_miss_rate": float(np.mean([not r.slo_met for r in requests])),
        "throughput_qps": len(requests) / makespan if makespan > 0 else float("nan"),
        "node_utilization": busy / capacity if capacity > 0 else float("nan"),
    }


def bootstrap_ci(values: np.ndarray, statistic=np.median,
                 n_boot: int = 1000, alpha: float = 0.05, seed: int = 0) -> tuple[float, float]:
    """Bootstrap 置信区间。用于报告不确定性，不代替显著性检验。"""
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    if len(values) == 0:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(values), size=(n_boot, len(values)))
    stats = statistic(values[idx], axis=1)
    lo = float(np.percentile(stats, 100 * alpha / 2))
    hi = float(np.percentile(stats, 100 * (1 - alpha / 2)))
    return (lo, hi)

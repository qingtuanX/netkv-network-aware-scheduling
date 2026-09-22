"""RQ1 探针：网络感知到底有没有用？

这是一个【探索性】实验，目的是在投入正式主实验之前，
先判断 RQ1 是否存在可观测的、有机制的答案，以及边界在哪里。

它不产生论文结论（当前用的是合成负载），只回答三个问题：
  1. NetworkAware 相对 LeastLoad 在什么条件下有收益？
  2. 收益的上界在哪里（即"完全知道网络状态"能省多少）？
  3. 什么条件下收益为零？——如果存在一大片"收益为零"的区域，
     那本身就是一个有价值的边界结论，而不是失败。

核心实验设计：NetworkAware 是 LeastLoad 的严格超集，
评分函数只多了一项 w_net × 网络代价。因此任何差异都干净地归因到网络项。

用法：
    sim/.venv/Scripts/python.exe probe_rq1.py
    sim/.venv/Scripts/python.exe probe_rq1.py --qps 25
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

from src.metrics import request_records, summarize          # noqa: E402
from src.model import NodeSpec, compute_work                # noqa: E402
from src.simulator import SimConfig, run                    # noqa: E402
from src.strategies import (                                # noqa: E402
    LeastLoadStrategy,
    LeastQueueStrategy,
    NetworkAwareStrategy,
    RandomStrategy,
)
from src.trace_loader import generate_synthetic             # noqa: E402

# --- 网络场景 -------------------------------------------------------------
# 关键区分：算力最强的节点，网络是好还是坏。
# 如果最好算力和最好网络总是同一个节点，LeastLoad 本来就会选对，
# 网络感知没有额外信息可用 —— 这本身就是一条边界结论。
GOOD = dict(latency=0.0005, bandwidth=1.25e9)    # 0.5 ms, 10 Gbps
MID = dict(latency=0.002, bandwidth=1.25e8)      # 2 ms,   1 Gbps
POOR = dict(latency=0.005, bandwidth=1.25e7)     # 5 ms,   100 Mbps

COMPUTE_RATES = [16000.0, 8000.0, 4000.0]        # 算力递减
SLOTS = [4, 4, 4]

NETWORK_SCENARIOS = {
    # 算力强的节点网络也最好 —— 网络感知无事可做
    "aligned": [GOOD, MID, POOR],
    # 算力最强的节点网络最差 —— 网络感知应该能救回来
    "anti": [POOR, MID, GOOD],
    # 全网一致 —— 网络感知没有任何信号
    "uniform": [GOOD, GOOD, GOOD],
}

# --- 每 context token 的传输字节数 ----------------------------------------
# 这是决定"网络感知值不值钱"的量级开关，必须扫，不能拍。
DATA_VOLUMES = [
    (4.0, "prompt ids only (4 B/token)"),
    (4.0e3, "compressed activations (4 KB/token)"),
    (3.3e4, "32 KB/token"),
    (3.3e5, "KV cache ~70B (330 KB/token)"),
]

STRATEGY_FACTORIES = {
    "Random": lambda seed: RandomStrategy(seed=seed),
    "LeastQueue": lambda seed: LeastQueueStrategy(seed=seed),
    "LeastLoad": lambda seed: LeastLoadStrategy(seed=seed),
    "NetworkAware(w=0)": lambda seed: NetworkAwareStrategy(seed=seed, w_net=0.0),
    "NetworkAware": lambda seed: NetworkAwareStrategy(seed=seed, w_net=1.0),
}


def build_nodes(scenario: str) -> list[NodeSpec]:
    nets = NETWORK_SCENARIOS[scenario]
    return [
        NodeSpec(node_id=i, compute_rate=COMPUTE_RATES[i], slots=SLOTS[i], **nets[i])
        for i in range(3)
    ]


def sig_test(a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
    """Wilcoxon signed-rank：同一请求在两种策略下的完成时间是否配对不同。

    配对是合法的：所有策略跑的是逐位相同的请求流，req_id 一一对应。
    """
    d = a - b
    d = d[np.isfinite(d)]
    if len(d) == 0 or np.allclose(d, 0):
        return 1.0, 0.0
    try:
        res = stats.wilcoxon(a, b)
        # Cliff's delta 作为效应量
        gt = np.sum(d > 0)
        lt = np.sum(d < 0)
        delta = float((gt - lt) / len(d))
        return float(res.pvalue), delta
    except Exception:
        return float("nan"), float("nan")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--qps", type=float, default=None,
                    help="到达率；默认自动标定到 rho≈0.55")
    ap.add_argument("--requests", type=int, default=20000)
    ap.add_argument("--seeds", type=int, nargs="*", default=[1, 2, 3])
    ap.add_argument("--out", default=str(ROOT / "results/raw"))
    ap.add_argument("--fig", default=str(ROOT / "figures/rq1_probe.png"))
    args = ap.parse_args()

    seeds = args.seeds
    run_id = f"rq1probe_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"

    # ---------- 校准到达率，使 rho 落在有压力的区间 ----------
    if args.qps is not None:
        qps = args.qps
    else:
        probe_nodes = build_nodes("uniform")
        probe_cfg = SimConfig(prefill_coef=1.0, decode_coef=4.0, max_requests=5000)
        wl_probe, _ = generate_synthetic(n_requests=5000, base_qps=20.0, seed=12345)
        mean_work = float(np.mean([compute_work(c, g, 1.0, 4.0)
                                   for _, c, g in wl_probe]))
        capacity = sum(n.compute_rate * n.slots for n in probe_nodes)
        qps = 0.55 * capacity / mean_work
        print(f"自动标定：平均工作量 {mean_work:,.0f} token-units，"
              f"系统容量 {capacity:,.0f} token-units/s，"
              f"目标 rho=0.55 -> 到达率 {qps:.2f} QPS")

    # ---------- 主循环 ----------
    rows: list[dict] = []
    paired: dict[tuple[str, float, str], np.ndarray] = {}

    for scenario in NETWORK_SCENARIOS:
        nodes = build_nodes(scenario)
        for bpc, bpc_label in DATA_VOLUMES:
            sim_cfg = SimConfig(
                prefill_coef=1.0, decode_coef=4.0, warmup_fraction=0.1,
                max_requests=args.requests,
                bytes_per_context_token=bpc, bytes_per_generated_token=4.0,
            )
            for seed in seeds:
                workload, _ = generate_synthetic(
                    n_requests=args.requests, base_qps=qps, burstiness=2.0,
                    seed=1000 + seed)
                for sname, factory in STRATEGY_FACTORIES.items():
                    r = run(workload, nodes, factory(seed=seed), sim_cfg, seed=seed)
                    measured = r.measured(sim_cfg.warmup_fraction)
                    slots_total = sum(n.slots for n in nodes)
                    row = summarize(measured, r.makespan, len(nodes), slots_total)
                    row.update({
                        "run_id": run_id, "scenario": scenario,
                        "bytes_per_ctx_token": bpc, "data_label": bpc_label,
                        "strategy": sname, "seed": seed, "qps": qps,
                    })
                    rows.append(row)

                    # 按 req_id 对齐，供配对检验
                    rec = request_records(measured).sort_values("req_id")
                    paired[(scenario, bpc, sname, seed)] = rec["sojourn"].to_numpy()

            # 逐场景打印
            sub = pd.DataFrame([r for r in rows
                                if r["scenario"] == scenario
                                and r["bytes_per_ctx_token"] == bpc])
            agg = sub.groupby("strategy")[["sojourn_p50", "sojourn_p95", "net_share"]].median()
            ll = agg.loc["LeastLoad", "sojourn_p95"]
            na = agg.loc["NetworkAware", "sojourn_p95"]
            print(f"[{scenario:<8} {bpc_label:<32}] "
                  f"net_share={agg.loc['LeastLoad','net_share']:.3f}  "
                  f"LL p95={ll:9.3f}s  NA p95={na:9.3f}s  "
                  f"Δ={na/ll-1:+8.2%}")

    df = pd.DataFrame(rows)
    Path(args.out).mkdir(parents=True, exist_ok=True)
    summary_path = Path(args.out) / f"{run_id}_summary.csv"
    df.to_csv(summary_path, index=False)

    # ---------- 验证：w_net=0 必须与 LeastLoad 逐位相同 ----------
    print()
    print("=" * 74)
    print("实现验证：NetworkAware(w=0) 是否与 LeastLoad 逐位相同")
    print("=" * 74)
    all_same = True
    for scenario in NETWORK_SCENARIOS:
        for bpc, _ in DATA_VOLUMES:
            for seed in seeds:
                a = paired[(scenario, bpc, "LeastLoad", seed)]
                b = paired[(scenario, bpc, "NetworkAware(w=0)", seed)]
                same = len(a) == len(b) and np.array_equal(a, b)
                all_same &= same
    print(f"所有 ({len(NETWORK_SCENARIOS)}×{len(DATA_VOLUMES)}×{len(seeds)}) 组合: "
          f"{'全部逐位相同 —— 消融干净，网络项的贡献被正确隔离' if all_same else '存在差异 —— 实现有 bug！'}")

    # ---------- 收益边界表 ----------
    agg = df.groupby(["scenario", "bytes_per_ctx_token", "strategy"])[
        ["sojourn_p50", "sojourn_p95", "sojourn_p99", "net_share", "net_mean"]
    ].median().reset_index()

    print()
    print("=" * 74)
    print("RQ1 探针结果：NetworkAware 相对 LeastLoad 的 p95 变化（负数=更好）")
    print("=" * 74)
    header = f"{'网络场景':<10} {'数据量':<34} {'net占比':>8} {'LL p95':>10} {'NA p95':>10} {'Δp95':>9} {'p值':>9} {'Cliff δ':>8}"
    print(header)
    print("-" * len(header))

    table_rows = []
    for scenario in NETWORK_SCENARIOS:
        for bpc, label in DATA_VOLUMES:
            s = agg[(agg.scenario == scenario) & (agg.bytes_per_ctx_token == bpc)]
            s = s.set_index("strategy")
            ll = s.loc["LeastLoad", "sojourn_p95"]
            na = s.loc["NetworkAware", "sojourn_p95"]
            share = s.loc["LeastLoad", "net_share"]

            pvals, deltas = [], []
            for seed in seeds:
                p, d = sig_test(paired[(scenario, bpc, "NetworkAware", seed)],
                                paired[(scenario, bpc, "LeastLoad", seed)])
                pvals.append(p)
                deltas.append(d)
            pmin, dmed = min(pvals), float(np.median(deltas))

            print(f"{scenario:<10} {label:<34} {share:>8.3f} {ll:>10.3f} {na:>10.3f} "
                  f"{na/ll-1:>+9.2%} {pmin:>9.2e} {dmed:>+8.3f}")
            table_rows.append({
                "scenario": scenario, "data": label, "net_share": share,
                "ll_p95": ll, "na_p95": na, "delta": na / ll - 1,
                "p_value": pmin, "cliffs_delta": dmed,
            })

    # ---------- 图 ----------
    fig, axes = plt.subplots(1, 2, figsize=(13, 4.8))
    markers = {"aligned": "o", "anti": "s", "uniform": "^"}
    for scenario in NETWORK_SCENARIOS:
        xs, ys = [], []
        for bpc, _ in DATA_VOLUMES:
            s = agg[(agg.scenario == scenario) & (agg.bytes_per_ctx_token == bpc)]
            s = s.set_index("strategy")
            xs.append(s.loc["LeastLoad", "net_share"])
            ys.append(s.loc["NetworkAware", "sojourn_p95"] /
                      s.loc["LeastLoad", "sojourn_p95"] - 1)
        axes[0].plot(xs, ys, marker=markers[scenario], label=scenario, lw=1.6)

    axes[0].axhline(0, color="k", lw=0.8, ls="--")
    axes[0].set_xscale("log")
    axes[0].set_xlabel("network share of completion time (LeastLoad)")
    axes[0].set_ylabel("Δ p95 of NetworkAware vs LeastLoad")
    axes[0].set_title("Does network awareness pay off?\n(negative = NetworkAware better)")
    axes[0].grid(alpha=0.25)
    axes[0].legend(fontsize=8)

    for scenario in NETWORK_SCENARIOS:
        xs, ys = [], []
        for bpc, _ in DATA_VOLUMES:
            s = agg[(agg.scenario == scenario) & (agg.bytes_per_ctx_token == bpc)]
            s = s.set_index("strategy")
            xs.append(s.loc["LeastLoad", "sojourn_p95"])
            ys.append(s.loc["NetworkAware", "sojourn_p95"])
        axes[1].plot(xs, ys, marker=markers[scenario], label=scenario, lw=1.6)

    lim = max(axes[1].get_xlim()[1], axes[1].get_ylim()[1])
    axes[1].plot([0, lim], [0, lim], "k--", lw=0.8, label="y = x")
    axes[1].set_xscale("log")
    axes[1].set_yscale("log")
    axes[1].set_xlabel("LeastLoad p95 (s)")
    axes[1].set_ylabel("NetworkAware p95 (s)")
    axes[1].set_title("NetworkAware vs LeastLoad, p95")
    axes[1].grid(alpha=0.25, which="both")
    axes[1].legend(fontsize=8)

    fig.tight_layout()
    Path(args.fig).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(args.fig, dpi=160)

    manifest = {
        "run_id": run_id,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "kind": "EXPLORATORY PROBE (synthetic workload, not paper results)",
        "qps": qps,
        "seeds": seeds,
        "n_requests": args.requests,
        "network_scenarios": {k: v for k, v in NETWORK_SCENARIOS.items()},
        "compute_rates": COMPUTE_RATES,
        "data_volumes_bytes_per_ctx_token": [d[0] for d in DATA_VOLUMES],
        "ablation_check_passed": bool(all_same),
    }
    with open(Path(args.out) / f"{run_id}_manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print()
    print(f"原始数据: {summary_path}")
    print(f"图:       {args.fig}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

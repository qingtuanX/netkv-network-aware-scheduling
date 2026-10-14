"""节点规模 × 对齐度：直接测"完全不看网络的策略是不是碰巧就挑到快网线"。

用法（在 `sim/` 下执行）：

    .venv/Scripts/python.exe analysis/measure_link_choice.py
    .venv/Scripts/python.exe analysis/measure_link_choice.py --requests 20000 --seeds 1 2 3

产物：`results/analysis_v2/link_choice_by_scale.csv`（每行 = 一个节点数 × 一个策略）。

【这个探针要回答什么】§18 观察到"对齐度效应随节点数变小"，有两种解释：
  (A) **调度回旋余地变大**：候选节点一多，随便挑一台也不会太差，所以网络信息能改进的空间变小；
  (B) **操纵范围收窄**：能构造出的"排名相关 ρ"本身随节点数变小（算力按周期 4 循环、链路按 n 铺开），
      于是"aligned 档"在大 n 上根本不是真的 aligned。
(A) 可以直接测：**完全忽略网络的 `LeastLoad`，它挑中的机器在"网线粗细"里排第几？**
  若节点数越多、这个名次越靠前（越接近 1），说明 LeastLoad 在"碰巧"拿到好网线，(A) 成立。

【为什么值得单独做一个脚本】它需要**跑仿真**（不是读 CSV），而 `analyze.py` 的硬约束是
"只从 CSV/JSON 读数、零硬编码结论"。所以按 `measure_components.py` 的同一模式：
独立探针负责产数，`analyze.py` 负责读数与判读。
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")   # Windows 控制台默认 GBK
    except Exception:                                        # noqa: BLE001
        pass

from src.simulator import SimConfig, run                     # noqa: E402


def _load_matrix_module():
    """把 `experiments/run_matrix.py` 当模块加载（它不在 `src` 包里）。

    复用它的 `cell_nodes` / `cell_network_condition` / `apply_alignment` / `make_trajectory`
    / `build_synthetic_cell_workload` / `rank_corr`——**必须复用**，否则探针里的节点与链路
    构造一旦和矩阵实验分叉，两边就不是同一个配置（踩过一次：`apply_alignment` 漏传节点数，
    落盘指标与真实仿真不是同一配置，趋势符号都反了）。
    """
    spec = importlib.util.spec_from_file_location(
        "rm_probe", ROOT / "experiments" / "run_matrix.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.NET_SET = "v2"       # 与 v2 批次一致（400/100/25 Gbps 量级）
    return mod


def bandwidth_ranks(nodes) -> np.ndarray:
    """带宽排名数组：1 = 最快。并列取平均秩。"""
    bw = np.array([n.bandwidth for n in nodes], dtype=float)
    order = np.argsort(-bw, kind="stable")
    ranks = np.empty(len(nodes), dtype=float)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and bw[order[j + 1]] == bw[order[i]]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return ranks


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--nodes", type=int, nargs="*", default=[3, 4, 8, 16],
                    help="节点数档位")
    ap.add_argument("--requests", type=int, default=12000)
    ap.add_argument("--seeds", type=int, nargs="*", default=[1],
                    help="种子=独立的负载实现；默认 1 个（探针测量的是**配置的几何性质**，"
                         "不是策略的统计效应，所以不需要 10 个种子；多给几个可以做重复性检查）")
    ap.add_argument("--workload", default="mixed")
    ap.add_argument("--network", default="degraded")
    ap.add_argument("--align", default="anti", choices=["anti", "rot1", "aligned"])
    ap.add_argument("--aligns", nargs="*", default=None,
                    help="一次跑多档对齐（默认三档全跑）：用来把「可构造的 ρ 区间」也落盘，"
                         "它是解释规模衰减的关键量")
    ap.add_argument("--strategies", nargs="*",
                    default=["LeastLoad", "NetworkAware2"],
                    help="要测量的策略（LeastLoad 是完全忽略网络的对照）")
    ap.add_argument("--out", default=str(ROOT / "results/analysis_v2"))
    ap.add_argument("--target-rho", type=float, default=None)
    args = ap.parse_args()

    rm = _load_matrix_module()
    rho_target = rm.TARGET_RHO if args.target_rho is None else args.target_rho
    aligns = args.aligns if args.aligns else (["anti", "rot1", "aligned"]
                                              if args.aligns is None and args.align == "anti"
                                              else [args.align])
    rows = []
    print("=" * 96)
    print(f"探针：各对齐档下，策略挑中的机器在「网线粗细」里排第几？"
          f"（{args.workload}/{args.network}，{args.requests} 请求/次）")
    print("=" * 96)
    print(f"{'对齐':>8}{'节点':>4}{'链路':>4}{'ρ':>8}{'种子':>5}{'策略':>16}"
          f"{'平均带宽排名':>14}{'落最快1/3':>12}{'p95(s)':>10}")
    for align in aligns:
        for n_nodes in args.nodes:
            for seed in args.seeds:
                cell = {"workload": args.workload, "network": args.network,
                        "staleness": "fresh", "staleness_queue_s": 0.0,
                        "staleness_network_s": 0.0, "seed": seed,
                        "data_volume": rm.DEFAULT_DATA_VOLUME, "n_nodes": n_nodes,
                        "data_source": "synthetic", "trace_segment": None,
                        "target_rho": rho_target, "net_set": rm.NET_SET,
                        "align": align}
                nodes = rm.cell_nodes(cell)
                cond = rm.cell_network_condition(cell)
                cond["_n_nodes"] = n_nodes
                cond = rm.apply_alignment(cond, align)
                bpc = rm.DATA_VOLUME[cell["data_volume"]]
                cfg = SimConfig(max_requests=args.requests, bytes_per_context_token=bpc,
                                slo_mode="relative", slo_multiplier=3.0)
                workload, qps, _ = rm.build_synthetic_cell_workload(
                    rm.WORKLOAD_CLASSES[cell["workload"]], args.requests, seed,
                    sum(n.compute_rate * n.slots for n in nodes), target_rho=rho_target,
                    net_capacity_bps=sum(n.bandwidth for n in nodes),
                    bytes_per_ctx_token=bpc,
                    node_compute=[n.compute_rate * n.slots for n in nodes],
                    node_bandwidth=[n.bandwidth for n in nodes])
                traj = rm.make_trajectory(cond, n_nodes, seed=seed)
                ranks = bandwidth_ranks(nodes)
                n_fast = max(1, int(round(n_nodes / 3.0)))
                fast = set(np.argsort([-n.bandwidth for n in nodes])[:n_fast].tolist())
                rho = rm.rank_corr(nodes)
                for s in args.strategies:
                    res = run(workload, nodes, rm.make_strategy(s, seed, 1.0), cfg,
                              seed, traj)
                    m = res.measured(cfg.warmup_fraction)
                    ids = np.array([r.node_id for r in m])
                    row = {
                        "n_nodes": n_nodes, "n_links": len(cond["links"]),
                        "rank_corr": round(float(rho), 4), "seed": seed,
                        "strategy": s, "align": align,
                        "workload": args.workload, "network": args.network,
                        "n_requests_measured": int(len(ids)),
                        "target_rho": rho_target, "qps_realized": round(float(qps), 4),
                        "mean_bandwidth_rank": round(float(ranks[ids].mean()), 4),
                        "mean_bandwidth_rank_norm": round(
                            float(ranks[ids].mean() / n_nodes), 4),
                        "frac_top_third_bandwidth": round(
                            float(np.mean([i in fast for i in ids])), 4),
                        "p95_s": round(float(np.percentile([r.sojourn for r in m], 95)), 4),
                    }
                    rows.append(row)
                    print(f"{align:>8}{n_nodes:>4}{row['n_links']:>4}{rho:>+8.2f}"
                          f"{seed:>5}{s:>16}"
                          f"{row['mean_bandwidth_rank']:>14.2f}"
                          f"{row['frac_top_third_bandwidth']*100:>11.0f}%"
                          f"{row['p95_s']:>10.2f}")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "link_choice_by_scale.csv"
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8-sig")
    print(f"\n[表] {path}  ({len(rows)} 行)")
    print("读法：`mean_bandwidth_rank` 1 = 最快的网线。随机挑的话它应约等于 (节点数+1)/2。"
          "\n      节点数越多这个值仍停在随机水平附近 ⇒ 不看网络的策略并没有「碰巧挑到好网线」，"
          "\n      即 §18 的规模衰减不是「回旋余地变大」造成的。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""临时对照脚本：三种降权规则（split / hard / linear）对照 NA2 与 LeastLoad。跑完即删。"""
import importlib.util
import sys

import numpy as np

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from src.simulator import SimConfig, run                                 # noqa: E402
from src.strategies import NetworkAwareAdaptiveStrategy                  # noqa: E402

spec = importlib.util.spec_from_file_location("rmx", "experiments/run_matrix.py")
rm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rm)
rm.NET_SET = "v2"

rec: list[tuple[float, float]] = []
_orig = NetworkAwareAdaptiveStrategy.weights


def patched(self, req, obs):
    wd, ws = _orig(self, req, obs)
    if self.w_net > 0:
        rec.append((wd, ws))
    return wd, ws


NetworkAwareAdaptiveStrategy.weights = patched

STRATS = [
    "LeastLoad",
    "NetworkAware2",
    {"name": "NetworkAware2Adapt", "label": "AdaptSplit"},
    {"name": "NetworkAware2Adapt", "label": "AdaptLinear", "decay": "linear"},
    {"name": "NetworkAware2AdaptHard", "label": "AdaptHard"},
]
NREQ = 4000


def mkcell(lq, ln, seed=1, wl="mixed", net="unstable"):
    return {"workload": wl, "network": net, "staleness": "fresh",
            "staleness_queue_s": lq, "staleness_network_s": ln,
            "seed": seed, "data_volume": "kv_cache", "n_nodes": 3,
            "data_source": "synthetic", "trace_segment": None,
            "target_rho": rm.TARGET_RHO, "net_set": "v2"}


def setup(cell):
    nodes = rm.cell_nodes(cell)
    cond = rm.cell_network_condition(cell)
    cond["_n_nodes"] = cell["n_nodes"]
    cond = rm.apply_alignment(cond, cell.get("align"))
    bpc = rm.DATA_VOLUME[cell["data_volume"]]
    cfg = SimConfig(prefill_coef=1.0, decode_coef=4.0, warmup_fraction=0.1,
                    max_requests=NREQ, bytes_per_context_token=bpc,
                    bytes_per_generated_token=4.0,
                    staleness_queue=cell["staleness_queue_s"],
                    staleness_network=cell["staleness_network_s"],
                    slo_mode="relative", slo_multiplier=3.0)
    wl, qps, _ = rm.build_synthetic_cell_workload(
        rm.WORKLOAD_CLASSES[cell["workload"]], NREQ, cell["seed"],
        sum(n.compute_rate * n.slots for n in nodes), target_rho=rm.TARGET_RHO,
        net_capacity_bps=sum(n.bandwidth for n in nodes), bytes_per_ctx_token=bpc,
        node_compute=[n.compute_rate * n.slots for n in nodes],
        node_bandwidth=[n.bandwidth for n in nodes])
    traj = rm.make_trajectory(cond, cell["n_nodes"], seed=cell["seed"])
    return nodes, cfg, wl, traj


def run_tbl(wl_name, net, grid):
    print("=" * 108)
    print(f"快速对照 · 负载={wl_name} · 网络={net} · {NREQ} 请求/1 种子 · "
          f"数值=p95 端到端耗时（秒，越低越好）")
    print("=" * 108)
    labels = [rm.label_of(s) for s in STRATS]
    print(f"{'队列旧':>6}{'链路旧':>7} | " + "".join(f"{x:>13}" for x in labels))
    out = {}
    for lq, ln in grid:
        cell = mkcell(lq, ln, wl=wl_name, net=net)
        nodes, cfg, wl, traj = setup(cell)
        row = {}
        for s in STRATS:
            rec.clear()
            res = run(wl, nodes, rm.make_strategy(s, 1, 1.0), cfg, 1, traj)
            m = res.measured(0.1)
            row[rm.label_of(s)] = float(np.percentile([r.sojourn for r in m], 95))
            if rm.label_of(s) in ("AdaptSplit", "AdaptSoft"):
                arr = np.array(rec)
                row["_wd"] = arr[:, 0].mean()
                row["_ws"] = arr[:, 1].mean()
        out[(lq, ln)] = row
        print(f"{lq:>6.0f}{ln:>7.0f} | "
              + "".join(f"{row[x]:>13.1f}" for x in labels))
        print(f"{'':>14}|   └ AdaptSplit 实际权重：动态 {row['_wd']:.3f} / 结构 {row['_ws']:.3f}")
    print("相对 LeastLoad（%）：")
    print(f"{'队列旧':>6}{'链路旧':>7} | "
          + "".join(f"{x:>13}" for x in labels[1:]))
    for (lq, ln), row in out.items():
        base = row["LeastLoad"]
        print(f"{lq:>6.0f}{ln:>7.0f} | "
              + "".join(f"{(row[x] - base) / base * 100:>12.1f}%" for x in labels[1:]))
    print()


run_tbl("mixed", "unstable",
        [(0.0, 0.0), (1.0, 0.0), (5.0, 0.0), (0.0, 15.0), (0.0, 60.0), (5.0, 60.0)])

# 门 2：w_net=0 时所有变体都必须与 LeastLoad 逐位相同
print("门 2（w_net=0 应与 LeastLoad 逐位相同）：")
for wl_name, net in [("mixed", "unstable"), ("data_heavy", "degraded")]:
    cell = mkcell(0.0, 0.0, wl=wl_name, net=net)
    nodes, cfg, wl, traj = setup(cell)

    def fins(strat):
        return np.array([r.finish for r in run(wl, nodes, strat, cfg, 1, traj).requests])

    f_ll = fins(rm.make_strategy("LeastLoad", 1, 1.0))
    for lab, sp in [("split", {"name": "NetworkAware2Adapt"}),
                    ("linear", {"name": "NetworkAware2Adapt", "decay": "linear"}),
                    ("hard", {"name": "NetworkAware2AdaptHard"})]:
        sp = dict(sp, w_net=0.0)
        ok = np.array_equal(f_ll, fins(rm.make_strategy(sp, 1, 1.0)))
        print(f"  {wl_name}/{net} · {lab:7s} → {ok}")

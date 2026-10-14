"""一次性核对：align/alignscale 里 3 个 'load_ratio>1' 的格子（不改任何产物）。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

import pandas as pd  # noqa: E402
import experiments.run_matrix as rmx  # noqa: E402

rmx.NET_SET = "v2"
SLOTS = 4

def R_of(links, n_nodes, mean_work, mean_bytes):
    rates = [16000.0 / (2 ** (i % 4)) for i in range(n_nodes)]
    return sum(min(rates[i] * SLOTS / mean_work, links[i % len(links)][1] / mean_bytes)
               for i in range(n_nodes))

cases = [
    ("align", "data_heavy", "degraded", 3, "align_20260929T160519Z"),
    ("align", "mixed", "degraded", 3, "align_20260929T160519Z"),
    ("alignscale", "mixed", "degraded", 4, None),
]

lr = pd.read_csv(sorted((ROOT / "results" / "analysis_v2").glob("load_intensity_rho.csv"))[-1])

for mode, wl, net, n, pref in cases:
    for rf in sorted((ROOT / "results" / "matrix_v2").glob(f"{mode}_*_runs.csv"), reverse=True):
        df = pd.read_csv(rf)
        sub = df[(df["workload"] == wl) & (df["network"] == net) & (df["n_nodes"] == n)]
        if not sub.empty:
            break
    mean_work = float(sub["mean_work"].mean())
    mean_bytes = float(sub["mean_bytes"].mean())
    base = rmx.active_network_conditions()[net]
    Rs = {}
    for lv in ("anti", "rot1", "aligned"):
        cond = dict(base)
        cond["_n_nodes"] = n
        links = rmx.alignment_links(n, lv, cond)
        Rs[lv] = R_of(links, n, mean_work, mean_bytes)
    pred = 0.6 * Rs["aligned"] / Rs["anti"]
    rec = float(lr[(lr["mode"] == mode) & (lr["workload"] == wl)
                   & (lr["network"] == net) & (lr["n_nodes"] == n)]["load_ratio"].iloc[0])
    act = sub.groupby("align")["qps_realized"].mean().to_dict()
    print(f"[{mode}/{wl}/{net}/n={n}]")
    print(f"  实际每档 qps: " + ", ".join(f"{k}={v:.4f}" for k, v in sorted(act.items())))
    print(f"  预测 λ=0.6R:  " + ", ".join(f"{k}={0.6*Rs[k]:.4f}" for k in ("anti", "rot1", "aligned")))
    print(f"  CSV load_ratio={rec:.5f}   预测 0.6·R_aligned/R_anti={pred:.5f}")
    print()

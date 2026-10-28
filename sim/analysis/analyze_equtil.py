# -*- coding: utf-8 -*-
"""Analyze the equal-utilization control (results/matrix_equtil_*)."""
from __future__ import annotations

import glob
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

SOURCES = {
    "synthetic": ("results/matrix_v2", "results/matrix_consL_synthetic", "results/matrix_equtil_synthetic"),
    "azure": ("results/matrix_trace_v2", "results/matrix_consL_azure", "results/matrix_equtil_azure"),
    "burstgpt": ("results/matrix_burstgpt_v2", "results/matrix_consL_burstgpt", "results/matrix_equtil_burstgpt"),
}


def latest(pattern: str, min_rows: int = 0) -> pd.DataFrame:
    files = sorted(glob.glob(str(ROOT / pattern)), key=lambda p: Path(p).stat().st_mtime)
    for f in reversed(files):
        try:
            d = pd.read_csv(f)
        except Exception:  # noqa: BLE001
            continue
        if len(d) >= min_rows:
            return d
    raise RuntimeError(f"no usable file for {pattern}")


def per_seed(df: pd.DataFrame) -> pd.DataFrame:
    out = []
    for (wl, net, seed), g in df.groupby(["workload", "network", "seed"]):
        p = g.set_index("strategy").sojourn_p95
        if "NetworkAware2" not in p or "LeastLoad" not in p:
            continue
        ll = float(p["LeastLoad"])
        na = float(p["NetworkAware2"])
        u = g[g.strategy == "LeastLoad"].node_utilization.mean()
        rho_eq = float(g["target_rho"].mean()) if "target_rho" in g.columns else None
        out.append({"workload": wl, "network": net, "seed": int(seed),
                    "p95_ll": ll, "p95_na2": na,
                    "dp95": (na - ll) / ll * 100.0, "util": float(u),
                    "rho_eq": rho_eq})
    return pd.DataFrame(out)


def main() -> int:
    outdir = ROOT / "results/analysis_equtil"
    outdir.mkdir(parents=True, exist_ok=True)
    all_cells, summary = [], []
    lines = []
    for src, (ideal_dir, nominal_dir, eq_dir) in SOURCES.items():
        ideal = latest(ideal_dir + "/main_*_runs.csv", 480)
        ideal = ideal[ideal.staleness == "fresh"]
        nominal = latest(nominal_dir + "/main_*_runs.csv", 480)
        nominal = nominal[nominal.staleness == "fresh"]
        eq = latest(eq_dir + "/equtil_*_runs.csv", 30)
        di, dn, de = per_seed(ideal), per_seed(nominal), per_seed(eq)
        common = set(zip(di.workload, di.network))
        for wl, net in sorted({(w, n) for w, n in set(zip(de.workload, de.network)) if (w, n) in common}):
            a = di[(di.workload == wl) & (di.network == net)]
            b = dn[(dn.workload == wl) & (dn.network == net)]
            c = de[(de.workload == wl) & (de.network == net)]
            if not len(a) or not len(c):
                continue
            summary.append({
                "source": src, "workload": wl, "network": net,
                "n_seeds": int(min(len(a), len(b), len(c))),
                "dp95_ideal": round(a.dp95.median(), 2),
                "dp95_nominal": round(b.dp95.median(), 2),
                "dp95_equtil": round(c.dp95.median(), 2),
                "util_ideal": round(a.util.mean(), 3),
                "util_nominal": round(b.util.mean(), 3),
                "util_equtil": round(c.util.mean(), 3),
                "rho_equtil": round(c.rho_eq.median(), 3) if "rho_eq" in c else None,
                "delta_eq_minus_ideal": round(c.dp95.median() - a.dp95.median(), 2),
                "delta_nominal_minus_ideal": round(b.dp95.median() - a.dp95.median(), 2),
            })
            for tag, d in (("ideal", a), ("nominal", b), ("equtil", c)):
                x = d.copy()
                x["source"] = src
                x["arm"] = tag
                all_cells.append(x)
    s = pd.DataFrame(summary)
    cells = pd.concat(all_cells, ignore_index=True)
    s.to_csv(outdir / "equtil_summary.csv", index=False)
    cells.to_csv(outdir / "equtil_cells.csv", index=False)

    lines.append("Equal-utilization control (calibrated f(c), linear).")
    lines.append("dp95 = (NetworkAware2 - LeastLoad) / LeastLoad * 100, median over seeds.")
    lines.append("util is LeastLoad node utilization; equtil rho is per-cell matched to ideal util.")
    lines.append("")
    for src in s.source.unique():
        lines.append("===== " + src)
        lines.append(s[s.source == src].to_string(index=False))
        lines.append("")
    text = chr(10).join(lines)
    (outdir / "report.txt").write_text(text, encoding="utf-8")
    print(text)
    print("written", outdir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

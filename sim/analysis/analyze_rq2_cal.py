# -*- coding: utf-8 -*-
"""Compare RQ2 staleness under ideal, nominal-calibrated and equal-U calibrated models."""
from __future__ import annotations

import glob
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

IDEAL = {"synthetic": "results/matrix_v2", "azure": "results/matrix_trace_v2",
         "burstgpt": "results/matrix_burstgpt_v2"}
NOMINAL = {"synthetic": "results/matrix_consL_synthetic", "azure": "results/matrix_consL_azure",
           "burstgpt": "results/matrix_consL_burstgpt"}
EQ = {"synthetic": "results/matrix_eqU_stale_synthetic", "azure": "results/matrix_eqU_stale_azure",
      "burstgpt": "results/matrix_eqU_stale_burstgpt"}


def latest(pattern: str) -> pd.DataFrame | None:
    fs = sorted(glob.glob(str(ROOT / pattern)))
    return pd.read_csv(fs[-1]) if fs else None


def sweep(df: pd.DataFrame, prefix: str) -> pd.DataFrame:
    d = df[df.staleness.astype(str).str.startswith(prefix)].copy()
    if d.empty:
        return d
    keys = ["network", "staleness_queue_s", "staleness_network_s", "strategy"]
    agg = d.groupby(keys, dropna=False).sojourn_p95.mean().reset_index()
    rows = []
    for (net, lq, ln), g in agg.groupby(["network", "staleness_queue_s", "staleness_network_s"]):
        p = g.set_index("strategy").sojourn_p95
        if "LeastLoad" not in p or "NetworkAware2" not in p:
            continue
        ll, na = float(p["LeastLoad"]), float(p["NetworkAware2"])
        rows.append({"network": net, "queue_s": float(lq), "link_s": float(ln),
                     "p95_ll": ll, "p95_na2": na,
                     "benefit_pct": (ll - na) / ll * 100.0})
    out = pd.DataFrame(rows).sort_values(["network", "queue_s", "link_s"])
    if out.empty:
        return out
    out["retention_pct"] = np.nan
    for net, g in out.groupby("network"):
        base = g[(g.queue_s == 0) & (g.link_s == 0)]
        if base.empty or float(base.benefit_pct.iloc[0]) == 0:
            continue
        b0 = float(base.benefit_pct.iloc[0])
        idx = g.index
        out.loc[idx, "retention_pct"] = g.benefit_pct / b0 * 100.0
    return out


def main() -> int:
    outdir = ROOT / "results/analysis_rq2_cal"
    outdir.mkdir(parents=True, exist_ok=True)
    frames = []
    for src in IDEAL:
        for arm, dirs in (("ideal", IDEAL), ("nominal", NOMINAL), ("equalU", EQ)):
            df = latest(dirs[src] + "/staleness_*_runs.csv")
            if df is None or df.cell_id.nunique() < 90:
                continue
            for prefix, channel in (("net", "link"), ("queue", "queue")):
                s = sweep(df, prefix)
                if s.empty:
                    continue
                s["source"] = src
                s["arm"] = arm
                s["channel"] = channel
                frames.append(s)
    if not frames:
        print("no data")
        return 1
    allx = pd.concat(frames, ignore_index=True)
    allx.to_csv(outdir / "rq2_calibrated_summary.csv", index=False)

    lines = ["RQ2 staleness under ideal / nominal-calibrated / equal-U calibrated (linear).",
             "benefit = (p95_LeastLoad - p95_NetworkAware2) / p95_LeastLoad * 100.",
             "retention = benefit(lag) / benefit(0) * 100.", ""]
    for src in IDEAL:
        lines.append("===== " + src)
        sub = allx[allx.source == src]
        for arm in ("ideal", "nominal", "equalU"):
            x = sub[sub.arm == arm]
            if x.empty:
                continue
            lines.append("-- " + arm)
            # link sweep: unstable network is the informative one
            for net in ("degraded", "unstable"):
                y = x[(x.channel == "link") & (x.network == net)]
                if y.empty:
                    continue
                line = "  link " + net + ": " + ", ".join(
                    f"{int(r.link_s)}s={r.benefit_pct:.1f}% (ret {r.retention_pct:.0f}%)"
                    for r in y.itertuples())
                lines.append(line)
            y = x[(x.channel == "queue") & (x.network == "unstable")]
            if not y.empty:
                lines.append("  queue unstable: " + ", ".join(
                    f"{r.queue_s:g}s={r.benefit_pct:.1f}% (ret {r.retention_pct:.0f}%)"
                    for r in y.itertuples()))
        lines.append("")
    text = chr(10).join(lines)
    (outdir / "report.txt").write_text(text, encoding="utf-8")
    print(text)
    print("written", outdir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# -*- coding: utf-8 -*-
"""Targeted calibrated re-runs: equal-U RQ2 and discounting (10k requests).

RQ2: unstable network, link ages 0/1/5/15/60 s, queue ages 0/1/5 s, 3 seeds.
Discounting: freshness (0,0), (0,60), (5,0), (5,60), 2 workloads x 2 networks x
5 seeds; five-arm method plus three-arm method_ch.

Usage:
  .venv/Scripts/python.exe experiments/run_target_cal.py synthetic 0.40
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "experiments"))
sys.path.insert(0, str(ROOT))
import run_matrix as R  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

TRACE = {"azure": ROOT / "data/raw/azure_code_1week.csv",
         "burstgpt": ROOT / "data/raw/burstgpt_1.csv"}
SEEDS = [1, 2, 3, 4, 5]
NREQ = 10000


def setup(src: str, rho: float) -> None:
    R.SOURCE = "synthetic" if src == "synthetic" else "trace"
    R.NET_SET = "v2"
    R.SEEDS = SEEDS
    R.TARGET_RHO = rho
    if src != "synthetic":
        wl, stats = R.load_trace_auto(TRACE[src])
        R._TRACE_STATS = stats
        R._TRACE_SEGMENTS = R.split_trace_segments(wl, max(SEEDS))
        print(f"trace {stats.source}, {stats.n_requests:,} requests", flush=True)


def rq2_cells(rho: float) -> list[dict]:
    out = []
    for c in R.build_matrix("staleness", {}):
        c["cell_id"] = f"rq2_{len(out):03d}"
        c["target_rho"] = rho
        c["service_slowdown"] = "linear"
        lab = str(c["staleness"])
        if c["network"] != "unstable":
            continue
        if lab.startswith("net") and c["staleness_network_s"] in (0, 1, 5, 15, 60):
            out.append(c)
        elif lab.startswith("queue") and c["staleness_queue_s"] in (0, 1, 5):
            out.append(c)
    return out


def disc_cells(mode: str, rho: float) -> list[dict]:
    keep = {(0.0, 0.0), (0.0, 60.0), (5.0, 0.0), (5.0, 60.0)}
    out = []
    for c in R.build_matrix(mode, {}):
        if (float(c["staleness_queue_s"]), float(c["staleness_network_s"])) not in keep:
            continue
        c["cell_id"] = f"{mode}_{len(out):03d}"
        c["target_rho"] = rho
        c["service_slowdown"] = "linear"
        out.append(c)
    return out


def run_set(src: str, tag: str, cells: list[dict], out: Path) -> None:
    runs_path = out / f"{tag}_runs.csv"
    paired_path = out / f"{tag}_paired.csv"
    t0 = time.perf_counter()
    for i, c in enumerate(cells, 1):
        strategies = c.get("strategies") or R.base_strategies()
        rows, tests = R.run_cell(c, strategies, NREQ, c.get("w_net_override", 1.0))
        pd.DataFrame(rows).to_csv(runs_path, mode="a", header=not runs_path.exists(), index=False)
        pd.DataFrame(tests).to_csv(paired_path, mode="a", header=not paired_path.exists(), index=False)
        if i % 5 == 0 or i == len(cells):
            el = time.perf_counter() - t0
            print(f"  {src}/{tag} {i}/{len(cells)}  {el/60:.1f} min", flush=True)
    df = pd.read_csv(runs_path)
    df_t = pd.read_csv(paired_path) if paired_path.exists() else pd.DataFrame()
    if len(df_t):
        adj = pd.Series(np.nan, index=df_t.index, dtype=float)
        for _, g in df_t.groupby(["cell_id", "candidate"]):
            adj.loc[g.index] = R.holm_bonferroni(list(g["p_value"].fillna(1.0)))
        df_t["p_holm"] = adj
        df_t.to_csv(paired_path, index=False)
    df.to_csv(runs_path, index=False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", choices=["synthetic", "azure", "burstgpt"])
    ap.add_argument("rho", type=float)
    args = ap.parse_args()
    setup(args.source, args.rho)
    out = ROOT / f"results/matrix_target_{args.source}"
    out.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for tag, cells in (("rq2", rq2_cells(args.rho)),
                       ("method", disc_cells("method", args.rho)),
                       ("method_ch", disc_cells("method_ch", args.rho))):
        print(f"== {args.source}/{tag}: {len(cells)} cells", flush=True)
        run_set(args.source, f"{tag}_{ts}", cells, out)
    (out / f"target_{ts}_manifest.json").write_text(json.dumps({
        "source": args.source, "target_rho": args.rho, "requests": NREQ,
        "seeds": SEEDS, "service_slowdown": "linear", "net_set": "v2",
        "notes": "targeted equal-U RQ2 (unstable) and discounting (4 freshness levels)",
    }, indent=2), encoding="utf-8")
    print("TARGET_DONE", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

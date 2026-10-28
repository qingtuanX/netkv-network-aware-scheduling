# -*- coding: utf-8 -*-
"""Equal-utilization control for the calibrated service-time model.

For every fresh (workload, network, seed) cell, we first read the realized
LeastLoad utilization of the *ideal* model at the nominal rho=0.6.  We then
search a calibrated-model target rho such that the calibrated LeastLoad
utilization matches that ideal value.  Finally we run LeastLoad and
NetworkAware2 at the matched rho, so the ideal-vs-calibrated comparison is at
equal effective utilization rather than equal nominal rho.

This is the control requested by the reviewer: the nominal-rho comparison in
S6 changes both service time and effective load; this experiment separates them.

Outputs (append-only, per source):
  results/matrix_equtil_<source>/equtil_<UTC>_runs.csv
  results/matrix_equtil_<source>/equtil_<UTC>_paired.csv
  results/matrix_equtil_<source>/equtil_<UTC>_manifest.json

Usage:
  .venv/Scripts/python.exe experiments/run_equalutil.py [synthetic|azure|burstgpt|all]
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
IDEAL_DIR = {"synthetic": ROOT / "results/matrix_v2",
             "azure": ROOT / "results/matrix_trace_v2",
             "burstgpt": ROOT / "results/matrix_burstgpt_v2"}
SEEDS = [1, 2, 3, 4, 5]
N_REQ = 20000
TARGET_RHO = 0.6
UTIL_TOL = 0.004


def latest_complete_main(d: Path) -> pd.DataFrame:
    """Latest runs.csv that has at least 480 rows (a complete 120-cell main run)."""
    cands = sorted(d.glob("main_*_runs.csv"), key=lambda p: p.stat().st_mtime)
    for f in reversed(cands):
        try:
            df = pd.read_csv(f)
        except Exception:  # noqa: BLE001
            continue
        if len(df) >= 480:
            return df
    raise RuntimeError(f"no complete main runs.csv under {d}")


def ideal_util_map(src: str) -> dict:
    df = latest_complete_main(IDEAL_DIR[src])
    df = df[(df["staleness"] == "fresh") & (df["strategy"] == "LeastLoad")]
    out = {}
    for (wl, net, seed), g in df.groupby(["workload", "network", "seed"]):
        out[(str(wl), str(net), int(seed))] = float(g["node_utilization"].mean())
    return out


def setup_source(src: str) -> None:
    R.SOURCE = "synthetic" if src == "synthetic" else "trace"
    R.NET_SET = "v2"
    R.SEEDS = SEEDS
    R.TARGET_RHO = TARGET_RHO
    if src != "synthetic":
        trace = TRACE[src]
        print(f"读取 trace（首次解析 1-3 分钟）：{trace}", flush=True)
        wl, stats = R.load_trace_auto(trace)
        R._TRACE_STATS = stats
        R._TRACE_SEGMENTS = R.split_trace_segments(wl, max(SEEDS))
        print(f"  {stats.source}: {stats.n_requests:,} 请求，{stats.duration_s/3600:.1f} h", flush=True)


def find_rho(cell: dict, target_u: float) -> tuple[float, float, int, list]:
    """Secant search for calibrated rho whose LeastLoad utilization matches target_u."""
    hist: list[tuple[float, float]] = []
    rho = 0.40
    for it in range(1, 7):
        cell["target_rho"] = rho
        rows, _ = R.run_cell(cell, ["LeastLoad"], N_REQ, 1.0)
        u = float(rows[0]["node_utilization"])
        hist.append((rho, u))
        if abs(u - target_u) <= UTIL_TOL:
            return rho, u, it, hist
        if len(hist) >= 2:
            (r0, u0), (r1, u1) = hist[-2], hist[-1]
            if abs(u1 - u0) > 1e-5:
                nxt = r1 + (target_u - u1) * (r1 - r0) / (u1 - u0)
            else:
                nxt = r1 * target_u / max(u1, 1e-9)
        else:
            nxt = rho * target_u / max(u, 1e-9)
        # damp and keep inside the sustainable region
        nxt = 0.7 * nxt + 0.3 * rho
        rho = float(min(0.60, max(0.10, nxt)))
    return rho, hist[-1][1], len(hist), hist


def run_source(src: str) -> int:
    setup_source(src)
    target = ideal_util_map(src)
    cells = [c for c in R.build_matrix("main", {})
             if c["staleness"] == "fresh"
             and c["workload"] in ("mixed", "data_heavy")
             and c["network"] in ("degraded", "unstable")
             and int(c["seed"]) in SEEDS]
    cells.sort(key=lambda c: (c["workload"], c["network"], c["seed"]))
    out = ROOT / f"results/matrix_equtil_{src}"
    out.mkdir(parents=True, exist_ok=True)
    run_id = f"equtil_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    runs_path = out / f"{run_id}_runs.csv"
    paired_path = out / f"{run_id}_paired.csv"
    t0 = time.perf_counter()
    all_rows, all_tests = [], []

    def append(path: Path, rows: list[dict]) -> None:
        if not rows:
            return
        pd.DataFrame(rows).to_csv(path, mode="a", header=not path.exists(), index=False)

    for i, cell in enumerate(cells, 1):
        cell["cell_id"] = f"eq_{i:03d}"
        cell["service_slowdown"] = "linear"
        key = (str(cell["workload"]), str(cell["network"]), int(cell["seed"]))
        if key not in target:
            raise KeyError(f"no ideal utilization for {key}")
        tu = target[key]
        rho, u, iters, hist = find_rho(cell, tu)
        cell["target_rho"] = rho
        cell["util_target"] = tu
        cell["util_calib"] = u
        cell["calib_iters"] = iters
        cell["calib_hist"] = json.dumps([[round(a, 4), round(b, 4)] for a, b in hist])
        rows, tests = R.run_cell(cell, ["LeastLoad", "NetworkAware2"], N_REQ, 1.0)
        append(runs_path, rows)
        append(paired_path, tests)
        all_rows.extend(rows)
        all_tests.extend(tests)
        el = time.perf_counter() - t0
        print(f"  {i}/{len(cells)} {key} targetU={tu:.3f} rho={rho:.3f} "
              f"U={u:.3f} it={iters}  ({el/60:.1f} min)", flush=True)

    df = pd.read_csv(runs_path)
    df_t = pd.read_csv(paired_path) if paired_path.exists() else pd.DataFrame()
    if len(df_t):
        adj = pd.Series(np.nan, index=df_t.index, dtype=float)
        for _, g in df_t.groupby(["cell_id", "candidate"]):
            adj.loc[g.index] = R.holm_bonferroni(list(g["p_value"].fillna(1.0)))
        df_t["p_holm"] = adj
    df.to_csv(runs_path, index=False)
    if len(df_t):
        df_t.to_csv(paired_path, index=False)

    manifest = {
        "run_id": run_id,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "equtil",
        "data_source": R.SOURCE,
        "source_tag": src,
        "net_set": R.NET_SET,
        "link_queue_model": True,
        "service_slowdown": "linear",
        "n_cells": len(cells),
        "n_runs": int(len(df)),
        "n_requests_per_run": N_REQ,
        "seeds": SEEDS,
        "nominal_rho": TARGET_RHO,
        "utilization_tolerance": UTIL_TOL,
        "ideal_matrix_dir": str(IDEAL_DIR[src].relative_to(ROOT)),
        "workloads": ["mixed", "data_heavy"],
        "networks": ["degraded", "unstable"],
        "note": "rho is per-cell matched to ideal LeastLoad utilization at nominal rho=0.6; "
                "final arms are LeastLoad and NetworkAware2.",
    }
    (out / f"{run_id}_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"完成 {src}: {len(cells)} cells, {len(df)} runs -> {runs_path}", flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("which", nargs="?", default="all",
                    choices=["all", "synthetic", "azure", "burstgpt"])
    args = ap.parse_args()
    todo = ["synthetic", "azure", "burstgpt"] if args.which == "all" else [args.which]
    print("[preflight] regtest_slowdown selftest", flush=True)
    import subprocess
    chk = subprocess.run([sys.executable, str(ROOT / "analysis/regtest_slowdown.py"), "selftest"],
                         cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if chk.returncode != 0:
        print(chk.stdout[-1000:], chk.stderr[-1000:])
        return chk.returncode
    for src in todo:
        rc = run_source(src)
        if rc:
            return rc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

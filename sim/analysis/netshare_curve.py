# -*- coding: utf-8 -*-
"""§5.2 份额曲线（2026-10-30 重建）：把"网络份额 → Δp95"从两个点变成一条曲线。

为什么重做（三条，都来自对旧稿的核对）：
  1. 旧图只用了两个数据量档（4 B 与 330 KB），份额从 1e-5 直接跳到 0.72–0.90，
     中间 0.02–0.72 一个点都没有；
  2. 网络份额闸门的阈值 0.10 正落在那个空洞里，等于"界线画在没有数据的地方"；
  3. 旧稿正文引的 −54.2%/−10.5%/−5.4% 出自另一张表（datavolume，3 种子），
     和图（bandwidth，2 个负载、3 种子）不是一套产物；闸门触发率来自第三条臂。

产物（results/analysis_netshare/）:
  netshare_points.csv   每个 (负载, 数据量, 链路档) 的份额、Δp95、95% CI、种子胜场
  netshare_gate.csv     每个闸门档的触发率、Δp95
  netshare_summary.txt  供正文取数：穿越点、分箱中位数、闸门代价

用法：.venv/Scripts/python.exe analysis/netshare_curve.py [--fig]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parents[1]
MAT = ROOT / "results/matrix_netshare"
GATE = ROOT / "results/matrix_netshare_gate"
OUT = ROOT / "results/analysis_netshare"

VOLUME_KB = {
    "ns_4B": 0.004, "ns_4KB": 4.0, "ns_32KB": 32.0, "ns_100KB": 100.0,
    "ns_120KB": 120.0, "ns_140KB": 140.0, "ns_160KB": 160.0,
    "ns_180KB": 180.0, "ns_200KB": 200.0, "ns_330KB": 330.0,
}
GATE_FLOORS = ["gate0", "gate0.05", "gate0.1", "gate0.2"]
BIN_EDGES = [1e-6, 1e-4, 1e-3, 1e-2, 3e-2, 1e-1, 2e-1, 4e-1, 1.0]


def t_crit(n: int) -> float:
    return float(stats.t.ppf(0.975, max(n - 1, 1)))


def read_runs(d: Path):
    files = sorted(Path(d).glob("*_runs.csv"))
    if not files:
        raise SystemExit(f"没有产物：{d}（先跑 run_matrix --mode netshare）")
    df = pd.concat([pd.read_csv(f) for f in files], ignore_index=True)
    before = len(df)
    df = df.drop_duplicates(subset=["cell_id", "strategy", "seed"])
    if len(df) != before:
        print(f"[警告] 去重 {before - len(df)} 行（分片重叠？）")
    return df, [f.name for f in files]


def paired_delta(g, cand, base="LeastLoad"):
    c = g[g.strategy == cand].set_index("seed")["sojourn_p95"]
    b = g[g.strategy == base].set_index("seed")["sojourn_p95"]
    idx = c.index.intersection(b.index)
    if len(idx) < 2:
        return {}
    d = 100.0 * (c[idx] - b[idx]) / b[idx]
    n = len(d)
    tc = t_crit(n)
    return dict(n_seeds=n, dp95_pct=float(d.mean()), dp95_median=float(d.median()),
                dp95_std=float(d.std(ddof=1)),
                ci95=float(tc * d.std(ddof=1) / np.sqrt(n)),
                n_better=int((d < 0).sum()))


def build_points(df):
    rows = []
    for (wl, dv, prof), g in df.groupby(["workload", "data_volume", "net_profile"]):
        st = paired_delta(g, "NetworkAware2")
        if not st:
            continue
        share = float(g[g.strategy == "NetworkAware2"]["net_share"].mean())
        rows.append(dict(workload=wl, data_volume=dv,
                         bytes_per_token=VOLUME_KB.get(dv, np.nan) * 1000.0,
                         net_profile=prof, net_share=share, **st))
    return pd.DataFrame(rows).sort_values(["workload", "net_share"]).reset_index(drop=True)


def binned(pts, wl):
    """按对数份额分箱取中位数。

    为什么不是保序回归：本曲线真实形状是"先小幅转正、再大幅转负"，不单调；
    保序拟合会抹平中段抬升，把穿越点拉早。分箱中位数对非单调形状是诚实的。
    """
    s = pts[pts.workload == wl].copy()
    s["bin"] = pd.cut(s["net_share"], bins=BIN_EDGES, labels=False)
    g = s.groupby("bin").agg(
        center=("net_share", lambda x: float(np.sqrt(x.min() * x.max()))),
        median=("dp95_pct", "median"), n=("dp95_pct", "size"),
        lo=("net_share", "min"), hi=("net_share", "max"))
    return g.reset_index(drop=True)


def crossing(pts, wl, thr):
    """Δp95 的分箱中位曲线穿过 thr 处的份额（对数线性内插）。"""
    g = binned(pts, wl)
    xs, ys = g["center"].to_numpy(), g["median"].to_numpy()
    for i in range(1, len(xs)):
        if ys[i - 1] > thr >= ys[i]:
            t = (ys[i - 1] - thr) / (ys[i - 1] - ys[i]) if ys[i - 1] != ys[i] else 0.0
            return float(xs[i - 1] * (xs[i] / xs[i - 1]) ** t)
    return float("nan")


def build_gate(df):
    rows = []
    for (wl, dv, prof), g in df.groupby(["workload", "data_volume", "net_profile"]):
        share = float(g[g.strategy == "LeastLoad"]["net_share"].mean())
        for arm in GATE_FLOORS:
            st = paired_delta(g, arm)
            if not st:
                continue
            sub = g[g.strategy == arm]
            gated = float(sub["w_gated_frac"].mean()) if "w_gated_frac" in sub else np.nan
            rows.append(dict(workload=wl, data_volume=dv,
                             bytes_per_token=VOLUME_KB.get(dv, np.nan) * 1000.0,
                             net_profile=prof, floor=float(arm.replace("gate", "")),
                             net_share=share, gated_frac=gated, **st))
    return pd.DataFrame(rows).sort_values(["workload", "floor", "net_share"]).reset_index(drop=True)


def report(pts, gate):
    lines = ["§5.2 份额曲线（netshare / netshare_gate）", ""]
    lines.append(f"格点数 {len(pts)}；份额范围 {pts.net_share.min():.2e} – {pts.net_share.max():.2f}")
    lines.append("")
    lines.append("[1] 各负载的穿越点（分箱中位曲线；对数线性内插）")
    for wl in sorted(pts.workload.unique()):
        c0, c1, c5 = (crossing(pts, wl, 0.0), crossing(pts, wl, -1.0),
                      crossing(pts, wl, -5.0))
        g = binned(pts, wl)
        peak = g.loc[g["median"].idxmax()]
        lines.append(f"  {wl:14s} 变号 {c0 if np.isnan(c0) else round(c0, 4)}"
                     f" | 穿过 -1%：{c1 if np.isnan(c1) else round(c1, 4)}"
                     f" | 穿过 -5%：{c5 if np.isnan(c5) else round(c5, 4)}"
                     f" | 最大正效应 {peak['median']:+.1f}% @ {peak['center']:.1e}")
    lines.append("")
    lines.append("[1b] 分箱中位数（份额中心 / 中位 Δp95 / 格点数）")
    for wl in sorted(pts.workload.unique()):
        g = binned(pts, wl)
        lines.append(f"  {wl:14s} " + "  ".join(
            f"{r.center:.1e}:{r.median:+.1f}(n={r.n})" for r in g.itertuples()))
    lines.append("")
    lines.append("[2] 闸门代价（同一 cell、同一请求流，只改 floor）")
    for wl in (sorted(gate.workload.unique()) if len(gate) else []):
        lines.append(f"  -- {wl}")
        for floor, g in gate[gate.workload == wl].groupby("floor"):
            lines.append(f"     floor={floor:>4.2f}  share={g.net_share.mean():.3f}"
                         f"  gated={g.gated_frac.mean()*100:5.1f}%"
                         f"  Δp95={g.dp95_pct.mean():+6.1f}%"
                         f"  (中位 {g.dp95_median.mean():+.1f}%)")
    lines.append("")
    if len(gate):
        lines.append("[3] 闸门开/关的差（floor 0.10 − floor 0）")
        piv = gate.pivot_table(index=["workload", "data_volume", "net_profile"],
                               columns="floor", values="dp95_pct")
        if 0.0 in piv.columns and 0.1 in piv.columns:
            d = (piv[0.1] - piv[0.0]).dropna()
            lines.append(f"  均值 {d.mean():+.2f} 个百分点，最差 {d.max():+.2f}，"
                         f"最好 {d.min():+.2f}（正=关门更差）")
        full = gate[gate.gated_frac > 0.99]
        lines.append(f"  全关的 cell：{len(full)} 个，Δp95 均值 "
                     f"{full.dp95_pct.mean():.3f}%" if len(full) else "  全关的 cell：0")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fig", action="store_true", help="同时出图")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    df, files = read_runs(MAT)
    print(f"读入 {len(files)} 个分片文件，{len(df)} 行")
    pts = build_points(df)
    pts.to_csv(OUT / "netshare_points.csv", index=False)
    print(f"points -> {OUT / 'netshare_points.csv'}（{len(pts)} 个格点）")

    gate = pd.DataFrame()
    if GATE.exists() and list(GATE.glob("*_runs.csv")):
        gdf, _ = read_runs(GATE)
        gate = build_gate(gdf)
        gate.to_csv(OUT / "netshare_gate.csv", index=False)
        print(f"gate   -> {OUT / 'netshare_gate.csv'}（{len(gate)} 行）")

    txt = report(pts, gate)
    (OUT / "netshare_summary.txt").write_text(txt, encoding="utf-8")
    print(txt)
    if args.fig:
        from make_netshare_figure import make_figure
        make_figure(pts, gate, OUT)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())


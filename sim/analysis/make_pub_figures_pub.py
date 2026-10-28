
#!/usr/bin/env python
"""Publication-quality redraw of the paper's data figures (Figures 2-9).

Style follows the Academic Research Skills *visualization_agent* standard
(APA 7 figure rules, Tol colour-blind-safe categorical palette, 300 dpi,
sans-serif type, no chart junk, no in-figure title -- the caption carries it).

Data are the SAME artefacts the original figures were built from:
  sim/results/analysis_v2|analysis_trace_v2|analysis_burstgpt_v2/*.csv
  sim/results/paper_numbers.txt
  sim/results/k8s_test/*.csv   (per-request rows -> p95)
Nothing is re-derived by hand.

Usage
  python make_pub_figures.py            # all figures -> out/
  python make_pub_figures.py --only 2,5
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

# ---------------------------------------------------------------- paths
HERE = Path(__file__).resolve().parent
REPO = HERE.parent
SIM = REPO / "sim"
RES = SIM / "results"
OUT = HERE / "out"

BATCH_DIRS = {
    "Synthetic": RES / "analysis_v2",
    "Azure": RES / "analysis_trace_v2",
    "BurstGPT": RES / "analysis_burstgpt_v2",
}
PAPER_NUMBERS = RES / "paper_numbers.txt"
K8S_DIR = RES / "k8s_test"

LEAST_LOAD = "LeastLoad"
NA2 = "NetworkAware2"

# ---------------------------------------------------------------- style
# Palette derived from Figure 1 (the architecture diagram, draw.io family), deepened for data marks:
#   fig-1 hues  blue #6c8ebf / orange #ea7230 / green #56863a / grey #bbbcbf, ink #172e54
BLUE, ORANGE, GREEN = "#2169b1", "#d2601f", "#4a7a2b"
RED, PURPLE, CYAN = "#b0392b", "#7a5ea8", "#2a7f8f"
TEAL, MAGENTA = "#4a7a2b", "#a8447a"
GREY, DGREY, LGREY = "#6f7680", "#3c3c3c", "#E8E8E8"

matplotlib.rcParams.update({
    "font.family": "sans-serif",
    "font.sans-serif": ["Arial", "Helvetica", "DejaVu Sans"],
    "font.size": 8.5,
    "axes.labelsize": 8.5,
    "axes.titlesize": 8.5,
    "xtick.labelsize": 8,
    "ytick.labelsize": 8,
    "legend.fontsize": 8,
    "axes.linewidth": 0.7,
    "axes.edgecolor": "#333333",
    "axes.labelcolor": "#222222",
    "xtick.color": "#333333",
    "ytick.color": "#333333",
    "xtick.major.width": 0.7, "ytick.major.width": 0.7,
    "xtick.minor.width": 0.5, "ytick.minor.width": 0.5,
    "xtick.major.size": 3.0, "ytick.major.size": 3.0,
    "xtick.minor.size": 1.6, "ytick.minor.size": 1.6,
    "xtick.direction": "out", "ytick.direction": "out",
    "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": False,
    "lines.linewidth": 1.1,
    "lines.markersize": 4.5,
    "legend.frameon": False,
    "legend.handlelength": 1.6,
    "legend.handletextpad": 0.6,
    "legend.columnspacing": 1.2,
    "figure.dpi": 160,
    "savefig.dpi": 300,
    "savefig.bbox": "tight",
    "savefig.pad_inches": 0.02,
    "pdf.fonttype": 42,
    "ps.fonttype": 42,
    "svg.fonttype": "none",
    "axes.unicode_minus": True,
})

W = 6.3          # full text width, inches (matches \columnwidth in the manuscript)


# ---------------------------------------------------------------- helpers
def read_csv(path: Path) -> pd.DataFrame | None:
    if not path.exists():
        print(f"  [missing] {path}")
        return None
    d = pd.read_csv(path)
    return d


def t_crit(n: int) -> float:
    from scipy import stats
    n = int(n)
    if n < 2:
        return float("nan")
    return float(stats.t.ppf(0.975, n - 1))


def tidy(ax, xgrid=False, ygrid=False):
    """Paper frame: all four spines, thin black, no grid (matches the reference papers)."""
    for s in ("top", "right", "left", "bottom"):
        ax.spines[s].set_visible(True)
        ax.spines[s].set_linewidth(0.8)
        ax.spines[s].set_color("#000000")
    ax.grid(False)
    ax.set_axisbelow(True)
    return ax


def head_legend(ax, ncol, y=1.02, **kw):
    return ax.legend(loc="lower center", bbox_to_anchor=(0.5, y), ncol=ncol,
                     frameon=False, borderaxespad=0.0, handletextpad=0.5, **kw)


def save(fig, stem: str, w: float, h: float):
    OUT.mkdir(parents=True, exist_ok=True)
    made = []
    for ext in (".png", ".pdf", ".svg"):
        p = OUT / f"{stem}{ext}"
        fig.savefig(p, dpi=300)
        made.append(p)
    plt.close(fig)
    print(f"  ok {stem}  ({w:.1f}x{h:.1f} in, aspect {w/h:.3f})")
    return made


# ---------------------------------------------------------------- Fig 2
def fig2_netshare():
    """Delta-p95 against network time share, split into the two payload regimes.

    The two regimes differ by ~5 orders of magnitude in share, so a single
    log axis leaves ~90% of the canvas empty and stacks every point at the two
    ends. Panel (a)/(b) give each regime its own linear axis, which is also the
    "vertical gap between the two payload regimes" the caption refers to.
    """
    d = read_csv(RES / "analysis_v2" / "rq13_bandwidth_effect.csv")
    d = d[d["baseline"] == LEAST_LOAD]

    colour = {"data_heavy": BLUE, "mixed": ORANGE}
    marker = {"data_heavy": "o", "mixed": "^"}
    label = {"data_heavy": "data-heavy", "mixed": "mixed"}

    fig = plt.figure(figsize=(W, 3.75))
    gs = fig.add_gridspec(1, 2, width_ratios=[1.0, 1.62], wspace=0.08,
                          left=0.105, right=0.985, bottom=0.175, top=0.80)
    axL = fig.add_subplot(gs[0, 0])
    axR = fig.add_subplot(gs[0, 1], sharey=axL)

    panels = [(axL, "prompt_only", r"4 B/token  (token-id only)", 1.20e-5,
               [0, 4e-6, 8e-6], ["0", "4", "8"], r"Network time share ($\times10^{-6}$)"),
              (axR, "kv_cache", r"330 kB/token  (KV cache)", 1.0,
               [0, 0.25, 0.50, 0.75, 1.0], ["0", "0.25", "0.50", "0.75", "1"],
               "Network time share")]

    for ax, dv, title, xmax, ticks, tlabs, xlab in panels:
        ax.axhspan(-1, 1, color="#F2F2F2", zorder=0)
        ax.axhline(0, color="#444444", lw=0.8, ls=(0, (4, 2)), zorder=1)
        for wl in ("data_heavy", "mixed"):
            s = d[(d["data_volume"] == dv) & (d["workload"] == wl)].sort_values("net_share_na")
            if s.empty:
                continue
            tc = t_crit(s["n_seeds"].iloc[0])
            ax.errorbar(s["net_share_na"], s["dp95_pct"], yerr=s["dp95_se"] * tc,
                        fmt=marker[wl], ms=4.0, color=colour[wl], mfc=colour[wl],
                        mec=colour[wl], mew=0.0, elinewidth=0.7, capsize=2.0, capthick=0.7,
                        ecolor=colour[wl], alpha=0.95, ls="none", zorder=3,
                        label=label[wl])
        ax.set_xlim(-0.04 * xmax, xmax)
        ax.set_xticks(ticks)
        ax.set_xticklabels(tlabs)
        ax.set_xlabel(xlab)
        ax.set_title(title, fontsize=8.5, loc="left", pad=4)
        tidy(ax)

    axL.set_ylim(-21, 27)
    axL.set_ylabel(r"$\Delta$p95 vs. LeastLoad (%)")
    axR.tick_params(labelleft=False, left=False)
    axL.text(0.03, 25.8, "shaded band: |Δp95| < 1%", fontsize=8, color="#999999",
             ha="left", va="top", transform=axL.get_yaxis_transform())

    hs = [Line2D([], [], marker="o", ls="none", color=BLUE, ms=4.6, label="data-heavy"),
          Line2D([], [], marker="^", ls="none", color=ORANGE, ms=4.6, label="mixed")]
    axL.legend(handles=hs, loc="lower right", bbox_to_anchor=(0.995, 0.025), ncol=2,
               frameon=False, fontsize=8, borderaxespad=0.0, handletextpad=0.5,
               columnspacing=1.0)
    return save(fig, "fig_benefit_vs_netshare", W, 3.75)


# ---------------------------------------------------------------- Fig 3
def fig3_alignment_bars():
    d = read_csv(RES / "analysis_v2" / "rq17_alignment_effect.csv")
    combos = [("data_heavy", "degraded"), ("data_heavy", "unstable"),
              ("mixed", "degraded"), ("mixed", "unstable")]
    arms = [("anti", "-1", "Anti-aligned  ($\\rho=-1$)"), 
            ("rot1", "0.5", "Rotated  ($\\rho=+0.50$)"),
            ("aligned", "1", "Aligned  ($\\rho=+1$)")]
    cols = {"anti": BLUE, "rot1": ORANGE, "aligned": GREY}

    fig, ax = plt.subplots(figsize=(W, 3.45))
    fig.subplots_adjust(left=0.085, right=0.985, bottom=0.155, top=0.845)
    x = np.arange(len(combos))
    width = 0.25
    for k, (arm, _, lab) in enumerate(arms):
        xs, ys, es = [], [], []
        for j, (wl, nw) in enumerate(combos):
            row = d[(d["align"] == arm) & (d["workload"] == wl) & (d["network"] == nw)]
            if row.empty:
                continue
            tc = t_crit(row["n_seeds"].iloc[0])
            xs.append(x[j] + (k - 1) * width)
            ys.append(float(row["dp95_pct"].iloc[0]))
            es.append(float(row["dp95_se"].iloc[0]) * tc)
        ax.bar(xs, ys, width=width * 0.88, color=cols[arm], label=lab,
               edgecolor="black", linewidth=0.5, zorder=2)
        ax.errorbar(xs, ys, yerr=es, fmt="none", ecolor="#333333",
                    elinewidth=0.7, capsize=1.9, capthick=0.7, zorder=3)

    ax.axhline(0, color="#333333", lw=0.8, zorder=4)
    ax.set_xticks(x)
    ax.set_xticklabels(["data-heavy\ndegraded", "data-heavy\nunstable",
                        "mixed\ndegraded", "mixed\nunstable"], fontsize=8)
    ax.set_xlim(-0.55, 3.55)
    ax.set_ylim(-80, 6)
    ax.set_yticks([0, -20, -40, -60, -80])
    ax.set_ylabel(r"$\Delta$p95 vs. LeastLoad (%)")
    for j in range(len(combos) - 1):
        ax.axvline(x[j] + 0.5, color="#DDDDDD", lw=0.6, ls=(0, (2, 3)), zorder=1)
    tidy(ax, ygrid=True)
    head_legend(ax, 3, y=1.01)
    return save(fig, "fig_alignment_bars", W, 3.45)


# ---------------------------------------------------------------- Fig 4
def fig4_alignment_vs_scale():
    d = read_csv(RES / "analysis_v2" / "rq18_alignment_scale_effect.csv")
    lc = read_csv(RES / "analysis_v2" / "link_choice_by_scale.csv")
    rc = {}
    if lc is not None:
        for (a, n), sub in lc.groupby(["align", "n_nodes"]):
            if int(n) == 3:          # N=3 config is not part of this sweep
                continue
            rc[(str(a), int(n))] = float(sub["rank_corr"].iloc[0])

    nodes = [4, 8, 16]
    xi = {n: i for i, n in enumerate(nodes)}
    series = [("anti", "Anti-aligned", BLUE, "o"),
              ("rot1", "Rotated", ORANGE, "s"),
              ("aligned", "Aligned", GREY, "^")]

    fig, ax = plt.subplots(figsize=(W, 3.95))
    fig.subplots_adjust(left=0.09, right=0.985, bottom=0.145, top=0.855)
    rho_txt = [dict() for _ in nodes]
    for arm, lab, col, mk in series:
        s = d[d["align"] == arm].sort_values("n_nodes")
        if s.empty:
            continue
        tc = t_crit(s["n_seeds"].iloc[0])
        xs = [xi[int(n)] for n in s["n_nodes"]]
        ax.errorbar(xs, s["dp95_pct"], yerr=s["dp95_se"] * tc, fmt=mk + "-", ms=4.0,
                    lw=1.0, color=col, mfc=(col if arm != "aligned" else "white"),
                    mec=col, mew=0.9, elinewidth=0.7, capsize=2.0, capthick=0.7,
                    label=lab, zorder=3)
        for _, row in s.iterrows():
            key = (arm, int(row["n_nodes"]))
            if key not in rc:
                continue
            rho_txt[xi[int(row["n_nodes"])]][arm] = rc[key]

    ax.axhline(0, color="#444444", lw=0.8, ls=(0, (4, 2)), zorder=1)
    ax.set_xticks([0, 1, 2])
    ax.set_xticklabels(["4", "8", "16"])
    ax.set_xlim(-0.48, 2.48)
    ax.set_ylim(-77, 3)
    ax.set_xlabel("Cluster size (nodes)")
    ax.set_ylabel(r"$\Delta$p95 vs. LeastLoad (%)")
    tidy(ax, ygrid=True)
    head_legend(ax, 3, y=1.01)

    # attained rank correlation, one colour-coded row inside the empty lower band
    yrho = -72.5
    ax.axhline(-66.5, color="#EEEEEE", lw=0.7, zorder=1)
    for i, dd in enumerate(rho_txt):
        for k, arm in enumerate(("anti", "rot1", "aligned")):
            if arm not in dd:
                continue
            col = {"anti": BLUE, "rot1": ORANGE, "aligned": GREY}[arm]
            ax.text(i + (k - 1) * 0.30, yrho,
                    f"$\\rho$ {dd[arm]:+.2f}".replace("-", "\u2212"), ha="center",
                    va="center", fontsize=8, color=col)
    return save(fig, "fig_alignment_vs_scale", W, 3.95)


# ---------------------------------------------------------------- Fig 5
def fig5_staleness_2d():
    d = read_csv(RES / "analysis_v2" / "rq16_staleness2d_effect.csv")
    qs = sorted(d["staleness_queue_s"].unique())
    ns = sorted(d["staleness_network_s"].unique())
    panels = [("data_heavy", "(a) data-heavy"), ("mixed", "(b) mixed")]

    fig = plt.figure(figsize=(W, 2.85))
    gs = fig.add_gridspec(1, 3, width_ratios=[1.0, 1.0, 0.042], wspace=0.22,
                          left=0.105, right=0.925, bottom=0.205, top=0.855)
    cmap = plt.get_cmap("hot_r")          # 0 -> black (no benefit), -30 -> white (largest benefit)
    norm = matplotlib.colors.Normalize(vmin=-30, vmax=0)
    im = None
    for i, (wl, title) in enumerate(panels):
        ax = fig.add_subplot(gs[0, i])
        sub = d[d["workload"] == wl]
        grid = np.full((len(qs), len(ns)), np.nan)
        for a, q in enumerate(qs):
            for b, n in enumerate(ns):
                row = sub[(sub["staleness_queue_s"] == q) & (sub["staleness_network_s"] == n)]
                if not row.empty:
                    grid[a, b] = float(row["dp95_pct"].iloc[0])
        im = ax.imshow(grid, cmap=cmap, norm=norm, aspect="auto", origin="upper",
                       interpolation="nearest")
        for a in range(len(qs)):
            for b in range(len(ns)):
                v = grid[a, b]
                if not np.isfinite(v):
                    continue
                txtcol = "white" if norm(v) > 0.55 else "#111111"
                ax.text(b, a, f"{v:.0f}".replace("-", "\u2212"), ha="center",
                        va="center", fontsize=8.5, color=txtcol)
        ax.set_xticks(range(len(ns)))
        ax.set_xticklabels([f"{x:g}" for x in ns])
        ax.set_yticks(range(len(qs)))
        ax.set_yticklabels([f"{x:g}" for x in qs])
        ax.set_xlabel("Link-state age (s)")
        if i == 0:
            ax.set_ylabel("Queue-state age (s)")
        ax.set_title(title, loc="left", pad=5, fontsize=8.5)
        for sp in ax.spines.values():
            sp.set_linewidth(0.8)
            sp.set_color("#000000")
        ax.tick_params(length=0)

    cax = fig.add_subplot(gs[0, 2])
    cb = fig.colorbar(im, cax=cax)
    cb.set_label(r"$\Delta$p95 vs. LeastLoad (%)", fontsize=8)
    cb.ax.tick_params(labelsize=8, length=2.4)
    cb.outline.set_linewidth(0.8)
    cax.text(0.5, -0.09, "brighter =\nlarger benefit", transform=cax.transAxes,
             ha="center", va="top", fontsize=8, color="#666666")
    return save(fig, "fig_staleness_2d", W, 2.85)


# ---------------------------------------------------------------- Fig 6
DISCOUNT_ARMS = [("NetworkAware2", "Full trust", BLUE),
                 ("AdaptChannel", "Per-channel", ORANGE),
                 ("AdaptSplit", "Term-split", GREEN),
                 ("AdaptLinear", "Uniform", RED),
                 ("AdaptHard", "Hard threshold", GREY)]

DISCOUNT_CELLS = [
    ("Synthetic", "data_heavy", "degraded", 0.0, 60.0, "Synthetic\ndata-heavy"),
    ("Synthetic", "mixed", "degraded", 0.0, 60.0, "Synthetic\nmixed"),
    ("Azure", "data_heavy", "degraded", 0.0, 60.0, "Azure\ndata-heavy"),
    ("Azure", "mixed", "degraded", 0.0, 60.0, "Azure\nmixed"),
    ("BurstGPT", "mixed", "degraded", 0.0, 60.0, "BurstGPT\nmixed"),
    ("BurstGPT", "mixed", "unstable", 5.0, 60.0, "BurstGPT\nmixed, volatile"),
]


def fig6_discount_rules():
    tabs = {k: read_csv(v / "rq19_decay_rule_effect.csv") for k, v in BATCH_DIRS.items()}
    x = np.arange(len(DISCOUNT_CELLS))
    width = 0.165
    fig, ax = plt.subplots(figsize=(W, 3.55))
    fig.subplots_adjust(left=0.085, right=0.985, bottom=0.175, top=0.83)

    for k, (arm, lab, col) in enumerate(DISCOUNT_ARMS):
        xs, ys = [], []
        for j, (batch, wl, nw, q, n, _) in enumerate(DISCOUNT_CELLS):
            t = tabs.get(batch)
            if t is None:
                continue
            row = t[(t["arm"] == arm) & (t["workload"] == wl) & (t["network"] == nw)
                    & (t["staleness_queue_s"] == q) & (t["staleness_network_s"] == n)]
            if row.empty:
                continue
            xs.append(x[j] + (k - (len(DISCOUNT_ARMS) - 1) / 2) * width)
            ys.append(float(row["dp95_vs_leastload_pct"].iloc[0]))
        ax.bar(xs, ys, width=width * 0.9, color=col, label=lab, edgecolor="black",
               linewidth=0.4, zorder=2)

    ax.axhline(0, color="#333333", lw=0.8, zorder=3)
    ax.set_xticks(x)
    ax.set_xticklabels([c[5] for c in DISCOUNT_CELLS], fontsize=8)
    ax.set_xlim(-0.55, len(DISCOUNT_CELLS) - 0.45)
    ax.set_ylim(-85, 42)
    ax.set_ylabel(r"$\Delta$p95 vs. LeastLoad (%)")
    ax.set_xlabel("Condition  (link snapshot ≈ 60 s old; queue fresh except the last cell)",
                  fontsize=8.5, labelpad=3)
    for j in (1.5, 3.5):
        ax.axvline(j, color="#DDDDDD", lw=0.6, ls=(0, (2, 3)), zorder=1)
    tidy(ax, ygrid=True)
    head_legend(ax, 5, y=1.01, fontsize=8)
    return save(fig, "fig_discount_rules", W, 3.55)


# ---------------------------------------------------------------- Fig 7
ABL_ROWS = [("Tq2", r"$\tau_q$: 4 s $\rightarrow$ 2 s"),
            ("Tq8", r"$\tau_q$: 4 s $\rightarrow$ 8 s"),
            ("Tn20", r"$\tau_n$: 60 s $\rightarrow$ 20 s"),
            ("Tn120", r"$\tau_n$: 60 s $\rightarrow$ 120 s"),
            ("floor0.5", r"floor: 0.7 $\rightarrow$ 0.5"),
            ("floor0.9", r"floor: 0.7 $\rightarrow$ 0.9")]
ABL_SRC = [("Synthetic", BLUE, "o"), ("Azure", ORANGE, "s"), ("BurstGPT", GREEN, "^")]


def fig7_ablation():
    tabs = {k: read_csv(v / "rq21_channel_threshold_ablation.csv") for k, v in BATCH_DIRS.items()}
    tabs = {k: (v.set_index("variant") if v is not None else None) for k, v in tabs.items()}

    fig, ax = plt.subplots(figsize=(W, 3.3))
    fig.subplots_adjust(left=0.235, right=0.865, bottom=0.155, top=0.86)

    n_src = len(ABL_SRC)
    dodge = 0.22
    flips_max = []
    for k, (src, col, mk) in enumerate(ABL_SRC):
        t = tabs.get(src)
        if t is None:
            continue
        xs, ys, es = [], [], []
        for i, (var, _) in enumerate(ABL_ROWS):
            if var not in t.index:
                continue
            row = t.loc[var]
            xs.append(float(row["vs_primary_mean_pct"]))
            ys.append(i + (k - (n_src - 1) / 2) * dodge)
            es.append(float(row["vs_primary_ci"]))
        ax.errorbar(xs, ys, xerr=es, fmt=mk, ms=4.0, color=col, mfc=col,
                    mec=col, mew=0.0, elinewidth=0.7, capsize=2.0, capthick=0.7,
                    ls="none", label=src, zorder=3)

    # flips: max over sources
    for i, (var, _) in enumerate(ABL_ROWS):
        flip = 0
        ncond = 12
        for src, _, _ in ABL_SRC:
            t = tabs.get(src)
            if t is None or var not in t.index:
                continue
            flip = max(flip, int(t.loc[var, "n_conditions_sign_flip_vs_na2"]))
            ncond = int(t.loc[var, "n_conditions"])
        ax.text(1.015, i, f"{flip}/{ncond}", transform=ax.get_yaxis_transform(),
                ha="left", va="center", fontsize=8, color="#444444")

    ax.axvline(0, color="#333333", lw=0.8, zorder=2)
    ax.set_yticks(range(len(ABL_ROWS)))
    ax.set_yticklabels([r[1] for r in ABL_ROWS], fontsize=8)
    ax.set_ylim(len(ABL_ROWS) - 0.45, -0.55)
    ax.set_xlim(-15.5, 16.0)
    ax.set_xticks([-15, -10, -5, 0, 5, 10, 15])
    ax.set_xlabel(r"Mean $\Delta$p95 vs. the unmodified rule (%)")
    ax.text(1.015, -0.5, "sign\nflips", transform=ax.get_yaxis_transform(),
            ha="left", va="center", fontsize=8, color="#444444")
    for j in (1.5, 3.5):
        ax.axhline(j, color="#EEEEEE", lw=0.8, zorder=1)
    tidy(ax, xgrid=True)
    head_legend(ax, 3, y=1.01, fontsize=8)
    return save(fig, "fig_ablation", W, 3.3)


# ---------------------------------------------------------------- Fig 8
K8S_RE = re.compile(r"^k3s_(?P<tag>[rq][1-3])_(?P<policy>least_load|contention)"
                    r"_lag(?P<lag>[0-9]+)_xfer(?P<xfer>[0-9.]+)_q(?P<qps>[0-9]+)\.csv$")


def _k8s_table():
    rows = []
    for path in sorted(K8S_DIR.glob("k3s_*.csv")):
        m = K8S_RE.match(path.name)
        if not m or float(m.group("xfer")) != 8.0:
            continue
        d = pd.read_csv(path, usecols=lambda c: c in ("ok", "latency_s"))
        good = d[(d["ok"] == 1) & d["latency_s"].notna()]
        if good.empty:
            continue
        rows.append(dict(tag=m.group("tag"), policy=m.group("policy"),
                         lag=int(m.group("lag")), qps=int(m.group("qps")),
                         p95=float(np.quantile(good["latency_s"].to_numpy(float), 0.95))))
    return pd.DataFrame(rows)


def fig8_real_cluster():
    tab = _k8s_table()
    cells = sorted({(int(r.qps), int(r.lag)) for r in tab.itertuples()})
    tags = [f"r{i}" for i in (1, 2, 3)] + [f"q{i}" for i in (1, 2, 3)]

    deltas = {c: [] for c in cells}
    for tag in tags:
        for c in cells:
            row_c = tab[(tab["tag"] == tag) & (tab["policy"] == "contention")
                        & (tab["qps"] == c[0]) & (tab["lag"] == c[1])]
            row_l = tab[(tab["tag"] == tag) & (tab["policy"] == "least_load")
                        & (tab["qps"] == c[0]) & (tab["lag"] == c[1])]
            if row_c.empty or row_l.empty or float(row_l["p95"].iloc[0]) <= 0:
                continue
            dv = (float(row_c["p95"].iloc[0]) - float(row_l["p95"].iloc[0])) \
                / float(row_l["p95"].iloc[0]) * 100.0
            deltas[c].append((tag, dv))

    fig, ax = plt.subplots(figsize=(W, 3.45))
    fig.subplots_adjust(left=0.09, right=0.90, bottom=0.145, top=0.845)

    ax.axhspan(-10, 10, color="#F2F2F2", zorder=0)

    rng = np.random.default_rng(7)
    for j, c in enumerate(cells):
        pts = deltas[c]
        for tag, dv in pts:
            loaded = tag.startswith("r")
            xj = j + rng.uniform(-0.12, 0.12)
            ax.plot([xj], [dv], marker="o", ms=3.8,
                    mfc=("#FFFFFF" if not loaded else BLUE),
                    mec=BLUE, mew=0.9, ls="none", zorder=3)
        med = np.median([p[1] for p in pts])
        ax.plot([j - 0.24, j + 0.24], [med, med], color="#222222", lw=1.5,
                solid_capstyle="butt", zorder=4)

    ax.axhline(0, color="#333333", lw=0.9, zorder=2)
    ax.set_xticks(range(len(cells)))
    ax.set_xticklabels([f"{q} QPS\n{lag:g} s lag" for q, lag in cells], fontsize=8)
    ax.set_xlim(-0.55, len(cells) - 0.45)
    ax.set_ylim(-47, 30)
    ax.set_yticks([-30, -20, -10, 0, 10, 20, 30])
    ax.set_ylabel(r"$\Delta$p95 vs. network-blind baseline (%)")
    ax.set_xlabel("(offered load, state lag)")

    ax.annotate("only cell with a stable\nnegative sign in all six",
                xy=(0.08, -31.7), xytext=(0.55, -42.5), fontsize=8, color=BLUE,
                ha="left", va="center",
                arrowprops=dict(arrowstyle="-", lw=0.6, color=BLUE, shrinkA=0, shrinkB=2))
    tidy(ax, ygrid=True)
    hs = [Line2D([], [], marker="o", ls="none", color=BLUE, ms=4.6,
                 label="concurrent job on host (r1–r3)"),
          Line2D([], [], marker="o", ls="none", color=BLUE, mfc="white", ms=4.6,
                 label="idle machine (q1–q3)"),
          Line2D([], [], color="#222222", lw=1.5, label="median of six"),
          Patch(facecolor="#F2F2F2", edgecolor="none",
                label="±10% band: below testbed dispersion")]
    head_legend(ax, 4, y=1.01, fontsize=8, handles=hs)
    print("  F8 deltas (cell -> repeats):")
    for c in cells:
        print("   ", c, " ".join(f"{d:+.1f}" for _, d in deltas[c]))
    return save(fig, "fig_real_cluster", W, 3.45)


# ---------------------------------------------------------------- Fig 9
def _pn():
    out = {}
    for line in PAPER_NUMBERS.read_text(encoding="utf-8").splitlines():
        if " = " in line and not line.lstrip().startswith("#"):
            k, v = line.split(" = ", 1)
            out[k.strip()] = v.strip()
    return out


def fig9_cloud_ttft():
    v = _pn()

    def rec(key):
        return {str(k): float(x) for k, x in ast.literal_eval(v[key]).items()}

    rows = [("Base", rec("B2.descriptive.base_c1"), rec("B2.descriptive.base_c4")),
            ("Same-cloud\nsplit", rec("B2.descriptive.disagg_c1"), rec("B2.descriptive.disagg_c4")),
            ("Alibaba\u2192Tencent\n(D1)", rec("C.D1.serial"), rec("C.D1.conc4")),
            ("Tencent\u2192Alibaba\n(D2)", rec("C.D2.serial"), rec("C.D2.conc4"))]

    fig = plt.figure(figsize=(W, 3.7))
    gs = fig.add_gridspec(1, 2, width_ratios=[1.02, 1.0], wspace=0.30,
                          left=0.115, right=0.985, bottom=0.135, top=0.795)
    ax0 = fig.add_subplot(gs[0, 0])
    ax1 = fig.add_subplot(gs[0, 1])

    # ---- panel (a): log-scale p50 -> p95 interval plot
    y = np.arange(len(rows))[::-1]
    for k, (key50, key95, col, mk, lab, dodge) in enumerate([
            ("ttft_p50", "ttft_p95", BLUE, "o", "serial", -0.16),
            ("ttft_p50_s", "ttft_p95_s", ORANGE, "s", "concurrency 4", 0.16)]):
        pass
    for k, (lab, col, mk, dodge) in enumerate([("serial", BLUE, "o", -0.15),
                                               ("concurrency 4", ORANGE, "s", 0.15)]):
        for i, (name, s, c) in enumerate(rows):
            if lab == "serial":
                p50 = s.get("ttft_p50", s.get("ttft_p50_s"))
                p95 = s.get("ttft_p95", s.get("ttft_p95_s"))
            else:
                p50 = c.get("ttft_p50", c.get("ttft_p50_s"))
                p95 = c.get("ttft_p95", c.get("ttft_p95_s"))
            yy = y[i] + dodge
            ax0.plot([p50, p95], [yy, yy], color=col, lw=1.0, alpha=0.55,
                     solid_capstyle="butt", zorder=2)
            ax0.plot([p50], [yy], marker=mk, ms=4.0, color=col, mfc=col,
                     mec=col, mew=0.0, ls="none", zorder=3,
                     label=(lab if i == 0 else None))

    ax0.set_xscale("log")
    ax0.set_xlim(0.055, 34)
    ax0.set_xticks([0.1, 1, 10])
    ax0.set_xticklabels(["0.1", "1", "10"])
    ax0.set_yticks(y)
    ax0.set_yticklabels([r[0] for r in rows], fontsize=8)
    ax0.set_ylim(-0.6, len(rows) - 0.4)
    ax0.set_xlabel("TTFT (s, log scale)")
    ax0.text(0.0, 1.105, "(a)", transform=ax0.transAxes, fontsize=8, fontweight="bold",
             ha="left", va="bottom")
    tidy(ax0, xgrid=True)
    h9 = [Line2D([], [], marker="o", ls="none", color=BLUE, ms=4.6, label="serial"),
          Line2D([], [], marker="s", ls="none", color=ORANGE, ms=4.6, label="concurrency 4"),
          Line2D([], [], color="#999999", lw=1.0, alpha=0.7, label="bar: p50–p95 range")]
    head_legend(ax0, 3, y=1.02, fontsize=8, handles=h9)

    # ---- panel (b): C-stage background
    bw = {"D1": 8.6, "D2": 11.7}
    style = {"D1": (BLUE, "o"), "D2": (ORANGE, "s")}
    for dtag in ("D1", "D2"):
        pts = []
        for pid in ("q01_rep6", "q02_rep10", "q03_rep14", "q04_rep18", "q05_rep22"):
            raw = v.get(f"C.bg.{dtag}.{pid}")
            if not raw:
                continue
            f = dict(p.split("=", 1) for p in raw.split())
            pts.append((float(f["est_KV_MB"]), float(f["ttft_p50"])))
        if not pts:
            continue
        pts.sort()
        xx = np.array([p[0] for p in pts])
        yy = np.array([p[1] for p in pts])
        col, mk = style[dtag]
        ax1.plot(xx, yy, marker=mk, ms=3.8, color=col, mfc=col, mec=col, mew=0.0,
                 ls="none", label=f"{dtag} observed", zorder=3)
        ref = yy[0] + (xx - xx[0]) / bw[dtag]
        ax1.plot(xx, ref, color=col, lw=1.0, ls=(0, (4, 2)), alpha=0.85,
                 label=f"{dtag}: 1/bandwidth reference", zorder=2)

    ax1.set_xlabel("Estimated KV volume (MB)")
    ax1.set_ylabel("TTFT p50 (s)")
    ax1.set_xlim(12, 74)
    ax1.set_ylim(1.6, 8.3)
    ax1.text(0.0, 1.105, "(b)", transform=ax1.transAxes, fontsize=8, fontweight="bold",
             ha="left", va="bottom")
    tidy(ax1, ygrid=True)
    ax1.legend(loc="upper left", fontsize=8, handletextpad=0.5,
               labelspacing=0.35, borderpad=0.2)
    return save(fig, "fig_cloudttft", W, 3.7)


FIGURES = {
    "2": fig2_netshare,
    "3": fig3_alignment_bars,
    "4": fig4_alignment_vs_scale,
    "5": fig5_staleness_2d,
    "6": fig6_discount_rules,
    "7": fig7_ablation,
    "8": fig8_real_cluster,
    "9": fig9_cloud_ttft,
}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    a = ap.parse_args(argv)
    want = [s.strip() for s in a.only.split(",") if s.strip()] or list(FIGURES)
    for k in want:
        if k not in FIGURES:
            print(f"unknown figure {k}")
            return 2
        print(f"[Fig {k}]")
        FIGURES[k]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

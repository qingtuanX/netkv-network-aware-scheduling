# -*- coding: utf-8 -*-
"""Fig. 2 重画：份额 → Δp95 的完整曲线 + 低份额"有害区"放大。

尺寸按 docx 里这张图的实际显示框来定：extent = 6.30 × 2.48 in。
figsize 取同一个数，图在正文里就是 1:1 显示，字号不会被缩放吃掉
（2026-10-28 的硬要求：最终图内不得有小于 8 pt 的文字）。

两处刻意设计：
  * 不画 0.10 竖线。闸门比较的是**预估**份额（观测平均带宽算出），
    与横轴的**实测**份额不是一个量；画在一起会复现旧稿的混淆。
  * 专门给 (b) 面板做放大。全额程从 +4% 到 −74%，低份额那一段在总轴上
    被压成一条线，而"小幅有害"正是本节能否洗掉"闸门同义反复"的关键证据。
"""
from __future__ import annotations

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

BLUE, ORANGE, GREEN, PURPLE = "#2169b1", "#d2601f", "#4a7a2b", "#7a5ea8"

COL = {"compute_heavy": BLUE, "data_heavy": ORANGE,
       "low_latency": GREEN, "mixed": PURPLE}
MK = {"compute_heavy": "s", "data_heavy": "o", "low_latency": "D", "mixed": "^"}
LAB = {"compute_heavy": "compute-heavy", "data_heavy": "data-heavy",
       "low_latency": "low-latency", "mixed": "mixed"}
WLS = ["compute_heavy", "data_heavy", "low_latency", "mixed"]

W_IN, H_IN = 6.30, 2.48          # = docx 里的显示尺寸（EMU 5760000×2268588）
FS_TICK, FS_LAB, FS_LEG, FS_NOTE = 8.0, 8.5, 8.0, 8.0


def _tidy(ax):
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_linewidth(0.8)
        ax.spines[s].set_color("#000000")
    ax.tick_params(labelsize=FS_TICK, width=0.8, length=3)
    ax.grid(False)


def _scatter(ax, pts, ms=3.4, ebar=True):
    for wl in WLS:
        s = pts[pts.workload == wl].sort_values("net_share")
        if s.empty:
            continue
        if ebar:
            ax.errorbar(s["net_share"], s["dp95_pct"], yerr=s["ci95"],
                        fmt=MK[wl], ms=ms, color=COL[wl], mfc=COL[wl], mec=COL[wl],
                        mew=0.0, elinewidth=0.6, capsize=1.5, capthick=0.6,
                        alpha=0.95, ls="none", zorder=3, label=LAB[wl])
        else:
            ax.plot(s["net_share"], s["dp95_pct"], MK[wl], ms=ms, color=COL[wl],
                    mec=COL[wl], ls="none", zorder=3)


def make_figure(pts: pd.DataFrame, gate: pd.DataFrame, out: Path,
                stem: str = "fig_benefit_vs_netshare") -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    fig = plt.figure(figsize=(W_IN, H_IN), dpi=300)
    gs = fig.add_gridspec(1, 2, width_ratios=[1.75, 1.0], wspace=0.20,
                          left=0.088, right=0.985, bottom=0.155, top=0.955)
    ax = fig.add_subplot(gs[0, 0])
    ax2 = fig.add_subplot(gs[0, 1])

    # ---------------- (a) 全曲线 ----------------
    ax.set_xscale("log")
    ax.axhspan(-1, 1, color="#F1F1F1", zorder=0)
    ax.axhline(0, color="#555555", lw=0.7, ls=(0, (4, 2)), zorder=1)
    _scatter(ax, pts)
    ax.set_xlim(1.2e-6, 1.3)
    ax.set_ylim(-82, 9)
    ax.set_xlabel("Network time share", fontsize=FS_LAB)
    ax.set_ylabel(r"$\Delta$p95 vs. LeastLoad (%)", fontsize=FS_LAB)
    ax.text(3.2e-6, 6.0, "shaded: $|\\Delta|$ < 1%", fontsize=FS_NOTE,
            color="#777777", ha="left", va="top")
    ax.legend(loc="lower left", frameon=False, fontsize=FS_LEG, ncol=2,
              handletextpad=0.4, columnspacing=0.8, borderaxespad=0.15,
              bbox_to_anchor=(0.0, 0.02))
    _tidy(ax)
    ax.set_title("(a) inert, then harmful, then large", fontsize=FS_NOTE,
                 loc="left", pad=3)

    # ---------------- (b) 有害区放大 ----------------
    ax2.set_xscale("log")
    ax2.axhspan(-1, 1, color="#F1F1F1", zorder=0)
    ax2.axhline(0, color="#555555", lw=0.7, ls=(0, (4, 2)), zorder=1)
    _scatter(ax2, pts, ms=3.2, ebar=False)
    ax2.set_xlim(8e-4, 4e-1)
    ax2.set_ylim(-11, 8)
    ax2.set_xlabel("Network time share", fontsize=FS_LAB)
    ax2.set_ylabel(r"$\Delta$p95 (%)", fontsize=FS_LAB)
    ax2.set_yticks([-5, 0, 5])
    ax2.set_title("(b) zoom on the harmful band", fontsize=FS_NOTE, loc="left", pad=3)
    ax2.text(0.97, 0.93, "harmful: up to +3.3%", transform=ax2.transAxes,
             fontsize=FS_NOTE, color="#444444", ha="right", va="top")
    _tidy(ax2)

    made = []
    for ext in (".png", ".pdf", ".svg"):
        p = out / f"{stem}{ext}"
        fig.savefig(p, dpi=300)
        made.append(p)
    plt.close(fig)
    print("figure ->", ", ".join(x.name for x in made))
    return made


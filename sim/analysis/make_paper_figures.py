#!/usr/bin/env python
"""从已有分析产物生成论文正文用的英文标签图（LaTeX 用 pdf，预览用 png）。

用法（在 ``sim/`` 下执行）::

    .venv/Scripts/python.exe analysis/make_paper_figures.py
    .venv/Scripts/python.exe analysis/make_paper_figures.py --fig-dir ../paper/figures
    .venv/Scripts/python.exe analysis/make_paper_figures.py --only F4,F7,F8

产物：``<仓库根>/paper/figures/fig_*.pdf`` + 同名 ``.png``。

为什么单独写一个脚本（而不是改 ``analyze.py``）
------------------------------------------------
``analyze.py`` 产出的图**是中文标签**（它按字体名自动挑到 Microsoft YaHei），
只能用于中文记录，**不能放进英文投稿**。这里读**同一批分析 CSV**（只读，
不改动 ``results/`` 下任何文件），重画成英文标签的版本。三条约定：

* 图内**所有**文字必须是英文。这里显式把字体钉死在不含 CJK 的 ``DejaVu Sans``
  （不许 matplotlib 自动挑字体），并且在每张图保存前把**图上真正渲染出来的每个字符串**
  扫一遍，发现 CJK 字符（U+4E00–U+9FFF）直接抛错——中文标签出现在英文稿里是硬错误，
  不能靠肉眼看图来发现。
* 所有写进图里的字符串都要经 ``T()`` 登记，脚本收尾统一再断言一次。
* 统计量（p95、se、CI、Δp95%）**一律读 CSV 里已经算好的列**，本脚本不重算，
  这样图上的数字与 ``report.txt``、``export_paper_numbers.py`` 必然一致。

列名与任务描述的两处出入（已按实际列名处理，见各 figure 函数的注释）：
``rq13_bandwidth_effect.csv`` 没有 ``arm`` 列（它按 ``baseline`` 分行，
每组合三行分别对 LeastLoad / LeastQueue / Random），故 F1 按 ``baseline == "LeastLoad"`` 过滤；
``rq21_channel_threshold_ablation.csv`` 没有 ``primary`` 行（primary 对自己恒为 0），
故 F6 补一行 ``primary = 0 ± 0`` 并标注 ``0/n``。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

import matplotlib
matplotlib.use("Agg")                                   # 必须在 import pyplot 之前

# 字体必须显式钉死：DejaVu Sans 是 matplotlib 自带字体，绝不含 CJK。
# 不写这一行的风险是某个环节把字体族设成中文字体，图里就出现画不出来的方块。
matplotlib.rcParams["font.family"] = "DejaVu Sans"
matplotlib.rcParams["axes.unicode_minus"] = False
matplotlib.rcParams["savefig.dpi"] = 200
matplotlib.rcParams["figure.dpi"] = 110
matplotlib.rcParams["font.size"] = 9.5
matplotlib.rcParams["pdf.fonttype"] = 42                # Type42：投稿用 pdf 字体可嵌入

import matplotlib.pyplot as plt                          # noqa: E402
from matplotlib.lines import Line2D                      # noqa: E402
from matplotlib.patches import Patch                     # noqa: E402
from matplotlib.text import Text                         # noqa: E402

# --------------------------------------------------------------------------
# 路径与常量
# --------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent            # sim/
sys.path.insert(0, str(ROOT))

FALLBACK_STDOUTS = [sys.stdout, sys.stderr]
for _stream in FALLBACK_STDOUTS:
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                     # noqa: BLE001
        pass

REPO = ROOT.parent                                       # 仓库根
DEFAULT_FIG_DIR = REPO / "paper" / "figures"

# 三批结果的**分析目录**（图只读这些目录里的 CSV）
BATCH_DIRS = {
    "synthetic": ROOT / "results" / "analysis_v2",
    "azure": ROOT / "results" / "analysis_trace_v2",
    "burstgpt": ROOT / "results" / "analysis_burstgpt_v2",
}
BATCH_ORDER = ["synthetic", "azure", "burstgpt"]
BATCH_PANEL_TITLE = {"synthetic": "Synthetic", "azure": "Azure", "burstgpt": "BurstGPT"}

K8S_DIR = ROOT / "results" / "k8s_test"
PAPER_NUMBERS = ROOT / "results" / "paper_numbers.txt"

NA2 = "NetworkAware2"
LEAST_LOAD = "LeastLoad"

# 图内文字登记表：凡是要写进图里的字符串都先经 T() 过一遍。
# 收尾统一断言里面没有 CJK——这是本脚本的硬门槛，不是可选的自检。
TEXTS: list[str] = []


def T(text: object) -> str:
    """登记一个将要写进图里的字符串并原样返回（收尾统一检查是否含 CJK）。"""
    s = str(text)
    TEXTS.append(s)
    return s


def has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(REPO))
    except ValueError:
        return str(path)


def paper_number_lines() -> dict[str, str]:
    """读取正文数字总表；真实云图表不得在脚本里另抄数字。"""
    out: dict[str, str] = {}
    if not PAPER_NUMBERS.exists():
        return out
    for line in PAPER_NUMBERS.read_text(encoding="utf-8").splitlines():
        if " = " in line and not line.lstrip().startswith("#"):
            key, value = line.split(" = ", 1)
            out[key.strip()] = value.strip()
    return out


def paper_number_float(values: dict[str, str], key: str) -> float:
    """取简单浮点标签；复杂字典标签由调用方按固定格式解析。"""
    return float(values[key])


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def read_csv(path: Path) -> pd.DataFrame | None:
    """读一个分析产物；缺文件/读不动时返回 None（图里留空，不崩）。"""
    if not path.exists():
        print(f"  [缺] {rel(path)}（该图/该批跳过）")
        return None
    try:
        return pd.read_csv(path)
    except Exception as exc:                              # noqa: BLE001
        print(f"  [错] 读 {rel(path)} 失败：{exc}")
        return None


def t_crit(n_seeds, conf: float = 0.975) -> float:
    """t 分布临界值 t(0.975, n-1)。优先用 scipy；没有就查一张小表。

    scipy 不在 requirements.txt 里（见 AGENTS §10 第 4 条），所以必须留退路。
    """
    try:
        n = int(n_seeds)
    except Exception:                                     # noqa: BLE001
        return float("nan")
    if n < 2:
        return float("nan")
    try:
        from scipy import stats                           # noqa: PLC0415
        return float(stats.t.ppf(conf, n - 1))
    except Exception:                                     # noqa: BLE001
        table = {2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447,
                 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228, 19: 2.093, 20: 2.086}
        return table.get(n, 2.04)


def fig_texts(fig) -> list[str]:
    """把图里**实际渲染出来**的所有字符串抓出来（含自动生成的刻度标签）。"""
    fig.canvas.draw()                                     # 先描一遍，刻度标签才实例化
    out: list[str] = []
    for artist in fig.findobj(Text):
        try:
            s = artist.get_text()
        except Exception:                                 # noqa: BLE001
            continue
        if s:
            out.append(str(s))
    return out


def save_fig(fig, stem: str, fig_dir: Path) -> list[Path]:
    """保存 pdf + png，并在保存前断言图上没有任何 CJK 字符。"""
    rendered = fig_texts(fig)
    bad = [s for s in rendered if has_cjk(s)]
    if bad:
        raise AssertionError(
            f"{stem}: 图里出现 CJK 字符，英文投稿不能这样：{bad[:5]}"
        )
    fig_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for ext in (".pdf", ".png"):
        path = fig_dir / f"{stem}{ext}"
        fig.savefig(path, bbox_inches="tight")
        paths.append(path)
    plt.close(fig)
    size = paths[-1].stat().st_size
    print(f"  [图] {rel(paths[0])} / {rel(paths[1])}  （{len(rendered)} 个文字串，"
          f"png {size / 1024:.0f} KiB）")
    return paths


# --------------------------------------------------------------------------
# F1：收益随网络占比的变化
# --------------------------------------------------------------------------
def fig_benefit_vs_netshare(fig_dir: Path) -> list[Path]:
    """Δp95 对网络占比散点：两个数据量档 × 两类负载。

    注意实际的列名：``rq13_bandwidth_effect.csv`` **没有 arm 列**，它是按
    ``baseline`` 分行（同一组合各有对 LeastLoad / LeastQueue / Random 的三行），
    所以这里过滤的是 ``baseline == "LeastLoad"``（论文的主基线）。
    x 用**对数轴**：``prompt_only`` 档的 net_share 是 1e-6 量级，``kv_cache`` 档是
    0.06–0.90，差五个数量级，线性轴会把前者全压在 0 上。
    """
    print("[F1] fig_benefit_vs_netshare")
    d = read_csv(ROOT / "results" / "analysis_v2" / "rq13_bandwidth_effect.csv")
    if d is None:
        return []
    missing = [c for c in ("baseline", "data_volume", "workload", "net_share_na", "dp95_pct")
               if c not in d.columns]
    if missing:
        print(f"  [错] 缺列 {missing}，实际列名：{list(d.columns)}")
        return []
    d = d[d["baseline"] == LEAST_LOAD].copy()
    if d.empty:
        print("  [错] 过滤 baseline==LeastLoad 后没有数据")
        return []

    fig, ax = plt.subplots(figsize=(6.6, 4.4), constrained_layout=True)
    dv_style = [("prompt_only", "#1f77b4"), ("kv_cache", "#d62728")]
    wl_marker = [("data_heavy", "o"), ("mixed", "^")]
    for dv, color in dv_style:
        for wl, marker in wl_marker:
            s = d[(d["data_volume"] == dv) & (d["workload"] == wl)].sort_values("net_share_na")
            if s.empty:
                continue
            tc = t_crit(s["n_seeds"].iloc[0])
            ax.errorbar(
                s["net_share_na"], s["dp95_pct"],
                yerr=(s["dp95_se"] * tc if np.isfinite(tc) else None),
                marker=marker, ls="-", lw=0.9, ms=6.5, capsize=2.5, elinewidth=0.8,
                color=color, mec=color, alpha=0.95,
                mfc=(color if dv == "kv_cache" else "none"),
            )
    ax.axhline(0.0, color="0.35", ls="--", lw=0.9)
    ax.set_xscale("log")
    ax.set_xlabel(T("Network time share"))
    ax.set_ylabel(T("Δp95 vs. LeastLoad (%)"))
    ax.set_title(T("Benefit vanishes as network share shrinks"), fontsize=10.5)
    ax.grid(True, which="both", alpha=0.28, linewidth=0.6)
    ax.set_axisbelow(True)
    vol_handles = [
        Line2D([], [], marker="o", ls="none", color="#1f77b4", label=T("prompt_only (4 B/token)")),
        Line2D([], [], marker="o", ls="none", mfc="#d62728", mec="#d62728",
               label=T("kv_cache (330 KB/token)")),
    ]
    leg = ax.legend(handles=vol_handles, loc="lower left", fontsize=8, framealpha=0.92,
                    title=T("Data volume"), title_fontsize=8)
    ax.add_artist(leg)
    wl_handles = [
        Line2D([], [], marker="o", ls="none", color="0.35", label=T("data_heavy")),
        Line2D([], [], marker="^", ls="none", color="0.35", label=T("mixed")),
    ]
    ax.legend(handles=wl_handles, loc="upper right", fontsize=8, framealpha=0.92,
              title=T("Workload"), title_fontsize=8)
    return save_fig(fig, "fig_benefit_vs_netshare", fig_dir)


# --------------------------------------------------------------------------
# F2：对齐度分组柱状图
# --------------------------------------------------------------------------
def fig_alignment_bars(fig_dir: Path) -> list[Path]:
    """四个 (负载 × 网络) 组合 × 三个对齐档的分组柱。

    误差棒 = ``dp95_se`` × t(0.975, n_seeds-1)：CSV 里的 se 是**跨种子**的标准误，
    统计单位是"独立的负载实现"（10 个时间片/实现），不是请求。
    """
    print("[F2] fig_alignment_bars")
    d = read_csv(ROOT / "results" / "analysis_v2" / "rq17_alignment_effect.csv")
    if d is None:
        return []
    missing = [c for c in ("align", "workload", "network", "dp95_pct", "dp95_se", "n_seeds")
               if c not in d.columns]
    if missing:
        print(f"  [错] 缺列 {missing}，实际列名：{list(d.columns)}")
        return []

    combos = [(wl, nw) for wl in ("data_heavy", "mixed")
              for nw in ("degraded", "unstable")]
    align_order = [("anti", "Anti-aligned", "#c44e52"),
                   ("rot1", "Rotated", "#dd8452"),
                   ("aligned", "Aligned", "#55a868")]
    x = np.arange(len(combos), dtype=float)
    width = 0.26

    fig, ax = plt.subplots(figsize=(7.2, 4.4), constrained_layout=True)
    for k, (align, label, color) in enumerate(align_order):
        vals, errs, xs = [], [], []
        for j, (wl, nw) in enumerate(combos):
            row = d[(d["align"] == align) & (d["workload"] == wl) & (d["network"] == nw)]
            if row.empty:
                continue
            tc = t_crit(row["n_seeds"].iloc[0])
            xs.append(x[j] + (k - 1) * width)
            vals.append(float(row["dp95_pct"].iloc[0]))
            errs.append(float(row["dp95_se"].iloc[0]) * tc
                        if np.isfinite(tc) else np.nan)
        if not xs:
            continue
        ax.bar(xs, vals, width=width * 0.92, color=color, label=T(label), alpha=0.92)
        ax.errorbar(xs, vals, yerr=errs, fmt="none", ecolor="0.15",
                    elinewidth=0.9, capsize=2.5)
    ax.axhline(0.0, color="0.25", lw=0.9)
    ax.set_xticks(x)
    ax.set_xticklabels([T(f"{wl}\n{nw}") for wl, nw in combos], fontsize=8.5)
    ax.set_ylabel(T("Δp95 vs. LeastLoad (%)"))
    ax.set_title(T("Benefit is governed by compute–bandwidth alignment"), fontsize=10.5)
    ax.grid(True, axis="y", alpha=0.28, linewidth=0.6)
    ax.set_axisbelow(True)
    ax.legend(loc="lower left", fontsize=8, framealpha=0.92)
    return save_fig(fig, "fig_alignment_bars", fig_dir)


# --------------------------------------------------------------------------
# F3：对齐度 × 规模
# --------------------------------------------------------------------------
def fig_alignment_vs_scale(fig_dir: Path) -> list[Path]:
    """三条折线（对齐档）× 三档规模，并在每个点旁标出该 (align, n_nodes) 的排名相关 ρ。

    ρ 从 ``link_choice_by_scale.csv`` 按 (align, n_nodes) 取第一条——它只随
    (align, n_nodes) 变化，与 seed / workload 无关（已核对 nunique == 1）。
    """
    print("[F3] fig_alignment_vs_scale")
    d = read_csv(ROOT / "results" / "analysis_v2" / "rq18_alignment_scale_effect.csv")
    if d is None:
        return []
    missing = [c for c in ("n_nodes", "align", "dp95_pct", "dp95_se", "n_seeds")
               if c not in d.columns]
    if missing:
        print(f"  [错] 缺列 {missing}，实际列名：{list(d.columns)}")
        return []

    rc_map: dict[tuple[str, int], float] = {}
    lc = read_csv(ROOT / "results" / "analysis_v2" / "link_choice_by_scale.csv")
    if lc is not None and {"align", "n_nodes", "rank_corr"} <= set(lc.columns):
        for (align, n_nodes), sub in lc.groupby(["align", "n_nodes"]):
            rc_map[(str(align), int(n_nodes))] = float(sub["rank_corr"].iloc[0])
    else:
        print("  [告警] 取不到 rank_corr，F3 里的 ρ 标注将省略")

    nodes = sorted(int(v) for v in d["n_nodes"].unique())
    align_order = [("anti", "Anti-aligned", "#c44e52", -13),
                   ("rot1", "Rotated", "#dd8452", 7),
                   ("aligned", "Aligned", "#55a868", 8)]

    fig, ax = plt.subplots(figsize=(6.6, 4.4), constrained_layout=True)
    for align, label, color, dy in align_order:
        s = d[d["align"] == align].sort_values("n_nodes")
        if s.empty:
            continue
        tc = t_crit(s["n_seeds"].iloc[0])
        err = s["dp95_se"] * tc if np.isfinite(tc) else None
        ax.errorbar(s["n_nodes"], s["dp95_pct"], yerr=err, marker="o", ms=6,
                    ls="-", lw=1.2, color=color, capsize=3, elinewidth=0.9,
                    label=T(label))
        for _, row in s.iterrows():
            key = (align, int(row["n_nodes"]))
            if key not in rc_map:
                continue
            # 最右一列的文字放到点的**左侧**，否则会顶出坐标框（或被图例盖住）
            last = int(row["n_nodes"]) == nodes[-1]
            ax.annotate(T(f"ρ={rc_map[key]:+.2f}"),
                        xy=(row["n_nodes"], row["dp95_pct"]),
                        xytext=(-7 if last else 6, dy), textcoords="offset points",
                        fontsize=7.5, color=color,
                        ha="right" if last else "left")
    ax.axhline(0.0, color="0.3", ls="--", lw=0.9)
    ax.set_xticks(nodes)
    ax.set_xticklabels([T(str(n)) for n in nodes])
    ax.set_xlabel(T("Cluster size (nodes)"))
    ax.set_ylabel(T("Δp95 vs. LeastLoad (%)"))
    ax.set_title(T("Anti-aligned penalty persists across cluster size"), fontsize=10.5)
    ax.grid(True, alpha=0.28, linewidth=0.6)
    ax.set_axisbelow(True)
    ax.legend(loc="upper right", fontsize=8, framealpha=0.92,
              title=T("Rank correlation of compute vs. bandwidth"), title_fontsize=8,
              ncols=1)
    return save_fig(fig, "fig_alignment_vs_scale", fig_dir)


# --------------------------------------------------------------------------
# F4：两条通道陈旧度的二维网格
# --------------------------------------------------------------------------
def fig_staleness_2d(fig_dir: Path) -> list[Path]:
    """两个负载各一张热力图：行=排队消息年龄，列=网线消息年龄，格值=Δp95%。

    发散色标以 0 为中心（负=更好=偏绿），因为"收益变号"正是这张图要说的事。
    """
    print("[F4] fig_staleness_2d")
    d = read_csv(ROOT / "results" / "analysis_v2" / "rq16_staleness2d_effect.csv")
    if d is None:
        return []
    missing = [c for c in ("staleness_queue_s", "staleness_network_s", "workload", "dp95_pct")
               if c not in d.columns]
    if missing:
        print(f"  [错] 缺列 {missing}，实际列名：{list(d.columns)}")
        return []

    loads = ["data_heavy", "mixed"]
    qs = sorted(d["staleness_queue_s"].unique())
    ns = sorted(d["staleness_network_s"].unique())

    fig, axes = plt.subplots(1, 2, figsize=(9.4, 3.9), constrained_layout=True)
    im = None
    for ax, wl in zip(axes, loads):
        sub = d[d["workload"] == wl]
        grid = np.full((len(qs), len(ns)), np.nan)
        for i, q in enumerate(qs):
            for j, n in enumerate(ns):
                row = sub[(sub["staleness_queue_s"] == q)
                          & (sub["staleness_network_s"] == n)]
                if not row.empty:
                    grid[i, j] = float(row["dp95_pct"].iloc[0])
        im = ax.imshow(grid, cmap="RdYlGn_r", vmin=-30.0, vmax=10.0,
                       aspect="auto", origin="upper")
        for i in range(len(qs)):
            for j in range(len(ns)):
                v = grid[i, j]
                if not np.isfinite(v):
                    continue
                ax.text(j, i, T(f"{v:.1f}"), ha="center", va="center", fontsize=8,
                        color="0.1")
        ax.set_xticks(range(len(ns)))
        ax.set_xticklabels([T(f"{x:g}") for x in ns])
        ax.set_yticks(range(len(qs)))
        ax.set_yticklabels([T(f"{x:g}") for x in qs])
        ax.set_xlabel(T("Link-state age (s)"))
        ax.set_ylabel(T("Queue-state age (s)"))
        ax.set_title(T(wl), fontsize=9.5)
    fig.suptitle(T("Two channels expire at different rates"), fontsize=10.5)
    if im is not None:
        fig.colorbar(im, ax=list(axes), location="left", pad=0.02, aspect=28,
                     label=T("Δp95 vs. LeastLoad (%)"))
    return save_fig(fig, "fig_staleness_2d", fig_dir)


# --------------------------------------------------------------------------
# F5：三条通道 vs 统一降权
# --------------------------------------------------------------------------
F5_CONFIGS = [(0, 0), (1, 0), (5, 0), (0, 15), (0, 60), (5, 60)]
F5_ARMS = [(LEAST_LOAD, "#8c8c8c"),
           (NA2, "#4c72b0"),
           ("AdaptChannel", "#55a868"),
           ("AdaptSplit", "#dd8452"),
           ("AdaptLinear", "#8172b3")]


def fig_discount_rules(fig_dir: Path) -> list[Path]:
    """三批各一个子图：6 个陈旧配置 × 5 条 arm 的 Δp95（只取 degraded + mixed）。

    x 刻度写 ``q0/n0`` 这种英文缩写（q = 排队消息年龄，n = 网线消息年龄）。
    某批缺某个 arm 时跳过那根柱（不报错），LeastLoad 对自己恒为 0，作为参考画上。
    """
    print("[F5] fig_discount_rules")
    panels: dict[str, pd.DataFrame] = {}
    for name in BATCH_ORDER:
        d = read_csv(BATCH_DIRS[name] / "rq19_decay_rule_effect.csv")
        if d is None:
            continue
        need = {"workload", "network", "staleness_queue_s", "staleness_network_s",
                "arm", "dp95_vs_leastload_pct"}
        if not need <= set(d.columns):
            print(f"  [错] {name} 缺列 {sorted(need - set(d.columns))}，"
                  f"实际列名：{list(d.columns)}")
            continue
        sub = d[(d["network"] == "degraded") & (d["workload"] == "mixed")]
        if not sub.empty:
            panels[name] = sub
    if not panels:
        print("  [错] 三批都没有可用数据")
        return []

    x = np.arange(len(F5_CONFIGS), dtype=float)
    width = 0.16
    fig, axes = plt.subplots(1, len(panels), figsize=(4.6 * len(panels), 4.3),
                             constrained_layout=True, sharey=True, squeeze=False)
    axes = axes[0]
    for ax, (name, sub) in zip(axes, panels.items()):
        for k, (arm, color) in enumerate(F5_ARMS):
            xs, ys = [], []
            for j, (q, n) in enumerate(F5_CONFIGS):
                row = sub[(sub["arm"] == arm)
                          & (sub["staleness_queue_s"] == q)
                          & (sub["staleness_network_s"] == n)]
                if row.empty:
                    continue
                xs.append(x[j] + (k - (len(F5_ARMS) - 1) / 2) * width)
                ys.append(float(row["dp95_vs_leastload_pct"].iloc[0]))
            if not xs:
                print(f"  [缺] {name} 批没有 arm={arm}，跳过该柱")
                continue
            ax.bar(xs, ys, width=width * 0.9, color=color, alpha=0.92)
        ax.axhline(0.0, color="0.25", lw=0.9)
        ax.set_xticks(x)
        ax.set_xticklabels([T(f"q{q:g}/n{n:g}") for q, n in F5_CONFIGS], fontsize=8)
        ax.set_xlabel(T("Queue-state age / link-state age (s)"))
        ax.set_title(T(BATCH_PANEL_TITLE.get(name, name)), fontsize=9.5)
        ax.grid(True, axis="y", alpha=0.28, linewidth=0.6)
        ax.set_axisbelow(True)
    axes[0].set_ylabel(T("Δp95 vs. LeastLoad (%)"))
    fig.suptitle(T("Per-channel discounting preserves the benefit that uniform "
                   "discounting destroys"), fontsize=10.5)
    handles = [Patch(facecolor=c, label=T(a)) for a, c in F5_ARMS]
    fig.legend(handles=handles, loc="lower center", ncols=len(F5_ARMS), fontsize=8,
               framealpha=0.92, bbox_to_anchor=(0.5, -0.06))
    return save_fig(fig, "fig_discount_rules", fig_dir)


# --------------------------------------------------------------------------
# F6：阈值消融
# --------------------------------------------------------------------------
F6_ORDER = ["primary", "Tq2", "Tq8", "Tn20", "Tn120", "floor0.5", "floor0.9"]


def fig_ablation(fig_dir: Path) -> list[Path]:
    """三批各一个子图：各变体相对 primary 的 Δp95（横向条形 + CI 误差棒 + 翻转计数）。

    CSV 里没有 ``primary`` 行（primary 相对自己恒为 0），这里补一行 0±0，
    翻转数记 ``0/n``；变体顺序按任务给定的 primary/Tq2/Tq8/Tn20/Tn120/floor0.5/floor0.9 固定。
    """
    print("[F6] fig_ablation")
    panels: dict[str, pd.DataFrame] = {}
    for name in BATCH_ORDER:
        d = read_csv(BATCH_DIRS[name] / "rq21_channel_threshold_ablation.csv")
        if d is None:
            continue
        need = {"variant", "vs_primary_mean_pct", "vs_primary_ci",
                "n_conditions_sign_flip_vs_na2", "n_conditions"}
        if not need <= set(d.columns):
            print(f"  [错] {name} 缺列 {sorted(need - set(d.columns))}，"
                  f"实际列名：{list(d.columns)}")
            continue
        d = d.set_index("variant")
        n_cond = int(d["n_conditions"].iloc[0]) if not d.empty else 0
        if "primary" not in d.index:
            d.loc["primary"] = {"vs_primary_mean_pct": 0.0, "vs_primary_ci": 0.0,
                                "n_conditions_sign_flip_vs_na2": 0,
                                "n_conditions": n_cond}
        panels[name] = d
    if not panels:
        print("  [错] 三批都没有可用数据")
        return []

    variants = [v for v in F6_ORDER
                if any(v in d.index for d in panels.values())]
    y = np.arange(len(variants), dtype=float)

    # 三张子图共用同一个 x 范围（否则一个面板里的 6% 和另一个面板里的 6% 不一样长，
    # 读者会看错）。极值必须**把所有批一起取、并且把误差棒两端算进来**：
    # BurstGPT 的 Tq2 是 1.94 ± 3.39，只按点值定范围会把它的 "1/12" 标注挤出坐标框。
    extremes: list[float] = []
    for d in panels.values():
        for v in variants:
            if v not in d.index:
                continue
            val = float(d.loc[v, "vs_primary_mean_pct"])
            ci = float(d.loc[v, "vs_primary_ci"])
            if not np.isfinite(val):
                continue
            extremes.append(abs(val))
            if np.isfinite(ci):
                extremes.append(abs(val + ci))
                extremes.append(abs(val - ci))
    span = max(extremes + [1e-6])
    xlo, xhi = -span * 1.12, span * 1.12

    fig, axes = plt.subplots(1, len(panels), figsize=(4.4 * len(panels), 4.2),
                             constrained_layout=True, sharey=True, squeeze=False)
    axes = axes[0]
    for ax, (name, d) in zip(axes, panels.items()):
        vals, errs, labels_pos = [], [], []
        for yi, v in enumerate(variants):
            if v not in d.index:
                vals.append(np.nan)
                errs.append(0.0)
                labels_pos.append((np.nan, v))
                continue
            row = d.loc[v]
            val = float(row["vs_primary_mean_pct"])
            ci = float(row["vs_primary_ci"])
            vals.append(val)
            errs.append(ci)
            labels_pos.append((val + ci, v))
        ax.barh(y, vals, xerr=errs, height=0.62, color="#4c72b0", alpha=0.92,
                error_kw=dict(ecolor="0.15", elinewidth=0.9, capsize=2.5))
        ax.axvline(0.0, color="0.25", ls="--", lw=0.9)
        for yi, (end, v) in enumerate(labels_pos):
            if v not in d.index or not np.isfinite(end):
                continue
            flips = int(d.loc[v, "n_conditions_sign_flip_vs_na2"])
            n_cond = int(d.loc[v, "n_conditions"])
            ax.text(end + 0.045 * span, yi, T(f"{flips}/{n_cond}"),
                    va="center", ha="left", fontsize=7.5, color="0.15")
        ax.set_yticks(y)
        ax.set_yticklabels([T(v) for v in variants], fontsize=8.5)
        ax.set_xlim(xlo, xhi)
        ax.set_xlabel(T("Δp95 vs. primary (%)"))
        ax.set_title(T(BATCH_PANEL_TITLE.get(name, name)), fontsize=9.5)
        ax.grid(True, axis="x", alpha=0.28, linewidth=0.6)
        ax.set_axisbelow(True)
    # **每张**子图都要翻 y 轴：只翻第一张会让另外两张的 primary 跑到最下面，
    # 三张图的同一行就不是同一个变体了（第一版就踩到）。
    for ax in axes:
        ax.invert_yaxis()
    fig.suptitle(T("Conclusion is insensitive to the fitted time constants"), fontsize=10.5)
    return save_fig(fig, "fig_ablation", fig_dir)


# --------------------------------------------------------------------------
# F7：真实集群（k3s 单节点）实测
# --------------------------------------------------------------------------
K8S_RE_TMPL = (r"^k3s_(?P<tag>[rq][1-3])_(?P<policy>least_load|contention)"
               r"_lag(?P<lag>[0-9]+)_xfer(?P<xfer>[0-9.]+)_q(?P<qps>[0-9]+)\.csv$")


def _k8s_p95_table() -> pd.DataFrame:
    """把 ``results/k8s_test/k3s_*.csv`` 逐请求明细汇总成 (tag, policy, qps, lag, p95)。"""
    import re                                             # noqa: PLC0415
    pattern = re.compile(K8S_RE_TMPL)
    rows: list[dict] = []
    for path in sorted(K8S_DIR.glob("k3s_*.csv")):
        m = pattern.match(path.name)
        if not m:
            continue
        if float(m.group("xfer")) != 8.0:                 # 只取 8 MB 传输档（其余档位另有图）
            continue
        try:
            d = pd.read_csv(path, usecols=lambda c: c in ("ok", "latency_s"))
        except Exception as exc:                          # noqa: BLE001
            print(f"  [错] 读 {path.name} 失败：{exc}")
            continue
        if not {"ok", "latency_s"} <= set(d.columns):
            print(f"  [错] {path.name} 缺 ok/latency_s，实际列名：{list(d.columns)}")
            continue
        good = d[(d["ok"] == 1) & d["latency_s"].notna()]
        if good.empty:
            print(f"  [告警] {path.name} 没有 ok==1 的行，跳过")
            continue
        rows.append({
            "tag": m.group("tag"),
            "policy": m.group("policy"),
            "qps": int(m.group("qps")),
            "lag": int(m.group("lag")),
            "n_ok": int(len(good)),
            "p95": float(np.quantile(good["latency_s"].to_numpy(dtype=float), 0.95)),
        })
    return pd.DataFrame(rows)


def fig_real_cluster(fig_dir: Path) -> list[Path]:
    """每请求明细 → p95，再按 (qps, lag) 分组画 6 根柱（3 次 r 批 + 3 次 q 批）。

    ``r*`` 批的机器上**同时跑着一个仿真进程**（有并发干扰），``q*`` 批是安静机器。
    柱高用 ``contention``（看链路积压的那条策略）；``LeastLoad`` 的 p95 用黑色短横
    叠在柱上，好让"两根柱之间差多少"一眼可见。组上方标注按同一 tag 配对的
    ``Δp95% = (contention − least_load) / least_load``（r 批与 q 批分别取均值）。
    """
    print("[F7] fig_real_cluster")
    tab = _k8s_p95_table()
    if tab.empty:
        print("  [错] 没解析出任何 k3s 运行结果")
        return []

    cfgs = sorted({(int(r.qps), int(r.lag)) for r in tab.itertuples()})
    tags = [f"r{i}" for i in (1, 2, 3)] + [f"q{i}" for i in (1, 2, 3)]
    tags = [t for t in tags if t in set(tab["tag"])]
    x = np.arange(len(cfgs), dtype=float)
    width = 0.9 / max(len(tags), 1)

    fig, ax = plt.subplots(figsize=(7.8, 4.6), constrained_layout=True)
    for k, tag in enumerate(tags):
        xs, ys = [], []
        for j, (qps, lag) in enumerate(cfgs):
            row = tab[(tab["tag"] == tag) & (tab["policy"] == "contention")
                      & (tab["qps"] == qps) & (tab["lag"] == lag)]
            if row.empty:
                continue
            xs.append(x[j] + (k - (len(tags) - 1) / 2) * width)
            ys.append(float(row["p95"].iloc[0]))
        if not xs:
            continue
        color = "#4c72b0" if tag.startswith("r") else "#dd8452"
        ax.bar(xs, ys, width=width * 0.88, color=color, alpha=0.92)
    # LeastLoad 的 p95 用黑色短横叠在柱上（同一 tag、同一 (qps, lag)）
    for tag in tags:
        for j, (qps, lag) in enumerate(cfgs):
            row = tab[(tab["tag"] == tag) & (tab["policy"] == "least_load")
                      & (tab["qps"] == qps) & (tab["lag"] == lag)]
            if row.empty:
                continue
            xi = x[j] + (tags.index(tag) - (len(tags) - 1) / 2) * width
            ax.plot([xi - width * 0.42, xi + width * 0.42],
                    [float(row["p95"].iloc[0])] * 2,
                    color="0.1", lw=1.6, solid_capstyle="butt", zorder=5)

    # 组上方的 Δp95% 标注（按同一 tag 配对求差）
    ymax = 0.0
    for j, (qps, lag) in enumerate(cfgs):
        for tag in tags:
            for pol in ("contention", "least_load"):
                row = tab[(tab["tag"] == tag) & (tab["policy"] == pol)
                          & (tab["qps"] == qps) & (tab["lag"] == lag)]
                if not row.empty:
                    ymax = max(ymax, float(row["p95"].iloc[0]))
    ymax = ymax if ymax > 0 else 1.0
    for j, (qps, lag) in enumerate(cfgs):
        notes = []
        for prefix, label in (("r", T("with-load")), ("q", T("quiet"))):
            deltas = []
            for tag in [t for t in tags if t.startswith(prefix)]:
                c = tab[(tab["tag"] == tag) & (tab["policy"] == "contention")
                        & (tab["qps"] == qps) & (tab["lag"] == lag)]
                l = tab[(tab["tag"] == tag) & (tab["policy"] == "least_load")
                        & (tab["qps"] == qps) & (tab["lag"] == lag)]
                if c.empty or l.empty or float(l["p95"].iloc[0]) <= 0:
                    continue
                deltas.append((float(c["p95"].iloc[0]) - float(l["p95"].iloc[0]))
                              / float(l["p95"].iloc[0]) * 100.0)
            if deltas:
                notes.append(T(f"Δ {label}: {np.mean(deltas):+.1f}%"))
        if notes:
            ax.text(x[j], ymax * 1.045, "\n".join(notes), ha="center", va="bottom",
                    fontsize=7.5)

    ax.set_xticks(x)
    ax.set_xticklabels([T(f"({qps} QPS, {lag:g} s)") for qps, lag in cfgs], fontsize=8.5)
    ax.set_xlabel(T("(offered load, state lag)"))
    ax.set_ylabel(T("p95 latency (s)"))
    ax.set_ylim(0, ymax * 1.30)
    ax.set_title(T("Real cluster: only the fresh, moderate-load cell is stable"),
                 fontsize=10.5)
    ax.grid(True, axis="y", alpha=0.28, linewidth=0.6)
    ax.set_axisbelow(True)
    handles = [
        Patch(facecolor="#4c72b0", label=T("With concurrent load (r1–r3)")),
        Patch(facecolor="#dd8452", label=T("Quiet machine (q1–q3)")),
        Line2D([], [], color="0.1", lw=1.6, label=T("LeastLoad p95")),
    ]
    # 图例放到坐标框**下方**：Δ 标注就压在每组上方，放框内会互相盖住（第一版踩到）。
    ax.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, -0.12),
              ncols=3, fontsize=8, frameon=False)
    return save_fig(fig, "fig_real_cluster", fig_dir)


# --------------------------------------------------------------------------
# F8：真实云锚点（B2 + C；C 侧只作描述性背景）
# --------------------------------------------------------------------------
def fig_cloud_ttft(fig_dir: Path) -> list[Path]:
    """绘制真实云锚点与 C 阶段 KV 背景关系。

    C 阶段只有两条方向和五条事后分组提示词，面板 (b) 只能作背景观察；
    参考线锚定首个观测点，不把它包装成回归证明。
    """
    print("[F8] fig_cloud_ttft")
    vals = paper_number_lines()
    required = [
        "B2.descriptive.base_c1", "B2.descriptive.disagg_c1",
        "B2.descriptive.base_c4", "B2.descriptive.disagg_c4",
        "C.D1.serial", "C.D1.conc4", "C.D2.serial", "C.D2.conc4",
    ]
    if any(k not in vals for k in required):
        print("  [错] paper_numbers.txt 缺少真实云图表标签")
        return []

    import ast

    def rec(key: str) -> dict[str, float]:
        raw = ast.literal_eval(vals[key])
        return {str(k): float(v) for k, v in raw.items()}

    # 面板 (a)：p50 TTFT，按两档并列显示四种部署形态。
    serial = [rec("B2.descriptive.base_c1")["ttft_p50"],
              rec("B2.descriptive.disagg_c1")["ttft_p50"],
              rec("C.D1.serial")["ttft_p50_s"],
              rec("C.D2.serial")["ttft_p50_s"]]
    conc4 = [rec("B2.descriptive.base_c4")["ttft_p50"],
             rec("B2.descriptive.disagg_c4")["ttft_p50"],
             rec("C.D1.conc4")["ttft_p50_s"],
             rec("C.D2.conc4")["ttft_p50_s"]]
    labels = [T("Base"), T("Same-cloud\ndisagg."), T("D1\nAlibaba→Tencent"),
              T("D2\nTencent→Alibaba")]
    x = np.arange(len(labels), dtype=float)
    width = 0.36
    fig, (ax0, ax1) = plt.subplots(1, 2, figsize=(10.4, 4.2),
                                   gridspec_kw={"width_ratios": [1.0, 1.12]},
                                   constrained_layout=True)
    ax0.bar(x - width / 2, serial, width, label=T("Serial"), color="#4c72b0")
    ax0.bar(x + width / 2, conc4, width, label=T("Concurrency 4"), color="#dd8452")
    ax0.set_xticks(x)
    ax0.set_xticklabels(labels, fontsize=7.6)
    ax0.set_ylabel(T("TTFT p50 (s)"))
    ax0.set_title(T("Real-cloud deployment anchor"), fontsize=10)
    ax0.grid(True, axis="y", alpha=0.28, linewidth=0.6)
    ax0.set_axisbelow(True)
    ax0.legend(frameon=False, fontsize=8)

    # 面板 (b)：C 阶段按题长分组的背景点。
    bg = {"D1": [], "D2": []}
    for dtag in ("D1", "D2"):
        for pid in ("q01_rep6", "q02_rep10", "q03_rep14", "q04_rep18", "q05_rep22"):
            raw = vals.get(f"C.bg.{dtag}.{pid}")
            if not raw:
                continue
            fields = dict(part.split("=", 1) for part in raw.split())
            bg[dtag].append((float(fields["est_KV_MB"]), float(fields["ttft_p50"])))
    colors = {"D1": "#1f77b4", "D2": "#d62728"}
    bandwidth = {"D1": 8.6, "D2": 11.7}
    for dtag, points in bg.items():
        if not points:
            continue
        points = sorted(points)
        xx = np.array([p[0] for p in points])
        yy = np.array([p[1] for p in points])
        ax1.scatter(xx, yy, color=colors[dtag], s=34, label=T(f"{dtag} observed"))
        # 仅作工程参考：以首个点为锚，不拟合斜率，也不作统计检验。
        ref = yy[0] + (xx - xx[0]) / bandwidth[dtag]
        ax1.plot(xx, ref, color=colors[dtag], ls="--", lw=1.0,
                 label=T(f"{dtag}: 1/bandwidth reference"))
    ax1.set_xlabel(T("Estimated KV volume (MB)"))
    ax1.set_ylabel(T("TTFT p50 (s)"))
    ax1.set_title(T("Descriptive background; not a linearity test"), fontsize=10)
    ax1.grid(True, alpha=0.28, linewidth=0.6)
    ax1.set_axisbelow(True)
    ax1.legend(frameon=False, fontsize=7.2, loc="upper left")
    return save_fig(fig, "fig_cloudttft", fig_dir)


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------
FIGURES = {
    "F1": ("fig_benefit_vs_netshare.pdf", fig_benefit_vs_netshare),
    "F2": ("fig_alignment_bars.pdf", fig_alignment_bars),
    "F3": ("fig_alignment_vs_scale.pdf", fig_alignment_vs_scale),
    "F4": ("fig_staleness_2d.pdf", fig_staleness_2d),
    "F5": ("fig_discount_rules.pdf", fig_discount_rules),
    "F6": ("fig_ablation.pdf", fig_ablation),
    "F7": ("fig_real_cluster.pdf", fig_real_cluster),
    "F8": ("fig_cloudttft.pdf", fig_cloud_ttft),
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="从已有分析产物生成英文标签的论文图（F1–F8）。")
    ap.add_argument("--fig-dir", default=str(DEFAULT_FIG_DIR),
                    help=f"输出目录（默认 {DEFAULT_FIG_DIR}）")
    ap.add_argument("--only", default="",
                    help="只画其中几张，逗号分隔的编号，例如 F1,F4,F7")
    args = ap.parse_args(argv)

    fig_dir = Path(args.fig_dir).expanduser().resolve()
    wanted = [k.strip().upper() for k in args.only.split(",") if k.strip()] or list(FIGURES)
    unknown = [k for k in wanted if k not in FIGURES]
    if unknown:
        print(f"[错] 不认识 --only 里的编号：{unknown}（可选 {list(FIGURES)}）")
        return 2

    print(f"输出目录：{fig_dir}")
    made: list[Path] = []
    for key in FIGURES:
        if key not in wanted:
            continue
        _, fn = FIGURES[key]
        try:
            made.extend(fn(fig_dir))
        except Exception as exc:                          # noqa: BLE001
            print(f"[错] {key} 作图失败：{type(exc).__name__}: {exc}")

    # 收尾自检：登记表里不许有 CJK（图内逐张已在 save_fig 里查过）
    bad = [s for s in TEXTS if has_cjk(s)]
    if bad:
        print(f"[错] 登记表里出现 CJK 字符：{bad[:10]}")
        return 1

    print()
    print(f"完成：{len(made)} 个文件（{len(made) // 2} 张图 × pdf+png），"
          f"共登记 {len(TEXTS)} 个图内文字串，全部无 CJK ✓")
    for p in made:
        print(f"  {rel(p):58s} {p.stat().st_size:>9d} B")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

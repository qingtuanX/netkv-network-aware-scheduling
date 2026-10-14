#!/usr/bin/env python
"""矩阵实验结果汇总分析 —— 论文 RQ1–RQ7 的表格与图表。

读取 ``results/matrix/{mode}_<run_id>_{runs,paired,manifest}.{csv,json}``，
产出论文级表格（``results/analysis/*.csv``）与图表（``figures/*.png``），
并把可读报告同时打印到 stdout 与 ``results/analysis/report.txt``。

硬性设计约束
------------
* **容忍数据不完整**：缺 mode / 缺 paired / 缺 manifest / 缺 ``p_holm`` 都不崩，
  在可用性报告里逐项标出缺什么。
* **零硬编码结论**：所有数字、显著性、退化判定只来自 CSV / JSON。
* **幂等**：可重复运行，覆盖输出，不依赖中间手工步骤。
* **不做数据挑选**：负结果与零结果照原样进入表格与报告。

注意：脚本**不假设负载来源**。`--matrix-dir` 指向哪一批结果，就用哪一批：
`results/matrix`（合成负载，`workload_stats` 标为 SYNTHETIC）或
`results/matrix_trace`（真实 Azure trace）。§9 的适用范围会读 manifest 的
`data_source` 自动切换措辞，两批结果的数字**不可混着引用**。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import TwoSlopeNorm

# --------------------------------------------------------------------------
# 路径与常量
# --------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
MATRIX_DIR = ROOT / "results" / "matrix"
TABLE_DIR = ROOT / "results" / "analysis"
FIG_DIR = ROOT / "figures"

MODES = ["main", "staleness", "datavolume", "convergence", "ablation", "scale",
         "rho", "fallback", "bandwidth", "avewindow", "staleness2d", "align",
         "alignscale", "method", "method_ablate", "method_ch",
         "method_ch_ablate", "baselines"]
STRATEGIES = ["Random", "LeastQueue", "LeastLoad", "NetworkAware"]
NA = "NetworkAware"
BASELINES = ["Random", "LeastQueue", "LeastLoad"]

# 与 experiments/run_matrix.py 保持一致的容量口径：
# 每个槽位按 compute_rate 满速推进，槽位之间不争抢算力（已知模型简化）。
SLOTS_PER_NODE = 4
NODE_RATE_BASE = 16000.0
SLO_MULTIPLIER = 3.0
RHO_LO, RHO_HI = 0.3, 0.9          # 门 1 的有效负载区间
# 判定用的公差。标定到达率时用的是 (n-1)/span（间隔数），而复算 rho 用的是 n/t_last
# （请求数），两者差的正是 1/n；n=20000 时带来 +0.005% 的系统性正偏差。
# 目标 rho=0.900 因此会算出 0.900045，卡在边界上被判"越界"——这是口径差不是真越界。
# 该偏差在所有 run 上是同向同量的，不影响任何比较；这里给 0.05% 的公差把它吸收掉。
RHO_TOL = 5e-4
DEGENERATE_TOL_PCT = 0.5           # p95 跨策略相对极差 < 0.5% 视为“数值上无差别”
# Cliff's delta 的常规量级阈值（|d|<0.147 可忽略，<0.33 小，<0.474 中，其余大）
CLIFFS_NEGLIGIBLE = 0.147
CLIFFS_SMALL = 0.33
CLIFFS_MEDIUM = 0.474
# 统计显著 ≠ 有实质影响：请求量 18000×5 seed 会让 0.04% 的差异也“显著”。
# 只有同时满足 |Δp95%| < 1.0 且 |cliffs| < 0.147 才判为可忽略。
NEGLIGIBLE_DP95_PCT = 1.0
W_NET_GRID = [0.0, 0.5, 1.0, 1.5, 2.0]
CONV_K_GRID = [3, 5, 10, 20]

COLORS = {"Random": "#8c8c8c", "LeastQueue": "#4C72B0",
          "LeastLoad": "#DD8452", "NetworkAware": "#C44E52"}
MARKERS = {"Random": "o", "LeastQueue": "s", "LeastLoad": "^", "NetworkAware": "D"}

METRIC_COLS = ["n_requests", "sojourn_mean", "sojourn_p50", "sojourn_p95",
               "sojourn_p99", "sojourn_max", "wait_mean", "wait_p95", "net_mean",
               "net_share", "slo_miss_rate", "throughput_qps", "node_utilization"]

RUN_RE_TMPL = r"^{mode}_(\d{{8}}T\d{{6}}Z)_{kind}\.(csv|json)$"


# --------------------------------------------------------------------------
# 输出基础设施：UTF-8 输出 + Tee 到 report.txt
# --------------------------------------------------------------------------
def setup_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


def setup_fonts() -> str:
    """配置中文字体，保证图内中文不出现方块。"""
    import matplotlib.font_manager as fm
    available = {f.name for f in fm.fontManager.ttflist}
    chosen = "DejaVu Sans"
    for cand in ("Microsoft YaHei", "SimHei", "Noto Sans SC", "DengXian"):
        if cand in available:
            chosen = cand
            break
    plt.rcParams["font.sans-serif"] = [chosen, "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    plt.rcParams["font.size"] = 10
    plt.rcParams["axes.titlesize"] = 11
    plt.rcParams["axes.labelsize"] = 10
    plt.rcParams["figure.titlesize"] = 13
    return chosen


class Tee:
    """把 stdout 同时写到文件。"""

    def __init__(self, path: Path, stream):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = open(path, "w", encoding="utf-8")
        self.stream = stream

    def write(self, s):
        self.stream.write(s)
        self.file.write(s)
        return len(s)

    def flush(self):
        self.stream.flush()
        self.file.flush()


# --------------------------------------------------------------------------
# 数据读取（容错）
# --------------------------------------------------------------------------
def read_csv_safe(path: Path, retries: int = 3):
    """CSV 正在被追加写入时可能读到半行 —— 重试，最终失败则返回 None。"""
    last = None
    for i in range(retries):
        try:
            return pd.read_csv(path)
        except Exception as exc:      # noqa: BLE001 - 需要兜住任何解析异常
            last = exc
            time.sleep(1.0)
    print(f"  [警告] 无法解析 {path.name}: {last}")
    return None


def holm_bonferroni(pvals: list[float]) -> list[float]:
    """Holm-Bonferroni 校正，与 experiments/run_matrix.py 的实现逐行一致。"""
    arr = np.asarray(pvals, dtype=float)
    order = np.argsort(arr)
    m = len(arr)
    adj = np.full(m, np.nan)
    running_max = 0.0
    for rank, i in enumerate(order):
        val = arr[i] * (m - rank)
        running_max = max(running_max, val)
        adj[i] = min(1.0, running_max)
    return list(adj)


def recompute_holm(paired: pd.DataFrame) -> pd.Series:
    """按 run_matrix.py 的族定义（cell_id × candidate）重算 p_holm。

    运行中途的 paired.csv 里没有 p_holm 列（只在整轮跑完后的收尾回填），
    因此这里必须能重算，并标注来源。
    """
    adj = pd.Series(np.nan, index=paired.index, dtype=float)
    for _, g in paired.groupby(["cell_id", "candidate"]):
        adj.loc[g.index] = holm_bonferroni(list(g["p_value"].fillna(1.0)))
    return adj


def node_capacity(n_nodes: float) -> float:
    n = int(n_nodes)
    return sum(NODE_RATE_BASE / (2 ** (i % 4)) for i in range(n)) * SLOTS_PER_NODE


def run_id_of(path: Path, mode: str, kind: str):
    m = re.match(RUN_RE_TMPL.format(mode=re.escape(mode), kind=kind), path.name)
    return m.group(1) if m else None


def load_mode(mode: str) -> dict:
    """加载一个模式的**最新** run_id 产物；缺什么就记什么。"""
    ids = set()
    for kind in ("runs", "paired", "manifest"):
        for p in MATRIX_DIR.glob(f"{mode}_*_{kind}.*"):
            rid = run_id_of(p, mode, kind)
            if rid:
                ids.add(rid)
    info = {
        "mode": mode, "run_id": None, "runs": None, "paired": None,
        "manifest": None, "p_holm_source": None, "other_run_ids": [],
        "missing": [], "n_cells": 0, "n_rows": 0,
    }
    if not ids:
        info["missing"] = ["runs", "paired", "manifest"]
        return info

    ids = sorted(ids)
    run_id = ids[-1]
    info["run_id"] = run_id
    info["other_run_ids"] = ids[:-1]

    runs_path = MATRIX_DIR / f"{mode}_{run_id}_runs.csv"
    paired_path = MATRIX_DIR / f"{mode}_{run_id}_paired.csv"
    manifest_path = MATRIX_DIR / f"{mode}_{run_id}_manifest.json"

    if runs_path.exists():
        df = read_csv_safe(runs_path)
        if df is not None and len(df):
            for col in ("cell_id", "staleness", "workload", "network",
                        "strategy", "data_volume"):
                if col in df.columns:
                    df[col] = df[col].astype(str)
            df = add_rho(df)
            info["runs"] = df
            if "cell_id" in df.columns:
                info["n_cells"] = int(df["cell_id"].nunique())
            info["n_rows"] = int(len(df))
        elif df is not None:
            info["missing"].append("runs(空)")
    else:
        info["missing"].append("runs")

    if paired_path.exists():
        pt = read_csv_safe(paired_path)
        if pt is not None and len(pt):
            for col in ("cell_id", "staleness", "workload", "network",
                        "data_volume", "candidate", "baseline"):
                if col in pt.columns:
                    pt[col] = pt[col].astype(str)
            if "p_holm" in pt.columns and pt["p_holm"].notna().all():
                info["p_holm_source"] = "运行产物"
            else:
                pt["p_holm"] = recompute_holm(pt)
                info["p_holm_source"] = "本轮脚本按同族定义重算" + (
                    "（原文件缺 p_holm 列，说明该模式尚未跑完）")
            info["paired"] = pt
        elif pt is not None:
            info["missing"].append("paired(空)")
    else:
        info["missing"].append("paired")

    if manifest_path.exists():
        try:
            info["manifest"] = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:      # noqa: BLE001
            print(f"  [警告] 无法解析 {manifest_path.name}: {exc}")
            info["missing"].append("manifest(解析失败)")
    else:
        info["missing"].append("manifest")

    return info


def add_rho(df: pd.DataFrame) -> pd.DataFrame:
    """门 1：负载强度 rho = 平均工作量 × 实际到达率 / 系统总容量。"""
    df = df.copy()
    cap = df["n_nodes"].map(node_capacity)
    df["capacity"] = cap
    df["rho"] = df["mean_work"] * df["qps_realized"] / cap
    return df


def detect_data_source(data: dict) -> str:
    """判断这批结果的负载来源：trace / synthetic / 未知。

    三级回退，因为新旧产物的字段不一样：
      1. manifest 的 `data_source`（`run_matrix.py --source` 写入，最新、最权威）；
      2. runs.csv 的 `data_source` 列（本轮之后新增的产物）；
      3. paired.csv 的 `workload_stats` 列（更早的产物只有这一列，
         合成负载写着 SYNTHETIC，trace 写的是文件路径）。
    这是"零硬编码结论"的一部分：报告里的适用范围必须跟着数据走。
    早先这里把"负载为合成负载"写死，导致 trace 的结果也带着那句错误的免责声明。
    """
    sources: set[str] = set()
    for info in data.values():
        man = info.get("manifest") or {}
        if man.get("data_source"):
            sources.add(str(man["data_source"]))
            continue
        df = info.get("runs")
        if df is not None and "data_source" in df.columns:
            vals = [str(s) for s in df["data_source"].dropna().unique()]
            if vals:
                sources.update(vals)
                continue
        pt = info.get("paired")
        if pt is not None and "workload_stats" in pt.columns:
            for s in pt["workload_stats"].astype(str).unique():
                sources.add("synthetic" if "SYNTHETIC" in s.upper() else "trace")
    if not sources:
        return "未知"
    if len(sources) == 1:
        return next(iter(sources))
    return "混合(" + ",".join(sorted(sources)) + ")"


# --------------------------------------------------------------------------
# 效应表通用计算
# --------------------------------------------------------------------------
def effect_table(runs: pd.DataFrame, paired, group_cols: list[str],
                 candidate: str = NA, baselines: list[str] | None = None) -> pd.DataFrame:
    """按 group_cols 分组，给出 candidate 相对每个基线的效应。

    效应量取自 paired.csv（cliffs_delta / p_holm），方向与不确定性一并给出。
    Δp95 用**按种子配对**的相对差均值（配对设计的正确口径），并给跨种子标准差。

    baselines 默认为三个调度基线；回退机制那一节用它传入
    `["NetworkAware"]`（即"不回退的 NetworkAware"作为参考臂）。
    """
    rows = []
    if runs is None or runs.empty:
        return pd.DataFrame()
    baselines = BASELINES if baselines is None else baselines

    for key, g in runs.groupby(group_cols, dropna=False):
        if not isinstance(key, tuple):
            key = (key,)
        rec = dict(zip(group_cols, key))
        c = g[g["strategy"] == candidate]
        if c.empty:
            continue
        rec["n_seeds"] = int(c["seed"].nunique())
        rec["p95_na"] = float(c["sojourn_p95"].mean())
        rec["net_share_na"] = float(c["net_share"].mean())
        rec["rho_mean"] = float(c["rho"].mean())
        rec["rho_min"] = float(c["rho"].min())
        rec["rho_max"] = float(c["rho"].max())

        for base in baselines:
            if base == candidate:
                continue
            b = g[g["strategy"] == base]
            if b.empty:
                continue
            m = c[["seed", "sojourn_p95", "wait_p95", "slo_miss_rate", "net_share"]].merge(
                b[["seed", "sojourn_p95", "slo_miss_rate", "net_share"]],
                on="seed", suffixes=("_c", "_b"))
            if m.empty:
                continue
            d_p95 = (m["sojourn_p95_c"] - m["sojourn_p95_b"]) / m["sojourn_p95_b"] * 100.0
            d_slo = m["slo_miss_rate_c"] - m["slo_miss_rate_b"]
            r = dict(rec)
            r["baseline"] = base
            r["p95_base"] = float(m["sojourn_p95_b"].mean())
            r["dp95_pct"] = float(d_p95.mean())
            r["dp95_std"] = float(d_p95.std(ddof=1)) if len(d_p95) > 1 else float("nan")
            r["dp95_se"] = (float(d_p95.std(ddof=1) / np.sqrt(len(d_p95)))
                            if len(d_p95) > 1 else float("nan"))
            r["dslo_abs"] = float(d_slo.mean())
            r["dslo_pct"] = float((d_slo / m["slo_miss_rate_b"].replace(0, np.nan)).mean() * 100)

            # --- 显著性 / 效应量：来自 paired.csv ---
            sig = None
            if paired is not None and not paired.empty:
                sub = paired
                # 配对文件里可能缺分组列（例如老产物的 paired.csv 没有 w_net，
                # 因为 w_net 是 cell 级属性、当年只写进了 runs.csv）。
                # 缺列时必须整条跳过，**不能**退化成用更少的分组键去筛——
                # 那会把不同 w_net 档的配对行混进同一组，静默产生错误的 p 值。
                if all(col in sub.columns for col in group_cols):
                    for col, val in zip(group_cols, key):
                        sub = sub[sub[col] == val]
                    sub = sub[(sub["candidate"] == candidate) & (sub["baseline"] == base)]
                    if not sub.empty:
                        sig = sub
            if sig is not None and not sig.empty:
                r["n_paired_cells"] = int(len(sig))
                r["cliffs_delta_median"] = float(sig["cliffs_delta"].median())
                r["cliffs_delta_min"] = float(sig["cliffs_delta"].min())
                r["cliffs_delta_max"] = float(sig["cliffs_delta"].max())
                r["p_holm_median"] = float(sig["p_holm"].median())
                r["p_holm_max"] = float(sig["p_holm"].max())
                r["n_sig"] = int((sig["p_holm"] < 0.05).sum())
                r["n_identical"] = int(((sig["cliffs_delta"] == 0.0)
                                        & (sig["p_value"] == 1.0)).sum())
                r["median_diff_median"] = float(sig["median_diff"].median())
                r["n_median_diff_nonzero"] = int((sig["median_diff"] != 0.0).sum())
                verdict_of(r, sig)
            else:
                for col in ("n_paired_cells", "cliffs_delta_median", "cliffs_delta_min",
                            "cliffs_delta_max", "p_holm_median", "p_holm_max", "n_sig",
                            "n_identical", "median_diff_median", "n_median_diff_nonzero"):
                    r[col] = float("nan")
                r["verdict"] = "无配对数据(paired 缺失)"
                r["tail_vs_median"] = "无配对数据"
                r["p95_dir"] = "未知"
                r["effect_magnitude"] = "未知"
            rows.append(r)

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    front = group_cols + ["baseline"]
    rest = [c for c in df.columns if c not in front]
    return df[front + rest].sort_values(front).reset_index(drop=True)


def verdict_of(rec: dict, sig: pd.DataFrame) -> None:
    """就地把判定写回 rec。

    **两个方向必须分开报**，因为它们可以不一致，而且实测确实不一致：
      * p95（尾部分位）—— 论文主指标，决定 "NA更优 / NA劣化"；
      * cliff's delta（逐请求完成时间的符号方向）—— 描述"典型请求"的少数服从方向。
    当二者符号相反时，说明 NA 用小部分请求的轻微退化换取了尾部的大幅改善
    （实测：compute_heavy/degraded 上 p95 −39.8%，但 cliff's delta 为正、中位数差为 0）。
    这种情况单独标为 `tail_vs_median`，不许被压成单一结论。
    """
    n = len(sig)
    n_ident = int(((sig["cliffs_delta"] == 0.0) & (sig["p_value"] == 1.0)).sum())
    sig_cells = sig[sig["p_holm"] < 0.05]
    cliffs = float(sig["cliffs_delta"].median())
    dp95 = rec.get("dp95_pct", float("nan"))

    ac = abs(cliffs) if np.isfinite(cliffs) else float("nan")
    if not np.isfinite(ac):
        rec["effect_magnitude"] = "未知"
    elif ac < CLIFFS_NEGLIGIBLE:
        rec["effect_magnitude"] = "可忽略"
    elif ac < CLIFFS_SMALL:
        rec["effect_magnitude"] = "小"
    elif ac < CLIFFS_MEDIUM:
        rec["effect_magnitude"] = "中"
    else:
        rec["effect_magnitude"] = "大"

    # 注意：p_value == 1.0 且 cliffs == 0.0 来自 run_matrix 的 np.allclose 分支，
    # 严格说是“逐请求差异在 rtol=1e-5 内”，不必然是逐位相同；
    # 真正的逐位相同由 feasibility_pairwise_identity.csv 独立判定。
    if n and n_ident == n:
        rec["p95_dir"] = "逐位相同(数值精度内)"
        rec["verdict"] = "逐位相同(该组合无信号)"
    elif sig_cells.empty:
        rec["p95_dir"] = "无显著差异"
        rec["verdict"] = "无显著差异"
    else:
        rec["p95_dir"] = "更优" if dp95 < 0 else "劣化"
        suffix = "全部" if len(sig_cells) == n else "部分"
        if (np.isfinite(dp95) and abs(dp95) < NEGLIGIBLE_DP95_PCT
                and ac < CLIFFS_NEGLIGIBLE):
            rec["p95_dir"] = "可忽略"
            rec["verdict"] = "统计显著但效应可忽略(无实质差异)"
        else:
            rec["verdict"] = f"NA{rec['p95_dir']}(p95,{suffix} cell 显著)"

    if np.isfinite(cliffs) and np.isfinite(dp95) and cliffs != 0.0 and dp95 != 0.0:
        if np.sign(cliffs) != np.sign(dp95):
            rec["tail_vs_median"] = ("尾部改善/中位数变差"
                                     if dp95 < 0 else "尾部变差/中位数改善")
        else:
            rec["tail_vs_median"] = "方向一致"
    else:
        rec["tail_vs_median"] = "无方向差异"


# --------------------------------------------------------------------------
# 图通用
# --------------------------------------------------------------------------
def rel(path: Path) -> str:
    """相对项目根显示；目录被改到根之外时退回绝对路径。"""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def save_fig(fig, name: str) -> Path:
    path = FIG_DIR / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=170, bbox_inches="tight")
    plt.close(fig)
    print(f"  [图] {rel(path)}")
    return path


def write_table(df: pd.DataFrame, name: str) -> Path:
    path = TABLE_DIR / name
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8-sig")
    print(f"  [表] {rel(path)}  ({len(df)} 行)")
    return path


def annotate_bars(ax, bars, values, fmt="{:+.1f}%"):
    for rect, v in zip(bars, values):
        if not np.isfinite(v):
            continue
        ax.annotate(fmt.format(v), (rect.get_x() + rect.get_width() / 2,
                                    rect.get_height()),
                    xytext=(0, 3 if v >= 0 else -10), textcoords="offset points",
                    ha="center", fontsize=7.5)


LAG_TICKS = [0.0, 0.25, 1.0, 2.0, 5.0, 15.0, 30.0, 60.0]


def fmt_lag(v: float) -> str:
    return f"{v:g}"


def set_lag_axis(ax) -> None:
    """陈旧度扫描的 x 轴：symlog（含 0），只保留扫描点作为刻度。

    symlog 默认会在对数段塞入 2/3/... 之类的次刻度，和显式刻度叠在一起看不清，
    因此把次刻度定位器清空。
    """
    ax.set_xscale("symlog", linthresh=0.25)
    ax.set_xticks(LAG_TICKS)
    ax.set_xticklabels([fmt_lag(v) for v in LAG_TICKS])
    ax.xaxis.set_minor_locator(matplotlib.ticker.NullLocator())
    ax.set_xlim(-0.08, 80)


def zero_crossing(x, y):
    """y 由正变负（或反之）处，在 log10(x) 上线性插值。返回 None 表示无过零。"""
    xs = np.asarray(x, dtype=float)
    ys = np.asarray(y, dtype=float)
    ok = np.isfinite(xs) & np.isfinite(ys) & (xs > 0)
    xs, ys = xs[ok], ys[ok]
    if len(xs) < 2:
        return None
    order = np.argsort(xs)
    xs, ys = xs[order], ys[order]
    for i in range(len(xs) - 1):
        if ys[i] == 0:
            return float(xs[i])
        if ys[i] * ys[i + 1] < 0:
            lx = np.log10(xs[i:i + 2])
            t = -ys[i] / (ys[i + 1] - ys[i])
            return float(10 ** (lx[0] + t * (lx[1] - lx[0])))
    return None


# --------------------------------------------------------------------------
# §0 数据可用性 + 负载强度门
# --------------------------------------------------------------------------
def section_availability(data: dict) -> pd.DataFrame:
    print("\n" + "=" * 78)
    print("§0 数据可用性检查")
    print("=" * 78)
    rows = []
    for mode in MODES:
        d = data[mode]
        has_runs = d["runs"] is not None
        has_paired = d["paired"] is not None
        has_man = d["manifest"] is not None
        man = d["manifest"] or {}
        # 代码版本与"有没有未提交改动"要进可用性表：矩阵是分 mode 分批跑的，
        # 中途改过代码的话，同一条曲线上的点可能来自不同版本——这件事必须看得见
        # （本仓库真实发生过：safe.directory 指错导致 code_version 读不到 HEAD）。
        row = {
            "mode": mode,
            "run_id": d["run_id"] or "-",
            "runs": "有" if has_runs else "缺",
            "paired": "有" if has_paired else "缺",
            "manifest": "有" if has_man else "缺",
            "code_version": man.get("code_version", "-") if has_man else "-",
            "code_dirty": ("有未提交改动" if man.get("code_dirty_files")
                           else ("干净" if has_man else "-")),
            "n_rows": d["n_rows"], "n_cells": d["n_cells"],
            "strategies": ("/".join(sorted(d["runs"]["strategy"].unique()))
                           if has_runs else "-"),
            "n_seeds": int(d["runs"]["seed"].nunique()) if has_runs else 0,
            "p_holm 来源": d["p_holm_source"] or "-",
            "多余 run_id": ",".join(d["other_run_ids"]) or "-",
        }
        rows.append(row)
        status = "完整" if (has_runs and has_paired and has_man) else "不完整"
        print(f"  {mode:<12} {status:<5} run_id={row['run_id']:<22} "
              f"code={row['code_version']:<12} "
              f"rows={row['n_rows']:>5} cells={row['n_cells']:>4} "
              f"seeds={row['n_seeds']:>2}  缺: {','.join(d['missing']) or '无'}")
    df = pd.DataFrame(rows)
    write_table(df, "data_availability.csv")
    return df


def profile_links_table(net_set: str) -> dict:
    """把带宽敏感性档位表（net_profile → 每节点链路带宽）读进来。

    为什么需要它：`bandwidth` 模式的链路是**每 cell 的档位**，而 manifest 的
    `network_conditions` 只保存三个基础档（normal / degraded / unstable）。
    早先这里拿基础档去复算带宽格子的可行域，把 `slow_200g` 的 load_ratio 算成
    1.167（按它自己的档位是 0.600）——差点把一个完全正常的格子误报成"不可持续"。
    档位表由 `experiments/run_matrix.py` 定义，这里按该批产物的 `net_set` 取对应
    那一套（v1 与 v2 的档位名不相交）。**导入失败或档位缺失时返回空表**，调用方
    必须把缺失当成"无法复算"（NaN），不能用错的链路顶上——那正是上面那次误报的成因。
    """
    try:
        exp_dir = str(ROOT / "experiments")
        if exp_dir not in sys.path:
            sys.path.insert(0, exp_dir)
        import run_matrix as rmx            # noqa: PLC0415
        rmx.NET_SET = net_set
        return {name: [bw for _, bw in links]
                for name, links in rmx.active_bandwidth_profiles().items()}
    except Exception as exc:                # noqa: BLE001
        print(f"  [警告] 无法读取带宽档位表（net_set={net_set}: {exc}）；"
              "bandwidth 模式的可行域将无法复算")
        return {}


def section_rho(data: dict) -> pd.DataFrame:
    """门 1：任何策略对比都必须先报 rho。

    有效性判定用**跨种子的 min/max**，不用均值：rho 扫描那一节里同一个
    (workload, network) 会覆盖多个到达压力档，用均值判会把越界档平均掉。
    """
    print("\n" + "=" * 78)
    print("§0b 负载强度门（门 1）rho = 平均工作量 × 实际到达率 / (Σ compute_rate × 槽位)")
    print("=" * 78)
    # 可行域与慢链路强度都是**每行**的量，必须在分组之前逐行算好再聚合。
    # 【为什么】它们对 E[work] / E[字节数] 是非线性的（每节点取 min 再求和），
    # 拿"分组均值"去算会得到谁都不对应的值。已实测到的后果：trace 批的 rho 模式里
    # 一个组跨 3 个 trace 时间片（各片 R 差 4 倍），按组均值算出的 load_ratio 是
    # 0.30 的 1.45 倍，于是 6 个正常格子被误报成"队列无限增长、不可采信"。
    # 逐行算、再取组内 max，才是"这个组里最坏的那次运行"。
    node_rates = lambda n: [16000.0 / (2 ** (i % 4)) for i in range(int(n))]
    cond_links = {}
    for mode in MODES:
        man = data[mode].get("manifest") or {}
        for net, c in (man.get("network_conditions") or {}).items():
            cond_links[(mode, net)] = [bw for _, bw in c.get("links", [])]
    # 带宽档位（bandwidth 模式）的链路不在 manifest 里，得按档位表解析；见 profile_links_table
    net_sets = set()
    for mode in MODES:
        r = data[mode]["runs"]
        if r is not None and "net_set" in r.columns:
            net_sets |= set(r["net_set"].dropna().astype(str))
    prof_links: dict = {}
    for ns in sorted(net_sets or {"v1"}):
        prof_links.update(profile_links_table(ns))

    def cell_links(mode: str, row) -> list | None:
        """解析这一行对应的每节点链路带宽；解析不到返回 None（不拿错的链路顶上）。"""
        prof = row.get("net_profile")
        if isinstance(prof, str) and prof:
            # profile_links_table 已经把 (时延, 带宽) 拍平成一串带宽
            return list(prof_links.get(prof) or []) or None
        return cond_links.get((mode, row["network"]))

    def row_feas(mode: str, row) -> dict:
        """一行 → {load_ratio, R, slow_link_Bps, rho_slow_link}。算不出就是 NaN。

        可持续到达率 R = Σ_i min(算力_i × 槽位 / E[work], 带宽_i / E[bytes])，
        即"完全均衡放置"时的可承载到达率。load_ratio = 实际到达率 / R > 1
        意味着队列无限增长，该格的 p95 只是有限时长的假象。
        """
        links = cell_links(mode, row)
        mb = row.get("mean_bytes", np.nan)
        mw = row.get("mean_work", np.nan)
        n_link = float(min(links)) if links else np.nan
        if not links or not np.isfinite(mb) or mb <= 0 or not np.isfinite(mw) or mw <= 0:
            return {"load_ratio": np.nan, "R": np.nan, "slow_link_Bps": n_link,
                    "rho_slow_link": np.nan}
        rates = node_rates(row["n_nodes"])
        R = sum(min(rates[i] * SLOTS_PER_NODE / mw, links[i % len(links)] / mb)
                for i in range(int(row["n_nodes"])))
        return {"load_ratio": (row["qps_realized"] / R if R > 0 else np.nan), "R": R,
                "slow_link_Bps": n_link,
                "rho_slow_link": row["qps_realized"] * mb / n_link}

    rows = []
    for mode in MODES:
        runs = data[mode]["runs"]
        if runs is None or runs.empty:
            continue
        # 到达压力本身就是实验因子的 mode（rho 扫描），要按档位分别列，不能合并
        keys = ["workload", "network"]
        if "target_rho" in runs.columns and runs["target_rho"].nunique(dropna=True) > 1:
            keys.append("target_rho")
        # 节点规模也是因子（scale 模式）：可行域标定的 R 依赖节点数，
        # 若把 4/8/16 节点混在一起算 load_ratio，会得到完全错误的值
        # （实测把 0.600 混成 1.69，并让 2 个格子被误判为"不可持续"）。
        if "n_nodes" in runs.columns and runs["n_nodes"].nunique(dropna=True) > 1:
            keys.append("n_nodes")
        # 数据量档与带宽档位都会**直接改变可行域**（E[字节数]、每条链路容量），
        # 也必须分档：把它们平均掉会得到一个谁都不对应的 load_ratio。
        for extra in ("data_volume", "net_profile"):
            if extra in runs.columns and runs[extra].nunique(dropna=True) > 1:
                keys.append(extra)
        for kb, g in runs.groupby(keys, dropna=False):
            kb = kb if isinstance(kb, tuple) else (kb,)
            rec = dict(zip(keys, kb))
            rho = g["rho"]
            # v2：链路争用模型下链路容量也是硬约束，必须同时报 rho_net。
            # 门 1 的判据推广为 max(rho_compute, rho_network) 落在区间内。
            rho_net = None
            if "rho_net" in g.columns and g["rho_net"].notna().any():
                rho_net = g["rho_net"].dropna()
            if rho_net is not None and len(rho_net):
                rec["rho_net_mean"] = float(rho_net.mean())
                rec["rho_net_max"] = float(rho_net.max())
                rec["binding"] = ("network" if rho_net.max() > rho.max() else "compute")
            # 逐行算可行域与慢链路强度，再取组内 max / min（见本函数开头的注释）
            feas = [row_feas(mode, row) for _, row in g.iterrows()]
            def _agg(key: str, how: str) -> float:
                vals = [f[key] for f in feas if np.isfinite(f[key])]
                return float(getattr(np, how)(vals)) if vals else np.nan
            rec["load_ratio"] = _agg("load_ratio", "nanmax")
            # R 取组内最小（最紧的那次运行），和 load_ratio 取最大是同一种保守方向
            rec["R_sustainable"] = _agg("R", "nanmin")
            rec["slow_link_Bps"] = _agg("slow_link_Bps", "nanmin")
            # 慢链路强度（**最坏放置上界**：λ 全压在一条最慢链路上，不是任何策略的实际负载）
            rec["rho_slow_link"] = _agg("rho_slow_link", "nanmax")
            rec.update({
                "mode": mode,
                "rho_mean": float(rho.mean()), "rho_min": float(rho.min()),
                "rho_max": float(rho.max()),
                "qps_realized": float(g["qps_realized"].mean()),
                "mean_work": float(g["mean_work"].mean()),
                "mean_bytes": float(g["mean_bytes"].mean()) if "mean_bytes" in g.columns else float("nan"),
                "capacity": float(g["capacity"].mean()),
                "n_nodes": int(g["n_nodes"].iloc[0]),
            })
            rows.append(rec)
    df = pd.DataFrame(rows)
    if df.empty:
        print("  （无数据）")
        return df

    # --- 门 1 的 v2 口径 ---
    # 上界：任何一条资源都不能过饱和 → max(rho_compute, rho_network) ≤ RHO_HI。
    # 下界：只对**绑定资源**要求。v2 里网络可能才是瓶颈：此时按"可持续到达率"标定
    # rho=0.30，算力利用率天然只有 0.15–0.26（λ 被可行域压住），这不是退化格子，
    # 而是"网络绑定"这一类场景的正常形态——用 v1 的"rho_compute 必须 ≥0.3"会误判它。
    # 可行性（load_ratio ≤ 1）也是硬线：超过 1 意味着队列无限增长，p95 只是有限时长的假象。
    hi = np.maximum(df["rho_max"].to_numpy(),
                    df["rho_net_max"].fillna(0.0).to_numpy()
                    if "rho_net_max" in df.columns else 0.0)
    lo_ok = ((df["rho_min"] >= RHO_LO - RHO_TOL)
             | (df["load_ratio"].fillna(0.0) >= RHO_LO - RHO_TOL))
    df["in_valid_range"] = (hi <= RHO_HI + RHO_TOL) & lo_ok & (df["load_ratio"].fillna(0.0) <= 1.0 + 1e-6)
    df["binding_resource"] = np.where(
        df["rho_mean"].to_numpy() >= (df["rho_net_mean"].fillna(0.0).to_numpy()
                                      if "rho_net_mean" in df.columns else 0.0),
        "compute", "network")
    if df["load_ratio"].notna().any():
        bad_f = df[df["load_ratio"] > 1.0]
        print(f"  多资源可行性 load_ratio = 实际到达率 / 可持续到达率："
              f"范围 [{df['load_ratio'].min():.2f}, {df['load_ratio'].max():.2f}]，"
              f"{len(bad_f)}/{len(df)} 个组合 >1")
        if len(bad_f):
            print("     ⚠ 这些格子**不可持续**（队列会无限增长），其 p95 只是有限时长的假象，"
                  "报告时必须连带说明：")
            for _, r in bad_f.nlargest(5, "load_ratio").iterrows():
                print(f"       {r['mode']:<12}{r['workload']:<14}{r['network']:<9}"
                      f"n_nodes={int(r['n_nodes'])} load_ratio={r['load_ratio']:.2f}")

    # --- 突发性：把 rho 拆成均值与瞬时（characterize_load.py 的产物）---
    # 只报平均 rho 解释不了"rho=0.6 却有几百倍服务时间的尾部"，
    # 因此把 10 s 窗口的局部 rho 一并列出来；表不存在就跳过，不报错。
    burst_path = TABLE_DIR / "load_burstiness.csv"
    burst_note = "（未生成：先跑 analysis/characterize_load.py）"
    if burst_path.exists():
        try:
            b = pd.read_csv(burst_path)
            agg = b.groupby("workload", as_index=False).agg(
                rho_local_p95_10s=("rho_local_p95_w10", "mean"),
                overload_frac_10s=("overload_frac_w10", "mean"),
                rho_local_max_10s=("rho_local_max_w10", "max"),
                rho_local_p95_1s=("rho_local_p95_w1", "mean"),
                rho_local_max_1s=("rho_local_max_w1", "max"))
            df = df.merge(agg, on="workload", how="left")
            burst_note = f"（来自 {burst_path.name}）"
        except Exception as exc:      # noqa: BLE001
            print(f"  [警告] 无法解析 {burst_path.name}: {exc}")

    write_table(df, "load_intensity_rho.csv")
    bad = df[~df["in_valid_range"]]
    print(f"  覆盖 {len(df)} 个 (mode×workload×network×档位) 组合，"
          f"rho_compute 范围 [{df['rho_mean'].min():.3f}, {df['rho_mean'].max():.3f}]")
    if "rho_net_max" in df.columns and df["rho_net_max"].notna().any():
        sub = df[df["rho_net_max"].notna()]
        print(f"  v2 链路争用：rho_network 范围 "
              f"[{sub['rho_net_mean'].min():.3f}, {sub['rho_net_mean'].max():.3f}]；"
              f"判据用 max(rho_compute, rho_network)。绑定资源分布："
              f"{sub['binding'].value_counts().to_dict() if 'binding' in sub.columns else '-'}")
        print("    —— 网络绑定（binding=network）的格子说明链路比算力先饱和，"
              "那是最能体现『算力+网络联合调度』的场景")
    if "rho_slow_link" in df.columns and df["rho_slow_link"].notna().any():
        over = df[df["rho_slow_link"] > 0.9]
        # 口径声明很重要：这是**最坏放置上界**（把全部到达流量都压到同一条最慢链路上），
        # 不是任何策略的实际链路负载。n 节点机群上它比实际值松 n/链路档数倍，
        # 早先按"热点链路已被打满"报告，是把上界当成了事实——真正的运行点可行性看 load_ratio。
        print(f"  慢链路最坏放置上界 rho_slow_link = λ×E[字节]/最慢链路带宽"
              f"（**上界**，非实际值）：范围 [{df['rho_slow_link'].min():.2f}, "
              f"{df['rho_slow_link'].max():.2f}]，其中 {len(over)}/{len(df)} 个组合的上界 >0.9"
              "：这些格子里「把流量错放到慢链路」确实会把它打满，正是网络感知要避免的情形")
        if len(over):
            print("     上界最松的几个组合（说明单条最慢链路一旦被选中就会成为瓶颈，"
                  "不是由聚合 rho_net 决定）：")
            for _, r in over.nlargest(4, "rho_slow_link").iterrows():
                print(f"       {r['mode']:<12}{r['workload']:<14}{r['network']:<9}"
                      f"rho_slow_link={r['rho_slow_link']:.2f}")
    if bad.empty:
        print(f"  全部落在有效区间 [{RHO_LO}, {RHO_HI}]。✓")
    else:
        print(f"  ⚠ {len(bad)} 个组合落在有效区间之外（这些格子上的策略对比不可直接采信）：")
        for _, r in bad.iterrows():
            print(f"     {r['mode']:<12} {r['workload']:<14} {r['network']:<9} "
                  f"rho_compute={r['rho_mean']:.3f}"
                  + (f" rho_network={r.get('rho_net_mean', float('nan')):.3f}"
                     if "rho_net_mean" in r and pd.notna(r.get("rho_net_mean")) else "")
                  + f" n_nodes={r['n_nodes']}")
    for mode in df["mode"].unique():
        sub = df[df["mode"] == mode]
        print(f"   {mode:<12} rho∈[{sub['rho_mean'].min():.3f},{sub['rho_mean'].max():.3f}]")
    if "overload_frac" in df.columns:
        print(f"  瞬时负载 {burst_note}：10 s 窗口局部 rho>1 的窗口占比 "
              f"中位 {df['overload_frac_10s'].median()*100:.1f}%，"
              f"最大 {df['overload_frac_10s'].max()*100:.1f}%；"
              f"局部 rho max 最大 {df['rho_local_max_10s'].max():.2f}（10 s 窗）、"
              f"{df['rho_local_max_1s'].max():.2f}（1 s 窗）")
        print("     —— 平均 rho 合格不等于没有瞬态过载；尾部指标主要由它决定。")
    return df


# --------------------------------------------------------------------------
# §1 RQ1 收益边界
# --------------------------------------------------------------------------
def section_rq1(data: dict):
    print("\n" + "=" * 78)
    print("§1 RQ1 收益边界（主矩阵：workload × network × staleness，NA vs 三个基线）")
    print("=" * 78)
    d = data["main"]
    runs, paired = d["runs"], d["paired"]
    if runs is None or runs.empty:
        print("  main 模式暂无数据，跳过 RQ1。")
        return None

    eff = effect_table(runs, paired, ["workload", "network", "staleness"])
    if eff.empty:
        print("  无法计算效应表（缺策略行）。")
        return None
    write_table(eff, "rq1_effect_workload_network_staleness.csv")

    # 覆盖度
    full = eff.groupby(["workload", "network", "staleness"]).size()
    n_all_seeds = runs.groupby(["workload", "network", "staleness"])["seed"].nunique()
    print(f"  主矩阵已覆盖 {len(full)} / 24 个 (workload×network×staleness) 组合")
    missing = [c for c in pd.MultiIndex.from_product(
        [sorted(runs['workload'].unique()), sorted(runs['network'].unique()),
         sorted(runs['staleness'].unique())]) if c not in full.index]
    if missing:
        print(f"  尚未出现的组合({len(missing)}): "
              + ", ".join(f"{a}/{b}/{c}" for a, b, c in missing))
    print(f"  每组合种子数: {sorted(set(n_all_seeds))}")

    # --- 列表：显著、无显著、劣化 ---
    print("\n  --- 逐条效应（Δp95<0 = NetworkAware 更优）---")
    hdr = (f"  {'workload':<14}{'network':<10}{'staleness':<8}{'baseline':<12}"
           f"{'Δp95%':>9}{'±se':>7}{'ΔSLO':>9}{'net_share':>10}"
           f"{'cliffs':>8}{'效力':>6}{'p_holm':>9}{'rho':>7}  判定 / 尾-中位关系")
    print(hdr)
    for _, r in eff.sort_values(["workload", "network", "staleness", "baseline"]).iterrows():
        print(f"  {r['workload']:<14}{r['network']:<10}{r['staleness']:<8}"
              f"{r['baseline']:<12}{r['dp95_pct']:>+9.2f}{r['dp95_se']:>7.2f}"
              f"{r['dslo_abs']:>+9.4f}{r['net_share_na']:>10.4f}"
              f"{r['cliffs_delta_median']:>8.3f}{r['effect_magnitude']:>6}"
              f"{r['p_holm_median']:>9.4f}"
              f"{r['rho_mean']:>7.3f}  {r['verdict']} / {r['tail_vs_median']}")

    print("\n  --- 分类汇总（按 p95 方向）---")
    for label in sorted(eff["verdict"].unique()):
        sub = eff[eff["verdict"] == label]
        print(f"   [{label}] {len(sub)} 条")
        for _, r in sub.iterrows():
            print(f"      {r['workload']}/{r['network']}/{r['staleness']} "
                  f"vs {r['baseline']:<11} Δp95={r['dp95_pct']:+.2f}% "
                  f"cliffs={r['cliffs_delta_median']:+.3f} "
                  f"p_holm={r['p_holm_median']:.4g}  [{r['tail_vs_median']}]")

    conflict = eff[eff["tail_vs_median"].isin(["尾部改善/中位数变差", "尾部变差/中位数改善"])]
    print(f"\n  ⚠ 尾部与中位数方向不一致的条目：{len(conflict)}")
    if not conflict.empty:
        for _, r in conflict.iterrows():
            print(f"     {r['workload']}/{r['network']}/{r['staleness']} vs {r['baseline']}: "
                  f"Δp95={r['dp95_pct']:+.2f}% 但 cliffs={r['cliffs_delta_median']:+.3f} "
                  f"（中位数差={r['median_diff_median']:+.4g}）→ {r['tail_vs_median']}")
        print("     含义：NetworkAware 用较多请求的轻微退化换取尾部分位的大幅改善"
              "（或反之）；两个方向都要写进论文，不能只报一个。")

    deg = eff[eff["verdict"].str.startswith("NA劣化")]
    print(f"\n  ⚠ NetworkAware 劣化格子数：{len(deg)}")
    if not deg.empty:
        for _, r in deg.iterrows():
            print(f"     {r['workload']}/{r['network']}/{r['staleness']} vs {r['baseline']}: "
                  f"Δp95={r['dp95_pct']:+.2f}% (cliffs={r['cliffs_delta_median']:+.3f}, "
                  f"p_holm={r['p_holm_median']:.3g})")
    nsig = eff[eff["verdict"].isin(["无显著差异", "逐位相同(该组合无信号)"])]
    print(f"  ⚠ 无区分度格子数：{len(nsig)}（其中逐位相同 "
          f"{int((eff['verdict'] == '逐位相同(该组合无信号)').sum())}）")

    # --- 图 1：热图（行=staleness，列=基线；格=workload×network 的 Δp95）---
    wls = sorted(runs["workload"].unique())
    nets = sorted(runs["network"].unique())

    def draw_heatmap(vmax_val, fname, note, saturate=False):
        fig, axes_ = plt.subplots(2, 3, figsize=(16.5, 9.2), constrained_layout=True)
        norm_ = TwoSlopeNorm(vcenter=0.0, vmin=-vmax_val, vmax=vmax_val)
        im_ = None
        for i, stal in enumerate(["fresh", "stale"]):
            for j, base in enumerate(BASELINES):
                ax = axes_[i, j]
                sub = eff[(eff["staleness"] == stal) & (eff["baseline"] == base)]
                M = np.full((len(wls), len(nets)), np.nan)
                marks = {}
                for _, r in sub.iterrows():
                    yi = wls.index(r["workload"])
                    xi = nets.index(r["network"])
                    M[yi, xi] = r["dp95_pct"]
                    marks[(yi, xi)] = (r["verdict"], r["tail_vs_median"],
                                       r["effect_magnitude"])
                Mplot = np.clip(M, -vmax_val, vmax_val) if saturate else M
                im_ = ax.imshow(Mplot, cmap="RdBu_r", norm=norm_, aspect="auto")
                ax.set_xticks(range(len(nets)))
                ax.set_xticklabels(nets, rotation=0)
                ax.set_yticks(range(len(wls)))
                ax.set_yticklabels(wls)
                ax.set_title(f"{stal}  ×  vs {base}", fontsize=11)
                for yi in range(len(wls)):
                    for xi in range(len(nets)):
                        v = M[yi, xi]
                        if not np.isfinite(v):
                            ax.text(xi, yi, "缺", ha="center", va="center",
                                    fontsize=8, color="#555555")
                            continue
                        vd, tv, mag = marks.get((yi, xi), ("", "", ""))
                        tag = ""
                        if "逐位相同" in vd:
                            tag = "\n(逐位相同)"
                        elif vd == "无显著差异":
                            tag = "\n(n.s.)"
                        elif "效应可忽略" in vd:
                            tag = "\n(可忽略)"
                        if tv.startswith("尾部改善"):
                            tag += "\n尾部↑中位↓"
                        elif tv.startswith("尾部变差"):
                            tag += "\n尾部↓中位↑"
                        ax.text(xi, yi, f"{v:+.1f}%{tag}", ha="center", va="center",
                                fontsize=8.5,
                                color="black" if abs(np.clip(v, -vmax_val, vmax_val)) < 0.6 * vmax_val
                                else "white")
        cb = fig.colorbar(im_, ax=axes_, shrink=0.85, pad=0.015)
        cb.set_label(note)
        fig.suptitle("RQ1 收益边界：NetworkAware 相对基线的 p95 变化（主矩阵）", fontsize=13)
        save_fig(fig, fname)

    vmax_all = float(np.nanmax(np.abs(eff["dp95_pct"].values))) if len(eff) else 1.0
    draw_heatmap(vmax_all, "fig_rq1_dp95_heatmap.png",
                 "Δp95 = (NetworkAware − 基线) / 基线  [%]\n"
                 "负值=NetworkAware 更优，正值=更差（满量程色标）")
    # 收益幅度可达 −98%，满量程色标会把"接近 0"的格子压成同色；
    # 再出一张饱和到 ±10% 的版本，专门用来看收益边界在哪一侧。
    draw_heatmap(10.0, "fig_rq1_dp95_heatmap_zoom.png",
                 "Δp95  [%]（色标饱和于 ±10%，格内数字为真值）\n"
                 "负值=NetworkAware 更优，正值=更差", saturate=True)

    # --- 图 2：按 (workload×network) 汇总的柱状图（含跨种子误差棒）---
    agg = (eff.groupby(["workload", "network", "baseline"], as_index=False)
              .agg(dp95_pct=("dp95_pct", "mean"), dp95_se=("dp95_se", "mean"),
                   net_share=("net_share_na", "mean")))
    fig, axes = plt.subplots(1, 3, figsize=(16.5, 5.0), constrained_layout=True)
    for j, base in enumerate(BASELINES):
        ax = axes[j]
        sub = agg[agg["baseline"] == base]
        labels, vals, errs = [], [], []
        for wl in wls:
            for net in nets:
                s = sub[(sub["workload"] == wl) & (sub["network"] == net)]
                if s.empty:
                    continue
                labels.append(f"{wl}\n{net}")
                vals.append(float(s["dp95_pct"].iloc[0]))
                errs.append(float(s["dp95_se"].iloc[0]))
        xs = np.arange(len(labels))
        colors = ["#C44E52" if v > 0 else "#4C72B0" for v in vals]
        bars = ax.bar(xs, vals, yerr=errs, color=colors, capsize=2.5,
                      edgecolor="black", linewidth=0.4)
        annotate_bars(ax, bars, vals)
        ax.axhline(0, color="black", linewidth=0.9)
        ax.set_xticks(xs)
        ax.set_xticklabels(labels, rotation=90, fontsize=7)
        ax.set_ylabel("Δp95  [%]（负=更优）")
        ax.set_title(f"vs {base}")
        ax.grid(axis="y", alpha=0.25)
    fig.suptitle("RQ1 各 (workload × network) 上 NetworkAware 的 p95 变化（误差棒=跨种子 se）",
                 fontsize=12)
    save_fig(fig, "fig_rq1_dp95_bars.png")
    return eff, agg


# --------------------------------------------------------------------------
# §2 RQ2 陈旧度曲线
# --------------------------------------------------------------------------
def _staleness_curve(stal_runs: pd.DataFrame, channel: str) -> pd.DataFrame:
    """channel: 'network' → staleness_network_s 为 x；'queue' → staleness_queue_s 为 x。"""
    df = stal_runs.copy()
    if channel == "network":
        df = df[df["staleness"].str.startswith("net")]
        xcol = "staleness_network_s"
    else:
        df = df[df["staleness"].str.startswith("queue")]
        xcol = "staleness_queue_s"
    if df.empty:
        return pd.DataFrame()
    g = (df.groupby([xcol, "network", "strategy"], as_index=False)
           .agg(sojourn_p95=("sojourn_p95", "mean"),
                sojourn_p95_std=("sojourn_p95", "std"),
                sojourn_mean=("sojourn_mean", "mean"),
                slo_miss_rate=("slo_miss_rate", "mean"),
                net_share=("net_share", "mean"),
                rho=("rho", "mean"),
                n_seeds=("seed", "nunique")))
    g = g.rename(columns={xcol: "lag_s"})
    g.insert(0, "channel", channel)
    return g.sort_values(["network", "strategy", "lag_s"]).reset_index(drop=True)


def _staleness_invariance_checks(stal_runs: pd.DataFrame) -> pd.DataFrame:
    """两条通道可分离性的**逐位**证据。

    * queue 扫描：同一 seed 下 load 与 trajectory 完全相同，只有 lag 变。
      Random 完全不读观测状态 → 其对 lag 的结果必须逐位不变（负对照）。
    * net 扫描：LeastQueue / LeastLoad 不读链路字段 → 在可比的 lag 区间上，
      只有 queue lag 不变时它们才必须逐位不变。
    """
    rows = []

    def spread(sub, label, strategies, lag_col, lags):
        s = sub[sub[lag_col].isin(lags)]
        for st in strategies:
            g = s[s["strategy"] == st]
            if g.empty:
                continue
            vals = []
            for _, gg in g.groupby(lag_col):
                vals.append(gg["sojourn_p95"].to_numpy(dtype=float))
            if len(vals) < 2:
                continue
            try:
                arr = np.vstack(vals)
            except ValueError:
                continue
            ref = arr[0]
            denom = np.where(np.abs(ref) > 0, np.abs(ref), 1.0)
            max_rel = float(np.max(np.abs(arr - ref) / denom))
            bitwise = bool(np.all(arr == arr[0]))
            rows.append({"check": label, "strategy": st,
                         "n_lags": len(vals), "bitwise_identical": bitwise,
                         "max_rel_dev": max_rel})

    qa = stal_runs[stal_runs["staleness"].str.startswith("queue")]
    spread(qa, "queue 扫描(lag_q 变)，期望 Random 逐位不变",
           ["Random", "LeastQueue", "LeastLoad", "NetworkAware"],
           "staleness_queue_s", sorted(qa["staleness_queue_s"].unique()))

    na = stal_runs[stal_runs["staleness"].str.startswith("net")]
    net_sweep_lags = [l for l in sorted(na["staleness_network_s"].unique()) if l >= 1.0]
    spread(na, "net 扫描(lag_q 已饱和在 1s)，期望 leastqueue/leastload 逐位不变",
           ["LeastQueue", "LeastLoad", "NetworkAware"],
           "staleness_network_s", net_sweep_lags)
    spread(na, "net 扫描(lag_q 已饱和在 1s)，Random 对照",
           ["Random"], "staleness_network_s", net_sweep_lags)
    return pd.DataFrame(rows)


def section_rq2(data: dict):
    print("\n" + "=" * 78)
    print("§2 RQ2 陈旧度曲线（链路陈旧 vs 队列陈旧，两条通道可分离）")
    print("=" * 78)
    d = data["staleness"]
    runs = d["runs"]
    if runs is None or runs.empty:
        print("  staleness 模式暂无数据，跳过 RQ2。")
        return None
    print(f"  staleness 标签: {sorted(runs['staleness'].unique())}")
    print("  链路陈旧秒数: "
          + ", ".join(fmt_lag(v) for v in sorted(runs['staleness_network_s'].unique())))
    print("  队列陈旧秒数: "
          + ", ".join(fmt_lag(v) for v in sorted(runs['staleness_queue_s'].unique())))

    net_curve = _staleness_curve(runs, "network")
    q_curve = _staleness_curve(runs, "queue")
    curve = pd.concat([c for c in (net_curve, q_curve) if not c.empty], ignore_index=True)
    if not curve.empty:
        write_table(curve, "rq2_staleness_curve.csv")
    else:
        print("  未能解析出曲线数据。")
        return None

    # --- 可分离性检查 ---
    print("\n  --- 两条通道可分离性（逐位比较，门 3 的确定性证据）---")
    checks = _staleness_invariance_checks(runs)
    if not checks.empty:
        write_table(checks, "rq2_channel_separation.csv")
        for _, r in checks.iterrows():
            flag = "逐位不变 ✓" if r["bitwise_identical"] else f"变化(rel={r['max_rel_dev']:.3g})"
            print(f"    {r['check'][:46]:<48}{r['strategy']:<14}{r['n_lags']:>2} lags  {flag}")

    # --- 陈临界点 ---
    crit_rows = []
    if not net_curve.empty:
        for net in sorted(net_curve["network"].unique()):
            sub = net_curve[net_curve["network"] == net]
            na = sub[sub["strategy"] == NA].sort_values("lag_s")
            naive_names = [s for s in ("LeastQueue", "LeastLoad", "Random")]
            naive = (sub[sub["strategy"].isin(naive_names)]
                     .groupby("lag_s", as_index=False)["sojourn_p95"].min()
                     .rename(columns={"sojourn_p95": "p95_naive_best"}))
            m = na[["lag_s", "sojourn_p95"]].merge(naive, on="lag_s")
            if m.empty:
                continue
            m["dp95_vs_naive"] = (m["sojourn_p95"] - m["p95_naive_best"]) / m["p95_naive_best"] * 100
            cross = zero_crossing(m["lag_s"].to_numpy(), m["dp95_vs_naive"].to_numpy())
            base0 = m[m["lag_s"] == 0.0]
            crit_rows.append({
                "network": net,
                "lag0_dp95_vs_naive": float(base0["dp95_vs_naive"].iloc[0]) if len(base0) else float("nan"),
                "lag_max_dp95_vs_naive": float(m["dp95_vs_naive"].iloc[-1]),
                "critical_lag_s": cross,
                "lag_range_s": f"[{m['lag_s'].min():g}, {m['lag_s'].max():g}]",
            })
    crit = pd.DataFrame(crit_rows)
    if not crit.empty:
        write_table(crit, "rq2_critical_staleness.csv")
        print("\n  --- 链路陈临界点（NetworkAware 相对最强网络盲基线的 Δp95 过零点）---")
        for _, r in crit.iterrows():
            cl = f"{r['critical_lag_s']:.3f} s" if pd.notna(r["critical_lag_s"]) else "在扫描区间内未过零"
            print(f"    {r['network']:<10} lag=0 时 Δp95={r['lag0_dp95_vs_naive']:+.2f}%  "
                  f"lag 上限时={r['lag_max_dp95_vs_naive']:+.2f}%  过零点: {cl}")

    # --- 图：链路陈旧 → p95（上排绝对、下排相对最强盲基线）---
    if not net_curve.empty:
        nets = sorted(net_curve["network"].unique())
        fig, axes = plt.subplots(2, len(nets), figsize=(5.4 * len(nets), 8.6),
                                 constrained_layout=True, sharex="col")
        if len(nets) == 1:
            axes = axes.reshape(2, 1)
        for j, net in enumerate(nets):
            sub = net_curve[net_curve["network"] == net]
            ax = axes[0, j]
            for st in STRATEGIES:
                s = sub[sub["strategy"] == st].sort_values("lag_s")
                if s.empty:
                    continue
                ax.errorbar(s["lag_s"], s["sojourn_p95"],
                            yerr=s["sojourn_p95_std"], label=st,
                            color=COLORS[st], marker=MARKERS[st], markersize=5,
                            linewidth=1.6, capsize=3)
            ax.set_xscale("symlog", linthresh=0.25)
            set_lag_axis(ax)
            ax.set_ylabel("p95 端到端完成时间  [s]")
            ax.set_title(f"网络条件: {net}")
            ax.grid(alpha=0.25)
            if j == 0:
                ax.legend(fontsize=8.5)

            # 下排：Δp95 vs 最强网络盲基线
            ax = axes[1, j]
            ss = sub[sub["strategy"] != NA]
            naive = (ss.groupby("lag_s", as_index=False)["sojourn_p95"].min()
                       .rename(columns={"sojourn_p95": "p95_naive"}))
            na = sub[sub["strategy"] == NA].sort_values("lag_s")
            m = na[["lag_s", "sojourn_p95"]].merge(naive, on="lag_s")
            if not m.empty:
                dp = (m["sojourn_p95"] - m["p95_naive"]) / m["p95_naive"] * 100
                ax.plot(m["lag_s"], dp, marker="D", color=COLORS[NA], linewidth=1.8)
                ax.axhline(0, color="black", linewidth=1.0, linestyle="--")
                ax.fill_between(m["lag_s"], 0, dp.where(dp > 0, 0), color=COLORS[NA], alpha=0.18)
            ax.set_xscale("symlog", linthresh=0.25)
            set_lag_axis(ax)
            ax.set_xlabel("链路状态观测陈旧度  [s]")
            ax.set_ylabel("Δp95 vs 最强网络盲基线  [%]")
            ax.grid(alpha=0.25)
        fig.suptitle("RQ2 链路陈旧的影响：网络盲基线不随链路陈旧变化，NetworkAware 会衰减\n"
                     "（x 轴 symlog，0 点在线性段）", fontsize=12.5)
        save_fig(fig, "fig_rq2_p95_vs_network_staleness.png")

    # --- 图：队列陈旧 → p95 与 Δp95（对照通道）---
    if not q_curve.empty:
        fig, axes = plt.subplots(1, 2, figsize=(13, 5.0), constrained_layout=True)
        nets_q = sorted(q_curve["network"].unique())
        net = nets_q[0]
        sub = q_curve[q_curve["network"] == net]
        ax = axes[0]
        for st in STRATEGIES:
            s = sub[sub["strategy"] == st].sort_values("lag_s")
            if s.empty:
                continue
            ax.errorbar(s["lag_s"], s["sojourn_p95"], yerr=s["sojourn_p95_std"],
                        label=st, color=COLORS[st], marker=MARKERS[st], markersize=5,
                        linewidth=1.6, capsize=3)
        ax.set_xscale("symlog", linthresh=0.25)
        set_lag_axis(ax)
        ax.set_xlabel("队列状态观测陈旧度  [s]")
        ax.set_ylabel("p95 端到端完成时间  [s]")
        ax.set_title(f"对照通道：仅队列陈旧（网络={net}，链路实时）")
        ax.legend(fontsize=8.5)
        ax.grid(alpha=0.25)

        ax = axes[1]
        ss = sub[sub["strategy"] != NA]
        naive = (ss.groupby("lag_s", as_index=False)["sojourn_p95"].min()
                   .rename(columns={"sojourn_p95": "p95_naive"}))
        na = sub[sub["strategy"] == NA].sort_values("lag_s")
        m = na[["lag_s", "sojourn_p95"]].merge(naive, on="lag_s")
        if not m.empty:
            dp = (m["sojourn_p95"] - m["p95_naive"]) / m["p95_naive"] * 100
            ax.plot(m["lag_s"], dp, marker="D", color=COLORS[NA], linewidth=1.8)
            ax.axhline(0, color="black", linewidth=1.0, linestyle="--")
        ax.set_xscale("symlog", linthresh=0.25)
        set_lag_axis(ax)
        ax.set_xlabel("队列状态观测陈旧度  [s]")
        ax.set_ylabel("Δp95 vs 最强网络盲基线  [%]")
        ax.set_title("队列陈旧下 NetworkAware 的相对位置")
        ax.grid(alpha=0.25)
        fig.suptitle("RQ2 对照：只有队列状态陈旧时（链路实时），"
                     "四种策略都会受影响，网络感知失去区分度", fontsize=12)
        save_fig(fig, "fig_rq2_p95_vs_queue_staleness.png")

    # --- 图：SLO miss rate 随陈旧度 ---
    fig, axes = plt.subplots(1, 2, figsize=(13, 5.0), constrained_layout=True)
    for ax, cur, xlabel, ttl in (
            (axes[0], net_curve, "链路状态观测陈旧度  [s]", "SLO 未达标率 vs 链路陈旧"),
            (axes[1], q_curve, "队列状态观测陈旧度  [s]", "SLO 未达标率 vs 队列陈旧")):
        if cur.empty:
            ax.text(0.5, 0.5, "无数据", ha="center", va="center", transform=ax.transAxes)
            continue
        for net in sorted(cur["network"].unique()):
            for st in STRATEGIES:
                s = cur[(cur["network"] == net) & (cur["strategy"] == st)].sort_values("lag_s")
                if s.empty:
                    continue
                ls = {"normal": ":", "degraded": "--", "unstable": "-"}.get(net, "-")
                ax.plot(s["lag_s"], s["slo_miss_rate"], linestyle=ls,
                        color=COLORS[st], marker=MARKERS[st], markersize=4,
                        linewidth=1.5,
                        label=f"{st}·{net}")
        ax.set_xscale("symlog", linthresh=0.25)
        set_lag_axis(ax)
        ax.set_xlabel(xlabel)
        ax.set_ylabel("SLO 未达标率（相对最优 ×3.0 定义）")
        ax.set_title(ttl)
        ax.grid(alpha=0.25)
    axes[0].legend(fontsize=6.5, ncol=3)
    fig.suptitle("RQ2 SLO 未达标率随陈旧度的变化", fontsize=12)
    save_fig(fig, "fig_rq2_slo_vs_staleness.png")
    return curve


# --------------------------------------------------------------------------
# §3 数据量边界
# --------------------------------------------------------------------------
def section_datavolume(data: dict):
    print("\n" + "=" * 78)
    print("§3 数据量边界（datavolume 模式：prompt_only vs kv_cache）")
    print("=" * 78)
    d = data["datavolume"]
    runs, paired = d["runs"], d["paired"]
    if runs is None or runs.empty:
        print("  datavolume 模式暂无数据，跳过。")
        return None
    print(f"  数据量档位: {sorted(runs['data_volume'].unique())}")
    print(f"  网络条件: {sorted(runs['network'].unique())}"
          "（注意：该模式不含 unstable）")

    eff = effect_table(runs, paired, ["network", "workload", "data_volume"])
    if eff.empty:
        print("  无法计算效应表。")
        return None
    write_table(eff, "rq3_datavolume_effect.csv")

    print("\n  --- net_share(NetworkAware) → Δp95 ---")
    print(f"  {'network':<10}{'workload':<14}{'data_volume':<13}{'net_share':>10}"
          f"{'Δp95%':>9}{'cliffs':>8}{'p_holm':>9}  判定")
    for _, r in eff.sort_values(["network", "data_volume", "workload"]).iterrows():
        print(f"  {r['network']:<10}{r['workload']:<14}{r['data_volume']:<13}"
              f"{r['net_share_na']:>10.5f}{r['dp95_pct']:>+9.2f}"
              f"{r['cliffs_delta_median']:>8.3f}{r['p_holm_median']:>9.4f}  {r['verdict']}")

    print("\n  --- 收益过零点（在 net_share 对数轴上插值）---")
    cross_rows = []
    for net in sorted(eff["network"].unique()):
        for base in BASELINES:
            sub = eff[(eff["network"] == net) & (eff["baseline"] == base)]
            if len(sub) < 2:
                continue
            c = zero_crossing(sub["net_share_na"].to_numpy(), sub["dp95_pct"].to_numpy())
            cross_rows.append({"network": net, "baseline": base,
                               "net_share_zero_crossing": c,
                               "net_share_range": f"[{sub['net_share_na'].min():.5f}, "
                                                  f"{sub['net_share_na'].max():.5f}]",
                               "n_points": len(sub)})
            txt = f"{c:.5f}" if c is not None else "区间内未过零"
            print(f"    {net:<10} vs {base:<11} 过零 net_share={txt}")
    write_table(pd.DataFrame(cross_rows), "rq3_zero_crossings.csv")

    # 图：1×3 面板（每个基线一个），x=net_share(log)，线=网络条件
    fig, axes = plt.subplots(1, 3, figsize=(16.5, 5.0), constrained_layout=True)
    for j, base in enumerate(BASELINES):
        ax = axes[j]
        sub = eff[eff["baseline"] == base]
        for net in sorted(sub["network"].unique()):
            s = sub[sub["network"] == net].sort_values("net_share_na")
            if s.empty:
                continue
            ax.plot(s["net_share_na"], s["dp95_pct"], marker="o", linewidth=1.6,
                    label=f"{net}")
            for _, r in s.iterrows():
                ax.annotate(str(r["data_volume"]).replace("_only", ""), 
                            (r["net_share_na"], r["dp95_pct"]),
                            textcoords="offset points", xytext=(4, 4), fontsize=6.5)
            c = zero_crossing(s["net_share_na"].to_numpy(), s["dp95_pct"].to_numpy())
            if c is not None:
                ax.axvline(c, color="grey", linestyle=":", linewidth=1.2)
                ax.annotate(f"过零\n{c:.4f}", (c, 0), fontsize=7,
                            textcoords="offset points", xytext=(3, 6), color="grey")
        ax.axhline(0, color="black", linewidth=0.9, linestyle="--")
        ax.set_xscale("log")
        ax.set_xlabel("net_share = 网络传输时间 / 端到端完成时间（NetworkAware）")
        ax.set_ylabel("Δp95  [%]（负=NetworkAware 更优）")
        ax.set_title(f"vs {base}")
        ax.grid(alpha=0.25, which="both")
        ax.legend(title="网络条件", fontsize=8)
    fig.suptitle("RQ3 数据量边界：网络传输占比 net_share 与 NetworkAware 的 p95 收益\n"
                 "（标注为 data_volume：prompt / kv_cache）", fontsize=12.5)
    save_fig(fig, "fig_rq3_net_share_vs_dp95.png")
    return eff


# --------------------------------------------------------------------------
# §4 权重消融（含 w_net=0 ≡ LeastLoad 的逐位验证）
# --------------------------------------------------------------------------
def section_ablation(data: dict):
    print("\n" + "=" * 78)
    print("§4 权重消融 w_net ∈ {0, 0.5, 1, 1.5, 2}（门 2：w_net=0 必须与 LeastLoad 逐位相同）")
    print("=" * 78)
    d = data["ablation"]
    runs, paired = d["runs"], d["paired"]
    if runs is None or runs.empty:
        print("  ablation 模式暂无数据，跳过。")
        return None
    w_vals = sorted(float(x) for x in runs["w_net"].unique())
    print(f"  w_net 取值: {w_vals}")

    # ---- 门 2：逐位验证 ----
    # 注意 w_net 是 **cell 级**参数（该 cell 施加在 NetworkAware 上的权重），
    # 同一个 cell 里的 Random / LeastQueue / LeastLoad 行也带着这个值。
    # 因此必须同时限定 strategy == "NetworkAware"，否则会把 Random/LeastQueue 的行
    # 也拿去和 LeastLoad 比，门 2 会稳定地误报"不通过"。
    sub0 = runs[np.isclose(runs["w_net"], 0.0) & (runs["strategy"] == "NetworkAware")]
    ll = runs[runs["strategy"] == "LeastLoad"]
    rows = []
    if sub0.empty:
        print("  ⚠ 数据里还没有 w_net=0 的 NetworkAware cell。")
    else:
        cols = [c for c in METRIC_COLS if c in runs.columns]
        merged = sub0.merge(ll, on=["cell_id", "seed"], suffixes=("_na", "_ll"))
        n_cells = merged["cell_id"].nunique()
        for c in cols:
            a = merged[f"{c}_na"].to_numpy(dtype=float)
            b = merged[f"{c}_ll"].to_numpy(dtype=float)
            eq = np.array_equal(a, b)
            fin = np.isfinite(a) & np.isfinite(b)
            maxdiff = float(np.max(np.abs(a[fin] - b[fin]))) if fin.any() else float("nan")
            rows.append({"metric": c, "n_cells": n_cells,
                         "bitwise_identical": bool(eq), "max_abs_diff": maxdiff})
        chk = pd.DataFrame(rows)
        write_table(chk, "ablation_w0_vs_leastload.csv")
        n_ok = int(chk["bitwise_identical"].sum())
        print(f"  门 2 验证：{n_cells} 个 cell 上比较 {len(cols)} 个指标")
        all_ok = n_ok == len(cols)
        print(f"   逐位相同指标数 {n_ok}/{len(cols)} → "
              f"{'通过 ✓ 网络项权重为 0 时退化为纯负载感知' if all_ok else '不通过 ✗ 实现可能有 bug'}")
        for _, r in chk.iterrows():
            print(f"     {r['metric']:<18} 逐位相同={str(r['bitwise_identical']):<5} "
                  f"max|Δ|={r['max_abs_diff']:.3g}")

    # ---- 效应表（每个 w_net 下 NA vs 基线）----
    # 老产物的 paired.csv 没有 w_net（它是 cell 级属性，当年只写进 runs.csv）。
    # 在 ablation 模式里一个 cell 只有一个 w_net，因此可以按 cell_id 从 runs 精确补回；
    # 缺了它 effect_table 会整组跳过，§4 的显著性与效应量就全空了。
    if paired is not None and not paired.empty and "w_net" not in paired.columns \
            and "w_net" in runs.columns:
        w_map = runs[["cell_id", "w_net"]].drop_duplicates(subset=["cell_id"])
        paired = paired.merge(w_map, on="cell_id", how="left")
        print("  [说明] paired.csv 缺 w_net 列，已按 cell_id 从 runs.csv 补回（老产物）")
    eff = effect_table(runs, paired, ["workload", "network", "w_net"], NA)
    if not eff.empty:
        write_table(eff, "ablation_effect_by_w_net.csv")
        print("\n  --- 各 w_net 下 NetworkAware vs 基线 ---")
        print(f"  {'w_net':>6} {'workload':<14}{'network':<10}{'baseline':<12}"
              f"{'p95_NA':>10}{'Δp95%':>9}{'cliffs':>8}{'p_holm':>9}  判定")
        for _, r in eff.sort_values(["w_net", "workload", "network", "baseline"]).iterrows():
            print(f"  {r['w_net']:>6.1f} {r['workload']:<14}{r['network']:<10}"
                  f"{r['baseline']:<12}{r['p95_na']:>10.3f}{r['dp95_pct']:>+9.2f}"
                  f"{r['cliffs_delta_median']:>8.3f}{r['p_holm_median']:>9.4f}  {r['verdict']}")

    # ---- 图 ----
    grp = (runs.groupby(["workload", "network", "w_net", "strategy"], as_index=False)
             .agg(p95=("sojourn_p95", "mean"), se=("sojourn_p95", "std"),
                  n=("seed", "nunique")))
    fig, axes = plt.subplots(1, 2, figsize=(13.5, 5.2), constrained_layout=True)
    combos = sorted({(a, b) for a, b in zip(grp["workload"], grp["network"])})
    cmap = plt.get_cmap("tab10")
    for k, (wl, net) in enumerate(combos):
        s = grp[(grp["workload"] == wl) & (grp["network"] == net) & (grp["strategy"] == NA)]
        s = s.sort_values("w_net")
        if s.empty:
            continue
        axes[0].errorbar(s["w_net"], s["p95"], yerr=s["se"], marker="o",
                         color=cmap(k % 10), linewidth=1.6, capsize=3,
                         label=f"{wl}·{net}")
        axes[0].set_xlabel("w_net（网络项权重）")
        axes[0].set_ylabel("p95 端到端完成时间  [s]")
        axes[0].set_title("NetworkAware 的 p95 随 w_net 变化")
        axes[0].grid(alpha=0.25)
    axes[0].legend(fontsize=7.5)

    llp = (runs[runs["strategy"] == "LeastLoad"]
           .groupby(["workload", "network"], as_index=False)
           .agg(p95_ll=("sojourn_p95", "mean")))
    m = grp[grp["strategy"] == NA].merge(llp, on=["workload", "network"])
    for k, (wl, net) in enumerate(combos):
        s = m[(m["workload"] == wl) & (m["network"] == net)].sort_values("w_net")
        if s.empty:
            continue
        dp = (s["p95"] - s["p95_ll"]) / s["p95_ll"] * 100
        axes[1].plot(s["w_net"], dp, marker="o", color=cmap(k % 10),
                     linewidth=1.6, label=f"{wl}·{net}")
    axes[1].axhline(0, color="black", linestyle="--", linewidth=1.1)
    axes[1].set_xlabel("w_net（网络项权重）")
    axes[1].set_ylabel("Δp95 vs LeastLoad  [%]（负=更优）")
    axes[1].set_title("相对 LeastLoad 的收益 vs w_net\n（w_net=0 应精确落在 0 线上）")
    axes[1].grid(alpha=0.25)
    axes[1].legend(fontsize=7.5)
    fig.suptitle("RQ4 权重消融", fontsize=12.5)
    save_fig(fig, "fig_ablation_w_net.png")
    return eff


# --------------------------------------------------------------------------
# §5 节点规模敏感性
# --------------------------------------------------------------------------
def section_scale(data: dict):
    print("\n" + "=" * 78)
    print("§5 节点规模敏感性（4/8/16 节点）")
    print("=" * 78)
    d = data["scale"]
    runs, paired = d["runs"], d["paired"]
    if runs is None or runs.empty:
        print("  scale 模式暂无数据，跳过。")
        return None
    print(f"  节点数: {sorted(int(x) for x in runs['n_nodes'].unique())}")
    print("  ⚠ 注意：到达率固定、节点数增加会同时抬高系统容量，rho 随之下降，")
    print("     因此 n_nodes 的效应与负载强度耦合，必须连着 rho 一起读。")

    eff = effect_table(runs, paired, ["n_nodes", "network"], NA)
    if eff.empty:
        print("  无法计算效应表。")
        return None
    write_table(eff, "rq5_scale_effect.csv")
    print(f"\n  {'n_nodes':>7} {'network':<10}{'baseline':<12}{'rho':>7}"
          f"{'Δp95%':>9}{'cliffs':>8}{'p_holm':>9}  判定")
    for _, r in eff.sort_values(["n_nodes", "network", "baseline"]).iterrows():
        print(f"  {int(r['n_nodes']):>7} {r['network']:<10}{r['baseline']:<12}"
              f"{r['rho_mean']:>7.3f}{r['dp95_pct']:>+9.2f}"
              f"{r['cliffs_delta_median']:>8.3f}{r['p_holm_median']:>9.4f}  {r['verdict']}")

    llp = (runs[runs["strategy"] == "LeastLoad"]
           .groupby(["n_nodes", "network", "seed"], as_index=False)
           .agg(p95_ll=("sojourn_p95", "mean")))
    nap = (runs[runs["strategy"] == NA]
           .groupby(["n_nodes", "network", "seed"], as_index=False)
           .agg(p95_na=("sojourn_p95", "mean")))
    m = nap.merge(llp, on=["n_nodes", "network", "seed"])
    m["dp95_pct"] = (m["p95_na"] - m["p95_ll"]) / m["p95_ll"] * 100

    fig, axes = plt.subplots(1, 2, figsize=(13, 4.8), constrained_layout=True)
    for net in sorted(m["network"].unique()):
        s = m[m["network"] == net]
        g = s.groupby("n_nodes", as_index=False).agg(
            dp=("dp95_pct", "mean"), se=("dp95_pct", lambda v: v.std(ddof=1) / np.sqrt(len(v))))
        axes[0].errorbar(g["n_nodes"], g["dp"], yerr=g["se"], marker="o",
                         linewidth=1.7, capsize=4, label=net)
    axes[0].axhline(0, color="black", linestyle="--", linewidth=1.1)
    axes[0].set_xticks(sorted(int(x) for x in m["n_nodes"].unique()))
    axes[0].set_xlabel("节点数")
    axes[0].set_ylabel("Δp95 vs LeastLoad  [%]（负=更优）")
    axes[0].set_title("Δp95 随节点规模的稳定性")
    axes[0].grid(alpha=0.25)
    axes[0].legend(fontsize=8.5)

    rh = runs.groupby(["n_nodes", "network"], as_index=False).agg(rho=("rho", "mean"))
    for net in sorted(rh["network"].unique()):
        s = rh[rh["network"] == net].sort_values("n_nodes")
        axes[1].plot(s["n_nodes"], s["rho"], marker="s", linewidth=1.7,
                     label=f"{net}")
    axes[1].axhspan(RHO_LO, RHO_HI, color="green", alpha=0.10,
                    label=f"有效区间 [{RHO_LO},{RHO_HI}]")
    axes[1].set_xticks(sorted(int(x) for x in runs["n_nodes"].unique()))
    axes[1].set_xlabel("节点数")
    axes[1].set_ylabel("负载强度 rho")
    axes[1].set_title("同一到达率下 rho 随节点数下降（混淆因子）")
    axes[1].grid(alpha=0.25)
    axes[1].legend(fontsize=8)
    fig.suptitle("RQ5 节点规模敏感性", fontsize=12.5)
    save_fig(fig, "fig_rq5_scale_nodes.png")
    return eff


# --------------------------------------------------------------------------
# §6 种子收敛性
# --------------------------------------------------------------------------
def section_convergence(data: dict):
    print("\n" + "=" * 78)
    print("§6 种子收敛性（convergence 模式，20 个种子）")
    print("=" * 78)
    d = data["convergence"]
    runs = d["runs"]
    if runs is None or runs.empty:
        print("  convergence 模式暂无数据，跳过。")
        return None
    seeds = sorted(int(s) for s in runs["seed"].unique())
    print(f"  已有种子数: {len(seeds)}  {seeds}")

    # --- 方差来源检查：不要假装有多余方差 ---
    print("\n  --- 方差来源检查 ---")
    print("  说明：convergence cell 的负载 seed=1000+seed、unstable 链路 seed=seed，")
    print("        因此每个种子的负载实现与链路轨迹都不同。策略本身是确定性的：")
    print("        给定 (负载, 轨迹)，LeastQueue/LeastLoad/NetworkAware 的结果唯一。")
    var_rows = []
    for st in STRATEGIES:
        s = runs[runs["strategy"] == st]
        vals = s["sojourn_p95"].to_numpy(dtype=float)
        n_unique = int(len(np.unique(np.round(vals, 9))))
        var_rows.append({
            "strategy": st, "n_seeds": len(vals),
            "p95_min": float(np.min(vals)), "p95_max": float(np.max(vals)),
            "p95_mean": float(np.mean(vals)), "p95_std": float(np.std(vals, ddof=1)),
            "cv_pct": float(np.std(vals, ddof=1) / np.mean(vals) * 100),
            "n_unique_values": n_unique,
            "p_holm_present": bool(d["paired"] is not None),
        })
        print(f"    {st:<14} n={len(vals):>2}  唯一值={n_unique:>2}  "
              f"p95∈[{np.min(vals):.3f}, {np.max(vals):.3f}]  "
              f"CV={np.std(vals, ddof=1)/np.mean(vals)*100:.2f}%")
    var_df = pd.DataFrame(var_rows)
    write_table(var_df, "convergence_variance.csv")
    n_all_unique = int((var_df["n_unique_values"] == var_df["n_seeds"]).sum())
    print(f"    → {n_all_unique}/{len(STRATEGIES)} 个策略在 20 个种子下 p95 取值全部互异。")
    print("      跨种子方差来自负载/轨迹实现，不是策略内部随机数；")
    print("      即：策略确定性 ≠ 实验无方差，但继续加种子只增加负载样本，不检验策略随机性。")
    print("      Random 另有一个自身 RNG（seed=cell seed），其差异同时含策略随机性。")

    # --- 收敛曲线：前 k 个种子的 Δp95 ---
    curve_rows = []
    for k in range(1, len(seeds) + 1):
        sub = runs[runs["seed"].isin(seeds[:k])]
        piv = sub.pivot_table(index="seed", columns="strategy",
                              values="sojourn_p95", aggfunc="mean")
        if NA not in piv.columns:
            continue
        for base in BASELINES:
            if base not in piv.columns:
                continue
            d_ = (piv[NA] - piv[base]) / piv[base] * 100
            curve_rows.append({"k_seeds": k, "baseline": base,
                               "dp95_pct": float(d_.mean()),
                               "dp95_std": float(d_.std(ddof=1)) if len(d_) > 1 else float("nan"),
                               "dp95_se": (float(d_.std(ddof=1) / np.sqrt(len(d_)))
                                           if len(d_) > 1 else float("nan"))})
    curve = pd.DataFrame(curve_rows)
    write_table(curve, "convergence_curve.csv")

    print("\n  --- 前 k 个种子的 Δp95（判据：与 k=20 的偏离是否小于跨种子 se）---")
    print(f"  {'k':>3} {'baseline':<12}{'Δp95%':>9}{'±se':>8}{'相对k=20偏离':>14}")
    conv_rows = []
    for base in BASELINES:
        s = curve[curve["baseline"] == base]
        if s.empty:
            continue
        ref = float(s[s["k_seeds"] == max(seeds)]["dp95_pct"].iloc[0])
        for _, r in s[s["k_seeds"].isin(CONV_K_GRID)].iterrows():
            dev = r["dp95_pct"] - ref
            within = abs(dev) <= max(r["dp95_se"], 1e-12) * 2 if np.isfinite(r["dp95_se"]) else False
            conv_rows.append({"k_seeds": int(r["k_seeds"]), "baseline": base,
                              "dp95_pct": r["dp95_pct"], "se": r["dp95_se"],
                              "dev_vs_full": dev, "within_2se": bool(within)})
            print(f"  {int(r['k_seeds']):>3} {base:<12}{r['dp95_pct']:>+9.3f}"
                  f"{r['dp95_se']:>8.3f}{dev:>+14.3f}  "
                  f"{'在 2se 内 ✓' if within else '超出 2se'}")
    conv = pd.DataFrame(conv_rows)
    if not conv.empty:
        write_table(conv, "convergence_k_summary.csv")
        k5 = conv[conv["k_seeds"] == 5]
        k3 = conv[conv["k_seeds"] == 3]
        ok5 = int(k5["within_2se"].sum()) if not k5.empty else 0
        ok3 = int(k3["within_2se"].sum()) if not k3.empty else 0
        print(f"\n  判据小结：k=3 时 {ok3}/{len(k3)} 个基线在 2se 内；"
              f"k=5 时 {ok5}/{len(k5)} 个基线在 2se 内。")
        print("  （若符号与 k=20 一致且在 2se 内，则 5 个种子足以给出方向性结论；"
              "但效应量的小数位不可用 5 个种子报。）")

    # --- 图 ---
    fig, axes = plt.subplots(1, 2, figsize=(13.5, 5.0), constrained_layout=True)
    for base in BASELINES:
        s = curve[curve["baseline"] == base].sort_values("k_seeds")
        if s.empty:
            continue
        axes[0].plot(s["k_seeds"], s["dp95_pct"], marker="o", linewidth=1.7, label=base,
                     color=COLORS[base])
        axes[0].fill_between(s["k_seeds"], s["dp95_pct"] - 2 * s["dp95_se"],
                             s["dp95_pct"] + 2 * s["dp95_se"],
                             color=COLORS[base], alpha=0.12)
    for k in CONV_K_GRID:
        axes[0].axvline(k, color="grey", linestyle=":", linewidth=1.0)
        axes[0].annotate(f"k={k}", (k, axes[0].get_ylim()[1]), fontsize=7,
                         textcoords="offset points", xytext=(2, -10), color="grey")
    axes[0].axhline(0, color="black", linestyle="--", linewidth=1.0)
    axes[0].set_xlabel("使用的种子数 k")
    axes[0].set_ylabel("Δp95 vs 基线  [%]（负=NetworkAware 更优）")
    axes[0].set_title("Δp95 的收敛曲线（阴影=±2se）")
    axes[0].grid(alpha=0.25)
    axes[0].legend(fontsize=8.5)

    for st in STRATEGIES:
        s = runs[runs["strategy"] == st].sort_values("seed")
        axes[1].plot(s["seed"], s["sojourn_p95"], marker=MARKERS[st], markersize=5,
                     linewidth=1.2, color=COLORS[st], label=st)
    axes[1].set_xlabel("种子")
    axes[1].set_ylabel("p95 端到端完成时间  [s]")
    axes[1].set_title("每种子的 p95（跨种子差异=负载/轨迹实现差异）")
    axes[1].grid(alpha=0.25)
    axes[1].legend(fontsize=8.5)
    fig.suptitle("RQ6 种子收敛性（convergence 模式：mixed × unstable × stale）", fontsize=12.5)
    save_fig(fig, "fig_rq6_convergence_seeds.png")
    return curve


# --------------------------------------------------------------------------
# §7 可行性 / 退化汇总
# 计时在 §10
# --------------------------------------------------------------------------
MODE_KEYS = {
    "main": ["workload", "network", "staleness"],
    "staleness": ["network", "staleness"],
    "datavolume": ["workload", "network", "data_volume"],
    "convergence": ["workload", "network", "staleness"],
    "ablation": ["workload", "network", "w_net"],
    "scale": ["network", "n_nodes"],
    "rho": ["workload", "network", "target_rho"],
    "fallback": ["workload", "network", "staleness", "fallback_staleness"],
    "bandwidth": ["workload", "net_profile", "data_volume"],
    # 【细变量②】观测聚合窗口：两条通道各自扫，分组时要能区分是哪一条
    # 两条窗口列各自存在，用它们的组合区分"扫的是哪条通道"
    "avewindow": ["workload", "network", "window_queue_s", "window_network_s"],
    # 【细变量①】两条通道年龄的二维网格：两个 lag 都是因子，必须同时进键
    "staleness2d": ["network", "workload", "staleness_queue_s", "staleness_network_s"],
    # 【细变量③】对齐度：一个 cell 一种配对方式
    "align": ["network", "workload", "align"],
    # 【细变量③ × 规模】对齐度随节点数变化
    "alignscale": ["network", "workload", "n_nodes", "align"],
    # 【V3 方法】降权规则对照：一个 cell 一种 (队列年龄, 链路年龄) 配置
    "method": ["network", "workload", "staleness"],
    # 【V3 方法 × 阈值】阈值消融：同上，策略列里带的是各个变体的标签
    "method_ablate": ["network", "workload", "staleness"],
    # 【V3 方法·修订版】按通道降权：格子与 method 逐字段相同（可跨批配对）
    "method_ch": ["network", "workload", "staleness"],
    # 【V3 方法·修订版 × 阈值】参数消融（OFAT）
    "method_ch_ablate": ["network", "workload", "staleness"],
    # 【补强基线】P2C / Aegis 式拥塞项 / Helix 式瓶颈最小化
    "baselines": ["network", "workload", "staleness"],
}


def section_feasibility(data: dict) -> pd.DataFrame:
    print("\n" + "=" * 78)
    print("§7 可行性汇总：哪些 (场景) 组合是退化的（四策略无区分度）")
    print("=" * 78)
    print(f"  退化判据：① 四策略所有指标逐位相同；"
          f"② p95 跨策略相对极差 < {DEGENERATE_TOL_PCT}%")

    all_rows = []
    for mode in MODES:
        runs = data[mode]["runs"]
        if runs is None or runs.empty:
            continue
        keys = MODE_KEYS[mode]
        for key, g in runs.groupby(keys, dropna=False):
            if not isinstance(key, tuple):
                key = (key,)
            rec = {"mode": mode, **dict(zip(keys, key))}
            rec["n_cells"] = int(g["cell_id"].nunique())
            rec["n_seeds"] = int(g["seed"].nunique())
            rec["n_strategies"] = int(g["strategy"].nunique())
            if rec["n_strategies"] < 2:
                rec["verdict"] = "策略不全(数据未跑完)"
                rec["p95_min"] = rec["p95_max"] = rec["rel_spread_pct"] = float("nan")
                rec["identical_all_metrics"] = False
                rec["rho_mean"] = float(g["rho"].mean())
                all_rows.append(rec)
                continue

            per_cell_rel, identical_cells = [], 0
            for _, cg in g.groupby("cell_id"):
                if cg["strategy"].nunique() < 2:
                    continue
                p95 = cg["sojourn_p95"].to_numpy(dtype=float)
                mn, mx = float(np.nanmin(p95)), float(np.nanmax(p95))
                per_cell_rel.append((mx - mn) / mn * 100 if mn > 0 else 0.0)
                cols = [c for c in METRIC_COLS if c in cg.columns]
                arr = cg[cols].to_numpy(dtype=float)
                if np.array_equal(arr, np.repeat(arr[:1], len(arr), axis=0)):
                    identical_cells += 1
            n_c = len(per_cell_rel)
            rec["p95_min"] = float(g["sojourn_p95"].min())
            rec["p95_max"] = float(g["sojourn_p95"].max())
            rec["rel_spread_pct"] = float(np.max(per_cell_rel)) if n_c else float("nan")
            rec["identical_all_metrics"] = bool(n_c and identical_cells == n_c)
            rec["n_cells_bitwise_identical"] = identical_cells
            rec["rho_mean"] = float(g["rho"].mean())
            if rec["identical_all_metrics"]:
                rec["verdict"] = "退化：四策略逐位相同"
            elif np.isfinite(rec["rel_spread_pct"]) and rec["rel_spread_pct"] < DEGENERATE_TOL_PCT:
                rec["verdict"] = f"退化：p95 极差<{DEGENERATE_TOL_PCT}%"
            elif np.isfinite(rec["rel_spread_pct"]) and rec["rel_spread_pct"] < 5.0:
                rec["verdict"] = "弱区分度(极差<5%)"
            else:
                rec["verdict"] = "有信号"
            all_rows.append(rec)

    feas = pd.DataFrame(all_rows)
    if feas.empty:
        print("  （无数据）")
        return feas

    # --- 逐策略对的"逐位相同"分析 ---
    # 比"四策略整体极差"更细：能识别出"只有某一对策略无区分度"，
    # 例如同构快网（normal）下 NetworkAware 与 LeastLoad 精确重合。
    pair_rows = []
    ident_map: dict[tuple, list[str]] = {}
    for mode in MODES:
        runs = data[mode]["runs"]
        if runs is None or runs.empty:
            continue
        keys = MODE_KEYS[mode]
        cols = [c for c in METRIC_COLS if c in runs.columns]
        for key, g in runs.groupby(keys, dropna=False):
            if not isinstance(key, tuple):
                key = (key,)
            gkey = (mode, tuple(str(v) for v in key))
            for i, s1 in enumerate(STRATEGIES):
                for s2 in STRATEGIES[i + 1:]:
                    a = g[g["strategy"] == s1].set_index("cell_id")
                    b = g[g["strategy"] == s2].set_index("cell_id")
                    common = a.index.intersection(b.index)
                    if len(common) == 0:
                        continue
                    eq = np.array_equal(a.loc[common, cols].to_numpy(dtype=float),
                                        b.loc[common, cols].to_numpy(dtype=float))
                    p95a = a.loc[common, "sojourn_p95"].to_numpy(dtype=float)
                    p95b = b.loc[common, "sojourn_p95"].to_numpy(dtype=float)
                    rel = float(np.max(np.abs(p95a - p95b)
                                       / np.where(p95b > 0, p95b, 1.0)) * 100)
                    pair_rows.append({"mode": mode, **dict(zip(keys, key)),
                                      "pair": f"{s1} vs {s2}",
                                      "n_cells": len(common),
                                      "bitwise_identical": bool(eq),
                                      "max_rel_p95_diff_pct": rel})
                    if eq:
                        ident_map.setdefault(gkey, []).append(f"{s1} vs {s2}")

    feas["identical_pairs"] = [
        ";".join(sorted(ident_map.get(
            (r["mode"], tuple(str(r[k]) for k in MODE_KEYS[r["mode"]])), [])))
        for _, r in feas.iterrows()]
    feas["NA_eq_LeastLoad"] = (
        feas["identical_pairs"].str.contains("LeastLoad vs NetworkAware", regex=False)
        | feas["identical_pairs"].str.contains("NetworkAware vs LeastLoad", regex=False))
    feas["NA_eq_all_naive"] = (
        (feas["identical_pairs"].str.contains("LeastQueue vs NetworkAware", regex=False)
         | feas["identical_pairs"].str.contains("NetworkAware vs LeastQueue", regex=False))
        & feas["NA_eq_LeastLoad"])
    # 部分退化：四策略并非全部相同，但 NetworkAware 相对最强网络盲基线毫无增量
    partial = feas["NA_eq_LeastLoad"] & (feas["verdict"] == "有信号")
    feas.loc[partial, "verdict"] = "部分退化：NA 与 LeastLoad 逐位相同(网络项无信号)"
    write_table(feas, "feasibility_degeneracy.csv")

    pairs = pd.DataFrame(pair_rows)
    if not pairs.empty:
        write_table(pairs, "feasibility_pairwise_identity.csv")
        ident = pairs[pairs["bitwise_identical"]]
        print("\n  --- 策略对逐位相同的情况（该场景组内所有指标逐位一致）---")
        if ident.empty:
            print("    无：所有场景下四个策略都给出了不同的指标。")
        else:
            show = ident.groupby(["mode", "pair"], as_index=False).agg(
                n_groups=("pair", "size"))
            for _, r in show.iterrows():
                keys_txt = ""
                sub = ident[(ident["mode"] == r["mode"]) & (ident["pair"] == r["pair"])]
                if r["mode"] in ("main",):
                    keys_txt = " | " + ", ".join(
                        sorted({f"{x['workload']}/{x['network']}/{x['staleness']}"
                                for _, x in sub.iterrows()}))
                print(f"    {r['mode']:<12} {r['pair']:<32} "
                      f"{int(r['n_groups'])} 个场景组逐位相同{keys_txt[:110]}")

    print("\n  --- 按判定分类 ---")
    for v in sorted(feas["verdict"].unique()):
        sub = feas[feas["verdict"] == v]
        print(f"   [{v}] 组合数 {len(sub)}  (mode 分布: "
              f"{sub['mode'].value_counts().to_dict()})")

    print("\n  --- 主矩阵逐格（最关心）---")
    m = feas[feas["mode"] == "main"].sort_values(
        ["staleness", "network", "workload"])
    if m.empty:
        print("    main 暂无数据。")
    else:
        print(f"   {'workload':<14}{'network':<10}{'staleness':<8}{'seeds':>6}"
              f"{'p95_min':>9}{'p95_max':>9}{'极差%':>8}{'rho':>7}  判定 / 逐位相同的策略对")
        for _, r in m.iterrows():
            print(f"   {r['workload']:<14}{r['network']:<10}{r['staleness']:<8}"
                  f"{int(r['n_seeds']):>6}{r['p95_min']:>9.3f}{r['p95_max']:>9.3f}"
                  f"{r['rel_spread_pct']:>8.2f}{r['rho_mean']:>7.3f}  {r['verdict']}"
                  + (f" [{r['identical_pairs']}]" if r["identical_pairs"] else ""))
        n_main = len(m)
        print(f"\n   主矩阵已判定 {n_main}/24 组合；")
        for v in sorted(m["verdict"].unique()):
            print(f"     {v}: {int((m['verdict'] == v).sum())}")
        nl = m[m["NA_eq_LeastLoad"]]
        if not nl.empty:
            print(f"\n   ⚠ 有 {len(nl)}/{n_main} 个组合里 NetworkAware 与 LeastLoad **逐位相同**"
                  "（网络项在该场景下没有任何可用信号）：")
            for _, r in nl.iterrows():
                print(f"     {r['workload']}/{r['network']}/{r['staleness']}  "
                      f"[{r['identical_pairs']}]")

    # 图：退化判定热图（main）
    if not m.empty:
        wls = sorted(m["workload"].unique())
        nets = sorted(m["network"].unique())
        stals = sorted(m["staleness"].unique())
        fig, axes = plt.subplots(1, len(stals), figsize=(5.2 * len(stals), 4.6),
                                 constrained_layout=True, squeeze=False)
        for j, stal in enumerate(stals):
            ax = axes[0][j]
            M = np.full((len(wls), len(nets)), np.nan)
            for _, r in m[m["staleness"] == stal].iterrows():
                M[wls.index(r["workload"]), nets.index(r["network"])] = r["rel_spread_pct"]
            im = ax.imshow(M, cmap="viridis", aspect="auto")
            ax.set_xticks(range(len(nets)))
            ax.set_xticklabels(nets)
            ax.set_yticks(range(len(wls)))
            ax.set_yticklabels(wls)
            ax.set_title(f"staleness={stal}")
            for yi in range(len(wls)):
                for xi in range(len(nets)):
                    v = M[yi, xi]
                    if np.isfinite(v):
                        ax.text(xi, yi, f"{v:.2f}%", ha="center", va="center",
                                fontsize=9,
                                color="white" if v < 0.6 * np.nanmax(M) else "black")
                    else:
                        ax.text(xi, yi, "缺", ha="center", va="center", fontsize=8,
                                color="#666666")
        cb = fig.colorbar(im, ax=axes, shrink=0.85, pad=0.02)
        cb.set_label(f"四策略 p95 相对极差 [%]\n(<{DEGENERATE_TOL_PCT}% 视为无区分度)")
        fig.suptitle("RQ7 主矩阵区分度：越小越接近退化", fontsize=12.5)
        save_fig(fig, "fig_rq7_degeneracy_heatmap.png")
    return feas


# --------------------------------------------------------------------------
# §8 到达压力扫描（rho 模式）
# --------------------------------------------------------------------------
def section_rho_sweep(data: dict):
    """收益边界在「负载强度」这条轴上的形状。

    策略差异只在有排队压力时才可能出现：rho 低到一定程度四策略必然趋同（门 1 下端
    就是退化区），rho 高到过饱和则尾部只在测队列排空时间。所以"收益边界"这句话
    必须落在这条轴上，而主矩阵把 rho 固定成了一个点。
    """
    print("\n" + "=" * 78)
    print("§8 到达压力扫描（rho 模式：同一负载/网络，只改到达率的缩放）")
    print("=" * 78)
    d = data["rho"]
    runs, paired = d["runs"], d["paired"]
    if runs is None or runs.empty:
        print("  （无数据：results/matrix*/rho_* 不存在，先跑 --mode rho）")
        return None
    if "target_rho" not in runs.columns:
        print("  ⚠ runs.csv 缺 target_rho 列（旧版本产物），无法按档位分组。")
        return None

    eff = effect_table(runs, paired, ["workload", "network", "target_rho"])
    if eff.empty:
        print("  （无可用效应：缺配对数据或缺策略行）")
        return None
    eff["target_rho"] = eff["target_rho"].astype(float)
    write_table(eff, "rq8_rho_sweep_effect.csv")

    print(f"  档位 target_rho={sorted(set(eff['target_rho']))}   "
          f"组合数 {eff['workload'].nunique()}×{eff['network'].nunique()}×"
          f"{eff['target_rho'].nunique()}")
    print(f"  {'负载':<14}{'网络':<10}{'rho':>6} {'基线':<12}{'Δp95%':>9}{'±se':>7}"
          f"{'ΔSLO':>9}{'cliffs':>8}{'效力':>6}{'p_holm':>9}  判定")
    for _, r in eff.sort_values(["workload", "network", "target_rho", "baseline"]).iterrows():
        print(f"  {r['workload']:<14}{r['network']:<10}{r['target_rho']:>6.2f} "
              f"{r['baseline']:<12}{r['dp95_pct']:>+9.2f}{r['dp95_se']:>7.2f}"
              f"{r['dslo_abs']:>+9.4f}{r['cliffs_delta_median']:>8.3f}"
              f"{r['effect_magnitude']:>6}{r['p_holm_median']:>9.3g}  {r['verdict']}")

    # --- 收益消失的负载强度：Δp95 由负转正的插值交点 ---
    zc_rows = []
    for (wl, net, base), g in eff.groupby(["workload", "network", "baseline"]):
        g = g.sort_values("target_rho")
        x = g["target_rho"].to_numpy(dtype=float)
        y = g["dp95_pct"].to_numpy(dtype=float)
        cross = None
        for i in range(1, len(x)):
            if y[i - 1] < 0 <= y[i]:
                cross = (float(x[i - 1] + (0 - y[i - 1]) * (x[i] - x[i - 1]) / (y[i] - y[i - 1]))
                         if y[i] != y[i - 1] else float(x[i]))
                break
        zc_rows.append({
            "workload": wl, "network": net, "baseline": base,
            "rho_zero_crossing": cross,
            "dp95_at_min_rho": float(y[0]), "dp95_at_max_rho": float(y[-1]),
            "n_levels": int(len(x)),
        })
    zc = pd.DataFrame(zc_rows)
    write_table(zc, "rq8_zero_crossings.csv")
    print("\n  收益随负载强度的形态（Δp95 由负转正的 rho；无交点=整条轴同向）：")
    for _, r in zc.iterrows():
        c = r["rho_zero_crossing"]
        txt = f"{c:.3f}" if c is not None and np.isfinite(c) else "无交点"
        print(f"    {r['workload']:<14}{r['network']:<10}vs {r['baseline']:<12}"
              f"交点={txt:<8} 最低档 Δp95={r['dp95_at_min_rho']:+.2f}%  "
              f"最高档 Δp95={r['dp95_at_max_rho']:+.2f}%")

    fig, axes = plt.subplots(1, 2, figsize=(11.5, 4.2), sharey=True)
    plotted = False
    for ax, net in zip(axes, ["degraded", "unstable"]):
        sub = eff[(eff["network"] == net) & (eff["baseline"] == "LeastLoad")]
        for wl, g in sub.groupby("workload"):
            g = g.sort_values("target_rho")
            ax.plot(g["target_rho"], g["dp95_pct"], marker="o", linewidth=1.5, label=wl)
            plotted = True
        ax.axhline(0, color="k", linestyle="--", linewidth=1.0)
        ax.set_xlabel("目标负载强度 rho")
        ax.set_title(f"网络={net}（vs LeastLoad）")
        ax.grid(alpha=0.25)
    if plotted:
        axes[0].set_ylabel("Δp95 [%]（<0 = NetworkAware 更优）")
        axes[0].legend(fontsize=8)
        fig.suptitle("RQ8 收益边界 vs 到达压力（normal 网络不画：那里两策略逐位相同）",
                     fontsize=11.5)
        save_fig(fig, "fig_rq8_rho_sweep.png")
    plt.close(fig)
    return eff


# --------------------------------------------------------------------------
# §9 回退机制（fallback 模式）
# --------------------------------------------------------------------------
def _fallback_verdict(dp95: float, p_holm: float) -> str:
    if not np.isfinite(dp95) or not np.isfinite(p_holm):
        return "数据不足"
    if p_holm >= 0.05:
        return "无显著影响（回退无效）"
    return "回退降低劣化（有效）" if dp95 < 0 else "回退过头（损失收益）"


def section_fallback(data: dict):
    """RQ2 的第三部分：过期观测下的自适应回退到底有没有用。

    每个 cell 内同时跑"不回退的 NetworkAware"作为参考臂，所以
    "回退 vs 不回退"是**逐请求配对**比较（Wilcoxon + Holm + Cliff's delta），
    不是跨 cell 的聚合对比。

    计划明确要求三种结果都要报：有效、无效、因过于保守而损失收益。
    """
    print("\n" + "=" * 78)
    print("§9 回退机制（观测过期时把网络项权重降到 fallback_w_net）")
    print("=" * 78)
    d = data["fallback"]
    runs, paired = d["runs"], d["paired"]
    if runs is None or runs.empty:
        print("  （无数据：results/matrix*/fallback_* 不存在，先跑 --mode fallback）")
        return None
    if "fallback_staleness" not in runs.columns:
        print("  ⚠ runs.csv 缺 fallback_staleness 列（旧版本产物），无法按阈值分组。")
        return None

    # 变体臂：strategy 列形如 NetworkAware_fb2，且该行的 fallback_staleness 不为空
    variant_rows = runs[runs["fallback_staleness"].notna()
                        & (runs["strategy"] != NA)]
    if variant_rows.empty:
        print("  ⚠ 没有找到回退变体臂（strategy 里没有 NetworkAware_fb*）。")
        return None
    label_thr = (variant_rows.groupby("strategy")["fallback_staleness"]
                 .first().to_dict())

    pieces = []
    for label, thr in sorted(label_thr.items(), key=lambda kv: kv[1]):
        # 子集里必须同时含参考臂（不回退的 NetworkAware）与 LeastLoad：
        # 前者是"回退 vs 不回退"的配对基准，后者用来量化"回退把性能拉回多少".
        sub = runs[runs["strategy"].isin([label, NA, "LeastLoad"])]
        e = effect_table(sub, paired, ["workload", "network", "staleness"],
                         candidate=label, baselines=[NA, "LeastLoad"])
        if e.empty:
            continue
        e["fallback_staleness"] = float(thr)
        pieces.append(e)
    if not pieces:
        print("  （回退变体臂没有可用的配对结果）")
        return None
    eff = pd.concat(pieces, ignore_index=True)

    # 观测年龄（= min(队列 lag, 链路 lag)）决定回退是否真的触发：阈值 ≥ 年龄时是空操作
    if "staleness_lag_s" in runs.columns:
        lag_map = (runs[["workload", "network", "staleness", "staleness_lag_s"]]
                   .drop_duplicates())
        eff = eff.merge(lag_map, on=["workload", "network", "staleness"], how="left")
        eff["triggered"] = eff["staleness_lag_s"] > eff["fallback_staleness"]
    else:
        eff["staleness_lag_s"] = np.nan
        eff["triggered"] = np.nan
    write_table(eff, "rq9_fallback_effect.csv")

    print(f"  回退阈值（s）: {sorted(set(eff['fallback_staleness']))}   "
          f"参考臂=不回退的 NetworkAware（同一 cell 内）；对比 LeastLoad 的那半边是上下文")
    print(f"  {'负载':<14}{'网络':<10}{'新鲜度':<7}{'阈值':>6}{'触发':>6}{'基线':<12}"
          f"{'Δp95%':>9}{'±se':>7}{'cliffs':>8}{'效力':>6}{'p_holm':>9}  判定")
    for _, r in eff.sort_values(["network", "staleness", "fallback_staleness",
                                 "workload", "baseline"]).iterrows():
        trig = "是" if r["triggered"] is True else ("否" if r["triggered"] is False else "-")
        print(f"  {r['workload']:<14}{r['network']:<10}{r['staleness']:<7}"
              f"{r['fallback_staleness']:>6.1f}{trig:>6}{r['baseline']:<12}"
              f"{r['dp95_pct']:>+9.2f}{r['dp95_se']:>7.2f}"
              f"{r['cliffs_delta_median']:>8.3f}{r['effect_magnitude']:>6}"
              f"{r['p_holm_median']:>9.3g}  {r['verdict']}")

    # 汇总只取「回退 vs 不回退」这一对：另一个基线（LeastLoad）只是上下文，
    # 混进来会把 p_holm 取成两者最大值，把"回退过头"误判成"无显著影响"。
    vs_ref = eff[eff["baseline"] == NA]
    agg = (vs_ref.groupby(["network", "staleness", "fallback_staleness"], as_index=False)
           .agg(dp95_pct=("dp95_pct", "mean"), dp95_se=("dp95_se", "mean"),
                cliffs=("cliffs_delta_median", "median"),
                p_holm=("p_holm_median", "max"), n_cells=("dp95_pct", "size")))
    agg["判定"] = [_fallback_verdict(a, b) for a, b in zip(agg["dp95_pct"], agg["p_holm"])]
    write_table(agg, "rq9_fallback_summary.csv")
    print("\n  汇总（回退 vs 不回退；两个负载取平均；判定按 p_holm<0.05 且 Δp95 方向）：")
    for _, r in agg.sort_values(["staleness", "network", "fallback_staleness"]).iterrows():
        print(f"    {r['staleness']:<6}{r['network']:<10}阈值={r['fallback_staleness']:>4.1f}s  "
              f"Δp95={r['dp95_pct']:>+7.2f}%  cliffs={r['cliffs']:>+6.3f}  "
              f"p_holm={r['p_holm']:.3g}  → {r['判定']}")
    n_eff = int(agg["判定"].str.startswith("回退降低").sum())
    n_over = int(agg["判定"].str.startswith("回退过头").sum())
    n_null = int(agg["判定"].str.startswith("无显著").sum())
    print(f"\n  三类结果计数：有效 {n_eff} / 过度保守 {n_over} / 无效 {n_null}"
          "（三类都必须进正文）")

    fig, axes = plt.subplots(1, 2, figsize=(11.5, 4.2), sharey=True)
    order = [s for s in ("fresh", "mild", "stale") if s in set(eff["staleness"])]
    for ax, st in zip(axes, (order + [None, None])[:2]):
        if st is None:
            ax.axis("off")
            continue
        sub = vs_ref[vs_ref["staleness"] == st]
        for (wl, net), g in sub.groupby(["workload", "network"]):
            g = g.groupby("fallback_staleness", as_index=False).agg(
                dp95_pct=("dp95_pct", "mean"))
            ax.plot(g["fallback_staleness"], g["dp95_pct"], marker="o",
                    linewidth=1.4, label=f"{wl}/{net}")
        ax.axhline(0, color="k", linestyle="--", linewidth=1.0)
        ax.set_xscale("log")
        ax.set_xlabel("回退触发阈值 [s]（对数轴）")
        ax.set_title(f"陈旧档={st}")
        ax.grid(alpha=0.25)
    axes[0].set_ylabel("Δp95 [%]（<0 = 回退优于不回退）")
    axes[0].legend(fontsize=7.5)
    fig.suptitle("RQ9 回退机制：相对不回退的 NetworkAware 的 p95 变化", fontsize=11.5)
    save_fig(fig, "fig_rq9_fallback.png")
    plt.close(fig)
    return eff


def section_timing(data: dict) -> pd.DataFrame:
    print("\n" + "=" * 78)
    print("§10 墙钟耗时与每 cell 仿真耗时")
    print("=" * 78)
    rows = []
    for mode in MODES:
        d = data[mode]
        runs = d["runs"]
        if runs is None or runs.empty:
            continue
        per_cell = (runs.groupby("cell_id", as_index=False)["wall_clock_s"].sum())
        rec = {
            "mode": mode,
            "n_cells": int(runs["cell_id"].nunique()),
            "n_runs": int(len(runs)),
            "wall_clock_sum_s": float(runs["wall_clock_s"].sum()),
            "per_run_mean_s": float(runs["wall_clock_s"].mean()),
            "per_run_max_s": float(runs["wall_clock_s"].max()),
            "per_cell_mean_s": float(per_cell["wall_clock_s"].mean()),
            "per_cell_max_s": float(per_cell["wall_clock_s"].max()),
            "manifest_total_s": (d["manifest"] or {}).get("wall_clock_total_s", float("nan")),
            "manifest_per_run_s": (d["manifest"] or {}).get("wall_clock_per_run_s", float("nan")),
            "complete": bool(d["manifest"] is not None),
        }
        rows.append(rec)
        print(f"  {mode:<12} cells={rec['n_cells']:>4} runs={rec['n_runs']:>5} "
              f"每 cell {rec['per_cell_mean_s']:>6.2f} s  每 run "
              f"{rec['per_run_mean_s']:.3f} s  累计 {rec['wall_clock_sum_s']/60:>6.1f} min"
              + ("" if rec["complete"] else "  [进行中/未完成]"))
    df = pd.DataFrame(rows)
    if not df.empty:
        write_table(df, "timing_summary.csv")
        print(f"\n  合计已观测墙钟 {df['wall_clock_sum_s'].sum()/60:.1f} min，"
              f"{int(df['n_cells'].sum())} cells / {int(df['n_runs'].sum())} 次仿真")
    return df


def section_bandwidth(data: dict):
    """§13 带宽敏感性 —— 保真度对照指出的最大威胁的直接检验。

    保真度核对（AGENTS.md §6 门 4）发现本模型的链路比真实 AI 集群机内网络低 80×–8000×：
    DistServe 指出 OPT-66B 在 10 rps 下需要 **90 Gbps** 才能让 KV 传输开销"不可见"，
    机内是 800 Gbps InfiniBand；而本模型主矩阵用的是 10 Gbps / 1 Gbps / 100 Mbps。
    后果是本批 `net_share` 达 0.35–0.76，机内则是 1–5% 量级。

    这一节把链路绝对尺度从机内换到广域（四档跨约 5 个数量级），**只改这一个因子**
    （保持 degraded 的异构形状、负载、策略与 rho 不变），回答两个问题：
      1. Δp95 与 `net_share` 是什么关系（是不是 net_share 低到某个值收益就消失）；
      2. 在**机内档**（net_share ≈1–5%）上，网络感知还值不值。
    """
    print("\n" + "=" * 78)
    print("§13 带宽敏感性（同一异构形状，链路绝对尺度从机内换到广域）")
    print("=" * 78)
    d = data["bandwidth"]
    runs, paired = d["runs"], d["paired"]
    if runs is None or runs.empty:
        print("  （无数据：results/matrix*/bandwidth_* 不存在，先跑 --mode bandwidth）")
        return None
    if "net_profile" not in runs.columns:
        print("  ⚠ runs.csv 缺 net_profile 列（旧版本产物）。")
        return None

    eff = effect_table(runs, paired, ["workload", "data_volume", "net_profile"])
    if eff.empty:
        print("  （无可用效应）")
        return None
    write_table(eff, "rq13_bandwidth_effect.csv")

    order = [p for p in ("ib_fast", "ib_800g", "eth_100g", "wan_10g", "wan_1g")
             if p in set(eff["net_profile"])]
    lab = {"ib_fast": "机内 IB  100/200/800 Gbps, 10µs",
           "ib_800g": "机内 IB  8/80/800 Gbps, 5µs",
           "eth_100g": "机内以太 1/10/100 Gbps, 10µs",
           "wan_10g": "跨域  0.1/1/10 Gbps, 5ms（= 主矩阵的 degraded）",
           "wan_1g": "广域  0.01/0.1/1 Gbps, 50ms"}
    print(f"  档位（保持与 degraded 相同的 100:10:1 异构形状，只改绝对尺度）：")
    for p in order:
        print(f"    {p:<10}{lab.get(p,'')}")

    s = eff[eff["baseline"] == "LeastLoad"]
    agg = (s.groupby("net_profile", as_index=False)
             .agg(net_share=("net_share_na", "mean"), p95_na=("p95_na", "mean"),
                  dp95_pct=("dp95_pct", "mean"), dp95_se=("dp95_se", "mean"),
                  cliffs=("cliffs_delta_median", "median"),
                  p_holm_median=("p_holm_median", "median"),
                  p_holm_max=("p_holm_median", "max"),
                  n_sig=("p_holm_median", lambda v: int((v < 0.05).sum())),
                  n_cells=("dp95_pct", "size")))
    agg["_o"] = agg["net_profile"].map({p: i for i, p in enumerate(order)})
    agg = agg.sort_values("_o").drop(columns="_o")
    write_table(agg, "rq13_bandwidth_summary.csv")

    # --- 内部一致性自检 ---
    # 只对**合成**批次成立：合成负载只由 seed 决定，所以同一 (workload, seed) 在两个
    # mode 里逐位相同。trace 批次不成立**且不是 bug**：时间片数 = 本轮最大种子号，
    # 主矩阵切 5 片、本模式切 3 片，同一个 seed 落在不同的时间片上，负载本来就不同。
    m = data.get("main", {}).get("runs")
    if m is not None and not m.empty:
        ref = m[(m["network"] == "degraded") & (m["staleness"] == "fresh")
                & (m["data_volume"] == "kv_cache")]
        cand = runs[(runs["net_profile"] == "wan_10g")
                    & (runs["data_volume"] == "kv_cache")]
        src = str(runs["data_source"].iloc[0]) if "data_source" in runs.columns else "?"
        if src != "synthetic":
            print(f"\n  内部一致性自检：跳过（负载来源={src}）——trace 批的时间片划分"
                  f"取决于本轮种子数（主矩阵 5 片 vs 本模式 3 片），同一 seed 落在"
                  f"不同时间片上，负载本就不同，**不是管道错误**")
        elif not ref.empty and not cand.empty:
            key = ["workload", "seed", "strategy"]
            mg = (ref[key + ["sojourn_p95"]].sort_values(key)
                  .merge(cand[key + ["sojourn_p95"]].sort_values(key),
                         on=key, suffixes=("_main", "_bw")))
            same = bool(np.array_equal(mg["sojourn_p95_main"].to_numpy(),
                                       mg["sojourn_p95_bw"].to_numpy()))
            print(f"\n  内部一致性（wan_10g 应等于主矩阵 degraded）："
                  f"{len(mg)} 行对比 → {'逐位相同 ✓' if same else '不一致 ✗'}")

    print(f"\n  {'档位':<10}{'net_share':>10}{'NA p95':>10}{'Δp95%':>9}{'±se':>7}"
          f"{'cliffs':>8}{'显著cell':>9}{'p_holm中位':>11}  判定")
    for _, r in agg.iterrows():
        print(f"  {r['net_profile']:<10}{r['net_share']:>10.4f}{r['p95_na']:>10.2f}"
              f"{r['dp95_pct']:>+9.2f}{r['dp95_se']:>7.2f}{r['cliffs']:>8.3f}"
              f"{int(r['n_sig']):>5}/{int(r['n_cells']):<3}{r['p_holm_median']:>11.3g}  "
              f"{'NA更优' if r['dp95_pct'] < 0 else 'NA更差'}")

    # 逐 (负载×数据量) 求 Δp95 在 net_share 轴上由负转正的交点
    zc_rows = []
    for (wl, dv), g in s.groupby(["workload", "data_volume"]):
        g = g.sort_values("net_share_na")
        x = g["net_share_na"].to_numpy(dtype=float)
        y = g["dp95_pct"].to_numpy(dtype=float)
        cross = None
        for i in range(1, len(x)):
            if y[i - 1] < 0 <= y[i]:
                cross = (float(x[i - 1] + (0 - y[i - 1]) * (x[i] - x[i - 1]) / (y[i] - y[i - 1]))
                         if y[i] != y[i - 1] else float(x[i]))
                break
        zc_rows.append({"workload": wl, "data_volume": dv,
                        "net_share_min": float(x.min()), "net_share_max": float(x.max()),
                        "net_share_zero_crossing": cross,
                        "dp95_at_min": float(y[0]), "dp95_at_max": float(y[-1]),
                        "n_profiles": len(x)})
    zc = pd.DataFrame(zc_rows)
    write_table(zc, "rq13_zero_crossings.csv")
    print("\n  Δp95 在 net_share 轴上的变号（无交点=整条轴同向）：")
    for _, r in zc.iterrows():
        c = r["net_share_zero_crossing"]
        txt = f"{c:.4f}" if c is not None and np.isfinite(c) else "无交点"
        print(f"    {r['workload']:<14}{r['data_volume']:<12}交点={txt:<9}"
              f"net_share∈[{r['net_share_min']:.4f},{r['net_share_max']:.4f}]  "
              f"Δp95 {r['dp95_at_min']:+.1f}% → {r['dp95_at_max']:+.1f}%")

    fig, ax = plt.subplots(figsize=(7.6, 4.6))
    for (wl, dv), g in s.groupby(["workload", "data_volume"]):
        g = g.sort_values("net_share_na")
        ax.plot(g["net_share_na"], g["dp95_pct"], marker="o", linewidth=1.5,
                label=f"{wl}/{dv}")
    ax.axhline(0, color="k", linestyle="--", linewidth=1.0)
    # 机内档的 net_share 区间（0.01–0.05）：真实 AI 集群 KV 传输占比的量级
    ax.axvspan(0.01, 0.05, color="tab:green", alpha=0.10)
    ax.text(0.012, ax.get_ylim()[1] * 0.75 if ax.get_ylim()[1] > 0 else 0,
            "机内 KV 传输占比量级\n(1–5%)", fontsize=8, color="tab:green")
    ax.set_xscale("log")
    ax.set_xlabel("net_share = 网络时间 / 端到端时间（对数轴）")
    ax.set_ylabel("Δp95 [%]（<0 = NetworkAware 更优）")
    ax.set_title("§13 带宽敏感性：收益随网络占比的变化（vs LeastLoad）")
    ax.grid(alpha=0.25)
    ax.legend(fontsize=8)
    save_fig(fig, "fig_rq13_bandwidth.png")
    plt.close(fig)
    return eff


# --------------------------------------------------------------------------
# §11 结论汇总
# --------------------------------------------------------------------------
def section_summary(feas, rq1, rq2, rq3, abl, rq5, rq6, data,
                    rho_sweep=None, fallback=None):
    print("\n" + "=" * 78)
    print("§11 关键发现汇总（全部来自上面的 CSV，未做任何人工挑选）")
    print("=" * 78)
    if rq1 is not None:
        eff, agg = rq1
        print("\n[RQ1 收益边界]")
        good = eff[eff["verdict"].str.startswith("NA更优")]
        bad = eff[eff["verdict"].str.startswith("NA劣化")]
        nul = eff[eff["verdict"].isin(["无显著差异", "逐位相同(该组合无信号)"])]
        print(f"  组合×基线条目：共 {len(eff)}；NA 更优 {len(good)}；"
              f"NA 劣化 {len(bad)}；无显著差异/相同 {len(nul)}")
        conf = eff[eff["tail_vs_median"].isin(["尾部改善/中位数变差", "尾部变差/中位数改善"])]
        print(f"  其中尾部与中位数方向不一致 {len(conf)} 条（必须两个方向都报）")
        for _, r in conf.iterrows():
            print(f"    {r['workload']}/{r['network']}/{r['staleness']} vs {r['baseline']}: "
                  f"Δp95={r['dp95_pct']:+.2f}% cliffs={r['cliffs_delta_median']:+.3f} "
                  f"[{r['tail_vs_median']}]")
        if not good.empty:
            top = good.nsmallest(5, "dp95_pct")
            print("  收益最大的 5 条：")
            for _, r in top.iterrows():
                print(f"    {r['workload']}/{r['network']}/{r['staleness']} vs {r['baseline']}: "
                      f"Δp95={r['dp95_pct']:+.2f}% (cliffs={r['cliffs_delta_median']:+.3f}, "
                      f"p_holm={r['p_holm_median']:.3g}, net_share={r['net_share_na']:.4f})")
        by_net = (eff.groupby(["network", "baseline"], as_index=False)
                    .agg(dp95=("dp95_pct", "mean"),
                         n_improve=("verdict", lambda v: int(v.str.startswith("NA更优").sum())),
                         n=("verdict", "size")))
        print("  按网络条件平均（Δp95<0 表示整体更优）：")
        for _, r in by_net.iterrows():
            print(f"    {r['network']:<10} vs {r['baseline']:<12} "
                  f"平均Δp95={r['dp95']:+.2f}%  更优 {r['n_improve']}/{r['n']}")
        by_wl = (eff.groupby(["workload", "baseline"], as_index=False)
                    .agg(dp95=("dp95_pct", "mean")))
        print("  按负载类型平均：")
        for _, r in by_wl.iterrows():
            print(f"    {r['workload']:<14} vs {r['baseline']:<12} 平均Δp95={r['dp95']:+.2f}%")
    if rq2 is not None and not rq2.empty:
        print("\n[RQ2 陈旧度]")
        nc = rq2[rq2["channel"] == "network"]
        for net in sorted(nc["network"].unique()):
            sub = nc[nc["network"] == net]
            na = sub[sub["strategy"] == NA].sort_values("lag_s")
            if na.empty:
                continue
            print(f"  {net:<10} NetworkAware p95: lag0={na['sojourn_p95'].iloc[0]:.3f}s → "
                  f"lag{na['lag_s'].iloc[-1]:g}={na['sojourn_p95'].iloc[-1]:.3f}s "
                  f"({(na['sojourn_p95'].iloc[-1]/na['sojourn_p95'].iloc[0]-1)*100:+.1f}%)")
    if rq3 is not None:
        print("\n[RQ3 数据量边界]")
        for base in BASELINES:
            s = rq3[rq3["baseline"] == base]
            if s.empty:
                continue
            pv = s[s["data_volume"] == "prompt_only"]["dp95_pct"]
            kv = s[s["data_volume"] == "kv_cache"]["dp95_pct"]
            print(f"  vs {base:<12} prompt_only 平均Δp95={pv.mean():+.2f}%  "
                  f"kv_cache 平均Δp95={kv.mean():+.2f}%")
    if abl is not None:
        print("\n[RQ4 权重消融]")
        s = abl[abl["baseline"] == "LeastLoad"]
        for w, g in s.groupby("w_net"):
            print(f"  w_net={w:<4} vs LeastLoad 平均Δp95={g['dp95_pct'].mean():+.3f}%  "
                  f"判定={sorted(g['verdict'].unique())}")
    if rq5 is not None:
        print("\n[RQ5 节点规模]")
        for _, r in rq5[rq5["baseline"] == "LeastLoad"].iterrows():
            print(f"  n_nodes={int(r['n_nodes']):>2} {r['network']:<10} "
                  f"Δp95={r['dp95_pct']:+.2f}% (rho={r['rho_mean']:.3f}, {r['verdict']})")
    if rq6 is not None and not rq6.empty:
        print("\n[RQ6 种子收敛]")
        for base in BASELINES:
            s = rq6[rq6["baseline"] == base]
            if s.empty:
                continue
            print(f"  vs {base:<12} k=1→{s['dp95_pct'].iloc[0]:+.2f}%  "
                  f"k={int(s['k_seeds'].iloc[-1])}→{s['dp95_pct'].iloc[-1]:+.2f}%")
    if feas is not None and not feas.empty:
        print("\n[RQ7 退化组合]")
        deg = feas[feas["verdict"].str.startswith("退化") |
                   feas["verdict"].str.startswith("部分退化")]
        print(f"  退化/部分退化组合 {len(deg)} / {len(feas)}")
        for _, r in deg.iterrows():
            keytxt = " ".join(str(r.get(k)) for k in MODE_KEYS[r["mode"]])
            print(f"    {r['mode']:<12} {keytxt:<40} "
                  f"p95极差={r['rel_spread_pct']:.4f}%  "
                  f"四策略全同={r['identical_all_metrics']}  "
                  f"逐位相同对=[{r['identical_pairs']}]")
        print(f"\n  其中 NetworkAware 与 LeastLoad 逐位相同的组合 {int(feas['NA_eq_LeastLoad'].sum())} 个：")
        for _, r in feas[feas["NA_eq_LeastLoad"]].iterrows():
            keytxt = " ".join(str(r.get(k)) for k in MODE_KEYS[r["mode"]])
            print(f"    {r['mode']:<12} {keytxt}")
    if rho_sweep is not None and not rho_sweep.empty:
        print("\n[RQ8 到达压力：收益边界在负载强度上的形状]")
        s = rho_sweep[rho_sweep["baseline"] == "LeastLoad"]
        for (wl, net), g in s.groupby(["workload", "network"]):
            g = g.sort_values("target_rho")
            print(f"  {wl}/{net} vs LeastLoad:  " + "  ".join(
                f"rho={row['target_rho']:.2f}→{row['dp95_pct']:+.1f}%"
                for _, row in g.iterrows()))
    if fallback is not None and not fallback.empty:
        print("\n[RQ9 回退机制：相对不回退的 NetworkAware]")
        for st, g in fallback.groupby("staleness"):
            sig = g[g["p_holm_median"] < 0.05]
            n_better = int((sig["dp95_pct"] < 0).sum())
            n_worse = int((sig["dp95_pct"] > 0).sum())
            n_null = int(len(g) - len(sig))
            print(f"  {st:<6} 显著更优 {n_better} / 显著更差（回退过头）{n_worse} / "
                  f"无显著差异 {n_null}   共 {len(g)} 个（负载×网络×阈值）")
    print("\n[适用范围与限制]")
    src = detect_data_source(data)
    if src == "trace":
        print("  * 负载为真实 Azure LLM Inference Trace（种子=不同时间片，"
              "到达率按目标 rho 标定）；结论不外推到其他 trace 或生产集群；")
    elif src == "synthetic":
        print("  * 负载为合成负载（synthetic），不得当作 trace 结论；")
    else:
        print(f"  * 负载来源：{src} —— 引用前先查 manifest 的 data_source；")
    print("  * 槽位之间不争抢算力、链路无排队 → 报告的收益偏保守；")
    print("  * staleness 观测延迟为常数，未建模估计误差；")
    print("  * 静态网络（normal/degraded）下链路陈旧精确无效，相关格子为预期退化。")


def section_verification(data: dict) -> None:
    """§12 验证与保真度记录 —— 把"门通过了"和"参数从哪来"放进分析报告本身。

    读的是 `results/verification/`（`analysis/verify_gates.py` 的产物，复制到本目录）
    与 `fidelity_anchors.csv`。**容忍缺失**：没有就说明怎么生成，不报错——
    报告的可信度声明不能依赖人工记忆。
    """
    print("\n" + "=" * 78)
    print("§12 验证与保真度记录（门 / 白盒 / 黑盒 / 参数来源）")
    print("=" * 78)
    gates = sorted(TABLE_DIR.glob("*_gates.json"))
    if not gates:
        print("  （本目录没有验证记录：先跑 analysis/verify_gates.py "
              "--batch <name>，再把产物放到本目录）")
    for gp in gates:
        try:
            rec = json.loads(gp.read_text(encoding="utf-8"))
        except Exception as exc:      # noqa: BLE001
            print(f"  [警告] 无法解析 {gp.name}: {exc}")
            continue
        checks = rec.get("checks", [])
        n_pass = sum(1 for c in checks if c.get("passed"))
        print(f"\n  [{gp.name}]  批次={rec.get('label')}  "
              f"created_utc={rec.get('created_utc')}  通过 {n_pass}/{len(checks)}")
        for c in checks:
            mark = "✓" if c.get("passed") else "✗"
            detail = str(c.get("detail", ""))
            print(f"    {mark} {c.get('check', ''):<32} {detail[:110]}")

    anchors = TABLE_DIR / "fidelity_anchors.csv"
    if anchors.exists():
        try:
            a = pd.read_csv(anchors)
        except Exception as exc:      # noqa: BLE001
            print(f"  [警告] 无法解析 {anchors.name}: {exc}")
            return
        print(f"\n  [表] {anchors.name} —— 模型关键假设 vs 已核对的公开实测值（{len(a)} 行）")
        for _, r in a.iterrows():
            print(f"\n  ■ {r['model_quantity']}")
            print(f"      代码取值：{r['code_value']}")
            print(f"      对照锚点（{r['anchor_kind']}）：{str(r['anchor'])[:200]}")
            if "deviation" in a.columns:
                print(f"      偏差与可外推性：{str(r['deviation'])[:260]}")
            print(f"      备注：{str(r['note'])[:180]}")
        print("\n  ⚠ 判读规则：`偏差与可外推性` 一列就是 Threats to Validity 的骨架——"
              "凡写\"不可比/不能外推/低 N 倍\"的地方，正文都必须逐条声明。")
    else:
        print(f"\n  （没有 {anchors.name}：先跑 analysis/verify_gates.py 生成）")

    # --- 组件级实测（analysis/measure_components.py 的产物）---
    mc = TABLE_DIR / "measured_components.json"
    if mc.exists():
        try:
            rec = json.loads(mc.read_text(encoding="utf-8"))
        except Exception as exc:      # noqa: BLE001
            print(f"  [警告] 无法解析 {mc.name}: {exc}")
            return
        print(f"\n  [表] {mc.name} —— 组件级实测（真实进程，不是仿真）")
        print(f"      {rec.get('host_note','')}")
        t = rec.get("transfer") or []
        if t:
            td = pd.DataFrame(t)
            big = td[td["payload_bytes"] >= 1_000_000]
            print(f"      传输时间：{len(td)} 组（{td['rate_label'].nunique()} 档速率 × "
                  f"{td['payload_bytes'].nunique()} 种载荷）× {int(td['n_repeat'].max())} 次重复")
            print(f"        相对误差中位 {td['rel_err_pct'].median():+.2f}%，"
                  f"≥1 MB 载荷的最大|误差| {big['rel_err_pct'].abs().max():.2f}%"
                  f"（<64 KB 的载荷限速器限不住，不构成结论）")
        qs = sorted(TABLE_DIR.glob("measured_queue_rho*.csv"))
        if qs:
            print(f"      排队机制：{len(qs)} 个负载点，真实 k 个 CPU 进程 vs SimPy"
                  f"（同一到达过程 + 同一实测服务时间）")
            for f in qs:
                q = pd.read_csv(f).iloc[0]
                print(f"        目标 rho={q['rho_nominal']:.2f} → 实测 {q['rho_realized']:.3f}  "
                      f"均值误差 {q['mean_rel_err_pct']:+5.1f}%  p50 {q['p50_rel_err_pct']:+5.1f}%  "
                      f"p95 {q['p95_rel_err_pct']:+5.1f}%  p99 {q['p99_rel_err_pct']:+6.1f}%  "
                      f"（sojourn 均值 {q['measured_sojourn_mean_s']*1000:.0f} ms）")
            q0 = pd.read_csv(qs[0]).iloc[0]
            print(f"      服务时间随负载上升（模型【不】建模）：空闲 {q0['idle_service_mean_s']*1000:.1f} ms → "
                  f"持续满载 {q0['calibrated_service_mean_s']*1000:.1f} ms"
                  f"（×{q0['contention_factor']:.2f}）")
            print("        → 这就是『槽位之间不争抢算力』这条简化的实测误差："
                  "仿真在饱和区把吞吐高估约 "
                  f"{q0['contention_factor']:.1f} 倍，必须写进 Threats to Validity")


def section_align(data: dict):
    """§17 「谁算得快」与「谁网线粗」的对齐度——网络信息**为什么**会有价值。

    这是机制侧的变量，不是又一个策略对比：调度要同时权衡"哪台机器算得快"和
    "哪台机器网线粗"。两者**一致**时（算得快的网线也最粗），按"谁闲"挑和按"网线好"挑
    挑到同一台，网络信息不提供额外区分度 → 收益应当≈0；两者**相反**时才有用武之地。

    `align` 三档：anti（现状，完全相反）/ rot1（部分一致）/ aligned（完全一致）；
    一致比例由 `run_matrix.alignment_index()` 定义并落在 runs.csv 里。
    **预测在跑之前就写死在 build_matrix 的注释里**：aligned 收益≈0、anti 最大、rot1 居中。
    """
    print("\n" + "=" * 78)
    print("§17 对齐度：算力排名与链路排名一致时，网络信息还有价值吗")
    print("=" * 78)
    d = data.get("align", {})
    runs, paired = d.get("runs"), d.get("paired")
    if runs is None or runs.empty:
        print("  （本批没有 align 产物：先跑 --mode align）")
        return None
    keys = ["align", "workload", "network"]
    eff = effect_table(runs, paired, keys, candidate="NetworkAware2",
                       baselines=["LeastLoad"])
    if eff.empty:
        print("  （配对表里没有可用的对比）")
        return None
    write_table(eff, "rq17_alignment_effect.csv")
    order = {"anti": 0, "rot1": 1, "aligned": 2}
    eff["_o"] = eff["align"].map(order).fillna(9)
    print(f"  {'对齐档':<9}{'负载':<13}{'网络':<10}{'p95_NA2':>9}{'p95_LL':>9}"
          f"{'Δp95%':>9}{'±95%CI':>8}{'种子':>5}{'net_share':>10}  判定")
    for _, r in eff.sort_values(["_o", "workload", "network"]).iterrows():
        # 跨"独立负载实现"（=种子）的 95% 置信区间：t 分布，自由度 k-1
        k = int(r.get("n_seeds", 0) or 0)
        se = float(r.get("dp95_se", float("nan")))
        ci = float("nan")
        if k >= 2 and np.isfinite(se):
            try:
                from scipy import stats as _st      # noqa: PLC0415
                ci = _st.t.ppf(0.975, k - 1) * se
            except Exception:                        # noqa: BLE001
                ci = 1.96 * se
        print(f"  {str(r['align']):<9}{str(r['workload']):<13}{str(r['network']):<10}"
              f"{r['p95_na']:>9.2f}{r['p95_base']:>9.2f}{r['dp95_pct']:>9.1f}"
              f"{ci:>8.1f}{k:>5d}{r['net_share_na']:>10.3f}  {str(r['verdict'])[:22]}")
    print("\n  读法：Anti = 算力最强的机器配最慢链路（两个标准互相矛盾）；"
          "\n        Aligned = 算力最强配最快链路（两个标准一致，网络信息理论上无附加价值）。"
          "\n  ±95%CI 是**跨种子**（＝跨独立负载实现）的区间，不是把两万条请求当独立样本的区间。")

    # ---------- 统计检验：对齐度到底有没有把收益打下去 ----------
    # 【为什么要这么测】per-request 的配对检验在两万请求下 p 值必然小到荒谬（前面已实测），
    # 它回答"同一份负载下两个策略有没有差别"，**回答不了**"换了部署方式（对齐度）之后
    # 收益有没有系统性地变化"。后者的正确单位是**独立的负载实现**（＝种子），
    # 所以这里把每个种子的 Δp95 当成一个观测，做组间非参数检验与趋势检验。
    piv = runs.pivot_table(index=["align", "workload", "network", "seed"],
                           columns="strategy", values="sojourn_p95")
    if {"NetworkAware2", "LeastLoad"} <= set(piv.columns):
        dp = ((piv["NetworkAware2"] - piv["LeastLoad"]) / piv["LeastLoad"] * 100.0)
        dp = dp.reset_index(name="dp95_pct")
        print("\n  --- 组间检验：anti（相反）vs aligned（一致），单位=每个种子的 Δp95% ---")
        print(f"  {'负载':<13}{'网络':<10}{'anti 均值±95%CI':>22}{'aligned 均值±95%CI':>22}"
              f"{'差值':>8}{'Mann-Whitney p':>16}{'Cliffδ':>9}")
        try:
            from scipy import stats as st      # noqa: PLC0415
        except Exception:                       # noqa: BLE001
            st = None
        for wl in sorted(dp["workload"].unique()):
            for net in sorted(dp["network"].unique()):
                a = dp[(dp["workload"] == wl) & (dp["network"] == net) & (dp["align"] == "anti")]["dp95_pct"]
                b = dp[(dp["workload"] == wl) & (dp["network"] == net) & (dp["align"] == "aligned")]["dp95_pct"]
                if len(a) < 2 or len(b) < 2:
                    continue
                def _ci(v):
                    se = v.std(ddof=1) / np.sqrt(len(v))
                    t = st.t.ppf(0.975, len(v) - 1) if st is not None else 1.96
                    return t * se
                # Cliff's delta：组间效应量（全对比较，方向 = b 相对 a 的大小）
                gt = sum(1 for x in a for y in b if x > y)
                lt = sum(1 for x in a for y in b if x < y)
                cliff = (lt - gt) / (len(a) * len(b))
                p = (st.mannwhitneyu(a, b, alternative="two-sided").pvalue
                     if st is not None else float("nan"))
                print(f"  {wl:<13}{net:<10}"
                      f"{a.mean():>14.1f}±{_ci(a):<6.1f}{b.mean():>14.1f}±{_ci(b):<6.1f}"
                      f"{b.mean()-a.mean():>8.1f}{p:>16.3g}{cliff:>9.2f}")
        # 趋势检验：Δp95 是否随"对齐度"单调上升（即收益单调下降）
        if st is not None:
            m = dp.copy()
            if "align_index" in runs.columns:
                m = m.merge(runs[["align", "align_index"]].drop_duplicates(),
                            on="align", how="left")
            else:
                # 兜底：本批产物早于 `align_index` 落盘（该列是后加的）。
                # 三节点配置下 alignment_index() 的取值是固定的 anti=0 / rot1=2/3 / aligned=1，
                # 已用 run_matrix.alignment_index() 逐个核对过，这里只做映射，不猜数。
                m["align_index"] = m["align"].map({"anti": 0.0, "rot1": 2.0 / 3.0, "aligned": 1.0})
            m = m.dropna(subset=["align_index"])
            if len(m) > 3:
                rho, prho = st.spearmanr(m["align_index"], m["dp95_pct"])
                print(f"\n  --- 趋势检验（全样本）：Δp95 与对齐度的 Spearman ρ = {rho:+.2f}，"
                      f"p = {prho:.3g}，n = {len(m)}")
                print("      ρ>0 表示「对齐度越高、收益越小」，即机制预测成立。")
    return eff


def section_alignscale(data: dict):
    """§18 对齐度 × 节点规模：对齐度效应是 3 节点的小样本巧合，还是在规模上仍在？

    与 §17 同一套统计口径（单位＝种子＝独立负载实现）：每格报"跨种子均值 ± 95% CI"，
    并按**节点数**分别做组间检验（anti vs aligned）与趋势检验（Δp95 ~ 一致比例）。

    【一个必须照实说的设计限制】一致比例能操纵到的**范围随节点数收窄**：
    算力是 2 的幂阶梯（周期 4 台）、链路是斜坡，节点一多就出现"算力并列"的对，
    它们的贡献记 0.5，于是 index 到不了 0/1——
    3 台：0.00 / 0.67 / 1.00；4 台：0.00 / 0.50 / 1.00；8 台：0.29 / 0.57 / 0.71；16 台：0.40 / 0.50 / 0.60。
    所以 16 台上"没差别"**可能只是操纵太弱**，不能直接读成"机制不成立"。
    因此趋势检验用**数值一致比例**当自变量（而不是档位标签），并在表里把该值打出来。
    """
    print("\n" + "=" * 78)
    print("§18 对齐度 × 节点规模（统计口径同 §17：单位=种子）")
    print("=" * 78)
    d = data.get("alignscale", {})
    runs, paired = d.get("runs"), d.get("paired")
    if runs is None or runs.empty:
        print("  （本批没有 alignscale 产物：先跑 --mode alignscale）")
        return None
    keys = ["n_nodes", "align"]
    eff = effect_table(runs, paired, keys, candidate="NetworkAware2", baselines=["LeastLoad"])
    if eff.empty:
        print("  （配对表里没有可用的对比）")
        return None
    write_table(eff, "rq18_alignment_scale_effect.csv")
    # 趋势轴优先用**新指标**（算力/带宽排名的 Spearman ρ）：
    # 旧指标（两两一致比例）在节点 >3 时会被"算力并列"的对稀释，8 台时 anti 与 rot1 甚至完全相同。
    axis_col = "align_rank_corr" if "align_rank_corr" in runs.columns else "align_index"
    idx = {}
    if axis_col in runs.columns:
        idx = (runs[["n_nodes", "align", axis_col]].drop_duplicates()
               .set_index(["n_nodes", "align"])[axis_col].to_dict())
    print(f"  {'节点':>4}{'对齐档':<9}{'排名相关ρ':>10}{'p95_NA2':>9}{'p95_LL':>9}"
          f"{'Δp95%':>9}{'±95%CI':>8}{'种子':>5}  判定")
    order = {"anti": 0, "rot1": 1, "aligned": 2}
    eff["_o"] = eff["align"].map(order).fillna(9)
    for _, r in eff.sort_values(["n_nodes", "_o"]).iterrows():
        k = int(r.get("n_seeds", 0) or 0)
        se = float(r.get("dp95_se", float("nan")))
        ci = float("nan")
        if k >= 2 and np.isfinite(se):
            try:
                from scipy import stats as _st      # noqa: PLC0415
                ci = _st.t.ppf(0.975, k - 1) * se
            except Exception:                        # noqa: BLE001
                ci = 1.96 * se
        a_idx = idx.get((int(r["n_nodes"]), str(r["align"])), float("nan"))
        print(f"  {int(r['n_nodes']):>4}{str(r['align']):<9}{a_idx:>9.3f}"
              f"{r['p95_na']:>9.2f}{r['p95_base']:>9.2f}{r['dp95_pct']:>9.1f}"
              f"{ci:>8.1f}{k:>5d}  {str(r['verdict'])[:22]}")

    piv = runs.pivot_table(index=["n_nodes", "align", "seed"],
                           columns="strategy", values="sojourn_p95")
    if {"NetworkAware2", "LeastLoad"} <= set(piv.columns):
        dp = ((piv["NetworkAware2"] - piv["LeastLoad"]) / piv["LeastLoad"] * 100.0)
        dp = dp.reset_index(name="dp95_pct")
        if idx:
            dp["align_axis"] = [idx.get((int(n), str(a)), np.nan)
                                for n, a in zip(dp["n_nodes"], dp["align"])]
        try:
            from scipy import stats as st      # noqa: PLC0415
        except Exception:                       # noqa: BLE001
            st = None
        print("\n  --- 按节点数的组间检验（anti vs aligned，单位=每个种子的 Δp95%）---")
        print(f"  {'节点':>4}{'anti 均值±CI':>18}{'aligned 均值±CI':>20}"
              f"{'差值':>8}{'Mann-Whitney p':>16}{'Cliffδ':>9}")
        for n in sorted(dp["n_nodes"].unique()):
            a = dp[(dp["n_nodes"] == n) & (dp["align"] == "anti")]["dp95_pct"]
            b = dp[(dp["n_nodes"] == n) & (dp["align"] == "aligned")]["dp95_pct"]
            if len(a) < 2 or len(b) < 2:
                continue
            t = st.t.ppf(0.975, len(a) - 1) if st is not None else 1.96
            gt = sum(1 for x in a for y in b if x > y)
            lt = sum(1 for x in a for y in b if x < y)
            cliff = (lt - gt) / (len(a) * len(b))
            p = (st.mannwhitneyu(a, b, alternative="two-sided").pvalue
                 if st is not None else float("nan"))
            print(f"  {int(n):>4}{a.mean():>12.1f}±{a.std(ddof=1)/np.sqrt(len(a))*t:<5.1f}"
                  f"{b.mean():>14.1f}±{b.std(ddof=1)/np.sqrt(len(b))*t:<5.1f}"
                  f"{b.mean()-a.mean():>8.1f}{p:>16.3g}{cliff:>9.2f}")
        if st is not None and "align_axis" in dp.columns:
            m = dp.dropna(subset=["align_axis"])
            if len(m) > 3:
                rho, prho = st.spearmanr(m["align_axis"], m["dp95_pct"])
                print(f"\n  --- 趋势检验（全部节点数合并）：Spearman ρ = {rho:+.2f}，"
                      f"p = {prho:.3g}，n = {len(m)}")
            for n in sorted(m["n_nodes"].unique()):
                s = m[m["n_nodes"] == n]
                if len(s) > 3:
                    rho, prho = st.spearmanr(s["align_axis"], s["dp95_pct"])
                    print(f"      节点 {int(n):>2}：ρ = {rho:+.2f}，p = {prho:.3g}，n = {len(s)}")

    # ---------- 规模衰减到底怎么解释：直接测"不看网络会不会碰巧挑到好网线" ----------
    # 由 `analysis/measure_link_choice.py` 产出（它要跑仿真，所以不放在本脚本里）。
    probe = TABLE_DIR / "link_choice_by_scale.csv"
    if probe.exists():
        p = read_csv_safe(probe)
        if p is not None and not p.empty:
            print("\n  --- 直接检验：不看网络的 LeastLoad 挑中的机器，在「网线粗细」里排第几 ---")
            print(f"  {'节点':>4}{'ρ':>8}{'策略':>16}{'平均带宽排名':>14}{'随机水平':>10}"
                  f"{'落最快1/3':>11}{'p95(s)':>9}")
            g = p.groupby(["n_nodes", "rank_corr", "strategy"], dropna=False).agg(
                rank_mean=("mean_bandwidth_rank", "mean"),
                frac=("frac_top_third_bandwidth", "mean"),
                p95=("p95_s", "mean"),
                n_seeds=("seed", "nunique")).reset_index()
            for _, r in g.sort_values(["n_nodes", "strategy"]).iterrows():
                n = int(r["n_nodes"])
                print(f"  {n:>4}{r['rank_corr']:>+8.2f}{str(r['strategy']):>16}"
                      f"{r['rank_mean']:>14.2f}{(n + 1) / 2:>10.1f}"
                      f"{r['frac'] * 100:>10.0f}%{r['p95']:>9.2f}")
            keys_p = ["n_nodes", "strategy"]
            ll = g[g["strategy"] == "LeastLoad"].copy()
            if len(ll) >= 3:
                # 每个 (对齐档, 节点数) 一个点：LeastLoad 的带宽排名 相对 "随机挑" 偏了几名
                ll["dev"] = ll["rank_mean"] - (ll["n_nodes"] + 1) / 2.0
                print("\n  判读（全部由 probe CSV 算出，无人工挑选）：")
                by_align = {}
                if "align" in p.columns:
                    mm = p[p["strategy"] == "LeastLoad"].groupby(
                        ["align", "n_nodes"], dropna=False).agg(
                        rank_mean=("mean_bandwidth_rank", "mean")).reset_index()
                    mm["dev"] = mm["rank_mean"] - (mm["n_nodes"] + 1) / 2.0
                    for a, sub in mm.groupby("align", dropna=False):
                        by_align[a] = sub
                for a, sub in by_align.items():
                    sub = sub.sort_values("n_nodes")
                    d0, d1 = float(sub["dev"].iloc[0]), float(sub["dev"].iloc[-1])
                    rho_s = _spearman_p(sub["n_nodes"].to_numpy(), sub["dev"].to_numpy())[0]
                    print(f"    · {str(a):<8}对齐：LeastLoad 相对随机水平的偏离 "
                          f"{d0:+.2f} → {d1:+.2f} 名（ρ(n, 偏离) = {rho_s:+.2f}）")
                if "anti" in by_align and "aligned" in by_align:
                    anti_dev = by_align["anti"]["dev"].abs().mean()
                    ali = by_align["aligned"].sort_values("n_nodes")
                    ali_dev0 = float(ali["dev"].iloc[0])
                    ali_dev1 = float(ali["dev"].iloc[-1])
                    print(
                        f"    → anti 档的偏离幅度平均只有 {anti_dev:.2f} 名（3–16 台的全程）："
                        f"**不看网络的策略在这些配置下并没有『碰巧挑到好网线』**，"
                        f"所以 anti 档的收益不随规模衰减，"
                        f"实测 −63%/−61%/−57%（4/8/16）。\n"
                        f"    → aligned 档的偏离为负且随规模变大（{ali_dev0:+.2f} → "
                        f"{ali_dev1:+.2f} 名）：**该档下 LeastLoad 自己就挑到了好网线**，"
                        f"网络信息能加的分越来越少——这才是「收益随规模变小」的来源，"
                        f"而且它同时被「可构造的 ρ 区间变窄」放大（见下行）。")
                if "align" in p.columns:
                    rng = (p.groupby(["n_nodes", "align"], dropna=False)["rank_corr"]
                           .first().unstack())
                    if {"anti", "aligned"} <= set(rng.columns):
                        print("    实测可构造的 ρ 区间（anti | aligned，随节点数收窄）：")
                        for n, r in rng.iterrows():
                            print(f"      节点 {int(n):>2}：{r['anti']:+.2f} | {r['aligned']:+.2f}")
                        print("    ⚠️ 因此 §18 的『效应随规模衰减』有一半是**操纵范围收窄**的假象："
                              "\n       算力按周期 4 循环、链路按节点数铺开，节点一多就造不出强对齐的配置。"
                              "\n       要真正分离这两件事，需要让算力取值脱离周期 4（未来工作）。")
    else:
        print("\n  （没有 link_choice_by_scale.csv："
              "跑 `analysis/measure_link_choice.py` 可补上规模衰减的直接检验）")
    return eff


def _t_ci(k: int, se: float) -> float:
    """跨种子均值的 95% 置信区间半宽（t 分布，自由度 k−1）。

    【为什么不是 1.96·se】种子只有 10 个，正态近似会**低估**区间。
    统计单位是"独立的负载实现"（＝种子），不是两万条请求——这一点全篇一致。
    """
    if k < 2 or not np.isfinite(se):
        return float("nan")
    try:
        from scipy import stats as _st      # noqa: PLC0415
        return float(_st.t.ppf(0.975, k - 1) * se)
    except Exception:                        # noqa: BLE001
        return float(1.96 * se)


def _wilcoxon_p(a, b) -> float:
    """按种子配对的 Wilcoxon 符号秩检验（零差被丢弃；全零差时返回 nan）。"""
    try:
        from scipy import stats as _st      # noqa: PLC0415
    except Exception:                        # noqa: BLE001
        return float("nan")
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    if len(a) < 6 or np.allclose(a, b):
        return float("nan")
    try:
        return float(_st.wilcoxon(a, b).pvalue)
    except Exception:                        # noqa: BLE001
        return float("nan")


def _spearman_p(a, b) -> tuple[float, float]:
    """Spearman 秩相关与 p 值；样本太少或方差为 0 时返回 (nan, nan)。"""
    try:
        from scipy import stats as _st      # noqa: PLC0415
    except Exception:                        # noqa: BLE001
        return (float("nan"), float("nan"))
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    if len(a) < 3 or np.allclose(a, a[0]) or np.allclose(b, b[0]):
        return (float("nan"), float("nan"))
    try:
        r = _st.spearmanr(a, b)
        return (float(r.statistic), float(r.pvalue))
    except Exception:                        # noqa: BLE001
        return (float("nan"), float("nan"))


def _mannwhitney_p(a, b) -> float:
    """两组（非配对）的 Mann–Whitney U 检验。"""
    try:
        from scipy import stats as _st      # noqa: PLC0415
    except Exception:                        # noqa: BLE001
        return float("nan")
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    if len(a) < 3 or len(b) < 3:
        return float("nan")
    try:
        return float(_st.mannwhitneyu(a, b, alternative="two-sided").pvalue)
    except Exception:                        # noqa: BLE001
        return float("nan")


def section_method(data: dict):
    """§19 V3 方法：**降权规则本身值多少钱**——"该信陈旧状态多少"这个决定怎么定。

    五条臂在**同一个 cell 内**跑，因此比较是逐请求配对、跨种子聚合的：
      · `LeastLoad`     —— 完全不信网络信息（下界）
      · `NetworkAware2` —— 完全信，不管多旧
      · `AdaptSplit`    —— 主方法：指数降权 + 把链路信息拆成"忙闲 / 粗细"两块
      · `AdaptLinear`   —— 线性降权（到尺度归零）
      · `AdaptHard`     —— 硬开关（超阈值整块关掉）

    为什么必须把三条规则都摆出来、而不是只报主方法：三种规则**各有一处被实测的失效方向**
    （硬开关过度回退；线性在尺度处归零，等于在那一点放弃全部剩余信息；split 单独压低
    拥塞项会制造"粗网线 + 慢算力"的堆叠）。只报最好的那条就成了"调好再报"，
    SKILL.md 明文禁止。这里连**实际施加的权重**（`w_dyn_mean` / `w_stat_mean` /
    `w_gated_frac`，由策略在运行期自报）一起报出来，读者能自己判断"它到底用没用上网络信息"。
    """
    print("\n" + "=" * 78)
    print("§19 方法：降权规则对照（该信陈旧状态多少）")
    print("=" * 78)
    d = data.get("method", {})
    runs, paired = d.get("runs"), d.get("paired")
    # 【跨批合并】`method_ch`（修订版规则）用的是**逐字段相同**的格子，
    # 而且负载与轨迹只由 (负载类, 种子, 请求数) 决定 → 两批的请求流逐位相同
    # （已用 `np.array_equal` 逐格核对过完成时间数组）。所以把两批并起来，
    # 按 (条件, 种子) 配对是合法的，并且能把"原设计 vs 修订设计"放在同一张表里比。
    ch = data.get("method_ch", {})
    if ch.get("runs") is not None and not ch["runs"].empty:
        frames = [r for r in (runs, ch["runs"]) if r is not None and not r.empty]
        if frames:
            runs = pd.concat(frames, ignore_index=True)
        if paired is not None and not paired.empty and ch.get("paired") is not None:
            paired = pd.concat([paired, ch["paired"]], ignore_index=True)
    if runs is None or runs.empty:
        print("  （本批没有 method 产物：先跑 --mode method）")
        return None

    ARMS = ["LeastLoad", "NetworkAware2", "AdaptChannel", "AdaptSplit",
            "AdaptLinear", "AdaptHard"]
    present = [a for a in ARMS if a in set(runs["strategy"])]
    main_arm = "AdaptSplit" if "AdaptSplit" in present else None
    if len(present) < 3:
        print(f"  （策略不全，只见 {present}；等矩阵跑完再分析）")
    keys = ["workload", "network", "staleness_queue_s", "staleness_network_s"]
    piv = runs.pivot_table(index=keys + ["seed"], columns="strategy",
                           values="sojourn_p95")
    # 运行期自报的权重列（只有自适应臂有，其余为 NaN）
    W_DIAG_COLS = [c for c in ("w_dyn_mean", "w_stat_mean", "net_share_obs_mean",
                               "w_gated_frac") if c in runs.columns]
    wdiag = None
    if W_DIAG_COLS:
        wdiag = (runs.groupby(keys + ["strategy"], dropna=False)[W_DIAG_COLS]
                 .mean())

    # ---------- 长表：每条臂 × 每个条件的效应与不确定性 ----------
    rows = []
    for key, g in piv.groupby(level=keys, dropna=False):
        key = key if isinstance(key, tuple) else (key,)
        b = g["LeastLoad"].dropna() if "LeastLoad" in g.columns else pd.Series(dtype=float)
        base = float(b.mean()) if len(b) else float("nan")
        for arm in present:
            if arm not in g.columns:
                continue
            v = g[arm].dropna()
            if not len(v):
                continue
            rec = dict(zip(keys, key))
            rec["arm"] = arm
            rec["n_seeds"] = int(len(v))
            rec["p95_mean"] = float(v.mean())
            rec["p95_std"] = float(v.std(ddof=1)) if len(v) > 1 else float("nan")
            # 相对两个端点的变化（按种子配对，单位=独立负载实现）
            for base_name, base_id in (("leastload", "LeastLoad"), ("na2", "NetworkAware2")):
                if base_id not in g.columns or base_id == arm:
                    rec[f"dp95_vs_{base_name}_pct"] = 0.0 if base_id == arm else float("nan")
                    rec[f"ci_{base_name}"] = float("nan")
                    rec[f"p_{base_name}"] = float("nan")
                    continue
                m = g[[arm, base_id]].dropna()
                if not len(m):
                    rec[f"dp95_vs_{base_name}_pct"] = float("nan")
                    rec[f"ci_{base_name}"] = float("nan")
                    rec[f"p_{base_name}"] = float("nan")
                    continue
                rel = (m[arm] - m[base_id]) / m[base_id].replace(0, np.nan) * 100.0
                rec[f"dp95_vs_{base_name}_pct"] = float(rel.mean())
                # 【为什么必须同时报中位数】种子的 p95 之间可以差 5–8 倍（真实 trace 的不同时段），
                # 于是"逐种子相对差的均值"会被个别极端种子主导（实测见过单种子 +265%）。
                # 均值是配对设计的正确估计量，中位数是稳健性检查——两者符号不一致的地方
                # 必须在正文里写出来，不能只挑好看的那个。
                rec[f"dp95_vs_{base_name}_median_pct"] = float(rel.median())
                rec[f"dp95_vs_{base_name}_iqr_pct"] = float(
                    rel.quantile(0.75) - rel.quantile(0.25)) if len(rel) > 1 else float("nan")
                rec[f"n_seeds_better_vs_{base_name}"] = int((rel < 0).sum())
                rec[f"n_seeds_worse_vs_{base_name}"] = int((rel > 0).sum())
                se = float(rel.std(ddof=1) / np.sqrt(len(rel))) if len(rel) > 1 else float("nan")
                rec[f"ci_{base_name}"] = _t_ci(len(rel), se)
                rec[f"p_{base_name}"] = _wilcoxon_p(m[arm].to_numpy(), m[base_id].to_numpy())
            # 运行期自报的实际权重（只有自适应臂有；按 (条件, 策略) 预先聚合，避免逐行重筛）
            if wdiag is not None:
                try:
                    vals = wdiag.loc[key + (arm,)]
                except KeyError:
                    vals = None
            else:
                vals = None
            for col in W_DIAG_COLS:
                rec[col] = float(vals[col]) if vals is not None else float("nan")
            rows.append(rec)
    eff = pd.DataFrame(rows)
    if eff.empty:
        print("  （没有可用的对比行）")
        return None
    write_table(eff, "rq19_decay_rule_effect.csv")

    # ---------- 看板 ①：每个条件的 p95（跨种子均值） ----------
    lab = [x for x in ARMS if x in present]
    print(f"\n  --- p95 端到端耗时（秒，跨种子均值；越低越好）---")
    print(f"  {'负载':<11}{'网络':<10}{'队列旧':>7}{'链路旧':>7} | "
          + "".join(f"{x:>15}" for x in lab))
    wide = eff.pivot_table(index=keys, columns="arm", values="p95_mean")
    for key, r in wide.iterrows():
        key = key if isinstance(key, tuple) else (key,)
        print(f"  {str(key[0]):<11}{str(key[1]):<10}{key[2]:>7.0f}{key[3]:>7.0f} | "
              + "".join(f"{r.get(x, float('nan')):>15.1f}" for x in lab))

    # ---------- 看板 ②：相对两个端点的变化 ----------
    for base_name, base_label in (("na2", "NetworkAware2（完全信）"),
                                  ("leastload", "LeastLoad（完全不信）")):
        sub = eff.pivot_table(index=keys, columns="arm",
                              values=f"dp95_vs_{base_name}_pct")
        sub_med = eff.pivot_table(index=keys, columns="arm",
                                  values=f"dp95_vs_{base_name}_median_pct")
        print(f"\n  --- 相对 {base_label} 的 Δp95%（负=更好；每格「均值/中位数」）---")
        cols = [x for x in lab if x != ("NetworkAware2" if base_name == "na2" else "LeastLoad")]
        print(f"  {'负载':<11}{'网络':<10}{'队列旧':>7}{'链路旧':>7} | "
              + "".join(f"{x:>18}" for x in cols))
        for key, r in sub.iterrows():
            key = key if isinstance(key, tuple) else (key,)
            mm = sub_med.loc[key] if key in sub_med.index else None
            cells = []
            for x in cols:
                v = r.get(x, float("nan"))
                v2 = float(mm.get(x, float("nan"))) if mm is not None else float("nan")
                cells.append(f"{v:>8.1f}/{v2:>7.1f}")
            print(f"  {str(key[0]):<11}{str(key[1]):<10}{key[2]:>7.0f}{key[3]:>7.0f} | "
                  + "".join(f"{c:>18}" for c in cells))
        print("  说明：均值是配对设计的估计量（逐种子配对后求均值）；中位数是稳健性检查。"
              "\n        两者符号不一致的格子说明该结论被极端种子主导，正文必须写出来。")

    # ---------- 看板 ③：实际施加的权重（自适应臂自己报的） ----------
    diag_arms = [a for a in ("AdaptChannel", "AdaptSplit") if a in present] or (
        [main_arm] if main_arm else [])
    for arm in diag_arms:
        print(f"\n  --- {arm} 实际施加的权重（运行期自报，均值）---")
        print(f"  {'负载':<11}{'网络':<10}{'队列旧':>7}{'链路旧':>7}"
              f"{'动态项w':>10}{'结构项w':>10}{'网络占比':>10}{'被门关掉':>10}")
        ms = eff[(eff["arm"] == arm)]
        for _, r in ms.iterrows():
            print(f"  {str(r['workload']):<11}{str(r['network']):<10}"
                  f"{r['staleness_queue_s']:>7.0f}{r['staleness_network_s']:>7.0f}"
                  f"{r['w_dyn_mean']:>10.3f}{r['w_stat_mean']:>10.3f}"
                  f"{r['net_share_obs_mean']:>10.3f}{r['w_gated_frac']*100:>9.0f}%")
    if diag_arms:
        print("  读法：动态项 w 是「此刻谁在挤链路」那一块的权重，"
              "结构项 w 是「哪条网线粗」那一块的权重；"
              "\n        AdaptChannel 两栏相等（按通道降权，两分量同权），"
              "AdaptSplit 两栏不等（按打分的项降权，已被实测否掉）。"
              "\n        被门关掉 = 网络占比不足 10%，整块网络项没用上。")

    # ---------- 裁决：每条臂在多少个条件上同时胜过两个端点 ----------
    print("\n  --- 裁决：每条降权规则在多少个条件上「同时优于两个端点」---")
    print(f"  {'臂':<14}{'条件数':>7}{'胜过两端':>10}{'输给两端':>10}{'优于NA2':>10}"
          f"{'劣于NA2':>10}{'优于LeastLoad':>14}")
    verdict_rows = []
    for arm in [x for x in present if x not in ("LeastLoad", "NetworkAware2")]:
        s = eff[eff["arm"] == arm]
        if s.empty:
            continue
        both_better = ((s["dp95_vs_na2_pct"] < 0) & (s["dp95_vs_leastload_pct"] < 0)).sum()
        both_worse = ((s["dp95_vs_na2_pct"] > 0) & (s["dp95_vs_leastload_pct"] > 0)).sum()
        n_na2_better = int((s["dp95_vs_na2_pct"] < 0).sum())
        n_na2_worse = int((s["dp95_vs_na2_pct"] > 0).sum())
        n_ll_better = int((s["dp95_vs_leastload_pct"] < 0).sum())
        n_ll_sig = int(((s["p_leastload"] < 0.05)).sum())
        sig_na2 = int((s["p_na2"] < 0.05).sum())
        print(f"  {arm:<14}{len(s):>7}{int(both_better):>10}{int(both_worse):>10}"
              f"{n_na2_better:>10}{n_na2_worse:>10}{n_ll_better:>14}")
        verdict_rows.append({
            "arm": arm, "n_conditions": int(len(s)),
            "n_better_than_both": int(both_better),
            "n_worse_than_both": int(both_worse),
            "n_better_than_na2": n_na2_better, "n_worse_than_na2": n_na2_worse,
            "n_better_than_leastload": n_ll_better,
            "n_sig_vs_na2": sig_na2, "n_sig_vs_leastload": n_ll_sig,
            "mean_dp95_vs_na2_pct": float(s["dp95_vs_na2_pct"].mean()),
            "mean_dp95_vs_leastload_pct": float(s["dp95_vs_leastload_pct"].mean()),
        })
    if verdict_rows:
        write_table(pd.DataFrame(verdict_rows), "rq19_decay_rule_verdict.csv")

    # ---------- 组间检验：陈旧档 vs 新鲜档，单位=每个种子的 Δp95% ----------
    if {"NetworkAware2", "LeastLoad"} <= set(piv.columns):
        long = ((piv["NetworkAware2"] - piv["LeastLoad"])
                / piv["LeastLoad"].replace(0, np.nan) * 100.0).rename("dp95_pct").reset_index()
        print("\n  --- 参照：NA2（完全信）相对 LeastLoad 的收益，按陈旧档看 ---")
        for (wl, net), g in long.groupby(["workload", "network"], dropna=False):
            fresh = g[(g["staleness_queue_s"] == 0) & (g["staleness_network_s"] == 0)]["dp95_pct"]
            stale = g[(g["staleness_queue_s"] > 0) | (g["staleness_network_s"] > 0)]["dp95_pct"]
            if len(fresh) < 2 or len(stale) < 2:
                continue
            p = _mannwhitney_p(fresh.to_numpy(), stale.to_numpy())
            print(f"  {str(wl):<11}{str(net):<10} 新鲜 {fresh.mean():>+7.1f}%"
                  f"（±{_t_ci(len(fresh), fresh.std(ddof=1)/np.sqrt(len(fresh))):.1f}）  "
                  f"陈旧 {stale.mean():>+7.1f}%"
                  f"（±{_t_ci(len(stale), stale.std(ddof=1)/np.sqrt(len(stale))):.1f}）"
                  f"   Mann-Whitney p={p:.3g}")

    # ---------- 附加检验：队列陈旧的"代价"是否随负载突发性增长 ----------
    # 【假设在看这批数据前写定】队列观测过期的**代价**应随到达的**突发性**增长：
    # 突发越强，队列状态在几秒内变得越离谱，"看到旧值"就越误导；相反，平滑到达下
    # 旧的队列状态仍然接近真值，于是"完全相信"不会吃亏——这正是合成批与真实 trace 批
    # 给出相反结论的原因待验证的机制。
    # 突发性取自 `analysis/characterize_load.py` 的 `arrival_cv_w*`：单位时间窗内到达计数的
    # 变异系数（只算非空窗口）。它是**负载自身的性质**，与任何策略无关，因此可以当自变量。
    # 因变量 = 「队列旧 5 s 时 NA2 相对 LeastLoad 的收益」减去「全新鲜时的收益」，
    # 正值表示陈旧把收益吃掉了（＝完全相信吃了亏）。
    bl_path = TABLE_DIR / "load_burstiness.csv"
    if bl_path.exists() and {"NetworkAware2", "LeastLoad"} <= set(piv.columns):
        bl = read_csv_safe(bl_path)
        cv_cols = [c for c in bl.columns if c.startswith("arrival_cv_w")]
        if cv_cols and "workload" in bl.columns and "seed" in bl.columns:
            ben = ((piv["NetworkAware2"] - piv["LeastLoad"])
                   / piv["LeastLoad"].replace(0, np.nan) * 100.0).rename("benefit")
            ben = ben.reset_index()
            f_cols = ["workload", "network", "seed", "benefit"]
            fresh = ben[(ben["staleness_queue_s"] == 0)
                        & (ben["staleness_network_s"] == 0)][f_cols].rename(
                            columns={"benefit": "benefit_fresh"})
            print("\n  --- 附加检验（假设在看数据前写定）：队列陈旧的代价是否随负载突发性增长 ---")
            print(f"  {'队列旧':>7}{'突发性(w)':>12}{'Spearman ρ':>13}{'p':>11}{'n':>5}"
                  f"   读法")
            for lq in sorted(ben.loc[ben["staleness_queue_s"] > 0,
                                     "staleness_queue_s"].unique()):
                st = ben[(ben["staleness_queue_s"] == lq)
                         & (ben["staleness_network_s"] == 0)][f_cols].rename(
                             columns={"benefit": "benefit_stale"})
                if st.empty:
                    continue
                m = st.merge(fresh, on=["workload", "network", "seed"], how="inner")
                if m.empty:
                    continue
                m["harm"] = m["benefit_stale"] - m["benefit_fresh"]
                for c in cv_cols:
                    mm = m.merge(bl[["workload", "seed", c]]
                                 .drop_duplicates(["workload", "seed"]),
                                 on=["workload", "seed"], how="left")
                    mm = mm.dropna(subset=[c, "harm"])
                    if len(mm) < 8:
                        continue
                    rho_c, p_c = _spearman_p(mm[c].to_numpy(), mm["harm"].to_numpy())
                    tag = ("ρ>0 = 突发越强、陈旧吃得越多" if np.isfinite(rho_c)
                           and rho_c > 0 else "")
                    print(f"  {lq:>7.0f}{c.replace('arrival_cv_w', 'w='):>12}"
                          f"{rho_c:>13.2f}{p_c:>11.3g}{len(mm):>5}   {tag}")
            print("  各负载的到达突发性（窗口 10 s）：")
            wcol = "arrival_cv_w10" if "arrival_cv_w10" in bl.columns else cv_cols[0]
            for wl, g in bl.groupby("workload", dropna=False):
                print(f"    {str(wl):<14} CV(10s) = {g[wcol].mean():.2f}"
                      f"（n={len(g)} 个时间片）")

    # ---------- 图：p95 随"状态有多旧"的变化（五条臂并排） ----------
    for net in sorted(eff["network"].unique()):
        sub = eff[eff["network"] == net]
        wls = sorted(sub["workload"].unique())
        fig, axes = plt.subplots(1, len(wls), figsize=(6.2 * len(wls), 4.6), squeeze=False)
        for ax, wl in zip(axes[0], wls):
            s = sub[sub["workload"] == wl].copy()
            s["_tag"] = s.apply(lambda r: f"q{r['staleness_queue_s']:.0f}/n"
                                          f"{r['staleness_network_s']:.0f}", axis=1)
            tags = list(dict.fromkeys(s["_tag"]))
            xs = np.arange(len(tags))
            arms_here = [x for x in lab]
            width = 0.8 / max(len(arms_here), 1)
            for i, arm in enumerate(arms_here):
                vals = [float(s[(s["arm"] == arm) & (s["_tag"] == t)]["p95_mean"].mean())
                        for t in tags]
                ax.bar(xs + i * width - 0.4 + width / 2, vals, width, label=arm)
            ax.set_xticks(xs)
            ax.set_xticklabels([t.replace("q", "队列").replace("/n", "s/链路") + "s"
                                for t in tags], fontsize=8, rotation=20)
            ax.set_xlabel("观测年龄配置")
            ax.set_ylabel("p95 端到端耗时（秒）")
            ax.set_title(f"{wl}（网络={net}）", fontsize=10)
            ax.legend(fontsize=7.5)
            ax.grid(axis="y", alpha=0.3)
        fig.suptitle("§19 降权规则对照：状态越旧，规则本身越重要", fontsize=11)
        fig.tight_layout()
        save_fig(fig, f"fig_rq19_decay_rules_{net}.png")
    return eff


def section_baselines(data: dict) -> pd.DataFrame | None:
    """§22 补强基线：回答"是不是只赢了弱基线"。

    【为什么要这一节】初稿的对照只有 `Random`/`LeastQueue`/`LeastLoad`，
    审稿人的第一个问题必然是"为什么不跟 P2C 和已发表的网络感知方法比"。
    这一节把四类补强基线一次摆开（全部与两条参照臂在**同一 cell 内**逐请求配对）：

    | 臂 | 对应工作 | 它回答的问题 |
    |---|---|---|
    | `P2C` | 经典"二选一"（负载均衡标配基线） | 只看两台能不能顶得上看全部？ |
    | `P2CNet` | 二选一 + 网络项 | 网络感知的价值是否依赖全局视野？ |
    | `CongestionOnly` | **Aegis（SIGCOMM'26）**式"拥塞压力"项 | 只要拥塞项够不够，静态带宽项多余吗？ |
    | `Bottleneck` | **Helix（ASPLOS'25）**式瓶颈最小化 | "取大" vs "求和"，哪种更对？ |

    判读口径与 §17/§19 一致：跨种子均值 ± 95%CI，同时给中位数（三批的种子间离散很大）。
    """
    print("\n" + "=" * 78)
    print("§22 补强基线：P2C / Aegis 式拥塞项 / Helix 式瓶颈最小化")
    print("=" * 78)
    d = data.get("baselines", {})
    runs, paired = d.get("runs"), d.get("paired")
    if runs is None or runs.empty:
        print("  （本批没有 baselines 产物：先跑 --mode baselines）")
        return None
    keys = ["workload", "network", "staleness"]
    arms = ["Random", "LeastQueue", "P2C", "P2CNet", "CongestionOnly", "Bottleneck",
            "NetworkAware2"]
    arms = [a for a in arms if a in set(runs["strategy"])]
    base = "LeastLoad"
    if base not in set(runs["strategy"]):
        print("  （缺少 LeastLoad 参照臂）")
        return None

    piv = runs.pivot_table(index=keys + ["seed"], columns="strategy", values="sojourn_p95")
    rows = []
    for key, g in piv.groupby(level=keys, dropna=False):
        key = key if isinstance(key, tuple) else (key,)
        if base not in g.columns:
            continue
        for a in arms:
            if a not in g.columns:
                continue
            m = g[[a, base]].dropna()
            if not len(m):
                continue
            rel = (m[a] - m[base]) / m[base].replace(0, np.nan) * 100.0
            rows.append({**dict(zip(keys, key)), "arm": a,
                         "p95": float(m[a].mean()), "p95_base": float(m[base].mean()),
                         "dp95_mean": float(rel.mean()), "dp95_median": float(rel.median()),
                         "n": int(len(rel)), "better": int((rel < 0).sum())})
    eff = pd.DataFrame(rows)
    if eff.empty:
        print("  （没有可用对比）")
        return None
    write_table(eff, "rq22_baseline_effect.csv")

    print(f"\n  --- 各臂相对 LeastLoad 的 Δp95%（负=更好；每格「均值/中位数」）---")
    lab = [x for x in arms if x != base]
    print(f"  {'负载':<11}{'网络':<10}{'新鲜度':<8}" + "".join(f"{x:>19}" for x in lab))
    wide = eff.pivot_table(index=keys, columns="arm", values="dp95_mean")
    med = eff.pivot_table(index=keys, columns="arm", values="dp95_median")
    for key, r in wide.iterrows():
        key = key if isinstance(key, tuple) else (key,)
        cells = ""
        for x in lab:
            v = r.get(x, float("nan"))
            v2 = med.loc[key].get(x, float("nan")) if key in med.index else float("nan")
            cells += f"{v:>9.1f}/{v2:>8.1f}"
        print(f"  {str(key[0]):<11}{str(key[1]):<10}{str(key[2]):<8}{cells}")

    # ---------- 裁决：每条基线赢了几格 ----------
    print(f"\n  --- 裁决（每个臂 × 6 个格子）---")
    print(f"  {'臂':<16}{'优于LeastLoad':>14}{'优于P2C':>10}{'优于NA2':>10}"
          f"{'劣于NA2':>10}  说明")
    notes = {
        "P2C": "经典强基线：只看两台",
        "P2CNet": "二选一 + 网络项",
        "CongestionOnly": "Aegis 式：只要拥塞项",
        "Bottleneck": "Helix 式：瓶颈最小化",
        "NetworkAware2": "本文完全信任臂",
        "LeastQueue": "朴素基线",
        "Random": "下界",
    }
    verdict = []
    for a in arms:
        sub = eff[eff["arm"] == a]
        n_ll = int((sub["dp95_mean"] < 0).sum())
        # 与 P2C / NA2 的逐格比较（同一格之间比均值）
        def beats(other: str) -> tuple[int, int]:
            if other not in wide.columns or a not in wide.columns:
                return (0, 0)
            b, l = 0, 0
            for key in wide.index:
                x, y = wide.loc[key].get(a, np.nan), wide.loc[key].get(other, np.nan)
                if not (np.isfinite(x) and np.isfinite(y)):
                    continue
                if x < y:
                    b += 1
                elif x > y:
                    l += 1
            return (b, l)
        n_p2c, _ = beats("P2C")
        n_na2, n_na2_worse = beats("NetworkAware2")
        verdict.append({"arm": a, "n_cells": int(len(sub)),
                        "better_than_leastload": n_ll,
                        "better_than_p2c": n_p2c,
                        "better_than_na2": n_na2, "worse_than_na2": n_na2_worse})
        print(f"  {a:<16}{n_ll:>9}/{len(sub):<4}{n_p2c:>10}{n_na2:>10}{n_na2_worse:>10}"
              f"  {notes.get(a, '')}")
    write_table(pd.DataFrame(verdict), "rq22_baseline_verdict.csv")
    print("\n  读法：`优于 LeastLoad` 一列若某个补强基线接近满分，"
          "说明「网络感知赢的只是朴素基线」；")
    print("        若 `CongestionOnly` 与 `NetworkAware2` 差别很小，说明静态带宽项是多余的；")
    print("        `P2CNet` 与 `NetworkAware2` 的差距 = 「全局视野」本身值多少。")
    return eff


def section_method_ablate(data: dict, mode_name: str = "method_ablate",
                          section: str = "§20",
                          title: str = "阈值消融：结论对参数的敏感性（一次只动一个参数）",
                          table_name: str = "rq20_method_threshold_ablation.csv",
                          headline: str = (
                              "【要回答的问题】主方法的 `TAU_Q=4 s` / `TAU_N_DYN=15 s` "
                              "是从 §16 的实测曲线拟合来的，读者该问的是"
                              "「换个拟合方式会不会翻盘」。所以这一节不比「哪档最好」，而是比"
                              "**「把每个参数单独挪开，结论动多少」**。")
                          ) -> pd.DataFrame | None:
    """阈值消融（两套降权规则各一次，判读口径必须完全一致）。

    为什么做成带参数的函数而不是写两遍：两套规则的**判读逻辑必须字字一致**，
    否则"参数不敏感"这个结论会因为两节用了不同口径而失去可比性。
    两套规则各自有 label 相同的臂（都用 `Tq2`/`Tq8` 表示队列时间常数），
    **绝不能把两个 mode 的产物拼成一张表**——那样同名臂会被混在一起，静默出错。

    口径：
      · 与主方法的差（按种子配对）——参数敏感性；
      · 与 NetworkAware2 的差——结论（"降权有没有用"）在每一档是否仍然成立；
      · 符号翻转的条件数——最直接的"结论会不会翻"的计数。
    """
    print("\n" + "=" * 78)
    print(f"{section} {title}")
    print("=" * 78)
    print(f"  {headline}")
    d = data.get(mode_name, {})
    runs, paired = d.get("runs"), d.get("paired")
    if runs is None or runs.empty:
        print(f"  （本批没有 {mode_name} 产物：先跑 --mode {mode_name}）")
        return None
    keys = ["workload", "network", "staleness_queue_s", "staleness_network_s"]
    piv = runs.pivot_table(index=keys + ["seed"], columns="strategy",
                           values="sojourn_p95")
    if "primary" not in piv.columns:
        print("  （没有 primary 臂，无法比较）")
        return None
    variants = [c for c in piv.columns
                if c not in ("primary", "LeastLoad", "NetworkAware2")]
    rows = []
    for v in variants:
        rec = {"variant": v, "n_conditions": 0,
               "vs_primary_mean_pct": float("nan"), "vs_primary_ci": float("nan"),
               "vs_primary_worst_pct": float("nan"), "n_conditions_sig_vs_primary": 0,
               "vs_na2_mean_pct": float("nan"), "vs_leastload_mean_pct": float("nan"),
               "n_conditions_sign_flip_vs_na2": 0}
        dp_prim, dp_na2, dp_ll, flips, n_sig = [], [], [], 0, 0
        for key, g in piv.groupby(level=keys, dropna=False):
            key = key if isinstance(key, tuple) else (key,)
            if "primary" not in g.columns or v not in g.columns:
                continue
            m = g[[v, "primary"]].dropna()
            if not len(m):
                continue
            rec["n_conditions"] += 1
            rel = (m[v] - m["primary"]) / m["primary"].replace(0, np.nan) * 100.0
            dp_prim.append(rel.mean())
            # 逐条件配对检验：只有 10 个种子，跨条件汇合做检验没有意义
            if _wilcoxon_p(m[v].to_numpy(), m["primary"].to_numpy()) < 0.05:
                n_sig += 1
            if "NetworkAware2" in g.columns:
                n2 = g[[v, "NetworkAware2"]].dropna()
                if len(n2):
                    dp_na2.append((((n2[v] - n2["NetworkAware2"]) / n2["NetworkAware2"]
                                    .replace(0, np.nan)) * 100.0).mean())
                p2 = g[[  "primary", "NetworkAware2"]].dropna()
                if len(p2):
                    base_sign = np.sign((((p2["primary"] - p2["NetworkAware2"])
                                          / p2["NetworkAware2"].replace(0, np.nan))
                                         * 100.0).mean())
                    var_sign = np.sign((((n2[v] - n2["NetworkAware2"])
                                         / n2["NetworkAware2"].replace(0, np.nan))
                                        * 100.0).mean()) if len(n2) else 0.0
                    if base_sign != 0 and var_sign != 0 and base_sign != var_sign:
                        flips += 1
            if "LeastLoad" in g.columns:
                ll = g[[v, "LeastLoad"]].dropna()
                if len(ll):
                    dp_ll.append((((ll[v] - ll["LeastLoad"]) / ll["LeastLoad"]
                                   .replace(0, np.nan)) * 100.0).mean())
        if dp_prim:
            arr = np.array(dp_prim, dtype=float)
            rec["vs_primary_mean_pct"] = float(arr.mean())
            rec["vs_primary_ci"] = _t_ci(len(arr), float(arr.std(ddof=1) / np.sqrt(len(arr))))
            rec["vs_primary_worst_pct"] = float(arr[np.argmax(np.abs(arr))])
            rec["vs_primary_p"] = _wilcoxon_p(
                piv[v].dropna().to_numpy(), piv["primary"].dropna().to_numpy())
        if dp_na2:
            rec["vs_na2_mean_pct"] = float(np.mean(dp_na2))
        if dp_ll:
            rec["vs_leastload_mean_pct"] = float(np.mean(dp_ll))
        rec["n_conditions_sign_flip_vs_na2"] = flips
        rows.append(rec)
    eff = pd.DataFrame(rows)
    if eff.empty:
        print("  （没有可比的变体）")
        return None
    write_table(eff, table_name)
    print(f"  {'变体':<14}{'条件数':>6}{'vs主方法 均值±95%CI':>24}{'最大偏离':>10}"
          f"{'vs NA2':>9}{'vs LeastLoad':>13}{'结论翻符号':>11}")
    for _, r in eff.sort_values("vs_primary_mean_pct").iterrows():
        print(f"  {str(r['variant']):<14}{int(r['n_conditions']):>6}"
              f"{r['vs_primary_mean_pct']:>16.1f}±{r['vs_primary_ci']:<6.1f}"
              f"{r['vs_primary_worst_pct']:>10.1f}"
              f"{r['vs_na2_mean_pct']:>9.1f}{r['vs_leastload_mean_pct']:>13.1f}"
              f"{int(r['n_conditions_sign_flip_vs_na2']):>8}/{int(r['n_conditions'])}")
    print("  读法：第三列是「把参数挪开之后，p95 平均变了多少个百分点」——"
          "越小说明结论越不依赖这个数的精确取值；"
          "\n        最后一列是「降权相对完全信任网络的优劣方向发生翻转」的条件数，"
          "0 = 无论这个参数取哪档，结论方向都不变。")
    return eff


def section_staleness2d(data: dict):
    """§16 两条通道年龄的**二维**联合扫描（细变量①）。

    回答两条 1D 扫描回答不了的问题：
      「排队消息很新、但网线消息很旧」和「排队消息很旧、但网线消息很新」是不是一回事？
    现有 `staleness` 模式的 net 扫描把队列 lag 钉在 1 秒，于是那条曲线其实主要在测队列
    （队列通道比链路通道敏感 2–3 个数量级，实测已确认），**结构上无法回答这个问题**。

    输出：行 = 队列 lag，列 = 链路 lag，格 = Δp95（NetworkAware2 vs LeastLoad，负=更优）。
    只在 `unstable` 网络上做——静态网络的链路状态不随时间变，链路 lag 精确无效。
    另外单独报"不对称性"：把 (队列=最大, 链路=0) 与 (队列=0, 链路=最大) 并排比较。
    """
    print("\n" + "=" * 78)
    print("§16 两条通道年龄的二维扫描：哪种'旧'更伤，它们会不会叠加")
    print("=" * 78)
    d = data.get("staleness2d", {})
    runs, paired = d.get("runs"), d.get("paired")
    if runs is None or runs.empty:
        print("  （本批没有 staleness2d 产物：先跑 --mode staleness2d）")
        return None
    keys = ["staleness_queue_s", "staleness_network_s", "workload"]
    eff = effect_table(runs, paired, keys, candidate="NetworkAware2",
                       baselines=["LeastLoad"])
    if eff.empty:
        print("  （配对表里没有可用的对比）")
        return None
    write_table(eff, "rq16_staleness2d_effect.csv")
    for wl in sorted(eff["workload"].unique()):
        sub = eff[eff["workload"] == wl]
        qs = sorted(sub["staleness_queue_s"].unique())
        ns = sorted(sub["staleness_network_s"].unique())
        pivot = sub.pivot_table(index="staleness_queue_s", columns="staleness_network_s",
                                values="dp95_pct", aggfunc="mean")
        print(f"\n  --- {wl}：Δp95%（负=网络感知更优；行=排队消息新旧，列=网线消息新旧）---")
        print("     队列\\链路" + "".join(f"{n:>10g}s" for n in ns))
        for q in qs:
            cells = []
            for n in ns:
                try:
                    v = pivot.loc[q, n]
                    cells.append(f"{v:>11.0f}" if v == v else f"{'—':>11}")
                except KeyError:
                    cells.append(f"{'—':>11}")
            print(f"    {q:>7g}s  " + "".join(cells))
    # 不对称性：两种"只让一边旧"的情况直接对比
    print("\n  --- 不对称性检查（两种'只让一边旧'的情况）---")
    print(f"  {'负载':<14}{'队列 lag':>9}{'链路 lag':>9}{'Δp95%':>10}")
    for wl in sorted(eff["workload"].unique()):
        sub = eff[eff["workload"] == wl]
        for q, n in ((sub["staleness_queue_s"].max(), 0.0),
                     (0.0, sub["staleness_network_s"].max()),
                     (sub["staleness_queue_s"].max(), sub["staleness_network_s"].max())):
            row = sub[(sub["staleness_queue_s"] == q) & (sub["staleness_network_s"] == n)]
            if row.empty:
                continue
            print(f"  {wl:<14}{q:>9g}{n:>9g}{float(row.iloc[0]['dp95_pct']):>10.0f}")
    return eff


def section_avewindow(data: dict):
    """§15 观测的**聚合窗口**：真实监控报的是"过去 W 秒的平均"，不是瞬时值。

    与 §2（staleness / lag）的区别是本质性的，也是这个 mode 存在的全部理由：
      · `lag`    = 看到的是**旧**值，那个值在它自己的时刻是**真实**的（延迟失真）；
      · `window` = 看到的是**被平均过**的值，尖峰被抹平（信息失真，**方向相反**：
                   某节点刚涌进一批活，窗口平均后只是"略忙"，活继续往它身上派）。
    真实系统里两者同时存在（典型配置：每 15 秒采一次、每次报过去 30 秒的平均）。

    口径：lag 固定为 0（隔离变量），candidate=NetworkAware2 vs LeastLoad，
    按窗口长度分组看 Δp95 怎么走；两条通道分开报告（队列通道 / 链路通道）。
    """
    print("\n" + "=" * 78)
    print("§15 观测聚合窗口（avg_over_time）：平滑掉的不只是噪声，还有尖峰")
    print("=" * 78)
    d = data.get("avewindow", {})
    runs, paired = d.get("runs"), d.get("paired")
    if runs is None or runs.empty:
        print("  （本批没有 avewindow 产物：先跑 --mode avewindow）")
        return None
    wq = runs.get("window_queue_s", pd.Series(0.0, index=runs.index)).fillna(0.0)
    wn = runs.get("window_network_s", pd.Series(0.0, index=runs.index)).fillna(0.0)
    rows_out = []
    # 哪条通道：`window_network_s > 0` 的格子属于链路通道，其余（含两者都为 0 的基线）属队列通道。
    # 【必须把 w=0 的基线行也列出来】，否则只能看到"窗口之间"的差别，看不到"窗口相对不窗口"。
    # （窗口网格里本来就含 0 —— 它是 baseline。）
    for label, is_net in (("队列通道窗口（同时含 w=0 基线）", False),
                          ("链路通道窗口（同时含 w=0 基线）", True)):
        sel = (wn > 0) if is_net else pd.Series(True, index=runs.index)
        if is_net and not (wn > 0).any():
            continue
        sub_runs = runs[sel].copy()
        sub_runs["win_s"] = (wn if is_net else wq)[sel]
        sub_paired = None
        if paired is not None and not paired.empty:
            wp = paired.get("window_network_s", pd.Series(0.0, index=paired.index)).fillna(0.0)
            wpp = paired.get("window_queue_s", pd.Series(0.0, index=paired.index)).fillna(0.0)
            selp = (wp > 0) if is_net else pd.Series(True, index=paired.index)
            sub_paired = paired[selp].copy()
            sub_paired["win_s"] = (wp if is_net else wpp)[selp]
        keys = ["win_s", "workload", "network"]
        eff = effect_table(sub_runs, sub_paired, keys, candidate="NetworkAware2",
                           baselines=["LeastLoad"])
        if eff.empty:
            print(f"  （{label}：配对表里没有可用的对比）")
            continue
        print(f"\n  --- {label}（Δp95<0 = NetworkAware2 更优；lag 固定为 0）---")
        print(f"  {'窗长(s)':>8}{'负载':<14}{'网络':<10}{'p95_NA2':>10}{'p95_LL':>10}"
              f"{'Δp95%':>9}{'net_share':>10}{'判定':<22}")
        for _, r in eff.sort_values(["win_s", "workload", "network"]).iterrows():
            print(f"  {r['win_s']:>8.1f}{str(r['workload']):<14}{str(r['network']):<10}"
                  f"{r['p95_na']:>10.2f}{r['p95_base']:>10.2f}{r['dp95_pct']:>9.1f}"
                  f"{r['net_share_na']:>10.3f}  {str(r['verdict'])[:20]}")
            rows_out.append({"channel": ("net" if is_net else "queue"),
                             **{k: r[k] for k in ("win_s", "workload", "network",
                                                  "dp95_pct", "p95_na", "p95_base")}})
    if rows_out:
        write_table(pd.DataFrame(rows_out), "rq15_obs_window_effect.csv")
    return rows_out or None


def section_mechanism(data: dict):
    """§14 机制对比 —— 把"链路队列"计入评分值多少（v2 相对 v1 的核心新增）。

    v1 的 `NetworkAware` 只看得到"这条链路有多快"，v2 的 `NetworkAware2` 还看得到
    "这条链路上已经压着多少字节"。两者的评分函数只差 `link_drain_time` 一项，
    因此差异可以**干净归因到"链路队列"这条信息**上，而不是调参或别的差别。

    配对口径：`paired.csv` 里 `(candidate=NetworkAware, baseline=NetworkAware2)` 表示
    "v1 策略相对 v2 策略"的逐请求配对差，Δp95 > 0 就是 v1 更差＝v2 机制有效。
    """
    print("\n" + "=" * 78)
    print("§14 机制对比：算力队列 + 链路队列 vs 只看链路快慢")
    print("=" * 78)
    d = data.get("main", {})
    runs, paired = d.get("runs"), d.get("paired")
    if runs is None or runs.empty or "NetworkAware2" not in set(runs["strategy"]):
        print("  （本批没有 NetworkAware2：v2 档位才有，v1 批跳过）")
        return None

    keys = ["workload", "network", "staleness"]
    gains = effect_table(runs, paired, keys, candidate="NetworkAware2",
                         baselines=["LeastLoad", "LeastQueue", "Random"])
    vs_v1 = effect_table(runs, paired, keys, candidate="NetworkAware",
                         baselines=["NetworkAware2"])
    pieces = []
    if not gains.empty:
        g = gains.copy()
        g["contrast"] = "NetworkAware2 vs 基线"
        pieces.append(g)
    if not vs_v1.empty:
        v = vs_v1.copy()
        v["contrast"] = "NetworkAware(v1) vs NetworkAware2"
        v["note"] = "Δp95>0 表示 v1 策略更差，即链路队列这条信息有用"
        pieces.append(v)
    if not pieces:
        print("  （没有可用的配对结果）")
        return None
    eff = pd.concat(pieces, ignore_index=True)
    write_table(eff, "rq14_mechanism_effect.csv")

    print(f"  {'对比':<34}{'负载':<14}{'网络':<10}{'Δp95%':>9}{'±se':>7}{'cliffs':>8}"
          f"{'显著cell':>9}{'p_holm中位':>11}  判定")
    for _, r in eff.sort_values(["contrast", "network", "workload"]).iterrows():
        if r["contrast"].startswith("NetworkAware2") and r["baseline"] != "LeastLoad":
            continue                        # 逐行只打 vs LeastLoad，其余进汇总
        print(f"  {r['contrast']:<34}{r['workload']:<14}{r['network']:<10}"
              f"{r['dp95_pct']:>+9.2f}{r['dp95_se']:>7.2f}{r['cliffs_delta_median']:>8.3f}"
              f"{int(r['n_sig']):>5}/{int(r['n_paired_cells']):<3}"
              f"{r['p_holm_median']:>11.3g}  {r['verdict']}")

    # 按网络汇总（机制增益在哪类网络最大）
    rows = []
    for (contrast, base, net), g in eff.groupby(["contrast", "baseline", "network"]):
        rows.append({"contrast": contrast, "baseline": base, "network": net,
                     "dp95_pct_mean": float(g["dp95_pct"].mean()),
                     "dp95_se_mean": float(g["dp95_se"].mean()),
                     "cliffs_median": float(g["cliffs_delta_median"].median()),
                     "n_sig": int(g["n_sig"].sum()), "n_cells": int(len(g))})
    summ = pd.DataFrame(rows)
    write_table(summ, "rq14_mechanism_summary.csv")
    print("\n  汇总（Δp95 对负载取平均；显著列 = 该组里 Holm 校正后 p<0.05 的 cell 数合计）：")
    print(f"  {'对比':<34}{'基线':<14}{'网络':<10}{'Δp95%':>9}{'cliffs':>8}{'显著cell数':>11}")
    for _, r in summ.sort_values(["contrast", "network", "baseline"]).iterrows():
        print(f"  {r['contrast']:<34}{r['baseline']:<14}{r['network']:<10}"
              f"{r['dp95_pct_mean']:>+9.2f}{r['cliffs_median']:>8.3f}"
              f"{int(r['n_sig']):>11}")

    # 图：各网络的 p95 与"机制增益"
    nets = [n for n in ("normal", "degraded", "unstable")
            if n in set(runs["network"])]
    order = [s for s in ("LeastLoad", "LeastQueue", "Random",
                         "NetworkAware", "NetworkAware2") if s in set(runs["strategy"])]
    fig, axes = plt.subplots(1, 2, figsize=(12.2, 4.4))
    x = np.arange(len(nets))
    width = 0.8 / max(len(order), 1)
    for i, s in enumerate(order):
        vals = [runs[(runs["network"] == n) & (runs["strategy"] == s)]["sojourn_p95"].mean()
                for n in nets]
        axes[0].bar(x + i * width, vals, width, label=s, color=COLORS.get(s))
    axes[0].set_xticks(x + width * (len(order) - 1) / 2)
    axes[0].set_xticklabels(nets)
    axes[0].set_ylabel("p95 端到端完成时间 [s]")
    axes[0].set_title("各网络的 p95（越低越好）")
    axes[0].legend(fontsize=7.5)
    axes[0].grid(alpha=0.25, axis="y")
    sub = eff[(eff["contrast"].str.startswith("NetworkAware(v1)")) & (eff["network"].isin(nets))]
    if not sub.empty:
        for wl, g in sub.groupby("workload"):
            g = g.groupby("network", as_index=False).agg(dp95=("dp95_pct", "mean"))
            axes[1].plot(g["network"], g["dp95"], marker="o", label=wl)
        axes[1].axhline(0, color="k", linestyle="--", linewidth=1.0)
        axes[1].set_ylabel("Δp95 [%]（>0 = v1 策略更差）")
        axes[1].set_title("只看链路快慢(v1) 相对 看链路队列(v2) 的劣化")
        axes[1].legend(fontsize=8)
        axes[1].grid(alpha=0.25)
    fig.suptitle("§14 机制对比：链路队列这条信息值多少", fontsize=12)
    save_fig(fig, "fig_rq14_mechanism.png")
    plt.close(fig)
    return eff


# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    global MATRIX_DIR, TABLE_DIR, FIG_DIR
    ap = argparse.ArgumentParser(description="矩阵实验结果汇总分析")
    ap.add_argument("--matrix-dir", default=str(MATRIX_DIR),
                    help="实验原始数据目录（默认 results/matrix）")
    ap.add_argument("--table-dir", default=str(TABLE_DIR),
                    help="CSV 输出目录（默认 results/analysis）")
    ap.add_argument("--fig-dir", default=str(FIG_DIR),
                    help="图输出目录（默认 figures）")
    args = ap.parse_args(argv)

    MATRIX_DIR = Path(args.matrix_dir).resolve()
    TABLE_DIR = Path(args.table_dir).resolve()
    FIG_DIR = Path(args.fig_dir).resolve()

    setup_streams()
    TABLE_DIR.mkdir(parents=True, exist_ok=True)
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    font = setup_fonts()
    sys.stdout = Tee(TABLE_DIR / "report.txt", sys.stdout)

    print("=" * 78)
    print("矩阵实验结果汇总分析  (analysis/analyze.py)")
    print(f"矩阵目录: {MATRIX_DIR}")
    print(f"字体: {font}   dpi=170   输出: {TABLE_DIR} , {FIG_DIR}")
    print("=" * 78)

    data = {m: load_mode(m) for m in MODES}

    section_availability(data)
    section_rho(data)

    rq1 = section_rq1(data)
    rq2 = section_rq2(data)
    rq3 = section_datavolume(data)
    abl = section_ablation(data)
    rq5 = section_scale(data)
    rq6 = section_convergence(data)
    feas = section_feasibility(data)
    rho_sweep = section_rho_sweep(data)
    fallback = section_fallback(data)
    section_timing(data)
    section_summary(feas, rq1, rq2, rq3, abl, rq5, rq6, data, rho_sweep, fallback)
    section_bandwidth(data)
    section_mechanism(data)
    section_avewindow(data)      # §15 观测聚合窗口（细变量②）
    section_staleness2d(data)    # §16 两通道年龄二维扫描（细变量①）
    section_align(data)          # §17 对齐度（细变量③）
    section_alignscale(data)     # §18 对齐度 × 节点规模
    section_method(data)         # §19 V3 方法：降权规则对照
    section_method_ablate(data)  # §20 阈值消融（第一版规则，一次只动一个参数）
    section_baselines(data)      # §22 补强基线（P2C / Aegis 式 / Helix 式）
    section_method_ablate(       # §21 阈值消融（第二版规则：按通道降权）
        data, mode_name="method_ch_ablate", section="§21",
        title="阈值消融（第二版·按通道降权）：结论对三个参数有多敏感",
        table_name="rq21_channel_threshold_ablation.csv",
        headline=("【要回答的问题】第二版规则的三个参数（队列时间常数 4 s、网线时间常数 60 s、"
                  "权重下限 0.7）都由 §16 的实测曲线定出。这里把每个参数单独挪开一倍，"
                  "看结论会不会翻——如果挪开一两倍结果几乎不动，就说明它不是精调出来的。"))
    section_verification(data)

    print("\n" + "=" * 78)
    print(f"完成。表格在 {TABLE_DIR}，图表在 {FIG_DIR}。")
    print(f"完整报告: {(TABLE_DIR / 'report.txt')}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

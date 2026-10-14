"""负载强度刻画 —— 把 rho 拆成「均值」与「瞬时」两部分。

为什么必须单独有这张表：
    主矩阵把平均负载强度标定在 rho=0.600，但同一个 cell 里的 p95 sojourn
    能到 100 s 量级，是平均服务时间（0.1 s 量级）的几百倍。
    这不是实现错误，而是真实 trace 的到达过程有强突发：改用 10 s 窗口看，
    局部 rho 会超过 1（瞬态过载），队列在突发期排不空。
    只报平均 rho 既解释不了尾部，也回答不了审稿人一定会问的
    "rho=0.6 为什么会有几百倍的服务时间尾巴"。

本脚本用**与矩阵完全相同**的负载构造代码（直接导入 experiments/run_matrix.py，
不复制公式），因此表里的数字与 runs.csv 里的对应行是同源的。

输出列：
    rho_mean          整段的平均负载强度（与 analyze.add_rho 同口径）
    rho_local_p95     10 s 窗口局部 rho 的 p95
    rho_local_max     局部 rho 的最大值
    overload_frac     局部 rho > 1 的窗口占比（瞬态过载比例）
    arrival_cv        单位窗口到达量的变异系数（突发强度）
    mean_work/qps     标定结果，便于人工复核

用法：
    sim/.venv/Scripts/python.exe analysis/characterize_load.py --source trace \\
        --table-dir results/analysis_trace --seeds 1 2 3 4 5
    sim/.venv/Scripts/python.exe analysis/characterize_load.py --source synthetic \\
        --table-dir results/analysis
产物：
    {table_dir}/load_burstiness.csv
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# 负载构造代码在 experiments/ 下，必须与矩阵共用同一份，避免公式复制后走样
sys.path.insert(0, str(ROOT / "experiments"))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

TARGET_RHO_DEFAULT = 0.6
# 局部 rho 的统计窗口（秒）。三档一起报，避免"窗口选得巧"变成结论。
# 注意标定会整体缩放时间轴，所以窗口是相对**缩放后**的时间轴。
WINDOWS_S_DEFAULT = [1.0, 10.0, 60.0]


def load_window_stats(workload: list, capacity: float,
                      windows: list[float]) -> dict:
    """从 (arrival, ctx, gen) 列表里算局部负载强度，**多个时间窗分别算**。

    局部 rho 的定义与全局一致，只是把统计窗口缩小：
        rho_window = Σ(窗口内工作量) / (窗口宽度 × 系统容量)

    为什么必须给多个窗口：标定到达率时会整体缩放时间轴（乘一个 scale），
    而固定宽度窗口里的平均会随缩放改变——压缩时间轴会让 10 s 窗口覆盖更多原始时间，
    把突发"抹平"。只报一个窗口，读者无法判断"局部过载"是真实现象还是窗口选得巧。
    1 s / 10 s / 60 s 三档一起报，读者自己就能看出突发在哪个尺度上。
    """
    t = np.array([w[0] for w in workload], dtype=float)
    work = np.array([1.0 * c + 4.0 * g for _, c, g in workload], dtype=float)
    span = float(t[-1] - t[0]) if len(t) > 1 else 0.0
    out = {"rho_mean": float(work.sum()) / span / capacity if span > 0 else float("nan"),
           "span_s": span}
    if span <= 0:
        for w in windows:
            out.update({f"rho_local_p95_w{w:g}": float("nan"),
                        f"rho_local_max_w{w:g}": float("nan"),
                        f"overload_frac_w{w:g}": float("nan"),
                        f"arrival_cv_w{w:g}": float("nan")})
        out["n_windows_w1"] = 0
        return out

    for w in windows:
        bins = np.arange(0.0, span + w, w)
        idx = np.clip(np.digitize(t - t[0], bins) - 1, 0, len(bins) - 2)
        n_win = len(bins) - 1
        counts = np.bincount(idx, minlength=n_win).astype(float)
        loads = np.bincount(idx, weights=work, minlength=n_win)
        local_rho = loads / w / capacity
        # 尾部空窗口会把 CV 拉高，用非空窗口算突发强度更诚实
        non_empty = counts[counts > 0]
        out.update({
            f"rho_local_p95_w{w:g}": float(np.percentile(local_rho, 95)),
            f"rho_local_max_w{w:g}": float(local_rho.max()),
            f"overload_frac_w{w:g}": float(np.mean(local_rho > 1.0)),
            f"arrival_cv_w{w:g}": (float(non_empty.std() / non_empty.mean())
                                   if len(non_empty) else float("nan")),
            f"n_windows_w{w:g}": int(n_win),
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="负载强度（均值 / 瞬时）刻画")
    ap.add_argument("--source", choices=["synthetic", "trace"], default="trace")
    ap.add_argument("--trace", default=str(ROOT / "data/raw/azure_code_1week.csv"))
    ap.add_argument("--seeds", type=int, nargs="*", default=[1, 2, 3, 4, 5])
    ap.add_argument("--requests", type=int, default=20000)
    ap.add_argument("--n-nodes", type=int, default=3)
    ap.add_argument("--target-rho", type=float, default=TARGET_RHO_DEFAULT)
    ap.add_argument("--windows", type=float, nargs="*", default=WINDOWS_S_DEFAULT,
                    help="局部 rho 的统计窗口（秒），可给多档")
    ap.add_argument("--workloads", nargs="*", default=None,
                    help="只刻画这些负载类（默认为全部）。"
                         "存在的理由：不同类的可切出请求数差很多，短 trace 上按全部类跑会"
                         "因为某一类切不够而整轮失败——而主实验可能只用其中两类，"
                         "切多切的那些类既费时又碍事。")
    ap.add_argument("--table-dir", default=str(ROOT / "results/analysis"))
    args = ap.parse_args()

    import run_matrix as rmx                     # noqa: E402
    from src.workload import WORKLOAD_CLASSES     # noqa: E402

    rmx.TARGET_RHO = args.target_rho
    nodes = rmx.make_nodes(args.n_nodes, rmx.NETWORK_CONDITIONS["degraded"]["links"])
    capacity = sum(n.compute_rate * n.slots for n in nodes)

    segments: list[list] = []
    if args.source == "trace":
        t0 = time.perf_counter()
        print(f"读取 trace：{args.trace}（首次解析需 1–3 分钟）", flush=True)
        trace_workload, stats = rmx.load_trace_auto(args.trace)
        print(f"  {len(trace_workload):,} 请求 / {stats.duration_s/3600:.1f} 小时"
              f"（{time.perf_counter()-t0:.0f}s）")
        segments = rmx.split_trace_segments(trace_workload, max(args.seeds))
        rmx._TRACE_SEGMENTS = segments

    print(f"来源={args.source}  容量={capacity:,.0f}  目标 rho={args.target_rho}  "
          f"窗口={args.windows}s")
    rows = []
    classes = {k: v for k, v in WORKLOAD_CLASSES.items()
               if args.workloads is None or k in args.workloads}
    if not classes:
        print(f"  --workloads 里没有一个已知的负载类；可选：{list(WORKLOAD_CLASSES)}")
        return 2
    for name, spec in classes.items():
        for seed in args.seeds:
            if args.source == "trace":
                workload, qps, mean_work = rmx.build_trace_cell_workload(
                    spec, segments[seed - 1], args.requests, capacity,
                    target_rho=args.target_rho)
            else:
                workload, qps, mean_work = rmx.build_synthetic_cell_workload(
                    spec, args.requests, seed, capacity, target_rho=args.target_rho)
            rec = load_window_stats(workload, capacity, args.windows)
            rec.update({"data_source": args.source, "workload": name, "seed": seed,
                        "n_requests": len(workload), "mean_work": mean_work,
                        "qps": qps})
            rows.append(rec)
            detail = "  ".join(
                f"w{w:g}: p95={rec[f'rho_local_p95_w{w:g}']:.2f} "
                f"max={rec[f'rho_local_max_w{w:g}']:.2f} "
                f"过载{rec[f'overload_frac_w{w:g}']*100:.0f}%" for w in args.windows)
            print(f"  {name:<14} seed{seed}  rho_mean={rec['rho_mean']:.3f}  {detail}")

    df = pd.DataFrame(rows)
    front = ["data_source", "workload", "seed", "n_requests", "mean_work", "qps",
             "rho_mean", "span_s"]
    rest = [c for c in df.columns if c not in front]
    df = df[front + rest]
    out_dir = Path(args.table_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "load_burstiness.csv"
    df.to_csv(out, index=False)
    print(f"\n[表] {out}  ({len(df)} 行，{len(df.columns)} 列)")
    for w in args.windows:
        print(f"  窗口 {w:g}s：局部 rho>1 的窗口占比 中位 "
              f"{df[f'overload_frac_w{w:g}'].median()*100:.1f}%  最大 "
              f"{df[f'overload_frac_w{w:g}'].max()*100:.1f}% ；"
              f"局部 rho max 最大 {df[f'rho_local_max_w{w:g}'].max():.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

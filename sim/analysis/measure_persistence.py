"""测量负载的「速率变化持续性」——用来解释"什么时候打折才有用"。

用法（在 `sim/` 下执行）：

    .venv/Scripts/python.exe analysis/measure_persistence.py --source synthetic \
        --seeds 1 2 3 4 5 6 7 8 9 10 --requests 12000 --table-dir results/analysis_v2
    .venv/Scripts/python.exe analysis/measure_persistence.py --source trace \
        --trace data/raw/azure_code_1week.csv ... --table-dir results/analysis_trace_v2

产物：`<table-dir>/load_persistence.csv`（每行 = 一个负载类 × 一个时间片）。

【为什么要这个量】`analyze.py` §19 的附加检验已经否掉了"突发性（变异系数）"这个解释：
三批按 1 秒窗到达 CV 排序是 Azure 0.28 < 合成 0.62 < BurstGPT 1.7，
但"收益被陈旧吃掉的幅度"是 Azure 最大 ≈ BurstGPT > 合成，**不单调**。

剩下的候选解释是**持续性**：
  · 合成的泊松噪声是**瞬间的**——速率这一刻跳高、下一刻就回来，
    所以"5 秒前的快照"只是**噪声**，平均下来仍然对；
  · Azure 有**昼夜爬坡**、BurstGPT 有**持续几十秒的洪峰**——速率的变化**不回来**，
    所以"5 秒前的快照"是**系统性错误的**，平均也救不回来。
这就是「变异系数（幅度）」与「自相关／漂移（持续性）」的区别，也是本脚本存在的全部理由。

两个指标（都只看到达流，与任何策略无关）：
  1. `rate_change_5s`：把到达按 5 秒滑动窗算速率，比较相邻窗的**平均相对变化**
     `mean|r(t) − r(t−5)| / mean(r)`——"5 秒里速率变了多少"；
  2. `autocorr_1s`：1 秒计数序列的**滞后 1 自相关**——"变化会不会回来"。
     平稳泊松 ≈ 0；持续爬坡 → 接近 1。
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                        # noqa: BLE001
        pass

import importlib.util                                        # noqa: E402


def _load_matrix_module():
    """复用 `run_matrix` 的负载构造（必须复用，否则测的不是同一份负载）。"""
    spec = importlib.util.spec_from_file_location(
        "rm_persist", ROOT / "experiments" / "run_matrix.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def windowed(series: np.ndarray, w: int) -> np.ndarray:
    """滑动窗和（窗口 w 个桶）——用前缀和算，避免逐窗循环。"""
    c = np.concatenate([[0.0], np.cumsum(series)])
    return c[w:] - c[:-w]


def persistence_metrics(arrival_s: np.ndarray, work: np.ndarray) -> dict:
    """从到达时刻与工作量算持续性指标（桶宽 1 秒）。"""
    t0 = float(arrival_s[0])
    span = float(arrival_s[-1] - t0)
    if span <= 0:
        return {}
    n_bins = int(np.floor(span)) + 1
    idx = np.clip((arrival_s - t0).astype(int), 0, n_bins - 1)
    counts = np.bincount(idx, minlength=n_bins).astype(float)
    loads = np.bincount(idx, weights=work, minlength=n_bins)
    out = {"span_s": span, "n_bins_1s": n_bins,
           "arrival_mean_rate_1s": float(counts.mean()),
           "work_mean_rate_1s": float(loads.mean())}

    # --- ① 速率变化的幅度（5 秒滑动窗，相邻窗的平均相对变化）---
    for w in (5, 15):
        if n_bins <= 2 * w + 1:
            out[f"rate_change_{w}s"] = float("nan")
            out[f"work_rate_change_{w}s"] = float("nan")
            continue
        rc = windowed(counts, w) / w
        rw = windowed(loads, w) / w
        base_c, base_w = float(rc.mean()), float(rw.mean())
        out[f"rate_change_{w}s"] = (
            float(np.mean(np.abs(rc[w:] - rc[:-w])) / base_c) if base_c > 0 else float("nan"))
        out[f"work_rate_change_{w}s"] = (
            float(np.mean(np.abs(rw[w:] - rw[:-w])) / base_w) if base_w > 0 else float("nan"))

    # --- ② 变化会不会回来（1 秒计数的滞后 k 自相关）---
    for lag in (1, 5, 30):
        if n_bins <= lag + 2:
            out[f"autocorr_{lag}s"] = float("nan")
            continue
        a, b = counts[:-lag], counts[lag:]
        if a.std() == 0 or b.std() == 0:
            out[f"autocorr_{lag}s"] = float("nan")
        else:
            out[f"autocorr_{lag}s"] = float(np.corrcoef(a, b)[0, 1])
    # 相对方差：1 秒计的方差 / 均值（泊松 = 1；>1 说明有超泊松的聚集）
    m = counts.mean()
    out["fano_1s"] = float(counts.var() / m) if m > 0 else float("nan")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="负载速率变化的持续性刻画")
    ap.add_argument("--source", choices=["synthetic", "trace"], default="synthetic")
    ap.add_argument("--trace", default=str(ROOT / "data/raw/azure_code_1week.csv"))
    ap.add_argument("--seeds", type=int, nargs="*", default=list(range(1, 11)))
    ap.add_argument("--requests", type=int, default=12000)
    ap.add_argument("--n-nodes", type=int, default=3)
    ap.add_argument("--target-rho", type=float, default=None)
    ap.add_argument("--workloads", nargs="*", default=["mixed", "data_heavy"])
    ap.add_argument("--table-dir", default=str(ROOT / "results/analysis_v2"))
    args = ap.parse_args()

    rm = _load_matrix_module()
    rho_target = rm.TARGET_RHO if args.target_rho is None else args.target_rho
    nodes = rm.make_nodes(args.n_nodes, rm.NETWORK_CONDITIONS["degraded"]["links"])
    capacity = sum(n.compute_rate * n.slots for n in nodes)

    segments: list = []
    if args.source == "trace":
        t0 = time.perf_counter()
        print(f"读取 trace：{args.trace}", flush=True)
        trace_workload, stats = rm.load_trace_auto(args.trace)
        print(f"  {len(trace_workload):,} 请求 / {stats.duration_s/3600:.1f} 小时"
              f"（{time.perf_counter()-t0:.0f}s）")
        segments = rm.split_trace_segments(trace_workload, max(args.seeds))

    rows = []
    print(f"来源={args.source} 目标 rho={rho_target} 请求/片={args.requests}")
    for name in args.workloads:
        spec = rm.WORKLOAD_CLASSES[name]
        for seed in args.seeds:
            if args.source == "trace":
                workload, qps, _ = rm.build_trace_cell_workload(
                    spec, segments[seed - 1], args.requests, capacity,
                    target_rho=rho_target)
            else:
                workload, qps, _ = rm.build_synthetic_cell_workload(
                    spec, args.requests, seed, capacity, target_rho=rho_target)
            arr = np.array([w[0] for w in workload], dtype=float)
            work = np.array([1.0 * c + 4.0 * g for _, c, g in workload], dtype=float)
            rec = {"data_source": args.source, "workload": name, "seed": seed,
                   "n_requests": len(workload), "qps": qps}
            rec.update(persistence_metrics(arr, work))
            rows.append(rec)
            print(f"  {name:<11} seed{seed:<3} "
                  f"rate_change_5s={rec.get('rate_change_5s', float('nan')):.3f}  "
                  f"work_rate_change_5s={rec.get('work_rate_change_5s', float('nan')):.3f}  "
                  f"autocorr_1s={rec.get('autocorr_1s', float('nan')):+.3f}  "
                  f"fano_1s={rec.get('fano_1s', float('nan')):.2f}")

    df = pd.DataFrame(rows)
    out = Path(args.table_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "load_persistence.csv"
    df.to_csv(path, index=False, encoding="utf-8-sig")
    print(f"\n[表] {path}  ({len(df)} 行，{len(df.columns)} 列)")
    print(f"  生成时间 {datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}")
    print("读法：`rate_change_5s` 大 = 速率 5 秒内变得多（陈旧快照偏离得远）；"
          "\n      `autocorr_1s` 接近 1 = 变化**不回来**（系统性错误，平均救不回来）；"
          "\n      平稳泊松的 `autocorr_1s` ≈ 0、`fano_1s` ≈ 1；"
          "\n      两个值都偏高说明负载的速率本身在被调制成慢变的过程（爬坡/洪峰）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

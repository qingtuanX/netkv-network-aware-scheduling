"""实验入口：跑 Random / LeastQueue / LeastLoad，出第一张 p95 图。

用法：
    sim/.venv/Scripts/python.exe run_min.py                  # 用合成负载打通链路
    sim/.venv/Scripts/python.exe run_min.py --source trace   # 用真实 Azure trace

产物：
    sim/results/raw/<run_id>_requests.csv     每请求级原始记录
    sim/results/raw/<run_id>_summary.csv      每次运行的汇总指标
    sim/figures/sojourn_distribution.png      第一张图
    sim/results/raw/<run_id>_manifest.json    运行元信息（代码版本、配置哈希、种子）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Windows 控制台默认可能是 GBK，中文输出会乱码；统一按 UTF-8 写出
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

from src.config import build_nodes, load_config            # noqa: E402
from src.metrics import bootstrap_ci, request_records, summarize  # noqa: E402
from src.model import compute_work                          # noqa: E402
from src.simulator import SimConfig, run                    # noqa: E402
from src.strategies import STRATEGIES                       # noqa: E402
from src.trace_loader import generate_synthetic, load_azure_trace  # noqa: E402


def code_version() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return "uncommitted"


def config_hash(obj) -> str:
    blob = json.dumps(obj, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["synthetic", "trace"], default="synthetic")
    ap.add_argument("--trace", default=str(ROOT / "data/raw/azure_code_first50MB.csv"))
    ap.add_argument("--config", default=str(ROOT / "configs/baseline.yaml"))
    ap.add_argument("--max-requests", type=int, default=None)
    ap.add_argument("--seeds", type=int, default=None,
                    help="覆盖配置里的种子个数（先用来测速）")
    args = ap.parse_args()

    cfg = load_config(args.config)
    nodes = build_nodes(cfg["nodes"])
    sim_cfg = SimConfig(**cfg["simulator"])
    seeds = cfg["seeds"][: args.seeds] if args.seeds else cfg["seeds"]
    max_req = args.max_requests or cfg["workload"].get("max_requests")

    # ---------- 1. 负载 ----------
    t0 = time.perf_counter()
    if args.source == "trace":
        wl = cfg["workload"]
        workload, stats = load_azure_trace(
            args.trace,
            max_requests=max_req,
            duration_s=wl.get("duration_s"),
            rate_scale=wl.get("rate_scale", 1.0),
        )
    else:
        g = cfg["workload"]["synthetic"]
        workload, stats = generate_synthetic(
            n_requests=max_req or g["n_requests"],
            base_qps=g["base_qps"],
            burstiness=g["burstiness"],
            seed=g["seed"],
        )
    load_time = time.perf_counter() - t0

    print("=" * 68)
    print(stats.report())
    print("=" * 68)

    # ---------- 负载合理性诊断 ----------
    # 这是最容易踩的坑：负载太轻时系统根本不排队，四种策略几乎无差别，
    # 实验结论会变成"网络感知没用"，而真相是"没跑到有压力的区域"。
    total_capacity = sum(n.compute_rate * n.slots for n in nodes)
    mean_work = float(np.mean([compute_work(c, g, sim_cfg.prefill_coef,
                                            sim_cfg.decode_coef)
                               for _, c, g in workload]))
    offered_rate = mean_work * stats.qps_mean
    rho = offered_rate / total_capacity if total_capacity > 0 else float("nan")
    print(f"系统容量      : {total_capacity:>12,.0f} token-units/s "
          f"({len(nodes)} 节点, {sum(n.slots for n in nodes)} 槽位)")
    print(f"单请求平均工作量: {mean_work:>12,.0f} token-units")
    print(f"负载强度 rho  : {rho:>12.3f}   (= 提供的工作量 / 系统容量)")
    if rho < 0.3:
        print("  !! rho < 0.3：系统基本空闲，几乎不会排队。"
              "此时四种策略差异很小，结论没有意义。")
        print("     请提高 workload.synthetic.base_qps 或 workload.rate_scale，"
              "或降低节点的 compute_rate。")
    elif rho > 0.95:
        print("  !! rho > 0.95：系统过饱和，队列会无限增长，"
              "p95 只是在测排空时间。这是合法但不同的问题，需明确说明。")
    else:
        print("  负载落在有压力但稳定的区间，适合观察排队与调度差异。")
    print("=" * 68)

    run_id = f"{args.source}_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    raw_dir = ROOT / "results" / "raw"
    fig_dir = ROOT / "figures"
    raw_dir.mkdir(parents=True, exist_ok=True)
    fig_dir.mkdir(parents=True, exist_ok=True)

    # ---------- 2. 运行 ----------
    summaries: list[dict] = []
    sojourn_parts: dict[str, list[np.ndarray]] = {}

    for sname, cls in STRATEGIES.items():
        for seed in seeds:
            t1 = time.perf_counter()
            strategy = cls(seed=seed)
            result = run(workload, nodes, strategy, sim_cfg, seed=seed)
            elapsed = time.perf_counter() - t1

            measured = result.measured(sim_cfg.warmup_fraction)
            slots_total = sum(n.slots for n in nodes)
            row = summarize(measured, result.makespan, len(nodes), slots_total)
            row.update({
                "run_id": run_id,
                "strategy": sname,
                "seed": seed,
                "source": args.source,
                "wall_clock_s": round(elapsed, 3),
                "sim_speedup": round(result.makespan / elapsed, 2) if elapsed > 0 else float("nan"),
            })
            summaries.append(row)

            rec = request_records(measured)
            rec.to_csv(raw_dir / f"{run_id}_{sname}_seed{seed}_requests.csv", index=False)

            arr = rec["sojourn"].to_numpy(dtype=float)
            sojourn_parts.setdefault(sname, []).append(arr)

            print(f"[{sname:<10}] seed={seed}  "
                  f"p50={row['sojourn_p50']:8.3f}s  p95={row['sojourn_p95']:9.3f}s  "
                  f"p99={row['sojourn_p99']:9.3f}s  thr={row['throughput_qps']:7.2f} qps  "
                  f"util={row['node_utilization']:.3f}  用时 {elapsed:.2f}s "
                  f"(仿真速度 {row['sim_speedup']}x)")

    df = pd.DataFrame(summaries)
    df.to_csv(raw_dir / f"{run_id}_summary.csv", index=False)

    per_strategy_sojourn = {
        name: np.concatenate(parts) for name, parts in sojourn_parts.items()
    }

    # ---------- 3. 图 ----------
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.5))
    colors = plt.cm.tab10.colors

    for i, (sname, arr) in enumerate(per_strategy_sojourn.items()):
        arr = arr[np.isfinite(arr) & (arr > 0)]
        if len(arr) == 0:
            continue
        color = colors[i % 10]

        # 左：对数分箱的完成时间直方图，y 为概率质量（每组求和为 1）
        lo, hi = max(arr.min(), 1e-6), arr.max()
        bins = np.logspace(np.log10(lo), np.log10(hi), 60)
        axes[0].hist(arr, bins=bins, histtype="step", linewidth=1.6, color=color,
                     weights=np.full(len(arr), 1.0 / len(arr)), label=sname)

        # 右：互补累积分布 P(X > x)，log-log
        xs = np.sort(arr)
        ccdf = 1.0 - np.arange(len(xs)) / len(xs)
        axes[1].plot(xs, ccdf, lw=1.6, color=color, label=sname)

    axes[0].set_xscale("log")
    axes[0].set_xlabel("end-to-end sojourn time (s), log scale")
    axes[0].set_ylabel("probability mass per log-bin")
    axes[0].set_title(f"Completion time distribution\n({args.source}, "
                      f"{len(seeds)} seeds, all strategies share one workload)")
    axes[0].legend(fontsize=8)

    axes[1].set_xscale("log")
    axes[1].set_yscale("log")
    axes[1].set_xlabel("end-to-end sojourn time (s), log scale")
    axes[1].set_ylabel("P(X > x)")
    axes[1].set_title("Tail: complementary CDF (log-log)")
    axes[1].grid(True, which="both", alpha=0.25, lw=0.5)
    axes[1].legend(fontsize=8)

    fig.tight_layout()
    fig_path = fig_dir / "sojourn_distribution.png"
    fig.savefig(fig_path, dpi=160)

    # ---------- 4. manifest ----------
    manifest = {
        "run_id": run_id,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "source": args.source,
        "trace_file": args.trace if args.source == "trace" else None,
        "code_version": code_version(),
        "code_dirty_files": len(subprocess.run(
            ["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True
        ).stdout.splitlines()) if code_version() != "uncommitted" else None,
        "config_path": str(args.config),
        "config_hash": config_hash(cfg),
        "config": cfg,
        "seeds": seeds,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "workload_stats": stats.__dict__,
        "load_time_s": round(load_time, 3),
        "wall_clock_total_s": round(sum(r["wall_clock_s"] for r in summaries), 2),
    }
    with open(raw_dir / f"{run_id}_manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    # ---------- 5. 汇总打印 ----------
    print()
    print("按策略聚合（对种子取中位数）:")
    agg = df.groupby("strategy")[["sojourn_p50", "sojourn_p95", "sojourn_p99",
                                  "throughput_qps", "node_utilization"]].median()
    print(agg.to_string(float_format=lambda v: f"{v:.4f}"))

    baseline = "LeastQueue"
    if baseline in agg.index:
        print()
        print(f"相对 {baseline} 的变化率（负数=更好）:")
        for name in agg.index:
            if name == baseline:
                continue
            p95 = agg.loc[name, "sojourn_p95"] / agg.loc[baseline, "sojourn_p95"] - 1
            p50 = agg.loc[name, "sojourn_p50"] / agg.loc[baseline, "sojourn_p50"] - 1
            lo, hi = bootstrap_ci(per_strategy_sojourn[name], np.median)
            print(f"  {name:<12} p50 {p50:+7.2%}   p95 {p95:+7.2%}   "
                  f"({name} 中位数 95% CI: [{lo:.3f}s, {hi:.3f}s])")

    print()
    print(f"原始数据: {raw_dir / (run_id + '_summary.csv')}")
    print(f"图:       {fig_path}")
    print()
    print(f"负载读取耗时 {load_time:.2f}s，仿真总耗时 "
          f"{manifest['wall_clock_total_s']:.2f}s。")
    print("如果仿真太慢，缩小 --max-requests 或 workload.duration_s 后再看是否仍成立。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

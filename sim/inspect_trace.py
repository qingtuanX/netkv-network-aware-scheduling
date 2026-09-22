"""trace 体检脚本 —— Day 2 的交付物。

回答四个问题，这四个答案直接决定实验因子怎么设：
  1. 这段数据覆盖多长时间
  2. 到达率是多少，有没有突发
  3. ContextTokens / GeneratedTokens 分布形状
  4. 异常值比例（token=0、时间戳乱序、截断行）

用法：
    sim/.venv/Scripts/python.exe inspect_trace.py sim/data/raw/azure_code_first50MB.csv
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Windows 控制台默认可能是 GBK，中文输出会乱码；统一按 UTF-8 写出
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

from src.trace_loader import load_azure_trace  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("trace", help="Azure trace CSV 路径（可以是截断下载的前 N MB）")
    ap.add_argument("--max-requests", type=int, default=None,
                    help="只体检前 N 个请求（大文件建议先设 200000）")
    ap.add_argument("--out", default=str(ROOT / "figures/trace_profile.png"))
    args = ap.parse_args()

    workload, stats = load_azure_trace(args.trace, max_requests=args.max_requests)

    print("=" * 68)
    print("trace 体检报告")
    print("=" * 68)
    print(stats.report())

    arrivals = np.array([w[0] for w in workload], dtype=float)
    ctx = np.array([w[1] for w in workload], dtype=float)
    gen = np.array([w[2] for w in workload], dtype=float)

    # 到达间隔
    gaps = np.diff(arrivals)
    gaps = gaps[gaps > 0]
    print()
    print(f"到达间隔: 均值 {gaps.mean():.6f}s  中位数 {np.median(gaps):.6f}s  "
          f"p99 {np.percentile(gaps, 99):.4f}s  max {gaps.max():.3f}s")
    print(f"   -> 平均 QPS 约 {1.0/gaps.mean():.2f}，"
          f"p99 间隔对应峰值约 {1.0/max(np.percentile(gaps, 99), 1e-9):.1f} QPS")

    # 工作量与突发
    print()
    print(f"ContextTokens 分位: "
          + "  ".join(f"p{q}={np.percentile(ctx, q):.0f}" for q in (50, 90, 99, 99.9)))
    print(f"GeneratedTokens 分位: "
          + "  ".join(f"p{q}={np.percentile(gen, q):.0f}" for q in (50, 90, 99, 99.9)))
    over_ctx = int((ctx > 32768).sum())
    print(f"ContextTokens > 32768 的请求: {over_ctx} ({over_ctx/len(ctx):.4%})"
          "  <- 若比例可观，需要在模型里处理超长上下文截断")

    # 按分钟的到达量，检查突发
    minute = (arrivals // 60).astype(int)
    per_min = np.bincount(minute)
    cv = per_min.std() / per_min.mean() if per_min.mean() > 0 else float("nan")
    print(f"每分钟到达量: 均值 {per_min.mean():.0f}  标准差 {per_min.std():.0f}  "
          f"变异系数 CV={cv:.3f}  max {per_min.max()}")
    print("   CV 越接近 0 越平稳；CV > 1 说明突发明显，"
          "必须保留到达过程结构，不能用固定 QPS 代替")

    # 绘图
    fig, axes = plt.subplots(1, 3, figsize=(15, 4))
    axes[0].plot(arrivals / 60.0, np.arange(len(arrivals)), lw=1)
    axes[0].set_xlabel("time (minutes)")
    axes[0].set_ylabel("cumulative requests")
    axes[0].set_title("Arrival process")

    t = arrivals / 60.0
    bins = np.arange(0, max(t[-1], 1.0) + 1, 1.0)
    axes[1].plot(bins[:-1], np.histogram(t, bins=bins)[0], lw=0.8)
    axes[1].set_xlabel("time (minutes)")
    axes[1].set_ylabel("requests per minute")
    axes[1].set_title(f"Burstiness (CV={cv:.2f})")

    axes[2].hist(ctx, bins=80, alpha=0.7, label="ContextTokens")
    axes[2].hist(gen, bins=80, alpha=0.7, label="GeneratedTokens")
    axes[2].set_yscale("log")
    axes[2].set_xlabel("tokens (log-y)")
    axes[2].set_title("Token size distribution")
    axes[2].legend(fontsize=8)

    fig.tight_layout()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=160)
    print()
    print(f"图已保存: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

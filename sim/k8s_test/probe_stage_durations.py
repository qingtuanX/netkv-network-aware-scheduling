"""只读探针：把 `measure_serving` 客户端时间轴的**五个阶段各自有多长**量出来。

为什么需要它：
    逐请求 CSV 只落盘了**判定结果**（`cov_loo_min_gap_s` / `cov_loo_stage`），
    没有落盘每个阶段自己的时长。而"两边跑同一套检查、结果一样"这句话不够——
    要看**同一个阶段的绝对时长在 Windows 与 Linux 上差多少**（回环开销、线程调度、
    时钟分辨率的直接读数），否则解释不了"为什么 LOO 揪出来的最短阶段在两边不是同一段"。

怎么做到"不改任何已有文件"：
    `check_coverage()` 是被客户端在算覆盖判据时调用的，它的第一个参数**就是**
    客户端五段的区间列表。所以这里把模块里的 `check_coverage` 换成一层记录器
    （monkeypatch 只作用于本进程），拿到真实区间后再原样转调原函数——
    被测代码一行都不用改，量到的也是最原始的采样值。

用法（在 sim/ 目录下执行，两侧同一份脚本）：
    .venv/Scripts/python.exe k8s_test/probe_stage_durations.py --out results/b0_linux/stage_durations_windows.json
    python3 k8s_test/probe_stage_durations.py --out results/b0_linux/stage_durations_linux.json
"""

from __future__ import annotations

import argparse
import json
import platform
import socket
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import measure_serving as ms  # noqa: E402

_captured: list[dict] = []
_lock = threading.Lock()
_orig_check_coverage = ms.check_coverage


def _spy(named, lo, hi, tol, loo_stages=None):
    names = tuple(n for n, _, _ in named)
    if names == ms.CLIENT_STAGES:  # 只收客户端那条时间轴（服务端那条是另一套）
        with _lock:
            _captured.append({"lo": lo, "hi": hi,
                              "iv": [(n, a, b) for n, a, b in named]})
    return _orig_check_coverage(named, lo, hi, tol, loo_stages)


def quant(sorted_xs: list[float], p: float) -> float:
    i = (len(sorted_xs) - 1) * p
    lo = int(i)
    hi = min(lo + 1, len(sorted_xs) - 1)
    return sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * (i - lo)


def main() -> int:
    ap = argparse.ArgumentParser(description="量客户端五个阶段的时长（只读，不改被测代码）")
    ap.add_argument("--requests", type=int, default=120)
    ap.add_argument("--qps", type=float, default=12.0)
    ap.add_argument("--concurrency", type=int, default=16)
    ap.add_argument("--port", type=int, default=9530)
    ap.add_argument("--token-count", type=int, default=32)
    ap.add_argument("--token-interval-ms", type=float, default=20.0)
    ap.add_argument("--queue-ms", type=float, default=10.0)
    ap.add_argument("--prefill-ms", type=float, default=100.0)
    ap.add_argument("--kv-mb", type=float, default=24.0)
    ap.add_argument("--kv-gbps", type=float, default=3.2)
    ap.add_argument("--label", default="stageprobe")
    ap.add_argument("--dump-intervals", action="store_true",
                    help="把每个请求的 5 段原始区间也落盘（排查亚毫秒值用）")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    ms.check_coverage = _spy
    cfg = ms.FakeBackendConfig(
        token_count=args.token_count, token_interval_s=args.token_interval_ms / 1000.0,
        queue_s=args.queue_ms / 1000.0, prefill_s=args.prefill_ms / 1000.0,
        kv_mb=args.kv_mb, kv_gbps=args.kv_gbps, kv_overlap_frac=0.0, seed=1)
    out_dir = Path(args.out).parent
    out_dir.mkdir(parents=True, exist_ok=True)
    srv, _st = ms.start_fake_backend(cfg, args.port)
    try:
        cargs = argparse.Namespace(
            url=f"http://127.0.0.1:{args.port}", out_dir=str(out_dir), label=args.label,
            requests=args.requests, qps=args.qps, arrival="uniform",
            concurrency=args.concurrency, prompt_tokens=64, max_tokens=args.token_count,
            request_timeout=30.0, cov_tol_s=ms.DEFAULT_COV_TOL_S, save_tokens=False, seed=1)
        rid = ms.run_client(cargs)
    finally:
        srv.shutdown()
        srv.server_close()

    # 逐段汇总：每段的时长 = b - a
    agg: dict[str, dict] = {}
    n_win = 0
    for rec in _captured:
        if rec["hi"] <= rec["lo"]:
            continue
        n_win += 1
        for name, a, b in rec["iv"]:
            d = b - a
            if d < 0:
                d = 0.0
            agg.setdefault(name, []).append(d)
    out = {"label": args.label, "run_id": rid, "n_windows": n_win,
           "host": ms.host_info(), "recipe": cfg.to_dict(), "stages": {}}
    for name in ms.CLIENT_STAGES:
        xs = sorted(agg.get(name, []))
        if not xs:
            continue
        out["stages"][name] = {"n": len(xs), "p50": quant(xs, 0.50), "p95": quant(xs, 0.95),
                               "min": xs[0], "max": xs[-1], "mean": sum(xs) / len(xs)}
    out["time_time_resolution_s"] = ms._clock_resolution()
    out["platform"] = platform.platform()
    out["hostname"] = socket.gethostname()
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str),
                              encoding="utf-8")
    if args.dump_intervals:
        dump = Path(args.out).with_suffix(".intervals.json")
        dump.write_text(json.dumps(_captured, ensure_ascii=False, indent=1, default=str),
                        encoding="utf-8")
        print(f"原始区间 -> {dump}")
    print(json.dumps({k: (v if k != "stages" else
                          {n: round(vv["p50"] * 1e6, 2) for n, vv in v.items()})
                      for k, v in out.items() if k in ("host", "stages", "run_id",
                                                       "time_time_resolution_s", "platform")},
                     ensure_ascii=False, indent=2, default=str))
    print(f"\n阶段 p50（微秒）已在上面; 产物 -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

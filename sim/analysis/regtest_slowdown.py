# -*- coding: utf-8 -*-
"""默认关闭回归检查（CONS_MODEL_PREREG §3：服务时间倍率注入不得改变默认行为）。

用法：
  python analysis/regtest_slowdown.py capture   # 改代码前：记录基准哈希
  python analysis/regtest_slowdown.py check     # 改代码后：与基准逐位比对
  python analysis/regtest_slowdown.py selftest  # 自检：倍率函数的 4 个冻结取值 + 门 2 恒等

固定 3 节点异构 + 固定合成负载（seed=42）+ LeastLoadStrategy(seed=7)；
哈希 = 每请求 (node_id, finish) 数组的 sha256 + 汇总指标元组。
默认 SimConfig（不设 service_slowdown ⇒ 无倍率 ⇒ 必须与改前逐位一致）。
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                                            # noqa: BLE001
    pass

from src.model import NodeSpec                              # noqa: E402
from src.simulator import SimConfig, run                     # noqa: E402
from src.strategies import LeastLoadStrategy, NetworkAwareStrategy  # noqa: E402

OUT = ROOT / "results" / "verification" / "regtest_slowdown.json"
NODES = [
    NodeSpec(node_id=0, compute_rate=16000.0, slots=4, latency=0.0005, bandwidth=1.25e9),
    NodeSpec(node_id=1, compute_rate=8000.0, slots=2, latency=0.002, bandwidth=1.25e8),
    NodeSpec(node_id=2, compute_rate=4000.0, slots=1, latency=0.005, bandwidth=1.25e7),
]


def build_workload():
    rng = np.random.default_rng(42)
    n = 500
    arrivals = np.cumsum(rng.exponential(0.02, n))
    ctx = rng.integers(200, 4000, n)
    gen = rng.integers(16, 256, n)
    return [(float(a), int(c), int(g)) for a, c, g in zip(arrivals, ctx, gen)]


def fingerprint() -> dict:
    wl = build_workload()
    res = run(wl, [NodeSpec(**vars(n)) for n in NODES], LeastLoadStrategy(seed=7),
              SimConfig(), seed=123)
    arr = np.array([(r.node_id, r.finish) for r in res.requests], dtype=np.float64)
    soj = np.array([r.finish - r.arrival for r in res.requests], dtype=np.float64)
    return {
        "hash": hashlib.sha256(arr.tobytes()).hexdigest(),
        "n_requests": len(res.requests),
        "sojourn_p50": round(float(np.percentile(soj, 50)), 12),
        "sojourn_p95": round(float(np.percentile(soj, 95)), 12),
        "sojourn_sum": round(float(soj.sum()), 12),
    }


def real_path_check() -> bool:
    """走**真实 run_matrix.run_cell 路径**，确认 cell 里的 service_slowdown 真的生效。

    【为什么必须单独有这一条】2026-10-12 的保守模型电池把 --service-slowdown
    写进了 cell 字典、也写进了 runs.csv 的列，但 run_cell 构造 SimConfig 时漏传，
    于是约 8,600 次仿真全部按理想模型跑，产物却声称用了 linear/pressure。
    上面 fingerprint() 直接构造 SimConfig，抓不到这种"接线断了"的 bug；
    这里故意不构造 SimConfig，只给 cell 塞 service_slowdown，看落盘指标变不变。

    用小规模（400 请求）+ compute_heavy + kv_cache，只做接线检查，
    不产生论文数字。返回 True 表示三种模式的 p95 符合预期。
    """
    sys.path.insert(0, str(ROOT / "experiments"))
    import run_matrix as rmx                       # noqa: E402

    rmx.NET_SET = "v2"                             # 与电池一致
    base_cell = {
        "workload": "compute_heavy", "network": "degraded", "staleness": "fresh",
        "staleness_queue_s": 0.0, "staleness_network_s": 0.0,
        "seed": 1, "data_volume": "kv_cache", "n_nodes": 3,
        "data_source": "synthetic", "trace_segment": None, "target_rho": 0.6,
        "net_set": "v2",
    }

    def p95(mode: str) -> float:
        c = dict(base_cell)
        c["service_slowdown"] = mode
        rows, _ = rmx.run_cell(c, ["LeastLoad"], 400, 1.0)
        return float(rows[0]["sojourn_p95"])

    p_none, p_lin, p_pre = p95("none"), p95("linear"), p95("pressure")
    ok = (p_lin > p_none) and (p_pre >= p_lin)
    print(f"  real_path: p95 none={p_none:.3f}s linear={p_lin:.3f}s "
          f"pressure={p_pre:.3f}s -> {'OK' if ok else 'FAIL（倍率没进 SimConfig）'}")
    return ok


def selftest() -> int:
    from src.model import service_slowdown_factor as f
    ok = True
    for c, want in ((1, 1.000), (2, 1.317), (3, 1.633), (4, 1.950), (8, 1.950)):
        got = round(f(c, "linear"), 3)
        print(f"  linear f({c}) = {got}（冻结值 {want}）{'OK' if got == want else 'FAIL'}")
        ok &= got == want
    for c, want in ((1, 1.000), (2, 1.950), (3, 1.950)):
        got = round(f(c, "pressure"), 3)
        print(f"  pressure f({c}) = {got}（冻结值 {want}）{'OK' if got == want else 'FAIL'}")
        ok &= got == want
    # 门 2：w_net=0 与 LeastLoad 在倍率开启下也必须逐位相同（调度相同 ⇒ 并发序列相同）
    wl = build_workload()
    a = run(wl, [NodeSpec(**vars(n)) for n in NODES], LeastLoadStrategy(seed=7),
            SimConfig(service_slowdown="linear"), seed=123)
    b = run(wl, [NodeSpec(**vars(n)) for n in NODES], NetworkAwareStrategy(seed=7, w_net=0.0),
            SimConfig(service_slowdown="linear"), seed=123)
    same = all(ra.node_id == rb.node_id and ra.finish == rb.finish
               for ra, rb in zip(a.requests, b.requests))
    print(f"  门 2（w_net=0 ≡ LeastLoad，linear 开启）：{'OK' if same else 'FAIL'}")
    ok &= same
    # 真实入口的接线检查：2026-10-12 的整批白跑就是因为漏了这一条
    ok &= real_path_check()
    return 0 if ok else 1


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    OUT.parent.mkdir(parents=True, exist_ok=True)
    if mode == "selftest":
        print("== 倍率函数冻结取值 + 门 2 自检 ==")
        return selftest()
    fp = fingerprint()
    if mode == "capture":
        OUT.write_text(json.dumps(fp, indent=1), encoding="utf-8")
        print(f"[capture] 基准已记录 → {OUT}\n{json.dumps(fp, indent=1)}")
        return 0
    base = json.loads(OUT.read_text(encoding="utf-8"))
    same = all(base[k] == fp[k] for k in fp)
    print("[check] 默认关闭回归：", "PASS（逐位一致）" if same else "FAIL（行为已改变！）")
    print("  基准:", json.dumps(base))
    print("  当前:", json.dumps(fp))
    return 0 if same else 1


if __name__ == "__main__":
    raise SystemExit(main())

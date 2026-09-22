"""实验门与实现验证的可复现入口 —— 把"四道门"从口头承诺变成产物。

为什么需要它：门 1–3 反反复复抓住过真实 bug（cell 级 `w_net` 被策略工厂丢掉、
观测年龄写成 min、阈值与年龄重合导致触发由浮点决定……）。但早先这些检查是
"需要时手写一段对比"，只存在于会话记录里，**不是产物**。这个脚本把它们固定下来：

    门 1  负载强度：从 runs.csv 复算 rho，检查是否都在 [RHO_LO, RHO_HI]
    门 2  消融严格子集：NetworkAware(w_net=0) 与 LeastLoad 逐位相同（两个层次都查）
            · 产物层：ablation 模式的 13 个指标逐位比较（12 个 cell）
            · 请求层：内存里跑同一个 cell 的两种策略，比 (node_id, finish) 数组
    门 3  同种子可复现：同一配置同一负载跑两次，(node_id, finish) 必须完全一致
    白盒  时间分解：只放 1 个请求时 finish == arrival + net_in + service + net_out
    黑盒  容量与单调性：交付工作量/容量随 rho 变化是否合理、p95 是否随 rho 单调不减

用法：
    sim/.venv/Scripts/python.exe analysis/verify_gates.py --batch synthetic
    sim/.venv/Scripts/python.exe analysis/verify_gates.py --batch azure
    sim/.venv/Scripts/python.exe analysis/verify_gates.py --batch burstgpt
    # 自定义批次
    sim/.venv/Scripts/python.exe analysis/verify_gates.py \
        --matrix-dir results/matrix_trace --source trace \
        --trace data/raw/azure_code_1week.csv --label azure

产物：
    results/verification/<label>_gates.csv    每项检查一行（passed / 实测值 / 说明）
    results/verification/<label>_gates.json   同上，含参数与 UTC 时间戳，供引用
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "experiments"))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

# 与 analyze.py 保持一致的口径（容量、rho 公差、指标列）
SLOTS_PER_NODE = 4
NODE_RATE_BASE = 16000.0
RHO_LO, RHO_HI = 0.3, 0.9
RHO_TOL = 5e-4
METRIC_COLS = ["n_requests", "sojourn_mean", "sojourn_p50", "sojourn_p95",
               "sojourn_p99", "sojourn_max", "wait_mean", "wait_p95", "net_mean",
               "net_share", "slo_miss_rate", "throughput_qps", "node_utilization"]

BATCHES = {
    "synthetic": dict(matrix_dir="results/matrix", source="synthetic", trace=None),
    "azure": dict(matrix_dir="results/matrix_trace", source="trace",
                  trace="data/raw/azure_code_1week.csv"),
    "burstgpt": dict(matrix_dir="results/matrix_burstgpt", source="trace",
                     trace="data/raw/burstgpt_1.csv"),
}

# 门 4：模型关键假设 vs 已核对的公开实测值。**这是参数来源档案，不是实验结果**，
# 所以是手写常量；但它必须和分析报告一起落盘，否则"参数从哪来"只存在于人的记忆里。
#
# 全部数字在 2026-09-22 用 pypdf 从 文献/ 下的 PDF 正文抽取核对（抽出文本缓存在
# data/processed/pdf_text/，可用 grep 复核）。`deviation` 一列是**偏差方向与量级**，
# 写正文时它比 `code_value` 更重要：能外推的和不能外推的都写在这里。
FIDELITY_ANCHORS = [
    dict(model_quantity="bytes_per_context_token（kv_cache 档）", code_value="3.3e5 B/token",
         anchor="vLLM(SOSP'23)§2.1 的公式：2(K,V)×kv_dim×n_layers×2 B(fp16)。"
                "同公式下 OPT-13B(MHA)=800 KB/token、OPT-66B≈2.2 MB/token"
                "（DistServe 实测：OPT-66B 的 512-token 请求 KV ≈ 1.13 GB）",
         anchor_kind="文献·已核对",
         deviation="本模型取 70B-GQA（Llama-3 级）配置：2×8头×128维×80层×2B = 327,680 B/token，"
                   "与本值差 +0.7%。若换成 MHA 的大模型会低 3–7 倍——**必须在正文写明模型配置**",
         note="KV 传输量是本课题的一等实验因子，不能当既定事实"),
    dict(model_quantity="compute_rate", code_value="16000 units/s（节点 0，按 2 的幂递减）",
         anchor="DistServe 的在线 SLO：OPT-13B TTFT 0.25 s / TPOT 0.1 s；"
                "OPT-66B 2.5 s / 0.15 s；OPT-175B 4.0 s / 0.2 s",
         anchor_kind="文献·已核对",
         deviation="本模型的单输出 token 服务时间 = 4/16000 = 0.25 ms，比真实 TPOT 低约 **400×**。"
                   "算力是抽象单元，**绝对时间不可与真实 GPU 相比**，只有节点间相对异构性有语义",
         note="禁止把 units/s 写成 tokens/s；SLO 只做相对比较"),
    dict(model_quantity="prefill_coef / decode_coef", code_value="1.0 / 4.0",
         anchor="Splitwise 指出 token 相位比 prompt 相位更受显存带宽限制；"
                "DistServe 的 OPT-13B 数据（TTFT 0.25 s vs TPOT 0.1 s）含批处理与排队，"
                "只能给出每 token 代价比的数量级",
         anchor_kind="文献·已核对（数量级）",
         deviation="真实 decode 与 prefill 的每 token 墙钟代价比常在 1:10–1:50，"
                   "本模型取 1:4 → **对 decode 的加权偏低约 3–12×**，属于对计算密集负载的偏保守处理",
         note="进 Threats；若论文结论对 decode 占比敏感，必须做系数敏感性"),
    dict(model_quantity="链路带宽", code_value="normal 10 Gbps / mid 1 Gbps / degraded 100 Mbps",
         anchor="DistServe：OPT-66B 在 10 rps 下需要 **90 Gbps** 才能让 KV 传输开销\"不可见\"，"
                "并指出现代 GPU 集群用 InfiniBand（**800 Gbps**）；MegaScale 的生产网络是"
                "**400 Gbps 端口**的三层 CLOS",
         anchor_kind="文献·已核对",
         deviation="本模型比机内网络低 **80×–8000×**。**已用 `--mode bandwidth` 直接检验过后果**"
                   "（保持异构形状，只换绝对尺度）：机内档 net_share ≈0.001–0.08 时 Δp95 变成 "
                   "**+3.3%（合成）/ +4.8%（Azure）/ +0.0%（BurstGPT）**，即收益消失、"
                   "甚至小幅劣化；净占比升到 0.15–0.30 才回到 −13%~−34%。"
                   "机内 KV 传输占比据 DistServe 是 1–5% 量级 → "
                   "**本课题的结论适用区间是带宽受限/跨域（算力网络），不是机内推理服务**；"
                   "正文必须写这句，否则 100 s 量级的 p95 会被当作模型错误",
         note="这是本课题最大的外部效度威胁，也是唯一一个已经用独立实验量化过的威胁"
              "（§13 + `rq13_bandwidth_summary.csv`）"),
    dict(model_quantity="单程时延", code_value="0.5 ms / 2 ms / 5 ms",
         anchor="DCQCN(SIGCOMM'15) 实测：RDMA 单程 1.7 µs(Read/Write) / 2.8 µs(Send)，"
                "TCP 25.4 µs",
         anchor_kind="文献·已核对",
         deviation="本模型高 **20×–3000×**；同样是跨域而非机内通信的建模选择",
         note="与带宽那一行同源，声明一次即可"),
    dict(model_quantity="staleness 量级",
         code_value="队列 0/2 s、链路 0/30 s，回退档另加 3 s",
         anchor="Sparrow(SOSP'13)：集中式状态需要在 sub-second 内复制/恢复才具备可用性，"
                "其调度采样是毫秒级",
         anchor_kind="文献·已核对（数量级）",
         deviation="本模型的\"陈旧\"档（链路 30 s）远大于调度器状态的刷新尺度 → "
                   "刻画的是**慢监控**场景；快刷新场景下的结论不能外推",
         note="已由 --mode staleness 扫成曲线，临界点实测 1.9–44.2 s 跨批次差一个数量级"),
    dict(model_quantity="SLO 定义", code_value="relative，slo_multiplier=3.0",
         anchor="DistServe 的绝对 SLO 量级：TTFT 0.25–4.0 s、TPOT 0.1–0.2 s",
         anchor_kind="文献·已核对（量级）+ 实验假设",
         deviation="本模型的绝对 p95 是 27–148 s，**比真实在线推理 SLO 高 2–3 个数量级** → "
                   "不能声称\"改善在线推理体验\"，只能做策略间的相对比较；且高网络占比格子上 "
                   "miss rate 会饱和（四策略全 >0.98），那里只有 p95 有信息量",
         note="这点必须进 Threats，否则结论会被读成生产可用性主张"),
    dict(model_quantity="仿真器保真度的可比基准（新增）", code_value="无端到端实测对照",
         anchor="Vidur(arXiv'24) 用真实 vLLM 对照：85% 容量下 <5% 误差、请求级 <9%、"
                "95% 容量下最高 12.65%",
         anchor_kind="文献·已核对（方法论基准）",
         deviation="我们只有**组件级**验证（时间分解误差 0、逐位可复现、容量口径一致），"
                   "**没有** Vidur 那种端到端吞吐对照 → 这是当前与同类仿真论文最大的方法论差距",
         note="补法：与公开实测吞吐做量级对照，或做一次小规模实测趋势验证"),
]


def node_capacity(n_nodes: float) -> float:
    n = int(n_nodes)
    return sum(NODE_RATE_BASE / (2 ** (i % 4)) for i in range(n)) * SLOTS_PER_NODE


def check(rows: list, name: str, passed: bool, detail: str, **extra) -> None:
    rows.append({"check": name, "passed": bool(passed), "detail": detail, **extra})
    print(f"  [{'✓' if passed else '✗'}] {name:<34} {detail}")


def latest_runs(matrix_dir: Path, mode: str):
    files = sorted(matrix_dir.glob(f"{mode}_*_runs.csv"))
    if not files:
        return None, None
    f = files[-1]
    rid = f.name.replace(f"{mode}_", "").replace("_runs.csv", "")
    return pd.read_csv(f), rid


def main() -> int:
    ap = argparse.ArgumentParser(description="实验门与实现验证")
    ap.add_argument("--batch", choices=sorted(BATCHES), default=None)
    ap.add_argument("--net-set", choices=["v1", "v2"], default="v1",
                    help="网络档位集合，必须与要验证的那批产物一致（v2 = 链路 FIFO 管道）")
    ap.add_argument("--matrix-dir", default=None)
    ap.add_argument("--source", choices=["synthetic", "trace"], default=None)
    ap.add_argument("--trace", default=None)
    ap.add_argument("--label", default=None)
    ap.add_argument("--seeds", type=int, nargs="*", default=[1, 2, 3])
    ap.add_argument("--requests", type=int, default=20000)
    ap.add_argument("--out", default=str(ROOT / "results/verification"))
    args = ap.parse_args()

    if args.batch:
        cfg = BATCHES[args.batch]
        label = args.batch
    else:
        cfg = dict(matrix_dir=args.matrix_dir, source=args.source, trace=args.trace)
        label = args.label or "custom"
    matrix_dir = (ROOT / cfg["matrix_dir"]).resolve()
    rows: list[dict] = []

    print("=" * 84)
    print(f"实验门与实现验证  批次={label}  网络档位={args.net_set}  矩阵目录={matrix_dir}")
    print("=" * 84)

    import run_matrix as rmx                      # noqa: E402
    rmx.NET_SET = args.net_set                    # 必须与产物用同一档位集合
    from src.workload import WORKLOAD_CLASSES      # noqa: E402
    from src.simulator import SimConfig            # noqa: E402
    from src.model import service_time, network_time  # noqa: E402

    # ---------------- 门 1：rho 落在有效区间 ----------------
    print("\n门 1  负载强度 rho（从 runs.csv 复算，不依赖 analyze 的中间产物）")
    modes = ["main", "staleness", "datavolume", "ablation", "scale",
             "convergence", "rho", "fallback", "bandwidth"]
    total_n = total_bad = 0
    per_mode = {}
    # v2：链路争用模型下还要查两条资源与可行域，判据是
    #   max(rho_compute, rho_network) ≤ 0.9   且   load_ratio = λ / R ≤ 0.9
    # R = Σ_i min(算力_i/E[work], 带宽_i/E[bytes])，即"完全均衡放置"的可承载到达率。
    def node_rates(n: int) -> list[float]:
        return [NODE_RATE_BASE / (2 ** (i % 4)) for i in range(int(n))]

    for mode in modes:
        df, rid = latest_runs(matrix_dir, mode)
        if df is None or df.empty:
            continue
        cap = df["n_nodes"].map(node_capacity)
        rho = df["mean_work"] * df["qps_realized"] / cap
        worst = rho.copy()
        detail = {}
        if "rho_net" in df.columns and df["rho_net"].notna().any():
            detail["rho_net_max"] = float(df["rho_net"].max())
            worst = np.maximum(worst, df["rho_net"].fillna(0.0))
            # 可行域：需要每节点带宽（取自 manifest 的网络条件）与每节点算力
            man = json.loads((matrix_dir / f"{mode}_{rid}_manifest.json")
                             .read_text(encoding="utf-8")) if (
                matrix_dir / f"{mode}_{rid}_manifest.json").exists() else {}
            conds = man.get("network_conditions") or {}
            loads = []
            for _, r in df.iterrows():
                c = conds.get(r["network"]) or {}
                links = [bw for _, bw in c.get("links", [])]
                if not links or not np.isfinite(r.get("mean_bytes", np.nan)) or r["mean_bytes"] <= 0:
                    continue
                rates = node_rates(r["n_nodes"])
                R = sum(min(rates[i] * SLOTS_PER_NODE / r["mean_work"],
                            links[i % len(links)] / r["mean_bytes"])
                        for i in range(int(r["n_nodes"])))
                loads.append(r["qps_realized"] / R if R > 0 else np.nan)
            if loads:
                detail["load_ratio_max"] = float(np.nanmax(loads))
        bad = int((worst > RHO_HI + RHO_TOL).sum()) + int((rho < RHO_LO - RHO_TOL).sum())
        if detail.get("load_ratio_max", 0) > 1.0 + 1e-6:
            bad += 1
        total_n += len(rho)
        total_bad += bad
        per_mode[mode] = {"runs": int(len(rho)), "rho_min": float(rho.min()),
                          "rho_max": float(rho.max()), "out_of_range": bad,
                          "run_id": rid, **detail}
        extra = ""
        if "rho_net_max" in detail:
            extra = (f"  rho_net_max={detail['rho_net_max']:.3f}"
                     f"  load_ratio_max={detail.get('load_ratio_max', float('nan')):.3f}")
        print(f"    {mode:<12} run={rid}  行={len(rho):<4} "
              f"rho_compute∈[{rho.min():.4f}, {rho.max():.4f}]  越界 {bad}{extra}")
    check(rows, "门1 rho 全部落在 [0.3, 0.9]（v2 含两资源与可行域）",
          total_bad == 0 and total_n > 0,
          f"共 {total_n} 行，越界/不可持续 {total_bad} 项（公差 {RHO_TOL:g}）",
          n_rows=total_n, n_out_of_range=total_bad, per_mode=per_mode)

    # ---------------- 门 2：产物层（消融矩阵的 13 个指标）----------------
    print("\n门 2  消融严格子集（产物层：ablation 模式逐指标逐位比较）")
    abl, rid = latest_runs(matrix_dir, "ablation")
    if abl is None or abl.empty:
        check(rows, "门2 产物层（ablation 逐指标）", False,
              "该批次没有 ablation 产物，无法验证（不是通过）", n_cells=0)
    else:
        # w_net 是 cell 级参数：同 cell 的 Random/LeastQueue 行也带这个值，
        # 因此必须同时限定 strategy，否则会误报不通过。
        # v2 下有两个网络感知策略，两个都必须满足"严格子集"性质。
        na_names = ["NetworkAware"] + (["NetworkAware2"]
                                       if "NetworkAware2" in set(abl["strategy"]) else [])
        ll = abl[abl["strategy"] == "LeastLoad"]
        cols = [c for c in METRIC_COLS if c in abl.columns]
        for na_name in na_names:
            sub0 = abl[np.isclose(abl["w_net"], 0.0) & (abl["strategy"] == na_name)]
            if sub0.empty:
                check(rows, f"门2 产物层（{na_name} w=0）", False,
                      "没有 w_net=0 的行", n_cells=0)
                continue
            merged = sub0.merge(ll, on=["cell_id", "seed"], suffixes=("_na", "_ll"))
            eq_all, details, n_eq = True, [], 0
            for c in cols:
                a = merged[f"{c}_na"].to_numpy(dtype=float)
                b = merged[f"{c}_ll"].to_numpy(dtype=float)
                eq = bool(np.array_equal(a, b))
                n_eq += int(eq)
                eq_all &= eq
                if not eq:
                    fin = np.isfinite(a) & np.isfinite(b)
                    details.append(f"{c} max|Δ|={np.max(np.abs(a[fin]-b[fin])):.3g}")
            check(rows, f"门2 产物层（{na_name} w=0 == LeastLoad）", eq_all,
                  f"{merged['cell_id'].nunique()} 个 cell × {len(cols)} 个指标："
                  f"{n_eq}/{len(cols)} 逐位相同"
                  + ("" if eq_all else "  差异：" + "; ".join(details)),
                  run_id=rid, n_cells=int(merged["cell_id"].nunique()),
                  n_metrics=len(cols))

    # ---------------- 门 2 / 3 / 白盒：请求层（内存内跑）----------------
    print("\n门 2/3 + 白盒（请求层：内存内跑，比 (node_id, finish) 数组）")
    # 网络条件取**当前档位集合**（v2 的链路是 FIFO 管道，v1 的不排队）
    _cond = rmx.cell_network_condition({"network": "degraded"})
    nodes = rmx.make_nodes(3, _cond["links"])
    capacity = sum(n.compute_rate * n.slots for n in nodes)
    cfg_sim = SimConfig(prefill_coef=1.0, decode_coef=4.0, warmup_fraction=0.1,
                        max_requests=args.requests,
                        bytes_per_context_token=rmx.DATA_VOLUME["kv_cache"],
                        bytes_per_generated_token=4.0, slo_mode="relative",
                        slo_multiplier=3.0)

    def build(name: str, seed: int):
        if cfg["source"] == "trace":
            if not hasattr(build, "_cache"):
                wl, stats = rmx.load_trace_auto(ROOT / cfg["trace"])
                build._cache = (wl, rmx.split_trace_segments(wl, max(args.seeds)), stats)
                rmx._TRACE_SEGMENTS = build._cache[1]
                rmx._TRACE_STATS = build._cache[2]
            segs = build._cache[1]
            return rmx.build_trace_cell_workload(
                WORKLOAD_CLASSES[name], segs[seed - 1], args.requests, capacity,
                target_rho=rmx.TARGET_RHO,
                node_compute=[n.compute_rate * n.slots for n in nodes],
                node_bandwidth=[n.bandwidth for n in nodes],
                bytes_per_ctx_token=rmx.DATA_VOLUME["kv_cache"],
                bytes_per_gen_token=4.0)[0]
        return rmx.build_synthetic_cell_workload(
            WORKLOAD_CLASSES[name], args.requests, seed, capacity,
            target_rho=rmx.TARGET_RHO,
            node_compute=[n.compute_rate * n.slots for n in nodes],
            node_bandwidth=[n.bandwidth for n in nodes],
            bytes_per_ctx_token=rmx.DATA_VOLUME["kv_cache"],
            bytes_per_gen_token=4.0)[0]

    def signature(workload, spec, seed, traj):
        r = rmx.run(workload, nodes, rmx.make_strategy(spec, seed, 1.0),
                    cfg_sim, seed, traj)
        order = sorted(r.requests, key=lambda x: x.req_id)
        return (np.array([x.node_id for x in order], dtype=float),
                np.array([x.finish for x in order], dtype=float), r)

    for wl_name in ("mixed", "data_heavy"):
        wl = build(wl_name, 1)
        traj = rmx.make_trajectory(_cond, 3, seed=1)
        ll = signature(wl, "LeastLoad", 1, traj)
        for na_name in ("NetworkAware", "NetworkAware2"):
            na0 = signature(wl, {"name": na_name, "w_net": 0.0}, 1, traj)
            na1 = signature(wl, {"name": na_name, "w_net": 1.0}, 1, traj)
            same = bool(np.array_equal(na0[1], ll[1]) and np.array_equal(na0[0], ll[0]))
            diff_w1 = float(np.mean(na1[1] != ll[1]))
            check(rows, f"门2 请求层 {wl_name}({na_name} w=0)", same,
                  f"(node_id, finish) 逐位相同={same}；"
                  f"对照：w_net=1 时 {diff_w1:.1%} 请求不同",
                  workload=wl_name, frac_diff_w1=diff_w1)

    wl = build("mixed", 1)
    traj = rmx.make_trajectory(rmx.NETWORK_CONDITIONS["unstable"], 3, seed=2)
    s1 = signature(wl, {"name": "NetworkAware", "w_net": 1.0}, 2, traj)
    s2 = signature(wl, {"name": "NetworkAware", "w_net": 1.0}, 2, traj)
    same = bool(np.array_equal(s1[1], s2[1]) and np.array_equal(s1[0], s2[0]))
    check(rows, "门3 同种子可复现", same,
          f"同一配置同一负载跑两次，(node_id, finish) 逐位相同={same}", n_requests=len(s1[1]))

    # 白盒：单请求、无排队 → finish 必须精确等于 arrival + net_in + service + net_out
    # 【必须在静态网络上做】时变网络里回传时间用的是**回传时刻**的链路状态
    # （调度时无法预知未来的带宽），因此用到达时刻的状态去算期望值会差一点点，
    # 那不是实现错误而是模型语义。
    cfg_one = SimConfig(prefill_coef=1.0, decode_coef=4.0, warmup_fraction=0.0,
                        max_requests=1, bytes_per_context_token=rmx.DATA_VOLUME["kv_cache"],
                        bytes_per_generated_token=4.0, slo_mode="relative",
                        slo_multiplier=3.0)
    _traj_static = rmx.make_trajectory(_cond, 3, seed=1)   # 用当前档位集合的条件
    wl_one = build("mixed", 1)
    r = rmx.run(wl_one, nodes, rmx.make_strategy("LeastLoad", 1, 1.0), cfg_one, 1,
                _traj_static)
    q = r.requests[0]
    node = nodes[q.node_id]
    tin, tout = network_time(q.in_bytes, q.out_bytes, node,
                             _traj_static.state(q.node_id, q.arrival))
    expect = q.arrival + tin + service_time(q.work, node) + tout
    check(rows, "白盒 时间分解精确性", abs(q.finish - expect) < 1e-9,
          f"静态网络下单请求：|finish − (arrival+net_in+service+net_out)| = "
          f"{abs(q.finish - expect):.3e}",
          finish=q.finish, expected=expect)

    # 黑盒：p95 随 rho 单调不减
    # 【必须传每节点容量】否则退回 v1 的聚合标定：在 v2 的链路争用模型下会把链路压爆，
    # p95 冲到几万秒（实测 73983 s）且失去单调性——那不是模型的错，是检查没给全参数。
    p95s = []
    for rho_t in (0.30, 0.45, 0.60, 0.75, 0.90):
        w = rmx.build_synthetic_cell_workload(
            WORKLOAD_CLASSES["mixed"], args.requests, 1, capacity, target_rho=rho_t,
            node_compute=[n.compute_rate * n.slots for n in nodes],
            node_bandwidth=[n.bandwidth for n in nodes],
            bytes_per_ctx_token=rmx.DATA_VOLUME["kv_cache"],
            bytes_per_gen_token=4.0)[0]
        rr = rmx.run(w, nodes, rmx.make_strategy("LeastLoad", 1, 1.0), cfg_sim, 1, _traj_static)
        m = rr.measured(cfg_sim.warmup_fraction)
        p95s.append(float(np.percentile([x.sojourn for x in m], 95)))
    mono = all(b >= a for a, b in zip(p95s, p95s[1:]))
    check(rows, "黑盒 p95 随 rho 单调不减", mono,
          "p95 = " + " → ".join(f"{v:.2f}" for v in p95s), p95_by_rho=p95s)

    # ---------------- 产物完整性（runs + paired + manifest，含 p_holm）----------------
    print("\n产物完整性（每个 mode 的 runs + paired + manifest，paired 须含 p_holm）")
    incomplete = []
    for mode in modes:
        runs_files = sorted(matrix_dir.glob(f"{mode}_*_runs.csv"))
        if not runs_files:
            continue
        rid = runs_files[-1].name.replace(f"{mode}_", "").replace("_runs.csv", "")
        man = matrix_dir / f"{mode}_{rid}_manifest.json"
        pai = matrix_dir / f"{mode}_{rid}_paired.csv"
        if not (man.exists() and pai.exists()):
            incomplete.append(f"{mode}(缺 manifest/paired)")
            continue
        pt = pd.read_csv(pai)
        if not ("p_holm" in pt.columns and pt["p_holm"].notna().all()):
            incomplete.append(f"{mode}(缺 p_holm)")
    check(rows, "产物完整性 runs+paired+manifest", not incomplete,
          "全部完整" if not incomplete else "不完整：" + "; ".join(incomplete),
          incomplete=incomplete)

    # ---------------- 落盘 ----------------
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    df = pd.DataFrame(rows)
    keep = ["check", "passed", "detail", "n_rows", "n_out_of_range", "n_cells",
            "n_metrics", "workload", "frac_diff_w1", "n_requests", "finish",
            "expected", "p95_by_rho", "incomplete", "run_id", "per_mode"]
    df = df.reindex(columns=[c for c in keep if c in df.columns])
    csv_path = out_dir / f"{label}_gates.csv"
    json_path = out_dir / f"{label}_gates.json"
    df.to_csv(csv_path, index=False)
    anchors_path = out_dir / "fidelity_anchors.csv"
    pd.DataFrame(FIDELITY_ANCHORS).to_csv(anchors_path, index=False)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"label": label, "created_utc": ts,
                   "matrix_dir": str(matrix_dir), "source": cfg["source"],
                   "trace": cfg["trace"], "checks": rows,
                   "fidelity_anchors": FIDELITY_ANCHORS}, f,
                  ensure_ascii=False, indent=2, default=str)

    n_pass = int(df["passed"].sum())
    print("\n" + "=" * 84)
    print(f"结论：{n_pass}/{len(df)} 项通过" +
          ("  —— 全部通过 ✓" if n_pass == len(df) else "  —— 存在未通过项 ✗"))
    print(f"记录：{csv_path}")
    print(f"      {json_path}")
    print(f"      {anchors_path}（{len(FIDELITY_ANCHORS)} 条参数来源档案，门 4）")
    print("=" * 84)
    return 0 if n_pass == len(df) else 1


if __name__ == "__main__":
    raise SystemExit(main())

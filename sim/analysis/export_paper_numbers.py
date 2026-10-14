"""导出论文正文要用的每一个数字（从三批分析产物里读，不手抄）。

用法（在 `sim/` 下执行）：

    .venv/Scripts/python.exe analysis/export_paper_numbers.py

产物：`results/paper_numbers.txt`——按论文小节编号排列的"`标签 = 值`"清单。
写正文时**只从这个文件复制数字**；审稿人质疑时重跑本脚本即可复算。

为什么单独做这一步（而不是写正文时现查 CSV）：正文有上百个数字，
手抄必然出错，而且错了之后没人能快速发现。把"取数"变成一条可复算的命令，
是这类实证论文最便宜的防错手段。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                            # noqa: BLE001
    pass

BATCHES = {
    "synthetic": ROOT / "results/analysis_v2",
    "azure": ROOT / "results/analysis_trace_v2",
    "burstgpt": ROOT / "results/analysis_burstgpt_v2",
}
OUT = ROOT / "results/paper_numbers.txt"
lines: list[str] = []


def emit(tag: str, value) -> None:
    lines.append(f"{tag} = {value}")


def head(title: str) -> None:
    lines.append("")
    lines.append(f"# {'=' * 74}")
    lines.append(f"# {title}")
    lines.append(f"# {'=' * 74}")


def rd(path: Path) -> pd.DataFrame | None:
    try:
        return pd.read_csv(path)
    except Exception:                                        # noqa: BLE001
        return None


def fmt(x, nd=2) -> str:
    try:
        v = float(x)
    except Exception:                                        # noqa: BLE001
        return str(x)
    if not np.isfinite(v):
        return "nan"
    return f"{v:.{nd}f}"


def _int_or_nan(x) -> str:
    try:
        v = float(x)
    except Exception:                                        # noqa: BLE001
        return "nan"
    return "nan" if not np.isfinite(v) else str(int(v))


def main() -> int:
    # ---------------- §4 setup：规模、门、负载强度 ----------------
    for name, d in BATCHES.items():
        head(f"§4 Setup · {name} 批")
        runs = None
        for m in ("main", "staleness", "method"):
            for p in sorted(d.glob(f"{m}_*_runs.csv")):
                df = rd(p)
                if df is not None and not df.empty:
                    runs = df if runs is None else pd.concat([runs, df], ignore_index=True)
        if runs is not None and not runs.empty:
            emit(f"{name}.n_runs_in_main_staleness_method", len(runs))
            emit(f"{name}.n_cells", runs["cell_id"].nunique())
            emit(f"{name}.n_seeds", sorted(runs["seed"].unique().tolist()))
        rho = rd(d / "load_intensity_rho.csv")
        if rho is not None and not rho.empty:
            for col in ("rho", "rho_compute", "rho_network", "load_ratio"):
                if col in rho.columns:
                    emit(f"{name}.{col}_min_max", f"{fmt(rho[col].min(),3)} .. {fmt(rho[col].max(),3)}")
        burst = rd(d / "load_burstiness.csv")
        if burst is not None and not burst.empty:
            for col in ("arrival_cv_w1", "arrival_cv_w10", "overload_frac_w10"):
                if col in burst.columns:
                    emit(f"{name}.{col}_per_workload_mean",
                         {str(k): fmt(v, 3) for k, v in
                          burst.groupby("workload")[col].mean().items()})
        persist = rd(d / "load_persistence.csv")
        if persist is not None and not persist.empty:
            for col in ("autocorr_1s", "work_rate_change_5s", "fano_1s"):
                if col in persist.columns:
                    emit(f"{name}.{col}_per_workload_mean",
                         {str(k): fmt(v, 3) for k, v in
                          persist.groupby("workload")[col].mean().items()})
        for g in sorted(d.glob("*_gates.json")):
            import json
            try:
                j = json.loads(g.read_text(encoding="utf-8"))
                emit(f"{name}.gates_file", json.dumps(j, ensure_ascii=False)[:600])
            except Exception:                                # noqa: BLE001
                pass

    # ---------------- §5 RQ1：收益边界 ----------------
    for name, d in BATCHES.items():
        head(f"§5 RQ1 · {name}")
        b = rd(d / "rq13_bandwidth_effect.csv")
        if b is not None and not b.empty:
            for _, r in b.iterrows():
                # 键里必须带 arm（同一个 profile 下有多条臂，否则会互相覆盖）
                emit(f"{name}.bandwidth.{r.get('net_profile')}.{r.get('data_volume')}"
                     f".{r.get('workload')}.{r.get('arm')}",
                     f"dp95={fmt(r.get('dp95_pct'),1)}% "
                     f"net_share={fmt(r.get('net_share_na'),3)}")
        z = rd(d / "rq13_zero_crossings.csv")
        if z is not None and not z.empty:
            emit(f"{name}.rq13_zero_crossings_rows", len(z))
        r = rd(d / "rq8_rho_sweep_effect.csv")
        if r is not None and not r.empty:
            key = [c for c in ("workload", "network", "target_rho") if c in r.columns]
            if {"dp95_pct"} <= set(r.columns):
                g = r.groupby(key)["dp95_pct"].mean()
                emit(f"{name}.rho_sweep_dp95_min_max",
                     f"{fmt(g.min(),1)}% .. {fmt(g.max(),1)}%")


    # ---------------- §5 RQ1 主矩阵：**逐策略**从 runs.csv 直接算（不经过 analyze 的候选定义）--------
    # 【为什么要自己算】analyze 的 rq1_* 表里候选策略固定是 NetworkAware(v1)，
    # 若照抄那张表会把 v1 的小收益当成"网络感知的收益"（v1 只看传输时间、没有链路排队项）。
    head("§5 RQ1 主矩阵（逐策略，5 种子，按种子配对）")
    # 注意：原始 runs.csv 在 matrix 目录，不在 analysis 目录
    MTX = {"synthetic": ROOT / "results/matrix_v2",
           "azure": ROOT / "results/matrix_trace_v2",
           "burstgpt": ROOT / "results/matrix_burstgpt_v2"}
    for name, d in MTX.items():
        runs = None
        for p2 in sorted(d.glob("main_*_runs.csv")):
            df = rd(p2)
            if df is not None and not df.empty:
                runs = df
        if runs is None or runs.empty:
            continue
        for (wl, net, st), g in runs.groupby(["workload", "network", "staleness"]):
            piv2 = g.pivot_table(index="seed", columns="strategy", values="sojourn_p95")
            if "LeastLoad" not in piv2.columns:
                continue
            base = piv2["LeastLoad"]
            for cand in ("NetworkAware", "NetworkAware2"):
                if cand not in piv2.columns:
                    continue
                rel = ((piv2[cand] - base) / base * 100.0).dropna()
                emit(f"rq1main.{name}.{wl}.{net}.{st}.{cand}",
                     f"mean={fmt(rel.mean(),1)}% median={fmt(rel.median(),1)}% "
                     f"n={len(rel)} p95_LL={fmt(base.mean(),2)} "
                     f"p95_cand={fmt(piv2[cand].mean(),2)} "
                     f"better={int((rel<0).sum())}/{len(rel)}")

    # ---------------- §5 机制：对齐度 ----------------
    head("§5 机制 · 对齐度（§17）+ 规模（§18）+ 直接探针")
    a17 = rd(BATCHES["synthetic"] / "rq17_alignment_effect.csv")
    if a17 is not None and not a17.empty:
        for _, r in a17.iterrows():
            emit(f"align.{r['align']}.{r['workload']}.{r['network']}",
                 f"dp95={fmt(r['dp95_pct'],1)}% p95_NA2={fmt(r['p95_na'],2)} "
                 f"p95_LL={fmt(r['p95_base'],2)} net_share={fmt(r['net_share_na'],3)} "
                 f"n_seeds={int(r['n_seeds'])}")
    a18 = rd(BATCHES["synthetic"] / "rq18_alignment_scale_effect.csv")
    if a18 is not None and not a18.empty:
        for _, r in a18.iterrows():
            emit(f"alignscale.{int(r['n_nodes'])}.{r['align']}",
                 f"dp95={fmt(r['dp95_pct'],1)}% CI=±{fmt(r['dp95_se'],1)} "
                 f"p95_NA2={fmt(r['p95_na'],2)} p95_LL={fmt(r['p95_base'],2)}")
    lc = rd(BATCHES["synthetic"] / "link_choice_by_scale.csv")
    if lc is not None and not lc.empty:
        g = lc.groupby(["align", "n_nodes", "strategy"], dropna=False).agg(
            rank=("mean_bandwidth_rank", "mean"),
            frac=("frac_top_third_bandwidth", "mean"),
            rho=("rank_corr", "first")).reset_index()
        for _, r in g.iterrows():
            emit(f"linkchoice.{r['align']}.n{int(r['n_nodes'])}.{r['strategy']}",
                 f"mean_bw_rank={fmt(r['rank'],2)} top_third={fmt(r['frac']*100,0)}% "
                 f"rho={fmt(r['rho'],2)}")

    # ---------------- §6 RQ2：状态新鲜度 ----------------
    head("§6 RQ2 · 二维陈旧度（§16）")
    s16 = rd(BATCHES["synthetic"] / "rq16_staleness2d_effect.csv")
    if s16 is not None and not s16.empty:
        for _, r in s16.iterrows():
            emit(f"s2d.{r['workload']}.q{fmt(r['staleness_queue_s'],0)}_n{fmt(r['staleness_network_s'],0)}",
                 f"dp95={fmt(r['dp95_pct'],0)}%")
    head("§6 RQ2 · 聚合窗口（§15）")
    w15 = rd(BATCHES["synthetic"] / "rq15_obs_window_effect.csv")
    if w15 is not None and not w15.empty:
        key = [c for c in ("channel", "win_s", "workload", "network")
               if c in w15.columns]
        if "dp95_pct" in w15.columns and key:
            for k, v in w15.groupby(key)["dp95_pct"].mean().items():
                emit(f"window.{k}", fmt(v, 1))
            # 绝对 p95：窗口把"双方"都拖慢多少（论文要引这个数）
            for k, sub in w15.groupby(key, dropna=False):
                emit(f"window_abs.{k}",
                     f"p95_NA={fmt(sub['p95_na'].mean(),2)} "
                     f"p95_base={fmt(sub['p95_base'].mean(),2)}")

    # ---------------- §7 方法评估 ----------------
    for name, d in BATCHES.items():
        head(f"§7 方法 · {name}")
        e = rd(d / "rq19_decay_rule_effect.csv")
        if e is not None and not e.empty:
            sel = e[(e["staleness_queue_s"].isin([0.0, 5.0]))
                    & (e["staleness_network_s"].isin([0.0, 60.0]))]
            for _, r in sel.iterrows():
                emit(f"m.{name}.{r['workload']}.{r['network']}.q{fmt(r['staleness_queue_s'],0)}"
                     f"n{fmt(r['staleness_network_s'],0)}.{r['arm']}",
                     f"vs_LL={fmt(r['dp95_vs_leastload_pct'],1)}"
                     f"(med {fmt(r['dp95_vs_leastload_median_pct'],1)}) "
                     f"vs_NA2={fmt(r['dp95_vs_na2_pct'],1)} "
                     f"seeds_better/worse_vs_LL="
                     f"{_int_or_nan(r.get('n_seeds_better_vs_leastload'))}"
                     f"/{_int_or_nan(r.get('n_seeds_worse_vs_leastload'))}")
        v = rd(d / "rq19_decay_rule_verdict.csv")
        if v is not None and not v.empty:
            for _, r in v.iterrows():
                emit(f"verdict.{name}.{r['arm']}",
                     f"better_than_both={_int_or_nan(r['n_better_than_both'])}/"
                     f"{_int_or_nan(r['n_conditions'])} "
                     f"worse_than_both={_int_or_nan(r['n_worse_than_both'])} "
                     f"better_than_na2={_int_or_nan(r['n_better_than_na2'])} "
                     f"worse_than_na2={_int_or_nan(r['n_worse_than_na2'])}")
        ab = rd(d / "rq21_channel_threshold_ablation.csv")
        if ab is not None and not ab.empty:
            for _, r in ab.iterrows():
                emit(f"ablate.{name}.{r['variant']}",
                     f"vs_primary={fmt(r['vs_primary_mean_pct'],1)}±{fmt(r['vs_primary_ci'],1)} "
                     f"worst={fmt(r['vs_primary_worst_pct'],1)} "
                     f"flips={_int_or_nan(r['n_conditions_sign_flip_vs_na2'])}/"
                     f"{_int_or_nan(r['n_conditions'])}")

    # ---------------- §9 degeneracy ----------------
    head("§9 退化与门记录")
    f7 = rd(BATCHES["synthetic"] / "feasibility_degeneracy.csv")
    if f7 is not None and not f7.empty:
        if "verdict" in f7.columns:
            emit("degeneracy.verdict_counts",
                 {str(k): int(v) for k, v in f7["verdict"].value_counts().items()})
        emit("degeneracy.n_rows", len(f7))

    # ---------------- §8R 真实系统锚点（B2 同云基线 + C 跨云；2026-10-12 增补） ----------------
    # 取数：results/b2/.../decision.json、results/c/.../decision.json（手工誊写件），
    #      C 的 KV 账目自 results/c/.../metrics_{d1,d2}_t{0,1,2}.txt 重算（冻结判据原始出处）。
    # 统计性质：B2 = 4 配对轮描述性；C = 2 方向描述性；均不作统计推断。
    import json as _json
    import re as _re

    def _loadj(p: Path):
        try:
            return _json.loads(p.read_text(encoding="utf-8"))
        except Exception:                                    # noqa: BLE001
            return None

    def _metrics(p: Path) -> dict:
        m = {}
        try:
            for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
                mm = _re.match(r"^(\S+)\{([^}]*)\}\s+([0-9.e+]+)$", ln.strip())
                if mm:
                    m[f"{mm.group(1)}{{{mm.group(2)}}}"] = float(mm.group(3))
        except Exception:                                    # noqa: BLE001
            pass
        return m

    def _pick(m: dict, name: str, frag: str = "") -> float:
        for k, v in m.items():
            if k.startswith(name + "{") and frag in k:
                return v
        return 0.0

    head("§8R 真实系统锚点 · B2 同云基线（n=4 配对轮）+ C 跨云（n=2 方向）· 描述性")
    b2j = _loadj(ROOT / "results/b2/b2_20261011T081400Z/decision.json")
    cj = _loadj(ROOT / "results/c/b2_c_20261012T031500Z/decision.json")
    if b2j:
        emit("B2.unit", '"4 paired rounds, descriptive system baseline"')
        emit("B2.request_account", b2j.get("request_account"))
        rp = b2j.get("results_per_round", {})
        for tier in ("serial_c1", "conc4_c4"):
            for m in ("TTFT", "avg_token_gap", "total"):
                d = rp.get(tier, {}).get(m, {})
                emit(f"B2.{tier}.{m}.rounds_pct", d.get("rounds"))
                emit(f"B2.{tier}.{m}.typical_pct", d.get("typical"))
                emit(f"B2.{tier}.{m}.range_pct", d.get("range"))
        for arm in ("base_c1", "disagg_c1", "base_c4", "disagg_c4"):
            emit(f"B2.descriptive.{arm}", b2j.get("descriptive_per_request", {}).get(arm))
        emit("B2.kv", b2j.get("kv_per_disagg_round"))
    if cj:
        emit("C.unit", '"2 directions, descriptive feasibility measurement"')
        rec = cj.get("questions", {}).get("Q3_增加多少", {}).get("recorded", {})
        for dkey, dtag in (("D1_阿里→腾讯", "D1"), ("D2_腾讯→阿里", "D2")):
            for tier in ("serial", "conc4"):
                emit(f"C.{dtag}.{tier}", rec.get(dkey, {}).get(tier))
        # Q1 账目：自 KV 快照重算（协议 §4 冻结判据）
        for dtag in ("d1", "d2"):
            cdir = ROOT / "results/c/b2_c_20261012T031500Z"
            t0 = _metrics(cdir / f"metrics_{dtag}_t0.txt")
            t1 = _metrics(cdir / f"metrics_{dtag}_t1.txt")
            xb = _pick(t1, "vllm:nixl_bytes_transferred_sum") - _pick(t0, "vllm:nixl_bytes_transferred_sum")
            xc = _pick(t1, "vllm:nixl_bytes_transferred_count") - _pick(t0, "vllm:nixl_bytes_transferred_count")
            fail = sum(_pick(t1, n) - _pick(t0, n) for n in
                       ("vllm:nixl_num_failed_transfers_total",
                        "vllm:nixl_num_failed_notifications_total",
                        "vllm:nixl_num_kv_expired_reqs_total"))
            ext = (_pick(t1, "vllm:prompt_tokens_by_source_total", "external_kv_transfer")
                   - _pick(t0, "vllm:prompt_tokens_by_source_total", "external_kv_transfer"))
            loc = (_pick(t1, "vllm:prompt_tokens_by_source_total", "local_compute")
                   - _pick(t0, "vllm:prompt_tokens_by_source_total", "local_compute"))
            emit(f"C.Q1.{dtag.upper()}.bytes_per_token", fmt(xb / ext if ext else float("nan"), 1))
            emit(f"C.Q1.{dtag.upper()}.transfers", int(xc))
            emit(f"C.Q1.{dtag.upper()}.failed", int(fail))
            emit(f"C.Q1.{dtag.upper()}.local_recompute", int(loc))
            emit(f"C.Q1.{dtag.upper()}.bytes_GB", fmt(xb / 1e9, 3))
            emit(f"C.Q1.{dtag.upper()}.external_kv_tokens", int(ext))
        # 派生：相对 B2 同云拆分臂的百分比（供正文引用，避免正文手算）
        b2d = b2j.get("descriptive_per_request", {})
        for dtag, tier, b2key in (("D1", "serial", "disagg_c1"), ("D2", "serial", "disagg_c1"),
                                  ("D1", "conc4", "disagg_c4"), ("D2", "conc4", "disagg_c4")):
            cd = rec.get("D1_阿里→腾讯" if dtag == "D1" else "D2_腾讯→阿里", {}).get(tier, {})
            b = b2d.get(b2key, {})
            for ck, bk, nm in (("ttft_p50_s", "ttft_p50", "ttft"), ("total_p50_s", "total_p50", "total"),
                               ("tok_gap_p50_s", "tok_gap_p50", "tok_gap")):
                if cd.get(ck) and b.get(bk):
                    emit(f"C.vs_B2.{dtag}.{tier}.{nm}_pct", fmt((cd[ck] / b[bk] - 1) * 100, 0))
        # 带宽（dd 200MB 走隧道实测；源：k8s_test/C_REPORT.md §2/§1）
        emit("C.bandwidth.MBps.A_to_T", 8.6)
        emit("C.bandwidth.MBps.T_to_A", 1.7)
        emit("C.bandwidth.MBps.T_to_B", 11.7)
        # 背景斜率图素材（描述性；q01–q05 按题长事后分组；KV 用字符数/4.9 估算，
        # 4.9 chars/token 为 q01 实测：3191 chars = 651 tokens，源 C 日志）
        import json as _json2
        try:
            pr = {p["id"]: p for p in _json2.loads(
                (ROOT / "k8s_test/b2_prompt_orders/prompts_r1.json").read_text(encoding="utf-8"))}
        except Exception:                                    # noqa: BLE001
            pr = {}
        for dtag in ("d1", "d2"):
            f = ROOT / "results/c/b2_c_20261012T031500Z" / f"perf_{dtag}_c1.json"
            j = _loadj(f)
            if not j:
                continue
            by = {}
            for r in j.get("requests", []):
                pid = r["id"].rsplit("_rep", 1)[0]
                by.setdefault(pid, []).append(r["ttft_s"])
            for pid in sorted(by, key=lambda x: len(pr.get(x, {}).get("prompt", ""))):
                med = sorted(by[pid])[len(by[pid]) // 2]
                ch = len(pr.get(pid, {}).get("prompt", ""))
                emit(f"C.bg.{dtag.upper()}.{pid}",
                     f"ttft_p50={fmt(med, 2)} chars={ch} est_KV_MB={fmt(ch / 4.9 * 28796 / 1e6, 1)}")
    emit("D.status", '"planned not executed (D-mini); see k8s_test/D_MINI_PREREG.md"')

    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[导出] {OUT}（{len(lines)} 行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""B1_4C_PREREG_3 的分析脚本：C1 冒烟 + E4/E1/E2/E3（口径冻结在预注册 §5）。

输入是 `b1_client.py` 的输出 JSON（列表，每条含 token_ids 与 logprobs）。
注意：上报值是 **log 空间**（负数），本脚本一律 exp() 成概率再算（预注册 §2）。
只用标准库。子命令：
  smoke   单个臂文件 → C1 检查（吐词 == processed 分第一名）；全过退出码 0，否则 1
  analyze 运行目录 → 按命名约定配对 r1..r3，算 E4/E1/E2/E3，落盘明细与汇总
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path


def load_records(path: Path) -> list:
    """读 b1_client.py 的输出：顶层是元数据字典，请求在 `requests` 键下（不是纯列表）。"""
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    recs = d.get("requests") if isinstance(d, dict) else d
    if not isinstance(recs, list):
        raise ValueError(f"{path}: 找不到 requests 列表（实际顶层 {type(d).__name__}）")
    return recs


def steps_of(rec: dict):
    lp = rec.get("logprobs") or {}
    toks = lp.get("tokens") or []
    tops = lp.get("top_logprobs") or []
    ids = rec.get("token_ids") or []
    if not toks or len(tops) != len(toks):
        return None
    return list(zip(ids, toks, tops))


def cmd_smoke(args) -> int:
    recs = load_records(Path(args.file))
    n = bad = 0
    for rec in recs:
        st = steps_of(rec)
        if st is None:
            print(f"!! {rec.get('id')}: logprobs 结构缺失")
            return 1
        for j, (_tid, tok, top) in enumerate(st):
            n += 1
            if not isinstance(top, dict) or not top:
                bad += 1
                print(f"   {rec['id']} j={j}: top 空")
                continue
            best = max(top, key=top.get)
            if tok != best:
                bad += 1
                print(f"   {rec['id']} j={j}: 吐词 {tok!r} != 第一名 {best!r}")
    print(f"[C1 冒烟] 步数={n} 不合={bad} → {'PASS' if bad == 0 else 'FAIL'}")
    return 0 if (bad == 0 and n > 0) else 1


def first_divergence(a_ids, b_ids):
    for i, (x, y) in enumerate(zip(a_ids, b_ids)):
        if x != y:
            return i
    return None


def cmd_analyze(args) -> int:
    d = Path(args.run_dir)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    pairs = {}
    for k in (1, 2, 3):
        b = d / f"arm_baseline_r{k}.json"
        g = d / f"arm_disagg_r{k}.json"
        if b.exists() and g.exists():
            pairs[k] = (b, g)
    if not pairs:
        print("!! 没有可配对的 arm_baseline_r*/arm_disagg_r*")
        return 1

    # E3：同臂重复性（r2 vs r3）
    e3 = {}
    for arm in ("baseline", "disagg"):
        f2, f3 = d / f"arm_{arm}_r2.json", d / f"arm_{arm}_r3.json"
        if f2.exists() and f3.exists():
            t2 = {r["id"]: r.get("token_ids") for r in load_records(f2)}
            t3 = {r["id"]: r.get("token_ids") for r in load_records(f3)}
            same = all(t2[k] == t3.get(k) for k in t2)
            e3[arm] = {"r2_vs_r3_identical": same,
                       "per_req": {k: (t2[k] == t3.get(k)) for k in t2}}
            print(f"[E3] {arm}: r2 vs r3 逐位相同 = {same}")
    if not all(v["r2_vs_r3_identical"] for v in e3.values()):
        print("!! E3 未过：先修测量，不解读 E1/E2/E4（预注册 §8）")

    e4_rows, m_vals, deltas = [], [], []
    identical = 0
    for k, (bf, gf) in sorted(pairs.items()):
        brecs = {r["id"]: r for r in load_records(bf)}
        grecs = {r["id"]: r for r in load_records(gf)}
        for pid in sorted(brecs):
            br, gr = brecs[pid], grecs.get(pid)
            if gr is None:
                continue
            bst, gst = steps_of(br), steps_of(gr)
            # 噪声地板原料：两臂选同一个词的步，top 窗口交集上的最大概率差
            for (_bi, bt, btop), (_gi, gt, gtop) in zip(bst or [], gst or []):
                if bt == gt and isinstance(btop, dict) and isinstance(gtop, dict):
                    for t in set(btop) & set(gtop):
                        deltas.append(abs(math.exp(btop[t]) - math.exp(gtop[t])))
            i = first_divergence(br.get("token_ids") or [], gr.get("token_ids") or [])
            if i is None:
                identical += 1
                e4_rows.append({"pair": k, "id": pid, "identical": True})
                continue
            btop = bst[i][2] if bst and i < len(bst) else {}
            gtop = gst[i][2] if gst and i < len(gst) else {}
            a, b = bst[i][1], gst[i][1]          # 两边实际选的词（token 文本）
            def p(top, t):
                return math.exp(top[t]) if isinstance(top, dict) and t in top else None
            pa, pb = p(btop, a), p(btop, b)
            qa, qb = p(gtop, a), p(gtop, b)
            row = {"pair": k, "id": pid, "identical": False, "div_step": i,
                   "a": a, "b": b, "pa": pa, "pb": pb, "qa": qa, "qb": qb}
            # E1（预注册 §5：比值与分档，只作描述）
            r_base = pb / pa if (pa and pb) else None
            r_disagg = qa / qb if (qa and qb) else None
            if r_base is not None and r_disagg is not None:
                m = min(r_base, r_disagg)
                row.update({"r_base": r_base, "r_disagg": r_disagg, "m": m,
                            "bucket": "A" if m >= 0.90 else ("B" if m >= 0.50 else "C")})
            # E2 决策余量
            if pa is not None and pb is not None:
                m_vals.append(abs(pa - pb))
            if qa is not None and qb is not None:
                m_vals.append(abs(qa - qb))
            e4_rows.append(row)

    # E2 汇总
    def q(xs, nd=5):
        return {"min": round(min(xs), nd), "med": round(statistics.median(xs), nd),
                "p90": round(sorted(xs)[int(0.9 * (len(xs) - 1))], nd),
                "max": round(max(xs), nd), "n": len(xs)} if xs else None
    e2 = {"noise_floor_delta": q(deltas), "decision_margin_M": q(m_vals)}
    if deltas:
        dmed = statistics.median(deltas)
        dp90 = sorted(deltas)[int(0.9 * (len(deltas) - 1))]
        e2["M_over_delta_med"] = [round(m / dmed, 2) for m in m_vals[:50]] if dmed > 0 else None
        e2["delta_med"] = dmed
        e2["delta_p90"] = dp90
    # E1 汇总
    ms = [r["m"] for r in e4_rows if r.get("m") is not None]
    buckets = {}
    for r in e4_rows:
        if r.get("bucket"):
            buckets[r["bucket"]] = buckets.get(r["bucket"], 0) + 1
    summary = {
        "n_pairs_files": len(pairs), "n_requests": len(e4_rows),
        "n_identical": identical,
        "n_divergent": sum(1 for r in e4_rows if r.get("identical") is False),
        "E1_m": q(ms), "E1_buckets": buckets,
        "E2": e2, "E3": e3,
    }
    (out / "e4_table.json").write_text(
        json.dumps(e4_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    with (out / "summary.txt").open("w", encoding="utf-8") as f:
        f.write(json.dumps(summary, ensure_ascii=False, indent=2) + "\n\n== E4 逐条 ==\n")
        for r in e4_rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"[汇总] 请求={len(e4_rows)} 逐位相同={identical} 有分歧={summary['n_divergent']}")
    print(f"[E1] m 分布={q(ms)} 分档={buckets}")
    print(f"[E2] Δ={e2['noise_floor_delta']}  M={e2['decision_margin_M']}")
    print(f"落盘：{out / 'e4_table.json'}、{out / 'summary.json'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("smoke")
    s.add_argument("--file", required=True)
    s.set_defaults(fn=cmd_smoke)
    a = sub.add_parser("analyze")
    a.add_argument("--run-dir", required=True)
    a.add_argument("--out-dir", required=True)
    a.set_defaults(fn=cmd_analyze)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())

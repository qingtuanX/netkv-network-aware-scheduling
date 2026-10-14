#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""4c 分歧原因诊断（第二轮）的指标计算 —— 严格按 `B1_4C_PREREG_2.md` §4 实现。

**这不是验收脚本。** 它不判"通过/不通过"，只回答预注册里那一个问题：
    「两边首次分歧的那一步，两台机器是不是本来就几乎不在乎该选哪个词？」

三个预注册指标：
  E1 主指标「竞争词比值」：
      a = 对照臂在分歧处选的词；b = 拆分臂在分歧处选的词
      r_base   = p_base(b)   / p_base(a)      对照臂给"对方那个词"打了几折
      r_disagg = p_disagg(a) / p_disagg(b)    拆分臂给"对方那个词"打了几折
      m = min(r_base, r_disagg)               ← 主统计量
      分档（冻结，仅描述）：m≥0.90 = A档；0.50≤m<0.90 = B档；m<0.50 = C档。
      注意：分档**不是通过线**，报告里只给分布与计数。

  E2 辅助「翻过去需要多大余量」：
      Δ(j) = 在两臂选了**同一个词**的步上，两臂 top-N **交集**里每个 token 的
             |p_base − p_disagg| 的最大值。取其**中位数与 p90** 作为"数值噪声地板"。
      M    = 首次分歧那一步，各臂"自己选的词"与"对方选的词"的概率差。
      报告 M / Δ。

  E3 对照「同臂重复性」：由调用方用同一脚本跑 `r2 vs r3`，看是否 5/5 逐位相同。

两个刻意的口径选择（写在这里，免得被误读）：
  * Δ 只在**两臂 top-N 的交集**上算。若把"只在一边出现"的词按 0 计，
    会把这个数夸大 —— 那不是"两台机器对同一个词的看法差多少"。
  * 概率用 `exp(logprob)` 还原。`logprobs=N` 的 N 由调用方决定，
    本轮预注册写死 **20**（上一轮是 5，出现过"对方的词没进 top-5"）。

退出码：0 = 算完了；1 = 有数据缺失致没法算（如两臂都没 logprobs）。**"分歧很大"不会返回非 0。**
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path

# 预注册 §4 冻结的分档阈值。**改成别的值就等于改预注册**，必须另立文件。
BAND_A = 0.90   # m >= 0.90  → A 档（两臂都近乎并列）
BAND_B = 0.50   # 0.50 <= m < 0.90 → B 档


def setup_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def first_diff(a, b) -> int:
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return i
    return -1 if len(a) == len(b) else n


def norm_top(entry) -> dict[str, float]:
    """把一格 top_logprobs 规整成 {token 文本: 对数概率}。取不到就返回空 dict（不补猜）。"""
    out: dict[str, float] = {}
    if entry is None:
        return out
    if isinstance(entry, dict):
        for k, v in entry.items():
            try:
                out[str(k)] = float(v)
            except (TypeError, ValueError):
                continue
    elif isinstance(entry, (list, tuple)):
        for it in entry:
            if isinstance(it, dict):
                tok = it.get("token", it.get("token_str"))
                lp = it.get("logprob")
                if tok is not None and lp is not None:
                    try:
                        out[str(tok)] = float(lp)
                    except (TypeError, ValueError):
                        continue
    return out


def P(lp: float | None) -> float | None:
    if lp is None:
        return None
    try:
        return float(math.exp(lp))
    except (OverflowError, ValueError):
        return float("nan")


def step(rec: dict, j: int) -> dict | None:
    """取某条请求第 j 步：实际选的词、它的概率、以及 {token: 概率} 的 top-N 表。"""
    lps = rec.get("logprobs")
    if not isinstance(lps, dict):
        return None
    tokens = lps.get("tokens") or []
    tlogps = lps.get("token_logprobs") or []
    tops = lps.get("top_logprobs") or []
    if j < 0 or j >= max(len(tokens), len(tlogps), len(tops)):
        return None
    chosen = tokens[j] if j < len(tokens) else None
    cp = P(tlogps[j]) if j < len(tlogps) else None
    table = {k: P(v) for k, v in norm_top(tops[j] if j < len(tops) else None).items()}
    return {"chosen": chosen, "chosen_p": cp, "table": table}


def band_of(m: float | None) -> str:
    if m is None or not isinstance(m, float) or math.isnan(m):
        return "n/a"
    if m >= BAND_A:
        return "A（两臂都近乎并列）"
    if m >= BAND_B:
        return "B（有明显倾向但仍接近）"
    return "C（各自都有明确偏好）"


def fmt(x, nd=4) -> str:
    return f"{x:.{nd}f}" if isinstance(x, (int, float)) and not math.isnan(x) else "n/a"


def quantile(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(s) - 1)
    frac = pos - lo
    return s[lo] * (1 - frac) + s[hi] * frac


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="4c 分歧原因诊断指标（B1_4C_PREREG_2.md §4）")
    ap.add_argument("--baseline", nargs="+", required=True, help="对照臂 arm_*.json（可给多个，与 --disagg 按位置配对）")
    ap.add_argument("--disagg", nargs="+", required=True, help="拆分臂 arm_*.json")
    ap.add_argument("--out", required=True, help="报告输出路径")
    ap.add_argument("--label", default="", help="写进报告头的说明，例如「确认性：v2 提示词」")
    args = ap.parse_args(argv)
    setup_streams()

    if len(args.baseline) != len(args.disagg):
        ap.error(f"--baseline 给了 {len(args.baseline)} 个、--disagg 给了 {len(args.disagg)} 个，必须一一对应")

    L: list[str] = []
    w = L.append
    w("=" * 78)
    w("4c 分歧原因诊断指标（B1_4C_PREREG_2.md §4：E1 / E2）")
    if args.label:
        w(f"用途标注 : {args.label}")
    w("=" * 78)
    w("**这不是验收结论，不判通过/不通过**；它只回答「分歧处是不是本来就无所谓选哪个词」。")
    w("")

    n_pairs = len(args.baseline)
    e1_rows: list[dict] = []
    delta_pool: list[float] = []
    n_req = 0
    n_ident = 0
    n_div = 0
    n_nodata = 0
    logprob_window: set[int] = set()

    for k in range(n_pairs):
        bp, dp = Path(args.baseline[k]), Path(args.disagg[k])
        base, dis = load(bp), load(dp)
        logprob_window.add(base.get("logprobs_requested") or 0)
        logprob_window.add(dis.get("logprobs_requested") or 0)
        br = {r["id"]: r for r in base["requests"]}
        dr = {r["id"]: r for r in dis["requests"]}
        w(f"---------- 配对 {k+1}/{n_pairs}: {bp.name}  ↔  {dp.name} ----------")
        w(f"  对照臂 url={base.get('target_url')}  拆分臂 url={dis.get('target_url')}")
        for rid in sorted(set(br) & set(dr)):
            b, d = br[rid], dr[rid]
            n_req += 1
            tid_b, tid_d = b.get("token_ids"), d.get("token_ids")
            if not tid_b or not tid_d:
                n_nodata += 1
                w(f"  {rid:<12} 缺 token_ids → 跳过")
                continue
            i = first_diff(tid_b, tid_d)

            # ---- E2 的噪声地板：两臂选了同一个词的步 ----
            for j in range(min(len(tid_b), len(tid_d))):
                if tid_b[j] != tid_d[j]:
                    break  # 之后不再"选同一个词"
                sb, sd = step(b, j), step(d, j)
                if not sb or not sd:
                    continue
                common = set(sb["table"]) & set(sd["table"])
                if not common:
                    continue
                delta_pool.append(max(abs(sb["table"][t] - sd["table"][t]) for t in common))

            if i == -1:
                n_ident += 1
                w(f"  {rid:<12} 逐位相同（{len(tid_b)} token）")
                continue

            n_div += 1
            sb, sd = step(b, i), step(d, i)
            if not sb or not sd:
                n_nodata += 1
                w(f"  {rid:<12} 分歧位置={i} 但缺 logprobs → E1 无法算（**不补猜**）")
                continue
            a, bb = sb["chosen"], sd["chosen"]
            pa, pb = sb["table"].get(a), sb["table"].get(bb)
            qa, qb = sd["table"].get(a), sd["table"].get(bb)
            row = {"rid": rid, "pair": k + 1, "idx": i, "a": a, "b": bb,
                   "pa": pa, "pb": pb, "qa": qa, "qb": qb}
            if not all(isinstance(v, float) and not math.isnan(v) for v in (pa, pb, qa, qb)):
                w(f"  {rid:<12} 分歧位置={i} 但竞争词有一侧没进 top-N → E1 无法算：")
                w(f"               对照臂 top-N 里有 a={a!r}? {a in sb['table']}；有 b={bb!r}? {bb in sb['table']}")
                w(f"               拆分臂 top-N 里有 a={a!r}? {a in sd['table']}；有 b={bb!r}? {bb in sd['table']}")
                n_nodata += 1
                row["m"] = None
                e1_rows.append(row)
                continue
            r_base = pb / pa if pa else float("nan")
            r_disagg = qa / qb if qb else float("nan")
            m = min(r_base, r_disagg)
            row.update({"r_base": r_base, "r_disagg": r_disagg, "m": m,
                        "M_base": abs(pa - pb), "M_disagg": abs(qa - qb)})
            e1_rows.append(row)
            w(f"  {rid:<12} 分歧位置={i}   a={a!r}(对照)  b={bb!r}(拆分)")
            w(f"               对照臂: p(a)={fmt(pa)}  p(b)={fmt(pb)}   r_base={fmt(r_base)}   M_base={fmt(abs(pa-pb))}")
            w(f"               拆分臂: p(a)={fmt(qa)}  p(b)={fmt(qb)}   r_disagg={fmt(r_disagg)}   M_disagg={fmt(abs(qa-qb))}")
            w(f"               **m = min(r_base,r_disagg) = {fmt(m)}  →  {band_of(m)}**")
        w("")

    # ---------------- E1 汇总 ----------------
    valid = [r for r in e1_rows if isinstance(r.get("m"), float) and not math.isnan(r["m"])]
    w("=" * 78)
    w("E1 汇总：竞争词比值（主指标）")
    w("=" * 78)
    w(f"请求总数 {n_req}：逐位相同 {n_ident}、发生分歧 {n_div}、无法计算 {n_nodata}")
    w(f"logprobs 窗口（请求值）: {sorted(logprob_window)}")
    w("")
    if not valid:
        w("**没有任何一条能算出 m** —— 见上面的逐条原因（多半是竞争词没进 top-N）。")
    else:
        ms = [r["m"] for r in valid]
        rb = [r["r_base"] for r in valid if isinstance(r.get("r_base"), float)]
        rd = [r["r_disagg"] for r in valid if isinstance(r.get("r_disagg"), float)]
        w(f"可计算的 m 样本数 : {len(valid)}")
        w(f"  r_base   最小/中位/最大 : {fmt(min(rb))} / {fmt(statistics.median(rb))} / {fmt(max(rb))}")
        w(f"  r_disagg 最小/中位/最大 : {fmt(min(rd))} / {fmt(statistics.median(rd))} / {fmt(max(rd))}")
        w(f"  m        最小/中位/最大 : {fmt(min(ms))} / {fmt(statistics.median(ms))} / {fmt(max(ms))}")
        w("")
        w("  分档计数（**仅描述，不是通过线**）:")
        for name in ("A（两臂都近乎并列）", "B（有明显倾向但仍接近）", "C（各自都有明确偏好）"):
            cnt = sum(1 for r in valid if band_of(r["m"]) == name)
            w(f"    {name} : {cnt} / {len(valid)}")
        w("")
        w("  逐条 m（便于复核）:")
        for r in valid:
            w(f"    配对{r['pair']} {r['rid']:<12} 第{r['idx']:>3}步  m={fmt(r['m'])}  → {band_of(r['m'])}")

    # ---------------- E2 汇总 ----------------
    w("")
    w("=" * 78)
    w("E2 汇总：翻过去需要多大余量（对比数值噪声地板）")
    w("=" * 78)
    if not delta_pool:
        w("噪声地板 Δ 没有样本（两臂同词的步上取不到共同 token）→ E2 无法计算。")
    else:
        dmed = statistics.median(delta_pool)
        dp90 = quantile(delta_pool, 0.90)
        w(f"噪声地板 Δ（两臂在**选了同一个词**的步上、top-N 交集的概率最大绝对差）:")
        w(f"  样本数 {len(delta_pool)}  中位={fmt(dmed)}  p90={fmt(dp90)}  max={fmt(max(delta_pool))}")
        w("  口径：只在两臂 top-N 的**交集**上算 —— 把「只在一侧出现」的词按 0 计会夸大这个数。")
        w("")
        if valid:
            w("  决策余量 M 与 Δ 的比（预注册读法：M/Δ 接近或小于 1 ⇒ 与数值噪声相容；≫1 ⇒ 不能这么解释）")
            for r in valid:
                mb, md = r.get("M_base"), r.get("M_disagg")
                w(f"    配对{r['pair']} {r['rid']:<12} M_base={fmt(mb)} M_disagg={fmt(md)}"
                  f"   M_base/Δ中位={fmt(mb/dmed if dmed else float('nan'), 2)}"
                  f"   M_disagg/Δ中位={fmt(md/dmed if dmed else float('nan'), 2)}"
                  f"   /Δp90={fmt(md/dp90 if dp90 else float('nan'), 2)}")

    w("")
    w("=" * 78)
    w("读这份输出时要记住的三件事")
    w("=" * 78)
    w("  1. **它不判通过/不通过**，也不改变 B1 的 4c 结论（仍是 6/15）。")
    w("  2. 分档（A/B/C）是**预注册里冻结的描述性分档**，不是放行线。")
    w("  3. 样本很小（5 条 × 3 遍），这只是**诊断**，不是统计推断。")

    text = "\n".join(L)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(text, encoding="utf-8", newline="\n")
    print(text)
    return 1 if (n_req == 0 or (n_div > 0 and not valid)) else 0


if __name__ == "__main__":
    raise SystemExit(main())

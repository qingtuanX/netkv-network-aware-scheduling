#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""4c 专项诊断用的比对脚本（对应 B1_4C_PREREG.md §6 与 §7）。

与 `b1_compare.py` 的关系：
  * `b1_compare.py` **保持原样不动** —— 它仍在支撑 B1 第二轮的证据复现。
  * 本脚本是**新增的**，只服务这一次诊断，多做了两件事：
      1. 在**首次分歧**处把两边的 `logprobs` 取出来，报出"两边各给竞争 token 多少概率"；
      2. 按预注册 §7 的分类规则，判成「近并列翻转」或「确定性路径差异」。
  * 判定口径（§6）与旧脚本一致：`token_ids` 逐位相同才算相同，
    **不允许拿"意思差不多"改判**。

退出码约定（**和旧脚本不同，这里要说清楚**）：
  * `0` = **比对完成**了（无论结论是"等价"还是"不等价"）。结论写在正文里。
  * `1` = 没比成（缺一侧数据 / HTTP 非 200 / 没有任何可比请求）。
  这样在运行台账里，FAIL 只表示"工具/数据出问题"，不会和"结论是不等价"混起来。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

# 预注册 §7 写死的阈值：两臂的 top1-top2 概率差**都**小于这个数，才算「近并列翻转」。
NEAR_TIE_GAP = 0.10


def setup_streams() -> None:
    """把 stdout/stderr 设成 UTF-8。

    为什么必须：报告里有 `↔` 这类字符，而 Windows 控制台默认是 GBK ⇒ `print` 会抛
    UnicodeEncodeError。**报告文件本身是好的**（`write_text` 显式写了 utf-8），
    崩的只是"往屏幕打印"这一步 —— 但崩了调用方就拿不到输出，看起来像"比对没做"。
    这正是仓库 AGENTS.md 要求"每个入口脚本顶部都要 reconfigure"要防的事。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def first_diff(a, b):
    """返回第一个不同处的下标；完全相同返回 -1；前缀相同但长度不同返回较短者长度。"""
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return i
    return -1 if len(a) == len(b) else n


def norm_top(entry) -> list[tuple[str, float]]:
    """把一格 `top_logprobs` 规整成 [(token 文本, 对数概率), ...]，按大小降序。

    vLLM 随版本有两种形状，都能吃到：
      * dict：{"token文本": logprob, ...}
      * list[dict]：[{"token": ..., "logprob": ...}, ...]
    取不到就返回空表 —— **不补猜、不填默认值**。
    """
    out: list[tuple[str, float]] = []
    if entry is None:
        return out
    if isinstance(entry, dict):
        for k, v in entry.items():
            try:
                out.append((str(k), float(v)))
            except (TypeError, ValueError):
                continue
    elif isinstance(entry, (list, tuple)):
        for it in entry:
            if isinstance(it, dict):
                tok = it.get("token", it.get("token_str"))
                lp = it.get("logprob")
                if tok is not None and lp is not None:
                    try:
                        out.append((str(tok), float(lp)))
                    except (TypeError, ValueError):
                        continue
    out.sort(key=lambda x: x[1], reverse=True)
    return out


def p(lp: float) -> float:
    """对数概率 → 概率。"""
    try:
        return float(math.exp(lp))
    except (OverflowError, ValueError):
        return float("nan")


def step_view(rec: dict, i: int) -> dict | None:
    """取某条请求在第 i 个 token 处的：实际选的 token、它的概率、以及 top-5。

    取不到（没请求 logprobs / 该位置不存在）就返回 None。
    """
    lps = rec.get("logprobs")
    if not isinstance(lps, dict):
        return None
    tokens = lps.get("tokens") or []
    tlogps = lps.get("token_logprobs") or []
    toplist = lps.get("top_logprobs") or []
    if i < 0 or i >= max(len(tokens), len(tlogps), len(toplist)):
        return None
    chose = tokens[i] if i < len(tokens) else None
    chose_lp = tlogps[i] if i < len(tlogps) else None
    top = norm_top(toplist[i] if i < len(toplist) else None)
    gap = None
    if len(top) >= 2:
        gap = p(top[0][1]) - p(top[1][1])
    elif len(top) == 1:
        gap = p(top[0][1]) - 0.0
    return {
        "index": i,
        "chosen": chose,
        "chosen_logprob": chose_lp,
        "chosen_p": (p(chose_lp) if isinstance(chose_lp, (int, float)) else None),
        "top": top[:5],
        "gap": gap,
    }


def fmt_top(top: list[tuple[str, float]]) -> str:
    if not top:
        return "（没有 top_logprobs）"
    return "  ".join(f"{t!r}={p(lp):.4f}" for t, lp in top)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="4c 专项诊断：逐 token + 首次分歧概率")
    ap.add_argument("--baseline", required=True, help="单机完整服务那条臂的 arm_*.json")
    ap.add_argument("--disagg", required=True, help="拆分式那条臂的 arm_*.json")
    ap.add_argument("--out", required=True, help="报告输出路径")
    ap.add_argument("--pair-label", default="", help="配对标签，写进报告头（例如 baseline4c_r2 ↔ disagg4c_r2）")
    args = ap.parse_args(argv)
    setup_streams()   # 必须最早调用：否则报告里的 `↔` 会让 print 在 GBK 控制台上崩掉

    base = load(Path(args.baseline))
    dis = load(Path(args.disagg))

    br = {r["id"]: r for r in base["requests"]}
    dr = {r["id"]: r for r in dis["requests"]}
    ids = sorted(set(br) | set(dr))

    lines: list[str] = []
    w = lines.append

    w("=" * 78)
    w("4c 专项诊断：拆分式输出 vs 单机完整服务输出（前缀缓存 = off）")
    if args.pair_label:
        w(f"配对     : {args.pair_label}")
    w("=" * 78)
    w(f"对照臂   : {base.get('target_url')}   /version={base.get('target_version_endpoint')}")
    w(f"拆分臂   : {dis.get('target_url')}    /version={dis.get('target_version_endpoint')}")
    w(f"模型     : {base.get('model')}   max_tokens={base.get('max_tokens')} "
      f"temperature={base.get('temperature')} seed={base.get('seed')}")
    w(f"logprobs : 请求档位 对照={base.get('logprobs_requested')} / 拆分={dis.get('logprobs_requested')}")
    w(f"请求数   : 对照 {len(br)} 条 / 拆分 {len(dr)} 条 / 并集 {len(ids)} 条")
    w("")

    have_ids = all(r.get("token_ids") for r in list(br.values()) + list(dr.values()))
    if have_ids:
        w("比对口径：**token_ids 逐位相同**（最硬的口径，不经任何文本后处理）")
    else:
        w("比对口径：有一端没返回 token_ids → **降级为 output_text 逐字符比对**（降级证据，必须写明）")
    w("-" * 78)
    w(f"{'请求 id':<16}{'http 对照/拆分':>16}{'prompt_tok':>22}"
      f"{'completion_tok':>22}{'逐 token':>11}{'首次分歧':>12}")
    w("-" * 78)

    n_same = 0
    n_diff = 0
    n_err = 0
    diffs: list[tuple[str, dict, dict, int]] = []  # (id, base_rec, dis_rec, first_diff_index)
    detail: list[str] = []

    for i in ids:
        b = br.get(i)
        d = dr.get(i)
        if b is None or d is None:
            w(f"{i:<16}{'缺一侧':>16}")
            n_err += 1
            detail.append(f"[{i}] 缺少一侧数据：baseline={b is not None} disagg={d is not None}")
            continue
        if b.get("http_status") != 200 or d.get("http_status") != 200:
            n_err += 1
            w(f"{i:<16}{(str(b.get('http_status')) + '/' + str(d.get('http_status'))):>16}"
              f"{'':>22}{'':>22}{'失败':>11}")
            detail.append(
                f"[{i}] HTTP 异常 baseline={b.get('http_status')} "
                f"({b.get('error_class')}: {(b.get('error_text') or '')[:200]}) / "
                f"disagg={d.get('http_status')} "
                f"({d.get('error_class')}: {(d.get('error_text') or '')[:200]})"
            )
            continue

        if have_ids:
            j = first_diff(b["token_ids"], d["token_ids"])
            same = j == -1
        else:
            same = b.get("output_text") == d.get("output_text")
            j = first_diff(b.get("output_text") or "", d.get("output_text") or "")

        n_same += int(same)
        n_diff += int(not same)
        pos = "-" if same else f"第 {j} 个"
        w(f"{i:<16}{(str(b.get('http_status')) + '/' + str(d.get('http_status'))):>16}"
          f"{(str(b.get('prompt_tokens')) + '/' + str(d.get('prompt_tokens'))):>22}"
          f"{(str(b.get('completion_tokens')) + '/' + str(d.get('completion_tokens'))):>22}"
          f"{('相同' if same else '不同'):>11}{pos:>12}")
        if not same:
            diffs.append((i, b, d, j))

    w("-" * 78)
    w("")
    total = len(ids)
    w(f"逐 token 相同的请求数 : {n_same} / {total}")
    w(f"不同的请求数           : {n_diff}")
    w(f"没能比成的请求数       : {n_err}（缺数据 / HTTP 非 200）")
    w("")

    if n_err > 0:
        verdict = "无法判定（有请求没比成）"
    elif n_diff == 0:
        verdict = "等价"
    else:
        verdict = "不等价"
    w(f"主判定（B1_4C_PREREG.md §6）：**{verdict}**")
    w("  口径：`token_ids` 逐位相同才算相同；**不把「意思差不多」改判为通过**；")
    w("        阈值与配对规则在采数之前就已冻结，不事后调整。")
    w("")

    # ------------------------------------------------------------------ §7 概率诊断
    w("=" * 78)
    w("首次分歧处的概率诊断（B1_4C_PREREG.md §7）")
    w("=" * 78)
    if not diffs:
        w("没有分歧 → 不需要做概率诊断。")
    else:
        any_lp = False
        for rid, b, d, j in diffs:
            w("")
            w(f"[{rid}] 首次分歧位置 = 第 {j} 个 token")
            if j < 0:
                w("  （位置 < 0 只可能是两侧长度不同且前缀一致：短的那侧到此为止）")
            for tag, rec in (("对照臂", b), ("拆分臂", d)):
                sv = step_view(rec, j) if j >= 0 else None
                if sv is None:
                    w(f"  {tag}: 该位置取不到 logprobs"
                      f"（未请求 logprobs 或该位置不存在）→ **如实写「未进 top-5 / 无数据」**")
                    continue
                any_lp = True
                cp = sv["chosen_p"]
                cp_txt = f"{cp:.4f}" if isinstance(cp, float) else "n/a"
                gap_txt = f"{sv['gap']:.4f}" if isinstance(sv["gap"], float) else "n/a"
                w(f"  {tag}: 实际选 {sv['chosen']!r}  p={cp_txt}   "
                  f"该步 top1-top2 差 gap={gap_txt}")
                w(f"      top5: {fmt_top(sv['top'])}")
        # 分类：只有两臂都有 gap 时才分类
        w("")
        w("-" * 78)
        w("分类（规则写死在 §7：**两臂的 gap 都 < 0.10 才算「近并列翻转」**）")
        for rid, b, d, j in diffs:
            sb = step_view(b, j) if j >= 0 else None
            sd = step_view(d, j) if j >= 0 else None
            gb = sb["gap"] if sb else None
            gd = sd["gap"] if sd else None
            if isinstance(gb, float) and isinstance(gd, float):
                if gb < NEAR_TIE_GAP and gd < NEAR_TIE_GAP:
                    cls = "近并列翻转"
                else:
                    cls = "确定性路径差异"
                w(f"  {rid}: gap_对照={gb:.4f} gap_拆分={gd:.4f}  →  **{cls}**")
            else:
                w(f"  {rid}: 两侧 gap 不齐（对照={gb} 拆分={gd}）→ **无法分类**，只报原始数据")
        if not any_lp:
            w("  （两侧都没拿到 logprobs ⇒ 本轮只能报「输出不同」，不能解释原因）")

    if detail:
        w("")
        w("-" * 78)
        w("逐条明细（原始，不做美化）:")
        for line in detail:
            w("  " + line)

    w("")
    w("读取时的注意事项：")
    w("  1. 本文件只判【输出是否一致】，**不包含任何性能判断**（吞吐/延迟/收益一律没有）。")
    w("  2. 前缀缓存是否真的关掉，看同次的 `cache_config_info_proof.txt`，**不看这里的命令行**。")
    w("  3. 样本很小（5 条 × 3 遍），**不是统计意义上的等价性证明**。")

    text = "\n".join(lines)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    # 退出码语义见文件头：0 = 比对完成，1 = 没比成
    return 1 if (n_err > 0 or total == 0) else 0


if __name__ == "__main__":
    raise SystemExit(main())

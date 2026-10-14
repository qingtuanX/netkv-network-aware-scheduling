#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""b1_prefix_4c.py —— 4c 收口的固定前缀下一词测试工具（B1_4C_PREREG_4.md §3/§4/§5 的落地）。

它做什么（teacher forcing，"固定前缀测试"）：
  1) `collect`：对一条提示词，先确定参考序列 t1..tL（**默认由对照臂自由生成**；
     拆分臂一律 `--reference-from` 指定对照臂产物目录，PREREG_4 §3.1）；
     然后对每个 k，把 **prompt 的 token ids + t1..tk** 喂给被测臂，只生成 1 个词、
     取 top-20 processed 分布。这样每一步两边看到完全相同的输入，
     检验"KV 传过去后，相同前缀下下一步判断是否一致"。
  2) `calib`：同机校准 Δ_cal（PREREG_4 §5）——同臂同前缀重复 N 次的分布差。
  3) `analyze`：把两臂的逐步分布做 A/B/C/D 判定（并列翻转口径，冻结在 PREREG_4 §4）。

口径（不许改，改 = 改预注册）：
  * 分数一律 processed 分，log 空间先 exp() 成普通概率（PREREG_4 §0）。
  * top_k=-1、temperature=0、top_p=1、repetition_penalty=1 全部**显式发送**，
    实发参数记进产物 `request_params_actual`（PREREG_4 §2.1）。
  * token 身份用 API 返回的 token 文本作键（与 b1_margin_4c.py 口径一致），
    被选 token 的 id 另记用于 token 对账。
  * Δ_gap 只用同词步估计；同词步（含 ≥1 候选对）少于 30 ⇒ 不可估计 ⇒ 不判通过。
  * 前缀默认 token-id 数组直传（`--prefix-mode tokenids`）；代理不透传时用
    `--prefix-mode text`（文本回拼 + **逐步 tokenize 对账**，对不上的步 `usable=false`
    弃用并报告，PREREG_4 §3.2）。

只用标准库；Windows 与 WSL 都能跑（采集在发起端跑，分析两边都行）。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# 冻结的请求侧生成参数（PREREG_4 §2）。**top_k 必须 -1 不能 1**：1 会把 processed 分布
# 裁成 one-hot，top-20 比较直接做不成（temperature=0 时 vLLM 内部还会重置 top_k，
# 但"实测非退化"才是证据，见 PREREG_4 §2.1）。
GEN_PARAMS = {
    "temperature": 0.0,
    "top_p": 1.0,
    "top_k": -1,
    "repetition_penalty": 1.0,
    "seed": 0,
    "stream": False,
    "return_token_ids": True,
}
LOGPROBS_TOP = 20          # PREREG_4 §2：logprobs=20
MIN_AGREEMENT_STEPS = 30   # PREREG_4 §4 样本量条件：同词步 < 30 ⇒ Δ_gap 不可估计
CALIB_POSITIONS = (1, 3, 8, 15, 30)   # PREREG_4 §5：校准回放位置（到 L 截止）


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def post_json(url: str, payload: dict, timeout: float = 600.0) -> dict:
    """发一个 POST，原样返回（不做加工）；网络层失败不抛，落进 error_* 字段。"""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body, code = resp.read(), resp.status
    except urllib.error.HTTPError as e:
        body, code = e.read(), e.code
    except Exception as e:
        return {
            "http_status": None,
            "elapsed_s": round(time.perf_counter() - t0, 4),
            "error_class": type(e).__name__,
            "error_text": str(e)[:2000],
            "body": b"",
        }
    return {
        "http_status": code,
        "elapsed_s": round(time.perf_counter() - t0, 4),
        "error_class": None,
        "error_text": None,
        "body": body,
    }


def get_json(url: str, timeout: float = 30.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return {"unavailable": f"{type(e).__name__}: {e}"}


# ------------------------------------------------------------------ 服务端小工具

def api_tokenize(base: str, model: str, text: str) -> list[int] | None:
    """/tokenize：文本 → token ids。取不到返回 None（不补猜）。"""
    r = post_json(f"{base}/tokenize", {"model": model, "prompt": text})
    try:
        j = json.loads(r["body"].decode("utf-8", "replace"))
    except Exception:
        return None
    toks = j.get("tokens", j.get("token_ids"))
    return [int(t) for t in toks] if isinstance(toks, list) else None


def api_detokenize(base: str, model: str, ids: list[int]) -> str | None:
    """/detokenize：token ids → 文本。取不到返回 None。"""
    r = post_json(f"{base}/detokenize", {"model": model, "tokens": ids})
    try:
        j = json.loads(r["body"].decode("utf-8", "replace"))
    except Exception:
        return None
    return j.get("prompt", j.get("text"))


def parse_top(resp_json: dict) -> dict | None:
    """从 /v1/completions 响应取一个位置的 top 分布 {token 文本: 普通概率}（exp 后）。

    口径与 b1_margin_4c.py 一致：logprobs.top_logprobs[i] 是 {token 文本: 对数概率}。
    """
    try:
        ch = (resp_json.get("choices") or [{}])[0]
        lp = ch.get("logprobs") or {}
        tops = lp.get("top_logprobs") or []
        if not tops or not isinstance(tops[0], dict):
            return None
        return {str(k): math.exp(float(v)) for k, v in tops[0].items() if v is not None}
    except Exception:
        return None


def parse_chosen(resp_json: dict) -> tuple[str | None, int | None]:
    """被选 token（文本, id）；max_tokens=1 时取第 0 位。"""
    try:
        ch = (resp_json.get("choices") or [{}])[0]
        lp = ch.get("logprobs") or {}
        texts = lp.get("tokens") or []
        ids = (ch.get("token_ids") or lp.get("token_ids") or [])
        return (str(texts[0]) if texts else None, int(ids[0]) if ids else None)
    except Exception:
        return None, None


def replay_once(base: str, tok_base: str, model: str, prompt_ids: list[int],
                ref_ids: list[int], k: int, mode: str) -> dict:
    """对前缀 prompt_ids + ref_ids[:k] 做一次 max_tokens=1 回放，返回结构化一步。

    tok_base：tokenize/detokenize 走的端点（拆分臂的代理没有 /tokenize，指到 vLLM 服务；
    同模型同 revision 的分词器逐位相同，跨端点对账成立）。
    """
    prefix_ids = prompt_ids + ref_ids[:k]
    usable = True
    note = None
    if mode == "tokenids":
        sent_prompt: str | list[int] = prefix_ids
    else:
        # 文本回拼：detokenize 整个前缀 → 文本作 prompt；**逐步 tokenize 对账**，
        # 对不上这一步弃用（PREREG_4 §3.2），不补猜。
        sent_prompt = api_detokenize(tok_base, model, prefix_ids) or ""
        rt = api_tokenize(tok_base, model, sent_prompt)
        if rt != prefix_ids:
            usable = False
            note = f"roundtrip_mismatch: tokenize(detokenize(prefix)) != prefix ({len(rt or [])} vs {len(prefix_ids)})"
    r = post_json(f"{base}/v1/completions",
                  {"model": model, "prompt": sent_prompt, "max_tokens": 1,
                   **GEN_PARAMS, "logprobs": LOGPROBS_TOP})
    try:
        rj = json.loads(r["body"].decode("utf-8", "replace"))
    except Exception:
        rj = {}
    top = parse_top(rj)
    chosen_text, chosen_id = parse_chosen(rj)
    usage = (rj.get("usage") or {})
    return {
        "k": k,
        "prefix_len_ids": len(prefix_ids),
        "usage_prompt_tokens": usage.get("prompt_tokens"),
        "prompt_tokens_match": usage.get("prompt_tokens") == len(prefix_ids),
        "usable": usable,
        "note": note,
        "top1_text": chosen_text,
        "top1_id": chosen_id,
        "top": top,
        "n_candidates": len(top) if top else 0,
        "http_status": r["http_status"],
        "elapsed_s": r["elapsed_s"],
    }


def load_reference(ref_dir: Path, pid: str) -> tuple[list[int], list[str]] | None:
    """参考序列来源（优先级）：collect 产物 base_<pid>.json → 校准产物 calib_refs_base.json。

    PREREG_4 §3.1：参考序列只有对照臂能生成；拆分臂一律从对照臂产物取。
    """
    p = ref_dir / f"base_{pid}.json"
    if p.exists():
        d = json.loads(p.read_text(encoding="utf-8"))
        return list(d.get("ref_ids") or []), list(d.get("ref_texts") or [])
    cr = ref_dir / "calib_refs_base.json"
    if cr.exists():
        d = json.loads(cr.read_text(encoding="utf-8"))
        if pid in d:
            ids = [int(x) for x in d[pid]]
            return ids, []
    return None


def gen_reference(base: str, model: str, prompt_ids: list[int], max_new: int
                  ) -> tuple[list[int], list[str], dict] | None:
    """自由生成参考序列（对照臂用）。返回 (ref_ids, ref_texts, usage)。"""
    r = post_json(f"{base}/v1/completions",
                  {"model": model, "prompt": prompt_ids, "max_tokens": max_new,
                   **GEN_PARAMS, "logprobs": LOGPROBS_TOP})
    try:
        j = json.loads(r["body"].decode("utf-8", "replace"))
    except Exception:
        return None
    ch = (j.get("choices") or [{}])[0]
    ref_ids = ch.get("token_ids") or []
    ref_texts = [str(t) for t in ((ch.get("logprobs") or {}).get("tokens") or [])]
    if not ref_ids:
        return None
    return list(ref_ids), ref_texts, (j.get("usage") or {})


# ------------------------------------------------------------------ collect

def cmd_collect(args: argparse.Namespace) -> int:
    base = args.url.rstrip("/")
    tok_base = (args.tokenize_url or args.url).rstrip("/")
    model = args.model
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    prompts = json.loads(Path(args.prompts_file).read_text(encoding="utf-8"))
    if args.only_ids:
        keep = set(args.only_ids)
        prompts = [p for p in prompts if p["id"] in keep]
    if not prompts:
        print("!! 过滤后没有提示词"); return 2
    ref_dir = Path(args.reference_from) if args.reference_from else None

    version = get_json(f"{base}/version")
    print(f"target={base} arm={args.arm} mode={args.prefix_mode} /version={version}")

    request_params_actual = dict(GEN_PARAMS)
    request_params_actual["logprobs"] = LOGPROBS_TOP
    summary = {
        "tool": "b1_prefix_4c.py collect",
        "utc_started": utcnow(),
        "arm": args.arm,
        "target_url": base,
        "target_version_endpoint": version,
        "model": model,
        "prefix_mode": args.prefix_mode,
        "max_new": args.max_new,
        "reference_from": str(ref_dir) if ref_dir else "(self-generated)",
        "request_params_actual": request_params_actual,
        "prompts": [],
    }

    for p in prompts:
        pid = p["id"]
        print(f"== [{args.arm}] {pid}")
        prompt_ids = api_tokenize(tok_base, model, p["prompt"])
        if not prompt_ids:
            print(f"!! {pid}: /tokenize 失败 → 该条中止（不补猜）")
            summary["prompts"].append({"id": pid, "error": "tokenize_failed"})
            continue

        # 参考序列：--reference-from（对照臂产物）或本臂自由生成（仅对照臂允许）
        if ref_dir is not None:
            got = load_reference(ref_dir, pid)
            if got is None:
                print(f"!! {pid}: {ref_dir} 里没有 base_{pid}.json → 中止该条")
                summary["prompts"].append({"id": pid, "error": "reference_missing"})
                continue
            ref_ids, ref_texts, ref_usage = got[0], got[1], {}
        else:
            g = gen_reference(base, model, prompt_ids, args.max_new)
            if g is None:
                print(f"!! {pid}: 参考生成失败 → 中止该条")
                summary["prompts"].append({"id": pid, "error": "reference_failed"})
                continue
            ref_ids, ref_texts, ref_usage = g
        print(f"   prompt_ids={len(prompt_ids)} ref_len={len(ref_ids)}")

        # E3 自检：同一前缀同臂连跑 2 次，要求逐位相同（PREREG_4 §3.4）
        probe_k = min(3, len(ref_ids))
        e3_t = [replay_once(base, tok_base, model, prompt_ids, ref_ids, probe_k, args.prefix_mode)
                for _ in range(2)]
        e3_identical = (e3_t[0].get("top") is not None
                        and e3_t[0]["top"] == e3_t[1].get("top"))
        print(f"   E3 self-check k={probe_k}: identical={e3_identical}")
        if not e3_identical:
            print(f"!! {pid}: E3 不过 → 按 PREREG_4 §8 先修测量；本条仍记录但不解读")

        steps = []
        for k in range(1, len(ref_ids) + 1):
            s = replay_once(base, tok_base, model, prompt_ids, ref_ids, k, args.prefix_mode)
            s["ref_next_text"] = str(ref_texts[k]) if k < len(ref_texts) else None
            s["ref_next_id"] = int(ref_ids[k]) if k < len(ref_ids) else None
            s["ref_consistent"] = ((s["top1_text"] == str(ref_texts[k]))
                                   if k < len(ref_texts) else None)
            steps.append(s)
            if k % 16 == 0:
                print(f"   step {k}/{len(ref_ids)}")

        rec = {
            "id": pid,
            "n_prompt_ids": len(prompt_ids),
            "ref_ids": ref_ids,
            "ref_texts": ref_texts,
            "reference_usage": ref_usage,
            "e3": {"probe_k": probe_k, "identical": e3_identical},
            "steps": steps,
        }
        out_path = out_dir / f"{args.arm}_{pid}.json"
        out_path.write_text(json.dumps(rec, ensure_ascii=False, indent=1),
                            encoding="utf-8", newline="\n")
        n_match = sum(1 for s in steps if s["prompt_tokens_match"])
        n_refok = sum(1 for s in steps if s["ref_consistent"])
        n_nd = sum(1 for s in steps if (s["n_candidates"] or 0) >= 2)
        n_unusable = sum(1 for s in steps if not s["usable"])
        print(f"   wrote {out_path.name}: steps={len(steps)} "
              f"prompt_tokens对账={n_match}/{len(steps)} 参考臂一致={n_refok}/{len(steps)} "
              f"非退化步={n_nd}/{len(steps)} 弃用步={n_unusable}")
        summary["prompts"].append({
            "id": pid, "n_prompt_ids": len(prompt_ids), "ref_len": len(ref_ids),
            "e3_identical": e3_identical,
            "prompt_tokens_match": f"{n_match}/{len(steps)}",
            "ref_consistent": f"{n_refok}/{len(steps)}",
            "non_degenerate": f"{n_nd}/{len(steps)}",
            "unusable_steps": n_unusable,
        })

    summary["utc_written"] = utcnow()
    (out_dir / f"collect_{args.arm}_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    print(f"wrote collect_{args.arm}_summary.json")
    return 0


# ------------------------------------------------------------------ calib

def cmd_calib(args: argparse.Namespace) -> int:
    """同机校准 Δ_cal（PREREG_4 §5）：同臂同前缀重复 --reps 次的分布差。

    位置网格 = CALIB_POSITIONS（到 L 截止）；校准题不进判定。
    附带完成 §2.1 的 top-20 非退化自检（顺手就有数据）。
    """
    base = args.url.rstrip("/")
    tok_base = (args.tokenize_url or args.url).rstrip("/")
    model = args.model
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    prompts = json.loads(Path(args.prompts_file).read_text(encoding="utf-8"))
    if args.only_ids:
        keep = set(args.only_ids)
        prompts = [p for p in prompts if p["id"] in keep]
    ref_dir = Path(args.reference_from) if args.reference_from else None
    positions = [int(x) for x in args.positions.split(",")]
    refs: dict[str, list[int]] = {}   # 参考序列存档（calib_refs_<arm>.json），供另一臂复用

    request_params_actual = dict(GEN_PARAMS)
    request_params_actual["logprobs"] = LOGPROBS_TOP
    result = {
        "tool": "b1_prefix_4c.py calib",
        "utc_started": utcnow(),
        "arm": args.arm,
        "target_url": base,
        "model": model,
        "positions": positions,
        "reps": args.reps,
        "prefix_mode": args.prefix_mode,
        "request_params_actual": request_params_actual,
        "per_prompt": [],
        "pool_diffs": [],
    }

    for p in prompts:
        pid = p["id"]
        prompt_ids = api_tokenize(tok_base, model, p["prompt"])
        if not prompt_ids:
            result["per_prompt"].append({"id": pid, "error": "tokenize_failed"})
            continue
        if ref_dir is not None:
            got = load_reference(ref_dir, pid)
            if got is None:
                result["per_prompt"].append({"id": pid, "error": "reference_missing"})
                continue
            ref_ids = got[0]
        else:
            g = gen_reference(base, model, prompt_ids, args.max_new)
            if g is None:
                result["per_prompt"].append({"id": pid, "error": "reference_failed"})
                continue
            ref_ids = g[0]
        refs[pid] = list(ref_ids)

        entry = {"id": pid, "ref_len": len(ref_ids), "positions": []}
        for k in positions:
            if k > len(ref_ids):
                entry["positions"].append({"k": k, "skipped": "beyond_ref_len"})
                continue
            tops = []
            n_nd = 0
            for _ in range(args.reps):
                s = replay_once(base, tok_base, model, prompt_ids, ref_ids, k, args.prefix_mode)
                tops.append(s.get("top") or {})
                if (s.get("n_candidates") or 0) >= 2:
                    n_nd += 1
            diffs = []
            for i in range(len(tops)):
                for j in range(i + 1, len(tops)):
                    inter = set(tops[i]) & set(tops[j])
                    d = max((abs(tops[i][t] - tops[j][t]) for t in inter), default=0.0)
                    diffs.append(d)
            result["pool_diffs"].extend(diffs)
            entry["positions"].append({
                "k": k, "n_reps": len(tops), "n_pairs": len(diffs),
                "median": sorted(diffs)[len(diffs) // 2] if diffs else None,
                "max": max(diffs) if diffs else None,
                "non_degenerate_reps": n_nd,
            })
            print(f"[{args.arm}] {pid} k={k}: pairs={len(diffs)} max_diff={max(diffs) if diffs else None}")
        result["per_prompt"].append(entry)

    pool = result["pool_diffs"]
    srt = sorted(pool)
    result["delta_cal"] = {
        "n_pairs": len(pool),
        "median": srt[len(srt) // 2] if srt else None,
        "p95_reference_upper": srt[max(0, math.ceil(0.95 * len(srt)) - 1)] if srt else None,
        "max": srt[-1] if srt else None,
    }
    result["utc_written"] = utcnow()
    (out_dir / f"calib_{args.arm}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
    # 参考序列存档：拆分臂校准时按 PREREG_4 §5 复用对照臂的参考序列（calib_refs_base.json）。
    (out_dir / f"calib_refs_{args.arm}.json").write_text(
        json.dumps(refs, ensure_ascii=False), encoding="utf-8", newline="\n")
    print(f"wrote calib_{args.arm}.json  Δ_cal={result['delta_cal']}")
    return 0


# ------------------------------------------------------------------ analyze

def nearest_rank_p95(vals: list[float]) -> float:
    """最近秩 p95（"95% 参考上界"的定义；不用插值——插值会让上界落在两个观测之间）。"""
    if not vals:
        raise ValueError("empty")
    s = sorted(vals)
    return s[max(0, math.ceil(0.95 * len(s)) - 1)]


def cmd_analyze(args: argparse.Namespace) -> int:
    in_dir, out_dir = Path(args.in_dir), Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    gap_pool: list[float] = []
    n_unusable = 0

    pids = sorted({f.stem.split("_", 1)[1] for f in in_dir.glob("base_*.json")})
    for pid in pids:
        fb, fs = in_dir / f"base_{pid}.json", in_dir / f"split_{pid}.json"
        if not fs.exists():
            continue
        rb = json.loads(fb.read_text(encoding="utf-8"))
        rs = json.loads(fs.read_text(encoding="utf-8"))
        sb = {s["k"]: s for s in rb["steps"]}
        ss = {s["k"]: s for s in rs["steps"]}
        for k in sorted(set(sb) & set(ss)):
            b, s = sb[k], ss[k]
            if not (b.get("usable", True) and s.get("usable", True)):
                n_unusable += 1
                rows.append({"id": pid, "k": k, "verdict": "X",
                             "reason": "回拼对账失败步（弃用，PREREG_4 §3.2）"})
                continue
            tb, ts = b.get("top") or {}, s.get("top") or {}
            top1_b, top1_s = b.get("top1_text"), s.get("top1_text")
            inter = sorted(set(tb) & set(ts))
            row = {"id": pid, "k": k,
                   "top1_base": top1_b, "top1_split": top1_s,
                   "agree": top1_b == top1_s,
                   "n_inter": len(inter),
                   "prompt_tokens_ok": bool(b.get("prompt_tokens_match") and s.get("prompt_tokens_match"))}
            if top1_b == top1_s:
                gmax = 0.0
                for i in range(len(inter)):
                    for j in range(i + 1, len(inter)):
                        x, y = inter[i], inter[j]
                        g = abs((tb[x] - tb[y]) - (ts[x] - ts[y]))
                        gap_pool.append(g)
                        gmax = max(gmax, g)
                row["step_gap_max"] = round(gmax, 8)
                row["verdict"] = "A"
            else:
                a, c = top1_b, top1_s          # a=对照臂选择，b=拆分臂选择（PREREG_4 §4）
                both_known = (a in tb and a in ts and c in tb and c in ts)
                if not both_known:
                    row.update({"verdict": "D", "G": None,
                                "reason": "竞争 token 不全在两臂 top-20"})
                else:
                    g = min(abs(tb[a] - tb[c]), abs(ts[a] - ts[c]))
                    row["G"] = round(g, 8)
                    row["verdict"] = "B_or_C"   # 需 Δ_gap 才能定，末尾统一判
            rows.append(row)

    n_agreement_steps = sum(1 for r in rows if r["verdict"] == "A" and r["n_inter"] >= 2)
    estimable = n_agreement_steps >= MIN_AGREEMENT_STEPS
    if gap_pool:
        srt = sorted(gap_pool)
        dg_med, dg_p95, dg_max = srt[len(srt) // 2], nearest_rank_p95(gap_pool), srt[-1]
    else:
        dg_p95 = dg_max = dg_med = None

    n_b = n_c = n_d = 0
    sens_flips = 0
    for r in rows:
        if r["verdict"] in ("A", "X"):
            continue
        if r["verdict"] == "D":
            n_d += 1
            continue
        if not estimable:
            r["verdict"] = "D"
            r["reason"] = "Δ_gap 不可估计"
            n_d += 1
            continue
        g = r["G"]
        v95 = "B" if g < dg_p95 else "C"
        vmax = "B" if g < dg_max else "C"
        r["verdict"] = v95
        r["verdict_max"] = vmax
        if v95 != vmax:
            sens_flips += 1
            r["sensitive"] = True
        if v95 == "B":
            n_b += 1
        else:
            n_c += 1

    summary = {
        "tool": "b1_prefix_4c.py analyze",
        "utc_written": utcnow(),
        "n_steps_total": len(rows),
        "n_unusable_steps": n_unusable,
        "n_agreement_steps_with_pairs": n_agreement_steps,
        "n_agreement_pairs_pooled": len(gap_pool),
        "delta_gap": {"median": dg_med, "p95_reference_upper": dg_p95, "max": dg_max,
                      "definition": "同词步 top-20 交集全部候选对的 |(pb(x)-pb(y))-(ps(x)-ps(y))|；"
                                    "p95=最近秩（ceil(0.95n)-1）"},
        "estimable": estimable,
        "min_agreement_steps_required": MIN_AGREEMENT_STEPS,
        "verdict_counts": {"A": sum(1 for r in rows if r["verdict"] == "A"),
                           "B": n_b, "C": n_c, "D": n_d,
                           "X_unusable": n_unusable},
        "sensitivity_flips_p95_vs_max": sens_flips,
        "sensitivity_rule": "p95 与 max 判定不一致 ⇒ 证据不稳健 ⇒ 不放行 B2（不是'真错误'）",
    }

    import csv
    cols = ["id", "k", "top1_base", "top1_split", "agree", "n_inter", "prompt_tokens_ok",
            "G", "step_gap_max", "verdict", "verdict_max", "sensitive", "reason"]
    with (out_dir / "prefix_steps.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    (out_dir / "analyze_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"wrote {out_dir / 'prefix_steps.csv'}")
    return 0


# ------------------------------------------------------------------ 入口

def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="4c 收口的固定前缀下一词测试（PREREG_4 §3/§4/§5）")
    sub = ap.add_subparsers(dest="mode", required=True)

    def common(cc) -> None:
        cc.add_argument("--url", required=True)
        cc.add_argument("--tokenize-url", default=None,
                        help="tokenize/detokenize 走哪个端点（默认同 --url）。拆分臂的代理可能不实现 "
                             "/tokenize ⇒ 指到任一 vLLM 服务（如前处理端）；同模型同 revision，"
                             "分词器逐位相同，跨端点对账仍然成立")
        cc.add_argument("--arm", required=True,
                        choices=["base", "split", "base_r2", "split_r2", "base_i1", "split_i1",
                                 "base_i2", "split_i2"])
        cc.add_argument("--model", default="Qwen/Qwen2.5-1.5B-Instruct")
        cc.add_argument("--prompts-file", required=True)
        cc.add_argument("--only-ids", nargs="*", default=None)
        cc.add_argument("--out-dir", required=True)
        cc.add_argument("--prefix-mode", choices=["tokenids", "text"], default="tokenids",
                        help="tokenids=前缀 token id 数组直传（默认）；text=文本回拼+逐步对账"
                             "（代理不透传 id 数组时的退化路径）")
        cc.add_argument("--reference-from", default=None,
                        help="对照臂 collect 产物目录；**拆分臂必填**（参考序列只有对照臂能生成）")

    c = sub.add_parser("collect", help="采集：参考生成 + E3 + 逐步回放")
    common(c)
    c.add_argument("--max-new", type=int, default=64)
    c.set_defaults(func=cmd_collect)

    ca = sub.add_parser("calib", help="同机校准 Δ_cal（PREREG_4 §5）")
    common(ca)
    ca.add_argument("--max-new", type=int, default=40)
    ca.add_argument("--positions", default=",".join(str(x) for x in CALIB_POSITIONS))
    ca.add_argument("--reps", type=int, default=10)
    ca.set_defaults(func=cmd_calib)

    a = sub.add_parser("analyze", help="分析：Δ_gap + A/B/C/D 判定 + 稳健性重判")
    a.add_argument("--in-dir", required=True)
    a.add_argument("--out-dir", required=True)
    a.set_defaults(func=cmd_analyze)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

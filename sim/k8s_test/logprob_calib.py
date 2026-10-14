#!/usr/bin/env python3
"""概率口径校准的采数与判定脚本（LOGPROBS_CALIB_PREREG.md 的执行工具）。

只用标准库；跑在 WSL 的系统 python3 上即可（服务也在 WSL 里）。
两个子命令：
  probe   对一个 vLLM 实例发冻结的 5 条提示词（预热 + N 遍），逐请求原样落盘
  analyze 对 raw / processed 两臂的落盘文件做 C1/C2/C3 判定（判据见预注册 §4）

纪律（照抄项目惯例）：原始响应只增不删、失败不重跑顶替、判定只按预注册里冻结的口径。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ---------------------------------------------------------------- probe 阶段


def post_completion(base: str, payload: dict, timeout: float = 300.0) -> dict:
    """发一条 /v1/completions，返回原始响应（不加工）。"""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/v1/completions", data=data,
        headers={"Content-Type": "application/json"}, method="POST")
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body, code = resp.read(), resp.status
    except urllib.error.HTTPError as e:
        body, code = e.read(), e.code
    except Exception as e:  # 超时/拒连等
        body, code = json.dumps({"error": str(e)}).encode("utf-8"), None
    return {"utc_sent": utcnow(), "elapsed_s": round(time.perf_counter() - t0, 3),
            "http_status": code, "body": json.loads(body.decode("utf-8", "replace"))}


def cmd_probe(args) -> int:
    prompts = json.loads(Path(args.prompts).read_text(encoding="utf-8"))
    assert len(prompts) == 5, f"冻结输入应为 5 条，实际 {len(prompts)}"
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rec_path = out / "record.csv"
    new_rec = not rec_path.exists()
    with rec_path.open("a", encoding="utf-8", newline="") as rf:
        w = csv.writer(rf)
        if new_rec:
            w.writerow(["utc_start", "utc_end", "arm", "step", "prompt_id",
                        "rep", "http_status", "elapsed_s", "note"])
        rounds = [("warmup", r) for r in range(1, args.warmup + 1)] + \
                 [("formal", r) for r in range(1, args.reps + 1)]
        for kind, rep in rounds:
            out_path = out / f"arm_{args.arm}_{kind}{'r' + str(rep) if kind == 'formal' else str(rep)}.jsonl"
            with out_path.open("a", encoding="utf-8") as jf:
                for p in prompts:
                    # 请求参数逐字对齐 B1（b1_client.py），只多不了任何东西
                    payload = {
                        "model": args.model, "prompt": p["prompt"],
                        "max_tokens": 64, "temperature": 0.0, "seed": 0,
                        "stream": False, "return_token_ids": True, "logprobs": 20,
                    }
                    r = post_completion(args.url, payload)
                    jf.write(json.dumps({
                        "id": p["id"], "kind": kind, "rep": rep,
                        "utc_sent": r["utc_sent"], "elapsed_s": r["elapsed_s"],
                        "http_status": r["http_status"], "response": r["body"],
                    }, ensure_ascii=False) + "\n")
                    w.writerow([r["utc_sent"], utcnow(), args.arm, kind, p["id"],
                                rep, r["http_status"], r["elapsed_s"], ""])
                    print(f"[{args.arm}] {kind} r{rep} {p['id']}: "
                          f"http={r['http_status']} {r['elapsed_s']}s", flush=True)
    return 0


# --------------------------------------------------------------- analyze 阶段


def steps_of(rec: dict):
    """一条记录 → [(步号, 吐出的token文本, top分布dict)]；结构异常返回 []。"""
    ch = (rec.get("response", {}).get("choices") or [{}])[0]
    lp = ch.get("logprobs") or {}
    tokens = lp.get("tokens") or []
    tops = lp.get("top_logprobs") or []
    if not tokens or len(tops) != len(tokens):
        return []
    return list(enumerate(zip(tokens, tops)))


def judge_arm(files: list[Path], arm: str) -> dict:
    """对一条臂的所有正式记录做逐步判定（预注册 §4）。"""
    reps = {}   # rep -> [token_ids 序列]
    steps = []  # 逐步明细
    for f in sorted(files):
        for line in f.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            if rec.get("kind") != "formal":
                continue
            ch = (rec.get("response", {}).get("choices") or [{}])[0]
            reps[rec["rep"]] = ch.get("token_ids") or []
            for j, (tok, top) in steps_of(rec):
                if not isinstance(top, dict) or not top:
                    steps.append({"rep": rec["rep"], "id": rec["id"], "j": j,
                                  "token": tok, "verdict": "no_top"})
                    continue
                best = max(top, key=top.get)
                if tok == best:
                    v = "top1"
                elif tok in top:
                    v = "in_window_not_top1"
                else:
                    v = "not_in_window"
                ordered = sorted(top.items(), key=lambda kv: -kv[1])
                steps.append({
                    "rep": rec["rep"], "id": rec["id"], "j": j, "token": tok,
                    "verdict": v, "top1": ordered[0][0], "p_top1": ordered[0][1],
                    "p_top2": ordered[1][1] if len(ordered) > 1 else 0.0,
                    "p_token": top.get(tok),
                })
    n = len(steps)
    bad = [s for s in steps if s["verdict"] != "top1"]
    # C3：同臂各遍的 token_ids 逐位比较
    ids = list(reps.values())
    c3 = all(x == ids[0] for x in ids[1:]) if len(ids) > 1 else False
    # 形态（描述性）：接口返回的是 log probability，不是普通概率。
    # 保留 logprob 统计，同时把概率差单独换算出来，避免把 1.0 的 logprob 差
    # 错写成 "one-hot"。主判定只依赖排序，不受此描述性修正影响。
    lp1 = [s["p_top1"] for s in steps if s.get("p_top1") is not None]
    lp_gaps = [s["p_top1"] - s["p_top2"] for s in steps
               if s.get("p_top1") is not None and s.get("p_top2") is not None]
    prob1 = [math.exp(x) for x in lp1]
    prob_gaps = [math.exp(s["p_top1"]) - math.exp(s["p_top2"])
                 for s in steps
                 if s.get("p_top1") is not None and s.get("p_top2") is not None]
    def q(xs):
        return {"min": round(min(xs), 4), "med": round(statistics.median(xs), 4),
                "max": round(max(xs), 4)} if xs else None
    return {
        "arm": arm, "n_steps": n, "n_bad": len(bad),
        "bad_rate": round(len(bad) / n, 4) if n else None,
        "bad_kinds": {k: sum(1 for s in bad if s["verdict"] == k)
                      for k in {s["verdict"] for s in bad}},
        "c3_repeat_identical": c3, "reps_seen": sorted(reps),
        # Legacy keys are retained for compatibility, but now contain the
        # correctly converted ordinary probabilities.
        "shape_p_top1": q(prob1),
        "shape_gap_top1_top2": q(prob_gaps),
        "shape_logprob_top1": q(lp1),
        "shape_logprob_gap_top1_top2": q(lp_gaps),
        "n_onehot_like": sum(1 for g in prob_gaps if g is not None and g > 0.999),
        "bad_steps": bad[:50],
    }


def cmd_analyze(args) -> int:
    out = Path(args.out_dir)
    res = {}
    for arm, pat in (("raw", args.raw_glob), ("processed", args.proc_glob)):
        files = sorted(Path(args.run_dir).glob(pat))
        if not files:
            print(f"!! {arm}: 没有匹配 {pat} 的文件")
            res[arm] = None
            continue
        res[arm] = judge_arm(files, arm)
        r = res[arm]
        print(f"[{arm}] 步数={r['n_steps']} 非top1={r['n_bad']} "
              f"({r['bad_rate']}) 分类={r['bad_kinds']} 重复逐位相同={r['c3_repeat_identical']}")
        print(f"       top1概率 {r['shape_p_top1']}  top1-top2概率差 {r['shape_gap_top1_top2']} "
              f"（对应logprob差 {r['shape_logprob_gap_top1_top2']}） "
              f"疑似one-hot步={r['n_onehot_like']}")
    # C1/C2 判定（判据见预注册 §4，冻结）
    raw, proc = res.get("raw"), res.get("processed")
    verdict = {}
    if proc and raw:
        c1_pass = proc["c3_repeat_identical"] and proc["n_bad"] == 0
        verdict["C1_processed_all_top1"] = c1_pass
        verdict["C2_raw_contradiction_rate"] = raw["bad_rate"]
        if c1_pass and raw["bad_rate"] > 0:
            verdict["conclusion"] = "口径解释成立：此前矛盾是 raw/processed 口径差"
        elif c1_pass and raw["bad_rate"] == 0:
            verdict["conclusion"] = "raw 臂本地未复现旧现象，不能用本地校准解释云端现象"
        else:
            verdict["conclusion"] = "C1 未过：processed 分也不是选词所用的分，口径解释在本地不成立"
    else:
        verdict["conclusion"] = "数据不全，不下判定"
    print("\n=== 判定（预注册 §4 冻结口径）===")
    for k, v in verdict.items():
        print(f"  {k}: {v}")
    (out / "verdict.json").write_text(
        json.dumps({"verdict": verdict, "raw": res.get("raw"),
                    "processed": res.get("processed")},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    with (out / "verdict.txt").open("w", encoding="utf-8") as f:
        f.write(f"utc={utcnow()}\n")
        for k, v in verdict.items():
            f.write(f"{k} = {v}\n")
        for arm in ("raw", "processed"):
            f.write(f"\n[{arm}]\n{json.dumps(res.get(arm), ensure_ascii=False, indent=2)}\n")
    print(f"\n落盘：{out / 'verdict.json'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("probe")
    p.add_argument("--url", default="http://127.0.0.1:8090")
    p.add_argument("--model", default="Qwen/Qwen2.5-1.5B-Instruct")
    p.add_argument("--arm", required=True, choices=["raw", "processed"])
    p.add_argument("--prompts", required=True)
    p.add_argument("--reps", type=int, default=3)
    p.add_argument("--warmup", type=int, default=1)
    p.add_argument("--out-dir", required=True)
    p.set_defaults(fn=cmd_probe)
    a = sub.add_parser("analyze")
    a.add_argument("--run-dir", required=True)
    a.add_argument("--raw-glob", default="arm_raw_*.jsonl")
    a.add_argument("--proc-glob", default="arm_processed_*.jsonl")
    a.add_argument("--out-dir", required=True)
    a.set_defaults(fn=cmd_analyze)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())

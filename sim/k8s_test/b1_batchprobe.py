#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""b1_batchprobe.py —— 批不变性开关的"真的生效吗"探针（B1_4C_PREREG_4.md §6 的 I1 验证协议）。

为什么不能只看环境变量存在：`VLLM_BATCH_INVARIANT=1` 在场 ≠ 计算真的不再随批排布变化。
本探针把验证变成对比实验：
  1) 同一提示词**单独**跑若干次，取逐 token 的 top-20 processed 分布；
  2) 同一提示词**混进一批并发请求**里跑同样的次数；
  3) 比较两种情况的输出 token 与逐位置分布差（top-20 交集内最大概率绝对差）；
  4) 开关关掉作对照重做一遍。

判读（PREREG_4 §6，不许越界）：
  * 关（无开关）时有批敏感、开（有开关）后消失 ⇒ 开关**生效**（I1 可用作因果证据）；
  * 两边都无差异 ⇒ 探针**无法确认生效** ⇒ I1 记"未验证"，不得当因果证据；
  * 开关导致启动失败/报错 ⇒ I1 记"未执行"。
只用标准库；采集在 WSL 里跑。
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

GEN_PARAMS = {
    "temperature": 0.0,
    "top_p": 1.0,
    "top_k": -1,
    "repetition_penalty": 1.0,
    "seed": 0,
    "stream": False,
    "return_token_ids": True,
}


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def complete(url: str, model: str, prompt: str, max_new: int, timeout: float = 600.0) -> dict:
    payload = {"model": model, "prompt": prompt,
               "max_tokens": max_new, **GEN_PARAMS, "logprobs": 20}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body, code = resp.read(), resp.status
    except urllib.error.HTTPError as e:
        body, code = e.read(), e.code
    except Exception as e:
        return {"http_status": None, "error": f"{type(e).__name__}: {e}"}
    out = {"http_status": code, "elapsed_s": round(time.perf_counter() - t0, 4)}
    try:
        j = json.loads(body.decode("utf-8", "replace"))
        ch = (j.get("choices") or [{}])[0]
        lp = ch.get("logprobs") or {}
        tops = lp.get("top_logprobs") or []
        out["token_ids"] = ch.get("token_ids") or []
        out["text"] = ch.get("text")
        # 逐位置 {token 文本: 普通概率}（exp 后），口径与主工具一致
        out["tops"] = [
            {str(k): math_exp(v) for k, v in (t or {}).items() if v is not None}
            for t in tops
        ]
    except Exception as e:
        out["parse_error"] = f"{type(e).__name__}: {e}"
    return out


def math_exp(v) -> float:
    import math
    return math.exp(float(v))


def diff_runs(a: dict, b: dict) -> dict:
    """两次运行的差异：token 序列是否一致 + 逐位置 top-20 交集内最大概率绝对差。"""
    res = {"tokens_equal": a.get("token_ids") == b.get("token_ids"),
           "first_diff_step": None, "max_prob_diff": 0.0, "per_step_max_diff": []}
    ta, tb = a.get("tops") or [], b.get("tops") or []
    for i in range(min(len(ta), len(tb))):
        inter = set(ta[i]) & set(tb[i])
        d = max((abs(ta[i][t] - tb[i][t]) for t in inter), default=0.0)
        res["per_step_max_diff"].append(round(d, 8))
        res["max_prob_diff"] = max(res["max_prob_diff"], d)
        if res["first_diff_step"] is None and d > 0:
            res["first_diff_step"] = i + 1
    res["max_prob_diff"] = round(res["max_prob_diff"], 8)
    return res


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="批不变性开关验证探针（PREREG_4 §6 I1）")
    ap.add_argument("--url", required=True)
    ap.add_argument("--model", default="Qwen/Qwen2.5-1.5B-Instruct")
    ap.add_argument("--prompts-file", required=True, help="cal10.json 之类")
    ap.add_argument("--probe-id", required=True)
    ap.add_argument("--filler-ids", nargs="*", default=None, help="混批伴跑的提示词 id")
    ap.add_argument("--fillers", type=int, default=3, help="并发伴跑请求数（默认 3）")
    ap.add_argument("--max-new", type=int, default=32)
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--label", default="batchinv_off", help="产物标签：batchinv_off / batchinv_on")
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args(argv)

    prompts = {p["id"]: p["prompt"]
               for p in json.loads(Path(args.prompts_file).read_text(encoding="utf-8"))}
    probe = prompts[args.probe_id]
    filler_ids = args.filler_ids or [i for i in prompts if i != args.probe_id][:args.fillers]
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    result = {
        "tool": "b1_batchprobe.py",
        "label": args.label,
        "utc_started": utcnow(),
        "target_url": args.url.rstrip("/"),
        "model": args.model,
        "probe_id": args.probe_id,
        "filler_ids": filler_ids,
        "max_new": args.max_new,
        "reps": args.reps,
        "request_params_actual": {**GEN_PARAMS, "logprobs": 20},
        "alone": [], "batched": [], "comparisons": [],
    }
    endpoint = f"{args.url.rstrip('/')}/v1/completions"

    # 阶段 A：单独跑
    for r in range(args.reps):
        run = complete(endpoint, args.model, probe, args.max_new)
        result["alone"].append(run)
        print(f"[{args.label}] alone rep{r+1}: tokens={len(run.get('token_ids') or [])} "
              f"http={run.get('http_status')} {run.get('elapsed_s')}s")

    # 阶段 B：混批跑（线程同时发出；伴跑用长生成把探针裹进同一个批里）
    for r in range(args.reps):
        holder: dict[str, dict] = {}
        threads = []

        def fire(name: str, text: str, max_new: int) -> None:
            holder[name] = complete(endpoint, args.model, text, max_new)

        t_probe = threading.Thread(target=fire, args=("probe", probe, args.max_new))
        threads.append(t_probe)
        for fi, fid in enumerate(filler_ids):
            threads.append(threading.Thread(
                target=fire, args=(f"filler{fi}", prompts[fid], max(args.max_new * 2, 64))))
        for t in threads:
            t.start()
        time.sleep(0.01)
        for t in threads:
            t.join()
        result["batched"].append(holder.get("probe") or {})
        print(f"[{args.label}] batched rep{r+1}: probe tokens="
              f"{len((holder.get('probe') or {}).get('token_ids') or [])} "
              f"http={(holder.get('probe') or {}).get('http_status')}")

    # 对比：alone 内部（重复一致性）、batched 内部、alone vs batched
    if len(result["alone"]) >= 2:
        result["comparisons"].append({"kind": "alone_rep1_vs_rep2",
                                      **diff_runs(result["alone"][0], result["alone"][1])})
    if len(result["batched"]) >= 2:
        result["comparisons"].append({"kind": "batched_rep1_vs_rep2",
                                      **diff_runs(result["batched"][0], result["batched"][1])})
    if result["alone"] and result["batched"]:
        result["comparisons"].append({"kind": "alone_rep1_vs_batched_rep1",
                                      **diff_runs(result["alone"][0], result["batched"][0])})

    result["utc_written"] = utcnow()
    out_path = out_dir / f"batchprobe_{args.label}.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=1),
                        encoding="utf-8", newline="\n")
    print(f"wrote {out_path}")
    for c in result["comparisons"]:
        print(f"  {c['kind']}: tokens_equal={c['tokens_equal']} "
              f"max_prob_diff={c['max_prob_diff']} first_diff_step={c['first_diff_step']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

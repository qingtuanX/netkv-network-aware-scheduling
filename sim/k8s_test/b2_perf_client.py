#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""b2_perf_client.py —— B2 同云性能基线的流式计时客户端 v2.1（只用标准库）。

v2.1 相对 v2 的修正（作者裁定 2026-10-11，token ID 兜底逻辑）：

- **禁止用块数冒充 token 数**（v2 的 `chunk_fallback` 静默兜底作废）：
  token 数只认流式块里 `logprobs.tokens` 数出来的 token ID。
  收不到 token ID ⇒ 该请求 `token_count_invalid=true`、`n_tokens` 不填、
  不算长度、不算平均 token 间隔。
- **严格模式（默认开）**：正式采数中出现**任意一次**收不到 token ID ⇒
  立即停止该格的采数（后续请求不再提交），结果标 `aborted_strict=true`，
  退出码 2——先停下来检查，不继续采该格。
- 汇总新增 `n_token_source_fallback` / `n_token_source_invalid` 两个计数。
- `--reps` 默认值 3→**10**（与协议每格 10 次一致；运行手册仍逐条显式传参）。

v2 的四处修正仍然有效：
1. "文字块"≠"一个 token"：块内可能多 token ⇒ 指标诚实命名 `chunk_gap_s`
   （文字块间隔）与 `avg_token_gap_s`（每请求平均 token 间隔，token 数由
   token ID 数出）；服务端 `vllm:request_time_per_output_token_seconds`
   （按请求观测后**汇总**的间隔分布，只作整体交叉核对，不用于逐请求配对）。
2. 固定生成长度：`ignore_eos=true + max_tokens`，每条恰好生成指定 token 数。
3. 请求参数与协议逐项对齐；`logprobs=0` **只为取 token ID，不取概率**；
   无任何概率记录设置（两臂服务参数一致）。
4. 独立重复单位是"轮"；本工具每请求明细只是抽样。

产物：<out> JSON（每请求明细 + 汇总）。只用标准库；WSL 与 Windows 都能跑。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# 生成参数：全部显式发送（协议 §3 与本表逐项一致，改一处必须改两处）。
GEN_PARAMS = {
    "temperature": 0.0,
    "top_p": 1.0,
    "top_k": -1,
    "repetition_penalty": 1.0,
    "seed": 0,
}
# logprobs=0：只为在流式块里拿到 token ID（不取概率值）。
# ignore_eos=True：固定生成长度，保证两臂工作量相同（协议 §3/§6）。
FIXED_PARAMS = {
    "ignore_eos": True,
    "logprobs": 0,
}


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def complete_stream(url: str, model: str, prompt: str, max_tokens: int,
                    timeout: float = 600.0) -> dict:
    """发一个流式请求，逐输出块记时间戳、从块里读 token ID。

    vLLM /v1/completions 的 SSE：每行 `data: {json}`，choices[0].text 是增量文本，
    choices[0].logprobs.tokens 是该块的 token ID 列表（可能多于一个）；
    结尾一个 `data: [DONE]`。
    """
    payload = {"model": model, "prompt": prompt, "max_tokens": max_tokens,
               "stream": True, **GEN_PARAMS, **FIXED_PARAMS}
    data = json.dumps(payload).encode("utf-8")
    # 约定与 b1_client.py 一致：--url 是服务**根**地址，端点固定 /v1/completions。
    endpoint = url.rstrip("/") + "/v1/completions"
    req = urllib.request.Request(endpoint, data=data,
                                 headers={"Content-Type": "application/json",
                                          "Accept": "text/event-stream"},
                                 method="POST")
    rec = {
        "utc_sent": utcnow(),
        "chunk_times": [],       # 每个非空输出块到达时刻（perf_counter 相对值）
        "n_chunks": 0,
        "n_tokens": 0,           # 只认 logprobs.tokens 数出来的 token ID
        "token_count_source": None,   # "logprobs_tokens" / "chunk_fallback"
        "token_count_invalid": True,  # 收到 token ID 才变 False
        "finish_reason": None,
        "http_status": None,
        "error": None,
    }
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            rec["http_status"] = resp.status
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                body = line[5:].strip()
                if body == "[DONE]":
                    break
                try:
                    j = json.loads(body)
                except Exception:
                    continue
                ch = (j.get("choices") or [{}])[0]
                lp = ch.get("logprobs") or {}
                toks = lp.get("tokens") or []
                text = ch.get("text")
                if toks or text:
                    rec["chunk_times"].append(time.perf_counter() - t0)
                    rec["n_chunks"] += 1
                if toks:
                    rec["n_tokens"] += len(toks)
                    rec["token_count_source"] = "logprobs_tokens"
                    rec["token_count_invalid"] = False
                if ch.get("finish_reason"):
                    rec["finish_reason"] = ch["finish_reason"]
    except urllib.error.HTTPError as e:
        rec["http_status"] = e.code
        rec["error"] = f"HTTPError: {e.read()[:500]!r}"
    except Exception as e:
        rec["error"] = f"{type(e).__name__}: {e}"
    # v2.1：不再用块数冒充 token 数。收不到 token ID 就如实标记无效。
    if rec["n_tokens"] == 0:
        rec["token_count_source"] = "chunk_fallback" if rec["n_chunks"] else "none"
        rec["token_count_invalid"] = True
    rec["total_s"] = round(time.perf_counter() - t0, 6)
    ct = rec["chunk_times"]
    rec["ttft_s"] = round(ct[0], 6) if ct else None
    rec["chunk_gap_s"] = [round(b - a, 6) for a, b in zip(ct, ct[1:])]
    # 平均 token 间隔只在 token ID 有效时计算
    if rec["token_count_invalid"] is False and rec["n_tokens"] > 1 and ct:
        rec["avg_token_gap_s"] = round((ct[-1] - ct[0]) / (rec["n_tokens"] - 1), 6)
    else:
        rec["avg_token_gap_s"] = None
    rec["utc_recv"] = utcnow()
    return rec


def pct(vals: list[float], q: float) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    return s[max(0, min(len(s) - 1, int(q * len(s))))]


def run_batch(items: list[tuple[str, str]], url: str, model: str, max_tokens: int,
              concurrency: int, strict_token_ids: bool) -> tuple[list[dict], bool]:
    """items = [(id, prompt)]；concurrency=1 串行，>1 线程并发。

    严格模式下一旦有请求收不到 token ID，停止提交后续请求（该格作废重查）。
    返回 (明细, 是否触发过严格停止)。
    """
    out: list[dict] = []
    lock = threading.Lock()
    idx = [0]
    abort = threading.Event()
    aborted = [False]

    def worker() -> None:
        while True:
            with lock:
                if idx[0] >= len(items) or (strict_token_ids and abort.is_set()):
                    return
                pid, prompt = items[idx[0]]
                idx[0] += 1
            r = complete_stream(url, model, prompt, max_tokens)
            r["id"] = pid
            with lock:
                out.append(r)
            if r["token_count_invalid"]:
                ok = f"TOKEN_ID_MISSING({r['token_count_source']})"
                if strict_token_ids:
                    abort.set()
                    aborted[0] = True
            else:
                ok = "OK" if r["n_tokens"] == max_tokens else f"LEN_MISMATCH({r['n_tokens']})"
            print(f"  [{pid}] ttft={r['ttft_s']} tokens={r['n_tokens']}/{max_tokens} {ok} "
                  f"total={r['total_s']} finish={r['finish_reason']} err={r['error']}")
        # end worker

    threads = [threading.Thread(target=worker) for _ in range(max(1, concurrency))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return out, aborted[0]


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(
        description="B2 流式计时客户端（TTFT/文字块间隔/平均token间隔/总耗时）")
    ap.add_argument("--url", required=True, help="被测端点服务**根**地址（对照臂服务或拆分臂代理）")
    ap.add_argument("--arm", required=True, help="标签：base / disagg 等")
    ap.add_argument("--model", default="Qwen/Qwen2.5-1.5B-Instruct")
    ap.add_argument("--prompts-file", required=True,
                    help="题库 JSON（每轮固定题序文件见 b2_prompt_orders/）")
    ap.add_argument("--only-ids", nargs="*", default=None)
    ap.add_argument("--max-tokens", type=int, default=64,
                    help="固定生成长度（配 ignore_eos=true，每条都应恰好生成这么多 token）")
    ap.add_argument("--concurrency", type=int, default=1, help="1=串行；>1=低并发档")
    ap.add_argument("--reps", type=int, default=10, help="整组重复次数（协议每格 10）")
    ap.add_argument("--no-strict-token-ids", dest="strict_token_ids",
                    action="store_false", default=True,
                    help="（仅调试用）关闭『收不到 token ID 就停该格』的严格模式")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    prompts = json.loads(Path(args.prompts_file).read_text(encoding="utf-8"))
    if args.only_ids:
        keep = set(args.only_ids)
        prompts = [p for p in prompts if p["id"] in keep]
    items = [(f"{p['id']}_rep{r}", p["prompt"]) for r in range(1, args.reps + 1)
             for p in prompts]
    print(f"target={args.url} arm={args.arm} n={len(items)} "
          f"concurrency={args.concurrency} max_tokens={args.max_tokens}(fixed) "
          f"strict_token_ids={args.strict_token_ids}")

    all_recs, aborted = run_batch(items, args.url, args.model, args.max_tokens,
                                  args.concurrency, args.strict_token_ids)

    valid = [r for r in all_recs if not r["token_count_invalid"]]
    n_fallback = sum(1 for r in all_recs
                     if r["token_count_source"] == "chunk_fallback")
    n_invalid = sum(1 for r in all_recs if r["token_count_invalid"])
    ttfts = [r["ttft_s"] for r in all_recs if r["ttft_s"] is not None]
    cgaps = [x for r in all_recs for x in (r["chunk_gap_s"] or [])]
    atgs = [r["avg_token_gap_s"] for r in valid if r["avg_token_gap_s"] is not None]
    totals = [r["total_s"] for r in all_recs if r["error"] is None]
    n_mismatch = sum(1 for r in valid if r["n_tokens"] != args.max_tokens)
    summary = {
        "n_requests": len(all_recs),
        "n_errors": sum(1 for r in all_recs if r["error"] is not None),
        "n_token_source_fallback": n_fallback,
        "n_token_source_invalid": n_invalid,
        "n_length_mismatch": n_mismatch,
        "token_count_min": min((r["n_tokens"] for r in valid), default=None),
        "token_count_max": max((r["n_tokens"] for r in valid), default=None),
        "token_count_source": sorted({str(r["token_count_source"]) for r in all_recs}),
        "ttft_p50": pct(ttfts, 0.50), "ttft_p95": pct(ttfts, 0.95),
        "chunk_gap_p50": pct(cgaps, 0.50), "chunk_gap_p95": pct(cgaps, 0.95),
        "avg_token_gap_p50": pct(atgs, 0.50), "avg_token_gap_p95": pct(atgs, 0.95),
        "total_p50": pct(totals, 0.50), "total_p95": pct(totals, 0.95),
        "total_mean": round(statistics.mean(totals), 6) if totals else None,
    }
    result = {
        "tool": "b2_perf_client.py v2.1",
        "utc_written": utcnow(),
        "arm": args.arm,
        "target_url": args.url.rstrip("/"),
        "model": args.model,
        "max_tokens": args.max_tokens,
        "concurrency": args.concurrency,
        "reps": args.reps,
        "strict_token_ids": args.strict_token_ids,
        "aborted_strict": aborted,
        "request_params_actual": {**GEN_PARAMS, **FIXED_PARAMS, "stream": True,
                                  "max_tokens": args.max_tokens},
        "metric_names_note": {
            "chunk_gap_s": "文字块间隔（一个块可能含多个 token，不叫逐 token 间隔）",
            "avg_token_gap_s": "每请求平均 token 间隔 = (末块-首块)/(token数-1)；"
                               "token 数只认 token ID；服务端"
                               "vllm:request_time_per_output_token_seconds 是"
                               "按请求观测后汇总的间隔分布，只作整体交叉核对，"
                               "不用于逐请求配对",
            "token_count_source": "logprobs_tokens=从块内 token ID 数出（合法）；"
                                  "chunk_fallback=收不到 token ID（该请求作废，"
                                  "严格模式下停该格）",
        },
        "summary": summary,
        "requests": all_recs,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=1),
                              encoding="utf-8", newline="\n")
    print(f"wrote {args.out}")
    print("summary:", json.dumps(summary, ensure_ascii=False))
    if aborted:
        print("★ 严格模式触发：已出现收不到 token ID 的请求，停止该格；"
              "该格作废并先检查（协议 §6/§7）")
        return 2
    return 0 if (summary["n_errors"] == 0 and n_mismatch == 0 and n_invalid == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())

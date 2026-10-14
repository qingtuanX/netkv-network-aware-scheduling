#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性探针：确认 `temperature=0` 到底是不是"贪婪解码"（即"每次都选概率最大的词"）。

【为什么要做这个】2026-10-08 诊断 4c 时发现：
    在 **约 22% 的步**上，服务返回的词**不是**它自己返回的 `top_logprobs` 里的最大值。
    贪婪解码下这是**不可能**的 —— 它只能选最大值。
    所以要么 `temperature=0` 没有被当成贪婪，要么服务在**采样**。

【这会推翻什么】如果服务其实是"按种子采样"而不是贪婪，那么：
  * 两条臂的输出差异**未必**来自"两台机器的分布不同"，
    也可能来自"同一个种子在一张**略有差异**的分布上采样出不同结果"；
  * 但**两种机器各自仍然完全可复现**（种子固定）——这与我们观测到的现象一致；
  * 于是 4c 的"输出不一致"很可能是**测量设置**造成的，而不是 KV 传输坏了。

【判据（这一条就够了）】
    同一条 prompt、`temperature=0`、**只改 seed**（0 vs 999）：
      * 输出**完全相同** → `temperature=0` 是贪婪的，种子无影响（预期行为）；
      * 输出**不同** → **它其实在采样** ⇒ 上面那条推论成立，必须重新设计 4c 的对照方式。

另外附带两个对照：
  * 同一 seed 发两次 → 应当完全相同（确认服务是确定性的）；
  * `temperature=1.0` 也发一次 → 作为"确实在采样时是什么样"的参照。

产物：`<out>` JSON（每次尝试的原始响应体 + token_ids + 逐次比较结论）。
**本脚本不做任何"性能"判断，也不改任何既有结论。**
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path


def setup_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


def post(url: str, payload: dict, timeout: float = 300.0):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return None, json.dumps({"client_error": f"{type(e).__name__}: {e}"}).encode()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="temperature=0 是不是贪婪？一次性探针")
    ap.add_argument("--url", required=True, help="服务根地址，例如 http://172.29.31.51:8300")
    ap.add_argument("--model", required=True)
    ap.add_argument("--prompts-file", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--prompt-id", default=None, help="用哪条 prompt（默认第一条）")
    ap.add_argument("--max-tokens", type=int, default=32)
    args = ap.parse_args(argv)
    setup_streams()

    prompts = json.loads(Path(args.prompts_file).read_text(encoding="utf-8"))
    p = next((x for x in prompts if x["id"] == args.prompt_id), prompts[0])
    base = args.url.rstrip("/")
    print(f"target = {base}")
    print(f"prompt = {p['id']}（{len(p['prompt'])} 字符）")
    print()

    # 序列：(标签, temperature, seed)
    trials = [
        ("t0_s0_a",   0.0,   0),
        ("t0_s0_b",   0.0,   0),      # 同参数再来一次 → 检验确定性
        ("t0_s999",   0.0, 999),      # ★ 只改种子 → 检验是否贪婪
        ("t0_s12345", 0.0, 12345),    # ★ 再换一个种子
        ("t1_s0",     1.0,   0),      # 参照：确定在采样时是什么样
    ]

    out = {"target_url": base, "prompt_id": p["id"], "max_tokens": args.max_tokens, "trials": []}
    for name, temp, seed in trials:
        payload = {
            "model": args.model, "prompt": p["prompt"],
            "max_tokens": args.max_tokens, "temperature": temp, "seed": seed,
            "stream": False, "return_token_ids": True, "logprobs": 5,
        }
        code, body = post(f"{base}/v1/completions", payload)
        rec = {"trial": name, "temperature": temp, "seed": seed, "http_status": code,
               "sent_payload_keys": sorted(payload.keys())}
        try:
            j = json.loads(body.decode("utf-8", "replace"))
            ch = (j.get("choices") or [{}])[0]
            rec["token_ids"] = ch.get("token_ids")
            rec["text"] = ch.get("text")
            rec["finish_reason"] = ch.get("finish_reason")
            rec["echoed_usage"] = j.get("usage")
        except Exception as e:
            rec["parse_error"] = f"{type(e).__name__}: {e}"
            rec["raw_head"] = body.decode("utf-8", "replace")[:800]
        out["trials"].append(rec)
        print(f"  {name:<10} T={temp} seed={seed:<6} http={code} "
              f"n_tokens={len(rec.get('token_ids') or [])} finish={rec.get('finish_reason')}")

    def ids(rec):
        return rec.get("token_ids")

    by = {t["trial"]: t for t in out["trials"]}
    same_seed_repeat = ids(by["t0_s0_a"]) == ids(by["t0_s0_b"])
    seed_matters = not (ids(by["t0_s0_a"]) == ids(by["t0_s999"]) == ids(by["t0_s12345"]))
    t1_differs = ids(by["t0_s0_a"]) != ids(by["t1_s0"])

    out["verdict"] = {
        "同一seed重复两次结果相同": same_seed_repeat,
        "temperature=0下改seed结果会变": seed_matters,
        "temperature=1与0结果不同": t1_differs,
    }
    if seed_matters:
        out["verdict"]["结论"] = ("**temperature=0 不是贪婪**：只改 seed 输出就变了 ⇒ 服务在对分布**采样**。"
                                  "这会让'两台机器分布略有差异'直接变成'采样结果不同'，"
                                  "是 4c 输出不一致的一个**强候选原因**（且与'两臂各自可复现'相容）。")
    else:
        out["verdict"]["结论"] = ("temperature=0 表现为贪婪：seed 不影响输出。"
                                  "那么'选中的词不是 top 最大值'这件事另有原因，需要继续查。")

    print()
    for k, v in out["verdict"].items():
        print(f"  {k}: {v}")

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2),
                              encoding="utf-8", newline="\n")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

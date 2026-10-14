#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B1 的测量客户端（只用标准库，避免任何依赖差异混进结果）。

它做两件事：
  1) `gen-prompts`：生成一套**确定性**的合成提示词（不含任何真实用户文本）。
     预设：v1（第一轮 5 条）、v2（4c 诊断第二轮 5 条）、**cal10（PREREG_4 同机校准 10 条）**。
  2) 默认模式：把同一套提示词按 `temperature=0` 发给某个 OpenAI 兼容端点，
     逐条记录原始输出与 token id，供 4c（输出对不对）做逐 token 比对。
     加 `--explicit-gen-params` 时显式发送 top_p=1 / top_k=-1 / repetition_penalty=1
     （B1_4C_PREREG_4.md §2；默认 legacy 不带，保持历史重放行为不变）。

为什么必须 temperature=0：只有让模型每次都选最可能的那个词，同一个输入才有
唯一正确答案，"拆分式结果 == 单机结果"这句话才有可判定的含义。

产物：
  <out>            结构化结果 JSON（每请求一行）
  <out 去 .json>_raw.jsonl   原始响应体逐条落盘（出错时也留原文，便于查）
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# 一段无意义的填充文本：用来把提示词撑到接近 1k token，让 KV 传输有实际体量。
# 刻意用合成文本（仓库纪律：日志与请求不得包含真实用户文本）。
FILLER = (
    "In a distributed inference cluster the scheduler must decide which worker "
    "should execute each incoming request, and that decision depends on both the "
    "compute backlog of a worker and the state of the network path that leads to "
    "it. This paragraph exists only to make the prompt long enough that the key "
    "value cache produced by the prefill stage has a measurable size. It carries "
    "no semantic instruction and no private information. "
)

# ---------------------------------------------------------------- v2 提示词组
# 为什么要有第二组：第一组（`q?` 之外的 p01..p05）**已经被用于
# `b1_4c_20261008T002222Z` 那一轮**。按作者裁定，那一轮的 15 对只能当"发现问题的线索"，
# **不能用来验证事后新假设**。所以 4c 分歧原因诊断（第二轮）必须换一组**没参与过**的提示词。
#
# ⚠️ 冻结来源：`B1_4C_PREREG_2.md` §2。规格是**逐字符**写死的：
#   * 填充文本 = 下面这段（预注册里 markdown 的换行是**排版折行**，不是内容，已按空格连成一句）
#   * reps 阶梯 = (6, 10, 14, 18, 22)
#   * 模板 = "Skim the passage below, ... \n\n{body}\n\nAnswer:"
#   * id 前缀 = q
#   **改这里就等于改预注册**，必须另立文件，不许顺手改。
FILLER_V2 = (
    "A fleet of inference servers receives requests of varying sizes, and the router "
    "in front of the fleet must pick one server for each request. The choice can "
    "consider how much work is already queued on a server, how fast that server "
    "computes, and how congested the path that leads to it happens to be. This "
    "passage is filler: it exists only so that the resulting prompt is long enough "
    "for the key value cache to have a measurable size. It contains no instruction, "
    "no personal data, and no claim about any real system. "
)


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# 两组提示词的冻结规格。**默认 v1** —— 这样旧行为一字不变，v2 必须显式要求。
PROMPT_SETS = {
    "v1": {
        "filler": FILLER,
        "reps": (8, 12, 16, 20, 24),
        "prefix": "p",
        "tpl": ("Read the following text, then reply with exactly one short "
                "sentence summarising its topic.\n\n{body}\n\nSummary:"),
    },
    "v2": {
        "filler": FILLER_V2,
        "reps": (6, 10, 14, 18, 22),
        "prefix": "q",
        "tpl": ("Skim the passage below, then answer with one short sentence "
                "naming what it is about.\n\n{body}\n\nAnswer:"),
    },
    # cal10：4c 收口（第四轮）的**同机校准题**，规格冻结在 `B1_4C_PREREG_4.md` §5。
    # 模板与填充与 v2 完全相同（同一任务域），只换 reps 阶梯与 id 前缀；10 条**从未
    # 参与任何采数**。用途：量"同一台机器自己重复跑"的分布波动（Δ_cal）+ top-20
    # 非退化自检。校准题不进判定，诊断题不进校准。
    # ⚠️ 与 v2 同理：**改这里就等于改预注册**，必须另立文件，不许顺手改。
    "cal10": {
        "filler": FILLER_V2,
        "reps": (4, 7, 10, 13, 16, 19, 22, 25, 28, 31),
        "prefix": "c",
        "tpl": ("Skim the passage below, then answer with one short sentence "
                "naming what it is about.\n\n{body}\n\nAnswer:"),
    },
}


def gen_prompts(path: Path, preset: str = "v1") -> None:
    """生成一套确定性提示词（v1/v2 各 5 条、cal10 共 10 条，长度递增），落成 JSON。

    preset：`v1`（第一轮用过的那组，默认）、`v2`（4c 分歧诊断第二轮用的新组）、
    `cal10`（B1_4C_PREREG_4.md §5 的同机校准 10 条）。各组规格见 `PROMPT_SETS`，
    来源是各自的预注册文件。
    """
    spec = PROMPT_SETS[preset]
    prompts = []
    for i, reps in enumerate(spec["reps"], start=1):
        body = spec["filler"] * reps
        prompts.append(
            {
                "id": f"{spec['prefix']}{i:02d}_rep{reps}",
                "reps": reps,
                "prompt": spec["tpl"].format(body=body),
            }
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    # ⚠️ `newline="\n"` 是**必需**的，不是随便加的：
    #   Windows 上 `write_text` 默认会把 `\n` 翻成 `\r\n`，于是"同一份规格"在
    #   Windows 和 Linux 上生成出**不同字节**的文件 —— 而预注册里承诺了
    #   "事后重跑必须与本轮用的文件逐字节相同"。强制 LF 之后，两边产出才一致。
    #   （2026-10-08 实测：不加这个参数时，本机生成的文件比远端多 26 个 `\r`。）
    path.write_text(
        json.dumps(prompts, ensure_ascii=False, indent=2),
        encoding="utf-8",
        newline="\n",
    )
    print(f"wrote {len(prompts)} prompts (preset={preset}) -> {path}")
    for p in prompts:
        print(f"  {p['id']}: {len(p['prompt'])} chars")


def post_json(url: str, payload: dict, timeout: float = 600.0):
    """发一个 POST，把原始响应体原文返回（不做任何加工）。"""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    started = utcnow()
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            code = resp.status
    except urllib.error.HTTPError as e:
        body = e.read()
        code = e.code
    except Exception as e:  # 超时、连接被拒、代理挂了……
        return {
            "utc_sent": started,
            "utc_recv": utcnow(),
            "http_status": None,
            "elapsed_s": time.perf_counter() - t0,
            "error_class": type(e).__name__,
            "error_text": str(e)[:2000],
            "body": b"",
        }
    return {
        "utc_sent": started,
        "utc_recv": utcnow(),
        "http_status": code,
        "elapsed_s": time.perf_counter() - t0,
        "error_class": None,
        "error_text": None,
        "body": body,
    }


def get_version(base: str) -> dict:
    """问一下目标端点的 vLLM 版本（代理上没有这个路由，属正常）。"""
    try:
        with urllib.request.urlopen(f"{base}/version", timeout=10) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return {"unavailable": f"{type(e).__name__}: {e}"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="B1 测量客户端")
    sub = ap.add_subparsers(dest="mode")
    g = sub.add_parser("gen-prompts", help="生成确定性提示词")
    g.add_argument("--out", required=True)
    g.add_argument(
        "--preset",
        choices=sorted(PROMPT_SETS),
        default="v1",
        help="提示词组：v1 = 第一轮用的那组（默认）；v2 = 4c 分歧诊断第二轮用的新组"
             "（规格冻结在 B1_4C_PREREG_2.md §2）；cal10 = B1_4C_PREREG_4.md §5 的"
             "同机校准 10 条（从未参与采数）",
    )

    ap.add_argument("--url", help="服务根地址，例如 http://172.29.31.52:8192")
    ap.add_argument("--model", default="Qwen/Qwen2.5-1.5B-Instruct")
    ap.add_argument("--prompts-file", required=False)
    ap.add_argument("--max-tokens", type=int, default=64)
    ap.add_argument(
        "--logprobs",
        type=int,
        default=0,
        help="请求 top-N 的 logprobs（0 = 不请求，**保持 B1 原有行为**）。"
             "B1_4C_PREREG（第一版）用 5；B1_4C_PREREG_3 用 20（与 LOGPROBS_CALIB_PREREG "
             "的 top-20 校准口径一致）：首次分歧处要拿两边给竞争 token 的概率，"
             "用来区分「近并列翻转」与「确定性路径差异」（见 B1_4C_PREREG.md §7 / B1_4C_PREREG_3.md §5）。",
    )
    ap.add_argument("--out", help="结果 JSON 路径")
    ap.add_argument(
        "--explicit-gen-params",
        action="store_true",
        help="显式发送 top_p=1 / top_k=-1 / repetition_penalty=1（B1_4C_PREREG_4.md §2 要求"
             "\"全部显式发出、不依赖默认值\"）。**默认关（legacy）**：B1 与 PREREG_3 的重放"
             "必须保持历史行为逐字不变。⚠️ top_k 必须是 -1 不能是 1：top_k=1 会把 processed "
             "分布裁成 one-hot，top-20 分布比较直接做不成。",
    )
    ap.add_argument(
        "--only-ids",
        nargs="*",
        default=None,
        help="只跑这些 prompt id（默认全跑）。用于「先单请求，再少量重复」的串行顺序。",
    )
    args = ap.parse_args(argv)

    if args.mode == "gen-prompts":
        gen_prompts(Path(args.out), args.preset)
        return 0

    if not args.url or not args.prompts_file or not args.out:
        ap.error("需要 --url / --prompts-file / --out")

    prompts = json.loads(Path(args.prompts_file).read_text(encoding="utf-8"))
    if args.only_ids:
        keep = set(args.only_ids)
        prompts = [p for p in prompts if p["id"] in keep]
        print(f"--only-ids={args.only_ids} → 保留 {len(prompts)} 条")
        if not prompts:
            ap.error(f"--only-ids 过滤后一条都不剩；文件里只有 "
                     f"{[p['id'] for p in json.loads(Path(args.prompts_file).read_text(encoding='utf-8'))]}")
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path = out_path.with_name(out_path.stem + "_raw.jsonl")

    base = args.url.rstrip("/")
    version = get_version(base)
    print(f"target={base}  /version={version}")

    records = []
    last_params: dict | None = None
    with raw_path.open("w", encoding="utf-8") as rawf:
        for p in prompts:
            payload = {
                "model": args.model,
                "prompt": p["prompt"],
                "max_tokens": args.max_tokens,
                "temperature": 0.0,
                "seed": 0,
                "stream": False,
                "return_token_ids": True,
            }
            # logprobs：只在显式要求时带上（默认不带 ⇒ B1 原有行为完全不变）。
            # 口径说明：OpenAI 风格的 /v1/completions 里 logprobs 是**整数 N**（不是对象），
            # vLLM 支持 N ∈ 0..20。这里直接传 int。
            if args.logprobs and args.logprobs > 0:
                payload["logprobs"] = args.logprobs
            # 显式生成参数（B1_4C_PREREG_4.md §2）：top_p / top_k / repetition_penalty
            # 默认**不带**（legacy，B1/PREREG_3 重放保持历史行为）；PREREG_4 流程显式传
            # --explicit-gen-params。top_k=-1（不裁剪候选）——写 1 会把 processed 分布
            # 裁成 one-hot，top-20 比较做不成。
            if args.explicit_gen_params:
                payload["top_p"] = 1.0
                payload["top_k"] = -1
                payload["repetition_penalty"] = 1.0
            # 记下这次**实际发出**的参数（除 model/prompt 外的全部键），供结果 JSON 留证。
            # PREREG_4 §2.1 要求证据"从实际发出的 payload 里取，不是从命令行抄"。
            last_params = {k: v for k, v in payload.items() if k not in ("model", "prompt")}
            r = post_json(f"{base}/v1/completions", payload)
            rawf.write(
                json.dumps(
                    {
                        "id": p["id"],
                        "utc_sent": r["utc_sent"],
                        "http_status": r["http_status"],
                        "elapsed_s": r["elapsed_s"],
                        "error_class": r["error_class"],
                        "error_text": r["error_text"],
                        "raw_body": r["body"].decode("utf-8", "replace")[:20000],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            rec = {
                "id": p["id"],
                "reps": p["reps"],
                "prompt_chars": len(p["prompt"]),
                "http_status": r["http_status"],
                "elapsed_s": round(r["elapsed_s"], 4),
                "error_class": r["error_class"],
                "error_text": r["error_text"],
                "raw_body_bytes": len(r["body"]),
            }
            try:
                j = json.loads(r["body"].decode("utf-8", "replace"))
                ch = (j.get("choices") or [{}])[0]
                rec.update(
                    {
                        "prompt_tokens": (j.get("usage") or {}).get("prompt_tokens"),
                        "completion_tokens": (j.get("usage") or {}).get("completion_tokens"),
                        "finish_reason": ch.get("finish_reason"),
                        "output_text": ch.get("text"),
                        "token_ids": ch.get("token_ids"),
                        # 原始 logprobs 结构**原样存**（不在这里加工），
                        # 分类与解读交给 b1_compare.py，避免"边采边解释"。
                        "logprobs": ch.get("logprobs"),
                    }
                )
            except Exception as e:
                rec["parse_error"] = f"{type(e).__name__}: {e}"
            records.append(rec)
            print(
                f"  {rec['id']}: http={rec['http_status']} "
                f"prompt_tokens={rec.get('prompt_tokens')} "
                f"completion_tokens={rec.get('completion_tokens')} "
                f"finish={rec.get('finish_reason')} "
                f"tok_ids={'yes' if rec.get('token_ids') else 'no'} "
                f"{rec['elapsed_s']}s"
            )

    result = {
        "target_url": base,
        "target_version_endpoint": version,
        "model": args.model,
        "max_tokens": args.max_tokens,
        "temperature": 0,
        "seed": 0,
        "return_token_ids_requested": True,
        "logprobs_requested": args.logprobs,
        "explicit_gen_params": bool(args.explicit_gen_params),
        # B1_4C_PREREG_4.md §2.1 的"请求侧实际参数"证据：从实际发出的 payload 里取。
        "request_params_actual": last_params,
        "utc_written": utcnow(),
        "requests": records,
    }
    # 同样强制 LF：让"在本机跑"与"在服务器上跑"产出同样的字节（原因见 gen_prompts 里的注释）。
    out_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
        newline="\n",
    )
    print(f"wrote {out_path}  ({len(records)} requests)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

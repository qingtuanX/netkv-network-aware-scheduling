#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B1 的 4b 辅助表：把关键 KV 序列在**多个检查点**上的原文数值排成一张表。

为什么要它：`b1_metrics_delta.py` 打的是"t0 → t1 两个快照"的增量报告，标签列为了对齐会被截断；
而本轮有**四个**检查点（t0 / 冒烟后 / 运行中 / 结束），且要求"保留全部标签"。
这个脚本只做一件事：**原样取出**指定序列在每个快照里的数值，不做任何判断、不算比率、不给结论。

用法：
    python b1_kv_table.py --dir <attempt 目录> --out <输出文本>
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

# 要取出原文的序列（**只取这几条，不挑不删**）
WANT = [
    "vllm:nixl_bytes_transferred_count",
    "vllm:nixl_bytes_transferred_sum",
    "vllm:nixl_num_descriptors_count",
    "vllm:nixl_num_descriptors_sum",
    "vllm:nixl_xfer_time_seconds_count",
    "vllm:nixl_xfer_time_seconds_sum",
    "vllm:nixl_post_time_seconds_count",
    "vllm:nixl_post_time_seconds_sum",
    "vllm:nixl_num_failed_transfers_total",
    "vllm:nixl_num_failed_notifications_total",
    "vllm:nixl_num_kv_expired_reqs_total",
]

LINE_RE = re.compile(r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?P<labels>\{.*\})?\s+(?P<value>[^\s]+)\s*$")


def load(path: Path) -> dict[str, str]:
    """返回 {(指标名 + 标签原文): 数值原文}。标签**原样保留**，只加一个空格分隔。"""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line or line.startswith("#"):
            continue
        m = LINE_RE.match(line)
        if not m:
            continue
        key = m.group("name") + (m.group("labels") or "")
        out[key] = m.group("value")
    return out


def num(v: str | None) -> str:
    if v is None:
        return "<无此序列>"
    try:
        f = float(v)
    except ValueError:
        return v
    return str(int(f)) if f == int(f) and abs(f) < 1e15 else f"{f:.6g}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    d = Path(args.dir)
    # (标签, 文件名前缀)。前缀与 run_b1.sh 的 grab_metrics 标签一一对应。
    ends = [
        ("P 前处理端 A:8100", "metrics_precheck_prefill_t0_raw.txt"),
        ("P 前处理端 A:8100", "metrics_precheck_prefill_t1_raw.txt"),
        ("P 前处理端 A:8100", "metrics_measure_prefill_t0_raw.txt"),
        ("P 前处理端 A:8100", "metrics_measure_prefill_mid_raw.txt"),
        ("P 前处理端 A:8100", "metrics_measure_prefill_t1_raw.txt"),
        ("D 生成端 B:8200", "metrics_precheck_decode_t0_raw.txt"),
        ("D 生成端 B:8200", "metrics_precheck_decode_t1_raw.txt"),
        ("D 生成端 B:8200", "metrics_measure_decode_t0_raw.txt"),
        ("D 生成端 B:8200", "metrics_measure_decode_mid_raw.txt"),
        ("D 生成端 B:8200", "metrics_measure_decode_t1_raw.txt"),
    ]

    snap = {name: load(d / name) for _, name in ends}
    lines: list[str] = []
    w = lines.append
    w("=" * 100)
    w("B1 4b 辅助表：KV 序列在四个检查点上的**原文数值**（只搬运，不判断）")
    w("=" * 100)
    w("列说明：pre_t0=拆分服务刚起来/无请求；pre_t1=冒烟后；t0=正式测量前；")
    w("        mid=**运行中**（第一条正式请求之后、重复请求之前）；t1=正式测量结束。")
    w("")

    for end in ("P 前处理端 A:8100", "D 生成端 B:8200"):
        w("#" * 100)
        w(f"### {end}")
        w("#" * 100)
        for want in WANT:
            # 找出该端所有快照里、以 want 开头的序列（含标签），合并
            keys: set[str] = set()
            for kind, name in ends:
                if kind != end:
                    continue
                for k in snap[name]:
                    if k.startswith(want + "{") or k == want:
                        keys.add(k)
            if not keys:
                w(f"  {want}: **两端快照里都没有这一项**")
                continue
            for k in sorted(keys):
                row = []
                for kind, name in ends:
                    if kind != end:
                        continue
                    short = {
                        "metrics_precheck_prefill_t0_raw.txt": "pre_t0",
                        "metrics_precheck_prefill_t1_raw.txt": "pre_t1",
                        "metrics_precheck_decode_t0_raw.txt": "pre_t0",
                        "metrics_precheck_decode_t1_raw.txt": "pre_t1",
                        "metrics_measure_prefill_t0_raw.txt": "t0",
                        "metrics_measure_prefill_mid_raw.txt": "mid",
                        "metrics_measure_prefill_t1_raw.txt": "t1",
                        "metrics_measure_decode_t0_raw.txt": "t0",
                        "metrics_measure_decode_mid_raw.txt": "mid",
                        "metrics_measure_decode_t1_raw.txt": "t1",
                    }[name]
                    row.append(f"{short}={num(snap[name].get(k))}")
                w(f"  {k}")
                w(f"      " + "  ".join(row))
        w("")

    # 生成端还有一个"这条 prompt 的 KV 是不是从外部来的"计数器，单列
    w("#" * 100)
    w("### D 生成端：prompt token 的来源（`vllm:prompt_tokens_by_source_total`）")
    w("#" * 100)
    for kind, name in ends:
        if kind != "D 生成端 B:8200":
            continue
        s = snap[name]
        picks = sorted(k for k in s if k.startswith("vllm:prompt_tokens_by_source_total"))
        w(f"  [{name}]")
        for k in picks:
            w(f"      {k} = {num(s[k])}")
    w("")
    w("怎么读：`external_kv_transfer` 变大、而 `local_compute` 保持 0 ⇒")
    w("        生成端**没有自己重算这段 prompt**，它的 KV 是从外面收进来的。")
    w("")
    w("本文件只做搬运：不计算差值、不判定通过与否、不含任何性能结论。")

    text = "\n".join(lines) + "\n"
    Path(args.out).write_text(text, encoding="utf-8")
    print(f"wrote {args.out} ({len(text)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

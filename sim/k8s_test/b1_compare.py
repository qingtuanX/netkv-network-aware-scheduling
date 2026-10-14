#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B1 的 4c 判据：把"拆分式"与"单机完整服务"的输出逐 token 比一遍。

判据（B1_PLAN.md §1 的 4c）：
    同一段 prompt、temperature=0、同一 max_tokens 下，
    **拆分式的输出与单机完整服务的输出逐 token 相同**。

怎么读这份报告：
  * `token_ids` 一栏是**最硬的证据**：它是模型实际吐出的词编号序列，
    不经过任何文本后处理，所以它相同就等于"逐 token 相同"。
  * 如果某一端没有返回 token_ids（服务没开 `return_token_ids`），
    就只能退到 `output_text` 逐字符比 —— 报告里会明确写出这一降级。
  * 只要有一条不一致，就打印**第一个不同的位置**，方便直接定位。

本脚本不做任何"挑好的报"：有几条就报几条，不一致就写不一致。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def first_diff(a, b):
    """返回第一个不同处的下标；完全相同返回 -1；长度不同且前缀相同返回较短者长度。"""
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return i
    return -1 if len(a) == len(b) else n


def compare_tokens(tb, td):
    i = first_diff(tb, td)
    if i == -1:
        return True, None
    ctx_lo = max(0, i - 5)
    return False, {
        "index": i,
        "baseline_tokens": tb[ctx_lo : i + 6],
        "disagg_tokens": td[ctx_lo : i + 6],
        "baseline_len": len(tb),
        "disagg_len": len(td),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="4c：逐 token 比对")
    ap.add_argument("--baseline", required=True, help="单机完整服务那次的 arm_*.json")
    ap.add_argument("--disagg", required=True, help="拆分式那次的 arm_*.json")
    ap.add_argument("--out", required=True, help="比对报告路径")
    args = ap.parse_args(argv)

    base = load(Path(args.baseline))
    dis = load(Path(args.disagg))

    br = {r["id"]: r for r in base["requests"]}
    dr = {r["id"]: r for r in dis["requests"]}
    ids = sorted(set(br) | set(dr))

    lines: list[str] = []
    w = lines.append

    w("=" * 78)
    w("4c 判据：拆分式输出 vs 单机完整服务输出（逐 token）")
    w("=" * 78)
    w(f"对照臂 : {base['target_url']}   /version={base.get('target_version_endpoint')}")
    w(f"拆分臂 : {dis['target_url']}    /version={dis.get('target_version_endpoint')}")
    w(f"模型   : {base.get('model')}     max_tokens={base.get('max_tokens')} "
      f"temperature={base.get('temperature')} seed={base.get('seed')}")
    w(f"请求数 : 对照 {len(br)} 条 / 拆分 {len(dr)} 条 / 并集 {len(ids)} 条")
    w("")

    have_token_ids = all(
        r.get("token_ids") for r in list(br.values()) + list(dr.values())
    )
    if have_token_ids:
        w("比对口径：**token_ids 逐位相同**（最硬的口径）")
    else:
        w("比对口径：有一端没返回 token_ids → **降级为 output_text 逐字符比对**")
        w("          （这是降级证据，报告里必须写明）")
    w("-" * 78)
    w(f"{'请求 id':<16}{'http 对照/拆分':>16}{'prompt_tok 对照/拆分':>24}"
      f"{'completion_tok 对照/拆分':>26}{'逐 token':>10}")
    w("-" * 78)

    n_same = 0
    n_diff = 0
    n_err = 0
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
            w(f"{i:<16}{str(b.get('http_status')) + '/' + str(d.get('http_status')):>16}"
              f"{'':>24}{'':>26}{'失败':>10}")
            detail.append(
                f"[{i}] HTTP 状态异常 baseline={b.get('http_status')} "
                f"({b.get('error_class')}: {(b.get('error_text') or '')[:200]}) / "
                f"disagg={d.get('http_status')} "
                f"({d.get('error_class')}: {(d.get('error_text') or '')[:200]})"
            )
            continue

        if have_token_ids:
            same, dd = compare_tokens(b["token_ids"], d["token_ids"])
        else:
            same = b.get("output_text") == d.get("output_text")
            dd = None

        n_same += int(same)
        n_diff += int(not same)
        w(f"{i:<16}{(str(b.get('http_status')) + '/' + str(d.get('http_status'))):>16}"
          f"{(str(b.get('prompt_tokens')) + '/' + str(d.get('prompt_tokens'))):>24}"
          f"{(str(b.get('completion_tokens')) + '/' + str(d.get('completion_tokens'))):>26}"
          f"{('相同' if same else '不同'):>10}")
        if not same:
            detail.append(f"[{i}] 输出不一致：{json.dumps(dd, ensure_ascii=False)}")
            if dd:
                detail.append(f"      对照臂 token: {dd['baseline_tokens']}")
                detail.append(f"      拆分臂 token: {dd['disagg_tokens']}")
                detail.append(f"      长度 对照/拆分 = {dd['baseline_len']}/{dd['disagg_len']}")
            else:
                bt = b.get("output_text") or ""
                dt = d.get("output_text") or ""
                j = first_diff(bt, dt)
                detail.append(f"      第一个不同字符位置 = {j}")
                detail.append(f"      对照臂文本片段: {bt[max(0, j - 40): j + 60]!r}")
                detail.append(f"      拆分臂文本片段: {dt[max(0, j - 40): j + 60]!r}")

    w("-" * 78)
    w("")
    w(f"逐 token 相同的请求数 : {n_same} / {len(ids)}")
    w(f"不同的请求数           : {n_diff}")
    w(f"没能比成的请求数       : {n_err}（缺数据 / HTTP 非 200）")
    w("")
    if n_diff == 0 and n_err == 0:
        w("结论：**4c 通过** —— 全部请求在" + ("token_ids" if have_token_ids else "output_text") + "口径上逐位相同。")
    else:
        w("结论：**4c 不通过** —— 见下面的逐条差异。不要重跑顶替，按记录纪律 R4 留档。")
    if detail:
        w("")
        w("逐条差异明细：")
        for line in detail:
            w("  " + line)
    w("")
    w("补充说明（读取时要注意）：")
    w("  1. 两端各自的启动参数见同目录 prefill_server.log / decode_server.log / "
      "baseline_server.log；")
    w("  2. 本文件只判【输出是否一致】，**不包含任何性能判断**。")

    text = "\n".join(lines)
    Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    return 0 if (n_diff == 0 and n_err == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())

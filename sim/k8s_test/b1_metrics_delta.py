#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B1 的 4b 判据：把 /metrics 里 **直方图** 的「样本数」与「字节总和」前后对比。

## 为什么不能用"指标出现过"当证据

`vllm:nixl_bytes_transferred` 这个名字听起来像计数器（counter），**其实它是直方图
（histogram）**。Prometheus 的直方图在 /metrics 里会展开成三条以上的时间序列：

    vllm:nixl_bytes_transferred_bucket{le="..."}   ← 分桶累计
    vllm:nixl_bytes_transferred_count              ← 样本数
    vllm:nixl_bytes_transferred_sum                ← 观测值总和

**官方说明：样本按"通知次数"统计，不等于请求次数。** 所以

  * `_count` 的增量 = 这一段时间里发生了多少次"KV 传输通知"（**不是**请求数）；
  * `_sum`   的增量 = 这一段时间里一共搬了多少字节（**这才是"传没传到"的证据**）。

只看"名字在不在"完全不能证明传输发生过：一个进程起来之后指标就会出现，值全是 0。
**必须比"运行前后的样本数与字节总和的变化"。**

## 本脚本的输出口径

1. 对每个被匹配到的指标族，按 **(指标名, 全部标签)** 分组，逐条列出 t0 / t1 / 增量；
2. 直方图按 `<base>_bucket|_count|_sum` 归并到 `<base>` 名下，并把 `le` 标签原样保留；
3. **三个判读**（互斥，写在报告顶部）：
     - `未出现`    = 两次快照都没有这个名字（**不是 0，是没这一项**）
     - `出现但未动` = 有名字，但 `_count` 与 `_sum` 的增量都是 0
     - `有增量`    = `_count` 和/或 `_sum` 变大了（这才是"传过了"的正证据）
4. 原始文本不加工：本脚本只读，不写回；raw 快照由 run_b1.sh 的 grab_metrics 原样落盘。

只依赖标准库。
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

# 直方图的三个后缀（Prometheus 约定）
HIST_SUFFIXES = ("_bucket", "_count", "_sum")

# 默认要盯住的族：只要能沾到 KV 传输的名字都列出来，避免"改名了就看不见"
DEFAULT_PATTERN = r"nixl|kv_transfer|kv_connector|kv_cache|prompt_tokens_by_source"

LINE_RE = re.compile(r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?P<labels>\{.*\})?\s+(?P<value>[^\s]+)\s*$")


def parse_metrics(path: Path) -> dict[tuple[str, str], float]:
    """把 /metrics 正文解析成 {(指标名, 标签原文): 数值}。

    标签原文**原样保留**（含顺序、含空格），因为"保留全部标签"是验收要求。
    """
    out: dict[tuple[str, str], float] = {}
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            m = LINE_RE.match(line)
            if not m:
                continue
            try:
                value = float(m.group("value"))
            except ValueError:
                # NaN / +Inf 等：原样记，但不能参与减法。用 nan 表示。
                if m.group("value") in ("NaN", "nan"):
                    value = float("nan")
                else:
                    continue
            out[(m.group("name"), m.group("labels") or "")] = value
    return out


def base_and_kind(name: str) -> tuple[str, str]:
    """把 `foo_sum` 拆成 (`foo`, `sum`)。不是直方图后缀的返回 (`name`, `plain`)。"""
    for suf in HIST_SUFFIXES:
        if name.endswith(suf) and len(name) > len(suf):
            return name[: -len(suf)], suf.lstrip("_")
    return name, "plain"


def fmt(v: float | None) -> str:
    if v is None:
        return "<无此序列>"
    if v != v:  # NaN
        return "NaN"
    if v == int(v) and abs(v) < 1e15:
        return str(int(v))
    return f"{v:.6g}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="B1 4b：/metrics 直方图增量比对")
    ap.add_argument("--t0", required=True, help="运行前的 /metrics 原始快照")
    ap.add_argument("--t1", required=True, help="运行后的 /metrics 原始快照")
    ap.add_argument("--out", required=True, help="报告输出路径")
    ap.add_argument("--label", default="(未命名)", help="这份报告对应谁，例如 P（前处理）")
    ap.add_argument("--pattern", default=DEFAULT_PATTERN, help="要盯住的族名正则")
    ap.add_argument(
        "--all-families",
        action="store_true",
        help="额外把 /metrics 里全部族名按字母序列一遍（查改名用）",
    )
    args = ap.parse_args(argv)

    t0p, t1p = Path(args.t0), Path(args.t1)
    rx = re.compile(args.pattern, re.IGNORECASE)

    lines: list[str] = []
    w = lines.append

    if not t0p.is_file() or not t1p.is_file():
        w(f"抓取失败：缺少快照 t0={t0p} 存在={t0p.is_file()} / t1={t1p} 存在={t1p.is_file()}")
        Path(args.out).write_text("\n".join(lines), encoding="utf-8")
        print("\n".join(lines))
        return 2

    a = parse_metrics(t0p)
    b = parse_metrics(t1p)

    w("=" * 78)
    w("4b 判据：KV 传输指标（直方图口径 —— 比样本数与字节总和的变化）")
    w("=" * 78)
    w(f"对象    : {args.label}")
    w(f"t0 快照 : {t0p.name}  ({t0p.stat().st_size} 字节, {len(a)} 条样本)")
    w(f"t1 快照 : {t1p.name}  ({t1p.stat().st_size} 字节, {len(b)} 条样本)")
    w(f"匹配正则: {args.pattern}")
    w("")
    w("读法：_count = 这段时间里发生了几次「KV 传输通知」（**不是请求数**）；")
    w("      _sum   = 这段时间里一共搬了多少字节（**这才是「传没传到」的证据**）。")
    w("      分桶 _bucket 的增量说明这次传输落在哪个大小档，属于辅助证据。")
    w("")

    keys = sorted(set(a) | set(b))
    matched = [(n, l) for (n, l) in keys if rx.search(n)]

    # 按族名归并：族名 = (base, kind)
    families: dict[str, list[tuple[str, str, float | None, float | None]]] = defaultdict(list)
    for name, lab in matched:
        base, kind = base_and_kind(name)
        families[base].append((kind, lab, a.get((name, lab)), b.get((name, lab))))

    verdicts: dict[str, str] = {}
    if not matched:
        w("判读：**未出现**")
        w("      —— 两份快照里没有任何一行 名字 命中上面的正则。")
        w("      ⚠️ 这**不是「传输量为 0」**，而是「这个版本里没有这一项」。")
        w("      处置见 B1_PLAN.md 停止条件 S2：先查是不是改名/开关没开，**不算 B1 失败**。")
    else:
        n_series = len(matched)
        w(f"命中 {n_series} 条样本序列，归并为 {len(families)} 个指标族：")
        w("")
        w(f"{'族名':<52}{'kind':>7}{'标签':<40}{'t0':>16}{'t1':>16}{'增量':>16}")
        w("-" * 140)
        for base in sorted(families):
            for kind, lab, v0, v1 in sorted(families[base], key=lambda x: (x[0], x[1])):
                if v0 is None or v1 is None:
                    delta = "<新增或消失>"
                    if v0 is None and v1 is not None:
                        delta = f"+{fmt(v1)}（t0 无此序列）"
                    elif v1 is None and v0 is not None:
                        delta = "（t1 无此序列）"
                else:
                    delta = fmt(v1 - v0)
                w(f"{base:<52}{kind:>7}{lab[:38]:<40}{fmt(v0):>16}{fmt(v1):>16}"
                  f"{str(delta):>16}")
        w("")
        w("【完整标签原文，不截断】逐条原样列出（上面那张表为了对齐会截断标签，本段不截断）：")
        for name, lab in matched:
            w(f"    {name}{lab}")
        w("")

        # ---- 逐族判读：count / sum 的增量 ----
        w("-" * 140)
        w("逐族判读（只看 sum 与 count；plain 族（无 _sum/_count 的普通计数器）单独看）：")
        w("")
        w(f"{'族名':<52}{'kind':>8}{'_count增量':>14}{'_sum增量':>16}{'判读':>24}")
        w("-" * 140)
        for base in sorted(families):
            has = defaultdict(dict)
            for kind, lab, v0, v1 in families[base]:
                has[kind][lab] = (v0, v1)
            dc = None
            ds = None
            for kind, lab, v0, v1 in families[base]:
                if kind == "count" and v0 is not None and v1 is not None:
                    dc = (dc or 0) + (v1 - v0)
                if kind == "sum" and v0 is not None and v1 is not None:
                    ds = (ds or 0) + (v1 - v0)
            if dc is None and ds is None:
                # plain 家族（例如某些 total 计数器）
                tot = 0.0
                anyv = False
                for kind, lab, v0, v1 in families[base]:
                    if v0 is not None and v1 is not None:
                        tot += v1 - v0
                        anyv = True
                v = "有增量" if (anyv and tot != 0) else ("出现但未动" if anyv else "证据不足")
                verdicts[base] = v
                w(f"{base:<52}{'plain':>8}{'—':>14}{fmt(tot) if anyv else '—':>16}{v:>24}")
                continue
            moved = (ds or 0) != 0 or (dc or 0) != 0
            v = "有增量" if moved else "出现但未动"
            verdicts[base] = v
            w(f"{base:<52}{'hist':>8}{fmt(dc) if dc is not None else '—':>14}"
              f"{fmt(ds) if ds is not None else '—':>16}{v:>24}")
        w("")

    # ---- 总判读 ----
    w("-" * 140)
    moved_fams = [f for f, v in locals().get("verdicts", {}).items() if v == "有增量"]
    if not matched:
        w("总体判读：**未出现 / 无法判定**（见上）")
    elif moved_fams:
        w(f"总体判读：**有增量** —— 下列族的数值在 t0→t1 之间变大了，传输确实发生过：")
        for f in moved_fams:
            w(f"    · {f}")
    else:
        w("总体判读：**出现但未动** —— 名字都在，但样本数与字节总和一点没变。")
        w("    ⚠️ 这仍然**不能**判「KV 已传到」：要么传输没触发，要么快照取早了/取错了端点。")
    w("")

    if args.all_families:
        w("-" * 140)
        w("附：/metrics 里全部族名（t1 快照，按名字排序；查「是不是改名了」用）")
        fams = sorted({base_and_kind(n)[0] for (n, _l) in b})
        for f in fams:
            mark = "  <=命中" if rx.search(f) else ""
            w(f"    {f}{mark}")
        w("")

    w("备注：本文件只做「传输有没有发生」的判读，**不含任何性能结论**。")

    text = "\n".join(lines)
    Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    return 0 if moved_fams else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""用 Crossref 给 `refs.bib` 回填 DOI（只回填核实到的，绝不编造）。

用法（在仓库根执行）：

    sim/.venv/Scripts/python.exe paper/backfill_dois.py            # 预览，不写文件
    sim/.venv/Scripts/python.exe paper/backfill_dois.py --write    # 实际写入

为什么需要：多数期刊（含 IEEE 与 Elsevier 系）现在要求参考文献带 DOI；
而"补 DOI"这件事**不能靠猜**——编一个 DOI 比留空更糟。Crossref 是本机可达的权威元数据源，
因此用它按标题检索、只在标题高度相似（默认 ≥0.90）时才回填。

跳过：`@misc` 且带 `eprint` 的 arXiv 预印本（它们的 DOI 形式不是 Crossref 那种），
以及项目/工具类条目（dynamo、llmd 等）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parent
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                        # noqa: BLE001
    pass

ENTRY_RE = re.compile(r"(@\w+\{)([^,]+),(.*?)(\n\})", re.S)
API = ("https://api.crossref.org/works?rows=3&select=title,DOI,author,issued,"
       "container-title,type,page,volume&query.bibliographic=")


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower()).strip()


def query(title: str) -> list[dict]:
    req = urllib.request.Request(API + urllib.parse.quote(title),
                                 headers={"User-Agent": "ref-doi-backfill/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode("utf-8", "replace")).get(
                "message", {}).get("items", [])
    except Exception:                                    # noqa: BLE001
        return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="真正写回 refs.bib")
    ap.add_argument("--min-ratio", type=float, default=0.90)
    args = ap.parse_args()

    path = ROOT / "refs.bib"
    text = path.read_text(encoding="utf-8")
    added, skipped, failed = [], [], []

    def handle(m: re.Match) -> str:
        head, key, body, tail = m.group(1), m.group(2).strip(), m.group(3), m.group(4)
        tm = re.search(r"title\s*=\s*\{(.*?)\}\s*,?", body, re.S)
        if not tm:
            return m.group(0)
        title = re.sub(r"[{}]", "", tm.group(1))
        title = " ".join(title.split())
        if "doi" in body.lower():
            skipped.append(f"{key}（已有 doi）")
            return m.group(0)
        if "eprint" in body or key in ("dynamo", "llmd", "mlperf", "azuretrace", "burstgpt"):
            skipped.append(f"{key}（预印本/项目，跳过）")
            return m.group(0)
        items = query(title)
        best, score = None, 0.0
        for it in items:
            t = (it.get("title") or [""])[0]
            s = SequenceMatcher(None, norm(title), norm(t)).ratio()
            if s > score:
                best, score = it, s
        if not best or score < args.min_ratio or not (best.get("DOI") or best.get("doi")):
            failed.append(f"{key}（最高相似度 {score:.2f}）")
            return m.group(0)
        # 【三重一致性校验】只凭标题相似度回填是危险的：实测出现过
        #   标题 1.00 相似、但 DOI 指向 Springer 图书章节 / Bloomsbury 图书 /
        #   AccessScience 百科条目（`mooncake` / `orca` / `libra` 都中过招）。
        # 所以要求：① 类型是论文（期刊或会议）；② 有场馆名；③ 年份与条目一致（±1）。
        kind = (best.get("type") or "").lower()
        venue = " ".join((best.get("container-title") or [""])[0].split())
        cr_year = ((best.get("issued", {}).get("date-parts") or [[None]])[0] or [None])[0]
        ym = re.search(r"year\s*=\s*\{(\d{4})\}", body)
        my_year = int(ym.group(1)) if ym else None
        why = None
        if kind not in ("proceedings-article", "journal-article"):
            why = f"类型是 {kind}"
        elif not venue:
            why = "Crossref 无场馆名"
        elif my_year and cr_year and abs(cr_year - my_year) > 1:
            why = f"年份不符（条目 {my_year} vs Crossref {cr_year}）"
        if why:
            failed.append(f"{key}（{why}；标题相似 {score:.2f}）")
            return m.group(0)
        doi = best.get("DOI") or best.get("doi")
        extra = f",\n  doi       = {{{doi}}}"
        if best.get("page") and "pages" not in body:
            extra += f",\n  pages     = {{{best['page']}}}"
        if best.get("volume") and "volume" not in body:
            extra += f",\n  volume    = {{{best['volume']}}}"
        added.append(f"{key:<16} {doi}  [{venue} {cr_year}] ({score:.2f})")
        return head + key + "," + body + extra + tail

    new = ENTRY_RE.sub(handle, text)
    print(f"可回填 {len(added)} 条：")
    for a in added:
        print("  +", a)
    if failed:
        print(f"\n未能核实（保持原样，需人工补）{len(failed)} 条：")
        for f in failed:
            print("  ?", f)
    if skipped:
        print(f"\n跳过 {len(skipped)} 条：" + ", ".join(s.split("（")[0] for s in skipped))
    if args.write:
        path.write_text(new, encoding="utf-8")
        print(f"\n已写回 {path}")
    else:
        print("\n（预览模式；加 --write 才会写文件）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

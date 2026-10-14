"""用 Crossref 逐条核实 `paper/refs.bib` 里的文献（作者/标题/年份/场馆）。

用法（在仓库根执行）：

    sim/.venv/Scripts/python.exe paper/verify_refs.py

为什么要单独做：本仓库收了很多 2025–2026 的预印本与"经典但只见过二手引用"的条目，
其中一部分的**作者名没有独立核实过**。写论文时**宁可留空也不能编**——
编出来的作者名是学术不端，而留空只是不好看。这个脚本把"能核实的核实掉"，
把"核实不了的"明确列出来，交给作者人工去查。

Crossref 是本机唯一稳定可达的权威元数据源（arXiv API 也可用；
Google/Bing/DuckDuckGo 在本机不通，Semantic Scholar 常被限流）。

输出：每条目一行——`[OK]` 表示 Crossref 命中且标题高度相似（附上作者与年份），
`[?]` 表示命中但标题差异较大或未命中，需要人工判断。
"""
from __future__ import annotations

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
except Exception:                                            # noqa: BLE001
    pass

ENTRY_RE = re.compile(r"@(\w+)\{([^,]+),(.*?)\n\}", re.S)
FIELD_RE = re.compile(r"(\w+)\s*=\s*\{(.*?)\}\s*,?", re.S)
API = "https://api.crossref.org/works?rows=3&select=title,author,issued,container-title,DOI,type&query.bibliographic="


def parse_bib(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    out = []
    for m in ENTRY_RE.finditer(text):
        kind, key, body = m.group(1), m.group(2).strip(), m.group(3)
        fields = {k.lower(): v.strip() for k, v in FIELD_RE.findall(body)}
        out.append({"kind": kind, "key": key, **fields})
    return out


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower()).strip()


def crossref(title: str) -> list[dict]:
    url = API + urllib.parse.quote(title)
    req = urllib.request.Request(url, headers={"User-Agent": "paper-ref-check/1.0 (mailto:author@example.org)"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        return data.get("message", {}).get("items", [])
    except Exception as e:                                   # noqa: BLE001
        return [{"_err": str(e)}]


def main() -> int:
    entries = parse_bib(ROOT / "refs.bib")
    print(f"refs.bib 条目：{len(entries)}\n")
    need = []
    for e in entries:
        title = e.get("title", "")
        if not title:
            continue
        items = crossref(title)
        if items and "_err" in items[0]:
            print(f"[网络] {e['key']}: Crossref 查询失败（{items[0]['_err']}）")
            need.append(e["key"])
            continue
        best, score = None, 0.0
        for it in items:
            t = (it.get("title") or [""])[0]
            s = SequenceMatcher(None, norm(title), norm(t)).ratio()
            if s > score:
                best, score = it, s
        if best is None or score < 0.80:
            print(f"[?]   {e['key']:<16} 标题差异大/未命中（最高 {score:.2f}）")
            if best:
                print(f"        Crossref 最接近：{(best.get('title') or [''])[0][:90]}")
            need.append(e["key"])
            continue
        auth = best.get("author") or []
        names = ", ".join(
            (a.get("family") or "") + (" " + a.get("given", "")[:1] + "." if a.get("given") else "")
            for a in auth[:4]) or "（Crossref 也无作者字段）"
        year = (best.get("issued", {}).get("date-parts") or [[None]])[0][0]
        venue = (best.get("container-title") or [""])[0]
        have_auth = bool(e.get("author"))
        flag = "OK " if have_auth else "缺作者"
        print(f"[{flag}] {e['key']:<16} {names[:60]}  {year}  {venue[:35]}")
        if not have_auth:
            need.append(e["key"])
    print("\n需要人工处理的条目：" + (", ".join(need) if need else "无"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

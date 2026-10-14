"""扫描候选期刊：用 Crossref 统计它们**实际发表过多少**与本文方向相关的论文。

用法（在仓库根执行）：

    sim/.venv/Scripts/python.exe paper/journal_scan.py

为什么要这么做：选刊不能凭印象。同一个"CCF-B / SCI 二区"档位里，有的期刊偏爱
系统实现、有的偏爱测量与刻画。最便宜的判别方法是**看它最近两年真发过什么**：
如果一本期刊 2024 年以来一篇 LLM 服务/网络感知调度/遥测新鲜度的文章都没有，
把这篇投过去就要冒"审稿人不熟悉"的风险；反之则说明选题在它的舒适区内。

方法：
  1. 按刊名在 Crossref 解析 ISSN（避免我手写 ISSN 出错）；
  2. 对每本期刊，用若干**主题检索式**统计 2024-01-01 之后的命中数；
  3. 同时打印该刊的总发文量作为分母（命中数/总量才是"浓不浓"）。

工具限制（照实说）：Crossref 的检索是**元数据全文模糊匹配**，不是主题分类，
所以命中数只能当"数量级信号"，不能当精确统计；地理可达性受限的期刊（部分 IEEE 刊）
可能因索引延迟而偏少。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                        # noqa: BLE001
    pass

CANDIDATES = [
    ("Computer Networks", "Elsevier"),
    ("IEEE Transactions on Network and Service Management", "IEEE"),
    ("IEEE Transactions on Cloud Computing", "IEEE"),
    ("IEEE Transactions on Parallel and Distributed Systems", "IEEE"),
    ("IEEE Transactions on Services Computing", "IEEE"),
    ("Journal of Systems Architecture", "Elsevier"),
    ("Future Generation Computer Systems", "Elsevier"),
    ("Performance Evaluation", "Elsevier"),
    ("Journal of Parallel and Distributed Computing", "Elsevier"),
    ("ACM Transactions on Modeling and Performance Evaluation of Computing Systems", "ACM"),
    ("Cluster Computing", "Springer"),
    ("Journal of Cloud Computing", "Springer"),
]

QUERIES = [
    ("LLM/inference serving", "large language model inference serving scheduling"),
    ("network-aware scheduling", "network aware scheduling datacenter"),
    ("staleness/telemetry", "stale telemetry monitoring freshness scheduling"),
]
FROM = "2024-01-01"
UA = {"User-Agent": "journal-scan/1.0 (paper targeting; mailto:author@example.org)"}


def get(url: str) -> dict:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def resolve_issn(title: str) -> tuple[str | None, str | None]:
    """按刊名解析 ISSN 与发文量（用 Crossref 的 journals 端点）。"""
    try:
        d = get("https://api.crossref.org/journals?rows=3&query=" + urllib.parse.quote(title))
        items = d["message"]["items"]
    except Exception:                                    # noqa: BLE001
        return None, None
    best, score = None, 0.0
    want = title.lower()
    for it in items:
        t = (it.get("title") or "").lower()
        # 简单包含式匹配：刊名高度重合才认
        common = len(set(want.split()) & set(t.split())) / max(len(set(want.split())), 1)
        if common > score:
            best, score = it, common
    if not best or score < 0.6:
        return None, None
    issns = best.get("ISSN") or []
    return (issns[0] if issns else None), best.get("title")


def count(issn: str, q: str) -> int | None:
    url = ("https://api.crossref.org/works?rows=0&filter="
           f"issn:{issn},from-pub-date:{FROM},type:journal-article&" +
           "query.bibliographic=" + urllib.parse.quote(q))
    try:
        return get(url)["message"]["total-results"]
    except Exception:                                    # noqa: BLE001
        return None


def main() -> int:
    print(f"检索窗口：{FROM} 起（Crossref 索引可能滞后数月）\n")
    header = f"{'期刊':<52}{'ISSN':<12}" + "".join(f"{n[:14]:>16}" for n, _ in QUERIES)
    print(header)
    print("-" * len(header))
    for title, pub in CANDIDATES:
        issn, real = resolve_issn(title)
        if not issn:
            print(f"{title[:50]:<52}{'未解析':<12}")
            continue
        cells = []
        for _, q in QUERIES:
            c = count(issn, q)
            cells.append("查询失败" if c is None else str(c))
            time.sleep(0.3)
        print(f"{real[:50]:<52}{issn:<12}" + "".join(f"{c:>16}" for c in cells))
    print("\n读法：数字是 2024 年以来的**期刊论文**命中数（模糊匹配，只看数量级）。")
    print("      某一列全为 0 的期刊，说明该主题不在它近期的主流里，投过去要冒审稿人不熟的风险。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

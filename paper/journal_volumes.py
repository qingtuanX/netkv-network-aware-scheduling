"""给候选期刊做"预警风险"的可核实信号：**逐年发文量**。

用法（在仓库根执行）：

    sim/.venv/Scripts/python.exe paper/journal_volumes.py

为什么看这个：中科院文献情报中心的《国际期刊预警名单》把"**发文量异常增长**"
（以及自引异常、撤稿异常等）作为主要触发条件之一。发文量可以公开核实，
自引率/撤稿率不能（需要 Web of Science 或期刊自报数据）。

**重要边界**：发文量增长只是**风险信号**，不等于"在预警名单上"。
在不在名单上必须以中科院文献情报中心公布的当年名单（以及你所在学校科研处的版本）为准。
本脚本帮你在"要不要花时间核实某本刊"这件事上排个优先级。
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

JOURNALS = [
    ("Computer Networks", "1389-1286"),
    ("IEEE Trans. Network and Service Management", "1932-4537"),
    ("IEEE Trans. Cloud Computing", "2168-7161"),
    ("IEEE Trans. Parallel and Distributed Systems", "1045-9219"),
    ("IEEE Trans. Services Computing", "1939-1374"),
    ("Journal of Systems Architecture", "1383-7621"),
    ("Future Generation Computer Systems", "0167-739X"),
    ("Performance Evaluation", "0166-5316"),
    ("Journal of Parallel and Distributed Computing", "0743-7315"),
    ("ACM TOMPECS", "2376-3639"),
    ("Cluster Computing", "1386-7857"),
    ("Journal of Cloud Computing", "2192-113X"),
]
YEARS = [2019, 2020, 2021, 2022, 2023, 2024]
UA = {"User-Agent": "journal-volume-scan/1.0 (mailto:author@example.org)"}


def count_year(issn: str, year: int) -> int | None:
    url = ("https://api.crossref.org/works?rows=0&filter="
           f"issn:{issn},from-pub-date:{year}-01-01,until-pub-date:{year}-12-31,"
           "type:journal-article")
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8", "replace"))["message"]["total-results"]
    except Exception:                                    # noqa: BLE001
        return None


def main() -> int:
    print("逐年期刊论文数（Crossref，按发表日期）")
    print(f"{'期刊':<46}" + "".join(f"{y:>8}" for y in YEARS) + f"{'23→24':>10}")
    print("-" * (46 + 8 * len(YEARS) + 10))
    for name, issn in JOURNALS:
        row = []
        for y in YEARS:
            row.append(count_year(issn, y))
            time.sleep(0.2)
        nz = [v for v in row if v]
        growth = ""
        if len(row) >= 2 and row[-1] and row[-2]:
            g = (row[-1] - row[-2]) / row[-2] * 100
            growth = f"{g:+.0f}%"
        cells = "".join(f"{(v if v is not None else 0):>8}" for v in row)
        print(f"{name:<46}{cells}{growth:>10}")
    print("\n读法：")
    print("  · 年发文量**突然成倍增长**是预警名单的主要触发信号之一（另一类是自引/撤稿异常）；")
    print("  · 但增长本身**不等于**被预警——综述型/开放获取型期刊正常增长也会很快；")
    print("  · 最终必须核对你所在学校科研处发布的**当年**名单。")
    print("\n注意：Crossref 对最新年份可能索引不全，最后一列偏低属正常。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

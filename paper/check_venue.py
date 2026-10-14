"""查刊：一次给出候选期刊的「国际预警名单 / CCF 等级 / 中科院分区 / 影响因子」。

用法（在仓库根执行）：

    sim/.venv/Scripts/python.exe paper/check_venue.py                     # 查内置候选列表
    sim/.venv/Scripts/python.exe paper/check_venue.py "Computer Networks" # 查指定刊名（可多个）

数据来源（本地 CSV，放在 `文献/期刊分区数据/`，从开源项目 `hitfyd/ShowJCR` 取得，
其上游指向官方发布源）：
  · `GJQKYJMD{2020,2021,2023,2024,2025}.csv` —— 中科院文献情报中心《国际期刊预警名单》
    （**表内只有被预警的期刊**，一年几十条；查不到 = 该年不在名单上）
  · `CCF2026-UTF8.csv` —— CCF《推荐国际学术会议和期刊目录》2026 版
  · `FQBJCR2025-UTF8.csv` —— 中科院期刊分区表升级版 2025（全量）
  · `JCR2024-UTF8.csv` —— JCR 2024 影响因子与分区

【为什么要有这个脚本】"某刊在不在预警名单上""是不是 CCF-B"这类问题**不能凭记忆答**：
名单每年更新、各校还会另加条目，记错会让人白投一轮。这里把判断落到**可核对的表**上，
并打印命中的原始行，便于人工复核。
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # 仓库根
DATA = ROOT / "文献" / "期刊分区数据"
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                        # noqa: BLE001
    pass

# 本文的候选期刊（按上次讨论的清单）
CANDIDATES = [
    "COMPUTER NETWORKS",
    "IEEE TRANSACTIONS ON NETWORK AND SERVICE MANAGEMENT",
    "IEEE TRANSACTIONS ON CLOUD COMPUTING",
    "IEEE TRANSACTIONS ON PARALLEL AND DISTRIBUTED SYSTEMS",
    "IEEE TRANSACTIONS ON SERVICES COMPUTING",
    "JOURNAL OF SYSTEMS ARCHITECTURE",
    "FUTURE GENERATION COMPUTER SYSTEMS",
    "PERFORMANCE EVALUATION",
    "JOURNAL OF PARALLEL AND DISTRIBUTED COMPUTING",
    "ACM TRANSACTIONS ON MODELING AND PERFORMANCE EVALUATION OF COMPUTING SYSTEMS",
    "CLUSTER COMPUTING",
    "JOURNAL OF CLOUD COMPUTING",
    "COMPUTER COMMUNICATIONS",
    "IEEE INTERNET OF THINGS JOURNAL",
    "IEEE TRANSACTIONS ON COMPUTERS",
]

WARN_YEARS = [2020, 2021, 2023, 2024, 2025]


def load_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def norm(s: str) -> str:
    return " ".join((s or "").upper().replace("&", " AND ").split())


def lookup(table: dict, name: str):
    """先精确匹配，再退到「包含」匹配。

    为什么要退一步：中科院分区表里存的是**期刊全称**（常带副标题，例如
    `FUTURE GENERATION COMPUTER SYSTEMS-THE INTERNATIONAL JOURNAL OF ESCIENCE`），
    而人一般只写主名。只做精确匹配会得到"未命中"，看起来像"这本刊不在分区表里"，
    其实是查法不对——**这种假阴性会误导选刊**。
    返回 `(命中行, 是否精确)`。
    """
    k = norm(name)
    if k in table:
        return table[k], True
    for key, row in table.items():
        if key.startswith(k) or k in key:
            return row, False
    return None, False


def main() -> int:
    wanted = sys.argv[1:] or CANDIDATES
    warns = {y: load_csv(DATA / f"GJQKYJMD{y}.csv") for y in WARN_YEARS}
    ccf = load_csv(DATA / "CCF2026-UTF8.csv")
    fqb = load_csv(DATA / "FQBJCR2025-UTF8.csv")
    jcr = load_csv(DATA / "JCR2024-UTF8.csv")
    if not any(warns.values()) and not ccf:
        print(f"找不到数据文件，请确认 {DATA} 下有 GJQKYJMD*/CCF2026/FQBJCR2025/JCR2024 等 CSV")
        return 2

    warn_keys = {y: {norm(r.get("Journal", "")): (r.get("预警原因（%d）" % y)
                                                  or list(r.values())[-1]) for r in rows}
                 for y, rows in warns.items()}
    ccf_keys = {norm(r.get("Journal", "")): r for r in ccf}
    fqb_keys = {norm(r.get("Journal", "")): r for r in fqb}
    jcr_keys = {norm(r.get("Journal", "")): r for r in jcr}

    print(f"{'期刊':<52}{'预警':<18}{'CCF':<6}{'中科院':<16}{'JCR IF':<10}")
    print("-" * 104)
    for name in wanted:
        k = norm(name)
        hit = [str(y) for y in WARN_YEARS if k in warn_keys[y]]
        warn_s = ("⚠ " + ",".join(hit)) if hit else "未列入(共5版)"
        c, _ = lookup(ccf_keys, name)
        ccf_s = (c or {}).get("CCF推荐类型", "—")
        f, exact = lookup(fqb_keys, name)
        if f:
            fqb_s = f"{f.get('大类分区','?')}区/{f.get('Top','?')}" + ("" if exact else "~")
        else:
            fqb_s = "未命中"
        j, _ = lookup(jcr_keys, name)
        jcr_s = (j or {}).get("IF(2024)", "—")
        print(f"{name[:50]:<52}{warn_s:<18}{ccf_s:<6}{fqb_s:<16}{jcr_s:<10}")

    print("\n说明：")
    print("  · 「预警」列写 `未列入(共5版)` = 2020/2021/2023/2024/2025 五版名单里都没有该刊；")
    print("    但**你所在学校可能另加条目**，最终以校方当年文件为准。")
    print("  · 「中科院」列是分区表升级版 2025 的**大类分区**与是否 Top；带 `~` = 按全称模糊匹配到的")
    print("    （分区表存的是带副标题的期刊全称）；`未命中` 才是真的没查到。")
    print("  · CCF 目录为 2026 版；`—` 表示该刊不在 CCF 推荐目录中（不代表水平低，只是不收录）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

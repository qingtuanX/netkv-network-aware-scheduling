"""实验数据清点与备份 —— 把"保留实验数据"变成可重复、可校验的动作。

为什么要专门有这一步：
    1. 这个仓库**没有任何 git 提交**，所以代码没有任何版本记录；
       `manifest.code_version` 至今是 `"uncommitted"`。只备份 CSV 而不备份代码，
       等于存下了数字却丢了产生数字的程序。
    2. `results/` 下的原始数据是 append-only 证据：不删除、不手工修改，
       但**磁盘本身会出事**（误删、断电、坏道）。唯一可靠的保护是异地副本 + 校验。

本脚本做三件事：
    清点  →  对每个文件算 sha256/大小/修改时间，写 `sim/results/ARTIFACTS.csv`
    备份  →  按同样的相对路径复制到 `--dest`（默认 D 盘），包括代码快照与文档
    校验  →  对副本**重新哈希**逐个比对，报告不一致项

被矩阵正在追加写的文件会被标记 `in_progress=True`；这类文件在复制过程中可能变大，
校验会报"大小不符"，属预期——等矩阵收尾后再跑一次即可。

用法：
    sim/.venv/Scripts/python.exe backup_artifacts.py
    sim/.venv/Scripts/python.exe backup_artifacts.py --dest "D:/论文实验数据备份_20260923"
    sim/.venv/Scripts/python.exe backup_artifacts.py --verify-only     # 只校验已有备份
产物：
    sim/results/ARTIFACTS.csv    证据清单（路径/大小/UTC 修改时间/sha256/是否在写）
    {dest}/ARTIFACTS.csv         同一份清单，随备份一起存放
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent       # 论文/（本脚本在 sim/ 下，要往上一级）
sys.path.insert(0, str(ROOT / "sim"))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

# 要备份的东西：实验产物 + 图 + 原始 trace + **代码快照** + 关键文档
TREE_ROOTS = [
    ("results", ROOT / "sim/results"),
    ("figures", ROOT / "sim/figures"),
    ("figures_trace", ROOT / "sim/figures_trace"),
    ("figures_burstgpt", ROOT / "sim/figures_burstgpt"),
    ("figures_v2", ROOT / "sim/figures_v2"),
    ("figures_trace_v2", ROOT / "sim/figures_trace_v2"),
    ("figures_burstgpt_v2", ROOT / "sim/figures_burstgpt_v2"),
    ("data/raw", ROOT / "sim/data/raw"),
    ("code", ROOT / "sim/src"),
    ("code", ROOT / "sim/experiments"),
    ("code", ROOT / "sim/analysis"),
    ("code", ROOT / "sim/configs"),
    # ⚠️ 2026-10-08 补：下面两项原来**完全不在清单里**，而它们是本次会话的**全部工作**。
    #   `k8s_test/` 里是真实系统实测（阶段 A / B0 / B1 / 4c）的全部脚本、预注册、报告与产物；
    #   `paper/` 是论文 LaTeX 工程。少了它们，备份里就只剩"数字"而没有"产生数字的程序与结论记录"。
    ("code", ROOT / "sim/k8s_test"),
    ("code", ROOT / "paper"),
]
EXTRA_FILES = [
    "AGENTS.md", "论文规划.md", "sim/RESUME.md", "sim/README.md",
    "sim/requirements.txt", "sim/run_min.py", "sim/inspect_trace.py",
    "sim/probe_rq1.py", "sim/backup_artifacts.py", "sim/rerun_missing_v2.sh",
    ".kimi-code/skills/research-experiment/SKILL.md",
    # 2026-10-08 补：根目录的规划/证据文档，以及真实系统实测那份计划书。
    # 它们不在 `sim/` 下，所以原来一条都没被备份。
    "论文草稿.md", "论文证据索引.md", "论文写作与从零学习规划.md",
    "学习什么.md", "真实跨云LLM服务研究计划书.md",
    "文献/MANIFEST.md", "文献/08-竞争定位（2025-2026）.md",
]
FIELDS = ["group", "rel_path", "bytes", "mtime", "sha256", "in_progress"]


def sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def inventory() -> list[dict]:
    now = time.time()
    rows: list[dict] = []
    for label, root in TREE_ROOTS:
        if not root.exists():
            print(f"  [跳过] {root} 不存在")
            continue
        for p in sorted(root.rglob("*")):
            if not p.is_file() or "__pycache__" in p.parts or p.name == ".gitkeep":
                continue
            # 跳过敏清单自身：它一边写一边算哈希，必然自相矛盾（复制后大小就变了）
            if p.name == "ARTIFACTS.csv":
                continue
            rows.append(_row(label, p, now))
    for rel in EXTRA_FILES:
        p = ROOT / rel
        if p.exists():
            rows.append(_row("docs", p, now))
    return rows


def _row(label: str, p: Path, now: float) -> dict:
    st = p.stat()
    return {"group": label, "rel_path": p.relative_to(ROOT).as_posix(),
            "bytes": st.st_size,
            "mtime": datetime.fromtimestamp(st.st_mtime, timezone.utc)
                            .isoformat(timespec="seconds"),
            "sha256": sha256(p), "in_progress": (now - st.st_mtime) < 600}


def main() -> int:
    ap = argparse.ArgumentParser(description="实验数据清点 / 备份 / 校验")
    ap.add_argument("--dest", default="D:/论文实验数据备份_20260923",
                    help="备份目标根目录（默认 D 盘；应与源盘不同）")
    ap.add_argument("--verify-only", action="store_true",
                    help="只校验已有备份，不重新清点与复制")
    args = ap.parse_args()
    dest = Path(args.dest)
    manifest_rel = "sim/results/ARTIFACTS.csv"

    if args.verify_only:
        src = ROOT / manifest_rel
        if not src.exists():
            print(f"找不到清单 {src}，先跑一次不带 --verify-only 的备份")
            return 2
        with open(src, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    else:
        print("清点中……")
        rows = inventory()
        total = sum(int(r["bytes"]) for r in rows)
        n_prog = sum(1 for r in rows if r["in_progress"])
        print(f"  {len(rows)} 个文件，共 {total/1e6:.1f} MB，其中 {n_prog} 个疑似仍在写")
        out = ROOT / manifest_rel
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            w.writerows(rows)
        print(f"  清单 → {out}")

    print(f"备份到 {dest} ……")
    dest.mkdir(parents=True, exist_ok=True)
    for r in rows:
        src, dst = ROOT / r["rel_path"], dest / r["rel_path"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    shutil.copy2(ROOT / manifest_rel, dest / "ARTIFACTS.csv")
    print(f"  复制完成 {len(rows)} 个文件")

    print("校验（对副本重新哈希逐个比对）：")
    bad = []
    for r in rows:
        dst = dest / r["rel_path"]
        if not dst.exists():
            bad.append(f"缺失 {r['rel_path']}")
        elif dst.stat().st_size != int(r["bytes"]):
            bad.append(f"大小不符 {r['rel_path']}（清单 {r['bytes']} → 实际 {dst.stat().st_size}）")
        elif sha256(dst) != r["sha256"]:
            bad.append(f"哈希不符 {r['rel_path']}")
    if bad:
        print(f"  ⚠ {len(bad)} 个不一致（若都在结果目录且刚被矩阵写过，属预期）：")
        for b in bad[:20]:
            print("   ", b)
    else:
        print("  全部一致 ✓")
    print(f"\n备份位置：{dest}")
    print(f"恢复方式：把 {dest} 下的 sim/ 与 *.md 复制回仓库根目录即可（路径结构一致）")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())

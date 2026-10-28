
# -*- coding: utf-8 -*-
"""Build the data inventory: every artefact behind the paper, plus the later supplementary experiments."""
import csv, hashlib, os, re
from pathlib import Path
from datetime import datetime

ROOT = Path(r"E:\代码项目文件\论文")
RES = ROOT / "sim" / "results"

def sha256(p: Path, cap=8 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
    return h.hexdigest()

def rows(p: Path):
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            n = sum(1 for _ in f) - 1
        return max(n, 0)
    except Exception:
        return ""

MAIN = {
    "matrix": "主矩阵 v1（合成负载）：120 cells × 4 策略",
    "matrix_trace": "主矩阵 v1（Azure trace）",
    "matrix_burstgpt": "主矩阵 v1（BurstGPT）",
    "matrix_v2": "主矩阵 v2（链路 FIFO 争用 + NetworkAware2，合成）",
    "matrix_trace_v2": "主矩阵 v2（Azure）",
    "matrix_burstgpt_v2": "主矩阵 v2（BurstGPT）",
    "analysis": "v1 合成：表 + report.txt",
    "analysis_trace": "v1 Azure：表 + report.txt",
    "analysis_burstgpt": "v1 BurstGPT：表 + report.txt",
    "analysis_v2": "v2 合成：论文引用的全部 CSV",
    "analysis_trace_v2": "v2 Azure",
    "analysis_burstgpt_v2": "v2 BurstGPT",
    "verification": "四道门 + 白盒/黑盒验证记录",
    "k8s_test": "真实 k3s 单节点：逐请求明细",
    "b0": "真实系统 B0：组件级实测",
    "b1": "真实系统 B1：跨云链路",
    "b2": "真实系统 B2：同云 prefill/decode 分离",
    "c": "真实跨云 C 阶段：两方向 KV",
    "stage_a": "传输层阶段 A 验证电池",
    "raw": "run_min / probe_rq1 的每请求明细（早期）",
}

def describe(name: str) -> str:
    for k, v in MAIN.items():
        if name == k:
            return v
    if name.startswith("matrix_cons"):
        return "补充：一致性/概率校准批"
    if name.startswith("matrix_equtil") or name.startswith("matrix_eqU"):
        return "补充：等利用率/方法对照批"
    if name.startswith("matrix_linkonly"):
        return "补充：link-only 降权检验"
    if name.startswith("analysis_cons"):
        return "补充：一致性批分析"
    if name.startswith("analysis_equtil") or name.startswith("analysis_rq2_cal"):
        return "补充：等利用率 / RQ2 校准分析"
    if name.startswith("_smoke"):
        return "冒烟测试（不引用）"
    if name.startswith("stage_a"):
        return "阶段 A 传输验证"
    return "其他"

out_rows = []
for d in sorted(RES.iterdir()):
    if not d.is_dir():
        continue
    files = [p for p in d.rglob("*") if p.is_file()]
    if not files:
        continue
    size = sum(p.stat().st_size for p in files)
    csvs = [p for p in files if p.suffix.lower() == ".csv"]
    total_rows = 0
    for p in csvs:
        r = rows(p)
        if isinstance(r, int):
            total_rows += r
    newest = max(p.stat().st_mtime for p in files)
    out_rows.append({
        "dir": str(d.relative_to(ROOT)),
        "what": describe(d.name),
        "files": len(files),
        "csv": len(csvs),
        "rows": total_rows,
        "MB": round(size / 1e6, 1),
        "newest": datetime.fromtimestamp(newest).strftime("%Y-%m-%d"),
    })

# key files
key_files = []
for pat in ["paper_numbers.txt", "ARTIFACTS.csv"]:
    p = RES / pat
    if p.exists():
        key_files.append((str(p.relative_to(ROOT)), p.stat().st_size, sha256(p)))

inv = ROOT / "sim" / "results" / "data_inventory.csv"
with open(inv, "w", newline="", encoding="utf-8-sig") as f:
    w = csv.DictWriter(f, fieldnames=["dir", "what", "files", "csv", "rows", "MB", "newest"])
    w.writeheader()
    w.writerows(out_rows)

tot_mb = sum(r["MB"] for r in out_rows)
print("dirs:", len(out_rows), "| total MB:", round(tot_mb, 1))
for r in out_rows:
    print("  %-28s %5d files %8.1f MB %10d rows  %s" % (r["dir"], r["files"], r["MB"], r["rows"], r["what"]))
print("inventory:", inv)
for k in key_files:
    print("key:", k)

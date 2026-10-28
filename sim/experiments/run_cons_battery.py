# -*- coding: utf-8 -*-
"""保守模型电池驱动（CONS_MODEL_PREREG §1.2 范围的超集执行）。

L（linear）/ P（pressure）× 三批负载 × 6 个 mode 全量：
  main / rho / align / alignscale / datavolume / bandwidth
产物：results/matrix_consL_{synthetic,azure,burstgpt}/、matrix_consP_{...}/（每批独立目录；
runs.csv 自带 service_slowdown 列）。三批混在一个目录会让 analyze.py 每个 mode 只取最新
run_id、静默只分析最后跑的那批（2026-10-20 实际发生，见 CONS_BATTERY_CLEANUP_2026-10-20.md §6）。
原则：全量超集 = 零挑选；同种子同请求流；只改服务时间倍率。
（同名 .sh 保留为命令清单文档；WSL bash 无法执行 Windows venv 解释器，
 故真正驱动器是本文件。）

用法：.venv/Scripts/python.exe experiments/run_cons_battery.py [all|linear|pressure]
"""
from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Windows 控制台默认可能是 GBK；中文输出不重配会乱码（AGENTS.md 的入口脚本约定）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
MODES = ("main", "rho", "align", "alignscale", "datavolume", "bandwidth")
TRACE = {"azure": ROOT / "data/raw/azure_code_1week.csv",
         "burstgpt": ROOT / "data/raw/burstgpt_1.csv"}
VARIANTS = {"linear": "L", "pressure": "P"}


def main() -> int:
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    log = ROOT / "results" / f"cons_battery_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.log"
    todo = list(VARIANTS) if which == "all" else [which]

    # 【preflight】先跑真实入口的接线自检；不过就拒绝启动，避免整批白跑。
    # 2026-10-12 的教训：--service-slowdown 只进了 cell/CSV，没进 SimConfig，
    # 8,600 次仿真全部按理想模型跑。检查逻辑在 analysis/regtest_slowdown.py
    # 的 real_path_check（走真实 run_cell）。
    print("[preflight] analysis/regtest_slowdown.py selftest ...", flush=True)
    chk = subprocess.run(
        [sys.executable, str(ROOT / "analysis/regtest_slowdown.py"), "selftest"],
        cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    print(chk.stdout or "", flush=True)
    if chk.returncode != 0:
        print(f"[preflight] FAILED rc={chk.returncode}，拒绝启动电池", flush=True)
        print((chk.stderr or "")[-800:], flush=True)
        return chk.returncode

    # 断点续跑：扫描既有电池日志里已"完成"的 (source/mode/variant) 组合，跳过之。
    # ⚠️ 只认 2026-10-12T08:45Z（第 2 轮 --net-set v2）之后的日志——
    # 第 1 轮是 v1 错配置（已改名留档），它的"完成"不算数。
    RESUME_SINCE = "20261012T084543Z"
    done: set[str] = set()
    for old in (ROOT / "results").glob("cons_battery_*.log"):
        if old.stem < f"cons_battery_{RESUME_SINCE}":
            continue
        for ln in old.read_text(encoding="utf-8", errors="replace").splitlines():
            if " 完成（" in ln and "] " in ln:
                done.add(ln.split("] ", 1)[-1].split(" 完成")[0].strip())
    if done:
        print(f"[resume] 已完成 {len(done)} 场，跳过：{sorted(done)}", flush=True)

    def say(msg: str) -> None:
        line = f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}"
        print(line, flush=True)
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    for var in todo:
        suf = VARIANTS[var]
        say(f"########## 变体 {var} → matrix_cons{suf} ##########")
        jobs = [((None, None), m) for m in MODES]
        for src, tr in TRACE.items():
            jobs += [((src, tr), m) for m in MODES]
        for (src, tr), mode in jobs:
            key = f"{src or 'synthetic'}/{mode}/{var}"
            if key in done:
                say(f"{key} 跳过（resume：已由上一轮完成）")
                continue
            # 每个负载源单独一个目录：analyze.py 每个 mode 只取最新 run_id，
            # 三批混在一个目录会让报告静默只分析最后跑的那批（2026-10-20 实际发生）。
            src_tag = src or "synthetic"
            cmd = [sys.executable, str(ROOT / "experiments/run_matrix.py"),
                   "--mode", mode, "--service-slowdown", var,
                   "--net-set", "v2",
                   "--out", str(ROOT / f"results/matrix_cons{suf}_{src_tag}")]
            if src:
                cmd += ["--source", "trace", "--trace", str(tr)]
            say(f"{src or 'synthetic'}/{mode}/{var} 启动")
            t0 = time.time()
            r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
            tail = (r.stdout or "")[-400:]
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(tail + "\n")
            if r.returncode != 0:
                say(f"!! {src or 'synthetic'}/{mode}/{var} 失败 rc={r.returncode}")
                say((r.stderr or "")[-800:])
                return r.returncode
            say(f"{src or 'synthetic'}/{mode}/{var} 完成（{time.time() - t0:.0f}s）")
        say(f"########## 变体 {var} 完成 ##########")
    say("BATTERY_DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

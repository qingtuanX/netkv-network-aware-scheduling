# -*- coding: utf-8 -*-
"""Follow-up conservative battery: RQ2 staleness + method_ch discounting.

Adds the two modes that the 2026-10-20 battery did not run under the calibrated
service-time model.  Outputs append to the existing per-source directories
results/matrix_consL_<src> and matrix_consP_<src>, so analyze.py picks the
latest run_id per mode without mixing sources.

Usage:
  .venv/Scripts/python.exe experiments/run_cons_followup.py linear
  .venv/Scripts/python.exe experiments/run_cons_followup.py pressure
"""
from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

MODES = ("staleness", "method_ch")
TRACE = {"azure": ROOT / "data/raw/azure_code_1week.csv",
         "burstgpt": ROOT / "data/raw/burstgpt_1.csv"}
VARIANTS = {"linear": "L", "pressure": "P"}


def main() -> int:
    which = sys.argv[1] if len(sys.argv) > 1 else "linear"
    if which not in VARIANTS:
        print("usage: run_cons_followup.py [linear|pressure]")
        return 2
    log = ROOT / "results" / f"cons_followup_{which}_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.log"

    print("[preflight] analysis/regtest_slowdown.py selftest ...", flush=True)
    chk = subprocess.run([sys.executable, str(ROOT / "analysis/regtest_slowdown.py"), "selftest"],
                         cwd=ROOT, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    print(chk.stdout or "", flush=True)
    if chk.returncode != 0:
        print((chk.stderr or "")[-800:], flush=True)
        return chk.returncode

    done: set[str] = set()
    for old in (ROOT / "results").glob("cons_followup_*.log"):
        for ln in old.read_text(encoding="utf-8", errors="replace").splitlines():
            if " 完成（" in ln and "] " in ln:
                done.add(ln.split("] ", 1)[-1].split(" 完成")[0].strip())
    if done:
        print(f"[resume] 已完成：{sorted(done)}", flush=True)

    def say(msg: str) -> None:
        line = f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}"
        print(line, flush=True)
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(line + chr(10))

    suf = VARIANTS[which]
    jobs = []
    for src, tr in [("synthetic", None), ("azure", TRACE["azure"]),
                    ("burstgpt", TRACE["burstgpt"])]:
        for mode in MODES:
            jobs.append((src, tr, mode))

    for src, tr, mode in jobs:
        key = f"{src}/{mode}/{which}"
        if key in done:
            say(f"{key} 跳过（resume）")
            continue
        cmd = [sys.executable, str(ROOT / "experiments/run_matrix.py"),
               "--mode", mode, "--service-slowdown", which, "--net-set", "v2",
               "--out", str(ROOT / f"results/matrix_cons{suf}_{src}")]
        if tr is not None:
            cmd += ["--source", "trace", "--trace", str(tr)]
        say(f"{key} 启动")
        t0 = time.time()
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        with open(log, "a", encoding="utf-8") as fh:
            fh.write((r.stdout or "")[-1000:] + chr(10))
        if r.returncode != 0:
            say(f"!! {key} 失败 rc={r.returncode}")
            say((r.stderr or "")[-1200:])
            return r.returncode
        say(f"{key} 完成（{time.time() - t0:.0f}s）")

    say(f"FOLLOWUP_DONE {which}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

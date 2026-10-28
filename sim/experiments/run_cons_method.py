# -*- coding: utf-8 -*-
"""Calibrated discounting battery: full five-arm method mode (5 seeds)."""
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

TRACE = {"azure": ROOT / "data/raw/azure_code_1week.csv",
         "burstgpt": ROOT / "data/raw/burstgpt_1.csv"}
VARIANTS = {"linear": "L", "pressure": "P"}


def main() -> int:
    which = sys.argv[1] if len(sys.argv) > 1 else "linear"
    if which not in VARIANTS:
        print("usage: run_cons_method.py [linear|pressure]")
        return 2
    log = ROOT / "results" / f"cons_method_{which}_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.log"
    chk = subprocess.run([sys.executable, str(ROOT / "analysis/regtest_slowdown.py"), "selftest"],
                         cwd=ROOT, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if chk.returncode != 0:
        print(chk.stdout[-1000:], chk.stderr[-1000:])
        return chk.returncode

    def say(msg: str) -> None:
        line = f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}"
        print(line, flush=True)
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(line + chr(10))

    suf = VARIANTS[which]
    for src, tr in [("synthetic", None), ("azure", TRACE["azure"]),
                    ("burstgpt", TRACE["burstgpt"])]:
        cmd = [sys.executable, str(ROOT / "experiments/run_matrix.py"),
               "--mode", "method", "--service-slowdown", which, "--net-set", "v2",
               "--seeds", "1", "2", "3", "4", "5",
               "--out", str(ROOT / f"results/matrix_cons{suf}_{src}")]
        if tr is not None:
            cmd += ["--source", "trace", "--trace", str(tr)]
        say(f"{src}/method/{which} 启动")
        t0 = time.time()
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        with open(log, "a", encoding="utf-8") as fh:
            fh.write((r.stdout or "")[-800:] + chr(10))
        if r.returncode != 0:
            say(f"!! {src}/method/{which} 失败 rc={r.returncode}")
            say((r.stderr or "")[-1000:])
            return r.returncode
        say(f"{src}/method/{which} 完成（{time.time() - t0:.0f}s）")
    say("METHOD_DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

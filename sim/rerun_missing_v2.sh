#!/usr/bin/env bash
# 补齐 v2 三批里**缺 manifest 的 mode**（= 上次被中断的那些）。
#
# 为什么需要它：矩阵是分 mode 逐个跑的，进程被中断（关机、重启、Ctrl-C）时，
# 当前 mode 只有 runs+paired、没有 manifest 与 p_holm（这两样只在全部 cell 跑完后写）。
# 直接重跑整个 mode 是最省事的做法：产物 append-only，analyze 只取最新 run_id，
# 旧的残缺 run 留着当证据、不参与结论。
#
# 用法（sim/ 目录下）：
#   DRY_RUN=1 bash rerun_missing_v2.sh    # 只看缺哪些 mode，**不开跑**（先看这个）
#   bash rerun_missing_v2.sh              # 三批都检查并补齐
#   bash rerun_missing_v2.sh azure        # 只补 Azure 那批
#
# ⚠ 直接跑它会真的启动矩阵：如果后台已经有一条链在跑同一个 mode，就会重复算。
#   先 DRY_RUN=1 看清楚。
set -uo pipefail
DRY_RUN="${DRY_RUN:-0}"

SIM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SIM_DIR"
PY=.venv/Scripts/python.exe
MODES="convergence scale ablation datavolume fallback bandwidth rho staleness main"

missing() {   # $1=产物目录  → 打印缺 manifest 的 mode 名（一行一个）
    "$PY" - "$1" <<'PYEOF' 2>/dev/null
import sys
from pathlib import Path
d = Path(sys.argv[1])
modes = "convergence scale ablation datavolume fallback bandwidth rho staleness main".split()
for m in modes:
    rf = sorted(d.glob(f"{m}_*_runs.csv"))
    if not rf:
        print(m); continue
    rid = rf[-1].name.replace(f"{m}_", "").replace("_runs.csv", "")
    if not (d / f"{m}_{rid}_manifest.json").exists():
        print(m)
PYEOF
}

run_batch() {  # $1=标签 $2=产物目录 $3=source $4=trace
    local label="$1" out="$2" src="$3" trace="$4"
    local todo
    todo=$(missing "$out")
    if [ -z "$todo" ]; then
        echo "[$label] 全部 mode 均有 manifest ✓ 无需补跑"
        return
    fi
    echo "[$label] 需补跑: $(echo "$todo" | tr '\n' ' ')"
    if [ "$DRY_RUN" != "0" ]; then
        echo "[$label] DRY_RUN=1 → 只看不跑"
        return
    fi
    for m in $todo; do
        echo "===== $label $m $(date +%H:%M:%S) ====="
        if [ "$src" = "trace" ]; then
            "$PY" experiments/run_matrix.py --mode "$m" --source trace --net-set v2 \
                --trace "$trace" --out "$out"
        else
            "$PY" experiments/run_matrix.py --mode "$m" --source synthetic --net-set v2 \
                --out "$out"
        fi
    done
}

want="${1:-all}"
case "$want" in
  all|synthetic) run_batch synthetic results/matrix_v2 synthetic "" ;;
esac
case "$want" in
  all|azure)     run_batch azure results/matrix_trace_v2 trace data/raw/azure_code_1week.csv ;;
esac
case "$want" in
  all|burstgpt)  run_batch burstgpt results/matrix_burstgpt_v2 trace data/raw/burstgpt_1.csv ;;
esac
echo "[完成] $(date +%H:%M:%S)"

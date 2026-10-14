#!/bin/bash
# 保守模型电池（CONS_MODEL_PREREG §1.2 范围的超集执行）
#
# L（linear）/ P（pressure）× 三批负载 × 6 个 mode 全量跑：
#   main（fresh/stale）rho（五档到达压力）align（对齐度图）
#   alignscale（对齐×规模）datavolume（prompt_only/kv_cache）bandwidth（带宽图）
# 产物：results/matrix_consL/、results/matrix_consP/（runs.csv 自带 service_slowdown 列）
# 原则：全量超集 = 零挑选；同种子同请求流；只改服务时间倍率。
# 用法：bash experiments/run_cons_battery.sh [linear|pressure|all]（默认 all）
set -e
cd "$(dirname "$0")/.."
PY=.venv/Scripts/python.exe
AZ=data/raw/azure_code_1week.csv
BG=data/raw/burstgpt_1.csv
MODES="main rho align alignscale datavolume bandwidth"
WHICH="${1:-all}"
LOG=results/cons_battery_$(date -u +%Y%m%dT%H%M%SZ).log

run_variant() {  # $1=slowdown 值 $2=目录后缀 L|P
  local var=$1 suf=$2
  echo "########## 变体 $var → matrix_cons$suf ##########" | tee -a "$LOG"
  for mode in $MODES; do
    echo "[$(date -u +%H:%M:%S)] synthetic/$mode/$var" | tee -a "$LOG"
    $PY experiments/run_matrix.py --mode "$mode" --service-slowdown "$var" \
      --out "results/matrix_cons$suf" >>"$LOG" 2>&1
  done
  for pair in "azure:$AZ" "burstgpt:$BG"; do
    local src="${pair%%:*}" tr="${pair#*:}"
    for mode in $MODES; do
      echo "[$(date -u +%H:%M:%S)] $src/$mode/$var" | tee -a "$LOG"
      $PY experiments/run_matrix.py --mode "$mode" --source trace --trace "$tr" \
        --service-slowdown "$var" --out "results/matrix_cons$suf" >>"$LOG" 2>&1
    done
  done
  echo "########## 变体 $var 完成 $(date -u +%H:%M:%S) ##########" | tee -a "$LOG"
}

case "$WHICH" in
  linear)   run_variant linear L ;;
  pressure) run_variant pressure P ;;
  all)      run_variant linear L; run_variant pressure P ;;
  *) echo "未知参数: $WHICH"; exit 2 ;;
esac
echo "BATTERY_DONE $(date -u +%H:%M:%S)" | tee -a "$LOG"

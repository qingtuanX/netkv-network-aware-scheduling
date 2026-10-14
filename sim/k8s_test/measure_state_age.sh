#!/usr/bin/env bash
# 实测：**调度器实际用到的状态有多旧**，以及这个"旧"花掉多少性能。
#
# 为什么要单独测这个：
#   仿真里"观测年龄"是一个我们自己设的常数（0/0.25/1/…/60 秒）。真实系统里它不是常数：
#     实际年龄 = 「距上次轮询过了多久（0～一个采集周期）」+「拉取这一次本身花了多久」+「我们注入的 lag」
#   其中"拉取花多久"会在系统忙的时候自己变长（worker 的 HTTP 线程抢不到 CPU，
#   连健康检查都会超时——k3s 事件里已经看到过）。**于是出现一个反馈：
#   越想看清谁忙，看得越慢。** 仿真缺的正是这一项。
#
# 做法：扫"监控采集周期"（--state-ttl），每个周期跑一次，记录
#   · 每次决策实际用到的状态年龄（obs_age_s，已经写进每请求 CSV）
#   · 拉取次数 / 平均拉取耗时 / 拉取失败次数
# 产物：results/k8s_test/stateage_ttl<周期>[_lag<lag>].csv（+ _workers.json）
#
# 用法（WSL 里，sim/ 目录下）：
#   bash k8s_test/measure_state_age.sh
#   TTL_LIST="0.05 1 15" LAG_LIST="0 3" bash k8s_test/measure_state_age.sh
set -uo pipefail
K="${K:-k3s kubectl}"
SIM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${SIM_DIR}/results/k8s_test"
QPS="${QPS:-8}"
REQUESTS="${REQUESTS:-240}"
XFER="${XFER:-8}"
TTL_LIST="${TTL_LIST:-0.05 0.25 1 5 15}"
LAG_LIST="${LAG_LIST:-0}"
POLICY="${POLICY:-least_load}"
mkdir -p "$OUT"

say() { printf '\n=== %s ===\n' "$*"; }
pod_ips() {
    $K get pods -l role=worker -o jsonpath='{range .items[*]}{.status.podIP}:9101{"\n"}{end}' 2>/dev/null \
        | paste -sd, - || true
}

say "等 worker 就绪并取地址"
for i in $(seq 1 40); do
    n=$($K get pods -l role=worker -o jsonpath='{range .items[*]}{.status.phase}{"\n"}{end}' 2>/dev/null \
        | grep -c '^Running$' || true)
    [ "${n:-0}" -ge 3 ] && break
    sleep 3
done
W=$(pod_ips)
echo "workers = ${W:-（空）}"
[ -n "$W" ] || { echo "[失败] 取不到 Pod IP"; exit 1; }

say "扫采集周期：${TTL_LIST}（注入 lag：${LAG_LIST}）"
for ttl in $TTL_LIST; do
  for lag in $LAG_LIST; do
    # 【每次都重新解析一次地址】Pod 一旦重启就换 IP（本项目实测过很多次：
    # WSL 虚拟机空闲约 60 秒会自动关机 → k3s 重起 → Pod 换 IP）。
    # 开头解析一次、后面一直用，会导致**所有请求打到死地址**：失败数是 100%，
    # 而且失败是瞬间返回的（0.1 ms），看起来像"状态很新鲜"——极容易误判。
    W=$(pod_ips)
    [ -n "$W" ] || { echo "[跳过] 取不到 Pod IP"; continue; }
    r0=$($K get pods -l role=worker -o jsonpath='{range .items[*]}{.status.containerStatuses[0].restartCount}{"\n"}{end}' 2>/dev/null | awk '{s+=$1} END{print s+0}')
    out="${OUT}/stateage_ttl${ttl}_lag${lag}.csv"
    echo "--- 采集周期=${ttl}s  注入lag=${lag}s  qps=${QPS}  requests=${REQUESTS}  workers=$W"
    python3 "${SIM_DIR}/k8s_test/measure_system.py" client \
        --workers "$W" --policy "$POLICY" --state-lag "$lag" --state-ttl "$ttl" \
        --qps "$QPS" --requests "$REQUESTS" --mean-transfer-mb "$XFER" \
        --mean-service-units 1.0 --seed 1 --request-timeout 30 --max-seconds 600 \
        --out "$out"
  done
done

say "汇总"
python3 "${SIM_DIR}/k8s_test/summarize_real.py" --dir "$OUT"

#!/usr/bin/env bash
# 真实集群（WSL2 里的单节点 k3s）对照网格 —— 在 WSL 里跑，不是 Windows 侧。
#
# 干什么：
#   1) 等 3 个 worker Pod Running，拿到它们的 **Pod IP**（集群里没有 CoreDNS）
#   2) 先跑"到达率阶梯"（门 1 的实测版）：找出哪个 qps 下系统真的被压住了
#   3) 再跑主网格：{least_load, contention} × {状态新鲜, 陈旧 3s} × {小传输, 大传输}
#   4) 用 summarize_real.py 出汇总表（p50/p95/p99、net_share、各 worker 利用率）
#
# 为什么 client 跑在集群外的 WSL 里而不是 Pod 里：
#   集群 DNS（CoreDNS）需要从 Docker Hub 拉镜像，本机不通，所以禁用了 CoreDNS。
#   没有 DNS 就用 Pod IP 直连——client 与 worker 之间仍然走真实内核网络栈（cni0/veth）。
#
# 【三条脆弱点的加固，都是踩过才加的】
#   a. **Pod IP 每次 client 运行前重新解析**：Pod 一旦重启（k3s 重启、镜像换 digest、
#      WSL 发行版被终止）IP 就会变，而 client 的 --workers 是启动时固定的。
#      写死 IP 的后果不是报错而是**每个请求卡到超时**（实测：挂了 2 分多钟只走了 145 个请求）。
#   b. **请求级超时 30 s**（measure_system.py --request-timeout）：正常请求在 1–2 s 内，
#      30 s 足够；卡住的请求会快速失败并记为 ok=0，而不是拖满 300 s。
#   c. **重启检测**：每次运行前后比对 Pod 的 restartCount 之和；变了就把这次输出加前缀
#      `CONTAM_` 并大声警告——运行中途重启会让绝对数值失去意义（趋势也许还看得出，
#      但不许当干净数据用）。
#   d. 不用 `| sed` 加缩进：sed 是块缓冲，输出会整段憋到运行结束，
#      看起来**像卡死**（我就因此误杀过一次正常运行的网格）。
#
# 用法（WSL 里，仓库的 sim/ 目录下）：
#   bash k8s_test/run_k3s_grid.sh                       # 默认网格
#   QPS_LIST="4 6" XFER_LIST="8" REQUESTS=240 bash k8s_test/run_k3s_grid.sh
set -uo pipefail

K="${K:-k3s kubectl}"
NS=default
SIM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${SIM_DIR}/results/k8s_test"
TAG="${TAG:-k3s}"
REQUESTS="${REQUESTS:-240}"          # 主网格每次运行的请求数
PILOT_REQUESTS="${PILOT_REQUESTS:-120}"   # 阶梯用更少请求：它只是用来找压力区间
QPS_LIST="${QPS_LIST:-4}"
PILOT_QPS="${PILOT_QPS:-1 2 4 8}"
XFER_LIST="${XFER_LIST:-0.5 8}"
LAG_LIST="${LAG_LIST:-0 3}"
POLICIES="${POLICIES:-least_load contention}"
REQ_TIMEOUT="${REQ_TIMEOUT:-30}"
RUN_GRID="${RUN_GRID:-1}"            # 只跑阶梯就设 RUN_GRID=0
mkdir -p "$OUT_DIR"

say()  { printf '\n=== %s ===\n' "$*"; }
warn() { printf '[警告] %s\n' "$*" >&2; }

# --- 工具 -------------------------------------------------------------------
pod_ips() {   # Pod IP:port 逗号分隔；取不到就输出空
    $K -n "$NS" get pods -l role=worker \
        -o jsonpath='{range .items[*]}{.status.podIP}:9101{"\n"}{end}' 2>/dev/null \
        | paste -sd, - || true
}
restart_sum() {   # 3 个 worker 的 restartCount 之和
    $K -n "$NS" get pods -l role=worker \
        -o jsonpath='{range .items[*]}{.status.containerStatuses[0].restartCount}{"\n"}{end}' 2>/dev/null \
        | awk '{s+=$1} END{print s+0}' || echo 0
}
run_client() {   # $1=输出文件 其余=client 参数
    local out="$1"; shift
    local r0 r1
    r0=$(restart_sum)
    python3 "${SIM_DIR}/k8s_test/measure_system.py" client \
        --workers "$(pod_ips)" --request-timeout "$REQ_TIMEOUT" --max-seconds 900 "$@" \
        --out "$out"
    local rc=$?
    r1=$(restart_sum)
    if [ "$r0" != "$r1" ]; then
        warn "运行期间 Pod 重启过（restartCount ${r0}→${r1}）：$out 标为 CONTAM_ 前缀，不当干净数据用"
        mv -f "$out" "$(dirname "$out")/CONTAM_$(basename "$out")" 2>/dev/null || true
        mv -f "${out%.csv}_workers.json" "$(dirname "$out")/CONTAM_$(basename "${out%.csv}")_workers.json" 2>/dev/null || true
    fi
    return $rc
}

# --- 1. 等 worker 就绪 -------------------------------------------------------
# 【为什么不用 kubectl wait】Deployment 还没创建出 Pod 时，`kubectl wait -l <selector>`
# 会直接返回"没有匹配的资源"而**不等待** → 脚本带着空地址列表往下跑。
# 【jsonpath 写法】`{...}` 里的字面量必须加引号：`{,}` 非法
# （实测报 `unrecognized character in action: U+002C ','`），要用 `{"\n"}` 再 paste 拼。
say "1. 等 3 个 worker Pod Running（最多 180 s）"
for i in $(seq 1 60); do
    n=$($K -n "$NS" get pods -l role=worker \
        -o jsonpath='{range .items[*]}{.status.phase}{"\n"}{end}' 2>/dev/null \
        | grep -c '^Running$' || true)
    printf '  Running=%s/3\n' "${n:-0}"
    [ "${n:-0}" -ge 3 ] && break
    sleep 3
done
$K -n "$NS" get pods -o wide -l role=worker || true
WORKERS=$(pod_ips)
echo "workers = ${WORKERS:-（空）}"
[ -n "$WORKERS" ] || { echo "[失败] 没有拿到 Pod IP，先看 kubectl get pods -o wide"; exit 1; }
echo "启动前 restartCount 合计 = $(restart_sum)"

# --- 2. 到达率阶梯（门 1 的实测版） -----------------------------------------
# 仿真里的门 1 是 rho = 平均工作量×到达率/容量落在 [0.3, 0.9]。真实系统里没有"容量"
# 这个解析量，所以用 worker 自报的忙时间占比当代理：util = Σbusy_s / (墙钟 × worker 数)。
# 这一步的产物是**证据**（说明后面那个 qps 不是在空转区间取的），不是结论。
say "2. 到达率阶梯（只跑 least_load，大传输；看 util 落在哪）"
for q in $PILOT_QPS; do
    echo "--- 阶梯 qps=$q  requests=$PILOT_REQUESTS"
    run_client "${OUT_DIR}/${TAG}_pilot_q${q}.csv" \
        --policy least_load --state-lag 0 --qps "$q" --requests "$PILOT_REQUESTS" \
        --mean-transfer-mb 8 --mean-service-units 1.0 --seed 1 || warn "阶梯 qps=$q 返回非零"
done

# --- 3. 主网格 ---------------------------------------------------------------
if [ "$RUN_GRID" != "0" ]; then
    say "3. 主网格：策略 × 状态新鲜度 × 传输量 × 到达率"
    for q in $QPS_LIST; do
      for pol in $POLICIES; do
        for lag in $LAG_LIST; do
          for xf in $XFER_LIST; do
            out="${OUT_DIR}/${TAG}_${pol}_lag${lag}_xfer${xf}_q${q}.csv"
            echo "--- $pol  lag=${lag}s  transfer=${xf}MB  qps=$q  requests=$REQUESTS"
            run_client "$out" \
                --policy "$pol" --state-lag "$lag" --qps "$q" --requests "$REQUESTS" \
                --mean-transfer-mb "$xf" --mean-service-units 1.0 --seed 1 \
                || warn "$pol lag=$lag xfer=$xf 返回非零"
          done
        done
      done
    done
fi

# --- 4. 汇总 -----------------------------------------------------------------
say "4. 汇总（表格写进 results/k8s_test/summary.csv）"
python3 "${SIM_DIR}/k8s_test/summarize_real.py" --dir "$OUT_DIR"
echo "结束时间：$(date '+%F %T')"

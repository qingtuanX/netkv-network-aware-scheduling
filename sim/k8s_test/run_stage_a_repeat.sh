#!/usr/bin/env bash
# =============================================================================
# 阶段 A「干净重复批次」：跨公网两台真机 · 真实字节传输 + 内核 tc 限速 + 同会话 iperf3
# =============================================================================
#
# 为什么要有这个脚本（上一批的失败教训，原文见 sim/k8s_test/STAGE_A_CLOUD_REPEAT.md §7）：
#   上一批 21 轮里，限速规则在**测量过程中**被删掉了——根因是"安全网"本身：
#   每轮装完限速都挂一个 `nohup sh -c 'sleep 900; tc qdisc del ... root' &`，
#   而一轮只要 4~5 分钟，于是 900 秒的定时器在批次跑到中段时触发，把**正在测量的那一轮**
#   的规则删了（`do04` 的 iperf3 逐秒曲线与 tc 轮询精确对上：04:57:18 从 htb 变 mq）。
#   再加上"判 FAIL 的轮次没有重跑"和"只存档了 qdisc、没存档 class/filter"，
#   坏数据混进了结果表，事后也查不出 tc 当时哪一步不对。
#
# 本脚本把五件事做成**硬约束**（对应任务书的一到五条）：
#   ① 每轮测量前断言限速在位：根队列必须是 `htb 1:`、慢类 `1:10` 的速率必须等于设定值、
#      每个实验端口的过滤器必须存在（按十六进制端口号核对）。断言不过 → 重设 → 再断言，
#      仍不过 → 该轮**不许开测**，记为作废。
#   ② 每轮把 `tc -s qdisc/class/filter show` 三份原文都存档（测量前 + 测量后各一轮）。
#   ③ 读 overlimits 一律带 `-s`：用一个只读的 HTTP 小服务把云端的
#      `tc -s qdisc show dev eth0` 原文取回来（云主机走 IPv6 的 SSH 只能从 Windows 侧发起，
#      而电池跑在 WSL 里；WSL 到云主机只有 IPv4 的 8000/5173/80 通），
#      并在运行时自检输出里**真的**有 "overlimits" 字样（只有 `-s` 才会打印那一行，
#      上一批 `do01` 就是漏了 `-s` 导致 A3 误报 +0）。
#   ④ 一轮只要"仪器/流程失效"就作废并重跑（限速掉线、overlimits 增量为 0、字节不一致、
#      请求失败、iperf3 连不上、轮询发现根队列变了）。作废轮**保留全部产物 + 标注原因**。
#      ⚠️ 口径说明：判据②③本身"没达标"（偏差超线、比值 < 1.5）**不作废**——那是本实验要量的结果，
#      重跑就等于挑数据。作废只针对"测量本身不可信"。
#   ⑤ 限速**只在批次开始时装、批次结束（含脚本异常退出）时统一拆**，
#      绝不挂每轮定时器（这正是上一批的根因）。
#
# 运行位置：**Git Bash / Windows 侧**。原因：云主机的 SSH 只放行 IPv6，而 WSL 里没有全局 IPv6
#   （实测 `ssh: connect to host ... Network is unreachable`）。本机 WSL 的 tc 则通过
#   `wsl -d Ubuntu -u root -- ...` 驱动；电池（measure_system.py transfer）也必须跑在 WSL 里，
#   因为上传方向的限速加在 WSL 的 eth0 出口，只有从 WSL 出去的流量才会被掐。
#
# 用法：
#   bash sim/k8s_test/run_stage_a_repeat.sh --phase both --up-rounds 10 --down-rounds 10
#   bash sim/k8s_test/run_stage_a_repeat.sh --phase up --up-rounds 1 --payload-mb 2   # 冒烟
#   bash sim/k8s_test/run_stage_a_repeat.sh --teardown-only                          # 只清理
#
# 产物（append-only，文件名带 UTC run_id）：
#   <out>/batch_<批次UTC>_rounds.csv      每轮一行（三项判据的逐轮判定）
#   <out>/batch_<批次UTC>_voided.csv      作废轮 + 原因
#   <out>/batch_<批次UTC>_provenance.txt  本批的参数、命令原文、软链与仪表指纹
#   <out>/batch_<批次UTC>.log             全过程日志
#   <out>/<轮次标签>_<轮UTC>_tc_{qdisc,class,filter}_{before,after}.txt   六份 tc 原文
#   <out>/<轮次标签>_<轮UTC>_battery_stdout.txt / _battery.{csv,json}      工具产物副本
#   <out>/<轮次标签>_<轮UTC>_iperf3.json / _iperf3_intervals.txt           同会话第三方
#   <out>/<轮次标签>_<轮UTC>_qdisc_poll.txt                                轮内按 5 s 轮询根队列
# =============================================================================

set -u

# ------------------------------ 默认参数 -------------------------------------
PHASE="both"
UP_ROUNDS=10
DOWN_ROUNDS=10
PAYLOAD_MB=8
RATE_UP=6
RATE_DOWN=1.5
SIZES_MB="0.2 2"
CONCURRENCY=3
MAX_ATTEMPTS=4
SKIP_SETUP=0
TEARDOWN_ONLY=0
KEEP_ON_EXIT=0

CLOUD6="${CLOUD6:-2402:4e00:c032:9200:2ea8:7f22:509c:0}"
CLOUD4="${CLOUD4:-49.235.115.164}"
SSH_KEY="${SSH_KEY:-/d/Documents/API/123.pem}"
WSL_DIST="${WSL_DIST:-Ubuntu}"
WORKER_PORT="${WORKER_PORT:-8000}"
IPERF_PORT="${IPERF_PORT:-5173}"
TCSTAT_PORT="${TCSTAT_PORT:-80}"          # 云防火墙放行 80（实测可达）；tc 只读仪表挂这儿

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SIM_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_DIR="$(cd "$SIM_DIR/.." && pwd)"
OUT_DIR="$SIM_DIR/results/stage_a_cloud_clean"
# WSL 侧路径（用于把 /e/... 映射成 /mnt/e/...）；用软链转成纯 ASCII，避免中文路径进 argv
WSL_SIM="/mnt/$(echo "$SIM_DIR" | sed 's|^/\([a-z]\)/|\1/|')"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --phase) PHASE="${2:?}"; shift 2;;
    --up-rounds) UP_ROUNDS="${2:?}"; shift 2;;
    --down-rounds) DOWN_ROUNDS="${2:?}"; shift 2;;
    --payload-mb) PAYLOAD_MB="${2:?}"; shift 2;;
    --rate-up) RATE_UP="${2:?}"; shift 2;;
    --rate-down) RATE_DOWN="${2:?}"; shift 2;;
    --sizes-mb) SIZES_MB="${2:?}"; shift 2;;
    --concurrency) CONCURRENCY="${2:?}"; shift 2;;
    --max-attempts) MAX_ATTEMPTS="${2:?}"; shift 2;;
    --out-dir) OUT_DIR="${2:?}"; shift 2;;
    --skip-setup) SKIP_SETUP=1; shift;;
    --keep-on-exit) KEEP_ON_EXIT=1; shift;;
    --teardown-only) TEARDOWN_ONLY=1; shift;;
    -h|--help) sed -n '2,40p' "${BASH_SOURCE[0]}"; exit 0;;
    *) echo "未知参数：$1" >&2; exit 2;;
  esac
done

BATCH_ID="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$OUT_DIR"
LOG_FILE="$OUT_DIR/batch_${BATCH_ID}.log"
ROUNDS_CSV="$OUT_DIR/batch_${BATCH_ID}_rounds.csv"
VOIDS_CSV="$OUT_DIR/batch_${BATCH_ID}_voided.csv"
PROV_FILE="$OUT_DIR/batch_${BATCH_ID}_provenance.txt"
STOPFILE="/tmp/stage_a_poll_stop_$$"
POLL_PID=""
TEARDOWN_DONE=0

# 电池与 iperf3 的载荷字节数（两者必须一致，否则"同条件对照"不成立）
PAYLOAD_BYTES="$(awk -v m="$PAYLOAD_MB" 'BEGIN{printf "%d", m*1048576}')"
PROBE_BYTES=524288
# 每轮"不限速基线"的载荷：4 MB。取 4 而不是 8，是为了省时间（链路约 5 Mbit 时 8 MB 要 13 s），
# 又足够甩开 TCP 慢启动（实测 2 MB 与 8 MB 给出的稳态速率一致：5.591 vs 5.592 Mbit/s）。
BASELINE_MB=4
BASELINE_BYTES="$(awk -v m="$BASELINE_MB" 'BEGIN{printf "%d", m*1048576}')"

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*" | tee -a "$LOG_FILE"; }
die() { log "致命错误：$*"; exit 1; }

# ---------------------------- SSH / WSL 封装 ---------------------------------
SSH_OPTS=(-6 -i "$SSH_KEY" -o IdentitiesOnly=yes -o StrictHostKeyChecking=no
          -o UserKnownHostsFile=/tmp/kh_stage_a_clean -o BatchMode=yes
          -o ConnectTimeout=12 -o LogLevel=ERROR)
ssh6() { ssh "${SSH_OPTS[@]}" "ubuntu@$CLOUD6" "$@"; }
scp6() { scp "${SSH_OPTS[@]}" "$1" "ubuntu@[$CLOUD6]:$2"; }
wslr() { MSYS_NO_PATHCONV=1 wsl -d "$WSL_DIST" -u root -- "$@"; }
# 【为什么用 stdin 而不是 `bash -c "..."`】Windows → wsl.exe 的参数编码会把含 `&`、`$!`
# 的命令行改样（实测 `wsl ... bash -c 'nohup sleep 300 & echo $! > f'` 的 $! 落空）。
# 走 stdin 就没有这层转义，脚本原样执行。
# 【为什么要 MSYS_NO_PATHCONV】Git Bash 会把看起来像 POSIX 路径的参数改写成 Windows 路径：
# `wsl … python3 /opt/stage_a_interval.py` 里的 `/opt/...` 被换成
# `C:/Program Files/Git/opt/...`，脚本在 WSL 里当然找不到（本轮实测踩到，逐秒曲线因此补跑）。
wslb() { MSYS_NO_PATHCONV=1 wsl -d "$WSL_DIST" -u root -- bash -s <<< "$1"; }

# =============================================================================
# 一、限速的安装 / 断言 / 拆除
# =============================================================================
# 【为什么要分开"装"和"断言"】上一批的教训是"装完就不再确认"。
# 这里把两者分开：装是幂等的（先 del 再 add），断言是**测量前必须通过的门**。

install_shaper_up() {
  wslb "tc qdisc del dev eth0 root 2>/dev/null || true
        tc qdisc add dev eth0 root handle 1: htb default 20 r2q 1000
        tc class add dev eth0 parent 1: classid 1:1 htb rate 1000mbit
        tc class add dev eth0 parent 1:1 classid 1:10 htb rate ${RATE_UP}mbit ceil ${RATE_UP}mbit quantum 1514
        tc class add dev eth0 parent 1:1 classid 1:20 htb rate 1000mbit ceil 1000mbit
        tc filter add dev eth0 parent 1: protocol ip prio 1 u32 match ip dport ${WORKER_PORT} 0xffff flowid 1:10
        tc filter add dev eth0 parent 1: protocol ip prio 1 u32 match ip dport ${IPERF_PORT} 0xffff flowid 1:10" >/dev/null 2>&1
}

install_shaper_down() {
  ssh6 "sudo tc qdisc del dev eth0 root 2>/dev/null || true
        sudo tc qdisc add dev eth0 root handle 1: htb default 20 r2q 1000
        sudo tc class add dev eth0 parent 1: classid 1:1 htb rate 1000mbit
        sudo tc class add dev eth0 parent 1:1 classid 1:10 htb rate ${RATE_DOWN}mbit ceil ${RATE_DOWN}mbit quantum 1514
        sudo tc class add dev eth0 parent 1:1 classid 1:20 htb rate 1000mbit ceil 1000mbit
        sudo tc filter add dev eth0 parent 1: protocol ip prio 1 u32 match ip sport ${WORKER_PORT} 0xffff flowid 1:10
        sudo tc filter add dev eth0 parent 1: protocol ip prio 1 u32 match ip sport ${IPERF_PORT} 0xffff flowid 1:10" >/dev/null 2>&1
}

remove_shaper_up()   { wslb "tc qdisc del dev eth0 root 2>/dev/null || true" >/dev/null 2>&1 || true; }
remove_shaper_down() { ssh6 "sudo tc qdisc del dev eth0 root 2>/dev/null || true" >/dev/null 2>&1 || true; }

# 读 tc 原文：$1 = qdisc|class|filter，$2 = up|down
# 【为什么两个方向读法不同】上传限速在 WSL（电池所在机器）→ 本地直接读；
# 下传限速在云主机，而电池在 WSL 里、WSL 只有 IPv4 能到云（22 端口不通）→
# 走挂在 80 端口的只读仪表（它内部执行的就是 `tc -s ... show dev eth0`）。
read_tc() {
  case "$2" in
    up)   wslr tc -s "$1" show dev eth0 2>&1;;
    down) curl -s -m 12 "http://$CLOUD4/tc/$1" 2>&1;;
    *)    echo "";;
  esac
}

# 【第三条要求】输出里必须真的出现 "overlimits" —— 只有带 -s 才会有那一行。
# 上一批 do01 的 A3 误报 +0，就是因为读计数器的命令漏了 -s。
assert_stats_readable() {
  local dir="$1" q
  q="$(read_tc qdisc "$dir")"
  if ! printf '%s' "$q" | grep -q 'overlimits'; then
    return 1
  fi
  printf '%s' "$q" | grep -q 'Sent ' || return 1
  return 0
}

# 【第一条要求】限速在位断言：根队列 + 慢类速率 + 两个端口的过滤器（按十六进制端口核对）
# 返回 0 = 在位；非 0 = 不在位（调用方必须先重设再断言）
assert_shaper() {
  local dir="$1" rate rate_expect hex_worker hex_iperf q c f
  case "$dir" in up) rate="$RATE_UP";; down) rate="$RATE_DOWN";; esac
  q="$(read_tc qdisc  "$dir")"
  c="$(read_tc class  "$dir")"
  f="$(read_tc filter "$dir")"

  # ① 根队列必须是 htb，默认类必须是 0x20（= 16 进制 default 20）
  printf '%s' "$q" | grep -q 'qdisc htb 1: root' || { echo "根队列不是 htb 1:"; return 1; }
  printf '%s' "$q" | grep -q 'default 0x20'     || { echo "默认类不是 0x20"; return 1; }
  # ② 慢类 1:10 必须在，且速率等于设定值（tc 会把它印成 6Mbit / 1500Kbit 等）
  printf '%s' "$c" | grep -qE '^class htb 1:10 ' || { echo "慢类 1:10 不在"; return 1; }
  rate_expect="$(printf '%s' "$c" | awk '$1=="class" && $2=="htb" && $3=="1:10"{for(i=1;i<=NF;i++) if($i=="rate"){print $(i+1); exit}}')"
  if ! awk -v got="$rate_expect" -v want="$rate" 'BEGIN{
        if (got ~ /Kbit$/){n=substr(got,1,length(got)-4)+0; g=n/1000}
        else if (got ~ /Mbit$/){n=substr(got,1,length(got)-4)+0; g=n}
        else if (got ~ /Gbit$/){n=substr(got,1,length(got)-4)+0; g=n*1000}
        else {exit 1}
        d=(g-want)/want; exit (d<0?-d:d)>0.02 }'; then
    echo "慢类速率对不上（读到 ${rate_expect:-无}，期望 ${rate}Mbit）"; return 1
  fi
  # ③ 两个实验端口的过滤器都必须在。u32 打印 match 时，**目的端口**是右对齐的
  #    16 位（`match 00001f40/0000ffff`），而**源端口**整体左移 16 位
  #    （`match 1f400000/ffff0000`，见云端实测输出），所以两种写法都要认。
  hex_worker="$(printf '%08x' "$WORKER_PORT")"; hex_worker_s="$(printf '%04x0000' "$WORKER_PORT")"
  hex_iperf="$(printf '%08x' "$IPERF_PORT")";   hex_iperf_s="$(printf '%04x0000' "$IPERF_PORT")"
  printf '%s' "$f" | grep -q "flowid 1:10" || { echo "过滤器里没有 flowid 1:10"; return 1; }
  printf '%s' "$f" | grep -qiE "match (${hex_worker}|${hex_worker_s})" || { echo "缺少端口 ${WORKER_PORT} 的过滤器"; return 1; }
  printf '%s' "$f" | grep -qiE "match (${hex_iperf}|${hex_iperf_s})"   || { echo "缺少端口 ${IPERF_PORT} 的过滤器"; return 1; }
  return 0
}

# 过滤器的命中计数（success N）——限速器真的把我们的包分类进慢类的第四个独立见证
filter_success_sum() { # 入参是 `tc -s filter show` 的原文
  printf '%s' "$1" | grep -o 'success [0-9]\+' | awk '{s+=$2} END{print s+0}'
}

# overlimits / dropped 增量（只取 qdisc 一处，与工具 tc_cmd 的口径一致）
counter_sum() { # $1=原文 $2=overlimits|dropped
  printf '%s' "$1" | grep -o "$2 [0-9]\+" | awk '{s+=$2} END{print s+0}'
}

# 功能探测：真发一小段被限速的流量，确认 overlimits 真的在涨。
# 【为什么必须有】"类在、过滤器在"只证明规则装上了；上一批的坏轮规则也在、但流量没被掐。
probe_shaper() {
  local dir="$1" log="$2" oc0 oc1 rc args=()
  oc0="$(counter_sum "$(read_tc qdisc "$dir")" overlimits)"
  [[ "$dir" == "down" ]] && args+=(-R)
  wslr iperf3 -c "$CLOUD4" -p "$IPERF_PORT" -n "$PROBE_BYTES" "${args[@]}" >>"$log" 2>&1
  rc=$?
  oc1="$(counter_sum "$(read_tc qdisc "$dir")" overlimits)"
  printf '探测：iperf3 rc=%s，overlimits %s → %s（增量 %s）\n' "$rc" "$oc0" "$oc1" "$((oc1-oc0))" >>"$log" 2>/dev/null
  [[ $rc -eq 0 && $((oc1-oc0)) -gt 0 ]]
}

# =============================================================================
# 二、旧数据 / 服务 / 仪表
# =============================================================================
write_provenance() {
  {
    echo "批次 $BATCH_ID   UTC $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "运行位置：Git Bash (Windows 侧)；电池在 WSL($WSL_DIST) 里；云主机 $CLOUD4 / [$CLOUD6]"
    echo "参数：phase=$PHASE up_rounds=$UP_ROUNDS down_rounds=$DOWN_ROUNDS payload_mb=$PAYLOAD_MB"
    echo "      rate_up=${RATE_UP}Mbit rate_down=${RATE_DOWN}Mbit sizes_mb='$SIZES_MB' concurrency=$CONCURRENCY"
    echo "      max_attempts=$MAX_ATTEMPTS probe_bytes=$PROBE_BYTES"
    echo "上传限速（WSL eth0 出口，匹配目的端口）：tc qdisc add dev eth0 root handle 1: htb default 20 r2q 1000"
    echo "      tc class add dev eth0 parent 1:1 classid 1:10 htb rate ${RATE_UP}mbit ceil ${RATE_UP}mbit quantum 1514"
    echo "      tc filter add dev eth0 parent 1: protocol ip prio 1 u32 match ip dport ${WORKER_PORT}/${IPERF_PORT} flowid 1:10"
    echo "下传限速（云 eth0 出口，匹配源端口）：同上，源端口 ${WORKER_PORT}/${IPERF_PORT}，速率 ${RATE_DOWN}mbit"
    echo "读 overlimits 的命令（必须带 -s）："
    echo "      上传：wsl -d $WSL_DIST -u root -- tc -s qdisc show dev eth0"
    echo "      下传：curl -s http://$CLOUD4/tc/qdisc   （云端仪表内部执行 tc -s qdisc show dev eth0）"
    echo "电池：python3 /opt/stage_a_sim/k8s_test/measure_system.py transfer --target http://$CLOUD4:$WORKER_PORT"
    echo "      --iface eth0 --tc-rate-mbps <RATE> --shaped-direction <dir> --sizes-mb $SIZES_MB"
    echo "      --rate-test-mb $PAYLOAD_MB --rate-test-concurrency $CONCURRENCY"
    echo "      （下传轮另加 --tc-cmd 'curl -s -m 10 http://$CLOUD4/tc/qdisc'）"
    echo "第三方：iperf3 -c $CLOUD4 -p $IPERF_PORT -n $PAYLOAD_BYTES [ -R ] -J"
    echo "本机 measure_system.py md5：$(md5sum "$SCRIPT_DIR/measure_system.py" | awk '{print $1}')"
  } > "$PROV_FILE"
}

TCSTAT_SRC="$OUT_DIR/.tc_stats_server_$$.py"
write_tcstat_server() {
  cat > "$TCSTAT_SRC" <<'PYEOF'
#!/usr/bin/env python3
"""云端 tc 只读仪表（阶段 A 干净重复批次的测量器械）。

为什么需要它：电池必须跑在 WSL 里（上传方向的限速加在 WSL 的 eth0 出口），
而云主机的 SSH 只放行 IPv6、WSL 里没有全局 IPv6；WSL 能到云主机的只有 IPv4 的
80 / 8000 / 5173。于是把"读云端 tc 计数器"这件事挂到 80 端口上，
命令内部固定带 -s（只有 -s 才会打印 overlimits 那一段）。
只读：没有任何写操作，也不接受参数注入（路径白名单）。
"""
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

IFACE = sys.argv[1] if len(sys.argv) > 1 else "eth0"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 80
ROUTES = {
    "/tc/qdisc":  ["tc", "-s", "qdisc",  "show", "dev", IFACE],
    "/tc/class":  ["tc", "-s", "class",  "show", "dev", IFACE],
    "/tc/filter": ["tc", "-s", "filter", "show", "dev", IFACE],
}


def run(args):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=10)
        return (r.stdout or "") + (r.stderr or "")
    except Exception as e:                      # noqa: BLE001
        return "读取失败: %s: %s\n" % (type(e).__name__, e)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path == "/health":
            body = "ok\n"
        elif path in ROUTES:
            body = run(ROUTES[path])
        elif path == "/tc/all":
            body = "".join("=== %s ===\n%s" % (k, run(v)) for k, v in ROUTES.items())
        else:
            self.send_response(404)
            self.end_headers()
            return
        data = body.encode("utf-8", "replace")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    try:
        with open("/tmp/tc_stats_server.pid", "w") as fh:
            fh.write(str(os.getpid()))
    except OSError:
        pass
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
PYEOF
}

setup_all() {
  log "== 部署：tc 只读仪表 + iperf3 服务端 + worker + WSL 软链 =="
  write_tcstat_server
  scp6 "$TCSTAT_SRC" "/home/ubuntu/tc_stats_server.py" >/dev/null || die "scp 仪表失败"
  rm -f "$TCSTAT_SRC"
  ssh6 'if [ -f /tmp/tc_stats_server.pid ]; then sudo kill $(cat /tmp/tc_stats_server.pid) 2>/dev/null || true; sudo rm -f /tmp/tc_stats_server.pid; fi
        sleep 1
        sudo nohup python3 /home/ubuntu/tc_stats_server.py eth0 '"$TCSTAT_PORT"' > /home/ubuntu/tcstats.log 2>&1 &
        sleep 2
        if [ -f /tmp/tc_stats_server.pid ]; then echo "仪表已起 PID=$(cat /tmp/tc_stats_server.pid)"; else echo "仪表未起"; tail -5 /home/ubuntu/tcstats.log; fi' \
    || die "启动仪表失败"

  # 【为什么按端口判而不是按进程名判】`pgrep -f "measure_system.py worker"` 会匹配到
  # **这条 ssh 命令自己的命令行**（里面就有这段字符串），于是"已经跑着"永远成立、worker 永不启动。
  # 端口是外部可观测的事实，不受这个坑影响。
  ssh6 'pgrep -x iperf3 >/dev/null || (nohup iperf3 -s -p '"$IPERF_PORT"' > /home/ubuntu/iperf5173.log 2>&1 &)
        ss -tln | grep -q ":'"$WORKER_PORT"'" || (nohup python3 /home/ubuntu/measure_system.py worker --bind 0.0.0.0 --port '"$WORKER_PORT"' --compute 1e6 > /home/ubuntu/worker8000.log 2>&1 &)
        for i in 1 2 3 4 5 6 7 8 9 10; do
          ss -tln | grep -q ":'"$WORKER_PORT"'" && ss -tln | grep -q ":'"$IPERF_PORT"'" && break
          sleep 1
        done
        echo "== 云上监听 =="; ss -tln | grep -E ":'"$WORKER_PORT"'|:'"$IPERF_PORT"'|:'"$TCSTAT_PORT"'" || (echo "（有端口没起来）"; tail -5 /home/ubuntu/worker8000.log)' || die "启动服务失败"

  # WSL 侧软链：把中文路径换成纯 ASCII，避免中文进 argv（Windows→WSL 的参数编码不作赌注）
  wslb "ln -sfn '$WSL_SIM' /opt/stage_a_sim; ln -sfn '$(echo "$OUT_DIR" | sed 's|^/\([a-z]\)/|/mnt/\1/|')' /opt/stage_a_out; ls -l /opt/stage_a_sim /opt/stage_a_out | sed 's/  */ /g'" || die "WSL 软链失败"
  wslb "cat > /opt/stage_a_interval.py <<'PYEOF'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    parts = []
    for iv in d.get('intervals', []):
        s = iv.get('sum')
        if isinstance(s, dict) and 'bits_per_second' in s:
            parts.append('%.3f' % (s['bits_per_second'] / 1e6))
    print(' '.join(parts) if parts else '（无逐秒数据）')
except Exception as e:
    print('解析失败: %s: %s' % (type(e).__name__, e))
PYEOF
echo 间隔解析器已就绪" >/dev/null || die "写间隔解析器失败"

  # 仪表连通性自检（这是后面每轮 A3 读数赖以成立的前提）
  if ! read_tc qdisc down | grep -q 'overlimits'; then
    die "云端 tc 仪表不可用（curl http://$CLOUD4/tc/qdisc 里没有 overlimits）"
  fi
  # WSL 侧的保活：【为什么必须做】WSL2 空闲会自动关机（本机实测：两次调用之间 VM 重启，
  # /proc/uptime 只剩 14 秒），一关机 tc 规则就全没了。轮内本来就有 WSL 活动，但批次很长，
  # 这里再挂一个长 sleep 把 VM 钉住；**断言**仍然是最终的门（保活只是减少无谓作废）。
  # PID 存文件，清理时按 PID 杀——不用 pkill 模式匹配，避免"模式匹配到自己的命令行"那个老坑。
  wslb "nohup sleep 10800 >/dev/null 2>&1 & echo \$! > /tmp/stage_a_keepalive.pid; sleep 0.5; echo 保活 PID=\$(cat /tmp/stage_a_keepalive.pid)" \
    2>&1 | tee -a "$LOG_FILE"

  # 仪表连通性自检（这是后面每轮 A3 读数赖以成立的前提）
  if ! read_tc qdisc down | grep -q 'overlimits'; then
    die "云端 tc 仪表不可用（curl http://$CLOUD4/tc/qdisc 里没有 overlimits）"
  fi
  if ! wslr curl -s -m 8 "http://$CLOUD4:$WORKER_PORT/health" | grep -q ok; then
    die "云端 worker /health 不可用"
  fi
  if ! wslr iperf3 -c "$CLOUD4" -p "$IPERF_PORT" -n 100000 >/dev/null 2>&1; then
    die "云上 iperf3 服务端（端口 $IPERF_PORT）连不上"
  fi
  log "部署完成（仪表 / worker / iperf3 三项自检都通过）"
}

teardown_all() {
  # 幂等：INT/TERM 触发的清理与 EXIT 兜底的清理只能实际跑一次
  if [[ "$TEARDOWN_DONE" == "1" ]]; then return 0; fi
  TEARDOWN_DONE=1
  log "== 清理：拆两边限速 + 停云上进程（显式拆除，不依赖任何定时器）=="
  remove_shaper_up
  remove_shaper_down
  wslb "if [ -f /tmp/stage_a_keepalive.pid ]; then kill \$(cat /tmp/stage_a_keepalive.pid) 2>/dev/null || true; rm -f /tmp/stage_a_keepalive.pid; fi; echo 保活进程已收" >/dev/null 2>&1 || true
  ssh6 'if [ -f /tmp/tc_stats_server.pid ]; then sudo kill $(cat /tmp/tc_stats_server.pid) 2>/dev/null || true; sudo rm -f /tmp/tc_stats_server.pid; fi
        pkill -x iperf3 2>/dev/null || true
        pkill -f "measure_system[.]py worker" 2>/dev/null || true
        sleep 1
        echo "残余进程：iperf3=[$(pgrep -x iperf3 | tr "\n" " ")] worker=[$(pgrep -f "measure_system[.]py worker" | tr "\n" " ")]"
        echo "云上根队列：$(sudo tc qdisc show dev eth0 | head -1)"
        echo "云上监听：$(ss -tln | grep -E ":'"$WORKER_PORT"'|:'"$IPERF_PORT"'|:'"$TCSTAT_PORT"'" | tr "\n" " ")"' 2>&1 | tee -a "$LOG_FILE"
  log "本机 WSL 根队列：$(wslr tc qdisc show dev eth0 2>&1 | head -1)"
}

cleanup_trap() {
  if [[ -n "$POLL_PID" ]]; then touch "$STOPFILE" 2>/dev/null; wait "$POLL_PID" 2>/dev/null; fi
  rm -f "$STOPFILE" "$TCSTAT_SRC" 2>/dev/null || true
  if [[ "$KEEP_ON_EXIT" == "0" ]]; then teardown_all; fi
}
# 【为什么要分开挂 INT/TERM 并显式 exit】bash 的 trap 处理函数返回后会**继续跑主流程**，
# 不 exit 就等于"收到终止信号只清理、不停止"——本轮冒烟实测：被 timeout 发 TERM 之后，
# 脚本在限速已被拆掉的情况下又跑完了一轮，overlimits 增量因此变成负数。
trap 'cleanup_trap; exit 130' INT
trap 'cleanup_trap; exit 143' TERM
trap 'cleanup_trap' EXIT

# =============================================================================
# 三、解析小工具（数字只从产物里取，绝不手打）
# =============================================================================
is_num() { [[ "${1:-}" =~ ^-?[0-9]+([.][0-9]+)?$ ]]; }
pct() { # pct A B → (A−B)/B×100；任一不是数或 B=0 就返回空
  local a="$1" b="$2"
  if is_num "$a" && is_num "$b" && awk -v b="$b" 'BEGIN{exit !(b!=0)}'; then
    awk -v a="$a" -v b="$b" 'BEGIN{printf "%.2f", (a-b)/b*100}'
  else printf ''; fi
}
ge() { awk -v a="${1:-nan}" -v b="${2:-nan}" 'BEGIN{ if (a+0==a && b+0==b) exit !(a>=b); exit 1 }'; }
tool_line() { grep -m1 -E "$2" "$1" 2>/dev/null || true; }
parse_a2() { printf '%s\n' "$1" | sed -n 's/^A2,[^,]*,实测 \([0-9.]*\) Mbit\/s \/ 设定 \([0-9.]*\) （偏差 \(-\{0,1\}[0-9.]*\)%）.*/\1 \2 \3/p'; }
parse_a3() { printf '%s\n' "$1" | sed -n 's/^A3,[^,]*,overlimits +\([0-9]*\)，dropped +\([0-9]*\).*/\1 \2/p'; }
parse_a4() { printf '%s\n' "$1" | sed -n 's/^A4,[^,]*,1 路 \([0-9.]*\) Mbit\/s → [0-9] 路时每路 \([0-9.]*\) Mbit\/s（比值 \([0-9.]*\)×；[0-9] 路合计 \([0-9.]*\) Mbit\/s）.*/\1 \2 \3 \4/p'; }
ipf_mbps() { # $1=json $2=sent|received
  awk -v want="$2" '
    BEGIN{ if (want=="received" || want=="recv") want="recv"; else want="sent" }
    /"sum_sent"/{f="sent"} /"sum_received"/{f="recv"}
    f==want && /bits_per_second/{ gsub(/[^0-9.]/,"",$2); if ($2!="") {printf "%.6f", $2/1e6; exit} }
  ' "$1" 2>/dev/null
}

# 本轮"不限速基线"：拆掉限速后，沿**同一个方向**量一小段。
# 【为什么每轮都要量】链路能力本身随时间变（本机实测：11 分钟内上传 13.4 → 5.6 Mbit/s）。
# 只有同轮的基线才能说明"本轮测到的是限速器的值，还是链路本来就那么慢"。
baseline_iperf() { # $1=dir $2=json 落地路径 → 打印 "<sent> <received>"
  local dir="$1" out="$2" args=(-c "$CLOUD4" -p "$IPERF_PORT" -n "$BASELINE_BYTES" -J)
  [[ "$dir" == "down" ]] && args+=(-R)
  wslr iperf3 "${args[@]}" > "$out" 2>/dev/null
  printf '%s %s' "$(ipf_mbps "$out" sent)" "$(ipf_mbps "$out" received)"
}

# =============================================================================
# 四、轮询（轮内按 5 s 记一次根队列，用来抓"限速中途消失"）
# =============================================================================
poll_start() {
  local dir="$1" out="$2"
  rm -f "$STOPFILE"; : > "$out"
  (
    while [[ ! -f "$STOPFILE" ]]; do
      printf '%s %s\n' "$(date -u +%H:%M:%SZ)" "$(read_tc qdisc "$dir" | head -1)" >> "$out"
      sleep 5
    done
  ) &
  POLL_PID=$!
}
poll_stop() { touch "$STOPFILE" 2>/dev/null; wait "$POLL_PID" 2>/dev/null; POLL_PID=""; rm -f "$STOPFILE"; }

archive_snap() { # $1=dir $2=tag $3=run_id $4=before|after
  local dir="$1" tag="$2" rid="$3" when="$4"
  read_tc qdisc  "$dir" > "$OUT_DIR/${tag}_${rid}_tc_qdisc_${when}.txt"  2>&1
  read_tc class  "$dir" > "$OUT_DIR/${tag}_${rid}_tc_class_${when}.txt"  2>&1
  read_tc filter "$dir" > "$OUT_DIR/${tag}_${rid}_tc_filter_${when}.txt" 2>&1
}

# =============================================================================
# 五、单轮
# =============================================================================
# 返回值：0 = 有效轮；1 = 本轮作废（已重跑）
run_round() {
  local dir="$1" idx="$2"
  local tag; tag="$(printf '%s%02d' "$dir" "$idx")"
  local attempt rid log_line rc

  for (( attempt=1; attempt<=MAX_ATTEMPTS; attempt++ )); do
    rid="$(date -u +%Y%m%dT%H%M%SZ)"
    local p="$OUT_DIR/${tag}_${rid}"
    local reason="" note=""

    # ---- 本轮"不限速基线"：先拆掉限速，沿同一方向量一小段（4 MB）----
    # 【为什么每轮都量、且放在最前面】链路能力本身随时间剧烈变（本机实测：同一小时内
    # 上传 13.4 → 5.6 Mbit/s）。只有同轮的基线才能回答"本轮测到的是限速器的值，
    # 还是链路本来就那么慢"——限速值定得比链路能力还高时，这两件事靠吞吐根本分不开。
    remove_shaper_any "$dir"; sleep 1
    local base_s base_r
    read -r base_s base_r <<< "$(baseline_iperf "$dir" "${p}_baseline_iperf3.json")"

    # ---- ① 装限速 → 断言在位（失败先重设再断言；仍失败则本轮不许开测）----
    install_shaper_any "$dir"; sleep 1
    if ! assert_shaper "$dir" >/tmp/.stage_a_assert 2>&1; then
      log "$tag/$rid 断言失败：$(cat /tmp/.stage_a_assert) → 重设限速后重试"
      install_shaper_any "$dir"
      sleep 1
      if ! assert_shaper "$dir" >/tmp/.stage_a_assert 2>&1; then
        reason="限速不在位：$(cat /tmp/.stage_a_assert)"
      fi
    fi
    # ---- ③ 计数器可读自检（带 -s 才会有 overlimits 那一行）----
    if [[ -z "$reason" ]] && ! assert_stats_readable "$dir"; then
      install_shaper_any "$dir"; sleep 1
      assert_stats_readable "$dir" || reason="tc 统计读不到 overlimits（可能漏了 -s 或仪表挂了）"
    fi
    # ---- 功能探测：确认限速真的在掐流量，而不是"规则装上了但没生效" ----
    if [[ -z "$reason" ]] && ! probe_shaper "$dir" "${p}_probe.txt"; then
      install_shaper_any "$dir"; sleep 1
      probe_shaper "$dir" "${p}_probe.txt" || reason="功能探测失败：限速未掐住流量（overlimits 不涨）"
    fi
    if [[ -n "$reason" ]]; then
      void_round "$dir" "$idx" "$tag" "$rid" "attempt${attempt}" "$reason"
      continue
    fi

    # ---- 本轮基线：tc 原文存档 + 计数器 ----
    archive_snap "$dir" "$tag" "$rid" before
    local qb qa cb fb oc0 dr0 oc1 dr1 fs0 fs1
    qb="$(cat "$OUT_DIR/${tag}_${rid}_tc_qdisc_before.txt")"
    cb="$(cat "$OUT_DIR/${tag}_${rid}_tc_class_before.txt")"
    fb="$(cat "$OUT_DIR/${tag}_${rid}_tc_filter_before.txt")"
    oc0="$(counter_sum "$qb" overlimits)"; dr0="$(counter_sum "$qb" dropped)"; fs0="$(filter_success_sum "$fb")"

    log "$tag/$rid 开始（第 ${attempt} 次尝试）：本轮不限速基线 ${base_r} Mbit/s（发送端 ${base_s}）；限速=在位，overlimits 基线 ${oc0}，filter success 基线 ${fs0}"

    # ---- ⑤ 轮内轮询 + 电池 + 同会话 iperf3 ----
    poll_start "$dir" "${p}_qdisc_poll.txt"
    local before_files after_files tool_csv tool_json
    before_files="$(ls "$OUT_DIR" | sort)"

    local batt_args=(
      --target "http://$CLOUD4:$WORKER_PORT"
      --iface eth0
      --tc-rate-mbps "$( [[ "$dir" == up ]] && echo "$RATE_UP" || echo "$RATE_DOWN" )"
      --shaped-direction "$( [[ "$dir" == up ]] && echo upload || echo download )"
      --sizes-mb $SIZES_MB
      --rate-test-mb "$PAYLOAD_MB"
      --rate-test-concurrency "$CONCURRENCY"
      --out-dir /opt/stage_a_out
    )
    # 【为什么这里手工拼引号】数组展开成字符串时元素之间的边界会丢：`--tc-cmd curl -s -m 10 URL`
    # 会被 argparse 拆成 `--tc-cmd curl` + 三个无法识别的参数（实测报 unrecognized arguments: -s -m 10）。
    local tc_cmd_arg=""
    [[ "$dir" == "down" ]] && tc_cmd_arg="--tc-cmd 'curl -s -m 10 http://$CLOUD4/tc/qdisc'"

    wslb "cd /opt/stage_a_sim && python3 k8s_test/measure_system.py transfer ${batt_args[*]} $tc_cmd_arg" \
      > "${p}_battery_stdout.txt" 2>&1
    rc=$?

    # 同会话第三方交叉验证：载荷与工具 A2 逐字节同量（-n 8388608），方向匹配
    local ipf_args=(-c "$CLOUD4" -p "$IPERF_PORT" -n "$PAYLOAD_BYTES" -J)
    [[ "$dir" == "down" ]] && ipf_args+=(-R)
    wslr iperf3 "${ipf_args[@]}" > "${p}_iperf3.json" 2>"${p}_iperf3.err"
    poll_stop

    # ---- 测量后存档 + 计数器 ----
    archive_snap "$dir" "$tag" "$rid" after
    qa="$(cat "$OUT_DIR/${tag}_${rid}_tc_qdisc_after.txt")"
    fb="$(cat "$OUT_DIR/${tag}_${rid}_tc_filter_before.txt")"
    local fa; fa="$(cat "$OUT_DIR/${tag}_${rid}_tc_filter_after.txt")"
    oc1="$(counter_sum "$qa" overlimits)"; dr1="$(counter_sum "$qa" dropped)"; fs1="$(filter_success_sum "$fa")"

    # ---- 工具产物的副本 + 逐秒曲线 ----
    after_files="$(ls "$OUT_DIR" | sort)"
    tool_csv="$(comm -13 <(printf '%s\n' "$before_files") <(printf '%s\n' "$after_files") | grep '_battery\.csv$' | head -1)"
    tool_json="$(comm -13 <(printf '%s\n' "$before_files") <(printf '%s\n' "$after_files") | grep '_battery\.json$' | head -1)"
    [[ -n "$tool_csv" ]]  && cp -f "$OUT_DIR/$tool_csv"  "${p}_battery.csv"
    [[ -n "$tool_json" ]] && cp -f "$OUT_DIR/$tool_json" "${p}_battery.json"
    wslr python3 /opt/stage_a_interval.py "/opt/stage_a_out/$(basename "${p}_iperf3.json")" \
      > "${p}_iperf3_intervals.txt" 2>&1

    # ---- 判定 ----
    local a1v a1f a2l a2p a3l a3p a4l a4p
    a1v=""; a1f=""; a2l=""; a3l=""; a4l=""
    [[ -n "$tool_csv" ]] && {
      a1v="$(tool_line "$OUT_DIR/$tool_csv" '^A1-summary,' | awk -F',' '{print $4}')"
      a1f="$(grep -c '^A1,.*,FAIL,' "$OUT_DIR/$tool_csv" || true)"
      a2l="$(tool_line "$OUT_DIR/$tool_csv" "^A2,$( [[ "$dir" == up ]] && echo 上传 || echo 下传 )吞吐,")"
      a3l="$(tool_line "$OUT_DIR/$tool_csv" '^A3,')"
      a4l="$(tool_line "$OUT_DIR/$tool_csv" ',1 路 vs [0-9] 路 单路吞吐,')"
    }
    local a2v a2dev a3oc a3dr a4s a4m a4r a4agg
    a2v=""; a2dev=""; a3oc=""; a3dr=""; a4s=""; a4m=""; a4r=""; a4agg=""
    [[ -n "$a2l" ]] && read -r a2v _ a2dev <<< "$(parse_a2 "$a2l")"
    [[ -n "$a3l" ]] && read -r a3oc a3dr <<< "$(parse_a3 "$a3l")"
    [[ -n "$a4l" ]] && read -r a4s a4m a4r a4agg <<< "$(parse_a4 "$a4l")"

    local isent irecv dsent drecv ocD drD fsD
    isent="$(ipf_mbps "${p}_iperf3.json" sent)"
    irecv="$(ipf_mbps "${p}_iperf3.json" received)"
    dsent="$(pct "$a2v" "$isent")"; drecv="$(pct "$a2v" "$irecv")"
    ocD=$((oc1-oc0)); drD=$((dr1-dr0)); fsD=$((fs1-fs0))

    # ---- 作废条件（只看"测量是否可信"，不看"结果是否达标"）----
    if [[ -n "$a1v" && "$a1v" != "PASS" ]]; then reason="A1 字节一致性 FAIL"; fi
    if [[ -z "$reason" && -n "$a1f" && "$a1f" != "0" ]]; then reason="A1 有 ${a1f} 项 FAIL"; fi
    if [[ -z "$reason" && -z "$a2v" ]]; then reason="工具 A2 无有效读数（请求失败或 CSV 缺行）"; fi
    if [[ -z "$reason" && -n "$a3oc" && "$a3oc" == "0" ]]; then reason="工具 A3 overlimits 增量 = 0"; fi
    if [[ -z "$reason" && "$ocD" -le 0 ]]; then reason="本轮 overlimits 增量为 $ocD"; fi
    if [[ -z "$reason" && -n "$isent" && -z "$irecv" ]]; then reason="iperf3 无有效读数"; fi
    # 轮内轮询：一次 htb 都没看到 → 没测到限速；看到 mq → 限速中途被拆
    local poll_htb poll_mq
    poll_htb="$(grep -c 'qdisc htb 1: root' "${p}_qdisc_poll.txt" || true)"
    poll_mq="$(grep -c 'qdisc mq' "${p}_qdisc_poll.txt" || true)"
    if [[ -z "$reason" && "$poll_htb" == "0" ]]; then reason="轮内轮询没看到一次 htb 根队列"; fi
    if [[ -z "$reason" && "$poll_mq" != "0" ]]; then
      reason="轮内轮询发现 ${poll_mq} 次根队列变成 mq（限速在测量过程中被拆）"
    fi
    # 并发段对账：3 路合计**远超过限速设定值**（>1.8×）→ 说明这段根本没被同一个瓶颈限住。
    # 【为什么不能拿"合计 vs 单路"当判据】单条 TCP 流在限速链路上往往吃不饱（实测单路 3.2~5.6
    # Mbit/s 都能出现，而 3 路合计稳定贴住 6 Mbit 的设定值），于是"合计 > 单路"是**正常**现象，
    # 拿它作废会误杀好轮（本轮冒烟就这样误杀过一次）。真正说明限速不在位的是"3 路各自跑在
    # 链路速度上、合计远超限速值"：上一批的坏轮 do06 是 2.746/1.5 = 1.83×、up05 是 49.3/6 = 8.2×。
    local rate_here; rate_here="$( [[ "$dir" == up ]] && echo "$RATE_UP" || echo "$RATE_DOWN" )"
    if [[ -z "$reason" && -n "$a4agg" ]] \
       && awk -v a="$a4agg" -v r="$rate_here" 'BEGIN{exit !(a>r*1.8)}'; then
      reason="并发段 3 路合计 $a4agg Mbit/s 是限速设定值 ${rate_here} 的 $(awk -v a="$a4agg" -v r="$rate_here" 'BEGIN{printf "%.2f", a/r}')× → 并发期间限速不在位"
    fi

    if [[ -n "$reason" ]]; then
      void_round "$dir" "$idx" "$tag" "$rid" "attempt${attempt}" "$reason"
      continue
    fi

    # ---- 有效轮：三项判据逐条判定 ----
    local c1 c2s c2r c3 r_agg_single r_agg_rate
    if [[ "$ocD" -gt 0 && "$drD" == "0" && -n "$a3oc" && "$a3oc" -gt 0 ]]; then c1="PASS"; else c1="FAIL"; fi
    c2s="$(verdict_tol "$dsent")"; c2r="$(verdict_tol "$drecv")"
    if is_num "$a4r" && ge "$a4r" 1.5; then c3="PASS"; else c3="FAIL"; fi
    r_agg_single="$(pct "$a4agg" "$a4s")"       # 诊断用：(3 路合计 − 单路)/单路
    r_agg_rate="$(pct "$a4agg" "$rate_here")"   # 诊断用：(3 路合计 − 限速值)/限速值

    printf '%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n' \
      "$dir" "$idx" "$tag" "$rid" "$attempt" \
      "${base_s:-}" "${base_r:-}" "${a2v:-}" "${a2dev:-}" "${isent:-}" "${irecv:-}" "${dsent:-}" "${drecv:-}" \
      "$ocD" "$drD" "${a3oc:-}" "${a3dr:-}" "$fsD" \
      "${a4s:-}" "${a4m:-}" "${a4r:-}" "${a4agg:-}" "${r_agg_single:-}" "${r_agg_rate:-}" \
      "${a1v:-}" "${a1f:-}" "$c1" "$c2s" "$c2r" "$c3" \
      "$tool_csv" "${note:-}" >> "$ROUNDS_CSV"

    log "$tag/$rid 有效：不限速基线 ${base_r} Mbit/s | 工具 ${a2v} Mbit/s（偏差 ${a2dev}%）| iperf3 发送 ${isent} / 接收 ${irecv}（差 ${dsent}% / ${drecv}%）| overlimits +${ocD}，dropped +${drD}，filter success +${fsD} | A4 比值 ${a4r} | 判据 ①${c1} ②发${c2s}/收${c2r} ③${c3}"
    return 0
  done

  log "$tag 连续 ${MAX_ATTEMPTS} 次尝试都作废，停止本阶段（不静默跳过）"
  return 2
}

install_shaper_any() { [[ "$1" == "up" ]] && install_shaper_up || install_shaper_down; }
remove_shaper_any()  { [[ "$1" == "up" ]] && remove_shaper_up  || remove_shaper_down; }

verdict_tol() { # 与同会话 iperf3 的差 → NEW/PASS(≤2%) / MARGINAL(≤5%) / FAIL
  local d="$1"
  if ! is_num "$d"; then printf 'n/a'; return; fi
  awk -v d="$d" 'BEGIN{ a=(d<0?-d:d); if (a<=2) print "PASS"; else if (a<=5) print "MARGINAL"; else print "FAIL" }'
}

void_round() { # $1=dir $2=idx $3=tag $4=rid $5=attempt $6=reason
  printf '%s,%s,%s,%s,%s,"%s"\n' "$1" "$2" "$3" "$4" "$5" "$6" >> "$VOIDS_CSV"
  printf '作废：轮次=%s run_id=%s 原因=%s\n' "$3" "$4" "$6" > "$OUT_DIR/${3}_${4}_VOID.txt"
  log "!! $3/$4 作废（原因：$6）"
}

# =============================================================================
# 六、主流程
# =============================================================================
run_phase() {
  local dir="$1" n="$2" i rc valid=0
  log "===== 阶段 $dir：目标 $n 轮有效 ====="
  # 阶段开始前把另一个方向的规则和本方向的旧规则都清掉，保证 overlimits 归属唯一
  remove_shaper_up; remove_shaper_down; sleep 1
  install_shaper_any "$dir"; sleep 1
  if ! assert_shaper "$dir" >/tmp/.stage_a_assert 2>&1; then
    install_shaper_any "$dir"; sleep 1
    assert_shaper "$dir" >/tmp/.stage_a_assert 2>&1 || die "阶段 $dir 起手限速断言失败：$(cat /tmp/.stage_a_assert)"
  fi
  log "阶段 $dir 限速已装并通过断言"

  i=1
  while [[ $valid -lt $n ]]; do
    run_round "$dir" "$i"; rc=$?
    if [[ $rc -eq 0 ]]; then valid=$((valid+1)); i=$((i+1));
    elif [[ $rc -eq 2 ]]; then die "阶段 $dir 在第 $i 轮连续作废，停止"; fi
  done
  log "===== 阶段 $dir 完成：$valid 轮有效 ====="
}

main() {
  {
    echo "phase,round_idx,tag,run_id,attempt,base_sent_mbps,base_recv_mbps,tool_a2_mbps,tool_dev_pct,ipf_sent_mbps,ipf_recv_mbps,diff_vs_sent_pct,diff_vs_recv_pct,oc_delta,dropped_delta,tool_a3_oc,tool_a3_dropped,filter_success_delta,a4_single_mbps,a4_multi_each_mbps,a4_ratio,a4_agg_mbps,a4_agg_over_single_pct,a4_agg_over_rate_pct,a1_verdict,a1_fails,c1_shaper_valid,c2_vs_sent,c2_vs_recv,c3_separable,tool_csv,note"
  } > "$ROUNDS_CSV"
  { echo "phase,round_idx,tag,run_id,attempt,reason"; } > "$VOIDS_CSV"

  if [[ "$TEARDOWN_ONLY" == "1" ]]; then
    teardown_all
    KEEP_ON_EXIT=1
    log "只做清理，已结束"
    return 0
  fi

  write_provenance
  log "批次 $BATCH_ID 开始：phase=$PHASE up=$UP_ROUNDS down=$DOWN_ROUNDS payload=${PAYLOAD_MB}MB"
  [[ "$SKIP_SETUP" == "1" ]] || setup_all
  [[ "$SKIP_SETUP" == "1" ]] && log "跳过部署（--skip-setup）"

  # 起手基线：确认链路本身没被限速（否则"限速生效"无法与"链路本来就慢"区分）
  remove_shaper_up; remove_shaper_down; sleep 1
  local base; base="$(baseline_iperf up "${OUT_DIR}/batch_${BATCH_ID}_baseline_up.json" | awk '{printf "%s", $2}')"
  log "起手不限速基线（上传 ${BASELINE_MB} MB）：${base} Mbit/s"
  echo "起手不限速基线（上传 ${BASELINE_MB} MB）：${base} Mbit/s" >> "$PROV_FILE"

  case "$PHASE" in
    up)   run_phase up "$UP_ROUNDS";;
    down) run_phase down "$DOWN_ROUNDS";;
    both) run_phase up "$UP_ROUNDS"; run_phase down "$DOWN_ROUNDS";;
    *) die "phase 只能是 up/down/both";;
  esac

  log "批次完成。有效轮 $(($(wc -l < "$ROUNDS_CSV") - 1))，作废轮 $(($(wc -l < "$VOIDS_CSV") - 1))"
  log "逐轮判定表：$ROUNDS_CSV"
  return 0
}

main

#!/usr/bin/env bash
# =============================================================================
# run_b1.sh —— 阶段 B1：在两台真实 GPU 服务器上跑通"前处理—生成"拆分服务
#
# 本脚本**只**回答四件事（B1_PLAN.md §1），多一件都不做：
#   4a 拆分式服务能不能跑起来
#   4b KV 是否可证实地传到了另一端（按"直方图增量"口径，见下）
#   4c 输出是否正确（按 **token 编号** 逐位比对，不是比显示文本）
#   4d 记录是否完整
#
# 它**不产生任何性能结论**：vLLM 官方把拆分式前处理标为实验功能，并明确说它
# 不直接提升总吞吐。B1 只问"能不能跑、传没传到、对不对、记全没有"。
#
# ---------------------------------------------------------------------------
# 4b 的口径（2026-10-07 修正，最容易搞错的一条）
# ---------------------------------------------------------------------------
# `vllm:nixl_bytes_transferred` 与传输耗时指标是 **直方图（histogram）**，不是计数器。
# Prometheus 的直方图会展开成 `_bucket` / `_count` / `_sum` 三条序列，而且官方说明
# **样本按"通知次数"统计，不等于请求次数**。因此：
#   * "指标名出现过" **不算证据**（进程一起来名字就有了，值全是 0）；
#   * 必须比 **运行前后 `_count`（样本数）与 `_sum`（字节总和）的增量**；
#   * 必须**保留全部标签**并落盘**原始文本**；
#   * **前处理端与生成端都要抓**。
# 这三条由 b1_metrics_delta.py 执行，raw 快照由 grab_metrics 原样落盘。
#
# ---------------------------------------------------------------------------
# 不许混为一谈的三样东西（写报告时逐条对照）
# ---------------------------------------------------------------------------
#   `kv_cache_size_tokens`  = 缓存**容量**信息        ❌ 不是某次请求实际传的字节
#   理论 KV 估算            = 量级参考                ❌ 不是实测传输量
#   显存占用                = 资源消耗                ❌ 不是传输量
#
# ---------------------------------------------------------------------------
# 记录纪律（B1_PLAN.md §4.2，硬约束）
# ---------------------------------------------------------------------------
#   R1 每次尝试都落盘，带 run_id + attempt 序号（只增不减）
#   R2 失败记录不删、不覆盖；新一次尝试写进新的 attempt 目录
#   R3 只有"开测之前的前置检查未通过"才算"未开测"，可以重试（但仍要留记录）
#   R4 一旦发出第一条**正式测量请求**，失败就是最终结果，不得重跑顶替
#   R5 汇总时失败轮计入分母
#   R6 本脚本**不得**出现"检测到失败 → 自动重跑"的代码路径
#      （run_stage_a_repeat.sh 里的"作废即重跑"分支是反面教材，不要复用）
#   R7 每次尝试记录 run_id/attempt/UTC 起止/退出码/失败原因/两端版本/依赖清单/
#      模型 revision 与哈希/连接器版本/机型/内网/时钟偏差
#   R8 收尾留下"服务已停、无残留"的证据
#
# 【冒烟请求 vs 正式测量请求】的分界（B1_PLAN.md §4.2 修正）
#   * 拆分服务起来后的**第一条请求 = 安装冒烟检查**，落 `smoke/` 子目录，**单独留档**，
#     **不混进正式请求数据**；它失败算"还没开测"（R3），可以修完重来并留记录。
#   * `measure` 阶段发出的请求才是**正式测量请求**；从这一条起按 R4，
#     失败即最终结果，**不重跑、不顶替**。
#
# 用法：
#   bash sim/k8s_test/run_b1.sh init            # 建本次 run 的目录与记录骨架
#   bash sim/k8s_test/run_b1.sh env             # 清点 + 云元数据 + 装环境 + 取官方脚本
#   bash sim/k8s_test/run_b1.sh digests         # 锁定版本：vLLM/NIXL wheel sha256 + 模型 revision
#   bash sim/k8s_test/run_b1.sh model           # 两端并行下模型，再比对整目录哈希
#   bash sim/k8s_test/run_b1.sh netbw           # 内网链路原始容量（环境事实，不是服务性能）
#   bash sim/k8s_test/run_b1.sh baseline        # 4c 对照臂：B 上单机完整服务（重复 3 次）
#   bash sim/k8s_test/run_b1.sh precheck        # 起三件套 + 抓 /metrics + 冒烟单请求 + 内网路由核实
#   bash sim/k8s_test/run_b1.sh netcapture      # 抓包：直接看 KV 走的是哪张网卡（需 precheck 的服务在跑）
#   bash sim/k8s_test/run_b1.sh measure         # 正式测量（**开测**）
#   bash sim/k8s_test/run_b1.sh compare         # 逐 token 比对（4c）
#   bash sim/k8s_test/run_b1.sh collect         # 拉回日志 + 远端 PID 台账
#   bash sim/k8s_test/run_b1.sh pids            # 只打印两端此刻的 B1 相关 PID（孤儿进程排查用）
#   bash sim/k8s_test/run_b1.sh stop            # 停掉两台上全部 B1 服务（**按 PID**，不用宽泛 pkill）
#   bash sim/k8s_test/run_b1.sh cleanup-check   # 4d 收尾：进程/端口/显存 + 公网不可达 + 私钥泄漏
#   bash sim/k8s_test/run_b1.sh budget          # 打印时间/费用台账与上限
#
# 环境变量：
#   RUN_ID    本次 run 的编号（默认从 sim/results/b1/CURRENT_RUN_ID 读）
#   ATTEMPT   尝试序号（默认 1；**换一次尝试就 +1，绝不覆盖旧目录**）
#   B1_DRY=1  只打印将要执行的远端命令，不真的执行
#   MODEL_WAIT_MIN   model 阶段最长等待分钟数（默认 360）
#   B1_CAP_SINGLE_HOURS / B1_CAP_TOTAL_CARD_HOURS / B1_UNIT_CNY  见 §费用
# =============================================================================

set -uo pipefail

# ---------------------------------------------------------------- 常量与路径
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd -P)"     # 仓库根 E:/代码项目文件/论文
RES="${ROOT}/sim/results/b1"

# ⚠️ 私钥文件名**含空格与括号**，所以必须是**一个整体**传进数组并处处加引号。
#    私钥内容永不打印、永不写进产物（cleanup-check 会扫一遍）。
SSH_KEY="/d/Documents/API/_网络感应 (1).pem"
SSH_OPTS=(
  -i "${SSH_KEY}"
  -o IdentitiesOnly=yes
  -o StrictHostKeyChecking=no
  -o UserKnownHostsFile=/tmp/kh_net
  -o BatchMode=yes
  -o ConnectTimeout=20
  -o LogLevel=ERROR
  -o ServerAliveInterval=30
  -o ServerAliveCountMax=6
)

# ------------------------------------------------- 真实机器事实（2026-10-07 第二轮在新机器上核实）
# 来源：云元数据服务 http://100.100.100.200/latest/meta-data/<key>（**不是靠猜**）。
# 上一批机器（乌兰察布，`172.16.206.251/252`）**已释放**，那批的记录见 B1_REPORT.md。
# 本段是**新一批两台机器**的实测身份；第三轮换机器时**只改这一段**即可。
CLOUD="阿里云（Alibaba Cloud）"
REGION_ID="cn-heyuan"; ZONE_ID="cn-heyuan-b"
VPC_ID="vpc-f8zxd5ciman5fejiit9gx"; VSWITCH_ID="vsw-f8zmfknugi51fr1hn7b5h"; VPC_CIDR="172.16.0.0/12"
VSWITCH_CIDR="172.29.16.0/20"
INSTANCE_TYPE="ecs.gn7i-c8g1.2xlarge"
ALIYUN_ACCOUNT="1135257242649102"
IMAGE_ID="ubuntu_24_04_x64_100G_with_gpu_driver_and_cuda_alibase_20260519.vhd"
A_INST_ID="i-f8z4ggqwhyqz0r6lp1mj"; B_INST_ID="i-f8z4ggqwhyqz0r6lp1mi"

A_PUB="8.155.133.216";  A_IN="172.29.31.52";  A_HOST="iZf8z4ggqwhyqz0r6lp1mjZ"
B_PUB="8.155.131.183";  B_IN="172.29.31.51";  B_HOST="iZf8z4ggqwhyqz0r6lp1miZ"

# ------------------------------------------------- 费用与时间上限（**硬约束**）
# ⚠️ 口径声明：**"机器已租 ⇒ 增量成本为 0" 是错的**。两台一直在计费，继续跑就是继续花钱。
# 计费方式（按量/包月）与单价：**本次未查到**（云元数据服务不暴露，控制台需登录，
# 本机无 aliyun CLI、无 RAM 角色）。因此金额上限只能写成公式，**不能编一个数**。
# 在单价未知的前提下，**可以强制执行的是"时间上限"**，所以把止损线建在时间上：
CAP_SINGLE_RUN_HOURS="${B1_CAP_SINGLE_HOURS:-4}"     # 单次连续运行上限（小时/台）
CAP_TOTAL_CARD_HOURS="${B1_CAP_TOTAL_CARD_HOURS:-16}" # 累计上限（卡时 = 2 台 × 小时）
UNIT_CNY="${B1_UNIT_CNY:-}"                            # 单价（元/卡/小时）；未核实 → 留空

# 远端固定目录
RDIR="/root/b1"
RVENV="${RDIR}/venv"
RMODEL="${RDIR}/models/Qwen2.5-1.5B-Instruct"
ROFFICIAL="${RDIR}/official"
RLOGS="${RDIR}/logs"

# 端口约定
PORT_PREFILL=8100      # A：前处理实例（绑 A 内网 IP）
PORT_DECODE=8200       # B：生成实例（绑 B 内网 IP）
PORT_PROXY=8192        # A：把请求路由给 P/D 的代理（只绑 127.0.0.1）
PORT_BASELINE=8300     # B：4c 对照臂（单机完整服务，绑 B 内网 IP）
SIDE_PORT=5600         # NIXL 握手侧信道（两端各自 5600，不同主机互不冲突）

# ------------------------------------------------- 模型（2026-10-07 第二轮改选 1.5B）
# 为什么**不用 7B**（三条理由，写进报告的同一段）：
#   ① B1 只验"流程通不通"，不验性能 —— 模型大小与四件事的结论无关；
#   ② 这两台内存只有 29.7 GB，7B 权重 15 GB、加载峰值约 30 GB，**太紧**（会 OOM 或换页）；
#   ③ 公网慢（实测 hf-mirror 1.82 MB/s(A) / 0.66 MB/s(B)），7B 要下 3.5–4 小时，纯白烧卡时。
# 1.5B 的 KV 在 4096 token 输入下约 117 MB（≈112 MiB），**足够证明"传没传过去"**：
#   2(K,V) × 28 层 × 2 个 KV 头 × 128 头维度 × 2 B(fp16) = 28,672 B/token。
MODEL_ID="Qwen/Qwen2.5-1.5B-Instruct"
# 官方 revision（hf-mirror Hub API 查得，2026-10-07）：两端必须锁到同一个 sha 上
MODEL_REV_EXPECT="989aa7980e4cf806f80c7fef2b1adb7bc71aa306"
# 理论 KV 量级（**只是量级参考**，不是实测传输量；报告里必须与实测分开写）
KV_THEORY_4096="4096 token ≈ 117.4 MB（112 MiB）"
VLLM_VER="0.31.0"
NIXL_VER="1.4.1"
MAX_TOKENS=64
N_BASELINE_REPEATS=3   # 先重复几次单机完整服务，确认基准自身稳定（B1_PLAN.md §2.7）
N_DISAGG_REPEATS=3     # 正式臂：先单请求，再 5 条 × N 次

# 两端逐字相同的服务参数（bind 地址由 b1_remote_serve.sh 的 B1_BIND_IP 提供）
SERVED_NAME="${MODEL_ID}"

# 模型/官方文件走哪条路取（GitHub 直连在本机不通，见仓库 AGENTS.md；Hub 走 hf-mirror）
HFHUB="https://hf-mirror.com"

# 本地 python（跑比对/分析脚本用）
PY_LOCAL="python3"
[[ -x "${ROOT}/sim/.venv/Scripts/python.exe" ]] && PY_LOCAL="${ROOT}/sim/.venv/Scripts/python.exe"

# ---------------------------------------------------------------- 小工具
ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { printf '[%s] %s\n' "$(ts)" "$*"; }
die() { printf '[%s] FATAL: %s\n' "$(ts)" "$*" >&2; exit 1; }

pub_of() { case "$1" in A|a) echo "${A_PUB}";; B|b) echo "${B_PUB}";; *) die "未知主机 $1";; esac; }
in_of()  { case "$1" in A|a) echo "${A_IN}";;  B|b) echo "${B_IN}";;  *) die "未知主机 $1";; esac; }
inst_of(){ case "$1" in A|a) echo "${A_INST_ID}";; B|b) echo "${B_INST_ID}";; *) die "未知主机 $1";; esac; }

run_id() {
  if [[ -n "${RUN_ID:-}" ]]; then echo "${RUN_ID}"; return; fi
  if [[ -f "${RES}/CURRENT_RUN_ID" ]]; then cat "${RES}/CURRENT_RUN_ID"; return; fi
  die "还没有 run_id：先跑 'run_b1.sh init'，或显式给 RUN_ID=..."
}
ATTEMPT="${ATTEMPT:-1}"
OUT=""

ensure_out() {
  [[ -n "${OUT}" ]] && return
  local rid; rid="$(run_id)"
  OUT="${RES}/${rid}/attempt${ATTEMPT}"
  mkdir -p "${OUT}"
}

# 每执行一段远端命令都留一行记录（R1/R7）。失败**不重跑**，只记录（R4）。
rec() {  # rec <host> <step> <utc_start> <utc_end> <exit_code> <status> <note>
  ensure_out
  local f="${OUT}/record.csv"
  [[ -f "$f" ]] || echo "run_id,attempt,host,step,utc_start,utc_end,exit_code,status,note" > "$f"
  printf '%s,%s,%s,%s,%s,%s,%s,%s,"%s"\n' \
    "$(run_id)" "${ATTEMPT}" "$1" "$2" "$3" "$4" "$5" "$6" "${7//\"/\"\"}" >> "$f"
}

# 记录纪律 R2 的机械保障：产物已存在就**拒绝写入**，逼调用者换一个 attempt 序号
no_clobber() {
  if [[ -e "$1" ]]; then
    log "拒绝覆盖已存在的产物：$1"
    log "按记录纪律 R2：换一次尝试再来 —— ATTEMPT=$((ATTEMPT + 1)) bash $0 <phase>"
    return 1
  fi
  return 0
}

# 在远端跑一段命令，输出同时进（a）终端（b）attempt 目录下的日志文件
rsh() {  # rsh <A|B> <logfile-name> <command...>
  local host="$1"; shift
  local name="$1"; shift
  ensure_out
  local ip; ip="$(pub_of "${host}")"
  local started; started="$(ts)"
  local f="${OUT}/${name}.log"
  no_clobber "$f" || return 1
  { echo "# ---- $name @ ${host} (${ip})  start=$(ts) ----"; } >> "$f"
  if [[ "${B1_DRY:-0}" == "1" ]]; then echo "DRY: ssh ${ip} :: $*"; return 0; fi
  ssh "${SSH_OPTS[@]}" "root@${ip}" "$@" >>"$f" 2>&1
  local rc=$?
  local ended; ended="$(ts)"
  echo "# ---- exit=${rc} end=${ended} ----" >> "$f"
  rec "${host}" "${name}" "${started}" "${ended}" "${rc}" "$([[ $rc -eq 0 ]] && echo OK || echo FAIL)" "cmd=$(echo "$*" | head -c 300)"
  tail -n 40 "$f"
  return $rc
}

# 同一个 step 名要跑两次（不同 attempt）时不撞 no_clobber：用 _2 后缀
rsh2() { local host="$1"; shift; local name="$1"; shift; rsh "${host}" "$name" "$@"; }

rpush() {  # rpush <A|B> <localfile> <remote-path>
  local host="$1" localf="$2" remotep="$3"
  local ip; ip="$(pub_of "${host}")"
  [[ "${B1_DRY:-0}" == "1" ]] && { echo "DRY: scp ${localf} -> ${ip}:${remotep}"; return 0; }
  scp -q "${SSH_OPTS[@]}" "${localf}" "root@${ip}:${remotep}"
}

rsh_script() {  # rsh_script <A|B> <logfile-name> <localfile> [args...]
  local host="$1" name="$2" localf="$3"; shift 3
  ensure_out
  local ip; ip="$(pub_of "${host}")"
  local started; started="$(ts)"
  local f="${OUT}/${name}.log"
  no_clobber "$f" || return 1
  { echo "# ---- ${name} @ ${host} (${ip}) start=${started} script=${localf} args=$* ----"; } >> "$f"
  if [[ "${B1_DRY:-0}" == "1" ]]; then echo "DRY: ssh ${ip} bash -s -- $* < ${localf}"; return 0; fi
  ssh "${SSH_OPTS[@]}" "root@${ip}" "bash -s -- $*" <"${localf}" >>"$f" 2>&1
  local rc=$?
  local ended; ended="$(ts)"
  echo "# ---- exit=${rc} end=${ended} ----" >> "$f"
  rec "${host}" "${name}" "${started}" "${ended}" "${rc}" "$([[ $rc -eq 0 ]] && echo OK || echo FAIL)" "script=${localf} args=$*"
  tail -n 40 "$f"
  return $rc
}

# 抓一次 /metrics 原始快照（append-only，原样存，**不加工**）
# ⚠️ 2026-10-07 修正（本轮才暴露的脚本 bug）：服务的**绑定地址是内网 IP**（这是设计要求，
#    不允许绑 0.0.0.0），但这里原来一直 curl `127.0.0.1` ⇒ **连不上**，表现为
#    "服务明明起来了（端口在听、日志打了 Application startup complete）、脚本却判它没起来"。
#    现在改成先试**本机内网 IP**，再退回 `127.0.0.1`
#    （代理是唯一**故意只绑回环**的组件，所以必须保留回环这条退路）。
grab_metrics() {  # grab_metrics <A|B> <port> <label>
  local host="$1" port="$2" label="$3"
  ensure_out
  local ip; ip="$(pub_of "${host}")"
  local myin; myin="$(in_of "${host}")"
  local started; started="$(ts)"
  local f="${OUT}/metrics_${label}_raw.txt"
  no_clobber "$f" || return 1
  if [[ "${B1_DRY:-0}" == "1" ]]; then echo "DRY: curl ${ip}:${port}/metrics -> ${f}"; return 0; fi
  ssh "${SSH_OPTS[@]}" "root@${ip}" "$(cat <<EOS
for a in ${myin} 127.0.0.1; do
  out=\$(curl -sS --max-time 20 http://\$a:${port}/metrics 2>/dev/null) || true
  if [[ -n "\$out" ]]; then echo "GRABBED_FROM=\$a"; printf '%s\n' "\$out"; exit 0; fi
done
echo "GRAB_FAILED 两端都试过（${myin} 与 127.0.0.1）都是空的"; exit 1
EOS
)" >"$f" 2>"${f}.err"
  local rc=$? bytes; bytes=$(wc -c <"$f")
  local ended; ended="$(ts)"
  rec "${host}" "metrics_${label}" "${started}" "${ended}" "${rc}" \
      "$([[ $rc -eq 0 ]] && echo OK || echo FAIL)" "port=${port} bytes=${bytes}"
  log "metrics[${label}] ${host}:${port} -> $(basename "$f") (${bytes} bytes)"
  return $rc
}

# 直方图增量比对（4b 的判据）。两端各跑一次。
mdelta() {  # mdelta <A|B> <label_t0> <label_t1> <中文名> <输出后缀>
  local host="$1" l0="$2" l1="$3" zh="$4" suf="$5"
  ensure_out
  local f="${OUT}/kv_delta_${suf}.txt"
  no_clobber "$f" || return 1
  if [[ "${B1_DRY:-0}" == "1" ]]; then echo "DRY: mdelta ${suf}"; return 0; fi
  "${PY_LOCAL}" "${SCRIPT_DIR}/b1_metrics_delta.py" \
      --t0 "${OUT}/metrics_${l0}_raw.txt" \
      --t1 "${OUT}/metrics_${l1}_raw.txt" \
      --label "${zh} ${host}（${l0} → ${l1}）" \
      --out "$f" --all-families || true
  echo "（完整报告见 $(basename "$f")）"
}

# 等到某个 HTTP 服务能用 / 健康检查返回 200。
# health_path 默认 /v1/models（vLLM 的 OpenAI 兼容路由）；
# ⚠️ 官方代理 toy_proxy_server.py **没有** /v1/models，只有 /healthcheck，
#    所以等代理时必须传 /healthcheck，否则会干等 240 s 然后误判"没起来"。
# ⚠️ 地址同样要试两个（本机内网 IP → 127.0.0.1），原因见 grab_metrics 上面的注释：
#    vLLM 实例绑内网 IP、代理绑回环，写死任何一个都会漏掉另一个。
wait_ready() {  # wait_ready <A|B> <port> <label> [health_path]
  local host="$1" port="$2" label="$3" path="${4:-/v1/models}"
  ensure_out
  local ip; ip="$(pub_of "${host}")"
  local myin; myin="$(in_of "${host}")"
  local started; started="$(ts)"
  local f="${OUT}/waitready_${label}.log"
  no_clobber "$f" || return 1
  if [[ "${B1_DRY:-0}" == "1" ]]; then echo "DRY: wait ${label}"; return 0; fi
  {
    echo "# 等待 ${label}：curl http://<${myin}|127.0.0.1>:${port}${path} 直到 200（最多 240 s）"
  } >>"$f"
  ssh "${SSH_OPTS[@]}" "root@${ip}" "$(cat <<EOS
for i in \$(seq 1 240); do
  for a in ${myin} 127.0.0.1; do
    code=\$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://\$a:${port}${path} || true)
    if [[ "\$code" == "200" ]]; then echo "READY ${label} after \${i}s (http://\$a:${port}${path})"; exit 0; fi
  done
  sleep 1
done
echo "NOT READY ${label} after 240s (${path}) —— 试过 ${myin} 与 127.0.0.1"; exit 1
EOS
)" >>"$f" 2>&1
  local rc=$?
  local ended; ended="$(ts)"
  rec "${host}" "waitready_${label}" "${started}" "${ended}" "${rc}" \
      "$([[ $rc -eq 0 ]] && echo OK || echo FAIL)" "port=${port} path=${path}"
  tail -n 3 "$f"
  return $rc
}

# ---------------------------------------------------------------- 时间/费用台账
# 从 record.csv 里取最早/最晚的时间戳。
#
# ⚠️ 2026-10-08 修掉的一个**静默失效**（发现于 4c 专项诊断收尾）：
#   原来这两处写的是 `$4` / `$5`，但 CSV 的列顺序是
#       run_id,attempt,host,step,utc_start,utc_end,exit_code,status,note
#   ⇒ **utc_start 是 $5、utc_end 是 $6**，而 `$4` 是 step（一般不含字母 T）。
#   后果链：`first` 恒为空 → 回退成"现在" → 已历时恒为 **0** →
#   **费用闸（CAP_SINGLE_RUN_HOURS / CAP_TOTAL_CARD_HOURS）永远不会触发**。
#   也就是说，之前所有"预算闸全程未触线"的说法都是**空的**——它想触发也触发不了。
#   这与 §10 第 37 条记过的"空集天真地通过校验"是同一类错误，所以这次改成：
#   **取不到就大声报出来**，不再静默回退成一个看起来正常的数。
record_ts() {  # record_ts <record.csv> <utc_start|utc_end>
  local rc="$1" col="$2" field
  case "$col" in
    utc_start) field=5 ;;
    utc_end)   field=6 ;;
    *) printf ''; return 0 ;;
  esac
  [[ -s "$rc" ]] || { printf ''; return 0; }
  local out n rows
  out="$(awk -F, -v f="$field" 'NR>1 && $f ~ /T/ {print $f}' "$rc" | sort)"
  n="$(printf '%s' "$out" | grep -c 'T' 2>/dev/null || true)"
  if [[ "${n:-0}" -eq 0 ]]; then
    rows=$(( $(wc -l < "$rc") - 1 ))
    if [[ "${rows:-0}" -gt 0 ]]; then
      log "‼ 时间台账异常：record.csv 有 ${rows} 行，却取不到任何 ${col}"
      log "   —— 列号或文件结构可能又变了；**不要相信本次的历时/卡时数字**"
      echo "# 时间台账异常：record.csv 有 ${rows} 行却取不到 ${col}（列号或结构变了）" >&2
    fi
    printf ''; return 0
  fi
  case "$col" in
    utc_start) printf '%s\n' "$out" | head -1 ;;
    utc_end)   printf '%s\n' "$out" | tail -1 ;;
  esac
}

budget_report() {
  ensure_out
  local f="${OUT}/budget.txt"
  local first="" last=""
  local rc="${OUT}/record.csv"
  if [[ -f "$rc" ]]; then
    first="$(record_ts "$rc" utc_start)"
    last="$(record_ts "$rc" utc_end)"
  fi
  [[ -z "$first" ]] && first="$(ts)"
  [[ -z "${last}" ]] && last="$(ts)"
  local now; now="$(ts)"
  local e0 e1 en
  e0="$(date -u -d "$first" +%s 2>/dev/null || echo 0)"
  e1="$(date -u -d "${last}" +%s 2>/dev/null || echo 0)"
  en="$(date -u -d "$now" +%s 2>/dev/null || echo 0)"
  # ⚠️ 2026-10-08 修：原来只算「now − first」，但 `last` 算出来却没被用上。
  #   对**已经跑完**的那一轮来说，`now` 可能比实验结束晚很多（收尾之后过了多久就算多久），
  #   于是台账会把"实验窗口"报大（实测：真实 0.097 h 被报成 0.45 h）。
  #   现在把两个数**分开报清楚**：
  #     · 实验窗口 = last − first   ← 这才是这一轮真正占用的机器时间
  #     · 距首条记录至今 = now − first ← 闸门用的口径（LIVE 时两者相同）
  #   超限判定仍用"至今"（对 LIVE 的闸门才正确），但金额按**实验窗口**估。
  local secs=$(( en - e0 )); [[ $secs -lt 0 ]] && secs=0
  local wsecs=$(( e1 - e0 )); [[ $wsecs -lt 0 ]] && wsecs=0
  local hours card_h whours wcard_h
  hours="$(awk -v s="$secs" 'BEGIN{printf "%.2f", s/3600}')"
  card_h="$(awk -v h="$hours" 'BEGIN{printf "%.2f", h*2}')"
  whours="$(awk -v s="$wsecs" 'BEGIN{printf "%.3f", s/3600}')"
  wcard_h="$(awk -v h="$whours" 'BEGIN{printf "%.3f", h*2}')"
  local over="否"
  awk -v c="$card_h" -v cap="${CAP_TOTAL_CARD_HOURS}" 'BEGIN{exit !(c>cap)}' && over="**是 —— 已超上限，按停止条件 S4/S5 应停止并拉回产物**"
  {
    echo "== B1 时间/费用台账（本次 run=${RUN_ID:-$(run_id)} attempt=${ATTEMPT}） =="
    echo "计量起点（record.csv 里最早的 utc_start）: ${first}"
    echo "record 最后一条的 utc_end               : ${last}"
    echo "台账生成时刻                            : ${now}"
    echo
    echo "**实验窗口（last − first）**              : ${whours} 小时/台  ⇒ **${wcard_h} 卡时**  ← 这一轮真正占用的机器时间"
    echo "距首条记录至今（now − first，闸门口径）    : ${hours} 小时/台  ⇒ ${card_h} 卡时"
    echo "  （两者不同是正常的：收尾之后又过去的时间会计进下面这个数，不计入实验窗口）"
    echo
    echo "-- 单价口径（**必须照实写，不许编**） --"
    echo "计费方式（按量/包月）: **未查到**（云元数据服务不暴露；控制台需登录；本机无 aliyun CLI、无 RAM 角色）"
    echo "实时账单余额/已出账金额: **未查到**（同上；本轮在实例内再次反复尝试过元数据与本地接口，均无此字段）"
    if [[ -n "${UNIT_CNY}" ]]; then
      local money; money="$(awk -v c="$wcard_h" -v u="$UNIT_CNY" 'BEGIN{printf "%.2f", c*u}')"
      echo "单价（作者提供）: ¥${UNIT_CNY}/卡/小时 → 按**实验窗口**估已产生 ≈ ¥${money}"
    else
      echo "单价: **未查到**，因此 **金额无法计算**。"
      echo "      → 在单价未知的前提下，可强制执行的止损线只能是「时间」（见下）。"
      echo "      → 一旦作者在控制台看到单价 P（元/卡/小时），凡超出上限即停（停止条件 S4）。"
    fi
    echo
    echo "-- 上限（硬约束） --"
    echo "单次连续运行上限 : ${CAP_SINGLE_RUN_HOURS} 小时/台"
    echo "累计上限         : ${CAP_TOTAL_CARD_HOURS} 卡时（= 2 台 × $((CAP_TOTAL_CARD_HOURS / 2)) 小时）"
    echo "是否已超累计上限 : ${over}"
    echo
    echo "⚠️ 口径提醒：**「机器已租 ⇒ 增量成本为 0」是错的**——两台一直在计费，继续跑就是继续花钱。"
    echo "⚠️ 方案里的 ¥400 是**用户给出的建议选项**，本文件**没有**把它当作已确认预算来叙述。"
  } >"$f" 2>&1
  cat "$f"
}

budget_gate() {  # 超上限就拒绝继续（返回非 0）
  ensure_out
  local rc="${OUT}/record.csv"
  [[ -f "$rc" ]] || return 0
  # ⚠️ 2026-10-08 修：这里原来也是 `$4`（＝step），导致 first 恒为空 ⇒ 直接 return 0
  #    ⇒ **这道闸形同不存在**。现在统一走 record_ts（列号已修正，且取不到会报警）。
  local first; first="$(record_ts "$rc" utc_start)"
  if [[ -z "$first" ]]; then
    # 取不到时间戳：**不再当成"还没开始"**（那是原来静默失效的根源）。
    # 按保守原则：文件里有数据行却读不出时间，说明台账结构可能变了，
    # 此时**不放行**更安全 —— 宁可停下让人看一眼，也不要让计费失控。
    if [[ "$(wc -l < "$rc")" -gt 1 ]]; then
      log "预算闸门：record.csv 有内容但读不出 utc_start —— 台账结构异常，**保守起见不放行**"
      return 1
    fi
    return 0
  fi
  local e0 en secs hours
  e0="$(date -u -d "$first" +%s 2>/dev/null || echo 0)"
  en="$(date -u +%s)"
  secs=$(( en - e0 ))
  hours="$(awk -v s="$secs" 'BEGIN{printf "%.3f", s/3600}')"
  awk -v h="$hours" -v cap="${CAP_SINGLE_RUN_HOURS}" 'BEGIN{exit !(h>cap)}' && {
    log "预算闸门：本次已历时 ${hours} h > 单次上限 ${CAP_SINGLE_RUN_HOURS} h —— 按停止条件 S5 停止"
    return 1
  }
  awk -v h="$hours" -v cap="${CAP_TOTAL_CARD_HOURS}" 'BEGIN{exit !(h*2>cap)}' && {
    log "预算闸门：已折算 $(awk -v h="$hours" 'BEGIN{printf "%.2f",h*2}') 卡时 > 累计上限 ${CAP_TOTAL_CARD_HOURS} —— 按 S4/S5 停止"
    return 1
  }
  return 0
}

# =============================================================================
# 阶段：init
# =============================================================================
phase_init() {
  # run_id 复用规则：一个 run_id 代表"一轮 B1 战役"，attempt 代表其中的第几次尝试。
  # 所以默认复用已有的 CURRENT_RUN_ID（换一次尝试只改 ATTEMPT，不改 run_id）；
  # 要开新的一轮用 `newrun`。
  local rid
  if [[ -n "${RUN_ID:-}" ]]; then
    rid="${RUN_ID}"
  elif [[ -f "${RES}/CURRENT_RUN_ID" ]]; then
    rid="$(cat "${RES}/CURRENT_RUN_ID")"
    log "复用已有 run_id=${rid}（要开新一轮：bash $0 newrun）"
  else
    rid="b1_$(date -u +%Y%m%dT%H%M%SZ)"
  fi
  mkdir -p "${RES}"
  echo "${rid}" > "${RES}/CURRENT_RUN_ID"
  OUT="${RES}/${rid}/attempt${ATTEMPT}"
  mkdir -p "${OUT}"
  rec "-" "init" "$(ts)" "$(ts)" 0 OK "建立 run/attempt 目录；run_id=${rid} attempt=${ATTEMPT}"
  log "run_id=${rid} attempt=${ATTEMPT}"
  log "产物目录：${OUT}"
  log "（想换一次尝试：ATTEMPT=$((ATTEMPT + 1)) bash $0 init —— 旧目录原样保留，绝不覆盖）"
}

phase_newrun() {
  RUN_ID="b1_$(date -u +%Y%m%dT%H%M%SZ)"
  phase_init
}

# =============================================================================
# 阶段：env —— 两端清点 + **云元数据** + 内网连通 + 时钟 + 装依赖 + 取官方脚本
# =============================================================================
phase_env() {
  ensure_out
  log "== 1/5 两端环境清点 =="
  rsh A inventory_A "$(cat <<'EOS'
set -x
hostname; date -u; uptime
nvidia-smi --query-gpu=name,memory.total,driver_version,compute_cap --format=csv,noheader
nproc; free -g | head -2; df -h /
python3 --version; pip3 --version
docker --version 2>/dev/null; which docker
ip -4 addr show | grep -E "inet "
ls -d /usr/local/cuda* 2>/dev/null
which nvcc gcc ninja iperf3 2>/dev/null
cat /etc/os-release | head -3
EOS
)" || true
  rsh B inventory_B "$(cat <<'EOS'
set -x
hostname; date -u; uptime
nvidia-smi --query-gpu=name,memory.total,driver_version,compute_cap --format=csv,noheader
nproc; free -g | head -2; df -h /
python3 --version; pip3 --version
docker --version 2>/dev/null
ip -4 addr show | grep -E "inet "
ls -d /usr/local/cuda* 2>/dev/null
which nvcc gcc ninja iperf3 2>/dev/null
cat /etc/os-release | head -3
EOS
)" || true

  log "== 2/5 云元数据（厂商/地域/可用区/私有网络/子网/机型 —— 用来替换方案里的「候选项」） =="
  rsh A cloud_meta_A "$(cloud_meta_snippet)" || true
  rsh B cloud_meta_B "$(cloud_meta_snippet)" || true
  cloud_meta_verdict

  log "== 3/5 内网连通性 + 任意端口可达性（真起监听再连，ping 不算证据） =="
  rsh A net_internal_A "echo '--- ping B ---'; ping -c 5 -W 2 ${B_IN}; \
    echo '--- 内网到 B 的路由 ---'; ip route get ${B_IN}" || true
  rsh B net_probe_listen "$(cat <<EOS
setsid nohup python3 -c "
import socket,time
socks=[]
for p in (5600,8200,8300,9101):
    s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
    s.bind(('0.0.0.0',p)); s.listen(4); socks.append(s)
print('listening 5600 8200 8300 9101', flush=True)
time.sleep(90)
" >/tmp/b1_portprobe.log 2>&1 </dev/null &
sleep 3; cat /tmp/b1_portprobe.log
EOS
)" || true
  rsh A net_probe_from_A "for p in ${SIDE_PORT} ${PORT_DECODE} ${PORT_BASELINE} 9101; do timeout 5 bash -c \"echo > /dev/tcp/${B_IN}/\$p\" 2>/dev/null && echo \"\$p 内网可达\" || echo \"\$p 不可达\"; done; \
    echo '--- 同一台 A 通过 B 的公网 IP 连同样的端口（应不可达） ---'; \
    for p in ${SIDE_PORT} 9101; do timeout 5 bash -c \"echo > /dev/tcp/${B_PUB}/\$p\" 2>/dev/null && echo \"公网 \$p 可达\" || echo \"公网 \$p 不可达\"; done" || true
  rsh B net_probe_cleanup "ss -ltnp | grep -E ':(5600|8200|8300|9101)' | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u | while read x; do kill \$x 2>/dev/null && echo killed \$x; done; sleep 1; ss -ltn | grep -E ':(5600|8200|8300|9101)' || echo '探测端口已全部释放'" || true

  log "== 4/5 时钟同步（本项目核心自变量是「状态有多旧」，时钟必须可信） =="
  rsh A clock_A "timedatectl 2>/dev/null | head -8; systemctl is-active chrony chronyd systemd-timesyncd 2>/dev/null; chronyc tracking 2>/dev/null | head -6 || true; date -u +%s.%N" || true
  rsh B clock_B "timedatectl 2>/dev/null | head -8; systemctl is-active chrony chronyd systemd-timesyncd 2>/dev/null; chronyc tracking 2>/dev/null | head -6 || true; date -u +%s.%N" || true

  log "== 5/5 装环境（venv + vLLM + NIXL）+ 取官方脚本 =="
  # 注意：日志文件名必须**带主机名**。早先两端都叫 setup_install.log，
  # 于是 B 的日志被 no_clobber 拒写，表现为"B 环境安装失败"（其实只是文件名撞了）。
  rsh_script A setup_install_A "${SCRIPT_DIR}/b1_remote_setup.sh" install \
    && log "A 装好了" || log "A 环境安装失败（已留档）"
  rsh_script B setup_install_B "${SCRIPT_DIR}/b1_remote_setup.sh" install \
    && log "B 装好了" || log "B 环境安装失败（已留档）"

  rsh A fetch_official "$(cat <<EOS
mkdir -p ${ROFFICIAL} && cd ${ROFFICIAL}
BASE=https://gh-proxy.com/https://raw.githubusercontent.com/vllm-project/vllm/v${VLLM_VER}
for f in tests/v1/kv_connector/nixl_integration/toy_proxy_server.py \
         tests/v1/kv_connector/nixl_integration/run_accuracy_test.sh \
         tests/v1/kv_connector/nixl_integration/test_accuracy.py \
         requirements/kv_connectors.txt ; do
  out=\$(basename \$f)
  curl -sS --max-time 60 -o "\$out" "\$BASE/\$f" && echo "OK \$out \$(wc -c < \$out)"
done
EOS
)" || true
  budget_report
}

# =============================================================================
# 阶段：digests —— 锁定版本（R7 要求的"镜像或依赖清单"等价物）
# =============================================================================
# 为什么单独成一步：本任务**不用容器镜像**，所以"镜像 digest"的等价物是
#   (a) 精确版本号 + (b) wheel 的 sha256 + (c) requirements_locked.txt 的 sha256。
# 取法：`pip download --no-deps` 从内网镜像取**原始 wheel**，算完 sha256 立刻删掉（不占磁盘）。
# 另外把**模型 revision**（Hub API 的 sha）也在这里锁定：两端必须锁到同一个 sha，
# 否则"形状对得上但权重不同"这种错误 vLLM **不报错**，KV 按字节搬运 ⇒ 结果静默算错。
phase_digests() {
  ensure_out
  log "== 1/3 记录官方模型 revision（Hub API） =="
  rsh A model_revision \
    "curl -sS --max-time 30 ${HFHUB}/api/models/${MODEL_ID} | tr ',' '\\n' | grep -E '\"(sha|lastModified|id)\"'" || true

  log "== 2/3 两端 wheel / 依赖清单 sha256 =="
  rsh A dep_digests_A "$(digest_snippet)" || true
  rsh B dep_digests_B "$(digest_snippet)" || true

  log "== 3/3 两端版本核对（对照官方 kv_connectors.txt 钉的版本） =="
  dependency_verdict
}

digest_snippet() {
  cat <<EOS
set -x
V=${RVENV}/bin
W=${RDIR}/_wheelcheck
rm -rf "\$W"; mkdir -p "\$W"
\$V/pip download --no-deps -q -i http://mirrors.cloud.aliyuncs.com/pypi/simple/ \
    --trusted-host mirrors.cloud.aliyuncs.com -d "\$W" vllm==${VLLM_VER} nixl==${NIXL_VER}
echo "pip download rc=\$?"
sha256sum "\$W"/*.whl 2>/dev/null || true
echo "--- requirements_locked.txt sha256"
sha256sum ${RDIR}/requirements_locked.txt 2>/dev/null || echo "(还没有 locked 清单)"
echo "--- 关键包版本"
grep -iE '^(vllm|torch|nixl|nixl-cu[0-9]+|transformers|flashinfer-python|ray|xformers|huggingface-hub|uvicorn|fastapi|httpx)==' \
    ${RDIR}/requirements_locked.txt 2>/dev/null || true
echo "--- 官方 kv_connectors.txt 钉的版本（vLLM v${VLLM_VER}）"
cat ${ROFFICIAL}/kv_connectors.txt 2>/dev/null || echo "(没取到官方文件)"
rm -rf "\$W"
EOS
}

dependency_verdict() {
  ensure_out
  local f="${OUT}/dependency_digests.txt"
  no_clobber "$f" || return 1
  {
    echo "== B1 依赖锁定：vLLM / NIXL 的精确版本与 wheel sha256 =="
    echo "取法：pip download --no-deps 从内网镜像取原始 wheel，再算 sha256（下完即删，不留大文件）。"
    echo "为什么需要它：本任务**不用容器镜像**，所以'镜像 digest'的等价物是"
    echo "  （a）精确版本号 +（b）wheel 的 sha256 +（c）requirements_locked.txt 的 sha256。"
    echo "  官方 requirements/kv_connectors.txt 把 NIXL 版本钉死在 nixl == ${NIXL_VER}。"
    echo
    for h in A B; do
      echo "---- 主机 ${h}（$(pub_of "$h")） ----"
      sed 's/^/    /' "${OUT}/dep_digests_${h}.log" 2>/dev/null || echo "    （缺 dep_digests_${h}.log）"
      echo
    done
    echo "== 判据（两端**必须**一致，否则握手直接失败或静默算错） =="
    local ha hb na nb la lb
    ha="$(grep -oE '^[0-9a-f]{64}  .*vllm-.*\.whl' "${OUT}/dep_digests_A.log" 2>/dev/null | awk '{print $1}' | head -1)"
    hb="$(grep -oE '^[0-9a-f]{64}  .*vllm-.*\.whl' "${OUT}/dep_digests_B.log" 2>/dev/null | awk '{print $1}' | head -1)"
    echo "  vllm wheel sha256：A=${ha:-<缺>}  B=${hb:-<缺>}"
    if [[ -n "${ha}" && "${ha}" == "${hb}" ]]; then
      echo "  → **两端 vLLM wheel 逐字节相同** ✓"
    else
      echo "  → **不一致或读取失败** ✗ —— 不要继续测量"
    fi
    na="$(grep -oE '^[0-9a-f]{64}  .*nixl-.*\.whl' "${OUT}/dep_digests_A.log" 2>/dev/null | awk '{print $1}' | head -1)"
    nb="$(grep -oE '^[0-9a-f]{64}  .*nixl-.*\.whl' "${OUT}/dep_digests_B.log" 2>/dev/null | awk '{print $1}' | head -1)"
    echo "  nixl wheel sha256：A=${na:-<缺>}  B=${nb:-<缺>}"
    la="$(grep -oE '^[0-9a-f]{64}  .*requirements_locked.txt' "${OUT}/dep_digests_A.log" 2>/dev/null | awk '{print $1}' | head -1)"
    lb="$(grep -oE '^[0-9a-f]{64}  .*requirements_locked.txt' "${OUT}/dep_digests_B.log" 2>/dev/null | awk '{print $1}' | head -1)"
    echo "  requirements_locked.txt sha256：A=${la:-<缺>}  B=${lb:-<缺>}"
    echo
    echo "== 模型修订号（锁定字段，R7 要求） =="
    echo "来源：Hub API  ${HFHUB}/api/models/${MODEL_ID}"
    echo "期望值（本脚本写死）：MODEL_REV_EXPECT = ${MODEL_REV_EXPECT}"
    echo "接口返回原文（含 sha / lastModified）见 ${OUT}/model_revision.log"
    echo "说明：这是**官方仓库的 revision**；两端实际下载到的目录哈希另见 model_identity.txt。"
  } >"$f" 2>&1
  cat "$f"
}

cloud_meta_snippet() {
  cat <<'EOS'
M=http://100.100.100.200/latest/meta-data
echo "--- 云元数据服务可达性（阿里云专用地址 100.100.100.200）"
for k in instance-id region-id zone-id instance/instance-type private-ipv4 eipv4 \
         vpc-id vswitch-id vswitch-cidr-block vpc-cidr-block image-id owner-account-id \
         instance/max-netbw-egress; do
  printf "%-28s = " "$k"; curl -sS --max-time 6 "$M/$k" 2>/dev/null || echo "(fail)"; echo
done
echo "--- 厂商线索（元数据地址本身 + 进程 + 镜像名）"
ls -d /usr/local/aegis /usr/local/share/aliyun-assist 2>/dev/null || true
EOS
}

cloud_meta_verdict() {
  ensure_out
  local f="${OUT}/cloud_identity.txt"
  no_clobber "$f" || return 1
  {
    echo "== 两台机器的真实云身份（证据来自元数据服务，不是推测） =="
    echo "取法：在实例内 curl http://100.100.100.200/latest/meta-data/<key>"
    echo "（100.100.100.200 是**阿里云专属**的元数据服务地址，它本身即厂商证据之一；"
    echo "  另有 /usr/local/aegis/AliYunDun 与 aliyun-assist 两个阿里云 agent 进程）"
    echo
    echo "厂商      : ${CLOUD}"
    echo "地域      : ${REGION_ID}"
    echo "可用区    : ${ZONE_ID}"
    echo "VPC       : ${VPC_ID}   网段 ${VPC_CIDR}"
    echo "子网      : ${VSWITCH_ID}   网段 ${VSWITCH_CIDR}"
    echo "机型      : ${INSTANCE_TYPE}（单卡 NVIDIA A10 24 GB，8 vCPU / 29.7 GB 可用内存）"
    echo "账号      : ${ALIYUN_ACCOUNT}"
    echo "镜像      : ${IMAGE_ID}"
    echo "A         : ${A_HOST}  ${A_PUB} / ${A_IN}   ${A_INST_ID}"
    echo "B         : ${B_HOST}  ${B_PUB} / ${B_IN}   ${B_INST_ID}"
    echo
    echo "结论：**两台同厂商、同地域、同可用区、同 VPC、同子网** —— 这正是内网直连的充分条件，"
    echo "      所以本批属于「同云（B）」，**不是跨云证据**。"
    echo "      若日后两台落在不同厂商，必须重做同一套核实并把结论改成「跨云（C）」。"
    echo
    echo "--- 两端元数据原文（逐字段） ---"
    for h in A B; do
      echo "[$h]"
      sed 's/^/    /' "${OUT}/cloud_meta_${h}.log" 2>/dev/null || echo "    （缺 cloud_meta_${h}.log）"
    done
  } >"$f" 2>&1
  cat "$f"
}

# =============================================================================
# 阶段：model —— 两端**并行**下模型，再比对整目录哈希
# =============================================================================
phase_model() {
  ensure_out
  local waitmin="${MODEL_WAIT_MIN:-360}"

  log "== 1/3 记录官方 revision（锁定用；两端必须同一 revision） =="
  rsh A model_revision "curl -sS --max-time 20 https://hf-mirror.com/api/models/${MODEL_ID} | head -c 2000" || true

  log "== 2/3 两端并行下载（幂等：已完整的不再下；**已在跑的不重复起第二个进程**） =="
  local need_A need_B run_A run_B
  model_is_complete A && need_A=0 || need_A=1
  model_is_complete B && need_B=0 || need_B=1
  model_download_running A && run_A=1 || run_A=0
  model_download_running B && run_B=1 || run_B=0
  log "完整? A=${need_A} B=${need_B}（1=需要下）   已在跑? A=${run_A} B=${run_B}（1=是）"

  if [[ "${need_A}" == "1" ]]; then
    if [[ "${run_A}" == "1" ]]; then
      log "A 上**已有**一份下载在跑（本次会话开始前遗留的），不重复起第二个，转为等待"
      rec A model_download_already_running "$(ts)" "$(ts)" 0 OK \
        "会话开始前已存在的 hf download 进程；沿用不重启（重启会撞 hf 的目录锁）"
    else
      model_start A || true
    fi
  fi
  if [[ "${need_B}" == "1" ]]; then
    if [[ "${run_B}" == "1" ]]; then
      log "B 上**已有**一份下载在跑，不重复起第二个，转为等待"
      rec B model_download_already_running "$(ts)" "$(ts)" 0 OK \
        "会话开始前已存在的 hf download 进程；沿用不重启"
    else
      model_start B || true
    fi
  fi

  if [[ "${need_A}" == "1" || "${need_B}" == "1" ]]; then
    model_wait "${waitmin}"
    model_is_complete A && log "A 模型完整 ✓" || log "A 模型**不完整**"
    model_is_complete B && log "B 模型完整 ✓" || log "B 模型**不完整**"
  fi

  log "== 3/3 两端模型目录一致性（整目录 sha256 + 逐文件清单） =="
  rsh A model_hash_A "$(model_hash_snippet)" || true
  rsh B model_hash_B "$(model_hash_snippet)" || true
  model_hash_verdict
}

# 远端判断模型是否已完整下载（幂等依据）
# ⚠️ 2026-10-07 修正：**不能假定一定有 `model.safetensors.index.json`**。
#    Qwen2.5-1.5B-Instruct 只有一个 `model.safetensors`（3,087,467,144 B），**没有分片、没有 index**。
#    旧实现硬性要求 index 存在，遇到单文件模型会一直报 `NO_INDEX` ⇒
#    表现为"模型永远下不完"，而实际早就下好了（会白等到 MODEL_WAIT_MIN 用尽）。
model_is_complete() {  # model_is_complete <A|B>
  local host="$1" ip; ip="$(pub_of "${host}")"
  [[ "${B1_DRY:-0}" == "1" ]] && return 1
  local out
  out="$(ssh "${SSH_OPTS[@]}" "root@${ip}" \
     "${RVENV}/bin/python - <<'PY'
import json,os,sys
D='${RMODEL}'
if not os.path.isdir(D): print('NO_DIR'); sys.exit(1)
idx=os.path.join(D,'model.safetensors.index.json')
single=os.path.join(D,'model.safetensors')
missing=[]
if os.path.isfile(idx):
    try: wm=json.load(open(idx))['weight_map']
    except Exception as e: print('BAD_INDEX'); sys.exit(1)
    shards=sorted(set(wm.values()))
    missing=[s for s in shards if not os.path.isfile(os.path.join(D,s))
             or os.path.getsize(os.path.join(D,s))<1_000_000]
    kind=f'{len(shards)} shards (index)'
elif os.path.isfile(single):
    missing=[] if os.path.getsize(single)>1_000_000 else ['model.safetensors(too small)']
    kind='single model.safetensors'
else:
    print('NO_WEIGHTS'); sys.exit(1)
for f in ('config.json','tokenizer.json','tokenizer_config.json','generation_config.json'):
    if not os.path.isfile(os.path.join(D,f)): missing.append(f)
inc=[f for f in os.listdir(D) if f.endswith('.incomplete')]
if missing: print('MISSING',missing[:6]); sys.exit(1)
if inc: print('INCOMPLETE',inc[:3]); sys.exit(1)
print('COMPLETE',kind)
PY" 2>&1 | tail -1)"
  echo "  [$host] $out"
  [[ "${out}" == COMPLETE* ]]
}

# 远端是否已经有我们自己的 hf 下载在跑（避免重复起第二个进程撞目录锁）
# 注意：这里用 `[h]f download` 的方括号写法，避免 grep 命中**本命令自己**。
model_download_running() {  # model_download_running <A|B>
  local host="$1" ip; ip="$(pub_of "${host}")"
  [[ "${B1_DRY:-0}" == "1" ]] && return 1
  local n
  n="$(ssh "${SSH_OPTS[@]}" "root@${ip}" \
       "ps -eo args --no-headers | grep -c '[h]f download' || true" 2>/dev/null | tail -1)"
  [[ "${n:-0}" -gt 0 ]] 2>/dev/null
}

# 把我们起的**每一个**远端后台任务的 PID 落到本地产物（防孤儿进程）
# 为什么：上一批机器上被叫停的子代理留下的孤儿进程在远端继续跑，
# 导致文件被"本会话之外"的改动、状态混乱。所以"我起了什么"必须落盘、收尾按 PID 精确停。
snap_pids() {  # snap_pids <A|B> <标签>
  local host="$1" label="$2"
  ensure_out
  local ip; ip="$(pub_of "${host}")"
  # 文件名带 UTC 时刻：每次快照都是**新文件**，天然满足 append-only，
  # 而且按时间排下来就是"我起了什么 → 什么时候还在 → 什么时候停了"的时间线。
  # （早先不带时刻，第二次快照会**静默覆盖**第一次，那正好抹掉孤儿进程的线索。）
  local f="${OUT}/pids_${label}_${host}_$(date -u +%H%M%SZ).txt"
  [[ "${B1_DRY:-0}" == "1" ]] && { echo "DRY: snap_pids ${label} ${host}"; return 0; }
  no_clobber "$f" || f="${f%.txt}_b.txt"
  {
    echo "# 机器 ${host}（内网 ${ip}）的 B1 相关进程台账   快照时刻 $(ts)"
    echo "# 判据：本文件里出现的每个 PID 都必须是**本会话起的**；收尾时按 PID 精停。"
    echo "--- 我起的后台任务 PID 文件（/root/b1/logs/*.pid）"
    ssh "${SSH_OPTS[@]}" "root@${ip}" \
      "for pf in ${RLOGS}/*.pid; do [[ -e \$pf ]] || continue; printf '%-14s pid=%s alive=%s\n' \"\$(basename \$pf .pid)\" \"\$(cat \$pf)\" \"\$(kill -0 \$(cat \$pf) 2>/dev/null && echo yes || echo no)\"; done" 2>&1
    echo "--- 当前匹配到的进程（vllm serve / toy_proxy_server / b1_client / hf download）"
    ssh "${SSH_OPTS[@]}" "root@${ip}" \
      "ps -eo pid,ppid,etimes,args --no-headers | grep -E '[v]llm serve|[t]oy_proxy_server|[b]1_client|[h]f download' || echo '(无)'" 2>&1
  } >"$f" 2>&1
  echo "PID 台账 → $(basename "$f")"
  cat "$f"
}

model_start() {  # model_start <A|B>  —— 起后台下载（关掉 Xet，hf-mirror 不支持 Xet）
  local host="$1" ip; ip="$(pub_of "${host}")"
  local started; started="$(ts)"
  local f="${OUT}/model_download_start_${host}.log"
  no_clobber "$f" || return 1
  if [[ "${B1_DRY:-0}" == "1" ]]; then echo "DRY: start download on ${host}"; return 0; fi
  {
    echo "# 在 ${host} 上起后台下载"
    echo "# 为什么必须关 Xet：hf-mirror 不代理 Xet，走 Xet 会报"
    echo "#   RuntimeError: CAS Client Error: HTTP 401 Unauthorized, domain: https://cas-server.xethub.hf.co/..."
    echo "# 实测发生过一次（B 端），所以显式 HF_HUB_DISABLE_XET=1。"
  } >> "$f"
  ssh "${SSH_OPTS[@]}" "root@${ip}" "mkdir -p ${RDIR}/models ${RLOGS}; cd ${RDIR}/models; export HF_ENDPOINT=https://hf-mirror.com; export HF_HUB_DISABLE_XET=1; export HF_HUB_DOWNLOAD_TIMEOUT=120; setsid nohup ${RVENV}/bin/hf download ${MODEL_ID} --local-dir ${RMODEL} > ${RLOGS}/model_download.log 2>&1 </dev/null & sleep 12; echo started; ps -eo pid,ppid,args --no-headers | grep '[h]f download' | tee ${RLOGS}/model_download.ps; ps -eo pid,args --no-headers | grep '[h]f download' | awk '{print \$1}' | paste -sd, - > ${RLOGS}/model_download.pid; echo \"pids=\$(cat ${RLOGS}/model_download.pid 2>/dev/null)\"; du -sb ${RDIR}/models | cut -f1" >>"$f" 2>&1
  local rc=$?
  # 把远端下载进程的 PID 落到本地产物（防孤儿：上一批机器就被别人的孤儿进程坑过）
  {
    echo "# ${host} 的 hf download 进程 PID（收尾时按这些 PID 精确停，不用宽泛 pkill）"
    ssh "${SSH_OPTS[@]}" "root@${ip}" \
      "cat ${RLOGS}/model_download.pid 2>/dev/null || echo '(没拿到 pid)'; ps -eo pid,ppid,args --no-headers | grep '[h]f download' || true" 2>&1
  } >"${OUT}/pids_model_download_${host}.txt" 2>&1
  cat "${OUT}/pids_model_download_${host}.txt" >>"$f"
  local ended; ended="$(ts)"
  echo "# ---- exit=${rc} end=${ended} ----" >> "$f"
  rec "${host}" "model_download_start" "${started}" "${ended}" "${rc}" \
      "$([[ $rc -eq 0 ]] && echo OK || echo FAIL)" "hf download ${MODEL_ID} (HF_HUB_DISABLE_XET=1)"
  tail -n 10 "$f"
}

model_wait() {  # model_wait <最多等多少分钟>
  local waitmin="$1"
  local logf="${OUT}/model_wait.log"
  local started; started="$(ts)"
  : >"$logf"
  echo "# model_wait 开始 ${started}，最多等 ${waitmin} 分钟（每 60 s 记一次进度）" >>"$logf"
  echo "# 为什么带"停滞看护"：实测遇到过 hf download 进程**悄悄消失**（日志里连报错都没有），" >>"$logf"
  echo "# 若不看护，就会白等几个小时。判据：连续 5 分钟字节数不涨 且 已无下载进程 且 尚未完整。" >>"$logf"
  local i=0 stall=0 last_a="" last_b=""
  while [[ $i -lt ${waitmin} ]]; do
    local a b
    a="$(model_size A)"; b="$(model_size B)"
    printf '[%s] +%dmin  A=%s  B=%s  (连续停滞 %d 分钟)\n' "$(ts)" "$i" "${a}" "${b}" "${stall}" >>"$logf"

    if model_is_complete A && model_is_complete B; then
      echo "[$(ts)] 两端都已完整，结束等待" >>"$logf"
      break
    fi

    if [[ "${a}" == "${last_a}" && "${b}" == "${last_b}" ]]; then
      stall=$(( stall + 1 ))
    else
      stall=0
    fi
    last_a="${a}"; last_b="${b}"

    if [[ $stall -ge 5 ]]; then
      echo "[$(ts)] 停滞 ${stall} 分钟 → 逐个检查是否还有下载进程，没有就**续传重启**" >>"$logf"
      for h in A B; do
        model_is_complete "$h" && continue
        if model_download_running "$h"; then
          echo "  [$h] 进程仍在（可能只是慢），不重启" >>"$logf"
        else
          echo "  [$h] **没有下载进程且未完整 → 续传重启**（.incomplete 会被复用）" >>"$logf"
          model_start "$h" >>"$logf" 2>&1 || true
        fi
      done
      stall=0
    fi

    sleep 60
    i=$(( i + 1 ))
  done
  tail -n 8 "$logf"
}

model_size() {  # model_size <A|B> —— 已下载字节数 + 是否还有下载进程
  local host="$1" ip; ip="$(pub_of "${host}")"
  [[ "${B1_DRY:-0}" == "1" ]] && { echo "DRY"; return; }
  ssh "${SSH_OPTS[@]}" "root@${ip}" \
    "{ du -sb ${RDIR}/models 2>/dev/null | cut -f1; ps -eo args --no-headers | grep -c '[h]f download' || true; } | paste -sd'/' -" 2>/dev/null | tail -1
}

# 比对两端模型目录哈希，把结论写进产物（R7：错了会静默传错数据）
model_hash_verdict() {
  ensure_out
  local fa="${OUT}/model_hash_A.log" fb="${OUT}/model_hash_B.log"
  local f="${OUT}/model_identity.txt"
  no_clobber "$f" || return 1
  {
    echo "== 两端模型目录一致性 =="
    echo "判据：整目录（按文件名排序后逐个 sha256，再取一次总哈希）必须两端相同。"
    echo "不一致的后果：形状对得上但权重不同 ⇒ vLLM **不报错**，KV 按字节搬运 ⇒ 结果静默算错。"
    echo "期望 revision（hf-mirror API）: ${MODEL_REV_EXPECT}"
    echo
    echo "--- A 的总哈希 / 文件数 / 总字节"
    grep -A3 -- '--- 逐文件' "$fa" 2>/dev/null | head -6 || echo "（${fa} 不存在）"
    sed -n '/--- 文件数/,+2p' "$fa" 2>/dev/null || true
    echo
    echo "--- B 的总哈希 / 文件数 / 总字节"
    grep -A3 -- '--- 逐文件' "$fb" 2>/dev/null | head -6 || echo "（${fb} 不存在）"
    sed -n '/--- 文件数/,+2p' "$fb" 2>/dev/null || true
    echo
    local ha hb
    ha="$(grep -A1 -- '--- 逐文件' "$fa" 2>/dev/null | tail -1 | awk '{print $1}')"
    hb="$(grep -A1 -- '--- 逐文件' "$fb" 2>/dev/null | tail -1 | awk '{print $1}')"
    echo "A 总哈希 = ${ha:-<缺>}"
    echo "B 总哈希 = ${hb:-<缺>}"
    if [[ -n "${ha}" && "${ha}" == "${hb}" ]]; then
      echo "结论：**两端模型逐文件一致** ✓"
    else
      echo "结论：**不一致或读取失败** ✗ —— 不要继续测量"
    fi
    echo
    echo "--- 权重文件原始精度 vs 实际运行精度（必须分开写，别混） ---"
    for h in A B; do
      echo "[$h] config.json 的 torch_dtype / dtype:"
      grep -oE '"(torch_dtype|dtype)"[[:space:]]*:[[:space:]]*"[^"]*"' "$fa" 2>/dev/null | head -2 | sed 's/^/    /' || true
    done
    echo "（原始精度 = 权重文件里写的；实际运行精度 = 服务启动日志里 vLLM 报的。"
    echo "  官方 ${MODEL_ID} 配置标 **bfloat16**；若运行时选了 FP16，"
    echo "  必须在报告里把这两个分开写。启动日志见 prefill_server.log / decode_server.log。）"
  } >"$f" 2>&1
  cat "$f"
}

model_hash_snippet() {
  cat <<EOS
set -e
D=${RMODEL}
if [[ ! -d "\$D" ]]; then echo "MISSING \$D"; exit 1; fi
echo "--- 目录：\$D"
echo "--- 文件数 / 总字节"
find "\$D" -type f | wc -l
du -sb "\$D" | cut -f1
echo "--- 逐文件 sha256（按名字排序，拼起来再取一次总哈希）"
find "\$D" -type f -not -path '*/.cache/*' | sort | xargs sha256sum | tee /tmp/b1_model_files.sha256 | sha256sum
echo "--- 关键文件单独列出"
for f in config.json generation_config.json tokenizer_config.json model.safetensors.index.json; do
  [[ -f "\$D/\$f" ]] && sha256sum "\$D/\$f" || echo "absent \$f"
done
echo "--- config.json 里的精度字段"
grep -oE '\"(torch_dtype|dtype)\"[[:space:]]*:[[:space:]]*\"[^\"]*\"' "\$D/config.json" 2>/dev/null || true
ls -1 "\$D"
EOS
}

# =============================================================================
# 阶段：baseline —— 4c 的对照臂（B 上单机完整服务）**先做基准与重复性检查**
# =============================================================================
phase_baseline() {
  ensure_out
  rpush B "${SCRIPT_DIR}/b1_remote_serve.sh" /tmp/b1_remote_serve.sh
  rpush A "${SCRIPT_DIR}/b1_client.py" "/tmp/b1_client.py"
  rpush A "${SCRIPT_DIR}/b1_remote_setup.sh" /tmp/b1_remote_setup.sh

  log "== 生成测量用的提示词（确定性合成文本，不含任何真实用户文本） =="
  rsh A gen_prompts "${RVENV}/bin/python /tmp/b1_client.py gen-prompts --out ${ROFFICIAL}/b1_prompts.json" || true
  scp -q "${SSH_OPTS[@]}" "root@${A_PUB}:${ROFFICIAL}/b1_prompts.json" "${OUT}/b1_prompts.json" 2>/dev/null || true

  log "== 起 B 上的单机完整服务（**不带** --kv-transfer-config） =="
  rsh B start_baseline "B1_BIND_IP=${B_IN} bash /tmp/b1_remote_serve.sh start-baseline ${PORT_BASELINE} ${RVENV}/bin/vllm ${RMODEL} ${SERVED_NAME}" || true
  wait_ready B "${PORT_BASELINE}" baseline || { log "对照臂没起来，基线测量无法进行"; return 1; }
  grab_metrics B "${PORT_BASELINE}" "baseline_idle_t0" || true

  log "== 对照臂重复 ${N_BASELINE_REPEATS} 次（先确认基准自身稳定，再谈拆分对照） =="
  # ⚠️ 2026-10-07 修正：客户端跑在 **A**（b1_client.py 与 b1_prompts.json 都只推/生成在 A），
  #    目标地址是 **B 的内网 IP**。原来写成 run_arm B（在 B 上跑客户端）⇒ B 上没有
  #    /tmp/b1_client.py，三次全部 `can't open file` 失败。
  #    两份臂（对照臂与拆分臂）都从 A 发起，客户机、提示词、参数完全一致，比对才干净。
  local r
  for r in $(seq 1 "${N_BASELINE_REPEATS}"); do
    log "  -- baseline 第 ${r}/${N_BASELINE_REPEATS} 次 --"
    run_arm A "baseline_r${r}" "${PORT_BASELINE}" "${B_IN}"
  done
  grab_metrics B "${PORT_BASELINE}" "baseline_after" || true

  baseline_stability
}

# 对照臂自身的稳定性：把 r1/r2/r3 两两按 token 编号比一遍
baseline_stability() {
  ensure_out
  local f="${OUT}/baseline_stability.txt"
  no_clobber "$f" || return 1
  {
    echo "== 单机完整服务的重复性（**这条要在正式拆分对照之前成立**） =="
    echo "判据：同一模型/精度/分词器/种子/生成参数下，重复跑同一批 prompt，"
    echo "      token 编号序列必须逐位相同（temperature=0 贪婪解码）。"
    echo "      若基准自身就不稳定，后面「拆分式 == 单机」的差异就分不清是谁的错。"
    echo
  } >"$f" 2>&1
  local present=()
  local r; for r in $(seq 1 "${N_BASELINE_REPEATS}"); do
    [[ -f "${OUT}/arm_baseline_r${r}.json" ]] && present+=("$r")
  done
  if [[ ${#present[@]} -lt 2 ]]; then
    echo "只有 ${#present[@]} 次成功落盘的对照臂 → **无法判稳定性**（照实写）" >>"$f"
    cat "$f"; return 0
  fi
  local i j
  for (( i=0; i<${#present[@]}; i++ )); do
    for (( j=i+1; j<${#present[@]}; j++ )); do
      local a="${present[$i]}" b="${present[$j]}"
      {
        echo "---------- r${a} vs r${b} ----------"
      } >>"$f"
      "${PY_LOCAL}" "${SCRIPT_DIR}/b1_compare.py" \
        --baseline "${OUT}/arm_baseline_r${a}.json" \
        --disagg   "${OUT}/arm_baseline_r${b}.json" \
        --out      "${OUT}/_tmp_stab_r${a}_r${b}.txt" >>"$f" 2>&1 || true
      grep -E "逐 token 相同的请求数|不同的请求数|结论：" "${OUT}/_tmp_stab_r${a}_r${b}.txt" 2>/dev/null >>"$f" || true
      echo >>"$f"
    done
  done
  cat "$f"
}

# =============================================================================
# 阶段：netbw —— 内网链路的原始容量（**环境事实**，不是服务性能结论）
# =============================================================================
# 为什么要有它：要求里说"内网这件事 ping 不够"。`ss -tnp` 能证明**连接用了哪个地址**，
# 但不能说明这条内网链路**有多宽**——而"KV 能不能在合理时间内搬过去"直接取决于它。
# 做法：B 上起一个只听内网 IP 的接收端（**不用公开端口扫描工具**，就是一个 socket），
# A 往 B 的内网 IP 发固定字节数，两端各自报速率。**接收端 PID 落盘、按 PID 停**。
# 注意口径：这是"链路层原始容量"，**不是 vLLM 的吞吐**，两者不要混在一起写。
phase_netbw() {
  ensure_out
  local mib="${NETBW_MIB:-512}"
  local port="${NETBW_PORT:-9102}"
  rsh B netbw_recv "$(cat <<EOS
pidfile=${RLOGS}/netbw_recv.pid
cat > /tmp/b1_netbw_recv.py <<'PY'
import socket, sys, time
port = int(sys.argv[1]); total = int(sys.argv[2])
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(('${B_IN}', port)); s.listen(1)
print('listening on ${B_IN}:%d, expecting %d bytes' % (port, total), flush=True)
c, addr = s.accept()
print('peer', addr, flush=True)
got = 0; t0 = None; t1 = None; t0_epoch = 0.0
while got < total:
    b = c.recv(1 << 20)
    if not b: break
    if t0 is None:
        t0 = time.perf_counter(); t0_epoch = time.time()
    got += len(b)
t1 = time.perf_counter()
print('recv_first_byte_epoch=%.6f' % t0_epoch)
print('received %d bytes in %.4f s -> %.1f MiB/s (%.2f Gbit/s)'
      % (got, t1 - t0, got / (t1 - t0) / 2**20, got * 8 / (t1 - t0) / 1e9), flush=True)
c.close()
PY
setsid nohup timeout 300 python3 /tmp/b1_netbw_recv.py ${port} $(( ${mib} * 1024 * 1024 )) \
    > ${RLOGS}/netbw_recv.log 2>&1 </dev/null &
echo \$! > "\$pidfile"
sleep 2
echo "recever pid=\$(cat \$pidfile) alive=\$(kill -0 \$(cat \$pidfile) 2>/dev/null && echo yes || echo no)"
cat ${RLOGS}/netbw_recv.log
ss -ltn | grep ":${port}" || echo "（接收端没在听？）"
EOS
)" || true
  snap_pids B netbw || true

  rsh A netbw_send "$(cat <<EOS
python3 - <<'PY'
import socket, time
total = $(( ${mib} * 1024 * 1024 ))
buf = b'0' * (1 << 20)
s = socket.create_connection(('${B_IN}', ${port}), timeout=20)
print('connected to ${B_IN}:${port}')
t0 = time.perf_counter(); t0_epoch = time.time()
sent = 0
while sent < total:
    n = s.send(buf if total - sent >= len(buf) else buf[: total - sent])
    sent += n
t1 = time.perf_counter()
print('sent_first_byte_epoch=%.6f' % t0_epoch)
print('sent %d bytes in %.4f s -> %.1f MiB/s (%.2f Gbit/s)'
      % (sent, t1 - t0, sent / (t1 - t0) / 2**20, sent * 8 / (t1 - t0) / 1e9))
s.shutdown(socket.SHUT_WR); s.close()
PY
sleep 2
EOS
)" || true

  rsh B netbw_recv_result "sleep 2; cat ${RLOGS}/netbw_recv.log" || true
  # 接收端可能已经自己退出（timeout 300 或 EOF）；不管退出与否都按 PID 收尾
  rsh B netbw_cleanup "$(cat <<EOS
pidfile=${RLOGS}/netbw_recv.pid
if [[ -f "\$pidfile" ]]; then
  pid=\$(cat "\$pidfile")
  if kill -0 "\$pid" 2>/dev/null; then echo "接收端 pid=\$pid 还在 → SIGTERM"; kill -TERM "\$pid" 2>/dev/null; sleep 2
    kill -0 "\$pid" 2>/dev/null && { echo "还在 → SIGKILL"; kill -KILL "\$pid" 2>/dev/null; }; fi
  rm -f "\$pidfile"
fi
ps -eo pid,args --no-headers | grep '[b]1_netbw_recv' || echo "接收端已退出 ✓"
ss -ltn | grep ":${port}" || echo "端口 ${port} 已释放 ✓"
EOS
)" || true
  netbw_verdict
}

netbw_verdict() {
  ensure_out
  local f="${OUT}/netbw.txt"
  no_clobber "$f" || return 1
  {
    echo "== 内网链路的原始容量（环境事实；**不是** vLLM 服务的性能） =="
    echo "做法：B 在它的内网 IP ${B_IN}:${NETBW_PORT:-9102} 上收，A 从内网 IP 发固定字节数。"
    echo "口径提醒：这是「链路能跑多宽」的上限参考，用来说明「KV 在这种链路上搬一次要多久」；"
    echo "         **不能**把它写成模型服务的吞吐或延迟结论。"
    echo
    echo "--- A 发送侧（客户端自报）"
    grep -E "connected to|sent_first_byte_epoch|sent [0-9]+ bytes" "${OUT}/netbw_send.log" 2>/dev/null | sed 's/^/    /' || echo "    （缺 netbw_send.log）"
    echo "--- B 接收侧（服务端自报，含来源地址）"
    grep -hE "listening|peer |received [0-9]+ bytes" \
        "${OUT}/netbw_recv.log" "${OUT}/netbw_recv_result.log" 2>/dev/null | sed 's/^/    /' \
      || echo "    （缺 netbw_recv*.log）"
    echo
    echo "怎么读：两侧速率应当同量级（差值来自缓冲与计时点）。"
    echo "        'peer' 那一行若显示 ${A_IN}（内网地址）⇒ 数据确实走内网，**不是公网**。"
  } >"$f" 2>&1
  cat "$f"
}

# =============================================================================
# 阶段：netcapture —— 直接抓包，看 KV 到底走的是哪张网卡（要求第 6 条的最强证据）
# =============================================================================
# 为什么要有它：`ss`/`ip route` 只能说明"路由**应该**走 eth0"，属于间接证据。
# 这一阶段在**生成端 B 的 eth0 上真抓一次包**，把每个 TCP 流的字节数排出来 ——
# 于是"KV 是不是走了内网网卡"变成**观测到的事实**，而不是推断。
#
# ⚠️ 前提：`precheck` 起的三件套必须**还在跑**（本阶段**不启动任何服务**，只抓包）。
# ⚠️ 口径：本阶段发的 1 条请求是**补充诊断请求**，不是正式测量请求。
#    正式测量已在 measure 阶段做完并按 R4 冻结；这里不替换、不重跑任何已有结果。
phase_netcapture() {
  ensure_out
  local prompt_id="${NETCAP_PROMPT:-p04_rep20}"
  local secs="${NETCAP_SECONDS:-30}"
  local cap="/root/b1/logs/b1_kv_capture.pcap"

  log "== 1/4 在 B 的 eth0 上起 tcpdump（排除 22 端口，即排除我们自己的 SSH） =="
  rsh B netcap_start "$(cat <<EOS
rm -f ${cap} ${cap}.pid
which tcpdump
setsid nohup tcpdump -i eth0 -nn -s 96 -U -w ${cap} -c 200000 'tcp and not port 22' \
    > ${RLOGS}/netcap_tcpdump.log 2>&1 </dev/null &
echo \$! > ${cap}.pid
sleep 3
echo "tcpdump pid=\$(cat ${cap}.pid) alive=\$(kill -0 \$(cat ${cap}.pid) 2>/dev/null && echo yes || echo no)"
ss -ltn | grep -E ':(8100|8200|8192)' || echo "（注意：没看到服务端口，precheck 是不是没跑？）"
EOS
)" || true
  snap_pids B netcap || true

  log "== 2/4 发 1 条**补充诊断**请求（${prompt_id}，不是正式测量） =="
  rsh A netcap_request "${RVENV}/bin/python /tmp/b1_client.py --url http://127.0.0.1:${PORT_PROXY} --model '${SERVED_NAME}' --prompts-file ${ROFFICIAL}/b1_prompts.json --max-tokens ${MAX_TOKENS} --only-ids ${prompt_id} --out ${RDIR}/out/netcap_probe.json" || true
  scp -q "${SSH_OPTS[@]}" "root@${A_PUB}:${RDIR}/out/netcap_probe.json" "${OUT}/netcap_probe.json" 2>/dev/null || true
  sleep 3

  log "== 3/4 停 tcpdump（按 PID）并汇总每条流的字节数 =="
  rsh B netcap_stop "$(cat <<EOS
pid=\$(cat ${cap}.pid 2>/dev/null)
if [[ -n "\$pid" ]] && kill -0 "\$pid" 2>/dev/null; then kill -INT "\$pid"; sleep 3; fi
kill -0 "\$pid" 2>/dev/null && { echo "还在 → SIGTERM"; kill -TERM "\$pid" 2>/dev/null; sleep 2; }
echo "--- tcpdump 自己报的收包统计"
cat ${RLOGS}/netcap_tcpdump.log 2>/dev/null | tail -8
echo "--- pcap 大小 / 文件"
ls -l ${cap} 2>/dev/null
echo "--- 每条 TCP 流承载的**字节总数**（按字节降序，前 12 条）"
tcpdump -r ${cap} -nn -q 2>/dev/null | awk '\$6=="tcp" && \$7>0 {print \$3">"\$5, \$7}' \
  | awk '{s[\$1]+=\$2} END{for(k in s) printf "%14d  %s\n", s[k], k}' | sort -rn | head -12
echo "--- 出现过的**对端地址**（内网 vs 公网，一眼可判）"
tcpdump -r ${cap} -nn -q 2>/dev/null | awk '{print \$3, \$5}' | grep -oE '172\.29\.31\.[0-9]+|8\.155\.[0-9]+\.[0-9]+' | sort | uniq -c | sort -rn
echo "--- 公网地址出现的次数（**应为 0 或只有 SSH 之外的少量**；SSH 已被过滤器排除）"
tcpdump -r ${cap} -nn -q 2>/dev/null | grep -cE '8\.155\.' || true
EOS
)" || true
  netcap_verdict
}

netcap_verdict() {
  ensure_out
  local f="${OUT}/netcap_kv_path.txt"
  no_clobber "$f" || return 1
  {
    echo "== 抓包证据：KV 数据走的是哪张网卡、哪个地址 =="
    echo "做法：在**生成端 B** 的 eth0 上抓「tcp and not port 22」（即排除我们自己的 SSH），"
    echo "      期间从前处理侧发 1 条请求（p04_rep20，prompt 约 1560 token）。"
    echo "口径：这条请求是**补充诊断请求**，不替换、不重跑 measure 阶段任何正式结果。"
    echo
    sed 's/^/    /' "${OUT}/netcap_stop.log" 2>/dev/null | head -60
    echo
    echo "怎么读："
    echo "  * 「每条 TCP 流的字节总数」里应当出现一条**量级与 KV 相符**的流（十几到几十 MB）；"
    echo "    它的两端若是 172.29.31.x（内网），就说明 KV 是**在内网网卡上搬的**。"
    echo "  * 「出现过的对端地址」里若只有 172.29.31.x、没有 8.155.x ⇒ 没有任何流量走公网。"
    echo "  * 判据不能只看第一条最大流：HTTP 请求/响应、NIXL 握手都在同一次抓包里。"
  } >"$f" 2>&1
  cat "$f"
}

# =============================================================================
# 阶段：precheck —— 起三件套 + 抓 /metrics + **冒烟单请求** + 内网路由核实
# =============================================================================
phase_precheck() {
  ensure_out
  budget_gate || return 1

  log "== 停掉对照臂，把 B 的显存腾出来 =="
  rpush B "${SCRIPT_DIR}/b1_remote_serve.sh" /tmp/b1_remote_serve.sh
  rsh B stop_baseline "bash /tmp/b1_remote_serve.sh stop baseline" || true
  sleep 8

  log "== 起前处理（A，绑内网）与生成（B，绑内网）两个 vLLM 实例 =="
  rpush A "${SCRIPT_DIR}/b1_remote_serve.sh" /tmp/b1_remote_serve.sh
  rpush A "${SCRIPT_DIR}/b1_client.py" "/tmp/b1_client.py"
  rsh A start_prefill "B1_BIND_IP=${A_IN} bash /tmp/b1_remote_serve.sh start-prefill ${PORT_PREFILL} ${SIDE_PORT} ${RVENV}/bin/vllm ${RMODEL} ${SERVED_NAME} ${A_IN}" || true
  rsh B start_decode  "B1_BIND_IP=${B_IN} bash /tmp/b1_remote_serve.sh start-decode  ${PORT_DECODE}  ${SIDE_PORT} ${RVENV}/bin/vllm ${RMODEL} ${SERVED_NAME} ${B_IN}" || true

  log "== 等两端就绪 =="
  wait_ready A "${PORT_PREFILL}" prefill
  wait_ready B "${PORT_DECODE}"  decode

  log "== 抓第一次 /metrics（拆分服务刚开始，还没有任何请求） =="
  grab_metrics A "${PORT_PREFILL}" "precheck_prefill_t0" || true
  grab_metrics B "${PORT_DECODE}"  "precheck_decode_t0"  || true

  log "== 起代理（A 上，只绑 127.0.0.1；客户端也在 A 上跑） =="
  rsh A start_proxy "bash /tmp/b1_remote_serve.sh start-proxy ${PORT_PROXY} ${A_IN}:${PORT_PREFILL} ${B_IN}:${PORT_DECODE} ${RVENV}/bin/python ${ROFFICIAL}/toy_proxy_server.py" || true
  wait_ready A "${PORT_PROXY}" proxy /healthcheck || true

  log "== 内网核实：NIXL 实际连的是内网还是公网？走哪张网卡？ =="
  netproof

  log "== **安装冒烟检查**：拆分服务起来后的第一条请求（单独留档，不混进正式数据） =="
  smoke_probe

  log "== 冒烟后的 /metrics + 直方图增量（两端） =="
  grab_metrics A "${PORT_PREFILL}" "precheck_prefill_t1" || true
  grab_metrics B "${PORT_DECODE}"  "precheck_decode_t1"  || true
  mdelta A precheck_prefill_t0 precheck_prefill_t1 "P（前处理端）" "smoke_A"
  mdelta B precheck_decode_t0  precheck_decode_t1  "D（生成端）"   "smoke_B"
}

# 冒烟：只发 1 条请求，落 smoke/ 子目录，**与正式测量数据分开**
smoke_probe() {
  ensure_out
  local sdir="${OUT}/smoke"
  mkdir -p "${sdir}"
  local started; started="$(ts)"
  local f="${sdir}/smoke.log"
  if [[ "${B1_DRY:-0}" == "1" ]]; then echo "DRY: smoke"; return 0; fi
  {
    echo "# 安装冒烟检查：拆分服务起来后的**第一条**请求"
    echo "# 定位：它属于「前置检查」而不是「正式测量」。"
    echo "#   - 通过 → 说明服务链路装起来了，可以进入 measure（开测）"
    echo "#   - 失败 → 按 R3 算「还没开测」，可以修完重来（但本次记录保留）"
    echo "# 它**不**计入正式测量数据，measure 阶段的请求才计入。"
    echo "# 请求：只发 1 条（p01_rep8），temperature=0，max_tokens=${MAX_TOKENS}"
  } >"$f"
  ssh "${SSH_OPTS[@]}" "root@${A_PUB}" \
    "${RVENV}/bin/python /tmp/b1_client.py --url http://127.0.0.1:${PORT_PROXY} --model '${SERVED_NAME}' --prompts-file ${ROFFICIAL}/b1_prompts.json --max-tokens ${MAX_TOKENS} --only-ids p01_rep8 --out ${RDIR}/out/smoke_disagg.json" >>"$f" 2>&1
  local rc=$?
  scp -q "${SSH_OPTS[@]}" "root@${A_PUB}:${RDIR}/out/smoke_disagg.json" "${sdir}/smoke_disagg.json" 2>/dev/null || true
  scp -q "${SSH_OPTS[@]}" "root@${A_PUB}:${RDIR}/out/smoke_disagg_raw.jsonl" "${sdir}/smoke_disagg_raw.jsonl" 2>/dev/null || true
  local ended; ended="$(ts)"
  echo "# ---- exit=${rc} end=${ended} ----" >>"$f"
  rec A "smoke_probe" "${started}" "${ended}" "${rc}" \
      "$([[ $rc -eq 0 ]] && echo OK || echo FAIL)" "冒烟单请求（拆分式，1 条）"
  tail -n 20 "$f"
  # 冒烟请求的原始响应（含 token_ids / usage）也留一份
  [[ -s "${sdir}/smoke_disagg.json" ]] && "${PY_LOCAL}" -c "
import json,sys
d=json.load(open(r'''${sdir}/smoke_disagg.json''',encoding='utf-8'))
for r in d['requests']:
    print(f\"  冒烟: {r['id']} http={r['http_status']} prompt_tokens={r.get('prompt_tokens')} completion={r.get('completion_tokens')} token_ids={'有' if r.get('token_ids') else '无'} err={r.get('error_class')}\")
" || true
  return $rc
}

# 内网核实：NIXL 的**实际连接地址与路由**（ping 低延迟不算证据）
netproof() {
  ensure_out
  local f="${OUT}/netproof.txt"
  no_clobber "$f" || return 1
  if [[ "${B1_DRY:-0}" == "1" ]]; then echo "DRY: netproof"; return 0; fi
  {
    echo "== 内网核实：KV 数据到底走哪条路？（**ping 0.25 ms 不能证明 NIXL 走了内网**） =="
    echo "要回答三件事：① 服务绑在哪个地址；② NIXL 实际连的是内网还是公网地址；③ 走哪张网卡。"
    echo
    for h in A B; do
      local ip; ip="$(pub_of "$h")"
      local peer_in; peer_in="$(in_of "$([[ "$h" == A ]] && echo B || echo A)")"
      local my_in; my_in="$(in_of "$h")"
      echo "==================== 机器 ${h}（公网 ${ip} / 内网 ${my_in}） ===================="
      ssh "${SSH_OPTS[@]}" "root@${ip}" "$(cat <<EOS
echo "--- 1) B1 服务端口绑在哪个地址（应当是内网地址或 127.0.0.1，**不应是 0.0.0.0**）"
ss -ltnp | grep -E ':(${PORT_PREFILL}|${PORT_PROXY}|${PORT_DECODE}|${PORT_BASELINE}|${SIDE_PORT})\\b' || echo "  （无 B1 端口在听）"
echo "--- 2) 与对端（${peer_in}）之间的**已建立**连接，看对端地址是内网还是公网"
ss -tnp state established | grep -E '${peer_in}' || echo "  （此刻与对端没有 established 连接）"
echo "--- 3) 有没有任何连接落在对端的公网地址上（出现就是走公网了）"
ss -tnp | grep -E '${A_PUB}|${B_PUB}' || echo "  无：没有任何连接使用公网地址 ✓"
echo "--- 4) 到对端内网地址的真实路由（走哪张网卡、源地址是谁）"
ip route get ${peer_in}
echo "--- 5) /metrics 里 KV 传输相关族的**首行**（只证明名字存在，不算证据）"
for a in \$(ip -4 -o addr show eth0 | awk '{print \$4}' | cut -d/ -f1) 127.0.0.1; do
  for p in ${PORT_PREFILL} ${PORT_DECODE}; do
    n=\$(curl -sS --max-time 8 http://\$a:\$p/metrics 2>/dev/null | grep -ciE 'nixl' || true)
    echo "    http://\$a:\$p/metrics 命中 nixl 的行数 = \${n:-0}"
  done
done
echo "--- 5b) 命中的前 3 行（原文）"
for a in \$(ip -4 -o addr show eth0 | awk '{print \$4}' | cut -d/ -f1) 127.0.0.1; do
  for p in ${PORT_PREFILL} ${PORT_DECODE}; do
    curl -sS --max-time 8 http://\$a:\$p/metrics 2>/dev/null | grep -iE 'nixl' | head -3 | sed "s|^|    [http://\$a:\$p] |" || true
  done
done
echo "--- 6) UCX/NIXL 在启动日志里选了什么传输层"
grep -iE 'nixl|ucx|side channel|kv_connector|kv transfer' ${RLOGS}/prefill.log ${RLOGS}/decode.log 2>/dev/null | head -25 || true
EOS
)"
      echo
    done
    echo "判读规则："
    echo "  · 第 1 项出现 0.0.0.0 ⇒ **不合规**（服务暴露给所有网卡，包括公网 IP）。"
    echo "  · 第 3 项出现公网地址 ⇒ KV **走了公网**，本批不能当内网证据，必须查安全组/NIXL 地址配置。"
    echo "  · 第 4 项 dev 应为内网网卡（eth0），源地址应为本机内网 IP。"
  } >"$f" 2>&1
  cat "$f"
}

# =============================================================================
# 阶段：measure —— 4a/4b/4c 的**正式测量**（第一条请求发出即"已开测"）
# =============================================================================
phase_measure() {
  ensure_out
  budget_gate || return 1
  rpush A "${SCRIPT_DIR}/b1_client.py" "/tmp/b1_client.py"

  log "！！ 从这里开始是**正式测量**：按 R4，任何失败都留档、不重跑顶替 ！！"

  log "== 4b 基线快照：正式请求之前的 /metrics（两端） =="
  grab_metrics A "${PORT_PREFILL}" "measure_prefill_t0" || true
  grab_metrics B "${PORT_DECODE}"  "measure_decode_t0"  || true

  log "== 正式臂 r1：**先单请求**（只发 p01） =="
  run_arm A "disagg_r1" "${PORT_PROXY}" "127.0.0.1" --only-ids p01_rep8 \
    || log "disagg_r1 失败（已留档，不重跑）"

  # 「运行中」快照：第一条正式请求已过、后续请求还没发。
  # 为什么要中间这一张：只有"开始/结束"两张的话，t0→t1 的增量只能说明"这段里传过"，
  # 分不清是哪一条请求传的；中间这张 + 逐条切分能把增量落到具体请求上。
  log "== 【运行中】快照：单请求之后、重复请求之前（两端） =="
  grab_metrics A "${PORT_PREFILL}" "measure_prefill_mid" || true
  grab_metrics B "${PORT_DECODE}"  "measure_decode_mid"  || true
  mdelta A measure_prefill_t0 measure_prefill_mid "P（前处理端，单请求后）" "measure_A_r1" || true
  mdelta B measure_decode_t0  measure_decode_mid  "D（生成端，单请求后）"   "measure_B_r1" || true

  log "== 正式臂 r2..r${N_DISAGG_REPEATS}：少量重复（每条独立编号） =="
  local r
  for r in $(seq 2 "${N_DISAGG_REPEATS}"); do
    run_arm A "disagg_r${r}" "${PORT_PROXY}" "127.0.0.1" \
      || log "disagg_r${r} 失败（已留档，不重跑）"
  done

  log "== 4b 收尾快照 + 直方图增量（**两端**） =="
  grab_metrics A "${PORT_PREFILL}" "measure_prefill_t1" || true
  grab_metrics B "${PORT_DECODE}"  "measure_decode_t1"  || true
  grab_metrics A "${PORT_PROXY}"   "measure_proxy_t1"   || true
  mdelta A measure_prefill_t0 measure_prefill_t1 "P（前处理端）" "measure_A"
  mdelta B measure_decode_t0  measure_decode_t1  "D（生成端）"   "measure_B"
  mdelta A measure_prefill_mid measure_prefill_t1 "P（前处理端，重复请求段）" "measure_A_r2r3" || true
  mdelta B measure_decode_mid  measure_decode_t1  "D（生成端，重复请求段）"   "measure_B_r2r3" || true
  kv_evidence_summary
  snap_pids A after_measure || true
  snap_pids B after_measure || true
  budget_report
}

run_arm() {  # run_arm <A|B> <tag> <port> <hostip> [额外客户端参数...]
  local chost="$1" tag="$2" port="$3" hip="$4"; shift 4
  local extra="$*"
  ensure_out
  local started; started="$(ts)"
  local ip; ip="$(pub_of "${chost}")"
  local f="${OUT}/arm_${tag}.log"
  local outjson="${RDIR}/out/${tag}.json"
  no_clobber "${OUT}/arm_${tag}.json" || return 1
  if [[ "${B1_DRY:-0}" == "1" ]]; then echo "DRY: arm ${tag}"; return 0; fi
  {
    echo "# arm=${tag} url=http://${hip}:${port} extra='${extra}' start=${started}"
    echo "# 客户端跑在 ${chost} 上（chost=${chost}），目标 ${hip}:${port}"
  } >>"$f"
  # ⚠️ 必须把客户端的退出码**传出来**：早先写成 `cmd; echo client_exit=$?`，
  #    ssh 的退出码永远是 echo 的 0 ⇒ 客户端明明失败（can't open file），
  #    record.csv 却记成 OK。**记录说谎比测试失败更糟**，所以这里显式 exit。
  ssh "${SSH_OPTS[@]}" "root@${ip}" \
    "mkdir -p ${RDIR}/out; rm -f '${outjson}' '${outjson%.json}_raw.jsonl'; ${RVENV}/bin/python /tmp/b1_client.py --url http://${hip}:${port} --model '${SERVED_NAME}' --prompts-file ${ROFFICIAL}/b1_prompts.json --max-tokens ${MAX_TOKENS} --out '${outjson}' ${extra}; rc=\$?; echo client_exit=\$rc; exit \$rc" >>"$f" 2>&1
  local rc=$?
  scp -q "${SSH_OPTS[@]}" "root@${ip}:${outjson}" "${OUT}/arm_${tag}.json" 2>/dev/null || true
  scp -q "${SSH_OPTS[@]}" "root@${ip}:${outjson%.json}_raw.jsonl" "${OUT}/arm_${tag}_raw.jsonl" 2>/dev/null || true
  local ended; ended="$(ts)"
  echo "# ---- exit=${rc} end=${ended} ----" >>"$f"
  rec "${chost}" "arm_${tag}" "${started}" "${ended}" "${rc}" \
      "$([[ $rc -eq 0 ]] && echo OK || echo FAIL)" "url=http://${hip}:${port} extra=${extra}"
  tail -n 20 "$f"
  return $rc
}

# 把 4b 的三路证据汇总成一页（原始快照与增量报告都在同目录）
kv_evidence_summary() {
  ensure_out
  local f="${OUT}/kv_transfer_evidence.txt"
  no_clobber "$f" || return 1
  {
    echo "== 4b：KV 到底有没有传到另一端 =="
    echo
    echo "口径（**最要紧，别读错**）：nixl_bytes_transferred 与传输耗时指标是**直方图**，"
    echo "  `_count` = 发生了几次「KV 传输通知」（**不是请求数**）；"
    echo "  `_sum`   = 一共搬了多少字节（**这才是「传没传到」的证据**）。"
    echo "  所以判据是「样本数与字节总和有没有变大」，不是「指标名出现过」。"
    echo
    echo "--- 证据 1：D（生成端，B:B${PORT_DECODE}）的直方图增量"
    sed -n '1,40p' "${OUT}/kv_delta_measure_B.txt" 2>/dev/null | sed 's/^/    /' || echo "    （缺 kv_delta_measure_B.txt）"
    echo
    echo "--- 证据 2：P（前处理端，A:${PORT_PREFILL}）的直方图增量"
    sed -n '1,40p' "${OUT}/kv_delta_measure_A.txt" 2>/dev/null | sed 's/^/    /' || echo "    （缺 kv_delta_measure_A.txt）"
    echo
    echo "--- 证据 3：D 端「外部 KV 传输进来的 token」计数器（vllm:prompt_tokens_by_source）"
    for label in measure_decode_t0 measure_decode_t1; do
      local mf="${OUT}/metrics_${label}_raw.txt"
      [[ -s "$mf" ]] || { echo "  [$label] 抓取失败/空"; continue; }
      echo "  [$label]"
      grep -iE 'prompt_tokens_by_source' "$mf" | sed 's/^/    /' || echo "    （该端没有这一项）"
    done
    echo
    echo "--- 不许混为一谈的三样东西（报告里逐条对照，别当成传输量） ---"
    echo "  * kv_cache_size_tokens / cache_config_info  = 缓存**容量**信息        ❌ 不是某次请求实际传的字节"
    echo "  * 理论 KV 估算（${MODEL_ID}：${KV_THEORY_4096}） = 量级参考        ❌ 不是实测传输量"
    echo "  * 显存占用（nvidia-smi）                    = 资源消耗                ❌ 不是传输量"
  } >"$f" 2>&1
  cat "$f"
}

# =============================================================================
# 阶段：diag4c —— 4c 专项诊断（前缀缓存 = off + 先预热 + logprobs）
# =============================================================================
# 为什么**另起一个阶段**，而不是重跑 baseline / measure / compare：
#   ① B1_PLAN.md 第 725 行要求**关闭前缀缓存**，B1 第二轮**没关**
#      （启动脚本里根本没有这个开关），于是 4c 的对照多出一个未受控的混淆项；
#      这一阶段把那条约定的配置**真正执行一次**。
#   ② 预热顺序必须**事先写定**（B1 第二轮没有预热，r1 被首次运行效应污染）。
#   ③ 首次分歧处要拿两边的概率 ⇒ 请求 logprobs。
#
# ⚠️ 规则写在 **B1_4C_PREREG.md**（**采数之前**冻结的），不是写完再补的。
# ⚠️ 产物落在**新的 run_id 目录**，**不替换** B1 第二轮的任何记录。
N_WARMUP_PASSES="${B1_WARMUP_PASSES:-3}"   # §4 写死：每条臂预热 3 遍
N_4C_REPEATS="${B1_4C_REPEATS:-3}"         # §5 写死：每条臂正式 3 遍
LOGPROBS_K="${B1_LOGPROBS_K:-5}"           # §5 写死：top-5

# 前置条件证明：**前缀缓存到底关了没有** —— 以 /metrics 原文为准，**不看命令行**。
# 返回 0 = 已证明关掉；1 = 没能证明（此时**不能判为通过**，按纪律如实标注）。
prefix_cache_proof() {  # prefix_cache_proof <A|B> <metrics_label> <角色名>
  local host="$1" label="$2" role="$3"
  ensure_out
  local f="${OUT}/cache_config_info_proof.txt"
  if [[ ! -f "$f" ]]; then
    {
      echo "== 前置条件：前缀缓存必须真的关掉（B1_4C_PREREG.md §3） =="
      echo "判据：/metrics 里 vllm:cache_config_info{...} 的 enable_prefix_caching 标签**必须是 False**。"
      echo "  * **只看命令行不算** —— B1 的教训正是「要求了，但没执行」。"
      echo "  * 命令行原文另行落盘，**只作辅助证据**。"
      echo
    } >"$f"
  fi
  local mf="${OUT}/metrics_${label}_raw.txt"
  {
    echo "---------- [${role}] ${host}   来源：metrics_${label}_raw.txt ----------"
  } >>"$f"
  if [[ ! -s "$mf" ]]; then
    echo "  ❌ 抓取失败 / 空文件 → **无法证明**前缀缓存已关（按纪律不能判为通过）" >>"$f"
    rec "${host}" "prefixcache_${label}" "$(ts)" "$(ts)" 1 FAIL "metrics 缺失"
    return 1
  fi
  local line; line="$(grep -m1 'vllm:cache_config_info{' "$mf" 2>/dev/null || true)"
  if [[ -z "$line" ]]; then
    echo "  ❌ 该端 /metrics 里**没有** cache_config_info 这一项 → 无法证明" >>"$f"
    rec "${host}" "prefixcache_${label}" "$(ts)" "$(ts)" 1 FAIL "无 cache_config_info"
    return 1
  fi
  local val; val="$(printf '%s' "$line" | grep -oE 'enable_prefix_caching="[^"]*"' | head -1)"
  echo "  $val" >>"$f"
  if [[ "$val" == 'enable_prefix_caching="False"' ]]; then
    echo "  ✅ 前缀缓存已关（判据满足）" >>"$f"
    rec "${host}" "prefixcache_${label}" "$(ts)" "$(ts)" 0 OK "$val"
    return 0
  fi
  echo "  ❌ **没有关掉**（实际 ${val}）→ 本轮 4c 的前置条件**不成立**，结论必须标注" >>"$f"
  rec "${host}" "prefixcache_${label}" "$(ts)" "$(ts)" 1 FAIL "$val"
  return 1
}

# 预热：完整跑 N_WARMUP_PASSES 遍 5 条提示词，顺序 = 提示词文件的顺序（p01→p05），串行。
# 预热结果落盘（文件名带 warmup_ 前缀），**不参与主比较**，只用来回答 Q3。
warmup_arm() {  # warmup_arm <A|B> <臂名> <port> <hostip>
  local host="$1" arm="$2" port="$3" hip="$4"
  local w
  for w in $(seq 1 "${N_WARMUP_PASSES}"); do
    log "  -- 预热【${arm}】第 ${w}/${N_WARMUP_PASSES} 遍（p01→p05，串行） --"
    run_arm "${host}" "warmup_${arm}_w${w}" "${port}" "${hip}" --logprobs "${LOGPROBS_K}" \
      || log "预热 ${arm} w${w} 失败（已留档，不重跑）"
  done
}

phase_diag4c() {
  ensure_out
  budget_gate || return 1

  log "==============================================================="
  log "4c 专项诊断 —— 前缀缓存 off + 先预热 + logprobs"
  log "规则见 B1_4C_PREREG.md（采数之前冻结）；**不替换** B1 第二轮的记录。"
  log "==============================================================="

  log "== 推最新的服务脚本与客户端（三条臂共用同一份，参数逐字相同） =="
  rpush B "${SCRIPT_DIR}/b1_remote_serve.sh" /tmp/b1_remote_serve.sh
  rpush A "${SCRIPT_DIR}/b1_remote_serve.sh" /tmp/b1_remote_serve.sh
  rpush A "${SCRIPT_DIR}/b1_client.py" "/tmp/b1_client.py"
  rpush A "${SCRIPT_DIR}/b1_compare_4c.py" "/tmp/b1_compare_4c.py"

  log "== 生成提示词（确定性合成文本；文件顺序即请求顺序 p01→p05） =="
  rsh A gen_prompts_4c "${RVENV}/bin/python /tmp/b1_client.py gen-prompts --out ${ROFFICIAL}/b1_prompts.json" || true
  scp -q "${SSH_OPTS[@]}" "root@${A_PUB}:${ROFFICIAL}/b1_prompts.json" "${OUT}/b1_prompts.json" 2>/dev/null || true

  PREFIX_CACHE_OK=1

  # ------------------------------------------------ 臂 1/2：单机完整服务（B）
  log "== 臂 1/2：起 B 上的单机完整服务，**关掉前缀缓存** =="
  rsh B start_baseline_4c "B1_BIND_IP=${B_IN} B1_PREFIX_CACHING=off bash /tmp/b1_remote_serve.sh start-baseline ${PORT_BASELINE} ${RVENV}/bin/vllm ${RMODEL} ${SERVED_NAME}" || true
  wait_ready B "${PORT_BASELINE}" baseline4c || { log "臂 1 没起来，无法继续（如实记，不换配置偷偷重来）"; return 1; }
  grab_metrics B "${PORT_BASELINE}" "diag4c_baseline_t0" || true
  log "-- 前置条件：证明前缀缓存真的关了（看 /metrics，不看命令行） --"
  prefix_cache_proof B diag4c_baseline_t0 "baseline（B:${PORT_BASELINE}，单机完整）" || PREFIX_CACHE_OK=0

  warmup_arm A baseline "${PORT_BASELINE}" "${B_IN}"

  log "== 臂 1：正式测量 r1..r${N_4C_REPEATS}（串行，每条独立编号） =="
  local r
  for r in $(seq 1 "${N_4C_REPEATS}"); do
    run_arm A "baseline4c_r${r}" "${PORT_BASELINE}" "${B_IN}" --logprobs "${LOGPROBS_K}" \
      || log "baseline4c_r${r} 失败（已留档，不重跑）"
  done
  grab_metrics B "${PORT_BASELINE}" "diag4c_baseline_t1" || true

  log "== 停掉臂 1，把 B 的显存腾给生成实例 =="
  rsh B stop_baseline_4c "bash /tmp/b1_remote_serve.sh stop baseline" || true
  sleep 8

  # ------------------------------------------------ 臂 2/2：拆分式
  log "== 臂 2/2：起前处理（A）与生成（B），**两条都关掉前缀缓存** =="
  rsh A start_prefill_4c "B1_BIND_IP=${A_IN} B1_PREFIX_CACHING=off bash /tmp/b1_remote_serve.sh start-prefill ${PORT_PREFILL} ${SIDE_PORT} ${RVENV}/bin/vllm ${RMODEL} ${SERVED_NAME} ${A_IN}" || true
  rsh B start_decode_4c  "B1_BIND_IP=${B_IN} B1_PREFIX_CACHING=off bash /tmp/b1_remote_serve.sh start-decode  ${PORT_DECODE}  ${SIDE_PORT} ${RVENV}/bin/vllm ${RMODEL} ${SERVED_NAME} ${B_IN}" || true
  wait_ready A "${PORT_PREFILL}" prefill4c
  wait_ready B "${PORT_DECODE}"  decode4c
  grab_metrics A "${PORT_PREFILL}" "diag4c_prefill_t0" || true
  grab_metrics B "${PORT_DECODE}"  "diag4c_decode_t0"  || true
  log "-- 前置条件：前处理端与生成端各自证明 --"
  prefix_cache_proof A diag4c_prefill_t0 "prefill（A:${PORT_PREFILL}，前处理）" || PREFIX_CACHE_OK=0
  prefix_cache_proof B diag4c_decode_t0  "decode（B:${PORT_DECODE}，生成）"    || PREFIX_CACHE_OK=0

  log "== 起代理（A，只绑 127.0.0.1；客户端也在 A 上跑） =="
  rsh A start_proxy_4c "bash /tmp/b1_remote_serve.sh start-proxy ${PORT_PROXY} ${A_IN}:${PORT_PREFILL} ${B_IN}:${PORT_DECODE} ${RVENV}/bin/python ${ROFFICIAL}/toy_proxy_server.py" || true
  wait_ready A "${PORT_PROXY}" proxy4c /healthcheck || true

  warmup_arm A disagg "${PORT_PROXY}" "127.0.0.1"

  log "== 臂 2：正式测量 r1..r${N_4C_REPEATS} =="
  for r in $(seq 1 "${N_4C_REPEATS}"); do
    run_arm A "disagg4c_r${r}" "${PORT_PROXY}" "127.0.0.1" --logprobs "${LOGPROBS_K}" \
      || log "disagg4c_r${r} 失败（已留档，不重跑）"
  done
  grab_metrics A "${PORT_PREFILL}" "diag4c_prefill_t1" || true
  grab_metrics B "${PORT_DECODE}"  "diag4c_decode_t1"  || true

  log "== 前置条件总判定 =="
  {
    echo
    echo "== 本轮前置条件总判定 =="
    if [[ "${PREFIX_CACHE_OK}" == "1" ]]; then
      echo "  ✅ 三条臂的 enable_prefix_caching 均为 False → 本轮 4c 的前置条件**成立**"
    else
      echo "  ❌ 至少有一条臂**没能证明**已关闭 → 本轮结论必须标注「前置条件未完全满足」"
    fi
  } >>"${OUT}/cache_config_info_proof.txt"
  tail -4 "${OUT}/cache_config_info_proof.txt"

  log "== 顺带复核：前缀缓存关掉后，KV 还真的在传吗（生成端直方图增量） =="
  mdelta B diag4c_decode_t0 diag4c_decode_t1 "D（生成端）" "diag4c_B" || true

  log "== 三条臂的启动命令行原文（**辅助证据**，不替代上面的指标判据） =="
  {
    echo
    echo "== 启动命令行原文（辅助证据） =="
    echo "（注意：命令行只是辅助。判据是上面 /metrics 里的 enable_prefix_caching 值。）"
    # ⚠️ 日志文件名是 rsh 的第二个参数原样，别拼错（早先写成 start_baseline4c.log 抓不到）。
    for lg in start_baseline_4c start_prefill_4c start_decode_4c; do
      echo "--- ${lg}.log ---"
      if [[ -s "${OUT}/${lg}.log" ]]; then
        grep -m1 -E '^[[:space:]]*cmd: ' "${OUT}/${lg}.log" 2>/dev/null \
          | tr ' ' '\n' | grep -E 'enable-prefix|no-enable-prefix|kv-transfer|--port|--host' \
          || echo "  （这一行里没有缓存/KV/端口相关参数）"
      else
        echo "  （缺 ${lg}.log）"
      fi
    done
  } >>"${OUT}/cache_config_info_proof.txt" 2>&1

  budget_report
}

# 4c 专项诊断的比对（与实验分开，方便只重跑分析）
phase_diag4c_compare() {
  ensure_out
  local f="${OUT}/consistency_4c_diff.txt"
  no_clobber "$f" || return 1
  {
    echo "配对规则在**采数之前**冻结（B1_4C_PREREG.md §5）："
    echo "  主比较 = baseline4c_r{k} ↔ disagg4c_r{k}（k = 1..${N_4C_REPEATS}）"
    echo "  辅助（只回答 Q3，不做主判定）：baseline4c_r2↔r3、disagg4c_r2↔r3、"
    echo "                                  以及预热 warmup_*_w1 ↔ w${N_WARMUP_PASSES}"
    echo
  } >"$f"

  local n_pair=0 n_equiv=0 n_notequiv=0 n_undecided=0 sum_same=0 sum_tot=0
  local r

  for r in $(seq 1 "${N_4C_REPEATS}"); do
    local b="${OUT}/arm_baseline4c_r${r}.json" d="${OUT}/arm_disagg4c_r${r}.json"
    local tf="${OUT}/_tmp_4c_pair_r${r}.txt"
    { echo; echo "################ 主配对 r${r}：baseline4c_r${r} ↔ disagg4c_r${r} ################"; } >>"$f"
    if [[ ! -s "$b" || ! -s "$d" ]]; then
      echo "  缺文件（baseline=$([[ -s "$b" ]] && echo 有 || echo 缺) / disagg=$([[ -s "$d" ]] && echo 有 || echo 缺)）" >>"$f"
      echo "  → 这一对**没比成**，照实记，**不补跑**" >>"$f"
      continue
    fi
    n_pair=$((n_pair + 1))
    "${PY_LOCAL}" "${SCRIPT_DIR}/b1_compare_4c.py" --baseline "$b" --disagg "$d" \
      --pair-label "baseline4c_r${r} ↔ disagg4c_r${r}" --out "$tf" >>"$f" 2>&1 || true
    if grep -q '\*\*等价\*\*' "$tf" 2>/dev/null; then n_equiv=$((n_equiv + 1));
    elif grep -q '\*\*不等价\*\*' "$tf" 2>/dev/null; then n_notequiv=$((n_notequiv + 1));
    else n_undecided=$((n_undecided + 1)); fi
    local ln; ln="$(grep -m1 '逐 token 相同的请求数' "$tf" 2>/dev/null || true)"
    if [[ -n "$ln" ]]; then
      local s t
      s="$(printf '%s' "$ln" | awk -F: '{print $2}' | awk -F/ '{gsub(/ /,"",$1); print $1}')"
      t="$(printf '%s' "$ln" | awk -F: '{print $2}' | awk -F/ '{gsub(/ /,"",$2); print $2}')"
      [[ "$s" =~ ^[0-9]+$ ]] && sum_same=$((sum_same + s))
      [[ "$t" =~ ^[0-9]+$ ]] && sum_tot=$((sum_tot + t))
    fi
  done

  {
    echo
    echo "=============================================================="
    echo "主判定汇总（B1_4C_PREREG.md §6）"
    echo "=============================================================="
    echo "臂配对成功     : ${n_pair} / ${N_4C_REPEATS}"
    echo "  判定「等价」   : ${n_equiv}"
    echo "  判定「不等价」 : ${n_notequiv}"
    echo "  无法判定       : ${n_undecided}"
    echo "请求级逐 token 相同 : ${sum_same} / ${sum_tot}"
    echo
    if [[ ${n_pair} -gt 0 && ${n_notequiv} -eq 0 && ${n_undecided} -eq 0 ]]; then
      echo "结论：**在本配置（前缀缓存 off）、本批输入下，拆分式输出与单机输出逐 token 等价。**"
      echo "      ⚠️ 适用范围仅限：本模型 / 本版本 / 本批 5 条合成输入 / 本精度。不是普遍等价性证明。"
    else
      echo "结论：**不等价（或无法判定）** —— 差异清单见上文各配对。"
      echo "      按 §6：**不把「意思差不多」改判为通过**；按 §8：**不重跑顶替**。"
    fi
    echo
    echo "===== 辅助比较（只回答 Q3：预热效应还在不在；不做主判定） ====="
  } >>"$f"

  _aux_cmp() {  # _aux_cmp <左侧文件> <右侧文件> <说明>
    local lb="$1" rb="$2" desc="$3" tk
    tk="${OUT}/_tmp_4c_aux_$(basename "$lb" .json)_$(basename "$rb" .json).txt"
    { echo; echo "---------- ${desc} ----------"; } >>"$f"
    if [[ ! -s "$lb" || ! -s "$rb" ]]; then
      echo "  缺文件 → 没比成（照实记）" >>"$f"; return 0
    fi
    "${PY_LOCAL}" "${SCRIPT_DIR}/b1_compare_4c.py" --baseline "$lb" --disagg "$rb" \
      --pair-label "$desc" --out "$tk" >>"$f" 2>&1 || true
  }
  _aux_cmp "${OUT}/arm_baseline4c_r2.json" "${OUT}/arm_baseline4c_r3.json" "对照臂内部 r2 vs r3"
  _aux_cmp "${OUT}/arm_disagg4c_r2.json"   "${OUT}/arm_disagg4c_r3.json"   "拆分臂内部 r2 vs r3"
  _aux_cmp "${OUT}/arm_warmup_baseline_w1.json" "${OUT}/arm_warmup_baseline_w${N_WARMUP_PASSES}.json" "预热：对照臂 w1 vs w${N_WARMUP_PASSES}"
  _aux_cmp "${OUT}/arm_warmup_disagg_w1.json"   "${OUT}/arm_warmup_disagg_w${N_WARMUP_PASSES}.json"   "预热：拆分臂 w1 vs w${N_WARMUP_PASSES}"

  cat "$f"
}

# =============================================================================
# 阶段：compare —— 4c 逐 token 比对
# =============================================================================
phase_compare() {
  ensure_out
  local started; started="$(ts)"
  local f="${OUT}/consistency_diff.txt"
  no_clobber "$f" || return 1

  # 拆分臂：优先用最后一次成功的重复（r3 → r2 → r1）
  local tag="" r
  for r in $(seq "${N_DISAGG_REPEATS}" -1 1); do
    [[ -f "${OUT}/arm_disagg_r${r}.json" ]] && { tag="disagg_r${r}"; break; }
  done
  local btag=""
  for r in $(seq "${N_BASELINE_REPEATS}" -1 1); do
    [[ -f "${OUT}/arm_baseline_r${r}.json" ]] && { btag="baseline_r${r}"; break; }
  done

  if [[ -n "${tag}" && -n "${btag}" ]]; then
    log "4c 比对：${btag}（单机完整） vs ${tag}（拆分式）"
    "${PY_LOCAL}" "${SCRIPT_DIR}/b1_compare.py" \
      --baseline "${OUT}/arm_${btag}.json" \
      --disagg   "${OUT}/arm_${tag}.json" \
      --out      "$f" || true
    cat "$f"
    rec A consistency "$started" "$(ts)" 0 DONE "4c：${btag} vs ${tag}，见 consistency_diff.txt"
  else
    {
      echo "无法比对：缺少对照臂或拆分臂的产物"
      echo "  对照臂候选 arm_baseline_r*.json : $(ls ${OUT}/arm_baseline_r*.json 2>/dev/null | wc -l) 个"
      echo "  拆分臂候选 arm_disagg_r*.json   : $(ls ${OUT}/arm_disagg_r*.json 2>/dev/null | wc -l) 个"
      ls -la "${OUT}" || true
    } | tee "$f"
    rec A consistency "$started" "$(ts)" 1 FAIL "缺输入文件"
  fi
}

# =============================================================================
# 阶段：collect —— 拉回日志
# =============================================================================
phase_collect() {
  ensure_out
  for pair in "A:prefill:${A_PUB}" "A:proxy:${A_PUB}" "B:decode:${B_PUB}" "B:baseline:${B_PUB}"; do
    IFS=: read -r host name ip <<<"$pair"
    scp -q "${SSH_OPTS[@]}" "root@${ip}:${RLOGS}/${name}.log" "${OUT}/${name}_server.log" 2>/dev/null \
      && log "拉回 ${name}.log" || log "（没有 ${name}.log，可能该服务没起过）"
  done
  # 依赖清单（R7 的「镜像或依赖清单」：本任务用 venv 而不是容器，所以锁 pip freeze）
  scp -q "${SSH_OPTS[@]}" "root@${A_PUB}:${RDIR}/requirements_locked.txt" "${OUT}/requirements_locked_A.txt" 2>/dev/null || true
  scp -q "${SSH_OPTS[@]}" "root@${B_PUB}:${RDIR}/requirements_locked.txt" "${OUT}/requirements_locked_B.txt" 2>/dev/null || true
  # 远端 PID 文件（防孤儿：这是"我起了什么、怎么停"的原始依据）
  for pair in "A:${A_PUB}" "B:${B_PUB}"; do
    IFS=: read -r host ip <<<"$pair"
    ssh "${SSH_OPTS[@]}" "root@${ip}" \
      "for pf in ${RLOGS}/*.pid; do [[ -e \$pf ]] || continue; printf '%s\t%s\n' \"\$(basename \$pf .pid)\" \"\$(cat \$pf)\"; done; echo '--- 下载进程 PID 文件'; cat ${RLOGS}/model_download.pid 2>/dev/null || echo '(无)'" \
      > "${OUT}/remote_pidfiles_${host}.txt" 2>&1 || true
    log "远端 PID 文件清单 → remote_pidfiles_${host}.txt"
  done
  rsh A final_state_A_env "nvidia-smi --query-gpu=memory.used --format=csv,noheader; ss -ltn | grep -E ':(${PORT_PREFILL}|${PORT_PROXY}|${PORT_DECODE}|${PORT_BASELINE}|${SIDE_PORT})\\b' || echo 'B1 端口未监听'" || true
}

# =============================================================================
# 阶段：stop —— 停掉全部 B1 服务（按 PID，不用宽泛 pkill）
# =============================================================================
phase_stop() {
  ensure_out
  rpush A "${SCRIPT_DIR}/b1_remote_serve.sh" /tmp/b1_remote_serve.sh
  rpush B "${SCRIPT_DIR}/b1_remote_serve.sh" /tmp/b1_remote_serve.sh
  rsh A stop_all_A "bash /tmp/b1_remote_serve.sh stop-all" || true
  rsh B stop_all_B "bash /tmp/b1_remote_serve.sh stop-all" || true
  # 模型下载进程是 setsid nohup 起的，不在 b1_remote_serve.sh 的 PID 文件里。
  # 它**不是服务**，但如果还在跑会持续占带宽/磁盘，收尾时也要按 PID 精确停。
  for h in A B; do
    local ip; ip="$(pub_of "$h")"
    # ⚠️ 这段有两个必须绕开的自伤坑（本轮实际踩到，exit=255）：
    #   ① `ps -eo args | grep '[h]f download'` 会**匹配到本命令自己** —— 因为本命令的
    #      命令行里只要出现字面量 "hf download"（哪怕是在 echo 的文案里），
    #      ps 就会把它列出来，于是脚本把自己的 shell 杀了、返回 255。
    #      ⇒ 文案里不许再出现那个字面量，并且用 $$ 兜底把自己排掉。
    #   ② 停进程只能按 PID，不用宽泛 pkill。
    rsh "$h" "stop_model_download_${h}" "$(cat <<EOS
self=\$\$
pids=\$(ps -eo pid,args --no-headers | grep '[h]f download' | awk '{print \$1}' | grep -v "^\$self\$")
if [[ -z "\$pids" ]]; then echo "  ${h}: 没有模型下载进程（无需停）"; else
  for p in \$pids; do echo "  ${h}: SIGTERM pid=\$p \$(ps -o args= -p \$p 2>/dev/null | head -c 60)"; kill -TERM "\$p" 2>/dev/null; done
  sleep 3
  for p in \$pids; do kill -0 "\$p" 2>/dev/null && { echo "  ${h}: \$p 还在，SIGKILL"; kill -KILL "\$p" 2>/dev/null; } ; done
fi
ps -eo pid,args --no-headers | grep '[h]f download' | grep -v "^\$self" || echo "  ${h}: 模型下载进程已全部退出 ✓"
EOS
)" || true
  done
  # 停完再各拍一张 PID 台账：这是"收尾无孤儿"的正面证据
  snap_pids A after_stop || true
  snap_pids B after_stop || true
}

# =============================================================================
# 阶段：pids —— 只打印两端此刻的 B1 相关进程台账（孤儿进程排查用，不做任何改动）
# =============================================================================
phase_pids() {
  ensure_out
  snap_pids A now || true
  snap_pids B now || true
}

# =============================================================================
# 阶段：cleanup-check —— 残留进程 / 端口 / 显存 + 公网不可达 + 私钥泄漏
# =============================================================================
phase_cleanup_check() {
  ensure_out
  local started; started="$(ts)"
  local f="${OUT}/cleanup_check.txt"
  no_clobber "$f" || return 1
  {
    echo "== B1 收尾检查：残留进程 / 端口 / 显存（三项） + 公网不可达 + 私钥未泄漏 =="
    echo "（判据：进程列表为空、监听端口为空、显存占用回到约 0 MiB）"
    echo
    for h in A B; do
      local ip; ip="$(pub_of "$h")"
      echo "==================== 机器 ${h}（${ip}） ===================="
      ssh "${SSH_OPTS[@]}" "root@${ip}" "$(cat <<'EOS'
echo "--- 1) 残留进程（vllm / 代理 / 我们的客户端）"
ps -eo pid,ppid,etimes,rss,cmd | grep -E '[v]llm serve|[t]oy_proxy_server|[b]1_client' || echo "  无 vllm / 代理 / 客户端进程 ✓"
echo "--- 2) 监听端口（B1 用过的）"
ss -ltnp | grep -E ':(8100|8192|8200|8300|5600)\b' || echo "  B1 相关端口全部未监听 ✓"
echo "--- 3) 显存占用"
nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu --format=csv,noheader
nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader || true
echo "--- 4) 磁盘"
df -h / | tail -1
EOS
)"
      echo
    done
  } >"$f" 2>&1

  {
    echo
    echo "==================== 公网不可达检查（从本机经**真实公网**连） ===================="
    echo "判据：B1 的服务端口在公网 IP 上**必须连不上**（服务只绑内网/回环）。"
    for hp in "${A_PUB}:${PORT_PREFILL}" "${A_PUB}:${PORT_PROXY}" "${B_PUB}:${PORT_DECODE}" "${B_PUB}:${PORT_BASELINE}"; do
      local ip="${hp%%:*}" port="${hp##*:}"
      if timeout 6 bash -c "echo > /dev/tcp/${ip}/${port}" 2>/dev/null; then
        echo "  ⚠️ ${ip}:${port} **公网可达** —— 不合规，需检查绑定地址与安全组"
      else
        echo "  ${ip}:${port} 公网不可达 ✓（服务未绑公网 / 安全组未放行）"
      fi
    done
    echo "  （注意：这条只能证明「此刻连不上」。服务运行期间的暴露面由 netproof.txt 的绑定地址证明。）"
  } >>"$f" 2>&1

  {
    echo
    echo "==================== 私钥泄漏扫描 ===================="
    echo "判据：本次 attempt 目录里**不得**出现任何私钥内容或私钥文件名。"
    if grep -rIl -e "BEGIN OPENSSH PRIVATE KEY" -e "BEGIN RSA PRIVATE KEY" -e "BEGIN PRIVATE KEY" "${OUT}" 2>/dev/null | head; then
      echo "  ⚠️ 上面文件里出现了私钥**内容** —— 必须立刻处理"
    else
      echo "  未在产物中找到私钥内容 ✓"
    fi
    local leak
    leak="$(grep -rIl -e "_网络感应" -e "\.pem" "${OUT}" 2>/dev/null | head || true)"
    if [[ -n "${leak}" ]]; then
      echo "  以下文件里出现了 .pem 字样（**只是路径，不是内容**，仍需人工确认无内容泄漏）："
      echo "${leak}" | sed 's/^/    /'
    else
      echo "  产物中没有出现私钥路径字样 ✓"
    fi
  } >>"$f" 2>&1

  cat "$f"
  rec "-" cleanup_check "$started" "$(ts)" 0 DONE "见 cleanup_check.txt"
}

# =============================================================================
# dispatch
# =============================================================================
case "${1:-}" in
  init)          phase_init ;;
  newrun)        phase_newrun ;;
  env)           phase_env ;;
  digests)       phase_digests ;;
  model)         phase_model ;;
  netbw)         phase_netbw ;;
  baseline)      phase_baseline ;;
  precheck)      phase_precheck ;;
  netcapture)    phase_netcapture ;;
  measure)       phase_measure ;;
  compare)       phase_compare ;;
  diag4c)        phase_diag4c ;;
  diag4c-compare) phase_diag4c_compare ;;
  collect)       phase_collect ;;
  pids)          phase_pids ;;
  stop)          phase_stop ;;
  cleanup-check) phase_cleanup_check ;;
  budget)        budget_report ;;
  *) sed -n '2,60p' "${BASH_SOURCE[0]}"; exit 2 ;;
esac

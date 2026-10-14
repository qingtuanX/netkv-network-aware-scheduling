#!/usr/bin/env bash
# =============================================================================
# b1_remote_serve.sh —— B1 的**远端**服务启停（在 GPU 机器上以 root 运行）
#
# 子命令：
#   start-prefill  <port> <side_port> <vllm_bin> <model> <served_name> <side_host>
#   start-decode   <port> <side_port> <vllm_bin> <model> <served_name> <side_host>
#   start-baseline <port> <vllm_bin> <model> <served_name>
#   start-proxy    <port> <p_host:port> <d_host:port> <python_bin> <proxy_script>
#   stop           <name>
#   stop-all
#
# 纪律：
#   * 进程用 `nohup setsid ... &` 起，PID 落盘，停的时候按 PID 杀（**不用宽泛 pkill**）
#   * 两端实例的公共启动参数**逐字相同**（见 run_b1.sh 的 COMMON_SERVE_ARGS）
#   * 日志一律留在 /root/b1/logs/<name>.log（这是 4a/4b 的证据之一）
# =============================================================================
set -uo pipefail

RDIR="/root/b1"
RLOGS="${RDIR}/logs"
mkdir -p "${RLOGS}"

# KV 传输需要一个额外的握手侧信道端口。两端各自 5600 不冲突（不同主机）。
# VLLM_NIXL_SIDE_CHANNEL_HOST 必须是**本机内网 IP**：对端要靠它连回来，
# 指向公网或者 localhost 都会握手失败。
#
# 传输层环境变量（依据 vLLM v0.31.0 官方 docs/features/nixl_connector_usage.md §Transport Configuration）：
#   官方多机示例用的是 `UCX_NET_DEVICES=all`；本脚本默认钉成 `eth0`，
#   因为这两台只有 eth0 是本机内网网卡（另有 lo 与 docker0），钉死更确定、也让
#   "走哪张网卡" 这件事在日志里可读。如果握手失败，第一个要试的就是 `all`。
#   两个变量都做成**可覆盖**，这样排障时不用改脚本：
#     B1_UCX_NET_DEVICES / B1_UCX_TLS
B1_UCX_NET_DEVICES="${B1_UCX_NET_DEVICES:-eth0}"
B1_UCX_TLS="${B1_UCX_TLS:-}"

# 显存预算（2026-10-08 补，**默认值 = B1 原值 0.85，行为不变**）：
# 本机 8 GB 单卡做 PREREG_3 预演时，双实例塞不下 0.85，需要按实例调低。
B1_GPU_UTIL="${B1_GPU_UTIL:-0.85}"

# ---------------------------------------------------------------- 生成配置与精度（2026-10-09 补）
# 背景（与前缀缓存那次同一类坑）：**预注册写了 ≠ 真的生效**。B1/PREREG_3 的启动参数里
# 从来没有 --generation-config 与 --dtype，"bf16 / vllm 生成配置"其实一直靠默认值凑。
# B1_4C_PREREG_4.md §2.1 要求每个冻结参数都留下"实际生效"的证据，所以补两个入口：
#   B1_GEN_CONFIG：--generation-config。PREREG_4 冻结为 **vllm**（用 vLLM 自己的默认生成
#     配置，不让模型文件里的 generation_config 悄悄覆盖实验参数）。
#     ⚠️ B1 当年**没传过**这个参数；复现 B1 原始行为需要先确认 vLLM 0.31.0 里不传时的
#     默认语义，再显式设置——不许猜。
#   B1_DTYPE：--dtype。B1 当年不传（auto = 模型自带 bf16）；对 Qwen2.5-1.5B（bf16 权重）
#     显式 bfloat16 与 auto 等价，但显式传才能在启动命令里留证。干预臂 I2 用 B1_DTYPE=float32。
#     ⚠️ 勘误（2026-10-09 本机验收第 1 项抓到）：CLI 合法值是 **bfloat16**，不是 bf16——
#     `vllm serve --help=dtype` 的 choices 为 {auto,bfloat16,float,float16,float32,half}，
#     写 bf16 会被 argparse 拒绝、服务起不来。PREREG_4 §2 同步勘误（只改拼写，不改语义）。
# 是否真生效以服务端证据为准（启动日志 / /metrics / 返回的配置），不以本行为准。
B1_GEN_CONFIG="${B1_GEN_CONFIG:-vllm}"
B1_DTYPE="${B1_DTYPE:-bfloat16}"

COMMON_ENV=(
  "UCX_NET_DEVICES=${B1_UCX_NET_DEVICES}"
  "VLLM_USE_FLASHINFER_SAMPLER=0"
  "HF_HUB_OFFLINE=1"
  "CUDA_HOME=/usr/local/cuda-13.0"
  "PATH=/usr/local/cuda-13.0/bin:/usr/local/bin:/usr/bin:/bin"
)
[[ -n "${B1_UCX_TLS}" ]] && COMMON_ENV+=("UCX_TLS=${B1_UCX_TLS}")

# 干预臂 I1（批不变性，B1_4C_PREREG_4.md §6）：**显式传 B1_BATCH_INVARIANT=1 才加**，
# 默认不加（基线行为不变）。注意"环境变量在"≠"开关生效"——验证协议见 PREREG_4 §6
# （单请求 vs 不同批大小对比）；验证不过就把 I1 记"未执行/未验证"，不得当因果证据。
[[ -n "${B1_BATCH_INVARIANT:-}" ]] && COMMON_ENV+=("VLLM_BATCH_INVARIANT=${B1_BATCH_INVARIANT}")

# 服务端口的绑定地址：**默认只绑实验内网 IP**，不绑 0.0.0.0。
# 为什么：计划书 §5 要求"服务端口只对实验内网开放"。绑 0.0.0.0 会让服务同时出现在
# 公网 IP 上（只要安全组放行就能被外部访问），这既不合规也把实验暴露在公网上。
# run_b1.sh 会显式传 B1_BIND_IP=<本机内网 IP>；缺省值刻意留成 127.0.0.1（最保守）。
B1_BIND_IP="${B1_BIND_IP:-127.0.0.1}"

COMMON_ARGS=(
  --host "${B1_BIND_IP}"
  --max-model-len 8192
  --gpu-memory-utilization "${B1_GPU_UTIL}"
  --enforce-eager
  --generation-config "${B1_GEN_CONFIG}"
  --dtype "${B1_DTYPE}"
)

# ---------------------------------------------------------------- 前缀缓存（2026-10-08 补的）
# 背景：B1_PLAN.md 第 725 行要求「前缀缓存按计划书 §4.3 先关闭」，
# **但 B1 第二轮的脚本里从来没有这个开关**（全脚本 grep `prefix` 零命中），
# 于是三条臂一起落到了 vLLM 的默认值 `enable_prefix_caching=True`。
# 后果：4c 的对照多出一个未受控的混淆项（证据见 B1_REPORT_2.md §3.3 的追加段）。
#
# ⚠️ 默认值是 **off**，也就是**方案要求的那个配置**（安全的一侧默认）。
#    要复现 B1 第二轮那次的偏离配置，必须**显式**传 B1_PREFIX_CACHING=on ——
#    这样"忘了写"只会落到正确的一边。
#
# 注意：这只影响**启动参数**。"到底关没关"以 /metrics 里的
# `vllm:cache_config_info{...enable_prefix_caching="..."}` 为准，不以本行为准。
B1_PREFIX_CACHING="${B1_PREFIX_CACHING:-off}"
if [[ "${B1_PREFIX_CACHING}" == "on" ]]; then
  :  # 不加参数 ⇒ 落到 vLLM 默认（开）
else
  COMMON_ARGS+=(--no-enable-prefix-caching)
fi

# logprobs 口径（2026-10-08 补，PREREG_3 用；**默认不传 ⇒ 保持 B1 原行为**（raw_logprobs））：
# 显式传 B1_LOGPROBS_MODE=processed_logprobs 才切换。校准依据见
# LOGPROBS_CALIB_PREREG.md（processed 分 498/498 与选词自洽，raw 分 22.3% 的步会骗人）。
if [[ -n "${B1_LOGPROBS_MODE:-}" ]]; then
  COMMON_ARGS+=(--logprobs-mode "${B1_LOGPROBS_MODE}")
fi

pidfile() { echo "${RLOGS}/$1.pid"; }

# 起一个带完整环境的后台进程；$! 因为 nohup/setsid 都是 exec，PID 会保留下来
launch() {  # launch <name> <env...> -- <cmd...>
  local name="$1"; shift
  local envs=()
  while [[ "$1" != "--" ]]; do envs+=("$1"); shift; done
  shift
  echo "--- launching ${name}"
  printf '  env: %s\n' "${envs[@]}"
  printf '  cmd: %s\n' "$*"
  nohup env "${envs[@]}" setsid "$@" >"${RLOGS}/${name}.log" 2>&1 </dev/null &
  echo $! >"$(pidfile "${name}")"
  sleep 1
  echo "  pid=$(cat "$(pidfile "${name}")")"
  # 起完立刻看一眼进程还在不在（早期崩溃能第一时间发现）
  if kill -0 "$(cat "$(pidfile "${name}")")" 2>/dev/null; then
    echo "  state=running"
  else
    echo "  state=EXITED (启动即退出，看 ${RLOGS}/${name}.log)"
    tail -n 30 "${RLOGS}/${name}.log"
    return 1
  fi
}

cmd_start_prefill() {
  local port="$1" side="$2" vllm="$3" model="$4" name="$5" side_host="$6"
  local cfg='{"kv_connector":"NixlConnector","kv_role":"kv_producer","kv_load_failure_policy":"fail"}'
  echo "== 前处理实例（kv_producer） port=${port} side=${side_host}:${side}"
  launch prefill \
    "${COMMON_ENV[@]}" \
    "VLLM_NIXL_SIDE_CHANNEL_HOST=${side_host}" \
    "VLLM_NIXL_SIDE_CHANNEL_PORT=${side}" \
    -- \
    "${vllm}" serve "${model}" \
      --port "${port}" \
      --served-model-name "${name}" \
      --kv-transfer-config "${cfg}" \
      "${COMMON_ARGS[@]}"
}

cmd_start_decode() {
  local port="$1" side="$2" vllm="$3" model="$4" name="$5" side_host="$6"
  local cfg='{"kv_connector":"NixlConnector","kv_role":"kv_consumer","kv_load_failure_policy":"fail"}'
  echo "== 生成实例（kv_consumer） port=${port} side=${side_host}:${side}"
  launch decode \
    "${COMMON_ENV[@]}" \
    "VLLM_NIXL_SIDE_CHANNEL_HOST=${side_host}" \
    "VLLM_NIXL_SIDE_CHANNEL_PORT=${side}" \
    -- \
    "${vllm}" serve "${model}" \
      --port "${port}" \
      --served-model-name "${name}" \
      --kv-transfer-config "${cfg}" \
      "${COMMON_ARGS[@]}"
}

cmd_start_baseline() {
  local port="$1" vllm="$2" model="$3" name="$4"
  echo "== 对照臂：单机完整服务（**不带** --kv-transfer-config） port=${port}"
  launch baseline \
    "${COMMON_ENV[@]}" \
    -- \
    "${vllm}" serve "${model}" \
      --port "${port}" \
      --served-model-name "${name}" \
      "${COMMON_ARGS[@]}"
}

cmd_start_proxy() {
  local port="$1" p="$2" d="$3" py="$4" script="$5"
  # 代理只绑回环：客户端在 A 本机跑，代理不需要对外（含内网）暴露。
  # 绑定地址写死 127.0.0.1 而不是 0.0.0.0，满足"服务端口只对实验内网开放"的要求。
  echo "== 代理：把请求先发给前处理、再发给生成 port=${port}（只绑 127.0.0.1）  P=${p}  D=${d}"
  launch proxy \
    "PATH=/usr/local/bin:/usr/bin:/bin" \
    -- \
    "${py}" "${script}" \
      --host 127.0.0.1 --port "${port}" \
      --prefiller-hosts "${p%%:*}" --prefiller-ports "${p##*:}" \
      --decoder-hosts   "${d%%:*}" --decoder-ports   "${d##*:}"
}

# 停一个服务：先温和（SIGTERM 整个进程组），等显存归还；还赖着就 SIGKILL。
# **按 PID 精确杀**，不用 pkill -f（仓库纪律：不用宽泛 pkill）。
cmd_stop() {
  local name="$1"
  local pf; pf="$(pidfile "${name}")"
  echo "== 停 ${name}"
  if [[ ! -f "$pf" ]]; then echo "  没有 PID 文件（该服务这次没起过）"; return 0; fi
  local pid; pid="$(cat "$pf")"
  if ! kill -0 "$pid" 2>/dev/null; then echo "  pid=${pid} 已经不在"; return 0; fi
  echo "  SIGTERM 进程组 -${pid}"
  kill -TERM -- "-${pid}" 2>/dev/null || kill -TERM "${pid}" 2>/dev/null || true
  for i in $(seq 1 45); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
  done
  if kill -0 "$pid" 2>/dev/null; then
    echo "  45 s 还没退，SIGKILL 进程组 -${pid}"
    kill -KILL -- "-${pid}" 2>/dev/null || kill -KILL "${pid}" 2>/dev/null || true
    sleep 3
  fi
  kill -0 "$pid" 2>/dev/null && echo "  **仍然存活**（需要人工处理）" || echo "  已退出"
  echo "--- 显存现状："
  nvidia-smi --query-gpu=memory.used --format=csv,noheader
  return 0
}

cmd_stop_all() {
  echo "########## 停掉本机全部 B1 服务 ##########"
  for n in proxy prefill decode baseline; do
    cmd_stop "$n" || true
  done
  echo
  echo "########## 残留检查 ##########"
  ps -eo pid,etimes,cmd | grep -E '[v]llm serve|[t]oy_proxy_server' || echo "无 vllm/代理进程"
  ss -ltn | grep -E ':(8100|8192|8200|8300|5600)\b' || echo "B1 端口全部未监听"
  nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader
}

case "${1:-}" in
  start-prefill)  shift; cmd_start_prefill  "$@" ;;
  start-decode)   shift; cmd_start_decode   "$@" ;;
  start-baseline) shift; cmd_start_baseline "$@" ;;
  start-proxy)    shift; cmd_start_proxy    "$@" ;;
  stop)           shift; cmd_stop           "$@" ;;
  stop-all)       cmd_stop_all ;;
  *) echo "未知子命令: ${1:-}"; exit 2 ;;
esac

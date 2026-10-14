#!/usr/bin/env bash
# =============================================================================
# run_prereg3_cloud.sh —— B1_4C_PREREG_3 云端采数的执行脚本（runbook 的脚本化）
#
# 用法（WSL bash）：
#   bash run_prereg3_cloud.sh check            # 只做连通与环境核对（免费，先跑这个）
#   bash run_prereg3_cloud.sh sync             # 同步本轮脚本并核对 sha256
#   bash run_prereg3_cloud.sh baseline         # 对照臂：起服务→C1冒烟→预热3→正式3→metrics→停
#   bash run_prereg3_cloud.sh disagg           # 拆分臂：P/D/代理→C1冒烟→预热3→正式3→metrics→停
#   bash run_prereg3_cloud.sh pull             # 拉回产物 + 本地分析
#   bash run_prereg3_cloud.sh check sync baseline disagg pull   # 一条龙
#
# 判据全部在 B1_4C_PREREG_3.md（冻结）：C1 冒烟不过 ⇒ 不采数、就地收尾。
# 传输层重试规则（2026-10-08 加入，因实测 WSL→云端 ssh 会偶发"静默死亡"：
#   进程无任何输出、远端无任何痕迹、随后的调用又正常）：
#     客户端日志里没有 "target=" 行 ⇒ 客户端从未到达网络阶段 ⇒ 未产生测量 ⇒
#       按传输故障重试（≤3 次；每次记 client-empty / 成功记 client-retry）；
#     一旦有 "target=" ⇒ 已产生测量 ⇒ 绝不重试、按判据执行。
#   stop / metrics 同理："输出为空 = 重试"。收尾再兜底一次 stop-all。
# 纪律：失败不自动重跑；按 PID 停服务；产物 append-only；跑完提醒停机。
# 公网 IP 若变更：A_PUB=xx B_PUB=yy bash run_prereg3_cloud.sh ...
# =============================================================================
set -u

SIM=/mnt/e/代码项目文件/论文/sim
RIDFILE="$SIM/results/b1/CURRENT_PREREG3_RUN_ID"
if [[ -f "$RIDFILE" ]]; then RUNID="$(cat "$RIDFILE")"; else
  RUNID="b1_4c3_$(date -u +%Y%m%dT%H%M%SZ)"; echo "$RUNID" > "$RIDFILE"; fi
R="$SIM/results/b1/$RUNID"
mkdir -p "$R"
SSH_KEY=/mnt/d/Documents/API/_网络感应\ \(1\).pem
A_PUB="${A_PUB:-8.155.133.216}"
B_PUB="${B_PUB:-8.155.131.183}"
A_IN=172.29.31.52
B_IN=172.29.31.51
SSH_OPTS=(-i "$SSH_KEY" -o IdentitiesOnly=yes -o StrictHostKeyChecking=no \
          -o UserKnownHostsFile=/tmp/kh_net -o BatchMode=yes -o ConnectTimeout=20 -o LogLevel=ERROR)
MODEL=Qwen/Qwen2.5-1.5B-Instruct   # served-model-name 与客户端 --model（名称，不是路径）
MODEL_PATH=/root/b1/models/Qwen2.5-1.5B-Instruct   # 服务端加载的本地目录（B1 同款；HF_HUB_OFFLINE 下用 repo id 会报 LocalEntryNotFoundError）
PROMPTS=/root/b1/prereg3_prompts.json
RB=/root/b1/prereg3_out            # 本轮远端输出目录（/root/b1/out 是 B1 旧证据，不碰）
RPROXY=/root/b1/official/toy_proxy_server.py
export HF_HOME=/root/hf-cache   # 仅本地分析用不到；远端脚本自带

ts(){ date -u +%Y-%m-%dT%H:%M:%SZ; }
log(){ printf '[%s] %s\n' "$(ts)" "$*"; printf '[%s] %s\n' "$(ts)" "$*" >>"$R/run.log"; }
rec(){ echo "$(ts),$1,$2,$3" >> "$R/record.csv"; }
rsh(){ local h="$1"; shift; ssh "${SSH_OPTS[@]}" "root@$h" "$@"; }

check_env_of() {  # $1 = pub ip
  rsh "$1" 'hostname; nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv,noheader; \
    /root/b1/venv/bin/python -c "import vllm,nixl,torch;print(vllm.__version__,torch.cuda.is_available())"; \
    ls /root/b1/venv/bin/vllm >/dev/null 2>&1 && echo tools-ok'
}

wait_ready() {  # $1 = pub ip $2 = url（在该主机上探测）$3 = name
  local code
  for i in $(seq 1 90); do
    code=$(rsh "$1" "curl -s -o /dev/null -w '%{http_code}' '$2' || true")
    if [[ "$code" == "200" ]]; then log "$3 就绪（第 $i 次探测）"; return 0; fi
    sleep 2
  done
  log "$3 180s 未就绪"; return 1
}

client_call() {  # $1=url $2=远端输出文件名 $3=本地日志名 $4=only-ids（可空）
  # 传输层重试规则（见文件头）：日志里没有 "target=" ⇒ 客户端没跑起来 ⇒ 重试；有 ⇒ 不重试
  local url="$1" outname="$2" logname="$3" only="${4:-}" attempt extra=""
  [ -n "$only" ] && extra="--only-ids $only"
  for attempt in 1 2 3; do
    rsh "$A_PUB" "mkdir -p $RB && cd /root/b1 && timeout 1800 ./venv/bin/python b1_client.py --url '$url' --model '$MODEL' \
      --prompts-file $PROMPTS --logprobs 20 $extra \
      --out $RB/$outname" >"$R/$logname" 2>&1
    if grep -q 'target=' "$R/$logname" 2>/dev/null; then
      [ "$attempt" -gt 1 ] && { log "[传输] $outname 第 $attempt 次调用成功"; rec "client-retry" "$attempt" "$outname"; }
      return 0
    fi
    log "[传输] $outname 客户端无输出（第 $attempt 次；ssh 链路故障）——未产生测量，重试"
    rec "client-empty" "$attempt" "$outname"
    sleep 5
  done
  return 1
}

stop_retry() {  # $1=ssh目标 $2=子命令(stop baseline|stop-all) $3=日志名
  local t
  for t in 1 2 3; do
    rsh "$1" "bash /root/b1/b1_remote_serve.sh $2" >"$R/stop_$3.log" 2>&1
    [ -s "$R/stop_$3.log" ] && ! grep -q '未知子命令' "$R/stop_$3.log" && return 0
    log "[传输/参数] stop（$2 @ $1）无有效输出（第 $t 次），重试"; sleep 2
  done
  return 1
}

fetch_metrics() {  # $1=ssh目标 $2=url $3=远端文件名（落在 /root/b1/）
  local t
  for t in 1 2 3; do
    rsh "$1" "curl -s '$2' > /root/b1/$3"
    if rsh "$1" "test -s /root/b1/$3 && echo m-ok" | grep -q m-ok; then return 0; fi
    log "[传输] $3 为空（第 $t 次）重试"; sleep 2
  done
  log "[传输] $3 三次都空（不致命，继续）"
  return 1
}

start_and_verify() {  # $1=ssh目标 $2=服务名(pid/log 前缀) $3=标签
  # 启动后等 20 秒看进程还活着吗；早崩立刻拉日志尾部（不空等 180s 的 wait_ready）
  sleep 20
  if rsh "$1" "p=\$(cat /root/b1/logs/$2.pid 2>/dev/null); kill -0 \$p 2>/dev/null && echo up || echo dead" | grep -q up; then
    log "[$3] 启动后 20s 进程存活 ✓"; return 0
  fi
  log "[$3] 启动后 20s 进程已死 —— 日志尾部如下"
  rsh "$1" "tail -30 /root/b1/logs/$2.log" | tee -a "$R/$2_early_death.log"
  return 1
}

smoke_and_gate() {  # $1 = url（在 A 上打）$2 = arm 名
  local url="$1" arm="$2"
  client_call "$url" "smoke_$arm.json" "smoke_$arm.log" "q01_rep6 q02_rep10 q03_rep14" \
    || { log "[$arm] 客户端 3 次都无输出（传输故障）⇒ 停，需人工检查"; rec "$arm-smoke" 1 "transport"; return 1; }
  rsh "$A_PUB" "cd /root/b1 && ./venv/bin/python b1_4c3_analyze.py smoke --file $RB/smoke_$arm.json" \
    | tee "$R/smoke_$arm.verdict"
  local rc=${PIPESTATUS[0]}
  rec "$arm-smoke" "$rc" "$(tail -1 "$R/smoke_$arm.verdict")"
  [ "$rc" -eq 0 ] || { log "[$arm] C1 冒烟 FAIL ⇒ 按预注册 §8 不采数"; return 1; }
  return 0
}

run_rounds() {  # $1 = url $2 = arm
  local url="$1" arm="$2" k crc
  for k in 1 2 3; do
    log "[$arm] 预热 r$k"
    client_call "$url" "warmup_${arm}_r$k.json" "warmup_${arm}_r$k.log"
    crc=$?
    [ "$crc" -eq 0 ] || log "[$arm] 预热 r$k：客户端 3 次无输出（传输故障），本回合缺失"
    rec "$arm-warmup-r$k" "$crc" ""
  done
  for k in 1 2 3; do
    log "[$arm] 正式 r$k"
    client_call "$url" "arm_${arm}_r$k.json" "arm_${arm}_r$k.log"
    crc=$?
    [ "$crc" -eq 0 ] || log "[$arm] 正式 r$k：客户端 3 次无输出（传输故障），本回合缺失"
    rec "$arm-formal-r$k" "$crc" ""
  done
}

phase_check() {
  log "=== check ==="; rm -f /tmp/kh_net
  for pair in "A:$A_PUB" "B:$B_PUB"; do
    n=${pair%%:*}; ip=${pair##*:}
    if check_env_of "$ip" >"$R/check_$n.txt" 2>&1; then
      log "[$n] $ip 连通"; cat "$R/check_$n.txt"; rec "check-$n" 0 "$ip"
      grep -q tools-ok "$R/check_$n.txt" || { log "[$n] venv/vllm 不全 ⇒ 看 $R/check_$n.txt"; rec "check-$n" 1 "tools"; return 1; }
    else
      log "[$n] $ip 连不通或环境检查未过 ⇒ 看 $R/check_$n.txt；公网 IP 变了就用 A_PUB=… B_PUB=… 重跑"
      rec "check-$n" 1 "$ip"; return 1
    fi
  done
  # 代理脚本只在 A（B1 惯例：代理跑在 A 上）
  if rsh "$A_PUB" "ls $RPROXY >/dev/null && echo proxy-ok" | grep -q proxy-ok; then
    log "[A] toy_proxy 就位 ✓"; rec check-proxy 0 ""
  else
    log "[A] toy_proxy 不在 $RPROXY ⇒ 停"; rec check-proxy 1 ""; return 1
  fi
  return 0
}

phase_sync() {
  log "=== sync ==="
  scp -q "${SSH_OPTS[@]}" "$SIM/k8s_test/b1_remote_serve.sh" "$SIM/k8s_test/b1_client.py" \
    "root@$A_PUB:/root/b1/" && rec sync-A 0 ""
  scp -q "${SSH_OPTS[@]}" "$SIM/k8s_test/b1_remote_serve.sh" "$SIM/k8s_test/b1_client.py" \
    "$SIM/k8s_test/b1_4c3_analyze.py" "root@$B_PUB:/root/b1/" && rec sync-B 0 ""
  scp -q "${SSH_OPTS[@]}" "$SIM/k8s_test/b1_remote_serve.sh" "$SIM/k8s_test/b1_client.py" \
    "$SIM/k8s_test/b1_4c3_analyze.py" "root@$A_PUB:/root/b1/" && rec sync-A2 0 ""
  sha256sum "$SIM/k8s_test/b1_remote_serve.sh" "$SIM/k8s_test/b1_client.py" "$SIM/k8s_test/b1_4c3_analyze.py" \
    | awk '{print $1}' > "$R/sha_local.txt"
  for pair in "A:$A_PUB" "B:$B_PUB"; do
    n=${pair%%:*}; ip=${pair##*:}
    rsh "$ip" 'cd /root/b1 && sha256sum b1_remote_serve.sh b1_client.py b1_4c3_analyze.py 2>/dev/null' \
      | awk '{print $1}' > "$R/sha_$n.txt"
    if diff -q "$R/sha_local.txt" "$R/sha_$n.txt" >/dev/null 2>&1; then log "[$n] 脚本哈希一致 ✓"
    else log "[$n] 脚本哈希不一致 ⇒ 停"; rec "sha-$n" 1 ""; return 1; fi
  done
  rsh "$A_PUB" "cd /root/b1 && ./venv/bin/python b1_client.py gen-prompts --preset v2 --out $PROMPTS" \
    >/dev/null 2>&1 && rsh "$A_PUB" "sha256sum $PROMPTS" | tee "$R/prompts_v2.sha256"
  rec genprompts 0 ""
  return 0
}

phase_baseline() {
  log "=== 对照臂 baseline（B:8300）==="
  rsh "$B_PUB" "B1_BIND_IP=$B_IN B1_LOGPROBS_MODE=processed_logprobs \
    bash /root/b1/b1_remote_serve.sh start-baseline 8300 /root/b1/venv/bin/vllm '$MODEL_PATH' '$MODEL'" \
    >"$R/start_baseline.log" 2>&1
  rec start-baseline "$?" ""
  start_and_verify "$B_PUB" baseline baseline || { stop_retry "$B_PUB" "stop baseline" "baseline_fail" || true; return 1; }
  wait_ready "$B_PUB" "http://$B_IN:8300/health" baseline || { stop_retry "$B_PUB" "stop baseline" "baseline_fail" || true; return 1; }
  smoke_and_gate "http://$B_IN:8300" baseline \
    || { stop_retry "$B_PUB" "stop baseline" "baseline_fail" || true; return 1; }
  run_rounds "http://$B_IN:8300" baseline
  fetch_metrics "$B_PUB" "http://$B_IN:8300/metrics" metrics_baseline_t1.txt || true
  rsh "$B_PUB" "grep -o 'enable_prefix_caching=\"[^\"]*\"' /root/b1/metrics_baseline_t1.txt | sort -u | tee /root/b1/metrics_baseline_proof.txt"
  rec metrics-baseline 0 ""
  stop_retry "$B_PUB" "stop baseline" "baseline" || log "[传输] baseline 停止未确认，请在 pull 阶段兜底"
  rec stop-baseline 0 ""
  return 0
}

phase_disagg() {
  log "=== 拆分臂 P=A:8100 D=B:8200 代理=A:8192 ==="
  rsh "$A_PUB" "B1_BIND_IP=$A_IN B1_LOGPROBS_MODE=processed_logprobs \
    bash /root/b1/b1_remote_serve.sh start-prefill 8100 5600 /root/b1/venv/bin/vllm '$MODEL_PATH' '$MODEL' $A_IN" \
    >"$R/start_prefill.log" 2>&1
  # 注意：start-decode 末位必须是 **B 自己的内网 IP**（踩过：传成 A_IN 会让 nixl 握手线程
  # ZMQ 绑定失败→服务装死、永远不就绪；对照 run_b1.sh:1614 的写法）
  rsh "$B_PUB" "B1_BIND_IP=$B_IN B1_LOGPROBS_MODE=processed_logprobs \
    bash /root/b1/b1_remote_serve.sh start-decode 8200 5600 /root/b1/venv/bin/vllm '$MODEL_PATH' '$MODEL' $B_IN" \
    >"$R/start_decode.log" 2>&1
  rec start-PD 0 ""
  start_and_verify "$A_PUB" prefill prefill || { stop_retry "$A_PUB" "stop-all" "disagg_failA" || true; stop_retry "$B_PUB" "stop-all" "disagg_failB" || true; return 1; }
  start_and_verify "$B_PUB" decode decode || { stop_retry "$A_PUB" "stop-all" "disagg_failA" || true; stop_retry "$B_PUB" "stop-all" "disagg_failB" || true; return 1; }
  wait_ready "$A_PUB" "http://$A_IN:8100/health" prefill || { stop_retry "$A_PUB" "stop-all" "disagg_failA" || true; stop_retry "$B_PUB" "stop-all" "disagg_failB" || true; return 1; }
  wait_ready "$B_PUB" "http://$B_IN:8200/health" decode || { stop_retry "$A_PUB" "stop-all" "disagg_failA" || true; stop_retry "$B_PUB" "stop-all" "disagg_failB" || true; return 1; }
  rsh "$A_PUB" "bash /root/b1/b1_remote_serve.sh start-proxy 8192 $A_IN:8100 $B_IN:8200 \
    /root/b1/venv/bin/python $RPROXY" >"$R/start_proxy.log" 2>&1
  rec start-proxy "$?" ""
  start_and_verify "$A_PUB" proxy proxy || { stop_retry "$A_PUB" "stop-all" "disagg_failA" || true; stop_retry "$B_PUB" "stop-all" "disagg_failB" || true; return 1; }
  wait_ready "$A_PUB" "http://127.0.0.1:8192/healthcheck" proxy \
    || { stop_retry "$A_PUB" "stop-all" "disagg_failA" || true; stop_retry "$B_PUB" "stop-all" "disagg_failB" || true; return 1; }
  smoke_and_gate "http://127.0.0.1:8192" disagg \
    || { stop_retry "$A_PUB" "stop-all" "disagg_failA" || true; stop_retry "$B_PUB" "stop-all" "disagg_failB" || true; return 1; }
  run_rounds "http://127.0.0.1:8192" disagg
  fetch_metrics "$A_PUB" "http://$A_IN:8100/metrics" metrics_prefill_t1.txt || true
  fetch_metrics "$B_PUB" "http://$B_IN:8200/metrics" metrics_decode_t1.txt || true
  rsh "$B_PUB" "grep -o 'enable_prefix_caching=\"[^\"]*\"' /root/b1/metrics_decode_t1.txt | sort -u" || true
  rec metrics-disagg 0 ""
  stop_retry "$A_PUB" "stop-all" "disagg_A" || log "[传输] 拆分臂 A 停止未确认，请在 pull 阶段兜底"
  stop_retry "$B_PUB" "stop-all" "disagg_B" || log "[传输] 拆分臂 B 停止未确认，请在 pull 阶段兜底"
  rec stop-disagg 0 ""
  return 0
}

phase_pull() {
  log "=== pull + analyze ==="
  scp -q "${SSH_OPTS[@]}" "root@$A_PUB:$RB/arm_*.json" "root@$A_PUB:$RB/warmup_*.json" \
    "root@$A_PUB:$RB/smoke_*.json" "$R/" 2>/dev/null
  scp -q "${SSH_OPTS[@]}" "root@$A_PUB:/root/b1/metrics_*.txt" "root@$B_PUB:/root/b1/metrics_*.txt" "$R/" 2>/dev/null
  local n_arm n_smoke
  n_arm=$(ls "$R"/arm_*.json 2>/dev/null | wc -l); n_smoke=$(ls "$R"/smoke_*.json 2>/dev/null | wc -l)
  if [ "$n_arm" -lt 6 ] || [ "$n_smoke" -lt 2 ]; then
    log "[传输] 拉回不足（arm=$n_arm/6, smoke=$n_smoke/2），重试一次 scp"; sleep 3
    scp -q "${SSH_OPTS[@]}" "root@$A_PUB:$RB/arm_*.json" "root@$A_PUB:$RB/warmup_*.json" \
      "root@$A_PUB:$RB/smoke_*.json" "$R/" 2>/dev/null
  fi
  rec pull 0 ""
  # 收尾保险：再喊一次 stop-all（若前面停服命令被传输故障吞掉，这里兜底；不致命）
  stop_retry "$A_PUB" "stop-all" "pull_A" || log "[传输] pull 兜底 stop-all @ A 未确认，请人工检查"
  stop_retry "$B_PUB" "stop-all" "pull_B" || log "[传输] pull 兜底 stop-all @ B 未确认，请人工检查"
  python3 "$SIM/k8s_test/b1_4c3_analyze.py" analyze --run-dir "$R" --out-dir "$R" | tee "$R/analyze.log"
  rec analyze "$?" ""
  log "产物目录：$R"
  log "★ 收尾提醒：控制台把两台实例用【节省停机模式】停机（普通停机继续收钱）；停完从本机探测两个公网 IP 的 22 端口应全部超时。"
  return 0
}

for ph in "$@"; do
  case "$ph" in
    check)    phase_check    || exit 1 ;;
    sync)     phase_sync     || exit 1 ;;
    baseline) phase_baseline || exit 1 ;;
    disagg)   phase_disagg   || exit 1 ;;
    pull)     phase_pull     || exit 1 ;;
    *) echo "未知阶段：$ph"; exit 2 ;;
  esac
done
log "全部所选阶段完成"

#!/usr/bin/env bash
# =============================================================================
# run_prereg4_cloud.sh —— B1_4C_PREREG_4 云端收口采数的执行脚本（WSL bash）
#
# 用法：
#   bash run_prereg4_cloud.sh check      # 连通与环境核对（先跑这个）
#   bash run_prereg4_cloud.sh sync       # 同步冻结版脚本/提示词并核对 sha256
#   bash run_prereg4_cloud.sh baseline   # 对照臂：起服务→参数证据→C1冒烟→calib→固定前缀→自由生成3→metrics→停
#   bash run_prereg4_cloud.sh disagg     # 拆分臂：P/D/代理→证据→C1冒烟→calib→固定前缀→自由生成3→KV指标→停
#   bash run_prereg4_cloud.sh i1         # 干预臂 I1（批不变性）：两臂重启+固定前缀重跑
#   bash run_prereg4_cloud.sh i2         # 干预臂 I2（fp32）：同上（时间不够先砍它）
#   bash run_prereg4_cloud.sh pull       # 拉回产物 + 本地 analyze + 兜底停服
#   bash run_prereg4_cloud.sh check sync baseline disagg i1 i2 pull   # 一条龙
#
# 判据全部在 B1_4C_PREREG_4.md（冻结）：
#   C1 冒烟不过 ⇒ 不采数、就地收尾；E3 不过 ⇒ 停；前一项失败就停。
#   优先级：基线固定前缀 > I1 > I2（时间预算不够砍 I2）。
# 传输层重试规则（沿用 run_prereg3_cloud.sh 文件头）：
#   客户端/采集日志里没有成功标记 ⇒ 未产生测量 ⇒ 重试 ≤3 次；
#   一旦有成功标记 ⇒ 已测量 ⇒ 绝不重试。stop/metrics"输出为空=重试"，收尾兜底 stop-all。
# 公网 IP 若变更：A_PUB=xx B_PUB=yy bash run_prereg4_cloud.sh ...
# 纪律：失败不自动重跑；按 PID 停服务；产物 append-only；跑完提醒节省停机。
# =============================================================================
set -u

SIM=/mnt/e/代码项目文件/论文/sim
K8S=$SIM/k8s_test
RIDFILE="$SIM/results/b1/CURRENT_PREREG4_RUN_ID"
if [[ -f "$RIDFILE" ]]; then RUNID="$(cat "$RIDFILE")"; else
  RUNID="b1_4c4_$(date -u +%Y%m%dT%H%M%SZ)"; echo "$RUNID" > "$RIDFILE"; fi
R="$SIM/results/b1/$RUNID"
mkdir -p "$R"
SSH_KEY=$HOME/.ssh/b1_key.pem
A_PUB="${A_PUB:-8.155.133.216}"
B_PUB="${B_PUB:-8.155.131.183}"
A_IN=172.29.31.52
B_IN=172.29.31.51
SSH_OPTS=(-i "$SSH_KEY" -o IdentitiesOnly=yes -o StrictHostKeyChecking=no \
          -o UserKnownHostsFile=/tmp/kh_net -o BatchMode=yes -o ConnectTimeout=20 \
          -o LogLevel=ERROR -o ServerAliveInterval=30 -o ServerAliveCountMax=6)
MODEL=Qwen/Qwen2.5-1.5B-Instruct
MODEL_PATH=/root/b1/models/Qwen2.5-1.5B-Instruct
RB=/root/b1/prereg4_out
PROMPTS_V2=/root/b1/prereg4_prompts_v2.json
PROMPTS_CAL=/root/b1/prereg4_cal10.json
RPROXY=/root/b1/official/toy_proxy_server.py
PY=/root/b1/venv/bin/python
VLLM=/root/b1/venv/bin/vllm

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

stop_retry() {  # $1=ssh目标 $2=子命令 $3=日志名（输出为空=重试）
  local n
  for n in 1 2 3; do
    rsh "$1" "bash /root/b1/b1_remote_serve.sh $2" >"$R/stop_$3.log" 2>&1
    if [[ -s "$R/stop_$3.log" ]]; then return 0; fi
    log "[传输] stop $3 输出为空（第 $n 次）重试"; sleep 3
  done
  return 1
}

fetch_metrics() {  # $1=ssh目标 $2=url $3=远端/本地文件名
  local n
  for n in 1 2 3; do
    rsh "$1" "curl -sf -m 20 '$2' > /root/b1/$3" >"$R/mf_$3.log" 2>&1
    if rsh "$1" "test -s /root/b1/$3"; then
      scp -q "${SSH_OPTS[@]}" "root@$1:/root/b1/$3" "$R/$3" 2>/dev/null && return 0
    fi
    log "[传输] metrics $3 为空（第 $n 次）重试"; sleep 3
  done
  return 1
}

start_and_verify() {  # $1=ssh目标 $2=服务名 $3=标签
  if rsh "$1" "test -s /root/b1/logs/$2.pid" && rsh "$1" "kill -0 \$(cat /root/b1/logs/$2.pid) 2>/dev/null"; then
    log "$3 进程在（pid=$(rsh "$1" "cat /root/b1/logs/$2.pid")）"; return 0
  fi
  log "$3 进程不在（启动即退出？看 /root/b1/logs/$2.log）"; return 1
}

# ---- 客户端调用（b1_client.py，自由生成/C1 冒烟；传输重试：无 target= 就重试）
client_call() {  # $1=url $2=远端输出名 $3=本地日志名 $4=only-ids(可空)
  local url="$1" outname="$2" logname="$3" only="${4:-}" attempt extra=""
  [ -n "$only" ] && extra="--only-ids $only"
  for attempt in 1 2 3; do
    rsh "$A_PUB" "mkdir -p $RB && cd /root/b1 && timeout 1800 ./venv/bin/python b1_client.py --url '$url' \
      --model '$MODEL' --prompts-file $PROMPTS_V2 --logprobs 20 --explicit-gen-params $extra \
      --out $RB/$outname" >"$R/$logname" 2>&1
    if grep -q 'target=' "$R/$logname" 2>/dev/null; then
      [ "$attempt" -gt 1 ] && { log "[传输] $outname 第 $attempt 次成功"; rec "client-retry" "$attempt" "$outname"; }
      return 0
    fi
    log "[传输] $outname 无 target=（第 $attempt 次）——未产生测量，重试"; rec "client-empty" "$attempt" "$outname"
    sleep 5
  done
  return 1
}

# ---- 固定前缀工具调用（b1_prefix_4c.py；成功标记：collect=target= 、calib=wrote calib_）
prefix_call() {  # $1=mode(collect|calib) $2=成功标记正则 $3=日志名 其余=工具参数
  local mode="$1" marker="$2" logname="$3"; shift 3
  local attempt
  for attempt in 1 2 3; do
    rsh "$A_PUB" "mkdir -p $RB && cd /root/b1 && timeout 3600 ./venv/bin/python b1_prefix_4c.py $mode $*" \
      >"$R/$logname" 2>&1
    if grep -q "$marker" "$R/$logname" 2>/dev/null; then
      [ "$attempt" -gt 1 ] && { log "[传输] $logname 第 $attempt 次成功"; rec "prefix-retry" "$attempt" "$logname"; }
      return 0
    fi
    log "[传输] $logname 无成功标记（第 $attempt 次）——未产生测量，重试"; rec "prefix-empty" "$attempt" "$logname"
    sleep 5
  done
  return 1
}

# ---- 参数生效证据（PREREG_4 §2.1）：启动日志 + /metrics + /version
grab_evidence() {  # $1=ssh目标 $2=服务名(baseline|prefill|decode) $3=标签 $4=服务根URL（含 http://）
  rsh "$1" "grep -m1 'non-default args' /root/b1/logs/$2.log" >"$R/evidence_$3_args.txt" 2>&1 || true
  fetch_metrics "$1" "$4/metrics" "metrics_$3_proof.txt" || true
}

# ---- C1 冒烟 + 门（PREREG_3 §2：吐词 == processed 第一名；不过不采数）
smoke_and_gate() {  # $1 = url（在 A 上打）$2 = arm 名
  client_call "$1" "smoke_$2.json" "smoke_$2.log" "q01_rep6 q02_rep10 q03_rep14" || return 1
  scp -q "${SSH_OPTS[@]}" "root@$A_PUB:$RB/smoke_$2.json" "$R/" 2>/dev/null
  python3 "$K8S/b1_4c3_analyze.py" smoke --file "$R/smoke_$2.json" | tee "$R/smoke_$2.verdict"
  local rc=${PIPESTATUS[0]}   # 必须在 rec 之前取——任何后续命令都会覆盖 PIPESTATUS
  rec "smoke-$2" "$rc" ""
  [[ "$rc" -eq 0 ]]
}

# ===================================================================== phases

phase_check() {
  log "=== check：连通与环境 ==="
  check_env_of "$A_PUB" | tee "$R/check_A.txt"; rec check-A "${PIPESTATUS[0]}" ""
  check_env_of "$B_PUB" | tee "$R/check_B.txt"; rec check-B "${PIPESTATUS[0]}" ""
}

phase_sync() {
  log "=== sync：冻结版脚本与提示词 ==="
  # 提示词在本地生成（与验收记录同源同字节），发两台
  python3 "$K8S/b1_client.py" gen-prompts --preset v2   --out "$R/prereg4_prompts_v2.json"
  python3 "$K8S/b1_client.py" gen-prompts --preset cal10 --out "$R/prereg4_cal10.json"
  local h
  for h in "$A_PUB" "$B_PUB"; do
    rsh "$h" "mkdir -p $RB"
    scp -q "${SSH_OPTS[@]}" "$K8S/b1_remote_serve.sh" "$K8S/b1_client.py" \
      "$K8S/b1_prefix_4c.py" "$K8S/b1_batchprobe.py" "$K8S/b1_4c3_analyze.py" \
      "$K8S/b1_metrics_delta.py" "$K8S/b1_kv_table.py" "root@$h:/root/b1/"
    scp -q "${SSH_OPTS[@]}" "$R/prereg4_prompts_v2.json" "root@$h:$PROMPTS_V2"
    scp -q "${SSH_OPTS[@]}" "$R/prereg4_cal10.json" "root@$h:$PROMPTS_CAL"
    rsh "$h" "cd /root/b1 && sha256sum b1_remote_serve.sh b1_client.py b1_prefix_4c.py b1_batchprobe.py \
      b1_4c3_analyze.py b1_metrics_delta.py b1_kv_table.py $PROMPTS_V2 $PROMPTS_CAL" >"$R/sync_sha_$h.txt"
  done
  sha256sum "$K8S/b1_remote_serve.sh" "$K8S/b1_client.py" "$K8S/b1_prefix_4c.py" "$K8S/b1_batchprobe.py" \
    "$K8S/b1_4c3_analyze.py" "$K8S/b1_metrics_delta.py" "$K8S/b1_kv_table.py" \
    "$R/prereg4_prompts_v2.json" "$R/prereg4_cal10.json" >"$R/sync_sha_local.txt"
  diff <(awk '{print $1}' "$R/sync_sha_local.txt" | sort) \
       <(awk '{print $1}' "$R/sync_sha_$A_PUB.txt" | sort) >/dev/null \
    && log "A 机 sha256 与本地一致 ✓" || { log "!! A 机 sha256 不一致"; return 1; }
  diff <(awk '{print $1}' "$R/sync_sha_local.txt" | sort) \
       <(awk '{print $1}' "$R/sync_sha_$B_PUB.txt" | sort) >/dev/null \
    && log "B 机 sha256 与本地一致 ✓" || { log "!! B 机 sha256 不一致"; return 1; }
  rec sync 0 ""
}

phase_baseline() {
  log "=== 对照臂 baseline（B:8300；干预臂经 EXTRA_ENV 传 B1_BATCH_INVARIANT / B1_DTYPE）==="
  local extra_env="${EXTRA_ENV:-}"
  rsh "$B_PUB" "B1_BIND_IP=$B_IN B1_LOGPROBS_MODE=processed_logprobs $extra_env \
    bash /root/b1/b1_remote_serve.sh start-baseline 8300 $VLLM '$MODEL_PATH' '$MODEL'" \
    >"$R/start_baseline.log" 2>&1
  rec start-baseline "$?" "${EXTRA_ENV:-none}"
  start_and_verify "$B_PUB" baseline baseline || return 1
  wait_ready "$B_PUB" "http://$B_IN:8300/health" baseline || return 1
  grab_evidence "$B_PUB" baseline baseline "http://$B_IN:8300"
  smoke_and_gate "http://$B_IN:8300" "base${ARM_SUFFIX:-}" \
    || { stop_retry "$B_PUB" "stop baseline" "baseline_fail" || true; return 1; }
  prefix_call calib 'wrote calib_' "calib_base${ARM_SUFFIX:-}.log" \
    --url "http://$B_IN:8300" --arm "base${ARM_SUFFIX:-}" --prompts-file "$PROMPTS_CAL" \
    --max-new 40 --reps 10 --out-dir "$RB" || return 1
  prefix_call collect 'target=' "collect_base${ARM_SUFFIX:-}.log" \
    --url "http://$B_IN:8300" --arm "base${ARM_SUFFIX:-}" --prompts-file "$PROMPTS_V2" \
    --max-new 64 --out-dir "$RB" || return 1
  local r
  for r in 1 2 3; do
    client_call "http://$B_IN:8300" "free_base${ARM_SUFFIX:-}_r$r.json" "free_base${ARM_SUFFIX:-}_r$r.log" || return 1
  done
  fetch_metrics "$B_PUB" "http://$B_IN:8300/metrics" "metrics_baseline${ARM_SUFFIX:-}_t1.txt" || true
  stop_retry "$B_PUB" "stop baseline" "baseline" || log "baseline 停止未确认，pull 兜底"
  rec stop-baseline 0 ""
  return 0
}

phase_disagg() {
  log "=== 拆分臂 P=A:8100 D=B:8200 代理=A:8192 ==="
  local extra_env="${EXTRA_ENV:-}"
  rsh "$A_PUB" "B1_BIND_IP=$A_IN B1_LOGPROBS_MODE=processed_logprobs $extra_env \
    bash /root/b1/b1_remote_serve.sh start-prefill 8100 5600 $VLLM '$MODEL_PATH' '$MODEL' $A_IN" \
    >"$R/start_prefill.log" 2>&1
  # ⚠️ start-decode 末位必须是 **B 自己的内网 IP**（踩过：传成 A_IN ⇒ nixl 握手线程
  # ZMQ 绑定失败 → 服务装死）
  rsh "$B_PUB" "B1_BIND_IP=$B_IN B1_LOGPROBS_MODE=processed_logprobs $extra_env \
    bash /root/b1/b1_remote_serve.sh start-decode 8200 5600 $VLLM '$MODEL_PATH' '$MODEL' $B_IN" \
    >"$R/start_decode.log" 2>&1
  rec start-PD 0 "${EXTRA_ENV:-none}"
  start_and_verify "$A_PUB" prefill prefill || { stop_retry "$A_PUB" "stop-all" "disagg_A" || true; stop_retry "$B_PUB" "stop-all" "disagg_B" || true; return 1; }
  start_and_verify "$B_PUB" decode decode || { stop_retry "$A_PUB" "stop-all" "disagg_A" || true; stop_retry "$B_PUB" "stop-all" "disagg_B" || true; return 1; }
  wait_ready "$A_PUB" "http://$A_IN:8100/health" prefill || return 1
  wait_ready "$B_PUB" "http://$B_IN:8200/health" decode || return 1
  rsh "$A_PUB" "bash /root/b1/b1_remote_serve.sh start-proxy 8192 $A_IN:8100 $B_IN:8200 $PY $RPROXY" \
    >"$R/start_proxy.log" 2>&1
  start_and_verify "$A_PUB" proxy proxy || return 1
  wait_ready "$A_PUB" "http://127.0.0.1:8192/healthcheck" proxy || return 1
  grab_evidence "$A_PUB" prefill prefill "http://$A_IN:8100"
  grab_evidence "$B_PUB" decode decode "http://$B_IN:8200"
  # KV 4b 证据的 t0 快照：在正式采集之前抓
  fetch_metrics "$A_PUB" "http://$A_IN:8100/metrics" "metrics_prefill${ARM_SUFFIX:-}_t0.txt" || true
  fetch_metrics "$B_PUB" "http://$B_IN:8200/metrics" "metrics_decode${ARM_SUFFIX:-}_t0.txt" || true
  smoke_and_gate "http://127.0.0.1:8192" "disagg${ARM_SUFFIX:-}" \
    || { stop_retry "$A_PUB" "stop-all" "disagg_A" || true; stop_retry "$B_PUB" "stop-all" "disagg_B" || true; return 1; }
  # 拆分臂的 tokenize 走前处理端（代理没有 /tokenize；同模型分词器逐位相同）
  prefix_call calib 'wrote calib_' "calib_split${ARM_SUFFIX:-}.log" \
    --url "http://127.0.0.1:8192" --tokenize-url "http://$A_IN:8100" \
    --arm "split${ARM_SUFFIX:-}" --prompts-file "$PROMPTS_CAL" --max-new 40 --reps 10 \
    --reference-from "$RB" --out-dir "$RB" || return 1
  prefix_call collect 'target=' "collect_split${ARM_SUFFIX:-}.log" \
    --url "http://127.0.0.1:8192" --tokenize-url "http://$A_IN:8100" \
    --arm "split${ARM_SUFFIX:-}" --prompts-file "$PROMPTS_V2" --max-new 64 \
    --reference-from "$RB" --out-dir "$RB" || return 1
  local r
  for r in 1 2 3; do
    client_call "http://127.0.0.1:8192" "free_disagg${ARM_SUFFIX:-}_r$r.json" "free_disagg${ARM_SUFFIX:-}_r$r.log" || return 1
  done
  # KV 4b 证据的 t1 快照 + 增量报告（字节/次数/失败数）
  fetch_metrics "$A_PUB" "http://$A_IN:8100/metrics" "metrics_prefill${ARM_SUFFIX:-}_t1.txt" || true
  fetch_metrics "$B_PUB" "http://$B_IN:8200/metrics" "metrics_decode${ARM_SUFFIX:-}_t1.txt" || true
  python3 "$K8S/b1_metrics_delta.py" --t0 "$R/metrics_decode${ARM_SUFFIX:-}_t0.txt" \
    --t1 "$R/metrics_decode${ARM_SUFFIX:-}_t1.txt" --out "$R/kv_delta_decode${ARM_SUFFIX:-}.txt" \
    --label "D${ARM_SUFFIX:-}" | tee "$R/kv_delta_decode${ARM_SUFFIX:-}.log"
  rec metrics-disagg 0 ""
  stop_retry "$A_PUB" "stop-all" "disagg_A" || log "A 停止未确认，pull 兜底"
  stop_retry "$B_PUB" "stop-all" "disagg_B" || log "B 停止未确认，pull 兜底"
  rec stop-disagg 0 ""
  return 0
}

phase_i1() {
  log "=== 干预 I1：批不变性（两臂 VLLM_BATCH_INVARIANT=1）==="
  EXTRA_ENV="B1_BATCH_INVARIANT=1" ARM_SUFFIX="_i1"
  phase_baseline && phase_disagg
  local rc=$?
  EXTRA_ENV=""; ARM_SUFFIX=""
  return $rc
}

phase_i2() {
  log "=== 干预 I2：fp32（两臂 B1_DTYPE=float32）==="
  EXTRA_ENV="B1_DTYPE=float32" ARM_SUFFIX="_i2"
  phase_baseline && phase_disagg
  local rc=$?
  EXTRA_ENV=""; ARM_SUFFIX=""
  return $rc
}

phase_pull() {
  log "=== pull + analyze ==="
  local n
  for n in 1 2 3; do
    scp -q "${SSH_OPTS[@]}" "root@$A_PUB:$RB/*" "$R/" 2>/dev/null
    local cnt; cnt=$(ls "$R" | wc -l)
    log "拉回文件数=$cnt（第 $n 次）"
    [[ "$cnt" -gt 15 ]] && break
    sleep 3
  done
  scp -q "${SSH_OPTS[@]}" "root@$A_PUB:/root/b1/metrics_*.txt" "root@$B_PUB:/root/b1/metrics_*.txt" "$R/" 2>/dev/null
  rec pull 0 ""
  stop_retry "$A_PUB" "stop-all" "pull_A" || log "pull 兜底 stop-all @ A 未确认"
  stop_retry "$B_PUB" "stop-all" "pull_B" || log "pull 兜底 stop-all @ B 未确认"
  python3 "$K8S/b1_prefix_4c.py" analyze --in-dir "$R" --out-dir "$R/analysis" | tee "$R/analyze.log"
  rec analyze "$?" ""
  log "产物目录：$R"
  log "★ 收尾提醒：控制台用【节省停机模式】停两台（普通停机继续收钱）；"
  log "  节省停机会释放公网 IP（私网不变），下次开机会变，届时用 A_PUB=.. B_PUB=.. 重跑。"
  return 0
}

case "${1:-}" in
  "") echo "用法见文件头"; exit 2;;
esac
for ph in "$@"; do
  case "$ph" in
    check)    phase_check || exit 1;;
    sync)     phase_sync || exit 1;;
    baseline) phase_baseline || { log "!! baseline 失败——按纪律停止，不继续"; exit 1; };;
    disagg)   phase_disagg || { log "!! disagg 失败——按纪律停止，不继续"; exit 1; };;
    i1)       phase_i1 || { log "!! I1 失败（不阻塞基线结论，如实记录）"; rec i1-fail "$?" ""; };;
    i2)       phase_i2 || { log "!! I2 失败（不阻塞基线结论，如实记录）"; rec i2-fail "$?" ""; };;
    pull)     phase_pull || exit 1;;
    *) echo "未知阶段: $ph"; exit 2;;
  esac
done
log "全部指定阶段完成"

#!/usr/bin/env bash
# =============================================================================
# run_prereg3_local.sh —— B1_4C_PREREG_3 的**本机预演**（方案 C 的第一步）
#
# 目的：在花云钱之前，把「服务启停 + processed 分采集 + C1 冒烟 + E4/E1/E2/E3 分析」
#       整条链路在本机跑顺。**预演产物不是正式证据**（单卡双实例 ≠ 云端两台 A10）。
# 已知偏离（都会写进 deviations.txt，云端正式采数时不偏离）：
#   D1 显存预算：P/D 双实例塞不下 B1 的 0.85，逐实例下调（下限预案见 PD_TIERS）；
#   D2 若显存仍不够，P/D 的 --max-model-len 降到 2048（本机预演专用）；
#   D3 单卡双实例的 NIXL 走本机传输（UCX_NET_DEVICES=all），与云端跨机不同；
#   D4 side 信道端口两实例必须不同（5601/5602），云端是同号不同机。
# 纪律：失败不自动重跑；按 PID 停服务；产物 append-only。
# =============================================================================
set -u

SIM=/mnt/e/代码项目文件/论文/sim
RUNID="b1_4c3_local_$(date -u +%Y%m%dT%H%M%SZ)"
R="$SIM/results/b1/$RUNID"
SS="$SIM/k8s_test/b1_remote_serve.sh"
CL="$SIM/k8s_test/b1_client.py"
AN="$SIM/k8s_test/b1_4c3_analyze.py"
VLLM=/root/vllm-env/bin/vllm
PYX=/root/vllm-env/bin/python          # 代理需要 fastapi/httpx ⇒ 用 vllm-env 的解释器
PY3=python3                            # 客户端/分析只用标准库
MODEL=Qwen/Qwen2.5-1.5B-Instruct
NAME=Qwen/Qwen2.5-1.5B-Instruct
PROXY=/root/prereg3/toy_proxy_server.py
export HF_HOME=/root/hf-cache HF_HUB_OFFLINE=1 VLLM_USE_FLASHINFER_SAMPLER=0 TMPDIR=/root/tmp
export B1_LOGPROBS_MODE=processed_logprobs   # ★ 本方案的关键新增（预注册 §2）
export B1_BIND_IP=127.0.0.1
export B1_UCX_NET_DEVICES=all                # D3：同机传输
BASE_UTIL=0.85
PD_TIERS=("0.44 8192" "0.42 2048")           # D1/D2 预案：先按冻结值，塞不下再降

mkdir -p /root/tmp "$R" "$R/deviations"
cd "$SIM" || exit 1
log(){ echo "[$(date -u +%H:%M:%S)] $*"; }
rec(){ echo "$(date -u +%Y-%m-%dT%H:%M:%SZ),$1,$2,$3" >> "$R/record.csv"; }

wait_ready(){ # $1 url $2 name
  for i in $(seq 1 90); do
    c=$(curl -s -o /dev/null -w '%{http_code}' "$1" 2>/dev/null || true)
    [ "$c" = "200" ] && { log "$2 就绪（第 $i 次探测）"; return 0; }
    sleep 2
  done
  log "$2 180s 内未就绪"; return 1
}

run_arm(){ # $1 = baseline|disagg  $2 = url
  local arm=$1 url=$2
  log "[$arm] C1 冒烟（q01-q03）"
  $PY3 "$CL" --url "$url" --model "$NAME" --prompts-file "$R/prompts_v2.json" \
    --logprobs 20 --only-ids q01_rep6 q02_rep10 q03_rep14 \
    --out "$R/smoke_$arm.json" >"$R/smoke_$arm.log" 2>&1 || { rec "$arm-smoke" 1 "client fail"; return 1; }
  $PY3 "$AN" smoke --file "$R/smoke_$arm.json" | tee "$R/smoke_$arm.verdict"
  local smoke_rc=${PIPESTATUS[0]}
  rec "$arm-smoke" "$smoke_rc" "see smoke_$arm.verdict"
  [ "$smoke_rc" -eq 0 ] || { log "[$arm] C1 冒烟 FAIL ⇒ 按预注册 §8 不采数"; return 1; }
  for k in 1 2 3; do
    log "[$arm] 预热 r$k"
    $PY3 "$CL" --url "$url" --model "$NAME" --prompts-file "$R/prompts_v2.json" \
      --logprobs 20 --out "$R/warmup_${arm}_r$k.json" >"$R/warmup_${arm}_r$k.log" 2>&1
    rec "$arm-warmup-r$k" "$?" ""
  done
  for k in 1 2 3; do
    log "[$arm] 正式 r$k"
    $PY3 "$CL" --url "$url" --model "$NAME" --prompts-file "$R/prompts_v2.json" \
      --logprobs 20 --out "$R/arm_${arm}_r$k.json" >"$R/arm_${arm}_r$k.log" 2>&1
    rec "$arm-formal-r$k" "$?" ""
  done
  return 0
}

# ---------- 0) 前置检查 ----------
echo "RUNID=$RUNID"
echo "运行目录=$R"
$PYX -c "import fastapi, httpx" 2>/dev/null || { echo "!! vllm-env 缺 fastapi/httpx（代理依赖）"; exit 1; }
for p in 8100 8192 8200 8300 5601 5602; do
  ss -ltn | grep -q ":$p " && { echo "!! 端口 $p 被占用"; exit 1; }
done
nvidia-smi --query-gpu=memory.total,memory.used,memory.free --format=csv,noheader | tee "$R/gpu_before.txt"

# ---------- 1) 冻结输入 ----------
$PY3 "$CL" gen-prompts --preset v2 --out "$R/prompts_v2.json" >"$R/genprompts.log" 2>&1
sha256sum "$R/prompts_v2.json" | tee "$R/prompts_v2.sha256"
sha256sum "$PROXY" | tee "$R/toy_proxy.sha256"

# ---------- 2) 对照臂（单机完整服务，B1 同款参数）----------
log "=== 对照臂 baseline（util=$BASE_UTIL，冻结参数）==="
B1_GPU_UTIL=$BASE_UTIL bash "$SS" start-baseline 8300 "$VLLM" "$MODEL" "$NAME" \
  || { rec start-baseline 1 ""; exit 1; }
rec start-baseline 0 ""
wait_ready http://127.0.0.1:8300/health baseline || exit 1
run_arm baseline http://127.0.0.1:8300 || exit 1
bash "$SS" stop baseline
rec stop-baseline 0 ""

# ---------- 3) 拆分臂（P + D + 代理；显存预案两档）----------
PD_OK=0
for tier in "${PD_TIERS[@]}"; do
  set -- $tier; UTIL=$1; MML=$2
  log "=== 拆分臂尝试：util=$UTIL max-model-len=$MML ==="
  echo "tier util=$UTIL max_model_len=$MML" >> "$R/deviations/pd_attempts.txt"
  if [ "$MML" != "8192" ]; then
    echo "D2 偏离：P/D --max-model-len 8192 → $MML（本机预演专用）" >> "$R/deviations/deviations.txt"
  fi
  B1_GPU_UTIL=$UTIL bash "$SS" start-prefill 8100 5601 "$VLLM" "$MODEL" "$NAME" 127.0.0.1 \
    > "$R/start_prefill.log" 2>&1
  B1_GPU_UTIL=$UTIL bash "$SS" start-decode 8200 5602 "$VLLM" "$MODEL" "$NAME" 127.0.0.1 \
    > "$R/start_decode.log" 2>&1
  sleep 5
  if ! kill -0 "$(cat /root/b1/logs/prefill.pid 2>/dev/null)" 2>/dev/null \
     || ! kill -0 "$(cat /root/b1/logs/decode.pid 2>/dev/null)" 2>/dev/null; then
    log "P/D 有实例启动即退（大概率显存不够），收尾换下一档"
    tail -n 8 /root/b1/logs/prefill.log /root/b1/logs/decode.log
    bash "$SS" stop-all >> "$R/pd_stop_try.log" 2>&1
    sleep 5
    continue
  fi
  wait_ready http://127.0.0.1:8100/health prefill || { bash "$SS" stop-all; continue; }
  wait_ready http://127.0.0.1:8200/health decode || { bash "$SS" stop-all; continue; }
  bash "$SS" start-proxy 8192 127.0.0.1:8100 127.0.0.1:8200 "$PYX" "$PROXY" \
    > "$R/start_proxy.log" 2>&1
  sleep 3
  wait_ready http://127.0.0.1:8192/healthcheck proxy || { bash "$SS" stop-all; continue; }
  if run_arm disagg http://127.0.0.1:8192; then PD_OK=1
  else log "拆分臂采数失败（含 C1 冒烟不过）"; fi
  bash "$SS" stop-all >> "$R/pd_stop_final.log" 2>&1
  rec stop-disagg 0 "tier=$UTIL/$MML"
  break
done
[ "$PD_OK" = "1" ] || echo "D5 预演降级：拆分臂未能在本机跑通（见 deviations/）⇒ 分析只能覆盖对照臂链路" \
  >> "$R/deviations/deviations.txt"

# ---------- 4) 分析（E4/E1/E2/E3；口径冻结在预注册 §5）----------
log "=== 分析 ==="
$PY3 "$AN" analyze --run-dir "$R" --out-dir "$R" | tee "$R/analyze.log"

# ---------- 5) 收尾核对 ----------
echo "=== 收尾核对 ==="
ss -ltn | grep -E ':(8100|8192|8200|8300|5601|5602) ' && echo "!! 有端口残留" || echo "端口全部释放 ✓"
ps -eo pid,cmd | grep -E '[v]llm serve|[t]oy_proxy' && echo "!! 有进程残留" || echo "无残留进程 ✓"
nvidia-smi --query-gpu=memory.used --format=csv,noheader
echo "偏离清单：$R/deviations/deviations.txt（若有）"
echo "RUNID=$RUNID"

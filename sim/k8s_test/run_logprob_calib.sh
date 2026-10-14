#!/usr/bin/env bash
# 概率口径校准的编排脚本（LOGPROBS_CALIB_PREREG.md §5 的执行步骤）。
# 用法（WSL 内，root）：bash /mnt/e/代码项目文件/论文/sim/k8s_test/run_logprob_calib.sh
# 两臂只差 --logprobs-mode；其余与 B1 逐条相同。失败不自动重跑，保留现场。
set -u

SIM=/mnt/e/代码项目文件/论文/sim
RUNID="logprob_calib_$(date -u +%Y%m%dT%H%M%SZ)"
R="$SIM/results/logprob_calib/$RUNID"
MODEL=Qwen/Qwen2.5-1.5B-Instruct
REV=989aa7980e4cf806f80c7fef2b1adb7bc71aa306
SNAP=/root/hf-cache/hub/models--Qwen--Qwen2.5-1.5B-Instruct/snapshots/$REV
export HF_HOME=/root/hf-cache HF_HUB_OFFLINE=1 VLLM_USE_FLASHINFER_SAMPLER=0 TMPDIR=/root/tmp
mkdir -p /root/tmp "$R"
echo "RUNID=$RUNID"
echo "产物目录=$R"

cd "$SIM" || exit 1

# ---------- 第 0 步：冻结输入与生效设置 ----------
python3 k8s_test/b1_client.py gen-prompts --preset v1 --out "$R/prompts_v1.json" > "$R/genprompts.log" 2>&1
sha256sum "$R/prompts_v1.json" | tee "$R/prompts_v1.sha256"
cp "$SNAP/generation_config.json" "$R/generation_config.json"

if ss -ltn 2>/dev/null | grep -q ':8090'; then
  echo "!! 8090 已被占用，先清理再跑（不自动杀别人的进程）"; exit 1
fi

SERVER_PID=""
cleanup() {
  if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null
    echo "已按 PID 停服务：$SERVER_PID"
  fi
}
trap cleanup EXIT

start_server() {  # $1 = raw|processed
  local arm=$1
  /root/vllm-env/bin/vllm serve "$MODEL" \
    --host 127.0.0.1 --port 8090 \
    --max-model-len 8192 --gpu-memory-utilization 0.85 \
    --enforce-eager --no-enable-prefix-caching \
    --logprobs-mode "${arm}_logprobs" \
    > "$R/server_${arm}.log" 2>&1 &
  SERVER_PID=$!
  echo "$SERVER_PID" > "$R/server_${arm}.pid"
  for i in $(seq 1 90); do
    code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:8090/health" || true)
    if [ "$code" = "200" ]; then echo "[$arm] 服务就绪（PID $SERVER_PID，第 $i 次探测）"; return 0; fi
    sleep 2
  done
  echo "[$arm] 服务 180s 内未就绪，停。日志：$R/server_${arm}.log"; exit 1
}

save_settings() {  # $1 = raw|processed
  local arm=$1
  {
    echo "== generation_config.json（模型继承的选词规则）=="
    cat "$R/generation_config.json"
    echo
    echo "== 服务启动日志里的覆盖警告 =="
    grep -n "generation_config" "$R/server_${arm}.log" || echo "(没抓到警告行)"
    echo
    echo "== 请求显式参数（其余全部继承）=="
    echo "temperature=0.0, seed=0, max_tokens=64, return_token_ids=true, logprobs=20, stream=false"
    echo
    echo "== 启动参数 =="
    echo "--enforce-eager --no-enable-prefix-caching --max-model-len 8192 --gpu-memory-utilization 0.85 --logprobs-mode ${arm}_logprobs"
  } > "$R/settings_${arm}.txt"
}

# ---------- 第 1 步：raw 臂（等价于 B1）----------
echo "=== 臂 1/2：raw_logprobs ==="
start_server raw
python3 k8s_test/logprob_calib.py probe --arm raw \
  --prompts "$R/prompts_v1.json" --out-dir "$R" || { echo "!! raw 臂采数失败"; exit 1; }
save_settings raw
cleanup; SERVER_PID=""; sleep 5

# ---------- 第 2 步：processed 臂 ----------
echo "=== 臂 2/2：processed_logprobs ==="
start_server processed
python3 k8s_test/logprob_calib.py probe --arm processed \
  --prompts "$R/prompts_v1.json" --out-dir "$R" || { echo "!! processed 臂采数失败"; exit 1; }
save_settings processed
cleanup; SERVER_PID=""; sleep 3

# ---------- 第 3 步：判定（口径冻结在预注册 §4）----------
echo "=== 判定 ==="
python3 k8s_test/logprob_calib.py analyze --run-dir "$R" --out-dir "$R"

# ---------- 收尾核对 ----------
echo "=== 收尾核对 ==="
ss -ltn 2>/dev/null | grep -q ':8090' && echo "!! 8090 仍被占用" || echo "8090 已释放 ✓"
nvidia-smi --query-gpu=memory.used --format=csv,noheader
ls -la "$R"

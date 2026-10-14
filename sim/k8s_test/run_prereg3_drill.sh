#!/usr/bin/env bash
# =============================================================================
# run_prereg3_drill.sh —— PREREG_3 分析链路的**工具演练**（不是证据）
#
# 背景：本机 8 GB 单卡 + 无 nixl，真拆分拓扑预演不了（见 deviations D5）。
# 这里用「假拆分臂」把 b1_4c3_analyze.py 的 E4/E1/E2/E3 全链路跑通：
#   假臂 = 同一模型同一请求，但服务端 `--generation-config vllm`
#          （关掉 generation_config.json 继承的 repetition_penalty/top_k/top_p）
#   ⇒ 与规则全开的对照臂必然在部分步分歧 ⇒ 正好覆盖分析脚本的"分歧"分支。
# ⚠️ 产物只进 tooling_drill/ 子目录，**不得当作 PREREG_3 的任何证据**。
# =============================================================================
set -u
SIM=/mnt/e/代码项目文件/论文/sim
RUN="${1:?用法：run_prereg3_drill.sh <run目录名，如 b1_4c3_local_20261008T130152Z>}"
R="$SIM/results/b1/$RUN/tooling_drill"
SS="$SIM/k8s_test/b1_remote_serve.sh"
CL="$SIM/k8s_test/b1_client.py"
AN="$SIM/k8s_test/b1_4c3_analyze.py"
VLLM=/root/vllm-env/bin/vllm
MODEL=Qwen/Qwen2.5-1.5B-Instruct
export HF_HOME=/root/hf-cache HF_HUB_OFFLINE=1 VLLM_USE_FLASHINFER_SAMPLER=0 TMPDIR=/root/tmp
export B1_LOGPROBS_MODE=processed_logprobs

mkdir -p "$R"
cd "$SIM" || exit 1
BASE="$SIM/results/b1/$RUN"

# 对照臂文件是真货，拷进来当 drill 的 baseline 半边（原件不动）
for k in 1 2 3; do cp "$BASE/arm_baseline_r$k.json" "$R/arm_baseline_r$k.json"; done
cp "$BASE/prompts_v2.json" "$R/prompts_v2.json"
echo "D6 工具演练：假拆分臂 = --generation-config vllm（规则全关）；产物仅限 tooling_drill/，不是证据" \
  >> "$BASE/deviations/deviations.txt"

# 假臂服务：与对照臂同参数，唯一差别是 --generation-config vllm
nohup env HF_HOME=/root/hf-cache HF_HUB_OFFLINE=1 VLLM_USE_FLASHINFER_SAMPLER=0 \
  "$VLLM" serve "$MODEL" --host 127.0.0.1 --port 8300 \
  --served-model-name "$MODEL" --max-model-len 8192 --gpu-memory-utilization 0.85 \
  --enforce-eager --no-enable-prefix-caching --generation-config vllm \
  --logprobs-mode processed_logprobs \
  > "$R/server_fakedisagg.log" 2>&1 &
echo $! > "$R/server_fakedisagg.pid"
for i in $(seq 1 90); do
  c=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8300/health 2>/dev/null || true)
  [ "$c" = "200" ] && break; sleep 2
done
echo "假臂服务就绪（第 $i 次探测）"

for k in 1 2 3; do
  python3 "$CL" --url http://127.0.0.1:8300 --model "$MODEL" \
    --prompts-file "$R/prompts_v2.json" --logprobs 20 \
    --out "$R/arm_disagg_r$k.json" > "$R/arm_disagg_r$k.log" 2>&1
  echo "假臂 r$k exit=$?"
done

kill "$(cat "$R/server_fakedisagg.pid")" 2>/dev/null
sleep 3
bash "$SS" stop-all > "$R/stop.log" 2>&1

python3 "$AN" analyze --run-dir "$R" --out-dir "$R"
echo "演练完成：$R"

#!/usr/bin/env bash
# =============================================================================
# b1_remote_setup.sh —— B1 的**远端**环境准备（在 GPU 机器上以 root 运行）
#
# 由 run_b1.sh 通过 `ssh root@ip "bash -s -- <args>" < 本文件` 送过去执行，
# 所以这里**不要**假设自己躺在仓库里。
#
# 子命令：
#   install                   建 venv、装 vLLM 与 NIXL、落一份依赖锁定清单
#   model-files               取权威文件清单（名字 + 字节数），存成 model_files.txt
#   model-download <part>     下载模型文件；part ∈ {all, 12, 34, small}
#                             （拆开是因为这两台机器的**公网出口被限到 ≈10 Mbps**）
#   model-hash                逐文件 sha256 + 整目录总哈希（**排除 .cache**）
#   model-serve <dir> <port> [bind]
#   model-serve-stop <port>
#   model-pull-files <peer> <port> <name> <file...>   从对端按文件名逐个拉（走内网）
#   clock                     打一份时钟状态
# =============================================================================
set -uo pipefail

RDIR="/root/b1"
RVENV="${RDIR}/venv"
MODELS="${RDIR}/models"
PIP_INDEX="http://mirrors.cloud.aliyuncs.com/pypi/simple/"
PIP_HOST="mirrors.cloud.aliyuncs.com"
VLLM_VER="${VLLM_VER:-0.31.0}"
NIXL_VER="${NIXL_VER:-1.4.1}"
MODEL_ID="${MODEL_ID:-Qwen/Qwen2.5-1.5B-Instruct}"
HUB="${HF_ENDPOINT:-https://hf-mirror.com}"
NAME="$(basename "${MODEL_ID}")"
DIR="${MODELS}/${NAME}"
MANIFEST="${RDIR}/model_files.txt"

pipi() { "${RVENV}/bin/pip" install -i "${PIP_INDEX}" --trusted-host "${PIP_HOST}" "$@"; }
step() { echo; echo "==================== $* ===================="; }

# ----------------------------------------------------------------- 安装
cmd_install() {
  step "0) 装之前先看空间（本题约需 6–7 G：venv + torch/cu130 的 wheel 约 3 G，模型 2.9 G）"
  df -h /
  echo "--- 已占用的："
  du -sh /root/b1 2>/dev/null || true

  step "1) 建目录与 venv"
  mkdir -p "${RDIR}"/{logs,out,models,official}
  # 判据用 "bin/python **和** bin/pip 都在"：上一轮失败留下的**半成品 venv**
  # （有 bin/python，但没有 pip）也必须被识别成"不可用"并重建，否则会一直卡在
  # "No such file or directory" 上。
  if [[ ! -x "${RVENV}/bin/python" || ! -x "${RVENV}/bin/pip" ]]; then
    # ⚠️ 2026-10-07（第二轮新机器实测的坑）：这个镜像**没有 `python3.12-venv`**，
    #    于是 `python3 -m venv` 会在 ensurepip 那步失败：
    #      "ensurepip is not available ... apt install python3.12-venv"
    #    而且**它建了一半的目录会留下来**（32 KB 的空壳），让后面的 `${RVENV}/bin/pip`
    #    一直报 "No such file or directory"。
    #    修法：不装系统包（不做系统级改动），改用**镜像里已经有的** `/usr/bin/virtualenv`
    #    （dpkg 里是 `python3-virtualenv 20.25.0+ds-2`，本来就在）。
    rm -rf "${RVENV}"
    if python3 -m venv "${RVENV}" 2>/dev/null && [[ -x "${RVENV}/bin/pip" ]]; then
      echo "venv 由 python3 -m venv 建成"
    else
      echo "python3 -m venv 不可用（本镜像缺 ensurepip / python3.12-venv）→ 改用系统自带 virtualenv"
      rm -rf "${RVENV}"
      virtualenv -p python3 "${RVENV}"
    fi
  fi
  # 硬判据：venv 必须**真的**能用。没有这一条，venv 失败会一路静默走完、
  # 最后打印"装好了"而实际什么都没装（本轮第一次跑就是这么骗过去的）。
  if [[ ! -x "${RVENV}/bin/pip" ]]; then
    echo "FATAL: venv 建不起来，${RVENV}/bin/pip 不存在 —— 环境安装**失败**"
    exit 1
  fi
  "${RVENV}/bin/python" --version
  "${RVENV}/bin/pip" --version

  step "2) 升级 pip（走内网 aliyun 镜像：区域内流量免费且快）"
  pipi -U pip setuptools wheel 2>&1 | tail -5

  step "3) 装 vLLM ${VLLM_VER}"
  pipi "vllm==${VLLM_VER}" 2>&1 | tail -30

  step "4) 装 NIXL ${NIXL_VER}（KV 传输用的库；官方 kv_connectors.txt 就把版本钉在这里）"
  pipi "nixl==${NIXL_VER}" 2>&1 | tail -20

  step "5) 版本与依赖锁定"
  "${RVENV}/bin/vllm" --version || true
  "${RVENV}/bin/pip" freeze > "${RDIR}/requirements_locked.txt"
  echo "--- 关键包："
  grep -iE '^(vllm|torch|nixl|nixl-cu|transformers|flashinfer|ray|xformers|uvicorn|fastapi|httpx|huggingface)' \
      "${RDIR}/requirements_locked.txt" || true
  echo "--- nixl 组件："
  "${RVENV}/bin/python" - <<'PY' || true
try:
    import importlib.metadata as md
    print("nixl version:", md.version("nixl"))
    for p in ("nixl-cu12", "nixl-cu13"):
        try: print(p, md.version(p))
        except Exception: print(p, "absent")
except Exception as e:
    print("nixl 查询失败:", type(e).__name__, e)
PY

  step "6) torch 能不能看到卡"
  "${RVENV}/bin/python" - <<'PY' || true
import torch
print("torch", torch.__version__, "cuda", torch.version.cuda, "avail", torch.cuda.is_available())
if torch.cuda.is_available():
    print("device:", torch.cuda.get_device_name(0), "cap", torch.cuda.get_device_capability(0))
PY

  step "7) 装之后的磁盘"
  df -h /
  du -sh "${RVENV}" "${MODELS}" 2>/dev/null || true

  # ---- 收尾硬判据：三条都过才算"装好了" ----
  # 为什么必须有：上一轮 venv 失败时本脚本仍以 rc=0 结束，run_b1.sh 打印"A 装好了",
  # 直到 digests 阶段才发现 vllm 根本不在。**"静默成功"是最贵的失败**。
  step "8) 安装结果自检（不过就报错退出）"
  "${RVENV}/bin/vllm" --version || { echo "FATAL: vllm 不可执行 → 安装失败"; exit 1; }
  "${RVENV}/bin/python" -c "import nixl; print('nixl import OK')" || { echo "FATAL: nixl 不能导入 → 安装失败"; exit 1; }
  "${RVENV}/bin/python" -c "import torch; assert torch.cuda.is_available(), 'CUDA 不可用'" \
      || { echo "FATAL: torch 看不到 GPU → 安装失败"; exit 1; }
  echo "自检通过：vllm 可执行、nixl 可导入、torch 看得到 GPU ✓"
}

# ------------------------------------------------------- 模型文件清单（权威）
cmd_model_files() {
  step "取权威文件清单：${HUB}/api/models/${MODEL_ID}"
  mkdir -p "${MODELS}" "${RDIR}"
  curl -sS --max-time 60 "${HUB}/api/models/${MODEL_ID}?blobs=true" -o "${RDIR}/model_api.json"
  "${RVENV}/bin/python" - "$RDIR/model_api.json" "$MANIFEST" <<'PY'
import json, sys
api, out = sys.argv[1], sys.argv[2]
d = json.load(open(api, encoding="utf-8"))
sib = d.get("siblings") or []
rows = []
for s in sib:
    n = s.get("rfilename")
    sz = s.get("size")
    if n is None or sz is None:
        continue
    rows.append((n, int(sz), ((s.get("lfs") or {}) or {}).get("oid") or "-"))
rows.sort()
with open(out, "w", encoding="utf-8") as fh:
    for n, sz, oid in rows:
        fh.write(f"{n}\t{sz}\t{oid}\n")
print("repo sha:", d.get("sha"))
print(f"{len(rows)} files, total {sum(r[1] for r in rows)/2**30:.2f} GiB")
for n, sz, oid in rows:
    print(f"  {n:44s} {sz:>13,d}  lfs={oid[:12]}")
PY
}

# ----------------------------------------------------------------- 下载
# ⚠️ 2026-10-07 第二轮：模型换成 **Qwen2.5-1.5B-Instruct**（单个 `model.safetensors`，
# 3,087,467,144 B ≈ 2.88 GiB，**没有分片、没有 index.json**），所以原来的拆段方案
# （按 7B 的 4 个分片切 12 / 34 两半）**已不适用**：现在一个文件就是全部权重，
# 拆也无处可拆。这台的公网出口实测 hf-mirror 1.82 MB/s（A）/ 0.66 MB/s（B），
# 2.88 GiB 单流约 28 min（A）/ 78 min（B），直接整份下即可。
# 保留下面的 part 接口是为了**旧产物仍可复现**；1.5B 请用 `all` 或 `small`。
# 若日后换成多分片模型，按分片名重写下面几行即可。
part_patterns() {
  case "$1" in
    all)   echo "" ;;
    small) echo ".gitattributes LICENSE README.md config.json generation_config.json merges.txt tokenizer.json tokenizer_config.json vocab.json" ;;
    single) echo ".gitattributes LICENSE README.md config.json generation_config.json merges.txt tokenizer.json tokenizer_config.json vocab.json model.safetensors" ;;
    *) echo "未知 part: $1（多分片模型的 12/34 分段已随 7B 一起废弃）" >&2; return 1 ;;
  esac
}

cmd_model_download() {
  local part="${1:-all}"
  step "下载 part=${part} 到 ${DIR}"
  mkdir -p "${DIR}"
  [[ -f "${MANIFEST}" ]] || cmd_model_files
  df -h / | tail -1

  local want
  want="$(part_patterns "${part}")" || return 1

  local n_ok=0 n_skip=0 n_fail=0
  local report="${RDIR}/download_${part}_report.txt"
  : >"${report}"

  while IFS=$'\t' read -r fname fsize foid; do
    [[ -n "${want}" ]] && [[ " ${want} " != *" ${fname} "* ]] && continue
    local dest="${DIR}/${fname}"
    local url="${HUB}/${MODEL_ID}/resolve/main/${fname}"
    local have=0
    [[ -f "${dest}" ]] && have=$(stat -c %s "${dest}")
    if [[ "${have}" == "${fsize}" ]]; then
      echo "  跳过（已完成）${fname} ${fsize}"
      n_skip=$((n_skip + 1))
      echo -e "${fname}\t${fsize}\tskip\t${foid}" >>"${report}"
      continue
    fi
    echo "  下载 ${fname}  需要 ${fsize} 字节，已有 ${have}，断点续传"
    local t0 t1 rc
    t0=$(date +%s)
    curl -L -C - --retry 5 --retry-delay 3 --retry-all-errors \
         --max-time 21600 -o "${dest}" "${url}"
    rc=$?
    t1=$(date +%s)
    local got; got=$(stat -c %s "${dest}" 2>/dev/null || echo 0)
    if [[ "${got}" == "${fsize}" ]]; then
      echo "    OK ${fname} ${got} 字节，用时 $((t1 - t0)) s"
      n_ok=$((n_ok + 1))
      echo -e "${fname}\t${fsize}\tok\t${foid}" >>"${report}"
    else
      echo "    **失败** ${fname} curl_rc=${rc} 得到 ${got} / 期望 ${fsize}，用时 $((t1 - t0)) s"
      n_fail=$((n_fail + 1))
      echo -e "${fname}\t${fsize}\tFAIL_rc${rc}_got${got}\t${foid}" >>"${report}"
    fi
  done <"${MANIFEST}"

  step "下载小结 part=${part}：新下 ${n_ok} / 跳过 ${n_skip} / 失败 ${n_fail}"
  cat "${report}"
  df -h / | tail -1
  [[ "${n_fail}" -eq 0 ]]
}

# ----------------------------------------------------------------- 校验
hash_dir() {  # hash_dir <dir> —— **排除 .cache**（那是下载器的临时区，两端内容天然不同）
  local d="$1"
  find "${d}" -type f -not -path '*/.cache/*' | sort | sed "s|^${d}/||" >"${RDIR}/.hashlist"
  local total
  total=$(sha256sum "$(find "${d}" -type f -not -path '*/.cache/*' | sort)" | sha256sum | awk '{print $1}')
  echo "${total}"
}

cmd_model_hash() {
  step "模型目录哈希（排除 .cache）"
  [[ -d "${DIR}" ]] || { echo "MISSING ${DIR}"; return 1; }
  echo "--- 目录：${DIR}"
  echo "--- 文件数（不含 .cache）：$(find "${DIR}" -type f -not -path '*/.cache/*' | wc -l)"
  echo "--- 总字节（不含 .cache）：$(find "${DIR}" -type f -not -path '*/.cache/*' -printf '%s\n' | awk '{s+=$1} END{print s}')"
  echo "--- 逐文件 sha256 与大小"
  ( cd "${DIR}" && find . -type f -not -path './.cache/*' | sort | xargs sha256sum ) \
      | tee "${RDIR}/model_files.sha256" \
      | sed 's|\./||'
  echo "--- 整目录总哈希（对上面每行的 sha256 再取一次）"
  awk '{print $1}' "${RDIR}/model_files.sha256" | sort | sha256sum | tee "${RDIR}/model_dir_hash.txt"
  echo "--- 与实际字节数对账（应全部一致）"
  ( cd "${DIR}" && for f in $(find . -type f -not -path './.cache/*' | sort); do
      printf "%s\tsize=%s\tsha=%s\n" "$f" "$(stat -c %s "$f")" "$(sha256sum "$f" | cut -c1-16)"
    done )
}

# --------------------------------------------------- 内网 HTTP 文件服务/拉取
cmd_model_serve() {
  local dir="$1" port="$2" bind="${3:-0.0.0.0}"
  step "在 ${bind}:${port} 上用 HTTP 开模型目录 ${dir}（供对端经内网拉取）"
  ip -4 addr show | grep -E 'inet ' || true
  cd "${dir}" || exit 1
  nohup python3 -m http.server "${port}" --bind "${bind}" >"${RDIR}/logs/httpserver_${port}.log" 2>&1 </dev/null &
  echo $! >"${RDIR}/logs/httpserver_${port}.pid"
  sleep 2
  echo "pid=$(cat "${RDIR}/logs/httpserver_${port}.pid")"
  ss -ltn | grep ":${port}" || true
}

cmd_model_serve_stop() {
  local port="$1"
  step "停掉 HTTP 文件服务 ${port}"
  local pf="${RDIR}/logs/httpserver_${port}.pid"
  if [[ -f "$pf" ]]; then
    local pid; pid="$(cat "$pf")"
    kill -TERM "$pid" 2>/dev/null && echo "killed $pid"
    sleep 1
    kill -KILL "$pid" 2>/dev/null || true
    rm -f "$pf"
  fi
  ss -ltn | grep ":${port}" || echo "端口 ${port} 已释放"
}

cmd_model_pull_files() {
  local peer="$1" port="$2" name="$3"; shift 3
  step "从 ${peer}:${port} 逐个拉文件到 ${MODELS}/${name}"
  mkdir -p "${MODELS}/${name}"
  local t0 t1 n=0
  t0=$(date +%s)
  for f in "$@"; do
    local dest="${MODELS}/${name}/${f}"
    echo "  ← ${f}"
    curl -sS -L -C - --retry 3 --retry-all-errors --max-time 3600 \
         -o "${dest}" "http://${peer}:${port}/${name}/${f}" || echo "    (curl rc=$?)"
    echo "    $(stat -c %s "${dest}" 2>/dev/null || echo 0) 字节"
    n=$((n + 1))
  done
  t1=$(date +%s)
  echo "共 ${n} 个文件，用时 $((t1 - t0)) s"
}

cmd_clock() {
  step "时钟状态"
  timedatectl 2>/dev/null | head -10
  systemctl is-active chrony chronyd systemd-timesyncd 2>/dev/null || true
  chronyc tracking 2>/dev/null | head -8 || echo "（没有 chronyc）"
  date -u +"%Y-%m-%dT%H:%M:%S.%NZ"
}

case "${1:-}" in
  install)          cmd_install ;;
  model-files)      cmd_model_files ;;
  model-download)   shift; cmd_model_download "$@" ;;
  model-hash)       cmd_model_hash ;;
  model-serve)      shift; cmd_model_serve "$@" ;;
  model-serve-stop) shift; cmd_model_serve_stop "$@" ;;
  model-pull-files) shift; cmd_model_pull_files "$@" ;;
  clock)            cmd_clock ;;
  *) echo "未知子命令: ${1:-}"; exit 2 ;;
esac

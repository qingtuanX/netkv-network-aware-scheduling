#!/usr/bin/env bash
# 体检：3 个 worker Pod 是否真的按各自的"算力 / 带宽档位"在跑。
# 这是上真实集群做对照之前必须过的一道小门——如果三个 Pod 参数一样，
# 后面"网络感知有没有用"的对照就没有意义（会得到全 0 的差异）。
set -uo pipefail
K="${K:-k3s kubectl}"

echo "--- Pod 与位置（class 来自 Pod 标签 node_class）"
$K get pods -l role=worker -o \
    jsonpath='{range .items[*]}  {.metadata.name}  class={.metadata.labels.node_class}  ip={.status.podIP}  {.status.phase}{"\n"}{end}'

echo "--- 每个 worker 自报的参数（compute / bw_mbps 必须三个都不同）"
ok=0
for ip in $($K get pods -l role=worker -o jsonpath='{range .items[*]}{.status.podIP}{"\n"}{end}'); do
    body=$(curl -s --max-time 6 "http://${ip}:9101/state" 2>/dev/null)
    echo "  ${ip}: ${body:-（无响应）}"
    case "$body" in *'"compute"'*) ok=$((ok+1));; esac
done
echo "  可用的 worker 数：${ok}/3"
[ "$ok" -ge 3 ] || { echo "[失败] 不足 3 个 worker 可访问，先查 kubectl describe pod"; exit 1; }

echo "--- 连通性：从 WSL 到 Pod IP（走真实的内核网络栈）"
for ip in $($K get pods -l role=worker -o jsonpath='{range .items[*]}{.status.podIP}{"\n"}{end}'); do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 6 "http://${ip}:9101/health")
    echo "  http://${ip}:9101/health → ${code}"
done

echo "--- 集群信息（写论文 Implementation 小节要引用）"
$K version --short 2>/dev/null | head -3 || true
$K get nodes -o wide --no-headers | awk '{print "  node="$1"  kernel="$NF"  runtime="$(NF-1)}'
echo "  containerd: $($K get nodes -o jsonpath='{.items[0].status.nodeInfo.containerRuntimeVersion}')"
echo "  kernel:     $(uname -r)"
echo "  cgroup:     $([ -f /sys/fs/cgroup/cgroup.controllers ] && echo v2 || echo v1)"

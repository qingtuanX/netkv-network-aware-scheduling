#!/usr/bin/env bash
# 阶段 A 的"第二台主机"：WSL2 里的 network namespace + veth + 双向内核限速
#
# 为什么必须有这个脚本（而不是手敲命令）：
#   1. `真实跨云LLM服务研究计划书.md` §5 阶段 A 要求"用 tc 改变真实数据包速度"，
#      而**同一台机器上两个普通 IP 之间的流量走内核本地路径、根本不经过 veth**，
#      于是完全绕过 tc（MULTIHOST.md §1.2 第 1 条实测：设 25 Mbit 测出 18827 Mbit/s）。
#      这条路径必须每次都由脚本原样重建，否则"这次到底限没限速"无法复现。
#   2. WSL2 空闲自动关机会清掉 netns/veth/tc（§1.2 第 2 条），所以重建是常态。
#
# 用法（在 WSL 里以 root 跑；Windows 侧通过 wsl -d Ubuntu -u root -- bash <本脚本> 调用）：
#   bash setup_netns_tc.sh up            # 建 hostB + veth + 双向 tbf 25 Mbit
#   bash setup_netns_tc.sh up 100        # 换成 100 Mbit
#   bash setup_netns_tc.sh show          # 只看现状（qdisc 与统计）
#   bash setup_netns_tc.sh down          # 拆掉
#
# 拓扑（谁在哪一侧决定哪条方向被限速）：
#   root 命名空间   10.250.0.1  ── veth0 ─╮
#                                          ├─ 实验数据流只走这一段
#   hostB 命名空间  10.250.0.2  ── vethB ─╯
#   · root 的 veth0 出口 qdisc → 限制 root→hostB（＝"服务端下传"方向）
#   · hostB 的 vethB 出口 qdisc → 限制 hostB→root（＝"客户端上传/KV 传输"方向）
#   两侧都加，是为了让 measure_system.py transfer 的 A2 能同时判定两个方向。
#
# 【限速只匹配实验数据流】本脚本只动 veth0/vethB 这两块**新建的**虚拟网卡，
# 不碰 eth0、不碰 k3s 的 cni*/flannel 接口 —— 计划书 §5 阶段 A 明文要求不得把
# SSH、集群管理或状态采集流量一起限速。
set -euo pipefail

NS="${NS:-hostB}"
VETH_HOST="${VETH_HOST:-veth0}"
VETH_NS="${VETH_NS:-vethB}"
IP_HOST="${IP_HOST:-10.250.0.1}"
IP_NS="${IP_NS:-10.250.0.2}"
PREFIX="${PREFIX:-24}"
RATE_MBIT="${2:-25}"
BURST="${BURST:-64kb}"
LATENCY="${LATENCY:-50ms}"

die() { echo "[失败] $*" >&2; exit 1; }
need_root() { [ "$(id -u)" = "0" ] || die "必须以 root 运行（netns 与 tc 都要 CAP_NET_ADMIN）"; }

up() {
    need_root
    command -v tc >/dev/null 2>&1 || die "找不到 tc"
    command -v ip >/dev/null 2>&1 || die "找不到 ip"
    # 幂等：先清掉可能残留的同名对象（WSL 重启后通常已经没了，残留时这里兜底）
    ip netns del "$NS" 2>/dev/null || true
    ip link del "$VETH_HOST" 2>/dev/null || true

    ip netns add "$NS"
    ip link add "$VETH_HOST" type veth peer name "$VETH_NS"
    ip link set "$VETH_NS" netns "$NS"
    ip addr add "${IP_HOST}/${PREFIX}" dev "$VETH_HOST"
    ip link set "$VETH_HOST" up
    ip netns exec "$NS" ip addr add "${IP_NS}/${PREFIX}" dev "$VETH_NS"
    ip netns exec "$NS" ip link set "$VETH_NS" up
    ip netns exec "$NS" ip link set lo up

    # modprobe 报失败是正常的（tbf/netem 编译进内核了）。真正的判据是下面 qdisc 能不能加上。
    ip netns exec "$NS" modprobe sch_netem 2>/dev/null || true

    ip netns exec "$NS" tc qdisc add dev "$VETH_NS" root handle 10: \
        tbf rate "${RATE_MBIT}mbit" burst "$BURST" latency "$LATENCY"
    tc qdisc add dev "$VETH_HOST" root handle 10: \
        tbf rate "${RATE_MBIT}mbit" burst "$BURST" latency "$LATENCY"

    echo "[ok] ${NS}(${IP_NS}) <-> ${VETH_HOST}(${IP_HOST})  双向 tbf ${RATE_MBIT} Mbit/s"
    echo "     重建过 netns 之后 WSL 里其它会话持有的 fd 都会失效，属预期。"
    show
}

down() {
    need_root
    ip netns del "$NS" 2>/dev/null || true
    ip link del "$VETH_HOST" 2>/dev/null || true
    echo "[ok] 已拆除 ${NS}/${VETH_HOST}"
}

show() {
    echo "--- ip netns ---"; ip netns list 2>/dev/null || true
    if ip netns list 2>/dev/null | grep -qw "$NS"; then
        echo "--- hostB 侧 vethB ---"
        ip netns exec "$NS" tc -s qdisc show dev "$VETH_NS" || true
        echo "--- root 侧 ${VETH_HOST} ---"
        tc -s qdisc show dev "$VETH_HOST" || true
        echo "--- 连通性 ---"
        ip netns exec "$NS" ping -c 2 -W 1 "$IP_HOST" || true
    else
        echo "[提示] netns ${NS} 不存在。WSL2 空闲会自动关机并清掉 netns/veth/tc，重跑 'up' 即可。"
    fi
}

case "${1:-}" in
    up)   up ;;
    down) down ;;
    show) show ;;
    *) echo "用法：$0 {up [速率Mbit]|down|show}"; exit 2 ;;
esac

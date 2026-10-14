#!/usr/bin/env bash
# 在 WSL2 的 Ubuntu 里装一个单节点 k3s，并把本项目的实测镜像**就地构建**出来。
#
# 为什么不用 Docker Desktop / kind / 官方文档的默认流程：
#   1. 本机 `registry-1.docker.io` 与 `auth.docker.io` **都不通**（实测 000 超时），
#      `download.docker.com` 也不通。所以任何"从 Docker Hub 拉基础镜像"的路线都走不通。
#   2. 因此本脚本从 **Ubuntu 的 apt 源**（archive.ubuntu.com / mirrors.aliyun.com，均实测可达）
#      用 debootstrap 造一个最小 rootfs，再用 podman 把它变成镜像导入 k3s 的 containerd。
#      全过程不需要任何一个公共镜像仓库。
#   3. k3s 本体**不走官方 install.sh**（它把版本号编成 `v1.31.5%2Bk3s1`，而镜像站路径是
#      `v1.31.5-k3s1`，实测 `[ERROR] Download failed`），改成直接从可用源下二进制：
#      rancher-mirror.rancher.cn（时通时断）与 gh-proxy.com 反代 GitHub release（实测可用），
#      并用**两个独立来源的 sha256 交叉一致**后再校验二进制。
#
# 用法（Windows 侧，管理员 PowerShell 或 Git Bash）：
#   wsl -d Ubuntu -u root -- bash /mnt/e/代码项目文件/论文/sim/k8s_test/setup_wsl_k3s.sh
# 说明：脚本幂等，可以重复跑；每步失败都会停下来并打印"下一步怎么办"。
#
# 产物：
#   /usr/local/bin/k3s                       k3s 二进制（同时提供 kubectl/crictl/ctr 软链）
#   /etc/rancher/k3s/config.yaml             k3s 启动参数（禁用用不到的组件，避免拉镜像）
#   simtest/base:1                          实测镜像（Ubuntu rootfs + python3 + measure_system.py）
#   /tmp/k3s-report.txt                     环境能力报告（内核/cgroup/tc/CPU 等，写论文要引用）
set -euo pipefail

K3S_VERSION="${K3S_VERSION:-v1.31.5+k3s1}"
K3S_MIRROR="${K3S_MIRROR:-https://rancher-mirror.rancher.cn/k3s}"
UBUNTU_RELEASE="${UBUNTU_RELEASE:-noble}"          # 24.04 LTS
UBUNTU_MIRROR="${UBUNTU_MIRROR:-http://archive.ubuntu.com/ubuntu}"
# 【为什么要写全 docker.io 前缀】不写的话 podman 会存成 `localhost/simtest/base:1`，
# 而 kubelet 解析 `simtest/base:1` 时按默认规则补成 `docker.io/simtest/base:1`，
# 两边字符串不一致 → `ImagePullBackOff`（imagePullPolicy=Never 也救不了）。
# k3s 的 `pause-image` 用的正是同一个字符串，所以这里必须是全限定名。
IMG="docker.io/simtest/base:1"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_SRC="${SCRIPT_DIR}/measure_system.py"
REPORT=/tmp/k3s-report.txt

say()  { printf '\n=== %s ===\n' "$*"; }
warn() { printf '\n[警告] %s\n' "$*" >&2; }
die()  { printf '\n[失败] %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = "0" ] || die "必须以 root 运行（wsl -d Ubuntu -u root -- bash ...）"
[ -f "$APP_SRC" ] || die "找不到 ${APP_SRC}（脚本要和 measure_system.py 放在同一目录）"

# ---------------------------------------------------------------------------
# 0. 环境能力报告：这些都写进论文的 "Implementation" 小节
# ---------------------------------------------------------------------------
say "0. 环境能力报告"
{
    echo "日期(UTC)      : $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "内核           : $(uname -srmo)"
    echo "发行版         : $(. /etc/os-release && echo "$PRETTY_NAME")"
    echo "WSL            : $(cat /proc/version | grep -o 'microsoft.*' | head -1 || true)"
    echo "逻辑 CPU       : $(nproc)"
    echo "内存           : $(free -h | awk '/^Mem:/{print $2}')"
    echo "cgroup 版本    : $( [ -f /sys/fs/cgroup/cgroup.controllers ] && echo v2 || echo v1 )"
    echo "systemd        : $( [ -d /run/systemd/system ] && echo yes || echo no )"
    echo "nftables 模块  : $( (nft --version 2>/dev/null || echo 'nft 未安装') )"
    echo "iproute2/tc    : $( (tc -V 2>/dev/null || echo '未安装') )"
    echo "tc qdisc 可用  : $( (tc qdisc add dev lo root netem delay 1ms 2>/dev/null && echo yes && tc qdisc del dev lo root) || echo 'no（内核无 netem 模块，退回应用层模拟网络）' )"
} | tee "$REPORT"

if [ ! -d /run/systemd/system ]; then
    warn "WSL 里没启用 systemd。k3s 在没有 systemd 时对 cgroup 的处理会退化成 cgroupfs，"
    warn "而且需要手动 nohup 起进程。建议先在 Windows 侧做一次："
    warn "  printf '[boot]\\nsystemd=true\\n' > /etc/wsl.conf      # 在 WSL 里执行"
    warn "  wsl --shutdown                                        # 在 Windows 侧执行"
    warn "然后重新跑本脚本。"
fi

# ---------------------------------------------------------------------------
# 1. 依赖（全部来自 apt 源）
# ---------------------------------------------------------------------------
say "1. 安装依赖（curl ca-certificates iproute2 util-linux jq podman debootstrap）"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
    curl ca-certificates iproute2 util-linux jq podman debootstrap >/dev/null
echo "ok: $(which podman debootstrap | tr '\n' ' ')"

# ---------------------------------------------------------------------------
# 2. 构建实测镜像：debootstrap 造 rootfs → podman 变成镜像 → 导入 k3s
#    这一步完全绕开 Docker Hub（本机不通）。
# ---------------------------------------------------------------------------
say "2. 构建镜像 ${IMG}"
RT=/tmp/simtest-rootfs
if [ ! -x "${RT}/usr/bin/python3" ]; then
    rm -rf "$RT"
    debootstrap --variant=minbase --components=main \
        --include=ca-certificates,python3 "${UBUNTU_RELEASE}" "$RT" "$UBUNTU_MIRROR"
else
    echo "复用已有 rootfs：$RT"
fi
install -m 0755 "$APP_SRC" "${RT}/opt/measure_system.py"
printf '#!/bin/sh\nexec python3 /opt/measure_system.py "$@"\n' > "${RT}/usr/local/bin/simtest"
chmod 0755 "${RT}/usr/local/bin/simtest"

# 容器里要有个"永远活着"的 PID 1 兜底（k3s 的 sandbox 也用它）
TAR=/tmp/simtest-rootfs.tar
tar -C "$RT" -cf "$TAR" .
# 【两个 --change 都不能少】podman import 造出来的镜像**没有 ENV PATH**，
# 于是 runc 按空 PATH 去找可执行文件，sandbox（pause）启动直接报
# `exec: "sleep": executable file not found in $PATH`（已踩过一次，Pod 卡在 ContainerCreating）。
#   · ENV PATH  → 让 Pod 里写 `python3` 也能解析（k8s 不会给容器注入 PATH）
#   · CMD 用绝对路径 → 连 PATH 都不依赖，作为双保险
podman import \
    --change 'ENV PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin' \
    --change 'CMD ["/bin/sleep", "infinity"]' "$TAR" "$IMG" >/dev/null
echo "ok: $(podman images --format '{{.Repository}}:{{.Tag}} {{.Size}}' | grep simtest || true)"

# 导成 docker-archive 再喂给 k3s 的 containerd（k8s.io 命名空间）
OCI=/tmp/simtest-image.tar
# docker-archive 不允许覆盖已存在的文件（会报 "doesn't support modifying existing images"），
# 而本脚本要幂等可重跑 → 先删掉旧归档。
rm -f "$OCI"
podman save --format docker-archive -o "$OCI" "$IMG" >/dev/null
k3s ctr images import "$OCI" 2>/dev/null || true   # k3s 还没装时先跳过，第 4 步会再导一次

# ---------------------------------------------------------------------------
# 3. 安装 k3s 本体（从 rancher 中国镜像取，避开 github.com）
# ---------------------------------------------------------------------------
say "3. 安装 k3s ${K3S_VERSION}（直接下二进制 + 自己校验 sha256）"
# 【为什么不走官方 install.sh】它是正确的做法，但本机环境下它下不来：脚本把版本号
# 编成 `v1.31.5%2Bk3s1`（`+`→`%2B`），而 rancher 镜像站的路径用的是 `v1.31.5-k3s1`（`-`），
# 于是 `[ERROR] Download failed`（已实测）。而且 rancher-mirror.rancher.cn 的 DNS
# 时通时断（第二次实跑时直接解析超时）。所以这里改成：**直接从可用源下二进制**，
# 并用 sha256sum 校验——比"信任一个脚本"更可复现，也更能看出是哪一步坏了。
#
# 哈希来自两个**独立**来源（rancher 镜像 + GitHub 代理），两者一致才继续：
# 同一个源既给文件又给哈希只能防传输损坏，防不了"源本身给错"。
# 同一个版本号在三个地方写法不同，别复制粘贴错：git tag 用 `+`、镜像路径用 `-`、URL 里用 `%2B`
K3S_TAG="${K3S_VERSION//+/-}"                       # v1.31.5+k3s1 → v1.31.5-k3s1
GH_TAG="${K3S_VERSION/+/%2B}"                       # v1.31.5+k3s1 → v1.31.5%2Bk3s1
GH_URL="https://gh-proxy.com/https://github.com/k3s-io/k3s/releases/download/${GH_TAG}"

if [ -x /usr/local/bin/k3s ]; then
    echo "已有 /usr/local/bin/k3s，跳过安装"
else
    # $1=源基址 → 打印 k3s 那一行的 sha256（失败则输出空）。
    # 【末尾的 `|| true` 是必须的】脚本开了 `set -euo pipefail`：源不可达时 curl 非零，
    # pipeline 状态非零，`H1=$(fetch_hash ...)` 这个赋值就会失败并让**整个脚本静默退出**
    # （没有报错、没有提示，只停在上一行 echo 上）——已真实踩过一次。
    fetch_hash() {
        curl -sfL --retry 2 --max-time 60 -A "Mozilla/5.0" "$1/sha256sum-amd64.txt" 2>/dev/null \
            | awk '/ k3s$/{print $1; exit}' || true
    }
    H1=$(fetch_hash "${K3S_MIRROR}/${K3S_TAG}")      # rancher 镜像（可能 DNS 挂）
    H2=$(fetch_hash "$GH_URL")                        # GitHub 代理
    echo "哈希来源：rancher=[${H1:0:12}] ghproxy=[${H2:0:12}]"
    if [ -n "$H1" ] && [ -n "$H2" ] && [ "$H1" != "$H2" ]; then
        die "两个来源的 sha256 不一致（可能被篡改），停止安装"
    fi
    WANT="${H1:-$H2}"
    [ -n "$WANT" ] || die "两个来源都拿不到 sha256，无法校验，停止安装"

    for base in "${K3S_MIRROR}/${K3S_TAG}" "$GH_URL"; do
        echo "尝试下载：$base/k3s"
        if curl -fL --retry 3 -C - --max-time 900 -A "Mozilla/5.0" \
                -o /usr/local/bin/k3s "$base/k3s"; then
            if echo "$WANT  /usr/local/bin/k3s" | sha256sum -c - ; then break; fi
            warn "校验失败，换下一个源重下"
            rm -f /usr/local/bin/k3s
        fi
    done
    # 注意判的是 `-s`（非空）不是 `-x`：curl -o 写出来的文件默认没有可执行位，
    # 先判 `-x` 会在 `chmod` 之前就把成功下载的结果当成失败（已踩过一次）。
    [ -s /usr/local/bin/k3s ] || die "k3s 二进制没下成。可手工：curl -fL -C - -o /usr/local/bin/k3s $GH_URL/k3s"
    chmod 0755 /usr/local/bin/k3s
    for c in kubectl crictl ctr; do ln -sf k3s "/usr/local/bin/$c"; done
fi
/usr/local/bin/k3s --version

# 【为什么手写 systemd 单元】官方 install.sh 会顺便写 /etc/systemd/system/k3s.service，
# 而本脚本绕过了它（见第 3 步的原因），于是 `systemctl enable --now k3s.service` 会报
# "Unit k3s.service does not exist" 并中断（已踩过）。这个单元就是官方那一份，
# 只把 ExecStart 固定成 `/usr/local/bin/k3s server`（参数全部走 config.yaml）。
if [ ! -f /etc/systemd/system/k3s.service ]; then
    cat > /etc/systemd/system/k3s.service <<'UNIT'
[Unit]
Description=Lightweight Kubernetes
Documentation=https://k3s.io
Wants=network-online.target
After=network-online.target

[Install]
WantedBy=multi-user.target

[Service]
Type=notify
EnvironmentFile=-/etc/default/%N
EnvironmentFile=-/etc/sysconfig/%N
EnvironmentFile=-/etc/systemd/system/k3s.service.env
KillMode=process
Delegate=yes
LimitNOFILE=1048576
LimitNPROC=infinity
LimitCORE=infinity
TasksMax=infinity
TimeoutStartSec=0
Restart=always
RestartSec=5s
ExecStart=/usr/local/bin/k3s server
UNIT
    systemctl daemon-reload
    echo "ok: 写入 /etc/systemd/system/k3s.service"
fi

# ---------------------------------------------------------------------------
# 4. k3s 启动参数：把"要拉镜像才有"的组件全关掉
#    保留的必须项：pause 镜像 → 用我们自己构建的（每个 Pod sandbox 都要它）
#    关掉的：traefik / servicelb / metrics-server / local-storage / coredns
#      —— 都要从 Docker Hub 拉镜像，本机不通。Pod 之间直接用 Pod IP 通信
#      （客户端在 WSL 里跑，见 run_k3s_grid.sh），所以不需要集群 DNS。
# ---------------------------------------------------------------------------
say "4. 写 /etc/rancher/k3s/config.yaml"
mkdir -p /etc/rancher/k3s /var/lib/rancher/k3s/server/manifests
cat > /etc/rancher/k3s/config.yaml <<YAML
# 由 sim/k8s_test/setup_wsl_k3s.sh 生成。改这里而不是改 systemd unit。
disable:
  - traefik
  - servicelb
  - metrics-server
  - local-storage
  - coredns
pause-image: ${IMG}
write-kubeconfig-mode: "0644"
YAML
cat /etc/rancher/k3s/config.yaml
# 【为什么镜像导入不在这里做】`k3s ctr` 走的是 /run/k3s/containerd/containerd.sock，
# 而那个 socket 是 k3s 起来之后才有的；在这里导入会报
# `dial unix:///run/k3s/containerd/containerd.sock: timeout` 并让脚本中断（已踩过）。
# 所以顺序是：先启动 k3s → 等节点 Ready → 再导入镜像（第 5 步末尾）。

# ---------------------------------------------------------------------------
# 5. 启动
# ---------------------------------------------------------------------------
say "5. 启动 k3s"
if [ -d /run/systemd/system ]; then
    # 启动失败不要立刻中断：先让它把日志吐出来，才好判断是哪一类问题
    # （cgroup / 网络 / 权限）。真正的判定放在下面的"等节点 Ready"。
    systemctl enable --now k3s.service || warn "systemctl 启动返回非零，继续看日志"
    sleep 5
    systemctl --no-pager --lines=20 status k3s.service || true
    journalctl -u k3s --no-pager -n 15 2>/dev/null || true
else
    warn "无 systemd：用 nohup 手工启动，日志在 /var/log/k3s.log"
    pgrep -x k3s >/dev/null || nohup k3s server >>/var/log/k3s.log 2>&1 &
    sleep 20
fi

echo "--- 等节点 Ready（最多 180 s）"
for i in $(seq 1 36); do
    if k3s kubectl get nodes 2>/dev/null | grep -q ' Ready'; then break; fi
    sleep 5
done
k3s kubectl get nodes -o wide || true
k3s kubectl get pods -A || true

# --- 5b. 现在 containerd 起来了，把自建镜像导进 k8s.io 命名空间 ---
# 镜像必须先存在，否则 Pod 的 sandbox 创建会失败（imagePullPolicy: Never 也救不了）。
say "5b. 导入镜像 ${IMG}"
k3s ctr images import "$OCI"
k3s ctr images ls | grep -i simtest || die "镜像没进 containerd：手工执行 k3s ctr images import ${OCI}"

say "完成"
cat <<EOF
下一步：
  # 1) 起 3 个 worker Pod（异构算力与带宽）
  k3s kubectl apply -f ${SCRIPT_DIR}/k8s/workers.yaml
  k3s kubectl get pods -o wide -w

  # 2) 跑对照网格（client 在 WSL 里，用 Pod IP 连 worker）
  bash ${SCRIPT_DIR}/run_k3s_grid.sh

  环境能力报告（写论文 Implementation 小节要引用）：${REPORT}
EOF

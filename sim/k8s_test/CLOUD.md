# 路线 C：云上 3 台 VM 组 k3s —— 可执行计划

> **目标**：把真机实测从"单机 WSL 容器 + 应用层限速"升级为**三台真实主机 + 内核级限速**，
> 去掉论文里两条硬伤中的一条（"单机"），并**新增**仿真给不了的证据（共享链路拥塞、跨机状态年龄）。
>
> **本文件是计划，不是脚本**。每一步都给了"验证点"，**没通过验证点就不要往下走**——
> 这类实验最常见的失败不是跑不起来，而是"以为生效了其实没生效"，最后写进论文的是假证据。

---

## 0. 三个决定成本与证据强度的设计选择（先读这段）

### ① 把 3 台放进**同一个 VPC、同一个交换机**，实验流量走**内网**

这是最省钱也最关键的一条：

- **内网流量在国内云上通常免费，且实例内网带宽远高于你买的公网带宽**（常 1 Gbps 起）；
- 于是**公网带宽只需要够 SSH**（1–5 Mbps 按量就够，花费几乎可以忽略）；
- "异构链路"用 `tc` 在**内网网卡**上做出来（100 / 500 / 1000 Mbit/s），而不是靠买不同带宽的实例。

> 反过来做（给每台买不同公网带宽、靠买带宽造异构）会贵几十倍，而且跨公网时延不可控。

### ② 用 `tc` 造异构，而不是买不同规格

三台实例**买一样的规格**（同样的 CPU/内存），"算力异构 4:2:1"用 Pod 的 `limits.cpu` 控制，
"链路异构"用 `tc` 控制。理由：可复现、可控、便宜；而且与仿真里的建模方式一致，便于对照。

### ③ 镜像不走仓库，**用 `scp` 直接拷到每台**

现在的 worker 镜像（`docker.io/simtest/base:1`）是本地 `debootstrap` + `podman` 造的，
导入 k3s 的 containerd。多机时有两个选择：

- **不要**为此搭私有 registry（要额外带宽、要证书、要维护）；
- **要**在 VM1 构建一次 → `podman save` 成 tar → `scp` 到 VM2/VM3 → 各自 `k3s ctr images import`。
  镜像约几十 MB，内网传输几秒钟。

---

## 1. 钱：怎么算、怎么不失控（**先做这一步**）

### 1.1 预估

| 项 | 量级 | 说明 |
|---|---|---|
| 按量计费实例 ×3 | **每小时几毛～一两块/台** | 规格：2 vCPU / 4 GB（够跑三个 2 核 Pod 的宿主机） |
| 公网带宽 | 1–5 Mbps 按量 | 只给 SSH，**按流量计费**，几毛 |
| 内网流量 | **通常免费** | 实验流量全在内网 |
| 磁盘 | 40 GB 高效云盘 ×3 | 含在实例价里 |

**总预算：跑一整天约 ¥20–50 量级**（**以控制台实时价为准**，各云各价、各时段不同）。

### 1.2 五条花钱纪律（**必须做，否则会莫名其妙扣费**）

1. **只选"按量付费"**，**不要**选包年包月（哪怕它看起来单价便宜）；
2. **不用就给机器"停机"不够——要"释放"**。停机通常仍收云盘费；
3. 开**预算告警**（控制台里设一个"每天超过 ¥20 提醒我"）；
4. **不要买"公网 IP 固定带宽"**，用"按使用流量"；
5. 实验做完当天**立刻释放实例**，并用控制台确认"实例列表为空"。

> 我是 AI，**没有你的云账号、也不该替你花钱**。这一节是清单，**开机器和付钱由你操作**。

---

## 2. 阶段一：开三台机器（约 1 小时）

**控制台操作顺序**（各云大同小异）：

1. 选**同一个地域 + 同一个可用区**（例如"华东1-可用区B"）；
2. 建一个 **VPC + 一个交换机（vSwitch）**，三台都放进去；
3. 三个实例，规格一样（2 vCPU / 4 GB），镜像 **Ubuntu 22.04 LTS 64 位**；
4. 网络：**内网 IP 自动分配**；公网选择"按使用流量"，带宽上限 5 Mbps（只给 SSH）；
5. 登录方式：**密钥对**（比密码安全，也避免被爆破）；
6. 安全组（**这一条最容易漏，漏了 k3s 组不起来**）：

| 方向 | 协议 | 端口 | 来源 | 用途 |
|---|---|---|---|---|
| 入站 | TCP | 22 | **你的公网 IP/32** | SSH（**不要开 0.0.0.0/0**） |
| 入站 | TCP | 6443 | 同 VPC 网段 | k3s API |
| 入站 | TCP | 10250 | 同 VPC 网段 | kubelet |
| 入站 | **UDP** | 8472 | 同 VPC 网段 | flannel VXLAN（**漏了 Pod 跨机不通**） |
| 入站 | TCP | 2379–2380 | 同 VPC 网段 | 仅 server 需要（可选） |
| 入站 | ICMP | — | 同 VPC 网段 | 排查用（可选） |

**验证点 1**（三条都要过）：
```bash
# 从你本地 SSH 进第一台，然后：
ssh <VM2 内网IP> 'hostname'        # 三台互 ping/ssh 通（可用密钥互信或私钥转发）
ping -c 2 <VM2 内网IP>             # RTT 通常 < 1 ms（同可用区）
for h in vm1 vm2 vm3; do ssh $h 'chronyc tracking | grep -i "system time"'; done
# → 三台时钟偏移都要 < 10 ms。不过就装 chrony：apt install -y chrony && systemctl enable --now chrony
```

> **为什么时钟这么重要**：论文里要报"调度器看到的状态有多旧"（`obs_age_s`），
> 跨机时它是**两台机器的时钟相减**。不同步 → 这个数直接是垃圾 → 基于它的结论全部作废。

---

## 3. 阶段二：组 k3s 集群（约 1–2 小时）

```bash
# —— 三台都先做 ——
sudo swapoff -a && sudo sed -i '/ swap / s/^/#/' /etc/fstab
# 关 swap 是 k3s 的硬要求，不关会起不来

# —— VM1：server ——
curl -sfL https://rancher-mirror.rancher.cn/k3s/k3s-install.sh | \
  INSTALL_K3S_MIRROR=cn sh -s - server \
  --disable traefik --disable servicelb --disable metrics-server --disable coredns \
  --node-ip <VM1 内网IP>
# 注意：这里沿用你现有配置禁用了 coredns（因为拉不到镜像）——Pod 之间用 IP 直连，
# 与你 WSL2 里已经跑通的做法一致，客户端脚本不用改。

sudo cat /var/lib/rancher/k3s/server/node-token      # 记下 token

# —— VM2、VM3：agent ——
curl -sfL https://rancher-mirror.rancher.cn/k3s/k3s-install.sh | \
  INSTALL_K3S_MIRROR=cn sh -s - agent \
  --server https://<VM1 内网IP>:6443 --token <token> --node-ip <本机内网IP>
```

**验证点 2**：
```bash
kubectl get nodes -o wide
# → 三台都 Ready，且 INTERNAL-IP 是各自内网地址
kubectl get pods -A
# → 没有 CrashLoopBackOff
```

> **常见失败**：`8472/udp` 没放行 → 节点 Ready 但 Pod 跨机不通；
> `--node-ip` 没指定 → k3s 选了错误的网卡，节点间用错地址。

---

## 4. 阶段三：构建并分发 worker 镜像（约 1–2 小时）

在 **VM1** 上构建（复用你现有脚本里那段构建逻辑）：

```bash
sudo apt update && sudo apt install -y curl ca-certificates iproute2 util-linux jq podman debootstrap
# 用你仓库里已有的构建片段（debootstrap 造 rootfs → podman import → 打成 tar）
#   ★ 不要在这里跑 setup_wsl_k3s.sh 的"装 k3s"部分（集群已经组好了）
# 产物：一个 docker-archive tar，例如 /tmp/simtest-base.tar
```

分发与导入（**每台都要**，因为 k3s 用各自的 containerd）：

```bash
scp /tmp/simtest-base.tar vm2:/tmp/ && scp /tmp/simtest-base.tar vm3:/tmp/
for h in vm1 vm2 vm3; do ssh $h 'sudo k3s ctr images import /tmp/simtest-base.tar'; done
for h in vm1 vm2 vm3; do ssh $h 'sudo k3s ctr images ls | grep simtest'; done
```

**验证点 3**：三台都能 `k3s ctr images ls` 看到 `docker.io/simtest/base:1`。

部署 worker：**一个节点一个 Pod**，用 `nodeSelector` 钉死（否则三台可能挤在一台上，
"异构"就无从谈起）：
```yaml
# k8s/workers.yaml 里给三个 Deployment 各加：
      nodeSelector:
        kubernetes.io/hostname: <对应节点名>
```

**验证点 4**：
```bash
kubectl get pods -o wide
# → 三个 worker Pod 分别在 vm1/vm2/vm3 上
kubectl exec deploy/worker-a -- /bin/sh -c 'echo ok'      # 容器能进
# 从集群外 curl 每个 Pod 的 IP:9101，都应返回
```

---

## 5. 阶段四：`tc` 造链路异构（30 分钟）

**在每台机器上按角色限内网网卡**（先确认网卡名，通常是 `eth0` 或 `ens5`）：

```bash
ip -br addr        # 找到承载内网 IP 的那块网卡名
# 三台分别执行（角色对应仿真里的 100/500/1000 Mbps）
sudo tc qdisc add dev <网卡> root handle 1: tbf rate 100mbit  burst 128kb latency 100ms
sudo tc qdisc add dev <网卡> root handle 1: tbf rate 500mbit  burst 256kb latency 50ms
sudo tc qdisc add dev <网卡> root handle 1: tbf rate 1000mbit burst 512kb latency 20ms
```

**验证点 5（最关键的一条，别跳）**：

```bash
# 用 iperf3 实测吞吐，必须 ≈ 设定值（±10%）
sudo apt install -y iperf3
# VM2 上： iperf3 -s
# VM1 上： iperf3 -c <VM2 内网IP> -t 5
# → 若 VM1 限 100 Mbit，结果应约 100 Mbit/s；若测出上千 Mbit/s，说明限速没生效
sudo tc -s qdisc show dev <网卡> | head -4     # overlimits 不为 0 才说明真掐过流量
```

> ⚠️ **不要限 SSH 的那块网卡到低于 5 Mbps**，否则你会把自己关在门外。
> 若公网和内网走同一块网卡，把 `tbf` 的 rate 设 ≥50 Mbit，或改用 `tc filter` 只对内网地址限速。

---

## 6. 阶段五：跑实验（半天）

```bash
# 沿用现有网格脚本；worker 地址会自动从集群里解析
QPS_LIST='6 8' XFER_LIST='8' REQUESTS=150 TAG=cloud_r1 bash k8s_test/run_k3s_grid.sh
# 重复 5 次（多机抖动更大，3 次判不出符号）
for r in 1 2 3 4 5; do
  QPS_LIST='6 8' XFER_LIST='8' REQUESTS=150 TAG=cloud_r$r bash k8s_test/run_k3s_grid.sh
done
```

**同时必须记录的三件事**（论文里要报）：
1. **时钟偏移**（跑前跑后各一次 `chronyc tracking`）；
2. **实测带宽**（`iperf3` 结果）；
3. **每格的重复次数与离散度**。

**新增的测量**（这是路线 C 相对单机的真正增量）：
- **共享链路拥塞**：让两个 Pod 同时往第三个 Pod 打流量（incast），看 p95 怎么变——
  **仿真里链路是互相独立的，这个现象被建模排除了**；
- **跨机状态年龄**：现在 `obs_age_s` 包含真实网络往返（同可用区约 0.2–1 ms），
  不再是你 WSL 里 loopback 的 12 µs。

---

## 7. 阶段六：收尾（30 分钟，**别忘了释放机器**）

```bash
# 1) 把产物拉回本地（不要留在云上）
scp -r vm1:/path/to/sim/results/k8s_test ./results_cloud/
scp -r vm1:/path/to/sim/results/matrix_* ./            # 若在云上还跑了仿真
# 2) 记录环境快照（写论文 Implementation 节要用）
for h in vm1 vm2 vm3; do ssh $h 'nproc; free -g | head -2; uname -r'; done > cloud_env.txt
# 3) ★ 在控制台释放三个实例，并确认"实例列表为空"
```

---

## 8. 验收标准（过不了就不能进论文）

| # | 检查 | 判据 |
|---|---|---|
| 1 | 时钟同步 | 三台 `chronyc tracking` 偏移 **< 10 ms**（并把数值写进正文） |
| 2 | 限速真生效 | `iperf3` 实测吞吐 ≈ 设定值 **±10%**；`tc -s` 的 `overlimits` 不为 0 |
| 3 | 重复次数 | **每格 ≥5 次**；离散度写出来（单机时已是 10–30 个百分点） |
| 4 | 无污染 | 脚本自动给 Pod 重启过的样本加 `CONTAM_` 前缀，**带该前缀的不能进结论** |
| 5 | 环境记录 | 实例规格、内核版本、网卡名、安全组规则、`tc` 参数全部留档 |

---

## 9. 做完之后，论文里能改哪几句（这才是目的）

| 现在（单机） | 做完 C 之后 |
|---|---|
| "真机是**单机** WSL2 容器 + **应用层**限速" | "**三台同 VPC 的云主机** + **内核 `tc`** 限速（带宽/时延/队列是真的）" |
| "只能支持方向" | 仍然只能支持方向（**没有 GPU、没有 RDMA、没有多机架**），但**共享链路拥塞**成了新增证据 |
| Threats 第 3 条很长 | 缩短，但**不能删**（要留"非生产 GPU 集群"这一条） |

> **诚实的边界**：云 VM 仍然是**通用 CPU 虚拟机**，不是 GPU、不是 RDMA 网络。
> 做完这一步，你能说"多主机、内核级限速、真实网络栈"，**不能说**"生产级 AI 集群"。

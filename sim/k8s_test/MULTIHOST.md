# 多台真机实测：三条路线与具体做法

> **本文件解决的问题**：当前真机实测的最大两条硬伤是
> ①**只有一台机器**（WSL2 里的单节点 k3s）；②**限速是应用层模拟**（worker 按字节数 sleep），
> 不是内核真的把网卡掐窄。审稿人（尤其 TCC 这类）会直接质疑这两点。
>
> **2026-10-06 更新**：②已经被消掉一半——`measure_system.py` 的传输已改成**真实字节收发**，
> 并且 §1 的 netns + `tc` 路径**已实测通过**（四项验收条件全过）。
> 剩下的②只对"跨机"这一半成立，①仍然完全没解决。完整实测数字见 **`STAGE_A.md`**。
> 本节下面关于"应用层限速"的描述只对老协议（`--fake-transfer` 对照臂）成立。
>
> ⚠️ **先说清楚可信度**：下面**方案 A 的命令我按 WSL2 + Linux 常规做法写，但未在本机验证过**
> （本机只有一台，没法验"多机"路径）；方案 B/C 是标准 k3s 组网步骤。
> 每步后面都标了"怎么验证这一步真的生效"，**请按验证点走，不要盲跑**。

---

## 0. 三条路线：各自能去掉哪条硬伤

| 路线 | 硬件/成本 | 能去掉的硬伤 | 剩下的硬伤 | 建议 |
|---|---|---|---|---|
| **A 同机多命名空间 + tc** | 不需要新硬件，1 天 | 应用层限速 → **真内核队列与真整形** | 仍是单机 | 成本最低，先做 |
| **B 物理多机（旧电脑/笔记本）** | 1–2 台旧机器 + 交换机，2–5 天 | 单机 + 应用层限速 | 机器性能弱、可能是 Wi-Fi | 性价比最高 |
| **C 云上按量 VM** | ¥几十（3 台 × 半天），1–2 天 | 同上，且**最接近生产**（跨机架真网络） | 要花钱、要会配云安全组 | 冲 TCC 建议这条 |

**我的建议：A 先做（当天见效，去掉"应用层模拟"这条），B 或 C 二选一做主要证据。**

---

## 1. 方案 A：同一台机器上做真内核限速（**已在本机 WSL2 验证通过**）

> **验证记录（2026-10-05，本机 WSL2，内核 6.18.33.2-microsoft-standard-WSL2）**
> - `netem` 与 `tbf` **均可用**（`modprobe` 会报失败，但它们是**编译进内核**的，直接 `tc` 就能用）；
> - 按下面步骤搭好后，`tbf rate 25mbit` 的**实测吞吐 = 24.0 Mbit/s**，且 `tc -s` 显示
>   `overlimits 17594`（说明限速器真的在掐流量）；
> - **三个坑必须避开**，见本节末。

### 1.1 逐步清单（照抄即可，每步都有验证点）

```bash
# ★ 第 0 步：先把 WSL 会话挂住！WSL2 空闲会自动关机，一关机 netns / veth / tc 规则全部消失。
#   在另一个终端或后台跑：wsl -d Ubuntu -u root -- sleep 7200
#   （不挂住的话，你会在下一步发现"刚建好的 hostB 不存在"——我踩过。）

# 第 1 步：确认整形模块可用。注意 modprobe 报失败是正常的（编译进内核了），
#          真正的验证是第 3 步能不能加上 qdisc。
wsl -d Ubuntu -u root -- bash -lc 'which tc && ip netns list >/dev/null && echo "tc 与 netns 均可用"'

# 第 2 步：建"远端主机"（network namespace）+ veth 对
ip netns add hostB
ip link add veth0 type veth peer name vethB
ip link set vethB netns hostB
ip addr add 10.250.0.1/24 dev veth0 && ip link set veth0 up
ip netns exec hostB ip addr add 10.250.0.2/24 dev vethB
ip netns exec hostB ip link set vethB up
ip netns exec hostB ip link set lo up
# 验证：ping 通，且 RTT ≈ 你设的 netem 延迟 × 2
ip netns exec hostB ping -c 3 -W 1 10.250.0.1

# 第 3 步：★ 真限速（这就是替换"应用层 sleep"的地方）
ip netns exec hostB tc qdisc add dev vethB root handle 10: tbf rate 25mbit burst 64kb latency 50ms
# 可选：叠加延迟与队列（netem 作为 tbf 的子 qdisc）
ip netns exec hostB tc qdisc add dev vethB parent 10:1 handle 20: netem delay 2ms limit 1000
# 验证：能看到 qdisc
ip netns exec hostB tc qdisc show dev vethB

# 第 4 步：★ 验证限速真的生效（**只看上面那行输出是不够的**）
#   在 hostB 里发，主机侧收；实测吞吐应 ≈ 25 Mbit/s（±10%）
python3 - <<'PY' &
import socket, time
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("10.250.0.1", 9999)); s.listen(1)
c, _ = s.accept(); n = 0; t0 = time.time()
while True:
    d = c.recv(65536)
    if not d: break
    n += len(d)
print(f"实测吞吐 {n*8/(time.time()-t0)/1e6:.1f} Mbit/s")
PY
sleep 1
ip netns exec hostB python3 -c "
import socket, time
s = socket.socket(); s.bind(('10.250.0.2', 0)); s.connect(('10.250.0.1', 9999))
buf = b'x'*65536; t0 = time.time(); sent = 0
while time.time()-t0 < 4: sent += s.send(buf)
s.close()"
wait
# 再看统计：`overlimits` 不为 0 才说明限速器真的掐过流量
ip netns exec hostB tc -s qdisc show dev vethB | head -4
```

### 1.2 三个坑（都是我在本机真踩到的）

| # | 现象 | 原因 | 处置 |
|---|---|---|---|
| 1 | 限速设了 25 Mbit，实测却是 **18827 Mbit/s** | **同一台机器上两个 IP 之间的流量走内核本地路径，根本不经过 veth**，于是绕过了 `tc` | **必须把一端放进 network namespace**（本节的 `hostB`）。这是最容易自欺欺人的一步 |
| 2 | 下一步执行时 `hostB` **不存在** | WSL2 空闲自动关机，**netns / veth / tc 规则不跨关机保留** | 用 `wsl ... -- sleep 7200` 把会话挂住，或把所有步骤放进**同一个会话**里跑 |
| 3 | `modprobe sch_netem` 报失败 | WSL2 内核把这些 qdisc **编译进内核**而非做成模块 | 不用管，直接 `tc` 用；真正的判据是 `tc qdisc add` 是否成功 + 实测吞吐 |

### 1.3 把 k3s 的 worker 接到这条真限速路径上

把 worker Pod 的 listener 绑到 `10.250.0.x`（而不是 Pod 网络），客户端也在这个 netns 里跑，
`measure_system.py` 的 `--workers` 指向该地址即可。脚本逻辑不用改。

> **2026-10-06 补充**：脚本现在**多了一个角色 `transfer`**（阶段 A 的验证电池），
> 以及一个环境重建脚本 `setup_netns_tc.sh`（`up [速率] | down | show`）。
> 用它们可以直接把这条路走完并逐项拿到 PASS/FAIL，命令见 `STAGE_A.md` §7。
> 另外注意：**k3s 镜像里 bake 的是老协议副本**，要用新 client 打 Pod 必须先重跑
> `setup_wsl_k3s.sh` 重建镜像。

> **能写进论文的话**：网络代价由**内核 `tc` 整形**产生（带宽、时延、队列深度都是真的，
> 有 `overlimits` 计数为证），worker 之间经 `veth` 与真实 qdisc。
> **仍然不能写**：跨机、真实机架、真实 RDMA。

> ⚠️ **本节的命令我在本机验证过；1.3 把 k3s 接上去这一步未验证**（需要重配 Pod 的监听地址），
> 请按"实测吞吐 ≈ 设定值"这个标准自行确认。

---

## 2. 方案 B/C：真正的多机 k3s

### 2.1 准备（每台机器都做）

```bash
# Ubuntu 22.04+，每台机器：
sudo apt update && sudo apt install -y curl ca-certificates iproute2 util-linux jq
# ① 时钟必须同步——跨机比较"状态年龄"依赖时钟，不然后面所有 obs_age_s 都是垃圾
sudo apt install -y chrony && sudo systemctl enable --now chrony
chronyc tracking | grep -E "System time|Last offset"     # 偏移应 < 10 ms
# ② 关 swap（k3s 要求）
sudo swapoff -a && sudo sed -i '/ swap / s/^/#/' /etc/fstab
# ③ 记下网卡名与带宽（后面 tc 要用）
ip -br addr; ethtool eth0 2>/dev/null | grep Speed
```

### 2.2 组 k3s 集群（第一台作 server，其余作 agent）

```bash
# —— 第 1 台（server）——
curl -sfL https://rancher-mirror.rancher.cn/k3s/k3s-install.sh | INSTALL_K3S_MIRROR=cn \
  sh -s - server --disable traefik --disable servicelb --disable metrics-server \
                 --node-ip <本机内网IP>
# 取 token 与地址，在 agent 上用：
sudo cat /var/lib/rancher/k3s/server/node-token
# —— 第 2、3 台（agent）——
curl -sfL https://rancher-mirror.rancher.cn/k3s/k3s-install.sh | INSTALL_K3S_MIRROR=cn \
  sh -s - agent --server https://<server 内网IP>:6443 \
                --token <上面的 token> --node-ip <本机内网IP>
```

**防火墙/安全组必须放行**：`6443/tcp`（API）、`8472/udp`（flannel VXLAN）、`10250/tcp`（kubelet）、
`2379-2380/tcp`（仅 server）。云 VM 要在**安全组**里开，本机 `ufw` 也要开。

**验证**：
```bash
kubectl get nodes -o wide          # 三台都 Ready
kubectl top nodes 2>/dev/null || true
```

### 2.3 把"异构"做出来

仿真里的异构是"快算力 + 慢链路 ↔ 慢算力 + 快链路"。真机上对应：

```bash
# 每台机器上按角色限速（把 eth0 换成你的网卡名）——这是"真网卡限速"
# 慢链路机器：100 Mbit/s
sudo tc qdisc add dev eth0 root handle 1: tbf rate 100mbit burst 128kb latency 100ms
# 中链路机器：500 Mbit/s
sudo tc qdisc add dev eth0 root handle 1: tbf rate 500mbit burst 256kb latency 50ms
# 快链路机器：1000 Mbit/s（或不限）
sudo tc qdisc add dev eth0 root handle 1: tbf rate 1000mbit burst 512kb latency 20ms
sudo tc -s qdisc show dev eth0        # 检查是否生效
```

Pod 侧用 `limits.cpu` 保持 4:2:1 的算力比（现有 `k8s/workers.yaml` 已是这个结构，改副本数与亲和性即可）。

### 2.4 跑现有网格（脚本不用改逻辑）

```bash
# 把 worker 地址指向跨机 Pod（脚本会自动解析 Pod IP）
bash k8s_test/run_k3s_grid.sh
# 建议参数：3 次重复，两个 qps，大传输档
QPS_LIST='6 8' XFER_LIST='8' REQUESTS=150 TAG=k3s_multi_r1 bash k8s_test/run_k3s_grid.sh
```

### 2.5 ⚠️ 四个必须避开的坑

1. **别在控制面网卡上无脑限速**：`tc` 限的是整块网卡，可能把 k3s 的心跳/etcd 一起掐了，
   集群会抖甚至掉线。要么**只在数据面接口上做**，要么把带宽下限留在 ≥50 Mbit/s。
2. **时钟偏移会毁掉状态年龄测量**：跨机时 `obs_age_s` 是跨机器时钟相减。上 chrony，
   跑前 `chronyc tracking` 确认偏移 < 10 ms，并在论文里报这个数字。
3. **Wi-Fi 不要用**：无线链路的带宽/时延抖动远大于你要测的效应。能接线就接线。
4. **重复次数**：单机时重复间离散度是 **10–30 个百分点**（见 `README.md` §5d）。
   多机只会更抖，**每格至少重复 5 次**，否则符号都判不出来。

---

## 3. 多机之后，"新增"了哪些仿真给不了的证据

这是多机实测真正的价值——不是"数字更真"，而是**能测到仿真里根本不存在的现象**：

| 能测到什么 | 为什么仿真给不了 |
|---|---|
| **跨机的状态年龄**（含真实网络往返） | 单机是 loopback，往返 12 µs；跨机后这一项会变成零点几到几毫秒，且随负载变化 |
| **共享链路拥塞**（多 worker 挤同一条上行/汇聚链路） | 仿真的链路互相独立，这个现象**被建模排除了** |
| **真实网卡排队与丢包** | `tc` 的队列会真的排队、真的丢包，能观察到重传对 p95 的影响 |
| **"看状态"本身的跨机代价** | 单机取状态是本机 HTTP；跨机后取状态要过网络，反馈环更强 |

建议在论文里就把这四条列成"多机实测能回答而仿真不能回答的问题"——这样多机不只是"补数据"，
而是**新增一类证据**。

---

## 4. 验收标准：怎么判断这次实测是"干净的"

跑完先过这四条，过了才进论文：

1. **时钟**：所有节点 `chronyc tracking` 偏移 < 10 ms（写进正文）。
2. **限速真的生效**：`tc -s qdisc show` 的 rate 与实测 `iperf3` 吞吐一致（±10%）。
3. **稳定**：同一格重复 5 次的 p95 离散度写出来；若 > 50% 说明环境不稳，先解决再加重复次数。
4. **没有污染**：`run_k3s_grid.sh` 会自动检测 Pod `restartCount` 变化并给输出加 `CONTAM_` 前缀——
   被标 `CONTAM_` 的样本**不能进结论**。

---

## 5. 一句话决策

- **只想去掉"应用层模拟"这条硬伤** → 做**方案 A**（1 天，零成本）。
- **要冲 TCC / CCF-B** → **方案 B 或 C**，并且**每格重复 ≥5 次**、**报时钟偏移**、
  **把"共享链路拥塞"作为新增发现单独写一节**。
- **暂时都不做** → 那就必须把这两条硬伤**写进 Threats**，并把真机结果**降级为"方向性证据"**
  （现在的正文已经这么写了，不会被判为隐瞒，但会被认为证据强度不足）。

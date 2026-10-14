# 真实系统实测（k8s_test）—— 状态与操作手册

> 最后更新：**2026-09-23 12:40 本地**。
> **当前状态：k3s 已装成并跑起来了。** 单节点 `localhost` Ready（k3s v1.31.5+k3s1 /
> containerd 1.7.23-k3s2 / 内核 6.18.33.2-microsoft-standard-WSL2 / cgroup v2 / Ubuntu 26.04），
> 自建镜像 `docker.io/simtest/base:1` 已导入，3 个 worker Pod 全部 Ready 且参数各异
> （算力 4/2/1、带宽 100/500/1000 Mbps），从 WSL 可直接用 Pod IP 访问。
> 装机过程失败 5 次，每条坑都记在下面 §6。

## 1. 为什么要做这一步

论文的全部主结论来自 SimPy 仿真。要让人相信**趋势**不是建模 artifact，
就必须在真实运行的系统上把同一个对照再做一遍。`论文规划.md` §7 的定位：
单节点 kind/k3d **只用于趋势对照**，不得包装成生产级集群性能。

对照的核心问题只有一个：**把"链路积压"放进调度决策，在什么条件下有用、在什么条件下有害。**

## 2. 环境现状（实测，不是推测）

| 项 | 状态 |
|---|---|
| 硬件虚拟化 | 已开启（Hyper-V 四项要求全"是"） |
| `Microsoft-Windows-Subsystem-Linux` | 已启用 |
| `VirtualMachinePlatform` | **启用待定 → 必须重启**（`wsl --status` 明确报"未启用虚拟化"） |
| Docker / kubectl / kind / k3d / minikube | **本机都没有**；WSL 里还没有发行版 |
| `registry-1.docker.io` / `auth.docker.io` | **不通**（000 超时）→ 不能用默认流程拉镜像 |
| `download.docker.com` | 不通 → Docker Desktop 装不上 |
| `rancher-mirror.rancher.cn/k3s/...` | **通**（k3s 二进制与 sha256 都是 200）→ 从中国镜像装 |
| `archive.ubuntu.com` / `mirrors.aliyun.com` | 通 → 用 apt + debootstrap 造 rootfs |
| `gh-proxy.com` | 通（GitHub 直连不通，必要时走它） |

**结论：走 WSL2 + k3s，镜像全部就地构建，不依赖任何公共镜像仓库。**

## 3. 操作顺序

### 第 1 步：Windows 侧重启（必做，只能人工）

重启后 WSL2 才能起来。重启前建议先做完正在跑的仿真矩阵
（`results/matrix_trace_v2/` 的 `rho/staleness/main` 三个 mode）。

### 第 2 步：装 Ubuntu（管理员 PowerShell）

```powershell
wsl --install -d Ubuntu --no-launch     # 只注册发行版，不弹交互式创建用户
wsl --shutdown                          # 让上面的注册生效
```

### 第 3 步：确认 WSL 里能跑（Git Bash 或 PowerShell）

```bash
wsl -d Ubuntu -u root -- /bin/bash -lc "uname -a; nproc; cat /proc/version | head -1"
```

若提示"未启用虚拟化"，说明重启没生效或 BIOS 里 VT-x 被关，先解决这一步再往下。

### 第 4 步：一键装 k3s + 构建镜像

```bash
wsl -d Ubuntu -u root -- bash /mnt/e/代码项目文件/论文/sim/k8s_test/setup_wsl_k3s.sh
```

脚本做的事（幂等，可重复跑）：
1. 打一份环境能力报告到 `/tmp/k3s-report.txt`（内核/cgroup 版本/nproc/tc 是否可用）——
   写论文 Implementation 小节要引用这些；
2. apt 装 `curl ca-certificates iproute2 util-linux jq podman debootstrap`；
3. `debootstrap` 造一个含 python3 的最小 rootfs → `podman import` 成镜像
   `simtest/base:1` → 导入 k3s 的 containerd（**全程不碰任何镜像仓库**）；
4. 从 rancher 中国镜像装 k3s（固定版本，不走 github，不查 channel）；
5. 写 `/etc/rancher/k3s/config.yaml`：禁用 `traefik / servicelb / metrics-server /
   local-storage / coredns`（它们都要从 Docker Hub 拉镜像），
   `pause-image` 指到我们自己构建的镜像；
6. 起 k3s 并等节点 Ready。

> 为什么禁用 CoreDNS：集群 DNS 的镜像拉不到。Pod 之间改用 **Pod IP** 直连，
> 客户端跑在集群外的 WSL 里，仍然走真实内核网络栈（cni0/veth）。

### 第 5 步：跑对照网格

```bash
wsl -d Ubuntu -u root -- bash /mnt/e/代码项目文件/论文/sim/k8s_test/run_k3s_grid.sh
```

它先起 3 个 worker Pod（`k8s/workers.yaml`，算力 4/2/1、带宽 100/500/1000 Mbps，
与仿真的"快算力+慢链路 ↔ 慢算力+快链路"权衡同构），再跑：
到达率阶梯（门 1 的实测版）→ `{least_load, contention} × {新鲜, 陈旧 3s} × {小传输, 大传输}` 网格，
最后用 `summarize_real.py` 出 `results/k8s_test/summary.csv` 与 `comparison.csv`。

## 4. 这一步能得出什么、不能得出什么

**能**：同一份调度逻辑在真实容器与真实网络栈上，"加不加链路积压项"哪个 p95 更低，
以及这个方向如何随**数据量**（`net_share`）与**观测陈旧度**翻转。

**不能**（写正文必须照抄）：

1. 单节点（WSL2 里一个 k3s）不是多机集群 → 只能比方向；
2. worker 的算力仍是**应用层抽象系数**（一段忙等除以 `compute`）；**注意 2026-10-06 起
   带宽不再是应用层系数**——传输改成了真实字节收发，快慢由内核 `tc` 与真实网卡决定
   （见 §4b）。老行为只在显式加 `--fake-transfer` 时才会出现；
3. worker 是线程化 HTTP 服务、并发度不设上限 → "排队"体现为 CPU 争抢导致服务时间拉长，
   不是固定槽位排队，所以绝对延迟/吞吐的具体数值不可引用；
4. 因此它与仿真的可比性**只在 net_share 落到同一区间时**成立。

## 4b. 协议变更：从"假装传输"改成"真实字节收发"（2026-10-06）

**为什么要改**：`真实跨云LLM服务研究计划书.md` §5 阶段 A 明文规定
"当前 `measure_system.py` 的 `transfer_mb` 只控制 `sleep`（程序暂停），
必须改掉后才能用于本阶段"。老协议里 `POST /work` 的 body 是一段 JSON，
worker 读到 `transfer_mb` 之后 `time.sleep(mb/带宽)` —— **传输是假装的，只占时间不占线路**，
所以"限速、网卡计数器、字节一致性"这三件事在老协议上根本无从验证。

**新协议**（`measure_system.py` 同时兼容老协议，走哪个由请求头决定）：

| | 老（`--fake-transfer`） | 新（默认） |
|---|---|---|
| 请求 body | 一小段 JSON（含 `transfer_mb`） | `X-Upload-Bytes` 个**真实字节** |
| worker 行为 | 按 `transfer_mb/带宽` 睡眠 | 分 256 KiB 逐块**真读完并计数**，再忙等 `service_units/compute` |
| 响应 body | 一小段 JSON | `X-Download-Bytes` 个**真实字节** |
| worker 的自测值 | JSON 字段里 | **响应头**里（`X-Received-Bytes`/`X-Sent-Bytes`/`X-Recv-S`/`X-Service-S`/`X-Payload-Crc`…） |
| 客户端实现 | `urllib.request` | `http.client`，分块 `send` / 分块 `read1` |

三条设计约束（改代码时必须维持）：

1. **客户端不能用 `urllib.request`**：它先把请求体整个缓冲、再把响应一次读完，
   "上传耗时"与"下传耗时"的分界被抹掉，而这两段正是要分开量的。
2. **worker 只信实际读到的字节数，不信 `Content-Length`**：阶段 A 的第一条通过条件
   就是"声明的字节数 = 真的过线的字节数"，只读头部等于什么都没验。
   载荷的 CRC32 也会两侧比对（`X-Payload-Crc`），排除"读到的其实是上次的残留"。
3. **载荷用一块 1 MiB 的固定缓冲反复复用，不用 `os.urandom`**：
   200 MB 随机数要花好几秒 CPU，那段时间会被算进"上传耗时"里。

`client` 角色每行 CSV 新增了 `transfer_mode` / `upload_bytes` / `download_bytes` /
`srv_recv_bytes` / `choose_s` / `ph_upload_s` / `ph_server_s` / `ph_download_s` 几列，
原有列一个没删（`summarize_real.py` 照常可读）。

> ⚠️ **k3s 镜像里 bake 的是老协议副本**（`setup_wsl_k3s.sh` 把 `measure_system.py`
> 拷进 `/opt/measure_system.py`）。要用新 client 打 Pod，**必须重跑
> `setup_wsl_k3s.sh` 重建镜像并重启 Pod**，否则老 worker 会把二进制 body 当 JSON 解析而报 500。

## 4c. 新增角色 `transfer`：阶段 A 的验证电池

不涉及调度器、不涉及 LLM、不涉及 GPU。它依次跑**六项检查**逐项给 PASS/FAIL
（其中 A1–A4 是计划书 §5 阶段 A 明文规定的通过条件，A1b 与 A5 是附带的核对），
产物落在 `results/stage_a/transfer_<UTC>_battery.{csv,json}`（文件名带 run_id，append-only）。

| 项 | 检查什么 | 判据 |
|---|---|---|
| A1 | 字节一致性 | 客户端发 N == 服务端实收 N == 服务端声明 N；服务端发 M == 客户端实收 M；两侧载荷 CRC 一致。至少覆盖 0.5 / 8 / 200 MB |
| A1b | 阶段时间加总 | 客户端四段（入口/上传/服务端段/下传）之和 == 端到端；服务端可自证的三段 ≤ 端到端，残差 ≤ 25% |
| A2 | 限速真生效 | 被限速方向的实测吞吐落在 `--tc-rate-mbps` 的 ±10% 内 |
| A3 | 限速统计确实增加 | `tc -s qdisc` 的 `overlimits` **增量 > 0**（只看 `tc qdisc show` 有输出不算） |
| A4 | 单路 vs 多路可分 | 单路吞吐 / 并发时每路吞吐 ≥ 1.5（共享瓶颈下理想值 = 并发路数） |
| A5 | 网卡计数器 / 应用层字节 | 只报告比值（TCP/IP 头开销 ≈ 1.03–1.07），不作 PASS/FAIL |

两种用法：

```bash
# ① 单机自足（电池自带一个进程内 worker）——只能验 A1/A1b；A3/A4 必然 FAIL，
#    因为回环不经 veth、没有真实队列。用来确认软件正确性是够的。
python3 sim/k8s_test/measure_system.py transfer --sizes-mb 0.5 8

# ② 两台"主机"（WSL2 network namespace，见 MULTIHOST.md §1.1）——四项都能验。
#    先建链路（双向 25 Mbit）：
bash sim/k8s_test/setup_netns_tc.sh up 25
#    服务端放在 root 命名空间：
cd sim && python3 k8s_test/measure_system.py worker --bind 10.250.0.1 --port 9201 --compute 1e6 &
#    电池放进 hostB（那里才看得到被 tc 整形的 vethB）：
ip netns exec hostB python3 k8s_test/measure_system.py transfer \
  --target 10.250.0.1:9201 --iface vethB --tc-rate-mbps 25 --shaped-direction both \
  --sizes-mb 0.5 8 200 --rate-test-mb 8 --rate-test-concurrency 3
```

- `--iface` 决定读哪块网卡的计数器；**netns 里没有默认路由**，所以两机模式下必须显式给。
- `--tc-cmd` 覆盖默认的 `tc -s qdisc show dev <iface>`；如果被整形的网卡在另一个命名空间里，
  就写 `--tc-cmd 'ip netns exec hostB tc -s qdisc show dev vethB'`。
- `--shaped-direction {both,upload,download}` 声明哪几个方向真的被限速了，
  只判定被限速的方向（拿未限速方向去比设定值必然 FAIL，那是口径错误不是发现）。
- 实测结果与完整记录：`sim/k8s_test/STAGE_A.md`。


## 5. 首轮真实集群结果（2026-09-23 13:32）

**门 1（实测版）**：`rho_cpu = 实测到达率 × 实测 CPU 服务时间 / 机群 CPU 配额`
（三个 Pod 各 `limits.cpu=2` → 6 核；传输是 sleep 不占 CPU，不进分子）。

| 目标 qps | 实测 qps | rho_cpu | p50 | p95 | net_share |
|---|---|---|---|---|---|
| 4 | 4.04 | 0.365 | 0.864 | 1.939 | 0.361 |
| 6 | 6.06 | **0.503** | 1.044 | 2.123 | 0.418 |
| 8 | 8.07 | **0.682** | 1.113 | 2.087 | 0.397 |
| 12 | 12.11 | 1.025 | 1.201 | 2.323 | 0.357 |

主网格取 qps 6 与 8（rho_cpu 0.49–0.89，落在有效区间内）；qps 12 过饱和，只作阶梯边界。

**主网格（`contention` = 算力+链路积压 vs `least_load` = 只看算力队列；Δp95<0 表示前者更优）**

| 数据量 | net_share | 列 | qps=6 lag 0 | qps=6 lag 3s | qps=8 lag 0 | qps=8 lag 3s |
|---|---|---|---|---|---|---|
| **38.9 MB** | **0.65** | least_load p95 | 5.93 | 5.54 | 6.99 | 7.21 |
| | | contention p95 | 3.25 | 4.36 | 4.37 | 5.19 |
| | | **Δp95** | **−45.2%** | −21.4% | **−37.5%** | −27.9% |
| 9.7 MB | 0.32–0.40 | Δp95 | −11.0% | +47.5% | +5.5% | +10.2% |
| 0.6 MB | 0.01–0.05 | Δp95 | −1.6% | +68.7% | +23.1% | −16.3% |

三句话读：

1. **收益只出现在网络占比大的时候**：38.9 MB（net_share ≈0.65）时 `contention` **4/4 全胜**，
   p95 低 21–45%；9.7 MB、0.6 MB 时基本无效甚至反向（+5%～+69%）。
   这与仿真的收益边界同向（仿真在 `degraded`、net_share 0.65–0.9 上给 −40%～−60%）。
2. **状态陈旧会收窄收益**：38.9 MB 档从 −45.2%/−37.5%（新鲜）变成 −21.4%/−27.9%（陈旧 3 s）。
   方向与仿真的 RQ2 一致。
3. **每格只有 1 次运行、没有置信区间**，而且抖动很大（同档位不同 qps 会出现符号相反的结果）。
   所以这套实测只能用来支持**方向与量级**，不能当统计结论；写正文必须连这句一起写。

产物：`results/k8s_test/{summary,comparison}.csv` + 每请求 CSV；`old_sync_*` 是同步 client
时代的样本（保留作证据、不进结论），`CONTAM_*` 是运行中 Pod 重启过的样本（脚本自动标记）。

本机多进程的早期版本（不启容器，net_share≈0.88 时 −64%）也在同一目录，可作交叉印证。

## 5c. 状态年龄实测：调度器看到的状态到底有多旧（2026-09-23 新做）

**为什么要测**：仿真里"观测年龄"是我们自己设的常数；真实系统里它是
「距上次轮询过了多久」+「**那一次拉取本身花了多久**」+ 注入的 lag。
其中第二项**随系统变忙而变长**，构成一个反馈：越想看清谁忙，看得越慢——仿真缺的正是这一项。

做法：`bash k8s_test/measure_state_age.sh`（扫 `--state-ttl`，即模拟的监控采集周期），
每请求都记录 `obs_age_s`。**判据：年龄 = 现在 − 视图里最旧那份数据的取回时刻。**

| 采集周期 | 实测年龄 p50 | p95 | **平均一次拉取耗时** | 请求 p50 | **请求 p95** | 超时次数 |
|---|---|---|---|---|---|---|
| 0.05 s | 64 ms | 237 ms | **56 ms** | 1.01 s | 2.31 s | 0 |
| 0.25 s | 178 ms | 338 ms | 93 ms | 1.04 s | 2.33 s | 0 |
| 1 s | 755 ms | 994 ms | 167 ms | 1.08 s | 2.46 s | 0 |
| 5 s | 3.58 s | 4.84 s | 625 ms | 1.64 s | **4.85 s** | 2 |
| 15 s | 6.43 s | 13.84 s | 876 ms | 2.50 s | **8.26 s** | 2 |

**三条发现（都是真机实测，不是仿真）**

1. **年龄 ≈ 采集周期的一半到全部**（p50 = 0.43–0.76 × 周期），符合"均匀采样"的直觉：
   一个周期内随时被查询，平均就等到半个周期。所以"用 15 秒的采集周期"意味着
   **平均看 6.4 秒前的状态、最坏 14.8 秒前的状态**。
2. **拉取本身慢得离谱：56–876 ms，而机内网络一个来回只要 12 µs（库内 OSCAR 论文的实测值）**
   ——也就是说，观测比网络允许的慢 **50–900 倍**。原因不是网络，是 worker 的 HTTP 线程被
   CPU 忙等挤掉了（每个 Pod 只有 2 核配额）。
   **这是"观测反馈环"的直接证据**：系统越忙，你看到的状态越旧、越慢。
3. **年龄对性能的影响是"门限式"的，不是线性的**：年龄从 64 ms 涨到 755 ms（12 倍），
   p95 只从 2.31 s 动到 2.46 s（**几乎没影响**）；但从 755 ms 再涨到 3.6–6.4 s，
   p95 直接到 **4.85 s / 8.26 s（坏 2–3.6 倍）**。
   → 也就是说，**"多旧算太旧"存在一个门限，位置大约在 1–5 秒之间**（本设置下），
   而不是"越新鲜越好"的连续关系。这与仿真里那条平滑的曲线**形状不同**，值得单独研究。
4. 附带一个真实的失效模式：**拉取超时（>2 s）时，client 会用"全都极忙"的假状态兜底**，
   调度器于是近似**盲选**。两处粗周期各出现 2 次。这条要写进 Threats。

**限制（照实写）**：每格只跑 1 次、无重复；qps=8、240 请求；Pod 的 CPU 配额是 2 核，
所以"拉取慢"的量级比生产环境更极端；采集周期是我们自己实现的轮询（不是 Prometheus）。

## 5d. 每格重复 3 次 ×2 批（2026-10-01）——把"每格 1 次、无置信区间"这条限制补掉

**为什么补**：§5 的首轮网格每格只跑 1 次，抖动很大（同档位不同 qps 甚至出现符号相反的结果），
所以那批只能支持"方向与量级"。这里在同一格上重复跑，直接看离散度。

**两批**（配置完全相同，只差运行环境）：

| 批次 | TAG | 运行环境 |
|---|---|---|
| 第一批 | `k3s_r{1,2,3}` | 机器上**同时跑着一个仿真进程**（参数消融） |
| 第二批 | `k3s_q{1,2,3}` | **安静机器**（只挂着一个不占 CPU 的 WSL 保活会话） |

配置：`QPS_LIST='6 8' XFER_LIST='8' REQUESTS=150`，即 38.9 MB 大传输（`net_share ≈ 0.65`）
× 2 个 qps × 2 个状态新鲜度（0 s / 3 s）。每批 8 次运行，两批共 6 次重复/格。

**每格 Δp95%（`contention` 看网络 相对 `least_load` 不看网络；负 = 看网络更好）**

| qps | 状态 lag | 并发 r1 | 并发 r2 | 并发 r3 | 干净 q1 | 干净 q2 | 干净 q3 | 6 次方向 |
|---|---|---|---|---|---|---|---|---|
| 6 | 0 s | −16.5 | −16.8 | −19.2 | −13.1 | −31.7 | −15.6 | **6/6 负** ✅ |
| 6 | 3 s | +16.6 | +9.9 | −1.8 | −8.2 | +9.4 | +4.6 | 4 正 2 负 |
| 8 | 0 s | −7.5 | −3.8 | +1.9 | −5.4 | −16.8 | +2.0 | 5 负 1 正 |
| 8 | 3 s | +23.4 | +15.0 | −11.4 | −8.6 | −11.8 | +2.7 | 3 正 3 负 |

**四条结论（含一条撤回）**：

1. **新鲜状态 + 中等负载（qps=6）下，"看网络更好"是这批实测里唯一稳的方向**：
   两批 6 次重复全部为负（−13.1% ~ −31.7%）。这是真机部分唯一能当结论写的一格。
2. **负载升高（qps=8）后收益变弱且不再一致**（5 负 1 正，幅度收到 −17% ~ +2%）：
   与仿真里"负载强度是收益的一个自变量、过饱和时 p95 只在测排队排空"同向。
3. **状态旧 3 秒时符号判不了**：两批合起来每次重复有正有负（q6：4 正 2 负；q8：3 正 3 负）。
   只能写"方向与仿真的『陈旧吃掉收益』一致，但**真机分辨率不足以判定符号**"。
   —— 这一条比第一版更保守，因为多跑的 3 次把它推回了"不可判定"。
4. **⚠️ 撤回一条上一版写过的"新发现"**：第一版我写过"陈旧状态下看网络的策略更**不稳定**
   （p95 三次 4.21/3.74/2.98，±17%，而基线只有 ±2.4%）"。**安静批次里它没有复现**
   （干净批 q8/lag3：`least_load` 波动 10.8%、`contention` 18.0%；q6/lag3 甚至反向：
   基线 29.5%、看网络的 12.5%）。**因此这条不能作为发现写入论文**——它是并发批次的偶然。
   → 这正是"补重复"的价值：**它否掉了我们自己的一条看起来很有戏的结论**。

**两批合起来最重要的一句话**：**重复之间的离散度是 10–30 个百分点**（安静机器上也一样，
所以不是并发造成的）。因此 §5 首轮那些"−45%、−70%"的单次数字，**必须读懂为带 ±10–30 pp 误差棒的方向性证据**，
不能当作精确幅度。这条限制直接决定了真机结果在论文里的地位：**只支持方向，不支持幅度**。

**产物**：`results/k8s_test/k3s_{r,q}{1,2,3}_*.csv`（每请求级，含 `obs_age_s` / `state_lag_s` /
`w_transfer_s` / `w_service_s` 分解），两批共 48 个文件 + 各自 `*_workers.json`。

## 6. 实际踩过的坑（2026-09-23 首次装成，逐条都真实发生过）

装这一步一共失败 5 次才通，每条都值得记下来——它们的共同特征是**报错位置和真实原因隔了很远**。

| # | 现象 | 真正原因 | 处置 |
|---|---|---|---|
| 1 | `[ERROR] Download failed`（官方 install.sh） | 脚本把版本号编成 `v1.31.5%2Bk3s1`，rancher 镜像站路径却是 `v1.31.5-k3s1`（`+` vs `-`） | 不走 install.sh，直接下二进制 |
| 2 | 脚本**静默退出**，没有任何报错，只停在上一行 | `H1=$(fetch_hash ...)`：源不可达时 `curl` 非零 → `pipefail` → 赋值失败 → `set -e` 直接终止 | 函数体末尾加 `\|\| true` |
| 3 | `podman save` 报 `docker-archive doesn't support modifying existing images` | 归档文件已存在；脚本要幂等可重跑 | save 前 `rm -f` |
| 4 | Pod 卡在 `ImagePullBackOff`（`imagePullPolicy: Never` 也救不了） | podman 把镜像存成 `localhost/simtest/base:1`，而 kubelet 把 `simtest/base:1` 规范化成 `docker.io/simtest/base:1`，两边字符串不匹配 | 镜像名与 Pod 里都写**全限定名** `docker.io/simtest/base:1` |
| 5 | Pod 卡在 `ContainerCreating`，事件里 `exec: "sleep": executable file not found in $PATH` | `podman import` 造出的镜像**没有 ENV PATH**，sandbox（pause）找不到 `sleep` | `--change 'ENV PATH=...'`，且 CMD 用绝对路径 `/bin/sleep` |
| 6 | `Unit k3s.service does not exist` | 绕过了官方 install.sh，就没人给你写 systemd 单元 | 脚本里手写官方那份 unit + `daemon-reload` |
| 7 | `ctr: failed to dial /run/k3s/containerd/containerd.sock` | 镜像导入做在 k3s 启动**之前**，那时 containerd 还没起来 | 顺序改为：起 k3s → 等节点 Ready → 再导镜像 |
| 8 | 下载成功却被判"没下成" | 先判 `[ -x ]`、后 `chmod +x`：curl 写出的文件默认没有可执行位 | 判 `[ -s ]`（非空） |

另外：`rancher-mirror.rancher.cn` 的 **DNS 时通时断**（同一台机器上，第一次 200、第二次直接解析超时），
所以脚本现在同时尝试 rancher 镜像与 `gh-proxy.com` 反代 GitHub release，
并要求**两个独立来源的 sha256 一致**才继续——同一个源既给文件又给哈希只能防传输损坏。

### 7. 实测工具自身踩过的两个坑（不是环境问题，是**测量口径**问题，更该记）

| # | 现象 | 真正原因 | 处置 |
|---|---|---|---|
| 9 | qps=2 与 qps=4 两档的 p50/p95/p99 **几乎逐位相同**（1.0152/1.9780/2.8566 vs 1.0153/1.9783/2.8564） | client 是"发一个等一个"的同步循环 → **client 自己就是限流器，永远压不出排队**，而本课题的机制（看积压量）只在有积压时才有意义 | 改成**开环并发**（`--concurrency`，默认 32）：按到达时刻提交、不等上一个完成；在途请求数由系统决定 |
| 10 | lag=3 的 4 次运行里 **240/240 个请求全打到同一个 worker**，p95 从 2 s 涨到 9–13 s，看起来像"陈旧很致命" | `_aged_snapshot()` 在 lag>0 时**从不调用 `snapshot()`** → `_history` 永远只有预热那一条（全空载）→ 每个请求都看到同一份"所有节点都空闲"的快照 → 打分并列 → 永远同一个节点 | 回看之前先 `snapshot()` 刷新历史；修后 lag=3 的路由分散到 3 个档位（28/21/11） |

另外两条口径修正：

- **利用率不能用 `busy_s` 算**：它记的是每个请求的墙钟时长（含排队），并发时还会重叠计时，
  实测虚报过 47 倍。真机的"门 1"改用 **`rho_cpu` = 实测到达率 × 实测 CPU 服务时间 / 机群 CPU 配额**
  （配额取 `workers.yaml` 的 `limits.cpu`，三个 Pod 各 2 → 6 核；传输是 sleep 不占 CPU，不进分子）。
- **节点身份按"算力/带宽档位"记，不按 Pod IP**：k3s 重启、Pod 重建都会换 IP，
  按 IP 统计会把不同档位混在一起（已经踩过一次，还以为 IP↔档位是固定的）。
  新版 client 每行都写 `worker_compute` / `worker_bw_mbps`。

### 8. 为什么集群会"自己重启"（已定位，不是 bug）

现象：k3s 的 `ActiveEnterTimestamp` 每隔一阵就更新、三个 worker 的 `restartCount` 慢慢涨、
Pod IP 偶尔变；`systemctl show k3s -p NRestarts` 却一直是 0（说明不是崩溃重启）。

根因：**WSL2 的虚拟机在"没有 wsl.exe 会话挂着"约 60 秒后会自动关机**
（`vmIdleTimeout`，默认 60 s）。实测：一条命令里 `uptime` 显示 `1:39`，
隔 100 秒再执行同一命令就变成 `up 0 min`——VM 被关掉又起来，k3s 与容器自然全没了，
下一次命令一进 WSL 就由 systemd 重新拉起，于是表现为"自己重启"。

影响与对策：
- 一次长网格跑在**一个持续挂着的 wsl.exe 会话**里，中途不会重启（首轮 27 分钟的网格只
  在开头那次引导时重启过，12 个格子没有一条被标 CONTAM_）；
- 但两条命令之间隔超过 1 分钟就会触发一次关机/开机循环，表现为"刚才还好好的 Pod 全重启了"；
- 想让 VM 常驻：在 Windows 侧 `C:\Users\<你>\.wslconfig` 写

  ```ini
  [wsl2]
  vmIdleTimeout=-1      # -1 = 不自动关机（也可以给一个大毫秒数）
  ```

  然后 `wsl --shutdown` 让它生效。**注意这会影响你机器上所有 WSL 发行版。**
- 脚本自带的重启检测（运行前后比 `restartCount`）会把跨越重启的运行标成 `CONTAM_`，
  这类样本不当干净数据用。

顺带记录一条观察：满载时 k3s 事件里会出现
`Readiness probe failed ... context deadline exceeded`——worker 是线程化 HTTP 服务，
2 核 CPU 配额在几十个并发请求下会被忙等吃满，健康检查都插不进去。这不影响
"哪个策略 p95 更低"的对照（两边同样受限），但它说明**worker 的并发度不设上限**这条简化
在真机上是真实存在的（写 Threats 时照抄）。

### 其他常见问题的处理

| 现象 | 处理 |
|---|---|
| 脚本报"未启用虚拟化" | Windows 没重启，或 BIOS 里虚拟化被关 |
| k3s 起不来、报 cgroup 相关错 | 确认 `/etc/wsl.conf` 里有 `[boot]\nsystemd=true`，然后 `wsl --shutdown` 重来 |
| 想要集群 DNS | 需要先让 Docker Hub 镜像可拉（配 containerd mirror），再把 `coredns` 从 disable 列表去掉 |
| 想要内核级网络整形 | 需要 worker 真的**发送字节**（现在是睡眠模拟），并给 Pod 加 `NET_ADMIN`；这是下一步扩展，不是现状 |

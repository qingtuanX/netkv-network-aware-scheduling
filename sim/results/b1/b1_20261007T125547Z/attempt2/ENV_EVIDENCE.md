# attempt2 —— 环境准备阶段的实测证据（由**另一个并行代理接手前**完成的）

> **这个目录为什么只有准备阶段的东西**
>
> 2026-10-07 13:19 UTC 前后发现：**同一个仓库目录里有另一个代理在并行执行同一个 B1 任务**。
> 证据（都在本机文件系统上，可复核）：
>
> | 证据 | 说明 |
> |---|---|
> | `sim/results/b1/b1_20261007T125547Z/attempt3/` 在 **13:19:27Z** 被创建 | 我这一轮**没有**创建 attempt3 |
> | `attempt3/` 里出现 `PREP_NOTES.md`、`cloud_identity.txt`、`cloud_meta_{A,B}.log` | 这些文件不是我写的 |
> | `sim/k8s_test/b1_metrics_delta.py` 在 **13:16** 出现 | 我没写过这个文件 |
> | `sim/k8s_test/run_b1.sh` 在 **13:19:26** 被改成 57,176 字节 | 我写的那版只有约 27.6 KB；现在的版本里有 `HF_HUB_DISABLE_XET`、`MODEL_WAIT_MIN`、`kv_delta_measure_*` 等我从未写过的内容 |
> | `sim/k8s_test/B1_PLAN.md` 从 40,890 字节涨到 59,776 字节（13:19） | 我**没有被授权**改这个文件，也没有改过 |
> | B 机（8.130.179.201）上有一个**不是我起的** `hf download` 在跑（PID 6952，13:14 起） | 我从未在 B 上起过任何下载 |
>
> **处置：我停止了一切会改动共用状态的动作。**
> 两台机器是**有状态的共用资源**（同一批端口 8100/8192/8200/8300/5600、同一个模型目录
> `/root/b1/models`），两个代理同时跑会互相污染，而且会烧掉租卡的钱。
> 所以本文件只记录**在我停手之前已经完成、且可复核**的准备阶段证据。
>
> **我离开时两台的远端状态**（已核对，我没有留下任何进程）：
>
> | 机器 | 我留下的进程 | B1 相关端口 | 显存 | 说明 |
> |---|---|---|---|---|
> | A `8.130.72.109` | **无** | **全部未监听** | **0 MiB** | 我起的下载已被我自己显式 kill 掉 |
> | B `8.130.179.201` | **无** | **全部未监听** | **0 MiB** | B 上那个 `hf download` 是**另一个代理**的，我**没有**去动它 |
>
> A 上 `/tmp` 里留有我的一次性文件（`b1_client.py`、`b1_remote_serve.sh`、`model_api.json`、
> `portprobe` 日志等），都是无害的临时文件；`/root/b1/logs/model_download.log` 是我那一轮
> 被中断的下载留下的日志（8 行）。

---

## 1. 两台机器（实测，来自 `inventory_A.log` / `inventory_B.log`）

| | A（前处理） | B（生成） |
|---|---|---|
| 内网 IP | `172.16.206.251/20`（网卡 `eth0`，**只有这一块真网卡**） | `172.16.206.252/20` |
| 系统 | Ubuntu 24.04.4 LTS | 同左 |
| GPU | NVIDIA A10，23028 MiB，算力 **8.6** | 同左 |
| 驱动 | 580.126.09 | 580.126.09 |
| CPU / 内存 | 16 vCPU / 58 GiB | 同左 |
| CUDA toolkit | `/usr/local/cuda-13.0`、`/usr/local/cuda-12.8`（预装） | 同左 |
| `nvcc` | **不在 PATH**（只有 `/usr/bin/gcc`；`ninja`、`iperf3` 都没有） | 同左 |
| Docker | 29.5.1（本次**没用**） | 同左 |

## 2. 内网（KV 必须走这条）—— 实测

- A→B `ping`：5 发 5 收、**丢包 0**，RTT 平均 **0.168 ms**（min 0.131 / max 0.252）。
- **安全组允许内网任意端口**：在 B 上真起监听（5600 / 8200 / 8300 / 9101），
  从 A 连过去**四个端口全部可达** → `net_probe_from_A.log`。
- **公网 IP 上同样端口不可达**（`5600`、`9101` 都不可达）。
  两条合起来说明"KV 走内网"是客观事实，不是我们的配置意愿。
- 注意：**只测"有没有监听"会误判**。第一次探测时因为还没有服务在听，
  5600/8200/8300 全报"不可达"，看起来像被防火墙挡了——所以必须**真起监听再连**。

## 3. 时钟（本项目核心自变量是"状态有多旧"，时钟不可信就什么都别谈）

两端都用 chrony 同步到云内 NTP `100.100.61.88`，`Stratum 3`：

| | A | B |
|---|---|---|
| `System clock synchronized` | yes | yes |
| 与 NTP 的偏差 | **+0.0000987 s** | **+0.0000162 s** |
| RMS 偏差 | 0.000408 s | 0.000300 s |

→ 两端时钟偏差 **0.1 毫秒量级**，远小于本项目最小状态年龄档位（1 s）的 1/10。

## 4. 依赖安装（走 pip + 阿里云**内网**镜像，没有用 Docker）

| 项 | 实测 |
|---|---|
| 镜像源 | `http://mirrors.cloud.aliyuncs.com/pypi/simple/`（两台**默认**源，区域内网；公网 `pypi.org` 也通） |
| Docker Hub | `registry-1.docker.io` **无响应**；国内加速器 `docker.m.daocloud.io`、`docker.1ms.run` 的 `/v2/` 返回 401（可达），但**镜像体积**这道门槛没过——磁盘只剩 67 G，官方镜像通常 20–30 G，再叠 15 G 模型就危险了 |
| 装了 | `vllm==0.31.0` + `nixl==1.4.1` |
| NIXL 版本依据 | vLLM **v0.31.0 tag** 自带的 `requirements/kv_connectors.txt` 原文就是 `nixl == 1.4.1`（不是我们拍的） |
| 装完占用 | venv **8.7 G**；A 的可用磁盘 67 G → 54 G |
| 实际版本 | torch **2.13.0+cu130**（`cuda.is_available()=True`，认到 A10 / 算力 8.6）、transformers 5.17.0、flashinfer-python 0.7.0.post1、nixl-cu12/cu13 各 1.4.1、fastapi 0.136.3 / uvicorn 0.54.0 / httpx 0.28.1（代理要用，vLLM 依赖里已带） |

**没有踩到本机那 5 个坑的前 3 个**（CUDA toolkit 与 gcc 都预装了）；
第 4、5 个（`ninja`、FlashInfer 采样内核）**预防性处理**：
服务启动统一设 `VLLM_USE_FLASHINFER_SAMPLER=0`（两端逐字相同）。

## 5. ★ 一个会让人白等 3 小时的实测发现：**公网出口被限到约 10 Mbps**

| 测法 | 结果 |
|---|---|
| 单连接从 hf-mirror 拉 50 MB | **1.20 MB/s** |
| 单连接从 modelscope 拉 50 MB | **1.24 MB/s** |
| **4 路并发**（同一源，各 25 MB） | 聚合仍只有 **1.24 MB/s** |
| 走内网 aliyun 镜像装 pip 包（8.7 GB） | 约 **27 MB/s** |

**结论：不是服务端慢、也不是连接数不够，而是这两台机器的公网出口被限在 ≈10 Mbps。**
所以：

- 单机下 `Qwen2.5-7B-Instruct`（分片合计 **15,231,271,888 字节 ≈ 14.19 GiB**）要 **3.5–5 小时**；
- **两台各下一半、再走内网交换**能把墙钟时间砍到大约一半
  （内网实测 0.15 ms RTT、任意端口可达，交换这条路是通的）。
- 各分片的精确字节数（来自 hf-mirror 的模型 API `?blobs=true`）：

| 文件 | 字节 |
|---|---|
| `model-00001-of-00004.safetensors` | 3,945,441,440 |
| `model-00002-of-00004.safetensors` | 3,864,726,352 |
| `model-00003-of-00004.safetensors` | 3,864,726,424 |
| `model-00004-of-00004.safetensors` | 3,556,377,672 |
| 其余（config / tokenizer / merges / vocab / index / LICENSE / README） | 约 11 MB |

- 顺带记两条网络事实：`github.com` 直连不通、`hf-mirror.com` 与 `modelscope.cn` 都通
  （所以官方脚本是经 `gh-proxy.com` 反代取的 v0.31.0 tag 原文）。

## 6. 官方用法（v0.31.0 **按 tag 原文核对**，不是照抄别的版本）

- 前处理：`vllm serve <model> --port 8100 --enforce-eager --kv-transfer-config
  '{"kv_connector":"NixlConnector","kv_role":"kv_producer","kv_load_failure_policy":"fail"}'`
- 生成：同上去掉 `kv_role` 换成 `"kv_consumer"`，`--port 8200`
- **代理在 v0.31.0 里位于 `tests/v1/kv_connector/nixl_integration/toy_proxy_server.py`**
  （**不在 `examples/online_serving/` 下**——按别的版本抄这个路径会 404，我实测过）
- 跨机必须设 `VLLM_NIXL_SIDE_CHANNEL_HOST=<本机内网 IP>`（默认 `localhost`），
  `VLLM_NIXL_SIDE_CHANNEL_PORT=5600`；`kv_both` 已被官方弃用
- 官方 Prometheus 指标名（v0.31.0 文档原文）：
  `vllm:nixl_bytes_transferred`、`vllm:nixl_xfer_time_seconds`、`vllm:nixl_post_time_seconds`、
  `vllm:nixl_num_descriptors`（这四个是 **Histogram**）、
  `vllm:nixl_num_failed_transfers`、`vllm:nixl_num_failed_notifications`、
  `vllm:nixl_num_kv_expired_reqs`（这三个是 **Counter**）

## 7. 我**没有**做的（照实写，免得被当成做过了）

- **没有**在本轮起过任何 vLLM 服务、代理或对照臂；
- **没有**发出过任何测量请求（所以从记录纪律 R3/R4 看，本轮**始终"未开测"**）；
- **没有**在租到的卡上抓过 `/metrics`（这一步被我停手打断，见 §8）；
- **没有**做过 4a/4b/4c/4d 的任何一条结论；
- 由于并行代理在跑，`run_b1.sh` 现在是**它的版本**，我写的版本已被覆盖。

## 8. 被并行代理打断在什么地方

时间线（UTC）：

| 时刻 | 事件 |
|---|---|
| 12:55:47 | 我建 run `b1_20261007T125547Z`（attempt1） |
| 12:56 | attempt1：清点 + 网络 + 时钟 OK；`setup_install` 因**我的脚本把本地路径写成了远端路径**直接退出（未开测，保留记录） |
| 12:59:59 | attempt2 开始；修掉路径 bug 后 A、B 的 `setup_install` 均 **exit 0** |
| 13:08:30 | 官方脚本取齐（`toy_proxy_server.py` 等 4 个文件，200 + 非零字节） |
| 13:09–13:18 | 我起了一个**单机全量** `hf download`（后发现公网限速 ≈10 Mbps，计划改成两台各一半） |
| ~13:18 | 我显式 kill 掉那个下载（`model_download` 记为 **FAIL**，按纪律保留） |
| **13:19:27** | **另一个代理创建 attempt3，接管远端** |

**我停手的原因**：两台机器是有状态的共用资源，同一个模型目录、同一批端口。
两个代理同时推进只会互相污染，还会继续烧租卡的钱。这不是"提前放弃"，
而是避免把一次本来能做成的实验搅坏。

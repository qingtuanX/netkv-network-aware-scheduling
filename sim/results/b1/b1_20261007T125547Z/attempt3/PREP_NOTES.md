# B1 attempt3 —— 脚本接管之前的探查记录（照实记，不美化）

> 本文件记录 **2026-10-07 13:05Z–13:20Z** 这段"上一次运行被叫停之后"的现场探查。
> 它不属于 `run_b1.sh` 的任何阶段，所以单独留档；`record.csv` 里从 attempt3 的 `init` 开始。
> **原则：照实记录，不盲目重下；失败与遗留原样保留。**

## 1. 上一次运行留下了什么（现场清点）

| 项 | 机器 A | 机器 B |
|---|---|---|
| 系统重启过 | 是（本次会话看到的 uptime 只有十几分钟） | 是 |
| `/root/b1/venv` | **已装完**（404 个包；vllm 0.31.0、nixl 1.4.1、torch 2.13.0+cu130，`torch.cuda.is_available()=True`，识别到 A10） | 会话开始时**正在装**（`pip install vllm==0.31.0` 在跑，PID 5373）；到 **13:08Z 左右装完**，`requirements_locked.txt` 已生成 |
| `/root/b1/requirements_locked.txt` | 有（sha256 `…`，见 collect 阶段拉回的文件） | 有（sha256 `e5fedd97089a156420b4434858b2c27b3e5a6177ca5d5e6221888e725b2b47f9`） |
| `/root/b1/models/` | 空 | 空 |
| Docker 镜像 | 无（`docker images` 为空） | 无 |
| **监听端口** | 只有 sshd(22) —— **没有遗留服务** | 只有 sshd(22) —— **没有遗留服务** |
| **显存占用** | **0 MiB** | **0 MiB** |
| 遗留的日志 | `/root/b1/logs/` 空 | `/tmp/b1_portprobe.log` 等端口探测残留（无害） |

**结论**：没有遗留的**服务**需要先停；遗留的是**依赖环境**（两台都装好了），直接复用，不重装。
**注意**：B 端那个 `pip install` 是上一次运行被叫停后**在远端继续跑**的孤儿进程，它把包装完了 —— 这属于"捡回来的成果"，已按上面照实记录。

## 2. 模型下载：一次真实的失败（**必须留档，不许覆盖**）

第一次在两端同时起 `hf download` 之后：

- **A 端：正常下载**（走的是普通 HTTP 路径）。
- **B 端：崩了**，报错原文（`/root/b1/logs/model_download_XETFAIL.log`）：

```
RuntimeError: Task error: File reconstruction error: CAS Client Error:
Request error: HTTP status client error (401 Unauthorized), domain:
https://cas-server.xethub.hf.co/v2/reconstructions/7a5652df...
```

**原因**：HuggingFace 的 **Xet** 存储后端。`hf-mirror.com` 只镜像了普通 HTTP 下载路径，
**没有代理 Xet**，于是客户端被重定向到 `cas-server.xethub.hf.co`，在未登录的情况下拿到 **401**。

**修法**：显式关掉 Xet —— `HF_HUB_DISABLE_XET=1`。已写进 `run_b1.sh` 的 `model_start()`，
并把 B 端那份失败的日志**改名保留**（`model_download_XETFAIL.log`），不删。

## 3. 一个会直接影响成本的实测发现：**公网入口带宽只有约 1 MB/s**

这是本轮最重要的成本发现。逐条实测（单流）：

| 源 | 实测速率 |
|---|---|
| `hf-mirror.com` | **0.89 MB/s** |
| `modelscope.cn`（Qwen 官方源） | **0.87 MB/s** |
| `mirrors.aliyun.com` | **0.61 MB/s** |
| 同一分片 **8 路并行** range 请求合计 | **≈0.58 MB/s**（**比单流更低**） |
| `mirrors.cloud.aliyuncs.com`（**内网**镜像，解析到 `100.100.2.148`） | **≈17 MB/s**（由 pip 4 分钟拉 4.1 GB 折算） |

**判读**：瓶颈在**实例侧的公网入口**，不是源站限速 —— 因为换三个互不相干的源都是同一个量级，
而且**加并发不涨反降**。

**后果**：15.2 GB 的 Qwen2.5-7B 要下 **约 4–5 小时**（实测 A 1.22 MB/s、B 0.89 MB/s），
**这段时间两台都在计费**。这条必须写进报告与方案的"成本口径"里。

## 4. 本轮做的判断（照实写，便于日后复核）

1. **复用已有 venv，不重装**（"不要盲目重下/重装"）。
2. **沿用已经在跑的两个下载**，不另起第二个进程（`hf` 用目录锁，重复起会互相等锁）。
   `run_b1.sh` 已加 `model_download_running()` 检测并记录。
3. **不做 Docker / 镜像**：本任务的 vLLM 是 pip venv 安装，所谓"镜像 digest"的等价物是
   `requirements_locked.txt` 的 sha256，已在 collect 阶段拉回。
4. **私钥只引用路径**：`/d/Documents/API/_网络感应.pem` 只作为 `ssh -i` 的参数出现，
   内容从未被打印、从未写入任何产物；收尾阶段有专门的泄漏扫描（`cleanup_check.txt`）。

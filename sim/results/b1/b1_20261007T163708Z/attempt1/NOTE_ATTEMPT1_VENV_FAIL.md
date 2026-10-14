# attempt1：env 阶段安装失败（记录留档，**不删、不覆盖**）

- run_id：`b1_20261007T163708Z`　attempt：**1**
- 失败时刻（UTC）：2026-10-07T16:37:41Z（A）/ 16:37:43Z（B）
- 失败项：**建 Python 虚拟环境（venv）**
- 结论：这一次尝试**只跑到 env 阶段就停下**，后面 model / baseline / precheck / measure / compare
  一律**没有执行**。按记录纪律 R3，这属于"**还没开测**"——从没有发出过任何一条正式测量请求，
  也不存在任何 B1 服务被启动过。修好后换 `ATTEMPT=2` 重跑（旧目录原样保留）。

## 报错原文（两端相同）

```
ensurepip is not available.  On Debian/Ubuntu systems, you need to install the python3-venv
package using the following command.

    apt install python3.12-venv

You may need to use sudo with that command.  After installing the python3-venv
package, recreate your virtual environment.

Failing command: /root/b1/venv/bin/python3
```

随后每一处都报（因为 venv 没建成）：

```
main: line 50: /root/b1/venv/bin/pip: No such file or directory
main: line 34: /root/b1/venv/bin/pip: No such file or directory
main: line 62: /root/b1/venv/bin/vllm: No such file or directory
ModuleNotFoundError: No module named 'torch'
```

## 根因（在实例内实测确认，不是猜）

| 检查 | 结果 |
|---|---|
| `python3 -c "import ensurepip"` | ❌ `ModuleNotFoundError: No module named 'ensurepip'` |
| `ls /usr/lib/python3.12/ensurepip` | ❌ 不存在 |
| `dpkg -l python3.12-venv` | `un`（**未安装**） |
| `dpkg -l python3-virtualenv` | ✅ `ii python3-virtualenv 20.25.0+ds-2`（**已经装了**） |
| `python3 -m pip --version` | ✅ `pip 24.0 from /usr/lib/python3/dist-packages/pip (python 3.12)` |
| 遗留的半成品 venv | `/root/b1/venv/{bin,include,lib}`（32 KB 空壳） |

## 修法（改的是 `b1_remote_setup.sh`，**没有装任何系统包**）

1. 判据改成"`bin/python` **和** `bin/pip` 都在"——半成品 venv 也会被识别成不可用并重建；
2. 先试 `python3 -m venv`；失败就 **`rm -rf` 后改用镜像里本来就有的 `/usr/bin/virtualenv`**；
3. 新增"安装结果自检"：`vllm --version`、`import nixl`、`torch.cuda.is_available()`
   三条任一不过就 `exit 1`。**之所以必须加**：本轮第一次运行时 venv 明明失败，
   脚本却以 `rc=0` 结束、`run_b1.sh` 打印"A 装好了 / B 装好了"——
   **这是"静默成功"型错误**，直到下一个阶段才发现什么都没装。

> ⚠️ 这一条要写进报告：**`b1_remote_setup.sh` 的 `install` 在 venv 失败时会静默返回 0**
> 是本次才暴露的设计缺陷（不是新机器的错），已按上面第 3 点补上硬判据。

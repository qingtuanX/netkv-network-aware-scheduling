"""真实系统实测 —— 同一个调度问题，跑到真进程/真容器上，只比"趋势"不比绝对值。

为什么要有这个文件：
    论文的全部主结论来自仿真。要让人相信仿真的**趋势**不是编出来的，就必须在一个
    真实运行的系统上把同样的对照再做一遍。`论文规划.md` §7 明确要求：单节点
    kind/k3d 只用于**趋势对照**，不用于宣称生产级性能。

    【适用边界，写正文时必须照抄】
      · 单节点（WSL2 里一个 k3s，或本机多进程），不是多机集群；
      · worker 的工作量是 CPU 忙等，**不是 GPU 推理**；"算力"是抽象单元；
      · 网络代价默认是**真实字节收发**（见下），可以叠加内核 `tc` 整形；
        想复现"应用层按带宽睡眠"的老行为要显式加 `--fake-transfer`；
      · 因此本实验只能验证"哪个策略在哪种条件下更好"的**方向**，
        不能验证绝对延迟、吞吐或尾部分位数的具体数值。

【2026-10-05 协议变更：从"假装传输"改成"真实字节收发"】
    老协议里 POST /work 的 body 是一段 JSON，worker 读到 `transfer_mb` 之后
    **time.sleep(mb/带宽)**——传输是假装的，只占时间不占线路。这使
    `真实跨云LLM服务研究计划书.md` §5 阶段 A 无法通过（"必须改掉后才能用于本阶段"）。
    新协议：

      请求  HTTP 头 X-Service-Units / X-Upload-Bytes / X-Download-Bytes
            body = X-Upload-Bytes 个**真实字节**（＝从 A 传到 B 的 KV 缓存占位）
      worker 按 256 KiB 分块把 body **真的读完并逐块计数**，再忙等
            service_units/compute 秒（阶段 A 里"算力"仍是 CPU 占位）
      响应  body = X-Download-Bytes 个**真实字节**；worker 自己的实测值
            （收到/发出的字节数、各阶段耗时、载荷校验和）放**响应头**回传

    【为什么客户端必须用 http.client 而不是 urllib.request】urllib 会把整个请求体
    先缓冲、再把响应一次读完，"上传耗时"与"下传耗时"的分界被抹掉；而这两段正好是
    阶段 A 要分别测量的对象。http.client 允许 putrequest/putheader/分块 send/
    分块读，才能各自计时。
    【为什么载荷不用 os.urandom】200 MB 随机数要花几秒 CPU，那会被算进"上传耗时"；
    这里用一块 1 MiB 的固定缓冲反复复用，生成成本≈0。
    【为什么要记网卡计数器与 tc 统计】应用层测的字节数是"我以为传了多少"，
    网卡计数器和 `tc -s` 的 overlimits 是**内核侧的独立见证**，两者对得上才叫证据。

它有 5 个角色（同一份代码，用 --role 选）：
    worker     一个"算力节点"：HTTP 服务，真实收完上传载荷 → CPU 忙等 →
               真实写出下传载荷；`/state` 暴露队列深度与积压工作量。
    scheduler  调度器：给每个请求选一个节点。两种策略：
                 least_load  —— 只看算力队列（对应论文的 LeastLoad）
                 contention  —— 算力积压 + 链路积压 + 本请求传输时间（对应 NetworkAware2）
               可用 --state-lag 注入"状态过期"（调度器看到的是 N 秒前的快照）。
    client     负载发生器：泊松到达，逐请求记录端到端延迟与各阶段耗时，输出 CSV。
    selftest   不起进程，只做一次自检（策略打分函数的边界与门 2 等价性）。
    transfer   **阶段 A 的验证电池**：不涉及调度器、不涉及 LLM、不涉及 GPU。
               依次跑字节一致性 / 限速吞吐 / overlimits 增量 / 单路 vs 多路 /
               网卡计数器比值，逐项给 PASS/FAIL。

用法（本机多进程模式，不起容器）：
    python measure_system.py selftest
    python measure_system.py worker --port 9101 --compute 4.0 --bandwidth-mbps 100 &
    python measure_system.py worker --port 9102 --compute 2.0 --bandwidth-mbps 500 &
    python measure_system.py client --workers 127.0.0.1:9101,127.0.0.1:9102 \
        --policy least_load --qps 8 --requests 600 --out results/k8s_test/local_leastload.csv

用法（k3s 模式）：worker 跑在 Pod 里（见 k8s/workers.yaml），client/scheduler 在 WSL 里跑，
    --workers 填各个 Service 的地址。

用法（阶段 A 验证电池；只有一台机器也能跑完）：
    # 单机自足：电池自己起一个进程内 worker
    python3 measure_system.py transfer --sizes-mb 0.5 8 200
    # 两台"主机"（WSL2 network namespace，见 MULTIHOST.md §1.1）：
    #   hostB 之外先跑服务端（root namespace 的 10.250.0.1）
    python3 measure_system.py worker --bind 10.250.0.1 --port 9201 --compute 1e6
    #   再进 hostB 跑电池（那里能看到被 tc 整形的 vethB）
    ip netns exec hostB python3 measure_system.py transfer \
        --target 10.250.0.1:9201 --iface vethB --tc-rate-mbps 25

产物：
    results/k8s_test/*.csv            client 角色：每个 (策略,陈旧度) 一个 CSV（每请求一行）
    results/stage_a/transfer_battery_<run_id>.{csv,json}
                                      transfer 角色：逐项检查 + 环境与 tc 原文快照
"""

from __future__ import annotations

import argparse
import csv
import http.client
import json
import math
import os
import platform
import random
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor, wait as futures_wait
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_STATE_TTL = 0.05          # 调度器最短刷新间隔（秒）

# --- 真实字节收发用到的常量 ---------------------------------------------------
# 本文件把所有 "MB" 一律按 10^6 字节算：老代码的 `mb / (bw_mbps/8.0)` 隐含的就是
# Mbit/s ÷ 8 = MB/s（两边同用十进制），换单位会让既有的 transfer_mb 语义漂移。
MB = 1_000_000
# 载荷分块大小。太大（几 MB）会让"上传/下传的分界"被 socket 缓冲抹平；
# 太小（几 KB）会让 syscall 次数暴增、测量被系统调用开销主导。256 KiB 是折中。
CHUNK_BYTES = 256 * 1024
# 首个分块用这个小尺寸，原因见 consume_body() 的注释（会影响吞吐的高估/低估方向）。
FIRST_READ_BYTES = 16 * 1024
# 1 MiB 固定缓冲，反复复用。bytes(range(256))*4096 恰好 1 MiB，且瞬间生成。
PAYLOAD_SRC_BYTES = 1024 * 1024
_PAYLOAD_BLOCK = bytes(range(256)) * (PAYLOAD_SRC_BYTES // 256)
assert len(_PAYLOAD_BLOCK) == PAYLOAD_SRC_BYTES

# Windows 控制台默认可能是 GBK，中文与 ✓/✗ 会直接抛 UnicodeEncodeError。
# 本仓库每个入口脚本都必须有这一段（AGENTS.md §10 第 1 条）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 0. 真实字节收发的公共工具（负载生成 / 逐块收发 / 环境探测）
# ---------------------------------------------------------------------------
def payload_chunks(total: int, chunk: int = CHUNK_BYTES):
    """生成 total 字节的载荷，块大小 ≤ chunk，内容来自复用的 1 MiB 缓冲。

    【为什么用生成器而不是先拼一个 bytes】200 MB 的载荷拼成一个对象要 200 MB 内存，
    而"上传"本来就是把同一块数据反复发出去；生成器让内存占用与载荷大小无关。
    【为什么不 per-request 随机化内容】内容是占位（KV 缓存的替身），
    重要的是**字节数与过线事实**；随机化只会把 CPU 时间混进"上传耗时"。
    """
    left = max(int(total), 0)
    while left > 0:
        block = min(chunk, left)
        off, remaining = 0, block
        while remaining > 0:
            take = min(remaining, PAYLOAD_SRC_BYTES - off)
            yield _PAYLOAD_BLOCK[off:off + take]
            off += take
            remaining -= take
        left -= block


def payload_crc(total: int, chunk: int = CHUNK_BYTES) -> int:
    """载荷内容的 CRC32。

    【为什么在计时区之外先算好】"字节数对得上"只证明数量对，不证明内容对；
    加一个校验和才能排除"读到的其实是上一轮的残留"。而逐字节算 CRC 本身要 CPU，
    所以必须在 t_upload_start 之前算完（载荷内容是确定的，可以预先算）。
    """
    h = 0
    for c in payload_chunks(total, chunk):
        h = zlib.crc32(c, h)
    return h


def consume_body(rfile, n: int, chunk: int = CHUNK_BYTES, want_crc: bool = False
                 ) -> tuple[int, float | None, float, int | None]:
    """把 body 读完并**逐块计数**，返回 (实际读到的字节数, 首块到达时刻, 读完时刻, crc)。

    【为什么不能只信 Content-Length】这正是阶段 A 的第一条通过条件：
    声明的字节数必须等于**真的过线**的字节数。只读头部就等于什么都没验。
    【为什么第一块要用 read1】BufferedReader.read(n) 会一直等到攒满 n 字节才返回，
    于是"首块到达时刻"被推迟了整整一块的时间（256 KiB @25 Mbit/s ≈ 84 ms，
    是 250 ms 吞吐容差里的三分之一）。read1 只要有一点点数据就返回，时刻才准。
    """
    got, t_first, h = 0, None, 0
    left = max(int(n), 0)
    while left > 0:
        data = (rfile.read1(min(FIRST_READ_BYTES, left)) if t_first is None
                else rfile.read(min(chunk, left)))
        if not data:
            break
        if t_first is None:
            t_first = time.perf_counter()
        got += len(data)
        if want_crc:
            h = zlib.crc32(data, h)
        left -= len(data)
    return got, t_first, time.perf_counter(), (h if want_crc else None)


def read_exact(rfile, n: int, chunk: int = CHUNK_BYTES) -> bytes:
    """小载荷用的"原样读完"（假传输分支要 JSON 内容，不能丢弃）。"""
    buf = bytearray()
    left = max(int(n), 0)
    while left > 0:
        data = rfile.read(min(chunk, left))
        if not data:
            break
        buf += data
        left -= len(data)
    return bytes(buf)


def _read_sysfs_int(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def read_nic_counters(iface: str | None) -> dict:
    """读网卡累计收发字节数（Linux sysfs `/sys/class/net/<iface>/statistics/`）。

    【为什么需要它】应用层数出来的字节数是"我以为传了多少"；网卡计数器是内核侧的
    独立见证。两者比值稳定在 1.02–1.06 说明只有 TCP/IP 头开销；若远大于 1，
    说明发生了大量重传，那批数据的吞吐不能引用。这是应用层测量唯一的交叉校验点。
    """
    if not iface:
        return {}
    base = Path("/sys/class/net") / iface / "statistics"
    out: dict = {"iface": iface}
    for key in ("rx_bytes", "tx_bytes"):
        v = _read_sysfs_int(base / key)
        if v is None:
            return {"iface": iface, "error": f"读不到 {base / key}（网卡名写错了？）"}
        out[key] = v
    return out


def _default_route_iface() -> str | None:
    """`/proc/net/route` 里 Destination 为 00000000 的那条即默认路由，第 1 列是网卡名。"""
    try:
        with open("/proc/net/route", encoding="utf-8") as f:
            for line in f.readlines()[1:]:
                p = line.split()
                if len(p) >= 2 and p[1] == "00000000":
                    return p[0]
    except OSError:
        return None
    return None


def detect_iface(target_ip: str | None = None, explicit: str | None = None) -> str | None:
    """确定统计用的网卡：显式指定 > 到目标 IP 的路由 > 默认路由。

    【为什么不能只查默认路由】阶段 A 的"第二台主机"是 network namespace，
    里面**没有默认路由**（只有 10.250.0.0/24 一条，见 MULTIHOST.md §1.1），
    只查默认路由会返回 None，网卡计数器这一项就整块失效了。
    """
    if explicit:
        return explicit
    if shutil.which("ip"):
        args = (["ip", "-o", "route", "get", target_ip] if target_ip
                else ["ip", "-o", "route", "show", "default"])
        try:
            r = subprocess.run(args, capture_output=True, text=True, timeout=5)
            m = re.search(r"\bdev\s+(\S+)", r.stdout or "")
            if m:
                return m.group(1)
        except Exception:
            pass
    return _default_route_iface()


TC_STAT_RE = {"overlimits": re.compile(r"overlimits\s+(\d+)"),
              "dropped": re.compile(r"dropped\s+(\d+)")}


def read_tc_stats(cmd: str | None) -> dict:
    """执行 `tc -s qdisc show ...` 并把 overlimits / dropped 累加出来。

    【为什么必须读这个】"限速生效了"的判据不是 `tc qdisc show` 有输出
    （那只说明规则被加上了），而是 ①实测吞吐 ≈ 设定值 ②`overlimits` 增量 > 0
    （整形器真的把包拦下来排过队）。只看 ① 有个漏洞：链路本来就慢时，
    "限速生效"与"没限速也一样快"无法区分。
    【为什么要累加而不是取第一条】一个接口上常常叠着多个 qdisc
    （tbf 下面挂 netem，见 MULTIHOST.md §1.1），统计分散在多行里。
    """
    if not cmd:
        return {}
    out: dict = {"cmd": cmd}
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
    except Exception as e:
        return {"cmd": cmd, "error": f"{type(e).__name__}: {e}"}
    text = (r.stdout or "") + (r.stderr or "")
    for key, rx in TC_STAT_RE.items():
        out[key] = sum(int(m) for m in rx.findall(text))
    out["rc"] = r.returncode
    out["raw"] = (r.stdout or "").strip()[:2000]
    return out


def tc_snapshot_cmd(iface: str | None, override: str | None) -> str | None:
    """构造读取 tc 统计的命令。override 用于 netns 场景（`ip netns exec hostB tc ...`）。"""
    if override:
        return override
    if iface and shutil.which("tc"):
        return f"tc -s qdisc show dev {iface}"
    return None


def busy_wait(seconds: float) -> None:
    """忙等（不用 sleep），避免被调度误差污染"算力"这一段的时间。

    【为什么故意烧 CPU】阶段 A 里"算力"仍是 CPU 占位；用 sleep 的话时间会被
    CPU 竞争拉长，与"节点算得快慢"的直觉对不上。代价是忙等线程会与网络线程抢
    同一个 CPU，这一点在 Threats 里要照实写。
    """
    end = time.perf_counter() + max(seconds, 0.0)
    x = 1.0
    while time.perf_counter() < end:
        x = math.sqrt(x * 1.0000001 + 1.0)


def header_safe(v: str) -> str:
    """把任意字符串转成能放进 HTTP 头的 ASCII 形式。

    【为什么真踩到过】Windows 那台机器的主机名是中文（"青山"），把它直接塞进
    `X-Server-Node` 会让 worker 在 `send_header` 里抛 UnicodeEncodeError
    （HTTP 头只能 latin-1）。症状极具迷惑性：**客户端只看到
    `RemoteDisconnected: Remote end closed connection without response`**，
    真正的错误只在服务端 stderr 里，看起来像网络/防火墙问题。
    """
    return str(v).encode("ascii", "backslashreplace").decode("ascii")


def host_info() -> dict:
    """把"这批数字是在什么机器上量的"固定下来（内核版本是 tc 行为的判据之一）。"""
    info = {"python": sys.version.split()[0], "platform": platform.platform(),
            "system": platform.system(), "uname": " ".join(platform.uname())}
    try:
        # 【为什么两条都留】Linux 上要看内核版本（tc 行为随内核变），
        # Windows 上 system_release() 只是 "11"，光看它分不出到底是哪台机器。
        info["kernel"] = platform.release()
        info["os_version"] = platform.version()
    except Exception:
        pass
    return info


def _r6(v) -> float | None:
    """CSV 落盘用：None 原样返回，数值保留 6 位小数（毫秒级分辨）。"""
    try:
        return None if v is None else round(float(v), 6)
    except (TypeError, ValueError):
        return None


def _nanf(v) -> float:
    """把 None 统一成 NaN——CSV 里 None 会写成空串，下游分不出"没测到"和"没这一列"。"""
    try:
        return float("nan") if v is None else float(v)
    except (TypeError, ValueError):
        return float("nan")


# ---------------------------------------------------------------------------
# 角色 1：worker（一个"算力节点"）
# ---------------------------------------------------------------------------
class WorkerState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.inflight = 0                 # 正在处理的请求数（含排队）
        self.backlog_work = 0.0           # 排队+在算的工作量（service_units 秒为单位）
        self.link_backlog_mb = 0.0        # 已派给本节点、还没传完的字节数（MB）
        self.done = 0
        self.busy_s = 0.0
        # 真实字节收发的累计量（/state 里暴露，用于核对"这一轮到底过了多少字节"）
        self.recv_bytes = 0
        self.sent_bytes = 0


def make_worker_handler(state: WorkerState, compute: float, bw_mbps: float,
                        force_fake: bool = False, node_id: str = ""):
    """compute = 相对算力（>=1 表示该节点处理同样工作量需要 1/compute 的时间）；
    bw_mbps = 该节点链路的带宽档位（MB/秒 = bw_mbps/8）。只被 --fake-transfer 用到，
    因为真实传输时"有多快"由内核 tc 与真实网卡决定，不再由这个系数决定。

    force_fake=True 时**无条件**走老协议（按 transfer_mb 睡眠），作为对照臂：
    它让我们能回答 Threats 里那个问题——"应用层模拟 vs 真实内核限速差多少"。
    注意即便如此也**必须把 body 读完**（HTTP/1.1 的分帧靠 Content-Length，
    剩下的字节留在 socket 里会把下一个请求的请求行吃掉）。
    """

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        # 【为什么关 Nagle】响应头与响应体是两次 write，Nagle 会把第二段攒起来等 ACK，
        # 客户端 getresponse() 返回的时刻就不再等于"服务端开始下传"的时刻了。
        disable_nagle_algorithm = True

        def log_message(self, *a):        # 静音
            pass

        def _json(self, obj, code=200, extra: dict | None = None):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, str(v))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith("/state"):
                with state.lock:
                    self._json({"inflight": state.inflight,
                                "backlog_work": state.backlog_work,
                                "link_backlog_mb": state.link_backlog_mb,
                                "done": state.done, "busy_s": round(state.busy_s, 3),
                                "recv_bytes": state.recv_bytes,
                                "sent_bytes": state.sent_bytes,
                                "compute": compute, "bw_mbps": bw_mbps})
            elif self.path.startswith("/health"):
                self._json({"ok": True})
            else:
                self._json({"err": "not found"}, 404)

        def do_POST(self):
            if not self.path.startswith("/work"):
                self._json({"err": "not found"}, 404)
                return
            n_body = int(self.headers.get("Content-Length") or 0)
            want_real = self.headers.get("X-Upload-Bytes") is not None
            mode = "real" if (want_real and not force_fake) else "fake"
            t0 = time.perf_counter()
            if mode == "real":
                upload_bytes = int(self.headers.get("X-Upload-Bytes") or 0)
                download_bytes = int(self.headers.get("X-Download-Bytes") or 0)
                units = float(self.headers.get("X-Service-Units") or 0.0)
            else:
                upload_bytes = download_bytes = 0
                units = 0.0
            with state.lock:
                state.inflight += 1
                state.backlog_work += units
                state.link_backlog_mb += (upload_bytes + download_bytes) / MB

            if mode == "real":
                # 阶段 1：真实收上传载荷。**逐块计数**，不信 Content-Length。
                want_crc = self.headers.get("X-Payload-Crc") is not None
                got, t_recv_first, t_recv_end, crc = consume_body(
                    self.rfile, n_body, want_crc=want_crc)
                with state.lock:
                    state.recv_bytes += got
                    state.link_backlog_mb = max(
                        0.0, state.link_backlog_mb - upload_bytes / MB)
                # 阶段 2：CPU 工作（忙等，避免被 sleep 的调度误差污染）
                service_s = units / max(compute, 1e-9)
                t_svc_start = time.perf_counter()
                busy_wait(service_s)
                t_svc_end = time.perf_counter()
                # 阶段 3：真实下发载荷。分块写并计数，写完 flush。
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(download_bytes))
                self.send_header("Connection", "close")
                hdrs = {
                    "X-Mode": "real",
                    "X-Received-Bytes": got,
                    "X-Declared-Bytes": n_body,
                    "X-Sent-Bytes": download_bytes,
                    "X-Server-Node": header_safe(node_id),
                    "X-Recv-First-S": f"{(t_recv_first or t0) - t0:.9f}",
                    "X-Recv-S": f"{t_recv_end - (t_recv_first or t0):.9f}",
                    "X-Recv-Total-S": f"{t_recv_end - t0:.9f}",
                    "X-Service-S": f"{t_svc_end - t_svc_start:.9f}",
                }
                if crc is not None:
                    hdrs["X-Payload-Crc"] = crc
                for k, v in hdrs.items():
                    self.send_header(k, v)
                self.end_headers()
                t_send_start = time.perf_counter()
                sent = 0
                try:
                    for c in payload_chunks(download_bytes):
                        self.wfile.write(c)
                        sent += len(c)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    # 客户端提前断开（超时/取消）不是错误，但会让 socketserver 打一堆
                    # traceback 混淆真正的失败原因；这里静默收尾并照实记账。
                    pass
                t_send_end = time.perf_counter()
                dt = t_send_end - t0
                with state.lock:
                    state.sent_bytes += sent
                    state.inflight -= 1
                    state.backlog_work = max(0.0, state.backlog_work - units)
                    state.link_backlog_mb = max(0.0, state.link_backlog_mb
                                                - download_bytes / MB)
                    state.done += 1
                    state.busy_s += dt
                return
                # noqa: 上面的 return 让 real 分支到此结束；下面全是 fake 分支

            # ---- 老协议（或 --fake-transfer 强制降级）：按带宽睡眠 ----
            raw = read_exact(self.rfile, n_body)
            t_recv_end = time.perf_counter()
            try:
                meta = json.loads(raw.decode() or "{}")
            except Exception:
                meta = {}
            if want_real and force_fake:
                # 客户端按真实协议发了载荷，但本 worker 被强制降级：
                # 睡眠时长按**实际过线的字节数**折算，这样对照臂的两个方向都动过真字节。
                mb = n_body / MB
                units = float(self.headers.get("X-Service-Units") or 0.0)
            else:
                mb = float(meta.get("transfer_mb", 1.0))
                units = float(meta.get("service_units", 50.0))
            with state.lock:
                state.recv_bytes += n_body
            transfer_s = mb / max(bw_mbps / 8.0, 1e-9)
            time.sleep(transfer_s)
            service_s = units / max(compute, 1e-9)
            busy_wait(service_s)
            dt = time.perf_counter() - t0
            with state.lock:
                state.inflight -= 1
                state.backlog_work = max(0.0, state.backlog_work - units)
                state.link_backlog_mb = max(0.0, state.link_backlog_mb - mb)
                state.done += 1
                state.busy_s += dt
            self._json({"done": True, "worker_time_s": dt,
                        "transfer_s": transfer_s, "service_s": service_s,
                        "mode": "fake", "server_worker_time_s": dt},
                       extra={"X-Mode": "fake",
                              "X-Received-Bytes": n_body,
                              "X-Sent-Bytes": 0,
                              "X-Service-S": f"{service_s:.9f}",
                              "X-Recv-Total-S": f"{t_recv_end - t0:.9f}"})

    return Handler


def start_http_server(bind: str, port: int, compute: float, bw_mbps: float,
                      force_fake: bool = False, node_id: str = ""
                      ) -> tuple[ThreadingHTTPServer, WorkerState]:
    """起一个 worker HTTP 服务（返回 server 对象，不阻塞）。

    【为什么单独抽出来】`transfer` 角色要能**在只有一台机器时**自足跑完电池，
    所以它需要在不 fork 进程的前提下起一个服务端；`run_worker` 也需要它。
    """
    state = WorkerState()
    # 【为什么要把 backlog 调大】socketserver 默认 request_queue_size=5；client 改成
    # 开环并发之后，同一瞬间可能有几十个连接进来，5 个的 backlog 会让一部分连接被
    # 直接拒绝（表现为 ok=0 的失败请求，与真实拥塞混在一起分不清）。
    ThreadingHTTPServer.request_queue_size = 128
    srv = ThreadingHTTPServer((bind, port),
                              make_worker_handler(state, compute, bw_mbps,
                                                  force_fake, node_id))
    return srv, state


def run_worker(port: int, compute: float, bw_mbps: float, bind: str = "0.0.0.0",
               force_fake: bool = False) -> int:
    node_id = platform.node() or "worker"
    srv, _ = start_http_server(bind, port, compute, bw_mbps, force_fake, node_id)
    print(f"[worker] {bind}:{port}  compute={compute}  bandwidth(coef)={bw_mbps} Mbps  "
          f"mode={'fake(--fake-transfer 强制降级)' if force_fake else 'real(真实字节)'}  "
          f"node={node_id}", flush=True)
    srv.serve_forever()
    return 0



# ---------------------------------------------------------------------------
# 角色 2：scheduler（两种策略 + 可注入的状态过期）
# ---------------------------------------------------------------------------
class Scheduler:
    """按策略给请求选节点。

    least_load ：score = 该节点的算力积压（backlog_work / compute）
    contention ：score = 算力积压 + 链路排空时间 + 本请求在该节点上的传输时间
                 （与仿真里的 NetworkAware2 同形：两条队列一起看）
    state_lag  ：>0 时，调度器看到的是 state_lag 秒前的快照（陈旧观测）。
    """

    def __init__(self, workers: list[str], policy: str, state_lag: float = 0.0,
                 state_ttl: float = DEFAULT_STATE_TTL):
        self.workers = workers
        self.policy = policy
        self.state_lag = state_lag
        self.state_ttl = state_ttl
        self._cache: dict[str, tuple[float, dict]] = {}   # url -> (取回时刻, 状态)
        self._history: list[tuple[float, dict[str, dict]]] = []
        self._lock = threading.Lock()
        # 埋点：拉取次数/总耗时/失败次数（用于报告"观测本身有多贵、有多旧"）
        self._fetch_calls = 0
        self._fetch_secs = 0.0
        self._fetch_fail = 0

    def _fetch(self, url: str) -> dict:
        """取一个节点的状态，并**记下这一次拉取花了多久**。

        为什么要记：调度器看到的状态"有多旧"，等于「轮询周期」+「拉取本身的耗时」。
        前者是我们设的，后者是**系统在被压的时候自己变长的**——满载时 worker 的
        HTTP 线程抢不到 CPU，连健康检查都会超时（k3s 事件里实测到过）。
        这构成一个反馈：越想看谁忙，看的时候越慢。仿真的"观测年龄=常数"缺少这一项。
        """
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(url + "/state", timeout=2.0) as r:
                st = json.loads(r.read())
            ok = True
        except Exception:
            st = {"inflight": 1e9, "backlog_work": 1e9, "link_backlog_mb": 1e9,
                  "compute": 1.0, "bw_mbps": 1.0}
            ok = False
        dt = time.perf_counter() - t0
        with self._lock:
            self._fetch_calls += 1
            self._fetch_secs += dt
            if not ok:
                self._fetch_fail += 1
        return st

    def snapshot(self) -> dict[str, dict]:
        now = time.perf_counter()
        with self._lock:
            need = [u for u in self.workers
                    if u not in self._cache or now - self._cache[u][0] > self.state_ttl]
        for u in need:
            st = self._fetch(u)
            with self._lock:
                self._cache[u] = (time.perf_counter(), st)
        with self._lock:
            snap = {u: st for u, (_, st) in self._cache.items()}
            self._history.append((now, snap))
            self.last_obs_time = now     # 这份快照的"出生时刻"，用于算观测年龄
            if len(self._history) > 4000:
                self._history = self._history[-2000:]
        return snap

    def fetch_stats(self) -> dict:
        """拉取统计：次数、失败次数、平均每次耗时（秒）。用于报告"观测本身的开销"。"""
        with self._lock:
            n = max(self._fetch_calls, 1)
            return {"n": self._fetch_calls, "fail": self._fetch_fail,
                    "mean_s": self._fetch_secs / n}

    def _aged_snapshot(self) -> dict[str, dict]:
        """取 state_lag 秒前的快照（陈旧观测）。

        【必须每次都先 snapshot() 再回看，否则陈旧观测会退化成"永远看预热那一条"】
        这条 if 曾经写成"lag>0 就直接在历史里找、不刷新历史"，后果是 `_history`
        永远只有预热时写入的那一条（全空载），于是调度器对每个请求都看到同一份
        "所有节点都空闲"的快照 → 打分并列 → 永远选同一个节点。
        实测症状：lag=3 的 4 次运行里 **240/240 个请求全打到同一个 worker**，
        单节点被压爆（p95 9–13 s），看起来像"陈旧很致命"，其实是代码 bug。
        """
        snap = self.snapshot()          # 先把当前状态写进历史
        if self.state_lag <= 0:
            return snap                 # last_obs_time 已由上面的 snapshot() 写好
        with self._lock:
            target = time.perf_counter() - self.state_lag
            pick, pick_t = None, None
            for t, s in reversed(self._history):
                if t <= target:
                    pick, pick_t = s, t
                    break
            if pick is None:            # 还没攒够 lag 秒的历史 → 用最早的那条
                pick_t = self._history[0][0] if self._history else None
                pick = self._history[0][1] if self._history else {}
        self.last_obs_time = pick_t or time.perf_counter()
        return pick or snap

    def obs_age(self) -> float:
        """这份"调度器看到的视图"有多旧（秒）。

        【定义必须是这个】= 现在 − 视图里**最旧**的那份数据的取回时刻。
        第一版写成了"距上次 snapshot() 调用过了多久"，结果 p50 恒为 0 ms——
        因为每个请求都会调用 snapshot()（哪怕没真的去拉数据，只是复用缓存），
        时间戳被无条件刷新了。这个 bug 会让"状态很陈旧"看起来像"状态很新鲜"，
        方向正好错，属于最危险的一类（已修正）。
        三项来源都在里面：①距上次真实轮询过了多久（≤ 一个 state_ttl）
        ②那次拉取本身花了多久（**随负载变长**）③我们故意注入的 lag。
        """
        now = time.perf_counter()
        if self.state_lag > 0:
            t = getattr(self, "last_obs_time", None)
            return (now - t) if t else float("nan")
        with self._lock:
            ts = [t for t, _ in self._cache.values()]
        return (now - min(ts)) if ts else float("nan")

    def _score(self, snap: dict, u: str, transfer_mb: float) -> tuple[float, float, float]:
        """给某个节点打分，并把它自报的 (compute, bw_mbps) 一并返回，便于记录身份。"""
        st = snap.get(u) or {"inflight": 0, "backlog_work": 0.0,
                             "link_backlog_mb": 0.0, "compute": 1.0, "bw_mbps": 1.0}
        comp = max(float(st.get("compute", 1.0)), 1e-9)
        bw = max(float(st.get("bw_mbps", 1.0)) / 8.0, 1e-9)       # MB/s
        compute_backlog = float(st.get("backlog_work", 0.0)) / comp
        if self.policy == "least_load":
            score = compute_backlog
        else:                                                      # contention
            link_drain = float(st.get("link_backlog_mb", 0.0)) / bw
            score = compute_backlog + link_drain + transfer_mb / bw
        return score, comp, bw * 8.0

    def choose_with_identity(self, service_units: float, transfer_mb: float,
                             ) -> tuple[str, float, float, float]:
        """选节点并返回 (url, 该节点自报的 compute, bw_mbps, **这次决策用的状态有多旧**)。

        【为什么要带身份】Pod IP 会变（k3s 重启、Pod 重建），只按 URL 统计
        "请求打到了哪个节点"会在重启后把身份搞混。算力/带宽是 worker 自报的固定档位，
        用它标身份才跨运行可比。
        【为什么要带状态年龄】这是本次实测的核心量：调度器**实际**用的是几秒前的状态。
        """
        snap = self._aged_snapshot()
        age = self.obs_age()
        best, best_score, best_id = None, float("inf"), (1.0, 1.0)
        for u in self.workers:
            score, comp, bw_mbps = self._score(snap, u, transfer_mb)
            if score < best_score:
                best, best_score, best_id = u, score, (comp, bw_mbps)
        return (best or self.workers[0]), best_id[0], best_id[1], age

    def choose(self, service_units: float, transfer_mb: float) -> str:
        return self.choose_with_identity(service_units, transfer_mb)[0]


# ---------------------------------------------------------------------------
# 请求原语：一次"真实字节"或"假传输"请求的完整计时
# ---------------------------------------------------------------------------
def split_host_port(url: str) -> tuple[str, int]:
    """把 `http://host:port`（或 `host:port`）拆成 (host, port)。

    【为什么不用 urllib.parse】只需要两个字段，而 urllib 会把 `10.250.0.2:9101`
    这类没有 scheme 的写法的 path 部分吃掉。
    """
    s = url.split("//", 1)[-1].rstrip("/")
    if ":" in s:
        host, _, p = s.rpartition(":")
        return host, int(p)
    return s, 80


def do_transfer(url: str, upload_bytes: int, download_bytes: int,
                service_units: float, timeout: float, fake: bool = False,
                use_crc: bool = False) -> dict:
    """发一次请求，**分别计时上传与下传**，返回客户端与 worker 两侧的测量值。

    【为什么不用 urllib.request】它会先把整个请求体缓冲好再发、再把响应一次读完，
    于是"上传花了多久"与"下传花了多久"这两段重合在一起——而它们正是阶段 A
    要分开量的东西（计划书 §7 第 3–4 周："增加阶段时间记录：入口等待、KV 传输、回传"）。

    返回字段（全部是秒/字节，失败时 ok=False 并带 error）：
      t_conn/t_pre_send_s/t_upload_start/t_upload_end/t_resp_hdr/t_download_end
                                                        客户端时刻（相对 t0）
      ph_upload_s / ph_server_s / ph_download_s         客户端看到的三段
      latency_s                                         端到端
      upload_bytes / download_bytes                     客户端发/收的真实字节数
      srv_recv_bytes / srv_sent_bytes / srv_declared_bytes   服务端自报的字节数
      srv_recv_s / srv_recv_total_s / srv_service_s     服务端自报的各段耗时（真实模式）
      srv_transfer_s                                    老协议里 worker 睡的那段时间（仅 fake）
      crc_sent / crc_recv / srv_crc                     载荷校验和（仅 use_crc）

    【四段划分必须无缝无重叠】t_pre_send_s（建连+组请求头）+ ph_upload_s + ph_server_s
    + ph_download_s 精确等于 latency_s。这是"阶段加总 ≈ 端到端"能当核对用的前提；
    第一版把 t_conn（纯 connect）当第一段，漏掉组头那几十微秒，残差就成了负数。
    """
    t0 = time.perf_counter()
    out: dict = {"ok": False, "t0": t0, "upload_bytes": 0, "download_bytes": 0,
                 "latency_s": float("nan")}
    conn = http.client.HTTPConnection(*split_host_port(url), timeout=timeout)
    try:
        conn.connect()
        t_conn = time.perf_counter()
        if fake:
            # 老协议：body 是一小段 JSON，worker 按 transfer_mb 睡对应时间。
            # 对照组的意义是量出"应用层模拟 vs 真实内核限速"差多少（见 Threats）。
            body = json.dumps({"service_units": service_units,
                               "transfer_mb": upload_bytes / MB}).encode()
            conn.putrequest("POST", "/work", skip_accept_encoding=True)
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", str(len(body)))
            conn.putheader("Connection", "close")
            conn.endheaders()
            t_up_start = time.perf_counter()
            conn.send(body)
            t_up_end = time.perf_counter()
            resp = conn.getresponse()
            t_hdr = time.perf_counter()
            # 【为什么这里要整段读并解析 JSON】老协议的响应体只有一段小 JSON，
            # 里面带着 worker 自报的 transfer_s / service_s —— 那正是对照臂要对比的量。
            # 不解析的话 client 的 net_share 会变成"推 60 字节 JSON 的耗时"，与老结果不可比。
            raw = resp.read()
            t_down_end = time.perf_counter()
            try:
                meta = json.loads(raw.decode() or "{}")
            except Exception:
                meta = {}
            out.update(mode="fake", upload_bytes=len(body), download_bytes=len(raw),
                       latency_s=t_down_end - t0, ph_upload_s=t_up_end - t_up_start,
                       ph_server_s=t_hdr - t_up_end, ph_download_s=t_down_end - t_hdr,
                       t_conn=t_conn - t0, t_pre_send_s=t_up_start - t0,
                       t_upload_start=t_up_start - t0,
                       t_upload_end=t_up_end - t0, t_resp_hdr=t_hdr - t0,
                       t_download_end=t_down_end - t0,
                       # 老 worker 没标 done 时按 HTTP 状态判断；有 done 就以它为准
                       ok=bool(meta.get("done", resp.status == 200)),
                       srv_transfer_s=meta.get("transfer_s"),
                       srv_worker_time_s=meta.get("worker_time_s"),
                       srv_service_s=meta.get("service_s",
                                              _hdr_float(resp, "X-Service-S")),
                       srv_recv_bytes=_hdr_int(resp, "X-Received-Bytes"),
                       srv_sent_bytes=_hdr_int(resp, "X-Sent-Bytes"),
                       srv_mode=resp.getheader("X-Mode"))
            return out
        crc_sent = payload_crc(upload_bytes) if use_crc else None
        conn.putrequest("POST", "/work", skip_accept_encoding=True)
        conn.putheader("Content-Type", "application/octet-stream")
        conn.putheader("Content-Length", str(upload_bytes))
        conn.putheader("Connection", "close")
        conn.putheader("X-Service-Units", f"{service_units:.6f}")
        conn.putheader("X-Upload-Bytes", str(int(upload_bytes)))
        conn.putheader("X-Download-Bytes", str(int(download_bytes)))
        if use_crc:
            conn.putheader("X-Payload-Crc", str(crc_sent))
        conn.endheaders()
        t_up_start = time.perf_counter()
        sent = 0
        for c in payload_chunks(upload_bytes):
            conn.send(c)
            sent += len(c)
        t_up_end = time.perf_counter()
        resp = conn.getresponse()
        t_hdr = time.perf_counter()
        got, t_down_first, t_down_end, crc_recv = _read_response_body(resp, use_crc)
        out.update(mode="real", upload_bytes=sent, download_bytes=got,
                   latency_s=t_down_end - t0, ph_upload_s=t_up_end - t_up_start,
                   ph_server_s=t_hdr - t_up_end, ph_download_s=t_down_end - t_hdr,
                   down_first_s=(t_down_first or t_hdr) - t_hdr,
                   t_conn=t_conn - t0, t_pre_send_s=t_up_start - t0,
                   t_upload_start=t_up_start - t0,
                   t_upload_end=t_up_end - t0, t_resp_hdr=t_hdr - t0,
                   t_download_end=t_down_end - t0, ok=True,
                   crc_sent=crc_sent, crc_recv=crc_recv,
                   srv_recv_bytes=_hdr_int(resp, "X-Received-Bytes"),
                   srv_declared_bytes=_hdr_int(resp, "X-Declared-Bytes"),
                   srv_sent_bytes=_hdr_int(resp, "X-Sent-Bytes"),
                   srv_recv_s=_hdr_float(resp, "X-Recv-S"),
                   srv_recv_total_s=_hdr_float(resp, "X-Recv-Total-S"),
                   srv_service_s=_hdr_float(resp, "X-Service-S"),
                   srv_node=resp.getheader("X-Server-Node"),
                   srv_mode=resp.getheader("X-Mode"),
                   srv_crc=_hdr_int(resp, "X-Payload-Crc"))
        return out
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        out["latency_s"] = time.perf_counter() - t0
        return out
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _read_response_body(resp, want_crc: bool = False
                        ) -> tuple[int, float | None, float, int | None]:
    """把响应体分块读完并计数（和 consume_body 对称）。

    用 read1 而不是 read：read(n) 会等到攒满 n 字节才返回，下传的首块时刻会被推迟。
    """
    got, t_first, h = 0, None, 0
    while True:
        c = resp.read1(CHUNK_BYTES)
        if not c:
            break
        if t_first is None:
            t_first = time.perf_counter()
        got += len(c)
        if want_crc:
            h = zlib.crc32(c, h)
    return got, t_first, time.perf_counter(), (h if want_crc else None)


def _hdr_int(resp, name: str) -> int | None:
    v = resp.getheader(name)
    try:
        return int(v) if v is not None else None
    except ValueError:
        return None


def _hdr_float(resp, name: str) -> float | None:
    v = resp.getheader(name)
    try:
        return float(v) if v is not None else None
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 角色 3：client（泊松到达 + 逐请求计时）
# ---------------------------------------------------------------------------
def run_client(args) -> int:
    workers = [w.strip() for w in args.workers.split(",") if w.strip()]
    for i, w in enumerate(workers):
        if not w.startswith("http"):
            workers[i] = "http://" + w
    sched = Scheduler(workers, args.policy, state_lag=args.state_lag,
                      state_ttl=args.state_ttl)
    sched.snapshot()                      # 预热一次状态

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)
    rows: list[dict] = []
    rows_lock = threading.Lock()
    truncated = False
    t_budget = time.perf_counter() + args.max_seconds

    # 【为什么必须并发发请求】早先这里是"发一个等一个"的同步循环，于是**永远压不出排队**：
    # client 自己成了限流器，实测 qps=2 与 qps=4 两档的 p50/p95/p99 几乎逐位相同
    # （1.0152/1.9780/2.8566 vs 1.0153/1.9783/2.8564）——因为服务时间 ~1 s > 到达间隔，
    # 请求根本没机会叠在一起。而本课题的机制（看队列/链路积压）恰恰只在有积压时才有意义。
    # 所以改成**开环**：按到达时刻提交任务、不等待上一个完成，在途请求数由系统自己决定。
    def one_request(i: int, transfer_mb: float, service_units: float,
                    arrival_rel: float) -> None:
        t0 = time.perf_counter()
        url, w_compute, w_bw_mbps, obs_age = sched.choose_with_identity(service_units, transfer_mb)
        upload_bytes = int(transfer_mb * MB)
        download_bytes = int(args.download_mb * MB)
        t_choose = time.perf_counter() - t0          # 调度决策（含取状态）花的时间
        # 真实传输默认走 http.client 分块收发；--fake-transfer 退回老协议（JSON+sleep）
        r = do_transfer(url, upload_bytes, download_bytes, service_units,
                        timeout=args.request_timeout, fake=args.fake_transfer)
        ok = bool(r.get("ok"))
        lat = r["latency_s"]
        # worker 自报的两项分解，用来算**真实的 net_share**——
        # 只有 net_share 与仿真场景落在同一区间，两边的方向才可比。
        # 真实传输时"传输段"由 worker 实测收到载荷那段（srv_recv_s）给出；
        # 假传输时 worker 回的是 JSON 里的 transfer_s。
        if ok:
            if r.get("mode") == "real":
                w_transfer = _nanf(r.get("srv_recv_s"))
                w_service = _nanf(r.get("srv_service_s"))
            else:
                # 老协议：worker 自报的睡眠时长就是这一臂的"传输时间"
                w_transfer = _nanf(r.get("srv_transfer_s"))
                w_service = _nanf(r.get("srv_service_s"))
        else:
            w_transfer = w_service = float("nan")
        with rows_lock:
            rows.append({"req_id": i, "truncated_run": int(truncated),
                         "arrival": round(arrival_rel, 6),
                         "worker": url, "policy": args.policy,
                         # 节点身份（它自报的档位）：Pod IP 会变，身份不会
                         "worker_compute": w_compute, "worker_bw_mbps": w_bw_mbps,
                         # 本次决策**实际用到**的状态有多旧（秒）——本实验的核心测量量
                         "obs_age_s": round(obs_age, 6),
                         "state_lag_s": args.state_lag, "latency_s": round(lat, 6),
                         "w_transfer_s": w_transfer, "w_service_s": w_service,
                         "service_units": round(service_units, 3),
                         "transfer_mb": round(transfer_mb, 3), "ok": int(ok),
                         # --- 2026-10-05 新增：真实字节与阶段分解 ---
                         "transfer_mode": r.get("mode", "fake" if args.fake_transfer else "real"),
                         "upload_bytes": r.get("upload_bytes", 0),
                         "download_bytes": r.get("download_bytes", 0),
                         "srv_recv_bytes": r.get("srv_recv_bytes"),
                         "choose_s": round(t_choose, 6),
                         # 客户端看到的三段：上传 / 服务端那段 / 下传。
                         # 三者之和精确等于 latency_s（同一次连续计时切出来的）。
                         "ph_upload_s": _r6(r.get("ph_upload_s")),
                         "ph_server_s": _r6(r.get("ph_server_s")),
                         "ph_download_s": _r6(r.get("ph_download_s")),
                         "error": r.get("error", "")})

    # 先按本地随机种子把泊松到达时刻整段算好（与墙钟对齐由 sleep 完成）。
    # 用累积和而不是每轮 sum(inter[:i+1])：后者是 O(n²)，240 个请求还不明显，
    # 但把请求数调到几千就会白烧 CPU。
    inter = [rng.expovariate(args.qps) for _ in range(args.requests)]
    offsets, acc = [], 0.0
    for gap in inter:
        acc += gap
        offsets.append(acc)
    params = [(rng.lognormvariate(math.log(args.mean_transfer_mb), 0.6),
               rng.lognormvariate(math.log(args.mean_service_units), 0.5))
              for _ in range(args.requests)]

    t_start = time.perf_counter()
    pool = ThreadPoolExecutor(max_workers=args.concurrency)
    futures = []
    for i, off in enumerate(offsets):
        if time.perf_counter() > t_budget:
            # 硬上限：宁可截断并如实标注，也不让一次误配把机器挂住几小时
            truncated = True
            print(f"  [警告] 超过 --max-seconds={args.max_seconds}s，在 {i}/{args.requests} 处截断；"
                  f"该批结果只作诊断用，不进论文", flush=True)
            break
        # 变量名不要叫 wait：它会遮蔽上面 import 进来的 futures.wait（踩过一次，
        # 结果是每个 run 都在收尾处抛 TypeError、整批没有产物）
        sleep_for = (t_start + off) - time.perf_counter()
        if sleep_for > 0:
            time.sleep(sleep_for)
        mb, su = params[i]
        futures.append(pool.submit(one_request, i, mb, su, off))
        if (i + 1) % 50 == 0:
            inflight = sum(1 for f in futures if not f.done())
            print(f"  已提交 {i+1}/{args.requests}  在途 {inflight}", flush=True)
    # 排空在途请求：给每个请求留出 max(--request-timeout) 的余量，避免无限等
    done, not_done = futures_wait(futures, timeout=args.request_timeout + 30) if futures \
        else (set(), set())
    if not_done:
        print(f"  [警告] 仍有 {len(not_done)} 个请求未返回（超时），照实记为失败", flush=True)
        for f in not_done:
            f.cancel()
    pool.shutdown(wait=False)
    rows.sort(key=lambda r: r["req_id"])

    lat = sorted(r["latency_s"] for r in rows if r["ok"])
    def q(p):
        return lat[min(len(lat) - 1, int(p / 100 * len(lat)))] if lat else float("nan")
    # net_share = 传输时间 / 端到端延迟。这是**能否与仿真对比的前提**：
    # 仿真里收益只在 net_share 足够大时出现，若真实系统这一指标落在别的区间，
    # 方向不同是正常的，不能算"仿真错了"。
    tr = [r["w_transfer_s"] for r in rows if r["ok"] and r["w_transfer_s"] == r["w_transfer_s"]]
    netshare = (statistics.mean(tr) / statistics.mean(lat)) if lat and tr else float("nan")
    ages = [r["obs_age_s"] for r in rows if r.get("obs_age_s") == r.get("obs_age_s")]
    fs = sched.fetch_stats()
    def _q(v, p):
        return sorted(v)[min(len(v) - 1, int(p / 100 * len(v)))] if v else float("nan")
    print(f"[观测] 调度器实际用到的状态年龄：p50={_q(ages,50)*1000:.0f}ms "
          f"p95={_q(ages,95)*1000:.0f}ms max={max(ages)*1000 if ages else float('nan'):.0f}ms"
          f"  | 拉取 {fs['n']} 次 平均 {fs['mean_s']*1000:.1f}ms 失败 {fs['fail']} 次")
    print(f"[client] policy={args.policy} lag={args.state_lag}s  n={len(lat)}  "
          f"p50={q(50):.4f}s  p95={q(95):.4f}s  p99={q(99):.4f}s  "
          f"net_share={netshare:.3f}  失败 {sum(1 for r in rows if not r['ok'])}"
          + ("  [已截断]" if truncated else ""))
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"  → {out}")

    # 每个 worker 的实际服务统计也留一份，便于核对"异构是否真的生效"
    stats = []
    for u in workers:
        stats.append({"worker": u, **(json.loads(
            urllib.request.urlopen(u + "/state", timeout=5).read()))})
    sp = out.with_name(out.stem + "_workers.json")
    sp.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  → {sp}")
    return 0


# ---------------------------------------------------------------------------
# 角色 5：transfer（阶段 A 的验证电池；不涉及调度器/LLM/GPU）
# ---------------------------------------------------------------------------
def measure_one(url: str, up_bytes: int, down_bytes: int, iface: str | None,
                tc_cmd: str | None, timeout: float, service_units: float = 0.0,
                use_crc: bool = True, fake: bool = False) -> dict:
    """一次传输 + 它前后的网卡计数器与 tc 统计快照。

    【为什么快照要贴着这次传输取】网卡计数器是**累计值**，只有"紧邻前后取差"
    才归因得到这次传输；间隔里任何别的流量（状态轮询、SSH）都会混进差值里。
    这也是脚本必须独占这张网卡的原因——写正文时要把"限速只匹配实验数据流"
    这条（计划书 §5 阶段 A 原话）一起声明。
    """
    nic0 = read_nic_counters(iface)
    tc0 = read_tc_stats(tc_cmd)
    r = do_transfer(url, up_bytes, down_bytes, service_units, timeout=timeout,
                    fake=fake, use_crc=use_crc)
    r["nic_before"], r["nic_after"] = nic0, read_nic_counters(iface)
    r["tc_before"], r["tc_after"] = tc0, read_tc_stats(tc_cmd)
    return r


def nic_delta(before: dict, after: dict) -> dict:
    """网卡计数器增量；任一缺失就返回空（不要用 0 冒充"没流量"）。"""
    out = {}
    for k in ("rx_bytes", "tx_bytes"):
        if isinstance(before.get(k), int) and isinstance(after.get(k), int):
            out[k] = after[k] - before[k]
    return out


def tc_delta(before: dict, after: dict) -> dict:
    out = {}
    for k in ("overlimits", "dropped"):
        if isinstance(before.get(k), int) and isinstance(after.get(k), int):
            out[k] = after[k] - before[k]
    return out


def up_thr_mbps(r: dict) -> float:
    """上传方向的吞吐（Mbit/s）——**以服务端收到的字节数除以服务端收到它的耗时**。

    【为什么不用客户端发完的时刻】客户端 sendall 返回只表示数据进了内核发送缓冲，
    在被限速的链路上这个时刻远早于"对面真的收完"；用它会**系统性高估**吞吐。
    """
    d = _nanf(r.get("srv_recv_s"))
    n = r.get("srv_recv_bytes") or 0
    return n * 8 / 1e6 / d if d and d > 0 else float("nan")


def down_thr_mbps(r: dict) -> float:
    """下传方向的吞吐（Mbit/s）——以客户端收到的字节数除以它收到这些字节的耗时。

    【老协议下必须返回 NaN】`--fake-transfer` 时响应体只有一段 JSON（几十字节），
    拿它除以一个极短的时长会算出几十 Mbit/s 这种**完全虚假**的吞吐
    （实测出过 48.8 Mbit/s 的假值）。没有载荷就没有吞吐，不能给数。
    """
    if not r.get("ok") or r.get("mode") == "fake":
        return float("nan")
    d = r["ph_download_s"] - (r.get("down_first_s") or 0.0)
    return r["download_bytes"] * 8 / 1e6 / d if d and d > 0 else float("nan")


def _fmt(v, unit: str = "", digits: int = 3) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "n/a"
    return "n/a" if f != f else f"{f:.{digits}f}{unit}"


def run_transfer_battery(args) -> int:
    """阶段 A 验证电池：逐项给 PASS/FAIL，并把原始数字落盘。

    通过条件来自 `真实跨云LLM服务研究计划书.md` §5 阶段 A：
      1. 应用收到的字节数与设定一致；
      2. 限速后实测吞吐在预定容差内（本脚本取 ±10%）；
      3. 限速统计确实增加（overlimits 增量 > 0）；
      4. 单独传输与多路同时传输能区分开。
    外加一条只报告不判定的：网卡计数器增量 / 应用层字节数的比值。

    【A2/A4 的载荷方向跟着 `--shaped-direction` 走】限速往往只加在一个方向上，而 A4 只能
    沿着一个方向跑（单路 + 多路）。若把大载荷固定往上发，"只限下传"的轮次就会去量
    **没被限速**的那条路，比值必然接近 1 并被误判成 FAIL（2026-10-06 真实发生过一次）。
    因此：`download` ⇒ 小上传/大下传、吞吐取 `down_thr_mbps`；`upload` 或 `both` ⇒
    沿用原来的大上传/小下传。A2 两条臂都跑（载荷构造与原来逐字节相同），只判定被限速的那条；
    另有一条"载荷方向自证"把选择结果与 `--shaped-direction` 当场对账。
    """
    run_id = f"transfer_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # ---- 目标与网卡 ----
    in_proc = None
    if args.target:
        target_url = args.target if args.target.startswith("http") else "http://" + args.target
        host, _port = split_host_port(target_url)
        src_desc = f"外部 worker（{target_url}），电池跑在客户端这一侧"
    else:
        # 自带 worker 时把 0.0.0.0 换成 127.0.0.1：连 0.0.0.0 在 Linux 上虽然能通，
        # 但那是在赌实现细节，写进记录里也容易被误读成"跨机"。
        bind = "127.0.0.1" if args.bind in ("0.0.0.0", "::") else args.bind
        host = bind
        target_url = f"http://{bind}:{args.local_port}"
        in_proc, _st = start_http_server(bind, args.local_port, args.compute,
                                         args.bandwidth_mbps, force_fake=False,
                                         node_id="inproc")
        threading.Thread(target=in_proc.serve_forever, daemon=True).start()
        src_desc = ("进程内 worker（同一台机器/同一个网络命名空间）。"
                    "此模式**只能**验字节一致性与阶段计时；A3/A4 必然 FAIL，"
                    "因为回环不经 veth、没有真实队列。要过 A2–A4 必须走 netns 或两机")
    iface = detect_iface(host, args.iface)
    tc_cmd = tc_snapshot_cmd(iface, args.tc_cmd)

    detail: dict = {"run_id": run_id, "utc": datetime.now(timezone.utc).isoformat(),
                    "host": host_info(), "source": src_desc,
                    "target": target_url, "iface": iface, "tc_cmd": tc_cmd,
                    "tc_rate_mbps": args.tc_rate_mbps,
                    "shaped_direction": args.shaped_direction,
                    "sizes_mb": args.sizes_mb, "rate_test_mb": args.rate_test_mb,
                    "concurrency": args.rate_test_concurrency,
                    "rate_load_on": ("download" if args.shaped_direction == "download"
                                     else "upload"),
                    "transfers": [], "checks": []}
    checks = detail["checks"]
    print(f"[transfer] 目标={target_url}  网卡={iface}  tc 统计命令={tc_cmd or '（无）'}")
    print(f"           限速设定={args.tc_rate_mbps or '未指定'} Mbit/s  "
          f"限速方向={args.shaped_direction}  内核={detail['host'].get('kernel')}")

    def record(check: str, item: str, measured: str, verdict: bool | None, note: str = ""):
        checks.append({"check": check, "item": item, "measured": measured,
                       "verdict": ("PASS" if verdict else "FAIL") if verdict is not None
                       else "INFO", "note": note})
        tag = "PASS" if verdict else ("FAIL" if verdict is not None else "INFO")
        print(f"  [{tag:4}] {check} / {item}: {measured}" + (f"  ({note})" if note else ""),
              flush=True)

    # ================= A1 字节一致性 =================
    a1_ok = True
    if args.fake_transfer:
        print("\n=== A1/A5 跳过：本轮是对照臂（--fake-transfer，老协议）===")
        print("    老协议根本不发载荷，只有一段 JSON；'字节一致性'与'网卡计数器比值'"
              "这两项在本模式下无从核对——这正是它要证明的事。")
        record("A1", "对照臂跳过", "老协议无载荷字节", None,
               "--fake-transfer：载荷不存在，字节一致性与网卡比值不适用")
    else:
        print("\n=== A1 字节一致性（客户端发 N → 服务端收到 N；服务端发 N → 客户端收到 N）===")
    for mb in ([] if args.fake_transfer else args.sizes_mb):
        n = int(mb * MB)
        r = measure_one(target_url, n, n, iface, tc_cmd, args.transfer_timeout, use_crc=True)
        detail["transfers"].append({"sub": "A1", "size_mb": mb, **r})
        if not r.get("ok"):
            record("A1", f"{mb} MB", f"请求失败：{r.get('error')}", False)
            a1_ok = False
            continue
        got = r.get("srv_recv_bytes")
        dec = r.get("srv_declared_bytes")
        down = r.get("download_bytes")
        up = r.get("upload_bytes")
        crc_up = (r.get("srv_crc") is not None and r.get("srv_crc") == r.get("crc_sent"))
        crc_dn = (r.get("crc_recv") is not None and r.get("crc_recv") == r.get("crc_sent"))
        # 阶段加总核对（计划书 §7 第 5–6 周的阶段出口之一）：
        #   客户端四段（建连/上传/服务端段/下传）之和**精确等于**端到端耗时——这是同一次
        #   连续计时切出来的，恒等式；有意义的检查是下一个：
        #   服务端能自证的三段（收完载荷 + 算力 + 把载荷交给内核）+ 下传观测 ≤ 端到端，
        #   残差就是 TCP 握手、首字节传播、响应头与两端计时边界。
        #   注意"下传"这一段只能用客户端观测的时长当上界（服务端把最后一个字节写进
        #   内核缓冲的时刻，客户端无从得知），所以残差是**下界**——真实未记账时间只会更多。
        ph_sum = sum(_nanf(r.get(k)) for k in ("ph_upload_s", "ph_server_s", "ph_download_s"))
        ph_sum += _nanf(r.get("t_pre_send_s"))
        cli_total = _nanf(r.get("latency_s"))
        srv_total = (sum(_nanf(r.get(k)) for k in
                         ("srv_recv_total_s", "srv_service_s")) +
                     max(0.0, _nanf(r.get("ph_download_s"))))
        residual = cli_total - srv_total
        ok = (up == n and got == n and dec == n and down == n and crc_up and crc_dn)
        record("A1", f"{mb} MB 字节数",
               f"客户端发={up} 服务端声明={dec} 服务端实收={got} 客户端实收={down}（设定 {n}）",
               ok, "载荷 CRC 两侧一致" if (crc_up and crc_dn) else "CRC 不一致！")
        detail["transfers"][-1].update({"cli_total_s": cli_total, "srv_total_s": srv_total,
                                        "phase_sum_s": ph_sum, "residual_s": residual,
                                        "crc_upload_ok": crc_up, "crc_download_ok": crc_dn})
        a1_ok &= ok
        # 阶段分解也逐档留一行纯报告：计划书 §7 第 3–4 周要求"增加阶段时间记录"
        record("A1", f"{mb} MB 阶段分解",
               f"入口(建连+组请求头) {_fmt(r.get('t_pre_send_s'), ' s')} | "
               f"上传 {_fmt(r.get('ph_upload_s'), ' s')} | "
               f"服务端段(含算力+余下接收) {_fmt(r.get('ph_server_s'), ' s')} | "
               f"下传 {_fmt(r.get('ph_download_s'), ' s')} | 端到端 {_fmt(cli_total, ' s')}",
               None,
               f"四段之和 − 端到端 = {_fmt(ph_sum - cli_total, ' s', 9)}（同一次计时切出来，应为 0）")
    # 阶段加总单独成一行（计划书 §7 第 5–6 周的阶段出口之一）
    big = [t for t in detail["transfers"]
           if t.get("sub") == "A1" and t.get("ok") and t.get("size_mb", 0) >= 8]
    if big:
        worst = max(big, key=lambda t: t["residual_s"])
        ratio = worst["residual_s"] / worst["cli_total_s"] if worst["cli_total_s"] else 1.0
        record("A1", "阶段时间加总 vs 端到端",
               f"最大残差 {_fmt(worst['residual_s'], ' s')} "
               f"（{worst['size_mb']} MB：客户端 {_fmt(worst['cli_total_s'], ' s')} "
               f"− 服务端三段 {_fmt(worst['srv_total_s'], ' s')} = "
               f"{_fmt(ratio*100, '%', 1)}）",
               (0 <= worst["residual_s"] and ratio <= 0.25),
               "残差＝TCP 握手+首字节+响应头+两端计时边界")

    # ================= A2 限速吞吐 + A3 overlimits =================
    print("\n=== A2 限速吞吐 / A3 overlimits 增量 ===")
    big_mb = int(args.rate_test_mb * MB)
    small_mb = max(1, int(args.rate_test_mb * MB / 16))
    # 【大载荷必须放在被限速的那一侧】A4 只沿着一个方向跑（单路 + 多路），如果它固定把
    # 大载荷往上发，那么"只限了下传"的轮次里它量的是**没被限速**的那条路，比值必然接近 1
    # 而误判 FAIL。这在 2026-10-06 的跨公网轮次里真实发生过（`STAGE_A_CLOUD.md` §3：
    # 1 路 14.103 → 3 路每路 9.624、比值 1.47× FAIL，而那一轮限的正是下传）。
    # `--shaped-direction=both` 时两条路都被限，测哪一条都成立，沿用原来的"大上传"口径。
    rate_load_on = "download" if args.shaped_direction == "download" else "upload"
    rate_label = "下传" if rate_load_on == "download" else "上传"
    rate_up_b, rate_dn_b = ((small_mb, big_mb) if rate_load_on == "download"
                            else (big_mb, small_mb))
    rate_thr_fn = down_thr_mbps if rate_load_on == "download" else up_thr_mbps
    # 两条臂的载荷构造与改动前逐字节相同（上传臂＝大上传/小下传，下传臂＝小上传/大下传），
    # 只是把"哪一条是判定臂"显式记下来，供下面的自证行使用。
    arms: dict[str, tuple[dict, float]] = {}
    for direction in ("upload", "download"):
        up_b, dn_b = ((big_mb, small_mb) if direction == "upload" else (small_mb, big_mb))
        r_arm = measure_one(target_url, up_b, dn_b, iface, tc_cmd, args.transfer_timeout,
                            fake=args.fake_transfer)
        detail["transfers"].append({"sub": f"A2_{direction}", **r_arm})
        arms[direction] = (r_arm,
                           up_thr_mbps(r_arm) if direction == "upload" else down_thr_mbps(r_arm))
    r_up, thr_up = arms["upload"]
    r_dn, thr_dn = arms["download"]
    rate = args.tc_rate_mbps
    print(f"    判定臂按 --shaped-direction={args.shaped_direction} 选为【{rate_label}】："
          f"大载荷 {big_mb} 字节放在 {rate_label} 侧，另一方向只报告不判定")
    # 自证：方向必须真的按 --shaped-direction 落地，而不是只在打印里换个说法。
    # 判据是"大载荷那一侧 == 被限速的那一侧"，所以改了载荷构造而忘记改上面的选择会在这里 FAIL。
    expect_load_on = "download" if args.shaped_direction == "download" else "upload"
    dir_ok = (rate_load_on == expect_load_on
              and ((rate_dn_b > rate_up_b) == (expect_load_on == "download")))
    record("A2", "载荷方向自证",
           f"--shaped-direction={args.shaped_direction} ⇒ 期望大载荷在【{rate_label}】侧；"
           f"实际 上={rate_up_b} B / 下={rate_dn_b} B、A2/A4 判定方向=【{rate_label}】",
           dir_ok,
           "方向由 --shaped-direction 唯一决定（both 沿用原口径＝大上传）；"
           "不一致会让 A4 去量没被限速的那条路，把'设计不匹配'报成 FAIL")
    for label, thr, direction in (("上传", thr_up, "upload"), ("下传", thr_dn, "download")):
        shaped_here = args.shaped_direction in ("both", direction)
        if thr != thr:
            # NaN 有两种来源，必须分开说，否则会把"请求失败"读成"老协议没吞吐"。
            r_this = r_up if direction == "upload" else r_dn
            failed = not r_this.get("ok")
            record("A2", f"{label}吞吐",
                   f"n/a（请求失败：{r_this.get('error')}）" if failed
                   else "n/a（老协议无载荷字节）", None,
                   "这次传输根本没成功，先查错误再谈吞吐" if failed
                   else "老协议下吞吐没有定义；这正是对照臂要说明的事")
        elif not rate:
            record("A2", f"{label}吞吐", f"实测 {_fmt(thr, ' Mbit/s')}", None,
                   "未给 --tc-rate-mbps，无法判定（只报告）")
        elif not shaped_here:
            record("A2", f"{label}吞吐", f"实测 {_fmt(thr, ' Mbit/s')}", None,
                   f"--shaped-direction={args.shaped_direction}，该方向未限速，不判定")
        else:
            dev = (thr - rate) / rate * 100 if rate else float("nan")
            ok = abs(dev) <= 10.0
            record("A2", f"{label}吞吐", f"实测 {_fmt(thr, ' Mbit/s')} / 设定 {rate} "
                   f"（偏差 {_fmt(dev, '%', 2)}）", ok, "容差 ±10%")
    # A3：把两次大传输的 overlimits 增量合起来看（限速器在这两段里都在掐流量）
    if not tc_cmd:
        record("A3", "overlimits 增量", "没有可用的 tc 统计命令", None,
               "给 --iface 或 --tc-cmd 才能读")
    else:
        d0 = tc_delta(r_up.get("tc_before", {}), r_dn.get("tc_after", {}))
        oc = d0.get("overlimits")
        record("A3", "overlimits 增量",
               f"overlimits +{oc}，dropped +{d0.get('dropped')}"
               if oc is not None else "读 tc 统计失败",
               (oc is not None and oc > 0),
               "只看 `tc qdisc show` 有输出不算生效，必须这个增量 > 0"
               + ("；本机没有加 tc 限速时这一项必然 FAIL——"
                  "阶段 A 必须在 netns / 两机模式下跑" if not rate else ""))

    # ================= A4 单路 vs 多路并发 =================
    print(f"\n=== A4 单路 vs {args.rate_test_concurrency} 路并发（共享出口被瓜分）===")
    print(f"    载荷方向：大载荷放在【{rate_label}】侧，吞吐取 {rate_label} 方向"
          f"（--shaped-direction={args.shaped_direction}）")
    n_flow = max(2, args.rate_test_concurrency)
    r_one = measure_one(target_url, rate_up_b, rate_dn_b, iface, tc_cmd, args.transfer_timeout,
                        fake=args.fake_transfer)
    detail["transfers"].append({"sub": "A4_single", **r_one})
    thr_one = rate_thr_fn(r_one)
    # 【为什么要用 Barrier 对齐】不等齐就分不出"并发"与"先后依次跑"：
    # 若第一路先跑完，第二路才开始，那测到的还是单路吞吐。
    barrier = threading.Barrier(n_flow)

    def _flow():
        barrier.wait(timeout=60)
        return do_transfer(target_url, rate_up_b, rate_dn_b, 0.0,
                           timeout=args.transfer_timeout, use_crc=False,
                           fake=args.fake_transfer)

    with ThreadPoolExecutor(max_workers=n_flow) as ex:
        r_multi = list(ex.map(lambda _: _flow(), range(n_flow)))
    detail["transfers"].append({"sub": "A4_multi", "flows": r_multi})
    thr_each = [rate_thr_fn(r) for r in r_multi]
    thr_multi = statistics.mean(thr_each) if thr_each else float("nan")
    agg_multi = sum(t for t in thr_each if t == t)
    ratio = thr_one / thr_multi if thr_multi and thr_multi == thr_multi else float("nan")
    ok_multi = all(r.get("ok") for r in r_multi)
    # 【为什么还要看端到端时间】老协议（--fake-transfer）里根本没有载荷在过线，
    # "吞吐"这个词不成立；而"并发请求会不会互相拖慢"用**每请求端到端时间**量是等价的
    # （传输占主导时，吞吐与时间是倒数）。对照臂正是靠这个比值说明问题：
    # 老协议下并发请求各睡各的、互不拖慢（比值≈1），真实链路下互相瓜分（比值≈并发路数）。
    lat_one = _nanf(r_one.get("latency_s"))
    lat_multi = statistics.mean([_nanf(r.get("latency_s")) for r in r_multi]) if r_multi \
        else float("nan")
    ratio_lat = lat_multi / lat_one if lat_one and lat_one == lat_one else float("nan")
    use_thr = ratio == ratio          # 有载荷吞吐就用它（真实模式）
    verdict = ok_multi and ((ratio >= 1.5) if use_thr else (ratio_lat == ratio_lat
                                                           and ratio_lat >= 1.5))
    record("A4", f"载荷方向（单路 vs {n_flow} 路实测的就是这一侧）",
           f"大载荷 {big_mb} 字节走在【{rate_label}】方向，吞吐用 "
           f"{'down_thr_mbps（客户端收到字节/耗时）' if rate_load_on == 'download' else 'up_thr_mbps（服务端收到字节/耗时）'}",
           None,
           f"--shaped-direction={args.shaped_direction} ⇒ 必须测【{rate_label}】方向；"
           "只限速其中一个方向时，测错方向必然得到接近 1 的比值并误判 FAIL")
    record("A4", f"1 路 vs {n_flow} 路 单路吞吐",
           f"1 路 {_fmt(thr_one, ' Mbit/s')} → {n_flow} 路时每路 "
           f"{_fmt(thr_multi, ' Mbit/s')}（比值 {_fmt(ratio, '×', 2)}；"
           f"{n_flow} 路合计 {_fmt(agg_multi, ' Mbit/s')}）",
           verdict, f"共享瓶颈下理想比值≈{n_flow}；判据 ≥1.5")
    record("A4", f"1 路 vs {n_flow} 路 每请求端到端",
           f"1 路 {_fmt(lat_one, ' s')} → {n_flow} 路时每路 {_fmt(lat_multi, ' s')}"
           f"（比值 {_fmt(ratio_lat, '×', 2)}）", None,
           "老协议下这个比值会≈1：它把'带宽'建模成一个与并发无关的睡眠时间，"
           "没有共享的物理瓶颈可瓜分")

    # ================= A5 网卡计数器 / 应用层字节 =================
    if args.fake_transfer:
        print("\n=== A5 跳过（对照臂：没有载荷过线，网卡计数器无从对比）===")
    else:
        print("\n=== A5 网卡计数器增量 vs 应用层字节数（只报告，不判定）===")
        dn = nic_delta(r_dn.get("nic_before", {}), r_dn.get("nic_after", {}))
        up = nic_delta(r_up.get("nic_before", {}), r_up.get("nic_after", {}))
        ratio_tx = (up.get("tx_bytes", 0) / big_mb) if up.get("tx_bytes") and big_mb \
            else float("nan")
        ratio_rx = (dn.get("rx_bytes", 0) / big_mb) if dn.get("rx_bytes") and big_mb \
            else float("nan")
        record("A5", "网卡 tx 增量 / 上传字节", f"{_fmt(ratio_tx, '×', 4)}", None,
               f"网卡 tx +{up.get('tx_bytes')} 字节，应用层发 {big_mb} 字节；"
               f"超出部分是 TCP/IP 头（20+20+12 字节/段）")
        record("A5", "网卡 rx 增量 / 下传字节", f"{_fmt(ratio_rx, '×', 4)}", None,
               f"网卡 rx +{dn.get('rx_bytes')} 字节，应用层收 {big_mb} 字节")

    # ================= 落盘 =================
    if args.fake_transfer:
        record("A1-summary", "全部档位字节一致", "对照臂无载荷，不适用", None,
               "--fake-transfer：这一项没有可核对的对象")
    else:
        record("A1-summary", "全部档位字节一致", "见上",
               "PASS" if a1_ok else "FAIL", "")
    csv_path = out_dir / f"{run_id}_battery.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["check", "item", "measured", "verdict", "note"])
        w.writeheader()
        for c in detail["checks"]:
            w.writerow(c)
    json_path = out_dir / f"{run_id}_battery.json"
    json_path.write_text(json.dumps(detail, ensure_ascii=False, indent=2, default=str),
                         encoding="utf-8")

    n_pass = sum(1 for c in checks if c["verdict"] == "PASS")
    n_fail = sum(1 for c in checks if c["verdict"] == "FAIL")
    n_info = sum(1 for c in checks if c["verdict"] == "INFO")
    print(f"\n[transfer] 结果：PASS {n_pass} / FAIL {n_fail} / 只报告 {n_info}")
    print(f"  → {csv_path}\n  → {json_path}")
    if in_proc is not None:
        in_proc.shutdown()
    return 0 if n_fail == 0 else 1


# ---------------------------------------------------------------------------
# 角色 4：selftest（不起进程，只查策略函数的性质）
# ---------------------------------------------------------------------------
def run_selftest() -> int:
    """两个必须成立的性质：
      1. 当所有节点同构且链路积压为 0 时，contention 与 least_load 的选择可以不同，
         但只要链路项为 0，两者的打分完全相同 → 这是"消融=严格子集"的实测版；
      2. 报出本机能用哪些内核手段：`tc` 是否存在、有哪些 qdisc 可用。
         （传输本身已经是真实字节，见文件头；`tc` 决定的是**能不能真的限速**。）
    """
    ok = True
    s1 = {"a": {"inflight": 1, "backlog_work": 10.0, "link_backlog_mb": 0.0,
                "compute": 1.0, "bw_mbps": 1000.0},
          "b": {"inflight": 2, "backlog_work": 20.0, "link_backlog_mb": 0.0,
                "compute": 1.0, "bw_mbps": 1000.0}}
    ll = Scheduler(["a", "b"], "least_load")
    ct = Scheduler(["a", "b"], "contention")
    ll._history = [(time.perf_counter(), s1)]
    ct._history = [(time.perf_counter(), s1)]
    ll._cache = {u: (time.perf_counter(), st) for u, st in s1.items()}
    ct._cache = dict(ll._cache)
    a, b = ll.choose(5.0, 1.0), ct.choose(5.0, 1.0)
    print(f"  链路积压为 0 时两种策略的选择：least_load={a}  contention={b}  "
          f"{'一致 ✓' if a == b else '不一致 ✗'}")
    ok &= (a == b)

    s2 = {"a": {"inflight": 1, "backlog_work": 10.0, "link_backlog_mb": 500.0,
                "compute": 1.0, "bw_mbps": 100.0},
          "b": {"inflight": 2, "backlog_work": 20.0, "link_backlog_mb": 0.0,
                "compute": 1.0, "bw_mbps": 1000.0}}
    ct2 = Scheduler(["a", "b"], "contention")
    ct2._history = [(time.perf_counter(), s2)]
    ct2._cache = {u: (time.perf_counter(), st) for u, st in s2.items()}
    pick = ct2.choose(5.0, 100.0)
    print(f"  a 的链路积压 500 MB（100 Mbps）时 contention 选：{pick}  "
          f"{'避开 a ✓' if pick == 'b' else '没避开 a ✗'}")
    ok &= (pick == "b")

    tc = shutil.which("tc")
    if tc:
        r = subprocess.run([tc, "qdisc", "show"], capture_output=True, text=True)
        print(f"  `tc` 存在（rc={r.returncode}）→ "
              f"{'可以给网卡加内核限速（阶段 A 用 setup_netns_tc.sh）' if r.returncode == 0 else '但 qdisc 查询失败，先排查权限'}")
    else:
        print("  没有 `tc` → 传输仍是真实的（字节会真的过网卡），但**限速只能用应用层系数**："
              "这会退回 `--fake-transfer` 那种'睡觉'语义，"
              "阶段 A 的 A2/A3/A4 三项都无法通过，必须写进 Threats to Validity")
    print("  selftest：" + ("通过 ✓" if ok else "未通过 ✗"))
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="真实系统实测（趋势对照 + 阶段 A 传输验证）")
    ap.add_argument("role", choices=["worker", "client", "selftest", "transfer"])
    ap.add_argument("--port", type=int, default=9101)
    ap.add_argument("--bind", default="0.0.0.0",
                    help="worker 监听地址。阶段 A 的两机模式下要绑到那台'主机'的 IP"
                         "（例如 netns 外一侧的 10.250.0.1），否则对端连不上")
    ap.add_argument("--compute", type=float, default=1.0,
                    help="worker 的相对算力（越大越快）")
    ap.add_argument("--bandwidth-mbps", type=float, default=1000.0,
                    help="worker 链路的带宽档位。**只有 --fake-transfer（假传输）才用它**；"
                         "真实传输时快慢由图卡与内核 tc 决定")
    ap.add_argument("--fake-transfer", action="store_true",
                    help="退回老行为：不传真实字节，worker 按 transfer_mb/带宽 睡眠。"
                         "两端都加这个开关才构成完整对照臂（用来量"
                         "'应用层模拟 vs 真实内核限速'差多少，进 Threats）。"
                         "--role transfer 加它时自动跳过 A1/A5（没有载荷字节可核对）")
    ap.add_argument("--workers", default="127.0.0.1:9101,127.0.0.1:9102,127.0.0.1:9103")
    ap.add_argument("--policy", choices=["least_load", "contention"], default="least_load")
    ap.add_argument("--state-lag", type=float, default=0.0,
                    help="状态过期秒数（>0 表示调度器看到的是旧快照）")
    ap.add_argument("--state-ttl", type=float, default=DEFAULT_STATE_TTL,
                    help="调度器多久重新拉一次状态（秒）＝模拟监控系统的采集周期。"
                         "实测要扫的就是它：0.05＝每秒 20 次，1.0＝每秒 1 次，"
                         "15＝常见监控周期，60＝很保守的配置")
    ap.add_argument("--qps", type=float, default=8.0)
    ap.add_argument("--requests", type=int, default=600)
    ap.add_argument("--mean-transfer-mb", type=float, default=8.0)
    ap.add_argument("--download-mb", type=float, default=0.05,
                    help="client 角色每个请求的下传字节数（MB）。对应 LLM 里"
                         "'生成完回给用户的输出'，相对 KV 上传很小；默认 0.05 MB")
    ap.add_argument("--mean-service-units", type=float, default=60.0)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--max-seconds", type=float, default=600.0,
                    help="整批实验的墙钟硬上限；超时则截断并标记（防止一次误配把机器挂死）")
    ap.add_argument("--request-timeout", type=float, default=30.0,
                    help="单个请求的 socket 超时（秒）。正常请求 1–2 s；"
                         "Pod 重启/IP 失效时快速失败记为 ok=0，而不是每个请求硬等几分钟")
    ap.add_argument("--concurrency", type=int, default=32,
                    help="在途请求数上限（开环发送）。=1 会退化成「发一个等一个」，"
                         "那时 client 自己就是限流器、永远压不出排队（实测过）")
    ap.add_argument("--out", default="results/k8s_test/run.csv")

    # --- 阶段 A（transfer 角色）专用参数 ---
    g = ap.add_argument_group("阶段 A 传输验证（--role transfer）")
    g.add_argument("--target", default=None,
                   help="外部 worker 的 host:port。**不给就自带一个进程内 worker**"
                        "（单机自足模式；那样只能验字节数与计时，验不了 tc，因为回环不经 veth）")
    g.add_argument("--local-port", type=int, default=9199,
                   help="自带 worker 时监听的端口")
    g.add_argument("--iface", default=None,
                   help="统计用的网卡名。不给就按到目标的路由自动判断"
                        "（netns 里没有默认路由，所以两机模式必须显式指定）")
    g.add_argument("--tc-cmd", default=None,
                   help="读取 tc 统计的命令，覆盖默认的 `tc -s qdisc show dev <iface>`。"
                        "netns 场景写 'ip netns exec hostB tc -s qdisc show dev vethB'")
    g.add_argument("--tc-rate-mbps", type=float, default=None,
                   help="tc tbf 的设定速率（Mbit/s）。给了才会判定吞吐是否落在 ±10%% 内")
    g.add_argument("--shaped-direction", choices=["both", "upload", "download"], default="both",
                   help="哪几个方向真的被 tc 限速了。只判定被限速的方向，"
                        "否则拿未限速方向去比设定值必然 FAIL")
    g.add_argument("--sizes-mb", type=float, nargs="+", default=[0.5, 8.0, 200.0],
                   help="A1 字节一致性要覆盖的档位（计划书 §5 用'小传输 vs 大传输'两组，"
                        "这里再加一档 200 MB 压住缓冲边界）")
    g.add_argument("--rate-test-mb", type=float, default=8.0,
                   help="A2/A4 每次传输的字节数。太小则连接建立/首字节占比过大，"
                        "太大则在 25 Mbit 档上要等很久（8 MB @25 Mbit ≈ 2.6 s）")
    g.add_argument("--rate-test-concurrency", type=int, default=3, help="A4 的并发路数")
    g.add_argument("--transfer-timeout", type=float, default=600.0,
                   help="阶段 A 单次传输的 socket 超时。必须比 request-timeout 大很多："
                        "200 MB @25 Mbit 单程就要 64 s")
    g.add_argument("--out-dir", default="results/stage_a",
                   help="阶段 A 产物目录（append-only：文件名带 run_id，不覆盖）")
    args = ap.parse_args()

    if args.role == "worker":
        return run_worker(args.port, args.compute, args.bandwidth_mbps,
                          bind=args.bind, force_fake=args.fake_transfer)
    if args.role == "client":
        return run_client(args)
    if args.role == "transfer":
        return run_transfer_battery(args)
    return run_selftest()


if __name__ == "__main__":
    raise SystemExit(main())

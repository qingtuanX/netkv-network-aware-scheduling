"""组件级实测 —— 用真实进程验证仿真模型里最可攻击的两个公式。

为什么需要它：
    本课题的全部结论来自仿真，而模型里有两条假设最容易被审稿人质疑：
      (1) 传输时间 = 时延 + 字节数 / 带宽（链路没有排队）；
      (2) 服务时间 = 工作量 / 算力，多槽位靠 k 个服务器排队（槽位之间不争抢算力）。
    同类的仿真论文（Vidur）会用真实系统给出误差百分比（<5% 典型、最高 12.65%）。
    本脚本不做端到端验证（那需要 GPU 与集群），而是**逐组件**给出实测误差：
      · transfer：在真实 socket 上按目标速率限速发送，测出的传输时间与
        `bytes / rate` 对比，报告相对误差的分布；
      · queue：用 k 个真实 CPU 进程做服务端，按泊松到达喂固定工作量的任务，
        把**实测的**服务时间与到达过程喂回 SimPy 仿真，比较两者的 sojourn 分布。

    **适用边界（写正文时必须照抄）**：loopback socket + 软件限速器，不是真实广域网；
    CPU 进程不是 GPU 槽位；因此这验证的是**模型的函数形式与排队机制**，
    不是绝对性能，也不能替代集群实测。

用法：
    sim/.venv/Scripts/python.exe analysis/measure_components.py transfer
    sim/.venv/Scripts/python.exe analysis/measure_components.py queue
    sim/.venv/Scripts/python.exe analysis/measure_components.py all --out results/verification
产物：
    {out}/measured_transfer.csv     每个 (目标速率, 载荷) 的实测/模型对比
    {out}/measured_queue.csv       真实进程 vs SimPy 的分位数对比
    {out}/measured_components.json 汇总（含相对误差），供报告引用
"""

from __future__ import annotations

import argparse
import json
import math
import multiprocessing as mp
import socket
import statistics
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "experiments"))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

# 与论文模型里的档位一致（bps → B/s）
RATE_PROFILES = {"10 Gbps": 1.25e9, "1 Gbps": 1.25e8, "100 Mbps": 1.25e7}


# ---------------------------------------------------------------------------
# 组件 1：传输时间
# ---------------------------------------------------------------------------
def _rate_limited_sender(sock: socket.socket, payload: bytes, rate_bps: float,
                         chunk: int = 1 << 16) -> None:
    """按目标速率发完 payload：令牌桶（睡眠补足），不用 OS 级限速。"""
    bytes_per_s = rate_bps / 8.0
    sent = 0
    t0 = time.perf_counter()
    n = len(payload)
    while sent < n:
        # 允许发送的字节数 = 已经过的时间 × 速率（简单漏桶，无突发）
        allowed = int((time.perf_counter() - t0) * bytes_per_s) + chunk
        want = min(chunk, n - sent)
        if sent + want > allowed:
            # 等到达标再发
            need = (sent + want) / bytes_per_s - (time.perf_counter() - t0)
            if need > 0:
                time.sleep(need)
        sock.sendall(payload[sent:sent + want])
        sent += want
    sock.shutdown(socket.SHUT_WR)


def _receiver(port: int, total: int, ready: threading.Event,
              result: dict) -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(1)
    ready.set()
    conn, _ = srv.accept()
    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    got = 0
    t_first = None
    while got < total:
        b = conn.recv(1 << 16)
        if not b:
            break
        if t_first is None:
            t_first = time.perf_counter()
        got += len(b)
    result["t_last"] = time.perf_counter()
    result["t_first"] = t_first
    result["bytes"] = got
    conn.close()
    srv.close()


def measure_transfer(payload_sizes: list[int], repeat: int = 3) -> pd.DataFrame:
    rows = []
    for label, rate in RATE_PROFILES.items():
        for size in payload_sizes:
            times = []
            for r in range(repeat):
                port = 39000 + (hash((label, size, r)) % 500)
                ready = threading.Event()
                res: dict = {}
                th = threading.Thread(target=_receiver, args=(port, size, ready, res),
                                      daemon=True)
                th.start()
                ready.wait(5.0)
                payload = b"\0" * size
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                s.connect(("127.0.0.1", port))
                t0 = time.perf_counter()
                _rate_limited_sender(s, payload, rate)
                s.close()
                th.join(30.0)
                if res.get("bytes") != size:
                    continue
                times.append(res["t_last"] - t0)
            if not times:
                continue
            t_meas = float(np.median(times))
            t_model = size / (rate / 8.0)          # 模型：字节数 / 带宽
            rows.append({
                "rate_label": label, "rate_bps": rate, "payload_bytes": size,
                "n_repeat": len(times),
                "t_measured_median_s": t_meas,
                "t_model_s": t_model,
                "rel_err_pct": (t_meas - t_model) / t_model * 100.0,
                "measured_rate_Bps": size / t_meas,
                "achieved_fraction": (size / t_meas) / (rate / 8.0),
            })
            print(f"    {label:<10} {size/1e6:>7.2f} MB  实测 {t_meas*1000:>8.1f} ms  "
                  f"模型 {t_model*1000:>8.1f} ms  相对误差 {(t_meas-t_model)/t_model*100:>+6.2f}%")
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 组件 2：排队与服务（真实 CPU 进程 vs SimPy）
# ---------------------------------------------------------------------------
def _worker_unit(work_iters: int) -> None:
    """一个固定工作量的 CPU 任务（与仿真里的抽象工作量对应）。"""
    x = 1.0
    for i in range(work_iters):
        x = math.sqrt(x * 1.0000001 + i % 7 + 1.0)
    return x


def _worker_loop(pipe, work_iters: int) -> None:
    while True:
        msg = pipe.recv()
        if msg is None:
            break
        t_start = time.perf_counter()
        _worker_unit(work_iters)
        pipe.send(time.perf_counter() - t_start)


def measure_queue(n_servers: int = 4, target_rho: float = 0.6,
                  n_tasks: int = 400, work_iters: int = 1_000_000,
                  seed: int = 7) -> dict:
    """真实进程版 M/G/k：k 个 CPU worker，泊松到达，测 sojourn 分布。

    【关键设计】派发必须**开放环**：每台服务器配一个消费者线程做阻塞式 send/recv，
    派发线程只负责按标称泊松时刻入队、随即返回。早先用单线程同步派发时，派发会被
    worker 占满而阻塞，到达过程被自我限流（标称 rho=0.9 实测到 1.155 却几乎不排队），
    再把这套被限流的到达喂给仿真，就变成同义反复而不是验证。
    """
    import queue as queue_mod
    import threading
    import simpy

    # 1) 起 worker 进程并**在持续负载下标定服务时间**
    pipes = [mp.Pipe() for _ in range(n_servers)]
    procs = [mp.Process(target=_worker_loop, args=(p[1], work_iters), daemon=True)
             for p in pipes]
    for pr in procs:
        pr.start()

    # 空闲标定：单发一个任务（用来量化"服务时间随负载上升"这件事）
    idle = []
    for _ in range(20):
        for parent, _ in pipes:
            parent.send("go")
        for parent, _ in pipes:
            idle.append(parent.recv())
    svc_idle = float(np.mean(idle))

    # 持续负载标定：让 k 个 worker 连续满载跑 warm_s 秒，统计到达稳态后的每任务耗时。
    # 【为什么必须这样做】实测同一份工作量在持续满载下是 165 ms，而空闲标定只有 94 ms
    # （+76%）。用空闲值去算到达率，会把实际 rho 推到 1.07/1.48/1.65（标称 0.6/0.85/0.95），
    # 标签直接失真。这个倍数同时就是"槽位之间不争抢算力"这条简化的实测误差。
    warm_s, calib_s = 2.0, 2.5
    steady: list[float] = []
    for parent, _ in pipes:
        parent.send("go")
    t_cal = time.perf_counter()
    while time.perf_counter() - t_cal < warm_s + calib_s:
        for j in range(n_servers):
            dt = pipes[j][0].recv()
            pipes[j][0].send("go")
            if time.perf_counter() - t_cal > warm_s:
                steady.append(dt)
    for parent, _ in pipes:
        parent.send(None)
    for parent, _ in pipes:
        parent.recv()                       # 收掉最后一个任务
    for pr in procs:                        # 标定完成，重启一批干净的 worker
        pr.join(3.0)
    procs = [mp.Process(target=_worker_loop, args=(p[1], work_iters), daemon=True)
             for p in pipes]
    for pr in procs:
        pr.start()

    svc_mean = float(np.mean(steady)) if steady else svc_idle
    svc_p50 = float(np.median(steady)) if steady else svc_idle
    contention_factor = svc_mean / svc_idle if svc_idle > 0 else float("nan")

    # 2) 标称泊松到达（开放环）
    lam = target_rho * n_servers / svc_mean
    rng = np.random.default_rng(seed)
    nominal = np.cumsum(rng.exponential(1.0 / lam, n_tasks))

    # 3) 每台服务器一个队列 + 一个消费者线程；派发用"最短队列"（与仿真的策略一致）
    qs = [queue_mod.Queue() for _ in range(n_servers)]
    inflight = [0] * n_servers
    lock = threading.Lock()
    records: list[tuple[float, float, float]] = []      # (submit, done, svc)
    stop = [False] * n_servers
    t0 = time.perf_counter()

    def consumer(j: int) -> None:
        parent = pipes[j][0]
        while True:
            item = qs[j].get()
            if item is None:
                return
            t_submit, idx = item
            parent.send("go")
            svc = parent.recv()
            t_done = time.perf_counter() - t0
            with lock:
                records.append((t_submit, t_done, svc))
                inflight[j] -= 1

    threads = [threading.Thread(target=consumer, args=(j,), daemon=True)
               for j in range(n_servers)]
    for th in threads:
        th.start()

    for idx, a in enumerate(nominal):
        w = a - (time.perf_counter() - t0)
        if w > 0:
            time.sleep(w)
        with lock:
            j = int(np.argmin(inflight))
            inflight[j] += 1
            t_submit = time.perf_counter() - t0
        qs[j].put((t_submit, idx))

    deadline = time.perf_counter() + 60.0
    while True:
        with lock:
            n_done = len(records)
        if n_done >= n_tasks or time.perf_counter() > deadline:
            break
        time.sleep(0.05)
    for j in range(n_servers):
        qs[j].put(None)
    for th in threads:
        th.join(2.0)
    for parent, _ in pipes:
        parent.send(None)
    for pr in procs:
        pr.join(3.0)

    rec = pd.DataFrame(records, columns=["submit", "done", "svc"]).sort_values("submit")
    measured = (rec["done"] - rec["submit"]).to_numpy(dtype=float)
    svc_arr = rec["svc"].to_numpy(dtype=float)
    arrivals_real = rec["submit"].to_numpy(dtype=float)

    # 实测负载强度：用**实际**提交间隔算到达率（不拿标称 λ 充数）
    span = float(arrivals_real[-1] - arrivals_real[0])
    lam_real = (len(arrivals_real) - 1) / span if span > 0 else float("nan")
    rho_real = float(svc_arr.mean() * lam_real / n_servers)   # 别忘了除以 k

    # 4) 同一到达过程 + 同一服务时间 + 同一"最短队列"策略，跑 SimPy
    env = simpy.Environment()
    ress = [simpy.Resource(env, capacity=1) for _ in range(n_servers)]
    out = []

    def job(arrival, svc):
        yield env.timeout(max(0.0, arrival - env.now))
        j = int(np.argmin([r.count + len(r.queue) for r in ress]))
        with ress[j].request() as req:
            yield req
            yield env.timeout(svc)
            out.append(env.now - arrival)

    for a, s in zip(arrivals_real, svc_arr):
        env.process(job(float(a), float(s)))
    env.run()
    sim = np.array(out, dtype=float)

    q = [50, 95, 99]
    res_dict = {
        "n_servers": n_servers, "target_rho": target_rho, "n_tasks": len(measured),
        "calibrated_service_mean_s": svc_mean, "calibrated_service_p50_s": svc_p50,
        "idle_service_mean_s": svc_idle,
        "contention_factor": contention_factor,
        "service_mean_s": float(svc_arr.mean()),
        "service_p95_s": float(np.percentile(svc_arr, 95)),
        "nominal_lambda_qps": lam, "realized_lambda_qps": lam_real,
        "rho_nominal": target_rho, "rho_realized": rho_real,
        "measured_sojourn_mean_s": float(measured.mean()),
        "sim_sojourn_mean_s": float(sim.mean()),
        "mean_rel_err_pct": (measured.mean() - sim.mean()) / sim.mean() * 100.0,
    }
    for k in q:
        m = float(np.percentile(measured, k))
        s = float(np.percentile(sim, k))
        res_dict[f"measured_p{k}_s"] = m
        res_dict[f"sim_p{k}_s"] = s
        res_dict[f"p{k}_rel_err_pct"] = (m - s) / s * 100.0
    print(f"    rho：标称 {target_rho:.2f} → 实测 {rho_real:.3f}"
          f"（λ_实测 {lam_real:.2f} QPS，服务 {svc_arr.mean()*1000:.1f} ms）")
    print(f"    真实进程：均值 {res_dict['measured_sojourn_mean_s']*1000:.2f} ms  "
          f"p95 {res_dict['measured_p95_s']*1000:.2f} ms  "
          f"（排队/服务 = {measured.mean()/svc_arr.mean():.2f}×）")
    print(f"    SimPy  ：均值 {res_dict['sim_sojourn_mean_s']*1000:.2f} ms  "
          f"p95 {res_dict['sim_p95_s']*1000:.2f} ms")
    print(f"    相对误差：均值 {res_dict['mean_rel_err_pct']:+.1f}%  "
          f"p50 {res_dict['p50_rel_err_pct']:+.1f}%  "
          f"p95 {res_dict['p95_rel_err_pct']:+.1f}%  "
          f"p99 {res_dict['p99_rel_err_pct']:+.1f}%")
    return res_dict


def main() -> int:
    ap = argparse.ArgumentParser(description="组件级实测（传输 / 排队）")
    ap.add_argument("what", choices=["transfer", "queue", "all"], default="all",
                    nargs="?")
    ap.add_argument("--out", default=str(ROOT / "results/verification"))
    ap.add_argument("--sizes", type=int, nargs="*",
                    default=[7680, 1_000_000, 8_000_000, 48_000_000],
                    help="传输载荷字节数")
    ap.add_argument("--servers", type=int, default=4)
    ap.add_argument("--rho", type=float, default=0.6)
    ap.add_argument("--tasks", type=int, default=400)
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    summary: dict = {"created_utc": datetime.now(timezone.utc).isoformat(),
                     "host_note": "loopback socket + 软件限速器；CPU 进程（非 GPU）；"
                                  "验证的是模型形式与排队机制，不是绝对性能",
                     "cpu_count": mp.cpu_count()}

    if args.what in ("transfer", "all"):
        print("=== 组件 1：传输时间（真实 socket + 软件限速）===")
        df = measure_transfer(args.sizes)
        df.to_csv(out_dir / "measured_transfer.csv", index=False)
        summary["transfer"] = df.to_dict(orient="records")
        print(f"  → {out_dir/'measured_transfer.csv'}  相对误差中位 "
              f"{df['rel_err_pct'].median():+.2f}%、最大 {df['rel_err_pct'].abs().max():.2f}%")

    if args.what in ("queue", "all"):
        print("=== 组件 2：排队与服务（真实 CPU 进程 vs SimPy）===")
        rec = measure_queue(n_servers=args.servers, target_rho=args.rho,
                            n_tasks=args.tasks)
        pd.DataFrame([rec]).to_csv(out_dir / "measured_queue.csv", index=False)
        summary["queue"] = rec
        print(f"  → {out_dir/'measured_queue.csv'}")

    with open(out_dir / "measured_components.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n汇总：{out_dir/'measured_components.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

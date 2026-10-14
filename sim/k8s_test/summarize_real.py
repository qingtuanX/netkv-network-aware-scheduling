#!/usr/bin/env python
"""把真实系统实测的每请求 CSV 汇总成论文级表格。

输入：`results/k8s_test/*.csv`（由 measure_system.py 的 client 角色逐请求写出）
      + 同名的 `*_workers.json`（每个 worker 结束时自报的 busy_s / done）
输出：同目录下 `summary.csv`（每次运行一行）与 `comparison.csv`（策略对比一行）

【为什么只用标准库】这个脚本要在 WSL 里的**系统 python3** 上跑（那段环境没有 pandas，
装它要额外的 apt 依赖且与仿真的 venv 无关），所以一律 csv + statistics。

【口径】与 measure_system.py 的 client 尾段保持同一套分位数取法，
否则"同一次运行两个脚本报的 p95 不一样"会变成无法解释的差异。
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass


# 每个 worker Pod 的 CPU 配额（k8s/workers.yaml 里的 resources.limits.cpu）。
# 真机的负荷率 rho_cpu 要用它做分母；改 workers.yaml 必须同步改这里，否则负荷率会算错。
CPU_LIMIT_PER_WORKER = 2.0


def pct(values: list[float], p: float) -> float:
    """与 measure_system.py 一致的分位数（整数下标，与仿真侧无关，只求内部自洽）。"""
    if not values:
        return float("nan")
    s = sorted(values)
    return s[min(len(s) - 1, int(p / 100 * len(s)))]


def load_run(csv_path: Path) -> dict | None:
    """读一次运行的每请求 CSV；缺必需列的旧文件返回 None（不猜、不补）。"""
    with csv_path.open(newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f)]
    if not rows or not {"ok", "latency_s", "policy"} <= set(rows[0].keys()):
        return None
    ok = [r for r in rows if r["ok"] == "1"]
    lat = [float(r["latency_s"]) for r in ok]
    tr = [float(r["w_transfer_s"]) for r in ok
          if r.get("w_transfer_s") not in (None, "", "nan")]
    sv = [float(r["w_service_s"]) for r in ok
          if r.get("w_service_s") not in (None, "", "nan")]
    # 本实验的核心测量量：每次决策**实际用到**的状态年龄（秒）
    ages = [float(r["obs_age_s"]) for r in ok
            if r.get("obs_age_s") not in (None, "", "nan")]
    arrivals = [float(r["arrival"]) for r in rows]
    # 【时间口径】client 是**开环并发**：请求按到达时刻提交、互不等待。
    # 所以"墙钟跨度" = 最后一次完成 − 第一次到达，而不是"最后一次到达 + 最后一个请求的延迟"
    # （后者在并发下会把跨度算短，把负荷率虚报好几倍——实测虚报过 47 倍）。
    span_lo = min(arrivals) if arrivals else float("nan")
    span_hi = max((float(r["arrival"]) + float(r["latency_s"]) for r in rows),
                  default=float("nan"))
    duration = span_hi - span_lo
    # 每个 worker 收到多少请求 → "网络感知有没有真的避开慢链路节点"的直接证据。
    # 【按身份而不是 IP 归类】Pod IP 会变（k3s 重启、Pod 重建都换 IP），按 IP 统计会把
    # 不同档位的节点混在一起。新版 client 每行都记了 worker 自报的档位，用它标身份。
    share: dict[str, float] = {}
    for r in ok:
        comp, bw = r.get("worker_compute"), r.get("worker_bw_mbps")
        key = (f"c{float(comp):g}_bw{float(bw):g}M"
               if comp not in (None, "", "nan") and bw not in (None, "", "nan")
               else r["worker"].split("//")[-1])
        share[key] = share.get(key, 0.0) + 1.0
    n_ok = max(len(ok), 1)
    share = {k: v / n_ok for k, v in share.items()}

    # 实际产生的到达率（不是文件里写的目标 qps）——门 1 的实测版要用实测值
    qps_realized = ((len(arrivals) - 1) / (max(arrivals) - min(arrivals))
                    if len(arrivals) > 1 and max(arrivals) > min(arrivals) else float("nan"))

    n_workers = 0
    wj = csv_path.with_name(csv_path.stem + "_workers.json")
    if wj.exists():
        stats = json.loads(wj.read_text(encoding="utf-8"))
        n_workers = len(stats)

    # 【真机上的"门 1"用 CPU 负荷率】仿真里 rho = 工作量×到达率/容量，是真机没有的解析量。
    # 这里用实测的服务时间（worker 自报的 service_s，纯 CPU 忙等那段，不含传输睡眠）
    # 乘以实测到达率，再除以机群的 CPU 配额总和：
    #     rho_cpu = 到达率 × E[service_s] / (worker 数 × 每 Pod CPU 配额)
    # 配额取 workers.yaml 里的 limits.cpu=2（三个 Pod 都是 2 → 6 个 CPU 核）。
    # 传输那一段是 sleep、不占 CPU，所以不进分子。这与仿真里"槽位按 compute_rate 满速推进"
    # 的假设是同一种量纲（CPU 秒/秒），可以放在同一张表里对照。
    rho_cpu = (qps_realized * statistics.mean(sv) / (n_workers * CPU_LIMIT_PER_WORKER)
               if sv and n_workers else float("nan"))
    return {
        "file": csv_path.name,
        "policy": ok[0]["policy"] if ok else "",
        "state_lag_s": float(ok[0]["state_lag_s"]) if ok else float("nan"),
        "n": len(lat), "n_failed": len(rows) - len(ok),
        "mean_transfer_mb": round(statistics.mean(
            [float(r["transfer_mb"]) for r in ok if r.get("transfer_mb")]), 3) if ok else float("nan"),
        "qps_realized": round(qps_realized, 3),
        "rho_cpu": round(rho_cpu, 3),
        "mean_service_s": round(statistics.mean(sv), 4) if sv else float("nan"),
        "mean_transfer_s": round(statistics.mean(tr), 4) if tr else float("nan"),
        "p50_s": round(pct(lat, 50), 4), "p95_s": round(pct(lat, 95), 4),
        "p99_s": round(pct(lat, 99), 4),
        "net_share": round(statistics.mean(tr) / statistics.mean(lat), 4) if lat and tr else float("nan"),
        "obs_age_p50_ms": round(pct(ages, 50) * 1000, 1) if ages else float("nan"),
        "obs_age_p95_ms": round(pct(ages, 95) * 1000, 1) if ages else float("nan"),
        "obs_age_max_ms": round(max(ages) * 1000, 1) if ages else float("nan"),
        "duration_s": round(duration, 1),
        # 每个 worker 各拿到多少比例的请求（"网络感知有没有真的避开慢链路节点"的直接证据）
        **{f"share_{k}": round(v, 4) for k, v in sorted(share.items())},
    }


def index_by_config(rows: list[dict]) -> dict:
    """按 (qps, 传输量档, lag) 归组，方便把两种策略配起来比。"""
    groups: dict[tuple, dict] = {}
    for r in rows:
        qps = round(r["_qps"], 4)
        # 【按实测数据量分档，别用大小阈值】早先用 ">=2MB 就算 large"，
        # 结果 8 MB 与 32 MB 的两次运行被塞进同一个 key、互相覆盖，
        # 表里静默少了一半行。这里直接用实测均值当档位标签。
        xf = f"{r['mean_transfer_mb']:.1f}MB" if r["mean_transfer_mb"] == r["mean_transfer_mb"] else "?"
        key = (qps, xf, r["state_lag_s"])
        groups.setdefault(key, {})[r["policy"]] = r
    return groups


def main() -> int:
    ap = argparse.ArgumentParser(description="汇总真实系统实测")
    ap.add_argument("--dir", default="results/k8s_test")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    d = Path(args.dir)
    files = sorted(p for p in d.glob("*.csv")
                   if not p.stem.endswith(("_workers", "summary", "comparison")))
    if not files:
        print(f"{d} 下没有可汇总的 CSV")
        return 1

    runs = []
    for f in files:
        r = load_run(f)
        if r is None:
            print(f"  [跳过] {f.name}：列不完整（旧格式），不猜不补")
            continue
        # qps 不从列里取（CSV 没记），从文件名尾部 _q<qps> 解析；解析不到就空
        q = f.stem.rsplit("_q", 1)
        r["_qps"] = float(q[1]) if len(q) == 2 else float("nan")
        runs.append(r)
    if not runs:
        print(f"{d} 下没有可用的每请求 CSV")
        return 1

    # old_sync_* 是"同步 client"时代留下的样本（那时 client 自己就是限流器、压不出排队）；
    # CONTAM_* 是运行中途 Pod 重启过的样本（脚本自动标出来的）；
    # stateage_* 是"状态年龄实测"的专用运行（见 measure_state_age.sh），单独一节报告。
    # 三者都保留在 summary.csv 里作证据，但**不参与阶梯与策略对照表**。
    def excluded(name: str) -> bool:
        return name.startswith(("old_sync_", "CONTAM_", "stateage_"))

    def is_stateage(name: str) -> bool:
        return name.startswith("stateage_")

    main_rows = [r for r in runs if "pilot" not in r["file"] and not excluded(r["file"])]
    ladder = [r for r in runs if "pilot" in r["file"] and not excluded(r["file"])]
    stateage = [r for r in runs if is_stateage(r["file"])]
    out = Path(args.out) if args.out else d / "summary.csv"
    cols = ["file", "policy", "state_lag_s", "n", "n_failed", "mean_transfer_mb",
            "qps_realized", "rho_cpu", "mean_service_s", "mean_transfer_s",
            "p50_s", "p95_s", "p99_s", "net_share",
            "obs_age_p50_ms", "obs_age_p95_ms", "obs_age_max_ms", "duration_s"] \
        + sorted({k for r in runs for k in r if k.startswith("share_")})
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(runs)
    print(f"[表] {out}  ({len(runs)} 行)")

    # 真机上的"门 1"用 rho_cpu：实测到达率 × 实测 CPU 服务时间 / 机群 CPU 配额。
    print("\n到达率阶梯（门 1 的实测版）")
    print(f"  {'qps目标':>7}{'qps实测':>9}{'rho_cpu':>9}{'p50':>8}{'p95':>8}"
          f"{'net_share':>11}")
    for r in sorted(ladder, key=lambda x: (x["_qps"] if x["_qps"] == x["_qps"] else 0)):
        flag = "" if not (r["rho_cpu"] == r["rho_cpu"]) or 0.3 <= r["rho_cpu"] <= 0.9 \
            else "  ← 落在有效区间外"
        print(f"  {r['_qps']:>7.0f}{r['qps_realized']:>9.2f}{r['rho_cpu']:>9.3f}"
              f"{r['p50_s']:>8.3f}{r['p95_s']:>8.3f}{r['net_share']:>11.3f}{flag}")

    # --- 状态年龄实测（measure_state_age.sh 的产物）---
    # 这一节回答的是：**调度器实际用到的状态有多旧**，以及"旧"花掉多少性能。
    if stateage:
        print("\n状态年龄实测（在真机 k3s 上扫「监控采集周期」；数字越小越好）")
        print(f"  {'采集周期(s)':>11}{'实测年龄p50':>12}{'实测年龄p95':>12}"
              f"{'最大年龄':>10}{'p50延迟':>9}{'p95延迟':>9}{'网络占比':>9}")
        for r in sorted(stateage, key=lambda x: x["file"]):
            ttl = r["file"].replace("stateage_ttl", "").split("_")[0]
            print(f"  {ttl:>11}{r['obs_age_p50_ms']:>10.0f}ms{r['obs_age_p95_ms']:>10.0f}ms"
                  f"{r['obs_age_max_ms']:>8.0f}ms{r['p50_s']:>9.3f}{r['p95_s']:>9.3f}"
                  f"{r['net_share']:>9.3f}")

    print("\n主网格：contention vs least_load（Δ>0 表示 contention 的 p95 更差）")
    print(f"  {'qps':>4}{'rho_cpu':>9}{'数据量':>8}{'lag':>5}{'least_load p95':>17}"
          f"{'contention p95':>17}{'Δp95%':>9}{'net_share':>11}")
    cmp_rows = []
    for (qps, xf, lag), g in sorted(index_by_config(main_rows).items()):
        a, b = g.get("least_load"), g.get("contention")
        if not a or not b:
            continue
        dp = (b["p95_s"] - a["p95_s"]) / a["p95_s"] * 100 if a["p95_s"] else float("nan")
        cmp_rows.append({"qps": qps, "rho_cpu": a["rho_cpu"],
                         "data_volume": xf, "state_lag_s": lag,
                         "p95_least_load": a["p95_s"], "p95_contention": b["p95_s"],
                         "dp95_pct": round(dp, 2),
                         "net_share": a["net_share"],
                         "share_least_load": json.dumps(
                             {k: v for k, v in a.items() if k.startswith("share_")},
                             ensure_ascii=False),
                         "share_contention": json.dumps(
                             {k: v for k, v in b.items() if k.startswith("share_")},
                             ensure_ascii=False)})
        print(f"  {qps:>4.0f}{a['rho_cpu']:>9.3f}{xf:>8}{lag:>5.0f}"
              f"{a['p95_s']:>17.3f}{b['p95_s']:>17.3f}"
              f"{dp:>9.1f}{a['net_share']:>11.3f}")

    if cmp_rows:
        cout = d / "comparison.csv"
        with cout.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(cmp_rows[0].keys()))
            w.writeheader()
            w.writerows(cmp_rows)
        print(f"\n[表] {cout}  ({len(cmp_rows)} 行)")

    print("\n【引用这些数字时必须同时写的三句】"
          "\n  1) 单节点（WSL2 里一个 k3s）不是多机集群，只能比方向；"
          "\n  2) worker 的算力仍是应用层抽象系数（同一段忙等除以 compute），不是 GPU 推理；"
          "\n  3) 传输自 2026-10-05 起是**真实字节收发**（`transfer_mode` 列标了本节走的是哪条路径）："
          "标 real 的走真实载荷，快慢由内核 tc 与真实网卡决定；标 fake 的是老协议"
          "（worker 按 transfer_mb/带宽睡眠），只能当对照臂用，不得当网络代价写进正文。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

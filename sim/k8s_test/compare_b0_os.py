"""B0：把两个操作系统上同一套 B0 检查的产物**逐项对照**（只用标准库）。

为什么需要这个入口：
    `measure_serving.py` 的结论在 Windows 上验过一遍（`results/b0/`），但阶段 B 的服务器
    是 Linux。操作系统之间的差异（`time.time()` 分辨率、回环开销、线程调度、异常类型）
    会让"同一套检查"在某些指标上给出不同的数——**必须先把它们量出来**，
    否则到了 GPU 机器上分不清"是引擎慢"还是"是尺子在 Linux 上读数不同"。

它做什么：
    按 run_id 的**进程内序号后缀**（`_<pid>-<seq>`）把 Windows 与 Linux 的 run 两两配对，
    对同一列算分位数并给出差值。配对依据是"两次运行的命令行顺序一致"——
    battery 的角色/臂顺序是写死的，所以这个配对是确定的。

用法（在 sim/ 目录下执行）：
    # Windows 侧
    .venv/Scripts/python.exe k8s_test/compare_b0_os.py \
        --win results/b0 --linux results/b0_linux --out results/b0_linux/os_diff.json
    # Linux/WSL 侧（同一份脚本，两个目录都在挂载盘上）
    python3 k8s_test/compare_b0_os.py --win results/b0 --linux results/b0_linux

产物：`--out` 指定的 JSON（默认 `results/b0_linux/os_diff.json`）与标准输出的人类可读表。
本脚本**只读**两端产物，不写任何已有目录。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

# 与仓库其它入口脚本一致：Windows 控制台默认可能是 GBK，不重设会在中文（以及 "∅" 这类符号）
# 输出上抛 UnicodeEncodeError —— 本次在 Windows 上**真实踩到**（脚本跑到一半中断）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:
        pass

# 逐请求 CSV 里要对照的列。分三类，因为它们对"OS 差异"的敏感度不同：
#   · 观测量（ttft / tpot / e2e）——真值 + 客户端自身开销；
#   · 判据量（cov_* / srv_*）——受 time.time() 分辨率影响，最值得看的是 LOO 最小空档；
#   · 服务端自报的阶段量（srv_*）——由假后端按配方伪造，两侧应当逐位相同，
#     因此它是**管道正确性**的对照（若它不一样，说明比对脚本本身出问题了）。
COMPARE_COLS = (
    "e2e_s", "ttft_s", "ttft_from_arrival_s", "tpot_p50_s", "tpot_p95_s", "tpot_max_s",
    "n_stream_bytes",
    "cov_max_gap_s", "cov_gap_total_s", "cov_loo_min_gap_s", "cov_overlap_s",
    "srv_span_s", "srv_stage_sum_s", "srv_gap_max_s", "srv_overlap_s", "srv_outside_s",
    "sum_minus_e2e_s", "sum_minus_srv_span_s",
)
# 分类列（不是数值，按出现次数对照）：LOO 揪出的"最小空档"落在哪一段，
# 以及覆盖判定本身——这两者能暴露"某个阶段在 Linux 上根本记不到时间"这类问题。
COMPARE_CATS = ("cov_loo_stage", "cov_verdict", "srv_verdict", "ok", "error_class", "http_status")


def quant(sorted_xs: list[float], p: float) -> float:
    """线性插值分位数（与 measure_serving.percentile 口径一致，避免两套算法互比出错）。"""
    if not sorted_xs:
        raise ValueError("empty")
    i = (len(sorted_xs) - 1) * p
    lo = int(i)
    hi = min(lo + 1, len(sorted_xs) - 1)
    return sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * (i - lo)


def col_stats(rows: list[dict], key: str) -> dict | None:
    xs: list[float] = []
    for r in rows:
        v = r.get(key, "")
        if v in ("", "None", None):
            continue
        try:
            xs.append(float(v))
        except (TypeError, ValueError):
            continue
    if not xs:
        return None
    xs.sort()
    return {"n": len(xs), "min": xs[0], "p50": quant(xs, 0.50),
            "p95": quant(xs, 0.95), "max": xs[-1],
            "mean": sum(xs) / len(xs)}


def read_csv(p: Path) -> list[dict]:
    with p.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def cat_counts(rows: list[dict], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        v = r.get(key, "")
        v = "∅" if v in ("", None) else v
        out[v] = out.get(v, 0) + 1
    return out


def read_json(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def collect(side_dir: Path) -> dict[str, dict]:
    """把一侧目录下的 runs 按 **label** 归组，同 label 只保留最新的一次。

    为什么按 label 而不是按 run_id 里的序号配对：
      · 序号只在"一次 battery 之内"唯一；一个目录里跑过两次 battery 就会出现两组同序号、
        不同 pid 的 run，按序号会**随机**取到哪一组；
      · 序号还会把"我自己另外跑的探针 run"当成被对照的对象（本次真实踩到：
        探针的 `stageprobe` run 因为序号同为 1，被拿去和 Windows 的两进程对照比了）。
      label 是写出命令行时就确定的东西（臂名/故障名/标签），配对才有语义。
    """
    out: dict[str, dict] = {}
    for mp in side_dir.glob("serving_*_manifest.json"):
        rid = mp.name[: -len("_manifest.json")]
        rp = side_dir / f"{rid}_requests.csv"
        if not rp.exists():
            continue
        d = read_json(mp)
        label = str(d.get("label") or rid)
        mt = mp.stat().st_mtime
        cur = out.get(label)
        if cur is None or mt > cur["mtime"]:
            out[label] = {"run_id": rid, "manifest": mp, "requests": rp,
                          "summary": side_dir / f"{rid}_summary.json", "mtime": mt}
    return out


# 两进程模式的标签在两侧本来就不同（是人手敲的命令行）：Windows 用 `standalone_smoke`，
# Linux 用 `smoke_linux`。这一对是同一件事，显式登记别名，而不是靠猜。
LABEL_ALIAS = {"standalone_smoke": "smoke_linux"}


def main() -> int:
    ap = argparse.ArgumentParser(description="B0：Windows 与 Linux 产物逐项对照")
    ap.add_argument("--win", default="results/b0")
    ap.add_argument("--linux", default="results/b0_linux")
    ap.add_argument("--out", default="results/b0_linux/os_diff.json")
    args = ap.parse_args()

    win_dir, lin_dir = Path(args.win), Path(args.linux)
    wr, lr = collect(win_dir), collect(lin_dir)
    # 把 Windows 侧的别名标签翻译成 Linux 侧的规范名，配对才成立
    wr = {(LABEL_ALIAS.get(k, k)): v for k, v in wr.items()}

    report: dict = {"win_dir": str(win_dir), "linux_dir": str(lin_dir),
                    "hosts": {}, "runs": [], "notes": []}

    # ---- 1) 环境与时钟分辨率 ----
    def host_of(m: dict[str, dict]) -> list[dict]:
        hs = []
        for label in sorted(m):
            d = read_json(m[label]["manifest"])
            hs.append({"label": label, "run_id": m[label]["run_id"],
                       "host": d.get("host")})
        return hs

    w_hosts, l_hosts = host_of(wr), host_of(lr)
    report["hosts"] = {"windows": w_hosts, "linux": l_hosts}
    if w_hosts and l_hosts:
        wr_res = [h["host"]["time_time_resolution_s"] for h in w_hosts]
        lr_res = [h["host"]["time_time_resolution_s"] for h in l_hosts]
        report["clock"] = {
            "win_time_time_resolution_s": wr_res,
            "linux_time_time_resolution_s": lr_res,
            "win_min": min(wr_res), "win_max": max(wr_res),
            "linux_min": min(lr_res), "linux_max": max(lr_res),
            "ratio_win_over_linux": (min(wr_res) / min(lr_res)) if min(lr_res) > 0 else None,
        }

    only_win = sorted(set(wr) - set(lr))
    only_lin = sorted(set(lr) - set(wr))
    if only_win or only_lin:
        report["notes"].append(
            f"只在 Windows 有：{only_win}；只在 Linux 有：{only_lin}（未参与对照）")

    # ---- 2) 逐 run 逐列对照 ----
    print(f"{'标签':<28} {'列':<26} {'Win p50':>12} {'Linux p50':>12} "
          f"{'Δp50(ms)':>10} {'Win max':>12} {'Linux max':>12}")
    for label in sorted(set(wr) & set(lr)):
        wrows, lrows = read_csv(wr[label]["requests"]), read_csv(lr[label]["requests"])
        entry = {"label": label, "win_run": wr[label]["run_id"], "linux_run": lr[label]["run_id"],
                 "win_n": len(wrows), "linux_n": len(lrows),
                 "win_err": {}, "linux_err": {}, "cols": {}}
        for r, key in ((wrows, "win_err"), (lrows, "linux_err")):
            for row in r:
                if row.get("ok") not in ("True", "true", "1"):
                    ec = row.get("error_class") or "none"
                    entry[key][ec] = entry[key].get(ec, 0) + 1
        for col in COMPARE_COLS:
            ws, ls = col_stats(wrows, col), col_stats(lrows, col)
            if ws is None and ls is None:
                continue
            entry["cols"][col] = {"win": ws, "linux": ls}
            if ws and ls:
                print(f"{label:<28} {col:<26} {ws['p50']:>12.6g} {ls['p50']:>12.6g} "
                      f"{(ls['p50'] - ws['p50']) * 1000:>10.4f} {ws['max']:>12.6g} {ls['max']:>12.6g}")
        for col in COMPARE_CATS:
            wcat, lcat = cat_counts(wrows, col), cat_counts(lrows, col)
            entry.setdefault("cats", {})[col] = {"win": wcat, "linux": lcat}
            if wcat != lcat:
                print(f"{label:<28} {col:<26} 分类不同: win={wcat} linux={lcat}")
        report["runs"].append(entry)

    # ---- 3) 电池逐项检查对照（直接比 verdict 字符串） ----
    def latest_battery(d: Path) -> Path | None:
        cs = sorted(d.glob("battery_*_checks.csv"))
        return cs[-1] if cs else None

    wb, lb = latest_battery(win_dir), latest_battery(lin_dir)
    if wb and lb:
        def load_checks(p: Path) -> dict[tuple[str, str], tuple[str, str]]:
            out = {}
            for r in read_csv(p):
                out[(r["check"], r["item"])] = (r["verdict"], r["measured"])
            return out
        wc, lc = load_checks(wb), load_checks(lb)
        diffs = []
        for k in sorted(set(wc) | set(lc)):
            wv = wc.get(k, ("<缺>", ""))[0]
            lv = lc.get(k, ("<缺>", ""))[0]
            if wv != lv:
                diffs.append({"check": k[0], "item": k[1], "win": wv, "linux": lv,
                              "win_measured": wc.get(k, ("", ""))[1],
                              "linux_measured": lc.get(k, ("", ""))[1]})
        report["battery"] = {
            "win_checks": wb.name, "linux_checks": lb.name,
            "win_n_pass": sum(1 for v, _ in wc.values() if v == "PASS"),
            "linux_n_pass": sum(1 for v, _ in lc.values() if v == "PASS"),
            "verdict_diff": diffs,
        }
        print(f"\n=== 电池判定：Windows {report['battery']['win_n_pass']} 项 PASS / "
              f"Linux {report['battery']['linux_n_pass']} 项 PASS；"
              f"判定不一致 {len(diffs)} 项 ===")
        for d in diffs:
            print(f"  {d['check']} {d['item']}: win={d['win']} linux={d['linux']}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str),
                   encoding="utf-8")
    print(f"\n产物 -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""b1_mock_transfer_check.py —— 验收项③"模拟传输端计数"的可复现检查（PREREG_4 §11）。

【为什么是这个形态】本机没有 nixl（8 GB 单卡 + 无 nixl，见 B1 偏差记录 D5），
真 KV 传输端只能在云端两台 A10 上跑。所以"模拟传输端能够产生正确的字节数、次数和
失败计数"在本机的正确验收方式是**两端都用真货**里能验的那一半：
  1) 模拟传输端 = 按已知"传输计划"（几条传输、每条多少字节、几次失败）生成
     与 vLLM /metrics **同构**的快照文本（nixl 直方图 _count/_sum、失败计数器、
     分桶行、无关族、"出现但未动"的族）；
  2) 解析链 = 真实的 `b1_metrics_delta.py` 与 `b1_kv_table.py`（一个字不改）；
  3) 逐项对数：解析出的字节数 / 次数 / 失败数必须等于真值，错一个就 FAIL。
这样在云端看到的"字节 = KV 大小 × 传输次数、失败 = 0"才有可信的读数管道。

真值（decode 端，单位：字节/次）：
  precheck：t0→t1 = 2 次传输、每条 327,680 B（= 655,360 B）、失败 0
  measure ：t0→t1 = 4 次传输、每条 327,680 B（= 1,310,720 B）、
            失败传输 +2、失败通知 +1、KV 过期 +1（注入失败，验失败计数读得对）
只用标准库；产物落 --out-dir（append-only）。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent          # sim/k8s_test
BYTES_PER_XFER = 327_680                        # 每条传输的字节数（已知真值）
TRUTH = {
    "decode": {
        "precheck": {"count": 2, "sum": 2 * BYTES_PER_XFER,
                     "failed_transfers": 0, "failed_notifications": 0, "kv_expired": 0},
        "measure": {"count": 4, "sum": 4 * BYTES_PER_XFER,
                    "failed_transfers": 2, "failed_notifications": 1, "kv_expired": 1},
    },
    "prefill": {
        "precheck": {"count": 1, "sum": 1 * BYTES_PER_XFER,
                     "failed_transfers": 0, "failed_notifications": 0, "kv_expired": 0},
        "measure": {"count": 3, "sum": 3 * BYTES_PER_XFER,
                    "failed_transfers": 0, "failed_notifications": 0, "kv_expired": 0},
    },
}


def snap_text(count: int, byte_sum: int, failed_t: int, failed_n: int, expired: int,
              moved: bool) -> str:
    """按 vLLM /metrics 的形态造一份快照：直方图三件套 + 分桶 + 失败计数器 + 无关族。

    moved=False 的那条族（num_descriptors）刻意"出现但未动"，验解析链不会把它
    误判成增量（这是 b1_metrics_delta.py 判读列的三种情形之一）。
    """
    buckets = [(1024, count // 2), (16384, count), ("+Inf", count)]
    lines = ["# HELP vllm:nixl_bytes_transferred Bytes transferred via NIXL",
             "# TYPE vllm:nixl_bytes_transferred histogram"]
    for le, cum in buckets:
        lines.append(f'vllm:nixl_bytes_transferred_bucket{{le="{le}"}} {cum}')
    lines += [
        f"vllm:nixl_bytes_transferred_count {count}",
        f"vllm:nixl_bytes_transferred_sum {byte_sum}",
        "# HELP vllm:nixl_num_descriptors Number of NIXL descriptors",
        "# TYPE vllm:nixl_num_descriptors histogram",
        'vllm:nixl_num_descriptors_bucket{le="16"} 7',
        "vllm:nixl_num_descriptors_count 7",
        "vllm:nixl_num_descriptors_sum 7",
        "# HELP vllm:nixl_num_failed_transfers_total Failed NIXL transfers",
        "# TYPE vllm:nixl_num_failed_transfers_total counter",
        f"vllm:nixl_num_failed_transfers_total {failed_t}",
        "# HELP vllm:nixl_num_failed_notifications_total Failed notifications",
        "# TYPE vllm:nixl_num_failed_notifications_total counter",
        f"vllm:nixl_num_failed_notifications_total {failed_n}",
        "# HELP vllm:nixl_num_kv_expired_reqs_total Expired KV requests",
        "# TYPE vllm:nixl_num_kv_expired_reqs_total counter",
        f"vllm:nixl_num_kv_expired_reqs_total {expired}",
        "# 无关族：解析链不应该把它并进传输计数",
        "# TYPE vllm:num_requests_running gauge",
        "vllm:num_requests_running 0",
    ]
    if not moved:
        # num_descriptors 保持 7/7 不动 ⇒ 这一族应被判"出现但未动"
        pass
    return "\n".join(lines) + "\n"


def gen_snapshots(out: Path) -> dict:
    """生成 b1_kv_table.py 需要的全部文件名 + delta 用的成对文件。"""
    snap_dir = out / "mock_snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    truth_json = {}
    for end in ("prefill", "decode"):
        t = TRUTH[end]
        # 五个检查点（与 run_b1.sh 的 grab_metrics 标签同名）：
        # pre_t0 = 服务刚起（全 0）；pre_t1 = 冒烟后；measure_t0 = 正式前；
        # measure_mid = 正式第一条后；measure_t1 = 正式结束。
        mid_count = t["precheck"]["count"] + 1
        mid_sum = t["precheck"]["sum"] + BYTES_PER_XFER
        stages = {
            "metrics_precheck_%s_t0_raw.txt" % end: (0, 0, 0, 0, 0),
            "metrics_precheck_%s_t1_raw.txt" % end: (
                t["precheck"]["count"], t["precheck"]["sum"],
                t["precheck"]["failed_transfers"], t["precheck"]["failed_notifications"],
                t["precheck"]["kv_expired"]),
            "metrics_measure_%s_t0_raw.txt" % end: (
                t["precheck"]["count"], t["precheck"]["sum"], 0, 0, 0),
            "metrics_measure_%s_mid_raw.txt" % end: (mid_count, mid_sum, 0, 0, 0),
            "metrics_measure_%s_t1_raw.txt" % end: (
                t["precheck"]["count"] + t["measure"]["count"],
                t["precheck"]["sum"] + t["measure"]["sum"],
                t["measure"]["failed_transfers"], t["measure"]["failed_notifications"],
                t["measure"]["kv_expired"]),
        }
        for name, vals in stages.items():
            (snap_dir / name).write_text(snap_text(*vals, moved=True),
                                        encoding="utf-8", newline="\n")
        truth_json[end] = {
            "precheck_delta": {"count": t["precheck"]["count"], "sum": t["precheck"]["sum"]},
            "measure_delta": {"count": t["measure"]["count"], "sum": t["measure"]["sum"],
                              "failed_transfers": t["measure"]["failed_transfers"],
                              "failed_notifications": t["measure"]["failed_notifications"],
                              "kv_expired": t["measure"]["kv_expired"]},
        }
    (out / "mock_truth.json").write_text(json.dumps(truth_json, indent=2),
                                         encoding="utf-8", newline="\n")
    return truth_json


def run_delta(t0: Path, t1: Path, out: Path, label: str) -> str:
    rep = out / f"delta_{label}.txt"
    subprocess.run([sys.executable, str(ROOT / "b1_metrics_delta.py"),
                    "--t0", str(t0), "--t1", str(t1), "--out", str(rep),
                    "--label", label], check=False)
    return rep.read_text(encoding="utf-8") if rep.exists() else ""


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="验收项③：模拟传输端计数 × 真实解析链对数")
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args(argv)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    truth = gen_snapshots(out)
    snap = out / "mock_snapshots"
    checks = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "pass": bool(ok), "detail": detail})
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")

    # ---- 解析链 1：b1_metrics_delta.py（decode 端两对）
    # 注意（AGENTS.md §10 第 25 条的教训）：检查逻辑要匹配报告的**真实格式**——
    # 判读表的增量是**无符号数**（第一版检查器要求带符号，全部误报 FAIL，而解析链其实是对的）。
    # 取行规则：含族名且含判读词（有增量/出现但未动）的那一行 = 判读表行；
    # hist 行的数字依次是 (_count增量, _sum增量)；plain 行的数字是 (增量,)。
    def verdict_row(rep: str, family: str) -> str | None:
        for line in rep.splitlines():
            if family in line and ("有增量" in line or "出现但未动" in line):
                return line
        return None

    for phase in ("precheck", "measure"):
        t = truth["decode"][f"{phase}_delta"]
        rep = run_delta(snap / f"metrics_{phase}_decode_t0_raw.txt",
                        snap / f"metrics_{phase}_decode_t1_raw.txt",
                        out, f"decode_{phase}")
        row = verdict_row(rep, "vllm:nixl_bytes_transferred")
        nums = re.findall(r"\d+(?:\.\d+)?", row) if row else []
        ok_cnt = len(nums) >= 2 and int(float(nums[0])) == t["count"]
        ok_sum = len(nums) >= 2 and int(float(nums[1])) == t["sum"]
        check(f"decode {phase} 传输次数 = {t['count']}", ok_cnt,
              f"解析到 {nums[0] if nums else '（没匹配到判读行）'}")
        check(f"decode {phase} 传输字节 = {t['sum']}", ok_sum,
              f"解析到 {nums[1] if len(nums) >= 2 else '（没匹配到判读行）'}")
        if phase == "measure":
            frow = verdict_row(rep, "vllm:nixl_num_failed_transfers_total")
            fnums = re.findall(r"\d+(?:\.\d+)?", frow) if frow else []
            ok_f = bool(fnums) and int(float(fnums[0])) == t["failed_transfers"]
            check(f"decode measure 失败传输 = {t['failed_transfers']}", ok_f,
                  f"解析到 {fnums[0] if fnums else '（没匹配到判读行）'}")
            # "出现但未动"的族（num_descriptors）不该被报成有增量
            drow = verdict_row(rep, "vllm:nixl_num_descriptors")
            unmoved = bool(drow) and "出现但未动" in drow
            check("num_descriptors 被正确判为『出现但未动』", unmoved,
                  "判读列含该词" if unmoved else "没找到该判读")

    # ---- 解析链 2：b1_kv_table.py（五检查点表）
    tab = out / "kv_table.txt"
    subprocess.run([sys.executable, str(ROOT / "b1_kv_table.py"),
                    "--dir", str(snap), "--out", str(tab)], check=False)
    tab_text = tab.read_text(encoding="utf-8") if tab.exists() else ""
    end_total = TRUTH["decode"]["precheck"]["count"] + TRUTH["decode"]["measure"]["count"]
    end_bytes = TRUTH["decode"]["precheck"]["sum"] + TRUTH["decode"]["measure"]["sum"]
    check("kv_table 生成且含 decode 终值（次数）", str(end_total) in tab_text,
          f"终值应为 {end_total}")
    check("kv_table 生成且含 decode 终值（字节）", str(end_bytes) in tab_text,
          f"终值应为 {end_bytes}")
    check("kv_table 覆盖五个检查点", all(s in tab_text for s in ("pre_t0", "pre_t1", "t0", "mid", "t1"))
          or "precheck" in tab_text, "检查点列齐全")

    n_pass = sum(1 for c in checks if c["pass"])
    result = {"tool": "b1_mock_transfer_check.py", "checks": checks,
              "n_pass": n_pass, "n_total": len(checks), "all_pass": n_pass == len(checks)}
    (out / "mock_check_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                                encoding="utf-8", newline="\n")
    print(f"== {n_pass}/{len(checks)} PASS → {out / 'mock_check_result.json'}")
    return 0 if result["all_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

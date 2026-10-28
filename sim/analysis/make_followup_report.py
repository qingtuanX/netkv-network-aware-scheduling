# -*- coding: utf-8 -*-
"""Generate the calibrated follow-up results report (2026-10-22/23)."""
from __future__ import annotations

import glob
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass


def latest(p):
    fs = sorted(glob.glob(str(ROOT / p)))
    return pd.read_csv(fs[-1]) if fs else None


def rq2_table(src, ideal_pat, target_pat):
    rows = []
    for arm, pat in (("ideal", ideal_pat), ("equalU", target_pat)):
        d = latest(pat)
        if d is None:
            continue
        for channel, prefix in (("link", "net"), ("queue", "queue")):
            s = d[(d.network == "unstable") & d.staleness.astype(str).str.startswith(prefix)]
            if s.empty:
                continue
            agg = s.groupby(["staleness_queue_s", "staleness_network_s", "strategy"]).sojourn_p95.mean().unstack()
            for (lq, ln), r in agg.iterrows():
                if "LeastLoad" not in r or "NetworkAware2" not in r:
                    continue
                ll, na = float(r["LeastLoad"]), float(r["NetworkAware2"])
                rows.append({"arm": arm, "channel": channel, "queue_s": float(lq), "link_s": float(ln),
                             "p95_ll": ll, "p95_na2": na,
                             "benefit": (ll - na) / ll * 100.0})
    return pd.DataFrame(rows)


def disc_table(src, target_dir):
    m = latest(f"{target_dir}/method_*_runs.csv")
    c = latest(f"{target_dir}/method_ch_*_runs.csv")
    if m is None or c is None:
        return pd.DataFrame()
    d = pd.concat([m, c], ignore_index=True)
    d = d[d.staleness.isin(["q0_n0", "q0_n60", "q5_n0", "q5_n60"])]
    p = d.groupby(["workload", "network", "staleness_queue_s", "staleness_network_s", "strategy"]).sojourn_p95.mean().unstack()
    rows = []
    for (wl, net, lq, ln), r in p.iterrows():
        if "AdaptChannel" not in r or pd.isna(r.get("AdaptChannel")):
            continue
        na2 = float(r["NetworkAware2"])
        rows.append({"workload": wl, "network": net, "queue_s": float(lq), "link_s": float(ln),
                     "p95_na2": na2, "p95_ch": float(r["AdaptChannel"]),
                     "ch_vs_na2": (float(r["AdaptChannel"]) - na2) / na2 * 100.0})
    return pd.DataFrame(rows)


def main():
    out = ROOT / "results" / "CALIBRATED_FOLLOWUP_2026-10-22.md"
    eq = pd.read_csv(ROOT / "results/analysis_equtil/equtil_summary.csv")
    lines = []
    lines.append("# Calibrated Model Follow-Up Results (2026-10-22/23)")
    lines.append("")
    lines.append("Three experiments were run to close the two gaps identified in the editorial review:")
    lines.append("1. **Equal-utilization control** — does the calibrated service-time model still remove the benefit when effective load is matched?")
    lines.append("2. **RQ2 staleness under calibrated service** — does link state still retain its value at 60 s?")
    lines.append("3. **Discounting under calibrated service** — does per-channel discounting still help?")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Experiment 1: Equal-Utilization Control")
    lines.append("")
    lines.append("Design: for each (source, workload, network, seed), find the calibrated-model target rho that matches the")
    lines.append("ideal-model LeastLoad utilization, then compare dp95 at matched utilization. 20k requests, 5 seeds, linear variant.")
    lines.append("")
    lines.append("| Source | Workload | Network | dp95 ideal | dp95 nominal | dp95 equal-U | U ideal | U nominal | U equal-U | rho_eq |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for _, r in eq.iterrows():
        lines.append(f"| {r.source} | {r.workload} | {r.network} | {r.dp95_ideal:.1f} | {r.dp95_nominal:.1f} | {r.dp95_equtil:.1f} | {r.util_ideal:.3f} | {r.util_nominal:.3f} | {r.util_equtil:.3f} | {r.rho_equtil:.3f} |")
    lines.append("")
    lines.append("Key findings:")
    lines.append("- Nominal-rho comparison raises utilization from 0.22-0.47 to 0.31-0.89. Part of the dp95 shrinkage is effective load, not service time.")
    lines.append("- At matched utilization, **static** (degraded) benefit is largely preserved on synthetic (dp95 -65 vs -66 ideal) but shrinks on Azure/BurstGPT (dp95 -62 vs -77 ideal).")
    lines.append("- At matched utilization, **volatile** (unstable) benefit is **not always zero**: synthetic -6 to -8 (vs -25 ideal), Azure -14 to -20 (vs -39 ideal), BurstGPT mixed ~0. The nominal-rho comparison overstates the collapse.")
    lines.append("- Per-seed variability is large on BurstGPT; medians should be read with care.")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Experiment 2: RQ2 Staleness under Calibrated Service (equal-U)")
    lines.append("")
    lines.append("Design: targeted re-run on unstable network, link ages 0/1/5/15/60 s, queue ages 0/1/5 s, 10k requests, 3 seeds.")
    lines.append("Benefit = (p95_LessLoad - p95_NetworkAware2) / p95_LessLoad * 100.")
    lines.append("")
    for src in ("synthetic", "azure", "burstgpt"):
        ideal_pat = {"synthetic": "results/matrix_v2/staleness_*_runs.csv",
                     "azure": "results/matrix_trace_v2/staleness_*_runs.csv",
                     "burstgpt": "results/matrix_burstgpt_v2/staleness_*_runs.csv"}[src]
        target_pat = f"results/matrix_target_{src}/rq2_*_runs.csv"
        t = rq2_table(src, ideal_pat, target_pat)
        lines.append(f"### {src}")
        lines.append("")
        lines.append("| Arm | Channel | Queue s | Link s | p95 LL | p95 NA2 | Benefit % |")
        lines.append("|---|---|---|---|---|---|---|")
        for _, r in t.iterrows():
            lines.append(f"| {r.arm} | {r.channel} | {r.queue_s:g} | {r.link_s:g} | {r.p95_ll:.1f} | {r.p95_na2:.1f} | {r.benefit:.1f} |")
        lines.append("")
    lines.append("Key findings:")
    lines.append("- Under calibrated service at equal-U, **link state expires within 1-5 s**, not 60 s. Synthetic benefit drops from 8.9% (fresh) to 0.1% (1 s); Azure from 19.3% to 4.5% (1 s).")
    lines.append("- This **reverses** the ideal-model finding that link state barely expires (65-97% at 60 s).")
    lines.append("- Queue state also expires fast (1-5 s), consistent with ideal-model ordering but at different magnitudes.")
    lines.append("- On BurstGPT the baseline benefit is already negative (network-aware is worse), so staleness retention is not meaningful.")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Experiment 3: Discounting under Calibrated Service (equal-U)")
    lines.append("")
    lines.append("Design: targeted re-run, freshness (0/0, 0/60, 5/0, 5/60), 2 workloads x 2 networks x 5 seeds, 10k requests.")
    lines.append("AdaptChannel = per-channel rule; positive % means AdaptChannel is **worse** than full trust (NetworkAware2).")
    lines.append("")
    for src in ("synthetic", "azure", "burstgpt"):
        t = disc_table(src, f"results/matrix_target_{src}")
        if t.empty:
            continue
        lines.append(f"### {src}")
        lines.append("")
        lines.append("| Workload | Network | Queue s | Link s | p95 NA2 | p95 AdaptChannel | ch vs NA2 % |")
        lines.append("|---|---|---|---|---|---|---|")
        for _, r in t.iterrows():
            lines.append(f"| {r.workload} | {r.network} | {r.queue_s:g} | {r.link_s:g} | {r.p95_na2:.1f} | {r.p95_ch:.1f} | {r.ch_vs_na2:+.1f} |")
        lines.append("")
    lines.append("Key findings:")
    lines.append("- Per-channel discounting **does not survive** calibrated service. On degraded (static) links where the benefit exists, AdaptChannel is **worse** than full trust by +10 to +75% when queue state is stale.")
    lines.append("- On unstable links the rule is roughly neutral (-5 to +3%).")
    lines.append("- The paper's claim that per-channel discounting retains 90-99% of the gain is **falsified** under realistic service time.")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Implications for the Paper")
    lines.append("")
    lines.append("1. **Section 6**: The nominal-rho comparison conflates service time with effective load. At matched utilization the static benefit is largely preserved (synthetic) or partially preserved (Azure/BurstGPT); the volatile benefit shrinks but does not always vanish. The claim must be softened.")
    lines.append("2. **Section 7**: The claim that link state retains 65-97% at 60 s is **wrong** under calibrated service. Link state expires within 1-5 s. This is a major reversal.")
    lines.append("3. **Section 8**: Per-channel discounting **fails** under calibrated service. The method contribution must be demoted to an ideal-model result and the negative outcome reported.")
    lines.append("4. **Abstract/Conclusion**: Finding (3) and (4) must be rewritten to reflect the calibrated-model reality.")
    lines.append("5. The honest story becomes: network awareness pays for **static misalignment** when state is **fresh**; under realistic service, link state expires fast and discounting hurts; simpler is better.")
    lines.append("")
    lines.append("## Data Sources")
    lines.append("- Equal-U control: results/matrix_equtil_{synthetic,azure,burstgpt}/")
    lines.append("- Targeted RQ2: results/matrix_target_{source}/rq2_*_runs.csv")
    lines.append("- Targeted discount: results/matrix_target_{source}/method_*_runs.csv and method_ch_*_runs.csv")
    lines.append("- Ideal baseline: results/matrix_{source}/staleness_*_runs.csv and method_*_runs.csv")
    lines.append("- Analysis: results/analysis_equtil/, results/analysis_rq2_cal/")
    out.write_text(chr(10).join(lines), encoding="utf-8")
    print("written", out, len(lines), "lines")


if __name__ == "__main__":
    main()

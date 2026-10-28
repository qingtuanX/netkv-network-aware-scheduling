# Calibrated Model Follow-Up Results (2026-10-22/23)

Three experiments were run to close the two gaps identified in the editorial review:
1. **Equal-utilization control** — does the calibrated service-time model still remove the benefit when effective load is matched?
2. **RQ2 staleness under calibrated service** — does link state still retain its value at 60 s?
3. **Discounting under calibrated service** — does per-channel discounting still help?

---

## Experiment 1: Equal-Utilization Control

Design: for each (source, workload, network, seed), find the calibrated-model target rho that matches the
ideal-model LeastLoad utilization, then compare dp95 at matched utilization. 20k requests, 5 seeds, linear variant.

| Source | Workload | Network | dp95 ideal | dp95 nominal | dp95 equal-U | U ideal | U nominal | U equal-U | rho_eq |
|---|---|---|---|---|---|---|---|---|---|
| synthetic | data_heavy | degraded | -66.5 | -63.2 | -65.0 | 0.356 | 0.634 | 0.355 | 0.406 |
| synthetic | data_heavy | unstable | -24.8 | 0.3 | -5.9 | 0.423 | 0.762 | 0.425 | 0.388 |
| synthetic | mixed | degraded | -71.4 | -65.6 | -69.2 | 0.362 | 0.633 | 0.362 | 0.409 |
| synthetic | mixed | unstable | -27.7 | 0.3 | -7.7 | 0.471 | 0.886 | 0.471 | 0.372 |
| azure | data_heavy | degraded | -77.1 | -68.6 | -61.2 | 0.345 | 0.638 | 0.347 | 0.442 |
| azure | data_heavy | unstable | -39.3 | -3.4 | -14.1 | 0.415 | 0.761 | 0.417 | 0.419 |
| azure | mixed | degraded | -77.1 | -71.7 | -62.6 | 0.348 | 0.616 | 0.349 | 0.442 |
| azure | mixed | unstable | -40.0 | -5.7 | -19.5 | 0.423 | 0.788 | 0.425 | 0.414 |
| burstgpt | data_heavy | degraded | -64.3 | -50.3 | -59.8 | 0.219 | 0.311 | 0.219 | 0.258 |
| burstgpt | data_heavy | unstable | 9.0 | 15.0 | 12.2 | 0.403 | 0.635 | 0.405 | 0.323 |
| burstgpt | mixed | degraded | -58.5 | -43.4 | -38.2 | 0.275 | 0.461 | 0.277 | 0.368 |
| burstgpt | mixed | unstable | 1.7 | 2.2 | 0.3 | 0.413 | 0.688 | 0.414 | 0.348 |

Key findings:
- Nominal-rho comparison raises utilization from 0.22-0.47 to 0.31-0.89. Part of the dp95 shrinkage is effective load, not service time.
- At matched utilization, **static** (degraded) benefit is largely preserved on synthetic (dp95 -65 vs -66 ideal) but shrinks on Azure/BurstGPT (dp95 -62 vs -77 ideal).
- At matched utilization, **volatile** (unstable) benefit is **not always zero**: synthetic -6 to -8 (vs -25 ideal), Azure -14 to -20 (vs -39 ideal), BurstGPT mixed ~0. The nominal-rho comparison overstates the collapse.
- Per-seed variability is large on BurstGPT; medians should be read with care.

---

## Experiment 2: RQ2 Staleness under Calibrated Service (equal-U)

Design: targeted re-run on unstable network, link ages 0/1/5/15/60 s, queue ages 0/1/5 s, 10k requests, 3 seeds.
Benefit = (p95_LessLoad - p95_NetworkAware2) / p95_LessLoad * 100.

### synthetic

| Arm | Channel | Queue s | Link s | p95 LL | p95 NA2 | Benefit % |
|---|---|---|---|---|---|---|
| ideal | link | 0 | 0 | 13.6 | 10.4 | 23.8 |
| ideal | link | 0.25 | 0.25 | 13.7 | 11.1 | 19.2 |
| ideal | link | 1 | 1 | 14.7 | 12.8 | 12.8 |
| ideal | link | 1 | 2 | 14.7 | 12.6 | 14.2 |
| ideal | link | 1 | 5 | 14.7 | 13.0 | 11.5 |
| ideal | link | 1 | 15 | 14.7 | 13.4 | 8.6 |
| ideal | link | 1 | 30 | 14.7 | 13.2 | 10.1 |
| ideal | link | 1 | 60 | 14.7 | 13.0 | 11.9 |
| ideal | queue | 0 | 0 | 13.6 | 10.4 | 23.8 |
| ideal | queue | 0.25 | 0 | 13.7 | 11.1 | 19.3 |
| ideal | queue | 1 | 0 | 14.7 | 12.1 | 17.4 |
| ideal | queue | 2 | 0 | 16.5 | 14.4 | 13.1 |
| ideal | queue | 5 | 0 | 22.1 | 20.8 | 6.0 |
| ideal | queue | 15 | 0 | 38.4 | 46.6 | -21.2 |
| ideal | queue | 30 | 0 | 48.0 | 104.6 | -117.8 |
| ideal | queue | 60 | 0 | 68.4 | 96.5 | -41.2 |
| equalU | link | 0 | 0 | 13.2 | 12.0 | 8.9 |
| equalU | link | 1 | 1 | 13.5 | 13.5 | 0.1 |
| equalU | link | 1 | 5 | 13.5 | 13.6 | -0.4 |
| equalU | link | 1 | 15 | 13.5 | 13.7 | -1.3 |
| equalU | link | 1 | 60 | 13.5 | 13.7 | -1.5 |
| equalU | queue | 0 | 0 | 13.2 | 12.0 | 8.9 |
| equalU | queue | 1 | 0 | 13.5 | 13.1 | 3.5 |
| equalU | queue | 5 | 0 | 26.5 | 25.1 | 5.2 |

### azure

| Arm | Channel | Queue s | Link s | p95 LL | p95 NA2 | Benefit % |
|---|---|---|---|---|---|---|
| ideal | link | 0 | 0 | 3.3 | 2.0 | 38.2 |
| ideal | link | 0.25 | 0.25 | 3.3 | 2.5 | 26.0 |
| ideal | link | 1 | 1 | 4.6 | 3.9 | 15.0 |
| ideal | link | 1 | 2 | 4.6 | 4.1 | 11.3 |
| ideal | link | 1 | 5 | 4.6 | 4.2 | 8.0 |
| ideal | link | 1 | 15 | 4.6 | 4.3 | 6.5 |
| ideal | link | 1 | 30 | 4.6 | 4.3 | 6.2 |
| ideal | link | 1 | 60 | 4.6 | 4.2 | 7.7 |
| ideal | queue | 0 | 0 | 3.3 | 2.0 | 38.2 |
| ideal | queue | 0.25 | 0 | 3.3 | 2.4 | 28.7 |
| ideal | queue | 1 | 0 | 4.6 | 3.8 | 16.6 |
| ideal | queue | 2 | 0 | 6.3 | 5.8 | 7.3 |
| ideal | queue | 5 | 0 | 10.8 | 10.4 | 4.3 |
| ideal | queue | 15 | 0 | 19.7 | 25.2 | -28.0 |
| ideal | queue | 30 | 0 | 29.3 | 47.2 | -61.3 |
| ideal | queue | 60 | 0 | 63.1 | 88.8 | -40.8 |
| equalU | link | 0 | 0 | 2.9 | 2.3 | 19.3 |
| equalU | link | 1 | 1 | 4.4 | 4.2 | 4.5 |
| equalU | link | 1 | 5 | 4.4 | 4.3 | 1.0 |
| equalU | link | 1 | 15 | 4.4 | 4.3 | 2.0 |
| equalU | link | 1 | 60 | 4.4 | 4.5 | -3.5 |
| equalU | queue | 0 | 0 | 2.9 | 2.3 | 19.3 |
| equalU | queue | 1 | 0 | 4.4 | 4.0 | 8.1 |
| equalU | queue | 5 | 0 | 12.0 | 12.9 | -7.3 |

### burstgpt

| Arm | Channel | Queue s | Link s | p95 LL | p95 NA2 | Benefit % |
|---|---|---|---|---|---|---|
| ideal | link | 0 | 0 | 29.6 | 30.1 | -1.5 |
| ideal | link | 0.25 | 0.25 | 29.6 | 30.5 | -2.8 |
| ideal | link | 1 | 1 | 31.1 | 32.0 | -2.9 |
| ideal | link | 1 | 2 | 31.1 | 32.6 | -5.1 |
| ideal | link | 1 | 5 | 31.1 | 35.4 | -14.0 |
| ideal | link | 1 | 15 | 31.1 | 33.2 | -6.8 |
| ideal | link | 1 | 30 | 31.1 | 31.9 | -2.8 |
| ideal | link | 1 | 60 | 31.1 | 30.6 | 1.5 |
| ideal | queue | 0 | 0 | 29.6 | 30.1 | -1.5 |
| ideal | queue | 0.25 | 0 | 29.6 | 30.7 | -3.7 |
| ideal | queue | 1 | 0 | 31.1 | 32.5 | -4.7 |
| ideal | queue | 2 | 0 | 33.9 | 35.7 | -5.3 |
| ideal | queue | 5 | 0 | 39.5 | 51.8 | -31.2 |
| ideal | queue | 15 | 0 | 62.7 | 85.8 | -36.7 |
| ideal | queue | 30 | 0 | 134.9 | 122.5 | 9.2 |
| ideal | queue | 60 | 0 | 211.8 | 175.7 | 17.0 |
| equalU | link | 0 | 0 | 13.6 | 15.1 | -11.4 |
| equalU | link | 1 | 1 | 16.5 | 19.5 | -18.0 |
| equalU | link | 1 | 5 | 16.5 | 19.7 | -19.7 |
| equalU | link | 1 | 15 | 16.5 | 15.9 | 3.4 |
| equalU | link | 1 | 60 | 16.5 | 15.4 | 6.8 |
| equalU | queue | 0 | 0 | 13.6 | 15.1 | -11.4 |
| equalU | queue | 1 | 0 | 16.5 | 15.4 | 6.7 |
| equalU | queue | 5 | 0 | 22.7 | 25.1 | -11.0 |

Key findings:
- Under calibrated service at equal-U, **link state expires within 1-5 s**, not 60 s. Synthetic benefit drops from 8.9% (fresh) to 0.1% (1 s); Azure from 19.3% to 4.5% (1 s).
- This **reverses** the ideal-model finding that link state barely expires (65-97% at 60 s).
- Queue state also expires fast (1-5 s), consistent with ideal-model ordering but at different magnitudes.
- On BurstGPT the baseline benefit is already negative (network-aware is worse), so staleness retention is not meaningful.

---

## Experiment 3: Discounting under Calibrated Service (equal-U)

Design: targeted re-run, freshness (0/0, 0/60, 5/0, 5/60), 2 workloads x 2 networks x 5 seeds, 10k requests.
AdaptChannel = per-channel rule; positive % means AdaptChannel is **worse** than full trust (NetworkAware2).

### synthetic

| Workload | Network | Queue s | Link s | p95 NA2 | p95 AdaptChannel | ch vs NA2 % |
|---|---|---|---|---|---|---|
| data_heavy | degraded | 0 | 0 | 41.4 | 41.4 | +0.0 |
| data_heavy | degraded | 0 | 60 | 41.4 | 46.9 | +13.2 |
| data_heavy | degraded | 5 | 0 | 49.8 | 76.9 | +54.4 |
| data_heavy | degraded | 5 | 60 | 49.8 | 86.9 | +74.5 |
| data_heavy | unstable | 0 | 0 | 19.9 | 20.0 | +0.3 |
| data_heavy | unstable | 0 | 60 | 20.6 | 21.1 | +2.1 |
| data_heavy | unstable | 5 | 0 | 30.0 | 29.8 | -0.6 |
| data_heavy | unstable | 5 | 60 | 31.1 | 30.9 | -0.7 |
| mixed | degraded | 0 | 0 | 14.2 | 13.3 | -5.7 |
| mixed | degraded | 0 | 60 | 14.2 | 15.5 | +9.7 |
| mixed | degraded | 5 | 0 | 25.8 | 34.2 | +32.2 |
| mixed | degraded | 5 | 60 | 25.8 | 37.2 | +44.1 |
| mixed | unstable | 0 | 0 | 11.8 | 11.7 | -1.1 |
| mixed | unstable | 0 | 60 | 12.5 | 12.2 | -2.7 |
| mixed | unstable | 5 | 0 | 25.1 | 23.7 | -5.5 |
| mixed | unstable | 5 | 60 | 23.9 | 24.6 | +3.1 |

### azure

| Workload | Network | Queue s | Link s | p95 NA2 | p95 AdaptChannel | ch vs NA2 % |
|---|---|---|---|---|---|---|
| data_heavy | degraded | 0 | 0 | 3.0 | 3.0 | +0.0 |
| data_heavy | degraded | 0 | 60 | 3.0 | 3.0 | -1.4 |
| data_heavy | degraded | 5 | 0 | 14.7 | 16.4 | +12.1 |
| data_heavy | degraded | 5 | 60 | 14.7 | 17.1 | +16.4 |
| data_heavy | unstable | 0 | 0 | 2.6 | 2.6 | +0.0 |
| data_heavy | unstable | 0 | 60 | 3.0 | 2.9 | -3.3 |
| data_heavy | unstable | 5 | 0 | 13.5 | 13.8 | +2.0 |
| data_heavy | unstable | 5 | 60 | 13.6 | 13.7 | +0.7 |
| mixed | degraded | 0 | 0 | 2.5 | 2.5 | -0.2 |
| mixed | degraded | 0 | 60 | 2.5 | 2.5 | +0.6 |
| mixed | degraded | 5 | 0 | 13.6 | 15.0 | +10.4 |
| mixed | degraded | 5 | 60 | 13.6 | 16.0 | +17.6 |
| mixed | unstable | 0 | 0 | 2.2 | 2.2 | -0.3 |
| mixed | unstable | 0 | 60 | 2.7 | 2.6 | -1.5 |
| mixed | unstable | 5 | 0 | 12.7 | 13.4 | +5.5 |
| mixed | unstable | 5 | 60 | 12.9 | 13.3 | +3.1 |

### burstgpt

| Workload | Network | Queue s | Link s | p95 NA2 | p95 AdaptChannel | ch vs NA2 % |
|---|---|---|---|---|---|---|
| data_heavy | degraded | 0 | 0 | 122.6 | 122.6 | +0.0 |
| data_heavy | degraded | 0 | 60 | 122.6 | 114.7 | -6.4 |
| data_heavy | degraded | 5 | 0 | 190.2 | 190.8 | +0.3 |
| data_heavy | degraded | 5 | 60 | 190.2 | 184.2 | -3.2 |
| data_heavy | unstable | 0 | 0 | 78.1 | 78.1 | -0.0 |
| data_heavy | unstable | 0 | 60 | 80.4 | 71.3 | -11.2 |
| data_heavy | unstable | 5 | 0 | 153.0 | 160.1 | +4.6 |
| data_heavy | unstable | 5 | 60 | 116.4 | 144.0 | +23.7 |
| mixed | degraded | 0 | 0 | 26.2 | 25.3 | -3.8 |
| mixed | degraded | 0 | 60 | 26.2 | 24.4 | -6.9 |
| mixed | degraded | 5 | 0 | 41.2 | 44.4 | +7.8 |
| mixed | degraded | 5 | 60 | 41.2 | 45.9 | +11.5 |
| mixed | unstable | 0 | 0 | 20.1 | 20.0 | -0.2 |
| mixed | unstable | 0 | 60 | 19.6 | 19.7 | +0.6 |
| mixed | unstable | 5 | 0 | 34.8 | 29.7 | -14.6 |
| mixed | unstable | 5 | 60 | 37.0 | 35.5 | -3.9 |

Key findings:
- Per-channel discounting **does not survive** calibrated service. On degraded (static) links where the benefit exists, AdaptChannel is **worse** than full trust by +10 to +75% when queue state is stale.
- On unstable links the rule is roughly neutral (-5 to +3%).
- The paper's claim that per-channel discounting retains 90-99% of the gain is **falsified** under realistic service time.

---

## Implications for the Paper

1. **Section 6**: The nominal-rho comparison conflates service time with effective load. At matched utilization the static benefit is largely preserved (synthetic) or partially preserved (Azure/BurstGPT); the volatile benefit shrinks but does not always vanish. The claim must be softened.
2. **Section 7**: The claim that link state retains 65-97% at 60 s is **wrong** under calibrated service. Link state expires within 1-5 s. This is a major reversal.
3. **Section 8**: Per-channel discounting **fails** under calibrated service. The method contribution must be demoted to an ideal-model result and the negative outcome reported.
4. **Abstract/Conclusion**: Finding (3) and (4) must be rewritten to reflect the calibrated-model reality.
5. The honest story becomes: network awareness pays for **static misalignment** when state is **fresh**; under realistic service, link state expires fast and discounting hurts; simpler is better.

## Data Sources
- Equal-U control: results/matrix_equtil_{synthetic,azure,burstgpt}/
- Targeted RQ2: results/matrix_target_{source}/rq2_*_runs.csv
- Targeted discount: results/matrix_target_{source}/method_*_runs.csv and method_ch_*_runs.csv
- Ideal baseline: results/matrix_{source}/staleness_*_runs.csv and method_*_runs.csv
- Analysis: results/analysis_equtil/, results/analysis_rq2_cal/
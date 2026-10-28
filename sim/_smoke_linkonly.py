import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent / 'experiments'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_matrix as R
R.SOURCE = 'synthetic'
R.NET_SET = 'v2'
R.SEEDS = [1, 2, 3, 4, 5]
R.TARGET_RHO = 0.40
cells = R.build_matrix('method_ch', {})
targets = [c for c in cells if c['workload'] == 'mixed' and c['network'] == 'degraded' and c['seed'] == 1]
for c in targets:
    c['cell_id'] = 'smoke'
    c['target_rho'] = 0.40
    c['service_slowdown'] = 'linear'
    strategies = [
        'LeastLoad', 'NetworkAware2',
        {'name': 'NetworkAware2Adapt', 'label': 'AdaptChannel', 'decay': 'channel'},
        {'name': 'NetworkAware2Adapt', 'label': 'AdaptLinkOnly', 'decay': 'linkonly'},
    ]
    rows, tests = R.run_cell(c, strategies, 10000, 1.0)
    for row in rows:
        w = row.get('w_dyn_mean', '-')
        wstr = f'w={w:.4f}' if isinstance(w, float) else ''
        print(f"{row['staleness']:10s} {row['strategy']:15s} {wstr} p95={row['sojourn_p95']:.1f}")
    print()

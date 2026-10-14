import json
from pathlib import Path

base = Path('/root/b1/prereg3_out')
files = sorted(base.glob('arm_baseline_r*.json')) + [
    base / 'warmup_baseline_r3.json', base / 'smoke_baseline.json']
for f in files:
    d = json.loads(f.read_text(encoding='utf-8'))
    rq = d.get('requests') if isinstance(d, dict) else d
    ok = sum(1 for r in rq if r.get('http_status') == 200)
    steps = sum(len(r.get('token_ids') or []) for r in rq)
    withlp = sum(1 for r in rq if (r.get('logprobs') or {}).get('tokens'))
    print(f"{f.name}: n={len(rq)} http200={ok} 总步数={steps} 带logprobs={withlp}")

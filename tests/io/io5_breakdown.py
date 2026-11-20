#!/usr/bin/env python3
"""io5_breakdown.py DIR [ANALYZE]: per-step time breakdown of a `run_io5.py profile` campaign.

For each window in DIR/windows.json (STEP:NAME -> host [start, end] ns, CLOCK_UPTIME_RAW) this hands DIR/perf-STEP.log
and its per-vCPU request logs to the November 5 breakdown script (ANALYZE, default
natemu-raw/breakdown-20261105-01/analyze24.py: the resident's buckets over the window from the `perf` resident's 5 ms
snapshots, the host bucket split into translation requests begun in the window and the rest), then prints one table:
seconds summed over vCPUs per bucket and each bucket's share of vCPU time (window x vCPUs). Summaries: DIR/NAME.summary.json."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import json
import os
from pathlib import Path
import subprocess
import sys

from raw_root import RAW_ROOT

d = Path(sys.argv[1])
analyze = Path(sys.argv[2]) if len(sys.argv) > 2 else RAW_ROOT / 'breakdown-20261105-01/analyze24.py'
windows = json.loads((d / 'windows.json').read_text())
# The boot itself: natemu's `mark run` to the host stamp of the guest's "IO5 system running" marker (includes the login).
if (d / 'markers-boot.tsv').exists() and (d / 'perf-boot.log').exists():
    run = next(int(l.split()[2]) for l in open(d / 'perf-boot.log') if l.startswith('mark run '))
    # Runs stamped before the escape fix lack the system marker: the idle step starts within a second of it.
    up = next((int(l.split('\t')[0]) for l in open(d / 'markers-boot.tsv') if '\tIO5 system ' in l), None) or windows['boot:idle'][0]
    windows = {'boot:startup': [run, up], **windows}
# The columns: the resident's in-VM buckets worth naming, then the host bucket's two halves; the rest is "other".
SHOWN = ('guest', 'halted', 'miss', 'shadow', 'tlb', 'system', 'irq', 'delivery', 'abort', 'revalidate', 'lookup', 'place')
rows = []
for key, (w0, w1) in windows.items():
    step, name = key.split(':'); label = f'{step}-{name}'
    with open(d / 'bench.jsonl', 'a') as f:
        f.write(json.dumps({'name': label, 'bench': name, 'usage': [{'label': 'start', 't_ns': w0}, {'label': 'end', 't_ns': w1}]}) + '\n')
    for src in [f'perf-{step}.log'] + sorted(p.name for p in d.glob(f'perf-{step}.log.v*')):
        dst = d / src.replace(f'perf-{step}.log', f'{label}.perf')
        if not dst.exists(): os.symlink(src, dst)
    subprocess.run([sys.executable, analyze, d, label], check=True)
    s = json.loads((d / f'{label}.summary.json').read_text())
    n, wall = s['n'], s['wall']
    total = {}
    for w in s['window']:
        for k, v in w.items(): total[k] = total.get(k, 0) + v / 1e9
    other = sum(v for k, v in total.items() if k not in SHOWN + ('host', 'host_requests', 'host_rest', 'not_started'))
    rows.append((label, wall, n, total, other, s['compiles']))
cols = SHOWN + ('compile', 'exits+dev', 'other')
print(f"{'step':22} {'wall s':>8} " + ' '.join(f'{c:>10}' for c in cols) + f" {'compiles':>9}")
for label, wall, n, t, other, comp in rows:
    vals = [t.get(c, 0) for c in SHOWN] + [t.get('host_requests', 0), t.get('host_rest', 0), other]
    cpu = wall * n
    print(f"{label:22} {wall:8.1f} " + ' '.join(f'{v:6.1f}/{v / cpu * 100:2.0f}%' for v in vals) + f" {comp['compiles']:9d}")
    rest = sorted(((v, k) for k, v in t.items() if k not in SHOWN + ('host', 'host_requests', 'host_rest', 'not_started')), reverse=True)
    print(f"{'':22} other: " + ', '.join(f'{k} {v:.1f}' for v, k in rest[:5]))

#!/usr/bin/env python3
"""summary.py OUTDIR/bench.jsonl: per (bench, vcpus, emulator) the median (min-max) START-to-END seconds over the verified runs:
run 1 (cold, `wall_s`) and, when the record has one, run 2 (warm, `wall2_s`); and the median of each phase of run 1."""
import json, statistics as st, sys, collections
rows = [json.loads(l) for l in open(sys.argv[1])]
d = collections.defaultdict(list); bad = [r['name'] for r in rows if not r['verified']]
for r in rows:
    if r['verified']: d[(r['bench'], r['vcpus'], r['emu'])].append(r)
f = lambda v: '%.2f (%.2f-%.2f)' % (st.median(v), min(v), max(v))
for (b, n, e), rs in sorted(d.items()):
    w2 = [r['wall2_s'] for r in rs if r.get('wall2_s') is not None]
    print(b, n, e, 'runs=%d' % len(rs), 'cold', f([r['wall_s'] for r in rs]), ('warm ' + f(w2)) if w2 else '',
          ' '.join('%s=%.2f' % (p['name'], st.median([r['phases'][i]['wall_s'] for r in rs])) for i, p in enumerate(rs[0]['phases'])),
          'boot=%.1f' % st.median([r['boot_to_start_s'] for r in rs]), 'contended=%d' % sum(bool(r['contended']) for r in rs))
print('unverified:', bad)

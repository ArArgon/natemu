#!/usr/bin/env python3
"""compare.py REF RUN...: per-test diff of CT-RESULT lines. REF and each RUN are serial logs or native result files
(several files of one run, the parts of a split suite, are joined with commas). Prints per-run totals, then every test
whose fields (all but sec=) differ from REF, and the tests missing on either side."""
import re, sys

def load(spec):
    res = {}
    for f in spec.split(','):
        for line in open(f, errors='replace'):
            i = line.find('CT-RESULT ')
            if i < 0: continue
            p = line[i:].split()
            res[p[2]] = ' '.join(x for x in p[3:] if not x.startswith('sec='))
    return res

def rc(v): return re.match(r'rc=(\S+)', v).group(1)

ref = load(sys.argv[1])
for spec in sys.argv[2:]:
    run = load(spec)
    diff = sorted(k for k in ref.keys() & run.keys() if ref[k] != run[k])
    print(f'== {spec}: {len(run)} results ({len(ref)} ref), {len(diff)} differ, '
          f'{len(ref.keys() - run.keys())} missing, {len(run.keys() - ref.keys())} extra')
    for k in diff: print(f'  {k}: ref {ref[k]}  run {run[k]}')
    for k in sorted(ref.keys() - run.keys())[:50]: print(f'  missing {k}')

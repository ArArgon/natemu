#!/usr/bin/env python3
"""io5_compiles.py DIR: where a `run_io5.py run --perf-log` boot's time went, per step (M2 checkpoint B's profile).

Reads DIR/markers-use.tsv (each step's host end time and seconds), DIR/perf-use.log (the `perf` resident's snapshots:
per vCPU, cumulative ns in each of its buckets) and DIR/perf-use.log.vN (one line per translation request, host
crates/host/src/perf.rs `Recorder::request`; each file stops at 1 GiB, so late steps may have none: `covered` says how
much of the step the lines span). Per step: seconds summed over vCPUs in each resident bucket and its share of the time
not halted (`host` is the time a vCPU waits for the host, nearly all of it translation requests), then the requests
begun in the step: how many compiled, their seconds, how many were for 32-bit code, and how many compiled bytes that an
earlier compile of the boot had already compiled (the same bytes hash and length), at the same address or another."""
import collections, json, pathlib, sys

d = pathlib.Path(sys.argv[1])
BUCKETS = ('start guest host halted mailbox link miss division helper stop irq abort place system tlb delivery revalidate '
           'shadow fetch shootdown lookup patch enter l1 enter_copy reuse pool relink').split()
steps = []
for line in (d / 'markers-use.tsv').read_text().splitlines():
    t, text = line.split('\t'); m = text.split()
    if m[1] == 'step': steps.append((m[2], int(t) - int(float(m[4]) * 1e9), int(t)))
snaps = [list(map(int, l.split()[1:])) for l in open(d / 'perf-use.log') if l.startswith('snap ')]
n = (len(snaps[-1]) - 1) // len(BUCKETS)


def at(t):   # the last snapshot at or before t (binary search)
    lo, hi = 0, len(snaps) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        lo, hi = (mid, hi) if snaps[mid][0] <= t else (lo, mid - 1)
    return snaps[lo]


req = {s[0]: collections.Counter() for s in steps}
seen = {}      # (bytes hash, length) -> a RIP it was compiled at
ends = []
for f in sorted(d.glob('perf-use.log.v*')):
    began = 0
    for l in open(f):
        w = l.split()
        if len(w) < 29: continue
        began = int(w[24]); total = int(w[19]); compiled = w[7] != '0'; mode32 = int(w[21], 16) >> 2 & 3 == 2
        step = next((s[0] for s in steps if s[1] <= began < s[2]), None)
        if compiled and w[4] == 'B':
            key = (w[9], w[8]); first = seen.setdefault(key, w[2])
            again = 'new' if first is w[2] else 'same_rip' if first == w[2] else 'other_rip'
        if step is None: continue
        c = req[step]; c['requests'] += 1; c['request_ns'] += total
        if compiled:
            c['compiles'] += 1; c['compile_ns'] += total; c['fexc_ns'] += int(w[13]); c["admit_ns"] += int(w[15])
            c['compiles32'] += mode32; c['compile32_ns'] += total * mode32
            if w[4] == 'B': c[again] += 1; c[again + '_ns'] += total
    ends.append(began)
out = {}
for name, t0, t1 in steps:
    a, b = at(t0), at(t1)
    sec = {k: sum(b[1 + v * len(BUCKETS) + i] - a[1 + v * len(BUCKETS) + i] for v in range(n)) / 1e9 for i, k in enumerate(BUCKETS)}
    busy = sum(sec.values()) - sec['halted']
    c = req[name]
    out[name] = {'seconds': round((t1 - t0) / 1e9, 1), 'vcpu_busy_s': round(busy, 1),
                 'buckets_s': {k: round(v, 1) for k, v in sorted(sec.items(), key=lambda kv: -kv[1]) if v >= 0.05},
                 'share_of_busy': {k: round(v / busy, 3) for k, v in sorted(sec.items(), key=lambda kv: -kv[1]) if k != 'halted' and v / busy >= 0.01},
                 'covered': round(max(0, min(1, (min(ends) - t0) / (t1 - t0))), 2),
                 'requests': {k: (round(v / 1e9, 1) if k.endswith('_ns') else v) for k, v in sorted(c.items())}}
print(json.dumps(out, indent=1))

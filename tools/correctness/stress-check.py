#!/usr/bin/env python3
"""stress-check.py ops LIST CAL...: print LIST with each OPS set to the bogo ops its stressor did in the calibration
logs CAL (native `cal=5` runs; at least 1; a stressor without metrics keeps its value).
stress-check.py check REF RUN...: per-stressor table of run-stress.sh results. REF: native result files (comma-joined);
each RUN: serial logs or result files (comma-joined parts). A stressor is flagged when its rc, passed or skipped count
differs from REF's, or it printed `fail:` lines, or it is missing (the boot ended before it); the run's CT-DMESG lines
matching a kernel-trouble pattern (BAD) are printed, stress-ng's OOM kills counted."""
import re, sys

def lines(spec):
    for f in spec.split(','):
        yield from open(f, errors='replace')

def results(spec):
    res = {}
    for line in lines(spec):
        i = line.find('CT-RESULT stress ')
        if i < 0: continue
        p = line[i:].split()
        res[p[2]] = dict(x.split('=', 1) for x in p[3:])
    return res

def ops(lst, cal):
    bogo = {}
    for line in lines(','.join(cal)):
        m = re.search(r'CT-LOG (\S+) \| stress-ng: metrc: \[\d+\] (\S+) +(\d+) ', line)
        if m and m.group(1).split(':')[0] == m.group(2): bogo[m.group(1)] = min(100000000, max(1, int(m.group(3))))  # stress-ng allows 1..1e8
    for line in open(lst):
        p = line.split()
        if line.startswith('#') or len(p) < 3 or p[1] not in bogo: print(line, end=''); continue
        p[2] = str(bogo[p[1]]); print(' '.join(p))

# Kernel-side trouble only: OOM kills of stress-ng's own (oom_score_adj 1000) children, their reports' Call Traces and
# user faults of the signal stressors are what the stressors ask for (counted, not listed).
BAD = re.compile(r'Oops|BUG:|WARNING:|general protection fault|soft lockup|blocked for more than|rcu_.*stall|Kernel panic|'
                 r'invalid opcode:|page allocation failure|Bad page|unable to handle|corrupt|list_add|list_del')
OOM = re.compile(r'Out of memory: Killed process \d+ \(stress-ng')

def check(ref, runs):
    r = results(ref)
    for spec in runs:
        x = results(spec); flagged = 0
        for k in sorted(r.keys() | x.keys()):
            a, b = r.get(k), x.get(k)
            why = []
            if b is None: why.append('missing')
            elif a is None: why.append('not in ref')
            else:
                why += [f'{f} {a.get(f)}->{b.get(f)}' for f in ('rc', 'pass', 'skip') if a.get(f) != b.get(f)]
                if b.get('fail') != '0': why.append(f'fail lines {b.get("fail")}')
            if why: flagged += 1; print(f'  {k}: ' + ', '.join(why))
        dl = [l.strip() for l in lines(spec) if 'CT-DMESG' in l]
        dm = [l for l in dl if BAD.search(l)]
        print(f'== {spec}: {len(x)} results ({len(r)} ref), {flagged} flagged, {len(dm)} dmesg lines of note, '
              f'{sum(1 for l in dl if OOM.search(l))} stress-ng OOM kills')
        for l in dm[:40]: print('  ' + l[l.find('CT-DMESG'):])

if __name__ == '__main__':
    {'ops': lambda a: ops(a[0], a[1:]), 'check': lambda a: check(a[0], a[1:])}[sys.argv[1]](sys.argv[2:])

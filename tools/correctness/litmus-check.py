#!/usr/bin/env python3
"""litmus-check.py ORACLE_DIR RUN [RUN...]: judge litmus7 histograms against herd7's x86-TSO final states.
ORACLE_DIR holds herd7's output per family (FAMILY.txt; the corpus' oracle/). Each RUN is a serial log or native
result file of run-litmus.sh (several files of one run joined with commas). Per run: tests run, skipped, failed to
run; FORBIDDEN (a state x86-TSO does not allow was observed: a defect; test, state, count); and the allowed states
never observed (weaker evidence), in particular tests whose exists-condition x86-TSO allows but that never showed it."""
import os, re, sys
from collections import defaultdict

def norm(s):
    kv = []
    for x in s.split(';'):
        x = x.strip()
        if not x: continue
        k, v = x.split('=')
        kv.append((k.strip().strip('[]').lower(), int(v.strip(), 0)))
    return tuple(sorted(kv))

def oracle(d):
    allowed, cond, kind = {}, {}, {}
    for f in sorted(os.listdir(d)):
        if not f.endswith('.txt'): continue
        fam, lines, name = f[:-4], open(f'{d}/{f}').read().split('\n'), None
        i = 0
        while i < len(lines):
            m = re.match(r'Test (\S+) (\S+)', lines[i])
            if m:
                name = f'{fam}.{m.group(1)}'; kind[name] = m.group(2); n = int(lines[i + 1].split()[1])
                allowed[name] = {norm(l) for l in lines[i + 2:i + 2 + n]}
                cond[name] = lines[i + 2 + n].strip()          # Ok: the exists-condition is reachable under x86-TSO
                i += 2 + n
            i += 1
    return allowed, cond, kind

def run(spec):
    hist, rc, skip, obs = defaultdict(lambda: defaultdict(int)), {}, set(), {}
    for f in spec.split(','):
        cur = None
        for line in open(f, errors='replace'):
            m = re.search(r'CT-LOG (\S+) \| (.*)', line)
            if m:
                t, body = m.groups()
                h = re.match(r'(\d+)\s*[*:]>(.*)', body)
                if h: hist[t][norm(h.group(2))] += int(h.group(1))
                continue
            m = re.search(r'CT-RESULT litmus (\S+) rc=(\S+) obs=(\S*)', line)
            if m: rc[m.group(1)] = m.group(2); obs[m.group(1)] = m.group(3)
            m = re.search(r'CT-SKIP litmus (\S+)', line)
            if m: skip.add(m.group(1))
    return hist, rc, skip, obs

allowed, cond, kind = oracle(sys.argv[1])
for spec in sys.argv[2:]:
    hist, rc, skip, obs = run(spec)
    bad = sorted(t for t, v in rc.items() if v != '0' or t not in hist)
    forb = [(t, s, c) for t in sorted(hist) for s, c in hist[t].items() if s not in allowed.get(t, ())]
    unknown = sorted(t for t in hist if t not in allowed)
    unseen = {t: allowed[t] - set(hist[t]) for t in hist if t in allowed}
    # exists-tests whose condition x86-TSO allows (herd7 "Ok") but this run never showed (litmus7 "Never")
    cond_unseen = sorted(t for t in rc if kind.get(t) == 'Allowed' and cond.get(t) == 'Ok' and obs.get(t) == 'Never')
    print(f'== {spec}: {len(rc)} run, {len(skip)} skipped, {len(bad)} failed to run, {len(forb)} FORBIDDEN states observed '
          f'in {len({t for t, _, _ in forb})} tests, {sum(len(v) for v in unseen.values())} of '
          f'{sum(len(allowed[t]) for t in unseen)} allowed states never observed ({sum(1 for v in unseen.values() if v)} tests); '
          f'{len(cond_unseen)} of {sum(1 for t in rc if kind.get(t) == "Allowed" and cond.get(t) == "Ok")} exists-tests with an '
          f'x86-TSO-allowed condition never showed it')
    for t in cond_unseen: print(f'  condition allowed, never observed: {t}')
    for t in bad: print(f'  did not run cleanly: {t} rc={rc.get(t)}')
    for t in unknown: print(f'  no oracle: {t}')
    for t, s, c in forb: print(f'  FORBIDDEN {t}: {"; ".join(f"{k}={v}" for k, v in s)} x{c}')
    print('  allowed states never observed, per test (exists-condition reachable under x86-TSO marked Ok):')
    for t in sorted(unseen):
        if unseen[t]: print(f'    {t} [{cond.get(t)}] {len(unseen[t])}/{len(allowed[t])}: ' +
                            ' | '.join('; '.join(f'{k}={v}' for k, v in s) for s in sorted(unseen[t])))

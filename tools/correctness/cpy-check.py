#!/usr/bin/env python3
"""cpy-check.py REF RUN...: per-test comparison of run-cpy.sh outputs (native result files or serial logs; the parts of a
split run joined with commas). Per test module: regrtest's verdict (from its per-test progress lines and closing summary:
ok, failed, skipped, resource denied, env changed, run no tests, ...) and the JUnit case counts; per test case: its status
when not passed. Prints per-run totals, then every module whose verdict or counts differ from REF and every case whose
status differs. `cpy-check.py hashes REF RUN`: compare the CT-HASH lines of two cpy-build.sh outputs."""
import re, sys
from collections import Counter

PROG = re.compile(r'\[\s*\d+/\d+(?:/\d+)?\] (\S+)')
HEAD = re.compile(r'^(?:All )?(\d+) tests? (.*?):?$')

def load(spec):
    mods, verdict, counts, cases, ends = set(), {}, {}, {}, 0
    for f in spec.split(','):
        sect = None
        for line in open(f, errors='replace'):
            line = line.rstrip('\n')
            i = line.find('CT-')
            if i < 0: continue
            line = line[i:]
            if line.startswith('CT-END'): ends += 1
            if line.startswith('CT-CASE '):
                cid, st = line[len('CT-CASE '):].rsplit(None, 1); cases[cid] = st; continue
            if line.startswith('CT-RESULT cpy '):
                p = line.split(); c = counts.setdefault(p[2], Counter())   # a module's cases may be reported by several parts
                for x in p[3:]:
                    k, v = x.split('='); c[k] += 0 if k == 'sec' else int(v)
                continue
            if not line.startswith('CT-LOG | '): continue
            body = line[9:]
            m = PROG.search(body)
            if m and not body.startswith(' '): mods.add(m.group(1)); sect = None; continue
            m = HEAD.match(body)
            if m and ('test' in body) and not body.startswith(' '):
                s = m.group(2)
                sect = None if s.startswith(('OK', 'slowest')) or 'slowest' in s else s
                continue
            if sect and body.startswith('    '):
                for t in body.split(): verdict[t] = sect
            elif body.strip() == '': continue
            else: sect = None
    v = {t: verdict.get(t, 'ok') for t in mods | set(verdict)}
    counts = {t: ' '.join(f'{k}={c[k]}' for k in ('cases', 'pass', 'fail', 'error', 'skip')) for t, c in counts.items()}
    return dict(verdict=v, counts=counts, cases=cases, ends=ends)

def main():
    if sys.argv[1] == 'hashes':
        h = [dict(reversed(l.split()[1:3]) for l in open(f, errors='replace') if l.find('CT-HASH ') >= 0
                  for l in [l[l.find('CT-HASH '):]]) for f in sys.argv[2:4]]
        a, b = h
        diff = sorted(k for k in a.keys() & b.keys() if a[k] != b[k])
        print(f'{len(a)} ref, {len(b)} run, {len(diff)} differ, {len(a.keys() - b.keys())} missing, {len(b.keys() - a.keys())} extra')
        for k in diff: print('  differ', k)
        for k in sorted(a.keys() ^ b.keys()): print('  only in', 'ref' if k in a else 'run', k)
        return
    ref = load(sys.argv[1])
    for spec in [sys.argv[1]] + sys.argv[2:]:
        r = load(spec)
        print(f'== {spec}: {len(r["verdict"])} modules ({dict(Counter(r["verdict"].values()))}), '
              f'{sum(int(c.split()[0][6:]) for c in r["counts"].values())} cases, CT-END x{r["ends"]}')
        if r is ref or spec == sys.argv[1]: continue
        missing = sorted(ref['verdict'].keys() - r['verdict'].keys())
        print(f'  {len(missing)} modules of REF not in this run' + (': ' + ' '.join(missing) if len(missing) <= 40 else ''))
        both = ref['verdict'].keys() & r['verdict'].keys()
        for t in sorted(both | (r['verdict'].keys() - ref['verdict'].keys())):
            a, b = ref['verdict'].get(t, '-'), r['verdict'].get(t, '-')
            ca, cb = ref['counts'].get(t, '-'), r['counts'].get(t, '-')
            if a != b or ca != cb: print(f'  {t}: ref {a} [{ca}]  run {b} [{cb}]')
        keys = sorted(((t if t.startswith('test.') else 'test.' + t) + '.') for t in both)
        mine = lambda c: any(c.startswith(k) for k in keys)
        for c in sorted(ref['cases'].keys() | r['cases'].keys()):
            a, b = ref['cases'].get(c, 'pass'), r['cases'].get(c, 'pass')
            if a != b and mine(c): print(f'    case {c}: ref {a}  run {b}')

if __name__ == '__main__':
    main()

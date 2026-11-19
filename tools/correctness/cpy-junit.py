"""cpy-junit.py JUNIT LIST OUT: run-cpy.sh's per-module results from regrtest's JUnit file. Prints `CT-CASE ID STATUS` per
case not passed and `CT-RESULT cpy MODULE cases=C pass=P fail=F error=E skip=S sec=T` per module of LIST (the modules
run, as regrtest was given them) that reported cases. Run by the image's own interpreter (python -I), so standard
library only.

A case belongs to the module its id starts with (`test.MODULE.`; the longest such module of LIST). Some cases carry
another module's name: doctests of the module tested (test_enum's `enum.Enum`), shared test bodies
(test_fork1's `test.fork_wait.ForkWait.test_wait`). regrtest writes one <testsuite> element per unittest run of a
module, in the order it reports the modules, so:
  - a case no module of LIST names belongs to the module most of its <testsuite>'s cases name;
  - a <testsuite> none of whose cases names a module belongs to the one module regrtest reported (OUT, regrtest's
    output: its `[N/M] MODULE ...` progress lines) between the modules of the nearest attributed elements before and
    after it whose progress line says passed (not skipped, failed or crashed) and owns no other element;
  - otherwise its cases are reported under `?FIRST` (FIRST their ids' first word), which no list names: the harness
    then fails them as unlisted, rather than credit them to a guessed module."""
import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict

PROG = re.compile(r'\[\s*\d+/\d+(?:/\d+)?\] (\S+)(.*)$')


def status(c):
    if c.find('error') is not None: return 'error'
    if c.find('failure') is not None: return 'fail'
    if c.find('skipped') is not None or c.get('status') == 'skipped': return 'skip'
    return 'pass'


def attribute(suites, names, out_lines):
    """[(case element, module)] for every case of the <testsuite> elements, in file order."""
    keys = [((t if t.startswith('test.') else 'test.' + t) + '.', t) for t in names]

    def named(i):
        return max(((len(k), t) for k, t in keys if i.startswith(k)), default=(0, None))[1]

    order, ran = [], set()
    for line in out_lines:
        if line.startswith(' '): continue
        m = PROG.search(line)
        if not m or m.group(1) in order: continue
        order.append(m.group(1))
        if m.group(2).strip().startswith('passed'): ran.add(m.group(1))     # not skipped, failed or crashed
    pos = {t: k for k, t in enumerate(order)}
    cases = [list(s.iter('testcase')) for s in suites]
    owner = []
    for cs in cases:
        got = [g for g in (named(c.get('name') or '') for c in cs) if g]
        owner.append(max(sorted(set(got)), key=got.count) if got else None)
    owned = set(filter(None, owner))
    for j, o in enumerate(owner):
        if o: continue
        before = next((owner[i] for i in range(j - 1, -1, -1) if owner[i]), None)
        after = next((owner[i] for i in range(j + 1, len(owner)) if owner[i]), None)
        a, b = pos.get(before, -1), pos.get(after, len(order))
        gap = [t for t in order[a + 1:b] if t in names and t in ran and t not in owned]
        if len(gap) == 1: owner[j] = gap[0]
    out = []
    for cs, o in zip(cases, owner):
        for c in cs:
            i = c.get('name') or ''
            out.append((c, named(i) or o or '?' + (i.split('.')[0].split() or [''])[0]))
    return out


def main(junit, listing, regrtest_out):
    with open(listing) as f: names = f.read().split()
    suites = list(ET.parse(junit).getroot().iter('testsuite'))
    with open(regrtest_out, errors='replace') as f:
        lines = f.read().splitlines()
    n = defaultdict(lambda: [0, 0, 0, 0, 0]); sec = defaultdict(float)
    for c, m in attribute(suites, names, lines):
        s = status(c)
        sec[m] += float(c.get('time') or 0); r = n[m]; r[0] += 1; r[('pass', 'fail', 'error', 'skip').index(s) + 1] += 1
        if s != 'pass': print('CT-CASE', c.get('name'), s)
    for m in sorted(n):
        print('CT-RESULT cpy %s cases=%d pass=%d fail=%d error=%d skip=%d sec=%.1f' % ((m,) + tuple(n[m]) + (sec[m],)))


if __name__ == '__main__':
    main(*sys.argv[1:4])

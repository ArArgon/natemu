#!/usr/bin/env python3
"""cases.py STRESS-LIST: stress-cases.txt for the stress campaign image (build.sh; testkit/harness/campaign.py), one
`PART TEST` line per stressor run, from the same tools/correctness/stress-list.txt that run-stress.sh reads in the guest.

TEST is the name run-stress.sh gives the run's CT-RESULT line, `STRESSOR[:LABEL].rI` (I = 1: one repeat), and PART the
kernel's part= word run-stress.sh takes, `FAMILY,seed=SEED,n=N`. Parts come in the order the list first names their
family; within a part the stressors run in list order (run-stress.sh greps the whole list for the family).

One pass, as the original campaign's first (docs/restructure/correctness-stress.md, "Families"): seed 1, N = 4
instances per stressor, except
  - mix: n = 1, one instance of each of its stressors at once (the list's own comment; the original mix boots ran so,
    11 + 12 passed instances);
  - ipc1t and code1t are left out: they are the tails of ipc1 and code1, re-run only where an N = 2 boot reached the
    900 s cap, and at N = 4 they repeat ipc1's and code1's stressors under the same CT-RESULT names, which the image's
    list may hold only once.
"""
import sys

SEED = 1
N = 4
ONE_EACH = {'mix': 1}                      # family -> n (part n=)
TAILS = ('ipc1t', 'code1t')                # left out (above)


def entries(text: str) -> list[tuple[str, str]]:
    """(family, STRESSOR[:LABEL]) per run, in list order, as run-stress.sh reads the list (comments are lines whose
    first character is #, fields split on white space)."""
    out = []
    for line in text.splitlines():
        if line.startswith('#'): continue
        w = line.split()
        if len(w) < 3: continue
        out.append((w[0], w[1]))
    return out


def cases(text: str, seed: int = SEED, n: int = N) -> list[tuple[str, str]]:
    """(PART, TEST) per run of every family but the tails."""
    out = []
    for fam, sl in entries(text):
        if fam in TAILS: continue
        out.append((f'{fam},seed={seed},n={ONE_EACH.get(fam, n)}', f'{sl}.r1'))
    order = list(dict.fromkeys(p for p, _ in out))
    return sorted(out, key=lambda c: order.index(c[0]))   # stable: list order within a part


def main(argv):
    got = cases(open(argv[1]).read())
    names = [t for _, t in got]
    if dup := sorted({t for t in names if names.count(t) > 1}):
        sys.exit(f'{argv[1]}: CT-RESULT names listed twice: {dup}')
    for part, test in got: print(f'{part} {test}')


if __name__ == '__main__':
    main(sys.argv)

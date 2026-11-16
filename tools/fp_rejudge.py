#!/usr/bin/env python3
"""Re-judges stored runner output with the current deviation rule (tools/fp_deviations.py), without a VM.

    python3 tools/fp_rejudge.py RUNNER RESULTS_DIR --host m2|m4 [--raw NATEMU_RAW]

RUNNER: run_i5c, run_i5a_de, run_i8_faults (RESULTS_DIR holds a run's results.json) or i8, i8b (the corpora's, one step's out
directory). `--host` is what the stored run's report would say (the old reports lack `afp` and `fp`): m2 is `afp` false, `fp`
fast (M2 emulation), m4 is `afp` true. The three runners store the failing cases' messages, not the driver's report: a failed
case's MXCSR is read from its message (`mxcsr 0x1fa0, want 0x1fa2`), a passed case's is the exact value; the corpora store each
program's report, so they are judged by the real code. Prints the runners' count line; exits 1 on any failure (and, for i8b, when
`run_suite.i8b_judge` rejects it).
"""
import argparse, json, pathlib, re, sys
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools')); sys.path.insert(0, str(ROOT / 'tools/reference'))
import fp_deviations as dev


def runner_mx(runner):
    """{case name: structured expected MXCSR or None} of a runner's cases."""
    if runner == 'run_i5c':
        import run_i5c
        return {c['name']: c['mx'] for c in run_i5c.cases()}
    if runner == 'run_i8_faults':
        import run_i8_faults
        return {c['name']: run_i8_faults.mx_of(c) for c in run_i8_faults.cases()}
    import run_i5a_de
    return {name: run_i5a_de.mx_of(mx, de) for name, _, _, _, mx, de in run_i5a_de.cases()}


def stored_cases(runner, results):
    """(name, ok, got MXCSR or None, other problems) of each stored case."""
    for r in results:
        if runner == 'run_i5a_de':
            yield r['case'], r['ok'], (int(r['mxcsr_out'], 0) & 2) | (int(r['mxcsr_in'], 0) & ~2), []
            continue
        mx = [p for p in r['problems'] if re.match(r'mxcsr 0x', p)]
        other = [p for p in r['problems'] if p not in mx]
        yield r['name'], r['ok'], int(re.match(r'mxcsr (0x[0-9a-f]+)', mx[0])[1], 0) if mx else None, other


def rejudge_runner(runner, out, report):
    results = json.loads((out / 'results.json').read_text())
    if isinstance(results, dict): results = results['results']
    mxs, counts, failed = runner_mx(runner), dev.Counts(), []
    for name, ok, got, other in stored_cases(runner, results):
        m = mxs[name]
        if other: kind = 'fail'
        elif m is None: kind = 'ok' if ok else 'fail'
        else:
            if got is None:       # passed: the exact value (a case with alternatives: the first)
                got = m['want'] if isinstance(m['want'], int) else m['want'][0]
            kind = dev.mxcsr_verdict(m, report, got)[0]
        counts.add(kind, m and m['tag'])
        if kind == 'fail': failed.append(name)
    print(f"{runner} {out}: afp {report['afp']}: " + counts.line())
    for n in failed[:20]: print('  FAIL', n)
    return counts.bad


def rejudge_corpus(corpus, out, report, raw):
    import run_slice1_vm as vm1, run_suite
    d = {'i8': 'i8-corpus-20261030-01', 'i8b': 'i8-corpus-20261030-02'}[corpus]
    tags = dev.corpus_deviations(d)
    progs = {p['name']: p for p in json.loads((raw / d / 'programs.json').read_text())['programs']}
    exp = {r['name']: r for r in json.loads((raw / d / 'expected.json').read_text())['results']}
    stored = json.loads((out / 'results.json').read_text())['results']
    new, counts = [], {'match': 0, 'unsupported': 0, 'fail': 0, 'deviated': 0}
    for r in stored:
        outcome, diffs = vm1.evaluate(progs[r['name']], exp[r['name']], {**r['vm'], **report}, tags.get(r['name']))
        counts[outcome] += 1
        new.append({**{k: v for k, v in r.items() if k != 'vm'}, 'outcome': outcome, 'diffs': diffs, 'vm': {**r['vm'], **report}})
    print(f'{corpus} {out}: afp {report["afp"]}: {counts}')
    ok = not counts['fail']
    if corpus == 'i8b':
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            (pathlib.Path(t) / 'results.json').write_text(json.dumps({'results': new}))
            ok, note = run_suite.i8b_judge(pathlib.Path(t))
        print(f'  i8b judge: {"PASS" if ok else "FAIL"} ({note})')
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('runner', choices=['run_i5c', 'run_i5a_de', 'run_i8_faults', 'i8', 'i8b']); ap.add_argument('results', type=pathlib.Path)
    ap.add_argument('--host', choices=['m2', 'm4'], required=True); ap.add_argument('--raw', type=pathlib.Path, default=ROOT.parent / 'natemu-raw')
    a = ap.parse_args()
    report = {'afp': a.host == 'm4', 'fp': 'fast'}
    bad = rejudge_corpus(a.runner, a.results, report, a.raw) if a.runner in ('i8', 'i8b') else rejudge_runner(a.runner, a.results, report)
    sys.exit(1 if bad else 0)

if __name__ == '__main__': main()

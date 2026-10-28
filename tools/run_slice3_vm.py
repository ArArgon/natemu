#!/usr/bin/env python3
"""N vCPUs on the general execution path (slice 3, S3a): several programs at once in one VM, one per vCPU.

    tools/with-env python3 tools/run_slice3_vm.py --fexc FEXC --out DIR corpora [--cpus 2 3 4] [--max-block N]
        [--parallel P] [--seed S] [--flow PROGRAMS.json EXPECTED.json] [--no-faults]
    tools/with-env python3 tools/run_slice3_vm.py --fexc FEXC --out DIR smp [--cpus 2 3 4] [--programs a,b] [--reps R]
        [--parallel P] [--max-block N]
    tools/with-env python3 tools/run_slice3_vm.py --fexc FEXC --out DIR limits

`corpora`: the single-vCPU corpora — the slice-1 fixtures, the control-flow corpus (default: the S2d run's
programs and QEMU results under NATEMU_RAW_ROOT/s2d/corpus) and the slice-1 fault cases (QEMU prefix references, as
tools/run_slice1_faults.py, its paging cells included except those with a 2 MiB page: guest RAM holds one 2 MiB
frame per run) — are shuffled (seed) and grouped N at a time; each group runs as one multi-vCPU
description (driver `cpu` sections), every program in its own address space (own tables, disjoint guest-physical
pages) and the same linear addresses, all vCPUs concurrently. Each vCPU's final state is judged by its corpus's own
evaluator against its own single-program reference. Also per vCPU: EnTSO read back, the FEX frame intact, and, when
the cache neither flushed (beyond the driver's initial one) nor invalidated, host requests = distinct block RIPs.
`--parallel P` runs P groups (processes, each a VM) at once: with P * N above the core count the vCPU threads are
oversubscribed and migrate between cores.

`smp`: the deterministic programs of tools/bench/smp (natemu-smp-v1: the bytes QEMU system mode ran) at N vCPUs over one
shared address space, judged against tools/bench/smp/model_smp.py with smp.compare. Programs using an instruction the
general path does not admit yet are reported `unsupported` when a vCPU stops on it (`unsupported_form`): handshake
and token-ring spin with PAUSE, xchg-lock needs memory XCHG (S3e). Run to the end, they are judged like the others.

`limits`: 5 vCPUs are refused (0 cannot be written: a description without `cpu` lines is one vCPU; the host's
`prepare` refuses 0 in its unit test); a forged block header on vCPU V of 4 (V = 0..3; the others spin in a linked
loop) halts V with bad_link, cancels and joins the others, and the VM is destroyed (the driver reports only then).

Writes DIR/<mode>.json; exit status 1 on any failure."""
import argparse, collections, concurrent.futures as cf, json, os, pathlib, random, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools')); sys.path.insert(0, str(ROOT / 'tools' / 'reference'))
sys.path.insert(0, str(ROOT / 'tools' / 'bench' / 'smp'))
import ref, run_slice1_vm as vm, run_slice1_faults as faults, smp, model_smp  # noqa: E402

# Programs needing an instruction not admitted yet (fexc refuses it: `unsupported_form`).
REFUSED = {'handshake': 'PAUSE', 'token-ring': 'PAUSE', 'xchg-lock': 'memory XCHG'}


def driver(host, runtime, fexc, text, work, extra=()):
    """Runs one description; (returncode, stdout JSON or None, stderr)."""
    work.mkdir(parents=True, exist_ok=True)
    (work / 'program.txt').write_text(text)
    run = subprocess.run([host, runtime, 'run-program', fexc, work / 'program.txt', work, *map(str, extra)], capture_output=True, text=True)
    out = json.loads(run.stdout) if run.returncode == 0 else None
    return run.returncode, out, run.stderr[-2000:]


def per_vcpu_checks(out):
    """Checks every vCPU's output must pass whatever it ran."""
    problems = []
    if not out.get('tso'): problems.append('EnTSO not read back')
    if not out.get('frame_intact'): problems.append('FEX frame changed')
    st = out['stats']
    if st['flushes'] == 1 and st['invalidations'] == 0 and st['steps'] == 0 and st['host_requests'] != st['distinct']:
        problems.append(f"host requests {st['host_requests']} != distinct blocks {st['distinct']}")
    return problems


def corpus_items(a):
    """[(name, single description, evaluate(out) -> (outcome, diffs))]."""
    items = []
    fixtures = json.loads((ROOT / 'fixtures/slice1/programs.json').read_text())['programs']
    expected = {r['name']: r for r in json.loads((ROOT / 'fixtures/slice1/expected.json').read_text())['results']}
    for p in fixtures:
        items.append(('fixture/' + p['name'], vm.description(p), lambda out, p=p: vm.evaluate(p, expected[p['name']], out)))
    if a.flow:
        programs = json.loads(a.flow[0].read_text())['programs']
        results = {r['name']: r for r in json.loads(a.flow[1].read_text())['results']}
        for p in programs:
            items.append(('flow/' + p['name'], vm.description(p), lambda out, p=p: vm.evaluate(p, results[p['name']], out)))
    if not a.no_faults:
        for case in faults.expectations(fixtures, faults.probes(), a.out / 'ref'):
            if case[2].get('large'): continue       # guest RAM holds one 2 MiB frame per run, not one per vCPU
            def judge(out, case=case):
                e = faults.evaluate(case, out)
                return e['outcome'], e['diffs']
            items.append((f'fault/{case[0]}', faults.case_description(case), judge))
    return items


def run_groups(a, host, runtime, groups, judge_group):
    """Runs every (label, text, extra) group (work directory: label), `a.parallel` at a time; judge_group(index, rc,
    out, err) -> entries."""
    entries = []
    with cf.ThreadPoolExecutor(a.parallel) as pool:
        futures = [pool.submit(driver, host, runtime, a.fexc, text, a.out / 'work' / label, extra)
                   for label, text, extra in groups]
        for i, f in enumerate(futures):
            rc, out, err = f.result()
            entries += judge_group(i, rc, out, err)
    return entries


def cmd_corpora(a, host, runtime):
    items = corpus_items(a)
    print(f'{len(items)} programs', flush=True)
    results, failed = [], 0
    for n in a.cpus:
        order = items[:]
        random.Random(a.seed + n).shuffle(order)
        order += order[:(-len(order)) % n]          # the last group is filled from the start
        groups = [order[i:i + n] for i in range(0, len(order), n)]
        limit = [a.max_block] if a.max_block else []
        texts = [(f'n{n}g{i}', ''.join('cpu\n' + text for _, text, _ in g), limit) for i, g in enumerate(groups)]

        def judge(i, rc, out, err, groups=groups, n=n):
            entries = []
            for v, (name, _, evaluate) in enumerate(groups[i]):
                e = {'n': n, 'group': i, 'vcpu': v, 'name': name}
                if rc or len(out['cpus']) != n:
                    e.update(outcome='fail', diffs=[f'driver exit {rc}: {err.strip()[-300:]}'])
                else:
                    cpu = out['cpus'][v]
                    outcome, diffs = evaluate(cpu)
                    diffs = list(diffs) + per_vcpu_checks(cpu)
                    if outcome == 'match' and diffs: outcome = 'fail'
                    e.update(outcome=outcome, diffs=diffs, stop=cpu['stop'], stats=cpu['stats'])
                entries.append(e)
            return entries
        entries = run_groups(a, host, runtime, texts, judge)
        counts = collections.Counter(e['outcome'] for e in entries)
        for e in entries:
            if e['outcome'] == 'fail': print(f"N={n} g{e['group']} vcpu{e['vcpu']} {e['name']}: {'; '.join(e['diffs'])[:300]}", flush=True)
        print(f'N={n}: {len(groups)} runs, {dict(counts)}', flush=True)
        failed += counts['fail']
        results += entries
    (a.out / 'corpora.json').write_text(json.dumps({'results': results}, indent=1, default=str) + '\n')
    return failed


def smp_description(prog):
    """natemu-smp-v1 -> driver description: the bodies and memory form the shared space, one `cpu` section per CPU."""
    lines = [f"body {smp.num(b['addr']):#x} {b['code']}" for b in prog['bodies']]
    lines += [f"memory {smp.num(r['addr']):#x} {smp.region_bytes(r).hex()}" for r in prog['memory']]
    for c in prog['cpus']:
        lines += ['cpu', f"rip {smp.num(c['rip']):#x}", f"rflags {smp.num(c['rflags']):#x}",
                  'gpr ' + ' '.join(f'{smp.num(v):#x}' for v in c['gpr'])]
    return '\n'.join(lines) + '\n'


def body_end(prog, rip):
    return next(smp.num(b['addr']) + len(b['code']) // 2 for b in prog['bodies']
                if smp.num(b['addr']) <= rip < smp.num(b['addr']) + len(b['code']) // 2)


def cmd_smp(a, host, runtime):
    programs = [p for p in json.loads((ROOT / 'tools/bench/smp/programs.json').read_text())
                if p['ncpu'] in a.cpus and (not a.programs or p['name'].split('@')[0] in a.programs)]
    groups = [(f"{p['name']}-{r}", smp_description(p), [a.max_block] if a.max_block else []) for p in programs for r in range(a.reps)]
    order = [p for p in programs for _ in range(a.reps)]

    def judge(i, rc, out, err):
        prog = order[i]
        base = prog['name'].split('@')[0]
        e = {'name': prog['name'], 'rep': i}
        if rc:
            outcome = 'unsupported' if base in REFUSED and 'unsupported_form' in err else 'fail'
            return [{**e, 'outcome': outcome, 'diffs': [f'driver exit {rc}: {err.strip()[-300:]}']}]
        cpus = out['cpus']
        diffs = []
        for v, (c, cpu) in enumerate(zip(prog['cpus'], cpus)):
            end = body_end(prog, smp.num(c['rip']))
            if cpu['stop'] != 'reached' or smp.num(cpu['rip']) != end:
                diffs.append(f"cpu{v} stopped {cpu['stop']} {cpu['reason']} at {cpu['rip']} (end {end:#x})")
            diffs += [f'cpu{v} ' + d for d in per_vcpu_checks(cpu)]
        captured = [r for r in prog['memory'] if r.get('capture', True)]
        memory = {smp.num(m['addr']): bytes.fromhex(m['data']) for m in cpus[0]['memory']}
        got = {'cpus': [{'gpr': [smp.num(x) for x in cpu['gpr']], 'rflags': smp.num(cpu['rflags'])} for cpu in cpus],
               'memory': [{'addr': smp.num(r['addr']), 'data': memory[smp.num(r['addr'])]} for r in captured]}
        diffs += smp.compare(prog, got, model_smp.expected(prog))
        if base in REFUSED and any('unsupported_form' in d for d in diffs):
            outcome = 'unsupported'
            diffs = [f'{REFUSED[base]} not admitted'] + diffs
        else: outcome = 'fail' if diffs else 'match'
        stats = [{k: cpu['stats'][k] for k in ('blocks', 'fast', 'requests', 'links', 'retries')} for cpu in cpus]
        return [{**e, 'outcome': outcome, 'diffs': diffs, 'stats': stats}]
    entries = run_groups(a, host, runtime, groups, judge)
    by = collections.defaultdict(collections.Counter)
    for e in entries: by[e['name']][e['outcome']] += 1
    for name, c in by.items(): print(f'{name}: {dict(c)}', flush=True)
    for e in entries:
        if e['outcome'] != 'match': print(f"{e['name']} rep{e['rep']} {e['outcome']}: {'; '.join(e['diffs'])[:400]}")
    (a.out / 'smp.json').write_text(json.dumps({'results': entries}, indent=1) + '\n')
    return sum(c['fail'] for c in by.values())


def cmd_limits(a, host, runtime):
    failures, report = [], {}
    loop = 'body 0x100000 ebfe\n'                  # jmp $: a linked loop with no exit
    five = loop + ''.join('cpu\nrip 0x100000\n' for _ in range(5))
    rc, out, err = driver(host, runtime, a.fexc, five, a.out / 'work' / 'five')
    report['five'] = {'returncode': rc, 'stderr': err.strip()}
    if rc == 0 or '5 vCPUs' not in err: failures.append(f'5 vCPUs not refused: {rc} {err.strip()[-200:]}')
    straight = vm.description(json.loads((ROOT / 'fixtures/slice1/programs.json').read_text())['programs'][0])
    for v in range(4):
        text = ''.join('cpu\n' + (straight if w == v else 'rip 0x100000\nbody 0x100000 ebfe\n') for w in range(4))
        rc, out, err = driver(host, runtime, a.fexc, text, a.out / 'work' / f'forge{v}', ['--forge-header', '--tamper-vcpu', v])
        stops = [(c['stop'], c['reason']) for c in out['cpus']] if out else None
        report[f'forge{v}'] = {'returncode': rc, 'stops': stops, 'stderr': err.strip()[-300:]}
        want = [('resident', 'bad_link status=1 detail=9') if w == v else ('cancelled', '') for w in range(4)]
        if rc or stops != want: failures.append(f'forged header on vCPU {v}: {rc} {stops} {err.strip()[-200:]}')
    for f in failures: print(f)
    print(json.dumps(report, indent=1))
    (a.out / 'limits.json').write_text(json.dumps({'report': report, 'failures': failures}, indent=1) + '\n')
    return len(failures)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('mode', choices=['corpora', 'smp', 'limits'])
    ap.add_argument('--fexc', type=pathlib.Path, required=True)
    ap.add_argument('--out', type=pathlib.Path, required=True)
    ap.add_argument('--cpus', type=int, nargs='+', default=[2, 3, 4])
    ap.add_argument('--max-block', type=int)
    ap.add_argument('--parallel', type=int, default=1)
    ap.add_argument('--seed', type=int, default=20261027)
    raw = pathlib.Path(os.environ.get('NATEMU_RAW_ROOT', ROOT.parent / 'natemu-raw'))
    flow = [raw / 's2d/corpus/programs.json', raw / 's2d/corpus/expected.json']
    ap.add_argument('--flow', type=pathlib.Path, nargs=2, default=flow if all(f.exists() for f in flow) else None)
    ap.add_argument('--no-faults', action='store_true')
    ap.add_argument('--programs', type=lambda s: s.split(','))
    ap.add_argument('--reps', type=int, default=1)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    host, runtime = vm.build()
    failed = {'corpora': cmd_corpora, 'smp': cmd_smp, 'limits': cmd_limits}[a.mode](a, host, runtime)
    print('failures:', failed)
    sys.exit(1 if failed else 0)


if __name__ == '__main__': main()

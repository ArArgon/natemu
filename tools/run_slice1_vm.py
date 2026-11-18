#!/usr/bin/env python3
"""Run straight-line x86-64 programs (tools/reference program schema) in the VM and compare with QEMU.

    tools/with-env python3 tools/run_slice1_vm.py --fexc FEXC --out DIR [PROGRAMS.json EXPECTED.json]

Builds and signs the release host and runtime, then for each program writes a description
(`natemu-host RUNTIME run-program`), runs it and compares the final state with the expected
QEMU result through `ref.compare`. Defaults: fixtures/slice1/{programs,expected}.json. Nothing is
keyed by program name. A program is "unsupported" when execution stops at a declared limit of the
slice before its end: an instruction it cannot compile (fexc `unsupported_form`, e.g. a LOCK form; stores
to the code's own page stopped `unsupported_smc` until S2e and now run). Every other stop before the end, and every
difference, is a failure. Independent page checks: a dirty page is
accessed, every page whose bytes changed is dirty, the code pages are accessed.
Writes DIR/results.json; exit status 1 on any failure.

AFP accurate-FP plan chunk 0 (tools/fp_deviations.py): for a corpus whose directory is listed in tools/models/afp_fp/predictions.json
(i8, i8b), a program tagged there with a NaN class is judged, when the run's own report says `afp` false and `fp` fast (a host
without FEAT_AFP, real or NATEMU_FEXC_FEATURE_OFF=afp), against the offline model's predicted final state: the outcome is `deviated`
when it matches, a failure when it does not, and a failure too when the result is the exact (QEMU's) one. Every other run and
program is judged as before. `--self-check` is the no-VM mutation check of that rule."""
import argparse, json, os, pathlib, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools' / 'reference'))
import ref, asmcache, fp_deviations as dev  # noqa: E402
asmcache.install(ref)      # clang runs once per distinct text (tools/asmcache.py), not once per shard and step
# The driver option that turns on admission's code-shape debug check (off by default in the host). Passed by every test
# runner (run_slice1_vm, run_slice1_faults, run_slice2_vm through run_program, run_slice2_faults, run_slice2_probes,
# run_slice3_vm except `contain`, upstream_tests/run_asm); not by `contain` (isolation must not depend on it) or
# tools/bench/run_natemu.py (the host's default admission).
CODE_SHAPE = ['--code-shape']

def frame_alias_args():
    """Test-driver override only: make selected-page and unmapped-hole probes deterministic."""
    va = os.environ.get('NATEMU_TEST_GUEST_STATE_ALIAS')
    return ['--guest-state-alias', va] if va is not None else []

def at_hlt(out, rip):
    """The driver's report `out` ended at the `hlt` at `rip` with the state there: HLT is the resident's transaction since
    X2, #GP(0) at CPL 3 (delivered through the driver's logging IDT, the state banked at it), a park at CPL 0 that the
    driver ends `halted` (RIP after it); before X2 the vCPU stopped `no_code` at it."""
    r = int(out['rip'], 0)
    return ((out['stop'] == 'no_code' and r == rip)
            or (out['stop'] == 'guest_fault' and [int(x, 0) for x in out['fault']] == [13, 0, 0, rip] and r == rip)
            or (out['stop'] == 'resident' and out['reason'].startswith('halted') and r == rip + 1))

def build():
    """The release host (signed) and runtime. NATEMU_PREBUILT=1 (tools/run_suite.py, which builds them once first): use them
    as built. Every cargo build, even a no-op one, links target/release/natemu-host and the runtime afresh from deps/ (the
    host unsigned until the codesign below), so a runner building while another starts them breaks that one
    (`hv_vm_create: 0xfae94007`, HV_DENIED; the runtime missing)."""
    target = pathlib.Path(os.environ.get('CARGO_TARGET_DIR', ROOT / 'target'))  # cargo's own output directory
    host = target / 'release/natemu-host'
    if os.environ.get('NATEMU_PREBUILT') == '1': return host, target / 'aarch64-unknown-none/release/natemu-runtime'
    cargo = ['cargo', '+1.97.0', 'build', '--offline', '--locked', '--release']
    subprocess.run(cargo + ['-p', 'natemu-host'], cwd=ROOT, check=True)
    subprocess.run(cargo + ['-p', 'natemu-runtime', '--target', 'aarch64-unknown-none'], cwd=ROOT, check=True)
    subprocess.run(['codesign', '--force', '--sign', '-', '--entitlements', ROOT / 'crates/host/entitlements.plist', host], check=True)
    return host, target / 'aarch64-unknown-none/release/natemu-runtime'

def description(p):
    lines = [f"rip {ref.num(p['rip']):#x}", f"rflags {ref.num(p['rflags']):#x}",
             'gpr ' + ' '.join(f'{ref.num(v):#x}' for v in p['gpr']), f"code {p['code']}"]
    lines += [f"memory {ref.num(m['addr']):#x} {m['data']}" for m in p['memory']]
    lines += [f"{k} {ref.num(p[k]) & (2**64 - 1):#x}" for k in ('fsbase', 'gsbase') if k in p]     # I7: segment bases
    lines += [f"cpl {p['cpl']}"] if 'cpl' in p else []
    lines += ['sse 1'] if p.get('sse') else []                                                        # I5a: CR4.OSFXSR
    lines += ['xcr0 7'] if p.get('avx') else []                                                       # I8: CR4.OSXSAVE, XCR0 = 7
    return '\n'.join(lines) + '\n'

def declared_limit(vm):
    """The slice's declared limit: an instruction it cannot compile (since the no-stop change, its #UD: a REFUSED decode)."""
    if vm['stop'] == 'no_code' and vm['reason'] == 'unsupported_form': return 'unsupported_form'
    if vm['stop'] == 'guest_fault' and int(vm['fault'][0], 0) == 6 and vm.get('stats', {}).get('decodes', {}).get(f'{0x7b:#x}'): return 'refused (#UD)'
    return None

def page_checks(p, vm):
    pages = {ref.num(x['va']): x for x in vm['pages']}
    problems = [f'{va:#x} dirty but not accessed' for va, x in pages.items() if x['dirty'] and not x['accessed']]
    rip = ref.num(p['rip'])
    for va in range(rip & ~4095, rip + len(p['code']) // 2, 4096):
        if not pages[va]['accessed']: problems.append(f'code page {va:#x} not accessed')
    for before, after in zip(p['memory'], vm['memory']):
        a, old, new = ref.num(before['addr']), bytes.fromhex(before['data']), bytes.fromhex(after['data'])
        for i in range(len(old)):
            if old[i] != new[i] and not pages[(a + i) & ~4095]['dirty']:
                problems.append(f'{(a + i) & ~4095:#x} written but not dirty'); break
    return problems

def evaluate(p, expected, vm, tagged=None):
    """The outcome (match | unsupported | fail | deviated) and differences of the VM's final state `vm` for program `p` against its
    QEMU result `expected`. `tagged`: the program's entry in the corpus's predictions (fp_deviations.corpus_deviations), if any."""
    diffs = ref.compare(expected, [ref.num(v) for v in vm['gpr']], ref.num(vm['rflags']), ref.num(vm['rip']), vm['memory'])
    limit = declared_limit(vm)
    if limit:
        return 'unsupported', [f"stopped at {vm['rip']}: {limit}"]
    if vm['stop'] != 'reached': diffs.insert(0, f"stopped {vm['stop']} {vm['reason']} at {vm['rip']}")
    diffs += page_checks(p, vm)
    if tagged and dev.applies(vm):
        name = '+'.join(tagged['classes'])
        if not diffs: return 'fail', [f'tagged {name} but exact: fast-mode code changed (the documented deviation was expected)']
        if vm['stop'] != 'reached': return 'fail', diffs
        wrong = dev.prediction_diffs(tagged, vm, expected) + page_checks(p, vm)
        return ('fail', [f'tagged {name}: not the predicted deviated state'] + wrong) if wrong else ('deviated', [f'as documented ({name})'])
    return ('fail' if diffs else 'match'), diffs

def run_program(host, runtime, fexc, p, expected, work, max_block=None, tagged=None):
    """Runs one program description in the VM (`natemu-host run-program`) and compares with its QEMU result `expected`:
    {'name', 'returncode', 'stderr', 'outcome': match | unsupported | fail | deviated, 'diffs', 'vm'}."""
    work.mkdir(parents=True, exist_ok=True)
    (work / 'program.txt').write_text(description(p))
    limit = [str(max_block)] if max_block else []
    limit += CODE_SHAPE
    run = subprocess.run([host, runtime, 'run-program', fexc, work / 'program.txt', work] + frame_alias_args() + limit, capture_output=True, text=True)
    entry = {'name': p['name'], 'returncode': run.returncode, 'stderr': run.stderr[-2000:]}
    if run.returncode == 0:
        vm = json.loads(run.stdout)
        entry['vm'] = vm
        entry['outcome'], entry['diffs'] = evaluate(p, expected, vm, tagged)
    else:
        entry['outcome'] = 'fail'
    return entry

def corpus_key(programs):
    """The predictions' key of a corpus: the name of the folder `programs` is in, as given (a symlinked corpus folder keeps its
    name: resolving it would give the link target's)."""
    return pathlib.Path(os.path.abspath(programs)).parent.name

def self_check(programs, expected):
    """No VM: the corpus judging rule on synthetic reports of a corpus (`programs`, `expected` as the arguments of a run). A program
    tagged in the predictions is judged against the model's state: the documented state is `deviated`, the exact (QEMU) one fails, a
    different wrong one fails, and the M4 (afp true), accurate mode and a report that does not say judge it exactly; an untagged
    program passes exactly and fails with one byte changed (a deviated state is no excuse)."""
    sys.path.insert(0, str(ROOT / 'tools/models/afp_fp'))
    import predict
    tags = dev.corpus_deviations(corpus_key(programs))
    progs = {p['name']: p for p in json.loads(programs.read_text())['programs']}
    exp = {r['name']: r for r in json.loads(expected.read_text())['results']}
    if not tags: sys.exit(f'{programs.parent.name}: no tagged programs in the predictions (nothing to check)')
    stale = sorted(set(tags) - set(progs))
    if stale: sys.exit(f'run_slice1_vm self-check FAILED: the predictions tag programs this corpus does not have (stale: '
                       f'rerun tools/models/afp_fp/predict.py): {stale[:5]}')
    def report(p, gpr, rflags, rip, memory, **extra):
        pages = {}
        for addr, size in [(ref.num(p['rip']), len(p['code']) // 2)] + [(ref.num(m['addr']), len(m['data']) // 2) for m in memory]:
            for va in range(addr & ~4095, addr + size, 4096): pages[va] = {'va': hex(va), 'accessed': True, 'dirty': True}
        return {'stop': 'reached', 'reason': '', 'gpr': [hex(v) for v in gpr], 'rflags': hex(rflags), 'rip': hex(rip), 'memory': memory,
                'pages': list(pages.values()), 'afp': False, 'fp': 'fast', **extra}
    def exact(n):
        q = exp[n]['qemu']
        return report(progs[n], [ref.num(v) for v in q['gpr']], ref.num(q['rflags']), ref.num(q['rip']), q['memory'])
    def model(n):
        gpr, fl, mem = predict.state(progs[n], predict.FAST)
        return report(progs[n], gpr, fl, ref.num(exp[n]['qemu']['rip']), [{'addr': hex(a), 'data': d.hex()} for a, d in mem])
    def judge(n, vm, **extra): return evaluate(progs[n], exp[n], {**vm, **extra}, tags.get(n))[0]
    def need(cond, what):
        if not cond: sys.exit(f'run_slice1_vm self-check FAILED: {what}')
    n = next(n for n in tags if n in progs)
    dm, ex = model(n), exact(n)
    need(judge(n, dm) == 'deviated', f'{n}: the predicted deviated state is not accepted')
    need(judge(n, ex) == 'fail', f'{n}: a tagged program that comes out exact passes')
    wrong = {**dm, 'gpr': [hex(int(dm['gpr'][0], 0) ^ 1)] + dm['gpr'][1:]}
    need(judge(n, wrong) == 'fail', f'{n}: a different wrong result passes')
    m0 = dm['memory'][0]; flipped = bytes.fromhex(m0['data']); flipped = bytes([flipped[0] ^ 1]) + flipped[1:]
    need(judge(n, {**dm, 'memory': [{**m0, 'data': flipped.hex()}] + dm['memory'][1:]}) == 'fail', f'{n}: a changed memory byte passes')
    for extra in ({'afp': True}, {'fp': 'accurate'}, {'afp': None}, {'afp': True, 'fp': 'accurate'}):
        need(judge(n, dm, **extra) == 'fail' and judge(n, ex, **extra) == 'match', f'{n}: strict judging broken for {extra}')
    u = next(n for n in progs if n not in tags)
    need(judge(u, exact(u)) == 'match', f'{u}: an untagged exact program fails')
    bad = {**exact(u), 'gpr': [hex(int(exact(u)['gpr'][0], 0) ^ 1)] + exact(u)['gpr'][1:]}
    need(judge(u, bad) == 'fail', f'{u}: an untagged failure passes')
    need(judge(u, model(n)) == 'fail', 'an untagged program accepts another program\'s deviated state')
    print(f'run_slice1_vm self-check: ok ({programs.parent.name}: {sum(n in progs for n in tags)} tagged programs; {n} used)')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('programs', type=pathlib.Path, nargs='?', default=ROOT / 'fixtures/slice1/programs.json')
    ap.add_argument('expected', type=pathlib.Path, nargs='?', default=ROOT / 'fixtures/slice1/expected.json')
    ap.add_argument('--fexc', type=pathlib.Path)
    ap.add_argument('--out', type=pathlib.Path)
    ap.add_argument('--self-check', action='store_true', help='no VM: the deviation rule on synthetic reports of this corpus (PROGRAMS EXPECTED)')
    ap.add_argument('--max-block', type=int, help='block size limit in guest instructions (runs each program as several blocks)')
    ap.add_argument('--cpl', type=int, choices=(0, 3), help="run every program at this CPL (the QEMU reference is CPL 3; valid for "
                    "programs without CPL-dependent instructions, e.g. the I7 segment corpus)")
    a = ap.parse_args()
    if a.self_check: return self_check(a.programs, a.expected)
    if not (a.fexc and a.out): ap.error('--fexc and --out are required')
    key = corpus_key(a.programs)
    tags = dev.corpus_deviations(key) if a.cpl is None else {}
    host, runtime = build()
    expected = {r['name']: r for r in json.loads(a.expected.read_text())['results']}
    a.out.mkdir(parents=True, exist_ok=True)
    results, counts = [], {'match': 0, 'unsupported': 0, 'fail': 0, 'deviated': 0}
    for p in json.loads(a.programs.read_text())['programs']:
        if a.cpl is not None: p = dict(p, cpl=a.cpl)
        entry = run_program(host, runtime, a.fexc, p, expected[p['name']], a.out / 'work' / p['name'], a.max_block, tags.get(p['name']))
        counts[entry['outcome']] += 1
        results.append(entry)
        stats = entry.get('vm', {}).get('stats', {})
        print(f"{p['name']}: {entry['outcome']} {stats} {'; '.join(entry.get('diffs', []))[:300]} {entry['stderr'].strip()[-300:]}")
    (a.out / 'results.json').write_text(json.dumps({'counts': counts, 'results': results}, indent=1) + '\n')
    print(counts)
    dv = sorted({c for p in results if p['outcome'] == 'deviated' for c in tags[p['name']]['classes']})
    if dv: print(f"{counts['deviated']} deviated as documented ({', '.join(dv)})")
    sys.exit(1 if counts['fail'] else 0)

if __name__ == '__main__': main()

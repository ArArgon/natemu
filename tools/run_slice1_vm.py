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
Writes DIR/results.json; exit status 1 on any failure."""
import argparse, json, pathlib, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools' / 'reference'))
import ref  # noqa: E402
# The driver option that turns on admission's code-shape debug check (off by default in the host). Passed by every test
# runner (run_slice1_vm, run_slice1_faults, run_slice2_vm through run_program, run_slice2_faults, run_slice2_probes,
# run_slice3_vm except `contain`, upstream_tests/run_asm); not by `contain` (isolation must not depend on it) or
# tools/bench/run_natemu.py (the host's default admission).
CODE_SHAPE = ['--code-shape']

def build():
    cargo = ['cargo', '+1.97.0', 'build', '--offline', '--locked', '--release']
    subprocess.run(cargo + ['-p', 'natemu-host'], cwd=ROOT, check=True)
    subprocess.run(cargo + ['-p', 'natemu-runtime', '--target', 'aarch64-unknown-none'], cwd=ROOT, check=True)
    host = ROOT / 'target/release/natemu-host'
    subprocess.run(['codesign', '--force', '--sign', '-', '--entitlements', ROOT / 'crates/host/entitlements.plist', host], check=True)
    return host, ROOT / 'target/aarch64-unknown-none/release/natemu-runtime'

def description(p):
    lines = [f"rip {ref.num(p['rip']):#x}", f"rflags {ref.num(p['rflags']):#x}",
             'gpr ' + ' '.join(f'{ref.num(v):#x}' for v in p['gpr']), f"code {p['code']}"]
    lines += [f"memory {ref.num(m['addr']):#x} {m['data']}" for m in p['memory']]
    lines += [f"{k} {ref.num(p[k]) & (2**64 - 1):#x}" for k in ('fsbase', 'gsbase') if k in p]     # I7: segment bases
    lines += [f"cpl {p['cpl']}"] if 'cpl' in p else []
    lines += ['sse 1'] if p.get('sse') else []                                                        # I5a: CR4.OSFXSR
    return '\n'.join(lines) + '\n'

def declared_limit(vm):
    """The slice's declared limit: an instruction it cannot compile."""
    if vm['stop'] == 'no_code' and vm['reason'] == 'unsupported_form': return 'unsupported_form'
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

def evaluate(p, expected, vm):
    """The outcome (match | unsupported | fail) and differences of the VM's final state `vm` for program `p` against its
    QEMU result `expected`."""
    diffs = ref.compare(expected, [ref.num(v) for v in vm['gpr']], ref.num(vm['rflags']), ref.num(vm['rip']), vm['memory'])
    limit = declared_limit(vm)
    if limit:
        return 'unsupported', [f"stopped at {vm['rip']}: {limit}"]
    if vm['stop'] != 'reached': diffs.insert(0, f"stopped {vm['stop']} {vm['reason']} at {vm['rip']}")
    diffs += page_checks(p, vm)
    return ('fail' if diffs else 'match'), diffs

def run_program(host, runtime, fexc, p, expected, work, max_block=None):
    """Runs one program description in the VM (`natemu-host run-program`) and compares with its QEMU result `expected`:
    {'name', 'returncode', 'stderr', 'outcome': match | unsupported | fail, 'diffs', 'vm'}."""
    work.mkdir(parents=True, exist_ok=True)
    (work / 'program.txt').write_text(description(p))
    limit = [str(max_block)] if max_block else []
    limit += CODE_SHAPE
    run = subprocess.run([host, runtime, 'run-program', fexc, work / 'program.txt', work] + limit, capture_output=True, text=True)
    entry = {'name': p['name'], 'returncode': run.returncode, 'stderr': run.stderr[-2000:]}
    if run.returncode == 0:
        vm = json.loads(run.stdout)
        entry['vm'] = vm
        entry['outcome'], entry['diffs'] = evaluate(p, expected, vm)
    else:
        entry['outcome'] = 'fail'
    return entry

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('programs', type=pathlib.Path, nargs='?', default=ROOT / 'fixtures/slice1/programs.json')
    ap.add_argument('expected', type=pathlib.Path, nargs='?', default=ROOT / 'fixtures/slice1/expected.json')
    ap.add_argument('--fexc', type=pathlib.Path, required=True)
    ap.add_argument('--out', type=pathlib.Path, required=True)
    ap.add_argument('--max-block', type=int, help='block size limit in guest instructions (runs each program as several blocks)')
    ap.add_argument('--cpl', type=int, choices=(0, 3), help="run every program at this CPL (the QEMU reference is CPL 3; valid for "
                    "programs without CPL-dependent instructions, e.g. the I7 segment corpus)")
    a = ap.parse_args()
    host, runtime = build()
    expected = {r['name']: r for r in json.loads(a.expected.read_text())['results']}
    a.out.mkdir(parents=True, exist_ok=True)
    results, counts = [], {'match': 0, 'unsupported': 0, 'fail': 0}
    for p in json.loads(a.programs.read_text())['programs']:
        if a.cpl is not None: p = dict(p, cpl=a.cpl)
        entry = run_program(host, runtime, a.fexc, p, expected[p['name']], a.out / 'work' / p['name'], a.max_block)
        counts[entry['outcome']] += 1
        results.append(entry)
        stats = entry.get('vm', {}).get('stats', {})
        print(f"{p['name']}: {entry['outcome']} {stats} {'; '.join(entry.get('diffs', []))[:300]} {entry['stderr'].strip()[-300:]}")
    (a.out / 'results.json').write_text(json.dumps({'counts': counts, 'results': results}, indent=1) + '\n')
    print(counts)
    sys.exit(1 if counts['fail'] else 0)

if __name__ == '__main__': main()

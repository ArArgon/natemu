#!/usr/bin/env python3
"""Run straight-line x86-64 programs (tools/reference program schema) in the VM and compare with QEMU.

    tools/with-env python3 tools/run_slice1_vm.py --fexc FEXC --out DIR [PROGRAMS.json EXPECTED.json]

Builds and signs the release host and runtime, then for each program writes a description
(`natemu-host RUNTIME run-program`), runs it and compares the final state with the expected
QEMU result through `ref.compare`. Defaults: fixtures/slice1/{programs,expected}.json. Nothing is
keyed by program name. A program is "unsupported" when execution stops at a declared limit of the
slice before its end: an instruction it cannot compile (fexc `unsupported_form`, e.g. memory xchg), or a
store to a page the running block was translated from (`unsupported_smc`; this includes data stores to
the code's own page, which the reference layout allows). Every other stop before the end, and every
difference, is a failure. Independent page checks: a dirty page is
accessed, every page whose bytes changed is dirty, the code pages are accessed.
Writes DIR/results.json; exit status 1 on any failure."""
import argparse, json, pathlib, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools' / 'reference'))
import ref  # noqa: E402

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
    return '\n'.join(lines) + '\n'

def declared_limit(vm):
    """The slice's declared limits: an instruction it cannot compile, and a store to a page a running block
    was translated from (translated pages stay read-only; any data store to the code's own page stops)."""
    if vm['stop'] == 'no_code' and vm['reason'] == 'unsupported_form': return 'unsupported_form'
    if vm['stop'] == 'resident' and vm['reason'].startswith('unsupported_smc'): return 'unsupported_smc'
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

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('programs', type=pathlib.Path, nargs='?', default=ROOT / 'fixtures/slice1/programs.json')
    ap.add_argument('expected', type=pathlib.Path, nargs='?', default=ROOT / 'fixtures/slice1/expected.json')
    ap.add_argument('--fexc', type=pathlib.Path, required=True)
    ap.add_argument('--out', type=pathlib.Path, required=True)
    ap.add_argument('--max-block', type=int, help='block size limit in guest instructions (runs each program as several blocks)')
    a = ap.parse_args()
    host, runtime = build()
    expected = {r['name']: r for r in json.loads(a.expected.read_text())['results']}
    a.out.mkdir(parents=True, exist_ok=True)
    results, counts = [], {'match': 0, 'unsupported': 0, 'fail': 0}
    for p in json.loads(a.programs.read_text())['programs']:
        work = a.out / 'work' / p['name']
        work.mkdir(parents=True, exist_ok=True)
        (work / 'program.txt').write_text(description(p))
        limit = [str(a.max_block)] if a.max_block else []
        run = subprocess.run([host, runtime, 'run-program', a.fexc, work / 'program.txt', work] + limit, capture_output=True, text=True)
        entry = {'name': p['name'], 'returncode': run.returncode, 'stderr': run.stderr[-2000:]}
        if run.returncode == 0:
            vm = json.loads(run.stdout)
            entry['vm'] = vm
            diffs = ref.compare(expected[p['name']], [ref.num(v) for v in vm['gpr']], ref.num(vm['rflags']),
                                ref.num(vm['rip']), vm['memory'])
            limit = declared_limit(vm)
            if limit:
                entry['outcome'] = 'unsupported'
                diffs = [f"stopped at {vm['rip']}: {limit}"]
            else:
                if vm['stop'] != 'reached': diffs.insert(0, f"stopped {vm['stop']} {vm['reason']} at {vm['rip']}")
                diffs += page_checks(p, vm)
                entry['outcome'] = 'fail' if diffs else 'match'
            entry['diffs'] = diffs
        else:
            entry['outcome'] = 'fail'
        counts[entry['outcome']] += 1
        results.append(entry)
        stats = entry.get('vm', {}).get('stats', {})
        print(f"{p['name']}: {entry['outcome']} {stats} {'; '.join(entry.get('diffs', []))[:300]} {run.stderr.strip()[-300:]}")
    (a.out / 'results.json').write_text(json.dumps({'counts': counts, 'results': results}, indent=1) + '\n')
    print(counts)
    sys.exit(1 if counts['fail'] else 0)

if __name__ == '__main__': main()

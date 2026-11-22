#!/usr/bin/env python3
"""natemu's side of the interp cases (docs/restructure/interp-cases.md, "What natemu's side is to do"): each case runs in
natemu's program driver as a machine description (`natemu-host RUNTIME run-program`, `machine`: the case's state set
directly, its memory physical, its stops and windows; crates/driver/src/drivers/program.rs), and the driver's `machine`
output, projected by the case's masks (`interp_rig.project`), must equal the locked answers: the QEMU answer where both
references answer a field (they agree, case `references`), the named one where `by` says `qemu` or `kvm`; `profile`
fields are not compared here.

    tools/natemu-test run tests/interp -k natemu-machine      # --selftest: the driver's machine mode in 64-bit mode

**Not registered yet:** the cases start in R, P16, P32 or C32, which the resident does not run until the interpreter is
integrated (today its `start()` halts on such a state). The integration task registers the corpus as
`[suite.cases."natemu"]` (argv: this script with `--host {natemu_host} --runtime {natemu_runtime} --fexc {fexc} --out
{out}`) once the interpreter asks the driver's hook at CS.base + EIP before it decodes (`execution::Hook::at`), which is how
the driver ends a run at a stop. `--selftest` runs today: a few machine descriptions in 64-bit mode (translated code), the
first-instruction stop, a landing stop and the readback, against the values the programs must leave.
"""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402,E702
import argparse
import fnmatch
import json
import subprocess

import interp_cases as G
import interp_rig as R

num = R.num
GPR16 = 16


def description(case: dict, common: list) -> str:
    """The case as a machine description: the common blocks, the case's memory and code (physical: linear = physical in
    every case), its whole starting state, every stop's linear address (ids in order) and every compared window."""
    st = case['state']
    lines = ['machine']
    blocks = [*common, *case['memory'], case['code']]
    lines += [f"memory {num(b['addr']):#x} {b['data']}" for b in blocks if b['data']]
    lines.append(f"rip {num(st['eip']):#x}")
    lines.append(f"rflags {num(st['eflags']):#x}")
    gpr = [num(st['gpr'][r]) for r in G.GPRS] + [0] * (GPR16 - len(G.GPRS))
    lines.append('gpr ' + ' '.join(f'{v:#x}' for v in gpr))
    lines.append(f"cpl {st['cpl']}")
    lines += [f"{k} {num(st[k]):#x}" for k in ('cr0', 'cr3', 'cr4', 'efer')]
    for s in G.SREGS:
        g = st['seg'][s]
        lines.append(f"seg {s} {num(g['sel']):#x} {num(g['base']):#x} {num(g['limit']):#x} {num(g['attr']):#x}")
    lines += [f"{t} {num(st[t]['base']):#x} {num(st[t]['limit']):#x}" for t in ('gdtr', 'idtr')]
    lines += [f"stop {num(s['linear']):#x}" for s in sorted(case['stops'], key=lambda s: s['id'])]
    lines += [f"window {num(w['addr']):#x} {w['len']:#x}" for w in case['compare']['windows']]
    return '\n'.join(lines) + '\n'


def judge(case: dict, natemu: dict, locked: dict) -> list:
    """The differences of natemu's fields (the driver's `machine` output) from the locked answers ({'qemu': answer,
    'kvm': answer}): a field both answer against QEMU's, a field one answers against that one's; `profile` fields
    never."""
    if natemu is None: return ['no machine output']
    by, diffs = case['compare']['by'], []
    for ref in G.REFERENCES:
        want, got = locked.get(ref), R.project(case, natemu, ref)
        for k in R.answered_by(case, ref):
            if ref == 'kvm' and by.get(k, 'both') == 'both': continue          # judged once, against QEMU's
            if want is None: diffs.append(f'{k}: no {ref} answer'); continue
            if got.get(k) != want.get(k):
                diffs.append(f'{k}: natemu {R._show(got.get(k))}, {ref} {R._show(want.get(k))}')
    return diffs


def run(host, runtime, fexc, text: str, work: pathlib.Path) -> dict:
    """One machine description through the program driver: its one state object (`machine` among it), or a failure."""
    work.mkdir(parents=True, exist_ok=True)
    (work / 'program.txt').write_text(text)
    r = subprocess.run([str(host), str(runtime), 'run-program', str(fexc), str(work / 'program.txt'), str(work)],
                       capture_output=True, text=True, timeout=300)
    (work / 'stderr.txt').write_text(r.stderr)
    try: out = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError): return {'stop': 'driver', 'reason': f'exit {r.returncode}: {r.stderr.strip()[-600:]}'}
    return out


# --- the self-test: machine descriptions in 64-bit mode, which run today ---

def selftests() -> list:
    """(name, case, the fields natemu must report) for machine descriptions in 64-bit mode, CPL 0, over the corpus's 4-level
    tables and GDT: a store and an indirect jump to a landing stop; arithmetic falling through to stop 0 (flags from the
    starting CF ZF to PF); an empty program (the first-instruction stop)."""
    code64 = G.loaded(0x28)
    segs = {'es': G.loaded(0x10), 'cs': code64, 'ss': G.loaded(0x10), 'ds': G.loaded(0x10), 'fs': G.loaded(0x40),
            'gs': G.loaded(0x48)}
    ctl = dict(cr0=G.CR0_PG | G.CR0_ET | G.CR0_PE, cr3=G.PML4, cr4=G.CR4_PAE, efer=G.EFER_LME | G.EFER_LMA)
    progs = [
        ('l64-store-jump', ['mov eax, 0x12345678', 'mov dword [abs 0x20100], eax', 'mov ebx, 0x11000', 'jmp rbx'],
         {'eax': 0x12345678, 'ebx': 0x11000}, 1, '78563412' + '00' * 12, 0x2 | G.CF | G.ZF),
        ('l64-fall-through', ['mov ecx, 0x55', 'add ecx, ecx'], {'ecx': 0xaa}, 0, '00' * 16, 0x2 | G.PF),
        ('l64-empty', [], {}, 0, '00' * 16, 0x2 | G.CF | G.ZF),
    ]
    out = []
    for (name, asm, changed, stop, window, flags), code in zip(progs, G.assemble([p[1] for p in progs], 64)):
        gpr = {r: 0x1111 * (i + 1) for i, r in enumerate(G.GPRS)}
        gpr['esp'] = G.STACK
        state = {'gpr': {r: G.h(v) for r, v in gpr.items()}, 'eip': G.h(G.CODE), 'eflags': G.h(0x2 | G.CF | G.ZF), 'cpl': 0,
                 'seg': {s: {k: G.h(v) for k, v in segs[s].items()} for s in G.SREGS},
                 **{k: G.h(v) for k, v in ctl.items()},
                 'gdtr': {'base': G.h(G.GDT), 'limit': G.h(G.GDT_LIMIT)}, 'idtr': {'base': G.h(G.IDT64), 'limit': G.h(0x1ff)}}
        stops = [{'id': 0, 'linear': G.h(G.CODE + len(code))}, {'id': 1, 'linear': G.h(G.LAND)}]
        case = {'name': name, 'state': state, 'code': {'addr': G.h(G.CODE), 'data': code.hex()},
                'memory': [{'addr': G.h(G.DATA), 'data': '00' * 16}], 'stops': stops,
                'compare': {'windows': [{'addr': G.h(G.DATA), 'len': 16}]}}
        want = {'stop': stop, **{r: changed.get(r, v) for r, v in gpr.items()}, 'eflags': flags,
                **{s: segs[s]['sel'] for s in G.SREGS}, **ctl, 'gdtr': [G.GDT_LIMIT, G.GDT], 'idtr': [0x1ff, G.IDT64],
                'window0': window}
        out.append((name, case, want))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--host', required=True)
    ap.add_argument('--runtime', required=True)
    ap.add_argument('--fexc', required=True)
    ap.add_argument('--out', required=True, type=pathlib.Path)
    ap.add_argument('--selftest', action='store_true', help='the 64-bit machine descriptions only')
    ap.add_argument('--cases', default='*', help='a glob over case names (the corpus)')
    a = ap.parse_args(argv)
    a.out.mkdir(parents=True, exist_ok=True)
    doc = G.corpus()
    common = doc['common']
    results = []
    if a.selftest:
        for name, case, want in selftests():
            out = run(a.host, a.runtime, a.fexc, description(case, common), a.out / 'work' / name)
            got = out.get('machine')
            diffs = [f'driver stop {out.get("stop")}: {out.get("reason")}'] if got is None else \
                [f'{k}: natemu {R._show(got.get(k))}, want {R._show(v)}' for k, v in want.items() if got.get(k) != v]
            results.append({'name': name, 'outcome': 'fail' if diffs else 'match', 'diffs': diffs})
    else:
        import run_interp_ref
        locked = run_interp_ref.locked(doc)
        for i, c in enumerate(doc['cases']):
            if not fnmatch.fnmatch(c['name'], a.cases): continue
            out = run(a.host, a.runtime, a.fexc, description(c, common), a.out / 'work' / c['name'])
            diffs = judge(c, out.get('machine'), {r: locked[r].get(i) for r in locked})
            if out.get('machine') is None or out['machine'].get('stop') is None:
                diffs.insert(0, f'driver stop {out.get("stop")}: {out.get("reason")}')
            results.append({'name': c['name'], 'outcome': 'fail' if diffs else 'match', 'diffs': diffs})
    (a.out / 'results.json').write_text(json.dumps({'results': results}, indent=1) + '\n')
    failed = [r for r in results if r['outcome'] == 'fail']
    for r in failed: print(f'FAIL {r["name"]}: {"; ".join(r["diffs"])[:600]}', flush=True)
    print(f'{len(results) - len(failed)} of {len(results)} pass', flush=True)
    sys.exit(1 if failed or not results else 0)


if __name__ == '__main__':
    testenv.require_framework(__file__)
    main()

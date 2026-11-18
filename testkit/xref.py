"""Cross-reference two x86 references over the program corpora: every program of the selected families' `programs`
suites runs under the pinned QEMU user build and natively on the x86 machine (tools/reference/ref_native.py), and each
case where their normalized final states differ is reported field by field (GPRs, RFLAGS bits under the compare mask,
RIP, memory bytes, the fault taken), with which side the locked answer agrees with and, with --natemu, natemu's own
result. A study tool: it changes no lock and decides nothing.

    tools/natemu-test xref tests/programs tests/fp [--suite i8b] [--host ubuntu@192.168.12.10] [--natemu] -o report.json
"""
import argparse
import json
import os
import pathlib
import sys
import shutil
import tempfile

from testkit import artifacts, family, lock
from testkit.harness import programs as H
from testkit.harness.normalize_programs import _ref, answer, mask_of

GPR = ['rax', 'rcx', 'rdx', 'rbx', 'rsp', 'rbp', 'rsi', 'rdi', 'r8', 'r9', 'r10', 'r11', 'r12', 'r13', 'r14', 'r15']
BITS = {0x1: 'CF', 0x4: 'PF', 0x10: 'AF', 0x40: 'ZF', 0x80: 'SF', 0x400: 'DF', 0x800: 'OF'}


def diff(a: dict, b: dict, mask: int) -> list[str]:
    """Where final states a (QEMU) and b (x86) differ, under the compare mask."""
    ref, _ = _ref()
    out = []
    for i, (x, y) in enumerate(zip(a['gpr'], b['gpr'])):
        if ref.num(x) != ref.num(y): out.append(f'{GPR[i]}: qemu {ref.num(x):#x} x86 {ref.num(y):#x}')
    fx = (ref.num(a['rflags']) ^ ref.num(b['rflags'])) & mask
    if fx:
        out.append('rflags ' + ','.join(n for bit, n in BITS.items() if fx & bit) +
                   f': qemu {ref.num(a["rflags"]) & mask:#x} x86 {ref.num(b["rflags"]) & mask:#x}')
    if ref.num(a['rip']) != ref.num(b['rip']): out.append(f'rip: qemu {a["rip"]} x86 {b["rip"]}')
    for ma, mb in zip(a['memory'], b['memory']):
        da, db = bytes.fromhex(ma['data']), bytes.fromhex(mb['data'])
        offs = [i for i in range(len(da)) if da[i] != db[i]]
        if offs:
            lo, hi = offs[0] // 16 * 16, min(len(da), (offs[-1] // 16 + 1) * 16)
            out.append(f'memory {ma["addr"]}+{lo:#x}..{hi:#x} ({len(offs)} bytes): qemu {da[lo:hi].hex()} x86 {db[lo:hi].hex()}')
    return out


def cross(fam, suite, host, with_natemu, work) -> dict:
    ref, flg = _ref()
    sys.path.insert(0, str(H.REF))
    import ref_native
    _, progs = H.build_input(fam, suite.input)
    locked = lock.read(fam).get('suite', {}).get(suite.name, {})
    masks = {k: int(v, 16) for k, v in locked.get('masks', {}).items()}
    os.environ.setdefault('NATEMU_REFERENCE_BACKEND', 'apple')
    runs = [dict(p['reference_program'], name=p['name']) if 'reference_program' in p else p for p in progs]
    try:
        qres, _ = ref.run_programs(runs, work / suite.name / 'qemu', artifacts.qemu_user())
    except ValueError:              # one program QEMU cannot run (SIGILL on SHA, ...): find which, one at a time
        qres = []
        for i, r in enumerate(runs):
            try: qres.append(ref.run_programs([r], work / suite.name / 'qemu' / str(i), artifacts.qemu_user())[0][0])
            except ValueError as e: qres.append({'error': str(e)[:300]})
    norm = [ref.norm(p) for p in progs]
    outs, env = ref_native.execute(norm, host, require_flag=suite.extra.get('x86_flag'))
    cases, n = [], None
    for p, np_, q, (rc, out) in zip(progs, norm, qres, outs):
        flow = np_['flow']
        m = masks.get(p['name']) if flow else mask_of(p)
        if m is None: m = ref.num(q.get('rflags_compare_mask', '0x8d5'))
        c = {'name': p['name'], 'reference': H.Programs.reference_of(suite, p['name'])}
        try:
            xfinal, xfault = ref.parse_output(np_, out) if rc == '0' or np_['faults'] is not None else (None, None)
        except Exception as e:          # an output the harness cannot parse (a crash mid-capture)
            xfinal, xfault = None, None; c['x86_error'] = str(e)[:300]
        if 'error' in q:
            c['differs'] = ['qemu did not complete: ' + q['error']]
            c['x86'] = 'completed' if xfinal is not None else c.get('x86_error', f'exit status {rc}')
        elif xfinal is None:
            c.setdefault('x86_error', f'exit status {rc}' + (' (signal)' if rc.isdigit() and int(rc) > 128 else ''))
            c['differs'] = ['x86 did not complete: ' + c['x86_error']]
        else:
            d = diff(q['qemu'], xfinal, m)
            qf = q.get('fault')
            if (qf or xfault) and (qf or {}).get('trapno') != (xfault or {}).get('trapno'):
                d.append(f'fault: qemu {qf} x86 {xfault}')
            c['differs'] = d
            qa, xa = answer(p, q['qemu'], m), answer(p, xfinal, m)
            la = locked.get('cases', {}).get(p['name'])
            c['lock_agrees'] = 'both' if qa == xa == la else 'qemu' if la == qa else 'x86' if la == xa else 'neither'
        if c['differs'] and with_natemu and 'error' not in q:
            if n is None: n, fexc = artifacts.natemu(), artifacts.fexc()
            c['natemu'] = {}
            for fp in ('accurate', 'fast'):         # natemu's FP modes (NATEMU_FP); fast follows the host's FP rules
                vm = H.Programs._run_natemu(n, fexc, p, dict(os.environ, NATEMU_FP=fp),
                                            work / suite.name / 'natemu' / fp / p['name'], None)
                if isinstance(vm, dict) and vm.get('stop') == 'reached':
                    na = answer(p, vm, m)
                    c['natemu'][fp] = ('qemu' if na == answer(p, q['qemu'], m) else
                                       'x86' if xfinal and na == answer(p, xfinal, m) else 'neither')
                    if c['natemu'][fp] == 'neither' and xfinal: c[f'natemu_{fp}_vs_x86'] = diff(vm, xfinal, m)
                else:
                    c['natemu'][fp] = f'did not complete: {str(vm)[:200] if not isinstance(vm, dict) else vm.get("stop")}'
        cases.append(c)
    return {'suite': suite.name, 'family': fam.name, 'input': suite.input, 'machine': env, 'programs': len(progs),
            'differ': sum(1 for c in cases if c['differs']), 'cases': [c for c in cases if c['differs']]}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('families', nargs='+', type=pathlib.Path)
    ap.add_argument('--suite', action='append', help='only these suites (repeatable)')
    ap.add_argument('--host', default=os.environ.get('NATEMU_X86_HOST', 'ubuntu@192.168.12.10'))
    ap.add_argument('--natemu', action='store_true', help="also run natemu (accurate and fast FP) on each differing case and say whom it agrees with")
    ap.add_argument('-o', '--output', type=pathlib.Path, required=True)
    a = ap.parse_args(argv)
    work = pathlib.Path(tempfile.mkdtemp(prefix='testkit-xref-'))
    report = []
    for d in a.families:
        fam = family.load(d / 'family.toml')
        for s in fam.suites:
            if s.harness != 'programs' or (a.suite and s.name not in a.suite): continue
            if any(r['input'] == s.input and r['family'] == fam.name for r in report): continue   # same corpus (i7-cpl0)
            r = cross(fam, s, a.host, a.natemu, work)
            report.append(r)
            print(f'{fam.name}/{s.name}: {r["differ"]} of {r["programs"]} differ', flush=True)
            a.output.write_text(json.dumps(report, indent=1) + '\n')
    shutil.rmtree(work, ignore_errors=True)
    print(f'wrote {a.output}')


if __name__ == '__main__':
    main()

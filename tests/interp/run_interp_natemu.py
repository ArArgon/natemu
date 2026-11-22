#!/usr/bin/env python3
"""natemu's side of the interp cases (docs/restructure/interp-cases.md, "What natemu's side is to do"): each case runs in
natemu's program driver as a machine description (`natemu-host RUNTIME run-program`, `machine`: the case's state set
directly, its memory physical, its stops and windows; crates/driver/src/drivers/program.rs), and the driver's `machine`
output, projected by the case's masks (`interp_rig.project`), must equal the locked answers: the QEMU answer where both
references answer a field (they agree, case `references`), the named one where `by` says `qemu` or `kvm`; `profile`
fields are not compared here. A stop is the hook's answer at a linear address (`execution::Hook::at`), which the
interpreter asks at CS.base + EIP before each decode, and the host never makes a record for a stop's address.

    tools/natemu-test run tests/interp -k natemu      # the corpus (case `natemu`) and the self-tests (`natemu-machine`)

The corpus case runs every case, `--jobs` at a time. The cases the interpreter does not run yet are listed in
`--pending` (natemu-pending.txt): each must end in the typed stop of a resident operation not built yet
(`interp_unsupported`: a protected-mode far transfer or segment load, MOV to CR, HLT), not in any other result; one that
matches its answers is an unexpected pass (remove it from the list). `--selftest` runs machine descriptions with the fields
each must leave: in 64-bit mode (translated code), and in the interpreted modes (the interpreter's own rules: an unaligned
LOCK inside and across 16 bytes, an instruction ending at a page end or at the CS limit and one running past it,
unassigned and device pages, self-modifying code, the typed stops' records).
"""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402,E702
import argparse
import concurrent.futures
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


# --- the interpreted modes' self-tests: the interpreter's own rules, each with the fields it must leave ---

PT = G.PML4 + 0x3000   # the common tables' page table of 0-2 MiB (4 KiB pages)
# exec::stop::interp: the details of `interp_unsupported` for a resident operation not built yet.
PENDING_DETAILS = {1: 'far_jump', 2: 'far_return', 3: 'load_segment', 4: 'mov_to_cr', 5: 'hlt'}


def interp_case(name, mode, asm, *, regs=None, eflags=0x2, segs=None, mem=(), at=G.CODE, stops=None, windows=()):
    """A case in `mode` (G.MODES' state, the corpus's starting segments with `segs` over them), `asm` (nasm lines) at
    linear `at`; GPRs 0 but ESP and `regs`; `mem`: (address, bytes) blocks after the common ones; `stops`: linear
    addresses (default: the code's end, stop 0); `windows`: (address, length) read back."""
    m, seg = G.MODES[mode], G.start_segments(mode)
    seg.update(segs or {})
    gpr = {r: 0 for r in G.GPRS}
    gpr['esp'] = G.STACK - seg['ss']['base']
    gpr.update(regs or {})
    state = {'gpr': {r: G.h(v) for r, v in gpr.items()}, 'eip': G.h(at - seg['cs']['base']), 'eflags': G.h(eflags | 2),
             'cpl': 0, 'seg': {s: {k: G.h(v) for k, v in seg[s].items()} for s in G.SREGS},
             **{k: G.h(m[k]) for k in ('cr0', 'cr3', 'cr4', 'efer')},
             'gdtr': {'base': G.h(G.GDT), 'limit': G.h(G.GDT_LIMIT)},
             'idtr': {'base': G.h(m['idtr'][0]), 'limit': G.h(m['idtr'][1])}}
    return {'name': name, 'mode': mode, 'asm': asm, 'at': at, 'state': state, 'stops': stops,
            'memory': [{'addr': G.h(a), 'data': bytes(d).hex()} for a, d in mem],
            'compare': {'windows': [{'addr': G.h(a), 'len': n} for a, n in windows]}}


def interp_selftests() -> list:
    """(name, case, want) in the interpreted modes. `want`: machine fields, and for a typed stop `resident` (its name) and
    `fault` (the record's first three words: the vector or 0, CR2 or the linear address, the error code or the page)."""
    unmapped = (PT + 0x11 * 8, bytes(8))                    # page 0x11000 not present in C32
    limit = lambda n: {'cs': {'sel': 0x08, 'base': 0, 'limit': n, 'attr': 0x409b}}    # a byte-granular 32-bit CS
    cases = [
        # FEAT_LSE2: an unaligned LOCK XADD and LOCK BTS inside 16 bytes (one `casal` each), and a LOCK XADD across 16
        # bytes (the split lock).
        (interp_case('p32-lock-unaligned', 'P32', ['lock xadd [0x20101], eax', 'lock bts dword [0x20105], 3',
                                                   'lock xadd [0x2010e], ebx'],
                     regs={'eax': 0x11223344, 'ebx': 0x55667788}, mem=[(G.DATA, bytes(32))], windows=[(G.DATA, 32)]),
         {'stop': 0, 'eax': 0, 'ebx': 0, 'eflags': 0x6,
          'window0': '0044332211080000000000000000887766550000000000000000000000000000'}),
        # An instruction ending at the end of a page whose next page is not present: no fault (the stop there is answered
        # first); one running into that page: #PF there (CR2 its first byte; a supervisor read fetch, NXE off: 0).
        (interp_case('c32-page-end', 'C32', ['mov eax, 0x12345678'], at=0x10ffb, mem=[unmapped]),
         {'stop': 0, 'eax': 0x12345678}),
        (interp_case('c32-page-cross', 'C32', ['mov eax, 0x12345678'], at=0x10ffd, mem=[unmapped], regs={'eax': 7}),
         {'resident': 'interp_fault', 'fault': [14, 0x11000, 0], 'eax': 7, 'stop': None}),
        # At the CS limit: an instruction ending there runs; the byte past it is #GP(0); an instruction crossing it is
        # #GP(0) before it runs.
        (interp_case('p32-limit-end', 'P32', ['mov eax, 1'], segs=limit(0x10104)), {'stop': 0, 'eax': 1}),
        (interp_case('p32-limit-past', 'P32', ['mov eax, 1', 'nop'], segs=limit(0x10104), stops=[0x10200]),
         {'resident': 'interp_fault', 'fault': [13, 0, 0], 'eax': 1, 'stop': None}),
        (interp_case('p32-limit-cross', 'P32', ['mov eax, 1'], segs=limit(0x10103)),
         {'resident': 'interp_fault', 'fault': [13, 0, 0], 'eax': 0, 'stop': None}),
        # Unassigned memory (the legacy hole): reads all-ones, writes ignored. A device's page (the local APIC's): a stop.
        (interp_case('r-unassigned', 'R', ['mov eax, [es:0]', 'mov dword [es:4], 0', 'mov ebx, [es:4]'],
                     segs={'es': G.real(0xa000)}), {'stop': 0, 'eax': 0xffffffff, 'ebx': 0xffffffff}),
        (interp_case('p32-device', 'P32', ['mov dword [0xfee000b0], 0']),
         {'resident': 'interp_memory', 'fault': [0, 0xfee000b0, 0xfee00000], 'stop': None}),
        # Self-modifying code: the second pass's MOV runs its new bytes (its cached record fails the byte check).
        (interp_case('p32-self-modifying', 'P32', ['mov ecx, 2', 'top: mov eax, 0x11111111', 'add ebx, eax',
                                                   'mov dword [0x10106], 0x22222222', 'loop top']),
         {'stop': 0, 'eax': 0x22222222, 'ebx': 0x33333333, 'ecx': 0, 'eflags': 0x6}),
        # Typed stops: a form outside the first version, TF, an exception.
        (interp_case('r-unsupported', 'R', ['out 0x80, al']), {'resident': 'interp_unsupported', 'stop': None}),
        (interp_case('r-trap-flag', 'R', ['nop'], eflags=0x102), {'resident': 'interp_debug', 'stop': None}),
        (interp_case('p32-ud2', 'P32', ['ud2']), {'resident': 'interp_fault', 'fault': [6, 0, 0], 'stop': None}),
    ]
    out = []
    for bits in (16, 32):
        group = [(c, w) for c, w in cases if G.MODES[c['mode']]['bits'] == bits]
        for (c, w), code in zip(group, G.assemble([c['asm'] for c, _ in group], bits)):
            c['code'] = {'addr': G.h(c['at']), 'data': code.hex()}
            c['stops'] = [{'id': k, 'linear': G.h(s)} for k, s in enumerate(c['stops'] or [c['at'] + len(code)])]
            out.append((c['name'], c, w))
    return out


def typed_stop(out: dict, want: dict) -> list:
    """The differences of how a run ended from `want`: a typed stop (`resident`, its name; `fault`, the record's first
    three words) or none."""
    reason = str(out.get('reason', ''))
    if 'resident' not in want:
        return [] if out.get('stop') == 'reached' else [f'stop {out.get("stop")}: {reason}']
    if out.get('stop') != 'resident' or not reason.startswith(want['resident'] + ' '):
        return [f'stop {out.get("stop")}: {reason}, want {want["resident"]}']
    fault = [int(x, 16) for x in out.get('fault', [])[:3]]
    if 'fault' in want and fault != want['fault']:
        return [f'fault {[hex(x) for x in fault]}, want {[hex(x) for x in want["fault"]]}']
    return []


def pending_detail(out: dict) -> str:
    """The resident operation not built yet whose typed stop ended the run, or ''."""
    if out.get('stop') != 'resident' or not str(out.get('reason', '')).startswith('interp_unsupported '):
        return ''
    return PENDING_DETAILS.get(int(out.get('native', ['0x0'])[0], 16) >> 24 & 0xff, '')


def read_pending(path: pathlib.Path) -> set:
    """The case names of a pending list: one per line, `#` comments."""
    return {line.split('#')[0].strip() for line in path.read_text().splitlines()} - {''}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--host', required=True)
    ap.add_argument('--runtime', required=True)
    ap.add_argument('--fexc', required=True)
    ap.add_argument('--out', required=True, type=pathlib.Path)
    ap.add_argument('--selftest', action='store_true', help='the machine descriptions with their expected fields only')
    ap.add_argument('--cases', default='*', help='a glob over case names (the corpus)')
    ap.add_argument('--pending', type=pathlib.Path, help='the corpus cases that stop at a form not built yet')
    ap.add_argument('--jobs', type=int, default=1, help='runs at once')
    a = ap.parse_args(argv)
    a.out.mkdir(parents=True, exist_ok=True)
    doc = G.corpus()
    common = doc['common']
    results = []
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, a.jobs))
    go = lambda name, case: run(a.host, a.runtime, a.fexc, description(case, common), a.out / 'work' / name)
    if a.selftest:
        tests = selftests() + interp_selftests()
        for (name, case, want), out in zip(tests, pool.map(lambda t: go(t[0], t[1]), tests)):
            got = out.get('machine')
            fields = {k: v for k, v in want.items() if k not in ('resident', 'fault')}
            diffs = typed_stop(out, want) + ([f'no machine output: {out.get("reason")}'] if got is None else
                                             [f'{k}: natemu {R._show(got.get(k))}, want {R._show(v)}'
                                              for k, v in fields.items() if got.get(k) != v])
            results.append({'name': name, 'outcome': 'fail' if diffs else 'match', 'diffs': diffs})
    else:
        import run_interp_ref
        locked = run_interp_ref.locked(doc)
        pending = read_pending(a.pending) if a.pending else set()
        if unknown := pending - {c['name'] for c in doc['cases']}:
            results.append({'name': 'pending-list', 'outcome': 'fail', 'diffs': [f'not a case: {n}' for n in sorted(unknown)]})
        chosen = [(i, c) for i, c in enumerate(doc['cases']) if fnmatch.fnmatch(c['name'], a.cases)]
        for (i, c), out in zip(chosen, pool.map(lambda ic: go(ic[1]['name'], ic[1]), chosen)):
            diffs = judge(c, out.get('machine'), {r: locked[r].get(i) for r in locked})
            if out.get('machine') is None or out['machine'].get('stop') is None:
                diffs.insert(0, f'driver stop {out.get("stop")}: {out.get("reason")}')
            outcome = 'fail' if diffs else 'match'
            if c['name'] in pending:
                detail = pending_detail(out)
                outcome = 'unsupported' if detail else 'fail'   # (the harness's declared limit: not a pass, not a failure)
                diffs = [f'stops at {detail}, not built yet'] if detail else \
                    diffs or ['unexpected pass: remove it from the pending list']
            results.append({'name': c['name'], 'outcome': outcome, 'diffs': diffs})
    (a.out / 'results.json').write_text(json.dumps({'results': results}, indent=1) + '\n')
    failed = [r for r in results if r['outcome'] == 'fail']
    waiting = [r for r in results if r['outcome'] == 'unsupported']
    for r in failed: print(f'FAIL {r["name"]}: {"; ".join(r["diffs"])[:600]}', flush=True)
    counts = {d: sum(r['diffs'][0].startswith(f'stops at {d},') for r in waiting) for d in PENDING_DETAILS.values()}
    print(f'{len(results) - len(failed) - len(waiting)} of {len(results)} pass; {len(waiting)} pending'
          f'{" (" + ", ".join(f"{d} {n}" for d, n in counts.items() if n) + ")" if waiting else ""}; {len(failed)} fail',
          flush=True)
    sys.exit(1 if failed or not results else 0)


if __name__ == '__main__':
    testenv.require_framework(__file__)
    main()

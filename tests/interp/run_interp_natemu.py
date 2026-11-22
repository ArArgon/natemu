#!/usr/bin/env python3
"""natemu's side of the interp cases (docs/restructure/interp-cases.md, "What natemu's side is to do"): each case runs in
natemu's program driver as a machine description (`natemu-host RUNTIME run-program`, `machine`: the case's state set
directly, its memory physical, its stops and windows; crates/driver/src/drivers/program.rs), and the driver's `machine`
output, projected by the case's masks (`interp_rig.project`), must equal the locked answers: the QEMU answer where both
references answer a field (they agree, case `references`), the named one where `by` says `qemu` or `kvm`; `profile`
fields are not compared here. A stop is the hook's answer at a linear address (`execution::Hook::at`), which the
interpreter asks at CS.base + EIP before each decode, and the host never makes a record for a stop's address.

    tools/natemu-test run tests/interp -k natemu      # the corpus (case `natemu`) and the self-tests (`natemu-machine`)

The corpus case runs every case, `--jobs` at a time; every one must match its answers. `--selftest` runs machine
descriptions with the fields each must leave: in 64-bit mode (translated code), in the interpreted modes (the
interpreter's own rules: an unaligned LOCK inside and across 16 bytes, an instruction ending at a page end or at the CS
limit and one running past it, unassigned and device pages, self-modifying code, the typed stops' records, HLT, the
order of an INIT before TF, and on two CPUs a SIPI that leaves an interpreted HLT as it is where an INIT ends it), and
with a firmware image (`firmware HEX`, docs/restructure/firmware-mode.md: the reset vector fetched from it, stores to it
dropped by the interpreter, by translated code and by the resident's `copy`, the resident's reads of it, code run from
it while a RAM page holding translations is written, the same above 3 GiB of RAM, and a LOCKed access to it refused, the
recorded deviation). `--faults` runs the fault and transition cases against the models (interp_faults.py: the typed stop
and record the model gives with nothing committed, or the state it reaches; modes_model's programs to where the model
ends them).
"""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402,E702
import argparse
import concurrent.futures
import fnmatch
import json
import subprocess
import tempfile

import interp_cases as G
import interp_rig as R

num = R.num
GPR16 = 16


def description(case: dict, common: list) -> str:
    """The case as a machine description: the common blocks, the case's memory and code (physical: linear = physical in
    every case), its whole starting state, every stop's linear address (ids in order) and every compared window."""
    st = case['state']
    lines = ['machine'] + ([f"firmware {case['firmware']}"] if case.get('firmware') else [])
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
    lines += case.get('items', [])                      # (self-tests: the driver's event sources)
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


def run(host, runtime, fexc, text: str, work: pathlib.Path, ram: int | None = None) -> dict:
    """One machine description through the program driver (`ram`: its guest RAM, else the driver's default): its one state
    object (`machine` among it), or a failure."""
    work.mkdir(parents=True, exist_ok=True)
    (work / 'program.txt').write_text(text)
    r = subprocess.run([str(host), str(runtime), 'run-program', str(fexc), str(work / 'program.txt'), str(work),
                        *(['--ram', hex(ram)] if ram else [])], capture_output=True, text=True, timeout=300)
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


def interp_case(name, mode, asm, *, regs=None, eflags=0x2, segs=None, mem=(), at=G.CODE, stops=None, windows=(), events=()):
    """A case in `mode` (G.MODES' state, the corpus's starting segments with `segs` over them), `asm` (nasm lines) at
    linear `at`; GPRs 0 but ESP and `regs`; `mem`: (address, bytes) blocks after the common ones; `stops`: linear
    addresses (default: the code's end, stop 0); `windows`: (address, length) read back; `events`: (kind, offset) the
    driver's event sources (`nmi`, `init`) at the instruction `offset` bytes into the code (posted at its decode, after
    that boundary's look at events)."""
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
            'items': [f'{kind} {at + k:#x}' for kind, k in events],
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
        # An INIT pending where TF is set is taken first (SDM Table 7-2: class 3 before the trap and breakpoint classes):
        # the INIT posted at the POPFD that sets TF; the BSP's INIT state then fetches at the reset vector, which is not
        # RAM here (a fetch stop at FFFFFFF0H), where interp_debug would say the order is wrong.
        (interp_case('p32-init-before-tf', 'P32', ['pushfd', 'or dword [esp], 0x100', 'popfd', 'nop'], events=[('init', 8)]),
         {'resident': 'interp_memory', 'fault': [0, 0xfffffff0, 0xfffff000], 'stop': None}),
        # HLT: it completes and waits; an NMI wakes it for the typed stop (vector 2), an INIT wakes it and is taken (the
        # reset vector's fetch stop again); alone, with nothing to wake it, the run ends with the vCPU halted.
        (interp_case('p32-hlt-nmi', 'P32', ['nop', 'hlt', 'mov eax, 1'], events=[('nmi', 1)]),
         {'resident': 'interp_event', 'fault': [2, 0, 0], 'eax': 0, 'stop': None}),
        (interp_case('r-hlt-init', 'R', ['nop', 'hlt', 'mov eax, 1'], events=[('init', 1)]),
         {'resident': 'interp_memory', 'fault': [0, 0xfffffff0, 0xfffff000], 'stop': None}),
        (interp_case('r-hlt-halted', 'R', ['cli', 'hlt', 'mov eax, 1']), {'resident': 'halted', 'eax': 0, 'stop': None}),
    ]
    out = []
    for bits in (16, 32):
        group = [(c, w) for c, w in cases if G.MODES[c['mode']]['bits'] == bits]
        for (c, w), code in zip(group, G.assemble([c['asm'] for c, _ in group], bits)):
            c['code'] = {'addr': G.h(c['at']), 'data': code.hex()}
            c['stops'] = [{'id': k, 'linear': G.h(s)} for k, s in enumerate(c['stops'] or [c['at'] + len(code)])]
            out.append((c['name'], c, w))
    return out


# Two CPUs (plan section 6, tools/models/initsipi_model.py `hlt-init-not-sipi`): CPU 1 in real mode signals at FLAGS and
# halts with IF = 0 in interpreted code; the BSP (P32) sends it a SIPI, which must not end that HLT (A3, A6: a SIPI to a
# CPU not waiting for one is discarded), then an INIT, which does (taken at the boundary after the HLT: the instruction
# after it never runs), then a SIPI, which starts it at vector 0AH (real mode, CS 0A00H), where it counts its start and
# halts. Each of the BSP's waits is bounded (LOOP) and records that it saw what it waited for (the windows are read when
# the run has ended, so CPU 1 may count a late start after the BSP gave up), and a delay between its IPIs gives CPU 1
# time to show a wrong wake.
FLAGS = 0x20100          # +0 CPU 1 reached its HLT; +1 the instruction after that HLT ran; +2 starts at vector 0AH;
                         # +3, +4 the BSP saw +0, then +2, within its wait
SIPI_VECTOR, AP_CODE = 0x0A, 0x12000
WAIT, DELAY = 0x400000, 0x4000


def two_cpu_selftests() -> list:
    """(name, description, [the BSP's fields, CPU 1's fields]); a CPU's `resident` names its typed stop or end."""
    icr = lambda low: ['mov ecx, 0x830', 'mov edx, 1', f'mov eax, {low:#x}', 'wrmsr']
    delay = lambda k: [f'mov ecx, {DELAY:#x}', f'd{k}: loop d{k}']
    bsp = ['mov ecx, 0x1b', 'rdmsr', 'or eax, 0xc00', 'wrmsr',                     # x2APIC on, the APIC software-enabled
           'mov ecx, 0x80f', 'mov eax, 0x1ff', 'xor edx, edx', 'wrmsr',
           f'mov ecx, {WAIT:#x}', f'w1: cmp byte [{FLAGS:#x}], 1', 'je w1e', 'pause', 'loop w1', 'jmp w1x',
           f'w1e: mov byte [{FLAGS + 3:#x}], 1', 'w1x:',
           *delay(1),                                                                # CPU 1 parks in its HLT
           *icr(0x4600 | SIPI_VECTOR), *delay(2),                                    # SIPI: discarded
           *icr(0x4500), *delay(3),                                                  # INIT: ends the HLT
           *icr(0x4600 | SIPI_VECTOR),                                               # SIPI: starts CPU 1
           f'mov ecx, {WAIT:#x}', f'w2: cmp byte [{FLAGS + 2:#x}], 0', 'jne w2e', 'pause', 'loop w2', 'jmp w2x',
           f'w2e: mov byte [{FLAGS + 4:#x}], 1', 'w2x:']
    ap = ['mov byte [0x100], 1', 'hlt', 'mov byte [0x101], 1', 'h1: hlt', 'jmp h1']    # DS 2000H: FLAGS
    start = ['mov ax, 0x2000', 'mov ds, ax', 'add byte [0x102], 1', 'h2: hlt', 'jmp h2']
    (bsp_code,), (ap_code, start_code) = G.assemble([bsp], 32), G.assemble([ap, start], 16)
    common = [f"memory {a:#x} {d.hex()}" for _, a, d in G.common_blocks()]
    def cpu(mode, segs, at):
        m = G.MODES[mode]
        lines = ['cpu', f"rip {at - segs['cs']['base']:#x}", 'rflags 0x2', 'cpl 0',
                 *[f"{k} {m[k]:#x}" for k in ('cr0', 'cr3', 'cr4', 'efer')],
                 *[f"seg {s} {g['sel']:#x} {g['base']:#x} {g['limit']:#x} {g['attr']:#x}" for s, g in ((s, segs[s]) for s in G.SREGS)],
                 f"gdtr {G.GDT:#x} {G.GDT_LIMIT:#x}", f"idtr {m['idtr'][0]:#x} {m['idtr'][1]:#x}"]
        return lines
    real = {s: G.real(0) for s in G.SREGS} | {'cs': G.real(AP_CODE >> 4, True), 'ds': G.real(0x2000)}
    text = ['machine', *common, f'memory {FLAGS:#x} {bytes(16).hex()}', f'memory {G.CODE:#x} {bsp_code.hex()}',
            f'memory {AP_CODE:#x} {ap_code.hex()}', f'memory {SIPI_VECTOR << 12:#x} {start_code.hex()}',
            *cpu('P32', G.start_segments('P32'), G.CODE), f'stop {G.CODE + len(bsp_code):#x}', f'window {FLAGS:#x} 0x10',
            *cpu('R', real, AP_CODE)]
    # CPU 1 ends halted at vector 0AH's HLT loop (its CS 0A00H), its HLT before the SIPIs never completed (+1 stays 0).
    want = [{'stop': 0, 'window0': '0100010101' + '00' * 11}, {'resident': 'halted', 'stop': None, 'cs': SIPI_VECTOR << 8}]
    return [('smp-hlt-sipi-init', '\n'.join(text) + '\n', want)]


# --- the firmware boot mode's self-tests: a firmware image (`firmware HEX`) read, fetched, its stores dropped ---

ROM = 0xFFFFF000        # a 4 KiB image: code at its start, data at ROM_DATA, the reset vector at FFFFFFF0H
ROM_DATA = ROM + 0x800
ROM_GDT = ROM + 0x880   # a GDT in the image: null, 64-bit code (08H), flat data with its accessed bit clear (10H)
ROM_BYTES = {0x800: 0x0123456789ABCDEF.to_bytes(8, 'little'), 0x810: bytes([0xA5] * 16), 0x820: (0xC0FFEE00).to_bytes(4, 'little'),
             0x828: (0x10).to_bytes(4, 'little'), 0x840: (0x17).to_bytes(2, 'little') + ROM_GDT.to_bytes(8, 'little'),
             0x880: b''.join(d.to_bytes(8, 'little') for d in (0, 0x00AF9B000000FFFF, 0x00CF92000000FFFF))}
ROM_PD = 0x98000        # a page directory for 3-4 GiB (PDPT entry 3 of the common tables): its last entry the 2 MiB leaf FFE00000H


def nasm(bits: int, org: int, lines: list) -> bytes:
    """`lines` assembled at `org` (absolute addresses, unlike `G.assemble`'s position-independent snippets)."""
    with tempfile.TemporaryDirectory() as d:
        src = pathlib.Path(d) / 'a.asm'
        src.write_text('\n'.join([f'bits {bits}', f'org {org:#x}', *lines]) + '\n')
        r = subprocess.run(['nasm', '-f', 'bin', '-O9', str(src), '-o', str(src.with_suffix('.bin'))], capture_output=True, text=True)
        if r.returncode: raise RuntimeError(f'nasm: {r.stderr[-2000:]}')
        return src.with_suffix('.bin').read_bytes()


def rom_image(code: bytes, reset: bytes = b'') -> str:
    """The image's hex: `code` at its start, ROM_BYTES, `reset` at FFFFFFF0H, 0xCC elsewhere."""
    img = bytearray([0xCC] * 0x1000)
    img[:len(code)] = code
    for o, b in ROM_BYTES.items(): img[o:o + len(b)] = b
    img[0xFF0:0xFF0 + len(reset)] = reset
    return bytes(img).hex()


def rom_state(regs: dict, eip: int, segs: dict, ctl: dict, idtr) -> dict:
    return {'gpr': {r: G.h(regs.get(r, 0)) for r in G.GPRS}, 'eip': G.h(eip), 'eflags': G.h(0x2), 'cpl': 0,
            'seg': {s: {k: G.h(v) for k, v in segs[s].items()} for s in G.SREGS}, **{k: G.h(v) for k, v in ctl.items()},
            'gdtr': {'base': G.h(G.GDT), 'limit': G.h(G.GDT_LIMIT)}, 'idtr': {'base': G.h(idtr[0]), 'limit': G.h(idtr[1])}}


def firmware_selftests() -> list:
    """(name, case, want) with a firmware image: `want` as interp_selftests'; `rip` and `gpr` ({index: value}) compare the
    driver's final RIP and 64-bit registers."""
    out = []
    # Real mode from the power-on state: the reset vector's near JMP fetched from the image, then stores to it dropped (MOV,
    # LOCK BTS of a clear bit: CF 0, the word unchanged), each read back; to a stop, and on to a form outside the first
    # version (its typed stop there).
    r16 = nasm(16, 0xF000, ['mov al, [cs:0xF800]', 'mov byte [cs:0xF800], 0x5A', 'mov bl, [cs:0xF800]',
                            'mov word [cs:0xF820], 0x1234', 'mov cx, [cs:0xF820]', 'lock bts word [cs:0xF828], 0',
                            'mov dx, [cs:0xF828]', 'out 0x80, al'])
    end16 = 0xF000 + len(r16) - 2                                   # the OUT's IP
    reset = nasm(16, 0xFFF0, ['jmp near 0xF000'])
    power_on = {s: {'sel': 0, 'base': 0, 'limit': 0xffff, 'attr': 0x93} for s in G.SREGS}
    power_on['cs'] = {'sel': 0xF000, 'base': 0xFFFF0000, 'limit': 0xffff, 'attr': 0x9b}
    ctl0 = dict(cr0=0x60000010, cr3=0, cr4=0, efer=0)
    r_regs = {'eax': 0xEF, 'ebx': 0xEF, 'ecx': 0xEE00, 'edx': 0x10, 'eflags': 0x2, 'cs': 0xF000}
    for name, stops, want in (('r-rom-reset', [0xFFFF0000 + end16], {'stop': 0, **r_regs, 'rip': end16}),
                              ('r-rom-typed-stop', [], {'resident': 'interp_unsupported', 'stop': None, **r_regs, 'rip': end16})):
        out.append((name, {'name': name, 'firmware': rom_image(r16, reset), 'state': rom_state({}, 0xFFF0, power_on, ctl0, (0, 0x3ff)),
                           'code': {'addr': G.h(G.CODE), 'data': ''}, 'memory': [],
                           'stops': [{'id': k, 'linear': G.h(s)} for k, s in enumerate(stops)], 'compare': {'windows': []}}, want))
    # P32, flat, code in the image: its reads, a MOV store and LOCK XADD to it dropped (ECX gets the old value), a RAM store.
    p32 = nasm(32, ROM, [f'mov eax, [{ROM_DATA:#x}]', f'mov dword [{ROM_DATA:#x}], 0', f'mov ebx, [{ROM_DATA:#x}]', 'mov ecx, 5',
                         f'lock xadd [{ROM_DATA + 0x28:#x}], ecx', f'mov edx, [{ROM_DATA + 0x28:#x}]', f'mov byte [{G.DATA:#x}], 0x77'])
    flat = G.start_segments('P32')
    out.append(('p32-rom', {'name': 'p32-rom', 'firmware': rom_image(p32), 'code': {'addr': G.h(G.CODE), 'data': ''},
                            'state': rom_state({'esp': G.STACK}, ROM, flat, {k: G.MODES['P32'][k] for k in ctl0}, G.MODES['P32']['idtr']),
                            'memory': [{'addr': G.h(G.DATA), 'data': '00' * 16}], 'stops': [{'id': 0, 'linear': G.h(ROM + len(p32))}],
                            'compare': {'windows': [{'addr': G.h(G.DATA), 'len': 16}]}},
                {'stop': 0, 'eax': 0x89ABCDEF, 'ebx': 0x89ABCDEF, 'ecx': 0x10, 'edx': 0x10, 'eflags': 0x2, 'window0': '77' + '00' * 15}))
    # 64-bit mode at CPL 0 over the common tables, the image mapped by a 2 MiB leaf: from RAM, a translated MOV store and SGDT
    # (the resident's `copy`) to the image, each read back; the resident's reads of it (LGDT of a GDT in it, a MOV DS
    # through a descriptor there whose accessed bit stays clear: `atomic_byte`'s write dropped, read back); then twice
    # into code run from the image, which stores to the
    # image, to the RAM page holding the RAM code's translations (the code-write protocol while the image's page holds
    # translations: past the per-page tables below 3 GiB of RAM, inside them above) and to the data window, and returns.
    back = G.CODE + 0x40
    rom64 = nasm(64, ROM, ['mov dword [rsi + 0x20], 0x22222222', 'mov edi, [rsi + 0x20]', 'mov byte [0x10ff0], 0x90',
                           'inc r10d', f'mov [{G.DATA:#x}], r10d', f'mov edx, {back:#x}', 'jmp rdx'])
    ram64 = nasm(64, G.CODE, [f'mov esi, {ROM_DATA:#x}', 'mov rax, [rsi]', 'mov dword [rsi], 0x11111111', 'mov rbx, [rsi]',
                              'sgdt [rsi + 0x10]', 'mov rcx, [rsi + 0x10]', 'lgdt [rsi + 0x40]', 'mov eax, 0x10', 'mov ds, ax',
                              'movzx r11d, byte [rsi + 0x95]', 'mov rax, [rsi]', 'mov r9d, 2', 'again:', f'mov edx, {ROM:#x}', 'jmp rdx',
                              f'times {back - G.CODE} - ($ - $$) nop', 'dec r9d', 'jnz again'])
    segs64 = {'es': G.loaded(0x10), 'cs': G.loaded(0x28), 'ss': G.loaded(0x10), 'ds': G.loaded(0x10), 'fs': G.loaded(0x40),
              'gs': G.loaded(0x48)}
    ctl64 = dict(cr0=G.CR0_PG | G.CR0_ET | G.CR0_PE, cr3=G.PML4, cr4=G.CR4_PAE, efer=G.EFER_LME | G.EFER_LMA)
    rom_map = [{'addr': G.h(G.PML4 + 0x1000 + 3 * 8), 'data': (ROM_PD | 0x23).to_bytes(8, 'little').hex()},
               {'addr': G.h(ROM_PD), 'data': bytes(511 * 8).hex() + (0xFFE00000 | 0xE3).to_bytes(8, 'little').hex()}]
    l64 = {'state': rom_state({'esp': G.STACK}, G.CODE, segs64, ctl64, (G.IDT64, 0x1ff)), 'firmware': rom_image(rom64),
           'code': {'addr': G.h(G.CODE), 'data': ram64.hex()},
           'memory': [*rom_map, {'addr': G.h(G.DATA), 'data': '00' * 16}],
           'stops': [{'id': 0, 'linear': G.h(G.CODE + len(ram64))}], 'compare': {'windows': [{'addr': G.h(G.DATA), 'len': 16}]}}
    want64 = {'stop': 0, 'eax': 0x89ABCDEF, 'ebx': 0x89ABCDEF, 'ecx': 0xA5A5A5A5, 'edi': 0xC0FFEE00, 'eflags': 0x46,
              'window0': '02' + '00' * 15, 'ds': 0x10, 'gdtr': [0x17, ROM_GDT],
              'gpr': {0: 0x0123456789ABCDEF, 3: 0x0123456789ABCDEF, 1: 0xA5A5A5A5A5A5A5A5, 7: 0xC0FFEE00, 9: 0, 10: 2, 11: 0x92},
              'rip': G.CODE + len(ram64)}
    out.append(('l64-rom', {'name': 'l64-rom', **l64}, want64))
    out.append(('l64-rom-high', {'name': 'l64-rom-high', 'ram': (4 << 30) + (2 << 20), **l64}, want64))
    # A LOCKed access to the image from translated code: refused before any effect (`unsupported_mmio`, the recorded
    # deviation: docs/restructure/boot-handoff.md, "Typed refusals").
    lock64 = nasm(64, G.CODE, [f'mov esi, {ROM_DATA:#x}', 'lock add dword [rsi], 1'])
    out.append(('l64-rom-lock', {'name': 'l64-rom-lock', **l64, 'code': {'addr': G.h(G.CODE), 'data': lock64.hex()},
                                 'stops': [{'id': 0, 'linear': G.h(G.CODE + len(lock64))}]},
                {'resident': 'unsupported_mmio', 'stop': None, 'rip': G.CODE + 5}))
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


def judge_fault(case: dict, out: dict, want: dict) -> list:
    """The differences of a fault case's run (interp_faults.py) from the model's result."""
    got, reason = out.get('machine'), str(out.get('reason', ''))
    if 'ending' in want:                                  # a modes_model program: where it ends, its controls there
        controls = ([f'{k}: natemu {R._show(got.get(k))}, want {R._show(v)}' for k, v in want['controls'].items()
                     if got.get(k) != v] if got else [f'no machine output: {reason}'])
        return ending(case, out, want, reason) + controls
    return reached(out, want, got, reason)


def ending(case: dict, out: dict, want: dict, reason: str) -> list:
    """The differences of where a modes_model program's run ended from where the model ends it."""
    got = out.get('machine')
    if want['ending'] is None:
        return [] if out.get('stop') == 'reached' and got and got.get('stop') == 0 else [f'stop {out.get("stop")}: {reason}, want the end']
    index, vector, error, paging = want['ending']
    name = 'interp_mode' if paging else 'interp_fault'
    if out.get('stop') != 'resident' or not reason.startswith(name + ' '):
        return [f'stop {out.get("stop")}: {reason}, want {name} at instruction {index}']
    up = out.get('startup') or {}
    at = (num(up.get('cs_base', 0)) + num(up.get('rip', 0))) & 0xffffffff     # (the driver's numbers: ints)
    where = at - num(case['code']['addr'])
    d = [] if where == want['offsets'][index] else [f'stopped at offset {where:#x}, want instruction {index} at {want["offsets"][index]:#x}']
    fault = [int(x, 16) for x in out.get('fault', [])[:3]]
    if paging:
        detail = int(out.get('native', ['0x0'])[0], 16) >> 24 & 0xff
        return d + ([] if detail == 1 else [f'detail {detail}, want PAGING (1)'])
    return d + ([] if (fault[0], fault[2]) == (vector, error) else [f'fault {fault}, want vector {vector} error {error:#x}'])


def reached(out: dict, want: dict, got, reason: str) -> list:
    """The differences of a segment case's run from the model's result (a typed stop, or the fields it reaches)."""
    fields = {k: v for k, v in want.items() if k not in ('resident', 'permitted', 'detail', 'may_complete')}
    if 'resident' in want:
        if out.get('stop') != 'resident' or not reason.startswith(want['resident'] + ' '):
            return [f'stop {out.get("stop")}: {reason}, want {want["resident"]}']
        fault = tuple(int(x, 16) for x in out.get('fault', [])[:3])
        if 'permitted' in want and list(fault) not in [list(f) for f in want['permitted']]:
            return [f'fault {[hex(x) for x in fault]}, the model permits {[[hex(x) for x in f] for f in want["permitted"]]}']
        if 'detail' in want and int(out.get('native', ['0x0'])[0], 16) >> 24 & 0xff != want['detail']:
            return [f'detail {int(out.get("native", ["0x0"])[0], 16) >> 24 & 0xff}, want {want["detail"]}']
    elif out.get('stop') != 'reached':
        return [f'stop {out.get("stop")}: {reason}, want the model\'s completion']
    if got is None:
        return [f'no machine output: {reason}']
    return [f'{k}: natemu {R._show(got.get(k))}, want {R._show(v)}' for k, v in fields.items() if got.get(k) != v]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--host', required=True)
    ap.add_argument('--runtime', required=True)
    ap.add_argument('--fexc', required=True)
    ap.add_argument('--out', required=True, type=pathlib.Path)
    ap.add_argument('--selftest', action='store_true', help='the machine descriptions with their expected fields only')
    ap.add_argument('--faults', action='store_true', help='the fault and transition cases against the models')
    ap.add_argument('--cases', default='*', help='a glob over case names (the corpus)')
    ap.add_argument('--jobs', type=int, default=1, help='runs at once')
    a = ap.parse_args(argv)
    a.out.mkdir(parents=True, exist_ok=True)
    doc = G.corpus()
    common = doc['common']
    results = []
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, a.jobs))
    go = lambda name, case: run(a.host, a.runtime, a.fexc, description(case, common), a.out / 'work' / name, case.get('ram'))
    if a.faults:
        import interp_faults
        tests = interp_faults.all_cases(sys.modules[__name__])
        for (name, case, want), out in zip(tests, pool.map(lambda t: go(t[0], t[1]), tests)):
            diffs = judge_fault(case, out, want)
            results.append({'name': name, 'outcome': 'fail' if diffs else 'match', 'diffs': diffs})
    elif a.selftest:
        tests = selftests() + interp_selftests() + firmware_selftests()
        for (name, case, want), out in zip(tests, pool.map(lambda t: go(t[0], t[1]), tests)):
            got = out.get('machine')
            fields = {k: v for k, v in want.items() if k not in ('resident', 'fault', 'rip', 'gpr')}
            diffs = typed_stop(out, want) + ([f'no machine output: {out.get("reason")}'] if got is None else
                                             [f'{k}: natemu {R._show(got.get(k))}, want {R._show(v)}'
                                              for k, v in fields.items() if got.get(k) != v])
            if 'rip' in want and num(out.get('rip', '0x0')) != want['rip']:
                diffs.append(f'rip: natemu {out.get("rip")}, want {want["rip"]:#x}')
            gpr = out.get('gpr') or []
            diffs += [f'r{k}: natemu {gpr[k] if k < len(gpr) else None}, want {v:#x}' for k, v in want.get('gpr', {}).items()
                      if k >= len(gpr) or num(gpr[k]) != v]
            results.append({'name': name, 'outcome': 'fail' if diffs else 'match', 'diffs': diffs})
        for name, text, want in two_cpu_selftests():
            out = run(a.host, a.runtime, a.fexc, text, a.out / 'work' / name)
            cpus = out.get('cpus') or [out]
            diffs = [] if len(cpus) == len(want) else [f'{len(cpus)} CPUs reported: {out.get("stop")}: {out.get("reason")}']
            for k, (o, w) in enumerate(zip(cpus, want)):
                got = o.get('machine') or {}
                diffs += [f'cpu{k} {d}' for d in typed_stop(o, w)]
                diffs += [f'cpu{k} {f}: natemu {R._show(got.get(f))}, want {R._show(v)}' for f, v in w.items()
                          if f not in ('resident', 'fault') and got.get(f) != v]
            results.append({'name': name, 'outcome': 'fail' if diffs else 'match', 'diffs': diffs})
    else:
        import run_interp_ref
        locked = run_interp_ref.locked(doc)
        chosen = [(i, c) for i, c in enumerate(doc['cases']) if fnmatch.fnmatch(c['name'], a.cases)]
        for (i, c), out in zip(chosen, pool.map(lambda ic: go(ic[1]['name'], ic[1]), chosen)):
            diffs = judge(c, out.get('machine'), {r: locked[r].get(i) for r in locked})
            if out.get('machine') is None or out['machine'].get('stop') is None:
                diffs.insert(0, f'driver stop {out.get("stop")}: {out.get("reason")}')
            results.append({'name': c['name'], 'outcome': 'fail' if diffs else 'match', 'diffs': diffs})
    (a.out / 'results.json').write_text(json.dumps({'results': results}, indent=1) + '\n')
    failed = [r for r in results if r['outcome'] == 'fail']
    for r in failed: print(f'FAIL {r["name"]}: {"; ".join(r["diffs"])[:600]}', flush=True)
    print(f'{len(results) - len(failed)} of {len(results)} pass; {len(failed)} fail', flush=True)
    sys.exit(1 if failed or not results else 0)


if __name__ == '__main__':
    testenv.require_framework(__file__)
    main()

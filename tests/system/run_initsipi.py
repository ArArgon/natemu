#!/usr/bin/env python3
"""INIT and start-up IPIs (SIPI) in natemu (boot-interpreter plan section 6; docs/restructure/initsipi-resident.md), each
case one scenario of tools/models/initsipi_model.py run in the program driver and judged by tests/system/initsipi_expected.py
against the model's architectural reference (`outcomes`).

    tests/system/run_initsipi.py --fexc FEXC --out DIR [--only NAME,..] [--max-block N] [--regime entry|interpreter]

Each scenario becomes one description (identity space, `nxe 0`, every vCPU at CPL 0 with IF as the scenario's): one
body at 110000H assembled with nasm (with tests/boot/exit_ap.inc): the exit test's AP stub (its image, copied by the
BSP to 8000H, SIPI vector 08H: real mode, the climb to 64-bit mode, an arrival on the stub page's counter, a `cli; hlt`
park: the model's STUB; the page a `memory` item, so its arrival bytes are reported), then the BSP's 64-bit program from the scenario's (the stub copied and its CR3 and EFER fields
filled, the local APIC enabled; each ICR write after `mfence; lfence`, through the x2APIC MSR 830H or, for
`kvm-unit-tests` as that suite does, the xAPIC page's ICR at FEE00300H/310H; `op` a NOP; `await N` a wait for the
counter bounded by the TSC; `halt` a HLT loop; `host` an IN from an unassigned port, a host request), and each other
CPU's program for its context: `wfs` the driver's `wait-for-sipi` item, `mbox` the ACPI mailbox wait (`mailbox`, never
woken), the others translated 64-bit code. The model's vectors map to natemu's: 0 to 0 (kvm-unit-tests' vector; in the
entry regime only, since the identity space cannot hold code on page 0: 08H in the interpreter regime), the scenario's
second vector to 09H (a real-mode park there, never to be started), any other to 08H.

`--regime entry` (the default, while the boot-time interpreter is the stop at its entry): the driver runs with
`--entry-ends`, and a CPU that stopped at the interpreter's entry is observed as done with the start its state shows
(the SIPI's CS VV00H, base VV000H, IP 0, or the BSP's reset state), each such state also checked field by field (CR0
CD/NW as before | ET, EFER 0, EDX the signature, RFLAGS 2). `--regime interpreter` (once the interpreter runs the stub):
a start counts when its CPU arrived on the counter (the stub's per-APIC-ID arrival byte), and scenarios that need
firmware at the reset vector (`bsp-init`) are left out.

Writes DIR/initsipi.json and DIR/results.json (one case per scenario); exit status 1 on any failure."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import argparse, json, pathlib, re, subprocess, sys
ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'tools/bench/smp'), str(ROOT / 'tests/smp')]
import case_results, run_slice1_vm as vm  # noqa: E402
import initsipi_expected as expect  # noqa: E402
from models import initsipi_model as m  # noqa: E402
from run_slice3_vm import driver  # noqa: E402

BOOT = ROOT / 'tests' / 'boot'
BODY = 0x110000
AP_BASE, PARK = 0x8000, 0x9000                 # the stub page (vector 08H); a real-mode park (vector 09H)
MAILBOX = 0x300000
SIGNATURE = 0x306C3                            # EDX after INIT (the CPU profile's CPUID.1:EAX)
TIMEOUT = 1_000_000_000                        # each await's bound, TSC cycles (1 s at natemu's 1 GHz)
# The scenarios run, in order. Not here: those whose boot programs are interpreted (hlt-init-not-sipi,
# nmi-in-interpreted-hlt, init-in-interpreted-code) or which deliver an NMI or interrupt to translated code, which the
# driver's logging IDT ends (nmi-around-init, fixed-around-init).
SCENARIOS = ('linux', 'sipi-sipi', 'kvm-unit-tests', 'edk2', 'edk2-back-to-back', 'init-sipi-init', 'sipi-then-init',
             'sipi-before-init-taken', 'sipi-during-host-request', 'init-in-mailbox', 'init-in-translated-hlt', 'nmi-in-wfs',
             'nmi-held', 'nmi-init-in-wfs', 'park-with-init', 'never-started', 'bsp-init', 'fixed-in-wfs', 'one-cpu')
XAPIC = {'kvm-unit-tests'}                     # their ICR writes through the xAPIC page, as kvm-unit-tests' smp.c
FIRMWARE = {'bsp-init'}                        # the BSP restarts at the reset vector: entry regime only


def vectors(sc, regime):
    """The scenario's model vectors -> natemu's (module doc)."""
    seen = []
    for c in sc['cpus']:
        for i in c.get('boot', []):
            if i[0] == 'icr' and i[1] == 'sipi' and i[3] not in seen:
                seen.append(i[3])
    return {v: 0 if v == 0 and regime == 'entry' else 0x09 if k == 1 else 0x08 for k, v in enumerate(seen)}


def icr(kind, dest, vector, xapic):
    """One ICR write (an INIT assert, its level de-assert, a SIPI, an NMI or a fixed interrupt) to `dest`: a CPU, 'self',
    'all' or 'others'."""
    low = {'init': 0x4500, 'deassert': 0x8500, 'sipi': 0x4600, 'nmi': 0x4400, 'fixed': 0x4000}[kind] | (vector or 0)
    low |= {'self': 0x40000, 'all': 0x80000, 'others': 0xC0000}.get(dest, 0)
    target = dest if isinstance(dest, int) else 0
    if xapic:
        return (f'        mfence\n        lfence\n        mov eax, 0xFEE00300\n        mov dword [rax + 0x10], {target << 24:#x}\n'
                f'        mov dword [rax], {low:#x}\n')
    return f'        mfence\n        lfence\n        mov ecx, 0x830\n        mov edx, {target}\n        mov eax, {low:#x}\n        wrmsr\n'


def program(label, prog, vec, xapic):
    """A CPU's 64-bit code for a model program: `label` then its instructions."""
    out = [f'{label}:\n']
    for k, ins in enumerate(prog):
        op = ins[0]
        if op == 'icr':
            out.append(icr(ins[1], ins[2], vec.get(ins[3]) if ins[1] == 'sipi' else ins[3] if len(ins) > 3 else None, xapic))
        elif op == 'op':
            out.append('        nop\n')
        elif op == 'host':
            out.append('        in al, 0x80\n')
        elif op == 'await':
            out.append(f'        TSC\n        lea r8, [rax + {TIMEOUT}]\n.w{k}:   cmp dword [abs AP_COUNT], {ins[1]}\n'
                       f'        jae .a{k}\n        pause\n        TSC\n        cmp rax, r8\n        jb .w{k}\n.a{k}:\n')
        elif op in ('halt', 'hlt'):
            out.append(f'.h{k}:   hlt\n        jmp .h{k}\n')
        else:
            raise ValueError(f'{label}: {ins} is not a translated instruction')
    return ''.join(out)


def source(name, regime):
    """The body's nasm source and the vector map."""
    sc = m.SCENARIOS[name]
    vec, xapic = vectors(sc, regime), name in XAPIC
    if xapic:
        apic_on = '        mov eax, 0xFEE000F0\n        mov dword [rax], 0x1FF\n'
    else:
        apic_on = ('        mov ecx, 0x1B\n        rdmsr\n        or eax, 0xC00\n        wrmsr\n'
                   '        mov ecx, 0x80F\n        mov eax, 0x1FF\n        xor edx, edx\n        wrmsr\n')
    bsp = ('bits 64\nstart:  cld\n        lea rsi, [rel ap_src]\n        mov edi, AP_BASE\n        mov ecx, ap_end - ap_src\n'
           '        rep movsb\n        mov rax, cr3\n        mov [abs AP_CR3], eax\n        mov ecx, 0xC0000080\n        rdmsr\n'
           '        and eax, ~0x400\n        mov [abs AP_EFER], eax\n' + apic_on)
    text = ['[map symbols MAPFILE]\n        org BODY\n        default rel\n%include "exit_ap.inc"\n        AP_STUB\n'
            '        times 0x1000 - ($ - $$) db 0\n', bsp, program('bsp', sc['cpus'][0].get('boot', []), vec, xapic)]
    for i, c in enumerate(sc['cpus'][1:], 1):
        ctx = c.get('ctx', 'run')
        if ctx == 'wfs':
            continue
        prog = [('halt',)] if ctx == 'mbox' else c.get('boot', [])
        if not c.get('tr', False) and ctx != 'mbox':
            raise ValueError(f'{name}: cpu{i} starts in interpreted code')
        code = program(f'ap{i}', prog, vec, xapic)
        if any(x[0] == 'icr' for x in prog):           # a sender enables its own APIC first, as the BSP does
            code = code.replace(f'ap{i}:\n', f'ap{i}:\n' + apic_on, 1)
        text.append(code)
    return ''.join(text), vec


def assemble(name, regime, work):
    """{symbol: address} and the body's bytes."""
    text, vec = source(name, regime)
    work.mkdir(parents=True, exist_ok=True)
    src, img, mp = work / 'body.asm', work / 'body.bin', work / 'body.map'
    src.write_text(text)
    subprocess.run(['nasm', '-f', 'bin', '-I', f'{BOOT}/', f'-DBODY={BODY:#x}', f'-DMAPFILE={mp}', '-o', str(img), str(src)],
                   check=True)
    syms = {}
    for line in mp.read_text().splitlines():
        r = re.fullmatch(r'\s*([0-9A-F]+)\s+([0-9A-F]+)\s+(\S+)', line)
        if r: syms[r[3]] = int(r[2], 16)
    return syms, img.read_bytes(), vec


def description(name, regime, work):
    """The scenario's description, the vector map and the stub's symbols."""
    sc = m.SCENARIOS[name]
    syms, body, vec = assemble(name, regime, work)
    lines = ['identity', f'body {BODY:#x} {body.hex()}', f'memory {AP_BASE:#x} {bytes(0x1000).hex()}', f'memory {PARK:#x} faf4ebfd']
    if any(c.get('ctx') == 'mbox' for c in sc['cpus']):
        lines.append(f'memory {MAILBOX:#x} {bytes(16).hex()}')
    if name in XAPIC:
        lines.append('mmio 0xfee00000 0xfee00000 unassigned')
    for i, c in enumerate(sc['cpus']):
        lines.append('cpu')
        if c.get('ctx') == 'wfs':
            lines.append('wait-for-sipi')
            continue
        lines += [f"rip {syms['start'] if i == 0 else syms[f'ap{i}']:#x}", 'cpl 0', 'nxe 0',
                  f"if {1 if c.get('if_') else 0}"]
        if c.get('ctx') == 'mbox':
            lines.append(f'mailbox {MAILBOX:#x}')
    return '\n'.join(lines) + '\n', vec, syms


def observe(name, out, vec, syms, regime):
    """The run as the judge sees it (`initsipi_expected.project`'s form) and the start states' field differences."""
    sc, back = m.SCENARIOS[name], {v: k for k, v in vec.items()}
    cpus, per, problems, stop = out['cpus'], [], [], None
    mem = {}
    for x in cpus[0].get('memory', []):
        mem[int(x['addr'], 0)] = bytes.fromhex(x['data'])
    for i, (c, o) in enumerate(zip(sc['cpus'], cpus)):
        st, reason, up = o['stop'], o.get('reason', ''), o.get('startup') or {}
        name_i = f'cpu{i}'
        if st == 'cancelled':
            per.append(expect.ANY)
            continue
        if st == 'resident' and reason.startswith(('unsupported_mode', 'unsupported_code_mode')) and regime == 'entry':
            cs, base, rip, word = up.get('cs'), up.get('cs_base'), up.get('rip'), up.get('pending', 0)
            if (cs, base, rip) == (0xF000, 0xFFFF0000, 0xFFF0):
                start = 'reset'
            elif rip == 0 and base == (cs or 0) << 4 and (cs or 0) & 0xFF == 0 and cs >> 8 in back:
                start = ('sipi', back[cs >> 8])
            else:
                problems.append(f'{name_i}: stopped at the interpreter outside a start state: {up}')
                per.append(('stopped', reason))
                continue
            # The start state (power_on::init then sipi): CD/NW as before (a CPU never started: power-on's), ET.
            cr0 = 0x60000010 if c.get('ctx') == 'wfs' else 0x10
            want = {'rflags': 2, 'cr0': cr0, 'efer': 0, 'edx': SIGNATURE}
            problems += [f'{name_i} start state {k}: {up.get(k)!r} != {v:#x}' for k, v in want.items() if up.get(k) != v]
            # INIT left the APIC software disabled with its IRR clear, and no scenario here sends a fixed interrupt that
            # races an INIT: none may be pending (fixed-in-wfs: the one sent in wait-for-SIPI was not accepted).
            if any(up.get('irr', [0])):
                problems.append(f"{name_i}: IRR {up.get('irr')} after INIT")
            # `pending` is the word as the CPU's run ended (IPIs that came later do not count).
            if word & 2:                               # INIT pending: the interpreter's first boundary would take it
                per.append(expect.ANY)
            elif word & 1:                             # NMI pending: its first boundary stops for it
                stop = ('stop', i, 'nmi')
                per.append(expect.ANY)
            else:
                per.append(('done', (start,)))
            continue
        label = None
        if st == 'resident' and reason.startswith('halted'):
            label = 'mailbox' if up.get('waiting') == 'mailbox' or o['stats'].get('mailbox', [0])[0] else 'halted'
        elif st == 'resident' and reason.startswith('wait_for_sipi'):
            label = 'wait-for-sipi'
        if label is None:
            if regime == 'interpreter' and st == 'resident':
                stop = ('stop', i, 'nmi' if 'nmi' in reason else reason)
                per.append(expect.ANY)
            else:
                per.append((st, reason))
            continue
        starts = ()
        if regime == 'interpreter' and i > 0:
            arrivals = mem.get(AP_BASE)
            k = syms['ap_arrivals'] - syms['ap_src']
            n = arrivals[k + i] if arrivals and len(arrivals) > k + i else 0
            v = next((mv for mv, nv in vec.items() if nv == 0x08), None)
            starts = (('sipi', v),) * n
        per.append((label, starts))
    return (stop if stop else ('end', tuple(per))), problems


def run(a, name):
    if a.regime == 'interpreter' and name in FIRMWARE:
        return None, []
    work = a.out / name
    text, vec, syms = description(name, a.regime, work)
    extra = (['--entry-ends'] if a.regime == 'entry' else []) + ([a.max_block] if a.max_block else [])
    rc, out, err = driver(a.host, a.runtime, a.fexc, text, work, extra, timeout=300)
    if rc:
        return None, [f'{name}: driver exit {rc}: {err.strip()[-600:]}']
    if 'cpus' not in out:
        out = {'cpus': [out]}
    (work / 'out.json').write_text(json.dumps(out, indent=1) + '\n')
    observed, problems = observe(name, out, vec, syms, a.regime)
    return observed, [f'{name}: {p}' for p in problems] + expect.judge(name, observed, a.regime)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--fexc', type=pathlib.Path, required=True)
    ap.add_argument('--out', type=pathlib.Path, required=True)
    ap.add_argument('--only', type=lambda s: s.split(','), default=list(SCENARIOS))
    ap.add_argument('--max-block', type=int, default=0)
    ap.add_argument('--regime', choices=('entry', 'interpreter'), default='entry')
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    a.host, a.runtime = vm.build()
    report, failures = [], []
    for name in a.only:
        observed, problems = run(a, name)
        report.append({'name': name, 'observed': repr(observed), 'problems': problems})
        failures += problems
        print(name, 'skipped' if observed is None and not problems else 'ok' if not problems else problems[:3], flush=True)
    (a.out / 'initsipi.json').write_text(json.dumps({'regime': a.regime, 'max_block': a.max_block, 'report': report,
                                                      'failures': failures}, indent=1) + '\n')
    case_results.write(a.out, [(r['name'], f"{r['name']}: ") for r in report], failures)
    for f in failures: print('FAIL', f)
    sys.exit(1 if failures else 0)


if __name__ == '__main__':
    testenv.require_framework(__file__)
    main()

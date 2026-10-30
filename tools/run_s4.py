#!/usr/bin/env python3
"""S4 checks: single-step (TF), INT1, instruction breakpoints and DR6/DR7 (boot-features plan, S4).

    tools/with-env python3 tools/run_s4.py --fexc FEXC --out DIR [--only programs,cases] [--host HOST --runtime RUNTIME]

- `programs`: tools/bench/smp/debug.py's programs (TF through mixed instructions with REP strings, CPUID, CALL/RET;
  POPF and IRETQ setting and clearing TF; MOV SS; INT3, INT n and INT1 under TF and INT1 with DR6 preset; UD2, WRMSR #GP
  and a #PF under TF; instruction breakpoints at a loop head, mid-block, a kept system instruction, inside an
  instruction, after MOV SS, at a REP, with LEN 11, a single-step trap landing on one; DR6/DR7 read-back, DR4/DR5
  aliases and DR7.GD; sticky DR6; SYSRET and SYSCALL with TF) at block sizes default and 1. Each event log is judged
  against `debug.expect` (written from the SDM); registers and captured memory are compared with pinned QEMU system mode,
  whose own log is judged too and reported (a QEMU departure from the SDM is listed, not a natemu failure).
- `cases`: natemu-only, judged by the SDM rules: HLT under TF (#DB with RIP after the HLT, no park; delivery_model A10),
  and the refusals of data and I/O breakpoints (`unsupported_system`), an instruction breakpoint with LEN 01.

Writes DIR/s4.json; exit status 1 on any failure."""
import argparse, json, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'tools' / 'reference'), str(ROOT / 'tools' / 'bench' / 'smp')]
import ref, run_slice1_vm as vm, run_slice3_vm as s3, smp, debug  # noqa: E402


# Where pinned QEMU departs from the SDM in a register (the SDM's value is judged by `debug.expect` or below): the
# compare's line prefix and why.
QEMU_DIFFERS = {'s4-dr': {'cpu0 r8:': 'QEMU keeps DR6 bit 12 written as 1; SDM Vol.3 18.2.3: bit 12 is reserved and reads 0'},
                's4-d-io': {'cpu0 rsi:': 'QEMU ends REP OUTSB after the element that met an I/O breakpoint (RIP past it, one element moved); '
                                         'SDM 18.3.1.2: a trap between iterations, the REP resumes'}}


def programs(a, report):
    failures, rows = [], []
    for name in a.programs or debug.PROGRAMS:
        prog = debug.program(name)
        try:
            want = s3.qemu_reference(prog, a.out / 'qemu' / name)
            qlog = debug.log_of(prog, {r['addr']: r['data'] for r in want['memory']})
            qemu_vs_sdm = debug.judge(prog, qlog)
        except RuntimeError as e:   # QEMU itself fails (recorded as its departure; natemu is judged by the SDM alone)
            want, qlog, qemu_vs_sdm = None, [], [f'QEMU did not finish: {str(e)[:200]}']
        for block in (None, 1):
            rc, out, err = s3.driver(a.host, a.runtime, a.fexc, s3.smp_description(prog), a.out / 'vm' / f'{name}-{block}', [block] if block else [], timeout=300)
            if out is None:
                failures.append(f'{name} block={block}: driver {rc} {err[-600:]}')
                continue
            cpu = out['cpus'][0]
            memory = {smp.num(m['addr']): bytes.fromhex(m['data']) for m in cpu['memory']}
            log = debug.log_of(prog, memory)
            vs_sdm = debug.judge(prog, log)
            vs_qemu = s3.judge_smp(prog, want, out, [None]) if want else []
            st = cpu['stats']
            rows.append({'program': name, 'block': block, 'events': len(log), 'sdm': vs_sdm, 'qemu': vs_qemu, 'qemu_vs_sdm': qemu_vs_sdm,
                         'log': debug.show(prog, log), 'qemu_log': debug.show(prog, qlog), 'expected': debug.show(prog, debug.expect(prog)),
                         **{k: st.get(k) for k in ('debug_steps', 'debug_traps', 'code_breaks', 'steps', 'requests', 'systems')}})
            print(f"{name:14} block={block}: {len(log)} events, SDM {'ok' if not vs_sdm else vs_sdm}, QEMU {'ok' if not vs_qemu else vs_qemu[:3]}"
                  f"{'' if not qemu_vs_sdm else ' (QEMU departs from the SDM: ' + '; '.join(qemu_vs_sdm[:3]) + ')'}", flush=True)
            failures += [f'{name} block={block} SDM: {d}' for d in vs_sdm]
            # Registers and memory must equal QEMU's; the log region only where QEMU's log follows the SDM, and a register
            # only where QEMU_DIFFERS does not name it.
            failures += [f'{name} block={block} QEMU: {d}' for d in vs_qemu
                         if not (qemu_vs_sdm and d.startswith(f'region {debug.LOG:#x}')) and not any(d.startswith(k) for k in QEMU_DIFFERS.get(name, {}))]
    report['programs'] = {'rows': rows, 'failures': failures}
    return failures


def cases(a, report):
    """natemu-only cases through the driver's logging IDT (`guest_fault` with the delivered frame) or a refusal."""
    code = lambda text: ref.assemble(text).hex()
    base = 'rip 0x100000\nrflags 0x2\ncpl 0\nif 0\nregion 0x3e0000 0x10000\nmmio 0x210000 0xc0000000\ngpr 0 0 0 0 0x3f0000 0 0 0 0 0 0 0 0 0 0 0\n'
    # (the RIP after the last instruction but a final NOP)
    before_nop = lambda text: 0x100000 + len(ref.assemble(text)) - 1
    tf_on = 'pushfq\nor qword ptr [rsp], 0x100\npopfq\n'
    dr7 = lambda v: f'mov eax, {v:#x}\nmov dr7, rax\n'
    hlt = ref.assemble(tf_on + 'nop\n')
    rows, failures = [], []
    # name: (code, expected stop, expected fault record [vector, error, cr2, rip] or None)
    table = {
        # TF = 1 at the HLT: it completes and the trap is delivered at once (no park), RIP after the HLT, DR6.BS.
        'hlt-tf': (code(tf_on + 'hlt\nnop'), 'guest_fault', [1, 0, 0, 0x100000 + len(ref.assemble(tf_on)) + 1]),
        # An I/O breakpoint (R/W 10) without CR4.DE: undefined in the SDM, refused.
        'dr7-io-no-de': (code(dr7(0x20001)), 'unsupported_system', None),
        # A device access (M3: a test device page) meets a data breakpoint: the completion in the resident checks it
        # (write breakpoint, a 4-byte store; read/write breakpoint, a 2-byte load straddling its range): #DB after it.
        'mmio-store': (code(f'mov rax, 0x210010\nmov dr0, rax\n' + dr7(0xd0001) + 'mov dword ptr [0x210010], eax\nnop'), 'guest_fault', [1, 0, 0, before_nop('mov rax, 0x210010\nmov dr0, rax\n' + dr7(0xd0001) + 'mov dword ptr [0x210010], eax\nnop')]),
        'mmio-load': (code(f'mov rax, 0x210013\nmov dr0, rax\n' + dr7(0x30001) + 'mov ax, word ptr [0x210012]\nnop'), 'guest_fault', [1, 0, 0, before_nop('mov rax, 0x210013\nmov dr0, rax\n' + dr7(0x30001) + 'mov ax, word ptr [0x210012]\nnop')]),
        'mmio-miss': (code(f'mov rax, 0x210014\nmov dr0, rax\n' + dr7(0x30001) + 'mov ax, word ptr [0x210012]\nnop'), 'reached', None),
        # An instruction breakpoint with LEN 01 (undefined for R/W 00): matched at its address as LEN 00 (pinned QEMU's choice).
        # (lea 7 bytes, mov dr0 3, mov eax 5, mov dr7 3: RIP + 14 is the fourth NOP, at 0x100015)
        'xbp-len01': (code('lea rax, [rip + 14]\nmov dr0, rax\n' + dr7(0x40001) + 'nop\nnop\nnop\nnop\nnop\nnop\nnop\nnop'), 'guest_fault', [1, 0, 0, 0x100015]),
    }
    for name, (hexcode, stop, fault) in table.items():
        for block in (None, 1):
            rc, out, err = s3.driver(a.host, a.runtime, a.fexc, base + f'code {hexcode}\n', a.out / 'cases' / f'{name}-{block}', [block] if block else [], timeout=120)
            stopped = out and (out['reason'].split()[0] if out['stop'] == 'resident' else out['stop'])
            got = out and (stopped, [int(v, 0) for v in out.get('fault', [])] if out['stop'] == 'guest_fault' else None)
            ok = bool(out) and got[0] == stop and (fault is None or got[1] == fault)
            rows.append({'case': name, 'block': block, 'ok': ok, 'got': got, 'err': None if out else err[-300:]})
            print(json.dumps(rows[-1]), flush=True)
            if not ok: failures.append(f'case {name} block={block}: {rows[-1]}')
    # SMP: debug registers are each vCPU's own. vCPU 0 arms a write breakpoint on A; vCPU 1 then writes A (no #DB on vCPU 1)
    # and raises a flag; vCPU 0 writes A: #DB on vCPU 0 after its store. vCPU 1's own breakpoint (on B) never fires on 0.
    c0 = ref.assemble('mov rax, 0x200100\nmov dr0, rax\n' + dr7(0x10001) + 'mov dword ptr [0x200300], 1\n'
                      '1: cmp dword ptr [0x200200], 0\nje 1b\nmov qword ptr [0x200180], 1\nmov dword ptr [0x200100], 7\nnop')
    c1 = ref.assemble('mov rax, 0x200180\nmov dr0, rax\n' + dr7(0x10001) + '1: cmp dword ptr [0x200300], 0\nje 1b\n'
                      'mov dword ptr [0x200100], 5\nmov dword ptr [0x200200], 1\nnop')
    at0 = 0x100000 + len(c0) - 1
    text = (f'body 0x100000 {c0.hex()}\nbody 0x101000 {c1.hex()}\nregion 0x200000 0x1000\nregion 0x3e0000 0x10000\n'
            'cpu\nrip 0x100000\nrflags 0x2\ncpl 0\nif 0\ncpu\nrip 0x101000\nrflags 0x2\ncpl 0\nif 0\n')
    for block in (None, 1):
        rc, out, err = s3.driver(a.host, a.runtime, a.fexc, text, a.out / 'cases' / f'smp-{block}', [block] if block else [], timeout=120)
        cpus = out and out['cpus']
        ok = bool(cpus) and cpus[0]['stop'] == 'guest_fault' and [int(v, 0) for v in cpus[0]['fault']] == [1, 0, 0, at0] and cpus[1]['stop'] == 'reached'
        rows.append({'case': 'smp-own-registers', 'block': block, 'ok': ok, 'got': cpus and [(c['stop'], c.get('fault')) for c in cpus], 'err': None if out else err[-300:]})
        print(json.dumps(rows[-1]), flush=True)
        if not ok: failures.append(f'case smp-own-registers block={block}: {rows[-1]}')
    report['cases'] = {'rows': rows, 'failures': failures}
    return failures


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--fexc', type=pathlib.Path, required=True)
    ap.add_argument('--out', type=pathlib.Path, required=True)
    ap.add_argument('--only', type=lambda s: s.split(','), default=['programs', 'cases'])
    ap.add_argument('--programs', type=lambda s: s.split(','))
    ap.add_argument('--host', type=pathlib.Path)
    ap.add_argument('--runtime', type=pathlib.Path)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    if not (a.host and a.runtime): a.host, a.runtime = vm.build()
    report, failures = {}, []
    if 'programs' in a.only: failures += programs(a, report)
    if 'cases' in a.only: failures += cases(a, report)
    (a.out / 's4.json').write_text(json.dumps(report, indent=1, default=str) + '\n')
    for f in failures: print('FAIL', f)
    sys.exit(1 if failures else 0)


if __name__ == '__main__': main()

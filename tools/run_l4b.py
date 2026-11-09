#!/usr/bin/env python3
"""L4b checks: the PIT and port 61H, the RTC, the ACPI PM block (PM1, PM timer, SCI), S5 and the reset register
(boot-features plan, L4b; programs in tools/bench/smp/timers.py).

    tools/with-env python3 tools/run_l4b.py --fexc FEXC --out DIR [--only qemu,halt,stress,cases,clocks] [--programs pit,rtc,pm]

- `qemu`: programs run unchanged by pinned QEMU (system mode, SeaBIOS) and natemu at block sizes default and 1, one CPU
  in x2APIC mode: the PIT (read-back status and count latches, access modes, a latch held across a second latch
  command, port 61H's gate, data, refresh toggle and OUT2, Linux's `pit_calibrate_tsc` and `quick_pit_calibrate`, IRQ0 at
  the mode 2 rate through I/O APIC pin 2, Linux's PIT-disable sequence), the RTC (70H, A-D, SET, rollover into 2000,
  UIP, binary and 12-hour formats, 37H, CMOS RAM, periodic / update-ended / alarm IRQ 8), the PM block (SCI_EN, PM1_EN
  read-back, PM1_STS write-1-to-clear, the PM timer, the PM timer's SCI on pin 9, level) and S5 (QEMU powers off, natemu
  stops `poweroff`). natemu must equal QEMU on every compared result (DATA slots 0-63: values that do not depend on
  timing) and both the model (`timers.check_*`); natemu's raw TSC/count samples are judged by the exact model (PIT edges,
  PM ticks and TSC all from V): IRQ0 deliveries never before their edge and at most 2 ms late, `pit_calibrate_tsc`'s
  10 ms window, `quick_pit_calibrate`'s kHz within 500 ppm of 1 GHz when it succeeds, PM ticks within one of the rate.
- `halt`: the halted rule with the PIT as an asynchronous source (its busy guard while a transition is scheduled): a
  vCPU halted on a 55 ms one-shot reaches its end when pin 2 is unmasked, and is ended `halted` when it is masked.
- `stress`: IRQ 0 (~1 kHz) and IRQ 8 (1024 Hz) broadcast to every CPU at 1, 2, 4 CPUs, each HLTing until it took 200
  of each: every CPU reaches its end with both counts, and no IRQ 0 is lost once the code is warm (between a CPU's 50th
  and last deliveries as many PIT periods as deliveries, within one).
- `cases` (natemu only): S5 and the reset register as typed stops, other reset values and SLP_TYP refused or ignored,
  SCI_EN read-only, TMR_STS clear until the first toggle, a 32-bit PM1 access.
- `clocks` (natemu only): over 10 s (400 samples 25 ms apart) the PM timer, the PIT (channel 2, mode 2, unwrapped),
  the LAPIC timer's count and the RTC's seconds agree with the TSC within one tick of each (`timers.check_clocks`).

Writes DIR/l4b.json; exit status 1 on any failure."""
import argparse, json, pathlib, sys
from fractions import Fraction

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'tools' / 'reference'), str(ROOT / 'tools' / 'bench' / 'smp')]
import smp, apic, timers, ref, run_slice1_vm as vm  # noqa: E402
from run_slice3_vm import driver  # noqa: E402
from run_x3 import natemu_memory, qemu_memory  # noqa: E402
from run_l4a import run_natemu, qemu  # noqa: E402

CHECKS = {'l4b-pit': timers.check_pit, 'l4b-rtc': timers.check_rtc, 'l4b-pm': timers.check_pm, 'l4b-more': timers.check_more}
# Compared slots where natemu departs from QEMU on purpose (each documented with its program).
DEPARTURES = {'l4b-more': timers.MORE_DEPARTURES}


def compared(mem):
    return [mem.read(apic.DATA(0) + 8 * k) for k in range(timers.RAW)]


def cmd_qemu(a, report):
    failures = []
    for prog, _ in [f() for f in (timers.pit_program, timers.rtc_program, timers.pm_program, timers.more_program) if not a.programs or f.__name__.split("_")[0] in a.programs]:
        label, check = prog['name'], CHECKS[prog['name'].split('-x')[0]]
        want, _, err = qemu(prog, a.out / 'qemu' / label)
        problems = [f'QEMU run failed ({err})'] if want is None else [f'QEMU vs model: {d}' for d in check(qemu_memory(prog, want), False)[0]]
        summaries = {}
        for limit in (None, 1):
            out, p = run_natemu(a, prog, a.out / 'work' / f"{label}-{limit or 'd'}", [limit] if limit else [])
            if out is not None:
                mem = natemu_memory(out)
                p2, summaries[limit or 'default'] = check(mem, True)
                p += p2
                if want is not None:
                    q = compared(qemu_memory(prog, want))
                    skip = DEPARTURES.get(label.split('-x')[0], set())
                    p += [f'k{k}: natemu {x:#x} != QEMU {y:#x}' for k, (x, y) in enumerate(zip(compared(mem), q)) if x != y and k not in skip]
            problems += [f'max-block {limit}: {x}' for x in p]
        report.append({'name': label, 'problems': problems, 'summary': summaries})
        failures += [f'{label}: {p}' for p in problems]
        print(f"{label}: {'FAIL' if problems else 'ok'} {summaries}", *problems[:8], sep='\n  ', flush=True)
    # S5: QEMU powers off (exit 0, no dump); natemu stops `poweroff` at the OUT.
    prog, labels = timers.s5_program()
    label = prog['name']
    rc, err, dump, _ = smp.run_image(prog, smp.build_image(prog, smp.build_stub(a.out / 'qemu' / label)), a.out / 'qemu' / label, 'multi')
    problems = [] if rc == 0 and not dump else [f'QEMU: exit {rc}, dump {len(dump)} bytes (expected a power-off)']
    out, p = run_natemu(a, prog, a.out / 'work' / label, reached=False)
    if out is not None and out['cpus'][0]['stop'] != 'poweroff':
        p.append(f"natemu: {out['cpus'][0]['stop']} {out['cpus'][0]['reason']}")
    problems += p
    report.append({'name': label, 'problems': problems})
    failures += [f'{label}: {x}' for x in problems]
    print(f"{label}: {'FAIL' if problems else 'ok'}", *problems[:4], sep='\n  ', flush=True)
    return failures


def cmd_halt(a, report):
    failures = []
    for unmasked in (True, False):
        prog, _ = timers.halt_program(unmasked)
        label = prog['name']
        out, problems = run_natemu(a, prog, a.out / 'work' / label, reached=unmasked, timeout=120)
        if out is not None and not unmasked:
            c = out['cpus'][0]
            if not (c['stop'] == 'resident' and 'halted' in c['reason']):
                problems.append(f"cpu0 {c['stop']} {c['reason']}, expected ended halted")
        report.append({'name': label, 'problems': problems})
        failures += [f'{label}: {p}' for p in problems]
        print(f"{label}: {'FAIL' if problems else 'ok'}", *problems[:4], sep='\n  ', flush=True)
    return failures


def cmd_stress(a, report, k=200):
    failures = []
    for n in (1, 2, 4):
        prog, _ = timers.stress_program(n, k)
        label = prog['name']
        out, problems = run_natemu(a, prog, a.out / 'work' / label, timeout=300)
        counts = []
        if out is not None:
            mem = natemu_memory(out)
            for i in range(n):
                c = (mem.read(apic.DATA(i)), mem.read(apic.DATA(i) + 8))
                counts.append(c)
                if c[0] < k or c[1] < k:
                    problems.append(f'cpu{i}: IRQ0, IRQ8 counts {c} < {k}')
                # IRQ 0's rising edges between the CPU's WARM-th and last deliveries (warm code): one per delivery, within
                # one for the handlers' delivery latencies; more would be merged ones (lost: with the PIT's default policy,
                # reinject, a merged one is delivered later, so the count still matches).
                span = Fraction(mem.read(apic.DATA(i) + 0x10) - mem.read(apic.DATA(i) + 0x20)) * timers.PIT_HZ / (1193 * timers.NS)
                if abs(span - (c[0] - timers.WARM)) > 1:
                    problems.append(f'cpu{i}: {float(span):.2f} IRQ0 periods between its deliveries {timers.WARM} and {c[0]} (merged ones)')
        report.append({'name': label, 'counts': counts, 'problems': problems})
        failures += [f'{label}: {p}' for p in problems]
        print(f"{label}: {'FAIL' if problems else 'ok'} {counts}", *problems[:4], sep='\n  ', flush=True)
    return failures


BASE = 0x110000
def ow(port, v): return f'mov edx, {port:#x}\nmov eax, {v:#x}\nout dx, ax\n'
def ob(port, v): return f'mov edx, {port:#x}\nmov al, {v:#x}\nout dx, al\n'
def iw(port, reg): return f'mov edx, {port:#x}\nxor eax, eax\nin ax, dx\nmov {reg}, rax\n'


PIC_INIT = ob(0x20, 0x11) + ob(0x21, 0x20) + ob(0x21, 0x04) + ob(0x21, 0x01) + ob(0x21, 0xff)
TSC = 'rdtsc\nshl rdx, 32\nor rax, rdx\n'
FROZEN = (ob(0x43, 0x36) + 'mov edx, 0x40\nmov al, 6\nout dx, al\nmov al, 0\nout dx, al\nmov r12d, 20000\nxor r15d, r15d\nfz_trial:\n' +
          PIC_INIT + TSC + 'mov rbx, rax\nadd rbx, 100000\nfz_spin:\npause\n' + TSC + 'cmp rax, rbx\njb fz_spin\n' + ob(0x20, 0x0a) +
          'in al, dx\ntest al, 1\njnz fz_good\ninc r15d\nfz_good:\ndec r12d\njnz fz_trial\nmov rbx, r15\n')


def cases():
    """(name, asm, check(out)) of natemu-only cases."""
    def stop(kind, at_end=False):
        def check(out):
            return [] if out['stop'] == kind else [f"{out['stop']} {out['reason']} (expected {kind})"]
        return check
    def regs(**want):
        def check(out):
            g = {r: smp.num(out['gpr'][smp.REGS.index(r)]) for r in want}
            return [] if out['stop'] == 'reached' and g == want else [f"{out['stop']} {out['reason']} {({k: hex(v) for k, v in g.items()})} want {({k: hex(v) for k, v in want.items()})}"]
        return check
    def unsupported(out):
        return [] if out['stop'] == 'resident' and 'unsupported_port' in out['reason'] else [f"{out['stop']} {out['reason']} (expected unsupported_port)"]
    def committed_power(kind, asm, repeat=False):
        # S5/reset complete the triggering instruction, or exactly one REP element, before drain.
        length = len(ref.assemble(asm))
        def check(out):
            errors = stop(kind)(out)
            expected = BASE + length - (3 if repeat else 0)  # F3 66 6F: REP OUTSW
            if smp.num(out['rip']) != expected: errors.append(f"power RIP {out['rip']} != {expected:#x}")
            if repeat:
                for reg, want in [('rsi', 0x200002), ('rcx', 1)]:
                    if smp.num(out['gpr'][smp.REGS.index(reg)]) != want: errors.append(f"power {reg} did not commit one element")
            return errors
        return check
    s5 = ow(0xb004, 0x2001)
    rep_s5 = 'mov esi, 0x200000\nmov word ptr [rsi], 0x2001\nmov ecx, 2\nmov edx, 0xb004\nrep outsw\n'
    return [
        ('s5', s5, committed_power('poweroff', s5)),
        ('s5-rep-element', rep_s5, committed_power('poweroff', rep_s5, True)),
        ('s5-high-byte', ob(0xb005, 0x20), stop('poweroff')),
        ('reset', ob(0xb040, 6), stop('reset')),
        ('reset-other-values', ob(0xb040, 5) + ob(0xb040, 0) + ob(0xb040, 0xff) + 'mov edx, 0xb040\nxor eax, eax\nin al, dx\nmov rbx, rax\n', regs(rbx=0)),
        ('sleep-type-1-refused', ow(0xb004, 0x2401), unsupported),
        ('sci-en-read-only', ow(0xb004, 0x0000) + iw(0xb004, 'rbx') + ow(0xb004, 0x1c00) + iw(0xb004, 'rcx'), regs(rbx=1, rcx=0x1c01)),
        ('tmr-sts-clear-at-start', iw(0xb000, 'rbx'), regs(rbx=0)),
        ('pm1-dword', 'mov edx, 0xb000\nmov eax, 0x05210000\nout dx, eax\nxor eax, eax\nin eax, dx\nmov rbx, rax\n', regs(rbx=0x05210000)),
        ('pm-timer-write-ignored', 'mov edx, 0xb008\nin eax, dx\nmov ebx, eax\nmov eax, 0\nout dx, eax\nin eax, dx\nsub eax, ebx\nmov rcx, rax\n'
         'and ecx, 0xffffff\nxor esi, esi\ncmp ecx, 0x100000\nsetb sil\n', regs(rsi=1)),
        # The review's probe: a running PIT (mode 3, count 6: a change every 3 PIT ticks), never accessed again; in each
        # of 20,000 windows of 100 us the PIC's IRR must hold IRQ 0's rising edge (rbx = windows without one).
        ('frozen', FROZEN, regs(rbx=0)),
        ('pit-reset-idle', 'mov edx, 0x43\nmov al, 0xe2\nout dx, al\nmov edx, 0x40\nxor eax, eax\nin al, dx\nmov rbx, rax\n', regs(rbx=0x86)),
    ]


def cmd_cases(a, report):
    failures = []
    for name, asm, check in cases():
        if a.cases and name not in a.cases:
            continue
        code = ref.assemble(asm + 'cmp r11, r11')
        text = f'cpl 0\nif 0\nrip {BASE:#x}\nregion 0x200000 0x1000\ncode {code.hex()}\n'
        rc, out, err = driver(a.host, a.runtime, a.fexc, text, a.out / 'cases' / name, [], timeout=120)
        problems = [f'driver exit {rc}: {err.strip()[-300:]}'] if rc else check(out)
        report.append({'name': name, 'problems': problems})
        failures += [f'case {name}: {p}' for p in problems]
        print(f"case {name}: {'FAIL' if problems else 'ok'}", *problems[:3], sep='\n  ', flush=True)
    return failures


def cmd_modes(a, report):
    """PIT modes 6 and 7 against the 8254 datasheet (natemu only), and the lost-tick policies of the PIT, the RTC and
    the LAPIC timer, each run under reinject and discard."""
    failures = []
    def done(label, problems, summary=None):
        report.append({'name': label, 'problems': problems, 'summary': summary})
        failures.extend(f'{label}: {p}' for p in problems)
        print(f"{label}: {'FAIL' if problems else 'ok'} {summary or ''}", *problems[:4], sep='\n  ', flush=True)
    prog, _ = timers.modes67_program()
    out, problems = run_natemu(a, prog, a.out / 'work' / prog['name'])
    if out is not None:
        mem = natemu_memory(out)
        got = [mem.read(apic.DATA(0) + 8 * k) for k in range(4)]
        if not (got[0] >= 15 and got[1] >= 15 and got[2:] == [0x3c, 0x3e] or got[2:] == [0xbc, 0xbe] or got[2] & 0x7f == 0x3c and got[3] & 0x7f == 0x3e):
            problems.append(f'mode 6, 7: deliveries {got[:2]} (>= 15 each), status {[hex(x) for x in got[2:]]}')
        done(prog['name'], problems, {'deliveries': got[:2]})
    for source in ('pit', 'rtc', 'lapic'):
        for policy in ('reinject', 'discard'):
            prog, _ = timers.policy_program(source)
            label = f"{prog['name']}-{policy}"
            out, problems = run_natemu(a, prog, a.out / 'work' / label, ['--lost-ticks', f'{source}={policy}'])
            summary = None
            if out is not None:
                mem = natemu_memory(out)
                got = mem.read(apic.DATA(0))
                total, window = (int(mem.read(apic.DATA(0) + 8 * (timers.RAW + j)) / timers.POLICY_PERIOD_NS[source]) for j in (0, 1))
                summary = {'deliveries': got, 'periods': total, 'periods_with_if': window}
                lo, hi = (total - 2, total + 1) if policy == 'reinject' else (window - 1, window + 2)
                if not lo <= got <= hi:
                    problems.append(f'{got} deliveries ({total} periods, {window} with IF = 1): {policy} expects [{lo}, {hi}]')
            done(label, problems, summary)
    return failures


def cmd_clocks(a, report):
    prog, _ = timers.clocks_program()
    label = prog['name']
    out, problems = run_natemu(a, prog, a.out / 'work' / label, ['--watchdog-ms', '60000'], timeout=300)
    summary = None
    if out is not None:
        p, summary = timers.check_clocks(natemu_memory(out))
        problems += p
    report.append({'name': label, 'problems': problems, 'summary': summary})
    print(f"{label}: {'FAIL' if problems else 'ok'} {summary}", *problems[:6], sep='\n  ', flush=True)
    return [f'{label}: {p}' for p in problems]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--fexc', type=pathlib.Path, required=True)
    ap.add_argument('--out', type=pathlib.Path, required=True)
    ap.add_argument('--only', type=lambda s: s.split(','), default=['qemu', 'halt', 'stress', 'cases', 'modes', 'clocks'])
    ap.add_argument('--programs', type=lambda s: s.split(','), default=[], help='qemu: only these of pit,rtc,pm')
    ap.add_argument('--cases', type=lambda s: s.split(','), default=[], help='cases: only these names')
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    a.host, a.runtime = vm.build()
    report, failures = {}, []
    for mode, fn in (('qemu', cmd_qemu), ('halt', cmd_halt), ('stress', cmd_stress), ('cases', cmd_cases), ('modes', cmd_modes), ('clocks', cmd_clocks)):
        if mode in a.only:
            report[mode] = []
            failures += fn(a, report[mode])
    (a.out / 'l4b.json').write_text(json.dumps({'report': report, 'failures': failures}, indent=1, default=str) + '\n')
    for f in failures: print('FAIL', f)
    print({k: f"{sum(1 for r in v if not r['problems'])}/{len(v)} ok" for k, v in report.items()})
    sys.exit(1 if failures else 0)


if __name__ == '__main__': main()

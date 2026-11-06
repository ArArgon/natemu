#!/usr/bin/env python3
"""L4a checks: the I/O APIC, the 8259 pair and their routing into the LAPICs (boot-features plan, L4a; programs in
tools/bench/smp/irqs.py).

    tools/with-env python3 tools/run_l4a.py --fexc FEXC --out DIR [--only qemu,xapic,stress,halt] [--stress-rounds K]

- `qemu`: programs run unchanged by pinned QEMU (system mode, COM1 = `-serial file:`) and natemu at block sizes default
  and 1, one CPU in xAPIC and x2APIC mode: I/O APIC register read-backs; IRQ4 (the UART's THRE interrupt) edge through
  pin 4 (each rise once, a rise while masked lost); level (Remote IRR, TMR, the EOI broadcast redelivering while the line
  is high, masked -> unmasked delivering at the unmask, the EOI register); the 8259 pair (Linux's probe_8259A and
  init_8259A, OCW3 reads, the ELCR, ExtINT through LINT0 with the I/O APIC masked, poll, ELCR level mode, auto-EOI,
  rotation and special mask); the TMR following each acceptance's trigger (54H level from pin 4, then as a self IPI:
  TMR 1 then 0, the second EOI broadcasting nothing). Destinations at 2, 3, 4 CPUs in x2APIC mode (QEMU's BSP mode with peers): physical each,
  logical (two sets), lowest priority, NMI, level to a peer whose handler lowers the line (its EOI broadcast). natemu must
  equal QEMU (registers, captured memory, logs without their TSC at one CPU; counts at several), the UART bytes too, and
  both the datasheet/SDM model below (`model_problems`).
- `xapic` (natemu only, Linux's path; QEMU's BSP cannot be xAPIC with peers): the destination program at 2, 3, 4 CPUs
  with flat logical IDs, counts by the model; LVT LINT0 fixed (edge, 45H): each rise of the 8259's output one 45H with no
  INTA (QEMU passes the 8259 through in any mode, so not compared), then the APIC hardware disabled: the 8259 wired
  through, ExtINT 34H.
- `stress` (natemu only): asynchronous lines from the host's test source (`--irq-source`) at 4 and 2 CPUs, each CPU's
  line routed to its neighbour, K edge then K level rounds each with a pseudo-random 0..255 us delay: every target counts
  exactly K + K deliveries of each source and K + K completions (no edge lost or doubled, one delivery per level
  assertion window with its EOI).
- `halt` (natemu only): X3's rule for ending halted vCPUs with an asynchronous source: a vCPU halted until a 60 ms request
  fires reaches its end (the request vetoes the decision); the negative control `--irq-source-no-veto` ends it `halted`;
  and with `--watchdog-ms 20` it still reaches its end (the watchdog never forces a halted vCPU out).
- `lint1` (natemu only): the board's NMI input (LINT1) pulsed by the test source: an NMI with LVT LINT1 unmasked in NMI
  mode, none while it is masked (2 NMIs of 3 pulses).

QEMU departures (not compared; `ioapic.rs`, `irq.rs`): an edge input raised again while high (QEMU delivers; the
programs make no such raise); the storm delay after 10,000 EOIs; an INTA cycle for a masked ExtINT entry; 8-byte
accesses. Writes DIR/l4a.json; exit status 1 on any failure."""
import argparse, json, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'tools' / 'reference'), str(ROOT / 'tools' / 'bench' / 'smp')]
import smp, apic, irqs, events, run_slice1_vm as vm  # noqa: E402
from run_slice3_vm import driver, per_vcpu_checks, body_end  # noqa: E402
from run_x3 import compare_qemu, natemu_memory, qemu_memory  # noqa: E402


def run_natemu(a, prog, work, extra=(), timeout=300, reached=True):
    text = events.description(prog, [[] for _ in range(prog['ncpu'])])
    for _ in range(2):  # one retry on a concurrent hv_vm_create (0xfae94007)
        rc, out, err = driver(a.host, a.runtime, a.fexc, text, work, list(extra), timeout=timeout)
        if rc == 0 or 'fae94007' not in err: break
    if rc:
        return None, [f'driver exit {rc}: {err.strip()[-400:]}']
    problems = []
    for v, c in enumerate(out['cpus']):
        end = body_end(prog, smp.num(prog['cpus'][v]['rip']))
        if reached and (c['stop'] != 'reached' or smp.num(c['rip']) != end):
            problems.append(f"cpu{v} stopped {c['stop']} {c['reason']} at {c['rip']} (end {end:#x})")
        problems += [f'cpu{v} ' + d for d in per_vcpu_checks(c)] if reached else []
    return out, problems


def qemu(prog, work):
    work.mkdir(parents=True, exist_ok=True)
    serial = work / 'serial.out'
    serial.unlink(missing_ok=True)
    stub = smp.build_stub(work)
    rc, err, dump, _ = smp.run_image(prog, smp.build_image(prog, stub), work, 'multi', extra=['-serial', f'file:{serial}'])
    if rc != 1:
        return None, None, f'{rc} {err}'
    return smp.parse_dump(prog, dump), serial.read_bytes().split(b'Booting from ROM..')[-1], None


# ---- the model: what the datasheets and the SDM give for each program ------------------------------------------
def q(mem, at): return mem.read(at)


def model_problems(prog, mem):
    name, n, out = prog['name'].split('@')[0], prog['ncpu'], []
    data = lambda k: q(mem, apic.DATA(0) + 8 * k)
    count = lambda i, v: q(mem, apic.COUNTS(i) + 8 * v)
    def want(what, got, expected):
        if got != expected: out.append(f'{what}: {got if not isinstance(got, int) else hex(got)} != model {expected if not isinstance(expected, int) else hex(expected)}')
    if name.startswith('l4a-ioapic-regs'):
        want('ID, VER, ARB', [data(0), data(1), data(2)], [0, 0x170020, 0])
        want('ID and ARB after writing FF000000H (4 bits)', [data(3), data(4)], [0x0f000000, 0x0f000000])
        want('VER after a write', data(5), 0x170020)
        want('entries at reset (masked, 0)', [data(6 + k) for k in range(48)], [0x10000, 0] * 24)
        want('written entries (Remote IRR and delivery status read-only)', [data(54 + k) for k in range(8)],
             [0x1afff, 0xffffffff, 0x10a41, 0x03000000, 0x10f7f, 0x80000000, 0x10031, 0])
        want('unmasked level entry with no source', data(62), 0xa045)
        want('IOREGSEL dword, byte; folded IOWIN; folded IOREGSEL', [data(63), data(64), data(65), data(66)], [0x13, 0x13, 0, 0x10000])
        want('byte IOWIN read; byte write ignored; EOI read; after EOI; offset 20H', [data(67), data(68), data(69), data(70), data(71)],
             [0xffffff00, 0x10000, 0, 0x10000, 0])
    elif name.startswith('l4a-edge'):
        want('deliveries of 44H (each rise once, the masked rise lost)', count(0, irqs.EDGE_V), 4)
        want('entry at the end; TMR[95:64] in the handler', [data(0), data(1) & 1 << 4], [irqs.EDGE_V, 0])
    elif name.startswith('l4a-level'):
        want('deliveries of 54H', count(0, irqs.LEVEL_V), 4)
        rirr = [q(mem, apic.DATA(0) + 0x100 + 8 * k) for k in range(1, 5)]
        want('entry in each handler (Remote IRR set)', rirr, [0xc054] * 4)
        want('TMR bit of 54H in each handler', [q(mem, apic.DATA(0) + 0x180 + 8 * k) >> 20 & 1 for k in range(1, 5)], [1] * 4)
        want('entry after: the window, masked, the unmask, the last', [data(2), data(3), data(4), data(5)], [0x8054, 0x18054, 0x8054, 0x8054])
        want('entry after the EOI register', q(mem, apic.DATA(0) + 0x200), 0x8054)
    elif name.startswith('l4a-pic'):
        want('probe_8259A IMR', data(0), 0xfb)
        want('IRR, ISR (master, slave), IMRs after init', [data(k) for k in range(1, 7)], [0, 0, 0, 0, 0xff, 0xff])
        want('ELCR writable bits', [data(7), data(8)], [0xf8, 0xde])
        want('IRR with IRQ4 masked; after ICW1', [data(9), data(10)], [0x10, 0])
        want('poll, ISR, poll after EOI', [data(11), data(12), data(13)], [0x84, 0x10, 0])
        want('ExtINT deliveries of 34H (1 + 2 level + 1 auto-EOI)', count(0, 0x34), 4)
        want('ISR after the auto-EOI INTA', data(14), 0)
        want('IRR; ISR (special mask); masked poll; poll; ISR after rotate-EOI; poll; IMR', [data(k) for k in range(15, 22)], [0, 0, 0, 0x84, 0, 0x84, 0xef])
        want('PIC ISR in the handlers', [q(mem, apic.DATA(0) + 0x100 + 8 * k) for k in range(1, 5)], [0x10, 0x10, 0x10, 0])
        want('LAPIC ISR[63:32] in the handlers (an ExtINT is not in service)', [q(mem, apic.DATA(0) + 0x180 + 8 * k) for k in range(1, 5)], [0] * 4)
    elif name.startswith('l4a-tmr'):
        want('deliveries of 54H (level from pin 4, then a self IPI)', count(0, irqs.LEVEL_V), 2)
        want('TMR bit of 54H in each handler', [q(mem, apic.DATA(0) + 0x180 + 8 * k) >> 20 & 1 for k in (1, 2)], [1, 0])
        want('entry at the end (Remote IRR clear)', data(0), 0x8054)
    elif name.startswith('l4a-lint0'):
        want('fixed LINT0 45H deliveries; ExtINT 34H with the APIC disabled', [count(0, 0x45), count(0, 0x34)], [2, 1])
        want('PIC IRR, ISR after the fixed delivery (no INTA); ISR after the ExtINT and its EOI', [data(0), data(1), data(2)], [0x10, 0, 0])
    elif name.startswith('l4a-dest'):
        for i, vs in irqs.dest_expected(n).items():
            got = {v: count(i, v) for v in range(256) if count(i, v)}
            want(f'cpu{i} counts', got, vs)
    return out


def check_program(a, prog, label, compare=True, modes=(None, 1)):
    """QEMU (when `compare`) and natemu at each block size: the model on both, natemu equal to QEMU."""
    problems, want, sent = [], None, None
    if compare:
        want, sent, err = qemu(prog, a.out / 'qemu' / label)
        if want is None:
            return [f'QEMU run failed ({err})']
        problems += [f'QEMU vs model: {d}' for d in model_problems(prog, qemu_memory(prog, want))]
    for limit in modes:
        out, p = run_natemu(a, prog, a.out / 'work' / f"{label}-{limit or 'd'}", [limit] if limit else [])
        if out is not None:
            p += model_problems(prog, natemu_memory(out))
            if want is not None:
                p += compare_qemu(prog, out, want)
                mine = bytes.fromhex(out.get('uart', ''))
                if mine != sent:
                    p.append(f'UART bytes {mine!r} != QEMU {sent!r}')
        problems += [f'max-block {limit}: {x}' for x in p]
    return problems


def cmd_qemu(a, report):
    progs = [f(m) for m in ('x', 'x2') for f in (irqs.regs_program, irqs.edge_program, irqs.level_program, irqs.pic_program, irqs.tmr_program)]
    progs += [irqs.dest_program(n) for n in (2, 3, 4)]
    return run_all(a, report, progs, compare=True)


def cmd_xapic(a, report):
    return run_all(a, report, [irqs.dest_program(n, 'x') for n in (2, 3, 4)] + [irqs.lint0_program()], compare=False)


def run_all(a, report, progs, compare):
    failures = []
    for prog, _ in progs:
        label = prog['name']
        problems = check_program(a, prog, label, compare)
        report.append({'name': label, 'problems': problems})
        failures += [f'{label}: {p}' for p in problems]
        print(f"{label}: {'FAIL' if problems else 'ok'}", *problems[:6], sep='\n  ', flush=True)
    return failures


def cmd_stress(a, report):
    failures = []
    for n in (4, 2):
        prog, _ = irqs.stress_program(n, a.stress_rounds)
        label = prog['name']
        out, problems = run_natemu(a, prog, a.out / 'work' / label, ['--irq-source', hex(irqs.SOURCE)], timeout=900)
        counts = {}
        if out is not None:
            mem = natemu_memory(out)
            for src in range(n):
                tgt = (src + 1) % n
                got = (q(mem, apic.COUNTS(tgt) + 8 * (0x70 + src)), q(mem, apic.SHARED + 0x200 + 8 * src))
                counts[src] = got
                if got != (2 * a.stress_rounds,) * 2:
                    problems.append(f'source {src} -> cpu{tgt}: deliveries, completions {got} != {2 * a.stress_rounds} each')
        report.append({'name': label, 'counts': counts, 'problems': problems})
        failures += [f'{label}: {p}' for p in problems]
        print(f"{label}: {'FAIL' if problems else 'ok'} {counts}", *problems[:6], sep='\n  ', flush=True)
    return failures


def cmd_halt(a, report):
    failures = []
    prog, _ = irqs.halt_program()
    # Perf chunk 7 review: and with a 20 ms watchdog, shorter than the 60 ms halt: a halted vCPU is idle, never forced out.
    for veto, watchdog in ((True, None), (False, None), (True, 20)):
        label = f"{prog['name']}-{'veto' if veto else 'no-veto'}" + (f'-watchdog{watchdog}' if watchdog else '')
        extra = ['--irq-source', hex(irqs.SOURCE)] + ([] if veto else ['--irq-source-no-veto']) + (['--watchdog-ms', str(watchdog)] if watchdog else [])
        out, problems = run_natemu(a, prog, a.out / 'work' / label, extra, reached=veto)
        if out is not None:
            c0 = out['cpus'][0]
            delivered = q(natemu_memory(out), apic.COUNTS(0) + 8 * 0x70)
            if veto and delivered != 1:
                problems.append(f'deliveries {delivered} != 1')
            if not veto and not (c0['stop'] == 'resident' and 'halted' in c0['reason']):
                problems.append(f"negative control: cpu0 {c0['stop']} {c0['reason']}, expected ended halted")
        report.append({'name': label, 'problems': problems})
        failures += [f'{label}: {p}' for p in problems]
        print(f"{label}: {'FAIL' if problems else 'ok'}", *problems[:6], sep='\n  ', flush=True)
    return failures


def cmd_lint1(a, report):
    prog, _ = irqs.lint1_program()
    label = prog['name']
    out, problems = run_natemu(a, prog, a.out / 'work' / label, ['--irq-source', hex(irqs.SOURCE)])
    if out is not None:
        mem = natemu_memory(out)
        got = (q(mem, apic.COUNTS(0) + 8 * apic.NMI), q(mem, apic.DATA(0)))
        if got != (2, 3):
            problems.append(f'NMIs, pulses {got} != (2, 3)')
    report.append({'name': label, 'problems': problems})
    print(f"{label}: {'FAIL' if problems else 'ok'}", *problems[:6], sep='\n  ', flush=True)
    return [f'{label}: {p}' for p in problems]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--fexc', type=pathlib.Path, required=True)
    ap.add_argument('--out', type=pathlib.Path, required=True)
    ap.add_argument('--only', type=lambda s: s.split(','), default=['qemu', 'xapic', 'stress', 'halt', 'lint1'])
    ap.add_argument('--stress-rounds', type=int, default=300)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    a.host, a.runtime = vm.build()
    report, failures = {}, []
    for mode, fn in (('qemu', cmd_qemu), ('xapic', cmd_xapic), ('stress', cmd_stress), ('halt', cmd_halt), ('lint1', cmd_lint1)):
        if mode in a.only:
            report[mode] = []
            failures += fn(a, report[mode])
    (a.out / 'l4a.json').write_text(json.dumps({'report': report, 'failures': failures}, indent=1, default=str) + '\n')
    for f in failures: print('FAIL', f)
    print({k: f"{sum(1 for r in v if not r['problems'])}/{len(v)} ok" for k, v in report.items()})
    sys.exit(1 if failures else 0)


if __name__ == '__main__': main()

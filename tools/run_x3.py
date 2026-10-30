#!/usr/bin/env python3
"""X3 checks: the local APIC, its timer and IPIs (boot-features plan, X3; programs in tools/bench/smp/apic.py).

    tools/with-env python3 tools/run_x3.py --fexc FEXC --out DIR [--only qemu,ipi,timers,stress,cases] [--stress-iterations N]

- `qemu`: programs run unchanged by pinned QEMU (system mode) and natemu at block sizes default and 1: register
  write/read-backs (xAPIC MMIO and x2APIC), priority/TPR/EOI order with nested interrupts and self IPIs (both modes),
  CR8 as TPR[7:4], the x2APIC fault rules, x2APIC IPIs at 2-4 CPUs (fixed physical, all-but-self, NMI, logical
  cluster, peer to CPU 0). natemu must equal QEMU (registers, logs without their TSC field, counters) except where an
  entry of QEMU_DEVIATIONS gives QEMU's departure from the SDM, and both are judged by the SDM model below.
- `ipi`: xAPIC MMIO IPIs (Linux's path; QEMU's BSP cannot use xAPIC with peers) at 2 and 4 CPUs: physical, logical flat,
  all-including-self, NMI, lowest priority; counts exact (judged by the model).
- `timers`: one-shot, periodic (two dividers), masked, and HLT-woken timers in both modes, judged from the TSC and the
  run's CNTFRQ by the exact model (QEMU's TCG timer runs at another rate and its own pace: no reference): every
  current-count sample equals the model's count at some V between the RDTSCs around it, no delivery before its expiry,
  lateness bounded, one-shot exactly once, masked never.
- `stress`: random IPIs among 4 (and 2) CPUs plus a periodic timer on each, nested handlers: every IPI counted exactly
  once (sent = received per flow), priority respected at every handler entry, timer deliveries never more than expiries.
- `halt`: HLT across peers: an IPI ring at 2 and 4 CPUs, each CPU halting until its next receipt; every CPU must reach
  its end (the driver must never end a vCPU a peer can still wake).
- `cost` (not in the default set): microseconds per iteration of an xAPIC EOI, TPR write and register read, an x2APIC
  EOI and TPR WRMSR, and a NOP loop, as K2's slope method.
- `cases`: natemu-only driver cases (SDM): xAPIC widths and alignment, a disabled APIC's page, re-enable resets,
  relocation (to a device page: works; onto RAM: typed stop), INIT/start-up (X4's typed hook), SMI, illegal vectors
  and the ESR, a software-disabled APIC, unimplemented xAPIC registers.

Writes DIR/x3.json; exit status 1 on any failure."""
import argparse, json, pathlib, sys
from fractions import Fraction

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'tools' / 'reference'), str(ROOT / 'tools' / 'bench' / 'smp')]
import ref, smp, apic, events, run_slice1_vm as vm  # noqa: E402
from run_slice3_vm import driver, per_vcpu_checks, body_end  # noqa: E402

R = Fraction(125, 3)              # TSC ticks per crystal tick (CPUID.15H: 1 GHz / 24 MHz)
LATE_TSC = 2_000_000              # 2 ms: the bound on a timer interrupt's lateness (doorbell + stop + HLT wake)

# QEMU 11.1.2 departures from the SDM met by these programs: {program: {case: why}}; judged by the SDM instead, and
# natemu is not compared with QEMU there.
_RO, _WO, _RSV = 'WRMSR to a read-only x2APIC register (Table 11-6: #GP)', 'RDMSR of a write-only x2APIC register (Table 11-6: #GP)', \
    'a reserved bit set in an x2APIC write (11.12.1.3: #GP)'
QEMU_DEVIATIONS = {'apic-faults': {
    1: _WO, 4: _RO, 5: _RO, 9: 'APR is not an x2APIC register (Table 11-6: #GP); QEMU reads one', 14: 'ICR high is not an x2APIC register (#GP)',
    15: 'a nonzero write to x2APIC EOI (Table 11-6: #GP)', 16: 'a nonzero write to x2APIC ESR (Table 11-6: #GP)', 17: _RSV, 18: _RSV, 19: _RSV,
    20: _RSV + ': TSC-deadline mode with CPUID.1:ECX.24 clear', 21: _RSV, 22: _RSV, 23: _RO, 24: _RO, 25: _RSV + ': EOI-broadcast suppression not enumerated',
    26: _RSV + ': ICR delivery status', 27: _RSV, 35: "IA32_TSC_DEADLINE: QEMU's CPU advertises TSC-deadline, the profile does not",
    40: 'x2APIC MSRs with the APIC disabled (11.12.5: #GP)'}}


def run_natemu(a, prog, work, limit, timeout=300):
    text = events.description(prog, [[] for _ in range(prog['ncpu'])])
    rc, out, err = driver(a.host, a.runtime, a.fexc, text, work, [limit] if limit else [], timeout=timeout)
    if rc:
        return None, [f'driver exit {rc}: {err.strip()[-400:]}']
    problems = []
    for v, c in enumerate(out['cpus']):
        end = body_end(prog, smp.num(prog['cpus'][v]['rip']))
        if c['stop'] != 'reached' or smp.num(c['rip']) != end:
            problems.append(f"cpu{v} stopped {c['stop']} {c['reason']} at {c['rip']} (end {end:#x})")
        problems += [f'cpu{v} ' + d for d in per_vcpu_checks(c)]
    return out, problems


class Memory:
    """A run's captured regions, read by address."""
    def __init__(self, regions):
        self.regions = regions

    def read(self, at, size=8):
        for base, data in self.regions.items():
            if base <= at and at + size <= base + len(data):
                return int.from_bytes(data[at - base:at - base + size], 'little')
        raise KeyError(hex(at))

    def bytes(self, at, size):
        for base, data in self.regions.items():
            if base <= at and at + size <= base + len(data):
                return data[at - base:at - base + size]
        raise KeyError(hex(at))


def natemu_memory(out):
    return Memory({smp.num(m['addr']): bytes.fromhex(m['data']) for m in out['cpus'][0]['memory']})


def qemu_memory(prog, want):
    return Memory({r['addr']: r['data'] for r in want['memory']})


def masked_logs(prog, data_by_addr):
    """Captured regions with each log entry's TSC field (bytes 56..63) cleared."""
    out = {}
    for base, data in data_by_addr.items():
        if any(base == apic.LOG(i) for i in range(prog['ncpu'])):
            data = bytearray(data)
            for k in range(0, len(data), 0x40):
                data[k + 56:k + 64] = bytes(8)
            data = bytes(data)
        out[base] = data
    return out


def logs(prog, mem):
    return [apic.log(mem.bytes(apic.LOG(i), apic.LOG_BYTES)) for i in range(prog['ncpu'])]


# ---- the SDM model's expectations for the QEMU-compared programs -----------------------------------------------
def isr_set(*v): return sum(1 << (x - 64) for x in v)


PRIO = [(0x71, 2, (0x71,)), (0x61, 2, (0x61,)), (0x51, 3, (0x51,)), (0x41, 4, (0x41,)), (0x42, 5, (0x42,)), (0x62, 5, (0x42, 0x62)),
        (0x52, 5, (0x42, 0x52)), (0x43, 7, (0x43,)), (0x63, 7, (0x43, 0x63))]


def model_problems(prog, mem, qemu=False):
    name, n, out = prog['name'].split('@')[0], prog['ncpu'], []
    entries = logs(prog, mem)
    if name.startswith('apic-prio'):
        got = [(e[0], e[6], e[2], e[3]) for e in entries[0]]
        want = [(v, r12, v & 0xf0, isr_set(*isr)) for v, r12, isr in PRIO]
        if got != want: out.append(f'delivery (vector, marker, PPR, ISR) {[tuple(map(hex, g)) for g in got]} != model {[tuple(map(hex, w)) for w in want]}')
        d = [mem.read(apic.DATA(0) + 8 * k) for k in range(5)]
        # End: PPR 0, ISR clear; in 0x43 after the nested 0x63's two EOIs: clear; in 0x63: 0x43 (ISR+2 holds 64..95).
        if d != [0, 0, 0, 1 << 3, 1 << 3]: out.append(f'DATA {list(map(hex, d))} != model [0, 0, 0, 0x8, 0x8]')
    elif name == 'apic-cr8':
        got = [(e[0], e[6]) for e in entries[0]]
        if got != [(0x55, 2)]: out.append(f'deliveries {got} != [(0x55, 2)]')
        d = [mem.read(apic.DATA(0) + 8 * k) for k in range(4)]
        if d != [0x50, 7, 0x7f, 0]: out.append(f'DATA {list(map(hex, d))} != model [0x50, 0x7, 0x7f, 0x0]')
    elif name == 'apic-faults':
        got = sorted(e[6] for e in entries[0] if e[0] == 13)
        want = [k + 1 for k, (_, gp) in enumerate(apic.FAULT_CASES) if gp]
        if qemu:
            known = QEMU_DEVIATIONS['apic-faults']
            got, want = [c for c in got if c not in known], [c for c in want if c not in known]
        if got != want:
            out.append(f'#GP at cases {sorted(set(got) ^ set(want))} differ from the SDM model ({[apic.FAULT_CASES[c - 1][0].split()[-1] for c in sorted(set(got) ^ set(want))]})')
    elif name.startswith('apic-ipi'):
        want = apic.expected_counts(name, n) if name == 'apic-ipi-x2' else apic.expected_counts_x(n)
        for i in range(n):
            got = {v: mem.read(apic.COUNTS(i) + 8 * v) for v in range(256) if mem.read(apic.COUNTS(i) + 8 * v)}
            if got != want[i]: out.append(f'cpu{i} counts {got} != model {want[i]}')
    return out


def compare_qemu(prog, out, want):
    """natemu vs QEMU: GPRs and flags, and every captured region (logs without TSC)."""
    diffs = []
    for i, (c, w) in enumerate(zip(out['cpus'], want['cpus'])):
        for k, (x, y) in enumerate(zip([smp.num(v) for v in c['gpr']], w['gpr'])):
            if x != y: diffs.append(f'cpu{i} {smp.REGS[k]} {x:#x} != QEMU {y:#x}')
    nat = masked_logs(prog, natemu_memory(out).regions)
    qem = masked_logs(prog, qemu_memory(prog, want).regions)
    for base, data in qem.items():
        # With peers, where an IPI lands in a spinning receiver depends on timing: the counts are compared, not the logs.
        if prog['ncpu'] > 1 and any(base == apic.LOG(i) for i in range(prog['ncpu'])):
            continue
        mine = nat.get(base)
        if mine != data:
            if any(base == apic.LOG(i) for i in range(prog['ncpu'])):
                a, b = apic.log(mine or b''), apic.log(data)
                k = next((j for j in range(max(len(a), len(b))) if a[j:j + 1] != b[j:j + 1]), None)
                diffs.append(f'log {base:#x} entry {k}: natemu {[hex(x) for x in a[k]] if k is not None and k < len(a) else None} QEMU {[hex(x) for x in b[k]] if k is not None and k < len(b) else None}')
            else:
                j = next(j for j in range(len(data)) if mine is None or mine[j] != data[j])
                diffs.append(f'region {base:#x} +{j:#x}: natemu {mine[j:j + 8].hex() if mine else None} QEMU {data[j:j + 8].hex()}')
    return diffs


def compare_fault_cases(prog, out, want):
    """apic-faults: natemu vs QEMU on every case outside QEMU_DEVIATIONS: #GP or not, and the faulting RIP."""
    known = QEMU_DEVIATIONS[prog['name'].split('@')[0]]
    gp = lambda mem: {e[6]: e[1] for e in logs(prog, mem)[0] if e[0] == 13 and e[6] not in known}
    mine, theirs = gp(natemu_memory(out)), gp(qemu_memory(prog, want))
    agreeing = len(apic.FAULT_CASES) - len(known)
    return [] if mine == theirs else [f'#GP (case: RIP) on the {agreeing} agreeing cases: natemu {mine} != QEMU {theirs}']


def cmd_qemu(a, report):
    stub = smp.build_stub(a.out / 'qemu-build')
    failures = []
    programs = [apic.regs_program('x'), apic.regs_program('x2'), apic.prio_program('x'), apic.prio_program('x2'), apic.cr8_program(),
                apic.faults_program()] + [apic.ipi_program(n) for n in (2, 3, 4)]
    for prog, _ in programs:
        label = prog['name']
        rc, err, dump, _ = smp.run_image(prog, smp.build_image(prog, stub), a.out / 'qemu' / label, 'multi', timeout=120)
        want = smp.parse_dump(prog, dump) if rc == 1 else None
        entry = {'name': label, 'runs': []}
        if want is None:
            failures.append(f'{label}: QEMU run failed ({rc} {err})')
        else:
            q = model_problems(prog, qemu_memory(prog, want), qemu=True)
            entry['qemu_vs_model'] = q
            failures += [f'{label}: QEMU vs model: {d}' for d in q]
        for limit in (None, 1):
            out, problems = run_natemu(a, prog, a.out / 'work' / f"{label}-{limit or 'd'}", limit)
            if out is not None:
                problems += model_problems(prog, natemu_memory(out))
                if want is not None and label.split('@')[0] not in QEMU_DEVIATIONS:
                    problems += compare_qemu(prog, out, want)
                elif want is not None:
                    problems += compare_fault_cases(prog, out, want)
            entry['runs'].append({'max_block': limit, 'problems': problems})
            failures += [f'{label} (max-block {limit}): {p}' for p in problems]
        print(f"{label}: {'FAIL' if any(r['problems'] for r in entry['runs']) else 'ok'}", *[p for r in entry['runs'] for p in r['problems']][:6], sep='\n  ', flush=True)
        report.append(entry)
    return failures


def cmd_ipi(a, report):
    failures = []
    for n in (2, 4):
        prog, _ = apic.ipi_x_program(n)
        for limit in (None, 1):
            out, problems = run_natemu(a, prog, a.out / 'work' / f"{prog['name']}-{limit or 'd'}", limit)
            if out is not None:
                problems += model_problems(prog, natemu_memory(out))
            report.append({'name': prog['name'], 'max_block': limit, 'problems': problems})
            failures += [f"{prog['name']} (max-block {limit}): {p}" for p in problems]
            print(f"{prog['name']} max-block {limit}: {'FAIL' if problems else 'ok'}", *problems[:6], sep='\n  ', flush=True)
    return failures


def timer_check(prog, out):
    """Problems and a summary of one timer run (see the module text)."""
    p, mem = prog['params'], natemu_memory(out)
    data, problems = apic.DATA(0), []
    N, div = p['count'], {0: 2, 1: 4, 2: 8, 3: 16, 8: 32, 9: 64, 0xa: 128, 0xb: 1}[p['dcr']]
    periodic, masked = bool(p['lvt'] & 0x20000), bool(p['lvt'] & 0x10000)
    t0, t1 = mem.read(data), mem.read(data + 8)

    def ticks(tb, ta):
        """The divided ticks the model allows between the initial-count write and a read made between TSC tb and ta."""
        lo, hi = Fraction(tb - t1) / R - 2, Fraction(ta - t0) / R + 2
        return max(0, lo // div), max(0, hi // div)
    summary = {'cntfrq': out['cpus'][0]['lapic']['cntfrq'], 'write_tsc_window': t1 - t0}
    last, bad = None, []
    for k in range(p['samples']):
        tb, c, ta = (mem.read(data + 0x100 + 24 * k + 8 * j) for j in range(3))
        emin, emax = ticks(tb, ta)
        if periodic:
            e = (N - c) % N
            first = emin + (e - emin) % N
            ok = 1 <= c <= N and first <= emax
        else:
            ok = max(0, N - emax) <= c <= max(0, N - emin)
            if last is not None and c > last: bad.append(f'sample {k}: count rose {last} -> {c}')
        if not ok: bad.append(f'sample {k}: count {c} outside the model [{emin}, {emax}] ticks (TSC {tb - t1}..{ta - t0} after the write)')
        last = c
    problems += bad[:5]
    summary['samples'] = p['samples']
    if not periodic and p['samples']:
        end_c, end_t = mem.read(data + 0x10), mem.read(data + 0x18)
        if Fraction(end_t - t0) / R > N * div + 2 and end_c != 0: problems.append(f'one-shot count {end_c} after its expiry')
    count = mem.read(apic.COUNTS(0) + 8 * apic.TIMER_V)
    ring = [mem.read(apic.RING(0) + 8 * j) for j in range(mem.read(apic.SHARED + 0x800) // 8)]
    period = N * div * R
    # Delivery j answers the earliest expiry not yet answered (`first`) and every later one already due (merged into it,
    # as the IRR merges them): never before `first` (no early delivery), and `first` waits at most the bound.
    late, early, merged, answered = [], [], 0, 0
    for j, d in enumerate(ring):
        first = answered + 1
        if d < t0 + first * period - 2 * R: early.append(j + 1)
        late.append(d - (t1 + first * period + 2 * R))
        due = int((d - t0 + 2 * R) / period) if periodic else 1
        answered = max(first, due)
        merged += answered - first
    if early: problems.append(f'deliveries {early[:5]} before their expiry')
    if masked and count: problems.append(f'masked timer delivered {count}')
    if not masked:
        want = p['deliveries'] if periodic else 1
        if count != want and not (periodic and count >= want): problems.append(f'{count} deliveries, {want} expected')
        if not periodic and count != 1: problems.append(f'one-shot delivered {count} times')
        if late and max(late) > LATE_TSC: problems.append(f'lateness {float(max(late)):.0f} TSC ticks > {LATE_TSC}')
        if len(ring) != count: problems.append(f'{len(ring)} ring entries for {count} deliveries')
    if p['hlt'] and not out['cpus'][0]['stats']['halts']: problems.append('no HLT waited')
    summary.update(deliveries=count, max_late_tsc=float(max(late)) if late else None, min_late_tsc=float(min(late)) if late else None,
                   merged=merged, halts=out['cpus'][0]['stats']['halts'])
    return problems, summary


def cmd_timers(a, report):
    failures = []
    for name in apic.TIMERS:
        for m in ('x2', 'x'):
            prog, _ = apic.timer_program(name, m)
            for limit in (None, 1):
                out, problems = run_natemu(a, prog, a.out / 'work' / f"{prog['name']}-{limit or 'd'}", limit)
                summary = None
                if out is not None:
                    p2, summary = timer_check(prog, out)
                    problems += p2
                report.append({'name': prog['name'], 'max_block': limit, 'problems': problems, 'summary': summary})
                failures += [f"{prog['name']} (max-block {limit}): {p}" for p in problems]
                print(f"{prog['name']} max-block {limit}: {'FAIL' if problems else 'ok'} {summary}", *problems[:6], sep='\n  ', flush=True)
    return failures


def stress_check(prog, out, n):
    mem, problems = natemu_memory(out), []
    flows = {}
    for s in range(n):
        for t in range(n):
            if s != t:
                sent, recv = mem.read(apic.SHARED + 0x300 + 8 * (4 * s + t)), mem.read(apic.SHARED + 0x200 + 8 * (4 * t + s))
                flows[f'{s}->{t}'] = sent
                if sent != recv: problems.append(f'flow {s}->{t}: sent {sent}, received {recv}')
                v = 0x50 + 0x10 * s + t
                if mem.read(apic.COUNTS(t) + 8 * v) != recv: problems.append(f'cpu{t} vector {v:#x}: {mem.read(apic.COUNTS(t) + 8 * v)} deliveries, {recv} counted')
    if not all(flows.values()): problems.append(f'a flow sent nothing: {flows}')
    if sum(flows.values()) != n * prog['params']['iterations']: problems.append(f"{sum(flows.values())} IPIs sent, {n * prog['params']['iterations']} expected")
    timers = []
    for i in range(n):
        viol = mem.read(apic.DATA(i) + 0x800)
        if viol: problems.append(f'cpu{i}: {viol} priority violations')
        d, elapsed = mem.read(apic.COUNTS(i) + 8 * 0x41), mem.read(apic.DATA(i) + 0x18) - mem.read(apic.DATA(i))
        most = int(Fraction(elapsed) / (prog['params']['period'] * R)) + 1
        if d > most: problems.append(f'cpu{i}: {d} timer deliveries, at most {most} expiries')
        timers.append((d, most))
    return problems, {'flows': flows, 'timer_deliveries_vs_expiries': timers,
                      'interrupts': [c['stats']['interrupts'] for c in out['cpus']], 'shadow_steps': [c['stats']['shadow_steps'] for c in out['cpus']]}


def cmd_stress(a, report):
    failures = []
    for n, m in ((4, 'x2'), (4, 'x'), (2, 'x2')):
        prog, _ = apic.stress_program(n, m, a.stress_iterations)
        for limit in (None, 1):
            out, problems = run_natemu(a, prog, a.out / 'work' / f"{prog['name']}@{n}-{limit or 'd'}", limit, timeout=900)
            summary = None
            if out is not None:
                p2, summary = stress_check(prog, out, n)
                problems += p2
            report.append({'name': prog['name'], 'max_block': limit, 'problems': problems, 'summary': summary})
            failures += [f"{prog['name']} (max-block {limit}): {p}" for p in problems]
            print(f"{prog['name']} max-block {limit}: {'FAIL' if problems else 'ok'} {summary}", *problems[:6], sep='\n  ', flush=True)
    return failures


def cmd_halt(a, report):
    """HLT across peers: the IPI ring (apic.pingpong_program) at 2 and 4 CPUs, block sizes default and 1: every CPU
    reaches its end with every receipt (the driver must never end a vCPU a peer can still wake)."""
    failures = []
    for n in (2, 4):
        prog, _ = apic.pingpong_program(n)
        for limit in (None, 1):
            out, problems = run_natemu(a, prog, a.out / 'work' / f"{prog['name']}-{limit or 'd'}", limit, timeout=600)
            if out is not None:
                mem = natemu_memory(out)
                got = [mem.read(apic.COUNTS(i) + 8 * 0x50) for i in range(n)]
                if got != [prog['params']['rounds']] * n: problems.append(f'receipts {got}')
                summary = {'halts': [c['stats']['halts'] for c in out['cpus']]}
            else:
                summary = None
            report.append({'name': prog['name'], 'max_block': limit, 'problems': problems, 'summary': summary})
            failures += [f"{prog['name']} (max-block {limit}): {p}" for p in problems]
            print(f"{prog['name']} max-block {limit}: {'FAIL' if problems else 'ok'} {summary}", *problems[:4], sep='\n  ', flush=True)
    # A vCPU woken only by its own timer (no post) is never ended: a timer vector, and an illegal timer vector whose
    # expiry raises the LVT error vector (the second review's repro); with the error entry's vector illegal too, the expiry
    # raises nothing and the HLT can never wake: the driver ends it (`halted`, RIP after the HLT).
    w = lambda msr, v: f'mov ecx, {msr:#x}\nmov eax, {v:#x}\nxor edx, edx\nwrmsr\n'
    head = 'mov ecx, 0x1b\nrdmsr\nor eax, 0xc00\nwrmsr\n' + w(0x80f, 0x1ff)
    for name, error, timer, want in (('selfwake-timer', 0x10060, 0x60, 0x60), ('selfwake-illegal-timer-via-error', 0x60, 3, 0x60),
                                     ('selfwake-none', 0x5, 3, None)):
        pre = head + w(0x837, error) + w(0x832, timer) + w(0x83e, 0xb) + w(0x838, 2_400_000)
        code = ref.assemble(pre + 'hlt\nmov ebx, 1\ncmp r11, r11')
        after = BASE + len(ref.assemble(pre + 'hlt'))
        text = f'cpl 0\nif 1\nrip {BASE:#x}\ncode {code.hex()}\n'
        for limit in (None, 1):
            rc, out, err = driver(a.host, a.runtime, a.fexc, text, a.out / 'work' / f"{name}-{limit or 'd'}", [limit] if limit else [], timeout=120)
            if rc:
                problems = [f'driver exit {rc}: {err.strip()[-300:]}']
            elif want is None:
                ok = out['stop'] == 'resident' and out['reason'].startswith('halted') and smp.num(out['rip']) == after
                problems = [] if ok else [f"{out['stop']} {out['reason']} at {out['rip']}"]
            else:
                f = [smp.num(x) for x in out.get('fault') or []]
                ok = out['stop'] == 'guest_fault' and f[:1] == [want] and f[3:] == [after]
                problems = [] if ok else [f"{out['stop']} {out['reason']} {out.get('fault')} (want vector {want:#x} at {after:#x})"]
            report.append({'name': name, 'max_block': limit, 'problems': problems})
            failures += [f'{name} (max-block {limit}): {p}' for p in problems]
            print(f"{name} max-block {limit}: {'FAIL' if problems else 'ok'}", *problems, sep='\n  ', flush=True)
    return failures


COSTS = {
    # name: (set-up, loop body); r8 counts the iterations; CPL 0, 1 vCPU, the APIC software enabled.
    'xapic-eoi': ('xor eax, eax\n', 'movabs dword ptr [0xfee000b0], eax'),
    'xapic-tpr': ('xor eax, eax\n', 'movabs dword ptr [0xfee00080], eax'),
    'xapic-read': ('', 'movabs eax, dword ptr [0xfee00030]'),
    'x2apic-eoi': ('mov ecx, 0x1b\nrdmsr\nor eax, 0xc00\nwrmsr\nmov ecx, 0x80b\nxor eax, eax\nxor edx, edx\n', 'wrmsr'),
    'x2apic-tpr': ('mov ecx, 0x1b\nrdmsr\nor eax, 0xc00\nwrmsr\nmov ecx, 0x808\nxor eax, eax\nxor edx, edx\n', 'wrmsr'),
    'nop': ('', 'nop'),
}


def cmd_cost(a, report, n=100_000, reps=3):
    """Microseconds per loop iteration of one APIC access, as the slope (t(2N) - t(N)) / N of the driver's wall time
    (start-up and translations fall into the intercept), median of `reps`; each run must reach its end."""
    import statistics, time
    failures = []
    for name, (setup, body) in COSTS.items():
        code = ref.assemble(setup + f'1: {body}\ndec r8\njnz 1b\ncmp r11, r11')
        times = {n: [], 2 * n: []}
        for rep in range(reps + 1):
            for k in (n, 2 * n):
                gpr = 'gpr ' + ' '.join(['0'] * 4 + ['0x201000'] + ['0'] * 3 + [hex(k)] + ['0'] * 7)
                text = f'cpl 0\nif 0\nsvr 0x1ff\nrip {BASE:#x}\nregion 0x200000 0x1000\n{gpr}\nmmio 0xfee00000 0xfee00000 unassigned\ncode {code.hex()}\n'
                t0 = time.perf_counter()
                rc, out, err = driver(a.host, a.runtime, a.fexc, text, a.out / 'cost' / f'{name}-{k}', ['--watchdog-ms', '600000'], timeout=600)
                dt = time.perf_counter() - t0
                if rc or out['stop'] != 'reached':
                    failures.append(f"cost {name} n={k}: {err[-200:] if rc else (out['stop'], out['reason'])}")
                elif rep:
                    times[k].append(dt)
        if times[n] and times[2 * n]:
            us = (statistics.median(times[2 * n]) - statistics.median(times[n])) / n * 1e6
            report.append({'name': name, 'us_per_iteration': round(us, 3)})
            print(f'{name}: {us:.3f} us per iteration', flush=True)
    return failures


# ---- natemu-only driver cases -------------------------------------------------------------------------------------
BASE = 0x110000
X2_ON = 'mov ecx, 0x1b\nrdmsr\nor eax, 0xc00\nwrmsr\n'
SVR_ON = 'mov ecx, 0x80f\nmov eax, 0x1ff\nxor edx, edx\nwrmsr\n'


def mmio_ld(off, reg='eax'): return f'movabs {reg}, dword ptr [{0xfee00000 + off:#x}]\n' if reg == 'eax' else ''
def mmio_st(off): return f'movabs dword ptr [{0xfee00000 + off:#x}], eax\n'


def cases():
    def reached(**want):
        def check(out):
            g = {r: smp.num(out['gpr'][smp.REGS.index(r)]) for r in want}
            return [] if out['stop'] == 'reached' and g == want else [f"{out['stop']} {out['reason']} at {out['rip']} {({k: hex(v) for k, v in g.items()})} want {({k: hex(v) for k, v in want.items()})}"]
        return check

    def stopped(reason, at, status=None):
        def check(out):
            ok = out['stop'] == 'resident' and out['reason'].startswith(reason) and smp.num(out['rip']) == at
            if status: ok = ok and out.get('mmio', {}).get('status') == status
            return [] if ok else [f"{out['stop']} {out['reason']} at {out['rip']} mmio {out.get('mmio')} (want {reason} {status or ''} at {at:#x})"]
        return check
    out = []
    apic_page = 'mmio 0xfee00000 0xfee00000 unassigned\n'

    def case(name, text, check, extra=apic_page, svr=None):
        code = ref.assemble(text + 'cmp r11, r11')
        out.append((name, f'cpl 0\nrip {BASE:#x}\n' + (f'svr {svr:#x}\n' if svr else '') + extra + f'code {code.hex()}\n', check))

    def at(text):
        return BASE + len(ref.assemble(text)) if text else BASE
    # xAPIC MMIO: a 32-bit read of the version; other widths and offsets inside a register's 16 bytes are refused
    # (11.4.1) as the device's (state at the instruction); the APIC disabled through IA32_APIC_BASE leaves the page to no
    # device (unassigned).
    case('xapic-version', mmio_ld(0x30) + 'mov ebx, eax\n', reached(rbx=0x50014))
    case('xapic-byte', 'nop\nmovabs al, byte ptr [0xfee00030]\n', stopped('unsupported_mmio', BASE + 1, 'device'))
    case('xapic-word', 'nop\nmovabs ax, word ptr [0xfee00030]\n', stopped('unsupported_mmio', BASE + 1, 'device'))
    case('xapic-qword', 'nop\nmovabs rax, qword ptr [0xfee00030]\n', stopped('unsupported_mmio', BASE + 1, 'device'))
    case('xapic-misaligned', 'nop\nmovabs eax, dword ptr [0xfee00034]\n', stopped('unsupported_mmio', BASE + 1, 'device'))
    off = 'mov ecx, 0x1b\nrdmsr\nand eax, 0xfffff7ff\nwrmsr\n'
    case('xapic-disabled', off + 'movabs eax, dword ptr [0xfee00030]\n', stopped('unsupported_mmio', at(off), 'unassigned'))
    # Re-enabled after a disable: every register at reset (SVR 0xff, LVT masked, TPR 0, LDR 0, DFR all ones).
    on = 'mov ecx, 0x1b\nrdmsr\nor eax, 0x800\nwrmsr\n'
    setup = 'mov eax, 0x40\n' + mmio_st(0x80) + 'mov eax, 0x1ff\n' + mmio_st(0xf0) + 'mov eax, 0x20033\n' + mmio_st(0x320) + 'mov eax, 0x5000000\n' + mmio_st(0xd0)
    reads = mmio_ld(0xf0) + 'mov ebx, eax\n' + mmio_ld(0x320) + 'mov esi, eax\n' + mmio_ld(0x80) + 'mov edi, eax\n' + mmio_ld(0xd0) + 'mov r8d, eax\n' + mmio_ld(0xe0) + 'mov r9d, eax\nmov r10, cr8\n'
    case('xapic-reenable-resets', setup + off + on + reads, reached(rbx=0xff, rsi=0x10000, rdi=0, r8=0, r9=0xffffffff, r10=0))
    # Relocation: onto a device page the APIC answers there; onto guest RAM it stops (the resident cannot overlay RAM).
    reloc = 'mov ecx, 0x1b\nrdmsr\nmov eax, 0xfed00900\nwrmsr\nmovabs eax, dword ptr [0xfed00030]\nmov ebx, eax\n'
    case('relocate-device', reloc, reached(rbx=0x50014), extra='mmio 0xfed00000 0xfed00000 unassigned\n')
    ram = 'mov ecx, 0x1b\nrdmsr\nmov eax, 0x200900\nnop\n'
    case('relocate-ram', ram + 'wrmsr\n', stopped('unsupported_system', at(ram)))
    # INIT, start-up (X4's hook) and SMI: typed stops before any effect; INIT level de-assert: no effect.
    for name, low in (('init', 0x4500), ('startup', 0x4608), ('smi', 0x4200)):
        pre = X2_ON + SVR_ON + f'mov ecx, 0x830\nmov eax, {low:#x}\nmov edx, 1\n'
        case(f'ipi-{name}', pre + 'wrmsr\n', stopped('unsupported_system', at(pre)))
    case('ipi-init-deassert', X2_ON + SVR_ON + 'mov ecx, 0x830\nmov eax, 0xc8500\nxor edx, edx\nwrmsr\nmov ebx, 1\n', reached(rbx=1))
    xpre = 'mov eax, 0x1000000\n' + mmio_st(0x310) + 'mov eax, 0x4500\nnop\n'
    case('ipi-init-xapic', xpre + mmio_st(0x300), stopped('unsupported_system', at(xpre)), svr=0x1ff)
    # Illegal vectors (11.5.3): a fixed IPI or SELF IPI with vector 0..15 sets "send illegal vector" (bit 5) and sends
    # nothing; an LVT timer vector 3 expiring sets "receive illegal vector" (bit 6). The ESR shows them after a write.
    esr = 'mov ecx, 0x828\nxor eax, eax\nxor edx, edx\nwrmsr\nrdmsr\n'
    irr0 = 'mov ecx, 0x820\nrdmsr\nmov r9d, eax\n'
    case('esr-send-illegal', X2_ON + SVR_ON + 'mov ecx, 0x830\nmov eax, 0x5\nxor edx, edx\nwrmsr\n' + irr0 + esr + 'mov ebx, eax\n' + esr + 'mov esi, eax\n', reached(rbx=0x20, rsi=0, r9=0))
    case('esr-self-illegal', X2_ON + SVR_ON + 'mov ecx, 0x83f\nmov eax, 3\nxor edx, edx\nwrmsr\n' + esr + 'mov ebx, eax\n', reached(rbx=0x20))
    timer3 = 'mov ecx, 0x832\nmov eax, 3\nxor edx, edx\nwrmsr\nmov ecx, 0x83e\nmov eax, 0xb\nwrmsr\nmov ecx, 0x838\nmov eax, 24\nwrmsr\n'
    wait = 'mov r8d, 2000000\n1: pause\ndec r8d\njnz 1b\n'
    case('esr-timer-illegal', X2_ON + SVR_ON + timer3 + wait + esr + 'mov ebx, eax\n' + irr0, reached(rbx=0x40, r9=0))
    # The ESR is latched by a write: an error shows only after the next write.
    case('esr-latch', X2_ON + SVR_ON + 'mov ecx, 0x83f\nmov eax, 3\nxor edx, edx\nwrmsr\nmov ecx, 0x828\nrdmsr\nmov ebx, eax\n' + esr + 'mov esi, eax\n', reached(rbx=0, rsi=0x20))
    # Unimplemented xAPIC offsets: an illegal register access (bit 7), read as 0; APR/RRD read 0 without one.
    case('xapic-illegal-register', 'mov eax, 1\n' + mmio_st(0x40) + mmio_ld(0x50) + 'mov esi, eax\n' + mmio_ld(0x90) + 'mov edi, eax\n' +
         'xor eax, eax\n' + mmio_st(0x280) + mmio_ld(0x280) + 'mov ebx, eax\n', reached(rbx=0x80, rsi=0, rdi=0), svr=0x1ff)
    # Read-only xAPIC registers ignore writes (ID, version, PPR, ISR, IRR, current count).
    ro = ''.join(f'mov eax, 0xffffffff\n' + mmio_st(o) for o in (0x20, 0x30, 0xa0, 0x100, 0x200, 0x390))
    case('xapic-readonly', ro + mmio_ld(0x20) + 'mov ebx, eax\n' + mmio_ld(0x30) + 'mov esi, eax\n' + mmio_ld(0xa0) + 'mov edi, eax\n' + mmio_ld(0x100) + 'mov r8d, eax\n'
         + mmio_ld(0x200) + 'mov r9d, eax\n' + mmio_ld(0x390) + 'mov r10d, eax\n', reached(rbx=0, rsi=0x50014, rdi=0, r8=0, r9=0, r10=0), svr=0x1ff)
    # Software disabled (SVR bit 8 clear, the reset state): a self IPI is not accepted; LVT writes stay masked.
    case('svr-disabled', X2_ON + 'mov ecx, 0x83f\nmov eax, 0x40\nxor edx, edx\nwrmsr\nmov ecx, 0x822\nrdmsr\nmov ebx, eax\n'
         'mov ecx, 0x832\nmov eax, 0x40\nwrmsr\nrdmsr\nmov esi, eax\n' + SVR_ON + 'mov ecx, 0x832\nmov eax, 0x40\nwrmsr\nrdmsr\nmov edi, eax\n', reached(rbx=0, rsi=0x10040, rdi=0x40))
    # Disabling through SVR masks every LVT entry.
    case('svr-disable-masks', X2_ON + SVR_ON + 'mov ecx, 0x835\nmov eax, 0x700\nxor edx, edx\nwrmsr\nmov ecx, 0x80f\nmov eax, 0xff\nwrmsr\nmov ecx, 0x835\nrdmsr\nmov ebx, eax\n', reached(rbx=0x10700))
    # x2APIC MSRs in xAPIC mode, and xAPIC's page in x2APIC mode (no APIC there: unassigned).
    case('x2-msr-in-xapic', 'nop\nmov ecx, 0x803\nrdmsr\n', lambda o: [] if o['stop'] == 'guest_fault' and [smp.num(x) for x in o['fault']] == [13, 0, 0, BASE + 6] else [f"{o['stop']} {o['reason']} {o.get('fault')}"])
    case('xapic-page-in-x2', X2_ON + 'movabs eax, dword ptr [0xfee00030]\n', stopped('unsupported_mmio', at(X2_ON), 'unassigned'))
    # Timer register writes while it counts (11.5.4): a one-shot count that reached 0 (masked) stays 0 with no expiry when
    # the LVT switches to periodic and unmasks; a divide change keeps the current count (counting on at the new rate).
    lvt = lambda v: f'mov ecx, 0x832\nmov eax, {v:#x}\nxor edx, edx\nwrmsr\n'
    start = 'mov ecx, 0x83e\nmov eax, 0xb\nxor edx, edx\nwrmsr\nmov ecx, 0x838\nmov eax, {n}\nwrmsr\n'
    ccr = 'mov ecx, 0x839\nrdmsr\n'
    case('timer-spent-oneshot-to-periodic', X2_ON + SVR_ON + lvt(0x10040) + start.format(n=24) + wait + lvt(0x20040) + wait + ccr + 'mov ebx, eax\n'
         'mov ecx, 0x822\nrdmsr\nmov esi, eax\n', reached(rbx=0, rsi=0))

    def continuous(out):
        g = lambda r: smp.num(out['gpr'][smp.REGS.index(r)])
        c1, c2, c3 = g('rbx'), g('rsi'), g('rdi')
        ok = out['stop'] == 'reached' and 23_000_000 < c2 <= c1 and c1 - c2 < 100_000 and c2 - c3 < 1000
        return [] if ok else [f"{out['stop']} {out['reason']} counts {c1}, {c2} (after /1 -> /128), {c3} (later)"]
    case('timer-divide-keeps-count', X2_ON + SVR_ON + lvt(0x10040) + start.format(n=24_000_000) + ccr + 'mov ebx, eax\n'
         'mov ecx, 0x83e\nmov eax, 0xa\nxor edx, edx\nwrmsr\n' + ccr + 'mov esi, eax\nmov r8d, 20000\n2: pause\ndec r8d\njnz 2b\n' + ccr + 'mov edi, eax\n', continuous)
    # CR8 <-> TPR across both access paths.
    case('cr8-tpr-xapic', 'mov eax, 0x9c\n' + mmio_st(0x80) + 'mov rbx, cr8\nmov eax, 3\nmov cr8, rax\n' + mmio_ld(0x80) + 'mov esi, eax\n' + mmio_ld(0xa0) + 'mov edi, eax\n',
         reached(rbx=9, rsi=0x30, rdi=0x30), svr=0x1ff)
    return out


def cmd_cases(a, report):
    failures = []
    for name, text, check in cases():
        for limit in (None, 1):
            rc, out, err = driver(a.host, a.runtime, a.fexc, text, a.out / 'work' / f"{name}-{limit or 'd'}", [limit] if limit else [], timeout=120)
            problems = check(out) if rc == 0 else [f'driver exit {rc}: {err.strip()[-300:]}']
            report.append({'name': name, 'max_block': limit, 'problems': problems})
            failures += [f'{name} (max-block {limit}): {p}' for p in problems]
            print(f"{name} max-block {limit}: {'ok' if not problems else 'FAIL'} {problems}", flush=True)
    return failures


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--fexc', type=pathlib.Path, required=True)
    ap.add_argument('--out', type=pathlib.Path, required=True)
    ap.add_argument('--only', type=lambda s: s.split(','), default=['qemu', 'ipi', 'timers', 'stress', 'halt', 'cases'])
    ap.add_argument('--stress-iterations', type=int, default=2000)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    a.host, a.runtime = vm.build()
    report, failures = {}, []
    for mode, fn in (('qemu', cmd_qemu), ('ipi', cmd_ipi), ('timers', cmd_timers), ('stress', cmd_stress), ('halt', cmd_halt), ('cases', cmd_cases), ('cost', cmd_cost)):
        if mode in a.only:
            report[mode] = []
            failures += fn(a, report[mode])
    (a.out / 'x3.json').write_text(json.dumps({'report': report, 'failures': failures, 'qemu_deviations': QEMU_DEVIATIONS}, indent=1, default=str) + '\n')
    for f in failures: print('FAIL', f)
    print({k: f'{sum(1 for r in v if not (r.get("problems") or any(x["problems"] for x in r.get("runs", []))))}/{len(v)} ok' for k, v in report.items()})
    sys.exit(1 if failures else 0)


if __name__ == '__main__': main()

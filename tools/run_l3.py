#!/usr/bin/env python3
"""L3 checks: port I/O (IN/OUT/INS/OUTS), the I/O protection, the 16550A UART and the unassigned rule (boot-features
plan, L3; programs in tools/bench/smp/ports.py).

    tools/with-env python3 tools/run_l3.py --fexc FEXC --out DIR [--only qemu,stress,cases,pages] [--kicks 2000]

- `qemu`: programs run unchanged by pinned QEMU (system mode, COM1 = `-serial file`) and natemu at block sizes default and
  1; final registers, the captured memory (every IN result with RAX preloaded, the exception log, string buffers) and the
  bytes the UART sent must be equal. `l3-uart`: every width at every UART register, divisor latch, REX.W forms, Linux's
  8250 `autoconfig`/`autoconfig_16550a`/`clear_fifos` and earlyprintk sequences transcribed, LSR-polled output, THRE
  interrupt bookkeeping, loopback with a 14-byte FIFO trigger (overrun, IIR priorities, draining). `l3-unassigned`: ports
  QEMU leaves unassigned, imm8 and DX, reads and writes at 1/2/4 bytes (all-ones, writes ignored); natemu's per-port counts
  must be exact. `l3-strings`: REP OUTSB/OUTSD, OUTSW, DF = 1, a zero count, 0x67 (ESI/ECX), an FS override, REP INSB/INSW/
  INSD (loopback FIFO, an unassigned port), and #PF midway through REP OUTSB and REP INSB (RSI/RDI/RCX, error code, CR2, and
  the FIFO keeps the bytes not read). `l3-io-*` (CPL 3): IOPL 3; IOPL 0 without a bitmap; a full bitmap with the trailing
  byte; a bitmap cut by the TSS limit (the two-byte read); an I/O map base beyond the limit: each IN/OUT/INS/OUTS case
  completes or takes #GP(0) at the instruction. QEMU's run starts after SeaBIOS, which has used COM1: programs first set
  the UART's reset values, and QEMU's output is taken after SeaBIOS's banner. Not compared with QEMU (QEMU 11.1.2 departs
  from a PC there): an access crossing 3FFH (QEMU aborts, serial.c:476), a misaligned wide access to an unassigned port
  (QEMU's memory core reads 0, not all-ones) and a wide access at FFFFH (its upper bytes lie past QEMU's 64 KiB I/O space; not run).
- `stress` (natemu only): a local APIC one-shot timer armed 20 us before each of 64 REP OUTSB of 512 bytes to an unassigned
  port (all but two of its interrupts must arrive between elements: deterministic, P3 review), then REP OUTSB to the THR
  and loopback REP OUTSB/REP INSB in a loop with IF = 1 while kicks post events (`--kick-events`, every 20-60 us): every
  delivery at a REP's RIP has RF = 1 and 0 < RCX < count (or RF = 0 before the first element), RF = 0 elsewhere (every
  delivery is logged); the UART's output and the received bytes are exactly the program's (each element once).
- `cases` (natemu only, judged by the SDM and the owner's rule): the strict switch (IN, REP OUTSD crossing into an
  unassigned port), Linux's PCI type 1/type 2 probes and `read_pci_config_16` with no PCI (all-ones; 0xCF8 does not hold
  its value: no type-1 mechanism), the port wrap at FFFFH, an access crossing the UART's end (byte by byte), LOCK IN (#UD), F3 IN (not decoded: a typed stop), INS to a device
  page (refused before the port is read), INS #AC and #PF at CPL 3 (the port not read), CPL 3 with the driver's TSS (no
  bitmap: #GP(0)), and CS/DS-prefixed near branches (admission).
- `pages` (natemu only, the owner's rule of October 29 and its amendment; a CPL 0 program on its own tables): unassigned
  pages (the legacy hole, 0xC0000000 above RAM (2 GiB, or 4 MiB), 0x80_0000_0000 above 4 GiB, 0xFED00000) read through the
  shared all-ones shadow by CMP, LODS, REPE CMPSB, MOVUPS and a load-op ADD (one fill each, counted), MOV writes ignored and
  counted, RMW/XCHG/STOS refused, strict read and write stops; a read crossing from RAM's last page; two vCPUs each filling;
  the xAPIC page disabled and in x2APIC mode (all-ones from the LAPIC, never shadowed, not counted) and a device at
  0xFEC00000, the I/O APIC's (L4a; never shadowed); the shadow drops: a write after a fill, a remap after INVLPG and after a CR3 reload (hole to
  RAM, RAM to hole), an APIC base moved onto a filled page, and two vCPUs with different APIC bases.

Writes DIR/l3.json; exit status 1 on any failure."""
import argparse, json, os, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'tools' / 'reference'), str(ROOT / 'tools' / 'bench' / 'smp')]
import ref, smp, events, ports, run_slice1_vm as vm  # noqa: E402
from run_slice3_vm import driver, per_vcpu_checks, body_end, vm_state, kick_checks  # noqa: E402

RF = 1 << 16


def natemu(a, prog, work, extra=(), timeout=300):
    text = events.description(prog['natemu'], prog['hooks'])
    for attempt in range(2):  # one retry on a concurrent hv_vm_create (0xfae94007)
        rc, out, err = driver(a.host, a.runtime, a.fexc, text, work, list(extra), timeout=timeout)
        if rc == 0 or 'fae94007' not in err: break
    if rc: return None, [f'driver exit {rc}: {err.strip()[-400:]}']
    problems = []
    for v, c in enumerate(out['cpus']):
        end = body_end(prog['natemu'], smp.num(prog['natemu']['cpus'][v]['rip']))
        if c['stop'] != 'reached' or smp.num(c['rip']) != end:
            problems.append(f"cpu{v} stopped {c['stop']} {c['reason']} at {c['rip']} (end {end:#x})")
        problems += [f'cpu{v} ' + d for d in per_vcpu_checks(c)]
    return out, problems


def qemu(prog, work):
    work.mkdir(parents=True, exist_ok=True)
    serial = work / 'serial.out'
    serial.unlink(missing_ok=True)
    stub = smp.build_stub(work)
    rc, err, dump, _ = smp.run_image(prog, smp.build_image(prog, stub), work, 'multi', extra=['-serial', f'file:{serial}'])
    if rc != 1: raise RuntimeError(f"QEMU {prog['name']}: {rc} {err}")
    # SeaBIOS writes its banner to COM1 before the program: the program's output is what follows it.
    sent = serial.read_bytes()
    return smp.parse_dump(prog, dump), sent.split(b'Booting from ROM..')[-1]


def cmd_qemu(a, report):
    progs = [ports.uart_program(), ports.unassigned_program(), ports.strings_program()] + [ports.protection_program(n) for n in ports.IO_CONFIGS]
    failures = []
    for p in progs:
        name = p['qemu']['name']
        want, sent = qemu(p['qemu'], a.out / 'qemu' / name)
        entry = {'name': name, 'qemu_uart_bytes': len(sent), 'runs': []}
        for block in (None, 1):
            out, problems = natemu(a, p, a.out / 'natemu' / f'{name}-{block}', [block] if block else [])
            if out:
                problems += smp.compare(p['qemu'], vm_state(p['natemu'], out), want)
                got = bytes.fromhex(out.get('uart', ''))
                if got != sent: problems.append(f'uart output {got!r} != QEMU {sent!r}')
                if name.startswith('l3-unassigned'):
                    counts = {int(e['port'], 0): (e['reads'], e['writes']) for e in out.get('ports', []) if e['unassigned']}
                    if counts != tally(ports.UNASSIGNED_ACCESSES): problems.append(f'unassigned port counts {counts} != {tally(ports.UNASSIGNED_ACCESSES)}')
                entry['ports'] = out.get('ports')
                gp = [e for e in events.log(bytes.fromhex(next(m['data'] for m in out['cpus'][0]['memory'] if smp.num(m['addr']) == ports.LOG(0))))]
                entry['exceptions'] = [(v, hex(r)) for v, _, r, *_ in gp]
            entry['runs'].append({'block': block, 'problems': problems})
            failures += [f'{name} block={block}: {d}' for d in problems]
            print(f"{name} block={block}: {'ok' if not problems else problems[:4]}", flush=True)
        report['qemu'].append(entry)
    return failures


def cmd_stress(a, report):
    """Deliveries between the elements of REP string instructions (RF = 1, RCX between its start and 0) and before the first
    (RF = 0, RCX unchanged), never elsewhere at a REP. The timer phase makes them deterministic: each of its one-shot
    interrupts must arrive between elements (at most two may come before the first, e.g. while the round's first
    translation is compiled); the driver's kicks (every 20 us plus up to 40 us at random, events 0x40/0x41) add coverage."""
    rounds = 150
    p = ports.stress_program(rounds)
    labels = p['labels'][0]
    reps = {labels['l3_outs']: len(ports.SOURCE), labels['l3_loop_outs']: 16, labels['l3_ins']: 16, labels['l3_touts']: ports.TIMER_OUTS}
    failures = []
    for block in (None, 1):
        extra = ([block] if block else []) + ['--kick-vcpu', 0, '--kicks', a.kicks, '--kick-every-us', 20, '--kick-jitter-us', 40,
                                              '--kick-events', '0x40,0x41']
        out, problems = natemu(a, p, a.out / 'stress' / f'{block}', extra, timeout=600)
        stats = {}
        if out:
            c = out['cpus'][0]
            # The timer's expiries ring the doorbell too (at most one per round): host doorbells are the kicks plus those.
            problems += [d for d in kick_checks(c) if not d.startswith('host doorbells')]
            k = c['kicks']
            if not 0 <= k['irqs'] - k['irq_sgis'] + k['pending'] - k['issued'] <= ports.TIMER_ROUNDS:
                problems.append(f"host doorbells {k['irqs'] - k['irq_sgis']} + pending {k['pending']}: not issued {k['issued']} plus timer rings")
            memory = {smp.num(m['addr']): bytes.fromhex(m['data']) for m in c['memory']}
            log = events.log(memory[ports.STRESS_LOG_AT])
            mid, timer, timer_mid = 0, 0, 0
            for vector, _, rip, rflags, _, rcx, _ in log:
                if vector not in (0x40, 0x41, ports.TIMER_VECTOR):
                    problems.append(f'vector {vector} at {rip:#x}'); continue
                timer += vector == ports.TIMER_VECTOR
                if rip in reps:
                    n = reps[rip]
                    if 0 < rcx < n and rflags & RF:
                        mid += 1
                        timer_mid += vector == ports.TIMER_VECTOR and rip == labels['l3_touts']
                    elif not (rcx == n and not rflags & RF): problems.append(f'delivery at the REP {rip:#x}: RCX {rcx}, RF {rflags >> 16 & 1}')
                elif rflags & RF: problems.append(f'RF = 1 at {rip:#x} (not a REP)')
            if len(log) * 0x40 >= ports.STRESS_LOG: problems.append('the log is full: deliveries were not logged')
            if timer != ports.TIMER_ROUNDS: problems.append(f'{timer} timer interrupts for {ports.TIMER_ROUNDS} rounds')
            if timer_mid < ports.TIMER_ROUNDS - 2: problems.append(f'only {timer_mid} of {ports.TIMER_ROUNDS} timer interrupts between elements')
            if bytes.fromhex(out.get('uart', '')) != ports.SOURCE * rounds: problems.append('UART output differs from the program\'s')
            if memory[ports.DATA(0)][0x100:0x100 + 16 * rounds] != ports.SOURCE[:16] * rounds: problems.append('received bytes differ')
            stats = {'deliveries': len(log), 'mid_string': mid, 'timer_mid': timer_mid, 'kicks': c['kicks']['issued']}
        report['stress'].append({'block': block, 'stats': stats, 'problems': problems})
        failures += [f'stress block={block}: {d}' for d in problems]
        print(f"stress block={block}: {stats} {'ok' if not problems else problems[:4]}", flush=True)
    return failures


# ---- natemu-only cases --------------------------------------------------------------------------------------------
CODE = 0x100000


def gprs(**kw):
    g = [0] * 16
    for k, v in kw.items(): g[['rax', 'rcx', 'rdx', 'rbx', 'rsp', 'rbp', 'rsi', 'rdi'].index(k)] = v
    return 'gpr ' + ' '.join(hex(x) for x in g)


def tally(accesses):
    """{port: (reads, writes)} of [(port, size, 'r'|'w')]: an access counts at every port it touches (FFFFH wraps)."""
    out = {}
    for port, size, kind in accesses:
        for k in range(size):
            r, w = out.get((port + k) & 0xffff, (0, 0))
            out[(port + k) & 0xffff] = (r + (kind == 'r'), w + (kind == 'w'))
    return out


def counts(out):
    return {int(e['port'], 0): (e['reads'], e['writes']) for e in out.get('ports', [])}


def cases():
    asm = ref.assemble
    out = []
    def case(name, text, check, extra=(), cpl=0, rflags=0x2, more='', regs=None):
        code = asm(text)
        out.append((name, f'rip {CODE:#x}\nrflags {rflags:#x}\ncpl {cpl}\n{regs or gprs(rsp=0x3e1800)}\ncode {code.hex()}\nregion 0x200000 0x1000\nregion 0x3e0000 0x2000\n{more}',
                    check, list(extra), code))
    def stop(reason, at, **want):
        def check(o, code):
            p = []
            if o['stop'] != 'resident' or o['reason'].split()[0] != reason: p.append(f"stop {o['stop']} {o['reason']}")
            if int(o['rip'], 0) != at(code): p.append(f"rip {o['rip']} != {at(code):#x}")
            for k, v in want.items():
                got = o['mmio'][k] if k in ('status', 'address') else int(o['gpr'][['rax', 'rcx'].index(k)], 0)
                if got != v: p.append(f'{k} {got} != {v}')
            return p
        return check
    first = lambda code: CODE
    case('strict-in', 'mov al, 7\nin al, 0x10', stop('unsupported_port', lambda c: CODE + 2, status='unassigned', address='0x10', rax=7), ['--strict-unassigned'])
    case('strict-rep-outsd', 'mov edx, 0x3fe\nlea rsi, [rip]\nmov ecx, 3\nrep outsd',
         stop('unsupported_port', lambda c: CODE + len(c) - 2, status='unassigned', address='0x3fe', rcx=3), ['--strict-unassigned'])
    # Linux arch/x86/pci/direct.c pci_check_type1 / pci_check_type2 and early.c read_pci_config_16 (bus 0, slot 2).
    pci = """mov dx, 0xcfb
mov al, 1
out dx, al
mov dx, 0xcf8
in eax, dx
mov r8, rax
mov eax, 0x80000000
out dx, eax
in eax, dx
mov r9, rax
mov eax, r8d
out dx, eax
mov dx, 0xcfb
xor eax, eax
out dx, al
mov dx, 0xcf8
out dx, al
mov dx, 0xcfa
out dx, al
mov dx, 0xcf8
in al, dx
mov r10, rax
mov dx, 0xcfa
in al, dx
mov r11, rax
mov dx, 0xcf8
mov eax, 0x80001000
out dx, eax
mov dx, 0xcfc
xor eax, eax
in ax, dx
mov r12, rax
xor eax, eax
mov dx, 0xcfe
in ax, dx
mov r13, rax"""
    def pci_check(o, code):
        p = [] if o['stop'] == 'resident' and o['reason'].startswith('halted') else [f"stop {o['stop']} {o['reason']}"]
        want = {8: 0xffffffff, 9: 0xffffffff, 10: 0xff, 11: 0xff, 12: 0xffff, 13: 0xffff}
        p += [f'r{r} {o["gpr"][r]} != {v:#x}' for r, v in want.items() if int(o['gpr'][r], 0) != v]
        want = tally([(0xcfb, 1, 'w'), (0xcf8, 4, 'r'), (0xcf8, 4, 'w'), (0xcf8, 4, 'r'), (0xcf8, 4, 'w'), (0xcfb, 1, 'w'), (0xcf8, 1, 'w'),
                      (0xcfa, 1, 'w'), (0xcf8, 1, 'r'), (0xcfa, 1, 'r'), (0xcf8, 4, 'w'), (0xcfc, 2, 'r'), (0xcfe, 2, 'r')])
        if counts(o) != want: p.append(f'counts {counts(o)} != {want}')
        return p
    case('pci-probe', pci + '\nhlt', pci_check)
    def wrap(o, code):
        p = [] if o['stop'] == 'resident' and o['reason'].startswith('halted') else [f"stop {o['stop']} {o['reason']}"]
        if [int(o['gpr'][r], 0) for r in (8, 9, 10)] != [0xffff, 0xffffffff, 0xffffffff]: p.append(f"r8-r10 {o['gpr'][8:11]}")
        want = tally([(0xffff, 2, 'r'), (0xffff, 4, 'r'), (0xffff, 4, 'w'), (0xfffe, 4, 'r')])
        if counts(o) != want: p.append(f'counts {counts(o)} != {want}')
        return p
    case('wrap', 'mov dx, 0xffff\nin ax, dx\nmov r8, rax\nin eax, dx\nmov r9, rax\nout dx, eax\nmov dx, 0xfffe\nin eax, dx\nmov r10, rax\nhlt', wrap)
    # An access crossing the UART's last port: MSR, SCR, then all-ones from 400H (unassigned), byte by byte.
    def crossing(o, code):
        p = [] if o['stop'] == 'resident' and o['reason'].startswith('halted') else [f"stop {o['stop']} {o['reason']}"]
        if [int(o['gpr'][r], 0) for r in (8, 9)] != [0xffff_5ab0, 0xff5a]: p.append(f"r8, r9 {o['gpr'][8:10]}")
        return p
    case('crossing', 'mov dx, 0x3ff\nmov al, 0x5a\nout dx, al\nmov dx, 0x3fe\nin eax, dx\nmov r8, rax\nmov dx, 0x3ff\nxor eax, eax\nin ax, dx\nmov r9, rax\nhlt', crossing)
    # Admission (found by the kernel run): near branches with null segment prefixes, as Linux's -mindirect-branch-cs-prefix
    # code and its alternatives emit them: `2e e8 rel32`, `3e e8 rel32`, `2e ff d0` (cs call rax), `2e e9 rel32`.
    branches = '''xor r8d, r8d
.byte 0x2e
call 1f
.byte 0x3e
call 2f
lea rax, [rip + 3f]
.byte 0x2e
call rax
.byte 0x2e
jmp 4f
1: inc r8
ret
2: add r8, 0x10
ret
3: add r8, 0x100
ret
4: add r8, 0x1000
hlt'''
    def cs_branches(o, code):
        p = [] if o['stop'] == 'resident' and o['reason'].startswith('halted') else [f"stop {o['stop']} {o['reason']}"]
        return p + ([] if int(o['gpr'][8], 0) == 0x1111 else [f"r8 {o['gpr'][8]}"])
    case('cs-prefixed-branches', branches, cs_branches)
    def fault(vector, error=0, port=None):
        def check(o, code):
            p = []
            if o['stop'] != 'guest_fault' or [int(x, 0) for x in o['fault'][:1]] != [vector] or int(o['fault'][2], 0) != error:
                p.append(f"stop {o['stop']} {o.get('fault')}")
            if port is not None and port in counts(o): p.append(f'port {port:#x} accessed: {counts(o)}')
            return p
        return check
    case('lock-in', 'mov dx, 0x3ff\n.byte 0xf0, 0xec', fault(6))
    def refused(o, code):
        return [] if o['stop'] not in ('reached', 'guest_fault') and o['stop'] != 'driver' and 'ports' not in o else [f"stop {o['stop']} {o['reason']} {o.get('ports')}"]
    case('f3-in', 'mov dx, 0x3ff\n.byte 0xf3, 0xec', refused)
    def device(o, code):
        p = stop('unsupported_mmio', lambda c: CODE + len(c) - 2, status='system')(o, code)
        if 0x3ff in counts(o): p.append(f'port read: {counts(o)}')
        if sum(o['devices'][0]['reads']) + sum(o['devices'][0]['writes']): p.append('device touched')
        return p
    case('ins-device', 'mov dx, 0x3ff\nmov rdi, 0x210008\nmov ecx, 2\nrep insb', device, more='mmio 0x210000 0xfeb00000\n')
    # CPL 3 (switch at the NOP), IOPL 3: #AC for a misaligned INSW destination under CR0.AM + RFLAGS.AC; #PF for an unmapped one.
    head = 'mov rax, cr0\nbts rax, 18\nmov cr0, rax\nnop\n'
    hl = len(asm(head)) - 1
    for name, text, check, flags in (('ins-ac', 'mov dx, 0x3ff\nmov rdi, 0x200001\ninsw', fault(17, 0, 0x3ff), 0x43002),
                                     ('ins-pf', 'mov dx, 0x3ff\nmov rdi, 0x400000\ninsb', fault(14, 6, 0x3ff), 0x3002),
                                     ('cpl3-no-bitmap', 'mov dx, 0x3ff\nin al, dx', fault(13, 0, 0x3ff), 0x0002)):
        case(name, head + text, check, rflags=flags, more=f'switch {CODE + hl:#x} 3\n')
    return out


def cmd_cases(a, report):
    failures = []
    for name, text, check, extra, code in cases():
        for block in (None, 1):
            w = a.out / 'cases' / f'{name}-{block}'
            rc, out, err = driver(a.host, a.runtime, a.fexc, text, w, ([block] if block else []) + extra, timeout=120)
            if rc == 0:
                problems = check(out, code)
                detail = {k: out.get(k) for k in ('stop', 'reason', 'rip', 'fault', 'mmio', 'ports')}
            elif name == 'f3-in':
                problems, detail = [], {'driver': err.strip().splitlines()[-1] if err.strip() else rc}
            else:
                problems, detail = [f'driver {rc}: {err.strip()[-300:]}'], {}
            report['cases'].append({'name': name, 'block': block, 'detail': detail, 'problems': problems})
            failures += [f'{name} block={block}: {d}' for d in problems]
            print(f"{name} block={block}: {'ok' if not problems else problems} {detail.get('reason', detail.get('driver', ''))}", flush=True)
    return failures


# ---- unassigned pages (owner decision, October 29, and its amendment after L3) -------------------------------------
# A CPL 0 program on its own tables (identity space, CR3 = TABLES): 0-4 MiB in 2 MiB pages, then 4 KiB pages: linear
# 0x400000 onto BUF (RAM, 0xff bytes), and at WINDOW + 0x1000 k the guest-physical pages of PAGES[k]. Every RAM page is
# below 4 MiB and RAM's last page follows NATEMU_RAM (owner decision 7, November 2: the 4 MiB suite runs every runner).
TABLES, BUF, WINDOW, RAM_END = 0x300000, 0x3d0000, 0x500000, int(os.environ.get('NATEMU_RAM', '0x80000000'), 16)
PAGES = {'last-ram': RAM_END - 0x1000, 'above-ram': 0xC000_0000, 'above-4g': 0x80_0000_0000, 'hole': 0xE_0000,
         'hpet': 0xFED0_0000, 'ioapic': 0xFEC0_0000, 'xapic': 0xFEE0_0000}
VA = {k: WINDOW + 0x1000 * i for i, k in enumerate(PAGES)}


def page_tables():
    q = lambda *v: b''.join(x.to_bytes(8, 'little') for x in v)  # noqa: E731
    pt = bytearray(0x1000)
    pt[0:8] = q(BUF | 0x67)
    for k, gpa in PAGES.items():
        i = (VA[k] - 0x400000) >> 12
        pt[8 * i:8 * i + 8] = q(gpa | 0x67)
    return [(TABLES, q((TABLES + 0x1000) | 0x27)), (TABLES + 0x1000, q((TABLES + 0x2000) | 0x27)),
            (TABLES + 0x2000, q(0xe7, 0x200000 | 0xe7, (TABLES + 0x3000) | 0x27)), (TABLES + 0x3000, bytes(pt))]


def page_cases():
    """(name, description, extra driver arguments, check(out) -> problems)."""
    asm, out = ref.assemble, []
    memory = ''.join(f'memory {a:#x} {d.hex()}\n' for a, d in page_tables())
    memory += f'memory {BUF:#x} {"ff" * 64}\nmemory {RAM_END - 0x1000:#x} {bytes(0xff8).hex()}1122334455667788\n'
    def case(name, text, check, extra=(), ncpu=1):
        code = asm(f'mov rax, {TABLES:#x}\nmov cr3, rax\n' + text + '\nhlt')
        cpus = ''.join(f'cpu\nrip 0x100000\nrflags 0x2\ncpl 0\nsse 1\n{gprs(rsp=0x3e1800 - 0x1000 * v)}\n' for v in range(ncpu))
        out.append((name, f'identity\nbody 0x100000 {code.hex()}\nregion 0x3e0000 0x2000\n{memory}{cpus}', list(extra), check))
    def halted(**want):
        def check(o):
            c = o['cpus'][0]
            p = [] if c['stop'] == 'resident' and c['reason'].startswith('halted') else [f"stop {c['stop']} {c['reason']}"]
            for r, v in want.items():
                got = int(c['gpr'][['rax', 'rcx', 'rdx', 'rbx', 'rsp', 'rbp', 'rsi', 'rdi', 'r8', 'r9', 'r10', 'r11'].index(r)], 0)
                if got != v: p.append(f'{r} {got:#x} != {v:#x}')
            return p
        return check
    def counted(page, reads, writes, check):
        def both(o):
            got = {e['page']: (e['reads'], e['writes']) for e in o.get('unassigned_pages', [])}
            return check(o) + ([] if got == {f'{page:#x}': (reads, writes)} else [f'counts {got} != {page:#x}: {(reads, writes)}'])
        return both
    def stopped(status, counts=None):
        def check(o):
            c = o['cpus'][0]
            p = [] if (c['stop'], c['reason'].split()[0], c['mmio']['status']) == ('resident', 'unsupported_mmio', status) else [f"stop {c['stop']} {c['reason']} {c.get('mmio')}"]
            if counts is not None and {e['page']: (e['reads'], e['writes']) for e in o.get('unassigned_pages', [])} != counts: p.append(f"counts {o.get('unassigned_pages')}")
            return p
        return check
    # Every read form natively: CMP, LODS, REPE CMPSB against RAM's 0xff bytes, MOVUPS, a load-op ADD; one fill per page.
    reads = ('mov rbx, {va}\ncmp dword ptr [rbx + 8], -1\nsete r8b\nlea rsi, [rbx + 16]\nlodsq\nmov r9, rax\n'
             'lea rsi, [rbx + 0x20]\nmov rdi, {buf}\nmov ecx, 16\nrepe cmpsb\nsete r10b\nmov r11, rcx\n'
             'movups xmm0, xmmword ptr [rbx + 0x40]\nmovq rdx, xmm0\nmov eax, 1\nadd eax, dword ptr [rbx + 0xff0]')
    writes = 'mov rbx, {va}\nmov dword ptr [rbx + 8], eax\nmov byte ptr [rbx], 1\nmov qword ptr [rbx + 0x100], rbx\nmov rcx, qword ptr [rbx]'
    for k in ('hole', 'above-ram', 'above-4g', 'hpet'):
        gpa, va = PAGES[k], VA[k]
        case(f'{k}-reads', reads.format(va=va, buf=BUF), counted(gpa, 1, 0, halted(r8=1, r9=2**64 - 1, r10=1, r11=0, rdx=2**64 - 1, rax=0)))
        case(f'{k}-writes', writes.format(va=va), counted(gpa, 1, 3, halted(rcx=2**64 - 1)))
        case(f'{k}-rmw', f'mov rbx, {va}\nadd dword ptr [rbx + 8], 1', stopped('form', {}))
        case(f'{k}-xchg', f'mov rbx, {va}\nxchg dword ptr [rbx + 8], eax', stopped('atomic', {}))
        case(f'{k}-stos', f'mov rdi, {va}\nmov ecx, 2\nrep stosb', stopped('form', {}))
        case(f'{k}-strict-read', f'mov rbx, {va}\ncmp dword ptr [rbx + 8], -1', stopped('unassigned', {f'{gpa:#x}': (1, 0)}), ['--strict-unassigned'])
        case(f'{k}-strict-write', f'mov rbx, {va}\nmov dword ptr [rbx + 8], eax', stopped('unassigned', {f'{gpa:#x}': (0, 1)}), ['--strict-unassigned'])
    # A read crossing from RAM's last page into [R, 4 GiB): RAM's bytes, then all-ones.
    case('cross-ram-above', f'mov rax, qword ptr [{VA["above-ram"] - 4:#x}]', counted(PAGES['above-ram'], 1, 0, halted(rax=0xffff_ffff_8877_6655)))
    # Both vCPUs read the hole: each fills its own shadow (one fill each), every read all-ones.
    case('smp-hole', f'mov rbx, {VA["hole"]}\nmov ecx, 1000\n1: cmp qword ptr [rbx + 8], -1\njne 2f\ndec ecx\njnz 1b\n2:',
         lambda o: [f"cpu{v} {c['stop']} {c['reason']} rcx {c['gpr'][1]}" for v, c in enumerate(o['cpus']) if not c['reason'].startswith('halted') or int(c['gpr'][1], 0) != 0]
         + ([] if o.get('unassigned_pages') == [{'page': f'{PAGES["hole"]:#x}', 'reads': 2, 'writes': 0}] else [f"counts {o.get('unassigned_pages')}"]), ncpu=2)
    # The xAPIC page is never all-ones-shadowed: disabled, a MOV load is all-ones from the LAPIC (no fill counted) and a CMP
    # is refused as a device page's; re-enabled, the version register reads at once.
    off, on = 'mov ecx, 0x1b\nrdmsr\nand eax, 0xfffff7ff\nwrmsr\n', 'mov ecx, 0x1b\nrdmsr\nor eax, 0x800\nwrmsr\n'
    xa = VA['xapic']
    case('xapic-disabled', off + f'mov r8d, dword ptr [{xa + 0x30:#x}]\nmov r9, qword ptr [{xa + 0x30:#x}]\n' + on + f'mov r10d, dword ptr [{xa + 0x30:#x}]',
         lambda o: halted(r8=0xffff_ffff, r9=2**64 - 1, r10=0x50014)(o) + ([f"counted {o['unassigned_pages']}"] if o.get('unassigned_pages') else []))
    case('xapic-disabled-cmp', off + f'cmp dword ptr [{xa + 0x30:#x}], 0', stopped('form'))
    case('xapic-x2', 'mov ecx, 0x1b\nrdmsr\nor eax, 0xc00\nwrmsr\n' + f'mov r8d, dword ptr [{xa + 0x30:#x}]', halted(r8=0xffff_ffff))
    # L4a: the I/O APIC's page is a device's (registered before the run: the registry is fixed then), never all-ones-shadowed:
    # a MOV reads its register (offset 8 folds to nothing: 0), a CMP is refused as a device page's.
    io = VA['ioapic']
    case('ioapic-device', f'mov r8d, dword ptr [{io + 8:#x}]\ncmp dword ptr [{io + 8:#x}], 0', stopped('form'))
    # Shadow drops (the second review's cases): an all-ones shadow is a TLB entry like any other, and a page that becomes the
    # vCPU's APIC page must lose it.
    pt = lambda k: TABLES + 0x3000 + 8 * ((VA[k] - 0x400000) >> 12)  # noqa: E731
    hole, ram_last, ones = VA['hole'], RAM_END - 0x1000, 2**64 - 1
    case('write-after-fill', f'mov rbx, {hole}\nmov r8, [rbx + 8]\nmov qword ptr [rbx + 8], rbx\nmov r9, [rbx + 8]\n'
         'mov byte ptr [rbx + 0x10], 5\nmov r10, [rbx + 0x10]', counted(PAGES['hole'], 1, 2, halted(r8=ones, r9=ones, r10=ones)))
    for name, drop in (('remap-invlpg', 'invlpg [rbx]'), ('remap-cr3', 'mov rax, cr3\nmov cr3, rax')):
        case(name, f'mov rbx, {hole}\nmov r8, [rbx + 0xff8]\nmov rax, {ram_last | 0x67:#x}\nmov [{pt("hole"):#x}], rax\n{drop}\n'
             'mov r9, [rbx + 0xff8]', halted(r8=ones, r9=0x8877_6655_4433_2211))
    case('remap-to-hole', f'mov rbx, {VA["last-ram"]}\nmov r8, [rbx + 0xff8]\nmov rax, {PAGES["hole"] | 0x67:#x}\n'
         f'mov [{pt("last-ram"):#x}], rax\ninvlpg [rbx]\nmov r9, [rbx + 0xff8]', halted(r8=0x8877_6655_4433_2211, r9=ones))
    case('apic-move-onto-fill', f'mov rbx, {VA["above-ram"]}\nmov r8d, [rbx + 0x30]\nmov ecx, 0x1b\nrdmsr\nmov r11, rax\n'
         f'mov eax, 0xc0000900\nxor edx, edx\nwrmsr\nmov r9d, [rbx + 0x30]\nmov r10d, [{VA["xapic"] + 0x30:#x}]',
         halted(r8=0xffff_ffff, r9=0x50014, r10=0xffff_ffff))
    # Two vCPUs, two APIC bases: vCPU 1 moves its APIC to 0xC0000000, where vCPU 0 keeps reading all-ones (its own unassigned
    # page) while its APIC stays at 0xFEE00000, which vCPU 1 now reads all-ones.
    ab, xa = VA['above-ram'], VA['xapic']
    smp_text = (f'cmp rsp, 0x3e1800\nje 5f\nmov ecx, 0x1b\nrdmsr\nand eax, 0xfff\nor eax, 0xc0000000\nxor edx, edx\nwrmsr\n'
                f'mov r9d, [{ab + 0x30:#x}]\nmov r10d, [{xa + 0x30:#x}]\njmp 9f\n5:\nmov r10d, [{xa + 0x30:#x}]\nmov ecx, 100000\n'
                f'mov r8, -1\n6:\nmov eax, [{ab + 0x30:#x}]\nand r8, rax\ndec ecx\njnz 6b\nmov r9, r8\n9:')
    def two_bases(o):
        want = [(0xffff_ffff, 0x50014), (0x50014, 0xffff_ffff)]
        return [f"cpu{v} {c['reason'][:30]} r9 {c['gpr'][9]} r10 {c['gpr'][10]}" for v, (c, (r9, r10)) in enumerate(zip(o['cpus'], want))
                if not c['reason'].startswith('halted') or (int(c['gpr'][9], 0), int(c['gpr'][10], 0)) != (r9, r10)]
    case('smp-apic-bases', smp_text, two_bases, ncpu=2)
    return out


def cmd_pages(a, report):
    failures = []
    for name, text, extra, check in page_cases():
        w = a.out / 'pages' / name
        for attempt in range(2):
            rc, out, err = driver(a.host, a.runtime, a.fexc, text, w, extra, timeout=120)
            if rc == 0 or 'fae94007' not in err: break
        problems = check(out) if rc == 0 else [f'driver {rc}: {err.strip()[-300:]}']
        report['pages'].append({'name': name, 'problems': problems, 'counts': out and out.get('unassigned_pages')})
        failures += [f'{name}: {d}' for d in problems]
        print(f"{name}: {'ok' if not problems else problems}", flush=True)
    return failures


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--fexc', type=pathlib.Path, required=True)
    ap.add_argument('--out', type=pathlib.Path, required=True)
    ap.add_argument('--host'); ap.add_argument('--runtime')
    ap.add_argument('--only', type=lambda s: s.split(','), default=['qemu', 'stress', 'cases', 'pages'])
    ap.add_argument('--kicks', type=int, default=2000)
    a = ap.parse_args()
    if not (a.host and a.runtime): a.host, a.runtime = vm.build()
    a.out.mkdir(parents=True, exist_ok=True)
    report, failures = {'qemu': [], 'stress': [], 'cases': [], 'pages': []}, []
    if 'qemu' in a.only: failures += cmd_qemu(a, report)
    if 'stress' in a.only: failures += cmd_stress(a, report)
    if 'cases' in a.only: failures += cmd_cases(a, report)
    if 'pages' in a.only: failures += cmd_pages(a, report)
    report['failures'] = failures
    (a.out / 'l3.json').write_text(json.dumps(report, indent=1, default=str) + '\n')
    print(f'{len(failures)} failures')
    sys.exit(1 if failures else 0)


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""X4 checks: AP start through the ACPI multiprocessor wakeup mailbox, version 0 (boot-features plan, X4).

    tools/with-env python3 tools/run_x4.py --fexc FEXC --out DIR [--only units,adversarial,trampoline] [--cpus 2 3 4]

- `units` (natemu only; judged by `tools/models/mailbox_model.py`, written independently from ACPI 6.5 5.2.12.19): the BSP
  runs an OS script from a table (per step: APIC ID, vector, command; it writes the APIC ID, the vector, then the command,
  waits up to 100 ms for the command to read Noop, else writes Noop itself). APs 1..N-1 wait on the mailbox
  (`mailbox GPA`). Scripts: `turns` (each AP in turn), `reverse` (each in turn, last first), `ignored` (a Wakeup for an
  absent APIC ID, for the BSP's, and a reserved command for AP 1, then each AP in turn), `again` (each AP, then a second
  Wakeup for AP 1), `partial` (only the last AP; at N = 2 none). The acknowledgements the BSP saw and the vectors each AP started at must equal the model's `run`;
  the trace (OS writes, each acknowledgement, the BSP overwriting the vector right after it, each start) must pass
  `check_trace`. The overwrite is only a probe: it shows a vector read after the acknowledgement only when it lands
  between the two, which a mutation showed it practically never does; the read-before-acknowledgement order is an
  invariant of `events::wait_mailbox`, checked by inspection. Each started AP runs once at its vector with the ACPI wake
  state (`boot::ap_wake`: IF = 0, long mode and paging, EFER.NXE, the BSP's CR3 (the section's), the BSP's flat 64-bit
  CS, CPUID's APIC ID = its number); an AP never woken ends `halted` still waiting. Block sizes default and 1. The wake latency (the BSP's TSC just
  before its command store to the AP's acknowledgement, the guest TSC) is reported for every acknowledged step but each
  run's first (whose command store waits for its block's first translation). `halting`: the BSP writes a Wakeup for
  AP 1 and halts for good at once; AP 1 must still start (the host's end rule with a waiting AP), the others end halted.
- `adversarial` (the X4 review's cases; natemu only, both block sizes): `reversed` (the command before the APIC ID: a
  Wakeup for the BSP's ID 0, then AP 1's ID; the vector preset: AP 1 starts at its vector), `early` (the Wakeup for the
  last AP is in the mailbox before any vCPU runs), `alias` (every AP woken through a second linear page mapped onto the
  mailbox page), `noise` (AP 1, once woken, stores 5,000 times into the page's OS area while the BSP wakes the others),
  `nowait` (two Wakeups, AP 1's and AP 2's, with no wait for the first acknowledgement, an OS protocol violation: AP 2
  starts, AP 1 starts or keeps waiting, never at AP 2's vector, nothing hangs). Started APs run once at their vector and
  reach their end; the others end halted.
- `trampoline`: Linux 6.12's AP path after the wakeup, transcribed (`trampoline_start64`'s 64-bit branch: LIDT, LGDT, the
  LA57 check, the LOCK BTS trampoline lock with PAUSE, the real-mode stack, MOV SS/DS/ES/FS/GS, MOV CR3, LRETQ; then
  `secondary_startup_64`: `verify_cpu`, CR3, the CR4 PSE/PGE writes, the APIC ID from the xAPIC page, the CPU number
  lookup, the per-CPU stack, the lock release), run by every AP at N = 2..4: on natemu the BSP wakes them through the
  mailbox, on pinned QEMU (no mailbox responder) its stub starts them at the same vector in the equivalent 64-bit state;
  registers and memory compared (QEMU and natemu programs differ only in the BSP's wake loop; the APs' start RFLAGS.IOPL,
  3 on QEMU's stub and 0 in the firmware's wake state, which `verify_cpu`'s PUSHF leaves in memory, is masked there).

Writes DIR/x4.json; exit status 1 on any failure."""
import argparse, json, pathlib, statistics, sys
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools')); sys.path.insert(0, str(ROOT / 'tools/bench/smp'))
import smp, run_slice1_vm as vm  # noqa: E402
from programs import STACK, asm  # noqa: E402
from run_slice3_vm import driver, per_vcpu_checks, smp_description  # noqa: E402
from models import mailbox_model  # noqa: E402

# Below 4 MiB (owner decision 7, November 2: the 4 MiB suite runs every runner; the mailbox's address is the program's
# choice, nothing here depends on it), clear of the driver's identity-space stacks and logging pages (0x3e0000-0x3eefff).
MB = 0x300000                                  # the mailbox page
SLOT = lambda i: 0x310000 + 0x100 * i          # what AP i saw at its first instructions
R = 0x320000                                   # the BSP's results: ACK[k] at +0, T0[k] at +0x100, T1[k] at +0x200, its CR3 and CS at +0x700; table at +0x800
V = lambda i: 0x130000 + 0x1000 * i            # AP i's vector
VA = lambda i: 0x140000 + 0x1000 * i           # a second vector for AP i (never to be run)
BSP = 0x110000
POISON = 0xdead0000
TIMEOUT_NS = 100_000_000
NAMES = ('cmd', 'rip', 'count', 'rflags', 'cr0', 'cr3', 'cr4', 'efer', 'cs', 'ss', 'ds', 'apic', 'mb_cmd', 'tsc')


def tsc(reg='rax'):
    return f'    rdtsc\n    shl rdx, 32\n    or rax, rdx\n' + (f'    mov {reg}, rax\n' if reg != 'rax' else '')


def bsp_code(nsteps):
    return f"""    mov rax, cr3
    mov [{R + 0x700:#x}], rax
    mov ax, cs
    movzx eax, ax
    mov [{R + 0x708:#x}], rax
    xor r14, r14
.step: cmp r14, {nsteps}
    jae .done
    imul r13, r14, 24
    add r13, {R + 0x800:#x}
    mov eax, [r13]
    mov [{MB + 4:#x}], eax
    mov rax, [r13 + 8]
    mov [{MB + 8:#x}], rax
    mov r12, [r13 + 16]
{tsc('r8')}    mov [{R + 0x100:#x} + 8 * r14], r8
    mov [{MB:#x}], r12w
    add r8, {TIMEOUT_NS}
.w: cmp word [{MB:#x}], 0
    je .acked
    pause
{tsc()}    cmp rax, r8
    jb .w
    mov word [{MB:#x}], 0
    jmp .next
.acked: mov rax, {POISON:#x}
    mov [{MB + 8:#x}], rax
{tsc()}    mov [{R + 0x200:#x} + 8 * r14], rax
    mov qword [{R:#x} + 8 * r14], 1
.next: inc r14
    jmp .step
.done: cmp r11, r11"""


def ap_code(i):
    s = SLOT(i)
    seg = lambda r, o: f'    mov ax, {r}\n    movzx eax, ax\n    mov [{s + o:#x}], rax\n'
    return f"""    lea rax, [rel $]
    mov [{s + 8:#x}], rax
    lock inc qword [{s + 0x10:#x}]
    mov rsp, {s + 0x100:#x}
    pushfq
    pop rax
    mov [{s + 0x18:#x}], rax
    mov rax, cr0
    mov [{s + 0x20:#x}], rax
    mov rax, cr3
    mov [{s + 0x28:#x}], rax
    mov rax, cr4
    mov [{s + 0x30:#x}], rax
    mov ecx, 0xc0000080
    rdmsr
    shl rdx, 32
    or rax, rdx
    mov [{s + 0x38:#x}], rax
{seg('cs', 0x40)}{seg('ss', 0x48)}{seg('ds', 0x50)}    mov eax, 1
    cpuid
    shr ebx, 24
    mov [{s + 0x58:#x}], rbx
    movzx eax, word [{MB:#x}]
    mov [{s + 0x60:#x}], rax
{tsc()}    mov [{s + 0x68:#x}], rax
    cmp r11, r11"""


def scripts(n):
    each = [{'apic_id': i, 'vector': V(i), 'command': 1} for i in range(1, n)]
    return {'turns': each, 'reverse': each[::-1],
            'ignored': [{'apic_id': n, 'vector': V(1), 'command': 1}, {'apic_id': 0, 'vector': V(1), 'command': 1},
                        {'apic_id': 1, 'vector': V(1), 'command': 2}] + each,
            'again': each + [{'apic_id': 1, 'vector': VA(1), 'command': 1}],
            'partial': each[-1:] if n > 2 else []}


def halting_description(n):
    """The end rule's race: the BSP writes a Wakeup for AP 1 and halts at once (IF = 0: for good), without waiting."""
    bsp = f"""    mov dword [{MB + 4:#x}], 1
    mov rax, {V(1):#x}
    mov [{MB + 8:#x}], rax
    mov word [{MB:#x}], 1
    hlt
    cmp r11, r11"""
    return unit_description(n, []).replace(f'body {BSP:#x} {asm(bsp_code(0)).hex()}', f'body {BSP:#x} {asm(bsp).hex()}')


def unit_description(n, steps):
    table = b''.join(s['apic_id'].to_bytes(8, 'little') + s['vector'].to_bytes(8, 'little') + s['command'].to_bytes(8, 'little') for s in steps)
    lines = ['identity', f'body {BSP:#x} {asm(bsp_code(len(steps))).hex()}']
    lines += [f'body {V(i):#x} {asm(ap_code(i)).hex()}' for i in range(1, n)]
    lines += [f'body {VA(i):#x} {asm(ap_code(i)).hex()}' for i in range(1, n)]
    lines += [f'memory {MB:#x} {bytes(16).hex()}',
              f'memory {SLOT(0):#x} {bytes(0x100 * n).hex()}', f'memory {R:#x} {bytes(0x800).hex() + table.hex()}']
    for i in range(n):
        lines += ['cpu', f'rip {V(i) if i else BSP:#x}', 'rflags 0x2', 'cpl 0', 'if 0'] + ([f'mailbox {MB:#x}'] if i else [])
    return '\n'.join(lines) + '\n'


def judge_unit(n, steps, out):
    problems, cpus = [], out['cpus']
    mem = {smp.num(m['addr']): bytes.fromhex(m['data']) for m in cpus[0]['memory']}
    q = lambda base, off: int.from_bytes(mem[base][off:off + 8], 'little')
    want = mailbox_model.run(n, steps)
    acks = [q(R, 8 * k) == 1 for k in range(len(steps))]
    if acks != want['acks']:
        problems.append(f"acknowledgements {acks} != model {want['acks']}")
    slot = lambda i, o: int.from_bytes(mem[SLOT(0)][0x100 * i + o:0x100 * i + o + 8], 'little')
    bsp = cpus[0]
    if bsp['stop'] != 'reached':
        problems.append(f"bsp stopped {bsp['stop']} {bsp['reason']}")
    # The trace for the model's checker: the OS's writes, each acknowledgement and the poisoning write right after it, each start.
    events, latency = [], []
    for k, s in enumerate(steps):
        events += [('os_write', 'apic_id', s['apic_id']), ('os_write', 'vector', s['vector']), ('os_write', 'command', s['command'])]
        if acks[k]:
            i = s['apic_id']
            events += [('ap_ack', i), ('os_write', 'vector', POISON)]
            if slot(i, 0x10):
                events.append(('ap_start', i, slot(i, 8)))
            acked = cpus[i]['stats']['mailbox'][2]
            latency.append(acked - int.from_bytes(mem[R][0x100 + 8 * k:0x108 + 8 * k], 'little'))
        else:
            events.append(('os_write', 'command', 0))
    problems += ['trace: ' + v for v in mailbox_model.check_trace(n, events)]
    for i in range(1, n):
        c, st = cpus[i], cpus[i]['stats']['mailbox']
        if want['starts'][i]:
            vec = want['starts'][i][0]
            body_end = vec + len(asm(ap_code(i)))
            if c['stop'] != 'reached' or smp.num(c['rip']) != body_end:
                problems.append(f"cpu{i} stopped {c['stop']} {c['reason']} at {c['rip']} (end {body_end:#x})")
            got = {'count': slot(i, 0x10), 'rip': slot(i, 8), 'if': slot(i, 0x18) >> 9 & 1, 'cr0_pg_pe': slot(i, 0x20) & 0x80000001,
                   'cr3': slot(i, 0x28), 'pae': slot(i, 0x30) >> 5 & 1, 'efer': slot(i, 0x38) & 0xd00, 'cs': slot(i, 0x40),
                   'apic': slot(i, 0x58), 'mailbox': st[0]}
            exp = {'count': 1, 'rip': vec, 'if': 0, 'cr0_pg_pe': 0x80000001, 'cr3': q(R, 0x700), 'pae': 1, 'efer': 0xd00,
                   'cs': q(R, 0x708), 'apic': i, 'mailbox': 0}
            problems += [f'cpu{i} {k}: {got[k]:#x} != {exp[k]:#x}' for k in exp if got[k] != exp[k]]
            problems += [f'cpu{i} ' + d for d in per_vcpu_checks(c)]
        elif c['stop'] != 'resident' or 'halted' not in c['reason'] or st[0] != MB or slot(i, 0x10) != 0:
            problems.append(f"cpu{i} (never woken) stopped {c['stop']} {c['reason']}, mailbox {st}, ran {slot(i, 0x10)}")
    # The first acknowledged step's latency includes the BSP's first translation of the block holding its command store
    # (RDTSC ends a block): only the later ones are the wake's.
    return problems, latency[1:], [c['stats']['mailbox'][1] for c in cpus[1:]]


def cmd_units(a, report):
    failures = []
    for n in a.cpus:
        for name, steps in scripts(n).items():
            for block in ([], [1]):
                tag = f"{name}@{n}{'-1' if block else ''}"
                rc, out, err = driver(a.host, a.runtime, a.fexc, unit_description(n, steps), a.out / 'units' / tag, block, timeout=120)
                problems, latency, wakes = ([f'driver exit {rc}: {err.strip()[-400:]}'], [], []) if rc else judge_unit(n, steps, out)
                report.append({'name': tag, 'problems': problems, 'latency_ns': latency, 'wakes': wakes})
                failures += [f'{tag}: {p}' for p in problems]
                print(tag, 'ok' if not problems else problems[:3], 'latency_us', [round(x / 1000, 1) for x in latency], 'wakes', wakes, flush=True)
        # The end rule with a waiting AP: the BSP halts for good right after its Wakeup command; AP 1 must still start (a
        # waiting AP reads the end request before the mailbox), every other AP ends halted, still waiting.
        tag = f'halting@{n}'
        rc, out, err = driver(a.host, a.runtime, a.fexc, halting_description(n), a.out / 'units' / tag, [], timeout=120)
        problems = [f'driver exit {rc}: {err.strip()[-400:]}'] if rc else []
        if not rc:
            cpus = out['cpus']
            mem = {smp.num(m['addr']): bytes.fromhex(m['data']) for m in cpus[0]['memory']}
            ran = [int.from_bytes(mem[SLOT(0)][0x100 * i + 0x10:0x100 * i + 0x18], 'little') for i in range(n)]
            if cpus[0]['stop'] != 'resident' or 'halted' not in cpus[0]['reason']:
                problems.append(f"bsp stopped {cpus[0]['stop']} {cpus[0]['reason']}")
            if ran[1:] != [1] + [0] * (n - 2) or cpus[1]['stop'] != 'reached':
                problems.append(f"APs ran {ran[1:]}, AP 1 stopped {cpus[1]['stop']} {cpus[1]['reason']}")
            problems += [f"cpu{i} stopped {cpus[i]['stop']} {cpus[i]['reason']}" for i in range(2, n)
                         if cpus[i]['stop'] != 'resident' or 'halted' not in cpus[i]['reason'] or cpus[i]['stats']['mailbox'][0] != MB]
        report.append({'name': tag, 'problems': problems, 'latency_ns': [], 'wakes': []})
        failures += [f'{tag}: {p}' for p in problems]
        print(tag, 'ok' if not problems else problems[:3], flush=True)
    return failures


# ---- the review's adversarial cases ------------------------------------------------------------------------------------
ALIAS = 0x3f0000           # a second linear page onto the mailbox page (its PTE's address bits cleared: 0x3f0000 -> 0x300000)


def wait_ack(mb, k):
    """Waits a bounded time for the command to read Noop; records the acknowledgement at R + 8k."""
    return f"""    mov r8, 0
.w{k}: cmp word [{mb:#x}], 0
    je .a{k}
    pause
    inc r8
    cmp r8, 50000000
    jb .w{k}
    jmp .n{k}
.a{k}: mov qword [{R + 8 * k:#x}], 1
.n{k}:
"""


def wake(mb, i, k):
    return f"""    mov dword [{mb + 4:#x}], {i}
    mov rax, {V(i):#x}
    mov [{mb + 8:#x}], rax
    mov word [{mb:#x}], 1
""" + wait_ack(mb, k)


def adversarial_description(n, bsp, mailbox=bytes(16), extra=(), ap_bodies=None):
    d = unit_description(n, []).replace(f'body {BSP:#x} {asm(bsp_code(0)).hex()}', f'body {BSP:#x} {asm(bsp).hex()}')
    d = d.replace(f'memory {MB:#x} {bytes(16).hex()}', f'memory {MB:#x} {mailbox.hex()}')
    for i, code in (ap_bodies or {}).items():
        d = d.replace(f'body {V(i):#x} {asm(ap_code(i)).hex()}', f'body {V(i):#x} {asm(code).hex()}')
    lines = d.split('\n')
    at = lines.index('cpu')
    return '\n'.join(lines[:at] + list(extra) + lines[at:])


def adversarial_cases():
    """name -> (vCPU counts, n -> (description, the APs that must start; None: `nowait`'s either-or))."""
    noise_ap = f"""    lea rax, [rel $]
    mov [{SLOT(1) + 8:#x}], rax
    lock inc qword [{SLOT(1) + 0x10:#x}]
    mov ecx, 5000
.l: mov [{MB + 0x100:#x}], rcx
    dec ecx
    jnz .l
    cmp r11, r11"""
    return {
        'reversed': ((2, 3, 4), lambda n: (adversarial_description(n, f"""    mov word [{MB:#x}], 1
    mov dword [{MB + 4:#x}], 1
""" + wait_ack(MB, 0) + '    cmp r11, r11', mailbox=bytes(8) + V(1).to_bytes(8, 'little')), {1})),
        'early': ((2, 3, 4), lambda n: (adversarial_description(n, wait_ack(MB, 0) + '    cmp r11, r11',
            mailbox=(1).to_bytes(4, 'little') + (n - 1).to_bytes(4, 'little') + V(n - 1).to_bytes(8, 'little')), {n - 1})),
        'alias': ((2, 3, 4), lambda n: (adversarial_description(n, ''.join(wake(ALIAS, i, i - 1) for i in range(1, n)) + '    cmp r11, r11',
            extra=[f'region {ALIAS:#x} 0x1000', f'entry {ALIAS:#x} 1 clear 0xf0000']), set(range(1, n)))),
        'noise': ((2, 3, 4), lambda n: (adversarial_description(n, ''.join(wake(MB, i, i - 1) for i in range(1, n)) + '    cmp r11, r11',
            mailbox=bytes(0x200), ap_bodies={1: noise_ap}), set(range(1, n)))),
        'nowait': ((3, 4), lambda n: (adversarial_description(n, f"""    mov dword [{MB + 4:#x}], 1
    mov rax, {V(1):#x}
    mov [{MB + 8:#x}], rax
    mov word [{MB:#x}], 1
""" + wake(MB, 2, 0) + '    cmp r11, r11'), None)),
    }


def cmd_adversarial(a, report):
    failures = []
    for name, (counts, make) in adversarial_cases().items():
        for n in (c for c in counts if c in a.cpus):
            for block in ([], [1]):
                tag = f"{name}@{n}{'-1' if block else ''}"
                text, want = make(n)
                rc, out, err = driver(a.host, a.runtime, a.fexc, text, a.out / 'adversarial' / tag, block, timeout=120)
                problems = [f'driver exit {rc}: {err.strip()[-400:]}'] if rc else []
                if not rc:
                    cpus = out['cpus']
                    mem = {smp.num(m['addr']): bytes.fromhex(m['data']) for m in cpus[0]['memory']}
                    slot = lambda i, o: int.from_bytes(mem[SLOT(0)][0x100 * i + o:0x100 * i + o + 8], 'little')
                    started = {i for i in range(1, n) if slot(i, 0x10)}
                    # `nowait`'s violation leaves AP 1's command pending while the BSP writes AP 2's ID, then its
                    # vector: AP 2 can read the command and its own ID before the new vector and start at AP 1's
                    # (as firmware would). It then runs AP 1's body (slot 1) and fails however that code fails, while
                    # AP 1 keeps waiting. Only the BSP's end and "nothing hangs" (driver exit 0) remain required.
                    if want is None and started == {1} and cpus[1]['stop'] == 'resident' and 'halted' in cpus[1]['reason']:
                        problems += [f"cpu0 stopped {cpus[0]['stop']} {cpus[0]['reason']}"] if cpus[0]['stop'] != 'reached' else []
                        report.append({'name': tag, 'problems': problems, 'outcome': 'AP 2 took the pending wakeup'})
                        failures += [f'{tag}: {p}' for p in problems]
                        print(tag, 'ok (AP 2 took the pending wakeup)' if not problems else problems[:3], flush=True)
                        continue
                    if not (started == want or want is None and started in ({2}, {1, 2})):
                        problems.append(f'started {sorted(started)}, want {want}')
                    problems += [f'cpu{i} ran {slot(i, 0x10)} times at {slot(i, 8):#x}' for i in started if slot(i, 0x10) != 1 or slot(i, 8) != V(i)]
                    problems += [f"cpu{i} stopped {c['stop']} {c['reason']}" for i, c in enumerate(cpus)
                                 if (i == 0 or i in started) and c['stop'] != 'reached'
                                 or i and i not in started and (c['stop'] != 'resident' or 'halted' not in c['reason'])]
                report.append({'name': tag, 'problems': problems})
                failures += [f'{tag}: {p}' for p in problems]
                print(tag, 'ok' if not problems else problems[:3], flush=True)
    return failures


# ---- the trampoline transcription -----------------------------------------------------------------------------------
T = 0x150000               # trampoline_start64 (the vector); its data page at T + 0x1000
SS64 = T + 0x800           # secondary_startup_64 (one body with the trampoline: the AP's program ends at its end)
TD = T + 0x1000            # tr_idt (+0), tr_gdt64 (+0x10), tr_cr4 (+0x20), tr_start (+0x28), tr_lock (+0x30), GDT (+0x40)
RM = 0x170000              # the real-mode area (pa_real_mode_base); rm_stack_end = +0x800
KD = 0x180000              # kernel data: smpboot_control +0, x2apic_mode +8, apic_mmio_base +0x10, nr_cpu_ids +0x18,
                           # trampoline_lock +0x20, cpuid_to_apicid +0x40 (u32 each), __per_cpu_offset +0x80, tasks +0x100
PCPU = lambda i: 0x190000 + 0x1000 * i      # per-CPU area i: current task at +0 (pcpu_hot + X86_current_task)
TASK = lambda i: 0x1a0000 + 0x1000 * i      # task i: TASK_threadsp at +0x10
KSTACK = lambda i: 0x1b0000 + 0x2000 * i + 0x1ff0
OUT = lambda i: 0x1c0000 + 0x100 * i        # what AP i records
PT = 0x1e0000                               # identity tables (trampoline_pgd and init_top_pgt): PML4, PDPT, PD 0..1 GiB, PD 3..4 GiB
STARTUP_READ_APICID = 1 << 31


def tables():
    """PML4 -> PDPT -> two PDs: 0..1 GiB and 3..4 GiB in 2 MiB pages, write-back; the xAPIC's 2 MiB page PCD|PWT."""
    pml4, pdpt, pd0, pd3 = (PT + 0x1000 * k for k in range(4))
    m = bytearray(0x4000)
    put = lambda at, v: m.__setitem__(slice(at - PT, at - PT + 8), v.to_bytes(8, 'little'))
    put(pml4, pdpt | 3); put(pdpt, pd0 | 3); put(pdpt + 24, pd3 | 3)
    for k in range(4):           # 0..8 MiB (the programs' range)
        put(pd0 + 8 * k, k << 21 | 0x83)
    put(pd3 + 8 * 503, 0xfee00000 & ~0x1fffff | 0x9b)      # 0xfee00000: P, RW, PWT, PCD, PS
    return pml4, bytes(m)


def tramp_code():
    return f"""    lidt [{TD:#x}]
    lgdt [{TD + 0x10:#x}]
    mov rax, cr4
    xor eax, [{TD + 0x20:#x}]
    test eax, 0x1000
    jnz .switch_paging
.lock: lock bts dword [{TD + 0x30:#x}], 0
    jnc .got
    pause
    jmp .lock
.got: mov esp, 0x800
    mov dx, 0x18
    mov ss, edx
    add esp, {RM:#x}
    mov ds, edx
    mov es, edx
    mov fs, edx
    mov gs, edx
    mov eax, {PT:#x}
    mov cr3, rax
    push 0x10
    push qword [{TD + 0x28:#x}]
    o64 retf
.switch_paging: ud2"""


def ss64_code():
    """`secondary_startup_64` to the lock release, `verify_cpu` inlined as a call; then the AP records and clears."""
    return f"""    call verify_cpu
    xor r15d, r15d
    mov eax, {PT:#x}
    mov cr3, rax
    mov edx, 0x20 | 0x1000 | 0x40
    mov rcx, cr4
    and ecx, edx
    bts ecx, 4
    mov cr4, rcx
    bts ecx, 7
    mov cr4, rcx
    mov ecx, [{KD:#x}]
    test ecx, {STARTUP_READ_APICID:#x}
    jnz .read_apicid
    ud2
.read_apicid: mov ecx, 0x1b
    rdmsr
    test eax, 0x400
    jnz .msr
    cmp dword [{KD + 8:#x}], 0
    jz .mmio
.msr: ud2
.mmio: mov rcx, [{KD + 0x10:#x}]
    add rcx, 0x20
    mov eax, [rcx]
    shr eax, 24
    xor ecx, ecx
    lea rbx, [{KD + 0x40:#x}]
.find: cmp [rbx + rcx * 4], eax
    jz .setup_cpu
    inc ecx
    cmp ecx, [{KD + 0x18:#x}]
    jb .find
    ud2
.setup_cpu: mov rdx, [{KD + 0x80:#x} + rcx * 8]
    mov rax, [rdx]
    mov rsp, [rax + 0x10]
    mov r8, rcx
    shl r8, 8
    mov rax, [{RM + 0x7f0:#x}]
    mov [r8 + {OUT(0) + 0x60:#x}], rax
    mov rax, [{KD + 0x20:#x}]
    test rax, rax
    jz .setup_gdt
    mov dword [rax], 0
.setup_gdt: mov rbx, rcx
    shl rbx, 8
    add rbx, {OUT(0):#x}
    mov [rbx], rsp
    mov [rbx + 8], rcx
    mov [rbx + 0x10], rdx
    mov rax, cr3
    mov [rbx + 0x18], rax
    mov rax, cr4
    mov [rbx + 0x20], rax
    mov ax, ss
    mov [rbx + 0x28], ax
    mov ax, ds
    mov [rbx + 0x30], ax
    mov ax, gs
    mov [rbx + 0x38], ax
    sgdt [rbx + 0x40]
    sidt [rbx + 0x50]
    push 0x5a5a
    pop rax
    mov [rbx + 0x68], rsp
    mov rax, [rsp - 8]
    mov [rbx + 0x70], rax
    xor r8d, r8d
    mov eax, ecx
    shl eax, 8
    add eax, {smp.RESULT:#x}
    xor edx, edx
    mov ecx, 0xc0000101
    wrmsr
    lock inc dword [{KD + 0x28:#x}]
    xor eax, eax
    xor ebx, ebx
    xor ecx, ecx
    xor edx, edx
    xor esi, esi
    xor edi, edi
    cmp r11, r11
    jmp .end
verify_cpu: pushf
    push 0
    popf
    mov eax, 0
    cpuid
    cmp eax, 1
    jb .no_longmode
    xor di, di
    cmp ebx, 0x68747541
    jnz .noamd
    cmp edx, 0x69746e65
    jnz .noamd
    cmp ecx, 0x444d4163
    jnz .noamd
    mov di, 1
    jmp .check
.noamd: cmp ebx, 0x756e6547
    jnz .check
    cmp edx, 0x49656e69
    jnz .check
    cmp ecx, 0x6c65746e
    jnz .check
    mov eax, 1
    cpuid
    mov ecx, eax
    and eax, 0x0ff00f00
    shr eax, 8
    cmp eax, 6
    ja .clear_xd
    jb .check
    and ecx, 0x000f00f0
    shr ecx, 4
    cmp ecx, 0xd
    jb .check
.clear_xd: mov ecx, 0x1a0
    rdmsr
    btr edx, 2
    jnc .check
    wrmsr
.check: mov eax, 1
    cpuid
    and edx, 0x0700a169
    xor edx, 0x0700a169
    jnz .no_longmode
    mov eax, 0x80000000
    cpuid
    cmp eax, 0x80000001
    jb .no_longmode
    mov eax, 0x80000001
    cpuid
    and edx, 0x20100800
    xor edx, 0x20100800
    jnz .no_longmode
    mov eax, 1
    cpuid
    and edx, 0x06000000
    cmp edx, 0x06000000
    je .sse_ok
    ud2
.no_longmode: popf
    mov eax, 1
    ret
.sse_ok: popf
    xor eax, eax
    ret
.end:"""


def trampoline_program(n, natemu):
    pml4, pt = tables()
    gdt = [0, 0x00cf9b000000ffff, 0x00af9b000000ffff, 0x00cf93000000ffff]          # __KERNEL32_CS 8, __KERNEL_CS 0x10, __KERNEL_DS 0x18
    td = bytearray(0x80)
    td[0x10:0x1a] = (8 * len(gdt) - 1).to_bytes(2, 'little') + (TD + 0x40).to_bytes(8, 'little')
    td[0x20:0x24] = (0x20).to_bytes(4, 'little')                                     # tr_cr4: PAE (LA57 clear: the 64-bit branch)
    td[0x28:0x30] = SS64.to_bytes(8, 'little')
    td[0x40:0x60] = b''.join(g.to_bytes(8, 'little') for g in gdt)
    kd = bytearray(0x100)
    kd[0:4] = STARTUP_READ_APICID.to_bytes(4, 'little'); kd[0x10:0x18] = (0xfee00000).to_bytes(8, 'little')
    kd[0x18:0x1c] = n.to_bytes(4, 'little'); kd[0x20:0x28] = (TD + 0x30).to_bytes(8, 'little')
    for i in range(n):
        kd[0x40 + 4 * i:0x44 + 4 * i] = i.to_bytes(4, 'little'); kd[0x80 + 8 * i:0x88 + 8 * i] = PCPU(i).to_bytes(8, 'little')
    mem = [{'addr': TD, 'data': td.hex()}, {'addr': KD, 'data': kd.hex()}, {'addr': PT, 'data': pt.hex(), 'capture': False},
           {'addr': RM, 'data': bytes(0x800).hex()}, {'addr': OUT(0), 'data': bytes(0x100 * n).hex()}]
    mem += [{'addr': PCPU(i), 'data': TASK(i).to_bytes(8, 'little').hex(), 'capture': False} for i in range(n)]
    mem += [{'addr': TASK(i), 'data': (bytes(0x10) + KSTACK(i).to_bytes(8, 'little')).hex(), 'capture': False} for i in range(n)]
    mem += [{'addr': KSTACK(i) - 0x100, 'data': bytes(0x110).hex(), 'capture': False} for i in range(n)]      # (QEMU's capture pushes there)
    # The BSP waits for every AP's arrival (+0x28 in kernel data); on natemu it first wakes each through the mailbox.
    wake = ''.join(f"""    mov dword [{MB + 4:#x}], {i}
    mov rax, {T:#x}
    mov [{MB + 8:#x}], rax
    mov word [{MB:#x}], 1
.w{i}: pause
    cmp word [{MB:#x}], 0
    jne .w{i}
""" for i in range(1, n)) if natemu else ''
    bsp = wake + f""".all: pause
    cmp dword [{KD + 0x28:#x}], {n - 1}
    jne .all
    xor eax, eax
    cmp r11, r11"""
    if natemu:
        mem.append({'addr': MB, 'data': bytes(0x1000).hex(), 'capture': False})
    ap = asm(tramp_code() + f'\n    times {SS64 - T} - ($ - $$) db 0xcc\n' + ss64_code())
    bodies = [{'addr': BSP, 'code': asm(bsp).hex()}, {'addr': T, 'code': ap.hex()}]
    # RSP: the stub's capture pushes (the APs replace theirs in the trampoline).
    cpus = [{'rip': BSP if i == 0 else T, 'gpr': [0] * 4 + [STACK(i)] + [0] * 11, 'rflags': 0, 'cpl': 0} for i in range(n)]
    return {'format': 'natemu-smp-v1', 'name': f'x4-trampoline@{n}', 'ncpu': n, 'system': True, 'bodies': bodies, 'cpus': cpus,
            'memory': [{**m, 'addr': f"{m['addr']:#x}"} for m in mem]}


def body_end_of(prog, rip):
    return next(smp.num(b['addr']) + len(b['code']) // 2 for b in prog['bodies'] if smp.num(b['addr']) <= rip < smp.num(b['addr']) + len(b['code']) // 2)


def cmd_trampoline(a, report):
    from run_slice3_vm import qemu_reference
    failures = []
    for n in a.cpus:
        q, m = trampoline_program(n, False), trampoline_program(n, True)
        want = qemu_reference(q, a.out / 'qemu' / q['name'])
        # As QEMU's stub leaves its CPUs: IOPL = 3, IF = 0.
        text = smp_description(m).replace('\nrflags 0x0\n', '\nrflags 0x3000\nif 0\n')
        text = '\n'.join(line + (f'\nmailbox {MB:#x}' if line == f'rip {T:#x}' else '') for line in text.splitlines()) + '\n'
        for block in ([], [1]):
            tag = f"{m['name']}{'-1' if block else ''}"
            rc, out, err = driver(a.host, a.runtime, a.fexc, text, a.out / 'trampoline' / tag, block, timeout=120)
            problems = []
            if rc:
                problems = [f'driver exit {rc}: {err.strip()[-400:]}']
            else:
                cpus = out['cpus']
                # QEMU's stub ends a body by falling through into its capture; natemu stops at the end of the body: the
                # SS64 body's last instruction is `jmp .end`, the label at its end.
                for v, (c, cpu) in enumerate(zip(m['cpus'], cpus)):
                    end = body_end_of(m, BSP if v == 0 else T)
                    if cpu['stop'] != 'reached' or smp.num(cpu['rip']) != end:
                        problems.append(f"cpu{v} stopped {cpu['stop']} {cpu['reason']} at {cpu['rip']} (end {end:#x})")
                    problems += [f'cpu{v} ' + d for d in per_vcpu_checks(cpu)]
                memory = {smp.num(x['addr']): bytes.fromhex(x['data']) for x in cpus[0]['memory']}
                got = {'cpus': [{'gpr': [smp.num(x) for x in cpu['gpr']], 'rflags': smp.num(cpu['rflags'])} for cpu in cpus],
                       'memory': [{'addr': smp.num(r['addr']), 'data': memory[smp.num(r['addr'])]} for r in q['memory'] if r.get('capture', True)]}
                # The start states differ in RFLAGS.IOPL only (natemu's APs: the firmware's wake state, RFLAGS = 2; QEMU's
                # stub: IOPL = 3), which `verify_cpu`'s PUSHF leaves on the trampoline stack (RM + 0x7f0) and the APs then
                # copy (OUT(i) + 0x60): IOPL is cleared in both before comparing (RFLAGS registers: under FLAG_MASK).
                def no_iopl(state):
                    for r in state['memory']:
                        for at in [RM + 0x7f0] + [OUT(i) + 0x60 for i in range(n)]:
                            k = at - r['addr']
                            if 0 <= k <= len(r['data']) - 8:
                                v = int.from_bytes(r['data'][k:k + 8], 'little') & ~0x3000
                                r['data'] = r['data'][:k] + v.to_bytes(8, 'little') + r['data'][k + 8:]
                    return state
                # The BSP's registers differ only by its wake loop (rax, rdx: none written; RIP is the stop): all compared.
                problems += smp.compare(q, no_iopl(got), no_iopl({**want, 'memory': [dict(r) for r in want['memory']]}))
            report.append({'name': tag, 'problems': problems})
            failures += [f'{tag}: {p}' for p in problems]
            print(tag, 'ok' if not problems else problems[:4], flush=True)
    return failures


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--fexc', type=pathlib.Path, required=True)
    ap.add_argument('--out', type=pathlib.Path, required=True)
    ap.add_argument('--only', type=lambda s: s.split(','), default=['units', 'adversarial', 'trampoline'])
    ap.add_argument('--cpus', type=int, nargs='+', default=[2, 3, 4])
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    a.host, a.runtime = vm.build()
    report, failures = {}, []
    for mode, fn in (('units', cmd_units), ('adversarial', cmd_adversarial), ('trampoline', cmd_trampoline)):
        if mode in a.only:
            report[mode] = []
            failures += fn(a, report[mode])
    lat = [x for r in report.get('units', []) for x in r['latency_ns']]
    summary = {k: f'{sum(1 for r in v if not r["problems"])}/{len(v)} ok' for k, v in report.items()}
    if lat:
        summary['wake_latency_us'] = {'n': len(lat), 'min': min(lat) / 1000, 'median': statistics.median(lat) / 1000, 'max': max(lat) / 1000}
    (a.out / 'x4.json').write_text(json.dumps({'report': report, 'failures': failures, 'summary': summary}, indent=1) + '\n')
    for f in failures: print('FAIL', f)
    print(summary)
    sys.exit(1 if failures else 0)


if __name__ == '__main__': main()

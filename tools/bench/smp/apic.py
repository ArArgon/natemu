"""X3 local APIC programs (SDM Vol.3 ch. 11): one text per CPU, run unchanged by pinned QEMU and natemu.

Every program runs at CPL 0 with its own IDT (CS 0x08 interrupt gates at A(i)) and enables its local APIC in the mode it
names: 'x2' (x2APIC MSRs, IA32_APIC_BASE.EXTD set) or 'x' (xAPIC MMIO at 0xfee00000: the program first maps that page,
building its own tables at TABLES, identity 0..4 MiB in 2 MiB pages plus the APIC's 2 MiB page, PCD|PWT, and loads
CR3). Register accesses: `rd(m, reg)` (value in eax), `wr(m, reg, value)`, `send(m, low, dest)` (ICR), with register
numbers = xAPIC offset / 16 = x2APIC MSR - 0x800. QEMU's BSP starts in x2APIC mode when it has peers (the stub woke them
with x2APIC IPIs) and QEMU cannot re-enable an APIC once disabled, so xAPIC programs that QEMU runs have one CPU.

Every listed vector's handler runs the program's entry text (`bodies[-v]`), logs one entry (0x40 bytes at LOG(i)): vector, the frame's RIP, then PPR, ISR[127:64],
IRR[127:64] and TPR read in the handler, r12 (the main line's marker) and RDTSC; then counts the vector at
COUNTS(i) + 8 v, runs the program's handler body, ends the interrupt (EOI; not for NMI or #GP), and returns. The #GP
handler logs only the vector, RIP and r12 (no APIC access: the APIC may be what faulted) and resumes after the faulting
2-byte RDMSR/WRMSR.
"""
import pathlib, re, sys
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent.parent))
from programs import BODY0, STACK, cpu, ref_assemble  # noqa: E402

A = lambda i: 0x300000 + 0x10000 * i      # IDT page; +0x1000 IDTR, +0x1010 log cursor, +0x1018 entry count
LOG = lambda i: A(i) + 0x2000             # 0x4000 bytes: 256 entries
DATA = lambda i: A(i) + 0x6000            # results (0x1000)
COUNTS = lambda i: A(i) + 0x7000          # deliveries per vector (256 x 8)
RING = lambda i: A(i) + 0x8000            # natemu-only timing samples (0x8000)
SHARED = 0x3D0000                          # flags and counters shared by the CPUs (0x1000)
TABLES = 0x3C0000                          # PML4, PDPT, PD 0..1 GiB, PD 3..4 GiB
CODE = lambda i: BODY0 + 0x4000 * i
APIC = 0xfee00000
LOG_BYTES, RING_BYTES = 0x4000, 0x8000
NMI, GP = 2, 13
# Registers (xAPIC offset / 16).
ID, VER, TPR, APR, PPR, EOI, LDR, DFR, SVR, ISR, TMR, IRR, ESR = 0x2, 0x3, 0x8, 0x9, 0xa, 0xb, 0xd, 0xe, 0xf, 0x10, 0x18, 0x20, 0x28
ICR, ICR2, LVT_TIMER, LVT_THERMAL, LVT_PMC, LVT_LINT0, LVT_LINT1, LVT_ERROR = 0x30, 0x31, 0x32, 0x33, 0x34, 0x35, 0x36, 0x37
INIT_COUNT, CUR_COUNT, DCR, SELF_IPI = 0x38, 0x39, 0x3e, 0x3f


def rd(m, reg):
    return f'    mov ecx, {0x800 + reg:#x}\n    rdmsr' if m == 'x2' else f'    mov eax, [qword {APIC + 16 * reg:#x}]'


def wr(m, reg, value, high=0):
    """`value` an immediate or a 32-bit register (eax is clobbered for xAPIC; ecx, edx for x2APIC)."""
    if m == 'x2':
        return f'    mov ecx, {0x800 + reg:#x}\n    mov eax, {value}\n    mov edx, {high}\n    wrmsr'
    return (f'    mov eax, {value}\n' if value != 'eax' else '') + f'    mov [qword {APIC + 16 * reg:#x}], eax'


def send(m, low, dest=0):
    """An ICR write: x2APIC one 64-bit WRMSR; xAPIC ICR high (the 8-bit destination) then ICR low, which sends."""
    if m == 'x2':
        return wr(m, ICR, low, dest)
    return wr(m, ICR2, dest << 24) + '\n' + wr(m, ICR, low)


def self_ipi(m, v):
    return wr(m, SELF_IPI, v) if m == 'x2' else send(m, 0x40000 | v)


def eoi(m):
    return wr(m, EOI, 0)


MAP_APIC = f"""    mov rax, {TABLES + 0x1000 | 0x27:#x}
    mov [{TABLES:#x}], rax
    mov rax, {TABLES + 0x2000 | 0x27:#x}
    mov [{TABLES + 0x1000:#x}], rax
    mov rax, {TABLES + 0x3000 | 0x27:#x}
    mov [{TABLES + 0x1018:#x}], rax
    mov rax, 0xe7
    mov [{TABLES + 0x2000:#x}], rax
    mov rax, 0x2000e7
    mov [{TABLES + 0x2008:#x}], rax
    mov rax, {APIC | 0xfb:#x}
    mov [{TABLES + 0x3000 + 8 * ((APIC >> 21) & 511):#x}], rax
    mov rax, {TABLES:#x}
    mov cr3, rax
"""


def head(m, i):
    """IDT, IF = 0, the APIC page (xAPIC), the mode and software enable (SVR 0x1ff), TPR 0."""
    f = dict(idtr=A(i) + 0x1000)
    text = f"    lidt [{f['idtr']:#x}]\n    cli\n"
    if m == 'x':
        text += MAP_APIC
    else:
        text += '    mov ecx, 0x1b\n    rdmsr\n    or eax, 0xc00\n    wrmsr\n'
    return text + wr(m, SVR, 0x1ff) + '\n' + wr(m, TPR, 0) + '\n'


def local_labels(text):
    """GAS-style numeric labels (`1:`, `1b`, `1f`) as unique nasm labels."""
    lines = text.split('\n')
    defs = {}
    for k, line in enumerate(lines):
        if d := re.fullmatch(r'\s*(\d+):\s*', line):
            defs.setdefault(d.group(1), []).append(k)
    out = []
    for k, line in enumerate(lines):
        if d := re.fullmatch(r'\s*(\d+):\s*', line):
            out.append(f'xl_{d.group(1)}_{k}:')
            continue
        def ref(r, k=k):
            ks = defs[r.group(1)]
            return f'xl_{r.group(1)}_' + str(max(x for x in ks if x < k) if r.group(2) == 'b' else min(x for x in ks if x > k))
        out.append(re.sub(r'(?<![\w$])(\d+)([bf])\b', ref, line))
    return '\n'.join(out)


def handlers(m, i, vectors, bodies):
    cur, count = A(i) + 0x1010, A(i) + 0x1018
    out = [f"""ap_log:
    mov rbx, 0x40
    xadd [{cur:#x}], rbx
    inc qword [{count:#x}]
    cmp rbx, {LOG(i) + LOG_BYTES:#x}
    jae ap_full
    mov [rbx], r14
    mov [rbx+8], r15
{rd(m, PPR)}
    mov [rbx+16], rax
{rd(m, ISR + 2)}
    mov [rbx+24], eax
{rd(m, ISR + 3)}
    mov [rbx+28], eax
{rd(m, IRR + 2)}
    mov [rbx+32], eax
{rd(m, IRR + 3)}
    mov [rbx+36], eax
{rd(m, TPR)}
    mov [rbx+40], rax
    mov [rbx+48], r12
    rdtsc
    mov [rbx+56], eax
    mov [rbx+60], edx
ap_full:
    ret
ap_log0:
    mov rbx, 0x40
    xadd [{cur:#x}], rbx
    inc qword [{count:#x}]
    cmp rbx, {LOG(i) + LOG_BYTES:#x}
    jae ap_full
    mov [rbx], r14
    mov [rbx+8], r15
    mov [rbx+48], r12
    ret
"""]
    for v in vectors:
        error = v == GP
        out.append(f"""ap_v{v}:
    push rax
    push rcx
    push rdx
    push rbx
    push r14
    push r15
{bodies.get(-v, '')}
    mov r14, {v}
    mov r15, [rsp+{56 if error else 48}]
    call {'ap_log0' if error else 'ap_log'}
    inc qword [{COUNTS(i) + 8 * v:#x}]
{bodies.get(v, '')}
""" + (f'    add qword [rsp+56], 2\n' if error else '') + ('' if v in (NMI, GP) or v < 32 else eoi(m) + '\n') + (bodies[v + 0x100] + '\n' if v + 0x100 in bodies else '') + f"""    pop r15
    pop r14
    pop rbx
    pop rdx
    pop rcx
    pop rax
""" + ('    add rsp, 8\n' if error else '') + '    iretq\n')
    return '\n'.join(out)


def build(name, m, texts, vectors, bodies=None, memory=(), capture_ring=False):
    """A natemu-smp-v1 program: CPU i runs head + texts[i] + the handlers; `bodies` {vector: text} per CPU or shared
    (key -v: before the log entry; v: before the EOI; v + 0x100: after it)."""
    n = len(texts)
    bodies = bodies or {}
    progs, labels = {'name': f'{name}@{n}', 'ncpu': n, 'system': True, 'format': 'natemu-smp-v1', 'params': {},
                     'bodies': [], 'cpus': [], 'memory': []}, []
    vectors = sorted(set(vectors) | {GP})
    for i, t in enumerate(texts):
        b = bodies[i] if isinstance(bodies, list) else bodies
        fmt = dict(i=i, data=DATA(i), counts=COUNTS(i), ring=RING(i), shared=SHARED)
        b = {v: t2.format(**fmt) for v, t2 in b.items()}
        text = head(m, i) + t.format(**fmt) + '\n    jmp ap_end\n' + handlers(m, i, vectors, b) + 'ap_end:\n    cmp r11, r11\n'
        code, lab = ref_assemble(local_labels(text))
        assert len(code) <= 0x4000, f'{name}: CPU {i} code {len(code)} bytes'
        gates = b''
        for v in range(256):
            off = CODE(i) + lab[f'ap_v{v}'] if f'ap_v{v}' in lab else 0
            lo = off & 0xffff | 0x08 << 16 | (0x8e if off else 0) << 40 | (off >> 16 & 0xffff) << 48
            gates += lo.to_bytes(8, 'little') + (off >> 32).to_bytes(8, 'little')
        idtr = (256 * 16 - 1).to_bytes(2, 'little') + A(i).to_bytes(8, 'little')
        progs['bodies'].append({'addr': f'0x{CODE(i):x}', 'code': code.hex()})
        progs['cpus'].append(dict(cpu(i, CODE(i)), cpl=0))
        progs['memory'] += [{'addr': f'0x{A(i):x}', 'data': gates.hex(), 'capture': False},
                            {'addr': f'0x{A(i) + 0x1000:x}', 'data': (idtr + bytes(6) + LOG(i).to_bytes(8, 'little') + bytes(0x28)).hex(), 'capture': True},
                            {'addr': f'0x{LOG(i):x}', 'size': LOG_BYTES, 'fill': 'zero'},
                            {'addr': f'0x{DATA(i):x}', 'size': 0x1000, 'fill': 'zero'},
                            {'addr': f'0x{COUNTS(i):x}', 'size': 0x800, 'fill': 'zero'},
                            {'addr': f'0x{STACK(i) - 0x800:x}', 'size': 0x800, 'fill': 'zero', 'capture': False}]
        if capture_ring:
            progs['memory'].append({'addr': f'0x{RING(i):x}', 'size': RING_BYTES, 'fill': 'zero'})
        labels.append({k: CODE(i) + v for k, v in lab.items() if not k.startswith('xl_')})
    progs['memory'] += [{'addr': f'0x{SHARED:x}', 'size': 0x1000, 'fill': 'zero'}, {'addr': f'0x{TABLES:x}', 'size': 0x4000, 'fill': 'zero', 'capture': False}]
    progs['memory'] += list(memory)
    return progs, labels


def log(data):
    """[(vector, rip, ppr, isr[127:64], irr[127:64], tpr, r12, tsc)] up to the first empty entry."""
    out = []
    for k in range(0, len(data), 0x40):
        q = [int.from_bytes(data[k + 8 * j:k + 8 * j + 8], 'little') for j in range(8)]
        if not any(q): break
        out.append(tuple(q))
    return out


def regions(prog, memory):
    """{address: bytes} of a run's captured memory (natemu's first CPU report or QEMU's parsed dump)."""
    return {int(r['addr'], 0) if isinstance(r['addr'], str) else r['addr']: r['data'] for r in memory}


# ---- QEMU-compared programs -------------------------------------------------------------------------------------
def store(k, reg='rax'):
    return f'    mov [{{data}} + {8 * k}], {reg}'


def regs_program(m):
    """Register write/read-back sequences Linux's set-up uses (each read stored at DATA + 8 k)."""
    seq, k = [], 0
    def rw(reg, value=None, high=0):
        nonlocal k
        if value is not None:
            seq.append(wr(m, reg, value, high))
        seq.append(rd(m, reg) + ('\n    shl rdx, 32\n    or rax, rdx' if m == 'x2' and reg == ICR else ''))
        seq.append(store(k)); k += 1
    rw(ID); rw(VER)
    rw(TPR, 0x5a); rw(PPR); rw(TPR, 0)
    if m == 'x':
        rw(LDR, 0x12345678); rw(DFR, 0x0fffffff); rw(DFR, 0xffffffff); rw(LDR, 0x01000000)
    else:
        rw(LDR)
    rw(SVR, 0x13f); rw(SVR, 0x1ff)
    rw(LVT_TIMER, 0x20045); rw(LVT_TIMER, 0x10045)
    rw(LVT_THERMAL, 0x10233); rw(LVT_PMC, 0x10444)
    rw(LVT_LINT0, 0x1a755); rw(LVT_LINT1, 0x10400); rw(LVT_ERROR, 0x10066)
    if m == 'x':
        rw(ICR2, 0x03000000)
    rw(DCR, 0xb); rw(DCR, 0x9); rw(INIT_COUNT, 0); rw(CUR_COUNT)
    for r in (ISR + 1, ISR + 2, TMR + 2, IRR + 2):
        rw(r)
    seq.append(wr(m, ESR, 0)); rw(ESR)
    return build(f'apic-regs-{m}', m, ['\n'.join(seq)], [])


def prio_program(m):
    """Priority, TPR and EOI order with nested interrupts (11.8.3.1, 11.8.4): self IPIs pending with IF = 0, the TPR
    masking classes, interrupts taken by class after STI and as the TPR drops, a nested delivery inside a handler that
    re-enables interrupts, and the EOI that ends the highest in-service vector."""
    main = '\n'.join([
        '    mov r12, 1', self_ipi(m, 0x41), self_ipi(m, 0x51), self_ipi(m, 0x71), self_ipi(m, 0x61), wr(m, TPR, 0x50),
        '    mov r12, 2', '    sti', '    nop', '    mov r12, 3', wr(m, TPR, 0x40), '    mov r12, 4', wr(m, TPR, 0x00),
        '    mov r12, 5', self_ipi(m, 0x42), '    mov r12, 6', '    nop', '    cli',
        # Two in service at once (0x43 under 0x63 by nesting): each EOI ends the highest.
        self_ipi(m, 0x43), '    mov r12, 7', '    sti', '    nop', '    cli', '    mov r12, 8',
        rd(m, PPR), store(0), rd(m, ISR + 2), store(1)])
    bodies = {0x42: '\n'.join([self_ipi(m, 0x62), self_ipi(m, 0x52), '    sti', '    nop', '    cli']),
              0x43: '\n'.join([self_ipi(m, 0x63), '    sti', '    nop', '    cli', rd(m, ISR + 2), store(2)]),
              0x63: '\n'.join([rd(m, ISR + 2), store(3), eoi(m), rd(m, ISR + 2), store(4)])}
    return build(f'apic-prio-{m}', m, [main], [0x41, 0x42, 0x43, 0x51, 0x52, 0x61, 0x62, 0x63, 0x71], bodies)


def cr8_program():
    """CR8 as TPR[7:4] (11.8.6): MOV to CR8 masks a class and clears TPR[3:0]; lowering it takes the pending interrupt
    right after the MOV; the TPR register and CR8 read each other's writes."""
    m = 'x2'
    main = '\n'.join([
        '    mov r12, 1', self_ipi(m, 0x55), '    mov eax, 5', '    mov cr8, rax', '    sti', '    nop', '    mov r12, 2',
        rd(m, TPR), store(0), '    mov eax, 4', '    mov cr8, rax', '    mov r12, 3', '    nop', '    cli',
        wr(m, TPR, 0x7f), '    mov rax, cr8', store(1), rd(m, TPR), store(2), '    xor eax, eax', '    mov cr8, rax', rd(m, PPR), store(3)])
    return build('apic-cr8', m, [main], [0x55])


def faults_program():
    """x2APIC register rules (11.12.1.2-3, Table 11-6), each access with r12 = its case number; the #GP handler logs it
    (r12 in the entry) and resumes after the RDMSR/WRMSR. `FAULT_CASES`: (case, text, #GP per the SDM)."""
    seq = []
    for k, (text, _) in enumerate(FAULT_CASES):
        seq += [f'    mov r12, {k + 1}', text]
    return build('apic-faults', 'x2', ['\n'.join(seq)], [])


def _rd(index): return f'    mov ecx, {index:#x}\n    rdmsr'
def _wr(index, lo, hi=0): return f'    mov ecx, {index:#x}\n    mov eax, {lo:#x}\n    mov edx, {hi:#x}\n    wrmsr'
FAULT_CASES = [
    (_rd(0x80b), True), (_rd(0x83f), True), (_wr(0x802, 0), True), (_wr(0x803, 0), True), (_wr(0x80a, 0), True),
    (_wr(0x80d, 0), True), (_rd(0x80e), True), (_wr(0x80e, 0xffffffff), True), (_rd(0x809), True), (_rd(0x80c), True),
    (_rd(0x82f), True), (_rd(0x804), True), (_rd(0x8ff), True), (_rd(0x831), True),
    (_wr(0x80b, 1), True), (_wr(0x828, 1), True), (_wr(0x808, 0x100), True), (_wr(0x808, 0, 1), True),
    (_wr(0x83f, 0x140), True), (_wr(0x832, 0x50040), True), (_wr(0x83e, 0x4), True), (_wr(0x838, 0, 1), True),
    (_wr(0x810, 0), True), (_wr(0x839, 0), True), (_wr(0x80f, 0x11ff), True), (_wr(0x830, 0x1000 | 0x40), True),
    (_wr(0x835, 0x8000 | 0x10000, 1), True),
    (_rd(0x810), False), (_rd(0x839), False), (_wr(0x80b, 0), False), (_wr(0x828, 0), False), (_rd(0x828), False),
    (_wr(0x832, 0x11040), False), (_wr(0x835, 0x15000 | 0x10000), False),
    (_wr(0x6e0, 1), True),                                                  # IA32_TSC_DEADLINE: not advertised
    (_wr(0x1b, 0xfee00900), True),                                          # x2APIC -> xAPIC
    (_wr(0x1b, 0xfee00500), True),                                          # EXTD without EN
    (_wr(0x1b, 0xfee00100), False),                                         # x2APIC -> disabled
    (_wr(0x1b, 0xfee00d00), True),                                          # disabled -> x2APIC
    (_rd(0x802), True),                                                     # x2APIC MSRs need x2APIC mode
]


def ipi_program(n, k=20):
    """x2APIC IPIs at n CPUs: CPU 0 sends k fixed IPIs (0x50, physical) to each peer, waiting for each to be counted;
    k all-but-self (0x60); one NMI to each peer; k logical cluster IPIs (0x70) to all peers at once; then each peer sends
    k fixed IPIs (0x58 + peer) to CPU 0. Counts are exact (no IPI is sent while the previous one may be pending)."""
    m, ready, done = 'x2', SHARED, SHARED + 0x100
    c0 = [f'    mov r12, 1', f'1:', f'    pause'] + [f'    cmp qword [{ready + 8 * j:#x}], 1\n    jne 1b' for j in range(1, n)]
    for j in range(1, n):
        c0 += [f'    mov r8d, {k}', f'2:', send(m, 0x50, j), f'    mov r9, {k + 1}', '    sub r9, r8', '3:', '    pause',
               f'    cmp [{COUNTS(j) + 8 * 0x50:#x}], r9', '    jne 3b', '    dec r8d', '    jnz 2b']
    c0 += ['    mov r12, 2', f'    mov r8d, {k}', '4:', send(m, 0xc0000 | 0x60), f'    mov r9, {k + 1}', '    sub r9, r8', '5:', '    pause']
    c0 += [f'    cmp [{COUNTS(j) + 8 * 0x60:#x}], r9\n    jne 5b' for j in range(1, n)] + ['    dec r8d', '    jnz 4b']
    for j in range(1, n):
        c0 += ['    mov r12, 3', send(m, 0x400, j), '6:', '    pause', f'    cmp qword [{COUNTS(j) + 8 * NMI:#x}], 1', '    jne 6b']
    mask = sum(1 << j for j in range(1, n))
    c0 += ['    mov r12, 4', f'    mov r8d, {k}', '7:', send(m, 0x800 | 0x70, mask), f'    mov r9, {k + 1}', '    sub r9, r8', '8:', '    pause']
    c0 += [f'    cmp [{COUNTS(j) + 8 * 0x70:#x}], r9\n    jne 8b' for j in range(1, n)] + ['    dec r8d', '    jnz 7b']
    c0 += ['    mov r12, 5', f'    mov qword [{done:#x}], 1', '    sti', '9:', '    pause']
    c0 += [f'    cmp qword [{COUNTS(0) + 8 * (0x58 + j):#x}], {k}\n    jne 9b' for j in range(1, n)]
    c0 += ['    cli', f'    mov qword [{done + 8:#x}], 1']
    peers = []
    for j in range(1, n):
        peers.append('\n'.join(['    sti', f'    mov qword [{ready + 8 * j:#x}], 1', '1:', '    pause', f'    cmp qword [{done:#x}], 1', '    jne 1b',
                                f'    mov r8d, {k}', '2:', send(m, 0x58 + j, 0), f'    mov r9, {k + 1}', '    sub r9, r8', '3:', '    pause',
                                f'    cmp [{COUNTS(0) + 8 * (0x58 + j):#x}], r9', '    jne 3b', '    dec r8d', '    jnz 2b',
                                '4:', '    pause', f'    cmp qword [{done + 8:#x}], 1', '    jne 4b', '    cli']))
    return build('apic-ipi-x2', m, ['\n'.join(c0)] + peers, [NMI, 0x50, 0x60, 0x70] + [0x58 + j for j in range(1, n)])


def expected_counts(name, n, k=20):
    """{cpu: {vector: deliveries}} the IPI programs must end with."""
    if name.startswith('apic-ipi'):
        out = {0: {0x58 + j: k for j in range(1, n)}}
        for j in range(1, n):
            out[j] = {0x50: k, 0x60: k, NMI: 1, 0x70: k}
        return out
    raise KeyError(name)


# ---- natemu-only programs (judged by the SDM model) -----------------------------------------------------------
def ipi_x_program(n, k=20):
    """xAPIC IPIs through the MMIO page at n CPUs (Linux's path): LDR = 1 << (24 + i), flat DFR; CPU 0 sends k fixed
    physical IPIs (0x50) to each peer, k logical flat IPIs (0x70) to all peers, k all-including-self (0x60, itself
    included), one NMI per peer, and k lowest-priority logical IPIs (0x48) to all peers (natemu: the lowest-numbered
    member takes each); then the peers each send k to CPU 0 (0x58 + peer)."""
    m, ready, done = 'x', SHARED, SHARED + 0x100
    setup = '\n'.join([wr(m, DFR, 0xffffffff), wr(m, LDR, '0x{0:x}')])
    c0 = [setup.format(1 << 24), '    mov r12, 1', '1:', '    pause'] + [f'    cmp qword [{ready + 8 * j:#x}], 1\n    jne 1b' for j in range(1, n)]
    def wait(cpus, vector, extra=''):
        return [f'    mov r9, {k + 1}', '    sub r9, r8', '3:', '    pause'] + [f'    cmp [{COUNTS(j) + 8 * vector:#x}], r9\n    jne 3b' for j in cpus]
    for j in range(1, n):
        c0 += [f'    mov r8d, {k}', '2:', send(m, 0x50, j)] + wait([j], 0x50) + ['    dec r8d', '    jnz 2b']
    mask = sum(1 << j for j in range(1, n))
    c0 += ['    mov r12, 2', f'    mov r8d, {k}', '2:', send(m, 0x800 | 0x70, mask)] + wait(range(1, n), 0x70) + ['    dec r8d', '    jnz 2b']
    c0 += ['    mov r12, 3', '    sti', f'    mov r8d, {k}', '2:', send(m, 0x80000 | 0x60)] + wait(range(n), 0x60) + ['    dec r8d', '    jnz 2b', '    cli']
    for j in range(1, n):
        c0 += ['    mov r12, 4', send(m, 0x400, j), '6:', '    pause', f'    cmp qword [{COUNTS(j) + 8 * NMI:#x}], 1', '    jne 6b']
    c0 += ['    mov r12, 5', f'    mov r8d, {k}', '2:', send(m, 0x800 | 0x100 | 0x48, mask)] + wait([1], 0x48) + ['    dec r8d', '    jnz 2b']
    c0 += ['    mov r12, 6', f'    mov qword [{done:#x}], 1', '    sti', '9:', '    pause']
    c0 += [f'    cmp qword [{COUNTS(0) + 8 * (0x58 + j):#x}], {k}\n    jne 9b' for j in range(1, n)]
    c0 += ['    cli', f'    mov qword [{done + 8:#x}], 1']
    peers = []
    for j in range(1, n):
        peers.append('\n'.join([setup.format(1 << (24 + j)), '    sti', f'    mov qword [{ready + 8 * j:#x}], 1', '1:', '    pause', f'    cmp qword [{done:#x}], 1', '    jne 1b',
                                f'    mov r8d, {k}', '2:', send(m, 0x58 + j, 0), f'    mov r9, {k + 1}', '    sub r9, r8', '3:', '    pause',
                                f'    cmp [{COUNTS(0) + 8 * (0x58 + j):#x}], r9', '    jne 3b', '    dec r8d', '    jnz 2b',
                                '4:', '    pause', f'    cmp qword [{done + 8:#x}], 1', '    jne 4b', '    cli']))
    return build('apic-ipi-x', m, ['\n'.join(c0)] + peers, [NMI, 0x48, 0x50, 0x60, 0x70] + [0x58 + j for j in range(1, n)])


def expected_counts_x(n, k=20):
    out = {0: {0x60: k, **{0x58 + j: k for j in range(1, n)}}}
    for j in range(1, n):
        out[j] = {0x50: k, 0x60: k, 0x70: k, NMI: 1, **({0x48: k} if j == 1 else {})}
    return out


# Timer programs: the handler of the timer vector (0x40) records RDTSC in the ring; the main line samples
# (TSC before, current count, TSC after) into DATA. Judged by `timer_check` from the run's CNTFRQ.
TIMER_V = 0x40


def timer_body(i):
    """The timer handler's first work (before it logs: key -vector): append RDTSC to RING(i) (cursor at SHARED + 0x800 + 8 i)."""
    cur = SHARED + 0x800 + 8 * i
    return {-TIMER_V: f"""    mov rbx, [{cur:#x}]
    cmp rbx, {RING_BYTES - 8}
    jae 1f
    rdtsc
    shl rdx, 32
    or rax, rdx
    mov [{RING(i):#x} + rbx], rax
    add qword [{cur:#x}], 8
1:"""}


def sample(m, k):
    """(TSC before, current count, TSC after) at DATA + 0x100 + 24 k (rsi = k)."""
    return '\n'.join(['    rdtsc', '    shl rdx, 32', '    or rax, rdx', '    mov r10, rax', rd(m, CUR_COUNT), '    mov r11d, eax',
                      '    rdtsc', '    shl rdx, 32', '    or rax, rdx', f'    imul rdi, rsi, 24', f'    mov [{{data}} + 0x100 + rdi], r10',
                      f'    mov [{{data}} + 0x108 + rdi], r11', f'    mov [{{data}} + 0x110 + rdi], rax'])


def start_timer(m, lvt, dcr, count):
    """DCR, the LVT timer, then the initial count between two RDTSCs (DATA + 0, + 8)."""
    return '\n'.join([wr(m, DCR, dcr), wr(m, LVT_TIMER, lvt), '    rdtsc', '    shl rdx, 32', '    or rax, rdx', '    mov [{data}], rax',
                      wr(m, INIT_COUNT, count), '    rdtsc', '    shl rdx, 32', '    or rax, rdx', '    mov [{data} + 8], rax'])


TIMERS = {
    # name: (LVT timer, DCR, initial count, samples, TSC ticks between samples, deliveries waited for, by HLT)
    'oneshot': (0x40, 0x0, 24000, 120, 25000, 1, False),        # divide by 2: 2 ms
    'periodic': (0x20040, 0xb, 2400, 100, 3000, 100, False),    # divide by 1: 100 us
    'periodic-div16': (0x20040, 0x3, 600, 100, 6000, 50, False),  # 400 us
    'hlt-oneshot': (0x40, 0xb, 48000, 0, 0, 1, True),           # 2 ms, woken from HLT
    'hlt-periodic': (0x20040, 0xb, 4800, 0, 0, 50, True),       # 200 us, 50 HLTs
    'masked': (0x10040, 0xb, 24000, 120, 12500, 0, False),      # 1 ms, masked: counts to 0, no delivery
}


def timer_program(name, m, rounds=2, count=None):
    """`rounds` runs of the same text; the last one's results stay (a different `count` re-times the timer)."""
    lvt, dcr, count0, samples, spacing, deliveries, halt = TIMERS[name]
    count = count0 if count is None else count
    cur = SHARED + 0x800
    # Two rounds (r13): the first translates every path the second takes (a first translation costs host time, which
    # would count as lateness); the second's samples and deliveries are the results.
    body = [f'    mov r13d, {rounds}', '6:', f'    mov qword [{cur:#x}], 0', f'    mov qword [{COUNTS(0) + 8 * TIMER_V:#x}], 0', start_timer(m, lvt, dcr, count), '    sti']
    if samples:
        # Samples spread over the timer's first period (and past it for one-shot and masked), `spacing` TSC ticks apart.
        body += ['    xor esi, esi', '1:', sample(m, 0), f'    lea rbx, [rax + {spacing}]', '2:', '    pause', '    rdtsc', '    shl rdx, 32',
                 '    or rax, rdx', '    cmp rax, rbx', '    jb 2b', '    inc esi', f'    cmp esi, {samples}', '    jb 1b']
    if deliveries:
        if halt:
            # Halt until the deliveries are in (STI's shadow makes `sti; hlt` wait for one taken after the check).
            body += ['3:', '    cli', f'    cmp qword [{COUNTS(0) + 8 * TIMER_V:#x}], {deliveries}', '    jae 4f', '    sti', '    hlt', '    jmp 3b', '4:']
        else:
            body += ['3:', '    pause', f'    cmp qword [{COUNTS(0) + 8 * TIMER_V:#x}], {deliveries}', '    jb 3b']
    body += ['    cli']
    if lvt & 0x20000:
        body += [wr(m, LVT_TIMER, lvt | 0x10000)]  # periodic: stop the interrupts (masked; the count runs on)
    body += [rd(m, CUR_COUNT), '    mov [{data} + 0x10], rax', '    rdtsc', '    shl rdx, 32', '    or rax, rdx', '    mov [{data} + 0x18], rax',
             wr(m, INIT_COUNT, 0), '    sti', '    nop', '    cli', '    dec r13d', '    jnz 6b']  # a pending expiry is taken before the next round
    text = '\n'.join(body)
    prog, labels = build(f'apic-timer-{name}-{m}', m, [text], [TIMER_V], [timer_body(0)], capture_ring=True)
    prog['params'] = {'timer': name, 'lvt': lvt, 'dcr': dcr, 'count': count, 'samples': samples, 'deliveries': deliveries, 'hlt': halt}
    return prog, labels


def stress_program(n, m, iterations=2000, period=1200):
    """Random IPIs and timers at n CPUs: CPU s sends `iterations` IPIs, each to a random peer t with its own vector
    V(s, t) = 0x50 + 0x10 s + t (classes 5..8 by sender), each once the previous one of that flow was counted (so none
    may merge: counts are exact); every CPU
    runs a periodic LAPIC timer (vector 0x41, `period` crystal ticks); handlers of IPI vectors re-enable interrupts for a
    moment, so higher classes nest. Every handler checks at entry that its vector is the highest in service and the only
    one of its class or above (11.8.4), counting violations at DATA + 0x800. Each CPU ends when every CPU has sent its
    IPIs and every flow is complete. RECV(t, s) at SHARED + 0x200 + 8 (4 t + s), SENT(s, t) at SHARED + 0x300 + ..."""
    done = SHARED + 0x100
    recv = lambda t, s: SHARED + 0x200 + 8 * (4 * t + s)
    sent = lambda s, t: SHARED + 0x300 + 8 * (4 * s + t)
    texts, bodies = [], []
    for s in range(n):
        lines = [start_timer(m, 0x20041, 0xb, period), '    sti', f'    mov r8d, {iterations}', f'    mov r9, {0x9e3779b97f4a7c15 + s:#x}', 'loop:',
                 '    mov rax, 6364136223846793005', '    imul r9, rax', '    mov rax, 1442695040888963407', '    add r9, rax', '    mov rax, r9', '    shr rax, 40',
                 '    xor edx, edx', f'    mov ecx, {n - 1}', '    div rcx', f'    lea r10, [rdx + 1 + {s}]', f'    cmp r10, {n}', '    jb 1f', f'    sub r10, {n}', '1:']
        lines[lines.index('loop:')] = 'st_loop:'
        # Flow control: send V(s, t) once RECV(t, s) == SENT(s, t) (waiting with interrupts enabled: every CPU keeps
        # taking its own IPIs meanwhile, so no wait is circular).
        lines += ['    mov r11, r10', '    shl r11, 5', 'st_flow:', f'    mov rax, [{SHARED + 0x200 + 8 * s:#x} + r11]', f'    cmp rax, [{SHARED + 0x300 + 32 * s:#x} + r10 * 8]',
                  '    je st_send', '    pause', '    jmp st_flow', 'st_send:', f'    inc qword [{SHARED + 0x300 + 32 * s:#x} + r10 * 8]']
        if m == 'x2':
            lines += ['    mov ecx, 0x830', f'    mov eax, {0x50 + 0x10 * s:#x}', '    add eax, r10d', '    mov edx, r10d', '    wrmsr']
        else:
            lines += ['    mov eax, r10d', '    shl eax, 24', f'    mov [qword {APIC + 0x310:#x}], eax', f'    mov eax, {0x50 + 0x10 * s:#x}', '    add eax, r10d',
                      f'    mov [qword {APIC + 0x300:#x}], eax']
        lines += ['    dec r8d', '    jnz st_loop', f'    lock inc qword [{done:#x}]', 'st_wait:', '    pause']
        # Done when every CPU finished sending and every flow (any sender, any target) is complete.
        lines += [f'    cmp qword [{done:#x}], {n}', '    jne st_wait']
        lines += [f'    mov rax, [{recv(t, u):#x}]\n    cmp rax, [{sent(u, t):#x}]\n    jne st_wait' for t in range(n) for u in range(n) if t != u]
        lines += ['    cli', wr(m, LVT_TIMER, 0x30041), '    rdtsc', '    shl rdx, 32', '    or rax, rdx', '    mov [{data} + 0x18], rax']
        texts.append('\n'.join(lines))
        b = {0x41: priority_check(m, s, 0x41)}
        for u in range(n):
            if u != s:
                v = 0x50 + 0x10 * u + s
                b[v] = priority_check(m, s, v) + f'\n    inc qword [{recv(s, u):#x}]\n    sti\n    pause\n    pause\n    cli'
        bodies.append(b)
    vectors = sorted({0x41} | {0x50 + 0x10 * u + s for s in range(n) for u in range(n) if u != s})
    prog, labels = build(f'apic-stress-{m}', m, texts, vectors, bodies)
    prog['params'] = {'stress': True, 'iterations': iterations, 'period': period}
    return prog, labels


def priority_check(m, i, v):
    """At a handler's entry: ISR[255:32] read; violation unless the highest set bit is `v` and no other bit of v's class
    or above is set (11.8.4: only a higher class preempts)."""
    lines = []
    for r in range(1, 8):
        lines += [rd(m, ISR + r), f'    mov [{DATA(i) + 0x900 + 4 * r:#x}], eax']
    w, b = v // 32, v % 32
    lines += [f'    mov eax, [{DATA(i) + 0x900 + 4 * w:#x}]', f'    btr eax, {b}', f'    jnc 8f',
              f'    and eax, {(~((1 << ((v & 0xf0) % 32)) - 1)) & 0xffffffff:#x}' if (v & 0xf0) // 32 == w else '    xor eax, eax',
              '    jnz 8f']
    lines += [f'    cmp dword [{DATA(i) + 0x900 + 4 * r:#x}], 0\n    jne 8f' for r in range(w + 1, 8)]
    lines += ['    jmp 9f', '8:', f'    inc qword [{DATA(i) + 0x800:#x}]', '9:']
    return '\n'.join(lines)


def pingpong_program(n, k=3000):
    """HLT across peers (the driver's rule for ending halted vCPUs): an IPI (0x50) runs round a ring of n CPUs k times;
    each CPU halts (`cli; check; sti; hlt`) until its next receipt, then sends to the next CPU. At almost every moment one
    IPI is in flight to a CPU that is still parked while the sender has halted, so a driver that ends vCPUs when it merely
    sees all of them halted ends one that a peer is about to wake. Every CPU must reach its end with k receipts."""
    m, ready = 'x2', SHARED
    texts = []
    for i in range(n):
        lines = [f'    mov qword [{ready + 8 * i:#x}], 1', '1:', '    pause'] + [f'    cmp qword [{ready + 8 * j:#x}], 1\n    jne 1b' for j in range(n)]
        lines += ['    mov r8d, 1', '5:']
        wait = ['3:', '    cli', f'    cmp [{COUNTS(i) + 8 * 0x50:#x}], r8', '    jae 4f', '    sti', '    hlt', '    jmp 3b', '4:']
        sendnext = [send(m, 0x50, (i + 1) % n)]
        lines += (sendnext + wait) if i == 0 else (wait + sendnext)
        lines += ['    inc r8d', f'    cmp r8d, {k}', '    jbe 5b', '    cli']
        texts.append('\n'.join(lines))
    prog, labels = build('apic-pingpong', m, texts, [0x50])
    prog['params'] = {'rounds': k}
    return prog, labels

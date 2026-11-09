"""L3 port I/O programs (SDM Vol.2 IN/OUT/INS/OUTS, Vol.1 19; the 16550A of QEMU's hw/char/serial.c): one source per
program, run unchanged by pinned QEMU (system mode, `-serial file:..`: COM1 at 3F8H) and natemu (identity space, COM1).

Programs use X2's event harness (`events.build`: their own IDT, every exception logged as (vector, error, RIP, RFLAGS &
0x10ed5, r12, RCX) and resumed at r13). Each IN result is stored as a quadword at the data cursor (rbx, from DATA(i),
captured), with RAX preloaded with PAT first, so the merge of a narrow IN is compared too.
"""
import pathlib, sys
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent.parent))
import events  # noqa: E402
from programs import STACK  # noqa: E402

# #PF (14) is logged and resumed like the vectors events.py stubs.
events.STUB_VECTORS = tuple(sorted(set(events.STUB_VECTORS) | {14}))
DATA = events.DATA
LOG = events.LOG
PAT = 0x1122_3344_5566_7788
REG = {1: 'al', 2: 'ax', 4: 'eax'}
SRC = 0x2EF000              # string sources (QEMU and natemu: a memory item)
NP = 0x2F0000               # not present under `TABLES`
TABLES = 0x3C0000           # own tables: 0-2 MiB one large page, 2-4 MiB 4 KiB pages without NP
GDT, TSS = 0x3D0000, 0x3D1000
COM1 = 0x3F8
# Ports QEMU's pc machine leaves unassigned (its `info mtree -f`, I/O space, before and after SeaBIOS): imm8 and DX ones.
UNASSIGNED_IMM = (0x10, 0x30, 0x50, 0x90, 0xa8, 0xec)
UNASSIGNED_DX = (0x100, 0x2f8, 0x3e8, 0x1234, 0x8000)


class Text:
    def __init__(self):
        self.lines, self.n = [], 0

    def __iadd__(self, s):
        self.lines.append(s.rstrip('\n'))
        return self

    def label(self):
        self.n += 1
        return f'l3_{self.n}'

    def rd(self, port, size, imm=False):
        src = f'{port:#x}' if imm else 'dx'
        self += f'    mov rax, {PAT:#x}\n' + ('' if imm else f'    mov edx, {port:#x}\n') + f'    in {REG[size]}, {src}\n    mov [rbx], rax\n    add rbx, 8'

    def wr(self, port, size, value, imm=False):
        dst = f'{port:#x}' if imm else 'dx'
        self += ('' if imm else f'    mov edx, {port:#x}\n') + f'    mov eax, {value:#x}\n    out {dst}, {REG[size]}'

    def store(self, reg):
        self += f'    mov [rbx], {reg}\n    add rbx, 8'

    def text(self):
        return '\n'.join(self.lines)


def build(name, t, memory=lambda i: [], rflags_regs=None, **kw):
    extra = lambda i: [{'addr': f'0x{DATA(i):x}', 'size': 0x1000, 'fill': 'zero'}] + memory(i)
    return events.build(name, f'    mov rbx, {{data}}\n' + t.text(), memory_extra=extra, regs=rflags_regs, **kw)


# ---- the UART: every width, the 8250 driver's probe, earlyprintk, loopback with FIFO ---------------------------------
# Linux v6.12 drivers/tty/serial/8250/8250_port.c `autoconfig` (scratch, loopback, FIFO detection), `autoconfig_16550a`
# (EFR checks, NS SuperIO, TI16750, UUE), `serial8250_clear_fifos`; arch/x86/kernel/early_printk.c `early_serial_init`.
AUTOCONFIG = [('r', 1), ('w', 1, 0), ('w80',), ('r', 1), ('w', 1, 0x0f), ('w80',), ('r', 1), ('w', 1, 0),
              ('r', 4), ('w', 4, 0x1a), ('r', 6), ('w', 4, 0x08),
              ('r', 3), ('w', 3, 0xbf), ('w', 2, 0), ('w', 3, 0), ('w', 2, 1), ('r', 2),
              ('w', 3, 0x80), ('r', 2), ('w', 3, 0xbf), ('r', 2),
              ('w', 3, 0), ('r', 4), ('w', 3, 0xe0), ('r', 2), ('w', 3, 0), ('w', 4, 0x18), ('w', 3, 0xe0), ('r', 2), ('w', 3, 0), ('w', 4, 0x08),
              ('w', 3, 0), ('w', 2, 0x21), ('r', 2), ('w', 2, 1), ('w', 3, 0x80), ('w', 2, 0x21), ('r', 2), ('w', 2, 1), ('w', 3, 0),
              ('r', 1), ('w', 1, 0), ('r', 1), ('w', 1, 0x40), ('r', 1), ('w', 1, 0),
              ('w', 3, 0x03), ('w', 2, 1), ('w', 2, 7), ('w', 2, 0), ('r', 0), ('r', 5), ('r', 2), ('r', 6),
              # earlyprintk=serial,115200
              ('w', 3, 3), ('w', 1, 0), ('w', 2, 0), ('w', 4, 3), ('r', 3), ('w', 3, 0x83), ('w', 0, 1), ('w', 1, 0), ('w', 3, 3)]


def normalize(t):
    """A known UART state first (QEMU's SeaBIOS has used COM1): divisor 0x0c, FIFO off, LCR 0, IER 0, MCR OUT2, SCR 0,
    the reset values (LSR and MSR are at theirs)."""
    for off, v in ((3, 0x80), (0, 0x0c), (1, 0), (3, 0), (2, 0), (1, 0), (4, 0x08), (7, 0)):
        t.wr(COM1 + off, 1, v)


def putc(t, ch):
    loop = t.label()
    t += f'{loop}:\n    mov edx, {COM1 + 5:#x}\n    in al, dx\n    and al, 0x60\n    cmp al, 0x60\n    jne {loop}\n    mov edx, {COM1:#x}\n    mov al, {ord(ch)}\n    out dx, al'


def wait_tx(t):
    """Compare stable UART state on both models; preserve the instruction-form register/flag oracle."""
    loop = t.label()
    t += f'    pushfq\n    push rax\n    push rdx\n{loop}:\n    mov edx, {COM1 + 5:#x}\n    in al, dx\n    and al, 0x60\n    cmp al, 0x60\n    jne {loop}\n    pop rdx\n    pop rax\n    popfq'


def uart_program():
    t = Text()
    normalize(t)
    for k in range(8):
        t.rd(COM1 + k, 1)
    t.wr(COM1 + 7, 1, 0xa5); t.rd(COM1 + 7, 1)
    t.wr(COM1 + 6, 2, 0x5a00); t.rd(COM1 + 4, 4); t.rd(COM1 + 6, 2)
    t.wr(COM1 + 4, 4, 0x7700000b); t.rd(COM1 + 4, 4); t.rd(COM1 + 3, 2)
    t.wr(COM1 + 3, 1, 0x83); t.rd(COM1, 4); t.wr(COM1, 2, 0x0201); t.rd(COM1, 2); t.rd(COM1, 1); t.rd(COM1 + 1, 1)
    # (No access crossing 3FFH: QEMU 11.1.2 aborts there, serial.c:476's assertion; natemu's crossing is `run_l3.py` cases.)
    t.wr(COM1 + 3, 1, 0x03); t.wr(COM1 + 4, 1, 0x08)
    # REX.W does not promote (32 bits; with 66 too: REX.W wins, 32 bits).
    for raw in ('0x48, 0xed', '0x66, 0x48, 0xed', '0x48, 0xe5, 0x10'):
        t += f'    mov rax, {PAT:#x}\n    mov edx, {COM1 + 4:#x}\n    db {raw}\n    mov [rbx], rax\n    add rbx, 8'
    for op in AUTOCONFIG:
        if op[0] == 'r': t.rd(COM1 + op[1], 1)
        elif op[0] == 'w': t.wr(COM1 + op[1], 1, op[2])
        else: t += '    xor eax, eax\n    out 0x80, al'
    for ch in 'L3 8250\r\n':
        putc(t, ch)
    # THRE bookkeeping after empty: IIR clears it; a completed THR write makes it pending again.
    wait_tx(t)
    t.wr(COM1 + 1, 1, 0x02); t.rd(COM1 + 2, 1); t.rd(COM1 + 2, 1); t.wr(COM1, 1, ord('!'))
    wait_tx(t)
    t.rd(COM1 + 2, 1); t.wr(COM1 + 1, 1, 0)
    # Loopback with a 14-byte trigger at the slowest rate (a character takes ~5.7 s: no FIFO timeout during the test).
    t.wr(COM1 + 3, 1, 0x83); t.wr(COM1, 1, 0xff); t.wr(COM1 + 1, 1, 0xff); t.wr(COM1 + 3, 1, 0x03)
    t.wr(COM1 + 2, 1, 0xc1); t.wr(COM1 + 1, 1, 0x05); t.wr(COM1 + 4, 1, 0x1f)
    for b in range(13):
        t.wr(COM1, 1, 0x40 + b)
    t.rd(COM1 + 2, 1); t.rd(COM1 + 5, 1)
    for b in range(13, 20):
        t.wr(COM1, 1, 0x40 + b)
    t.rd(COM1 + 2, 1); t.rd(COM1 + 5, 1); t.rd(COM1 + 2, 1); t.rd(COM1 + 6, 1)
    for _ in range(3):
        t.rd(COM1, 1)
    t.rd(COM1 + 2, 1)
    drain = t.label()
    t += f'{drain}:\n    mov edx, {COM1 + 5:#x}\n    in al, dx\n    test al, 1\n    jz {drain}_done\n    mov edx, {COM1:#x}\n    in al, dx\n    mov [rbx], al\n    inc rbx\n    jmp {drain}\n{drain}_done:\n    add rbx, 16'
    t.rd(COM1 + 2, 1); t.rd(COM1, 1); t.rd(COM1 + 5, 1)
    t.wr(COM1 + 1, 1, 0); t.wr(COM1 + 4, 1, 0x08); t.wr(COM1 + 2, 1, 0x07); t.rd(COM1 + 2, 1); t.rd(COM1 + 5, 1); t.rd(COM1 + 6, 1)
    return build('l3-uart', t)


# ---- unassigned ports (ports QEMU leaves unassigned): every width, both encodings ------------------------------------
def unassigned_program():
    t = Text()
    for p, size, kind, imm in UNASSIGNED_ACCESS_LIST:
        if kind == 'r': t.rd(p, size, imm)
        else: t.wr(p, size, 0x9abcdef0 >> (8 * (4 - size)), imm)
    return build('l3-unassigned', t)


# Every access of `unassigned_program`: (port, size, 'r'|'w', imm8 form).
UNASSIGNED_ACCESS_LIST = [(p, size, kind, imm) for imm in (True, False)
                          for p in (UNASSIGNED_IMM if imm else UNASSIGNED_DX + (UNASSIGNED_IMM[0],))
                          for size, kind in ((1, 'r'), (2, 'r'), (4, 'r'), (1, 'w'), (2, 'w'), (4, 'w'), (4, 'r'))]
UNASSIGNED_ACCESSES = [(p, size, kind) for p, size, kind, _ in UNASSIGNED_ACCESS_LIST]


# ---- INS/OUTS ---------------------------------------------------------------------------------------------------------
SOURCE = b'The quick brown fox, 0123456789!'


def tables(i):
    pt = b''.join(((0x200000 + 0x1000 * k) | 0x67 if 0x200000 + 0x1000 * k != NP else 0).to_bytes(8, 'little') for k in range(512))
    return [{'addr': f'0x{TABLES:x}', 'data': ((TABLES + 0x1000) | 0x27).to_bytes(8, 'little').hex(), 'capture': False},
            {'addr': f'0x{TABLES + 0x1000:x}', 'data': ((TABLES + 0x2000) | 0x27).to_bytes(8, 'little').hex(), 'capture': False},
            {'addr': f'0x{TABLES + 0x2000:x}', 'data': (0xe7).to_bytes(8, 'little').hex() + ((TABLES + 0x3000) | 0x27).to_bytes(8, 'little').hex(), 'capture': False},
            {'addr': f'0x{TABLES + 0x3000:x}', 'data': pt.hex(), 'capture': False},
            {'addr': f'0x{SRC:x}', 'data': (SOURCE + bytes(0x1000 - 2 * len(SOURCE)) + SOURCE[::-1]).hex()}]


def strings_program():
    t = Text()
    end = SRC + 0x1000
    def rep(insn, rsi=None, rdi=None, rcx=None, port=COM1, df=False, prefix='rep '):
        if rsi is not None: t.__iadd__(f'    mov rsi, {rsi:#x}')
        if rdi is not None: t.__iadd__(f'    mov rdi, {rdi:#x}')
        if rcx is not None: t.__iadd__(f'    mov rcx, {rcx:#x}')
        t.__iadd__(f'    mov edx, {port:#x}\n' + ('    std\n' if df else '') + f'    {prefix}{insn}\n' + ('    cld' if df else ''))
        for r in ('rsi', 'rdi', 'rcx'):
            t.store(r)
    rep('outsb', SRC, rcx=12)
    rep('outsb', SRC + 20, rcx=5, df=True)
    rep('outsb', SRC + 3, rcx=0)                                   # a zero count: nothing
    rep('outsw', SRC + 4, port=COM1 + 6, prefix='')                # MSR (ignored), SCR
    t.rd(COM1 + 7, 1)
    rep('outsd', SRC + 8, rcx=2, port=COM1 + 4)                    # MCR, LSR, MSR, SCR twice
    wait_tx(t)
    t.rd(COM1 + 4, 4)
    t.wr(COM1 + 4, 1, 0x08)
    # 0x67: ESI and ECX (upper halves ignored; the writes zero-extend).
    rep('a32 rep outsb', 0xdead_beef_0000_0000 | SRC + 12, rcx=0x5555_5555_0000_0003, prefix='')
    # FS override on the source (FS.base by WRMSR; the stub's capture uses GS only).
    t += f'    mov ecx, 0xc0000100\n    mov eax, {SRC - 0x1000:#x}\n    xor edx, edx\n    wrmsr'
    rep('fs outsb', 0x1000 + 16, rcx=4, prefix='rep ')
    t += '    mov ecx, 0xc0000100\n    xor eax, eax\n    xor edx, edx\n    wrmsr'
    t.wr(COM1 + 7, 1, 0x3c)
    rep('insb', rdi=DATA(0) + 0x800, rcx=3, port=COM1 + 7)
    rep('insd', rdi=DATA(0) + 0x810, rcx=2, port=0x2f8)               # unassigned: all-ones
    # Loopback with FIFO: 8 bytes in by REP OUTSB, out by REP INSB / INSW / INSB DF.
    t.wr(COM1 + 2, 1, 0x07); t.wr(COM1 + 4, 1, 0x1f)
    rep('outsb', SRC, rcx=10)
    rep('insb', rdi=DATA(0) + 0x820, rcx=4)
    rep('insw', rdi=DATA(0) + 0x830, rcx=2)                          # RBR, IER per word
    rep('insb', rdi=DATA(0) + 0x84f, rcx=2, df=True)
    t.rd(COM1 + 5, 1)
    # Mid-string faults under the program's own tables (NP not present): OUTSB from a source crossing into NP takes #PF
    # after 6 elements; INSB to a destination crossing into NP after 4, and the FIFO keeps every byte not read.
    t += f'    mov rax, {TABLES:#x}\n    mov cr3, rax'
    t += '    lea r13, [rel l3_pf1]'
    rep('outsb', NP - 6, rcx=10)
    t += 'l3_pf1:'
    for r in ('rsi', 'rcx'): t.store(r)
    t += '    mov rax, cr2'
    t.store('rax')
    rep('outsb', SRC, rcx=10)
    t += '    lea r13, [rel l3_pf2]'
    rep('insb', rdi=NP - 4, rcx=10)
    t += 'l3_pf2:'
    for r in ('rdi', 'rcx'): t.store(r)
    drain = t.label()
    t += f'{drain}:\n    mov edx, {COM1 + 5:#x}\n    in al, dx\n    test al, 1\n    jz {drain}_done\n    mov edx, {COM1:#x}\n    in al, dx\n    mov [rbx], al\n    inc rbx\n    jmp {drain}\n{drain}_done:\n    add rbx, 16'
    t.wr(COM1 + 4, 1, 0x08); t.wr(COM1 + 2, 1, 0)
    return build('l3-strings', t, memory=tables)


# ---- I/O protection at CPL 3 (IOPL, the TSS I/O permission bitmap) -----------------------------------------------------
def tss_bytes(limit, base_field, allowed, rsp0):
    tss = bytearray(max(limit + 1, 0x68))
    tss[4:12] = rsp0.to_bytes(8, 'little')
    tss[0x66:0x68] = base_field.to_bytes(2, 'little')
    if base_field == 0x68 and limit > 0x68:
        bitmap = bytearray(b'\xff' * (limit + 1 - 0x68))
        for p in allowed:
            if p // 8 < len(bitmap): bitmap[p // 8] &= ~(1 << (p % 8))
        tss[0x68:] = bitmap
    return bytes(tss)


def gdt_bytes(limit):
    lo = (limit & 0xffff) | (TSS & 0xffffff) << 16 | 0x89 << 40 | (limit >> 16 & 0xf) << 48 | (TSS >> 24 & 0xff) << 56
    entries = [0, 0x00209b0000000000, 0x00cf93000000ffff, 0x00cff3000000ffff, 0x0020fb0000000000, lo, TSS >> 32]
    return b''.join(e.to_bytes(8, 'little') for e in entries) + (0x37).to_bytes(2, 'little') + GDT.to_bytes(8, 'little')


ALLOWED = [*range(0x3f8, 0x3fb), *range(0x3fc, 0x400), 0x80, 0x2f8, 0x2f9, 0xffff]
IO_CONFIGS = {
    'iopl3': dict(iopl=3, limit=0x67, base=0x68),
    'no-bitmap': dict(iopl=0, limit=0x67, base=0x68),
    'bitmap': dict(iopl=0, limit=0x68 + 0x2000, base=0x68),
    'short': dict(iopl=1, limit=0x68 + 0x7f, base=0x68),
    'base-beyond': dict(iopl=0, limit=0x68 + 0x2000, base=0xffff),
}


def protection_program(name):
    c = IO_CONFIGS[name]
    allowed = ALLOWED if name != 'short' else range(0x400)
    t = Text()
    normalize(t)
    t += f'    lgdt [{GDT + 0x38:#x}]\n    mov ax, 0x28\n    ltr ax'
    t += '    mov ecx, 0xc0000080\n    rdmsr\n    or eax, 1\n    wrmsr'
    t += '    mov ecx, 0xc0000081\n    xor eax, eax\n    mov edx, 0x00100008\n    wrmsr'
    t += '    mov ecx, 0xc0000082\n    lea rax, [rel l3_back]\n    mov rdx, rax\n    shr rdx, 32\n    wrmsr'
    t += '    mov ecx, 0xc0000084\n    xor eax, eax\n    xor edx, edx\n    wrmsr'
    t += f'    push 0x1b\n    push {STACK(0) - 0x400:#x}\n    push {c["iopl"] << 12 | 2:#x}\n    push 0x23\n    lea rax, [rel l3_user]\n    push rax\n    iretq'
    t += 'l3_user:'
    cases = [('rd', COM1 + 7, 1), ('rd', COM1 + 3, 1), ('rd', COM1 + 2, 2), ('rd', COM1 + 4, 4), ('rd', COM1 + 1, 4),
             ('wr', COM1 + 7, 1), ('rdi', 0x80, 1), ('rd', 0x2f8, 2), ('rd', 0x2f8, 4), ('rd', 0xffff, 1), ('rd', 0x3f0, 1),
             ('rd', 0x3e8, 2), ('ins', COM1 + 7, 1), ('outs', COM1 + 7, 1), ('outs', COM1 + 3, 1),
             # A zero count still makes the permission check (#GP(0) where denied).
             ('ins0', COM1 + 7, 1), ('outs0', COM1 + 7, 1), ('outs0', COM1 + 3, 1), ('outs0', 0x3e8, 1)]
    for k, (op, port, size) in enumerate(cases):
        resume = f'l3_case{k}'
        t += f'    lea r13, [rel {resume}]\n    mov r12, {k}'
        if op == 'rd': t.rd(port, size)
        elif op == 'rdi': t.rd(port, size, imm=True)
        elif op == 'wr': t.wr(port, size, 0x60 + k)
        else:
            count, op = (0, op[:-1]) if op.endswith('0') else (2, op)
            t += f'    mov edx, {port:#x}\n    lea rsi, [rel l3_src]\n    lea rdi, [rbx + 0x100]\n    mov ecx, {count}\n    rep {op}b'
            t.store('rcx')
        t += f'{resume}:'
    t += '    nop\n    syscall\nl3_src:\n    db 0x41, 0x42\nl3_back:'
    rsp0 = STACK(0)
    gdt = gdt_bytes(c['limit'])
    tss = tss_bytes(c['limit'], c['base'], allowed, rsp0)
    memory = lambda i: [{'addr': f'0x{GDT:x}', 'data': gdt.hex(), 'capture': False}, {'addr': f'0x{TSS:x}', 'data': tss.hex(), 'capture': False}]
    return build(f'l3-io-{name}', t, memory=memory)


TIMER_VECTOR, TIMER_ROUNDS, TIMER_COUNT, TIMER_OUTS = 0x50, 64, 480, 512
STRESS_LOG_AT, STRESS_LOG = 0x340000, 0x40000   # 4,096 entries: every delivery of the run is logged


def stress_program(rounds=150):
    """natemu only (kicks with events). First, TIMER_ROUNDS times, the local APIC's one-shot timer (vector TIMER_VECTOR,
    TIMER_COUNT ticks of the 24 MHz crystal at divide 1: 20 us) is armed right before a REP OUTSB of TIMER_OUTS bytes to an
    unassigned port (each element a port access, far longer in all than 20 us), so the interrupt
    arrives between its elements whatever the host's timing; then REP OUTSB of SOURCE to the THR and, in loopback with
    FIFO, REP OUTSB then REP INSB of 16 bytes, `rounds` times with IF = 1, so the driver's kicks land between elements too.
    Every delivery is logged (STRESS_LOG)."""
    t = Text()
    t += f'    sti\n    mov ecx, 0x83e\n    mov eax, 0xb\n    xor edx, edx\n    wrmsr\n    mov ecx, 0x832\n    mov eax, {TIMER_VECTOR:#x}\n    wrmsr'
    t += '    xor r9d, r9d'
    t += f'l3_tround:\n    mov ecx, 0x838\n    mov eax, {TIMER_COUNT}\n    xor edx, edx\n    wrmsr'
    t += f'    mov edx, {UNASSIGNED_DX[1]:#x}\n    mov rsi, {SRC:#x}\n    mov ecx, {TIMER_OUTS}\nl3_touts:\n    rep outsb'
    t += f'    inc r9\n    cmp r9, {TIMER_ROUNDS}\n    jb l3_tround'
    t += f'    xor r9d, r9d\n    mov rdi, {DATA(0) + 0x100:#x}'
    t += 'l3_round:'
    t += f'    mov edx, {COM1:#x}\n    mov rsi, {SRC:#x}\n    mov ecx, {len(SOURCE)}\nl3_outs:\n    rep outsb'
    t += f'    mov edx, {COM1 + 4:#x}\n    mov al, 0x1f\n    out dx, al\n    mov edx, {COM1 + 2:#x}\n    mov al, 0x07\n    out dx, al'
    t += f'    mov edx, {COM1:#x}\n    mov rsi, {SRC:#x}\n    mov ecx, 16\nl3_loop_outs:\n    rep outsb\n    mov ecx, 16\nl3_ins:\n    rep insb'
    t += f'    mov edx, {COM1 + 4:#x}\n    mov al, 0x08\n    out dx, al\n    mov edx, {COM1 + 2:#x}\n    xor eax, eax\n    out dx, al'
    t += f'    inc r9\n    cmp r9, {rounds}\n    jb l3_round\n    cli'
    return build('l3-stress', t, memory=tables, log_size=STRESS_LOG, log_at=lambda i: STRESS_LOG_AT)

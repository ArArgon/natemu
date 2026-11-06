"""L4a programs: the I/O APIC at 0xFEC00000, the 8259 pair (20H/21H, A0H/A1H, ELCR 4D0H/4D1H) and their routing into
the LAPICs, run unchanged by pinned QEMU (system mode, `-serial file:..`: COM1's IRQ4 is the trigger) and natemu.

Built on X3's harness (`apic.build`: own IDT, a log entry per delivery, counts per vector at COUNTS(i), EOI at the end of
each handler). Every program first maps the APIC and I/O APIC pages (2 MiB, PCD|PWT) on its own tables, stops QEMU's PIT
(control word 30H: mode 0, OUT low until a count is written; natemu has no PIT yet and ignores it), sets the UART to its
reset values (SeaBIOS used COM1) and masks LINT0 (QEMU's BSP resets it to ExtINT). Results are stored at DATA(i) + 8 k.
"""
import pathlib, sys
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent.parent))
import apic  # noqa: E402
from apic import build, rd, wr, eoi, COUNTS, DATA, SHARED, TABLES, LVT_LINT0, ISR, TMR, NMI  # noqa: E402

IOAPIC = 0xfec00000
COM1 = 0x3f8
LINE = 4             # COM1's ISA IRQ = I/O APIC pin 4


def map_pages():
    """MAP_APIC plus the I/O APIC's 2 MiB page (the same PD), for either mode."""
    return apic.MAP_APIC.replace(f'    mov rax, {TABLES:#x}\n    mov cr3, rax', f"""    mov rax, {IOAPIC | 0xfb:#x}
    mov [{TABLES + 0x3000 + 8 * ((IOAPIC >> 21) & 511):#x}], rax
    mov rax, {TABLES:#x}
    mov cr3, rax""")


def out(port, value):
    return f'    mov edx, {port:#x}\n    mov al, {value:#x}\n    out dx, al'


def inb(port):
    return f'    mov edx, {port:#x}\n    xor eax, eax\n    in al, dx'


def io_wr(sel, value):
    """IOREGSEL then IOWIN, 32-bit MOVs (Linux's `io_apic_write`); `value` an immediate or 'ecx'."""
    v = '    mov eax, ecx' if value == 'ecx' else f'    mov eax, {value:#x}'
    return f'    mov eax, {sel:#x}\n    mov [qword {IOAPIC:#x}], eax\n{v}\n    mov [qword {IOAPIC + 0x10:#x}], eax'


def io_rd(sel):
    return f'    mov eax, {sel:#x}\n    mov [qword {IOAPIC:#x}], eax\n    mov eax, [qword {IOAPIC + 0x10:#x}]'


def entry(pin, low, high=0):
    """A redirection entry, high half first (Linux's `__ioapic_write_entry`: the low half, holding the mask, last)."""
    return io_wr(0x11 + 2 * pin, high) + '\n' + io_wr(0x10 + 2 * pin, low)


def store(k, reg='rax'):
    return f'    mov [{{data}} + {8 * k}], {reg}'


def head(m):
    """Own tables with both APIC pages, QEMU's PIT stopped, the UART reset (as `ports.normalize`), LINT0 masked."""
    t = [map_pages(), out(0x43, 0x30)]
    for off, v in ((3, 0x80), (0, 0x0c), (1, 0), (3, 0), (2, 0), (1, 0), (4, 0x08), (7, 0)):
        t.append(out(COM1 + off, v))
    t += [inb(COM1 + 2), inb(COM1 + 5), inb(COM1), wr(m, LVT_LINT0, 0x10700)]
    return '\n'.join(t)


def ier(v):
    return out(COM1 + 1, v)


# Triggers run with IF = 0 and the interrupt is taken in an STI window, after the instruction after STI: QEMU's TCG takes
# an interrupt raised by an OUT or a store only at its next translation block, natemu at the next boundary.
WINDOW = '    sti\n    nop\n    cli'


LINUX_PIC_INIT = [(0x21, 0xff), (0x20, 0x11), (0x21, 0x30), (0x21, 0x04), (0x21, 0x01), (0xa0, 0x11), (0xa1, 0x38), (0xa1, 0x02), (0xa1, 0x01)]


def pic_init(master_mask=0xff, slave_mask=0xff, icw4=0x01):
    """Linux's `init_8259A` (arch/x86/kernel/i8259.c): ICW1-4 to both chips, vectors 30H/38H, then the IMRs."""
    seq = [(p, icw4 if (p, v) == (0x21, 0x01) else v) for p, v in LINUX_PIC_INIT] + [(0x21, master_mask), (0xa1, slave_mask)]
    return '\n'.join(out(p, v) for p, v in seq)


# ---- QEMU-compared (one CPU unless noted) --------------------------------------------------------------------------
def regs_program(m='x2'):
    """Register read-backs: ID (4 bits written), VER (0x170020), ARB, every entry, writes of every field with the
    read-only Remote IRR and delivery status bits set, IOREGSEL read-back, the page folded every 100H, IOWIN at byte
    width (reads 0, ignored), the EOI register with nothing pending."""
    t, k = [head(m)], 0
    def keep():
        nonlocal k
        t.append(store(k)); k += 1
    for sel in (0, 1, 2):
        t.append(io_rd(sel)); keep()
    t.append(io_wr(0, 0xff000000)); t.append(io_rd(0)); keep(); t.append(io_rd(2)); keep(); t.append(io_wr(0, 0))
    t.append(io_wr(1, 0x12345678)); t.append(io_rd(1)); keep()
    for pin in range(24):
        t.append(io_rd(0x10 + 2 * pin)); keep(); t.append(io_rd(0x11 + 2 * pin)); keep()
    # Every field written (masked, so no delivery): vector, modes, logical, polarity, level, the RO bits attempted.
    for pin, low, high in ((3, 0x1_ffff, 0xffff_ffff), (5, 0x1_5a00 | 0x41, 0x0300_0000), (7, 0x1_0f00 | 0x7f, 0x8000_0000), (11, 0x1_4000 | 0x31, 0)):
        t.append(entry(pin, low, high)); t.append(io_rd(0x10 + 2 * pin)); keep(); t.append(io_rd(0x11 + 2 * pin)); keep()
    # Unmasked level entries on GSIs no source asserts (16-23): nothing delivered, Remote IRR stays 0.
    t.append(entry(20, 0xa000 | 0x45, 0)); t.append(io_rd(0x10 + 40)); keep(); t.append(entry(20, 0x10000, 0))
    t.append(f'    mov eax, 0x13\n    mov [qword {IOAPIC:#x}], eax\n    mov eax, [qword {IOAPIC:#x}]'); keep()
    t.append(f'    xor eax, eax\n    mov al, [qword {IOAPIC:#x}]'); keep()
    t.append(f'    mov eax, [qword {IOAPIC + 0x110:#x}]'); keep()                       # folded: IOWIN of entry 1 (low)
    t.append(f'    mov eax, 0x10\n    mov [qword {IOAPIC + 0x200:#x}], eax\n    mov eax, [qword {IOAPIC + 0x310:#x}]'); keep()
    t.append(f'    mov eax, -1\n    mov al, [qword {IOAPIC + 0x10:#x}]'); keep()          # byte IOWIN: 0
    t.append(f'    mov al, 0x22\n    mov [qword {IOAPIC + 0x10:#x}], al'); t.append(io_rd(0x10)); keep()  # ignored
    t.append(f'    mov eax, [qword {IOAPIC + 0x40:#x}]'); keep()
    t.append(f'    mov eax, 0x45\n    mov [qword {IOAPIC + 0x40:#x}], eax'); t.append(io_rd(0x10 + 40)); keep()
    t.append(f'    mov eax, [qword {IOAPIC + 0x20:#x}]'); keep()
    t.append(io_wr(0, 0))
    return build(f'l4a-ioapic-regs-{m}', m, ['\n'.join(t)], [])


EDGE_V, LEVEL_V = 0x44, 0x54


def edge_program(m='x2'):
    """IRQ4 (the UART's THRE interrupt) through pin 4, edge, physical to CPU 0: each rise delivered once (IER 0 -> 2,
    a THR write's low-then-high), a rise while masked lost (no delivery at the unmask), TMR clear in the handler."""
    t = [head(m), pic_init(), entry(LINE, 0x10000 | EDGE_V, 0), entry(LINE, EDGE_V, 0),
         '    mov r12, 1', ier(2), WINDOW, '    mov r12, 2', ier(0), ier(2), WINDOW,
         '    mov r12, 3', out(COM1, ord('E')), WINDOW, ier(0),
         '    mov r12, 4', entry(LINE, 0x10000 | EDGE_V, 0), ier(2), entry(LINE, EDGE_V, 0), WINDOW,
         '    mov r12, 5', ier(0), ier(2), WINDOW, ier(0), io_rd(0x10 + 2 * LINE), store(0)]
    bodies = {EDGE_V: '\n'.join([rd(m, TMR + 2), f'    mov [{{data}} + 8], rax'])}
    return build(f'l4a-edge-{m}', m, ['\n'.join(t)], [EDGE_V], bodies)


def level_program(m='x2'):
    """IRQ4 through pin 4, level (the UART's line held high while its THRE interrupt is pending): delivered with Remote
    IRR set and TMR set; an EOI with the line still high delivers again; the handler clearing the source (IER 0, an IIR
    read) before its EOI ends it; masked while asserted, then unmasked: delivered at the unmask; the EOI register (40H)
    clears Remote IRR."""
    t = [head(m), pic_init(), entry(LINE, 0x10000 | 0x8000 | LEVEL_V, 0), entry(LINE, 0x8000 | LEVEL_V, 0),
         '    mov r12, 1', ier(2), WINDOW, '    mov r12, 2', io_rd(0x10 + 2 * LINE), store(2),
         '    mov r12, 3', entry(LINE, 0x18000 | LEVEL_V, 0), ier(2), WINDOW, io_rd(0x10 + 2 * LINE), store(3),
         '    mov r12, 4', entry(LINE, 0x8000 | LEVEL_V, 0), WINDOW, io_rd(0x10 + 2 * LINE), store(4),
         '    mov r12, 5', ier(0), ier(2), WINDOW, io_rd(0x10 + 2 * LINE), store(5)]
    count = f'[{COUNTS(0) + 8 * LEVEL_V:#x}]'
    body = '\n'.join([
        io_rd(0x10 + 2 * LINE), f'    mov rbx, {count}', f'    mov [{{data}} + 0x100 + 8 * rbx], rax',
        rd(m, TMR + 2), f'    mov [{{data}} + 0x180 + 8 * rbx], rax',
        '    cmp rbx, 1', '    je 9f',                         # 1st: EOI with the line high (delivered again)
        '    cmp rbx, 3', '    jne 1f', inb(COM1 + 2), '    jmp 9f',   # 3rd: an IIR read clears THRI
        '1:', ier(0),                                           # 2nd, 4th: IER 0
        '    cmp rbx, 4', '    jne 9f',
        f'    mov eax, {LEVEL_V:#x}\n    mov [qword {IOAPIC + 0x40:#x}], eax', io_rd(0x10 + 2 * LINE), f'    mov [{{data}} + 0x200], rax',
        '9:'])
    return build(f'l4a-level-{m}', m, ['\n'.join(t)], [LEVEL_V], {LEVEL_V: body})


def pic_program(m='x2'):
    """The 8259 pair: Linux's `probe_8259A` and `init_8259A`, IMR/IRR/ISR read-backs (OCW3), the ELCR's writable bits;
    ExtINT through LINT0 with the I/O APIC masked (IRQ4 -> vector 34H, the ISR before and after a specific EOI, no LAPIC
    ISR bit); the poll command; level mode by the ELCR (a PIC EOI with the line high requests again); auto-EOI; rotation
    (OCW2 rotate on non-specific EOI, set priority) and special mask mode read through the poll command."""
    s, k = [head(m)], 0
    def keep(reg='rax'):
        nonlocal k
        s.append(store(k, reg)); k += 1
    s += [out(0xa1, 0xff), out(0x21, 0xfb), inb(0x21)]; keep()                         # probe_8259A
    s += [pic_init()]
    for port, ocw3 in ((0x20, 0x0a), (0x20, 0x0b), (0xa0, 0x0a), (0xa0, 0x0b)):
        s += [out(port, ocw3), inb(port)]; keep()
    s += [inb(0x21)]; keep(); s += [inb(0xa1)]; keep()
    s += [out(0x4d0, 0xff), out(0x4d1, 0xff), inb(0x4d0)]; keep(); s += [inb(0x4d1)]; keep()
    s += [out(0x4d0, 0), out(0x4d1, 0)]
    # IRR with IRQ4 masked: the request is latched, the output stays low.
    s += [out(0x20, 0x0a), ier(2), inb(0x20)]; keep(); s += [ier(0)]
    s += [pic_init(), out(0x20, 0x0a), inb(0x20)]; keep()                             # ICW1 clears an edge IRR
    # ExtINT through LINT0.
    s += [wr(m, LVT_LINT0, 0x700), out(0x21, 0xef), '    mov r12, 1', ier(2), WINDOW, ier(0)]
    # Poll with IF = 0: the request is acknowledged by the read (ISR set), a specific EOI ends it; a second poll reads 0.
    s += ['    mov r12, 2', ier(2), out(0x20, 0x0c), inb(0x20)]; keep()
    s += [out(0x20, 0x0b), inb(0x20)]; keep(); s += [out(0x20, 0x64), out(0x20, 0x0c), inb(0x20)]; keep(); s += [ier(0)]
    # Level by the ELCR: the handler's PIC EOI with the line high requests again; the second clears the source first.
    s += [out(0x4d0, 0x10), '    mov r12, 3', ier(2), WINDOW, out(0x4d0, 0)]
    # Auto-EOI (ICW4 = 3): no ISR bit after the INTA.
    s += [pic_init(0xef, 0xff, icw4=0x03), '    mov r12, 4', ier(2), WINDOW, ier(0), out(0x20, 0x0b), inb(0x20)]; keep()
    # Rotation and special mask, seen through polls and ISR reads (IRQ4 is the one source).
    s += [pic_init(0xff, 0xff), out(0x20, 0xc5), out(0x20, 0x0a), inb(0x20)]; keep()   # OCW2 set priority (lowest = IRQ5)
    s += [out(0x20, 0x68), out(0x20, 0x0b), inb(0x20)]; keep()                         # OCW3 special mask on
    s += [ier(2), out(0x20, 0x0c), inb(0x20)]; keep()                                  # masked IRQ4: poll reads 0
    s += [out(0x21, 0xef), out(0x20, 0x0c), inb(0x20)]; keep()                         # unmasked: 84H
    s += [out(0x20, 0xa0), out(0x20, 0x0b), inb(0x20)]; keep()                         # rotate on non-specific EOI
    s += [out(0x20, 0x48), ier(0), ier(2), out(0x20, 0x0c), inb(0x20)]; keep()         # special mask off, poll again
    s += [out(0x20, 0x20), ier(0), inb(0x21)]; keep()
    s += [wr(m, LVT_LINT0, 0x10700), out(0x21, 0xff), out(0xa1, 0xff)]
    count = f'[{COUNTS(0) + 8 * 0x34:#x}]'
    body = '\n'.join([
        f'    mov rbx, {count}', out(0x20, 0x0b), inb(0x20), f'    mov [{{data}} + 0x100 + 8 * rbx], rax', rd(m, ISR + 1), f'    mov [{{data}} + 0x180 + 8 * rbx], rax',
        f'    cmp r12, 3', '    jne 1f', '    cmp rbx, 3', '    jne 1f', ier(0), '1:',  # level: the 2nd in the step clears the source
        f'    cmp r12, 4', '    je 2f', out(0x20, 0x64), '2:', out(0x20, 0x0b), inb(0x20), f'    mov [{{data}} + 0x200 + 8 * rbx], rax'])
    return build(f'l4a-pic-{m}', m, ['\n'.join(s)], [0x34], {0x34: body})


# Destination cases at n CPUs: (label, entry low without vector, destination, {cpu: deliveries}); CPU 0 drives IRQ4.
def dest_cases(n):
    every = sum(1 << j for j in range(n))
    cases = [(f'phys{j}', 0, j, {j: 1}) for j in range(n)]
    cases += [('logical-odd', 0x800, 0b1010 & every, {j: 1 for j in range(n) if j & 1}),
              ('logical-all', 0x800, every, {j: 1 for j in range(n)}),
              ('lowest', 0x800 | 0x100, every & ~1, {1: 1}),
              ('nmi', 0x400, n - 1, {n - 1: 'nmi'}),
              ('level-peer', 0x8000, n - 1, {n - 1: 1})]
    return cases


def dest_program(n, m='x2'):
    """I/O APIC destinations at n CPUs (QEMU: x2APIC, its BSP's mode with peers; natemu also xAPIC with flat LDRs
    1 << (24 + i)): CPU 0 programs pin 4 per case (vector 0x60 + case, or NMI), raises IRQ4 (IER 0 -> 2), waits for each
    expected delivery, lowers it; the level case's handler (on the peer) lowers the line itself before its EOI."""
    ready, done = SHARED, SHARED + 0x100
    setup = (wr(m, apic.DFR, 0xffffffff) + '\n' + wr(m, apic.LDR, '0x{0:x}')) if m == 'x' else '    nop'
    c0 = [head(m), setup.format(1 << 24), pic_init(), '    mov r12, 1', '1:', '    pause'] + [f'    cmp qword [{ready + 8 * j:#x}], 1\n    jne 1b' for j in range(1, n)]
    c0 += ['    sti']
    vectors = []
    for c, (label, low, dest, want) in enumerate(dest_cases(n)):
        v = 0x60 + c
        vectors.append(v)
        high = dest << 24
        c0 += [f'    mov r12, {0x10 + c}', entry(LINE, low | (0 if low & 0x700 == 0x400 else v), high), ier(2)]
        for j, what in want.items():
            counter = COUNTS(j) + 8 * (NMI if what == 'nmi' else v)
            c0 += ['2:', '    pause', f'    cmp qword [{counter:#x}], 1', '    jne 2b']
        if label == 'level-peer':
            c0 += ['3:', '    pause', f'    cmp qword [{done + 8:#x}], 1', '    jne 3b']
        c0 += [ier(0), entry(LINE, 0x10000, 0)]
    c0 += ['    cli', f'    mov qword [{done:#x}], 1']
    peers = ['\n'.join([setup.format(1 << (24 + j)), '    sti', f'    mov qword [{ready + 8 * j:#x}], 1', '1:', '    pause',
                        f'    cmp qword [{done:#x}], 1', '    jne 1b', '    cli']) for j in range(1, n)]
    level_v = 0x60 + len(dest_cases(n)) - 1
    bodies = {level_v: '\n'.join([ier(0), f'    mov qword [{done + 8:#x}], 1'])}
    texts = ['\n'.join(c0)] + peers
    return build(f'l4a-dest-{m}', m, texts, vectors + [NMI], bodies)


def dest_expected(n):
    out = {i: {} for i in range(n)}
    for c, (_, _, _, want) in enumerate(dest_cases(n)):
        for j, what in want.items():
            v = NMI if what == 'nmi' else 0x60 + c
            out[j][v] = out[j].get(v, 0) + 1
    return out


# ---- natemu only: the asynchronous test source (`run-program --irq-source PORT`) ------------------------------------
SOURCE = 0x510


def stress_program(n, k=300, m='x'):
    """Asynchronous lines across n CPUs: CPU i owns GSI 16 + i, routed (pin 16 + i, vector 0x70 + i, physical) to CPU
    (i + 1) % n. Each round asks the host's test source for the line after a pseudo-random 0..255 us, then waits for the
    target's handler to report it done: k edge rounds, then k level rounds (the target's handler deasserts the line
    through the source's port +4 before its EOI). Each target counts every delivery and every completion per source.
    IOREGSEL/IOWIN is one shared index pair: entries are written under a guest lock with IF = 0 (as Linux's
    `ioapic_lock`), and handlers learn the trigger mode from the source's flag, never from the I/O APIC."""
    texts, lock = [], SHARED + 0x400
    for i in range(n):
        tgt, v, pin = (i + 1) % n, 0x70 + i, 16 + i
        done, level = SHARED + 0x200 + 8 * i, SHARED + 0x300 + 8 * i
        t = [map_pages(), f'    mov r15, {0x9e3779b97f4a7c15 ^ (i + 1):#x}']
        for phase, (low, flag) in enumerate(((v, 0), (0x8000 | v, 0x100))):
            t += ['    cli', '3:', '    mov eax, 1', f'    xchg [{lock:#x}], eax', '    test eax, eax', '    jnz 3b',
                  entry(pin, low, tgt << 24), f'    mov qword [{lock:#x}], 0', f'    mov qword [{level:#x}], {phase}', '    sti',
                  f'    mov r8d, {k}', '1:',
                  '    mov rax, r15', '    shl rax, 13', '    xor r15, rax', '    mov rax, r15', '    shr rax, 7', '    xor r15, rax',
                  '    mov rax, r15', '    shl rax, 17', '    xor r15, rax',
                  f'    mov rax, r15\n    and eax, 0xff0000\n    or eax, {flag | pin:#x}\n    mov edx, {SOURCE:#x}\n    out dx, eax',
                  f'    mov r9, {k + 1}', '    sub r9, r8']
            if phase:
                t[-1] += f'\n    add r9, {k}'
            t += ['2:', '    pause', f'    cmp [{done:#x}], r9', '    jne 2b', '    dec r8d', '    jnz 1b']
        # A vCPU that ended takes no more interrupts: wait (IF = 1) until the source routed here is done too.
        t += ['4:', '    pause', f'    cmp qword [{SHARED + 0x200 + 8 * ((i - 1) % n):#x}], {2 * k}', '    jne 4b']
        texts.append('\n'.join(t) + '\n    cli')
    bodies = []
    for j in range(n):
        src = (j - 1) % n
        v, pin = 0x70 + src, 16 + src
        level = f'    mov eax, {pin:#x}\n    mov edx, {SOURCE + 4:#x}\n    out dx, eax'
        bodies.append({v: '\n'.join([f'    cmp qword [{SHARED + 0x300 + 8 * src:#x}], 0', '    je 1f', level, '1:']),
                       v + 0x100: f'    inc qword [{SHARED + 0x200 + 8 * src:#x}]'})   # done, after the EOI
    progs, labels = build(f'l4a-stress-{m}', m, texts, [0x70 + i for i in range(n)], bodies)
    return progs, labels


def halt_program(delay_us=60_000):
    """The halted rule with an asynchronous source: CPU 0 asks for an edge on GSI 16 (pin 16 -> itself, vector 0x70)
    after delay_us and halts until it arrives, while CPU 1 has ended: the driver must not end CPU 0 while the request is
    pending (the source vetoes); without the veto it is ended `halted`."""
    m = 'x'
    assert delay_us < 1 << 16
    c0 = [map_pages(), entry(16, 0x70, 0), f'    mov eax, {delay_us << 16 | 16:#x}',
          f'    mov edx, {SOURCE:#x}', '    out dx, eax', '1:', '    cli', f'    cmp qword [{COUNTS(0) + 8 * 0x70:#x}], 1', '    je 2f', '    sti', '    hlt', '    jmp 1b', '2:']
    return build('l4a-halt', m, ['\n'.join(c0), '    nop'], [0x70])


def lint1_program():
    """The board's NMI input (LINT1, the MADT's Local APIC NMI) through the test source (request bit 9): with LVT LINT1
    unmasked in NMI mode each pulse is an NMI; masked, a pulse is lost (the source's fired count says it happened)."""
    m, req = 'x', f'    mov eax, 0x200\n    mov edx, {SOURCE:#x}\n    out dx, eax'
    def until(what, n):
        at = {'nmi': f'cmp qword [{COUNTS(0) + 8 * NMI:#x}], {n}', 'fired': f'mov edx, {SOURCE:#x}\n    in eax, dx\n    cmp eax, {n}'}[what]
        return f'1:\n    pause\n    {at}\n    jne 1b'
    t = [wr(m, apic.LVT_LINT1, 0x400), req, until('nmi', 1), wr(m, apic.LVT_LINT1, 0x10400), req, until('fired', 2),
         wr(m, apic.LVT_LINT1, 0x400), req, until('nmi', 2), f'    mov edx, {SOURCE:#x}\n    in eax, dx', store(0)]
    return build('l4a-lint1', m, ['\n'.join(t)], [NMI])


def tmr_program(m='x2'):
    """The TMR follows the trigger of each acceptance (SDM 11.8.4; the review's probe): 54H accepted level-triggered from
    pin 4 (TMR 1 in its handler), then the same vector as a self IPI (edge: TMR 0 in its handler, and its EOI broadcasts
    nothing)."""
    t = [head(m), pic_init(), entry(LINE, 0x8000 | LEVEL_V, 0),
         '    mov r12, 1', ier(2), WINDOW, '    mov r12, 2', apic.self_ipi(m, LEVEL_V), WINDOW, io_rd(0x10 + 2 * LINE), store(0)]
    count = f'[{COUNTS(0) + 8 * LEVEL_V:#x}]'
    body = '\n'.join([f'    mov rbx, {count}', rd(m, TMR + 2), f'    mov [{{data}} + 0x180 + 8 * rbx], rax', ier(0)])
    return build(f'l4a-tmr-{m}', m, ['\n'.join(t)], [LEVEL_V], {LEVEL_V: body})


def lint0_program():
    """natemu only (QEMU passes the 8259 through LINT0 in any unmasked mode): LVT LINT0 fixed, edge, vector 45H: the PIC
    output's rise is one 45H interrupt (SDM 11.5.1), no INTA (the PIC's IRR keeps IRQ4, its ISR stays clear); the output
    falling and rising again (IMR) is a second. Then the APIC hardware disabled (IA32_APIC_BASE.EN = 0, LINT0 masked): the
    8259 is wired through, an ExtINT 34H with its INTA."""
    m = 'x'
    t = [head(m), pic_init(0xef, 0xff), wr(m, LVT_LINT0, 0x45), '    mov r12, 1', ier(2), WINDOW,
         out(0x20, 0x0a), inb(0x20), store(0), out(0x20, 0x0b), inb(0x20), store(1),
         '    mov r12, 2', out(0x21, 0xff), out(0x21, 0xef), WINDOW,
         '    mov r12, 3', wr(m, LVT_LINT0, 0x10045), '    mov ecx, 0x1b\n    rdmsr\n    and eax, 0xfffff7ff\n    wrmsr', WINDOW,
         out(0x20, 0x0b), inb(0x20), store(2), ier(0)]
    return build('l4a-lint0', m, ['\n'.join(t)], [0x45, 0x34], {0x34: out(0x20, 0x64)})

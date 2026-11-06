"""L4b programs: the 8254 PIT (40H-43H) and port 61H, the MC146818 RTC (70H-71H), the ACPI PM block (B000H: PM1 event and
control, the PM timer) and the reset register (B040H), run unchanged by pinned QEMU (system mode under SeaBIOS, which
puts the PIIX4 PM block at B000H) and natemu.

Built on L4a's harness (`irqs.head`: own tables with the APIC and I/O APIC pages, QEMU's PIT stopped with control word
30H, the UART reset, LINT0 masked; X3's `apic.build` handlers). Each program stores its compared results at DATA(0) +
8 k for k < 64 (values that do not depend on timing: register read-backs, flags, counts) and the raw time-dependent
values (TSC brackets, counts, PM ticks) at DATA(0) + 8 k for k >= 64, which only the exact model judges (`check_*`).
Timing-sensitive sequences run twice (r13): the first round translates every path, so the second is not slowed by
first translations (a host compile costs milliseconds, the PIT's MSB lasts 214 us).
"""
import pathlib, sys
from fractions import Fraction
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent.parent))
import apic  # noqa: E402
from apic import build, COUNTS, DATA, SHARED, RING, RING_BYTES  # noqa: E402
from irqs import head, out, inb, entry, map_pages  # noqa: E402

PIT_HZ, PM_HZ, TSC_HZ = 1_193_182, 3_579_545, 1_000_000_000
NS = Fraction(TSC_HZ)
RAW = 64
IRQ0_V, RTC_V, SCI_V = 0x40, 0x48, 0x49
CURSOR = SHARED + 0x800                        # ring cursor (bytes), as X3's timer programs
TSC = '    rdtsc\n    shl rdx, 32\n    or rax, rdx'


def store(k, reg='rax'):
    return f'    mov [{{data}} + {8 * k}], {reg}'


def outw(port, v):
    return f'    mov edx, {port:#x}\n    mov eax, {v:#x}\n    out dx, ax'


def inw(port):
    return f'    mov edx, {port:#x}\n    xor eax, eax\n    in ax, dx'


def inl(port):
    return f'    mov edx, {port:#x}\n    in eax, dx'


def wait_tsc(ticks):
    """Busy-waits `ticks` TSC ticks (clobbers rax, rbx, rdx)."""
    return '\n'.join([TSC, f'    mov rbx, {ticks}', '    add rbx, rax', '1:', '    pause', TSC, '    cmp rax, rbx', '    jb 1b'])


def in_range(k, lo, hi):
    """Stores at k whether eax (zero-extended) is in [lo, hi]."""
    return '\n'.join(['    xor ecx, ecx', f'    cmp eax, {lo:#x}', '    jb 1f', f'    cmp eax, {hi:#x}', '    ja 1f', '    mov ecx, 1', '1:', store(k, 'rcx')])


def halt_until(counter, n):
    """HLT (IF = 1 only in the HLT's window) until the 64-bit counter reaches n."""
    return '\n'.join(['1:', '    cli', f'    cmp qword [{counter:#x}], {n}', '    jae 2f', '    sti', '    hlt', '    jmp 1b', '2:'])


def drain():
    """Takes any interrupt still pending (one STI window), then IF = 0."""
    return '    sti\n    nop\n    cli'


def ring_body():
    """A handler's TSC into the ring (X3's natemu-only samples): RING(0) + cursor."""
    return '\n'.join([TSC, f'    mov rbx, [{CURSOR:#x}]', f'    cmp rbx, {RING_BYTES}', '    jae 9f', f'    mov [rbx + {RING(0):#x}], rax',
                      f'    add qword [{CURSOR:#x}], 8', '9:'])


def rounds(text, n=2, label='rounds'):
    return '\n'.join([f'    mov r13d, {n}', f'{label}:', text, '    dec r13d', f'    jnz {label}'])


# ---- the PIT ----------------------------------------------------------------------------------------------------
QPC_SUBS = """    jmp qp_over
qp_verify:
    mov edx, 0x42
    in al, dx
    in al, dx
    cmp al, sil
    sete al
    movzx eax, al
    ret
qp_expect:
    xor edi, edi
    xor ebx, ebx
    xor r15d, r15d
qp_e1:
    call qp_verify
    test eax, eax
    jz qp_e2
    mov r15, rbx
    rdtsc
    shl rdx, 32
    or rax, rdx
    mov rbx, rax
    inc edi
    cmp edi, 50000
    jb qp_e1
qp_e2:
    rdtsc
    shl rdx, 32
    or rax, rdx
    sub rax, r15
    mov rcx, rax
    xor eax, eax
    cmp edi, 5
    seta al
    ret
qp_over:"""


def quick_pit_calibrate(k_ok, raw):
    """Linux's `quick_pit_calibrate` (arch/x86/kernel/tsc.c): stores 1 at k_ok on success; raw: i, delta, d1, d2."""
    return '\n'.join([
        '    mov edx, 0x61', '    in al, dx', '    and al, 0xfc', '    or al, 1', '    out dx, al',
        out(0x43, 0xb0), '    mov edx, 0x42', '    mov al, 0xff', '    out dx, al', '    out dx, al',
        '    xor esi, esi', '    call qp_verify',
        '    mov esi, 0xff', '    call qp_expect', '    test eax, eax', '    jz qp_fail', '    mov r9, rbx', '    mov r11, rcx',
        '    mov r8d, 1',
        'qp_loop:', '    cmp r8d, 233', '    ja qp_fail', '    mov esi, 0xff', '    sub esi, r8d', '    call qp_expect',
        '    test eax, eax', '    jz qp_fail', '    mov r10, rbx', '    sub r10, r9', '    lea rax, [r11 + rcx]',
        '    cmp r8d, 1', '    jne qp_l2', '    imul rdx, r10, 233', '    shr rdx, 11', '    cmp rax, rdx', '    jae qp_fail',
        'qp_l2:', '    mov rdx, r10', '    shr rdx, 11', '    cmp rax, rdx', '    jae qp_next',
        '    mov esi, 0xfe', '    sub esi, r8d', '    push rcx', '    call qp_verify', '    pop rcx', '    test eax, eax', '    jz qp_fail',
        '    mov qword [{data} + %d], 1' % (8 * k_ok), store(raw, 'r8'), store(raw + 1, 'r10'), store(raw + 2, 'r11'), store(raw + 3, 'rcx'),
        '    jmp qp_done',
        'qp_next:', '    inc r8d', '    jmp qp_loop',
        'qp_fail:', '    mov qword [{data} + %d], 0' % (8 * k_ok), store(raw, 'r8'),
        'qp_done:'])


def pit_calibrate_tsc(k_ok, raw, latch=11932):
    """Linux's `pit_calibrate_tsc` loop (10 ms, CAL_LATCH): OUT2 polled on 61H; raw: t2 - t1, pitcnt, tscmin, tscmax."""
    return '\n'.join([
        '    mov edx, 0x61', '    in al, dx', '    and al, 0xfc', '    or al, 1', '    out dx, al',
        out(0x43, 0xb0), out(0x42, latch & 0xff), out(0x42, latch >> 8),
        TSC, '    mov r9, rax', '    mov rbx, rax', '    mov rcx, -1', '    xor r10d, r10d', '    xor edi, edi',
        'pc_loop:', '    mov edx, 0x61', '    in al, dx', '    test al, 0x20', '    jnz pc_done', TSC, '    mov rsi, rax', '    sub rsi, rbx',
        '    mov rbx, rax', '    cmp rsi, rcx', '    cmovb rcx, rsi', '    cmp rsi, r10', '    cmova r10, rsi', '    inc edi', '    jmp pc_loop',
        'pc_done:', '    mov rax, rbx', '    sub rax, r9', store(raw), store(raw + 1, 'rdi'), store(raw + 2, 'rcx'), store(raw + 3, 'r10'),
        '    xor eax, eax', '    test edi, edi', '    setnz al', store(k_ok)])


def pit_program(m='x2'):
    """Read-back status and count latches, the access modes, the counter latch held across a second latch command, port
    61H (gate, data, refresh toggle, OUT2 in mode 0), Linux's `pit_calibrate_tsc` and `quick_pit_calibrate`, IRQ0 at
    the rate generator's rate (mode 2, 1193: 20 deliveries through I/O APIC pin 2) and Linux's PIT-disable sequence
    (30H, 0, 0, 30H: QEMU's model gives one more IRQ0 55 ms later, the reloaded count's terminal count).
    Raw slots: RAW + 0..4 counts, + 8..11 `pit_calibrate_tsc`, + 12..15 `quick_pit_calibrate`, + 16..18 IRQ0 TSCs."""
    t = []
    t += [out(0x43, 0x30), out(0x40, 0x00), out(0x40, 0x10), out(0x43, 0xe2), inb(0x40), store(0)]           # status 30H
    t += [out(0x43, 0xc2), inb(0x40), store(1), inb(0x40), '    mov ecx, eax', inb(0x40), '    shl eax, 8', '    or eax, ecx',
          store(RAW), in_range(2, 0xf00, 0x1000)]                                                           # status, count
    t += [out(0x43, 0x36), out(0x40, 0), out(0x40, 0), out(0x43, 0xe2), inb(0x40), store(3)]                 # mode 3: B6H
    t += [out(0x43, 0x34), out(0x40, 0x34), out(0x40, 0x12), out(0x43, 0xe2), inb(0x40), '    and eax, 0x7f', store(4)]
    t += [out(0x43, 0x50), out(0x41, 0x80), inb(0x41), '    mov esi, eax', inb(0x41), store(RAW + 1), '    mov [{data} + %d], rsi' % (8 * (RAW + 2)),
          '    xor ecx, ecx', '    cmp esi, 0x80', '    ja 1f', '    cmp eax, esi', '    ja 1f', '    mov ecx, 1', '1:', store(5, 'rcx')]
    t += [out(0x43, 0x60), out(0x41, 0x10), inb(0x41), in_range(6, 0x0f, 0x10)]                             # MSB only
    # Counter latch: a second latch before the first is read is ignored (mode 2, 1234H: the held value is >= 50 us old).
    t += [out(0x43, 0x00), wait_tsc(50_000), out(0x43, 0x00), inb(0x40), '    mov ecx, eax', inb(0x40), '    shl eax, 8', '    or eax, ecx',
          '    mov esi, eax', out(0x43, 0x00), inb(0x40), '    mov ecx, eax', inb(0x40), '    shl eax, 8', '    or eax, ecx',
          store(RAW + 3), '    mov [{data} + %d], rsi' % (8 * (RAW + 4)),
          '    mov ecx, esi', '    sub ecx, eax', '    jns 2f', '    add ecx, 0x1234', '2:', '    xor eax, eax', '    cmp ecx, 40', '    setae al', store(7)]
    # A status latch is not latched again until read; the next read gives the count.
    t += [out(0x43, 0xe2), out(0x43, 0xe2), inb(0x40), '    and eax, 0x7f', store(8), inb(0x40), inb(0x40)]
    # Port 61H.
    t += [out(0x61, 0), inb(0x61), '    mov esi, eax', inb(0x61), '    xor eax, esi', '    and eax, 0x10', store(10), '    mov eax, esi', '    and eax, 0x0f', store(9),
          out(0x61, 3), inb(0x61), '    and eax, 3', store(11), out(0x61, 1),
          out(0x43, 0xb0), out(0x42, 0x00), out(0x42, 0x04), inb(0x61), '    shr eax, 5', '    and eax, 1', store(12),
          '    mov ecx, 2000000', '3:', inb(0x61), '    test al, 0x20', '    jnz 4f', '    dec ecx', '    jnz 3b', '4:',
          '    xor eax, eax', '    test ecx, ecx', '    setnz al', store(13), inb(0x61), '    shr eax, 5', '    and eax, 1', store(14)]
    t += [pit_calibrate_tsc(15, RAW + 8)]
    # IRQ0, mode 2, 1193 (~1 ms), 20 deliveries; then Linux's PIT-disable sequence.
    t += [entry(2, IRQ0_V), f'    mov qword [{CURSOR:#x}], 0', f'    mov qword [{COUNTS(0) + 8 * IRQ0_V:#x}], 0', TSC, store(RAW + 16),
          out(0x43, 0x34), out(0x40, 0xa9), out(0x40, 0x04), TSC, store(RAW + 17), halt_until(COUNTS(0) + 8 * IRQ0_V, 20),
          out(0x43, 0x30), out(0x40, 0), out(0x40, 0), out(0x43, 0x30), TSC, store(RAW + 18), drain(),
          f'    mov rsi, [{COUNTS(0) + 8 * IRQ0_V:#x}]', '    xor eax, eax', '    cmp rsi, 20', '    setae al', store(20),
          '    sti', wait_tsc(100_000_000), '    cli', f'    mov rax, [{COUNTS(0) + 8 * IRQ0_V:#x}]', '    sub rax, rsi', store(21),
          entry(2, 0x10000 | IRQ0_V)]
    # quick_pit_calibrate: every failure ends on a path not run before (a compile there costs more than the 214 us an
    # MSB lasts), so it runs 8 times; the last result stays.
    text = head(m) + '\n' + QPC_SUBS + '\n' + rounds('\n'.join(t)) + '\n' + rounds(quick_pit_calibrate(16, RAW + 12), 8, 'qp_rounds')
    return build(f'l4b-pit-{m}', m, [text], [IRQ0_V], {IRQ0_V: ring_body()}, capture_ring=True)


PIT_WANT = {0: 0x30, 1: 0x30, 2: 1, 3: 0xb6, 4: 0x34, 5: 1, 6: 1, 7: 1, 8: 0x34, 9: 0, 10: 0x10, 11: 3, 12: 0, 13: 1, 14: 1,
            15: 1, 20: 1, 21: 1}


def tsc_window(mem, k):
    return mem.read(DATA(0) + 8 * k)


def check_pit(mem, natemu):
    """The model on either machine's results; `natemu`: also the exact timing (TSC = V at 1 GHz, PIT edges from V)."""
    got = {k: mem.read(DATA(0) + 8 * k) for k in PIT_WANT}
    out = [f'k{k}: {got[k]:#x} != model {v:#x}' for k, v in PIT_WANT.items() if got[k] != v]
    if not natemu:
        return out, {}
    raw = lambda j: mem.read(DATA(0) + 8 * (RAW + j))
    summary = {}
    # pit_calibrate_tsc: OUT2 rises 11932 PIT ticks (+ at most one edge) after the load, which lies before t1.
    t21, pitcnt, tmin, tmax = raw(8), raw(9), raw(10), raw(11)
    rise = Fraction(11932, PIT_HZ) * NS
    summary['pit_calibrate'] = {'t2_t1_ns': t21, 'pitcnt': pitcnt, 'tscmin': tmin, 'tscmax': tmax, 'linux_accepts': pitcnt >= 1000 and tmax <= 10 * tmin}
    # t1 follows the load by one port exit at most (< 5 us); t2 is the poll before the read that saw OUT2 high.
    lo, hi = rise - tmax - 2 * NS / PIT_HZ - 5000, rise + 5000
    if not lo <= t21 <= hi:
        out.append(f'pit_calibrate_tsc: t2 - t1 = {t21} ns, model [{float(lo):.0f}, {float(hi):.0f}]')
    # quick_pit_calibrate: success and the kHz it computes (Linux: delta * PIT_TICK_RATE / (i * 256 * 1000)).
    i, delta = raw(12), raw(13)
    khz = delta * PIT_HZ // (i * 256 * 1000) if mem.read(DATA(0) + 8 * 16) else None
    summary['quick_pit_calibrate'] = {'ok': bool(mem.read(DATA(0) + 8 * 16)), 'i': i, 'delta': delta, 'd1': raw(14), 'd2': raw(15), 'khz': khz}
    if khz is not None and abs(khz - 1_000_000) > 500:
        out.append(f'quick_pit_calibrate: {khz} kHz, not 1 GHz within 500 ppm')
    # IRQ0: rising edges at the load edge + 1193 j (j >= 1), the load between the two TSCs around the OUTs.
    t0, t1 = raw(16), raw(17)
    period = Fraction(1193, PIT_HZ) * NS
    ring = [mem.read(RING(0) + 8 * j) for j in range(mem.read(CURSOR) // 8)]
    late = []
    for j, d in enumerate(ring[:20], 1):
        lo, hi = t0 + j * period - NS / PIT_HZ, t1 + j * period + NS / PIT_HZ
        if d < lo:
            out.append(f'IRQ0 delivery {j} at {d - t0} ns before its edge ({float(lo - t0):.0f})')
        late.append(float(d - hi))
    if late:
        summary['irq0_late_us'] = [round(max(late) / 1000, 1), round(min(late) / 1000, 1)]
        if max(late) > 2_000_000:
            out.append(f'IRQ0 lateness {max(late):.0f} ns > 2 ms')
    return out, summary


# ---- the RTC ----------------------------------------------------------------------------------------------------
def cmos_rd(i):
    return out(0x70, i) + '\n' + inb(0x71)


def cmos_wr(i, v):
    return out(0x70, i) + '\n' + out(0x71, v)


def poll(cond, ticks=3_000_000_000):
    """Loops `cond` (ends with a jump to 2f when done) at most `ticks` TSC ticks; r8 = 1 if it ended by the condition."""
    return '\n'.join([TSC, '    mov r9, rax', f'    mov r10, {ticks}', '    add r9, r10', '    xor r8d, r8d', '1:', cond,
                      TSC, '    cmp rax, r9', '    jb 1b', '    jmp 3f', '2:', '    mov r8d, 1', '3:'])


RTC_BODY = '\n'.join([cmos_rd(0x0c), '    mov [{data} + 0x400], rax', ring_body()])


def rtc_irq(b, n, k_count, k_c, mask):
    """Enables B = `b`, HLTs until n IRQ 8s, then B = 02H, C read, a drain; stores the count and the last C & mask."""
    c = COUNTS(0) + 8 * RTC_V
    return '\n'.join([cmos_rd(0x0c), f'    mov qword [{c:#x}], 0', f'    mov qword [{CURSOR:#x}], 0', TSC, store(RAW + 8),
                      cmos_wr(0x0b, b), halt_until(c, n), f'    mov rax, [{c:#x}]', store(k_count), '    mov rax, [{data} + 0x400]',
                      f'    and eax, {mask:#x}', store(k_c), cmos_wr(0x0b, 0x02), cmos_rd(0x0c), drain()])


def rtc_program(m='x2'):
    """Port 70H reads FFH and drops bit 7; A-D after reset; the time set under SET and read back; 23:59:58 31 Dec 1999
    rolling over to 00:00:00 1 Jan 2000 (century 20H, weekday 7); UIP set before an update and the seconds one later
    after it (raw: the seconds read during UIP may already be the new ones, QEMU's too); binary and 12-hour formats,
    re-encoded on a mode change (entering binary mode under SET reinterprets the BCD date: century 20H reads as 32, the
    year becomes 3200, as on the chip); index 37H = the century; CMOS RAM; IRQ 8 from
    the periodic (1024 Hz), update-ended and alarm (don't-care) interrupts, with C as their handler reads it."""
    t = [inb(0x70), store(0), out(0x70, 0x8d), inb(0x71), store(1), cmos_rd(0x0a), '    and eax, 0x7f', store(2), cmos_rd(0x0b), store(3),
         cmos_rd(0x0c), '    and eax, 0x80', store(4)]
    t += [cmos_wr(0x0b, 0x82)] + [cmos_wr(i, v) for i, v in ((0, 0x58), (2, 0x59), (4, 0x23), (7, 0x31), (8, 0x12), (9, 0x99), (0x32, 0x19))]
    t += [cmos_wr(0x0b, 0x02)] + [cmos_rd(i) + '\n' + store(5 + j) for j, i in enumerate((0, 2, 4, 7, 8, 9, 0x32))]
    t += [poll(cmos_rd(0) + '\n    test al, al\n    jz 2f'), store(RAW, 'r8')]
    t += [cmos_rd(i) + '\n' + store(12 + j) for j, i in enumerate((2, 4, 7, 8, 9, 0x32, 6))]
    t += [poll(cmos_rd(0x0a) + '\n    test al, 0x80\n    jnz 2f'), store(19, 'r8'), cmos_rd(0), '    mov esi, eax',
          poll(cmos_rd(0x0a) + '\n    test al, 0x80\n    jz 2f'), cmos_rd(0), '    sub eax, esi', store(RAW + 1)]
    t += [cmos_wr(0x0b, 0x84), cmos_wr(4, 0x81), cmos_wr(2, 30), cmos_wr(0, 0), cmos_wr(0x0b, 0x04), cmos_rd(4), store(21), cmos_rd(2), store(22),
          cmos_wr(0x0b, 0x06), cmos_rd(4), store(23), cmos_wr(0x0b, 0x02), cmos_rd(4), store(24)]
    t += [out(0x70, 0x37), inb(0x71), store(25), cmos_wr(0x40, 0x5a), cmos_rd(0x40), store(26)]
    t += [entry(8, RTC_V), cmos_wr(0x0a, 0x26), rtc_irq(0x42, 16, 27, 28, 0xc0)]
    t += [rtc_irq(0x12, 2, 29, 30, 0x90)]
    t += [cmos_wr(1, 0xff), cmos_wr(3, 0xff), cmos_wr(5, 0xff), rtc_irq(0x22, 1, 31, 32, 0xb0), entry(8, 0x10000 | RTC_V)]
    text = head(m) + '\n' + '\n'.join(t)
    return build(f'l4b-rtc-{m}', m, [text], [RTC_V], {RTC_V: RTC_BODY}, capture_ring=True)


RTC_WANT = {0: 0xff, 1: 0x80, 2: 0x26, 3: 0x02, 4: 0, 5: 0x58, 6: 0x59, 7: 0x23, 8: 0x31, 9: 0x12, 10: 0x99, 11: 0x19,
            12: 0, 13: 0, 14: 1, 15: 1, 16: 0, 17: 0x20, 18: 7, 19: 1, 21: 0x81, 22: 30, 23: 13, 24: 0x13, 25: 0x32, 26: 0x5a,
            27: 16, 28: 0xc0, 29: 2, 30: 0x90, 31: 1, 32: 0xb0}


def check_rtc(mem, natemu):
    out = [f'k{k}: {mem.read(DATA(0) + 8 * k):#x} != model {v:#x}' for k, v in RTC_WANT.items() if mem.read(DATA(0) + 8 * k) != v]
    return out, {}


# ---- the ACPI PM block ------------------------------------------------------------------------------------------
# QEMU's PIIX4 PM (PCI 00:01.3) decodes its block only once PMBA (config 40H) is set and PMIOSE (config 80H bit 0) on;
# the programs do that first through 0CF8H/0CFCH (natemu has no PCI: those ports are unassigned, the writes ignored).
PIIX4_PM_ON = '\n'.join([f'    mov edx, 0xcf8\n    mov eax, {a:#x}\n    out dx, eax\n    mov edx, 0xcfc\n    mov eax, {v:#x}\n    out dx, eax'
                         for a, v in ((0x80000b40, 0xb001), (0x80000b80, 1))])
PM_BODY = '\n'.join([inw(0xb000), '    mov [{data} + 0x400], rax', outw(0xb000, 1)])


def pm_program(m='x2'):
    """ACPI mode (QEMU: 0F1H to the APM command port B2H, which natemu leaves unassigned; natemu is always ACPI-only),
    PM1_CNT (SCI_EN), PM1_EN read-back, PM1_STS write-1-to-clear, the PM timer (24 bits, monotonic, its rate against the
    TSC: natemu's exact model), and the PM timer's SCI: TMR_EN, IRQ 9 level through I/O APIC pin 9, the handler reading
    TMR_STS and clearing it (one delivery)."""
    t = [PIIX4_PM_ON, out(0xb2, 0xf1), inw(0xb004), store(0)]
    for k, v in ((1, 0x0521), (2, 0xffff), (3, 0)):
        t += [outw(0xb002, v), inw(0xb002), store(k)]
    t += [outw(0xb000, 0xffff), inw(0xb000), store(4)]
    sample = lambda j: [TSC, store(RAW + 3 * j), inl(0xb008), store(RAW + 3 * j + 1), TSC, store(RAW + 3 * j + 2)]
    t += [rounds('\n'.join(sample(0) + sample(1) + sample(2) + [wait_tsc(10_000_000)] + sample(3)))]
    t += ['    mov eax, [{data} + %d]' % (8 * (RAW + 1)), '    mov ecx, [{data} + %d]' % (8 * (RAW + 4)), '    mov esi, [{data} + %d]' % (8 * (RAW + 7)),
          '    xor edi, edi', '    cmp eax, ecx', '    ja 1f', '    cmp ecx, esi', '    ja 1f', '    mov edi, 1', '1:', store(5, 'rdi'),
          '    mov eax, [{data} + %d]' % (8 * (RAW + 10)), '    shr eax, 24', store(6)]
    c = COUNTS(0) + 8 * SCI_V
    t += [entry(9, 0x8000 | SCI_V), outw(0xb000, 0xffff), f'    mov qword [{c:#x}], 0', outw(0xb002, 1), '    sti',
          poll(f'    cmp qword [{c:#x}], 1\n    jae 2f', 5_000_000_000), '    cli', outw(0xb002, 0),
          drain(), f'    mov rax, [{c:#x}]', store(7), '    mov rax, [{data} + 0x400]', '    and eax, 1', store(8), entry(9, 0x18000 | SCI_V)]
    text = head(m) + '\n' + '\n'.join(t)
    return build(f'l4b-pm-{m}', m, [text], [SCI_V], {SCI_V: PM_BODY})


PM_WANT = {0: 1, 1: 0x0521, 2: 0xffff, 3: 0, 4: 0, 5: 1, 6: 0, 7: 1, 8: 1}


def check_pm(mem, natemu):
    out = [f'k{k}: {mem.read(DATA(0) + 8 * k):#x} != model {v:#x}' for k, v in PM_WANT.items() if mem.read(DATA(0) + 8 * k) != v]
    if not natemu:
        return out, {}
    raw = lambda j: mem.read(DATA(0) + 8 * (RAW + j))
    # Each pair of PM reads: the ticks between them lie within one tick of the exact rate over the TSC windows around.
    samples = [(raw(3 * j), raw(3 * j + 1), raw(3 * j + 2)) for j in range(4)]
    for j in range(1, 4):
        (b0, p0, a0), (b1, p1, a1) = samples[0], samples[j]
        d = (p1 - p0) % (1 << 24)
        lo, hi = Fraction(b1 - a0) * PM_HZ / NS - 1, Fraction(a1 - b0) * PM_HZ / NS + 1
        if not lo <= d <= hi:
            out.append(f'PM timer: {d} ticks between samples 0 and {j}, model [{float(lo):.1f}, {float(hi):.1f}]')
    return out, {'pm_ticks_10ms': (samples[3][1] - samples[0][1]) % (1 << 24)}


def s5_program(m='x2'):
    """SLP_TYP 0 with SLP_EN: QEMU powers off (no dump); natemu stops `poweroff` at the OUT."""
    return build(f'l4b-s5-{m}', m, [head(m) + '\n' + PIIX4_PM_ON + '\n' + out(0xb2, 0xf1) + '\n' + outw(0xb004, 0x2001) + '\n' + store(0)], [])


# ---- natemu only ------------------------------------------------------------------------------------------------
def halt_program(unmasked):
    """The halted rule with the PIT as an asynchronous source: channel 0 one-shot (mode 0, FFFFH: 55 ms), I/O APIC pin 2
    unmasked (the vCPU is woken: it reaches its end) or masked (after the terminal count nothing can fire: the driver
    ends it `halted`)."""
    m = 'x'
    t = [map_pages(), entry(2, IRQ0_V if unmasked else 0x10000 | IRQ0_V), out(0x43, 0x30), out(0x40, 0xff), out(0x40, 0xff),
         halt_until(COUNTS(0) + 8 * IRQ0_V, 1)]
    return build(f"l4b-halt-{'unmasked' if unmasked else 'masked'}", m, ['\n'.join(t)], [IRQ0_V])


WARM = 50


def stress_program(n, k=200):
    """Asynchronous sources with HLT at n CPUs (xAPIC, physical broadcast FFH): IRQ 0 (PIT mode 2, 1193: ~1 kHz, vector
    40H) and IRQ 8 (RTC periodic, 1024 Hz, vector 48H; every CPU's handler reads C, which lowers IRQ 8 for the next
    edge) reach every CPU; each HLTs until it took k of each, then counts itself done; CPU 0 then stops both sources
    (Linux's PIT-disable sequence, B = 02H). DATA(i): its counts, the TSC of its last IRQ 0 and of its WARM-th (the
    deliveries before it pay the first translations, during which edges merge)."""
    m, done = 'x', SHARED + 0x100
    cnt = lambda i, v: COUNTS(i) + 8 * v
    texts = []
    for i in range(n):
        t = ([map_pages(), entry(2, IRQ0_V, 0xff << 24), entry(8, RTC_V, 0xff << 24), cmos_wr(0x0a, 0x26), cmos_rd(0x0c), cmos_wr(0x0b, 0x42),
              TSC, '    mov [{data} + 0x18], rax', out(0x43, 0x34), out(0x40, 0xa9), out(0x40, 0x04)] if i == 0 else [])
        t += ['1:', '    cli', f'    cmp qword [{cnt(i, IRQ0_V):#x}], {k}', '    jb 2f', f'    cmp qword [{cnt(i, RTC_V):#x}], {k}', '    jae 3f',
              '2:', '    sti', '    hlt', '    jmp 1b', '3:', f'    lock inc qword [{done:#x}]']
        if i == 0:
            t += ['4:', '    pause', f'    cmp qword [{done:#x}], {n}', '    jb 4b', out(0x43, 0x30), out(0x40, 0), out(0x40, 0), out(0x43, 0x30),
                  cmos_wr(0x0b, 0x02), cmos_rd(0x0c), entry(2, 0x10000 | IRQ0_V), entry(8, 0x10000 | RTC_V)]
        t += [f'    mov rax, [{cnt(i, IRQ0_V):#x}]', store(0), f'    mov rax, [{cnt(i, RTC_V):#x}]', store(1)]
        texts.append('\n'.join(t))
    body = {IRQ0_V: '\n'.join([TSC, '    mov [{data} + 0x10], rax', f'    cmp qword [{{counts}} + {8 * IRQ0_V}], {WARM}', '    jne 1f',
                                '    mov [{data} + 0x20], rax', '1:']), RTC_V: '\n'.join([out(0x70, 0x0c), inb(0x71)])}
    return build('l4b-stress', m, texts, [IRQ0_V, RTC_V], body)


SAMPLES, SPACING = 400, 25_000_000


def clocks_program(m='x2'):
    """Clock agreement over 10 s (natemu only): SAMPLES samples SPACING TSC ticks apart, each [TSC, PM timer, TSC, PIT
    channel 2 (mode 2, count 10000H, latched), TSC, LAPIC current count (one-shot FFFFFFFFH, divide by 1, masked), TSC,
    RTC seconds, TSC] at RING(0) + 72 j: every read between its own two TSCs."""
    def read(at, text):
        return [text, f'    mov [r8 + {at}], rax', TSC, f'    mov [r8 + {at + 8}], rax']
    t = [out(0x61, 1), out(0x43, 0xb4), out(0x42, 0), out(0x42, 0), apic.wr(m, apic.DCR, 0xb), apic.wr(m, apic.LVT_TIMER, 0x10040),
         apic.wr(m, apic.INIT_COUNT, 0xffffffff), f'    mov r8, {RING(0):#x}', f'    mov r12d, {SAMPLES}', 'cl_loop:', TSC, '    mov [r8], rax']
    t += read(8, inl(0xb008))
    t += read(24, '\n'.join([out(0x43, 0x80), inb(0x42), '    mov ecx, eax', inb(0x42), '    shl eax, 8', '    or eax, ecx']))
    t += read(40, apic.rd(m, apic.CUR_COUNT)) + read(56, cmos_rd(0))
    t += [wait_tsc(SPACING), '    add r8, 72', '    dec r12d', '    jnz cl_loop']
    return build(f'l4b-clocks-{m}', m, [head(m) + '\n' + '\n'.join(t)], [], capture_ring=True)


def check_clocks(mem):
    """Every sample against sample 1 (sample 0 pays first translations): each clock's ticks between them within one tick
    of the exact count over its TSC windows (TSC = V at 1 GHz; PM, PIT and the crystal from the same V): d in
    [rate x (before_j - after_1) - 1, rate x (after_j - before_1) + 1]. The PIT's 16-bit and the PM timer's 24-bit counts
    are unwrapped (samples 25 ms apart; periods 54.9 ms and 4.7 s); the RTC's seconds change once a second (their TSCs
    one second apart within the sample spacing). Reports the widest window (ticks) and the worst excess (< 0: inside)."""
    s = [[mem.read(RING(0) + 72 * j + 8 * f) for f in range(9)] for j in range(SAMPLES)]
    def unwrap(step):
        total, out = 0, [0]
        for j in range(1, SAMPLES):
            total += step(j)
            out.append(total)
        return out
    pit = unwrap(lambda j: (s[j - 1][3] - s[j][3]) % 0x10000)
    pm = unwrap(lambda j: (s[j][1] - s[j - 1][1]) % (1 << 24))
    # name: (rate, value(j), index of the TSC before the read)
    clocks = {'pm': (PM_HZ, lambda j: pm[j], 0), 'pit': (PIT_HZ, lambda j: pit[j], 2), 'lapic': (24_000_000, lambda j: -s[j][5], 4)}
    out, worst, window = [], {}, {}
    for name, (hz, value, b) in clocks.items():
        for j in range(2, SAMPLES):
            d = value(j) - value(1)
            lo, hi = Fraction(s[j][b] - s[1][b + 2]) * hz / NS - 1, Fraction(s[j][b + 2] - s[1][b]) * hz / NS + 1
            excess = max(lo - d, d - hi)
            worst[name] = max(worst.get(name, -10**9), float(excess))
            window[name] = max(window.get(name, 0), float(Fraction(s[j][b + 2] - s[j][b]) * hz / NS))
            if excess > 0:
                out.append(f'{name}: {d} ticks between samples 1 and {j}, exact within one: [{float(lo):.2f}, {float(hi):.2f}]')
                break
    changes = [s[j][6] for j in range(1, SAMPLES) if s[j][7] != s[j - 1][7]]
    for a, b in zip(changes, changes[1:]):
        if abs((b - a) - TSC_HZ) > 2 * SPACING:
            out.append(f'rtc: seconds changed {b - a} ns apart')
    span = (s[-1][8] - s[1][0]) / 1e9
    return out, {'seconds': round(span, 2), 'widest_read_window_ticks': {k: round(v, 1) for k, v in window.items()},
                 'worst_excess_ticks': {k: round(v, 3) for k, v in worst.items()}, 'rtc_changes': len(changes)}


# ---- review round: more PIT modes, the RTC's period change and 12-hour alarm, the lost-tick policies ---------------
def more_program(m='x2'):
    """QEMU-compared: PIT channel 0 mode 4 (Linux's one-shot, 38H; count 1193: one IRQ 0 through pin 2, status read-backs
    38H before and after the strobe); channel 2 mode 1 (B2H: status 32H, OUT2 high once the count has run after a gate
    rise on 61H) and mode 5 (BAH: status 3AH; both count 10000H, 55 ms, so a first translation cannot pass it; OUT2 low before and 1 ms after the gate rise: high only at d = count);
    the RTC's period change (QEMU's `periodic_timer_update` lost clock: from rate 15 (0.5 s) to 14 (0.25 s) 100 ms
    after a periodic interrupt, the next comes 0.25 s after that interrupt, not 0.25 s after the change; k8, the raw gap
    at RAW + 1). k9 (natemu only, departure: QEMU's 12-hour alarm decode): 11:59:59 AM, alarm 12:00:00 PM, AF within
    3 s (the datasheet's 1 s; QEMU's decode puts the alarm at 8 AM the next day)."""
    c0 = COUNTS(0) + 8 * IRQ0_V
    t = [entry(2, IRQ0_V), f'    mov qword [{c0:#x}], 0', out(0x43, 0x38), out(0x40, 0xa9), out(0x40, 0x04), out(0x43, 0xe2), inb(0x40), store(0),
         '    sti', wait_tsc(10_000_000), '    cli', f'    mov rax, [{c0:#x}]', store(1), out(0x43, 0xe2), inb(0x40), store(2),
         entry(2, 0x10000 | IRQ0_V)]
    t += [out(0x61, 0), out(0x43, 0xb2), out(0x42, 0), out(0x42, 0), out(0x43, 0xe8), inb(0x42), store(3), out(0x61, 1),
          poll(inb(0x61) + '\n    test al, 0x20\n    jnz 2f', 200_000_000), store(4, 'r8')]
    t += [out(0x61, 0), out(0x43, 0xba), out(0x42, 0), out(0x42, 0), out(0x43, 0xe8), inb(0x42), store(5), out(0x61, 1),
          inb(0x61), '    shr eax, 5', '    and eax, 1', store(6), wait_tsc(1_000_000), inb(0x61), '    shr eax, 5', '    and eax, 1', store(7), out(0x61, 0)]
    c8 = COUNTS(0) + 8 * RTC_V
    t += [entry(8, RTC_V), cmos_rd(0x0c), f'    mov qword [{c8:#x}], 0', f'    mov qword [{CURSOR:#x}], 0', cmos_wr(0x0a, 0x2f), cmos_wr(0x0b, 0x42),
          halt_until(c8, 2), f'    mov rax, [{RING(0) + 8:#x}]', '    mov r12, rax', '    add rax, 100000000', '    mov rbx, rax',
          '1:', '    pause', TSC, '    cmp rax, rbx', '    jb 1b', cmos_wr(0x0a, 0x2e), halt_until(c8, 3),
          f'    mov rax, [{RING(0) + 16:#x}]', '    sub rax, r12', store(RAW + 1), '    sub rax, 250000000', '    jns 2f', '    neg rax', '2:',
          '    xor ecx, ecx', '    cmp rax, 20000000', '    setb cl', store(8, 'rcx'), cmos_wr(0x0b, 0x02), cmos_rd(0x0c), drain(), entry(8, 0x10000 | RTC_V)]
    t += [cmos_wr(0x0b, 0x80)] + [cmos_wr(i, v) for i, v in ((4, 0x11), (2, 0x59), (0, 0x59), (5, 0x92), (3, 0), (1, 0))]
    t += [cmos_wr(0x0b, 0x00), cmos_rd(0x0c), poll(cmos_rd(0x0c) + '\n    test al, 0x20\n    jnz 2f'), store(9, 'r8'), cmos_wr(0x0b, 0x02)]
    text = head(m) + '\n' + '\n'.join(t)
    return build(f'l4b-more-{m}', m, [text], [IRQ0_V, RTC_V], {RTC_V: RTC_BODY}, capture_ring=True)


MORE_WANT = {0: 0x38, 1: 1, 2: 0x38, 3: 0x32, 4: 1, 5: 0x3a, 6: 0, 7: 0, 8: 1, 9: 1}
MORE_DEPARTURES = {9}  # the 12-hour alarm, fixed per the datasheet (QEMU: 0)


def check_more(mem, natemu):
    want = MORE_WANT if natemu else {k: v for k, v in MORE_WANT.items() if k not in MORE_DEPARTURES}
    return [f'k{k}: {mem.read(DATA(0) + 8 * k):#x} != model {v:#x}' for k, v in want.items() if mem.read(DATA(0) + 8 * k) != v], \
        {'rtc_period_change_gap_ms': round(mem.read(DATA(0) + 8 * (RAW + 1)) / 1e6, 2)}


def modes67_program():
    """natemu only (8254 datasheet: M2..M0 = X10 is mode 2, X11 mode 3; QEMU computes OUT for 6 and 7 as mode 0, one
    edge): channel 0 with control word 3CH (mode 6) then 3EH (mode 7), count 1193 (~1 ms), 20 ms each: IRQ 0 every
    period (k0, k1: deliveries; k2, k3: the read-back status keeps M2..M0 as written)."""
    m, c0 = 'x2', COUNTS(0) + 8 * IRQ0_V
    t = [entry(2, IRQ0_V)]
    for k, cw in enumerate((0x3c, 0x3e)):
        t += [f'    mov qword [{c0:#x}], 0', out(0x43, cw), out(0x40, 0xa9), out(0x40, 0x04), out(0x43, 0xe2), inb(0x40), '    and eax, 0x7f', store(2 + k),
              '    sti', wait_tsc(20_000_000), '    cli', f'    mov rax, [{c0:#x}]', store(k), out(0x43, 0x30), out(0x40, 0), out(0x40, 0), drain()]
    t += [entry(2, 0x10000 | IRQ0_V)]
    return build('l4b-modes67', m, [head(m) + '\n' + '\n'.join(t)], [IRQ0_V])


def policy_program(source):
    """natemu only, the lost-tick policies: a periodic source at ~1 kHz (`pit`: mode 2, 1193; `lapic`: periodic, 24,000
    crystal ticks; `rtc`: rate 6, 1024 Hz), 20 ms with IF = 0 (the vCPU cannot take it), 20 ms with IF = 1. k0: the
    deliveries in all; raw: the time from the start and from the STI to the end (second of two rounds). Reinject: about
    one delivery per period from the start (the ~19 owed ones follow, one per acceptance); discard: about one per
    period of the IF = 1 window, plus the one merged pending at the STI."""
    m = 'x2'
    v = {'pit': IRQ0_V, 'rtc': RTC_V, 'lapic': 0x41}[source]
    c = COUNTS(0) + 8 * v
    start = {'pit': [entry(2, IRQ0_V), out(0x43, 0x34), out(0x40, 0xa9), out(0x40, 0x04)],
             'rtc': [entry(8, RTC_V), cmos_rd(0x0c), cmos_wr(0x0a, 0x26), cmos_wr(0x0b, 0x42)],
             'lapic': [apic.wr(m, apic.DCR, 0xb), apic.wr(m, apic.LVT_TIMER, 0x20041), apic.wr(m, apic.INIT_COUNT, 24000)]}[source]
    stop = {'pit': [out(0x43, 0x30), out(0x40, 0), out(0x40, 0), entry(2, 0x10000 | IRQ0_V)],
            'rtc': [cmos_wr(0x0b, 0x02), cmos_rd(0x0c), entry(8, 0x10000 | RTC_V)],
            'lapic': [apic.wr(m, apic.LVT_TIMER, 0x10041)]}[source]
    body = {RTC_V: '\n'.join([cmos_rd(0x0c)])}.get(v, '    nop')
    t = [f'    mov qword [{c:#x}], 0', TSC, '    mov r12, rax'] + start + [wait_tsc(20_000_000), TSC, '    mov r14, rax', '    sti',
         wait_tsc(20_000_000), '    cli', TSC, '    mov rbx, rax', '    sub rax, r12', store(RAW), '    mov rax, rbx', '    sub rax, r14', store(RAW + 1),
         f'    mov rax, [{c:#x}]', store(0)] + stop + [drain()]
    # Two rounds: the first translates the handler and the loops (a first translation holds the vCPU for milliseconds).
    return build(f'l4b-policy-{source}', m, [head(m) + '\n' + rounds('\n'.join(t))], [v], {v: body})


POLICY_PERIOD_NS = {'pit': Fraction(1193 * TSC_HZ, PIT_HZ), 'rtc': Fraction(TSC_HZ, 1024), 'lapic': Fraction(1_000_000)}

"""The expected final state of the L3 `qemu` programs (tests/system/run_l3.py; tools/bench/smp/ports.py), written from the
programs: their text runs here instruction by instruction against a 16550A (the National Semiconductor/TI PC16550D
datasheet's registers: DLAB, IER, IIR priorities, FCR and the 16-byte FIFOs with their trigger level, LCR, MCR loopback,
LSR DR/OE/THRE/TEMT, MSR, SCR), with
- a transmitter that is never busy (no line timing is visible to these programs: every wait polls LSR),
- unassigned ports reading all ones and ignoring writes (the PC rule the runner judges by),
- the I/O permission rules (SDM Vol.1 19.5: CPL > IOPL consults the TSS bitmap, two bytes, within the TSS limit; INS/OUTS
  check before their first element, even with a zero count) and #PF midway through a REP string (SDM Vol.2 REP),
- X2's event harness (tools/bench/smp/events.py): an exception is logged as (vector, error code, RIP, RFLAGS & 0x10ed5
  with RF set: a fault, r12, RCX) and resumed at r13.
`expected(p)` -> (state as smp.parse_dump gives it, the bytes COM1 sent). Equal to QEMU 11.1.2's answers
(tests/system/test_l3_expected.py, tools/reference/crosscheck/l3.json)."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import pathlib, re, sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'tools' / 'reference'), str(ROOT / 'tools' / 'bench' / 'smp')]
import events, expect, ports, smp  # noqa: E402
from programs import ref_assemble  # noqa: E402

M = (1 << 64) - 1
REGS = smp.REGS
R32 = {'eax': 0, 'ecx': 1, 'edx': 2, 'ebx': 3, 'esp': 4, 'ebp': 5, 'esi': 6, 'edi': 7}
R16 = {'ax': 0, 'cx': 1, 'dx': 2, 'bx': 3}
R8 = {'al': 0, 'cl': 1, 'dl': 2, 'bl': 3}
CF, PF, AF, ZF, SF, DF, OF, IF, RF = 0x1, 0x4, 0x10, 0x40, 0x80, 0x400, 0x800, 0x200, 0x10000
COM1 = ports.COM1


class Uart:
    """COM1's 16550A. `fresh`: the state the programs that do not set it first find (the firmware's console: DLAB
    clear, no interrupt enabled, FIFOs off, nothing received)."""
    def __init__(self):
        self.dll, self.dlm, self.ier, self.fcr, self.lcr, self.mcr, self.scr = 0x0c, 0, 0, 0, 0x03, 0x08, 0
        self.lsr, self.msr, self.rbr = 0x60, 0xb0, 0                  # THRE, TEMT; DCD, DSR, CTS
        self.rx, self.thr_pending, self.itl, self.sent = [], True, 1, bytearray()

    def iir(self):
        """The highest-priority pending interrupt (datasheet Table 4): line status, received data (FIFO: at the trigger
        level), THR empty, modem status; FIFO-enabled bits 7:6."""
        fifo = 0xc0 if self.fcr & 1 else 0
        if self.ier & 4 and self.lsr & 0x1e: return fifo | 0x06
        if self.ier & 1 and self.lsr & 1 and (not self.fcr & 1 or len(self.rx) >= self.itl): return fifo | 0x04
        if self.ier & 2 and self.thr_pending: return fifo | 0x02
        if self.ier & 8 and self.msr & 0x0f: return fifo | 0x00
        return fifo | 0x01

    def receive(self, b):
        if self.fcr & 1:
            if len(self.rx) < 16: self.rx.append(b)
            else: self.lsr |= 0x02                                   # overrun: the FIFO keeps its bytes
        else:
            if self.lsr & 1: self.lsr |= 0x02
            self.rbr = b
        self.lsr |= 0x01

    def read(self, off):
        if off == 0:
            if self.lcr & 0x80: return self.dll
            if self.fcr & 1:
                v = self.rx.pop(0) if self.rx else 0
                if not self.rx: self.lsr &= ~0x11
                return v
            self.lsr &= ~0x11
            return self.rbr
        if off == 1: return self.dlm if self.lcr & 0x80 else self.ier
        if off == 2:
            v = self.iir()
            if v & 0x0f == 0x02: self.thr_pending = False             # reading IIR clears a THR-empty interrupt
            return v
        if off == 3: return self.lcr
        if off == 4: return self.mcr
        if off == 5:
            v = self.lsr
            self.lsr &= ~0x12                                        # OE and BI clear when LSR is read
            return v
        if off == 6:
            if self.mcr & 0x10:                                      # loopback: the modem outputs as the inputs
                return (self.mcr & 0x0c) << 4 | (self.mcr & 0x02) << 3 | (self.mcr & 0x01) << 5
            v = self.msr; self.msr &= 0xf0
            return v
        return self.scr

    def write(self, off, v):
        if off == 0:
            if self.lcr & 0x80: self.dll = v; return
            # The transmitter takes the byte at once (THRE, TEMT set again); in loopback it is received.
            self.thr_pending = True
            if self.mcr & 0x10: self.receive(v)
            else: self.sent.append(v)
        elif off == 1:
            if self.lcr & 0x80: self.dlm = v; return
            changed, self.ier = (self.ier ^ v) & 0x0f, v & 0x0f
            if changed & 2: self.thr_pending = bool(self.ier & 2 and self.lsr & 0x20)   # enabling it with THR empty raises it
        elif off == 2:
            if (v ^ self.fcr) & 1: v |= 0x06                         # FIFO enable changed: both FIFOs reset
            if v & 0x02: self.rx = []; self.lsr &= ~0x11
            if v & 0x04: self.thr_pending = True
            self.fcr = v & 0xc9
            if v & 1: self.itl = {0: 1, 0x40: 4, 0x80: 8, 0xc0: 14}[v & 0xc0]
        elif off == 3: self.lcr = v
        elif off == 4: self.mcr = v & 0x1f
        elif off == 7: self.scr = v                                  # LSR and MSR writes have no effect


class Fault(Exception):
    def __init__(self, vector, error=0, cr2=None):
        self.vector, self.error, self.cr2 = vector, error, cr2


class Machine:
    def __init__(self, p, uart):
        q = p['qemu']
        self.g = [smp.num(x) for x in q['cpus'][0]['gpr']]
        self.flags, self.cpl, self.iopl = 0x2, 0, 3                  # the stub's start: IOPL 3, IF 0
        self.mem = [(smp.num(r['addr']), bytearray(smp.region_bytes(r))) for r in q['memory']] \
            + [(smp.num(b['addr']), bytearray.fromhex(b['code'])) for b in q['bodies']]     # (OUTS reads the code's own bytes)
        self.uart, self.msr, self.paging, self.cr2 = uart, {0xc0000080: 0xd00}, False, 0

    # registers
    def get(self, r):
        if r in REGS: return self.g[REGS.index(r)]
        if r in R32: return self.g[R32[r]] & 0xffffffff
        if r in R16: return self.g[R16[r]] & 0xffff
        if r in R8: return self.g[R8[r]] & 0xff
        raise KeyError(r)

    def set(self, r, v):
        if r in REGS: self.g[REGS.index(r)] = v & M
        elif r in R32: self.g[R32[r]] = v & 0xffffffff
        elif r in R16: k = R16[r]; self.g[k] = self.g[k] & ~0xffff | v & 0xffff
        elif r in R8: k = R8[r]; self.g[k] = self.g[k] & ~0xff | v & 0xff
        else: raise KeyError(r)

    def width(self, r): return 64 if r in REGS else 32 if r in R32 else 16 if r in R16 else 8

    # memory (linear = physical; under the program's own tables NP is not present)
    def where(self, a, n, write):
        if self.paging and ports.NP <= a + n - 1 and a < ports.NP + 0x1000:
            raise Fault(14, (2 if write else 0) | (4 if self.cpl == 3 else 0), max(a, ports.NP))
        for base, d in self.mem:
            if base <= a and a + n <= base + len(d): return d, a - base
        raise KeyError(hex(a))

    def load(self, a, n=8):
        d, o = self.where(a, n, False); return int.from_bytes(d[o:o + n], 'little')

    def store(self, a, v, n=8):
        d, o = self.where(a, n, True); d[o:o + n] = (v & ((1 << 8 * n) - 1)).to_bytes(n, 'little')

    def szp(self, r, bits, keep=0):
        self.flags = self.flags & ~(CF | PF | AF | ZF | SF | OF) | keep | (ZF if r == 0 else 0) | (SF if r >> (bits - 1) & 1 else 0) \
            | (PF if bin(r & 0xff).count('1') % 2 == 0 else 0)

    # ports
    def permitted(self, port, n):
        if self.cpl <= self.iopl: return True
        tss = self.load(ports.GDT + 0x28) >> 16 & 0xffffff | (self.load(ports.GDT + 0x28) >> 56) << 24 | self.load(ports.GDT + 0x30) << 32
        lim = self.load(ports.GDT + 0x28) & 0xffff | (self.load(ports.GDT + 0x28) >> 48 & 0xf) << 16
        base = self.load(tss + 0x66, 2)
        at = base + port // 8
        if at + 1 > lim: return False                               # both bytes of the bitmap word must lie within the limit
        bits = self.load(tss + at, 2)
        return not any(bits >> (port % 8 + k) & 1 for k in range(n))

    def port_in(self, port, n):
        if not self.permitted(port, n): raise Fault(13)
        return int.from_bytes(bytes(self.uart.read(p - COM1) if COM1 <= p < COM1 + 8 else 0xff
                                    for p in range(port, port + n)), 'little')

    def port_out(self, port, n, v):
        if not self.permitted(port, n): raise Fault(13)
        for k, p in enumerate(range(port, port + n)):
            if COM1 <= p < COM1 + 8: self.uart.write(p - COM1, v >> 8 * k & 0xff)


SIZE = {'b': 1, 'w': 2, 'd': 4}


def program_text(p):
    """CPU 0's whole text as QEMU runs it (events.source + expand), with the per-line addresses."""
    body = p['body'].replace('{data}', f'{events.DATA(0):#x}')
    text, _ = events.expand(events.source(body, 0), 0, True)
    lines = text.splitlines()
    marked = '\n'.join(f'zz_{n}:\n{l}' if l.strip() and not l.strip().endswith(':') else l for n, l in enumerate(lines))
    _, lab = ref_assemble(marked)
    addr = {int(k[3:]): events.CODE(0) + v for k, v in lab.items() if k.startswith('zz_')}
    labels = {l.strip()[:-1]: n for n, l in enumerate(lines) if l.strip().endswith(':')}
    return lines, addr, labels, {k: events.CODE(0) + v for k, v in lab.items() if not k.startswith('zz_')}


def run(p):
    lines, addr, labels, where = program_text(p)
    at_line = {a: n for n, a in addr.items()}
    for name, n in labels.items():
        at_line.setdefault(where[name], n)
    u = Uart()
    m = Machine(p, u)
    log_at, nlog = events.LOG(0), 0
    n = 0
    end = labels['ev_end']
    while n < len(lines):
        line = lines[n].strip()
        if not line or line.endswith(':'): n += 1; continue
        if n == end or line.startswith('ev_common'): break
        try:
            nxt = step(m, line, labels, where, addr, n)
        except Fault as f:
            # X2's harness: the log entry (vector, error, RIP, RFLAGS & 0x10ed5 with RF set, r12, RCX, active), resumed at r13.
            if f.cr2 is not None: m.cr2 = f.cr2
            entry = [f.vector, f.error, addr[n], (m.flags | RF) & 0x10ed5, m.get('r12'), m.get('rcx'), 0]
            for k, v in enumerate(entry): m.store(log_at + 0x40 * nlog + 8 * k, v)
            nlog += 1
            nxt = at_line[m.get('r13')]
        if nxt == 'end': break
        n = n + 1 if nxt is None else nxt
    m.set('r11', m.get('r11'))
    m.flags = 0x46                                                  # ev_end: cmp r11, r11
    return m


def operand_value(m, s):
    s = s.strip()
    if s in REGS or s in R32 or s in R16 or s in R8: return m.get(s)
    return int(s, 0) & M


def step(m, line, labels, where, addr, n):
    """One instruction; returns the next line index, None (the next line) or 'end'."""
    line = re.sub(r'\s+', ' ', line)
    w = line.split(' ', 1)
    op, rest = w[0], (w[1] if len(w) > 1 else '')
    args = [a.strip() for a in re.split(r',(?![^\[]*\])', rest)] if rest else []
    ip_next = lambda: addr[next(k for k in sorted(addr) if k > n)]
    if op in ('lidt', 'cli', 'lgdt', 'ltr', 'nop', 'sfence', 'mfence'): return None
    if op == 'jmp':
        if args[0] == 'ev_end': return 'end'
        return labels[args[0]]
    if op in ('jne', 'jz', 'je', 'jnz'):
        taken = bool(m.flags & ZF) == (op in ('jz', 'je'))
        return labels[args[0]] if taken else None
    if op == 'std': m.flags |= DF; return None
    if op == 'cld': m.flags &= ~DF; return None
    if op == 'lea':
        src = args[1]
        mm = re.fullmatch(r'\[rel (\w+)\]', src)
        v = where[mm.group(1)] if mm else ea(m, src[1:-1])
        m.set(args[0], v); return None
    if op == 'mov':
        d, s = args
        if s.startswith('cr'): m.set(d, m.cr2 if s == 'cr2' else 0); return None
        if d.startswith('cr'):
            if d == 'cr3': m.paging = m.get(s) == ports.TABLES
            return None
        if d.startswith('['):
            reg = s; m.store(ea(m, d[1:-1]), m.get(reg), m.width(reg) // 8); return None
        if s.startswith('['): m.set(d, m.load(ea(m, s[1:-1]), m.width(d) // 8)); return None
        if d in ('ss', 'ds', 'es', 'fs', 'gs'): return None
        if s in ('ss', 'ds'): m.set(d, 0x10); return None
        m.set(d, operand_value(m, s)); return None
    if op in ('add', 'and', 'or', 'xor', 'cmp', 'test', 'shr'):
        d, s = args
        bits, a, b = m.width(d), m.get(d), operand_value(m, s)
        mask = (1 << bits) - 1; b &= mask
        if op == 'add':
            r = (a + b) & mask
            m.szp(r, bits, (CF if a + b > mask else 0) | (AF if (a ^ b ^ r) & 0x10 else 0) | (OF if (~(a ^ b) & (a ^ r)) >> (bits - 1) & 1 else 0))
        elif op == 'cmp':
            r = (a - b) & mask
            m.szp(r, bits, (CF if a < b else 0) | (AF if (a ^ b ^ r) & 0x10 else 0) | (OF if ((a ^ b) & (a ^ r)) >> (bits - 1) & 1 else 0))
            return None
        elif op == 'shr':
            r = a >> b; m.szp(r, bits, (CF if a >> (b - 1) & 1 else 0)); m.set(d, r); return None
        else:
            r = a & b if op in ('and', 'test') else a | b if op == 'or' else a ^ b
            m.szp(r, bits)                                          # CF, OF clear; AF undefined (left clear)
            if op == 'test': return None
        m.set(d, r); return None
    if op == 'inc':
        r = (m.get(args[0]) + 1) & ((1 << m.width(args[0])) - 1)
        m.szp(r, m.width(args[0]), m.flags & CF | (AF if r & 0xf == 0 else 0)); m.set(args[0], r); return None
    if op == 'push':
        v = m.flags & ~RF if args[0] == 'rflags' else operand_value(m, args[0])
        m.set('rsp', m.get('rsp') - 8); m.store(m.get('rsp'), v); return None
    if op == 'pushfq':
        m.set('rsp', m.get('rsp') - 8); m.store(m.get('rsp'), m.flags | m.iopl << 12); return None
    if op == 'pop':
        m.set(args[0], m.load(m.get('rsp'))); m.set('rsp', m.get('rsp') + 8); return None
    if op == 'popfq':
        v = m.load(m.get('rsp')); m.set('rsp', m.get('rsp') + 8)
        m.flags = v & ~0x3000 | 2
        if m.cpl == 0: m.iopl = v >> 12 & 3
        return None
    if op == 'iretq':
        sp = m.get('rsp')
        rip, cs, fl, rsp = m.load(sp), m.load(sp + 8), m.load(sp + 16), m.load(sp + 24)
        m.cpl, m.iopl, m.flags = cs & 3, fl >> 12 & 3, fl & ~0x3000 | 2
        m.set('rsp', rsp)
        return next(k for k, a in addr.items() if a == rip) if rip in addr.values() else [ln for name, ln in labels.items() if where.get(name) == rip][0]
    if op == 'syscall':
        m.set('rcx', ip_next()); m.set('r11', m.flags | m.iopl << 12)
        m.cpl = 0
        lstar = m.msr[0xc0000082]
        return [ln for name, ln in labels.items() if where.get(name) == lstar][0]
    if op == 'wrmsr':
        m.msr[m.get('ecx')] = m.get('edx') << 32 | m.get('eax'); return None
    if op == 'rdmsr':
        v = m.msr.get(m.get('ecx'), 0); m.set('rax', v & 0xffffffff); m.set('rdx', v >> 32); return None
    if op == 'db':
        raw = bytes(int(x, 0) for x in rest.split(','))
        port = raw[-1] if raw[-2] == 0xe5 else m.get('dx')
        m.set('rax', m.port_in(port, 4)); return None                # REX.W IN: 32 bits (zero-extended)
    if op == 'in':
        d, s = args
        port = m.get('dx') if s == 'dx' else int(s, 0)
        m.set(d, m.port_in(port, m.width(d) // 8)); return None
    if op == 'out':
        d, s = args
        port = m.get('dx') if d == 'dx' else int(d, 0)
        m.port_out(port, m.width(s) // 8, m.get(s)); return None
    if op in ('rep', 'a32', 'fs') or op in ('outsb', 'outsw', 'outsd', 'insb', 'insw', 'insd'):
        return string(m, line)
    raise ValueError(f'not interpreted: {line}')


def ea(m, text):
    total = 0
    for sign, term in re.findall(r'([+-]?)\s*([^+-]+)', text.replace(' ', '')):
        v = m.get(term) if term in REGS else int(term, 0)
        total += -v if sign == '-' else v
    return total & M


def string(m, line):
    words = line.split()
    rep, a32, fs = 'rep' in words, 'a32' in words, 'fs' in words
    insn = words[-1]
    kind, n = insn[:-1], SIZE[insn[-1]]
    port = m.get('dx')
    count = (m.get('ecx') if a32 else m.get('rcx')) if rep else 1
    if not m.permitted(port, n): raise Fault(13)                    # before the first element, even with a zero count
    step_ = -n if m.flags & DF else n
    si, di = ('esi', 'edi') if a32 else ('rsi', 'rdi')
    base = m.msr.get(0xc0000100, 0) if fs else 0
    while count:
        if kind == 'outs':
            v = m.load((base + m.get(si)) & M, n)
            m.port_out(port, n, v)
            m.set(si, m.get(si) + step_)
        else:
            m.where(m.get(di), n, True)                              # the destination's #PF comes before the port is read
            m.store(m.get(di), m.port_in(port, n), n)
            m.set(di, m.get(di) + step_)
        count -= 1
        if rep: m.set('ecx' if a32 else 'rcx', count)
    return None


def expected(p):
    """(the state smp.parse_dump gives, COM1's output) of an L3 `qemu` program (ports.uart_program and the others)."""
    m = run(p)
    q = p['qemu']
    state = expect.state(q, [{r: v for r, v in zip(REGS, m.g)} | {'rflags': m.flags, 'fault': None}],
                         [(a, bytes(d)) for a, d in m.mem])
    return state, bytes(m.uart.sent)

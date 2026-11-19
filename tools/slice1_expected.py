"""The prefix state of tools/run_slice1_faults.py's built-in probes (the state just before the probed instruction),
written from the probe: its initial registers and memory, then the instructions before the probe, which are a prelude of
MOV, MOVABS, ADD, SUB, INC, CMP (and NOP, MFENCE, PREFETCH: no architectural effect) on known values (SDM Vol.2). A
probe whose prefix holds any other instruction (or a segment override) keeps QEMU's answer: `prefix(...)` gives None.
Equal to QEMU 11.1.2's answers for every probe it covers (tools/test_slice1_expected.py,
tools/reference/crosscheck/slice1.json)."""
import pathlib, re, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'tools' / 'reference')]
import ref  # noqa: E402

M = (1 << 64) - 1
R64 = {r: i for i, r in enumerate(ref.REGS)}
R32 = {'eax': 0, 'ecx': 1, 'edx': 2, 'ebx': 3, 'esp': 4, 'ebp': 5, 'esi': 6, 'edi': 7, **{f'r{i}d': i for i in range(8, 16)}}
CF, PF, AF, ZF, SF, OF = 0x1, 0x4, 0x10, 0x40, 0x80, 0x800
SIZES = {'byte': 1, 'word': 2, 'dword': 4, 'qword': 8}
NOEFFECT = ('nop', 'mfence', 'lfence', 'sfence', 'prefetcht0', 'prefetcht1', 'prefetcht2', 'prefetchnta')


class Unknown(Exception):
    """An instruction this interpreter does not cover: the probe keeps QEMU's answer."""


def szp(r, bits):
    return (ZF if r == 0 else 0) | (SF if r >> (bits - 1) & 1 else 0) | (PF if bin(r & 0xff).count('1') % 2 == 0 else 0)


class Machine:
    def __init__(self, gpr, rflags, memory):
        self.g, self.flags = [ref.num(v) for v in gpr], ref.num(rflags)
        self.mem = [(ref.num(m['addr']), bytearray.fromhex(m['data'])) for m in memory]

    def reg(self, r):
        if r in R64: return self.g[R64[r]], 64
        if r in R32: return self.g[R32[r]] & 0xffffffff, 32
        raise Unknown(r)

    def set(self, r, v):
        if r in R64: self.g[R64[r]] = v & M
        elif r in R32: self.g[R32[r]] = v & 0xffffffff
        else: raise Unknown(r)

    def ea(self, text):
        total = 0
        for sign, term in re.findall(r'([+-]?)([^+-]+)', text.replace(' ', '')):
            if '*' in term: r, s = term.split('*'); v = self.reg(r)[0] * int(s)
            elif term in R64: v = self.reg(term)[0]
            else: v = int(term, 0)
            total += -v if sign == '-' else v
        return total & M

    def region(self, at, n):
        for a, d in self.mem:
            if a <= at and at + n <= a + len(d): return a, d
        raise Unknown(f'access at {at:#x} outside the regions')

    def operand(self, text):
        m = re.fullmatch(r'(byte|word|dword|qword) ptr \[([^\]:]*)\]', text)
        if m: return ('mem', self.ea(m.group(2)), SIZES[m.group(1)])
        if text in R64 or text in R32: return ('reg', text, self.reg(text)[1] // 8)
        if re.fullmatch(r'-?(0x[0-9a-f]+|\d+)', text): return ('imm', int(text, 0), None)
        raise Unknown(text)

    def read(self, op, size=8):
        if op[0] == 'imm': return op[1] & ((1 << 8 * size) - 1)
        if op[0] == 'reg': return self.reg(op[1])[0]
        a, d = self.region(op[1], op[2]); return int.from_bytes(d[op[1] - a:op[1] - a + op[2]], 'little')

    def write(self, op, v):
        if op[0] == 'reg': return self.set(op[1], v)
        a, d = self.region(op[1], op[2]); d[op[1] - a:op[1] - a + op[2]] = (v & ((1 << 8 * op[2]) - 1)).to_bytes(op[2], 'little')

    def run(self, line):
        if ':' in line or line.startswith('{') or line.startswith('.'): raise Unknown(line)   # segment overrides, raw bytes
        op, _, rest = line.partition(' ')
        if op in NOEFFECT: return
        args = [x.strip() for x in re.split(r',(?![^\[]*\])', rest)] if rest else []
        if op in ('mov', 'movabs') and len(args) == 2:
            d = self.operand(args[0]); s = self.operand(args[1])
            size = d[2] or s[2]
            if s[0] == 'imm' and d[0] == 'mem' and size == 8: v = s[1] & M   # (imm32 sign-extended: as given)
            else: v = self.read(s, size)
            self.write(d, v)
        elif op in ('add', 'sub', 'cmp') and len(args) == 2:
            d = self.operand(args[0]); s = self.operand(args[1])
            n = 8 * d[2]; mask = (1 << n) - 1
            a, b = self.read(d), self.read(s, d[2]) & mask
            r = (a + b if op == 'add' else a - b) & mask
            cf = a + b > mask if op == 'add' else a < b
            of = ((~(a ^ b) if op == 'add' else (a ^ b)) & (a ^ r)) >> (n - 1) & 1
            if op != 'cmp': self.write(d, r)
            self.flags = self.flags & ~0x8d5 | (CF if cf else 0) | szp(r, n) | (AF if (a ^ b ^ r) & 0x10 else 0) | (OF if of else 0)
        elif op == 'inc' and len(args) == 1:
            d = self.operand(args[0]); n = 8 * d[2]; a = self.read(d); r = (a + 1) & ((1 << n) - 1)
            self.write(d, r)
            self.flags = self.flags & ~(0x8d5 & ~CF) | szp(r, n) | (AF if a & 0xf == 0xf else 0) | (OF if r == 1 << (n - 1) else 0)
        else:
            raise Unknown(line)


class FormMachine(Machine):
    """`Machine` with the paging programs' data forms (run_slice1_faults.DATA_FORMS and CROSSING; SDM Vol.2 each form).
    Undefined flags are left as they come: the comparison masks them (ref.flags.analyze)."""
    R8 = {'al': 0, 'cl': 1, 'dl': 2, 'bl': 3}

    def reg(self, r):
        if r in self.R8: return self.g[self.R8[r]] & 0xff, 8
        return Machine.reg(self, r)

    def set(self, r, v):
        if r in self.R8: k = self.R8[r]; self.g[k] = self.g[k] & ~0xff | v & 0xff
        else: Machine.set(self, r, v)

    def operand(self, text):
        if text in self.R8: return ('reg', text, 1)
        return Machine.operand(self, text)

    def push(self, v):
        self.g[R64['rsp']] = (self.g[R64['rsp']] - 8) & M
        self.write(('mem', self.g[R64['rsp']], 8), v)

    def pop(self):
        v = self.read(('mem', self.g[R64['rsp']], 8)); self.g[R64['rsp']] = (self.g[R64['rsp']] + 8) & M
        return v

    def arith(self, op, a, b, n, carry=0):
        mask = (1 << n) - 1
        r = (a + b + carry if op == 'add' else a - b - carry) & mask
        cf = a + b + carry > mask if op == 'add' else a < b + carry
        of = ((~(a ^ b) if op == 'add' else (a ^ b)) & (a ^ r)) >> (n - 1) & 1
        self.flags = self.flags & ~0x8d5 | (CF if cf else 0) | szp(r, n) | (AF if (a ^ b ^ r) & 0x10 else 0) | (OF if of else 0)
        return r

    def run(self, line):
        line = line.removeprefix('lock ')
        op, _, rest = line.partition(' ')
        args = [x.strip() for x in re.split(r',(?![^\[]*\])', rest)] if rest else []
        o = [self.operand(x) for x in args] if op not in ('enter',) else []
        bits = lambda x: 8 * x[2]
        if op in ('mov', 'movabs', 'add', 'sub', 'cmp', 'inc') or op in NOEFFECT:
            return Machine.run(self, line)
        if op == 'push': self.push(self.read(o[0]))
        elif op == 'pushfq': self.push(self.flags & 0xcd5 | 0x202)
        elif op == 'pop': self.write(o[0], self.pop())
        elif op == 'ret': self.pop()                                  # (its target is the next instruction by construction)
        elif op == 'not': self.write(o[0], ~self.read(o[0]))
        elif op == 'neg':
            a = self.read(o[0]); r = self.arith('sub', 0, a, bits(o[0])); self.write(o[0], r)
        elif op == 'dec':
            n = bits(o[0]); a = self.read(o[0]); cf = self.flags & CF
            r = self.arith('sub', a, 1, n); self.flags = self.flags & ~CF | cf; self.write(o[0], r)
        elif op == 'adc':
            n = bits(o[0]); r = self.arith('add', self.read(o[0]), self.read(o[1], o[0][2]), n, self.flags & CF); self.write(o[0], r)
        elif op == 'xchg':
            a, b = self.read(o[0]), self.read(o[1]); self.write(o[0], b); self.write(o[1], a)
        elif op == 'xadd':
            n = bits(o[0]); a, b = self.read(o[0]), self.read(o[1]); r = self.arith('add', a, b, n)
            self.write(o[1], a); self.write(o[0], r)
        elif op == 'cmpxchg':
            n = bits(o[0]); acc = ('reg', {32: 'eax', 64: 'rax'}[n], n // 8)
            a, d = self.read(acc), self.read(o[0])
            self.arith('sub', a, d, n)
            if a == d: self.write(o[0], self.read(o[1]))
            else: self.write(o[0], d); self.write(acc, d)             # (the destination always receives a write)
        elif op in ('shld', 'shrd'):
            n = bits(o[0]); c = self.read(o[2]) & (0x3f if n == 64 else 0x1f)
            if c:
                d, s, mask = self.read(o[0]), self.read(o[1]), (1 << n) - 1
                if op == 'shld':
                    r = (d << c | s >> (n - c)) & mask; cf = d >> (n - c) & 1
                else:
                    r = (d >> c | s << (n - c)) & mask; cf = d >> (c - 1) & 1
                self.flags = self.flags & ~0x8d5 | (CF if cf else 0) | szp(r, n) | (OF if c == 1 and (r ^ d) >> (n - 1) & 1 else 0)
                self.write(o[0], r)
        elif op == 'popcnt':
            v = self.read(o[1]); self.write(o[0], bin(v).count('1')); self.flags = self.flags & ~0x8d5 | (ZF if v == 0 else 0)
        elif op == 'tzcnt':
            n = bits(o[0]); v = self.read(o[1]); r = (v & -v).bit_length() - 1 if v else n
            self.write(o[0], r); self.flags = self.flags & ~(CF | ZF) | (CF if v == 0 else 0) | (ZF if r == 0 else 0)
        elif op == 'andn':
            n = bits(o[0]); r = ~self.read(o[1]) & self.read(o[2]) & ((1 << n) - 1)
            self.write(o[0], r); self.flags = self.flags & ~(CF | OF | ZF | SF) | szp(r, n) & (ZF | SF)
        elif op == 'shrx':
            n = bits(o[0]); self.write(o[0], self.read(o[1]) >> (self.read(o[2]) & (n - 1)))
        elif op == 'movnti': self.write(o[0], self.read(o[1]))
        elif op == 'bts':
            n = bits(o[0]); k = self.read(o[1]) % n; v = self.read(o[0])
            self.flags = self.flags & ~CF | (v >> k & 1); self.write(o[0], v | 1 << k)
        elif op == 'enter':
            size = int(args[0], 0) if args else int(rest.split(',')[0], 0)
            self.push(self.g[R64['rbp']]); self.g[R64['rbp']] = self.g[R64['rsp']]; self.g[R64['rsp']] = (self.g[R64['rsp']] - size) & M
        elif op == 'leave':
            self.g[R64['rsp']] = self.g[R64['rbp']]; self.g[R64['rbp']] = self.pop()
        else:
            raise Unknown(line)


def program_state(q, upto, rip):
    """A paging program `q` (run_slice1_faults.paging_programs, as placed: `relocate`'s program) run to its line `upto` (its
    prefix there; `len(lines)`: the full run), at `rip`: a result in ref.py's format with ref.py's mask (the flags the run
    leaves undefined are not compared); None where a line is not covered."""
    m = FormMachine(q['gpr'], q['rflags'], q['memory'])
    try:
        for line in q['asm'][:upto]: m.run(line)
    except (Unknown, KeyError, ValueError):
        return None
    und = ref.flags.analyze(q['insns'][:upto])
    return {'name': q['name'], 'qemu': {'gpr': [ref.hx(v) for v in m.g], 'rflags': ref.hx(m.flags), 'rip': ref.hx(rip),
                                        'memory': [{'addr': ref.hx(a), 'data': bytes(d).hex()} for a, d in m.mem]},
            'rflags_compare_mask': ref.hx(ref.flags.TRACKED & ~und)}


def prefix(p, lines, k, rip):
    """The state of probe program `p` (run_slice1_faults.probe_program) after its lines before `k`, at `rip` (the probed
    instruction's address), in ref.py's result format; None where the interpreter does not cover the prefix."""
    m = Machine(p['gpr'], p['rflags'], p['memory'])
    try:
        for line in lines[:k]: m.run(line)
    except Unknown:
        return None
    return {'name': p['name'], 'qemu': {'gpr': [ref.hx(v) for v in m.g], 'rflags': ref.hx(m.flags), 'rip': ref.hx(rip),
                                        'memory': [{'addr': ref.hx(a), 'data': bytes(d).hex()} for a, d in m.mem]},
            'rflags_compare_mask': ref.hx(ref.flags.TRACKED)}

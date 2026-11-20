"""The expected final state of run_slice2_probes.PROBES (the loop programs), from a small interpreter of their unrolled
form's instructions (SDM Vol.2: MOV, ADD, SUB, XOR, two-operand IMUL, ROL by an immediate, LEA, DEC, PUSH (imm32 or a 64-bit register), POP to a 64-bit register, and ADD
to a qword in memory): `expected(probe)` -> a result in ref.py's format, as add_chain gives for the long loops. Every
probe ends with a `dec` to 0 after a CF-defining instruction, so all the compared flags are defined. Equal to the answers
QEMU 11.1.2 gave (tools/test_slice2_expected.py, tools/reference/crosscheck/slice2.json)."""
import pathlib, re, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'tools' / 'reference')]
import ref  # noqa: E402

M = (1 << 64) - 1
R64 = {r: i for i, r in enumerate(ref.REGS)}
R32 = {'eax': 0, 'ecx': 1, 'edx': 2, 'ebx': 3, 'esp': 4, 'ebp': 5, 'esi': 6, 'edi': 7, **{f'r{i}d': i for i in range(8, 16)}}
CF, PF, AF, ZF, SF, OF = 0x1, 0x4, 0x10, 0x40, 0x80, 0x800
MASK = CF | PF | AF | ZF | SF | OF | 0x400          # the arithmetic flags and DF: all defined at these programs' ends


def szp(r, bits=64):
    return (ZF if r == 0 else 0) | (SF if r >> (bits - 1) & 1 else 0) | (PF if bin(r & 0xff).count('1') % 2 == 0 else 0)


def signed(v, bits=64): return v - (1 << bits) if v >> (bits - 1) & 1 else v


class Machine:
    def __init__(self, gpr, memory):
        self.g, self.flags = list(gpr), 0
        self.mem = [(a, bytearray(d)) for a, d in memory]

    def reg(self, name): return self.g[R64[name]] if name in R64 else self.g[R32[name]] & 0xffffffff
    def set(self, name, v):
        if name in R64: self.g[R64[name]] = v & M
        else: self.g[R32[name]] = v & 0xffffffff           # a 32-bit write zero-extends

    def addr(self, text):
        """[base + index*scale + disp]"""
        total = 0
        for sign, term in re.findall(r'([+-]?)\s*([^+-]+)', text.replace(' ', '')):
            if '*' in term: r, s = term.split('*'); v = self.reg(r) * int(s)
            elif term in R64: v = self.reg(term)
            else: v = int(term, 0)
            total += -v if sign == '-' else v
        return total & M

    def load(self, at, n=8):
        for a, d in self.mem:
            if a <= at and at + n <= a + len(d): return int.from_bytes(d[at - a:at - a + n], 'little')
        raise KeyError(hex(at))

    def store(self, at, v, n=8):
        for a, d in self.mem:
            if a <= at and at + n <= a + len(d): d[at - a:at - a + n] = (v & ((1 << 8 * n) - 1)).to_bytes(n, 'little'); return
        raise KeyError(hex(at))

    def operand(self, text):
        m = re.fullmatch(r'qword ptr \[(.*)\]', text)
        if m: return ('mem', self.addr(m.group(1)))
        return ('reg', text) if text in R64 or text in R32 else ('imm', int(text, 0))

    def read(self, op): return self.load(op[1]) if op[0] == 'mem' else self.reg(op[1]) if op[0] == 'reg' else op[1]
    def write(self, op, v): self.store(op[1], v) if op[0] == 'mem' else self.set(op[1], v)

    def bits(self, op): return 32 if op[0] == 'reg' and op[1] in R32 else 64

    def run(self, line):
        op, _, rest = line.partition(' ')
        args = [x.strip() for x in re.split(r',(?![^\[]*\])', rest)] if rest else []
        if op == 'mov':
            self.write(self.operand(args[0]), self.read(self.operand(args[1])))
        elif op == 'lea':
            self.set(args[0], self.addr(args[1].strip('[]')))
        elif op == 'push':
            self.g[R64['rsp']] = (self.g[R64['rsp']] - 8) & M
            self.store(self.g[R64['rsp']], self.read(self.operand(args[0])) & M)
        elif op == 'pop':
            v = self.load(self.g[R64['rsp']])
            self.g[R64['rsp']] = (self.g[R64['rsp']] + 8) & M
            self.set(args[0], v)
        elif op in ('add', 'sub'):
            d, s = self.operand(args[0]), self.operand(args[1])
            n, a, b = self.bits(d), self.read(d), self.read(s) & ((1 << self.bits(d)) - 1)
            mask = (1 << n) - 1
            r = (a + b if op == 'add' else a - b) & mask
            cf = a + b > mask if op == 'add' else a < b
            of = ((~(a ^ b) if op == 'add' else (a ^ b)) & (a ^ r)) >> (n - 1) & 1
            self.write(d, r)
            self.flags = (CF if cf else 0) | szp(r, n) | (AF if (a ^ b ^ r) & 0x10 else 0) | (OF if of else 0)
        elif op == 'xor':
            d = self.operand(args[0]); r = self.read(d) ^ self.read(self.operand(args[1]))
            self.write(d, r); self.flags = szp(r)                  # CF, OF clear (AF undefined: never the last writer here)
        elif op == 'imul':
            d = self.operand(args[0]); full = signed(self.read(d)) * signed(self.read(self.operand(args[1])))
            r = full & M; self.write(d, r)
            self.flags = (CF | OF) if signed(r) != full else 0      # SF ZF AF PF undefined (a later DEC defines them)
        elif op == 'rol':
            d = self.operand(args[0]); k = int(args[1], 0) % 64; v = self.read(d)
            r = ((v << k) | (v >> (64 - k))) & M if k else v
            self.write(d, r)
            if k: self.flags = (self.flags & ~CF) | (r & 1)        # CF = the result's bit 0; OF undefined (count > 1)
        elif op == 'dec':
            d = self.operand(args[0]); n = self.bits(d); a = self.read(d); r = (a - 1) & ((1 << n) - 1)
            self.write(d, r)
            self.flags = (self.flags & CF) | szp(r, n) | (AF if a & 0xf == 0 else 0) | (OF if a == 1 << (n - 1) else 0)
        else:
            raise ValueError(f'not interpreted: {line}')


def expected(probe):
    """The unrolled form of `probe` (run_slice2_probes.build) run here: a result in ref.py's format (RIP not compared)."""
    import run_slice2_probes as S
    _, reference, _ = S.build(probe)
    lines = [l for l in (S.Form(probe.get('functions', {}), False, _returns(probe)).program(probe['main']))]
    m = Machine(probe['gpr'], [(ref.num(x['addr']), bytes.fromhex(x['data'])) for x in probe['memory']])
    for line in lines: m.run(line)
    return {'qemu': {'gpr': [hex(v) for v in m.g], 'rflags': hex(m.flags | 2), 'rip': '0x0',
                     'memory': [{'addr': hex(a), 'data': bytes(d).hex()} for a, d in m.mem]},
            'rflags_compare_mask': hex(MASK)}


def _returns(probe):
    import run_slice2_probes as S
    _, labels = ref.assemble('\n'.join(S.Form(probe.get('functions', {}), True).program(probe['main'])), labels=True)
    return {name: S.CODE + offset for name, offset in labels.items()}

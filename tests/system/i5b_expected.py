"""The expected final state of the I5b `images` programs (tests/system/run_i5b.py), written from the programs and the SDM's
save-area layouts (Vol.1 10.5.1 FXSAVE area, 13.4 XSAVE area, 13.7-13.8 XSAVE/XRSTOR standard form; Vol.2 FXSAVE,
FXRSTOR, XSAVE, XSAVEOPT, XRSTOR, FNSTENV, XGETBV, XSETBV, CPUID 0DH): an x87 unit (physical registers, TOP, tags), MXCSR
and 16 XMM/YMM registers run through each program's lines. Values the runner masks (FOP, FIP, FDP and their selectors,
ST slots' reserved bytes, the MXCSR QEMU leaves for RFBM = AVX alone) are compared by neither side.
`expected(prog, mask)` -> the state smp.parse_dump gives, with the masked bytes zero (the runner's view). Equal to QEMU
11.1.2's answers (tests/system/test_i5b_expected.py, tools/reference/crosscheck/i5b.json). `expected(prog, mask,
'masked-x87-de')`: the state fast mode gives on a Mac without AFP (tests/lib/fp_deviations.py), where an FLD of a denormal
m64 sets no DE; `deviation(prog, mask)` names that class for a program whose expected state it changes."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import pathlib, re, struct, sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'tools' / 'reference'), str(ROOT / 'tools' / 'bench' / 'smp')]
import expect, smp  # noqa: E402
import run_i5b as I  # noqa: E402

M = (1 << 64) - 1
MXCSR_MASK = 0xffff           # the profile's (DAZ supported)
START_CR4 = 0x20              # the stub's: PAE
CPUID_D = lambda xcr0: (7, 832 if xcr0 & 4 else 576, 832, 0)       # leaf 0DH.0: supported XCR0, size for XCR0, maximum size


def f80(d):
    """A double's exact 80-bit form: (significand with its integer bit, sign and exponent)."""
    bits = struct.unpack('<Q', struct.pack('<d', d))[0]
    sign, exp, frac = bits >> 63, bits >> 52 & 0x7ff, bits & ((1 << 52) - 1)
    if exp == 0 and frac == 0: return 0, sign << 15
    if exp == 0x7ff: return (1 << 63) | frac << 11, sign << 15 | 0x7fff
    if exp == 0:                                                     # a denormal: normalized in the wider exponent range
        shift = 53 - frac.bit_length()
        return frac << (11 + shift), sign << 15 | (1 - 1023 - shift + 16383)
    return (1 << 63) | frac << 11, sign << 15 | (exp - 1023 + 16383)


def tag_of(m, se):
    """FNSTENV's tag of a non-empty register: 0 valid, 1 zero, 2 special (NaN, infinity, denormal, unsupported)."""
    exp = se & 0x7fff
    if exp == 0: return 1 if m == 0 else 2
    if exp == 0x7fff or not m >> 63: return 2
    return 0


class State:
    def __init__(self, d, masked_x87_de=False):
        self.masked_x87_de = masked_x87_de                             # fp_deviations `masked-x87-de`: no DE from a memory operand
        self.mem = bytearray(d)
        self.g = {r: 0 for r in smp.REGS}; self.g.update(rdi=I.D, rsp=I.STACK)
        self.flags, self.cr4, self.xcr0 = 0x2, START_CR4, 1
        self.fcw, self.fsw, self.top = 0x37f, 0, 0
        self.r = [(0, 0)] * 8                                          # physical registers (power-up +0.0)
        self.full = [False] * 8                                        # physical registers in use
        self.mxcsr, self.xmm, self.ymmh = 0x1f80, [bytes(16)] * 16, [bytes(16)] * 16

    # memory at [rdi + disp]
    def addr(self, text):
        m = re.search(r'\[rdi(?: \+ (0x[0-9a-f]+|\d+))?\]', text)
        return int(m.group(1) or '0', 0)
    def get(self, o, n): return bytes(self.mem[o:o + n])
    def put(self, o, b): self.mem[o:o + len(b)] = b

    def setr(self, r, v, logic=False):
        r32 = {'eax': 'rax', 'ebx': 'rbx', 'ecx': 'rcx', 'edx': 'rdx'}
        if r in r32: self.g[r32[r]] = v & 0xffffffff
        else: self.g[r] = v & M

    def logic_flags(self, v, bits):
        self.flags = 0x2 | (0x40 if v == 0 else 0) | (0x80 if v >> (bits - 1) & 1 else 0) | (0x4 if bin(v & 0xff).count('1') % 2 == 0 else 0)

    # x87
    def fsw_word(self): return self.fsw & ~0x3800 | self.top << 11
    def abridged(self): return sum(1 << p for p in range(8) if self.full[p])
    def push(self, v):
        self.top = (self.top - 1) % 8; self.r[self.top] = v; self.full[self.top] = True

    def legacy_x87(self, o):
        self.put(o, struct.pack('<HHB', self.fcw, self.fsw_word(), self.abridged()) + b'\0')
        for k in range(8):
            m, se = self.r[(self.top + k) % 8]
            self.put(o + 32 + 16 * k, struct.pack('<QH', m, se))

    def load_x87(self, o):
        self.fcw, fsw, tags = struct.unpack('<HHB', self.get(o, 5))
        self.top, self.fsw = fsw >> 11 & 7, fsw
        self.full = [bool(tags >> p & 1) for p in range(8)]
        for k in range(8):
            self.r[(self.top + k) % 8] = struct.unpack('<QH', self.get(o + 32 + 16 * k, 10))

    def init_x87(self):
        self.fcw, self.fsw, self.top, self.full, self.r = 0x37f, 0, 0, [False] * 8, [(0, 0)] * 8

    def mxcsr_out(self, o): self.put(o + 24, struct.pack('<II', self.mxcsr, MXCSR_MASK))
    def xmm_out(self, o):
        for k in range(16): self.put(o + 160 + 16 * k, self.xmm[k])
    def xmm_in(self, o): self.xmm = [self.get(o + 160 + 16 * k, 16) for k in range(16)]

    def run(self, line):
        op, _, rest = line.partition(' ')
        a = [x.strip() for x in rest.split(',')] if rest else []
        if line == 'mov rax, cr4': self.g['rax'] = self.cr4
        elif line == 'mov cr4, rax': self.cr4 = self.g['rax']
        elif op == 'or':
            v = self.g[a[0]] | int(a[1], 0); self.g[a[0]] = v; self.logic_flags(v, 64)
        elif op == 'and':
            v = self.g['rcx'] & 0xffffffff & int(a[1], 0); self.setr(a[0], v); self.logic_flags(v, 32)
        elif op == 'xor' and a[0] == a[1]:
            self.setr(a[0], 0); self.logic_flags(0, 32)
        elif op == 'mov' and a[0] in ('eax', 'ebx', 'ecx', 'edx', 'rcx', 'rax') and not a[1].startswith('dword'):
            self.setr(a[0], int(a[1], 0) & (M if a[0].startswith('r') else 0xffffffff))
        elif op == 'mov' and a[0].startswith('dword ptr'):
            self.put(self.addr(a[0]), struct.pack('<I', self.g['r' + a[1][1:]] & 0xffffffff))
        elif op == 'mov' and a[1].startswith('dword ptr'):
            self.setr(a[0], struct.unpack('<I', self.get(self.addr(a[1]), 4))[0])
        elif op == 'xsetbv': self.xcr0 = self.g['rax'] & 0xffffffff | (self.g['rdx'] & 0xffffffff) << 32
        elif op == 'xgetbv': self.setr('eax', self.xcr0); self.setr('edx', self.xcr0 >> 32)    # ECX = 0 (its upper half ignored)
        elif op == 'cpuid':
            if self.g['rax'] & 0xffffffff == 0xd:
                for r, v in zip(('eax', 'ebx', 'ecx', 'edx'), CPUID_D(self.xcr0)): self.setr(r, v)
            else:     # leaf 1: only ECX's XSAVE, OSXSAVE (CR4.OSXSAVE) and AVX bits are kept by the program
                self.setr('ecx', 1 << 26 | (1 << 27 if self.cr4 >> 18 & 1 else 0) | 1 << 28)
                self.setr('eax', 0); self.setr('ebx', 0); self.setr('edx', 0)
        elif op == 'fninit': self.fcw, self.fsw, self.top, self.full = 0x37f, 0, 0, [False] * 8
        elif op == 'fldcw': self.fcw = struct.unpack('<H', self.get(self.addr(a[0]), 2))[0]
        elif op == 'fld':
            d = struct.unpack('<d', self.get(self.addr(a[0]), 8))[0]
            bits = struct.unpack('<Q', struct.pack('<d', d))[0]
            if bits >> 52 & 0x7ff == 0 and bits & ((1 << 52) - 1) and not self.masked_x87_de: self.fsw |= 0x2   # #D: a denormal source (masked: DE)
            self.push(f80(d))
        elif op == 'fld1': self.push((1 << 63, 0x3fff))
        elif op == 'ldmxcsr': self.mxcsr = struct.unpack('<I', self.get(self.addr(a[0]), 4))[0]
        elif op == 'stmxcsr': self.put(self.addr(a[0]), struct.pack('<I', self.mxcsr))
        elif op == 'movdqu':
            if not a[0].startswith('xmmword'): self.xmm[int(a[0][3:])] = self.get(self.addr(a[1]), 16)
            else: self.put(self.addr(a[0]), self.xmm[int(a[1][3:])])
        elif op == 'pxor': self.xmm[int(a[0][3:])] = bytes(16)                          # (same register: zero; legacy SSE keeps YMM_H)
        elif op == 'pcmpeqd': self.xmm[int(a[0][3:])] = b'\xff' * 16
        elif op in ('fxsave', 'fxsave64'):
            o = self.addr(a[0]); self.legacy_x87(o); self.mxcsr_out(o); self.xmm_out(o)
        elif op in ('fxrstor', 'fxrstor64'):
            o = self.addr(a[0]); self.load_x87(o); self.mxcsr = struct.unpack('<I', self.get(o + 24, 4))[0]; self.xmm_in(o)
        elif op == 'fnstenv':
            o = self.addr(a[0])
            tags = sum((tag_of(*self.r[p]) if self.full[p] else 3) << 2 * p for p in range(8))
            self.put(o, struct.pack('<III', self.fcw, self.fsw_word(), tags))           # (bits 31:16 reserved: written 0)
            self.fcw |= 0x3f                                                             # then every exception masked
        elif op in ('xsave', 'xsave64', 'xsaveopt', 'xsaveopt64'):
            o = self.addr(a[0]); rfbm = (self.g['rax'] & 0xffffffff | (self.g['rdx'] & 0xffffffff) << 32) & self.xcr0
            if rfbm & 1: self.legacy_x87(o)
            if rfbm & 6: self.mxcsr_out(o)
            if rfbm & 2: self.xmm_out(o)
            if rfbm & 4:
                for k in range(16): self.put(o + 576 + 16 * k, self.ymmh[k])
            bv = struct.unpack('<Q', self.get(o + 512, 8))[0]
            self.put(o + 512, struct.pack('<Q', bv | rfbm))                              # XINUSE: every component in use
        elif op == 'xrstor':
            o = self.addr(a[0]); rfbm = (self.g['rax'] & 0xffffffff | (self.g['rdx'] & 0xffffffff) << 32) & self.xcr0
            bv = struct.unpack('<Q', self.get(o + 512, 8))[0]
            if rfbm & 1: self.load_x87(o) if bv & 1 else self.init_x87()
            if rfbm & 6: self.mxcsr = struct.unpack('<I', self.get(o + 24, 4))[0]
            if rfbm & 2:
                if bv & 2: self.xmm_in(o)
                else: self.xmm = [bytes(16)] * 16
            if rfbm & 4: self.ymmh = [self.get(o + 576 + 16 * k, 16) if bv & 4 else bytes(16) for k in range(16)]
        else:
            raise ValueError(f'not interpreted: {line}')


def expected(prog, mask, deviation=None):
    assert deviation in (None, 'masked-x87-de'), deviation
    d = smp.region_bytes(prog['memory'][0])
    s = State(d, masked_x87_de=deviation == 'masked-x87-de')
    for line in prog['asm']: s.run(line)
    data = bytearray(s.mem)
    for off, n, *_ in mask: data[off:off + n] = bytes(n)             # the runner's view: masked bytes zero on both sides
    regs = {r: v for r, v in s.g.items()}
    return expect.state(prog, [dict(regs, rflags=s.flags, fault=None)], [(I.D, bytes(data))])


def deviation(prog, mask):
    """The documented deviation of fast mode without AFP (a tests/lib/fp_deviations.py class) that changes the program's
    expected state, derived from the model and never written by hand: `masked-x87-de` where a denormal m64 operand's DE
    reaches the final state (fxsave-full-stack: the prelude's FLD of 2^-1060, then FXSAVE), else None."""
    return 'masked-x87-de' if expected(prog, mask) != expected(prog, mask, 'masked-x87-de') else None

"""The expected final state of the M2 `qemu` programs (tools/run_m2.py: `pat`, `mtrr`, `uc`), written from the programs.
Memory types change no value (SDM Vol.3 14.3: a type decides caching and ordering, never what a load returns), so each
program is its accesses' effects on its eight test pages, run here instruction by instruction (SDM Vol.2 semantics):
`expected(name, n, prog)` -> the state smp.parse_dump would give (tools/bench/smp/expect.py). Equal to QEMU 11.1.2's
answers at 1 and 2 CPUs (tools/test_m2_expected.py, tools/reference/crosscheck/m2.json)."""
import math, pathlib, struct, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'tools' / 'reference'), str(ROOT / 'tools' / 'bench' / 'smp')]
import expect, programs as P, smp  # noqa: E402
import run_m2 as X  # noqa: E402

M = (1 << 64) - 1


class Pages:
    """CPU i's eight test pages T(i, 0..7) (one contiguous span), with `alias` linear addresses mapping onto them."""
    def __init__(self, prog, i):
        self.base = X.T(i, 0)
        self.b = bytearray(0x8000)
        for k in range(8):
            r = next(x for x in prog['memory'] if smp.num(x['addr']) == X.T(i, k))
            self.b[0x1000 * k:0x1000 * (k + 1)] = smp.region_bytes(r)

    def off(self, a, n):
        o = (a - X.ALIAS + 0x20_0000 if a >= X.ALIAS else a) - self.base
        assert 0 <= o and o + n <= len(self.b), hex(a)
        return o

    def get(self, a, n=8): o = self.off(a, n); return int.from_bytes(self.b[o:o + n], 'little')
    def put(self, a, v, n=8): o = self.off(a, n); self.b[o:o + n] = (v & ((1 << 8 * n) - 1)).to_bytes(n, 'little')
    def raw(self, a, n): o = self.off(a, n); return bytes(self.b[o:o + n])
    def set_raw(self, a, data): o = self.off(a, len(data)); self.b[o:o + len(data)] = data
    def writes(self): return [(self.base, bytes(self.b))]


def access(m, r, a, k):
    """run_m2.access at linear `a` (page k's code at a + 0xf00: `add r11, k + 1`)."""
    rax = m.get(a)
    r['r8'] = (r['r8'] + rax) & M
    m.put(a + 8, r['r8'])
    old = m.get(a + 16); m.put(a + 16, old + r['r8']); r['r8'] = old                   # lock xadd
    old = m.get(a + 24); m.put(a + 24, r['r9']); r['r9'] = old                          # xchg
    r['r9'] = (r['r9'] + r['r8']) & M
    m.put(a + 32, r['r9'])                                                              # movnti
    m.set_raw(a + 0x40, b'\x5a' * 32)                                                   # rep stosb (CLFLUSH* change nothing)
    r['r10'] = (r['r10'] + m.get(a + 0x38)) & M                                         # lodsq
    r['r10'] = (r['r10'] + m.get(a + 0x800)) & M
    m.put(a + 0x808, m.get(a + 0x808) + 1)                                              # inc qword
    r['r11'] = (r['r11'] + k + 1) & M
    r.update(rbx=a, rsi=a + 0x40, rdi=a + 0x60)


def _pat_mtrr(name, prog, i):
    m, r = Pages(prog, i), {x: 0 for x in ('r8', 'r9', 'r10', 'r11')}
    rounds = [[X.T(i, k) for k in range(8)]] * 2
    if name == 'pat': rounds.append([X.ALIAS + X.T(i, k) - 0x20_0000 for k in range(8)])   # the 2 MiB leaf onto the same frames
    for pages in rounds:
        for k, a in enumerate(pages): access(m, r, a, k)
    if name == 'mtrr':      # the variable range 5 written (page 1's, WC) and IA32_MTRR_DEF_TYPE (E, UC default) read back
        base, t = X.MTRR_RANGES[1]
        r.update(r12=X.T(0, 1) | t, r14=X.MTRR_MASK, r15=0xc00)
    return r, m.writes()


def _x87_extended(d):
    """The 80-bit form of a double (normal, nonzero): significand with its integer bit, then sign and exponent."""
    bits = struct.unpack('<Q', struct.pack('<d', d))[0]
    exp, frac = bits >> 52 & 0x7ff, bits & ((1 << 52) - 1)
    return ((1 << 63) | frac << 11).to_bytes(8, 'little') + ((bits >> 63) << 15 | (exp - 1023 + 16383)).to_bytes(2, 'little')


def _uc(prog, i, text):
    """run_m2.uc_body, instruction by instruction (its comments name the forms)."""
    m = Pages(prog, i)
    u0, w1, u2, m3 = (X.T(i, k) for k in range(4))
    g = {'rbx': u0, 'rbp': w1, 'r12': u2, 'r15': m3, 'r9': 0}
    s8 = lambda v, n: v - (1 << n) if v >> (n - 1) & 1 else v
    rax = m.get(u0, 1)                                       # mov al, [rbx] (rax was 7: xsetbv's eax)
    m.put(u0 + 1, rax, 1)
    m.put(u0 + 4, m.get(u0 + 2, 2), 2)
    m.put(u0 + 12, m.get(u0 + 8, 4), 4)
    m.put(u0 + 24, m.get(u0 + 16))
    m.put(u0 + 0x20, 0x7f, 1); m.put(u0 + 0x28, -5)
    rcx = m.get(u0 + 5, 1)                                    # movzx ecx, byte
    rdx = s8(m.get(u0 + 6, 2), 16) & M                        # movsx rdx, word
    r8 = (rcx + rdx) & M
    r10 = m.get(m3 + 8)
    m.put(m3 + 16, r8)
    m.put(u0 + 0x30, m.get(u0 + 0x30) + r8)
    m.put(u0 + 0x38, m.get(u0 + 0x38, 4) + 1, 4)
    m.put(u0 + 0x40, ~m.get(u0 + 0x40))
    m.put(u0 + 0x48, m.get(u0 + 0x48) << 3)
    m.put(u0 + 0x50, m.get(u0 + 0x50, 4) ^ (r8 & 0xffffffff), 4)
    b = m.get(u0 + 0x58, 1); m.put(u0 + 0x58, -b, 1)
    zf = (-b & 0xff) == 0                                     # NEG's ZF: BTS and BTR below leave ZF unaffected (SDM Vol.2 BTS, BTR)
    v = m.get(u0 + 0x60); m.put(u0 + 0x60, v | 1 << 5)
    v = m.get(u0 + 0x68, 4); cf = v >> 3 & 1; m.put(u0 + 0x68, v & ~(1 << 3), 4)
    m.put(u0 + 0x6c, cf, 1)                                   # setc: BTR's CF (the old bit)
    r9 = g['r9'] if zf else m.get(u0 + 0x70)                  # cmovnz
    cf = m.get(u0 + 0x78) < 7                                 # cmp qword [rbx+0x78], 7: the borrow
    r9 = (r9 + m.get(u0 + 0x80) + cf) & M                     # adc
    m.put(u0 + 0x88, m.get(u0 + 0x88) + r8)                   # lock add
    old = m.get(u0 + 0x90); m.put(u0 + 0x90, old + r9); r9 = old   # lock xadd
    m.put(u0 + 0x98, r8, 4)                                   # lock cmpxchg: eax was just loaded from it, so it succeeds
    old = m.get(u0 + 0xa0); m.put(u0 + 0xa0, r10); r10 = old  # xchg
    m.put(u0 + 0xa8, m.get(u0 + 0xa8) + 1)
    m.put(u0 + 0xb8, m.get(u0 + 0xb8) | 1 << 7)
    m.put(u0 + 0xc0, 0x22 << 32 | 0x11)                       # cmpxchg8b (edx:eax loaded from it): ecx:ebx stored
    m.put(u0 + 0xd0, 0x33); m.put(u0 + 0xd8, 0x44)            # cmpxchg16b likewise: rcx:rbx
    m.set_raw(u0 + 0x200, m.raw(w1 + 0x100, 24))              # rep movsb
    m.set_raw(w1 + 0x200, m.raw(u0 + 0x100, 24))              # rep movsq (3)
    rax = m.get(u0 + 0x300)                                   # lodsq
    r8 = (r8 + rax) & M
    for q in range(5): m.put(u2 + 0x300 + 8 * q, rax)         # stosq, rep stosq (4)
    n = next((j + 1 for j in range(8) if m.get(u0 + 0x340 + j, 1) != m.get(u2 + 0x340 + j, 1)), 8)   # repe cmpsb
    rsi = u0 + 0x340 + n
    n = next((j + 1 for j in range(16) if m.get(u0 + 0x380 + j, 1) == 0), 16)                         # repne scasb, al = 0
    rdi, rcx = u0 + 0x380 + n, 16 - n
    r9 = (r9 + rcx) & M
    lanes = lambda a, k: [m.get(a + 4 * j, 4) for j in range(k)]
    xmm0 = m.raw(u0 + 0x400, 16)
    m.set_raw(u0 + 0x410, xmm0)
    m.set_raw(u0 + 0x430, b''.join(((x + y) & 0xffffffff).to_bytes(4, 'little') for x, y in zip(lanes(u0 + 0x400, 4), lanes(u0 + 0x420, 4))))
    m.set_raw(u0 + 0x448, m.raw(u0 + 0x440, 4))               # movq xmm1, movd [..], xmm1
    m.set_raw(u0 + 0x4c0, b''.join(((x + y) & 0xffffffff).to_bytes(4, 'little') for x, y in zip(lanes(u0 + 0x480, 8), lanes(u0 + 0x4a0, 8))))
    m.put(u0 + 0x500, 0x400921fb54442d18)                     # pi
    pi = struct.unpack('<d', m.raw(u0 + 0x500, 8))[0]
    m.set_raw(u0 + 0x510, struct.pack('<d', float(s8(m.get(u0 + 0x508, 4), 32))))   # fild dword, fstp qword: exact
    m.set_raw(u0 + 0x530, _x87_extended(pi))                  # fstp tword
    m.put(u0 + 0x518, round(pi))                              # fld tword, fistp qword: round to nearest
    # FNSTSW: TOP back at 0, only PE (fistp's rounding was inexact; masked after FNINIT, so ES clear); C1 0: rounded down.
    m.put(u0 + 0x540, 0x20, 2)
    m.put(u2 + 0x7f8, r8)                                     # push r8
    m.put(u2 + 0x7f0, m.get(u0 + 0x18))                       # push qword [rbx+0x18]
    m.put(u0 + 0x600, m.get(u2 + 0x7f0))                      # pop qword [rbx+0x600]
    rcx = r8                                                  # pop rcx
    r9 = (r9 + rcx) & M
    m.put(u2 + 0x7f8, P.SYS_CODE(i) + expect.labels(text)['.f'] - 2)   # call .f: its return address, the `jmp .g` (2 bytes) after it
    r9 = (r9 + 3) & M
    r8 = (r8 + m.get(u0 + 0xffc)) & M                         # loads crossing u0 | w1 and w1 | u2
    r8 = (r8 + m.get(w1 + 0xffd)) & M
    m.put(u0 + 0xffa, r8); m.put(w1 + 0xffb, r9)
    m.set_raw(u0 + 0xff4, m.raw(w1 + 0xff8, 16))              # movups across w1 | u2, stored across u0 | w1
    m.set_raw(w1 + 0xff0, m.raw(u0 + 0xfe8, 32))              # vmovups ymm likewise
    m.put(u0 + 0x700, r8)                                     # movnti
    g.update(rsi=rsi, rdi=rdi, r8=r8, r9=r9, r10=r10, r11=1, r14=P.STACK(i))    # page 0's code: add r11, 1
    return g, m.writes()


def expected(name, n, prog):
    cpus, writes = [], []
    for i in range(n):
        text = P.sys_text(i, X.PROGRAMS[name][0](i))
        regs, w = _uc(prog, i, text) if name == 'uc' else _pat_mtrr(name, prog, i)
        regs['r13'] = P.SYS_CODE(i) + expect.labels(text)[expect.last_r13(text)]
        cpus.append({**expect.RESTORED, **regs})
        writes += w
    return expect.state(prog, cpus, writes)

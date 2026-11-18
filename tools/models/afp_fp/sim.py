"""x86 AVX/FMA program simulator for the i8/i8b corpora with switchable NaN/flush models (see fp.py)."""
import re, json, subprocess, os, struct, tempfile
from fractions import Fraction as Fr
import fp
from fp import F16, F32, F64, cls, enc, rnd, rint, p2, floorlog2

M128 = (1 << 128) - 1; M256 = (1 << 256) - 1
SZ = {'byte': 8, 'word': 16, 'dword': 32, 'qword': 64, 'xmmword': 128, 'ymmword': 256}
G64 = ['rax', 'rcx', 'rdx', 'rbx', 'rsp', 'rbp', 'rsi', 'rdi'] + [f'r{i}' for i in range(8, 16)]
G32 = ['eax', 'ecx', 'edx', 'ebx', 'esp', 'ebp', 'esi', 'edi'] + [f'r{i}d' for i in range(8, 16)]
G16 = ['ax', 'cx', 'dx', 'bx', 'sp', 'bp', 'si', 'di'] + [f'r{i}w' for i in range(8, 16)]
G8 = ['al', 'cl', 'dl', 'bl', 'spl', 'bpl', 'sil', 'dil'] + [f'r{i}b' for i in range(8, 16)]
GN = {}
for _w, _l in ((64, G64), (32, G32), (16, G16), (8, G8)):
    for _i, _n in enumerate(_l): GN[_n] = (_i, _w)

class Unsupported(Exception): pass

def L(v, w, n): return [(v >> (i * w)) & ((1 << w) - 1) for i in range(n)]
def P(l, w):
    r = 0
    for i, x in enumerate(l): r |= (x & ((1 << w) - 1)) << (i * w)
    return r
def sg(x, w): return x - (1 << w) if x >> (w - 1) & 1 else x
def halves(v, size): return [(v >> (128 * i)) & M128 for i in range(size // 128)]
def unhalves(l): return P(l, 128)

def parse(t):
    t = t.strip()
    m = re.fullmatch(r'([xy])mm(\d+)', t)
    if m: return dict(k='v', i=int(m.group(2)), size=128 if m.group(1) == 'x' else 256)
    if t in GN: return dict(k='g', i=GN[t][0], size=GN[t][1])
    m = re.fullmatch(r'(\w+) ptr (fs:)?\[(.*)\]', t)
    if m:
        e = m.group(3); d = dict(k='m', size=SZ[m.group(1)], fs=bool(m.group(2)), base=None, idx=None, sc=1, disp=0, rip=False)
        for part in e.split(' + '):
            part = part.strip()
            if part == 'rip': d['rip'] = True
            elif '*' in part:
                r, s = part.split('*'); d['idx'] = GN[r][0]; d['sc'] = int(s)
            elif part in GN: d['base'] = GN[part][0]
            else: d['disp'] += int(part, 0)
        return d
    return dict(k='i', v=int(t, 0))

def split_ops(s):
    out, cur, depth = [], '', 0
    for ch in s:
        if ch == '[': depth += 1
        if ch == ']': depth -= 1
        if ch == ',' and depth == 0: out.append(cur); cur = ''
        else: cur += ch
    if cur.strip(): out.append(cur)
    return [parse(x) for x in out]

def rip_lengths(prog):
    """instruction end addresses via clang+llvm-objdump (only needed for rip-relative operands)"""
    code = bytes.fromhex(prog['code'])
    with tempfile.TemporaryDirectory() as d:     # (was a fixed scratch folder of one session, shared by concurrent runs)
        s = d + '/p.s'; o = d + '/p.o'
        with open(s, 'w') as f: f.write('.text\n.globl _f\n_f:\n.byte ' + ','.join(str(b) for b in code) + '\n')
        subprocess.run(['clang', '-target', 'x86_64-apple-macos', '-c', s, '-o', o], check=True)
        out = subprocess.run(['/Library/Developer/CommandLineTools/usr/bin/llvm-objdump', '-d', '--no-show-raw-insn', '--x86-asm-syntax=att', o],
                             capture_output=True, text=True, check=True).stdout
    addrs = []
    for line in out.splitlines():
        m = re.match(r'\s*([0-9a-f]+):\s', line)
        if m: addrs.append(int(m.group(1), 16))
    return addrs

class Ctx:
    pass

class Sim:
    def __init__(s, prog, mode, pseudo=None):
        s.mode = mode
        s.prog = prog
        s.mem = {}
        for m in prog['memory']:
            s.mem[int(m['addr'], 0)] = bytearray(bytes.fromhex(m['data']))
        s.gpr = [int(x, 0) for x in prog['gpr']]
        s.ymm = [0] * 16
        s.mx = 0x1f80
        s.fl = int(prog['rflags'], 0)
        s.fsbase = int(prog.get('fsbase', '0'), 0)
        s.rip0 = int(prog['rip'], 0)
        s.asm = prog['asm']
        s.ends = None
        if any('rip' in a for a in s.asm):
            ad = rip_lengths(prog)
            # ad = start addresses of each insn; end of insn i = start of insn i+1
            s.ends = ad[1:] + [len(bytes.fromhex(prog['code']))]
        s.trace = []

    # ---- memory
    def region(s, a, n):
        for b, d in s.mem.items():
            if b <= a and a + n <= b + len(d): return d, a - b
        raise Unsupported(f'mem {a:#x}+{n}')
    def rd(s, a, n):
        d, o = s.region(a, n); return int.from_bytes(d[o:o + n], 'little')
    def wr(s, a, n, v):
        d, o = s.region(a, n); d[o:o + n] = (v & ((1 << (8 * n)) - 1)).to_bytes(n, 'little')
    def ea(s, op):
        a = op['disp']
        if op['rip']: a += s.rip0 + s.ends[s.i]
        if op['base'] is not None: a += s.gpr[op['base']]
        if op['idx'] is not None: a += s.gpr[op['idx']] * op['sc']
        if op['fs']: a += s.fsbase
        return a & ((1 << 64) - 1)

    # ---- operand access
    def read(s, op, size=None):
        k = op['k']
        if k == 'v':
            sz = size or op['size']
            return s.ymm[op['i']] & ((1 << sz) - 1)
        if k == 'g': return s.gpr[op['i']] & ((1 << op['size']) - 1)
        if k == 'm':
            sz = size or op['size']; return s.rd(s.ea(op), sz // 8)
        if k == 'i': return op['v']
    def wv(s, op, val, size, vex):
        i = op['i']
        if vex: s.ymm[i] = val & ((1 << size) - 1)
        else:
            m = (1 << size) - 1
            s.ymm[i] = (s.ymm[i] & ~m) | (val & m)
    def wg(s, op, val):
        sz = op['size']; i = op['i']
        if sz == 64: s.gpr[i] = val & ((1 << 64) - 1)
        elif sz == 32: s.gpr[i] = val & 0xffffffff
        elif sz == 16: s.gpr[i] = (s.gpr[i] & ~0xffff) | (val & 0xffff)
        else: s.gpr[i] = (s.gpr[i] & ~0xff) | (val & 0xff)
    def store(s, op, val, size):
        if op['k'] == 'm': s.wr(s.ea(op), size // 8, val)
        elif op['k'] == 'g': s.wg(op, val)

    # ---- running
    def run(s):
        for i in range(len(s.asm)): s.exec(i)
        return s

    def exec(s, i):
        s.i = i; text = s.asm[i]
        parts = text.split(None, 1)
        op = parts[0]; ops = split_ops(parts[1]) if len(parts) > 1 else []
        s.st = set()
        vex = op.startswith('v') and op not in ('vzeroall',)
        base = op[1:] if (op.startswith('v') and op not in ('vzeroall', 'vzeroupper')) else op
        h = HANDLERS.get(op if op in ('vzeroall', 'vzeroupper') else base)
        if h is None: raise Unsupported(op)
        h(s, base, ops, vex)
        for f in s.st: s.mx |= FLAGBIT[f]
        s.after()

    def clone(s, mode):
        c = object.__new__(Sim)
        c.__dict__.update(s.__dict__)
        c.mode = mode
        c.mem = {b: bytearray(d) for b, d in s.mem.items()}
        c.gpr = list(s.gpr); c.ymm = list(s.ymm)
        return c

    def state(s):
        return (tuple(s.ymm), tuple(s.gpr), s.mx, s.fl, tuple((b, bytes(d)) for b, d in sorted(s.mem.items())))

    def after(s): pass

    def result(s):
        out = []
        for i in range(16): out.append(s.ymm[i])
        return out

FLAGBIT = {'IE': 1, 'DE': 2, 'ZE': 4, 'OE': 8, 'UE': 16, 'PE': 32}

# ------------------------------------------------------------------ helpers
def vsize(ops):
    for o in ops:
        if o['k'] == 'v': return o['size']
    return 128

def src12(ops, imm=False):
    """returns (dst, src1, src2, immop) for B-class ops; legacy 2-operand forms get src1=dst"""
    o = list(ops); im = None
    if o and o[-1]['k'] == 'i': im = o.pop()['v']
    if len(o) == 1: return o[0], o[0], None, im
    if len(o) == 2: return o[0], o[0], o[1], im
    return o[0], o[1], o[2], im

def nonvec_ok(o): return True

# generic binary lanewise
def H_lane(fn, w):
    def h(S, base, ops, vex):
        d, a, b, im = src12(ops); size = d['size']
        x = S.read(a, size); y = S.read(b, size)
        n = size // w
        S.wv(d, P([fn(p, q) for p, q in zip(L(x, w, n), L(y, w, n))], w), size, vex)
    return h

HANDLERS = {}
def reg(names, h):
    for n in names.split(): HANDLERS[n] = h

# ------------------------------------------------------------------ moves
def H_mov(S, base, ops, vex):
    d, s_ = ops
    if d['k'] == 'm':
        n = s_['size']; S.store(d, S.read(s_, n), n)
    else:
        n = d['size']; S.wv(d, S.read(s_, n), n, vex)
reg('movaps movups movapd movupd movdqa movdqu lddqu movntdqa movntdq movntps movntpd', H_mov)

def H_vzeroupper(S, base, ops, vex):
    for i in range(16): S.ymm[i] &= M128
def H_vzeroall(S, base, ops, vex):
    for i in range(16): S.ymm[i] = 0
HANDLERS['vzeroupper'] = H_vzeroupper; HANDLERS['vzeroall'] = H_vzeroall

def H_movss(S, base, ops, vex):
    w = 32 if base == 'movss' else 64
    if ops[0]['k'] == 'm': S.store(ops[0], S.read(ops[1], w), w); return
    d = ops[0]
    if len(ops) == 3:
        a, b = ops[1], ops[2]
        v = (S.read(a, 128) & ~((1 << w) - 1)) | (S.read(b, 128) & ((1 << w) - 1))
        S.wv(d, v, 128, vex)
    elif ops[1]['k'] == 'm':
        S.wv(d, S.read(ops[1], w), 128, vex)            # load zeroes the rest of the 128 bits
        if not vex: pass
    else:
        v = (S.read(d, 128) & ~((1 << w) - 1)) | (S.read(ops[1], 128) & ((1 << w) - 1))
        S.wv(d, v, 128, vex)
reg('movss movsd', H_movss)

def H_movdq(S, base, ops, vex):   # movd/movq
    d, s_ = ops
    w = 32 if base == 'movd' else 64
    if d['k'] == 'v':
        if s_['k'] == 'v': v = S.read(s_, 128) & ((1 << w) - 1)
        else: v = S.read(s_, w)
        if vex: S.ymm[d['i']] = v
        else: S.wv(d, v, 128, False) if True else None   # legacy movd/movq xmm,r zeroes bits 127:w, keeps 255:128
    else:
        S.store(d, S.read(s_, 128) & ((1 << w) - 1), d['size'] if d['k'] == 'g' else w)
reg('movd movq', H_movdq)

def H_movlh(S, base, ops, vex):   # movlps movlpd movhps movhpd
    hi = base.startswith('movh')
    if ops[0]['k'] == 'm':
        v = S.read(ops[1], 128); S.store(ops[0], (v >> 64) if hi else v & ((1 << 64) - 1), 64); return
    d = ops[0]
    src1 = ops[1] if len(ops) == 3 else d
    m = S.read(ops[-1], 64); a = S.read(src1, 128)
    v = (a & ((1 << 64) - 1)) | (m << 64) if hi else (a & (M128 ^ ((1 << 64) - 1))) | m
    S.wv(d, v, 128, vex)
reg('movlps movlpd movhps movhpd', H_movlh)

def H_movhl(S, base, ops, vex):
    d, a, b, _ = src12(ops)
    x, y = S.read(a, 128), S.read(b, 128)
    m = (1 << 64) - 1
    if base == 'movhlps': v = (y >> 64) | (x & ~m & M128)
    else: v = (x & m) | ((y & m) << 64)
    S.wv(d, v, 128, vex)
reg('movhlps movlhps', H_movhl)

def H_movdup(S, base, ops, vex):
    d, s_ = ops; size = d['size']
    if base == 'movddup':
        x = S.read(s_, size if s_['k'] == 'v' or s_['size'] > 64 else 64) if s_['k'] != 'm' else S.rd(S.ea(s_), 8) if size == 128 else S.rd(S.ea(s_), 32)
        if s_['k'] == 'm' and size == 128: x = S.rd(S.ea(s_), 8)
        res = []
        if size == 128: res = [x & ((1 << 64) - 1)] * 2
        else:
            q = L(x, 64, 4); res = [q[0], q[0], q[2], q[2]]
        S.wv(d, P(res, 64), size, vex); return
    x = S.read(s_, size)
    q = L(x, 32, size // 32)
    if base == 'movsldup': r = [q[i & ~1] for i in range(len(q))]
    else: r = [q[i | 1] for i in range(len(q))]
    S.wv(d, P(r, 32), size, vex)
reg('movddup movsldup movshdup', H_movdup)

def H_bcast(S, base, ops, vex):
    d, s_ = ops; size = d['size']
    w = {'broadcastss': 32, 'broadcastsd': 64, 'pbroadcastb': 8, 'pbroadcastw': 16, 'pbroadcastd': 32, 'pbroadcastq': 64}.get(base)
    if base == 'broadcastf128':
        x = S.read(s_, 128); S.wv(d, x | (x << 128), 256, vex); return
    x = S.read(s_, 128 if s_['k'] == 'v' else w) & ((1 << w) - 1)
    S.wv(d, P([x] * (size // w), w), size, vex)
reg('broadcastss broadcastsd broadcastf128 pbroadcastb pbroadcastw pbroadcastd pbroadcastq', H_bcast)

# ---- logic
def H_logic(kind):
    def h(S, base, ops, vex):
        d, a, b, _ = src12(ops); size = d['size']
        x, y = S.read(a, size), S.read(b, size)
        if kind == 'and': r = x & y
        elif kind == 'andn': r = ~x & y & ((1 << size) - 1)
        elif kind == 'or': r = x | y
        else: r = x ^ y
        S.wv(d, r, size, vex)
    return h
for k, ns in (('and', 'andps andpd pand'), ('andn', 'andnps andnpd pandn'), ('or', 'orps orpd por'), ('xor', 'xorps xorpd pxor')):
    reg(ns, H_logic(k))

# ---- integer arithmetic
def sat(x, lo, hi): return max(lo, min(hi, x))
def mask(w): return (1 << w) - 1
for w, n in ((8, 'b'), (16, 'w'), (32, 'd'), (64, 'q')):
    reg('padd' + n, H_lane(lambda a, b, w=w: (a + b) & mask(w), w))
    reg('psub' + n, H_lane(lambda a, b, w=w: (a - b) & mask(w), w))
    reg('pcmpeq' + n, H_lane(lambda a, b, w=w: mask(w) if a == b else 0, w))
    reg('pcmpgt' + n, H_lane(lambda a, b, w=w: mask(w) if sg(a, w) > sg(b, w) else 0, w))
for w, n in ((8, 'b'), (16, 'w')):
    reg('padds' + n, H_lane(lambda a, b, w=w: sat(sg(a, w) + sg(b, w), -(1 << (w - 1)), (1 << (w - 1)) - 1) & mask(w), w))
    reg('psubs' + n, H_lane(lambda a, b, w=w: sat(sg(a, w) - sg(b, w), -(1 << (w - 1)), (1 << (w - 1)) - 1) & mask(w), w))
    reg('paddus' + n, H_lane(lambda a, b, w=w: min(a + b, mask(w)), w))
    reg('psubus' + n, H_lane(lambda a, b, w=w: max(a - b, 0), w))
    reg('pavg' + n, H_lane(lambda a, b: (a + b + 1) >> 1, w))
for w, n in ((8, 'b'), (16, 'w'), (32, 'd')):
    reg('pmaxs' + n, H_lane(lambda a, b, w=w: a if sg(a, w) >= sg(b, w) else b, w))
    reg('pmins' + n, H_lane(lambda a, b, w=w: a if sg(a, w) <= sg(b, w) else b, w))
    reg('pmaxu' + n, H_lane(lambda a, b: max(a, b), w))
    reg('pminu' + n, H_lane(lambda a, b: min(a, b), w))
    reg('psign' + n, H_lane(lambda a, b, w=w: 0 if b == 0 else (a if not b >> (w - 1) else (-a) & mask(w)), w))
reg('pmullw', H_lane(lambda a, b: (a * b) & 0xffff, 16))
reg('pmulld', H_lane(lambda a, b: (a * b) & 0xffffffff, 32))
reg('pmulhw', H_lane(lambda a, b: ((sg(a, 16) * sg(b, 16)) >> 16) & 0xffff, 16))
reg('pmulhuw', H_lane(lambda a, b: (a * b) >> 16, 16))
reg('pmulhrsw', H_lane(lambda a, b: (((sg(a, 16) * sg(b, 16)) >> 14) + 1 >> 1) & 0xffff, 16))
def H_pmul64(signed):
    def h(S, base, ops, vex):
        d, a, b, _ = src12(ops); size = d['size']
        x, y = L(S.read(a, size), 32, size // 32), L(S.read(b, size), 32, size // 32)
        r = []
        for i in range(0, len(x), 2):
            p = (sg(x[i], 32) * sg(y[i], 32)) if signed else x[i] * y[i]
            r.append(p & mask(64))
        S.wv(d, P(r, 64), size, vex)
    return h
reg('pmuldq', H_pmul64(True)); reg('pmuludq', H_pmul64(False))
def H_unary_lane(fn, w):
    def h(S, base, ops, vex):
        d, a = ops; size = d['size']
        x = S.read(a, size)
        S.wv(d, P([fn(p) for p in L(x, w, size // w)], w), size, vex)
    return h
reg('pabsb', H_unary_lane(lambda a: abs(sg(a, 8)) & 0xff, 8)); reg('pabsw', H_unary_lane(lambda a: abs(sg(a, 16)) & 0xffff, 16))
reg('pabsd', H_unary_lane(lambda a: abs(sg(a, 32)) & 0xffffffff, 32))

def H_pmaddwd(S, base, ops, vex):
    d, a, b, _ = src12(ops); size = d['size']
    x, y = L(S.read(a, size), 16, size // 16), L(S.read(b, size), 16, size // 16)
    r = [(sg(x[2 * i], 16) * sg(y[2 * i], 16) + sg(x[2 * i + 1], 16) * sg(y[2 * i + 1], 16)) & 0xffffffff for i in range(size // 32)]
    S.wv(d, P(r, 32), size, vex)
reg('pmaddwd', H_pmaddwd)
def H_pmaddubsw(S, base, ops, vex):
    d, a, b, _ = src12(ops); size = d['size']
    x, y = L(S.read(a, size), 8, size // 8), L(S.read(b, size), 8, size // 8)
    r = [sat(x[2 * i] * sg(y[2 * i], 8) + x[2 * i + 1] * sg(y[2 * i + 1], 8), -32768, 32767) & 0xffff for i in range(size // 16)]
    S.wv(d, P(r, 16), size, vex)
reg('pmaddubsw', H_pmaddubsw)
def H_psadbw(S, base, ops, vex):
    d, a, b, _ = src12(ops); size = d['size']
    x, y = L(S.read(a, size), 8, size // 8), L(S.read(b, size), 8, size // 8)
    r = [sum(abs(x[8 * i + j] - y[8 * i + j]) for j in range(8)) for i in range(size // 64)]
    S.wv(d, P(r, 64), size, vex)
reg('psadbw', H_psadbw)

def per128(fn):
    """fn(a128, b128, imm) per 128-bit half"""
    def h(S, base, ops, vex):
        d, a, b, im = src12(ops); size = d['size']
        x, y = halves(S.read(a, size), size), halves(S.read(b, size), size)
        S.wv(d, unhalves([fn(p, q, im) for p, q in zip(x, y)]), size, vex)
    return h

def hop(w, sub, sat_):
    def fn(a, b, im):
        n = 128 // w
        r = []
        for src in (L(a, w, n), L(b, w, n)):
            for i in range(0, n, 2):
                x, y = sg(src[i], w), sg(src[i + 1], w)
                v = x - y if sub else x + y
                if sat_: v = sat(v, -(1 << (w - 1)), (1 << (w - 1)) - 1)
                r.append(v & mask(w))
        return P(r, w)
    return per128(fn)
reg('phaddw', hop(16, 0, 0)); reg('phaddd', hop(32, 0, 0)); reg('phaddsw', hop(16, 0, 1))
reg('phsubw', hop(16, 1, 0)); reg('phsubd', hop(32, 1, 0)); reg('phsubsw', hop(16, 1, 1))

def pack_fn(wi, wo, signed_out):
    def fn(a, b, im):
        r = []
        for v in L(a, wi, 128 // wi) + L(b, wi, 128 // wi):
            x = sg(v, wi)
            if signed_out: r.append(sat(x, -(1 << (wo - 1)), (1 << (wo - 1)) - 1) & mask(wo))
            else: r.append(sat(x, 0, mask(wo)))
        return P(r, wo)
    return per128(fn)
reg('packsswb', pack_fn(16, 8, True)); reg('packssdw', pack_fn(32, 16, True)); reg('packuswb', pack_fn(16, 8, False)); reg('packusdw', pack_fn(32, 16, False))

def unpck(w, hi):
    def fn(a, b, im):
        n = 128 // w; o = n // 2 if hi else 0
        A, B = L(a, w, n), L(b, w, n); r = []
        for i in range(n // 2): r += [A[o + i], B[o + i]]
        return P(r, w)
    return per128(fn)
for n_, w in (('bw', 8), ('wd', 16), ('dq', 32), ('qdq', 64)):
    reg('punpckl' + n_, unpck(w, False)); reg('punpckh' + n_, unpck(w, True))
reg('unpcklps', unpck(32, False)); reg('unpckhps', unpck(32, True)); reg('unpcklpd', unpck(64, False)); reg('unpckhpd', unpck(64, True))

def pshufb_fn(a, b, im):
    A, B = L(a, 8, 16), L(b, 8, 16)
    return P([0 if x & 0x80 else A[x & 15] for x in B], 8)
reg('pshufb', per128(pshufb_fn))
def palignr_fn(a, b, im):
    v = (a << 128) | b
    return (v >> (8 * im)) & M128 if im < 32 else 0
reg('palignr', per128(palignr_fn))

def shiftops(ops):
    if ops[-1]['k'] == 'i':
        if len(ops) == 3: return ops[0], ops[1], None, ops[2]['v']
        return ops[0], ops[0], None, ops[1]['v']
    return src12(ops)
def H_shift(w, kind):
    def h(S, base, ops, vex):
        d, a, b, im = shiftops(ops); size = d['size']
        x = L(S.read(a, size), w, size // w)
        if im is not None: cnt = im
        else:
            cv = S.read(b, 128); cnt = cv & mask(64)
        out = []
        for e in x:
            if kind == 'l': out.append((e << cnt) & mask(w) if cnt < w else 0)
            elif kind == 'r': out.append(e >> cnt if cnt < w else 0)
            else: out.append((sg(e, w) >> min(cnt, w - 1)) & mask(w))
        S.wv(d, P(out, w), size, vex)
    return h
for w, n in ((16, 'w'), (32, 'd'), (64, 'q')):
    reg('psll' + n, H_shift(w, 'l')); reg('psrl' + n, H_shift(w, 'r'))
    if w != 64: reg('psra' + n, H_shift(w, 'a'))
def H_shiftv(w, kind):
    def h(S, base, ops, vex):
        d, a, b, _ = src12(ops); size = d['size']
        x, c = L(S.read(a, size), w, size // w), L(S.read(b, size), w, size // w)
        out = []
        for e, k in zip(x, c):
            if kind == 'l': out.append((e << k) & mask(w) if k < w else 0)
            elif kind == 'r': out.append(e >> k if k < w else 0)
            else: out.append((sg(e, w) >> min(k, w - 1)) & mask(w))
        S.wv(d, P(out, w), size, vex)
    return h
reg('psllvd', H_shiftv(32, 'l')); reg('psllvq', H_shiftv(64, 'l')); reg('psrlvd', H_shiftv(32, 'r')); reg('psrlvq', H_shiftv(64, 'r')); reg('psravd', H_shiftv(32, 'a'))
def H_bshift(left):
    def h(S, base, ops, vex):
        d, a, b, im = shiftops(ops); size = d['size']
        x = halves(S.read(a, size), size); r = []
        for v in x:
            if im > 15: r.append(0)
            elif left: r.append((v << (8 * im)) & M128)
            else: r.append(v >> (8 * im))
        S.wv(d, unhalves(r), size, vex)
    return h
reg('pslldq', H_bshift(True)); reg('psrldq', H_bshift(False))

def H_mpsadbw(S, base, ops, vex):
    d, a, b, im = src12(ops); size = d['size']
    ha, hb = halves(S.read(a, size), size), halves(S.read(b, size), size); out = []
    for k, (x, y) in enumerate(zip(ha, hb)):
        ctl = im >> (3 * k)
        so = ((ctl >> 2) & 1) * 4; do = (ctl & 3) * 4
        X, Y = L(x, 8, 16), L(y, 8, 16)
        r = []
        for i in range(8):
            r.append(sum(abs(X[so + i + j] - Y[do + j]) for j in range(4)))
        out.append(P(r, 16))
    S.wv(d, unhalves(out), size, vex)
reg('mpsadbw', H_mpsadbw)
def H_phmin(S, base, ops, vex):
    d, a = ops; x = L(S.read(a, 128), 16, 8)
    m = min(x); i = x.index(m)
    S.wv(d, m | (i << 16), 128, vex)
reg('phminposuw', H_phmin)

def H_pmov(S, base, ops, vex):
    d, a = ops; size = d['size']
    kind = base[4]; sw = {'b': 8, 'w': 16, 'd': 32}[base[6]]; dw = {'w': 16, 'd': 32, 'q': 64}[base[7]]
    n = size // dw
    x = S.read(a, 128 if a['k'] == 'v' else n * sw)
    x &= mask(n * sw) if n * sw < 128 else M128
    r = []
    for v in L(x, sw, n): r.append((sg(v, sw) if kind == 's' else v) & mask(dw))
    S.wv(d, P(r, dw), size, vex)
for k in 'sz':
    for sw, dw in (('b', 'w'), ('b', 'd'), ('b', 'q'), ('w', 'd'), ('w', 'q'), ('d', 'q')): reg(f'pmov{k}x{sw}{dw}', H_pmov)

# ---- blends / shuffles
def H_blend(w):
    def fn(a, b, im):
        n = 128 // w
        return P([(L(b, w, n)[i] if im >> i & 1 else L(a, w, n)[i]) for i in range(n)], w)
    def h(S, base, ops, vex):
        d, a, b, im = src12(ops); size = d['size']
        ha, hb = halves(S.read(a, size), size), halves(S.read(b, size), size)
        n = 128 // w
        r = []
        for k, (x, y) in enumerate(zip(ha, hb)):
            imm = im >> (k * n) if base in ('blendps', 'blendpd') and False else im
            # imm bits index element within the full vector for dd/ps/pd; pblendw uses same imm for both halves
            if base == 'pblendw': ii = im
            else: ii = (im >> (k * n))
            r.append(fn(x, y, ii))
        S.wv(d, unhalves(r), size, vex)
    return h
reg('blendps pblendd', H_blend(32)); reg('blendpd', H_blend(64)); reg('pblendw', H_blend(16))

def H_blendv(w):
    def h(S, base, ops, vex):
        if len(ops) == 4: d, a, b, m = ops
        else: d, b, m = ops; a = d; m = dict(k='v', i=0, size=128)
        size = d['size']
        x, y, k = S.read(a, size), S.read(b, size), S.read(m, size)
        n = size // w
        r = [(Y if (K >> (w - 1)) & 1 else X) for X, Y, K in zip(L(x, w, n), L(y, w, n), L(k, w, n))]
        S.wv(d, P(r, w), size, vex)
    return h
reg('blendvps', H_blendv(32)); reg('blendvpd', H_blendv(64)); reg('pblendvb', H_blendv(8))

def H_shufps(S, base, ops, vex):
    d, a, b, im = src12(ops); size = d['size']
    ha, hb = halves(S.read(a, size), size), halves(S.read(b, size), size); out = []
    for x, y in zip(ha, hb):
        if base == 'shufps':
            X, Y = L(x, 32, 4), L(y, 32, 4)
            out.append(P([X[im & 3], X[im >> 2 & 3], Y[im >> 4 & 3], Y[im >> 6 & 3]], 32))
        else:
            k = len(out)
            X, Y = L(x, 64, 2), L(y, 64, 2)
            out.append(P([X[(im >> (2 * k)) & 1], Y[(im >> (2 * k + 1)) & 1]], 64))
    S.wv(d, unhalves(out), size, vex)
reg('shufps shufpd', H_shufps)
def H_pshuf(S, base, ops, vex):
    d, a, im_ = ops[0], ops[1], ops[2]; im = im_['v']; size = d['size']
    out = []
    for x in halves(S.read(a, size), size):
        if base == 'pshufd':
            X = L(x, 32, 4); out.append(P([X[im >> (2 * i) & 3] for i in range(4)], 32))
        elif base == 'pshuflw':
            X = L(x, 16, 8); out.append(P([X[im >> (2 * i) & 3] for i in range(4)] + X[4:], 16))
        else:
            X = L(x, 16, 8); out.append(P(X[:4] + [X[4 + (im >> (2 * i) & 3)] for i in range(4)], 16))
    S.wv(d, unhalves(out), size, vex)
reg('pshufd pshuflw pshufhw', H_pshuf)

def H_permil(S, base, ops, vex):
    d = ops[0]; size = d['size']; w = 32 if base == 'permilps' else 64
    x = S.read(ops[1], size)
    third = ops[2]
    out = []
    for k, h in enumerate(halves(x, size)):
        n = 128 // w; X = L(h, w, n)
        if third['k'] == 'i':
            im = third['v']
            if w == 32: out.append(P([X[im >> (2 * i) & 3] for i in range(4)], 32))
            else: out.append(P([X[im >> (2 * k + i) & 1] for i in range(2)], 64))
        else:
            c = halves(S.read(third, size), size)[k]; C = L(c, w, n)
            if w == 32: out.append(P([X[C[i] & 3] for i in range(4)], 32))
            else: out.append(P([X[C[i] >> 1 & 1] for i in range(2)], 64))
    S.wv(d, unhalves(out), size, vex)
reg('permilps permilpd', H_permil)

def H_perm_cross(S, base, ops, vex):
    d = ops[0]; size = 256
    if base in ('permd', 'permps'):
        idx, src = L(S.read(ops[1], 256), 32, 8), L(S.read(ops[2], 256), 32, 8)
        S.wv(d, P([src[i & 7] for i in idx], 32), 256, vex)
    else:
        src = L(S.read(ops[1], 256), 64, 4); im = ops[2]['v']
        S.wv(d, P([src[im >> (2 * i) & 3] for i in range(4)], 64), 256, vex)
reg('permd permps permq permpd', H_perm_cross)
def H_perm2(S, base, ops, vex):
    d, a, b, im = src12(ops)
    h = halves(S.read(a, 256), 256) + halves(S.read(b, 256), 256)
    r = []
    for i in range(2):
        c = im >> (4 * i) & 0xf
        r.append(0 if c & 8 else h[c & 3])
    S.wv(d, unhalves(r), 256, vex)
reg('perm2f128 perm2i128', H_perm2)
def H_insert128(S, base, ops, vex):
    d, a, b, im = src12(ops)
    h = halves(S.read(a, 256), 256); h[im & 1] = S.read(b, 128)
    S.wv(d, unhalves(h), 256, vex)
reg('insertf128 inserti128', H_insert128)
def H_extract128(S, base, ops, vex):
    d, a, im = ops
    h = halves(S.read(a, 256), 256)[im['v'] & 1]
    if d['k'] == 'm': S.store(d, h, 128)
    else: S.wv(d, h, 128, vex)
reg('extractf128 extracti128', H_extract128)

def H_insertps(S, base, ops, vex):
    d, a, b, im = src12(ops)
    cs = im >> 6 & 3; cd = im >> 4 & 3; zm = im & 15
    if b['k'] == 'm': v = S.read(b, 32)
    else: v = L(S.read(b, 128), 32, 4)[cs]
    X = L(S.read(a, 128), 32, 4); X[cd] = v
    for i in range(4):
        if zm >> i & 1: X[i] = 0
    S.wv(d, P(X, 32), 128, vex)
reg('insertps', H_insertps)
def H_extractps(S, base, ops, vex):
    d, a, im = ops
    v = L(S.read(a, 128), 32, 4)[im['v'] & 3]
    S.store(d, v, 32)
reg('extractps', H_extractps)
def H_pins(S, base, ops, vex):
    w = {'pinsrb': 8, 'pinsrw': 16, 'pinsrd': 32, 'pinsrq': 64}[base]
    d, a, b, im = src12(ops)
    n = 128 // w
    X = L(S.read(a, 128), w, n); X[im & (n - 1)] = S.read(b, w) & mask(w) if b['k'] != 'g' else S.read(b) & mask(w)
    S.wv(d, P(X, w), 128, vex)
reg('pinsrb pinsrw pinsrd pinsrq', H_pins)
def H_pext(S, base, ops, vex):
    w = {'pextrb': 8, 'pextrw': 16, 'pextrd': 32, 'pextrq': 64}[base]
    d, a, im = ops
    v = L(S.read(a, 128), w, 128 // w)[im['v'] & (128 // w - 1)]
    if d['k'] == 'g': S.wg(d, v)    # zero-extends
    else: S.store(d, v, w)
reg('pextrb pextrw pextrd pextrq', H_pext)
def H_movmsk(S, base, ops, vex):
    d, a = ops; size = a['size']
    x = S.read(a, size)
    w = {'movmskps': 32, 'movmskpd': 64, 'pmovmskb': 8}[base]
    r = 0
    for i, e in enumerate(L(x, w, size // w)): r |= (e >> (w - 1) & 1) << i
    S.wg(d, r)
reg('movmskps movmskpd pmovmskb', H_movmsk)

# ---- tests / flags
def setfl(S, **kw):
    bits = dict(CF=1, PF=4, AF=0x10, ZF=0x40, SF=0x80, OF=0x800)
    for k, v in kw.items():
        if v: S.fl |= bits[k]
        else: S.fl &= ~bits[k]
def H_ptest(S, base, ops, vex):
    a, b = ops; size = a['size']
    x, y = S.read(a, size), S.read(b, size)
    if base == 'ptest': zf = (x & y) == 0; cf = (~x & y & ((1 << size) - 1)) == 0
    else:
        w = 32 if base == 'testps' else 64
        sm = P([1 << (w - 1)] * (size // w), w)
        zf = (x & y & sm) == 0; cf = (~x & y & sm) == 0
    setfl(S, ZF=zf, CF=cf, PF=0, AF=0, OF=0, SF=0)
reg('ptest testps testpd', H_ptest)

# ---- GPR instrs
def H_ldmxcsr(S, base, ops, vex): S.mx = S.read(ops[0]) & 0xffffffff
def H_stmxcsr(S, base, ops, vex): S.store(ops[0], S.mx, 32)
reg('ldmxcsr', H_ldmxcsr); reg('stmxcsr', H_stmxcsr)
def H_and(S, base, ops, vex):
    d, im = ops; sz = d['size']
    r = S.read(d) & (im['v'] & mask(sz))
    S.store(d, r, sz)
    setfl(S, CF=0, OF=0, AF=0, ZF=r == 0, SF=r >> (sz - 1) & 1, PF=bin(r & 0xff).count('1') % 2 == 0)
reg('and', H_and)
def H_xor(S, base, ops, vex):
    d, b = ops
    r = S.read(d) ^ S.read(b); S.store(d, r, d['size'])
    setfl(S, CF=0, OF=0, AF=0, ZF=r == 0, SF=r >> (d['size'] - 1) & 1, PF=bin(r & 0xff).count('1') % 2 == 0)
reg('xor', H_xor)
def H_lahf(S, base, ops, vex):
    ah = (S.fl & 0xd5) | 2
    S.gpr[0] = (S.gpr[0] & ~0xff00) | (ah << 8)
reg('lahf', H_lahf)
def H_seto(S, base, ops, vex): S.wg(ops[0], 1 if S.fl & 0x800 else 0)
reg('seto', H_seto)
def H_rol(S, base, ops, vex):
    d, c = ops; sz = d['size']; n = c['v'] & (63 if sz == 64 else 31) % sz
    v = S.read(d)
    if n:
        r = ((v << n) | (v >> (sz - n))) & mask(sz); S.store(d, r, sz)
        setfl(S, CF=r & 1, OF=(r >> (sz - 1) & 1) ^ (r & 1))
reg('rol', H_rol)

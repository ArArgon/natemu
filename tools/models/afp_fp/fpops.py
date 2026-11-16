from sim import *
import sim, fp
from fp import F16, F32, F64, cls, enc, rnd, rint, p2, floorlog2, unpack, quiet, dnan

BIG = Fr(10) ** 5000
def isnan(c): return c in ('qnan', 'snan')
def sval(c, s, v):
    if c == 'inf': return -BIG if s else BIG
    return -v if s else v
def FF(base): return F32 if base.endswith(('ps', 's', 'ss')) and not base.endswith(('pd', 'sd')) else F64
def fmt_of(base):
    return F32 if base[-2:] in ('ps', 'ss') else F64
def is_scalar(base): return base[-2:] in ('ss', 'sd')

def mk_fbin(fn):
    def h(S, base, ops, vex):
        f = fmt_of(base); w = f.w
        d, a, b, im = src12(ops)
        if is_scalar(base):
            x = S.read(a, 128); y = S.read(b, w) if b['k'] == 'm' else S.read(b, 128) & mask(w)
            r = fn(S, f, x & mask(w), y, base, 0)
            S.wv(d, (x & ~mask(w) & M128) | r, 128, vex)
        else:
            size = d['size']; n = size // w
            X, Y = L(S.read(a, size), w, n), L(S.read(b, size), w, n)
            S.wv(d, P([fn(S, f, p, q, base, i) for i, (p, q) in enumerate(zip(X, Y))], w), size, vex)
    return h

def f_add(S, f, a, b, base, i): return fp.addsub(f, a, b, False, S.mode, S.mx, S.st)
def f_sub(S, f, a, b, base, i): return fp.addsub(f, a, b, True, S.mode, S.mx, S.st)
def f_mul(S, f, a, b, base, i): return fp.mul(f, a, b, S.mode, S.mx, S.st)
def f_div(S, f, a, b, base, i): return fp.div(f, a, b, S.mode, S.mx, S.st)
def f_minmax(ismax):
    def fn(S, f, a, b, base, i):
        ca, sa, va = unpack(f, a, S.mode, S.mx, S.st); cb, sb_, vb = unpack(f, b, S.mode, S.mx, S.st)
        if isnan(ca) or isnan(cb):
            if S.mode.minmax_qnan_ie or 'snan' in (ca, cb) or not is_scalar(base): S.st.add('IE')
            return b
        A, B = sval(ca, sa, va), sval(cb, sb_, vb)
        if A == 0 and B == 0: return b
        if ismax: return a if A > B else b
        return a if A < B else b
    return fn
for nm, fn in (('add', f_add), ('sub', f_sub), ('mul', f_mul), ('div', f_div), ('min', f_minmax(False)), ('max', f_minmax(True))):
    reg(f'{nm}ps {nm}pd {nm}ss {nm}sd', mk_fbin(fn))

def hadd1(S, f, x, y, sub):
    if S.mode.hadd_swap and not sub:
        cx, cy = cls(f, x)[0], cls(f, y)[0]
        if isnan(cx) and isnan(cy): return fp.addsub(f, y, x, False, S.mode, S.mx, S.st)
    return fp.addsub(f, x, y, sub, S.mode, S.mx, S.st)
def H_hop(S, base, ops, vex):
    f = fmt_of(base); w = f.w; sub = base.startswith('hsub')
    d, a, b, _ = src12(ops); size = d['size']; n = 128 // w
    r = []
    for x, y in zip(halves(S.read(a, size), size), halves(S.read(b, size), size)):
        for src in (L(x, w, n), L(y, w, n)):
            for i in range(0, n, 2):
                r.append(hadd1(S, f, src[i], src[i + 1], sub))
    S.wv(d, P(r, w), size, vex)
reg('haddps haddpd hsubps hsubpd', H_hop)

def H_addsub(S, base, ops, vex):
    f = fmt_of(base); w = f.w
    d, a, b, _ = src12(ops); size = d['size']; n = size // w
    X, Y = L(S.read(a, size), w, n), L(S.read(b, size), w, n); r = []
    if not S.mode.negflip_pk:   # fast mode without AFP: the subtrahend is multiplied by (-1, 1) first, which quiets an SNaN (IE) before the add
        for i in range(n):
            if cls(f, Y[i])[0] == 'snan': S.st.add('IE'); Y[i] = quiet(f, Y[i])
    for i in range(n):
        if i % 2 == 0:
            y = Y[i]
            if S.mode.negflip_pk and isnan(cls(f, y)[0]): y ^= f.sign
            r.append(fp.addsub(f, X[i], y, True, S.mode, S.mx, S.st))
        else: r.append(fp.addsub(f, X[i], Y[i], False, S.mode, S.mx, S.st))
    S.wv(d, P(r, w), size, vex)
reg('addsubps addsubpd', H_addsub)

def H_dp(S, base, ops, vex):
    f = F32 if base == 'dpps' else F64; w = f.w
    d, a, b, im = src12(ops); size = d['size']
    out = []
    for x, y in zip(halves(S.read(a, size), size), halves(S.read(b, size), size)):
        n = 128 // w
        X, Y = L(x, w, n), L(y, w, n)
        prods = []
        for i in range(n):
            if im >> (4 + i) & 1: prods.append(fp.mul(f, X[i], Y[i], S.mode, S.mx, S.st))
            else: prods.append(0)
        if n == 4: s = fp.addsub(f, fp.addsub(f, prods[0], prods[1], False, S.mode, S.mx, S.st), fp.addsub(f, prods[2], prods[3], False, S.mode, S.mx, S.st), False, S.mode, S.mx, S.st)
        else: s = fp.addsub(f, prods[0], prods[1], False, S.mode, S.mx, S.st)
        out.append(P([s if im >> i & 1 else 0 for i in range(n)], w))
    S.wv(d, unhalves(out), size, vex)
reg('dpps dppd', H_dp)

def H_sqrt(S, base, ops, vex):
    f = fmt_of(base); w = f.w
    if is_scalar(base):
        d, a, b, _ = src12(ops)
        x = S.read(a, 128); y = (S.read(b, w) if b['k'] == 'm' else S.read(b, 128) & mask(w))
        r = fp.sqrt(f, y, S.mode, S.mx, S.st)
        S.wv(d, (x & ~mask(w) & M128) | r, 128, vex)
    else:
        d, a = ops; size = d['size']
        S.wv(d, P([fp.sqrt(f, v, S.mode, S.mx, S.st) for v in L(S.read(a, size), w, size // w)], w), size, vex)
reg('sqrtps sqrtpd sqrtss sqrtsd', H_sqrt)
def rcp_fn(S, f, v): return fp.div(f, 0x3f800000, v, S.mode, S.mx, set())
def rsq_fn(S, f, v):
    t = fp.sqrt(f, v, S.mode, S.mx, set())
    return fp.div(f, 0x3f800000, t, S.mode, S.mx, set())
def H_rcp(fn):
    def h(S, base, ops, vex):
        f = F32; w = 32
        if is_scalar(base):
            d, a, b, _ = src12(ops)
            x = S.read(a, 128); y = (S.read(b, w) if b['k'] == 'm' else S.read(b, 128) & mask(w))
            S.wv(d, (x & ~mask(w) & M128) | fn(S, f, y), 128, vex)
        else:
            d, a = ops; size = d['size']
            S.wv(d, P([fn(S, f, v) for v in L(S.read(a, size), w, size // w)], w), size, vex)
    return h
reg('rcpps rcpss', H_rcp(rcp_fn)); reg('rsqrtps rsqrtss', H_rcp(rsq_fn))

# ---- compares
SIGPREDS = {1, 2, 5, 6, 9, 10, 13, 14, 16, 19, 20, 23, 24, 27, 28, 31}
def cmp_pred(S, f, a, b, p):
    ca, sa, va = unpack(f, a, S.mode, S.mx, S.st); cb, sb_, vb = unpack(f, b, S.mode, S.mx, S.st)
    u = isnan(ca) or isnan(cb)
    if p in SIGPREDS:
        if u: S.st.add('IE')
    elif 'snan' in (ca, cb): S.st.add('IE')
    if u: lt = eq = gt = False
    else:
        A, B = sval(ca, sa, va), sval(cb, sb_, vb); lt, eq, gt = A < B, A == B, A > B
    q = p & 15
    r = {0: eq, 1: lt, 2: lt or eq, 3: u, 4: not eq, 5: not lt, 6: not (lt or eq), 7: not u, 8: eq or u, 9: lt or u, 10: (lt or eq) or u,
         11: False, 12: (not eq) and not u, 13: gt or eq, 14: gt, 15: True}
    # q=9 NGE = not >= = lt or u ; q=10 NGT = not > = lt or eq or u ; q=13 GE ordered ; q=14 GT ordered
    return r[q]
PREDNAMES = {}
_base = ['eq_oq', 'lt_os', 'le_os', 'unord_q', 'neq_uq', 'nlt_us', 'nle_us', 'ord_q', 'eq_uq', 'nge_us', 'ngt_us', 'false_oq', 'neq_oq', 'ge_os', 'gt_os', 'true_uq',
         'eq_os', 'lt_oq', 'le_oq', 'unord_s', 'neq_us', 'nlt_uq', 'nle_uq', 'ord_s', 'eq_us', 'nge_uq', 'ngt_uq', 'false_os', 'neq_os', 'ge_oq', 'gt_oq', 'true_us']
def H_cmp(S, base, ops, vex):
    f = fmt_of(base); w = f.w
    d, a, b, im = src12(ops); p = im & (31 if vex else 7)
    if is_scalar(base):
        x = S.read(a, 128); y = S.read(b, w) if b['k'] == 'm' else S.read(b, 128) & mask(w)
        r = mask(w) if cmp_pred(S, f, x & mask(w), y, p) else 0
        S.wv(d, (x & ~mask(w) & M128) | r, 128, vex)
    else:
        size = d['size']; n = size // w
        X, Y = L(S.read(a, size), w, n), L(S.read(b, size), w, n)
        S.wv(d, P([mask(w) if cmp_pred(S, f, p_, q_, p) else 0 for p_, q_ in zip(X, Y)], w), size, vex)
reg('cmpps cmppd cmpss cmpsd', H_cmp)

def H_comis(S, base, ops, vex):
    f = F32 if base.endswith('ss') else F64; w = f.w
    a, b = ops
    x = S.read(a, 128) & mask(w); y = S.read(b, w) if b['k'] == 'm' else S.read(b, 128) & mask(w)
    ca, sa, va = unpack(f, x, S.mode, S.mx, S.st); cb, sb_, vb = unpack(f, y, S.mode, S.mx, S.st)
    if isnan(ca) or isnan(cb):
        if base.startswith('comis') or 'snan' in (ca, cb): S.st.add('IE')
        setfl(S, ZF=1, PF=1, CF=1, OF=0, SF=0, AF=0)
    else:
        A, B = sval(ca, sa, va), sval(cb, sb_, vb)
        setfl(S, ZF=A == B, PF=0, CF=A < B, OF=0, SF=0, AF=0)
reg('comiss comisd ucomiss ucomisd', H_comis)

# ---- FMA
def fma_core(S, f, a, b, c, negp, negc, ndst_neg_a):
    mode = S.mode; mx = S.mx; st = S.st; rc = mx >> 13 & 3
    ca, sa, va = unpack(f, a, mode, mx, st); cb, sb_, vb = unpack(f, b, mode, mx, st); cc, sc, vc = unpack(f, c, mode, mx, st)
    infzero = (ca == 'inf' and cb == 'zero') or (ca == 'zero' and cb == 'inf')
    nans = [(a, ca, 'a'), (b, cb, 'b'), (c, cc, 'c')]
    if any(isnan(x[1]) for x in nans):
        if mode.fma_inf0q and cc == 'qnan' and infzero:
            st.add('IE'); return dnan(f, mode)
        if any(x[1] == 'snan' for x in nans): st.add('IE')
        pick = None
        if mode.fma == 'arm':
            for want in ('snan', 'qnan'):
                for k in ('c', 'a', 'b'):
                    t = [x for x in nans if x[2] == k][0]
                    if t[1] == want: pick = t; break
                if pick: break
        else:
            for k in ('a', 'b', 'c'):
                t = [x for x in nans if x[2] == k][0]
                if isnan(t[1]): pick = t; break
        r = quiet(f, pick[0])
        if (mode.negflip and pick[2] == 'a' and negp) or (pick[2] == 'c' and negc and (mode.negflip_pk if not S.fma_scalar else mode.negflip)): r ^= f.sign
        return r
    if infzero:
        st.add('IE'); return dnan(f, mode)
    ps = sa ^ sb_ ^ (1 if negp else 0); cs = sc ^ (1 if negc else 0)
    pinf = ca == 'inf' or cb == 'inf'
    if pinf and cc == 'inf':
        if ps != cs: st.add('IE'); return dnan(f, mode)
        return f.inf | (f.sign if ps else 0)
    if pinf: return f.inf | (f.sign if ps else 0)
    if cc == 'inf': return f.inf | (f.sign if cs else 0)
    P_ = va * vb; C_ = vc
    tot = (-P_ if ps else P_) + (-C_ if cs else C_)
    if tot == 0:
        if P_ == 0 and C_ == 0: sign = ps if ps == cs else (1 if rc == 1 else 0)
        else: sign = 1 if rc == 1 else 0
        return f.sign if sign else 0
    return rnd(f, tot < 0, abs(tot), rc, st, mode, mx)

FMAMAP = {'132': ('d', 'c3', 'c2'), '213': ('c2', 'd', 'c3'), '231': ('c2', 'c3', 'd')}
def H_fma(S, base, ops, vex):
    m = re.fullmatch(r'(fmadd|fmsub|fnmadd|fnmsub|fmaddsub|fmsubadd)(132|213|231)(ps|pd|ss|sd)', base)
    kind, form, sfx = m.groups()
    f = F32 if sfx[1] == 's' else F64; w = f.w
    d, s2, s3 = ops; size = d['size'] if sfx[0] == 'p' else 128
    scalar = sfx[0] == 's'
    n = 1 if scalar else size // w
    D = L(S.read(d, size), w, size // w); B = L(S.read(s2, size), w, size // w)
    C = L(S.read(s3, size if not scalar else w), w, size // w if not scalar else 1)
    if scalar: C = [S.read(s3, w) if s3['k'] == 'm' else S.read(s3, 128) & mask(w)]
    S.fma_order = ['a', 'b', 'c']; S.fma_scalar = scalar
    r = list(D)
    for i in range(n):
        roles = dict(d=D[i], c2=B[i], c3=C[i])
        a_, b_, c_ = (roles[k] for k in FMAMAP[form])
        if kind == 'fmadd': np_, nc_ = 0, 0
        elif kind == 'fmsub': np_, nc_ = 0, 1
        elif kind == 'fnmadd': np_, nc_ = 1, 0
        elif kind == 'fnmsub': np_, nc_ = 1, 1
        elif kind == 'fmaddsub': np_, nc_ = 0, 1 if i % 2 == 0 else 0
        else: np_, nc_ = 0, 0 if i % 2 == 0 else 1
        if not scalar and not S.mode.negflip_pk and (nc_ or kind in ('fmaddsub', 'fmsubadd')) and cls(f, c_)[0] == 'snan':
            S.st.add('IE'); c_ = quiet(f, c_)   # fast mode without AFP: the addend is multiplied by -1.0 / (-1, 1) first, which quiets an SNaN
        r[i] = fma_core(S, f, a_, b_, c_, np_, nc_, 0)
    S.wv(d, P(r, w) if not scalar else (S.read(d, 128) & ~mask(w) & M128) | r[0], size, vex)
for k in ('fmadd', 'fmsub', 'fnmadd', 'fnmsub', 'fmaddsub', 'fmsubadd'):
    for o in ('132', '213', '231'):
        for t in ('ps', 'pd') + (('ss', 'sd') if 'sub' not in k or k in ('fmsub', 'fnmsub') else ()):
            if k in ('fmaddsub', 'fmsubadd') and t in ('ss', 'sd'): continue
            reg(f'{k}{o}{t}', H_fma)

# ---- conversions
def f2int(S, f, bits, rc, outw, trunc=False):
    c, s, v = unpack(f, bits, S.mode, S.mx, S.st)
    indef = 1 << (outw - 1)
    if isnan(c): S.st.add('IE'); return indef
    if c == 'inf': S.st.add('IE'); return indef
    q, inex = rint(v, s, 3 if trunc else rc)
    val = -q if s else q
    if val < -(1 << (outw - 1)) or val >= (1 << (outw - 1)): S.st.add('IE'); return indef
    if inex: S.st.add('PE')
    return val & mask(outw)
def int2f(S, f, v, w):
    v = sg(v & mask(w), w)
    if v == 0: return 0
    return rnd(f, v < 0, Fr(abs(v)), S.mx >> 13 & 3, S.st, S.mode, S.mx, nofz=True)
def f2f(S, fi, fo, bits, nofz=False):
    c, s, v = unpack(fi, bits, S.mode, S.mx, S.st)
    sb = fo.sign if s else 0
    if c == 'snan': S.st.add('IE')
    if isnan(c):
        m = bits & fi.manmask
        if fo.mb > fi.mb: m <<= fo.mb - fi.mb
        else: m >>= fi.mb - fo.mb
        return sb | fo.expmask | m | fo.qbit
    if c == 'inf': return sb | fo.inf
    if c == 'zero': return sb
    return rnd(fo, s, v, S.mx >> 13 & 3, S.st, S.mode, S.mx, nofz=nofz)

def H_cvt(S, base, ops, vex):
    t = base
    if t in ('cvtdq2ps', 'cvtps2dq', 'cvttps2dq'):
        d, a = ops; size = d['size']; X = L(S.read(a, size), 32, size // 32)
        if t == 'cvtdq2ps': r = [int2f(S, F32, v, 32) for v in X]
        else: r = [f2int(S, F32, v, S.mx >> 13 & 3, 32, t == 'cvttps2dq') for v in X]
        S.wv(d, P(r, 32), size, vex)
    elif t == 'cvtdq2pd':
        d, a = ops; size = d['size']; n = size // 64
        X = L(S.read(a, size // 2 if a['k'] == 'm' else 128) & mask(size // 2), 32, n)
        S.wv(d, P([enc(F64, sg(v, 32) < 0, Fr(abs(sg(v, 32)))) for v in X], 64), size, vex)
    elif t == 'cvtps2pd':
        d, a = ops; size = d['size']; n = size // 64
        X = L(S.read(a, size // 2) if a['k'] == 'm' else S.read(a, 128) & mask(size // 2), 32, n)
        S.wv(d, P([f2f(S, F32, F64, v) for v in X], 64), size, vex)
    elif t in ('cvtpd2ps', 'cvtpd2dq', 'cvttpd2dq', 'cvtpd2psx', 'cvtpd2psy', 'cvtpd2dqx', 'cvtpd2dqy', 'cvttpd2dqx', 'cvttpd2dqy'):
        d, a = ops
        asz = a['size']
        X = L(S.read(a, asz), 64, asz // 64)
        if t.startswith('cvtpd2ps'): r = [f2f(S, F64, F32, v) for v in X]
        else: r = [f2int(S, F64, v, S.mx >> 13 & 3, 32, t.startswith('cvtt')) for v in X]
        S.wv(d, P(r, 32), 128, vex)
    elif t in ('cvtss2sd', 'cvtsd2ss'):
        d, a, b, _ = src12(ops)
        fi, fo = (F32, F64) if t == 'cvtss2sd' else (F64, F32)
        x = S.read(a, 128); y = S.read(b, fi.w) if b['k'] == 'm' else S.read(b, 128) & mask(fi.w)
        r = f2f(S, fi, fo, y)
        S.wv(d, (x & ~mask(fo.w) & M128) | r, 128, vex)
    elif t in ('cvtsi2ss', 'cvtsi2sd'):
        d, a, b, _ = src12(ops)
        f = F32 if t.endswith('ss') else F64
        iw = b['size']
        v = S.read(b, iw)
        x = S.read(a, 128)
        r = int2f(S, f, v, iw)
        S.wv(d, (x & ~mask(f.w) & M128) | r, 128, vex)
    elif t in ('cvtss2si', 'cvtsd2si', 'cvttss2si', 'cvttsd2si'):
        d, a = ops
        f = F32 if t.endswith('ss2si') else F64
        y = S.read(a, f.w) if a['k'] == 'm' else S.read(a, 128) & mask(f.w)
        S.wg(d, f2int(S, f, y, S.mx >> 13 & 3, d['size'], t.startswith('cvtt')))
    elif t == 'cvtph2ps':
        d, a = ops; size = d['size']; n = size // 32
        X = L(S.read(a, size // 2) if a['k'] == 'm' else S.read(a, 128) & mask(size // 2), 16, n)
        S.wv(d, P([f2f_nodaz(S, F16, F32, v) for v in X], 32), size, vex)
    elif t == 'cvtps2ph':
        d, a, im = ops; imm = im['v']
        asz = a['size']; X = L(S.read(a, asz), 32, asz // 32)
        rcv = S.mx >> 13 & 3 if imm & 4 else imm & 3
        smx = S.mx; S.mx = (S.mx & ~0x6000) | (rcv << 13)
        r = [f2f(S, F32, F16, v, nofz=True) for v in X]
        S.mx = smx
        if d['k'] == 'm': S.store(d, P(r, 16), asz // 2)
        else: S.wv(d, P(r, 16), 128, vex)
    else: raise Unsupported(t)
def f2f_nodaz(S, fi, fo, bits):
    mx = S.mx; S.mx = mx & ~0x8040
    try: return f2f(S, fi, fo, bits)
    finally: S.mx = mx
reg('cvtdq2ps cvtps2dq cvttps2dq cvtdq2pd cvtps2pd cvtpd2ps cvtpd2dq cvttpd2dq cvtss2sd cvtsd2ss cvtsi2ss cvtsi2sd cvtsi2sdl cvtsi2sdq cvtsi2ssl cvtsi2ssq '
    'cvtss2si cvtsd2si cvttss2si cvttsd2si cvtph2ps cvtps2ph', H_cvt)

def H_round(S, base, ops, vex):
    f = fmt_of(base); w = f.w
    if is_scalar(base):
        d, a, b, im = src12(ops)
        x = S.read(a, 128); y = S.read(b, w) if b['k'] == 'm' else S.read(b, 128) & mask(w)
        r = round1(S, f, y, im)
        S.wv(d, (x & ~mask(w) & M128) | r, 128, vex)
    else:
        d, a, im = ops; size = d['size']; im = im['v']
        S.wv(d, P([round1(S, f, v, im) for v in L(S.read(a, size), w, size // w)], w), size, vex)
def round1(S, f, bits, im):
    c, s, v = unpack(f, bits, S.mode, S.mx, S.st)
    if c == 'snan': S.st.add('IE'); return quiet(f, bits)
    if c == 'qnan' or c == 'inf': return bits
    sb = f.sign if s else 0
    if c == 'zero': return sb
    rc = S.mx >> 13 & 3 if im & 4 else im & 3
    q, inex = rint(v, s, rc)
    if inex and not im & 8: S.st.add('PE')
    if q == 0: return sb
    return enc(f, s, Fr(q))
reg('roundps roundpd roundss roundsd', H_round)

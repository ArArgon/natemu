"""Exact IEEE helpers with x86 flag semantics and switchable NaN / flush models."""
from fractions import Fraction as Fr

class Fmt:
    def __init__(s, eb, mb):
        s.eb, s.mb = eb, mb; s.w = 1 + eb + mb
        s.bias = (1 << (eb - 1)) - 1; s.emin = 1 - s.bias; s.emax = s.bias
        s.sign = 1 << (s.w - 1); s.expmask = ((1 << eb) - 1) << mb; s.manmask = (1 << mb) - 1
        s.qbit = 1 << (mb - 1)
        s.minnorm = Fr(2) ** s.emin
        s.dflt = s.expmask | s.qbit          # positive default NaN
        s.inf = s.expmask
        s.maxfin = (s.expmask - (1 << mb)) | s.manmask
F16, F32, F64 = Fmt(5, 10), Fmt(8, 23), Fmt(11, 52)

class Mode:
    """Behaviour switches. name: 'q' (QEMU/x86 reference), 'hw' (M4 natemu), 'm2' (no AFP)."""
    def __init__(s, name, dnan_neg=True, nan2='qemu', fma='qemu', negflip=False, negflip_pk=False, daz_ignored=False, ftz_in=False, fz_out_arm=False, fma_inf0q=False, tiny_before=False,
                 minmax_qnan_ie=True, ue_nozero=False, hadd_swap=False):
        s.__dict__.update(locals()); del s.__dict__['s']

Q = Mode('q')
HW = Mode('hw', nan2='hw', fma='hw')
M2 = Mode('m2', dnan_neg=False, nan2='arm', fma='arm', negflip=True, negflip_pk=True, daz_ignored=True, ftz_in=True, fz_out_arm=True, fma_inf0q=True, tiny_before=True, minmax_qnan_ie=False, hadd_swap=True)
import dataclasses
BASE = Mode('base', nan2='hw', hadd_swap=True)    # natemu on the M4 (AH=1) as far as the corpora show
def variant(**kw):
    d = dict(BASE.__dict__); d.update(kw); return Mode(**d)
CLASSES = {
    'a': dict(dnan_neg=False),
    'b2': dict(nan2='arm'),
    'b3': dict(fma='arm'),
    'c': dict(negflip=True),
    'c2': dict(negflip_pk=True),   # the packed addend negation and ADDSUB: fixed in fast mode since the fmul-by-minus-one lowering (not in FAST_ON)
    'd1': dict(daz_ignored=True),
    'd2': dict(ftz_in=True),
    'd3': dict(fz_out_arm=True),
    'e2': dict(tiny_before=True),
    'e': dict(minmax_qnan_ie=False),
    'f1': dict(fma_inf0q=True),
}
ABL = {k: variant(name=k, **v) for k, v in CLASSES.items()}

def floorlog2(v):
    n, d = v.numerator, v.denominator
    e = n.bit_length() - d.bit_length()
    if (n << max(0, -e)) < (d << max(0, e)): e -= 1
    return e

def p2(e): return Fr(1 << e) if e >= 0 else Fr(1, 1 << -e)

def rint(v, sign, rc):
    """round non-negative Fraction to integer by x86 rc (0 near,1 down,2 up,3 trunc); returns (q, inexact)"""
    n, d = v.numerator, v.denominator
    q, r = divmod(n, d)
    if r == 0: return q, False
    if rc == 0:
        if 2 * r > d or (2 * r == d and q & 1): q += 1
    elif rc == 1:
        if sign: q += 1
    elif rc == 2:
        if not sign: q += 1
    return q, True

def cls(f, b):
    s = b >> (f.w - 1) & 1; e = b >> f.mb & ((1 << f.eb) - 1); m = b & f.manmask
    if e == (1 << f.eb) - 1:
        if m == 0: return 'inf', s, None
        return ('qnan' if m & f.qbit else 'snan'), s, None
    if e == 0:
        if m == 0: return 'zero', s, Fr(0)
        return 'den', s, Fr(m) * p2(f.emin - f.mb)
    return 'nrm', s, Fr((1 << f.mb) + m) * p2(e - f.bias - f.mb)

def enc(f, sign, val):
    """encode exactly representable non-negative Fraction"""
    sb = f.sign if sign else 0
    if val == 0: return sb
    if val < f.minnorm:
        return sb | int(val / p2(f.emin - f.mb))
    e = floorlog2(val)
    m = int((val / p2(e) - 1) * (1 << f.mb))
    return sb | ((e + f.bias) << f.mb) | m

def rnd(f, sign, v, rc, st, mode, mx, nofz=False, suppress_pe=False):
    """round exact positive Fraction v; set flags in st (set); honour FTZ per mode. returns bits"""
    sb = f.sign if sign else 0
    e = floorlog2(v)
    qe = e - f.mb
    qu, ixu = rint(v / p2(qe), sign, rc)
    vu = Fr(qu) * p2(qe)
    if e >= f.emin:
        val, inex = vu, ixu
        tiny = False
    else:
        q2, ix2 = rint(v / p2(f.emin - f.mb), sign, rc)
        val, inex = Fr(q2) * p2(f.emin - f.mb), ix2
        tiny = (e < f.emin) if mode.tiny_before else (vu < f.minnorm)
    ftz = mx & 0x8000 and not nofz
    if ftz and e < f.emin and (mode.fz_out_arm or vu < f.minnorm):
        st.add('UE')
        if not mode.fz_out_arm: st.add('PE')
        return sb
    if val >= p2(f.emax + 1):
        st.add('OE'); st.add('PE')
        if rc == 0 or (rc == 1 and sign) or (rc == 2 and not sign): return sb | f.inf
        return sb | f.maxfin
    if inex:
        if not suppress_pe: st.add('PE')
        if tiny and (val != 0 or not mode.ue_nozero): st.add('UE')
    return enc(f, sign, val)

def quiet(f, b): return b | f.qbit

def dnan(f, mode): return f.dflt | (f.sign if mode.dnan_neg else 0)

def flush_in(mode, mx):
    return bool((mx & 0x8000) and mode.ftz_in) or bool((mx & 0x40) and not mode.daz_ignored)

def unpack(f, b, mode, mx, st):
    c, s, v = cls(f, b)
    if c == 'den' and flush_in(mode, mx): return 'zero', s, Fr(0)
    return c, s, v

def nan_pick2(f, a, b, ca, cb, mode, st):
    """a,b bits, ca,cb classes (at least one NaN); returns result NaN bits (quieted)"""
    sa, sb_ = ca == 'snan', cb == 'snan'
    if sa or sb_: st.add('IE')
    na, nb = ca in ('qnan', 'snan'), cb in ('qnan', 'snan')
    if na and not nb: r = a
    elif nb and not na: r = b
    else:
        if mode.nan2 == 'hw': r = a
        elif mode.nan2 == 'arm':
            if sa: r = a
            elif sb_: r = b
            elif ca == 'qnan': r = a
            else: r = b
        else:  # qemu x87-like
            if sa:
                if sb_: r = qemu_larger(f, a, b)
                else: r = b if cb == 'qnan' else a
            elif ca == 'qnan':
                if sb_ or cb != 'qnan': r = a
                else: r = qemu_larger(f, a, b)
            else: r = b
    return quiet(f, r)

def qemu_larger(f, a, b):
    ma, mb_ = a & (f.manmask | 0), b & f.manmask
    if ma < mb_: return b
    if ma > mb_: return a
    return a if not (a & f.sign) else b

def addsub(f, a, b, sub, mode, mx, st):
    rc = mx >> 13 & 3
    ca, sa, va = unpack(f, a, mode, mx, st); cb, sb_, vb = unpack(f, b, mode, mx, st)
    if ca in ('qnan', 'snan') or cb in ('qnan', 'snan'): return nan_pick2(f, a, b, ca, cb, mode, st)
    if sub: sb_ ^= 1
    if ca == 'inf' or cb == 'inf':
        if ca == 'inf' and cb == 'inf' and sa != sb_:
            st.add('IE'); return dnan(f, mode)
        return f.inf | (f.sign if (sa if ca == 'inf' else sb_) else 0)
    x = (-va if sa else va) + (-vb if sb_ else vb)
    if x == 0:
        sign = sa if sa == sb_ else (1 if rc == 1 else 0)
        return f.sign if sign else 0
    return rnd(f, x < 0, abs(x), rc, st, mode, mx)

def mul(f, a, b, mode, mx, st):
    rc = mx >> 13 & 3
    ca, sa, va = unpack(f, a, mode, mx, st); cb, sb_, vb = unpack(f, b, mode, mx, st)
    if ca in ('qnan', 'snan') or cb in ('qnan', 'snan'): return nan_pick2(f, a, b, ca, cb, mode, st)
    s = sa ^ sb_
    if ca == 'inf' or cb == 'inf':
        if ca == 'zero' or cb == 'zero': st.add('IE'); return dnan(f, mode)
        return f.inf | (f.sign if s else 0)
    x = va * vb
    if x == 0: return f.sign if s else 0
    return rnd(f, s, x, rc, st, mode, mx)

def div(f, a, b, mode, mx, st):
    rc = mx >> 13 & 3
    ca, sa, va = unpack(f, a, mode, mx, st); cb, sb_, vb = unpack(f, b, mode, mx, st)
    if ca in ('qnan', 'snan') or cb in ('qnan', 'snan'): return nan_pick2(f, a, b, ca, cb, mode, st)
    s = sa ^ sb_
    if ca == 'inf':
        if cb == 'inf': st.add('IE'); return dnan(f, mode)
        return f.inf | (f.sign if s else 0)
    if cb == 'inf': return f.sign if s else 0
    if cb == 'zero':
        if ca == 'zero': st.add('IE'); return dnan(f, mode)
        st.add('ZE'); return f.inf | (f.sign if s else 0)
    if ca == 'zero': return f.sign if s else 0
    return rnd(f, s, va / vb, rc, st, mode, mx)

def isqrt_frac(v, S):
    """floor(sqrt(v)*2^S) and exactness"""
    from math import isqrt
    t = v * p2(2 * S)
    fl = t.numerator // t.denominator
    r = isqrt(fl)
    return r, (Fr(r * r) == t)

def sqrt(f, a, mode, mx, st):
    rc = mx >> 13 & 3
    ca, sa, va = unpack(f, a, mode, mx, st)
    if ca in ('qnan', 'snan'):
        if ca == 'snan': st.add('IE')
        return quiet(f, a)
    if ca == 'zero': return f.sign if sa else 0
    if sa: st.add('IE'); return dnan(f, mode)
    if ca == 'inf': return a
    # sqrt with enough bits: compute scaled integer sqrt with sticky
    bits = f.mb + 4 + 2 * (f.emax + f.mb)  # generous
    bits = f.mb + 60
    e = floorlog2(va)
    # scale so result has bits fractional bits relative to result exponent
    # sqrt(va) ~ 2^(e/2); use fixed scale 2^-S with S large enough
    S = f.mb + 8 - (e // 2) + 2
    r, exact = isqrt_frac(va, S)
    # value = (r + sticky)/2^S ; make a Fraction slightly above r if inexact, preserving rounding direction
    val = Fr(r) * p2(-S)
    if not exact:
        # add a tiny epsilon below 1/2 ulp of r: sticky -> use r + 1/2 in the last place
        val = Fr(2 * r + 1) * p2(-(S + 1))
    return rnd(f, 0, val, rc, st, mode, mx)

def tofr(f, b):
    c, s, v = cls(f, b)
    return c, s, v

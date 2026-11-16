#!/usr/bin/env python3
"""Exact reference for crates/abi/src/tiny.rs: x86 tininess AFTER rounding (SDM Vol.1 4.9.1.5), with Python fractions.

For a multiply, divide, FMA or binary64->binary32 convert of finite operands, `x86_tiny` rounds the exact result to p significant
bits with an UNBOUNDED exponent range under the MXCSR rounding mode and calls it tiny when the rounded magnitude is below
MIN_NORMAL (an exact tiny result is tiny as well: an unmasked UE does not need inexactness). Arm (FPCR.AH = 0, no FEAT_AFP)
judges before rounding: UFC is raised when the exact value is below MIN_NORMAL and the (bounded) rounding is inexact. The vector
file lists `kind wide rc negp negc a b c tiny` (hex patterns; kind 1 MUL, 2 DIV, 3 FMA, 4 CVT); the script also prints how many
vectors Arm flags with a result of +-MIN_NORMAL (the band) and x86 still calls tiny: the band is not a signature.

  gen_tiny_vectors.py [OUT]      (default crates/abi/tests/tiny_vectors.txt)

The reference agrees with the M4's hardware (FPCR.AH = 1 detects underflow after rounding): tools/models/tiny_vectors_hw.c.
"""
import random
import sys
from fractions import Fraction as F

P = {False: (24, -126, 8, 127), True: (53, -1022, 11, 1023)}  # p, emin, exponent bits, bias


def parse(bits, wide):
    p, emin, eb, bias = P[wide]
    fb = p - 1
    e = bits >> fb & ((1 << eb) - 1)
    frac = bits & ((1 << fb) - 1)
    sign = bits >> (fb + eb) & 1
    if e == (1 << eb) - 1:
        return None
    if e == 0:
        v = F(frac) * F(2) ** (emin - fb)
    else:
        v = F(frac | 1 << fb) * F(2) ** (e - bias - fb)
    return -v if sign else v


def floor_log2(a):
    n, d = a.numerator, a.denominator
    e = n.bit_length() - d.bit_length()
    if F(2) ** e > a:
        e -= 1
    return e


def round_int(q, rc, neg):
    f = q.numerator // q.denominator
    r = q - f
    if r == 0:
        return f
    if rc == 0:
        return f + 1 if (r > F(1, 2) or (r == F(1, 2) and f & 1)) else f
    up = (rc == 1 and neg) or (rc == 2 and not neg)  # the magnitude-increasing directed mode
    return f + 1 if up else f


def round_p(v, wide, rc, bounded):
    """v rounded to p bits; bounded: denormals below emin (the result Arm/IEEE deliver), else an unbounded exponent range."""
    p, emin, _, _ = P[wide]
    if v == 0:
        return F(0)
    neg = v < 0
    a = abs(v)
    e = floor_log2(a)
    if bounded:
        e = max(e, emin)
    ulp = F(2) ** (e - p + 1)
    r = round_int(a / ulp, rc, neg) * ulp
    return -r if neg else r


def x86_tiny(v, wide, rc):
    if v == 0:
        return False
    return abs(round_p(v, wide, rc, False)) < F(2) ** P[wide][1]


def arm_band(v, wide, rc):
    """Arm raises UFC (exact |v| < MIN_NORMAL, bounded rounding inexact) and the result is +-MIN_NORMAL."""
    if v == 0 or abs(v) >= F(2) ** P[wide][1]:
        return False
    r = round_p(v, wide, rc, True)
    return r != v and abs(r) == F(2) ** P[wide][1]


def to_bits(v, wide, rc=0):
    p, emin, eb, bias = P[wide]
    fb = p - 1
    r = round_p(v, wide, rc, True)
    sign = 1 if v < 0 else 0
    a = abs(r)
    if a == 0:
        return sign << (fb + eb)
    e = floor_log2(a)
    if e < emin:
        return sign << (fb + eb) | int(a / F(2) ** (emin - fb))
    return sign << (fb + eb) | (e + bias) << fb | (int(a / F(2) ** (e - fb)) - (1 << fb))


def neighbors(bits, wide, k=2):
    p, _, eb, _ = P[wide]
    mask = (1 << (p - 1 + eb)) - 1
    top = ((1 << eb) - 2) << (p - 1) | ((1 << (p - 1)) - 1)  # the largest finite magnitude
    return [bits & ~mask | m for d in range(-k, k + 1) if 0 <= (m := (bits & mask) + d) <= top]


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "crates/abi/tests/tiny_vectors.txt"
    rng = random.Random(0x7131)
    lines, stats = [], {}

    def emit(kind, wide, rc, negp, negc, a, b, c):
        fmt = wide and kind != 4 or kind == 4 and False
        va, vb, vc = parse(a, wide), parse(b, wide), parse(c, wide)
        v = None
        if kind == 4:
            v = va
        elif va is not None and vb is not None and (kind != 3 or vc is not None):
            if kind == 1:
                v = va * vb
            elif kind == 2:
                v = va / vb if vb != 0 else None
            else:
                v = (-va * vb if negp else va * vb) + (-vc if negc else vc)
        fmt = wide if kind != 4 else False
        t = v is not None and x86_tiny(v, fmt, rc)
        s = stats.setdefault((kind, wide), [0, 0, 0])
        s[0] += 1
        if v is not None and arm_band(v, fmt, rc):
            s[1] += 1
            s[2] += t
        lines.append(f"{kind} {int(wide)} {rc} {int(negp)} {int(negc)} {a:x} {b:x} {c:x} {int(t)}")

    def targets(wide):
        p, emin, _, _ = P[wide]
        u = F(2) ** (emin - p - 1)  # half the spacing below MIN_NORMAL
        return [F(2) ** emin - m * u for m in list(range(0, 10)) + [rng.randrange(10, 1 << 12) for _ in range(6)]]

    def pat(wide, lo=None, hi=None):
        p, emin, eb, bias = P[wide]
        fb = p - 1
        lo = emin - fb if lo is None else max(lo, emin - fb)
        hi = (1 << eb) - 2 - bias if hi is None else min(hi, (1 << eb) - 2 - bias)
        e = rng.randint(lo, hi)
        sign = rng.getrandbits(1) << (fb + eb)
        return sign | (rng.getrandbits(fb) if e < emin else (e + bias) << fb | rng.getrandbits(fb))

    nsign = lambda wide: rng.getrandbits(1) << (P[wide][0] - 1 + P[wide][2])
    for wide in (False, True):
        p, emin, eb, bias = P[wide]
        for rc in range(4):
            for _ in range(45):
                tg = rng.choice(targets(wide)) * rng.choice([1, -1])
                a = pat(wide, emin - 3 * p, 3 * p)
                va = parse(a, wide)
                if va != 0:  # MUL: b = the neighbours of target / a
                    for bb in neighbors(to_bits(tg / va, wide), wide):
                        emit(1, wide, rc, 0, 0, a, bb, 0)
                b = pat(wide, emin - 3 * p, 3 * p)
                vb = parse(b, wide)
                if vb != 0:  # DIV: a = the neighbours of target * b
                    for aa in neighbors(to_bits(tg * vb, wide), wide):
                        emit(2, wide, rc, 0, 0, aa, b, 0)
                a, b = pat(wide, emin - 2 * p, 2 * p), pat(wide, emin - 2 * p, 2 * p)  # FMA: c = the neighbours of target - a*b
                va, vb = parse(a, wide), parse(b, wide)
                negp, negc = rng.getrandbits(1), rng.getrandbits(1)
                prod = -va * vb if negp else va * vb
                c0 = (tg - prod) * (-1 if negc else 1)
                for cc in neighbors(to_bits(c0, wide), wide):
                    emit(3, wide, rc, negp, negc, a, b, cc)
                emit(3, wide, rc, rng.getrandbits(1), rng.getrandbits(1), pat(wide, emin - p, emin // 2),  # a tiny product and addend
                     pat(wide, emin // 2 - p, emin // 2 + p), pat(wide, emin - p, emin + 2))
                for _ in range(2):  # anything, zeros, infinities and NaNs included
                    ops = [rng.choice([0, pat(wide), pat(wide, emin - p, emin + p), ((1 << eb) - 1) << (p - 1)]) for _ in range(3)]
                    emit(rng.choice([1, 2, 3]), wide, rc, rng.getrandbits(1), rng.getrandbits(1), *ops)
    for rc in range(4):  # binary64 -> binary32, around binary32's MIN_NORMAL
        for _ in range(40):
            tg = rng.choice(targets(False)) * rng.choice([1, -1])
            for x in neighbors(to_bits(abs(tg), True), True, 3):
                emit(4, True, rc, 0, 0, x | (1 << 63 if tg < 0 else 0), 0, 0)
        for _ in range(100):
            emit(4, True, rc, 0, 0, pat(True, -160, -100), 0, 0)
            emit(4, True, rc, 0, 0, pat(True), 0, 0)
    seen = set()
    uniq = [l for l in lines if not (l in seen or seen.add(l))]
    with open(out, "w") as f:
        f.write("# generated by tools/models/gen_tiny_vectors.py: kind wide rc negp negc a b c tiny (hex)\n")
        f.write("\n".join(uniq) + "\n")
    for (kind, wide), (n, band, st) in sorted(stats.items()):
        print(f"kind {kind} wide {int(wide)}: {n} vectors; Arm band (UFC, result +-MIN_NORMAL) {band}, of them x86-tiny {st}")
    print(len(uniq), "vectors ->", out)


if __name__ == "__main__":
    main()

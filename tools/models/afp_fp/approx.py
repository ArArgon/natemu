"""RCPPS/RCPSS/RSQRTPS/RSQRTSS (and their VEX forms) judged by the Intel SDM's rules instead of one processor's bits.

The SDM fixes only some results: a 0.0 or denormal source (a denormal is treated as 0.0) gives an infinity of its sign; a
NaN source gives the quieted NaN; RSQRT of a negative value (other than -0.0) gives the default NaN; 1/inf and 1/sqrt(+inf)
are 0. Every other result is an approximation within |relative error| <= 1.5 * 2^-12, which each processor computes
from its own table (QEMU returns the correctly rounded value; AMD and Intel parts differ in the low bits). RCP flushes
tiny results to 0.0. The SDM's sentence on where that starts gives two inputs on either side of 2^126 with the
directions as printed reversed (a larger input gives a smaller result); read as the arithmetic requires, a source up to
1.11111111110100000000000b * 2^125 never gives a tiny result, one from 1.00000000000110000000001b * 2^126 always gives
0.0, and in between either may happen.

`judge` checks a final state against these rules: it finds where each approximation's result ends up (running the model
with that one result changed), takes the value natemu produced from there, checks it against the bound, then replays
the whole program in the model with natemu's values and requires the final state to match exactly. A result that later
instructions consume without leaving it visible is read from a prefix of the program (`prefix_value`).
"""
import re
from fractions import Fraction as Fr

import fp
import fpops
import predict

BOUND = Fr(3, 2) * Fr(1, 2 ** 12)
NO_TINY_BELOW = Fr(int('1' + '11111111110100000000000', 2), 2 ** 23) * 2 ** 125      # 1.11111111110100000000000b * 2^125
TINY_FROM = Fr(int('100000000000110000000001', 2), 2 ** 23) * 2 ** 126          # 1.00000000000110000000001b * 2^126
DEFAULT_NAN = 0xffc00000
SMALLEST_NORMAL = Fr(1, 2 ** 126)


def value(bits: int) -> Fr:
    e = bits >> 23 & 0xff; m = bits & 0x7fffff
    v = Fr(m, 2 ** 23) * Fr(2) ** (-126) if e == 0 else (1 + Fr(m, 2 ** 23)) * Fr(2) ** (e - 127)
    return -v if bits >> 31 else v


def rule(kind: str, x: int):
    """('exact', bits) where the SDM fixes the result, else ('approx', may_be_zero): RCP's implementation-defined band."""
    s, e, m = x >> 31, x >> 23 & 0xff, x & 0x7fffff
    if e == 0xff and m: return 'exact', x | 0x400000                       # NaN: quieted
    if e == 0: return 'exact', (s << 31) | 0x7f800000                       # 0.0 or denormal: infinity of its sign
    if e == 0xff:
        if kind == 'rcp' or not s: return 'exact', s << 31                  # 1/inf = 0, 1/sqrt(+inf) = +0
        return 'exact', DEFAULT_NAN
    if kind == 'rsqrt':
        return ('exact', DEFAULT_NAN) if s else ('approx', False)
    a = abs(value(x))
    if a >= TINY_FROM: return 'exact', s << 31
    return 'approx', a > NO_TINY_BELOW


def why_exact(kind: str, x: int) -> str:
    e, m = x >> 23 & 0xff, x & 0x7fffff
    if e == 0xff and m: return 'a NaN source gives the quieted NaN'
    if e == 0: return 'a denormal source counts as 0.0, which gives infinity' if m else '0.0 gives infinity'
    if e == 0xff: return 'infinity gives 0.0' if kind == 'rcp' or not x >> 31 else 'a negative source gives the default NaN'
    if kind == 'rsqrt': return 'a negative source gives the default NaN'
    return 'the result is tiny and flushed to 0.0'


def valid(kind: str, x: int, v: int) -> str | None:
    """None if `v` is an SDM-valid result for source `x`, else why not."""
    r = rule(kind, x)
    if r[0] == 'exact':
        return None if v == r[1] else f'{kind} of {x:#010x}: {v:#010x}, the SDM gives {r[1]:#010x} ({why_exact(kind, x)})'
    _, may_zero = r
    if may_zero and v == (x >> 31) << 31: return None
    if v >> 23 & 0xff in (0, 0xff) or v >> 31 != x >> 31:
        return f'{kind} of {x:#010x}: {v:#010x} is not a normal number of the source sign'
    vx, vv = abs(value(x)), abs(value(v))
    q = vv * vx if kind == 'rcp' else vv * vv * vx                    # 1 within the bound (squared for RSQRT)
    lo, hi = (1 - BOUND, 1 + BOUND) if kind == 'rcp' else ((1 - BOUND) ** 2, (1 + BOUND) ** 2)
    if not lo <= q <= hi:
        err = float(q - 1) if kind == 'rcp' else float(q) ** 0.5 - 1
        return f'{kind} of {x:#010x}: {v:#010x} has relative error {err:.3g}, over 1.5 * 2^-12'
    return None


def run(program, mode, choose):
    """The model's final state, each approximation's result from `choose(index, kind, x, nominal)`; and the events
    [(instruction index, kind, x, nominal)] in program order."""
    ev = []

    def hook(S, kind, x, r):
        ev.append((S.i, kind, x, r))
        return choose(len(ev) - 1, kind, x, r)
    fpops.APPROX = hook
    try: st = predict.state(program, mode)
    finally: fpops.APPROX = None
    return st, ev


def sdm_or(values):
    """A chooser: the SDM's fixed results, else values[i], else the model's (correctly rounded) value."""
    def c(i, kind, x, r):
        ru = rule(kind, x)
        return ru[1] if ru[0] == 'exact' else values.get(i, r)
    return c


PERTURB = (0x1, 0x7ff, 0x400000, 0x7fffff)      # changes to the result's mantissa: a low bit hides behind a rounding or a
                                               # high-bits extract, a high one behind a low-bits use; any of them shows a use


def sinks(program, mode):
    """Per event: (class, [(addr, off)]) where the list holds the final memory words that keep the result unchanged and
    the class is 'fixed' (the SDM fixes the result), 'dead' (nothing final depends on it), 'copies' (only those words
    do) or 'consumed' (later instructions compute with it)."""
    default = sdm_or({})
    base, ev = run(program, mode, default)
    out = []
    for i, (_, kind, x, r) in enumerate(ev):
        r = default(i, kind, x, r)
        copies, other = None, False
        for d in PERTURB:
            st, _ = run(program, mode, lambda j, k, y, q, i=i, d=d, r=r: (r ^ d) if j == i else default(j, k, y, q))
            here = set()
            other |= st[0] != base[0] or st[1] != base[1]
            mb, ms = dict(base[2]), dict(st[2])
            for a in mb:
                d0, d1 = mb[a], ms[a]
                for w in sorted({k - k % 4 for k in range(len(d0)) if d0[k] != d1[k]}):
                    if int.from_bytes(d0[w:w + 4], 'little') == r and int.from_bytes(d1[w:w + 4], 'little') == r ^ d:
                        here.add((a, w))
                    else: other = True
            if copies is not None and here != copies: other = True
            copies = here if copies is None else copies & here
        copies = sorted(copies)
        cls = 'fixed' if rule(kind, x)[0] == 'exact' else 'dead' if not copies and not other else \
            'consumed' if other else 'copies'
        out.append((cls, copies))
    return out, ev


def word(state_memory, addr, off):
    for a, d in state_memory:
        if a == addr: return int.from_bytes(d[off:off + 4], 'little')
    return None


def word_diffs(expected, got, limit=6) -> list[str]:
    """The differing 32-bit words of two final memories, as 'addr: expected, got'."""
    g = dict(got); out = []
    for a, d in expected:
        e = g.get(a, b'')
        for w in range(0, len(d), 4):
            if d[w:w + 4] != e[w:w + 4]:
                out.append(f'{a + w:#x}: {int.from_bytes(d[w:w + 4], "little"):#010x} expected, '
                           f'{int.from_bytes(e[w:w + 4], "little"):#010x}')
    return out[:limit] + ([f'and {len(out) - limit} more'] if len(out) > limit else [])


def judge(program, mode, mem, prefix_value):
    """(expected final state as the model gives it with natemu's approximations, [problems]). `mem` is natemu's final
    memory as [(addr, bytes)]; `prefix_value(event index, instruction index)` returns natemu's result of that event from
    a prefix run, or None."""
    kinds, ev = sinks(program, mode)
    values, problems = {}, []
    for i, ((n, kind, x, r), (cls, at)) in enumerate(zip(ev, kinds)):
        if cls == 'dead': continue
        where = f"`{program['asm'][n]}` (instruction {n})"
        got = {word(mem, a, o) for a, o in at}
        if len(got) > 1:
            problems.append(f'{where}: copies of one result differ ({", ".join(sorted(map(hex, got)))})'); continue
        v = got.pop() if got else None
        if cls == 'fixed':
            if v is not None and (why := valid(kind, x, v)): problems.append(f'{where}: {why}')
            continue
        if v is None: v = prefix_value(i, n)
        if v is None:
            problems.append(f'{where}: its result is consumed and could not be read'); continue
        if why := valid(kind, x, v): problems.append(f'{where}: {why}')
        values[i] = v
    st, _ = run(program, mode, sdm_or(values))
    return st, problems


def uses_approx(program) -> bool:
    return any(a.split(None, 1)[0].lstrip('v') in ('rcpps', 'rcpss', 'rsqrtps', 'rsqrtss') for a in program.get('asm', []))


DUMP = re.compile(r'(v?movdq[ua] [xy]mmword ptr \[\d+\], [xy]mm\d+|stmxcsr dword ptr \[\d+\]|and dword ptr \[\d+\], -3)$')


def prefix(program, k: int) -> dict:
    """The program's first k + 1 instructions followed by its final register dump (the corpora end with one)."""
    import ref
    asm = program['asm']; t = len(asm)
    while t and DUMP.match(asm[t - 1]): t -= 1
    a = asm[:k + 1] + asm[t:]
    return dict(program, name=f"{program['name']}#{k + 1}", asm=a, insns=program['insns'][:k + 1] + program['insns'][t:],
                code=ref.assemble('\n'.join(a)).hex())


def prefix_reader(program, mode, run_prefix):
    """A `prefix_value` for `judge`: `run_prefix(prefix program)` runs it under test and returns its final memory as
    [(addr, bytes)] (None if it could not run); the event's result is read where the model shows the dump holds it."""
    seen = {}

    def value(i, k):
        if k not in seen:
            q = prefix(program, k)
            seen[k] = (q, run_prefix(q))
        q, mem = seen[k]
        if mem is None: return None
        kinds, _ = sinks(q, mode)
        if i >= len(kinds) or kinds[i][0] != 'copies': return None
        return word(mem, *kinds[i][1][0])
    return value

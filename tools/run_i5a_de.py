#!/usr/bin/env python3
"""MXCSR.DE (denormal operand) per SSE form (boot-features plan, chunk I5a), judged by the SDM's exception tables: QEMU 7.2
never sets DE, so it cannot arbitrate.

    tools/with-env python3 tools/run_i5a_de.py --fexc FEXC --out DIR [--max-block N]
    python3 tools/run_i5a_de.py --self-check        (no VM: the case tags and the judging rule's mutation checks)

Expected (SDM Vol.2, each instruction's "SIMD Floating-Point Exceptions"; Vol.1 4.9.2 and 11.5.2 precedence; every exception
masked): DE is set when an operand the instruction computes on is a denormal and MXCSR.DAZ is 0, for the forms that list
Denormal (add/sub/mul/div/min/max/sqrt, horizontal and alternating adds, compares and (U)COMIS, DPPS/DPPD on selected elements,
float-to-float conversions), never for the forms that do not (RCP/RSQRT estimates, ROUND*, conversions to integers, logical,
shuffle, blend, insert and move forms), never under DAZ, and not when the same operation reports IE (a NaN operand, QNaN
included: no denormal is examined then) or ZE (a denormal divided by zero). Each case is one instruction between an LDMXCSR
(1F80H, or 1FC0H with DAZ) and an STMXCSR; the other operand elements are 1.0. Only MXCSR.DE is judged.
On a host without FEAT_AFP in fast mode (the run's own report says `afp` false and `fp` fast) a case that expects DE with DE
masked is judged against the documented deviation `masked-sse-de` (DE missing, tools/fp_deviations.py), not skipped: DE set
there fails, and so does any other result; everywhere else every case is judged as the SDM says.
"""
import argparse, json, pathlib, struct, subprocess, sys
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools')); sys.path.insert(0, str(ROOT / 'tools/reference'))
import run_slice1_vm as vm1, ref, fp_deviations as dev

D = 0x200000
F32 = dict(one=0x3f800000, den=0x00000001, qnan=0x7fc00000, zero=0)
F64 = dict(one=0x3ff0000000000000, den=0x0000000000000001, qnan=0x7ff8000000000000, zero=0)
# (asm with x = destination/first operand, y = source; element type; reads the destination; lists Denormal)
FORMS = [(f'{m} xmm1, xmm2', t, True, True) for t, ms in (
    (32, ['addps', 'subps', 'mulps', 'divps', 'minps', 'maxps', 'haddps', 'hsubps', 'addsubps', 'addss', 'subss', 'mulss',
          'divss', 'minss', 'maxss']),
    (64, ['addpd', 'subpd', 'mulpd', 'divpd', 'minpd', 'maxpd', 'haddpd', 'hsubpd', 'addsubpd', 'addsd', 'subsd', 'mulsd',
          'divsd', 'minsd', 'maxsd'])) for m in ms] + [
    ('cmpps xmm1, xmm2, 0', 32, True, True), ('cmpps xmm1, xmm2, 1', 32, True, True), ('cmpss xmm1, xmm2, 2', 32, True, True),
    ('cmppd xmm1, xmm2, 3', 64, True, True), ('cmpsd xmm1, xmm2, 4', 64, True, True),
    ('comiss xmm1, xmm2', 32, True, True), ('ucomiss xmm1, xmm2', 32, True, True), ('comisd xmm1, xmm2', 64, True, True),
    ('ucomisd xmm1, xmm2', 64, True, True), ('dpps xmm1, xmm2, 0xff', 32, True, True), ('dppd xmm1, xmm2, 0x33', 64, True, True),
    ('sqrtps xmm1, xmm2', 32, False, True), ('sqrtss xmm1, xmm2', 32, False, True), ('sqrtpd xmm1, xmm2', 64, False, True),
    ('sqrtsd xmm1, xmm2', 64, False, True), ('cvtps2pd xmm1, xmm2', 32, False, True), ('cvtss2sd xmm1, xmm2', 32, False, True),
    ('cvtpd2ps xmm1, xmm2', 64, False, True), ('cvtsd2ss xmm1, xmm2', 64, False, True),
    ('rcpps xmm1, xmm2', 32, False, False), ('rcpss xmm1, xmm2', 32, False, False), ('rsqrtps xmm1, xmm2', 32, False, False),
    ('rsqrtss xmm1, xmm2', 32, False, False), ('roundps xmm1, xmm2, 4', 32, False, False), ('roundss xmm1, xmm2, 1', 32, False, False),
    ('roundpd xmm1, xmm2, 8', 64, False, False), ('roundsd xmm1, xmm2, 4', 64, False, False),
    ('cvtps2dq xmm1, xmm2', 32, False, False), ('cvttps2dq xmm1, xmm2', 32, False, False), ('cvtpd2dq xmm1, xmm2', 64, False, False),
    ('cvttpd2dq xmm1, xmm2', 64, False, False), ('cvtss2si eax, xmm2', 32, False, False), ('cvttss2si rax, xmm2', 32, False, False),
    ('cvtsd2si rax, xmm2', 64, False, False), ('cvttsd2si eax, xmm2', 64, False, False),
    ('andps xmm1, xmm2', 32, True, False), ('xorpd xmm1, xmm2', 64, True, False), ('shufps xmm1, xmm2, 0x1b', 32, True, False),
    ('unpcklps xmm1, xmm2', 32, True, False), ('blendps xmm1, xmm2, 5', 32, True, False), ('insertps xmm1, xmm2, 0x10', 32, True, False),
    ('movss xmm1, xmm2', 32, True, False), ('movaps xmm1, xmm2', 32, False, False), ('movshdup xmm1, xmm2', 32, False, False),
    ('pshufd xmm1, xmm2, 0', 32, False, False)]
# DPPS/DPPD's unselected elements are not multiplied: a denormal there raises nothing (lanes 3 / 1 left out by the mask).
EXTRA = [('dpps xmm1, xmm2, 0x7f', 32, 3, False), ('dpps xmm1, xmm2, 0x7f', 32, 0, True), ('dppd xmm1, xmm2, 0x13', 64, 1, False),
         ('dppd xmm1, xmm2, 0x13', 64, 0, True), ('dpps xmm1, xmm2, 0xf0', 32, 2, True), ('dppd xmm1, xmm2, 0x30', 64, 1, True)]
# (the last two: a zero destination mask still multiplies and adds: x86 raises the flags, the result is zero)


def vec(t, lanes):
    return b''.join(struct.pack('<I' if t == 32 else '<Q', v) for v in lanes)


def cases():
    out = []
    for text, t, binary, de in FORMS:
        k = F32 if t == 32 else F64; n = 4 if t == 32 else 2
        one = [k['one']] * n
        lane0 = lambda v: [v] + one[1:]
        out.append((f'{text} | src denormal', text, one, lane0(k['den']), 0x1f80, de))
        out.append((f'{text} | dst denormal', text, lane0(k['den']), one, 0x1f80, de and binary))
        out.append((f'{text} | src denormal, DAZ', text, one, lane0(k['den']), 0x1fc0, False))
        out.append((f'{text} | normal', text, one, one, 0x1f80, False))
        if binary and de:
            # (HADD/HSUB add pairs inside one operand: the QNaN and the denormal meet in different operations, so DE stays.)
            out.append((f'{text} | QNaN and denormal', text, lane0(k['qnan']), lane0(k['den']), 0x1f80, text.startswith(('hadd', 'hsub'))))
        # The screen's zero tier (zero lanes are exact: no DE; only a nonzero mantissa under a zero exponent is a denormal):
        # -0.0, the largest denormal, a zero operand beside a denormal one, a zero lane beside a denormal lane.
        big, neg0 = (0x007fffff, 0x80000000) if t == 32 else (0x000fffffffffffff, 1 << 63)
        out.append((f'{text} | src -0.0', text, one, lane0(neg0), 0x1f80, False))
        out.append((f'{text} | src largest denormal', text, one, lane0(big), 0x1f80, de))
        out.append((f'{text} | zero dst, smallest denormal src', text, lane0(k['zero']), lane0(k['den']), 0x1f80, de))
        out.append((f'{text} | -0.0 dst, largest denormal src', text, lane0(neg0), lane0(big), 0x1f80, de))
        out.append((f'{text} | zero src beside a normal dst', text, one, lane0(k['zero']), 0x1f80, False))
        out.append((f'{text} | smallest denormal, zero in lane 1', text, one, [k['den'], k['zero']] + one[2:], 0x1f80, de))
        out.append((f'{text} | negative smallest denormal', text, one, lane0(k['den'] | neg0), 0x1f80, de and not text.startswith('sqrt')))   # (sqrt of a negative: IE, no DE)
        if text.startswith('div'):
            out.append((f'{text} | denormal / 0', text, lane0(k['den']), lane0(k['zero']), 0x1f80, False))
    for text, t, lane, de in EXTRA:
        k = F32 if t == 32 else F64; n = 4 if t == 32 else 2
        v = [k['den'] if i == lane else k['one'] for i in range(n)]
        out.append((f'{text} | denormal in element {lane}', text, v, v, 0x1f80, de))
    return [c + (None,) for c in out] + sequences()


def sequences():
    """Several instructions between the LDMXCSR and the STMXCSR (the in-block skip of the DE screen, AFP accurate plan: an operand that is
    the result of an earlier FP op of the block needs no screen; the cases are the independent reviewer's). `@mid` / `@tmp` / `@y1` are
    this case's addresses of an extra STMXCSR result, an 8-byte scratch and the second quadword of the source. The last element is the
    expected DE at the `@mid` STMXCSR (None: the case has none). x86 sets DE exactly when an FP op computes on a denormal operand."""
    one, mx = [0x3f800000] * 4, 0x1f80
    d64 = lambda lo, hi=0: [lo & 0xffffffff, lo >> 32, hi & 0xffffffff, hi >> 32]
    f64 = lambda v: struct.unpack('<Q', struct.pack('<d', v))[0]
    D1, MIN64, QNAN64 = 1, 0x0010000000000000, 0x7ff8000000000000
    # a tiny result that is inexact: (1 + 2^-52) 2^-540 * 1.5 * 2^-500 = 1.5 * 2^-1040 + 2^-1091 on a grid of 2^-1074
    xi, yi = f64((1 + 2.0 ** -52) * 2.0 ** -540), f64(1.5 * 2.0 ** -500)
    rows = [
        # an f64 result read as f32: the low word of 1 + 2^-52 is the f32 denormal 1 (the high lane 0.0)
        ('addpd result, mulps reads its low word', 'addpd xmm1, xmm2\nmulps xmm1, xmm1', d64(f64(1.0)), d64(f64(2.0 ** -52)), True, None),
        ('addpd result, addss reads its low word', 'addpd xmm1, xmm2\naddss xmm1, xmm1', d64(f64(1.0)), d64(f64(2.0 ** -52)), True, None),
        # an f32 result read as f64: 2.0f with a zero upper word is the f64 denormal 0x40000000
        ('addps result, mulpd reads two lanes as one double', 'addps xmm1, xmm2\nmulpd xmm1, xmm1', [0x3f800000, 0, 0, 0], [0x3f800000, 0, 0, 0], True, None),
        # addss writes lane 0 only: lane 1 is the entry value, a denormal
        ('addss merges into an entry denormal, mulps reads it', 'addss xmm1, xmm2\nmulps xmm1, xmm2', [0x3f800000, 1, 0x3f800000, 0x3f800000], one, True, None),
        ('addsd merges into an entry denormal, mulpd reads it', 'addsd xmm1, xmm2\nmulpd xmm1, xmm2', d64(f64(1.0), D1), d64(f64(1.0), f64(1.0)), True, None),
        # bit operations and a blend turn an in-block result into a denormal (the value is unknown after them)
        ('andps makes a denormal of an in-block result', 'addps xmm1, xmm2\nandps xmm1, xmm2\nmulps xmm1, xmm1', one, [0x3f800003] * 4, True, None),
        ('xorps makes a denormal of an in-block result', 'addps xmm1, xmm2\nxorps xmm1, xmm2\nmulps xmm1, xmm1', [0x34000000] * 4, one, True, None),
        ('blendps puts a denormal into an in-block result', 'addps xmm1, xmm1\nblendps xmm1, xmm2, 1\nmulss xmm1, xmm1', one, [1, 0x3f800000, 0x3f800000, 0x3f800000], True, None),
        # a store and a load are unknown (x87 stores a denormal into the slot the producer's result was stored to)
        ('fstp m64 then movsd load', 'addsd xmm1, xmm2\nmovsd qword ptr [@tmp], xmm1\nfld qword ptr [@y1]\nfstp qword ptr [@tmp]\nmovsd xmm1, qword ptr [@tmp]\nmulsd xmm1, xmm1',
         d64(f64(1.0)), d64(f64(1.0), D1), True, None),
        # DE is no function of the denormal alone: a QNaN beside it suppresses it, a normal operand does not
        ('x denormal: addsd nan,x then mulsd 1.0,x', 'movapd xmm3, xmm2\naddsd xmm3, xmm1\nmovsd xmm4, qword ptr [@y1]\nmulsd xmm4, xmm1',
         d64(D1), d64(QNAN64, f64(1.0)), True, None),
        ('x denormal: addsd nan,x only', 'movapd xmm3, xmm2\naddsd xmm3, xmm1', d64(D1), d64(QNAN64, f64(1.0)), False, None),
        # a denormal made by subtraction and stored is no operand
        ('denormal by subtraction, stored, no consumer', 'subsd xmm1, xmm2\nmovsd qword ptr [@tmp], xmm1', d64(MIN64 + 1), d64(MIN64), False, None),
        ('inexact tiny product, no consumer', 'mulsd xmm1, xmm2\nmovsd qword ptr [@tmp], xmm1', d64(xi), d64(yi), False, None),
        # a tiny result consumed two instructions later: STMXCSR before the consumer shows no DE, after it DE
        ('tiny result, consumer after STMXCSR', 'subsd xmm1, xmm2\nmovaps xmm5, xmm5\nstmxcsr dword ptr [@mid]\naddsd xmm1, xmm1', d64(MIN64 + 1), d64(MIN64), True, False),
        ('inexact tiny product, consumer after STMXCSR', 'mulsd xmm1, xmm2\nstmxcsr dword ptr [@mid]\naddsd xmm1, xmm1', d64(xi), d64(yi), True, False),
        ('tiny result, copy, consumer of the copy', 'subsd xmm1, xmm2\nmovaps xmm3, xmm1\naddsd xmm3, xmm3', d64(MIN64 + 1), d64(MIN64), True, None),
        ('tiny result, a producer between, consumer', 'subsd xmm1, xmm2\naddsd xmm5, xmm5\nmulsd xmm1, xmm1', d64(MIN64 + 1), d64(MIN64), True, None),
        ('tiny result (2^-64 * 2^-64), packed consumer', 'mulps xmm1, xmm2\naddps xmm1, xmm1', [0x1f800000] * 4, [0x1f800000] * 4, True, None),
        ('normal chain, no DE', 'addsd xmm1, xmm2\nmulsd xmm1, xmm1\nsubsd xmm1, xmm2', d64(f64(1.0)), d64(f64(1.0)), False, None),
        # LDMXCSR clears DE: the denormal is consumed again after it
        ('LDMXCSR clears DE between two consumers', 'mulss xmm3, xmm1\nldmxcsr dword ptr [@mx]\nstmxcsr dword ptr [@mid]\nmulss xmm3, xmm1', [1, 0x3f800000, 0x3f800000, 0x3f800000], one, True, False),
    ]
    return [(name, text, x, y, mx, de, mid) for name, text, x, y, de, mid in rows]


TAGGED = 355    # the cases tagged `masked-sse-de`: 355 of 896 (update with the case list)


def mx_of(mx, de):
    """The structured expected MXCSR of a case, DE only: every other bit is the starting one (the case judges DE alone)."""
    return dev.make_mx(mx, mx | 2 if de else mx)


def self_check():
    cs = cases()
    tags = [mx_of(c[4], c[5]) for c in cs]
    n = sum(t['tag'] is not None for t in tags)
    if n != TAGGED: sys.exit(f'run_i5a_de self-check FAILED: {n} tagged cases, expected {TAGGED}')
    dev.self_check(next(t for t in tags if t['tag']), next(t for t in tags if not t['tag'] and t['want'] & 2 == 0), 'run_i5a_de')
    print(f'{len(cs)} cases, {n} tagged masked-sse-de')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--fexc', type=pathlib.Path); ap.add_argument('--out', type=pathlib.Path)
    ap.add_argument('--max-block', type=int); ap.add_argument('--no-build', action='store_true')
    ap.add_argument('--self-check', action='store_true')
    a = ap.parse_args()
    if a.self_check: return self_check()
    if not (a.fexc and a.out): ap.error('--fexc and --out are required')
    host, runtime = (ROOT / 'target/release/natemu-host', ROOT / 'target/aarch64-unknown-none/release/natemu-runtime') if a.no_build else vm1.build()
    cs = cases()
    mem, lines = bytearray(0x40 * len(cs) + 0x40), []
    for i, (name, text, x, y, mx, _, _) in enumerate(cs):
        base = 0x40 * i
        mem[base:base + 16], mem[base + 16:base + 32] = vec(32 if len(x) == 4 else 64, x), vec(32 if len(y) == 4 else 64, y)
        struct.pack_into('<I', mem, base + 32, mx)
        for tag, at in (('@mid', 40), ('@tmp', 48), ('@y1', 24), ('@mx', 32)): text = text.replace(tag, str(D + base + at))
        lines += [f'ldmxcsr dword ptr [{D + base + 32}]', f'movdqu xmm1, xmmword ptr [{D + base}]',
                  f'movdqu xmm2, xmmword ptr [{D + base + 16}]'] + text.split('\n') + [f'stmxcsr dword ptr [{D + base + 36}]']
    code = ref.assemble('\n'.join(lines + ['hlt']))
    a.out.mkdir(parents=True, exist_ok=True)
    desc = f"rip 0x100000\nrflags 0x2\ngpr {' '.join(['0x0'] * 4 + ['0x301000'] + ['0x0'] * 11)}\ncode {code.hex()}\nmemory {D:#x} {mem.hex()}\nsse 1\n"
    (a.out / 'program.txt').write_text(desc)
    run = subprocess.run([host, runtime, 'run-program', a.fexc, a.out / 'program.txt', a.out] + ([str(a.max_block)] if a.max_block else []) +
                         vm1.CODE_SHAPE, capture_output=True, text=True)
    if run.returncode: sys.exit(f'driver: {run.stderr[-500:]}')
    d = json.loads(run.stdout); got = bytes.fromhex(d['memory'][0]['data'])
    if not vm1.at_hlt(d, 0x100000 + len(code) - 1): sys.exit(f"stopped {d['stop']} {d['reason']} at {d['rip']}")
    counts, results = dev.Counts(), []
    for i, (name, text, x, y, mx, de, mid) in enumerate(cs):
        out = struct.unpack_from('<I', got, 0x40 * i + 36)[0]
        m = mx_of(mx, de)
        kind, why = dev.mxcsr_verdict(m, d, (mx & ~2) | out & 2)      # DE only
        counts.add(kind, m['tag'])
        results.append({'case': name, 'mxcsr_in': hex(mx), 'mxcsr_out': hex(out), 'de_expected': de, 'tag': m['tag'], 'outcome': kind, 'ok': kind != 'fail'})
        if kind == 'fail': print('MISMATCH', name, f'MXCSR {out:#x}, DE expected {de}' + (f': {why}' if why else ''))
        if mid is not None:   # the STMXCSR in the middle of a sequence (expected DE clear: no deviation applies)
            got_mid = struct.unpack_from('<I', got, 0x40 * i + 40)[0]
            kind, why = dev.mxcsr_verdict(mx_of(mx, mid), d, (mx & ~2) | got_mid & 2)
            counts.add(kind, None)
            if kind == 'fail': print('MISMATCH', name, '(STMXCSR in the middle)', f'MXCSR {got_mid:#x}, DE expected {mid}' + (f': {why}' if why else ''))
            bad_mid = kind == 'fail'
            results[-1]['mid_ok'] = not bad_mid
    (a.out / 'results.json').write_text(json.dumps(results, indent=1) + '\n')
    print(f"afp {d.get('afp')}, fp {d.get('fp')}: " + counts.line().replace('cases,', f'cases ({len(FORMS)} forms),', 1))
    bad = counts.bad
    sys.exit(1 if bad else 0)

if __name__ == '__main__': main()

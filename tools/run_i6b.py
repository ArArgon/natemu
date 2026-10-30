#!/usr/bin/env python3
"""I6b checks: PCMPxSTRx and the x87 transcendental, remainder, scale and extract forms, run by the resident through `svc`
(boot-features plan, chunk I6b).

    tools/with-env python3 tools/run_i6b.py corpus --fexc FEXC --out DIR PROGRAMS.json EXPECTED.json [--max-block N]
    tools/with-env python3 tools/run_i6b.py cases --fexc FEXC --out DIR [--max-block N] [--only TEXT]

`corpus`: the tools/reference/gen_i6b.py programs against the pinned QEMU 7.2 (tools/reference/ref.py), compared as
run_slice1_vm.py does except at the addresses a program lists under `tolerance` (an x87 result QEMU computes in 80 bits or with
its host's libm): there two doubles agree when equal, both NaN with the same bits, or within TOLERANCE ulps (counted and
reported; the reduced-precision deviation).

`cases`: natemu-only cases judged by the SDM rules in their table below (QEMU models no x87 stack fault and its FSIN/FCOS/FPTAN/
FSINCOS raise no flag): the C2 out-of-range rule at and around 2^63, FPREM/FPREM1 quotient bits and partial remainders, stack
underflow and overflow for every form with the masked responses, the exception word (IE ZE OE UE PE), invalid operands, and
for PCMPxSTRx the memory faults (#PF not present and crossing, #GP/#SS non-canonical, no alignment fault), each with the state
at the fault equal to the state before the instruction.
"""
import argparse, json, pathlib, struct, subprocess, sys
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools')); sys.path.insert(0, str(ROOT / 'tools/reference'))
import run_slice1_vm as vm1, ref

TOLERANCE = 2


def ulps(a, b):
    ia, ib = (struct.unpack('<q', x)[0] for x in (a, b))
    ia, ib = (v if v >= 0 else -(v & (2**63 - 1)) for v in (ia, ib))   # sign-magnitude to a monotonic line
    return abs(ia - ib)


def corpus(a, host, runtime):
    expected = {r['name']: r for r in json.loads(a.expected.read_text())['results']}
    counts, spread, results = {'match': 0, 'unsupported': 0, 'fail': 0}, {}, []
    for p in json.loads(a.programs.read_text())['programs']:
        if a.only and a.only not in p['name']: continue
        work = a.out / 'work' / p['name']; work.mkdir(parents=True, exist_ok=True)
        (work / 'program.txt').write_text(vm1.description(p))
        run = subprocess.run([host, runtime, 'run-program', a.fexc, work / 'program.txt', work] + ([str(a.max_block)] if a.max_block else []) +
                             vm1.CODE_SHAPE, capture_output=True, text=True)
        entry = {'name': p['name'], 'stderr': run.stderr[-1000:]}
        if run.returncode:
            entry['outcome'], entry['diffs'] = 'fail', ['driver failed']
        else:
            vm = json.loads(run.stdout)
            q = expected[p['name']]['qemu']['memory'][0]
            base, ours, theirs = ref.num(q['addr']), bytearray.fromhex(vm['memory'][0]['data']), bytes.fromhex(q['data'])
            tol = []
            for t in p.get('tolerance', []):
                o = ref.num(t) - base
                x, y = bytes(ours[o:o + 8]), theirs[o:o + 8]
                nan = lambda b: (int.from_bytes(b, 'little') & (2**63 - 1)) > 0x7ff0 << 48
                d = 0 if x == y else (None if nan(x) or nan(y) else ulps(x, y))
                if d is not None and d <= TOLERANCE:
                    spread[d] = spread.get(d, 0) + 1
                    ours[o:o + 8] = y
                else:
                    tol.append(f'{t}: {x[::-1].hex()} vs qemu {y[::-1].hex()}')
            vm['memory'][0]['data'] = ours.hex()
            entry['outcome'], entry['diffs'] = vm1.evaluate(p, expected[p['name']], vm)
            if entry['outcome'] != 'match': entry['vm'] = vm
            if tol:
                entry['outcome'], entry['diffs'] = 'fail', tol + entry['diffs']
        counts[entry['outcome']] += 1
        results.append(entry)
        if entry['outcome'] != 'match': print(f"{p['name']}: {entry['outcome']} {'; '.join(entry['diffs'])[:400]} {entry['stderr'].strip()[-200:]}")
    (a.out / 'results.json').write_text(json.dumps({'counts': counts, 'ulps': spread, 'results': results}, indent=1) + '\n')
    print(counts, 'tolerance-compared doubles by ulps:', dict(sorted(spread.items())))
    return counts['fail'] == 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('mode', choices=('corpus', 'cases'))
    ap.add_argument('programs', type=pathlib.Path, nargs='?'); ap.add_argument('expected', type=pathlib.Path, nargs='?')
    ap.add_argument('--fexc', type=pathlib.Path, required=True); ap.add_argument('--out', type=pathlib.Path, required=True)
    ap.add_argument('--max-block', type=int); ap.add_argument('--no-build', action='store_true'); ap.add_argument('--only')
    a = ap.parse_intermixed_args()
    host, runtime = (ROOT / 'target/release/natemu-host', ROOT / 'target/aarch64-unknown-none/release/natemu-runtime') if a.no_build else vm1.build()
    a.out.mkdir(parents=True, exist_ok=True)
    ok = corpus(a, host, runtime) if a.mode == 'corpus' else cases(a, host, runtime)
    sys.exit(0 if ok else 1)


# ---- cases: natemu only, judged by the SDM ----------------------------------------------------------------------------------
import run_i6_faults as f6
D, NONCANON, INDEF = f6.D, f6.NONCANON, 0xfff8000000000000
IE, ZE, OE, UE, PE, SF = 1, 2, 4, 8, 0x10, 0x40
def d2b(x): return struct.unpack('<Q', struct.pack('<d', x))[0]
def b2d(b): return struct.unpack('<d', struct.pack('<Q', b))[0]
ONE = d2b(1.0)
KEEP = ['nop', 'nop', 'hlt']   # f6.AFTER's length (its judge counts from the end) without writing ECX


def x87_case(name, values, insn, want, pre=()):
    """FNINIT, FLD m64 of `values` (the last is ST(0)), `pre`, then `insn`; `want` = {'st': {i: double bits | None (empty)},
    'top': TOP, 'c': {k: Ck}, 'flags': exception word (IE ZE OE UE PE, SF)} after it."""
    loads = [f'fld qword ptr [rdi + {8 * k}]' for k in range(len(values))]
    setup = [f'memory {D + 0x200:#x} ' + b''.join(struct.pack('<Q', v if isinstance(v, int) else d2b(v)) for v in values).hex()] if values else []
    return {'name': name, 'lines': ['fninit'] + loads + list(pre) + [insn] + f6.AFTER, 'setup': setup, 'want': None, 'cpl': 3, 'x87want': want}


def sdm_pcmp(lhs, rhs, imm, rax=0, rdx=0, explicit=False, wide=False):
    """PCMPxSTRx by the SDM (Vol.2 4.1; a third writing, independent of the Rust port and of its unit test's model): (IntRes2,
    CF, ZF, SF, OF)."""
    n = 8 if imm & 1 else 16
    el = lambda v, i: (int.from_bytes(v[2 * i:2 * i + 2], 'little', signed=bool(imm & 2)) if imm & 1 else
                       int.from_bytes(v[i:i + 1], 'little', signed=bool(imm & 2)))
    def length(v, reg):
        if not explicit: return next((i for i in range(n) if el(v, i) == 0), n)
        x = reg - (1 << 64) if reg >> 63 else reg
        if not wide: x = (reg & 0xffffffff) - ((reg & 0x80000000) << 1)
        return min(abs(x), n)
    l1, l2 = length(lhs, rax), length(rhs, rdx)
    agg = imm >> 2 & 3
    def br(j, i):   # rhs element j, lhs element i, with Table 4-7's overrides
        vi, vj = i < l1, j < l2
        if agg in (0, 1) and not (vi and vj): return False
        if agg == 2 and not (vi and vj): return not vi and not vj
        if agg == 3 and not vi: return True
        if agg == 3 and not vj: return False
        if agg == 1: return el(lhs, i) <= el(rhs, j) if i % 2 == 0 else el(lhs, i) >= el(rhs, j)
        return el(lhs, i) == el(rhs, j)
    res = 0
    for j in range(n):
        if agg == 0: b = any(br(j, i) for i in range(n))
        elif agg == 1: b = any(br(j, i) and br(j, i + 1) for i in range(0, n, 2))
        elif agg == 2: b = br(j, j)
        else: b = all(br(j + i, i) for i in range(n - j))
        res |= b << j
    pol = imm >> 4 & 3
    if pol == 1: res ^= (1 << n) - 1
    if pol == 3: res ^= (1 << l2) - 1
    return res, res != 0, l2 < n, l1 < n, bool(res & 1)


def cases_list():
    out = []
    T63 = 2.0 ** 63
    # C2: the trigonometric forms leave an operand with |x| >= 2^63 unchanged (nothing pushed) and set C2; no exception.
    for op in ('fsin', 'fcos', 'fptan', 'fsincos'):
        for tag, x in (('2p63', T63), ('-2p63', -T63), ('2p64', 2.0 ** 64), ('1e300', 1e300)):
            out.append(x87_case(f'c2-{op}-{tag}', [x], op, {'st': {0: d2b(x), 1: None}, 'top': 7, 'c': {1: 0, 2: 1}, 'flags': 0}))
        x = T63 - 1024   # the largest double below 2^63: in range
        out.append(x87_case(f'c2-{op}-below', [x], op, {'top': 6 if op in ('fptan', 'fsincos') else 7, 'c': {1: 0, 2: 0}, 'flags': PE}))
        out.append(x87_case(f'inf-{op}', [float('inf')], op, {'st': {0: INDEF, 1: INDEF if op in ('fptan', 'fsincos') else None}, 'c': {2: 0}, 'flags': IE}))
        st = {'fsin': {0: 1 << 63}, 'fcos': {0: ONE}, 'fptan': {0: ONE, 1: 1 << 63}, 'fsincos': {0: ONE, 1: 1 << 63}}[op]   # cosine pushed over the sine
        out.append(x87_case(f'zero-{op}', [-0.0], op, {'st': st, 'c': {1: 0, 2: 0}, 'flags': 0}))
        out.append(x87_case(f'snan-{op}', [0x7ff0000000000005], op, {'st': {0: 0x7ff8000000000005}, 'flags': IE}))
    # FPREM: quotient bits (C0 = Q2, C3 = Q1, C1 = Q0), complete (C2 = 0) for an exponent difference below 64.
    for name, op, x, y, r, q in (('fprem-7-2', 'fprem', 7.0, 2.0, 1.0, 3), ('fprem--7-2', 'fprem', -7.0, 2.0, -1.0, 3),
                                 ('fprem-29-3', 'fprem', 29.0, -3.0, 2.0, 9), ('fprem-small', 'fprem', 1.0, 3.0, 1.0, 0),
                                 ('fprem1-7-2', 'fprem1', 7.0, 2.0, -1.0, 4), ('fprem1-5-2', 'fprem1', 5.0, 2.0, 1.0, 2),
                                 ('fprem1--7-2', 'fprem1', -7.0, 2.0, 1.0, 4), ('fprem1-29-3', 'fprem1', 29.0, 3.0, -1.0, 10),
                                 ('fprem1-2^62', 'fprem1', 2.0 ** 62 + 2048, 3.0, None, None), ('fprem-2^62', 'fprem', 2.0 ** 62, 3.0, 1.0, (2 ** 62 - 1) // 3)):
        if r is None:   # exact: (2^62 + 2048) = 3q + r, r in {-1, 0, 1}
            n = 2 ** 62 + 2048; q = (n + 1) // 3; r = float(n - 3 * q)
        c = {0: q >> 2 & 1, 1: q & 1, 2: 0, 3: q >> 1 & 1}
        out.append(x87_case(name, [y, x], op, {'st': {0: d2b(r), 1: d2b(y)}, 'c': c, 'flags': 0}))
    # The partial remainder (exponent difference >= 64): x - y * QQ * 2^(D - N), N = 32 + D mod 32, C2 = 1 and C0 C1 C3 = 0; the
    # loop's last step has the complete quotient's bits.
    from fractions import Fraction
    for name, x, y in (('2^100-3', 2.0 ** 100, 3.0), ('1e300-7', 1e300, 7.25), ('denormal', 1e-300, 5e-324 * 3)):
        X, Y = Fraction(x), Fraction(y)
        e = lambda v: v.numerator.bit_length() - v.denominator.bit_length() - (Fraction(2) ** (v.numerator.bit_length() - v.denominator.bit_length()) > v)
        dd = e(X) - e(Y)
        assert dd >= 64
        n = 32 + dd % 32
        Ys = Y * Fraction(2) ** (dd - n)
        part = X - Ys * (X // Ys)
        out.append(x87_case(f'partial-{name}', [y, x], 'fprem', {'st': {0: d2b(float(part))}, 'c': {0: 0, 1: 0, 2: 1, 3: 0}, 'flags': 0}, pre=['fnclex']))
        q = int(X // Y); rr = X - Y * q
        loop = ['1:', 'fprem', 'fnstsw ax', 'test ah, 4', 'jnz 1b']
        out.append(dict(x87_case(f'loop-{name}', [y, x], 'fnop', {'st': {0: d2b(float(rr))}, 'c': {0: q >> 2 & 1, 1: q & 1, 2: 0, 3: q >> 1 & 1}, 'flags': 0},
                                 pre=['fnclex'] + loop)))
    # Invalid operands and the other exception flags.
    out += [x87_case('fprem-y0', [0.0, 5.0], 'fprem', {'st': {0: INDEF}, 'c': {0: 0, 1: 0, 2: 0, 3: 0}, 'flags': IE}),
            x87_case('fprem1-xinf', [2.0, float('inf')], 'fprem1', {'st': {0: INDEF}, 'flags': IE}),
            x87_case('fprem-yinf', [float('-inf'), -3.5], 'fprem', {'st': {0: d2b(-3.5)}, 'c': {0: 0, 1: 0, 2: 0, 3: 0}, 'flags': 0}),
            x87_case('fyl2x-zero', [2.0, 0.0], 'fyl2x', {'st': {0: d2b(float('-inf')), 1: None}, 'top': 7, 'flags': ZE}),
            x87_case('fyl2x-neg', [1.0, -1.0], 'fyl2x', {'st': {0: INDEF}, 'top': 7, 'flags': IE}),
            x87_case('fyl2x-0x1', [float('inf'), 1.0], 'fyl2x', {'st': {0: INDEF}, 'flags': IE}),
            x87_case('fyl2x-exact', [3.0, 8.0], 'fyl2x', {'st': {0: d2b(9.0)}, 'flags': 0}),
            x87_case('fyl2xp1-inf0', [float('inf'), 0.0], 'fyl2xp1', {'st': {0: INDEF}, 'flags': IE}),
            x87_case('fyl2xp1-pe', [1.0, 0.25], 'fyl2xp1', {'flags': PE}),
            x87_case('fpatan-pe', [1.0, 1.0], 'fpatan', {'st': {0: d2b(0.7853981633974483)}, 'flags': PE}),
            x87_case('fpatan-zero', [-0.0, 2.0], 'fpatan', {'st': {0: 1 << 63}, 'flags': 0}),
            x87_case('f2xm1-pe', [0.5], 'f2xm1', {'flags': PE}),
            x87_case('f2xm1-m1', [-1.0], 'f2xm1', {'st': {0: d2b(-0.5)}, 'flags': 0}),
            x87_case('fscale-over', [2000.0, 1.0], 'fscale', {'st': {0: d2b(float('inf')), 1: d2b(2000.0)}, 'flags': OE | PE}),
            x87_case('fscale-under', [-1080.0, 1.0], 'fscale', {'st': {0: 0}, 'flags': UE | PE}),
            x87_case('fscale-denormal-exact', [-1074.0, 3.0], 'fscale', {'st': {0: 3}, 'flags': 0}),
            x87_case('fscale-trunc', [-2.9, 12.0], 'fscale', {'st': {0: d2b(3.0)}, 'flags': 0}),
            x87_case('fscale-rc-down', [-1074.0, -1.5], 'fscale', {'st': {0: (1 << 63) | 2}, 'flags': UE | PE},
                     pre=['mov word ptr [rdi + 0x100], 0x077f', 'fldcw word ptr [rdi + 0x100]']),
            x87_case('fscale-inv', [float('inf'), 0.0], 'fscale', {'st': {0: INDEF}, 'flags': IE}),
            x87_case('fxtract-12', [12.0], 'fxtract', {'st': {0: d2b(1.5), 1: d2b(3.0)}, 'top': 6, 'flags': 0}),
            x87_case('fxtract-denormal', [5e-324 * 3], 'fxtract', {'st': {0: d2b(1.5), 1: d2b(-1073.0)}, 'top': 6, 'flags': 0}, pre=['fnclex']),
            x87_case('fxtract-zero', [-0.0], 'fxtract', {'st': {0: 1 << 63, 1: d2b(float('-inf'))}, 'top': 6, 'flags': ZE}),
            x87_case('fxtract-inf', [float('-inf')], 'fxtract', {'st': {0: d2b(float('-inf')), 1: d2b(float('inf'))}, 'flags': 0}),
            x87_case('fxtract-snan', [0x7ff0000000000003], 'fxtract', {'st': {0: 0x7ff8000000000003, 1: 0x7ff8000000000003}, 'flags': IE})]
    # Stack underflow (an empty operand) and overflow (TOP - 1 not empty) with the masked responses: IE and SF, C1 = 0 / 1.
    unary, pushing, binary, popping = ('fsin', 'fcos', 'f2xm1'), ('fptan', 'fsincos', 'fxtract'), ('fprem', 'fprem1', 'fscale'), ('fpatan', 'fyl2x', 'fyl2xp1')
    for op in unary + pushing + binary + popping:
        out.append(x87_case(f'underflow-empty-{op}', [], op, {'st': {0: INDEF, **({1: INDEF} if op in pushing else {})}, 'top': 7 if op in pushing else 1 if op in popping else 0,
                                                             'c': {1: 0}, 'flags': IE | SF}))
        if op in binary + popping:
            out.append(x87_case(f'underflow-st1-{op}', [2.0], op, {'st': {0: INDEF} if op in binary else {0: INDEF, 1: None}, 'top': 7 if op in binary else 0,
                                                                   'c': {1: 0}, 'flags': IE | SF}))
    for op in pushing:
        out.append(x87_case(f'overflow-{op}', [1.0] * 8, op, {'st': {0: INDEF, 1: INDEF, 2: ONE}, 'top': 7, 'c': {1: 1}, 'flags': IE | SF}))
    # FPTAN pushes 1.0; FSINCOS the cosine.
    out.append(x87_case('fptan-push', [0.0], 'fptan', {'st': {0: ONE, 1: 0}, 'top': 6, 'flags': 0}))
    out.append(x87_case('fsincos-push', [0.0], 'fsincos', {'st': {0: ONE, 1: 0}, 'top': 6, 'flags': 0}))
    # Gating: #NM under CR0.TS (CPL 0) for a helper form and a string compare.
    for f in ('fsin', 'fprem', 'pcmpistri xmm1, xmm2, 0', 'pcmpestrm xmm1, xmmword ptr [rax], 0x40'):
        out.append({'name': f'ts-{f.split()[0]}', 'lines': ['mov rax, cr0', 'or rax, 8', 'mov cr0, rax', f'mov eax, {D + 0x100:#x}', f] + f6.AFTER,
                    'setup': [], 'want': ['nm', 7, 0, 0], 'cpl': 0})
    # PCMPxSTRx memory operands: no alignment requirement; #PF (not present, crossing), #GP/#SS non-canonical, precise.
    PF = lambda cr2: ['pf', 14, cr2, 4]
    for form, vex in (('pcmpistri', False), ('pcmpestrm', False), ('vpcmpistrm', True), ('vpcmpestri', True)):
        insn = lambda m: f'{form} xmm1, xmmword ptr [{m}], 0x0c'
        base = ['movdqu xmm1, xmmword ptr [rdi]']
        setup = (['xcr0 7'] if vex else []) + ['gpr-rax 5', 'gpr-rdx 9']
        out.append({'name': f'{form}-notpresent', 'lines': base + [insn(D + 0x1010)] + f6.AFTER, 'setup': setup + [f'unmap {D + 0x1000:#x}'], 'want': PF(D + 0x1010), 'cpl': 3, 'pcmp': True})
        out.append({'name': f'{form}-crossing', 'lines': base + [insn(D + 0x1000 - 7)] + f6.AFTER, 'setup': setup + [f'unmap {D + 0x1000:#x}'], 'want': PF(D + 0x1000), 'cpl': 3, 'pcmp': True})
        out.append({'name': f'{form}-noncanonical', 'lines': base + [insn('rsi')] + f6.AFTER, 'setup': setup + [f'gpr-rsi {NONCANON:#x}'], 'want': ['gp', 13, 0, 0], 'cpl': 3, 'pcmp': True})
        out.append({'name': f'{form}-noncanonical-rsp', 'lines': base + [insn('rsp')] + f6.AFTER, 'setup': setup + [f'gpr-rsp {NONCANON:#x}'], 'want': ['ss', 12, 0, 0], 'cpl': 3, 'pcmp': True})
        out.append({'name': f'{form}-straddle', 'lines': base + [insn('rsi')] + f6.AFTER, 'setup': setup + [f'gpr-rsi {NONCANON - 8:#x}'], 'want': ['gp', 13, 0, 0], 'cpl': 3, 'pcmp': True})
        out.append({'name': f'{form}-misaligned', 'lines': base + [insn('rdi + 0x13')] + KEEP, 'setup': setup, 'want': None, 'cpl': 3,
                    'pcmp': (form, D + 0x213, 0x0c, False)})
    # The 64-bit lengths of REX.W/VEX.W PCMPESTRx (the pinned QEMU 7.2 drops the W bit): RAX/RDX read whole, then saturated.
    for tag, rax, rdx, w in (('w-high', 2**32 + 3, 2**32 + 2, True), ('w-neg', 2**64 - 5, 2**63, True), ('no-w-high', 2**32 + 3, 2**32 + 2, False)):
        for vex in (False, True):
            name = f'pcmpestri-{"vex-" if vex else ""}{tag}'
            text = f'{"v" if vex else ""}pcmpestri xmm1, xmmword ptr [rdi + 0x20], 0x00'
            code = bytearray(ref.assemble(text))
            if w and vex: code[2] |= 0x80
            elif w: code[1:1] = b'\x48'
            out.append({'name': name, 'lines': ['movdqu xmm1, xmmword ptr [rdi + 1]', '.byte ' + ', '.join(f'{c:#x}' for c in code)] + KEEP,
                        'setup': (['xcr0 7'] if vex else []) + [f'gpr-rax {rax:#x}', f'gpr-rdx {rdx:#x}'], 'want': None, 'cpl': 3,
                        'pcmp': ('pcmpestri', D + 0x220, 0, w, rax, rdx, D + 0x201)})
    return out


def judge(c, code, vm, offsets, regs):
    problems = f6.judge({**c, 'x87': None, 'mem_check': None}, code, vm, offsets, regs)
    w = c.get('x87want')
    if w:
        x = vm['x87']
        mm, top, tags = [int(v, 0) for v in x['mm']], x['top'], int(x['ftw'], 0)
        if 'top' in w and top != w['top']: problems.append(f"top {top}, want {w['top']}")
        for i, v in w.get('st', {}).items():
            r = (top + i) & 7
            got = mm[r] if tags >> r & 1 else None
            if got != v: problems.append(f"st({i}) {got if got is None else hex(got)}, want {v if v is None else hex(v)}")
        for k, v in w.get('c', {}).items():
            if x['c'][k] != v: problems.append(f"c{k} {x['c'][k]}, want {v}")
        flags = int(x['exceptions'], 0) | x['ie'] | x['pe'] << 4 | x['sf'] << 6
        if 'flags' in w and flags != w['flags']: problems.append(f"flags {flags:#x}, want {w['flags']:#x}")
    pc = c.get('pcmp')
    if pc is True:   # at a fault: ECX, flags and XMM0 as before the instruction
        if vm['gpr'][1] != regs['rcx']: problems.append(f"rcx {vm['gpr'][1]:#x} written")
        if any(int(v, 0) for v in vm['xmm'][0:2]): problems.append('xmm0 written')
    elif pc:
        form, addr, imm, wide = pc[:4]
        mem = (bytes(range(256)) * 32)
        at1 = pc[6] if len(pc) > 6 else D + 0x200
        lhs, rhs = mem[at1 - D:at1 - D + 16], mem[addr - D:addr - D + 16]
        rax, rdx = (pc[4], pc[5]) if len(pc) > 4 else (5, 9)
        res, cf, zf, sf, of = sdm_pcmp(lhs, rhs, imm, rax, rdx, form.removeprefix('v').startswith('pcmpe'), wide)
        n = 8 if imm & 1 else 16
        rf = int(vm['rflags'], 0)
        if [rf & 1, rf >> 6 & 1, rf >> 7 & 1, rf >> 11 & 1, rf >> 2 & 1, rf >> 4 & 1] != [cf, zf, sf, of, 0, 0]:
            problems.append(f'rflags {rf:#x}, want CF {cf} ZF {zf} SF {sf} OF {of} PF AF 0')
        if form.endswith('i'):
            want = (res.bit_length() - 1 if imm & 0x40 else (res & -res).bit_length() - 1) if res else n
            if vm['gpr'][1] != want: problems.append(f"ecx {vm['gpr'][1]}, want {want}")
        else:
            got = int(vm['xmm'][0], 0) | int(vm['xmm'][1], 0) << 64
            if got != res: problems.append(f'xmm0 {got:#x}, want {res:#x}')
    return problems


def cases(a, host, runtime):
    results, bad = [], 0
    for c in cases_list():
        if a.only and a.only not in c['name']: continue
        code, labels = ref.assemble('\n'.join(f'{l}\nl{i}:' if not l.endswith(':') else l for i, l in enumerate(c['lines'])), labels=True)
        n = len(c['lines'])
        offsets = [0] + [labels.get(f'l{i}', None) for i in range(n - 1)]
        offsets = [o if o is not None else offsets[k - 1] for k, o in enumerate(offsets)]
        work = a.out / 'work' / c['name']; work.mkdir(parents=True, exist_ok=True)
        text, regs = f6.description(c, code)
        (work / 'program.txt').write_text(text)
        run = subprocess.run([host, runtime, 'run-program', a.fexc, work / 'program.txt', work] + ([str(a.max_block)] if a.max_block else []) +
                             vm1.CODE_SHAPE, capture_output=True, text=True)
        problems = [f'driver: {run.stderr.strip()[-300:]}'] if run.returncode else judge(c, code, json.loads(run.stdout), offsets, regs)
        bad += bool(problems)
        results.append({'name': c['name'], 'ok': not problems, 'problems': problems})
        if problems: print('FAIL', c['name'], '; '.join(problems)[:400])
    (a.out / 'cases.json').write_text(json.dumps(results, indent=1) + '\n')
    print(f'{len(results)} cases, {len(results) - bad} as expected, {bad} not')
    return bad == 0


if __name__ == '__main__': main()

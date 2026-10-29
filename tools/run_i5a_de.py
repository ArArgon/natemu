#!/usr/bin/env python3
"""MXCSR.DE (denormal operand) per SSE form (boot-features plan, chunk I5a), judged by the SDM's exception tables: QEMU 7.2
never sets DE, so it cannot arbitrate.

    tools/with-env python3 tools/run_i5a_de.py --fexc FEXC --out DIR [--max-block N]

Expected (SDM Vol.2, each instruction's "SIMD Floating-Point Exceptions"; Vol.1 4.9.2 and 11.5.2 precedence; every exception
masked): DE is set when an operand the instruction computes on is a denormal and MXCSR.DAZ is 0, for the forms that list
Denormal (add/sub/mul/div/min/max/sqrt, horizontal and alternating adds, compares and (U)COMIS, DPPS/DPPD on selected elements,
float-to-float conversions), never for the forms that do not (RCP/RSQRT estimates, ROUND*, conversions to integers, logical,
shuffle, blend, insert and move forms), never under DAZ, and not when the same operation reports IE (a NaN operand, QNaN
included: no denormal is examined then) or ZE (a denormal divided by zero). Each case is one instruction between an LDMXCSR
(1F80H, or 1FC0H with DAZ) and an STMXCSR; the other operand elements are 1.0. Only MXCSR.DE is judged.
"""
import argparse, json, pathlib, struct, subprocess, sys
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools')); sys.path.insert(0, str(ROOT / 'tools/reference'))
import run_slice1_vm as vm1, ref

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
        if text.startswith('div'):
            out.append((f'{text} | denormal / 0', text, lane0(k['den']), lane0(k['zero']), 0x1f80, False))
    for text, t, lane, de in EXTRA:
        k = F32 if t == 32 else F64; n = 4 if t == 32 else 2
        v = [k['den'] if i == lane else k['one'] for i in range(n)]
        out.append((f'{text} | denormal in element {lane}', text, v, v, 0x1f80, de))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--fexc', type=pathlib.Path, required=True); ap.add_argument('--out', type=pathlib.Path, required=True)
    ap.add_argument('--max-block', type=int); ap.add_argument('--no-build', action='store_true')
    a = ap.parse_args()
    host, runtime = (ROOT / 'target/release/natemu-host', ROOT / 'target/aarch64-unknown-none/release/natemu-runtime') if a.no_build else vm1.build()
    cs = cases()
    mem, lines = bytearray(0x40 * len(cs) + 0x40), []
    for i, (name, text, x, y, mx, _) in enumerate(cs):
        base = 0x40 * i
        mem[base:base + 16], mem[base + 16:base + 32] = vec(32 if len(x) == 4 else 64, x), vec(32 if len(y) == 4 else 64, y)
        struct.pack_into('<I', mem, base + 32, mx)
        lines += [f'ldmxcsr dword ptr [{D + base + 32}]', f'movdqu xmm1, xmmword ptr [{D + base}]',
                  f'movdqu xmm2, xmmword ptr [{D + base + 16}]', text, f'stmxcsr dword ptr [{D + base + 36}]']
    code = ref.assemble('\n'.join(lines + ['hlt']))
    a.out.mkdir(parents=True, exist_ok=True)
    desc = f"rip 0x100000\nrflags 0x2\ngpr {' '.join(['0x0'] * 4 + ['0x301000'] + ['0x0'] * 11)}\ncode {code.hex()}\nmemory {D:#x} {mem.hex()}\nsse 1\n"
    (a.out / 'program.txt').write_text(desc)
    run = subprocess.run([host, runtime, 'run-program', a.fexc, a.out / 'program.txt', a.out] + ([str(a.max_block)] if a.max_block else []) +
                         vm1.CODE_SHAPE, capture_output=True, text=True)
    if run.returncode: sys.exit(f'driver: {run.stderr[-500:]}')
    d = json.loads(run.stdout); got = bytes.fromhex(d['memory'][0]['data'])
    if d['stop'] != 'no_code': sys.exit(f"stopped {d['stop']} {d['reason']} at {d['rip']}")
    bad, results = 0, []
    for i, (name, text, x, y, mx, de) in enumerate(cs):
        out = struct.unpack_from('<I', got, 0x40 * i + 36)[0]
        ok = bool(out & 2) == de
        bad += not ok
        results.append({'case': name, 'mxcsr_in': hex(mx), 'mxcsr_out': hex(out), 'de_expected': de, 'ok': ok})
        if not ok: print('MISMATCH', name, f'MXCSR {out:#x}, DE expected {de}')
    (a.out / 'results.json').write_text(json.dumps(results, indent=1) + '\n')
    print(f'{len(cs)} cases ({len(FORMS)} forms), {len(cs) - bad} as the SDM says, {bad} not')
    sys.exit(1 if bad else 0)

if __name__ == '__main__': main()

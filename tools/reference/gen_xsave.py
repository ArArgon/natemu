#!/usr/bin/env python3
"""Seeded generator for the XSAVEOPT/XSAVEC corpus (CPUID.(EAX=0DH,ECX=1):EAX[1:0], advertised since November 3; boot-features
plan, Built (XSAVEOPT, XSAVEC)): images byte for byte against the reference AMD machine, where user mode may run XSAVE,
XSAVEOPT, XSAVEC and XRSTOR (QEMU 11.1.2's TCG has no XSAVEC or compacted XRSTOR; pinned QEMU 7.2's neither).

    python3 tools/reference/gen_xsave.py -o OUT.json [--seed N]

Each program loads known x87 (FNINIT, five FLD m64: TOP 3), MXCSR (a value with RC and masks set), XMM0..15 and YMM_Hi128
(VINSERTF128) from random data, so every component is in use on the AMD machine too (its init optimization then writes what
natemu, whose XINUSE is all ones, writes: probe `xs_probe.c`). The areas start filled with the region's random bytes, so every
byte an instruction must not write (legacy bytes 416..511, header bytes 16..63, components outside RFBM) is compared. The one
byte the SDM leaves to the implementation is normalized after each store: MXCSR_MASK (AMD 0002FFFFH with MXCSR.MM, natemu
0000FFFFH) is ANDed with FFFFH. Families:
  - `xs-xsavec-*`: XSAVEC under every RFBM (EDX:EAX within 7: the AMD machine's XCR0 is 2E7H), XSAVEC64, 0x67, a DS prefix,
    areas at 64-byte alignment within a page and across the page boundary;
  - `xs-xsaveopt-*`: XSAVEOPT likewise (the standard format; XSTATE_BV merged into the old value);
  - `xs-xrstorc-*`: XSAVEC with all components, the state changed (FNINIT, other XMM/YMM values, MXCSR), the image's
    XSTATE_BV edited, XRSTOR (compacted) under each RFBM, then XSAVE of everything into a third area (standard format; a
    component XRSTOR initialized is in its initial configuration, which the AMD machine's XSAVE records as XSTATE_BV[i] = 0 and
    natemu's as 1: that byte is ORed with 7 first);
  - `xs-xcomp-*`: XSAVEC with an RFBM subset (XCOMP_BV = 2^63 | subset), then XRSTOR with RFBM 7: components outside XCOMP_BV
    are initialized (MXCSR 1F80H with SSE), then XSAVE as above.
"""
import argparse, json, pathlib, random, struct, subprocess, sys
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gen_i9, gen_slice1 as g
from gen_slice1 import DATA

VALS, XMMV, YMMV, ALTV = DATA + 0x000, DATA + 0x40, DATA + 0x140, DATA + 0x240   # doubles, XMM, YMM_Hi128, other XMM/YMM
MX = DATA + 0x3c0                                                                 # MXCSR slots
AREAS = [DATA + 0x400, DATA + 0xe00, DATA + 0x1400]   # A within a page, B across the boundary at DATA + 0x1000, C
DOUBLES = [1.5, -2.25, 3.0e10, 3.141592653589793, 1e-300, -0.0, 7.0, 0.1]


def setup(p, r):
    p.x('fninit')
    for k in range(5): p.x(f'fld qword ptr [{VALS + 8 * k}]', 'fld')
    p.x(f'mov dword ptr [{MX}], {r.choice([0x3f80, 0x5fa0, 0x1f00 | 0x8000, 0x7f80, 0x1f80 | 0x40])}', 'mov')
    p.x(f'ldmxcsr dword ptr [{MX}]', 'ldmxcsr')
    for i in range(16):
        p.x(f'movdqu xmm{i}, xmmword ptr [{XMMV + 16 * i}]', 'movdqu')
        p.x(f'vinsertf128 ymm{i}, ymm{i}, xmmword ptr [{YMMV + 16 * i}], 1', 'vinsertf128')


def change(p, r):
    """A different state: FNINIT and one FLD, other XMM/YMM values, another MXCSR."""
    p.x('fninit'); p.x(f'fld qword ptr [{VALS + 40}]', 'fld')
    p.x(f'mov dword ptr [{MX + 4}], 0x1f80', 'mov'); p.x(f'ldmxcsr dword ptr [{MX + 4}]', 'ldmxcsr')
    for i in range(16): p.x(f'vmovdqu ymm{i}, ymmword ptr [{ALTV + 8 * i}]', 'vmovdqu')


def store(p, insn, area, rfbm, mxcsr_written, form=''):
    p.x(f'mov eax, {rfbm}', 'mov'); p.emit('xor edx, edx')
    text = f'{insn} [{area}]'
    if form == '64': text = f'{insn}64 [{area}]'
    elif form == 'rbx': p.x(f'mov ebx, {area}', 'mov'); text = f'{insn} [ebx]'      # 0x67: a 32-bit address
    elif form == 'ds': text = f'{insn} ds:[{area}]'
    p.x(text, insn)
    if mxcsr_written: p.emit(f'and dword ptr [{area + 28}], 0xffff')   # AF undefined after it (SDM AND)


def zero_header(p, area):
    """Header bytes 16..63, which XSAVEC leaves (the region's random bytes) and a compacted XRSTOR requires zero."""
    for k in range(16, 64, 8): p.x(f'mov qword ptr [{area + 512 + k}], 0', 'mov')


def program(name, rng, family):
    r = random.Random(rng.getrandbits(64)); gpr, rf = g.initial(r); p = gen_i9.Prog(name, r)
    setup(p, r)
    A, B, C = AREAS if r.random() < 0.5 else (AREAS[1], AREAS[0], AREAS[2])
    if family in ('xsavec', 'xsaveopt'):
        for k in range(3):
            rfbm = r.randrange(8); form = r.choice(['', '', '64', 'rbx', 'ds'])
            store(p, family, [A, B, C][k], rfbm, rfbm & (2 if family == 'xsavec' else 6), form)
    elif family == 'xrstorc':
        store(p, 'xsavec', A, 7, True)
        change(p, r)
        bv = r.choice([7, 7, 0, 1, 2, 3, 4, 5, 6]); rfbm = r.choice([7, 7, 1, 2, 3, 4, 5, 6, 0])
        p.x(f'mov byte ptr [{A + 512}], {bv}', 'mov')
        zero_header(p, A)
        p.x(f'mov eax, {rfbm}', 'mov'); p.emit('xor edx, edx'); p.x(f'xrstor [{A}]', 'xrstor')
        store(p, 'xsave', C, 7, True); p.emit(f'or byte ptr [{C + 512}], 7')
        p.meta = {'bv': bv, 'rfbm': rfbm}
    elif family == 'xcomp':
        sub = r.choice([0, 1, 2, 3, 4, 5, 6])
        store(p, 'xsavec', A, sub, sub & 2)
        change(p, r); zero_header(p, A)
        p.x('mov eax, 7', 'mov'); p.emit('xor edx, edx'); p.x(f'xrstor [{A}]', 'xrstor')
        store(p, 'xsave', C, 7, True); p.emit(f'or byte ptr [{C + 512}], 7')
    out = g.finish(p, gpr, rf, r)
    mem = bytearray.fromhex(out['memory'][0]['data'])
    mem[0:64] = b''.join(struct.pack('<d', v) for v in DOUBLES)
    out['memory'][0]['data'] = bytes(mem).hex()
    return out | {'sse': True, 'avx': True}


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('-o', '--output', type=pathlib.Path, required=True)
    ap.add_argument('--seed', type=int, default=20261104)
    a = ap.parse_args(); rng = random.Random(a.seed)
    progs = [program(f'xs-{f}-{i:03d}', rng, f) for f, n in (('xsavec', 16), ('xsaveopt', 12), ('xrstorc', 20), ('xcomp', 8)) for i in range(n)]
    clang = subprocess.run(['clang', '--version'], capture_output=True, text=True).stdout.splitlines()[0]
    a.output.write_text(json.dumps({'generator': {'script': 'tools/reference/gen_xsave.py', 'seed': a.seed, 'assembler': clang},
                                    'programs': progs}, indent=1) + '\n')
    print(f'{len(progs)} programs -> {a.output}')

if __name__ == '__main__': main()

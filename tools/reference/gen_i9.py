#!/usr/bin/env python3
"""Seeded generator for the I9 corpus: MOVBE, MMX, AES-NI/PCLMULQDQ (legacy and VEX.128), PDEP/PEXT and FFREEP (boot-features
plan, chunk I9).

    python3 tools/reference/gen_i9.py -o OUT.json [--seed N]

Program families (each 16 operations of its family, random data, RAX..R15 random; results stored to RESULT and compared with
the data region, the general registers and RFLAGS against pinned QEMU 7.2):
  - `i9-movbe-*`: MOVBE loads and stores at 16/32/64 bits, operands anywhere in the data region including across the page
    boundary at DATA + 0x1000 and unaligned, with ADD/XOR consumers so the loaded value matters;
  - `i9-mmx-*`: MMX forms on MM0..MM7 (every admitted opcode: arithmetic, packs/unpacks, compares, shifts by imm8 and by
    register/memory, MOVD/MOVQ with general registers and memory, PINSRW/PEXTRW, PSHUFW, PMOVMSKB, MOVNTQ, the SSE/SSE2 and
    SSSE3 MMX forms, MOVQ2DQ/MOVDQ2Q, CVTPI2PS/PD and CVT(T)PS2PI/PD2PI on integers and small floats), memory operands
    anywhere (PUNPCKL* m32 at the page boundary's last 4 bytes); then the MMX registers stored, FXSAVE's FSW and abridged tag
    byte (TOP = 0, all valid after MMX; empty after EMMS) copied out (the image's register fields hold MMX values, whose
    80-bit form natemu does not build: plan I9 deviation; the image itself is cleared), EMMS, an x87 round trip and FXSAVE's
    tag byte again;
  - `i9-aes-*`: AESENC/AESENCLAST/AESDEC/AESDECLAST/AESIMC/AESKEYGENASSIST and PCLMULQDQ (every imm8 bit pattern that selects,
    others random), legacy (m128 aligned) and VEX.128 (memory anywhere), XMM0..15 stored;
  - `i9-bmi2-*`: PDEP/PEXT at 32 and 64 bits, register and memory masks, random, sparse, dense, zero and all-ones values;
  - `i9-ffreep-*`: FLD m64 values, FFREEP ST(i) for random i, FXSAVE's FSW and tag byte and the ST fields (doubles: exact
    in both) copied out.
"""
import argparse, json, pathlib, random, struct, subprocess, sys
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import flags, gen_slice1 as g
from gen_slice1 import DATA

OPND = DATA + 0x800        # operand data (0x800..0x13ff: across the page boundary at 0x1000)
FXAREA = DATA + 0x1400     # FXSAVE image (512 bytes), cleared after use
RESULT = DATA + 0x1800
CPU = 'qemu64,+ssse3,+sse4_1,+sse4_2,+popcnt,+xsave,+avx,+movbe,+aes,+pclmulqdq,+bmi1,+bmi2'
R64 = ['rax', 'rcx', 'rdx', 'r9', 'r10', 'r11', 'r14', 'r15']   # scratch (rbx/rsi/rdi/rbp/r12/r13 point into DATA, r8 an index)
R32 = {'rax': 'eax', 'rcx': 'ecx', 'rdx': 'edx', 'r9': 'r9d', 'r10': 'r10d', 'r11': 'r11d', 'r14': 'r14d', 'r15': 'r15d'}
R16 = {'rax': 'ax', 'rcx': 'cx', 'rdx': 'dx', 'r9': 'r9w', 'r10': 'r10w', 'r11': 'r11w', 'r14': 'r14w', 'r15': 'r15w'}


class Prog(g.Prog):
    def x(self, text, op=None):
        a = {'op': op or text.split()[0], 'sse': True}
        self.undef = flags.step(a, self.undef); self.cl = None
        self.lines.append(text); self.insns.append(a)
        if len(self.insns) % 10 == 9:   # end QEMU's translation block now and then (its in_asm log garbles an instruction
            self.jumps = getattr(self, 'jumps', 0) + 1                     # crossing 1 KiB of one, gen_i6)
            self.emit(f'jmp tb{self.jumps}'); self.lines.append(f'tb{self.jumps}:')


def addr(r, size, align=1):
    """A memory operand of `size` bytes in the operand area: aligned to `align`, often near or across the page boundary."""
    if align == 1 and r.random() < 0.3:
        a = DATA + 0x1000 - r.randrange(1, size + 1)             # crossing (or ending exactly at) the boundary
    else:
        a = OPND + r.randrange(0, 0xb00 - size)
    return a - a % align


def clear_fx(p):
    """Zero the FXSAVE image (its register fields differ by plan I9's deviation and FIP/FDP by I6 deviation 8)."""
    p.x('pxor xmm15, xmm15', 'pxor')
    for k in range(0, 512, 16): p.x(f'movdqa xmmword ptr [{FXAREA + k}], xmm15', 'movdqa')


def movbe_program(name, rng):
    r = random.Random(rng.getrandbits(64)); gpr, rf = g.initial(r); p = Prog(name, r)
    for _ in range(16):
        w = r.choice([16, 32, 64]); reg = r.choice(R64); name_ = {16: R16, 32: R32, 64: {k: k for k in R64}}[w][reg]
        ptr = {16: 'word', 32: 'dword', 64: 'qword'}[w]; a = addr(r, w // 8)
        if r.random() < 0.5:
            p.x(f'movbe {name_}, {ptr} ptr [{a}]')
            other = {16: R16, 32: R32, 64: {k: k for k in R64}}[w][r.choice(R64)]
            p.emit(f'{r.choice(["add", "xor", "sub"])} {other}, {name_}')
        else:
            p.x(f'movbe {ptr} ptr [{a}], {name_}')
    out = g.finish(p, gpr, rf, r)
    return out | {'cpu': CPU}


MMX2 = ['paddb', 'paddw', 'paddd', 'paddq', 'paddsb', 'paddsw', 'paddusb', 'paddusw', 'psubb', 'psubw', 'psubd', 'psubq', 'psubsb',
        'psubsw', 'psubusb', 'psubusw', 'pmullw', 'pmulhw', 'pmulhuw', 'pmuludq', 'pmaddwd', 'pand', 'pandn', 'por', 'pxor',
        'pcmpeqb', 'pcmpeqw', 'pcmpeqd', 'pcmpgtb', 'pcmpgtw', 'pcmpgtd', 'packsswb', 'packssdw', 'packuswb', 'punpckhbw',
        'punpckhwd', 'punpckhdq', 'pavgb', 'pavgw', 'pmaxub', 'pmaxsw', 'pminub', 'pminsw', 'psadbw', 'psllw', 'pslld', 'psllq',
        'psrlw', 'psrld', 'psrlq', 'psraw', 'psrad',
        'pshufb', 'phaddw', 'phaddd', 'phaddsw', 'pmaddubsw', 'phsubw', 'phsubd', 'phsubsw', 'psignb', 'psignw', 'psignd',
        'pmulhrsw', 'pabsb', 'pabsw', 'pabsd']
UNPACKL = ['punpcklbw', 'punpcklwd', 'punpckldq']
SHIFT_IMM = ['psllw', 'pslld', 'psllq', 'psrlw', 'psrld', 'psrlq', 'psraw', 'psrad']


def mmx_op(p, r):
    mm = lambda: f'mm{r.randrange(8)}'
    k = r.random()
    if k < 0.45:
        op = r.choice(MMX2)
        src = mm() if r.random() < 0.6 else f'qword ptr [{addr(r, 8)}]'
        p.x(f'{op} {mm()}, {src}')
    elif k < 0.52:
        op = r.choice(UNPACKL)
        src = mm() if r.random() < 0.4 else f'dword ptr [{addr(r, 4) if r.random() < 0.7 else DATA + 0x1000 - 4}]'
        p.x(f'{op} {mm()}, {src}')
    elif k < 0.60:
        p.x(f'{r.choice(SHIFT_IMM)} {mm()}, {r.choice([0, 1, 7, 8, 15, 16, 31, 32, 63, 64, 200, r.randrange(256)])}')
    elif k < 0.66:
        reg = r.choice(R64)
        if r.random() < 0.5: p.x(f'movd {mm()}, {R32[reg]}') if r.random() < 0.5 else p.x(f'movq {mm()}, {reg}')
        else: p.x(f'movd {R32[reg]}, {mm()}') if r.random() < 0.5 else p.x(f'movq {reg}, {mm()}')
    elif k < 0.74:
        a = addr(r, 8); w = r.choice([4, 8]); ptr = 'dword' if w == 4 else 'qword'; a = addr(r, w)
        load = r.random() < 0.5
        mn = 'movd' if w == 4 else 'movq'
        p.x(f'{mn} {mm()}, {ptr} ptr [{a}]' if load else f'{mn} {ptr} ptr [{a}], {mm()}')
    elif k < 0.77:
        p.x(f'movntq qword ptr [{addr(r, 8)}], {mm()}')
    elif k < 0.81:
        src = mm() if r.random() < 0.5 else f'qword ptr [{addr(r, 8)}]'
        p.x(f'pshufw {mm()}, {src}, {r.randrange(256)}')
    elif k < 0.85:
        if r.random() < 0.5:
            src = R32[r.choice(R64)] if r.random() < 0.5 else f'word ptr [{addr(r, 2)}]'
            p.x(f'pinsrw {mm()}, {src}, {r.randrange(8)}')
        else:
            p.x(f'pextrw {R32[r.choice(R64)]}, {mm()}, {r.randrange(8)}')
    elif k < 0.87:
        p.x(f'pmovmskb {R32[r.choice(R64)]}, {mm()}')
    elif k < 0.90:
        src = mm() if r.random() < 0.5 else f'qword ptr [{addr(r, 8)}]'
        p.x(f'palignr {mm()}, {src}, {r.randrange(24)}')
    elif k < 0.93:
        x = f'xmm{r.randrange(15)}'
        p.x(f'movq2dq {x}, {mm()}') if r.random() < 0.5 else p.x(f'movdq2q {mm()}, {x}')
    else:
        # The conversions on integers and small floats (exact: no MXCSR flag beyond what both compute exactly).
        x = f'xmm{r.randrange(15)}'
        c = r.choice(['cvtpi2ps', 'cvtpi2pd', 'cvtps2pi', 'cvttps2pi', 'cvtpd2pi', 'cvttpd2pi'])
        if c in ('cvtpi2ps', 'cvtpi2pd'):
            src = mm() if r.random() < 0.6 else f'qword ptr [{OPND + 0xb00 + 8 * r.randrange(4)}]'   # small integers
            p.x(f'{c} {x}, {src}')
        else:
            src = f'{"qword" if c.endswith("ps2pi") else "xmmword"} ptr [{OPND + 0xb40 + 16 * r.randrange(4)}]'
            p.x(f'{c} {mm()}, {src}')


def copy_header(p, at):
    """FCW, FSW and the abridged tag byte (image bytes 0..5; FOP, FIP and FDP are 0 in natemu: I6 deviation 8)."""
    p.x(f'mov eax, dword ptr [{FXAREA}]', 'mov'); p.x(f'mov dword ptr [{at}], eax', 'mov')
    p.x(f'mov ax, word ptr [{FXAREA + 4}]', 'mov'); p.x(f'mov word ptr [{at + 4}], ax', 'mov')


def fxheader(p, at):
    """FXSAVE, then FCW/FSW/FTW (bytes 0..5) and nothing else out of the image; the image is cleared."""
    p.x(f'fxsave [{FXAREA}]')
    copy_header(p, at)
    clear_fx(p)


def mmx_program(name, rng):
    r = random.Random(rng.getrandbits(64)); gpr, rf = g.initial(r); p = Prog(name, r)
    if r.random() < 0.5: p.x('fld1'); p.x('fldpi')        # MMX after x87 use: TOP = 0, all tags valid
    for i in range(8): p.x(f'movq mm{i}, qword ptr [{OPND + 8 * r.randrange(0x100)}]')
    for _ in range(16): mmx_op(p, r)
    for i in range(8): p.x(f'movq qword ptr [{RESULT + 8 * i}], mm{i}')
    fxheader(p, RESULT + 0x40)
    p.x('emms')
    fxheader(p, RESULT + 0x48)
    p.x(f'fld qword ptr [{OPND + 0x10}]'); p.x(f'fstp qword ptr [{RESULT + 0x50}]')     # x87 after EMMS
    p.x(f'movq mm3, qword ptr [{OPND + 0x18}]'); fxheader(p, RESULT + 0x58); p.x('emms')
    out = g.finish(p, gpr, rf, r)
    data = bytearray.fromhex(out['memory'][0]['data'])
    small = lambda: struct.pack('<i', r.randrange(-1000, 1000))
    data[0xb00:0xb20] = b''.join(small() for _ in range(8))
    data[0xb40:0xb80] = b''.join(struct.pack('<ff', r.randrange(-500, 500) + r.choice([0, 0.25, 0.5]), r.randrange(-500, 500)) +
                                 struct.pack('<d', r.randrange(-500, 500)) for _ in range(4))
    for k in range(0x0, 0x20, 8): data[0x800 + k: 0x808 + k] = struct.pack('<d', r.uniform(-100, 100))
    out['memory'][0]['data'] = data.hex()
    return out | {'sse': True, 'cpu': CPU}


def aes_program(name, rng):
    r = random.Random(rng.getrandbits(64)); gpr, rf = g.initial(r); p = Prog(name, r)
    for i in range(16): p.x(f'movdqu xmm{i}, xmmword ptr [{OPND + 16 * r.randrange(0xa0)}]')
    for _ in range(16):
        op = r.choice(['aesenc', 'aesenclast', 'aesdec', 'aesdeclast', 'aesimc', 'aeskeygenassist', 'pclmulqdq', 'pclmulqdq'])
        vex = r.random() < 0.5
        d, s1 = f'xmm{r.randrange(16)}', f'xmm{r.randrange(16)}'
        src = f'xmm{r.randrange(16)}' if r.random() < 0.6 else f'xmmword ptr [{addr(r, 16, 1 if vex else 16)}]'
        imm = r.choice([0, 1, 0x10, 0x11, r.randrange(256)]) if op == 'pclmulqdq' else r.randrange(256)
        if op in ('aesimc', 'aeskeygenassist'):
            text = f'{"v" if vex else ""}{op} {d}, {src}' + (f', {imm}' if op == 'aeskeygenassist' else '')
        elif vex:
            text = f'v{op} {d}, {s1}, {src}' + (f', {imm}' if op == 'pclmulqdq' else '')
        else:
            text = f'{op} {d}, {src}' + (f', {imm}' if op == 'pclmulqdq' else '')
        p.x(text, ('v' if vex else '') + op)
    for i in range(16): p.x(f'movdqu xmmword ptr [{RESULT + 16 * i}], xmm{i}', 'movdqu')
    out = g.finish(p, gpr, rf, r)
    return out | {'sse': True, 'avx': True, 'cpu': CPU}


def bmi2_program(name, rng):
    r = random.Random(rng.getrandbits(64)); gpr, rf = g.initial(r); p = Prog(name, r)
    for i in g.SCRATCH:
        gpr[i] = r.choice([0, 2**64 - 1, r.getrandbits(64), r.getrandbits(64) & r.getrandbits(64) & r.getrandbits(64),
                           r.getrandbits(64) | r.getrandbits(64), 1 << r.randrange(64), (1 << r.randrange(65)) - 1])
    for _ in range(16):
        op = r.choice(['pdep', 'pext']); w = r.choice([32, 64])
        name_ = (lambda x: x) if w == 64 else (lambda x: R32[x])
        d, s = name_(r.choice(R64)), name_(r.choice(R64))
        m = name_(r.choice(R64)) if r.random() < 0.7 else f'{"qword" if w == 64 else "dword"} ptr [{addr(r, w // 8)}]'
        p.x(f'{op} {d}, {s}, {m}')
    out = g.finish(p, gpr, rf, r)
    return out | {'cpu': CPU}


def ffreep_program(name, rng):
    r = random.Random(rng.getrandbits(64)); gpr, rf = g.initial(r); p = Prog(name, r)
    p.x('fninit')
    depth = 0
    for _ in range(12):
        if depth < 8 and (depth == 0 or r.random() < 0.6):
            p.x(f'fld qword ptr [{OPND + 8 * r.randrange(0x20)}]'); depth += 1
        else:
            p.x(f'ffreep st({r.randrange(depth)})'); depth -= 1
    p.x(f'fxsave [{FXAREA}]')
    copy_header(p, RESULT)
    for i in range(8):   # the ST fields (bytes 0..9 of each 16): doubles widened, exact in both
        p.x(f'mov rax, qword ptr [{FXAREA + 32 + 16 * i}]', 'mov'); p.x(f'mov qword ptr [{RESULT + 8 + 16 * i}], rax', 'mov')
        p.x(f'mov ax, word ptr [{FXAREA + 40 + 16 * i}]', 'mov'); p.x(f'mov word ptr [{RESULT + 16 + 16 * i}], ax', 'mov')
    clear_fx(p)
    p.x('fninit')
    out = g.finish(p, gpr, rf, r)
    data = bytearray.fromhex(out['memory'][0]['data'])
    data[0x800:0x900] = b''.join(struct.pack('<d', r.uniform(-1e6, 1e6)) for _ in range(0x20))
    out['memory'][0]['data'] = data.hex()
    return out | {'sse': True, 'cpu': CPU}


def big_program(name, rng, op, n):
    """I9 review: n copies of one long-lowering instruction in one straight block (no QEMU block ends: the natemu side must fit
    them by translating again with fewer instructions, the host's retry), each followed by an XOR so the values chain."""
    r = random.Random(rng.getrandbits(64)); gpr, rf = g.initial(r); p = Prog(name, r)
    for i in g.SCRATCH: gpr[i] = r.getrandbits(64)
    p.x('movdqu xmm1, xmmword ptr [%d]' % OPND, 'movdqu')
    for k in range(n):
        if op == 'pcmpistri':
            p.lines.append(f'pcmpistri xmm1, xmmword ptr [{OPND + 16 + 16 * (k % 64)}], {k % 256}'); p.insns.append({'op': 'pcmpistri', 'sse': True})
            p.lines.append('add rdx, rcx'); p.insns.append({'op': 'add', 'width': 64})
        else:
            p.lines.append(op); p.insns.append({'op': op.split()[0], 'sse': True})
            p.lines.append('xor rdx, rcx'); p.insns.append({'op': 'xor', 'width': 64})
    out = g.finish(p, gpr, rf, r)
    return out | {'cpu': CPU, 'sse': True} | ({'unannotated': True} if op == 'pcmpistri' else {})


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('-o', '--output', type=pathlib.Path, required=True)
    ap.add_argument('--seed', type=int, default=20261030)
    ap.add_argument('--big', action='store_true', help='the I9 review\'s block-size cases instead (PDEP/PEXT/PCMPISTRI in numbers)')
    a = ap.parse_args(); rng = random.Random(a.seed)
    if a.big:
        progs = [big_program(f'i9-big-{op.split()[0]}{w}-{n}', rng, op, n) for op, w, n in
                 [('pdep rcx, rdx, r9', 64, 25), ('pdep rcx, rdx, r9', 64, 40), ('pdep ecx, edx, r9d', 32, 60),
                  ('pext rcx, rdx, r9', 64, 120), ('pcmpistri', '', 1000)]]
    else: progs = [f(f'i9-{k}-{i:03d}', rng) for k, f, n in [('movbe', movbe_program, 16), ('mmx', mmx_program, 48), ('aes', aes_program, 24),
                                                      ('bmi2', bmi2_program, 16), ('ffreep', ffreep_program, 16)] for i in range(n)]
    clang = subprocess.run(['clang', '--version'], capture_output=True, text=True).stdout.splitlines()[0]
    a.output.write_text(json.dumps({'generator': {'script': 'tools/reference/gen_i9.py', 'seed': a.seed, 'assembler': clang,
        'layout': {'code': hex(g.CODE), 'data': hex(DATA), 'result': hex(RESULT)}}, 'programs': progs}, indent=1) + '\n')
    print(f'{len(progs)} programs -> {a.output}')

if __name__ == '__main__': main()

#!/usr/bin/env python3
"""x87 gating, precise faults, refusals and the kernel's FPU probes (boot-features plan, chunk I6), judged by the SDM rules
below (no QEMU run: QEMU user mode cannot set CR0, and its fault state has no x87 registers).

    tools/with-env python3 tools/run_i6_faults.py --fexc FEXC --out DIR [--max-block N]

Each case: a prelude (FNINIT; FLD1; FLDZ: TOP 6, physical registers 6 and 7 valid), one x87 instruction, then instructions
that must not run; data pages may be not present or read-only, CR0 set by the prelude at CPL 0. Expected:
  - #NM under CR0.EM or CR0.TS for every x87 form (SDM Vol.3 Table 2-2), refused or resident ones included, before anything
    else; WAIT/FWAIT #NM only with CR0.TS and CR0.MP; EM alone leaves WAIT running;
  - #PF (CR2 the first faulting byte's page, error P | W | U), #GP(0) or #SS(0) (RSP base) for a non-canonical operand;
  - at a fault: RIP the instruction, the general registers as the prelude left them, the x87 state as the prelude left it
    (TOP, tags, ST values, C0..C3) and no byte of a store written;
  - an FCW clearing an exception mask (FLDCW, FLDENV, FRSTOR) loads (I5c: unmasked exceptions are tools/run_i5c.py's);
    `unsupported_form` (no code) for FBSTP (a host helper; the transcendental, remainder and scale forms run since I6b:
    tools/run_i6b.py);
  - FLD m80 onto a full stack and FSTP m80 from an empty ST(0): IE and SF, C1 = 1 / 0, the indefinite loaded or stored;
  - LOCK on an x87 form: #UD.
And the FPU probes Linux runs: the decompressor's FNINIT; FNSTSW; FNSTCW (status 0, control 037FH: an FPU is present), and the
FDIV-bug check (FLD; FDIV; FMUL; FLD; FSUBP; FISTP; FWAIT; FNINIT gives 0).
"""
import argparse, json, pathlib, struct, subprocess, sys
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools')); sys.path.insert(0, str(ROOT / 'tools/reference'))
import run_slice1_vm as vm1, ref

D = 0x200000
CODE = 0x10000
GPR0 = {'rax': D + 0x100, 'rbx': D + 0x1000 - 8, 'rcx': 0x1234, 'rdx': 0xabcdef, 'rsi': D + 0x1000, 'rdi': D + 0x200}
NONCANON = 0x0000_8000_0000_0000
PRELUDE = ['fninit', 'fld1', 'fldz']
AFTER = ['mov ecx, 1', 'mov edx, 2', 'hlt']
ONE, ZERO = 0x3ff0000000000000, 0
STATE0 = {'top': 6, 'ftw': 0xc0, 'st': {6: ZERO, 7: ONE}}     # after PRELUDE: ST(0) = 0 (register 6), ST(1) = 1 (register 7)


def case(name, insn, setup=(), want=None, prelude=(), cpl=3, mem_check=None, x87=None):
    return {'name': name, 'lines': list(prelude) + PRELUDE + [insn] + AFTER, 'setup': list(setup), 'want': want, 'cpl': cpl,
            'mem_check': mem_check, 'x87': x87, 'insn': insn}


# (name, instruction with {a}, bytes, write)
ACCESSES = [('fld-m32', 'fld dword ptr [{a}]', 4, False), ('fld-m64', 'fld qword ptr [{a}]', 8, False),
            ('fld-m80', 'fld tbyte ptr [{a}]', 10, False), ('fild-m16', 'fild word ptr [{a}]', 2, False),
            ('fild-m32', 'fild dword ptr [{a}]', 4, False), ('fild-m64', 'fild qword ptr [{a}]', 8, False),
            ('fadd-m64', 'fadd qword ptr [{a}]', 8, False), ('fiadd-m32', 'fiadd dword ptr [{a}]', 4, False),
            ('fcom-m64', 'fcom qword ptr [{a}]', 8, False), ('fdivr-m32', 'fdivr dword ptr [{a}]', 4, False),
            ('fldcw', 'fldcw word ptr [{a}]', 2, False), ('fldenv', 'fldenv [{a}]', 28, False), ('frstor', 'frstor [{a}]', 108, False),
            ('fst-m32', 'fst dword ptr [{a}]', 4, True), ('fstp-m64', 'fstp qword ptr [{a}]', 8, True),
            ('fstp-m80', 'fstp tbyte ptr [{a}]', 10, True), ('fist-m16', 'fist word ptr [{a}]', 2, True),
            ('fistp-m32', 'fistp dword ptr [{a}]', 4, True), ('fistp-m64', 'fistp qword ptr [{a}]', 8, True),
            ('fisttp-m16', 'fisttp word ptr [{a}]', 2, True), ('fnstsw-m16', 'fnstsw word ptr [{a}]', 2, True),
            ('fnstcw-m16', 'fnstcw word ptr [{a}]', 2, True), ('fnstenv', 'fnstenv [{a}]', 28, True), ('fnsave', 'fnsave [{a}]', 108, True)]


def cases():
    out = []
    PF = lambda cr2, write, present: ['pf', 14, cr2, (1 if present else 0) | (2 if write else 0) | 4]
    for name, text, n, write in ACCESSES:
        a = D + 0x1010
        out.append(case(f'{name}-notpresent', text.format(a=a), [f'unmap {D + 0x1000:#x}'], PF(a, write, False), x87=STATE0))
        if write:
            out.append(case(f'{name}-readonly', text.format(a=a), [f'readonly {D + 0x1000:#x}'], PF(a, True, True), mem_check=(a, n), x87=STATE0))
        if n > 1:
            c = D + 0x1000 - n // 2
            out.append(case(f'{name}-crossing-notpresent', text.format(a=c), [f'unmap {D + 0x1000:#x}'], PF(D + 0x1000, write, False),
                            mem_check=(c, n) if write else None, x87=STATE0))
        out.append(case(f'{name}-noncanonical', text.format(a='rdi'), [f'gpr-rdi {NONCANON:#x}'], ['gp', 13, 0, 0], x87=STATE0))
        out.append(case(f'{name}-noncanonical-rsp', text.format(a='rsp'), [f'gpr-rsp {NONCANON:#x}'], ['ss', 12, 0, 0], x87=STATE0))
        if n > 1:
            out.append(case(f'{name}-straddle', text.format(a='rdi'), [f'gpr-rdi {NONCANON - n // 2:#x}'], ['gp', 13, 0, 0], x87=STATE0))
    # Gating at CPL 0 (the prelude writes CR0 before the x87 prelude: FNINIT is gated too, so CR0 is set after it).
    cr0 = lambda bits: ['mov rax, cr0', f'or rax, {bits}', 'mov cr0, rax', f'mov eax, {D + 0x100:#x}']
    forms = ['fld1', 'fadd st(0), st(1)', 'fld qword ptr [rax]', 'fnstsw ax', 'fninit', 'fnclex', 'fldcw word ptr [rax]',
             'fnstenv [rax]', 'fld tbyte ptr [rax]', 'fsin', 'fxam', 'fistp dword ptr [rax]', 'fcomi st(0), st(1)']
    for f in forms:
        tag = f.split()[0]
        for ctl, bits in (('ts', 8), ('em', 4), ('em-ts', 12), ('ts-mp', 10)):
            out.append({'name': f'{ctl}-{tag}', 'lines': PRELUDE + cr0(bits) + [f] + AFTER, 'setup': [], 'want': ['nm', 7, 0, 0],
                        'cpl': 0, 'mem_check': None, 'x87': None, 'insn': f})
    for ctl, bits, want in (('ts', 8, None), ('ts-mp', 10, ['nm', 7, 0, 0]), ('em', 4, None), ('em-mp', 6, None), ('mp', 2, None)):
        out.append({'name': f'{ctl}-wait', 'lines': PRELUDE + cr0(bits) + ['fwait'] + AFTER, 'setup': [], 'want': want, 'cpl': 0,
                    'mem_check': None, 'x87': None, 'insn': 'fwait'})
    # Refusals.
    # (I5c: an FCW clearing a mask loads; unmasked x87 exceptions are tools/run_i5c.py's.)
    out += [case('fldcw-unmasked', 'fldcw word ptr [rdi]', [f'memory {D + 0x200:#x} 7e03'], None, x87=STATE0),
            case('fldenv-unmasked', 'fldenv [rdi]', [f'memory {D + 0x200:#x} 7303'], None),
            case('frstor-unmasked', 'frstor [rdi]', [f'memory {D + 0x200:#x} 3302'], None),
            case('fldcw-masked-ok', 'fldcw word ptr [rdi]', [f'memory {D + 0x200:#x} 7f0f'], None),
            case('fbstp-refused', 'fbstp tbyte ptr [rdi]', [], ['nocode', 'unsupported_form']),
            case('lock-fadd', '.byte 0xf0, 0xd8, 0xc1', [], ['ud', 6, 0, 0], x87=STATE0)]
    # Stack faults the resident handles exactly (masked responses): FLD m80 onto a full stack, FSTP m80 from an empty one.
    out.append({'name': 'fld-m80-overflow', 'lines': ['fninit'] + ['fld1'] * 8 + ['fld tbyte ptr [rdi]', 'fnstsw ax', 'fstp qword ptr [rdi + 0x40]'] + AFTER,
                'setup': [], 'want': None, 'cpl': 3, 'mem_check': None, 'x87': None, 'insn': 'fld tbyte',
                'check': lambda vm, mem: (vm['gpr'][0] & 0xffff == 0x3a41 or f"fsw {vm['gpr'][0] & 0xffff:#x}, want 3A41H (IE SF C1, TOP 7)",
                                           mem[0x240:0x248] == struct.pack('<Q', 0xfff8000000000000) or 'indefinite not loaded')})
    out.append({'name': 'fstp-m80-underflow', 'lines': ['fninit', 'fstp tbyte ptr [rdi]', 'fnstsw ax'] + AFTER,
                'setup': [], 'want': None, 'cpl': 3, 'mem_check': None, 'x87': None, 'insn': 'fstp tbyte',
                'check': lambda vm, mem: (vm['gpr'][0] & 0xffff == 0x0841 or f"fsw {vm['gpr'][0] & 0xffff:#x}, want 0841H (IE SF, TOP 1)",
                                           mem[0x200:0x20a] == bytes.fromhex('00000000000000c0ffff') or f'stored {mem[0x200:0x20a].hex()}')})
    # FXAM (patch 0019): x86's classes of a double (C3 C2 C0; C1 the sign): empty, zero, normal (a denormal double too: a normal
    # 80-bit value), infinity, NaN.
    for tag, value, fsw in (('zero', 0, 0x4000 | 6 << 11), ('neg-normal', 0xbff0000000000000, 0x0600 | 6 << 11),
                            ('denormal-double', 1, 0x0402 | 6 << 11), ('neg-inf', 0xfff0000000000000, 0x0700 | 6 << 11),
                            ('nan', 0x7ff8000000000001, 0x0100 | 6 << 11)):
        out.append({'name': f'fxam-{tag}', 'lines': ['fninit', 'fld1', 'fld qword ptr [rdi]', 'fxam', 'fnstsw ax'] + AFTER,
                    'setup': [f'memory {D + 0x200:#x} {value.to_bytes(8, "little").hex()}'], 'want': None, 'cpl': 3, 'mem_check': None,
                    'x87': None, 'insn': 'fxam',
                    'check': lambda vm, mem, fsw=fsw: (vm['gpr'][0] & 0xffff == fsw or f"fsw {vm['gpr'][0] & 0xffff:#x}, want {fsw:#x}",)})
    out.append({'name': 'fxam-empty', 'lines': ['fninit', 'fxam', 'fnstsw ax'] + AFTER, 'setup': [], 'want': None, 'cpl': 3,
                'mem_check': None, 'x87': None, 'insn': 'fxam',
                'check': lambda vm, mem: (vm['gpr'][0] & 0xfdff == 0x4100 or f"fsw {vm['gpr'][0] & 0xffff:#x}, want 4100H (C1: the stale sign)",)})
    # x87 flags stay out of MXCSR, and x87 arithmetic rounds by FCW.RC when MXCSR.RC differs (fexc --x87-fpcr): 1/3 to
    # nearest by x87, then by DIVSD under MXCSR's round-up in the same block, and the other way round.
    third = struct.pack('<d', 3.0).hex()
    for tag, mxcsr, fcw, x87, sse in (('x87-near-sse-up', 0x5f80, 0x037f, 0x3fd5555555555555, 0x3fd5555555555556),
                                      ('x87-up-sse-near', 0x1f80, 0x0b7f, 0x3fd5555555555556, 0x3fd5555555555555),
                                      ('x87-zero-sse-ftz', 0x9f80, 0x0f7f, 0x3fd5555555555555, 0x3fd5555555555555)):
        out.append({'name': f'rounding-{tag}', 'lines': ['fninit', 'fldcw word ptr [rdi + 16]', 'fld1', 'fdiv qword ptr [rdi + 8]',
                    'fstp qword ptr [rdi + 24]', 'mov eax, 1', 'cvtsi2sd xmm1, eax', 'divsd xmm1, qword ptr [rdi + 8]',
                    'movsd qword ptr [rdi + 32], xmm1', 'fld1', 'fldz', 'fdivp st(1), st(0)', 'fstp st(0)'] + AFTER,
                    'setup': [f'mxcsr {mxcsr:#x}', f'memory {D + 0x208:#x} {third}{fcw.to_bytes(2, "little").hex()}'], 'want': None, 'cpl': 3,
                    'mem_check': None, 'x87': None, 'insn': 'fdiv',
                    'check': lambda vm, mem, x87=x87, sse=sse, mxcsr=mxcsr: (
                        mem[0x218:0x220] == struct.pack('<Q', x87) or f'x87 1/3 {mem[0x218:0x220][::-1].hex()}',
                        mem[0x220:0x228] == struct.pack('<Q', sse) or f'sse 1/3 {mem[0x220:0x228][::-1].hex()}',
                        int(vm['mxcsr'], 0) == mxcsr | 0x20 or f"mxcsr {vm['mxcsr']} (x87 ZE/PE must not reach it)",
                        int(vm['x87']['exceptions'], 0) & 0x12 == 0x12 or f"x87 exceptions {vm['x87']['exceptions']} (want ZE and PE)")})
    # FIST of a NaN or out of range: the integer indefinite and IE, never PE (patch 0019); in range and inexact: PE alone.
    for tag, value, insn, n, want, fsw in (('fistp16-range', 40000.5, 'fistp word ptr [rdi + 16]', 2, 0x8000, 0x0001),
                                          ('fistp32-range', 3e9 + 0.5, 'fistp dword ptr [rdi + 16]', 4, 0x80000000, 0x0001),
                                          ('fistp64-range', 1e19, 'fistp qword ptr [rdi + 16]', 8, 1 << 63, 0x0001),
                                          ('fisttp16-range', -40000.75, 'fisttp word ptr [rdi + 16]', 2, 0x8000, 0x0001),
                                          # -2.5 to nearest is -2: rounded toward zero, so C1 = 0 is also x86's (not deviation 5)
                                          ('fistp16-inexact', -2.5, 'fistp word ptr [rdi + 16]', 2, 0xfffe, 0x0020),
                                          ('fistp32-nan', float('nan'), 'fistp dword ptr [rdi + 16]', 4, 0x80000000, 0x0001)):
        out.append({'name': tag, 'lines': ['fninit', 'fld qword ptr [rdi + 8]', insn, 'fnstsw ax'] + AFTER,
                    'setup': [f'memory {D + 0x208:#x} {struct.pack("<d", value).hex()}{"00" * 8}'], 'want': None, 'cpl': 3,
                    'mem_check': None, 'x87': None, 'insn': insn,
                    'check': lambda vm, mem, n=n, want=want, fsw=fsw: (
                        int.from_bytes(mem[0x210:0x210 + n], 'little') == want or f'stored {mem[0x210:0x210 + n][::-1].hex()}',
                        vm['gpr'][0] & 0xffff == fsw or f"fsw {vm['gpr'][0] & 0xffff:#x}, want {fsw:#x}")})
    # Stack faults in generated code (patch 0019), masked responses by SDM Vol.1 8.5.1.1 and each instruction's "FPU Flags
    # Affected" (QEMU 7.2 models none: it pushes over a full stack and reads empty registers' stale values). Overflow: IE, SF,
    # C1 = 1, the indefinite pushed; underflow: IE, SF, C1 = 0, the indefinite as the operand (an arithmetic or FXCH destination
    # takes it, a store writes it or the integer indefinite, a compare is unordered). Each case ends with FNSTSW AX; `ftw` is
    # the abridged tag byte, `st` physical registers' doubles, `mem` (offset from rdi, bytes) a store's result.
    IND, IND32 = 0xfff8000000000000, 0xffc00000
    full = ['fninit'] + ['fld1'] * 8
    stack = (
        ('of-fld-m64', full + ['fld qword ptr [rdi + 8]'], 0x3a41, 0xff, {7: IND}, None),
        ('of-fild-m32', full + ['fild dword ptr [rdi + 8]'], 0x3a41, 0xff, {7: IND}, None),
        ('of-fldz', full + ['fldz'], 0x3a41, 0xff, {7: IND}, None),
        ('of-fld-st', full + ['fld st(3)'], 0x3a41, 0xff, {7: IND}, None),
        ('of-fld-m32', full + ['fld dword ptr [rdi + 8]'], 0x3a41, 0xff, {7: IND}, None),
        ('uf-fadd-st', ['fninit', 'fld1', 'fadd st(0), st(1)'], 0x3841, 0x80, {7: IND}, None),
        ('uf-fadd-m64', ['fninit', 'fadd qword ptr [rdi + 8]'], 0x0041, 0x01, {0: IND}, None),
        ('uf-fsubp', ['fninit', 'fld1', 'fsubp st(1), st(0)'], 0x0041, 0x01, {0: IND}, None),   # ST(1) empty: destination register 0
        ('uf-fsqrt', ['fninit', 'fsqrt'], 0x0041, 0x01, {0: IND}, None),
        ('uf-fchs', ['fninit', 'fchs'], 0x0041, 0x01, {0: IND}, None),                          # the indefinite, not its negation
        ('uf-fabs', ['fninit', 'fabs'], 0x0041, 0x01, {0: IND}, None),
        ('uf-fst-m64', ['fninit', 'fst qword ptr [rdi + 0x40]'], 0x0041, 0x00, {}, (8, IND)),
        ('uf-fstp-m64', ['fninit', 'fstp qword ptr [rdi + 0x40]'], 0x0841, 0x00, {}, (8, IND)),
        ('uf-fstp-m32', ['fninit', 'fstp dword ptr [rdi + 0x40]'], 0x0841, 0x00, {}, (4, IND32)),
        ('uf-fst-st', ['fninit', 'fld1', 'fincstp', 'fst st(1)'], 0x0041, 0x82, {1: IND}, None),  # ST(0) empty: ST(1) takes the indefinite
        ('uf-fist-m32', ['fninit', 'fist dword ptr [rdi + 0x40]'], 0x0041, 0x00, {}, (4, 0x80000000)),
        ('uf-fistp-m16', ['fninit', 'fistp word ptr [rdi + 0x40]'], 0x0841, 0x00, {}, (2, 0x8000)),
        ('uf-fisttp-m64', ['fninit', 'fisttp qword ptr [rdi + 0x40]'], 0x0841, 0x00, {}, (8, 1 << 63)),
        ('uf-fcom', ['fninit', 'fld1', 'fcom st(1)'], 0x7d41, 0x80, {7: ONE}, None),
        ('uf-fucom', ['fninit', 'fld1', 'fucom st(1)'], 0x7d41, 0x80, {7: ONE}, None),
        ('uf-fcomp', ['fninit', 'fld1', 'fcomp st(1)'], 0x4541, 0x00, {}, None),
        ('uf-ftst', ['fninit', 'ftst'], 0x4541, 0x00, {}, None),
        ('uf-fcomi', ['fninit', 'fld1', 'fcomi st(0), st(1)', 'setz byte ptr [rdi + 0x40]', 'setp byte ptr [rdi + 0x41]', 'setb byte ptr [rdi + 0x42]'],
         0x3841, 0x80, {7: ONE}, (3, 0x010101)),
        ('uf-fucomip', ['fninit', 'fld1', 'fucomip st(0), st(1)'], 0x0041, 0x00, {}, None),
        ('uf-fxch', ['fninit', 'fld1', 'fxch st(1)'], 0x3841, 0x81, {7: IND, 0: ONE}, None),   # the empty register takes the indefinite, then the exchange
        ('uf-fld-st', ['fninit', 'fld1', 'fld st(1)'], 0x3041, 0xc0, {6: IND, 7: ONE}, None),
        ('no-fault-fxam-ffree', ['fninit', 'fld1', 'ffree st(0)', 'fxam'], 0x7900, 0x00, {}, None),        # FXAM of an empty register: no fault
        ('clean-after-fault', ['fninit', 'fadd qword ptr [rdi + 8]', 'fnclex', 'fld1', 'fadd st(0), st(1)'], 0x3800, 0x81, {7: IND, 0: IND}, None),
    )
    for tag, lines, fsw, ftw, st, mem in stack:
        out.append({'name': f'stack-{tag}', 'lines': lines + ['fnstsw ax'] + AFTER, 'setup': [f'memory {D + 0x208:#x} 000000000000f03f{"00" * 0x40}'],
                    'want': None, 'cpl': 3, 'mem_check': None, 'x87': None, 'insn': lines[-1],
                    'check': lambda vm, mem_, fsw=fsw, ftw=ftw, st=st, m=mem: (
                        vm['gpr'][0] & 0xffff == fsw or f"fsw {vm['gpr'][0] & 0xffff:#x}, want {fsw:#x}",
                        int(vm['x87']['ftw'], 0) == ftw or f"ftw {vm['x87']['ftw']}, want {ftw:#x}",
                        all(int(vm['x87']['mm'][r], 0) == v for r, v in st.items()) or f"mm {vm['x87']['mm']}, want {st}",
                        m is None or int.from_bytes(mem_[0x240:0x240 + m[0]], 'little') == m[1] or f'stored {mem_[0x240:0x240 + m[0]][::-1].hex()}')})
    # A store's stack fault and its #PF: the #PF wins, nothing of the instruction is done (FSW, TOP, tags as before).
    out.append({'name': 'stack-uf-fstp-pf', 'lines': ['fninit', 'fld1', 'fldz', 'ffree st(1)', 'fincstp', 'fincstp', f'fstp qword ptr [{D + 0x1010}]'] + AFTER,
                'setup': [f'unmap {D + 0x1000:#x}'], 'want': PF(D + 0x1010, True, False), 'cpl': 3, 'mem_check': None,
                'x87': {'top': 0, 'ftw': 0x40, 'st': {6: ZERO}}, 'insn': 'fstp qword'})
    # Review cases (I6 review): C1 after loads and FCHS/FABS (SDM: 0, 1 only on overflow), SNaN through m32/m64 loads and m64
    # stores (IE, quieted), FLD m64 of a denormal (DE), an underflow beside a QNaN (the indefinite), FCOMI's OF/SF/AF and C1,
    # 66 + REX.W environment images, re-keying across FLDCW/FLDENV/LDMXCSR, #AC on x87 forms at CPL 3.
    q = lambda x: struct.pack('<Q', x).hex()
    m8 = lambda m, o: struct.unpack('<Q', m[o:o + 8])[0]
    def rc(name, lines, setup, check, cpl=3):
        out.append({'name': name, 'lines': lines + AFTER, 'setup': setup, 'want': None, 'cpl': cpl, 'mem_check': None, 'x87': None,
                    'insn': lines[-1], 'check': check})
    for tag, insn in (('fld-m64', 'fld qword ptr [rdi + 8]'), ('fld1', 'fld1'), ('fldpi', 'fldpi'), ('fchs', 'fchs'), ('fabs', 'fabs'),
                      ('fild', 'fild dword ptr [rdi + 8]'), ('fld-st', 'fld st(0)'), ('fcomi', 'fcomi st(0), st(0)')):
        rc(f'c1-after-{tag}', ['fninit', 'fld qword ptr [rdi + 0x10]', 'fxam', insn, 'fnstsw ax'],
           [f'memory {D + 0x208:#x} {q(0x3ff0000000000000)}{q(0xbff0000000000000)}'],
           lambda vm, m: (vm['gpr'][0] & 0x200 == 0 or f"fsw {vm['gpr'][0] & 0xffff:#x}: C1 set (SDM: 0)",))
    for tag, setup_bits, lines, want_bits, want_fsw in (
            ('snan-fld-m64', q(0x7ff0000000000001), ['fld qword ptr [rdi + 8]', 'fstp qword ptr [rdi + 0x40]'], 0x7ff8000000000001, 0x0001),
            ('snan-fld-m32', '0100807f00000000', ['fld dword ptr [rdi + 8]', 'fstp qword ptr [rdi + 0x40]'], 0x7ff8000020000000, 0x0001),
            ('snan-fst-m64', q(0x8000000000000800) + 'ff7f', ['fld tbyte ptr [rdi + 8]', 'fstp qword ptr [rdi + 0x40]'], 0x7ff8000000000001, 0x0001),
            ('denormal-fld-m64', q(1), ['fld qword ptr [rdi + 8]', 'fstp qword ptr [rdi + 0x40]'], 1, 0x0002),
            ('uf-with-qnan', q(0x7ff8000000000123), ['fld qword ptr [rdi + 8]', 'fadd st(0), st(1)', 'fstp qword ptr [rdi + 0x40]'], 0xfff8000000000000, 0x0041)):
        rc(tag, ['fninit'] + lines + ['fnstsw ax'], [f'memory {D + 0x208:#x} {setup_bits}'],
           lambda vm, m, wb=want_bits, wf=want_fsw: (m8(m, 0x240) == wb or f'stored {m8(m, 0x240):#x}, want {wb:#x}',
                                                     vm['gpr'][0] & 0xffff == wf or f"fsw {vm['gpr'][0] & 0xffff:#x}, want {wf:#x}"))
    rc('fcomi-clears-osa', ['lea rsp, [rsi]', 'fninit', 'fld1', 'fldz', 'mov eax, 0x7fffffff', 'add eax, 0x7fffffff', 'mov al, 0x0f', 'add al, 1',
                            'fcomi st(0), st(1)', 'pushfq', 'pop rax'], [],
       lambda vm, m: (vm['gpr'][0] & 0x8d5 == 0x01 or f"rflags {vm['gpr'][0]:#x}: want CF only (OF SF AF ZF PF cleared)",))
    # 66 + REX.W: REX.W wins, the 28-byte image (bytes 12..27 written, 0 here).
    rc('fnstenv-66-rexw', ['fninit', 'fld1', 'mov dword ptr [rdi + 0x4c], 0x11111111', '.byte 0x66, 0x48, 0xd9, 0x77, 0x40', 'fnstsw ax'], [],
       lambda vm, m: (m[0x24c:0x250] == bytes(4) or f'image {m[0x240:0x25c].hex()}: the 16-bit image (x86: 28 bytes)',
                      m[0x240:0x24a] == bytes.fromhex('7f03000000380000ff3f') or f'image {m[0x240:0x24a].hex()}'))
    third = struct.pack('<d', 3.0).hex()
    for tag, last, setup, second in (('fldcw', 'fldcw word ptr [rdi + 16]', [f'memory {D + 0x208:#x} {third}{(0x0b7f).to_bytes(2, "little").hex()}'], 0x3fd5555555555556),
                                     ('ldmxcsr', 'ldmxcsr dword ptr [rdi + 16]', [f'memory {D + 0x208:#x} {third}{(0x5f80).to_bytes(4, "little").hex()}'], 0x3fd5555555555555),
                                     ('fldenv', 'fldenv [rdi + 0x80]', [f'memory {D + 0x208:#x} {third}', f'memory {D + 0x280:#x} 7f0b000000000000ffff0000' + '00' * 16], 0x3fd5555555555556)):
        rc(f'rekey-{tag}', ['fninit', 'lea rbx, [rdi + 0x40]', 'mov ecx, 2', 'jmp 1f', '1: fld1', 'fdiv qword ptr [rdi + 8]', 'fstp qword ptr [rbx]',
                            'add rbx, 8', last, 'dec ecx', 'jnz 1b', 'fnstsw ax'], setup,
           lambda vm, m, second=second: (m8(m, 0x240) == 0x3fd5555555555555 or f'first {m8(m, 0x240):#x}',
                                         m8(m, 0x248) == second or f'second {m8(m, 0x248):#x}, want {second:#x}'))
    # #AC at CPL 3 (CR0.AM, RFLAGS.AC) by SDM Vol.3 Table 7-7: m16 2, m32 4, m64 and m80 8, environment/save images 4 (2 with 66).
    head = ['mov rax, cr0', 'bts rax, 18', 'mov cr0, rax']
    nop_at = CODE + len(ref.assemble('\n'.join(head)))
    for tag, insn, ac in (('fld80-4', 'fld tbyte ptr [rdi + 4]', True), ('fld80-8', 'fld tbyte ptr [rdi + 8]', False),
                          ('fstp80-4', 'fstp tbyte ptr [rdi + 4]', True), ('fnstenv-2', 'fnstenv [rdi + 2]', True), ('fnstenv-4', 'fnstenv [rdi + 4]', False),
                          ('fnstenv16-1', '.byte 0x66, 0xd9, 0x77, 0x01', True), ('fnstenv16-2', '.byte 0x66, 0xd9, 0x77, 0x02', False),
                          ('fldcw-1', 'fldcw word ptr [rdi + 0x11]', True), ('fnsave-2', 'fnsave [rdi + 2]', True), ('fnsave-4', 'fnsave [rdi + 4]', False),
                          ('fld64-4', 'fld qword ptr [rdi + 4]', True), ('fnstsw-1', 'fnstsw word ptr [rdi + 1]', True),
                          ('fild16-1', 'fild word ptr [rdi + 1]', True), ('fist32-2', 'fist dword ptr [rdi + 2]', True), ('fst32-4', 'fst dword ptr [rdi + 4]', False)):
        out.append({'name': f'ac-{tag}', 'lines': head + ['nop', 'fninit', 'fld1', insn] + AFTER, 'setup': ['rflags 0x40202', f'switch {nop_at:#x} 3'],
                    'want': ['ac', 17, 0, 0] if ac else None, 'cpl': 0, 'mem_check': None, 'x87': None, 'insn': insn})
    # Conversion flags belong to a completed instruction: #PF/#GP takes priority over a masked x87 store's PE/OE/UE/IE.
    # Existing access cases store exact zero, so they cannot detect prematurely committed FPSR flags.
    stores = [('fist-pe', 'fist dword ptr [{a}]', 4, 1.5),
              ('fistp-pe', 'fistp qword ptr [{a}]', 8, 1.5),
              ('fisttp-pe', 'fisttp word ptr [{a}]', 2, 1.5),
              ('fst-oe', 'fst dword ptr [{a}]', 4, 1e300),
              ('fstp-ue', 'fstp dword ptr [{a}]', 4, 2.0 ** -150),
              ('fist-ie', 'fist dword ptr [{a}]', 4, float('inf'))]
    for name, text, width, value in stores:
        bits = struct.unpack('<Q', struct.pack('<d', value))[0]
        pre = ['fninit', 'fld qword ptr [rdi]', 'fnclex']
        state = {'top': 7, 'ftw': 0x80, 'st': {7: bits}}
        for kind, addr, setup, fault in (
            ('notpresent', D + 0x1010, [f'unmap {D + 0x1000:#x}'], PF(D + 0x1010, True, False)),
            ('readonly', D + 0x1010, [f'readonly {D + 0x1000:#x}'], PF(D + 0x1010, True, True)),
            ('crossing', D + 0x1000 - width // 2, [f'unmap {D + 0x1000:#x}'], PF(D + 0x1000, True, False)),
            ('noncanonical', NONCANON, [], ['gp', 13, 0, 0])):
            # A 64-bit address is explicit in a register: x86 disp32 cannot encode NONCANON.
            insn = text.format(a='rbx')
            out.append({'name': f'commit-{name}-{kind}', 'lines': pre + [insn] + AFTER,
                        'setup': [f'memory {D + 0x200:#x} {struct.pack("<d", value).hex()}', f'gpr-rbx {addr:#x}'] + setup,
                        'want': fault, 'cpl': 3, 'mem_check': (addr, width) if kind in ('readonly', 'crossing') else None,
                        'x87': state, 'insn': insn})
    # A masked stack underflow has priority over denormal arithmetic flags (SDM Vol.1 4.9.2).
    for mnemonic in ('fadd', 'fcom'):
        insn = f'{mnemonic} qword ptr [rdi]'
        out.append({'name': f'commit-stack-{mnemonic}', 'lines': ['fninit', insn] + AFTER,
                    'setup': [f'memory {D + 0x200:#x} 0100000000000000'], 'want': None, 'cpl': 3,
                    'mem_check': None, 'x87': None, 'insn': insn,
                    'check': lambda vm, mem: (vm['x87']['ie'] == 1 and vm['x87']['sf'] == 1 or 'missing stack fault',
                                              int(vm['x87']['exceptions'], 0) & ~1 == 0 or f"extra operation flags {vm['x87']['exceptions']}")})
    out.append({'name': 'commit-stack-fld-overflow', 'lines': ['fninit'] + ['fld1'] * 8 + ['fld qword ptr [rdi]'] + AFTER,
                'setup': [f'memory {D + 0x200:#x} 0100000000000000'], 'want': None, 'cpl': 3,
                'mem_check': None, 'x87': None, 'insn': 'fld qword',
                'check': lambda vm, mem: (vm['x87']['ie'] == 1 and vm['x87']['sf'] == 1 or 'missing stack fault',
                                          int(vm['x87']['exceptions'], 0) & ~1 == 0 or f"extra operation flags {vm['x87']['exceptions']}")})
    # Linux's FPU probes.
    out.append({'name': 'probe-has-fpu', 'lines': ['mov word ptr [rdi], 0xffff', 'fninit', 'fnstsw ax', 'fnstcw word ptr [rdi]'] + AFTER,
                'setup': [], 'want': None, 'cpl': 0, 'mem_check': None, 'x87': None, 'insn': 'fninit',
                'check': lambda vm, mem: (vm['gpr'][0] & 0xffff == 0 or 'status not 0',
                                           int.from_bytes(mem[0x200:0x202], 'little') & 0x103f == 0x3f or 'control & 103FH != 3FH')})
    x, y = struct.pack('<d', 4195835.0), struct.pack('<d', 3145727.0)
    out.append({'name': 'probe-fdiv-bug', 'lines': ['fninit', 'fld qword ptr [rdi + 8]', 'fdiv qword ptr [rdi + 16]', 'fmul qword ptr [rdi + 16]',
                                                  'fld qword ptr [rdi + 8]', 'fsubp st(1), st(0)', 'fistp dword ptr [rdi]', 'fwait', 'fninit'] + AFTER,
                'setup': [f'memory {D + 0x200:#x} ffffffff00000000{x.hex()}{y.hex()}'], 'want': None, 'cpl': 0, 'mem_check': None, 'x87': None,
                'insn': 'fdiv', 'check': lambda vm, mem: (mem[0x200:0x204] == bytes(4) or f'fdiv_bug {mem[0x200:0x204].hex()}',)})
    return out


def description(c, code):
    regs = dict(GPR0)
    setup = []
    for s in c['setup']:
        if s.startswith('gpr-'):
            r, v = s[4:].split(); regs[r] = int(v, 0)
        else: setup.append(s)
    lines = [f'rip {CODE:#x}', 'rflags 0x202', 'gpr ' + ' '.join(f'{regs.get(r, 0):#x}' for r in ref.REGS), f'code {code.hex()}',
             f'region {D:#x} 0x2000', f'cpl {c["cpl"]}', 'sse 1']
    lines += [f'memory {D:#x} ' + bytes(range(256)).hex() * 32]
    return '\n'.join(lines + setup) + '\n', regs


def judge(c, code, vm, offsets, regs):
    problems = []
    k = len(c['lines']) - len(AFTER) - 1
    rip = CODE + offsets[k]
    gpr = [int(v, 0) for v in vm['gpr']]
    vm['gpr'] = gpr
    mem = bytes.fromhex(vm['memory'][0]['data'])
    want = c['want']
    if want and want[0] == 'stop':
        if vm['stop'] != 'resident' or not vm['reason'].startswith(want[1]) or int(vm['rip'], 0) != rip:
            problems.append(f"stop {vm['stop']} {vm['reason']} rip {vm['rip']}, want {want[1]} at {rip:#x}")
    elif want and want[0] == 'nocode':
        # A refused form: since the no-stop change #UD at the instruction (a REFUSED decode), not a stop.
        if vm['stop'] != 'guest_fault' or int(vm['fault'][0], 0) != 6 or int(vm['rip'], 0) != rip or not vm['stats']['decodes'].get(f'{0x7b:#x}'):
            problems.append(f"stop {vm['stop']} {vm['reason']} {vm['fault']} rip {vm['rip']}, want the REFUSED #UD ({want[1]}) at {rip:#x}")
    elif want:
        _, vector, cr2, error = want
        got = [int(v, 0) for v in vm['fault']]
        if vm['stop'] != 'guest_fault' or got[:3] != [vector, cr2, error] or got[3] != rip:
            problems.append(f"fault {vm['stop']} {vm['reason']} {vm['fault']} rip {vm['rip']}, want {[vector, cr2, error]} at {rip:#x}")
        if 'mov rax, cr0' not in c['lines'] and gpr != [regs.get(r, 0) for r in ref.REGS]:
            problems.append(f'gpr {gpr}')
    elif not vm1.at_hlt(vm, CODE + offsets[-1]):
        problems.append(f"did not reach hlt: {vm['stop']} {vm['reason']} {vm['fault']} rip {vm['rip']}")
    if c['x87']:
        x = vm['x87']
        mm = [int(v, 0) for v in x['mm']]
        state = (x['top'], int(x['ftw'], 0), {r: mm[r] for r in c['x87']['st']}, x['c'], int(x['exceptions'], 0), x['ie'] | x['pe'] | x['sf'])
        if state != (c['x87']['top'], c['x87']['ftw'], c['x87']['st'], [0, 0, 0, 0], 0, 0):
            problems.append(f'x87 state {state}')
    if c['mem_check']:
        a, n = c['mem_check']
        if mem[a - D:a - D + n] != (bytes(range(256)) * 32)[a - D:a - D + n]: problems.append(f'bytes at {a:#x} written')
    for r in c.get('check', lambda vm, mem: ())(vm, mem[0:]):
        if r is not True: problems.append(r)
    return problems


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--fexc', type=pathlib.Path, required=True); ap.add_argument('--out', type=pathlib.Path, required=True)
    ap.add_argument('--max-block', type=int); ap.add_argument('--no-build', action='store_true'); ap.add_argument('--only')
    a = ap.parse_args()
    host, runtime = (ROOT / 'target/release/natemu-host', ROOT / 'target/aarch64-unknown-none/release/natemu-runtime') if a.no_build else vm1.build()
    results, bad = [], 0
    for c in cases():
        if a.only and a.only not in c['name']: continue
        code, labels = ref.assemble('\n'.join(f'{l}\nl{i}:' for i, l in enumerate(c['lines'])), labels=True)
        offsets = [0] + [labels[f'l{i}'] for i in range(len(c['lines']) - 1)]
        work = a.out / 'work' / c['name']; work.mkdir(parents=True, exist_ok=True)
        text, regs = description(c, code)
        (work / 'program.txt').write_text(text)
        run = subprocess.run([host, runtime, 'run-program', a.fexc, work / 'program.txt', work] + ([str(a.max_block)] if a.max_block else []) +
                             vm1.CODE_SHAPE, capture_output=True, text=True)
        (work / 'stdout').write_text(run.stdout)
        (work / 'stderr').write_text(run.stderr)
        problems = [f'driver: {run.stderr.strip()[-300:]}'] if run.returncode else judge(c, code, json.loads(run.stdout), offsets, regs)
        bad += bool(problems)
        results.append({'name': c['name'], 'insn': c['insn'], 'ok': not problems, 'problems': problems})
        if problems: print('FAIL', c['name'], '; '.join(problems)[:400])
    (a.out / 'results.json').write_text(json.dumps(results, indent=1) + '\n')
    print(f'{len(results)} cases, {len(results) - bad} as expected, {bad} not')
    sys.exit(1 if bad else 0)

if __name__ == '__main__': main()

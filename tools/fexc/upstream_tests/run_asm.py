#!/usr/bin/env python3
"""Run FEX's own 64-bit ASM suite (unittests/ASM/**/*.asm) through natemu's program driver (slice 2, S2f).

usage: run_asm.py --fexc FEXC --out DIR [--src .../unittests/ASM] [--only SUBSTR] [--limit N] [--jobs N] [--keep]
       run_asm.py --fexc FEXC --out DIR --selftest

Conventions, copied from FEX (CMakeLists.txt in unittests/ASM, Scripts/json_asm_config_parse.py,
Source/Tools/TestHarnessRunner, CommonTools/HarnessHelpers.h) and checked against them:
  * Build: nasm `-i Includes/` on "BITS 64\\n" + the file + "\\nret\\n" (the CMake sed edits; the ret is dead code after the
    test's hlt) -> flat binary, loaded at 0x10000 (Code_start_page); RIP starts there.
  * Initial state: every GPR 0 (RSP too; tests set their own stack), RFLAGS 0x202 (reserved + IF, as FEX's thread init).
  * Memory: FEX maps 0xe0000000 (10 pages) and 0xe7fff000 (2 pages) for every test, plus the JSON `MemoryRegions`
    (base: size), plus the code pages, all RWX; page size 4 KiB. natemu: `region` lines (eager, zero-filled, writable, NX)
    for the data ranges, and a `region` over the code pages so they are writable too (RWX as FEX; stores to translated
    pages invalidate or single-step). `MemoryData` bytes are decoded like parse_hexstring (each blank-
    separated token is a little-endian number) and loaded as `memory` lines.
  * End: the first executed hlt (FEX's SIGSEGV handler accepts a fault only when the byte at RIP is hlt). natemu: HLT at
    CPL 3 is #GP(0) (X2), delivered through the driver's logging IDT with the state banked at the hlt, RIP = the hlt.
  * Expected: only `RegData`. In FEX's JIT mode `Match`/`Ignore` have no effect (the second state is null), so they are
    ignored here too. A register value list compares the first value (GPR/RIP). FS/GS, XMMn and MMn need state natemu does
    not have (see below). `Mode` 32-bit is out of the slice. `HostFeatures` and `Env` are recorded, not applied (fexc runs
    with its own feature table; Env switches such as x87 reduced precision only matter for x87/SSE code).
Outcome per test:
  pass         ran to the hlt and every GPR/RIP expectation matches (vector expectations, if any, are separate: see below)
  unsupported  with a reason: an instruction outside the slice's allowlist (fexc unsupported_form / system_instruction at a
               RIP before the hlt, with the instruction's mnemonic), 32-bit mode,
               region beyond guest RAM, expectations on vector/x87/segment state natemu does not model (the GPR part had
               matched), ...
  fail         anything else: registers differ, a guest fault, the driver or nasm erred. Each is diagnosed by hand (see the
               report); none is silently ignored.
x87 (I6): natemu runs x87 in FEX's reduced-precision mode (doubles; owner direction). A test whose `Env` does not set
FEX_X87REDUCEDPRECISION expects FEX's 80-bit mode; when such a test runs to the hlt and every differing value is an x87
value that is exactly the 80-bit expectation rounded to 53 bits under some rounding mode (see `x87_value_ok`: MMn, or an
XMM register holding an 80-bit image), it is `precision deviation` (recorded in the plan's I6 deviations), never a pass; any
GPR, RIP or other difference fails. An FLDCW/FLDENV/FRSTOR that clears
an x87 exception mask stops `unsupported_system` (unmasked x87 exceptions, #MF, are not built): unsupported with that reason.
I5b: likewise an FXRSTOR/XRSTOR whose FCW or MXCSR clears a mask (#XM is not built either). Tests run with CR4.OSXSAVE and
XCR0 = 7, as Linux leaves user mode on this profile.
Known_Failures / Disabled_Tests (the jit lists and the ARMv8.4 class file, the M4's class) are reported as `listed`, separately;
the test is still run."""
import argparse, collections, concurrent.futures as cf, json, pathlib, re, shutil, subprocess, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'tools'))
import run_slice1_vm as vm1  # noqa: E402

CODE = 0x10000
PAGE = 4096
DEFAULT_REGIONS = [(0xe0000000, 10 * PAGE), (0xe8000000 - PAGE, 2 * PAGE)]
# RegData key -> index into the driver's GPR array (rax rcx rdx rbx rsp rbp rsi rdi r8..r15)
GPR = {'RAX': 0, 'RCX': 1, 'RDX': 2, 'RBX': 3, 'RSP': 4, 'RBP': 5, 'RSI': 6, 'RDI': 7, **{f'R{i}': i for i in range(8, 16)}}
# Tests whose expectations are FEX's Linux user-mode environment, not x86 semantics: FEX loads any selector unchecked,
# returns through IRETQ/far JMP without descriptor checks and answers SGDT with a fixed GDTR. natemu executes these
# instructions architecturally (plan S2b) against the driver's start-up descriptor tables (GDTR base 0, limit FFFFH), so the
# selectors fault or SGDT reads other values: counted unsupported with this reason.
NOT_ADVERTISED = {'rdpid', 'rdseed', 'clflushopt', 'clzero', 'clwb', 'serialize', 'rdpkru', 'wrpkru', 'monitorx', 'mwaitx', 'rdpru',
                  # I9: ADX and SHA (CPUID.7.0:EBX bits 19, 29 = 0)
                  'adcx', 'adox', 'sha1msg1', 'sha1msg2', 'sha1nexte', 'sha1rnds4', 'sha256msg1', 'sha256msg2', 'sha256rnds2',
                  # I10: delivered #UD (not refused): 3DNow! (CPUID.80000001H:EDX[31] = 0), VAES/VPCLMULQDQ at 256 bits (their VEX.128
                  # forms run: a #UD at these names is the VEX.L = 1 one), AVX-VNNI (objdump prints its VEX form as `{vex} vpdp...`)
                  'pi2fw', 'pi2fd', 'pf2iw', 'pf2id', 'pfrcpv', 'pfrsqrtv', 'pfnacc', 'pfpnacc', 'pfcmpge', 'pfmin', 'pfrcp', 'pfrsqrt',
                  'pfsub', 'pfadd', 'pfcmpgt', 'pfmax', 'pfrcpit1', 'pfrsqit1', 'pfsubr', 'pfacc', 'pfcmpeq', 'pfmul', 'pfrcpit2',
                  'pmulhrwa', 'pswapd', 'pavgusb', 'femms', 'vaesenc', 'vaesenclast', 'vaesdec', 'vaesdeclast', 'vpclmullqlqdq',
                  'vpclmulhqlqdq', 'vpclmullqhqdq', 'vpclmulhqhqdq', 'vpclmulqdq', '{vex}'}
# I9: tests whose expectation needs the 80-bit aliasing of MMX and x87 registers (an MMX write's bits 79:64 = FFFFH, an MMX read
# of an x87 register's 64-bit significand, FNSAVE tags of MMX values), which natemu does not build: MMn is the slot's low 64 bits
# and the reduced-precision x87 value is a double there (plan I9, MMX deviations); their MMX-only results match.
MMX_X87_ALIASING = {
    'X87/X87MMXInteraction.asm': "expects 80-bit MMX/x87 aliasing (bits 79:64 FFFFH after an MMX write, FNSAVE's tags of MMX values; "
                                 "plan I9 deviation) and FEX's tag word for zeros",
    'FEX_bugs/mmx_x87_register_conflating.asm': "x87 values from FLD m80 of random 80-bit data beyond the double's range through "
                                                "XSAVE/XRSTOR between MMX phases (I6 deviation 2; the MMX results match)",
}
# Tests whose expectation is FEX's own x87 behaviour where it is not x86's (the test says so): natemu gives the SDM's.
FEX_EXPECTS_ITS_BUG = {'X87/FXAM_Simple.asm': "expects FEX's FXAM of +0 (its comment: 'should be 0x4000 for zero, but there's no "
                                              "support'); natemu gives the SDM's 4000H (patch 0019)",
                      'X87/D9_F9.asm': "FYL2XP1 of 2.0, outside the SDM's range (|x| < 1 - sqrt(2)/2: 'undefined'); expects 15 * "
                                       "log2(3) to give 8.0 (FEX lists the test as disabled); natemu gives 23.77",
                      'VEX/vldmxcsr.asm': "expects FEX's VSTMXCSR, which masks off the exception flags (FFC0H); x86 stores MXCSR as "
                                          "VLDMXCSR loaded it, FFFFH (SDM STMXCSR; natemu's is the resident's transaction, I8 review)"}
NEEDS_OS = {'syscall'}   # #UD with EFER.SCE = 0: no OS has set up SYSCALL here
OS_SEGMENTS = {'Primary/Primary_8C.asm', 'Primary/Primary_8C_2.asm', 'Primary/Primary_CF.asm', 'PrimaryGroup/5_FF_05.asm',
               'PrimaryGroup/5_FF_05_03_o64.asm', 'PrimaryGroup/5_FF_05_03_o64_imm.asm', 'Secondary/07_XX_00.asm'}


# I6: x87 values against 80-bit expectations, as tight as the recorded deviations. A test's differences are a precision
# deviation only when every one is an 80-bit x87 value (MMn: the x87 register widened from its double; an XMM register whose
# high quadword holds only a sign and exponent: an 80-bit image FEX's tests load with MOVUPS) that is exactly the expectation
# rounded to a double's 53 bits under one of the four rounding modes (deviations 1-3: doubles for every value, FLD m80 and
# the constants); a NaN must agree in sign, quiet bit and the top 51 significand bits; an 80-bit denormal or pseudo-denormal
# expectation (exponent 0, a nonzero significand) never matches. The FEX precision tests run at FCW.PC = single, double or
# extended (PRECISION_PC_TESTS, deviations 1 and 2): there the expectation may also be the double rounded to 24 bits, or lie
# one 53-bit ulp from it (FLD m80 rounded the operand under the group's own RC). Any GPR, RIP or
# other difference fails, and so does every other XMM value (no doubles-in-XMM path).
X87_TEXT = re.compile(r'^\s*f(?!emms)[a-z0-9]+\b', re.M)
PRECISION_PC_TESTS = {f'X87/precision_test_{n}.asm' for n in ('fabs', 'fadd', 'fdiv', 'fdivr', 'fmul', 'fsqrt', 'fsub', 'fsubr',
                      'neg_fabs', 'neg_fadd', 'neg_fdiv', 'neg_fdivr', 'neg_fmul', 'neg_fsub', 'neg_fsubr')}
def f80(d):
    """A double widened to the 80-bit format: [significand, sign and exponent] (natemu_abi::x87::to_f80)."""
    sign, exp, frac = d >> 63, d >> 52 & 0x7ff, d & ((1 << 52) - 1)
    if exp == 0:
        if frac == 0: return 0, sign << 15
        lz = 64 - frac.bit_length()
        return frac << lz, sign << 15 | (15372 - lz)
    return (1 << 63 | frac << 11), sign << 15 | (0x7fff if exp == 0x7ff else exp + 15360)
def rounded80(m, se, bits, rc):
    """An 80-bit normal value rounded to `bits` significand bits under x87 rounding control `rc` (0 nearest, 1 down, 2 up,
    3 toward zero), in the 80-bit format; None for anything but a normal value."""
    e, sign = se & 0x7fff, se >> 15 & 1
    if e in (0, 0x7fff) or not m >> 63: return None
    drop = 64 - bits
    kept, rest, half = m >> drop, m & ((1 << drop) - 1), 1 << (drop - 1)
    up = {0: rest > half or (rest == half and kept & 1), 1: sign and rest, 2: not sign and rest, 3: False}[rc]
    kept += bool(up)
    if kept >> bits: kept, e = kept >> 1, e + 1
    return kept << drop, sign << 15 | e
# I6b: the helper forms on an operand FLD m80 rounded to a double (deviation 2). The transcendental ones within 53-bit ulps
# of the 80-bit expectation (libm's result within an ulp, the operand's rounding another: 2; FPTAN's 3, its slope
# sec^2(x) = 3.6 at the test's operand growing the operand's error). The remainders are exact, so their result must be
# exactly the remainder of the operands as FLD m80 rounded them under one of the four rounding controls (computed below
# from the test's operands: dividend, divisor, FPREM1's round-to-nearest quotient).
HELPER_PRECISION = {**{f'X87/precision_test_{n}{f}.asm': 2 for n in ('', 'neg_') for f in ('fsin', 'fyl2x', 'fyl2xp1')},
                    **{f'X87/precision_test_{n}ftan.asm': 3 for n in ('', 'neg_')}}
def _log2(v):
    """floor(log2(v)) of a positive Fraction."""
    from fractions import Fraction
    e = v.numerator.bit_length() - v.denominator.bit_length()
    return e - 1 if Fraction(2) ** e > v else e
def _f80_of(text):
    """The 80-bit (significand, sign|exponent) nasm's `dt TEXT` assembles (round to nearest, ties to even)."""
    from fractions import Fraction
    v = Fraction(text); sign = v < 0; v = abs(v)
    e = _log2(v)
    q, r = divmod(v * Fraction(2) ** (63 - e), 1)
    q += r > Fraction(1, 2) or (r == Fraction(1, 2) and q & 1)
    if q >> 64: q, e = q >> 1, e + 1
    return int(q), sign << 15 | (e + 16383)
HELPER_REMAINDERS = {**{f'X87/precision_test_{n}{f}.asm': ((0x8222222222222222, se), (1 << 63, 0x3fff), f == 'fprem1')
                        for n, se in (('', 0x3fff), ('neg_', 0xbfff)) for f in ('fprem', 'fprem1')},
                     'X87/D9_F5.asm': (_f80_of('5.1'), _f80_of('3.0'), True)}
def remainder_ok(got, name):
    """`got` is exactly FPREM/FPREM1 of the test's operands rounded to doubles under some rounding control."""
    from fractions import Fraction
    val = lambda m, se: (-1) ** (se >> 15) * Fraction(m) * Fraction(2) ** ((se & 0x7fff) - 16383 - 63)
    (xm, xse), (ym, yse), nearest = HELPER_REMAINDERS[name]
    for rc in range(4):
        x, y = val(*rounded80(xm, xse, 53, rc)), val(*rounded80(ym, yse, 53, rc))
        q = x / y
        n = int(q) if not nearest else round(q)   # Python's round: to nearest, ties to even
        r = x - n * y
        if r == 0: want = (0, xse & 0x8000)
        else:
            e = _log2(abs(r))
            m = abs(r) * Fraction(2) ** (63 - e)
            if m.denominator != 1: continue
            want = (int(m), (r < 0) << 15 | (e + 16383))
        if tuple(got) == want: return True
    return False
def x87_value_ok(got, want, name):
    """`got` (an 80-bit image our double widens to) against the 80-bit expectation `want` (both [significand, sign|exponent])."""
    gm, gse, wm, wse = got[0], got[1] & 0xffff, want[0], want[1] & 0xffff
    if wse & 0x7fff == 0 and wm: return False                         # an 80-bit denormal expectation: never
    if wse & 0x7fff == 0x7fff and wm << 1 & (2**64 - 1):              # a NaN: sign, quiet bit, top 51 significand bits
        return gse == wse and gm >> 11 == wm >> 11
    if any(rounded80(wm, wse, 53, rc) == (gm, gse) for rc in range(4)): return True
    if wse & 0x7fff not in (0, 0x7fff) and not wm >> 63:             # an unnormal: FLD m80 loads the indefinite (deviation 2)
        return (gm, gse) == (0xc000000000000000, 0xffff)
    e = (wse & 0x7fff) - 16383
    if wse & 0x7fff not in (0, 0x7fff) and not -1075 <= e <= 1023:   # beyond the double's range: FLD m80 rounds it to a double
        sign = wse & 0x8000                                            # by some RC (deviation 2): an infinity, the largest finite,
        big = [(1 << 63, sign | 0x7fff), (0xfffffffffffff800, sign | 0x43fe)]   # a zero or the smallest denormal
        small = [(0, sign), (1 << 63, sign | 0x3bcd)]
        return (gm, gse) in (big if e > 0 else small)
    if wse & 0x7fff not in (0, 0x7fff) and e < -1022:                 # a double denormal: fewer significand bits
        return any(rounded80(wm, wse, 53 - (-1022 - e), rc) == (gm, gse) for rc in range(4))
    if name in HELPER_REMAINDERS: return remainder_ok((gm, gse), name)
    if name in HELPER_PRECISION:
        return gse == wse and gm >> 63 and wm >> 63 and abs(gm - wm) <= HELPER_PRECISION[name] << 11
    if name not in PRECISION_PC_TESTS: return False
    # FCW.PC = single: the expectation is our double rounded to 24 bits. PC = double or extended: the operand was rounded by FLD m80
    # under the group's RC before the operation (deviation 2), so the result may be one 53-bit ulp off the x87 result.
    if any(rounded80(gm, gse, 24, rc) == (wm, wse) for rc in range(4)): return True
    return gse == wse and gm >> 63 and wm >> 63 and abs(gm - wm) <= 1 << 11
def image80(x):
    """An XMM pair [low, high] holding an 80-bit image (high: sign and exponent only, a nonzero exponent), or None."""
    return x if x[1] <= 0xffff and x[1] & 0x7fff else None
def header(text):
    parts = text.split('%ifdef CONFIG')
    if len(parts) < 2: return {}
    return {k.upper(): v for k, v in json.loads(parts[1].split('%endif')[0].strip()).items()}


def memory_data(s):
    """Scripts/json_config_parse.py parse_hexstring, verbatim (bug-compatible)."""
    out = []
    for num in s.split(' '):
        if s.startswith('0x'): num = num[2:]
        while num:
            out.append(int(num[-2:], 16)); num = num[:-2]
    return bytes(out)


def lists(src):
    def read(*names):
        r = set()
        for n in names:
            f = src / n
            if f.exists(): r |= {l.strip() for l in f.read_text().splitlines() if l.strip() and not l.startswith('#')}
        r |= {x.removeprefix('Test_') for x in r}
        return r
    return read('Known_Failures', 'Known_Failures_jit'), read('Disabled_Tests', 'Disabled_Tests_ARMv8.4')


def description(h, binary):
    lines = [f'rip {CODE:#x}', 'rflags 0x202', 'gpr ' + ' '.join(['0'] * 16), f'code {binary.hex()}', 'sse 1', 'fsgsbase 1', 'xcr0 7']   # I5a: CR4.OSFXSR, S3: CR4.FSGSBASE, I5b: CR4.OSXSAVE and XCR0 7, as Linux
    regions = DEFAULT_REGIONS + [(int(a, 0), int(n, 0)) for a, n in h.get('MEMORYREGIONS', {}).items()]
    regions.append((CODE, -(-len(binary) // PAGE) * PAGE))   # FEX maps the code RWX
    lines += [f'region {a:#x} {n:#x}' for a, n in regions]
    lines += [f'memory {int(a, 0):#x} {memory_data(d).hex()}' for a, d in h.get('MEMORYDATA', {}).items()]
    return '\n'.join(lines) + '\n'


PREFIXES = ('lock', 'rep', 'repe', 'repne', 'repz', 'repnz', 'a32', 'o16', 'o32', 'o64', 'a16', 'xacquire', 'xrelease')


def mnemonic(binary, address):
    r = subprocess.run(['ndisasm', '-b', '64', '-o', hex(CODE), '-e', str(address - CODE), '/dev/stdin'], input=binary, capture_output=True)
    first = r.stdout.decode(errors='replace').splitlines()[:1]
    words = first[0].split()[2:] if first else []
    return ' '.join(words[:2] if words and words[0] in PREFIXES else words[:1]) or '?'


def one(host, runtime, fexc, src, out, path, known, disabled, keep, max_block=None):
    name = str(path.relative_to(src))
    work = out / 'work' / name.replace('/', '__')
    work.mkdir(parents=True, exist_ok=True)
    rec = {'test': name, 'known_failure': name in known, 'disabled': name in disabled}
    try:
        text = path.read_text()
        h = header(text)
        if h.get('MODE', '64BIT').upper() != '64BIT':
            return {**rec, 'outcome': 'unsupported', 'reason': '32-bit code'}
        if name in OS_SEGMENTS:
            return {**rec, 'outcome': 'unsupported', 'reason': "needs FEX's Linux user-mode descriptor tables"}
        rec['env'] = h.get('ENV')
        rec['host_features'] = h.get('HOSTFEATURES')
        (work / 't.asm').write_text('BITS 64\n' + text + '\nret\n')
        n = subprocess.run(['nasm', '-i', str(src / 'Includes') + '/', work / 't.asm', '-o', work / 't.bin'], capture_output=True, text=True)
        if n.returncode:
            return {**rec, 'outcome': 'fail', 'reason': 'harness: nasm failed', 'detail': n.stderr[-500:]}
        binary = (work / 't.bin').read_bytes()
        want = {k.upper(): [int(v, 0) for v in (vals if isinstance(vals, list) else [vals])] for k, vals in h.get('REGDATA', {}).items()}
        # Expectations natemu cannot check: XMM16+ (AVX-512). MMn is x87/MMX (I6); XMM0..15 with four values a YMM register
        # (I8: the upper half from the report's `ymmh`).
        vector = sorted(k for k in want if k not in GPR and k != 'RIP' and not (re.fullmatch(r'XMM\d+', k) and int(k[3:]) < 16)
                        and not re.fullmatch(r'MM[0-7]', k))
        (work / 'program.txt').write_text(description(h, binary))
        run = subprocess.run([host, runtime, 'run-program', fexc, work / 'program.txt', work] + ([str(max_block)] if max_block else []) + vm1.CODE_SHAPE, capture_output=True, text=True, timeout=120)
        if run.returncode:
            err = run.stderr.strip()[-300:]
            if 'guest RAM exhausted' in err: return {**rec, 'outcome': 'unsupported', 'reason': 'region beyond 4 MiB guest RAM'}
            if 'object refused: code layout' in err: return {**rec, 'outcome': 'unsupported', 'reason': 'block over the 32 KiB code-block limit (admission: code layout)'}
            return {**rec, 'outcome': 'fail', 'reason': 'harness: driver error', 'detail': err}
        s = json.loads(run.stdout)
        rip = int(s['rip'], 0)
        # X2: HLT is the resident's transaction, and these tests run at CPL 3, where HLT is #GP(0) with RIP at it and the
        # state banked there (delivered through the driver's logging IDT): that is reaching the hlt. (Before X2: no code.)
        hlt_gp = s['stop'] == 'guest_fault' and [int(x, 0) for x in s['fault']] == [13, 0, 0, rip]
        at_hlt = (hlt_gp or (s['stop'] == 'no_code' and s['reason'] == 'system_instruction')) and CODE <= rip < CODE + len(binary) and binary[rip - CODE] == 0xF4
        if not at_hlt:
            rec['stop'] = f"{s['stop']} {s['reason']} rip={rip:#x}"
            if CODE <= rip < CODE + len(binary): rec['at'] = mnemonic(binary, rip); rec['bytes'] = binary[rip - CODE:rip - CODE + 8].hex()
            if s['stop'] == 'no_code':
                # fexc's end reason; the instruction is the one at RIP
                return {**rec, 'outcome': 'unsupported', 'reason': f"{s['reason']}: {rec.get('at', '?')}"}
            if s['stop'] == 'resident' and s['reason'].startswith('unsupported_atomic'):
                # A memory xchg whose operand crosses 16 bytes (a split lock): stopped before any effect (S3e, boot phase)
                return {**rec, 'outcome': 'unsupported', 'reason': f"split lock: {rec.get('at', '?')}"}
            at = lambda a, n: binary[a - CODE:a - CODE + n].hex() if CODE <= a and a + n <= CODE + len(binary) else ''
            vector = int(s['fault'][0], 0) if s['stop'] == 'guest_fault' else None
            if (vector == 3 and at(rip - 1, 1) == 'cc') or (vector is not None and at(rip - 2, 2) == f'cd{vector:02x}') \
                    or (vector == 6 and (at(rip, 1) == 'ce' or at(rip, 2) in ('0f0b', '0fb9', '0fff'))):
                # X1a: INT3/INT n/INTO/UD0-2 are delivered through the driver's logging IDT; these tests expect Linux's
                # signal for them (before X1a: no code, `system_instruction`).
                return {**rec, 'outcome': 'unsupported', 'reason': f"software interrupt or #UD instruction delivered (vector {int(s['fault'][0], 0)}): {rec.get('at', '?')}"}
            if vector == 6 and (rec.get('at') or '').split()[-1:] and (rec.get('at') or '').split()[-1] in NOT_ADVERTISED:
                # S3: #UD for an instruction the presented CPU (tools/models/cpu_profile.json) does not advertise, as the SDM
                # gives; FEX's tests assume its host's features.
                return {**rec, 'outcome': 'unsupported', 'reason': f"#UD: {rec['at']} not advertised by the CPU profile"}
            if vector == 6 and rec.get('at') in NEEDS_OS:
                return {**rec, 'outcome': 'unsupported', 'reason': f"#UD: {rec['at']} with EFER.SCE = 0 (needs an OS)"}
            if s['stop'] == 'resident' and s['reason'].startswith('unsupported_system') and rec.get('at') in ('fldcw', 'fldenv', 'frstor'):
                return {**rec, 'outcome': 'unsupported', 'reason': f"x87 FCW unmasking an exception (refused: #MF not built): {rec['at']}"}
            if s['stop'] == 'resident' and s['reason'].startswith('unsupported_system') and rec.get('at') in ('fxrstor', 'fxrstor64', 'xrstor', 'xrstor64'):
                return {**rec, 'outcome': 'unsupported', 'reason': f"FCW or MXCSR unmasking an exception (refused: #MF/#XM not built): {rec['at']}"}
            return {**rec, 'outcome': 'fail', 'reason': 'did not reach hlt', 'detail': rec['stop'] + ' fault=' + json.dumps(s['fault'])}
        gpr = [int(v, 0) for v in s['gpr']]
        xmm = [int(v, 0) for v in s['xmm']]
        ymmh = [int(v, 0) for v in s['ymmh']]
        reduced = 'FEX_X87REDUCEDPRECISION' in (h.get('ENV') or {})
        x87 = X87_TEXT.search(text) is not None
        mm = [int(v, 0) for v in s['x87']['mm']]
        diffs, soft = [], []   # soft: an x87 floating-point value within the reduced-precision bound of an 80-bit expectation
        for k, vals in want.items():
            if k == 'RIP': got = rip
            elif k in GPR: got = gpr[GPR[k]]
            elif re.fullmatch(r'XMM\d+', k) and int(k[3:]) < 16:   # I5a: XMM (low, high); I8: YMM (four quadwords)
                n = int(k[3:])
                got4 = xmm[2 * n:2 * n + 2] + ymmh[2 * n:2 * n + 2]
                if got4[:len(vals)] != vals[:4]:
                    d = f'{k}: got {" ".join(f"{v:#x}" for v in got4[:len(vals)])}, want {" ".join(f"{v:#x}" for v in vals)}'
                    if len(vals) > 2 and xmm[2 * n:2 * n + 2] == vals[:2]:
                        diffs.append(d)
                        continue
                    # (the image we stored may be a zero: an 80-bit value below the double's range, deviation 2)
                    g80, w80 = xmm[2 * n:2 * n + 2] if xmm[2 * n + 1] <= 0xffff else None, image80(vals[:2])
                    (soft if x87 and not reduced and g80 and w80 and x87_value_ok(g80, w80, name) else diffs).append(d)
                continue
            elif re.fullmatch(r'MM[0-7]', k):   # I6: x87 register n (physical), 80-bit, or a double under reduced precision
                g = mm[int(k[2])]
                if len(vals) == 1:   # I9: an MMX register's 64-bit value: the slot's raw low half (FEX's MMn), exactly
                    if g != vals[0]: diffs.append(f'{k}: got {g:#x}, want {vals[0]:#x}')
                    continue
                got2 = [g, 0] if reduced else list(f80(g))
                if got2 != vals[:2]:
                    d = f'{k}: got {got2[0]:#x} {got2[1]:#x}, want {vals[0]:#x} {vals[1]:#x}'
                    (soft if not reduced and x87_value_ok(got2, vals[:2], name) else diffs).append(d)
                continue
            else: continue
            if got != vals[0]: diffs.append(f'{k}: got {got:#x}, want {vals[0]:#x}')
        if (diffs or soft) and name in MMX_X87_ALIASING:
            return {**rec, 'outcome': 'unsupported', 'reason': MMX_X87_ALIASING[name], 'detail': '; '.join(diffs + soft)}
        if (diffs or soft) and name in FEX_EXPECTS_ITS_BUG:
            return {**rec, 'outcome': 'unsupported', 'reason': FEX_EXPECTS_ITS_BUG[name], 'detail': '; '.join(diffs + soft)}
        if diffs: return {**rec, 'outcome': 'fail', 'reason': 'registers differ', 'detail': '; '.join(diffs + soft), 'stats': s['stats']}
        if soft:
            return {**rec, 'outcome': 'precision deviation', 'reason': '80-bit expectation, our double is it rounded to 53 bits (plan I6)', 'detail': '; '.join(soft)}
        if 'FS' in want or 'GS' in want:
            return {**rec, 'outcome': 'unsupported', 'reason': 'needs segment base state (GPR part matched)'}
        if vector:
            kinds = sorted({'x87/MMX' if k.startswith('MM') else 'YMM' if k.startswith('XMM') else k for k in vector})
            return {**rec, 'outcome': 'unsupported', 'reason': f"needs vector state: {'+'.join(kinds)} (GPR part matched)"}
        return {**rec, 'outcome': 'pass', 'expectations': len(want), 'stats': s['stats']}
    except Exception as e:
        return {**rec, 'outcome': 'fail', 'reason': 'harness: exception', 'detail': repr(e)}
    finally:
        if not keep: shutil.rmtree(work, ignore_errors=True)


def category(reason):
    """Coarse family of an unsupported reason, for the summary."""
    if not reason.startswith(('unsupported_form', 'system_instruction')): return reason.split(' (')[0]
    kind, text = reason.split(': ', 1)
    if kind == 'system_instruction': return 'system instruction (cpuid, segment/cr moves, pushf/popf, rdtsc, ...)'
    w = text.split()
    pre = w[0] if w[0] in PREFIXES else None
    base = w[1] if pre and len(w) > 1 else w[0]
    if base.startswith('v'): return 'AVX (VEX-encoded)'
    if base == 'db': return 'x87: undocumented alias or reserved encoding'
    if base in ('f2xm1', 'fyl2x', 'fyl2xp1', 'fptan', 'fpatan', 'fsin', 'fcos', 'fsincos', 'fprem', 'fprem1', 'fscale'):
        return 'x87: transcendental, remainder or scale (FEX host libm helper)'
    if base in ('fbld', 'fbstp'): return 'x87: BCD (FEX host helper)'
    if base.startswith('f') and base != 'femms': return 'x87: ' + base
    if re.match(r'^(movap[sd]|movup[sd]|movdq[au]|movntdqa|movss|movsd|movq|movd|p[a-z0-9]+|cvt.*|round.*|aes.*|sha.*|lddqu|[ls]dmxcsr|emms|femms|pf.*|pi2f.*)$', base):
        return 'MMX/SSE/3DNow/AES/SHA vector'
    if pre == 'lock' or base in ('xchg', 'cmpxchg', 'xadd', 'cmpxchg8b', 'cmpxchg16b'): return 'atomics: lock prefix, xchg/cmpxchg/xadd on memory'
    if base in ('cld', 'std', 'stc', 'clc', 'lahf', 'sahf'): return 'flag moves: cld std stc clc lahf sahf'
    if base in ('shld', 'shrd'): return 'shld/shrd'
    if base in ('popcnt', 'lzcnt', 'tzcnt', 'crc32', 'movbe', 'adox', 'blsr', 'blsi', 'blsmsk', 'bextr', 'bzhi', 'pdep', 'pext', 'mulx', 'rorx', 'sarx', 'shlx', 'shrx', 'andn', 'rdrand', 'rdseed', 'rdpid', 'clwb'): return 'BMI/ABM/other ISA extensions'
    return 'other: ' + text


SELFTEST = {   # synthetic tests: (source, expected outcome, reason prefix)
    'pass.asm': ('%ifdef CONFIG\n{"RegData":{"RAX":"0x1","RSP":"0xE0000020","RIP":"0x1000a"}}\n%endif\nmov rsp, 0xe0000020\nmov eax, 1\nhlt\n', 'pass', ''),
    'wrong.asm': ('%ifdef CONFIG\n{"RegData":{"RAX":"0x2"}}\n%endif\nmov eax, 1\nhlt\n', 'fail', 'registers differ'),
    'memory.asm': ('%ifdef CONFIG\n{"RegData":{"RAX":"0x0807060504030201","RBX":"0x1234"},"MemoryRegions":{"0x100000000":"4096"},"MemoryData":{"0x100000000":"0102030405060708"}}\n%endif\n'
                   'mov rcx, 0x100000000\nmov rax, [rcx]\nbswap rax\nmov word [rcx+8], 0x1234\nmovzx ebx, word [rcx+8]\nhlt\n', 'pass', ''),
    'vector.asm': ('%ifdef CONFIG\n{"RegData":{"XMM16":["0","0"],"RAX":"0"}}\n%endif\nxor eax,eax\nhlt\n', 'unsupported', 'needs vector state'),
    'mm.asm': ('%ifdef CONFIG\n{"RegData":{"MM7":["0x8000000000000000","0x3fff"]}}\n%endif\nfld1\nhlt\n', 'pass', ''),
    'mm_close.asm': ('%ifdef CONFIG\n{"RegData":{"MM7":["0x8000000000000001","0x3fff"]}}\n%endif\nfld1\nhlt\n', 'precision deviation', '80-bit'),
    # Must-fail holes (I6 re-review): an integer in XMM read as an 80-bit denormal, a 64-bit integer off by one, a double 2^28
    # ulps wrong, MM7 = 1 + 2^-24 against 1.0, a NaN with another payload, an 80-bit denormal expectation.
    'hole_int_xmm.asm': ('%ifdef CONFIG\n{"RegData":{"XMM0":["0x2","0x0"]}}\n%endif\nfld1\nmov eax, 1\nmovd xmm0, eax\nhlt\n', 'fail', 'registers differ'),
    'hole_i64_xmm.asm': ('%ifdef CONFIG\n{"RegData":{"XMM0":["0x100000000","0x0"]}}\n%endif\nfld1\nmov rax, 0xffffffff\nmovq xmm0, rax\nhlt\n', 'fail', 'registers differ'),
    'hole_dbl_far.asm': ('%ifdef CONFIG\n{"RegData":{"XMM0":["0x3ff0000010000000","0x0"]}}\n%endif\nfld1\nfstp qword [rel v]\nmovq xmm0, [rel v]\nhlt\nv: dq 0\n', 'fail', 'registers differ'),
    'hole_mm_far.asm': ('%ifdef CONFIG\n{"RegData":{"MM7":["0x8000008000000000","0x3fff"]}}\n%endif\nfld1\nhlt\n', 'fail', 'registers differ'),
    'hole_nan_payload.asm': ('%ifdef CONFIG\n{"RegData":{"MM7":["0xc000000000100000","0x7fff"]}}\n%endif\nfld qword [rel v]\nhlt\nv: dq 0x7ff8000000000001\n', 'fail', 'registers differ'),
    'hole_denormal_want.asm': ('%ifdef CONFIG\n{"RegData":{"MM7":["0x1","0x0"]}}\n%endif\nfldz\nhlt\n', 'fail', 'registers differ'),
    'nan_ok.asm': ('%ifdef CONFIG\n{"RegData":{"MM7":["0xc000000000000a00","0x7fff"]}}\n%endif\nfld qword [rel v]\nhlt\nv: dq 0x7ff8000000000001\n', 'precision deviation', '80-bit'),
    # I9: an 80-bit value beyond the double's range loads as the infinity (deviation 2), but 1.0 never matches it.
    'range_ok.asm': ('%ifdef CONFIG\n{"RegData":{"MM7":["0x8000000000000000","0x478f"]}}\n%endif\nfld tword [rel v]\nhlt\nv: dq 0x8000000000000000\ndw 0x478f\n', 'precision deviation', '80-bit'),
    'hole_range.asm': ('%ifdef CONFIG\n{"RegData":{"MM7":["0x8000000000000000","0x478f"]}}\n%endif\nfld1\nhlt\n', 'fail', 'registers differ'),
    'mm_far.asm': ('%ifdef CONFIG\n{"RegData":{"MM7":["0x8000000000000000","0x4000"]}}\n%endif\nfld1\nhlt\n', 'fail', 'registers differ'),
    'vector_insn.asm': ('%ifdef CONFIG\n{"RegData":{"RAX":"0"}}\n%endif\nxor eax,eax\nmaskmovq mm0, mm1\nhlt\n', 'unsupported', 'unsupported_form'),
    'xmm.asm': ('%ifdef CONFIG\n{"RegData":{"XMM0":["0x1","0x0"],"XMM1":["0x0","0x0","0x0","0x0"]}}\n%endif\nmov eax, 1\nmovd xmm0, eax\nhlt\n', 'pass', ''),
    'xmm_wrong.asm': ('%ifdef CONFIG\n{"RegData":{"XMM0":["0x2","0x0"]}}\n%endif\nmov eax, 1\nmovd xmm0, eax\nhlt\n', 'fail', 'registers differ'),
    'bits32.asm': ('%ifdef CONFIG\n{"Mode":"32BIT","RegData":{"RAX":"0"}}\n%endif\nhlt\n', 'unsupported', '32-bit'),
    'fault.asm': ('%ifdef CONFIG\n{"RegData":{"RAX":"0"}}\n%endif\nmov rax, [0x70000000]\nhlt\n', 'fail', 'did not reach hlt'),
    'huge.asm': ('%ifdef CONFIG\n{"RegData":{"RAX":"0"},"MemoryRegions":{"0x500000000":"0x100000000"}}\n%endif\nhlt\n', 'unsupported', 'region beyond'),
}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--src', type=pathlib.Path, default=ROOT / 'third_party/FEX/unittests/ASM')
    ap.add_argument('--fexc', type=pathlib.Path, required=True)
    ap.add_argument('--out', type=pathlib.Path, required=True)
    ap.add_argument('--only')
    ap.add_argument('--limit', type=int)
    ap.add_argument('--jobs', type=int, default=6)
    ap.add_argument('--max-block', type=int, help='block size limit in guest instructions (upstream jit_1 = 1, jit_500 = 500)')
    ap.add_argument('--no-build', action='store_true', help='use the binaries already in target/')
    ap.add_argument('--keep', action='store_true', help='keep per-test work dirs')
    ap.add_argument('--selftest', action='store_true')
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    if a.selftest:
        a.src = pathlib.Path(tempfile.mkdtemp(dir=a.out))
        (a.src / 'Includes').mkdir()
        for n, (t, _, _) in SELFTEST.items(): (a.src / n).write_text(t)
    if a.no_build: host, runtime = ROOT / 'target/release/natemu-host', ROOT / 'target/aarch64-unknown-none/release/natemu-runtime'
    else: host, runtime = vm1.build()
    known, disabled = lists(a.src)
    tests = sorted(p for p in a.src.rglob('*.asm') if 'Includes' not in p.parts)
    if a.only: tests = [t for t in tests if a.only in str(t.relative_to(a.src))]
    if a.limit: tests = tests[:a.limit]
    with cf.ThreadPoolExecutor(a.jobs) as ex:
        results = list(ex.map(lambda p: one(host, runtime, a.fexc, a.src, a.out, p, known, disabled, a.keep, a.max_block), tests))
    (a.out / 'results.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in results))
    if a.selftest:
        bad = [(r['test'], r['outcome'], r.get('reason'), SELFTEST[r['test']][1:]) for r in results
               if (r['outcome'], (r.get('reason') or '')[:len(SELFTEST[r['test']][2])]) != SELFTEST[r['test']][1:]]
        print('selftest', 'FAILED' if bad else 'ok', bad or [(r['test'], r['outcome'], r.get('reason')) for r in results])
        sys.exit(1 if bad else 0)
    c = collections.Counter(r['outcome'] for r in results)
    print(f'{len(results)} tests: ' + ', '.join(f'{k} {v}' for k, v in sorted(c.items())))
    by = collections.Counter((r['outcome'], category(r['reason']) if r['outcome'] == 'unsupported' else r.get('reason', '')) for r in results if r['outcome'] != 'pass')
    for (o, why), n in by.most_common(): print(f'  {o:12} {n:5}  {why}')
    for flag in ('known_failure', 'disabled'):
        sel = [r for r in results if r[flag]]
        print(f'{flag}: ' + ', '.join(f"{r['test']}={r['outcome']}" for r in sel))
    for r in results:
        if r['outcome'] == 'fail': print('FAIL', r['test'], r.get('reason'), r.get('detail', ''))


if __name__ == '__main__': main()

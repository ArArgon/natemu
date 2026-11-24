#!/usr/bin/env python3
"""The EL0 sampler report's word roles on fexc's output (docs/restructure/el0-sampler-plan.md chunk 1): an idiom corpus, in
64-bit and 32-bit forms, compiled with the host's own options (crates/host/src/execution.rs body_with: --profile slice1,
TRANSLATION_ARGS' --ext-conds 4, --push-run, --narrow-cmp, the `--feature NAME=0` overrides of every feature this Mac's vCPU
lacks (admission.rs feature_overrides; afp=0 on M1 and M2), --bitness 32, an x87 guess as x87_args gives a fresh process;
the kernel's --cpl0 --tsc --bulk), each object classified by tests/bench/sample_roles.py. Three checks per idiom:
- HAND: words derived from the idiom itself (the guest-code review's anatomy and fexc's fixed register map: RAX x4, RCX x7,
  RDX x5, RBX x6, RSP x8, RDI x11, the PF and AF registers x26 and x27, the temporary x20), each with the role it must
  have: these fail when the classifier errs.
- the block's whole role sequence (run-length, in word order), as reviewed: this fails when fexc's output shape changes,
  with each word's encoding, so the classifier is revised with it.
- the guest-instruction tests the report uses (REP string bodies, VEX.256). No word may be `unknown`.

    check_sample_roles.py FEXC [--print]    (--print: the sequences as compiled, to review when revising the corpus)
"""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402
import subprocess
import tempfile

import sample_roles as R

HOST = ['--profile', 'slice1', '--ext-conds', '4', '--push-run', '--narrow-cmp']
# admission.rs FEATURES entries detected from the vCPU's ID registers, by the sysctl hw.optional.arm keys that say the
# same on macOS (Unverifiable entries are always off, Nothing and Tso always on: admission.rs detected()).
SYSCTL = {'atomics_lse': ['FEAT_LSE'], 'lrcpc': ['FEAT_LRCPC'], 'lrcpc2': ['FEAT_LRCPC2'], 'sve128': ['FEAT_SVE'],
          'flagm': ['FEAT_FlagM'], 'flagm2': ['FEAT_FlagM2'], 'fcma': ['FEAT_FCMA'], 'crc32': ['FEAT_CRC32'],
          'aes': ['FEAT_AES'], 'sha': ['FEAT_SHA1', 'FEAT_SHA256'], 'pmull128': ['FEAT_PMULL'], 'rng': ['FEAT_RNG'],
          'afp': ['FEAT_AFP'], 'frintts': ['FEAT_FRINTTS'], 'i8mm': ['FEAT_I8MM'], 'dotprod': ['FEAT_DotProd'],
          'ecv': None}
# None: off whatever macOS says. HVF's vCPU hides ECV: an M2 Max's served objects (io5-solo32-sample) carry feature bits
# without it where sysctl has FEAT_ECV (rdtsc's counter word differs; HAND accepts both).


def feature_overrides():
    """The host's `--feature NAME=0` arguments on this Mac: admission.rs FEATURES, in order, read from the source."""
    import re
    src = (testenv.ROOT / 'crates/host/src/admission.rs').read_text()
    table = src[src.index('pub(crate) const FEATURES'):]
    table = table[:table.index('\n];')]
    entries = re.findall(r'feature\("(\w+)",\s*(\w+)', table)
    have = {}
    for line in subprocess.run(['sysctl', 'hw.optional.arm'], capture_output=True, text=True).stdout.splitlines():
        k, _, v = line.partition(':')
        if k.startswith('hw.optional.arm.'): have[k[len('hw.optional.arm.'):]] = v.strip() == '1'
    out = []
    for name, detect in entries:
        if detect in ('Nothing', 'Tso'): continue
        if detect == 'Unverifiable': on = False
        elif name in SYSCTL: on = SYSCTL[name] is not None and all(have.get(k, False) for k in SYSCTL[name])
        else: raise SystemExit(f'admission.rs feature {name} ({detect}) has no sysctl key here: add it to SYSCTL')
        if not on: out += ['--feature', f'{name}=0']
    return out
KERNEL = ['--cpl0', '--tsc', '--bulk']
U64, K64 = 0x40_1000, 0xffff_ffff_8100_1000

# name: (bitness, extra options, RIP, guest bytes, the roles the idiom must show, (unused: WANT holds the sequences),
# guest tests). A sequence is `role` or `role*N` per run, in word order from word 0 (a PIC full entry's word) to the code
# region's end.
CORPUS = {
    'push rbx; ret': (64, [], U64, '53c3', {'stack_check', 'guard_check', 'exit_computed'},
                      None, {}),
    'mov [rdi+8],rax; mov [rdi],ecx; jmp': (64, [], U64, '48894708890feb00', {'split_check', 'split_stub', 'exit_literal'},
                                            None, {}),
    'mov rax,[rdi+8]; ret': (64, [], U64, '488b4708c3', {'load_move'},
                             None, {}),
    'call rel32': (64, [], U64, 'e800000000', {'fex_const', 'stack_check', 'exit_literal'},
                   None, {}),
    'add dword [rdi+8],1; dec rcx; jnz': (64, [], U64, '8347080148ffc975f7', {'nzcv_save', 'split_check', 'flags'},
                                          None, {}),
    'cmp rax,rbx; sete al; ret': (64, [], U64, '4839d80f94c0c3', {'flags', 'fex_alu'}, None, {}),
    'movaps xmm0,[rdi]; lock add [rdi],eax; ret': (64, [], U64, '0f2807f00107c3', {'align_check', 'load_move', 'flags'},
                                                   None, {}),
    'rep movsb': (64, [], U64, 'f3a4', {'fex_mem'},
                  None, {0: 'movs'}),
    'rep stosq': (64, [], U64, 'f348ab', {'split_check'},
                  None, {0: 'stos'}),
    'div rcx; ret': (64, [], U64, '48f7f1c3', {'divide_check', 'helper'},
                     None, {}),
    'vpcmpeqb ymm1,ymm0,[rdi]; vpmovmskb eax,ymm1; ret': (64, [], U64, 'c5fd740fc5fdd7c1c3', {'fex_state', 'fex_simd'},
                                                          None, {0: 'vex256', 4: 'vex256'}),
    'pop rbx; jmp rax': (64, [], U64, '5bffe0', {'load_move', 'guard_check', 'exit_computed'},
                         None, {}),
    'cmp al,0x37; sete cl; ret': (64, [], U64, '3c370f94c1c3', {'flags'},
                                  None, {}),
    'mov rax,fs:[0x28]; ret': (64, [], U64, '64488b042528000000c3', {'fex_state', 'load_move'},
                               None,
                               {}),
    'movsd xmm0,[rdi]; mulsd; movsd [rdi],xmm0; ret': (64, [], U64, 'f20f1007f20f59c1f20f1107c3', {'load_move', 'split_check'},
                                                       None, {}),
    'kernel: rdtsc; ret': (64, KERNEL, K64, '0f31c3', {'fex_other'},
                           None, {}),
    'kernel: rep stosb (bulk)': (64, KERNEL, K64, 'f3aa', {'fex_mem'},
                                 None,
                                 {0: 'stos'}),
    '32: push ebx; call +0; ret': (32, [], U64, '53e800000000c3', {'stack_check', 'fex_const', 'guard_check'},
                                   None, {}),
    '32: add dword [ebx+8],1; dec ecx; jnz': (32, [], U64, '834308014975f7', {'nzcv_save', 'split_check', 'flags'},
                                             None, {}),
    '32: fld; faddp; fstp (x87 guess)': (32, ['--x87-guess', '0x1000'], U64, 'd900dec1d918c3', {'x87_check', 'x87_run'}, None, {}),
    '32: mov [ebx+8],eax; ret': (32, [], U64, '894308c3', {'split_check'},
                                 None, {}),
    '32: mov eax,[ebx+4]; ret': (32, [], U64, '8b4304c3', {'load_move'},
                                 None, {}),
    '32: mov eax,fs:[0x14]; ret (limited FS)': (32, ['--fs-limited'], U64, '64a114000000c3', {'segment_check'},
                                                None,
                                                {}),
    '32: fld dword [eax]; faddp; fstp dword [eax]; ret': (
        32, [], U64, 'd900dec1d918c3', {'x87_check', 'split_check'},
        None, {}),
}


# Each idiom's role sequence as fexc compiles it with the host's options, reviewed against the idiom; per AFP (a Mac with
# it compiles x87 and SSE code otherwise). A Mac whose kind has no entry yet checks the HAND words only, and says so.
# Each idiom's role sequence as fexc compiles it with the host's options, reviewed against the idiom; per AFP (a Mac with
# it compiles x87 and SSE code otherwise). A Mac whose kind has no entry checks the HAND words and the rest only.
WANT = {'afp': {}, 'no_afp': {
    'push rbx; ret':
        'entry stack_check*2 fex_mem*2 guard_check*2 fex_alu exit_computed*2 stub*2',
    'mov [rdi+8],rax; mov [rdi],ecx; jmp':
        'entry split_check*4 fex_mem split_check*3 fex_mem exit_literal*3 split_stub*4 literal',
    'mov rax,[rdi+8]; ret':
        'entry fex_mem load_move fex_mem guard_check*2 fex_alu exit_computed*2 stub',
    'call rel32':
        'entry fex_const*2 stack_check*2 fex_mem exit_literal*3 stub',
    'add dword [rdi+8],1; dec rcx; jnz':
        'entry nzcv_save fex_mem fex_alu split_check*4 fex_mem flags fex_alu flags*2 fex_alu fex_branch '
        'exit_literal*6 split_stub*2 literal',
    'cmp rax,rbx; sete al; ret':
        'entry flags fex_alu flags fex_alu fex_mem guard_check*2 fex_alu exit_computed*2 stub',
    'movaps xmm0,[rdi]; lock add [rdi],eax; ret':
        'entry align_check*2 fex_mem load_move*2 fex_mem flags fex_alu fex_mem guard_check*2 fex_alu flags '
        'exit_computed*2 stub*2',
    'rep movsb':
        'entry fex_branch fex_mem*2 fex_state fex_alu*3 exit_literal*6',
    'rep stosq':
        'entry fex_branch split_check*3 fex_mem fex_state fex_alu*2 exit_literal*6 split_stub*2 literal',
    'div rcx; ret':
        'entry divide_check fex_branch helper*4 divide_check fex_alu*2 fex_branch fex_alu*4 fex_mem '
        'guard_check*2 fex_alu exit_computed*2 stub*3',
    'vpcmpeqb ymm1,ymm0,[rdi]; vpmovmskb eax,ymm1; ret':
        'entry fex_state fex_mem fex_simd*2 fex_state fex_const*4 fex_simd fex_const*4 fex_simd*13 fex_alu*2 '
        'fex_mem guard_check*2 fex_alu exit_computed*2 stub literal',
    'pop rbx; jmp rax':
        'entry fex_mem load_move guard_check*2 exit_computed*2 stub',
    'cmp al,0x37; sete cl; ret':
        'entry flags fex_const fex_alu flags*2 fex_alu fex_mem guard_check*2 fex_alu exit_computed*2 stub',
    'mov rax,fs:[0x28]; ret':
        'entry fex_state fex_mem load_move fex_mem guard_check*2 fex_alu exit_computed*2 stub literal',
    'movsd xmm0,[rdi]; mulsd; movsd [rdi],xmm0; ret':
        'entry fex_mem load_move fex_simd split_check*3 fex_mem*2 guard_check*2 fex_alu exit_computed*2 '
        'split_stub*2 stub literal',
    'kernel: rdtsc; ret':
        'entry fex_other*2 fex_const*4 fex_alu*2 fex_const fex_alu*2 fex_const fex_alu*3 fex_const fex_alu*2 '
        'fex_state fex_alu*3 fex_mem guard_check*2 fex_alu exit_computed*2 stub',
    'kernel: rep stosb (bulk)':
        'entry fex_state fex_branch fex_state fex_branch fex_alu*2 fex_const fex_alu*5 fex_branch fex_alu*4 '
        'fex_mem fex_alu*3 fex_branch*2 fex_mem fex_state fex_alu*2 fex_branch exit_literal*6 literal',
    '32: push ebx; call +0; ret':
        'entry stack_check*2 fex_mem fex_const*2 fex_other fex_mem*2 guard_check*2 fex_alu exit_computed*2 '
        'stub*2',
    '32: add dword [ebx+8],1; dec ecx; jnz':
        'entry nzcv_save fex_mem fex_alu split_check*4 fex_mem flags fex_alu flags*2 fex_alu fex_branch '
        'exit_literal*8 split_stub*2 literal',
    '32: fld; faddp; fstp (x87 guess)':
        'entry x87_check*5 fex_mem x87_run*18 fex_simd fex_state fex_const fex_state*2 fex_const fex_state '
        'fex_const*2 x87_check*2 fex_const fex_simd*2 fex_fpctl fex_alu fex_state fex_simd split_check*3 '
        'fex_mem fex_const fex_state*4 fex_mem guard_check*2 fex_alu exit_computed*2 stub*2 split_stub*2 stub '
        'literal',
    '32: mov [ebx+8],eax; ret':
        'entry split_check*4 fex_mem*2 guard_check*2 fex_alu exit_computed*2 split_stub*2 stub literal',
    '32: mov eax,[ebx+4]; ret':
        'entry fex_mem load_move fex_mem guard_check*2 fex_alu exit_computed*2 stub',
    '32: mov eax,fs:[0x14]; ret (limited FS)':
        'entry x87_run*2 fex_const segment_check*11 fex_alu*2 fex_mem load_move fex_mem guard_check*2 fex_alu '
        'exit_computed*2 x87_run*10 stub*2 literal',
    '32: fld dword [eax]; faddp; fstp dword [eax]; ret':
        'entry fex_state fex_alu*3 fex_state x87_check*2 fex_mem x87_run*18 fex_simd fex_state*2 fex_alu '
        'fex_state fex_const fex_alu*2 fex_state x87_check*2 fex_alu fex_state fex_simd fex_state fex_alu '
        'fex_state fex_const fex_alu*2 fex_state fex_fpctl fex_alu fex_state fex_simd split_check*3 fex_mem '
        'fex_alu*2 fex_state fex_const fex_alu*2 fex_state*2 fex_mem guard_check*2 fex_alu exit_computed*2 '
        'stub*2 split_stub*2 stub literal',
}}

# Words each idiom must hold, with the role the idiom gives them (derived from the idiom, not from the classifier).
X87_RUN_TEST, MSR_FPCR_X0 = 0x3950_5380, 0xd51b_4400
HAND = {
    'push rbx; ret': [(0x9240_0900, 'stack_check', 'and x0, x8, #7: RSP aligned for the run of pushes'),
                      (0xf81f_8d06, 'fex_mem', 'str x6, [x8, #-8]!: the push'),
                      (0xf940_0114, 'fex_mem', 'ldr x20, [x8]: the return address'),
                      (0xca14_4280, 'guard_check', 'eor x0, x20, x20, lsl #16: the target is canonical'),
                      (0x9100_2108, 'fex_alu', 'add x8, x8, #8: RSP after the pop'),
                      (0xaa14_03e0, 'exit_computed', 'mov x0, x20: the computed exit')],
    'mov [rdi+8],rax; mov [rdi],ecx; jmp': [(0x9100_2161, 'split_check', 'add x1, x11, #8: the store address the check needs'),
                                            (0x9100_1c20, 'split_check', 'add x0, x1, #7: its last byte'),
                                            (0xca01_0000, 'split_check', 'eor x0, x0, x1: same page?'),
                                            (0xf900_0564, 'fex_mem', 'str x4, [x11, #8]: the store'),
                                            (0xb900_0167, 'fex_mem', 'str w7, [x11]: the second store, ECX')],
    'mov rax,[rdi+8]; ret': [(0xf940_0574, 'fex_mem', 'ldr x20, [x11, #8]: the load into the temporary'),
                             (0xaa14_03e4, 'load_move', 'mov x4, x20: into RAX')],
    'add dword [rdi+8],1; dec rcx; jnz': [(0xd53b_4219, 'nzcv_save', 'mrs x25, nzcv: the RMW site\'s incoming flags'),
                                          (0xb940_0974, 'fex_mem', 'ldr w20, [x11, #8]: the load half'),
                                          (0x3100_0694, 'fex_alu', 'adds w20, w20, #1: the add and its flags'),
                                          (0xb900_0974, 'fex_mem', 'str w20, [x11, #8]: the store half'),
                                          (0xf100_04fa, 'fex_alu', 'subs x26, x7, #1: dec\'s result and flags (PF\'s register)'),
                                          (0xba1f_8682, 'flags', 'rmif: dec keeps CF'),
                                          (0xaa07_03fb, 'flags', 'mov x27, x7: AF\'s operand'),
                                          (0xaa1a_03e7, 'fex_alu', 'mov x7, x26: the result into RCX')],
    'cmp rax,rbx; sete al; ret': [(0xca06_009b, 'flags', 'eor x27, x4, x6: AF\'s operands'),
                                  (0xeb06_009a, 'fex_alu', 'subs x26, x4, x6: the compare itself (result kept for PF)')],
    'movaps xmm0,[rdi]; lock add [rdi],eax; ret': [(0x9240_0d60, 'align_check', 'and x0, x11, #15: MOVAPS\'s alignment'),
                                                   (0xa940_5574, 'fex_mem', 'ldp x20, x21, [x11]: the 16 bytes'),
                                                   (0x9e67_0290, 'load_move', 'fmov d16, x20: into XMM0\'s low half'),
                                                   (0x9eaf_02b0, 'load_move', 'fmov v16.d[1], x21: its high half'),
                                                   (0xb8e4_0174, 'fex_mem', 'ldaddal w4, w20, [x11]: the locked add'),
                                                   (0xca04_029b, 'flags', 'eor x27, x20, x4: AF\'s operands'),
                                                   (0x2b04_029a, 'fex_alu', 'adds w26, w20, w4: the sum and its flags')],
    'div rcx; ret': [(0xd400_0061, 'helper', 'svc #3: the 128/64 divide helper'),
                     (0x9ac7_0894, 'fex_alu', 'udiv x20, x4, x7: the 64/64 divide')],
    'cmp al,0x37; sete cl; ret': [(0x2a24_03fb, 'flags', 'mvn w27, w4: AF\'s operand (narrow compare)'),
                                  (0x2b04_601f, 'fex_alu', 'cmn w0, w4, lsl #24: the compare'),
                                  (0x5100_dc9a, 'flags', 'sub w26, w4, #0x37: PF\'s result'),
                                  (0x9a9f_17f4, 'flags', 'cset x20, eq: SETE\'s flag')],
    'vpcmpeqb ymm1,ymm0,[rdi]; vpmovmskb eax,ymm1; ret': [(0x3dc0_3382, 'fex_state', 'ldr q2, [x28, #0xc0]: YMM0\'s upper half'),
                                                          (0x6e23_8e11, 'fex_simd', 'cmeq v17, v16, v3: the low lanes')],
    'kernel: rdtsc; ret': [((0xd53b_e054, 0xd53b_e0d4), 'fex_other', 'mrs x20, cntvct_el0 (cntvctss_el0 with ECV): the counter')],
    '32: add dword [ebx+8],1; dec ecx; jnz': [(0x7100_04fa, 'fex_alu', 'subs w26, w7, #1: dec\'s result and flags'),
                                              (0xaa07_03fb, 'flags', 'mov x27, x7: AF\'s operand')],
    '32: mov eax,fs:[0x14]; ret (limited FS)': [(X87_RUN_TEST, 'x87_run', 'ldrb w0, [x28, #X87_RUN]: an open x87 run?'),
                                                (0xd100_1042, 'segment_check', 'sub x2, x2, #4: the operand within FS\'s limit'),
                                                (0xb944_b781, 'x87_run', 'the run\'s end: the exception word')],
    '32: fld dword [eax]; faddp; fstp dword [eax]; ret': [(0x1ad5_26c0, 'x87_check', 'lsr w0, w22, w21: FLD\'s stack slot empty?'),
                                                          (X87_RUN_TEST, 'x87_run', 'the open-run test'),
                                                          (MSR_FPCR_X0, 'x87_run', 'msr fpcr, x0: the x87\'s FPCR'),
                                                          (0x1e62_2862, 'fex_simd', 'fadd d2, d3, d2: FADDP')],
    '32: fld; faddp; fstp (x87 guess)': [(0x2a00_2020, 'x87_check', 'orr w0, w1, w0, lsl #8: TOP and tags against the guess'),
                                         (X87_RUN_TEST, 'x87_run', 'the open-run test')],
}


def compile_(fexc, d, name, bits, extra, rip, hexs, features=()):
    obj = d / 'o.obj'
    cmd = [fexc, *HOST, *features, *extra, '--rip', hex(rip), '--hex', hexs, '--out', str(d / 'o.json'), '--object', str(obj)]
    if bits == 32: cmd += ['--bitness', '32']
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode or not obj.exists(): raise SystemExit(f'{name}: fexc failed ({r.returncode}): {r.stderr.strip()}')
    return R.Code(obj.read_bytes()), bytes.fromhex(hexs)


def rle(roles):
    out = []
    for r in roles:
        if out and out[-1][0] == r: out[-1][1] += 1
        else: out.append([r, 1])
    return ' '.join(r if n == 1 else f'{r}*{n}' for r, n in out)


def main(argv):
    if not argv: sys.exit(__doc__)
    fexc, show = argv[0], '--print' in argv[1:]
    failures = []
    features = feature_overrides()
    print('host features off:', ' '.join(features[1::2]) or 'none')
    afp = 'afp=0' not in features
    if not WANT['afp' if afp else 'no_afp']: print('note: no reviewed sequences for a Mac ' + ('with' if afp else 'without') + ' AFP: HAND words and the rest only')
    with tempfile.TemporaryDirectory() as t:
        d = pathlib.Path(t)
        for name, (bits, extra, rip, hexs, must, want, tests) in CORPUS.items():
            want = WANT['afp' if afp else 'no_afp'].get(name)
            code, guest = compile_(fexc, d, name, bits, extra, rip, hexs, features)
            roles = R.roles(code)
            got = rle(roles)
            insns = code.word_insns()
            bad = []
            if 'unknown' in roles: bad.append('a word has no role')
            for w, role, why in HAND.get(name, ()):
                ws = w if isinstance(w, tuple) else (w,)
                at = [i for i, x in enumerate(code.words) if x in ws]
                if not at: bad.append(f'no word {ws[0]:08x} ({why})')
                elif any(roles[i] != role for i in at): bad.append(f'{code.words[at[0]]:08x} ({why}) is {roles[at[0]]}, not {role}')
            if missing := must - set(roles): bad.append(f'no {", ".join(sorted(missing))}')
            if want is not None and got != want: bad.append(f'roles\n      got  {got}\n      want {want}')
            for g, kind in tests.items():
                ib = R.insn_bytes(code, guest, g)
                k = R.x86_rep_string(ib, bits) if kind in ('movs', 'stos') else R.x86_wide_vector(ib, bits)
                if k != kind: bad.append(f'guest offset {g} ({ib.hex()}): {k}, not {kind}')
            if show: print(f'{name}: {got}')
            if bad:
                failures.append(name)
                print(f'FAIL {name}: ' + '; '.join(bad))
                for i, (w, r) in enumerate(zip(code.words, roles)): print(f'      {4 * i:#06x} {w:08x} g={insns[i]} {r}')
            else: print(f'ok   {name}')
    # The guest tests alone, on forms the corpus does not compile.
    for ib, bits, kind in ((b'\xf3\x48\xa5', 64, 'movs'), (b'\x66\xf3\xab', 64, 'stos'), (b'\xf3\xa6', 64, None), (b'\xa4', 64, None),
                           (b'\xf3\x41\xaa', 32, None), (b'\xf3\xa5', 32, 'movs')):
        if R.x86_rep_string(ib, bits) != kind: failures.append(ib.hex()); print(f'FAIL rep test {ib.hex()} ({bits}): not {kind}')
    for ib, bits, kind in ((b'\xc4\xe2\x7d\x17\xc1', 64, 'vex256'), (b'\xc5\xf9\xd7\xc1', 64, None), (b'\x62\xf1\x7d\x48\x74\xc1', 64, 'evex'),
                           (b'\xc5\x00', 32, None), (b'\xc5\xfd\xd7\xc1', 32, 'vex256')):
        if R.x86_wide_vector(ib, bits) != kind: failures.append(ib.hex()); print(f'FAIL vector test {ib.hex()} ({bits}): not {kind}')
    print(f'{len(CORPUS)} idioms, {len(failures)} failed')
    return 1 if failures else 0


if __name__ == '__main__':
    testenv.require_framework(__file__)
    sys.exit(main(sys.argv[1:]))

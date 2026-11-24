#!/usr/bin/env python3
"""The EL0 sampler report's word roles on fexc's output (docs/restructure/el0-sampler-plan.md chunk 1): an idiom corpus, in
64-bit and 32-bit forms, compiled with the host's own options (crates/host/src/execution.rs: --profile slice1, --ext-conds 4,
--push-run, --narrow-cmp; --bitness 32; the kernel's --cpl0 --tsc --bulk), each object classified by
tests/bench/sample_roles.py. Every word must get the role its idiom gives it: the case compares each block's whole role
sequence (run-length, in word order), the roles its idiom must show, and the guest-instruction tests the report uses (REP
string bodies, VEX.256). No word may be `unknown`. A change in fexc's output shape fails here, with each word's encoding,
so the classifier is revised with it.

    check_sample_roles.py FEXC [--print]    (--print: the sequences as compiled, to review when revising the corpus)
"""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402
import subprocess
import tempfile

import sample_roles as R

HOST = ['--profile', 'slice1', '--ext-conds', '4', '--push-run', '--narrow-cmp']
KERNEL = ['--cpl0', '--tsc', '--bulk']
U64, K64 = 0x40_1000, 0xffff_ffff_8100_1000

# name: (bitness, extra options, RIP, guest bytes, the roles the idiom must show, the block's role sequence, guest tests).
# A sequence is `role` or `role*N` per run, in word order from word 0 (a PIC full entry's word) to the code region's end.
CORPUS = {
    'push rbx; ret': (64, [], U64, '53c3', {'stack_check', 'guard_check', 'exit_computed'},
                      'entry stack_check*2 fex_mem*2 guard_check*2 fex_alu exit_computed*2 stub*2', {}),
    'mov [rdi+8],rax; mov [rdi],eax; jmp': (64, [], U64, '48894708890feb00', {'split_check', 'stub', 'exit_literal'},
                                            'entry split_check*4 fex_mem split_check*3 fex_mem exit_literal*3 stub*4 literal', {}),
    'mov rax,[rdi+8]; ret': (64, [], U64, '488b4708c3', {'load_move'},
                             'entry fex_mem load_move fex_mem guard_check*2 fex_alu exit_computed*2 stub', {}),
    'call rel32': (64, [], U64, 'e800000000', {'fex_const', 'stack_check', 'exit_literal'},
                   'entry fex_const*2 stack_check*2 fex_mem exit_literal*3 stub', {}),
    'add dword [rdi+8],1; dec rcx; jnz': (64, [], U64, '8347080148ffc975f7', {'nzcv_save', 'split_check', 'flags'},
                                          'entry nzcv_save fex_mem fex_alu split_check*4 fex_mem flags*4 fex_alu fex_branch '
                                          'exit_literal*6 stub*2 literal', {}),
    'movaps xmm0,[rdi]; lock add [rdi],eax; ret': (64, [], U64, '0f2807f00107c3', {'align_check', 'load_move', 'flags'},
                                                   'entry align_check*2 fex_mem load_move*2 fex_mem flags*2 fex_mem '
                                                   'guard_check*2 fex_alu flags exit_computed*2 stub*2', {}),
    'rep movsb': (64, [], U64, 'f3a4', {'fex_mem'},
                  'entry fex_branch fex_mem*2 fex_state fex_alu*3 exit_literal*6', {0: 'movs'}),
    'rep stosq': (64, [], U64, 'f348ab', {'split_check'},
                  'entry fex_branch split_check*3 fex_mem fex_state fex_alu*2 exit_literal*6 stub*2 literal', {0: 'stos'}),
    'div rcx; ret': (64, [], U64, '48f7f1c3', {'divide_check', 'helper'},
                     'entry divide_check fex_branch helper*4 divide_check fex_alu*2 fex_branch fex_alu*4 fex_mem '
                     'guard_check*2 fex_alu exit_computed*2 stub*3', {}),
    'vpcmpeqb ymm1,ymm0,[rdi]; vpmovmskb eax,ymm1; ret': (64, [], U64, 'c5fd740fc5fdd7c1c3', {'fex_state', 'fex_simd'},
                                                          'entry fex_state fex_mem fex_simd*2 fex_state fex_const*4 fex_simd '
                                                          'fex_const*4 fex_simd*13 fex_alu*2 fex_mem guard_check*2 fex_alu '
                                                          'exit_computed*2 stub literal', {0: 'vex256', 4: 'vex256'}),
    'pop rbx; jmp rax': (64, [], U64, '5bffe0', {'load_move', 'guard_check', 'exit_computed'},
                         'entry fex_mem load_move guard_check*2 exit_computed*2 stub', {}),
    'cmp al,0x37; sete cl; ret': (64, [], U64, '3c370f94c1c3', {'flags'},
                                  'entry flags fex_const fex_alu flags*2 fex_alu fex_mem guard_check*2 fex_alu exit_computed*2 '
                                  'stub', {}),
    'mov rax,fs:[0x28]; ret': (64, [], U64, '64488b042528000000c3', {'fex_state', 'load_move'},
                               'entry fex_state fex_mem load_move fex_mem guard_check*2 fex_alu exit_computed*2 stub literal',
                               {}),
    'movsd xmm0,[rdi]; mulsd; movsd [rdi],xmm0; ret': (64, [], U64, 'f20f1007f20f59c1f20f1107c3', {'load_move', 'split_check'},
                                                       'entry fex_mem load_move fex_simd split_check*3 fex_mem*2 '
                                                       'guard_check*2 fex_alu exit_computed*2 stub*3 literal', {}),
    'kernel: rdtsc; ret': (64, KERNEL, K64, '0f31c3', {'fex_other'},
                           'entry fex_other fex_const*4 fex_alu*2 fex_const fex_alu*2 fex_const fex_alu*3 fex_const fex_alu*2 '
                           'fex_state fex_alu*3 fex_mem guard_check*2 fex_alu exit_computed*2 stub literal', {}),
    'kernel: rep stosb (bulk)': (64, KERNEL, K64, 'f3aa', {'fex_mem'},
                                 'entry fex_state fex_branch fex_state fex_branch fex_alu*2 fex_const fex_alu*5 fex_branch fex_alu*4 '
                                 'fex_mem fex_alu*3 fex_branch*2 fex_mem fex_state fex_alu*2 fex_branch exit_literal*6 literal',
                                 {0: 'stos'}),
    '32: push ebx; call +0; ret': (32, [], U64, '53e800000000c3', {'stack_check', 'fex_const', 'guard_check'},
                                   'entry stack_check*2 fex_mem fex_const*2 fex_other fex_mem*2 guard_check*2 fex_alu '
                                   'exit_computed*2 stub*2', {}),
    '32: add dword [ebx+8],1; dec ecx; jnz': (32, [], U64, '834308014975f7', {'nzcv_save', 'split_check', 'flags'},
                                             'entry nzcv_save fex_mem fex_alu split_check*4 fex_mem flags*4 fex_alu fex_branch '
                                             'exit_literal*8 stub*2 literal', {}),
    '32: mov [ebx+8],eax; ret': (32, [], U64, '894308c3', {'split_check'},
                                 'entry split_check*4 fex_mem*2 guard_check*2 fex_alu exit_computed*2 stub*3 literal', {}),
    '32: mov eax,[ebx+4]; ret': (32, [], U64, '8b4304c3', {'load_move'},
                                 'entry fex_mem load_move fex_mem guard_check*2 fex_alu exit_computed*2 stub', {}),
    '32: mov eax,fs:[0x14]; ret (limited FS)': (32, ['--fs-limited'], U64, '64a114000000c3', {'segment_check'},
                                                'entry fex_state fex_branch fex_const segment_check*11 fex_alu*2 fex_mem '
                                                'load_move fex_mem guard_check*2 fex_alu exit_computed*2 fex_fpctl fex_alu '
                                                'fex_state fex_alu fex_state*2 fex_fpctl*2 fex_state fex_branch stub*2 literal',
                                                {}),
    '32: fld dword [eax]; faddp; fstp dword [eax]; ret': (
        32, [], U64, 'd900dec1d918c3', {'x87_check', 'split_check'},
        'entry fex_state fex_alu*3 fex_state x87_check*2 fex_mem fex_state fex_branch fex_fpctl fex_branch fex_state fex_alu '
        'fex_state fex_fpctl fex_const fex_state fex_fpctl fex_state*2 fex_alu*2 fex_const fex_alu fex_fpctl*2 fex_branch*2 '
        'fex_fpctl fex_alu fex_state fex_alu fex_state fex_fpctl fex_simd fex_state*2 fex_alu fex_state fex_const fex_alu*2 '
        'fex_state x87_check*2 fex_alu fex_state fex_simd fex_state fex_alu fex_state fex_const fex_alu*2 fex_state fex_fpctl '
        'fex_alu fex_state fex_simd split_check*3 fex_mem fex_alu*2 fex_state fex_const fex_alu*2 fex_state*2 fex_mem '
        'guard_check*2 fex_alu exit_computed*2 stub*5', {}),
}


def compile_(fexc, d, name, bits, extra, rip, hexs):
    obj = d / 'o.obj'
    cmd = [fexc, *HOST, *extra, '--rip', hex(rip), '--hex', hexs, '--out', str(d / 'o.json'), '--object', str(obj)]
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
    with tempfile.TemporaryDirectory() as t:
        d = pathlib.Path(t)
        for name, (bits, extra, rip, hexs, must, want, tests) in CORPUS.items():
            code, guest = compile_(fexc, d, name, bits, extra, rip, hexs)
            roles = R.roles(code)
            got = rle(roles)
            insns = code.word_insns()
            bad = []
            if 'unknown' in roles: bad.append('a word has no role')
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

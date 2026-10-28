"""S1c checks: profile slice1 of fexc (block cutting at an allowlist, single-gate exits, profile record).

Run: python3 -m unittest tools/fexc/test_slice1.py      (binary: $FEXC, default the series10 build)

The x86 bytes below are assembled by hand and only drive the compiler; every expectation (where the block ends, why)
comes from the instruction's architectural class, never from fexc's output. The cut position is also compared with a
second compile of just the instructions before it: the code must be identical, so nothing of the cut instruction (or
of FEX's mis-decode of it) leaks into the block.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import check_sites  # noqa: E402  (disassemble, object reader)
import scan_skips  # noqa: E402

FEXC = os.environ.get('FEXC', '/Users/liaozping/orca/natemu-raw/fexc-native-20261026-01/series10/obj/fexc')
REPO = HERE.parents[1]
RIP = 0x102000
PRE = '4801d8' + '4831c9'   # add rax, rbx ; xor rcx, rcx

# name -> hex. Architecturally: privilege/descriptor/MSR/segment/interrupt/IO/TLB/cache/flag-control, or VM-owned values.
SYSTEM = {
    'mov cr0,rax': '0f22c0', 'mov cr3,rax': '0f22d8', 'mov cr4,rax': '0f22e0', 'mov cr8,rax': '440f22c0',
    'mov rax,cr0': '0f20c0', 'mov rax,cr3': '0f20d8', 'mov rax,cr4': '0f20e0', 'mov dr7,rax': '0f23f8', 'mov rax,dr0': '0f21c0',
    'lgdt [rax]': '0f0110', 'lidt [rax]': '0f0118', 'sgdt [rax]': '0f0100', 'ltr ax': '0f00d8', 'lldt ax': '0f00d0',
    'wrmsr': '0f30', 'rdmsr': '0f32', 'rdpmc': '0f33',
    'mov ds,ax': '8ed8', 'mov ss,ax': '8ed0', 'mov fs,ax': '8ee0', 'mov ax,ds': '8cd8', 'push fs': '0fa0', 'pop gs': '0fa9',
    'lss eax,[rax]': '0fb200', 'lgs eax,[rax]': '0fb500',
    'int3': 'cc', 'int 0x80': 'cd80', 'iretq': '48cf', 'iretd': 'cf', 'retf': 'cb', 'jmp far [rax]': 'ff28', 'call far [rax]': 'ff18',
    'in al,0x80': 'e480', 'out 0x80,al': 'e680', 'in al,dx': 'ec', 'out dx,al': 'ee', 'insb': '6c', 'rep outsb': 'f36e',
    'hlt': 'f4', 'invlpg [rax]': '0f0138', 'invd': '0f08', 'wbinvd': '0f09', 'clflush [rax]': '0fae38', 'clts': '0f06',
    'invpcid': '660f3882' + '00',
    'syscall': '0f05', 'sysret': '480f07', 'sysenter': '0f34', 'sysexit': '0f35',
    'cli': 'fa', 'sti': 'fb', 'popfq': '9d', 'pushfq': '9c',
    'cpuid': '0fa2', 'rdtsc': '0f31', 'rdtscp': '0f01f9', 'xgetbv': '0f01d0', 'swapgs': '0f01f8', 'clac (FEX cannot decode)': '0f01ca',
    'rdfsbase': 'f3480faec0', 'wrfsbase': 'f3480faed0', 'ud2': '0f0b',
}

# name -> hex. Decodable instructions that are not slice-1 forms, and admitted opcodes with an excluded prefix or operand.
UNSUPPORTED = {
    'lock xchg rax,rcx (#UD)': 'f04887c1', 'rep xchg r8,rax (not pause)': 'f34190', 'o16 mfence': '660faef0',
    'lfence modrm e9': '0faee9', 'rep mfence': 'f30faef0',
    'lock add [rax],rcx': 'f0480108', 'lock inc [rax]': 'f0ff00', 'cmpxchg [rax],ecx': '0fb108', 'xadd [rax],ecx': '0fc108',
    'rep movsb': 'f3a4', 'movsb': 'a4', 'stosq': '48ab', 'scasb': 'ae', 'cmpsb': 'a6', 'lodsb': 'ac',
    'mov rax,fs:[rax]': '64488b00', 'mov rax,gs:[rax]': '65488b00', 'mov eax,[eax] (addr32)': '678b00',
    'addps': '0f58c1', 'movaps': '0f28c1', 'fld st0': 'd9c0', 'vzeroupper': 'c5f877', 'cld': 'fc', 'std': 'fd',
    'clc': 'f8', 'stc': 'f9', 'lahf': '9f', 'sahf': '9e',
    'fxsave': '0fae00', 'loop 67-prefix': '67e2fe', 'jecxz': '67e3fe', 'loop o16': '66e2fe', 'shld': '0fa4c101', 'shrd': '0facc101', 'popcnt': 'f3480fb8c1',
    'tzcnt': 'f30fbcc1', 'lzcnt': 'f30fbdc1', 'rdrand': '0fc7f0', 'rdseed': '0fc7f8', 'cmpxchg16b': '480fc708', 'cmpxchg8b': '0fc708',
    'leave': 'c9', 'enter': 'c8080000', 'xlat': 'd7', 'movbe': '0f38f000', 'prefetch': '0f1808', 'sal /6 alias': 'd1f0',
    'bnd ret': 'f2c3', 'o16 ret': '66c3', 'o16 jmp': '66e9fbffffff', 'bswap r9w (undefined)': '66410fc9', 'bswap ax': '660fc8',
    # FEX branches around the whole access for a zero count (static or dynamic), losing a guest #PF
    'rcl dword [rax],1': 'd110', 'rcr qword [rax],cl': '48d318', 'rcr qword [rax],0': '48c11800', 'rcl byte [rax],cl': 'd210',
    'rcr word [rax],5': '66c11805', 'rcl dword [rax],cl': 'd310', 'rcr dword [rax+rcx*4+8],cl': 'd35c8808', 'rcl byte [rax],32': 'c01020',
    'rcr [rax],imm (c1, rex.w)': '48c11805',
}

# One instruction each, all inside the allowlist; every one must translate completely.
ALLOWED = {
    'nop': '90', 'nop dword [rax]': '0f1f00', 'nop word [rax+rax]': '660f1f440000', 'xchg rax,rcx': '4891', 'xchg rcx,rdx': '4887d1',
    'xchg al,ah': '86c4', 'add [rax],rcx': '480108', 'add rcx,[rax]': '480308', 'sub eax,5': '83e805', 'adc rax,rbx': '4811d8',
    'sbb rax,[rax]': '481b00', 'and al,1': '2401', 'or ax,0x1234': '660d3412', 'xor eax,eax': '31c0', 'cmp [rax],rcx': '483908',
    'test rax,rax': '4885c0', 'test [rax],al': '8400', 'test rax,imm': '48f7c001000000', 'inc dword [rax]': 'ff00', 'dec rax': '48ffc8',
    'neg rcx': '48f7d9', 'not dword [rax]': 'f710', 'mul rcx': '48f7e1', 'imul rcx': '48f7e9', 'imul rax,rcx': '480fafc1',
    'imul rax,rcx,5': '486bc105', 'imul eax,[rax],0x1234': '690034120000', 'shl rax,cl': '48d3e0', 'sar eax,3': 'c1f803',
    'rol al,1': 'd0c0', 'rcl rax,1': '48d1d0', 'ror word [rax],cl': '66d308', 'shr [rax],5': '48c12805',
    'mov eax,ecx': '89c8', 'mov rax,[rax]': '488b00', 'mov [rax],ecx': '8908', 'mov al,[rax+rcx*4+8]': '8a448808', 'mov rax,1': '48c7c001000000',
    'mov byte [rax],1': 'c60001', 'mov eax,imm': 'b801000000', 'movabs rax,imm64': '48b80102030405060708',
    'movabs al,[moffs]': '48a00102030405060708', 'movabs [moffs],rax': '48a30102030405060708', 'movzx eax,cl': '0fb6c1',
    'movzx eax,word [rax]': '0fb700', 'movsx rax,cl': '480fbec1', 'movsx eax,ax': '0fbfc0', 'movsxd rax,ecx': '4863c1',
    'lea rax,[rax+rcx*2+5]': '488d440805', 'lea rax,[rip+0]': '488d0500000000', 'sete al': '0f94c0', 'setl [rax]': '0f9c00',
    'cmove rax,rcx': '480f44c1', 'cmovg eax,[rax]': '0f4f00', 'bswap rax': '480fc8', 'bswap r9d': '410fc9', 'bsf rax,rcx': '480fbcc1',
    'bsr eax,[rax]': '0fbd00', 'bt rax,rcx': '480fa3c8', 'bts [rax],rcx': '480fab08', 'btr rax,5': '480fbaf005', 'btc [rax],5': '480fba3805',
    'div cl': 'f6f1', 'div rcx': '48f7f1', 'idiv [rcx]': '48f739', 'idiv r15d': '41f7ff', 'div byte [rax]': 'f630', 'idiv bx': '66f7fb',
    'cbw': '6698', 'cwde': '98', 'cdqe': '4898', 'cwd': '6699', 'cdq': '99', 'cqo': '4899', 'push rax': '50', 'push r9': '4151',
    'endbr64': 'f30f1efa', 'xchg [rax],ecx': '8708', 'xchg [rax],rcx': '488708', 'xchg [rax],cl': '8608', 'xchg cx,[rax]': '668708',
    'lock xchg [rax],rcx': 'f0488708', 'pause': 'f390', 'lfence': '0faee8', 'mfence': '0faef0', 'sfence': '0faef8', 'rcl rax,cl': '48d3d0', 'rcr eax,1': 'd1d8', 'rcl al,5': 'c0d005', 'pop rbx': '5b', 'push imm8': '6a01', 'push imm32': '6800010000', 'push [rax]': 'ff30', 'pop [rax]': '8f00', 'push ax': '6650',
}

# (name, hex, translated-instruction count): control transfers end the block with their normal exit.
BRANCHES = {
    'jmp rel8': 'eb05', 'jmp rel32': 'e905000000', 'jne rel8': '7505', 'je rel32': '0f8405000000', 'call rel32': 'e805000000',
    'loop rel8': 'e205', 'loope rel8': 'e105', 'loopne rel8': 'e005', 'jrcxz rel8': 'e305',
    'ret': 'c3', 'rep ret': 'f3c3', 'ret 8': 'c20800', 'jmp rax': 'ffe0', 'call rax': 'ffd0', 'jmp [rax]': 'ff20', 'call [rax]': 'ff10',
}


def run(hexs, rip=RIP, *extra, profile='slice1'):
    args = [FEXC] + (['--profile', profile] if profile else []) + ['--hex', hexs, '--rip', hex(rip), *extra]
    r = subprocess.run(args, capture_output=True, text=True, timeout=120)
    return json.loads(r.stdout), r.returncode


def region(d):
    """The code region between the header and the tail (relocation slots zeroed)."""
    lay = d['code']['layout']
    return bytes.fromhex(d['code']['hex'])[lay['code_region_begin']:lay['code_region_end']]


@unittest.skipUnless(os.access(FEXC, os.X_OK), f'fexc not built: {FEXC} (set $FEXC)')
class Slice1Cutting(unittest.TestCase):
    def cut_after_two(self, name, x, reason):
        window = PRE + x + PRE   # allowed instructions on both sides: only the first two may be translated
        d, rc = run(window)
        pre_len = len(PRE) // 2
        self.assertEqual(rc, 0, name)
        g = d['guest']
        self.assertEqual((g['instructions_translated'], g['bytes_translated']), (2, pre_len), name)
        self.assertEqual(g['end']['rip'], hex(RIP + pre_len), name)
        self.assertEqual(g['end']['reason'], reason, (name, g['end']['detail']))
        self.assertFalse(g['instructions'][2]['translated'], name)
        # Identical to compiling only the two instructions before it: nothing of the cut instruction is in the block.
        ref, rc = run(PRE)
        self.assertEqual(rc, 0)
        self.assertEqual(ref['guest']['end']['reason'], 'end_of_input')
        self.assertEqual(region(d), region(ref), name)
        self.assertEqual([r for r in d['code']['relocations'] if r['kind'] == 'guest_rip_literal' and r['guest_rip'] != hex(RIP)][0]['guest_rip'],
                         hex(RIP + pre_len), name)
        # As the first instruction of the window: no code, same reason, at the window start.
        d, rc = run(x + PRE)
        self.assertEqual(rc, 1, name)
        self.assertEqual(d['status'], 'no_code', name)
        self.assertEqual(d['guest']['instructions_translated'], 0, name)
        self.assertEqual((d['guest']['end']['rip'], d['guest']['end']['reason']), (hex(RIP), reason), name)

    def test_system_instructions(self):
        for name, x in SYSTEM.items():
            with self.subTest(name):
                self.cut_after_two(name, x, 'system_instruction')

    def test_unsupported_forms(self):
        for name, x in UNSUPPORTED.items():
            with self.subTest(name):
                self.cut_after_two(name, x, 'unsupported_form')

    def test_allowed_forms_translate(self):
        for name, x in ALLOWED.items():
            with self.subTest(name):
                d, rc = run(PRE + x + PRE)
                g = d['guest']
                self.assertEqual(rc, 0, name)
                self.assertEqual((g['instructions_translated'], g['end']['reason']), (5, 'end_of_input'), (name, g['end']))

    def test_branches_end_block_with_normal_exit(self):
        for name, x in BRANCHES.items():
            with self.subTest(name):
                d, rc = run(PRE + x + PRE)
                g = d['guest']
                self.assertEqual(rc, 0, name)
                self.assertEqual((g['instructions_translated'], g['end']['reason']), (3, 'block_ending_instruction'), (name, g['end']))
                self.assertEqual(g['end']['rip'], hex(RIP + (len(PRE) + len(x)) // 2))

    def test_decoder_failure_on_first_instruction_gives_no_code(self):
        # not FEX's NoExec/SIGSEGV block, and never an object file
        for name, x, reason in (('lone REX', '48', 'partial_decode'), ('lone 0f', '0f', 'not_executable'), ('0f 01 without ModRM', '0f01', 'partial_decode'),
                                ('lone 66', '66', 'partial_decode'), ('f3 48', 'f348', 'partial_decode')):
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                obj = Path(tmp) / 'o.bin'
                d, rc = run(x, RIP, '--object', str(obj))
                self.assertEqual((rc, d['status']), (1, 'no_code'), name)
                self.assertEqual((d['guest']['instructions_translated'], d['guest']['end']['rip'], d['guest']['end']['reason']), (0, hex(RIP), reason), name)
                self.assertNotIn('hex', d.get('code', {}))
                self.assertFalse(obj.exists(), name)

    def test_decoder_failure_mid_block_ends_the_block_without_a_signal_block(self):
        ref, _ = run(PRE)
        for name, x, reason in (('lone REX', '48', 'partial_decode'), ('lone 0f', '0f', 'not_executable'), ('lone 66', '66', 'partial_decode')):
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                obj = Path(tmp) / 'o.bin'
                d, rc = run(PRE + x, RIP, '--object', str(obj))
                self.assertEqual((rc, d['guest']['instructions_translated'], d['guest']['end']['reason']), (0, 2, reason), name)
                self.assertEqual(region(d), region(ref), name)   # nothing emitted for the undecodable bytes
                check_exits(self, d, Path(tmp), name)            # in particular no SIGSEGV/SIGILL pointer loads
                self.assertTrue(obj.exists())

    def test_no_object_unless_status_ok(self):
        for profile in ('slice1', None):
            with tempfile.TemporaryDirectory() as tmp:
                obj = Path(tmp) / 'o.bin'
                d, rc = run('48', RIP, '--object', str(obj), profile=profile)
                self.assertEqual(rc, 1)
                self.assertFalse(obj.exists(), profile)

    def test_sparse_pc_refused(self):
        for args in (['--profile', 'slice1', '--sparse-pc'], ['--sparse-pc', '--profile', 'slice1']):
            r = subprocess.run([FEXC, *args, '--hex', '90', '--rip', '0x1000'], capture_output=True, text=True)
            self.assertEqual(r.returncode, 2, args)

    def test_no_skippable_site_in_any_admitted_form(self):
        # branches inside one guest instruction's native range that jump over a recorded access (rcl/rcr by a zero count
        # were such forms and are cut); and no memory form without any access
        scan_skips.FEXC = FEXC
        cut, bad, fixtures = scan_skips.scan()
        self.assertEqual(bad, {})
        self.assertGreaterEqual(fixtures, 60)
        self.assertTrue(cut and all(k.startswith(('rcl', 'rcr')) and '[rax]' in k for k in cut), cut)

    def test_object_matches_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            for x in (PRE + '488b00', PRE + SYSTEM['cli'], PRE + '7505'):
                obj = Path(tmp) / 'o.bin'
                d, rc = run(x, RIP, '--object', str(obj))
                self.assertEqual(rc, 0)
                o = check_sites.read_object(obj)
                self.assertEqual(check_sites.object_vs_json(o, d), [])
                self.assertEqual((o['profile'], o['settings']), (1, 4))   # slice1; only the dense PC table is on
                self.assertTrue(all(s[24] == 1 for s in o['sites']))      # located
                self.assertEqual(o['pre_nzcv'], d['state_abi']['state_offsets']['natemu_pre_nzcv'])   # 1032: the frame NZCV slot
            d, _ = run(PRE + SYSTEM['cli'], RIP, '--object', str(obj))
            self.assertEqual(check_sites.END_REASONS[check_sites.read_object(obj)['end_reason']], 'system_instruction')

    def test_max_instructions_still_works(self):
        d, rc = run(PRE + PRE, RIP, '--max-insts', '3')
        self.assertEqual((rc, d['guest']['instructions_translated'], d['guest']['end']['reason']), (0, 3, 'max_instructions'))

    def test_block_over_32k_after_far_branch_restart(self):
        # 1500 x `mov qword ptr [rbx+8], rax` (48 89 43 08): guard + split checks push the block past tbnz's 32 KiB
        # range, FEX restarts with far branches; the PC table must hold one entry per instruction, in order, and the
        # guards (far form) must verify.
        import verify_precision
        d, rc = run('48894308' * 1500, RIP, '--max-insts', '1500')
        self.assertEqual((rc, d['guest']['instructions_translated']), (0, 1500))
        self.assertGreater(d['code']['layout']['code_region_end'], 32768)
        entries = d['pc_table']['entries']
        self.assertEqual([e['guest_offset'] for e in entries], [0] + [4 * k for k in range(1500)])
        self.assertEqual([e['native_offset'] for e in entries], sorted({e['native_offset'] for e in entries}))
        with tempfile.TemporaryDirectory() as t:
            self.assertEqual(verify_precision.verify(d, Path(t))[0], [])

    def test_window_end_inside_instruction_is_not_a_cut(self):
        # 0f 01 with its ModRM byte missing: the fetch runs out of window; the decoder's own reason is kept.
        d, rc = run(PRE + '0f01')
        self.assertEqual((rc, d['guest']['instructions_translated'], d['guest']['end']['reason']), (0, 2, 'partial_decode'))

    def test_profile_rules(self):
        r = subprocess.run([FEXC, '--profile', 'slice1', '--bitness', '32', '--hex', '90', '--rip', '0x1000'], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        for flag in ('--multiblock', '--no-end-marker'):   # --no-end-marker is a no-op, but --multiblock must be refused
            r = subprocess.run([FEXC, '--profile', 'slice1', flag, '--hex', '90', '--rip', '0x1000'], capture_output=True, text=True)
            self.assertEqual(r.returncode, 2 if flag == '--multiblock' else 0, flag)
        r = subprocess.run([FEXC, '--profile', 'slice1', '--feature', 'flagm=0', '--hex', '90', '--rip', '0x1000'], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)

    def test_profile_recorded(self):
        d, _ = run(PRE)
        s = d['settings']
        self.assertEqual((s['profile'], s['block_cutting'], s['exits'], s['tso'], s['multiblock'], s['dense_pc_table'], s['end_marker']),
                         ('slice1', 'allowlist', 'dispatcher_gate', False, False, True, False))
        self.assertTrue(d['features']['host_features']['flagm'])
        self.assertEqual(d['features']['required_by_settings_profile'], 'flagm')
        d, _ = run(PRE, profile=None)
        s = d['settings']
        self.assertEqual((s['profile'], s['block_cutting'], s['exits'], s['tso'], s['end_marker']), ('default', 'none', 'linked', True, True))

    def test_default_profile_does_not_cut(self):
        # the same system instructions translate (or fail) as before in the default profile: no cut is ever reported
        for x in (SYSTEM['mov cr3,rax'], SYSTEM['cli'], UNSUPPORTED['lock add [rax],rcx']):
            d, rc = run(PRE + x + PRE, profile=None)
            self.assertNotIn(d['guest']['end']['reason'], ('system_instruction', 'unsupported_form'))
            self.assertNotIn('cut_opcode', d['guest']['end'])


# `ldr x1, [x28, #pointer]; br x1` must be the only way out, fed by `str xR, [x28, #rip]`: a literal exit is exactly
# `ldr x0, L; str x0, [x28, #rip]; ldr x1, [x28, #ExitFunctionLinker]; br x1` (the resident links it), a computed one loads
# DispatcherLoopTop. The only call is FEX's call of the resident 128/64 division helper of a 64-bit div/idiv
# (verify_precision.py P11 checks its #DE branch).
HELPERS = {'LUDIVHandler', 'LDIVHandler'}
def check_exits(test, d, tmp, what):
    off = d['state_abi']['state_offsets']
    rip_off, dlt_off, efl_off = off['rip'], off['dispatcher_loop_top'], off['exit_function_linker']
    # exit RIP literals, plus guest-RIP constants materialised for RIP-relative addresses; nothing host-specific
    test.assertEqual({r['kind'] for r in d['code']['relocations']} - {'guest_rip_literal', 'guest_rip_move'}, set(), what)
    test.assertFalse([r for r in d['code']['relocations'] if r['kind'] == 'named_symbol_literal'], what)
    test.assertEqual({p['pointer'] for p in d['state_abi']['state_pointer_loads']} - {'DispatcherLoopTop', 'ExitFunctionLinker'} - HELPERS, set(), what)
    helpers = {p['state_offset'] for p in d['state_abi']['state_pointer_loads'] if p['pointer'] in HELPERS}
    test.assertIsNone(d['code']['layout']['end_marker_offset'], what)
    code = bytes.fromhex(d['code']['hex'])
    end = d['code']['layout']['code_region_end']
    lits = [(r['offset'], r['size']) for r in d['code']['relocations'] if r['size']]
    dis = check_sites.disassemble(code, end, tmp, lits)
    lines = sorted(dis.items())
    brs = [i for i, (o, t) in enumerate(lines) if re.match(r'br\s', t)]
    test.assertTrue(brs, what)
    for i in brs:
        o, t = lines[i]
        reg = re.match(r'br\s+(x\d+)$', t)
        test.assertTrue(reg and reg.group(1) == 'x1', (what, o, t))
        test.assertRegex(lines[i - 2][1], rf'^str x\d+, \[x28, #{hex(rip_off)}\]$', (what, o))
        literal = re.fullmatch(r'ldr x0, 0x[0-9a-f]+', lines[i - 3][1]) and lines[i - 2][1].startswith('str x0,')
        test.assertEqual(lines[i - 1][1], f'ldr x1, [x28, #{hex(efl_off if literal else dlt_off)}]', (what, o))
    # no other control transfer through registers or calls, and no use of the call-return stack register
    for i, (o, t) in enumerate(lines):
        if t == 'blr x3':
            test.assertRegex(lines[i - 2][1], r'^ldr x3, \[x28, #0x[0-9a-f]+\]$', (what, o))
            test.assertEqual(lines[i - 1][1], 'str x30, [sp, #-0x10]!', (what, o))
            test.assertEqual(lines[i + 1][1], 'ldr x30, [sp], #0x10', (what, o))
            continue
        test.assertIsNone(re.match(r'(blr|ret|bl)\s', t), (what, o, t))
        test.assertIsNone(re.search(r'\bx25\b', t), (what, o, t))
    # every ldr from the frame in the code reads DispatcherLoopTop, ExitFunctionLinker or a division helper (a literal pool load has no x28 base)
    for o, t in lines:
        m = re.match(r'ldr\s+x\d+, \[x28, #(0x[0-9a-f]+)\]', t)
        if m:
            test.assertIn(int(m.group(1), 16), {dlt_off, efl_off} | helpers, (what, o, t))
    return len(brs), '\n'.join(f'{o:4x}: {t}' for o, t in lines)


@unittest.skipUnless(os.access(FEXC, os.X_OK), f'fexc not built: {FEXC} (set $FEXC)')
class Slice1Exits(unittest.TestCase):
    def test_exit_sequences(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            for name, x in BRANCHES.items():
                with self.subTest(name):
                    d, rc = run(PRE + x)
                    self.assertEqual(rc, 0)
                    n, _ = check_exits(self, d, tmp, name)
                    self.assertEqual(n, 2 if name in ('jne rel8', 'je rel32', 'loop rel8', 'loope rel8', 'loopne rel8', 'jrcxz rel8') else 1, name)   # taken and fall-through gates
            for name, x in (('fall off the window', PRE), ('cut before cr3 write', PRE + SYSTEM['mov cr3,rax'])):
                with self.subTest(name):
                    d, rc = run(x)
                    n, _ = check_exits(self, d, tmp, name)
                    self.assertEqual(n, 1)

    def test_exit_sequence_text(self):
        # the literal exit form for a conditional branch: two gates, one resident pointer
        d, _ = run(PRE + '7505')
        with tempfile.TemporaryDirectory() as t:
            n, text = check_exits(self, d, Path(t), 'jne')
        self.assertEqual(n, 2)
        self.assertEqual(len(re.findall(r'ldr x\d+, \[x28, #0x[0-9a-f]+\]', text)), 2)
        print('\nexit sequences of "add; xor; jne +5" under slice1:\n' + text)

    def test_fixture_programs(self):
        programs = check_sites.fixture_programs()
        full = cut = 0
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            for p in programs:
                with self.subTest(p['name']):
                    k = check_sites.first_cut(p['asm'])
                    d, rc = run(p['code'], int(p['rip'], 16))
                    g = d['guest']
                    if k is None:
                        full += 1
                        self.assertEqual((rc, g['instructions_translated'], g['end']['reason']), (0, len(p['asm']), 'end_of_input'))
                    else:
                        cut += 1
                        self.assertEqual(g['instructions_translated'], k)
                        self.assertEqual(g['end']['reason'], 'unsupported_form')
                        if k == 0:
                            self.assertEqual((rc, d['status']), (1, 'no_code'))
                            continue
                    check_exits(self, d, tmp, p['name'])
                    for s in d['sites']:
                        self.assertEqual(s['ordering'], 'plain', p['name'])   # hardware TSO: no ldar/stlr
        print(f'\nfixtures: {full} translate completely, {cut} are cut (unsupported_form)')
        self.assertEqual((full, cut), (64, 0))



@unittest.skipUnless(os.access(FEXC, os.X_OK), f'fexc not built: {FEXC} (set $FEXC)')
class Slice1DivisionChecks(unittest.TestCase):
    def test_p11_pins_the_exact_check(self):
        # A real compile of every div/idiv form verifies; one-word mutations of its #DE checks (the extend of the
        # 32-bit high check, the bitfield of the 8-bit one, the divisor register, the 8-bit dividend copy, FEX's
        # 64-bit fast-path selector) must each be reported by P11.
        import verify_precision as vp
        p = next(p for p in vp.adversarial() if p['name'] == 'division-forms')
        d, _ = run(p['code'], int(p['rip'], 16))
        code = bytes.fromhex(d['code']['hex'])
        with tempfile.TemporaryDirectory() as t:
            self.assertEqual(vp.verify(d, Path(t))[0], [])
            dis = check_sites.disassemble(code, d['code']['layout']['code_region_end'], Path(t))
            for text, change, why in (('sub x0, x0, w7, uxtw', lambda w: w & ~(7 << 13) | 1 << 13, 'exact #DE check'),   # uxth
                                      ('ubfx x0, x23, #8, #8', lambda w: w + (1 << 16) + (1 << 10), 'exact #DE check'),   # #9, #8
                                      ('sub x0, x0, w20, uxtb', lambda w: w + (1 << 16), 'operands'),                     # w21
                                      ('uxth w23, w4', lambda w: w + (2 << 5), 'copy of AX'),                             # w6
                                      ('cbz x5, ', lambda w: w + 1, 'selector'),                                          # cbz x6
                                      ('eor x0, x0, x5', lambda w: w & ~(31 << 16), 'selector')):                         # x0
                o = min(o for o, x in dis.items() if x.startswith(text))   # first occurrence
                w = change(int.from_bytes(code[o:o + 4], 'little'))
                bad = dict(d, code=dict(d['code'], hex=(code[:o] + w.to_bytes(4, 'little') + code[o + 4:]).hex()))
                problems = vp.verify(bad, Path(t))[0]
                self.assertTrue(any(why in m for m in problems), (text, problems))


@unittest.skipUnless(os.access(FEXC, os.X_OK), f'fexc not built: {FEXC} (set $FEXC)')
class Slice1SelfLoopsAndStop(unittest.TestCase):
    """S2c0: a jcc back to its own block's entry exits like any taken edge; every block starts with the pinned stop store."""
    STOP = 0xf907639f   # str xzr, [x28, #3776]

    def test_self_loop_exits(self):
        body = '4883c001' + '48ffc9'   # add rax,1 ; dec rcx  (7 bytes)
        import verify_precision as vp
        with tempfile.TemporaryDirectory() as t:
            for cc in range(16):
                for name, jcc, size in (('rel8', f'{0x70 + cc:02x}f7', 2), ('rel32', f'0f{0x80 + cc:02x}f3ffffff', 6)):
                    with self.subTest(f'j{cc:x} {name}'):
                        d, rc = run(body + jcc)
                        self.assertEqual((rc, d['guest']['instructions_translated']), (0, 3))
                        n, _ = check_exits(self, d, Path(t), name)
                        self.assertEqual(n, 2)                                # taken and fall-through gates
                        lits = sorted(r['guest_rip'] for r in d['code']['relocations'] if r['kind'] == 'guest_rip_literal')
                        self.assertIn(hex(RIP), lits)                         # the taken edge exits to the block's own entry
                        self.assertIn(hex(RIP + 7 + size), lits)
                        code = bytes.fromhex(d['code']['hex'])
                        end = d['code']['layout']['code_region_end']
                        lit = [(r['offset'], r['size']) for r in d['code']['relocations'] if r['size']]
                        dis = check_sites.disassemble(code, end, Path(t), lit)
                        self.assertEqual(vp.entry_and_branches(d, dis, sorted(dis)), [])   # P12, P13

    def test_self_loop_exits_of_loop_and_jrcxz(self):
        body = '4883c001' + '48ffc9'
        import verify_precision as vp
        with tempfile.TemporaryDirectory() as t:
            for name, op in (('loopne', 0xe0), ('loope', 0xe1), ('loop', 0xe2), ('jrcxz', 0xe3)):
                with self.subTest(name):
                    d, rc = run(body + f'{op:02x}f7')
                    self.assertEqual((rc, d['guest']['instructions_translated']), (0, 3))
                    n, _ = check_exits(self, d, Path(t), name)
                    self.assertEqual(n, 2)
                    lits = sorted(r['guest_rip'] for r in d['code']['relocations'] if r['kind'] == 'guest_rip_literal')
                    self.assertIn(hex(RIP), lits)
                    self.assertIn(hex(RIP + 9), lits)
                    code = bytes.fromhex(d['code']['hex'])
                    end = d['code']['layout']['code_region_end']
                    lit = [(r['offset'], r['size']) for r in d['code']['relocations'] if r['size']]
                    dis = check_sites.disassemble(code, end, Path(t), lit)
                    self.assertEqual(vp.entry_and_branches(d, dis, sorted(dis)), [])   # P12, P13

    def test_stop_store_is_the_third_prologue_word_of_every_block(self):
        for name, x in list(BRANCHES.items()) + [('add', '4801d8'), ('push', '50'), ('mov [rax],rcx', '488908'), ('div', '48f7f1')]:
            with self.subTest(name):
                d, rc = run(x)
                self.assertEqual(rc, 0)
                code = bytes.fromhex(d['code']['hex'])
                self.assertEqual(d['state_abi']['state_offsets']['natemu_interrupt_fault_page'], 3776)
                self.assertEqual(int.from_bytes(code[12:16], 'little'), self.STOP)
                self.assertEqual(d['code']['layout']['entries'][0]['body_native_offset'], 16)
                self.assertEqual(int.from_bytes(code[8:12], 'little'), 0xf900_0380)   # str x0, [x28]
                words = [int.from_bytes(code[o:o + 4], 'little') for o in range(16, d['code']['layout']['code_region_end'], 4)]
                self.assertNotIn(self.STOP, words)                                      # only at the entry

    def test_p12_p13_mutations(self):
        import verify_precision as vp
        d, _ = run('4883c001' + '48ffc9' + '75f7')
        with tempfile.TemporaryDirectory() as t:
            code = bytes.fromhex(d['code']['hex'])
            end = d['code']['layout']['code_region_end']
            lit = [(r['offset'], r['size']) for r in d['code']['relocations'] if r['size']]
            dis = check_sites.disassemble(code, end, Path(t), lit)
            order = sorted(dis)
            self.assertEqual(vp.entry_and_branches(d, dis, order), [])
            nop = 'mov x0, x0'
            for what, mutate, why in (
                    ('stop store removed', lambda m: m.update({12: nop}), 'pinned entry sequence'),
                    ('wrong field', lambda m: m.update({12: 'str xzr, [x28, #0xec8]'}), 'pinned entry sequence'),
                    ('wrong register', lambda m: m.update({12: 'str x0, [x28, #0xec0]'}), 'pinned entry sequence'),
                    ('moved to the second word', lambda m: m.update({8: m[12], 12: m[8]}), 'pinned entry sequence'),
                    ('a second store later', lambda m: m.update({0x14: 'str xzr, [x28, #0xec0]'}), 'interrupt-fault word'),
                    ('a load of it', lambda m: m.update({0x14: 'ldr x1, [x28, #0xec0]'}), 'interrupt-fault word'),
                    ('backward branch to the entry body', lambda m: m.update({0x28: 'b.ne 0x10'}), 'not a forward branch')):
                bad = dict(dis)
                mutate(bad)
                problems = vp.entry_and_branches(d, bad, sorted(bad))
                self.assertTrue(any(why in m for m in problems), (what, problems))


if __name__ == '__main__':
    unittest.main()

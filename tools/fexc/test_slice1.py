"""S1c checks: profile slice1 of fexc (block cutting at an allowlist, single-gate exits, profile record).

Run: python3 -m unittest tools/fexc/test_slice1.py      (binary: $FEXC, default the series20 build)

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

FEXC = os.environ.get('FEXC', '/Users/liaozping/orca/natemu-raw/fexc-native-20261026-01/series20/obj/fexc')
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
    'cli': 'fa', 'sti': 'fb', 'popfq': '9d',
    'cpuid': '0fa2', 'rdtsc': '0f31', 'rdtscp': '0f01f9', 'xgetbv': '0f01d0', 'swapgs': '0f01f8', 'clac (FEX cannot decode)': '0f01ca',
    'rdfsbase': 'f3480faec0', 'wrfsbase': 'f3480faed0', 'ud2': '0f0b',
}

# name -> hex. Decodable instructions that are not slice-1 forms, and admitted opcodes with an excluded prefix or operand.
UNSUPPORTED = {
    'rep xchg r8,rax (not pause)': 'f34190', 'o16 mfence': '660faef0',
    'lfence modrm e9': '0faee9', 'rep mfence': 'f30faef0',
    # I3: LOCK with a REP prefix (XACQUIRE/XRELEASE without HLE), an operand-size prefix on cmpxchg8b/16b (no SDM meaning)
    'rep lock add [rax],rcx': 'f3f0480108', 'repne lock xadd [rax],ecx': 'f2f00fc108', 'o16 cmpxchg8b': '660fc708',
    # I2: string forms outside the allowlist (REPNE only on cmps/scas, never with REP; no 0x67; FS/GS and SS rules)
    'repne movsb': 'f2a4', 'repne stosq': 'f248ab', 'rep repne cmpsb': 'f3f2a6', 'addr32 rep movsb': '67f3a4', 'addr32 lodsb': '67ac',
    'fs stosb (ES:RDI only)': '64aa', 'gs rep scasq': '65f348af', 'ss lodsb': '36ac', 'fs gs movsb': '6465a4',
    'mov eax,[eax] (addr32)': '678b00',
    # I7: an FS/GS override with nothing to override, or with a second segment prefix (no SDM rule; FEX and QEMU differ)
    'fs mov rax,rcx': '644889c8', 'gs push rax': '6550', 'gs ret': '65c3', 'fs leave': '64c9', 'gs pushf': '659c', 'fs jmp rel8': '64eb00',
    'fs cs mov rax,[rax]': '642e488b00', 'ds gs mov rax,[rax]': '3e65488b00', 'fs gs mov rax,[rax]': '6465488b00', 'fs fs mov [rax],rcx': '6464894808',
    'fs addr32 mov rax,[eax]': '6467488b00',
    'ss mov rax,[rbx] (#SS or #GP)': '36488b03', 'ds mov rax,[rbp] (#GP or #SS)': '3e488b4500', 'cs mov [rsp+8],rcx': '2e48894c2408',
    'ss mov rax,[rip]': '36488b0500000000', 'es push [rbp+8]': '26ff7508',
    'paddd mm (MMX, I6)': '0ffec1', 'pcmpistri (host helper)': '660f3a63c100', 'maskmovdqu': '660ff7c1', 'f3 66 movss': 'f3660f10c1',
    'insertps m32 zeroing its element': '660f3a21000f', 'aesenc': '660f38dcc1', 'fld st0': 'd9c0', 'vzeroupper': 'c5f877',
    'o16 pushf': '669c', 'o16 leave': '66c9', 'enter level 1': 'c8080001', 'o16 shld': '660fa4c101', 'o16 shrd cl': '660fadc1',
    'prefetchw': '0f0d08', 'prefetch /4 (reserved nop)': '0f1820', 'prefetch reg (reserved nop)': '0f18c0', 'movnti reg': '0fc3c1',
    'pdep': 'c4e2fbf5c1', 'pext': 'c4e2faf5c1', 'andn L=1': 'c4e27cf2c1', 'blsr /0 (#UD)': 'c4e278f3c1', 'vpxor': 'c5f9efc0',
    'rex andn (#UD)': '48c4e278f2c1', 'o16 andn (#UD)': '66c4e278f2c1', 'rep popcnt-like f2 0f b8': 'f20fb8c1',
    'fxsave': '0fae00', 'loop 67-prefix': '67e2fe', 'jecxz': '67e3fe', 'loop o16': '66e2fe', 'rdrand': '0fc7f0', 'rdseed': '0fc7f8',
    'xlat': 'd7', 'movbe': '0f38f000', 'sal /6 alias': 'd1f0',
    'bnd ret': 'f2c3', 'o16 ret': '66c3', 'o16 jmp': '66e9fbffffff', 'bswap r9w (undefined)': '66410fc9', 'bswap ax': '660fc8',
    # FEX branches around the whole access for a zero count (static or dynamic), losing a guest #PF
    'rcl dword [rax],1': 'd110', 'rcr qword [rax],cl': '48d318', 'rcr qword [rax],0': '48c11800', 'rcl byte [rax],cl': 'd210',
    'rcr word [rax],5': '66c11805', 'rcl dword [rax],cl': 'd310', 'rcr dword [rax+rcx*4+8],cl': 'd35c8808', 'rcl byte [rax],32': 'c01020',
    'rcr [rax],imm (c1, rex.w)': '48c11805',
}

# One instruction each, all inside the allowlist; every one must translate completely.
# I3: LOCK on a form that is not lockable is #UD whatever the instruction: a system cut (the host names it, the resident raises
# #UD), whether FEX's decoder took the instruction (cmp, bt, andn) or refused it (a register operand, mov, nop, movs).
LOCK_UD = {
    'lock xchg rax,rcx': 'f04887c1', 'lock add rax,rcx': 'f04801c8', 'lock mov [rax],rcx': 'f0488908', 'lock cmp [rax],1': 'f048833801',
    'lock bt [rax],1': 'f0480fba2001', 'lock nop': 'f090', 'lock movsb': 'f0a4', 'lock andn': 'f0c4e278f2c1', 'lock inc rax': 'f048ffc0',
    'lock cmpxchg rcx,rdx': 'f0480fb1d1', 'lock lea rax,[rax]': 'f0488d00', 'lock movaps': 'f00f2800',
}

ALLOWED = {
    # I3: every lockable memory form with LOCK (one LSE atomic each), cmpxchg8b/16b with or without it
    'lock add [rax],rcx': 'f0480108', 'lock inc [rax]': 'f0ff00', 'lock cmpxchg [rax],ecx': 'f00fb108', 'lock xadd [rax],ecx': 'f00fc108',
    'lock adc [rax],cl': 'f01008', 'lock sbb word [rax],5': '66f0831805', 'lock and [rax],rcx': 'f0482108', 'lock or [rax],ecx': 'f00908',
    'lock xor byte [rax],1': 'f0803001', 'lock sub [rax],rcx': 'f0482908', 'lock dec qword [rax]': 'f048ff08', 'lock not [rax]': 'f0f710',
    'lock bts [rax],rcx': 'f0480fab08', 'lock btr [rax],5': 'f00fba3005', 'lock btc word [rax],cx': '66f00fbb08', 'lock xadd [rax],cl': 'f00fc008',
    'lock cmpxchg16b': 'f0480fc708', 'lock cmpxchg8b': 'f00fc708', 'cmpxchg16b': '480fc708', 'cmpxchg8b': '0fc708',
    'fs lock add [rax],rcx': '64f0480108', 'lock gs or [rax+8],ecx': 'f0650948 08'.replace(' ', ''),
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
    # I1: flag moves, pushf, small integer and BMI forms
    'cld': 'fc', 'std': 'fd', 'clc': 'f8', 'stc': 'f9', 'cmc': 'f5', 'lahf': '9f', 'sahf': '9e', 'pushfq': '9c',
    'leave': 'c9', 'enter': 'c8080000', 'shld': '0fa4c101', 'shrd': '0facc101', 'shld [rax],cl': '480fa508', 'shrd [rax],5': '0fac0805',
    'cmpxchg [rax],ecx': '0fb108', 'cmpxchg [rax],cl': '0fb008', 'cmpxchg rcx,rdx': '480fb1d1', 'xadd [rax],ecx': '0fc108', 'xadd rcx,rdx': '480fc1d1',
    'popcnt': 'f3480fb8c1', 'popcnt [rax]': 'f30fb800', 'o16 popcnt': '66f30fb8c1', 'tzcnt': 'f30fbcc1', 'lzcnt': 'f30fbdc1', 'tzcnt [rax]': 'f3480fbc00',
    'prefetcht0': '0f1808', 'prefetchnta': '0f1800', 'prefetcht1': '0f1810', 'prefetcht2': '0f1818', 'movnti [rax],rcx': '480fc308',
    'andn': 'c4e278f2c1', 'andn [rax]': 'c4e2f8f200', 'bextr': 'c4e270f7c1', 'blsr': 'c4e278f3c9', 'blsmsk': 'c4e278f3d1', 'blsi': 'c4e2f8f3d8',
    'bzhi': 'c4e270f5c1', 'mulx': 'c4e2f3f6c2', 'rorx': 'c4e3fbf0c107', 'sarx': 'c4e2faf7c1', 'shlx': 'c4e2f9f7c1', 'shrx [rax]': 'c4e2fbf700',
    # I7: FS/GS overrides on memory forms; CS/DS/ES/SS prefixes (ignored in 64-bit mode)
    'mov rax,fs:[rax]': '64488b00', 'mov rax,gs:[rax]': '65488b00', 'add gs:[rax+8],rcx': '6548014808', 'mov gs:[0x28],rax': '654889042528000000',
    'mov rax,fs:[rip+0x10]': '64488b0510000000', 'push fs:[rax]': '64ff30', 'pop gs:[rax]': '658f00', 'xchg gs:[rax],rcx': '65488708',
    'movabs rax,gs:[moffs]': '6548a10102030405060708', 'cmpxchg fs:[rax],ecx': '640fb108', 'andn rax,rcx,gs:[rax]': '65c4e2f0f200',
    'lea rax,fs:[rax+8]': '64488d4008', 'rex fs mov rax,[rax]': '4064488b00',
    'cs mov rax,[rax]': '2e488b00', 'ds mov [rax],rcx': '3e488908', 'es add [rax],ecx': '260108', 'ss mov rax,[rsp]': '36488b0424',
    'ss mov rax,[rbp+8]': '36488b4508', 'ds mov rax,[rax+rbp*2]': '3e488b0468', 'ds movabs rax,[moffs]': '3e48a10102030405060708',
    'cs push rax': '2e50', 'ds mov rax,rcx': '3e4889c8', 'cs ds mov rax,[rbx]': '2e3e488b03', 'ss ss mov rax,[rsp+8]': '3636488b442408',
    'cs nop word [rax+rax]': '662e0f1f840000000000',
    # I2: string instructions without a repeat prefix (one element)
    'movsb': 'a4', 'movsq': '48a5', 'o16 cmpsw': '66a7', 'stosq': '48ab', 'stosd': 'ab', 'lodsb': 'ac', 'scasb': 'ae', 'cmpsb': 'a6',
    'fs movsq': '6448a5', 'gs lodsd': '65ad', 'fs cmpsb': '64a6', 'ds movsb': '3ea4', 'es stosb': '26aa',
}

# I3: LOCK NEG ends its block too (one compare-and-swap per pass, exiting to its own RIP on a lost race).
LOCK_NEGS = {'lock neg [rax]': 'f0f718', 'lock neg byte [rax+8]': 'f0f65808', 'lock neg qword fs:[rax]': '64f048f718'}

# I2: REP/REPE/REPNE string instructions end their block (they exit to their own RIP until RCX is 0).
REPS = {'rep movsb': 'f3a4', 'rep movsq': 'f348a5', 'rep stosq': 'f348ab', 'rep stosb': 'f3aa', 'rep lodsw': '66f3ad', 'repe cmpsb': 'f3a6',
        'repne cmpsd': 'f2a7', 'repe scasq': 'f348af', 'repne scasb': 'f2ae', 'fs rep movsq': '64f348a5', 'gs repe cmpsb': '65f3a6'}

# (name, hex, translated-instruction count): control transfers end the block with their normal exit.
BRANCHES = {
    'jmp rel8': 'eb05', 'jmp rel32': 'e905000000', 'jne rel8': '7505', 'je rel32': '0f8405000000', 'call rel32': 'e805000000',
    'ds jne rel8 (hint, ignored)': '3e7505', 'gs jmp [rax+8]': '65ff6008', 'fs call [rip+0x10]': '64ff1510000000',
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
        self.assertEqual([r['guest_rip'] for r in d['code']['relocations'] if r['kind'] == 'guest_rip_move' and r['register'] == 0],
                         [hex(RIP + pre_len)], name)
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

    def test_lock_on_forms_that_are_not_lockable_is_ud(self):
        for name, x in LOCK_UD.items():
            with self.subTest(name):
                self.cut_after_two(name, x, 'system_instruction')
                d, _ = run(x + PRE)
                self.assertIn('not lockable (#UD)', d['guest']['end']['detail'], name)

    def test_lock_neg_ends_its_block(self):
        for name, x in LOCK_NEGS.items():
            with self.subTest(name):
                d, rc = run(PRE + x + PRE)
                g = d['guest']
                self.assertEqual((rc, g['instructions_translated'], g['end']['reason']), (0, 3, 'block_ending_instruction'), (name, g['end']))
                at = RIP + len(PRE) // 2
                moves = sorted(r['guest_rip'] for r in d['code']['relocations'] if r['kind'] == 'guest_rip_move' and r['register'] == 0)
                self.assertEqual(moves, sorted([hex(at), hex(at + len(x) // 2)]), name)    # its own RIP (retry) and the next
                sites = [(s['kind'], s['rmw'], s['intent']) for s in d['sites']]
                self.assertEqual(sites, [('load', 'load', 'write'), ('atomic', 'store', 'write')], name)

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

    def test_rep_strings_end_their_block(self):
        for name, x in REPS.items():
            with self.subTest(name):
                d, rc = run(PRE + x + PRE)
                g = d['guest']
                self.assertEqual(rc, 0, name)
                self.assertEqual((g['instructions_translated'], g['end']['reason']), (3, 'block_ending_instruction'), (name, g['end']))
                at = RIP + len(PRE) // 2
                self.assertEqual(g['end']['rip'], hex(at + len(x) // 2), name)
                # Literal exits: the instruction's own RIP (next pass) and the next instruction's (done).
                moves = sorted(r['guest_rip'] for r in d['code']['relocations'] if r['kind'] == 'guest_rip_move' and r['register'] == 0)
                self.assertEqual(moves, sorted([hex(at), hex(at + len(x) // 2)]), name)
                kinds = [s['kind'] for s in d['sites'] if s['kind'] in ('load', 'store')]
                self.assertEqual(kinds, {'mov': ['load', 'store'], 'sto': ['store'], 'lod': ['load'], 'cmp': ['load', 'load'],
                                         'sca': ['load']}[name.split()[-1][:3]], name)
                self.assertTrue(all(s['intent'] == ('write' if s['kind'] == 'store' else 'read') and s['rmw'] == 'none'
                                    for s in d['sites'] if s['kind'] in ('load', 'store')), name)

    def test_rip_relative_after_an_immediate(self):
        # [rip + disp] is relative to the end of the instruction, immediates included (SDM vol. 2 §2.2.1.6): shld/shrd imm8
        # (the pinned QEMU 7.2 is one byte low here), rorx imm8 and add imm8, each at 0x1000 with disp 0x100.
        for name, x, n in (('shld', '4c0fa4350001000020', 9), ('shrd', '4c0fac350001000005', 9), ('rorx', 'c4e3fbf0050001000005', 10),
                           ('add', '4883050001000005', 8)):
            with self.subTest(name):
                d, rc = run(x, 0x1000)
                moves = [int(r['guest_rip'], 16) for r in d['code']['relocations'] if r['kind'] == 'guest_rip_move' and r['register'] != 0]
                self.assertIn(0x1000 + n + 0x100, moves, (name, moves))

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
                self.assertEqual(o['pre_nzcv'], d['state_abi']['state_offsets']['natemu_pre_nzcv_register'])   # x25
            d, _ = run(PRE + SYSTEM['cli'], RIP, '--object', str(obj))
            self.assertEqual(check_sites.END_REASONS[check_sites.read_object(obj)['end_reason']], 'system_instruction')

    def test_max_instructions_still_works(self):
        d, rc = run(PRE + PRE, RIP, '--max-insts', '3')
        self.assertEqual((rc, d['guest']['instructions_translated'], d['guest']['end']['reason']), (0, 3, 'max_instructions'))

    def test_block_over_32k_after_far_branch_restart(self):
        # 1500 x `mov qword ptr [rbx+8], rax` (48 89 43 08): split checks and stubs push the block past tbnz's 32 KiB
        # range, FEX restarts with far branches; the PC table must hold one entry per instruction, in order, and the
        # checks (far form) must verify.
        import verify_precision
        d, rc = run('48894308' * 1500, RIP, '--max-insts', '1500')
        self.assertEqual((rc, d['guest']['instructions_translated']), (0, 1500))
        self.assertGreater(d['code']['layout']['code_region_end'], 32768)
        entries = d['pc_table']['entries']
        self.assertEqual([e['guest_offset'] for e in entries], [0] + [4 * k for k in range(1500)])
        # The block entry and its first instruction share native offset 4 (no stop check before it, E1b); then one
        # strictly increasing offset per instruction.
        native = [e['native_offset'] for e in entries]
        self.assertEqual(native[:2], [4, 4])
        self.assertEqual(native[1:], sorted(set(native[1:])))
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
        for x in (SYSTEM['mov cr3,rax'], SYSTEM['cli'], UNSUPPORTED['repne movsb']):
            d, rc = run(PRE + x + PRE, profile=None)
            self.assertNotIn(d['guest']['end']['reason'], ('system_instruction', 'unsupported_form'))
            self.assertNotIn('cut_opcode', d['guest']['end'])


# The code touches no emulator data, and its only ways out are the resident's: a literal exit is exactly a guest-RIP move
# into x0 then `svc #1` (the resident links it), a computed one `mov x0, xR` then the placeholder `b .` at a
# dispatch_branch relocation (the resident relocates it to its dispatcher). The only other `svc`s are the 128/64
# division's (verify_precision.py P11 checks its #DE branch); there is no `bl` (no stop check: E1b unlinks instead).
def literal_exits(d):
    return sorted(r['guest_rip'] for r in d['code']['relocations'] if r['kind'] == 'guest_rip_move' and r['register'] == 0)


def check_exits(test, d, tmp, what):
    test.assertEqual({r['kind'] for r in d['code']['relocations']} - {'guest_rip_literal', 'guest_rip_move', 'dispatch_branch'}, set(), what)
    test.assertEqual(d['state_abi']['state_pointer_loads'], [], what)
    test.assertIsNone(d['code']['layout']['end_marker_offset'], what)
    code = bytes.fromhex(d['code']['hex'])
    end = d['code']['layout']['code_region_end']
    dis = check_sites.disassemble(code, end, tmp)
    lines = sorted(dis.items())
    moves = {r['offset'] for r in d['code']['relocations'] if r['kind'] == 'guest_rip_move' and r['register'] == 0}
    dispatch = {r['offset'] for r in d['code']['relocations'] if r['kind'] == 'dispatch_branch'}
    exits = 0
    for i, (o, t) in enumerate(lines):
        if t == 'svc #0x1':
            exits += 1
            k = i - 1
            while re.fullmatch(r'movk [wx]0, #0x[0-9a-f]+, lsl #\d+', lines[k][1]):
                k -= 1
            test.assertIn(lines[k][0], moves, (what, o))
            test.assertRegex(lines[k][1], r'^(mov [wx]0, #|orr [wx]0, [wx]zr, #)', (what, o))
        elif o in dispatch:
            exits += 1
            test.assertEqual(t, f'b {o:#x}', (what, o))
            test.assertRegex(lines[i - 1][1], r'^mov x0, x([1-9]|[12]\d|30)$', (what, o))
        else:
            test.assertIsNone(re.match(r'(blr|br|ret|bl)\s', t), (what, o, t))
            test.assertIsNone(re.match(r'svc\s', t) if t not in ('svc #0x3', 'svc #0x4') else None, (what, o, t))
            # no frame or stack access but a guest-state load of the FS/GS base (I7: ldr xN, [x28, #0x3e0|#0x3e8])
            if not re.fullmatch(r'ldr x\d+, \[x28, #0x3e[08]\]', t):
                test.assertIsNone(re.search(r'\[(x28|sp)\b', t), (what, o, t))
            test.assertIsNone(re.match(r'ldr\w* \w+, 0x', t), (what, o, t))         # no literal load
            if t.lower() != 'mrs x25, nzcv':
                test.assertIsNone(re.match(r'\w+ [wx]25\b', t), (what, o, t))       # x25: only the pre-NZCV save
    test.assertTrue(exits, what)
    return exits, '\n'.join(f'{o:4x}: {t}' for o, t in lines)


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
                    self.assertEqual(n, 2 if name in ('jne rel8', 'ds jne rel8 (hint, ignored)', 'je rel32', 'loop rel8', 'loope rel8', 'loopne rel8', 'jrcxz rel8') else 1, name)   # taken and fall-through gates
            for name, x in (('fall off the window', PRE), ('cut before cr3 write', PRE + SYSTEM['mov cr3,rax'])):
                with self.subTest(name):
                    d, rc = run(x)
                    n, _ = check_exits(self, d, tmp, name)
                    self.assertEqual(n, 1)

    def test_exit_sequence_text(self):
        # the literal exit form for a conditional branch: two link points, no frame access
        d, _ = run(PRE + '7505')
        with tempfile.TemporaryDirectory() as t:
            n, text = check_exits(self, d, Path(t), 'jne')
        self.assertEqual(n, 2)
        self.assertEqual(len(re.findall(r'svc #0x1', text)), 2)
        self.assertNotIn('x28', text)
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
    """S2c0: a jcc back to its own block's entry exits like any taken edge; E1b: no block has a stop check."""
    STOP = 0x94000000   # bl . (E1's stop check, gone)

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
                        lits = literal_exits(d)
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
                    lits = literal_exits(d)
                    self.assertIn(hex(RIP), lits)
                    self.assertIn(hex(RIP + 9), lits)
                    code = bytes.fromhex(d['code']['hex'])
                    end = d['code']['layout']['code_region_end']
                    lit = [(r['offset'], r['size']) for r in d['code']['relocations'] if r['size']]
                    dis = check_sites.disassemble(code, end, Path(t), lit)
                    self.assertEqual(vp.entry_and_branches(d, dis, sorted(dis)), [])   # P12, P13

    def test_no_block_has_a_stop_check(self):
        for name, x in list(BRANCHES.items()) + [('add', '4801d8'), ('push', '50'), ('mov [rax],rcx', '488908'), ('div', '48f7f1')]:
            with self.subTest(name):
                d, rc = run(x)
                self.assertEqual(rc, 0)
                code = bytes.fromhex(d['code']['hex'])
                self.assertEqual(d['code']['layout']['entries'][0]['body_native_offset'], 4)   # the entry is the body
                words = [int.from_bytes(code[o:o + 4], 'little') for o in range(4, d['code']['layout']['code_region_end'], 4)]
                self.assertNotIn(self.STOP, words)

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
            for what, mutate, why in (
                    ('a stop check at the entry', lambda m: m.update({4: 'bl 0x4'}), 'call, return or indirect branch'),
                    ('a bl later', lambda m: m.update({0x14: 'bl 0x4'}), 'call, return or indirect branch'),
                    ('a return', lambda m: m.update({0x14: 'ret'}), 'call, return or indirect branch'),
                    ('the old stop store', lambda m: m.update({0x14: 'str xzr, [x28, #0xec0]'}), 'emulator data'),
                    ('a frame load', lambda m: m.update({0x14: 'ldr x1, [x28, #0xb68]'}), 'emulator data'),
                    ('a stack push', lambda m: m.update({0x14: 'str x30, [sp, #-0x10]!'}), 'emulator data'),
                    ('a literal load', lambda m: m.update({0x14: 'ldr x0, 0x30'}), 'literal load'),
                    ('backward branch to the entry body', lambda m: m.update({0x28: 'b.ne 0x8'}), 'not a forward branch')):
                bad = dict(dis)
                mutate(bad)
                problems = vp.entry_and_branches(d, bad, sorted(bad))
                self.assertTrue(any(why in m for m in problems), (what, problems))


if __name__ == '__main__':
    unittest.main()

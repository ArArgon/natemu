"""S1c checks: profile slice1 of fexc (block cutting at an allowlist, single-gate exits, profile record).

Run: python3 -m unittest tools/fexc/test_slice1.py      (binary: $FEXC, default the series20 build)

The x86 bytes below are assembled by hand and only drive the compiler; every expectation (where the block ends, why)
comes from the instruction's architectural class, never from fexc's output. The cut position is also compared with a
second compile of just the instructions before it: the code must be identical, so nothing of the cut instruction (or
of FEX's mis-decode of it) leaks into the block.
"""
import collections
import itertools
import json
import os
import random
import re
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import check_sites  # noqa: E402  (disassemble, object reader)
import scan_skips  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1])); from raw_root import RAW_ROOT  # noqa: E402
FEXC = os.environ.get('FEXC', str(RAW_ROOT / 'fexc-native-20261026-01/series20/obj/fexc'))
REPO = HERE.parents[1]
RIP = 0x102000
PRE = '4801d8' + '4831c9'   # add rax, rbx ; xor rcx, rcx

# name -> hex. Architecturally: privilege/descriptor/MSR/segment/interrupt/IO/TLB/cache/flag-control, or VM-owned values.
SYSTEM = {
    'enter': 'c8080000', 'enter level 1': 'c8080001', 'enter16': '66c8080001',
    'vcvtps2ph m': 'c4e37d1d3004', 'iretw': '66cf', 'retfw': '66cb', 'call far16': '66ff18', 'jmp far16': '66ff28',
    'maskmovq': '0ff7c1',
    'o16 shrd cl': '660fadc1', 'o16 shld cl': '660fa5d3',
    'rcr qword [rax],cl': '48d318', 'rcr qword [rax],0': '48c11800', 'rcl byte [rax],cl': 'd210',
    'rcl dword [rax],cl': 'd310', 'rcr dword [rax+rcx*4+8],cl': 'd35c8808', 'rcl byte [rax],32': 'c01020', 'rcr qword [rax],64': '48c11840',
    # X1c: Reserved-NOP cells (owner decision: NOPs, the resident's RESERVED_NOP)
    '0f 0d /0 m': '0f0d00', 'prefetch /4 m': '0f1820', 'prefetch reg': '0f18c0', 'f3 prefetchnta': 'f30f1800', '0f 19 m': '0f1900',
    '66 0f 1a m (bndmov form)': '660f1a00', 'f2 0f 1b reg': 'f20f1bc0', '0f 1c reg': '0f1cc0', '66 cldemote': '660f1c00', '0f 1d m': '0f1d00',
    # I9: #UD the host decodes (MOVBE with a register operand; CET_SS not advertised); 0f 38 f6 with F2 before F3 (the host's
    # stop) or F2 last (blank)
    'movbe rax,rcx (#UD)': '480f38f0c1', 'movbe cx,ax (#UD)': '660f38f1c1', 'wrussq (#UD)': '66480f38f508', 'wrssq (#UD)': '480f38f6c1',
    'f2 f3 0f 38 f6': 'f2f30f38f6c1', 'f2 0f 38 f6': 'f20f38f6c1', 'f3 f2 0f 38 f6': 'f3f20f38f6c1',
    'mov cr0,rax': '0f22c0', 'mov cr3,rax': '0f22d8', 'mov cr4,rax': '0f22e0', 'mov cr8,rax': '440f22c0',
    'mov rax,cr0': '0f20c0', 'mov rax,cr3': '0f20d8', 'mov rax,cr4': '0f20e0', 'mov dr7,rax': '0f23f8', 'mov rax,dr0': '0f21c0',
    'lgdt [rax]': '0f0110', 'lidt [rax]': '0f0118', 'sgdt [rax]': '0f0100', 'ltr ax': '0f00d8', 'lldt ax': '0f00d0',
    'wrmsr': '0f30', 'rdmsr': '0f32', 'rdpmc': '0f33',
    'fxsave [rax] (I5b)': '0fae00',
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
    # S3: rdrand (/6), rdseed (/7), rdpid (f3 /7): register forms of group 9
    'rdrand': '0fc7f0', 'rdrand ax': '660fc7f0', 'rdseed': '0fc7f8', 'rdpid': 'f30fc7f8',
    # The AVX2 gathers (VEX.66.0F38 90..93, VSIB [rdi + vreg1], mask vreg2): the resident's element-by-element transaction.
    'vpgatherdd ymm': 'c4e26d90040f', 'vpgatherdq xmm': 'c4e2e990040f', 'vpgatherqd xmm': 'c4e26991040f',
    'vpgatherqq ymm': 'c4e2ed91040f', 'vgatherdps xmm': 'c4e26992040f', 'vgatherdpd ymm': 'c4e2ed92040f',
    'vgatherqps ymm index': 'c4e26d93040f', 'vgatherqpd xmm': 'c4e2e993040f',
    # No-stop change: the masked moves, the resident's (VMASKMOVPS/PD, VPMASKMOVD/Q, VMASKMOVDQU, MASKMOVDQU).
    'vmaskmovps ld': 'c4e2752c00', 'vmaskmovps st': 'c4e2752e08', 'vmaskmovdqu': 'c5f9f7ca', 'maskmovdqu': '660ff7c1',
    'vpmaskmovq ld': 'c4e2f58c00', 'vpmaskmovd st': 'c4e2758e08',
}

# name -> hex. Decodable instructions that are not slice-1 forms, and admitted opcodes with an excluded prefix or operand.
UNSUPPORTED = {
    'rep xchg r8,rax (not pause)': 'f34190',
    'lfence modrm e9': '0faee9',
    # I3: LOCK with a REP prefix (XACQUIRE/XRELEASE without HLE), an operand-size prefix on cmpxchg8b/16b (no SDM meaning)
    'rep lock add [rax],rcx': 'f3f0480108', 'repne lock xadd [rax],ecx': 'f2f00fc108', 'o16 cmpxchg8b': '660fc708',
    # I2: string forms outside the allowlist (REPNE only on cmps/scas, never with REP)
    'repne movsb': 'f2a4', 'repne stosq': 'f248ab', 'rep repne cmpsb': 'f3f2a6',
    'f3 66 movss': 'f3660f10c1',
    # I8: AVX forms FEX cannot do exactly (I10: the encodings x86 makes #UD are in UD)
    'o16 shld by 17': '660fa4c111',
    'movnti reg': '0fc3c1',
    'rep popcnt-like f2 0f b8': 'f20fb8c1',
    'sal /6 alias': 'd1f0',
    'bnd loop (BND is not a LOOP prefix)': 'f2e2fe',
}

# I10: encodings the presented CPU raises #UD for, reserved or not advertised: a system cut with `cut_ud` (the host's UD).
UD = {
    # X1c: 0f 0d with a register operand (PREFETCHW takes m8; pinned QEMU: #UD)
    'prefetchw reg': '0f0dc8', '66 0f 0d reg': '660f0dc0',
    'psllq mm /3': '0f73d801', 'psrlw mm, m (mmx shift, memory)': '0f7110', 'pi2fd (3DNow!)': '0f0fc10d',
    'vaesenc L=1 (VAES)': 'c4e275dcc2', 'vpclmulqdq L=1': 'c4e37544c200', 'vpcmpistri L=1': 'c4e37d63c112', 'vmovd L=1': 'c5fd6ec6',
    'vpermq L=0': 'c4e3f900c11b', 'vpermq W0': 'c4e37d00c11b', 'vmovaps vvvv': 'c5f428c1', 'vpdpbusd (VNNI)': 'c4e27950e1',
    'andn L=1': 'c4e27cf2c1', 'blsr /0': 'c4e278f3c1', 'vmovhlps L=1': 'c5fc12c1', 'f3 movbe': 'f30f38f000', 'f3 movbe store': 'f30f38f108',
    'vprotd (XOP)': '8fe878c2c005', 'pop /1 (reserved)': '8fc8', 'evex vpxord': '62f17d48efc0', 'xbegin (RTM)': 'c7f800000000',
    'xabort (RTM)': 'c6f805', 'mov /1 (reserved)': 'c708', 'xstorerng (PadLock)': '0fa7c0', 'rep xcryptecb (PadLock)': 'f30fa7c8',
    'montmul (PadLock)': 'f30fa6c0', 'incsspq': 'f3480faee8', 'incsspd': 'f30faee8', 'daa': '27', 'pusha': '60', 'aam': 'd40a',
    'group 1 82': '82c001', 'fe /2': 'fed0', 'ff /7': 'fff8', 'bt group /0': '0fba0001',
    # WAITPKG (not advertised): 66/F3/F2 0f ae /6 register (the earlier `o16 mfence`/`rep mfence` refusals are these)
    'tpause eax': '660faef0', 'umonitor rax': 'f30faef0', 'umwait ecx': 'f20faef1', 'tpause rex': '66410faef3',
}

# One instruction each, all inside the allowlist; every one must translate completely.
# I3: LOCK on a form that is not lockable is #UD whatever the instruction: a system cut (the host names it, the resident raises
# #UD), whether FEX's decoder took the instruction (cmp, bt, andn) or refused it (a register operand, mov, nop, movs).
LOCK_UD = {
    'lock xchg rax,rcx': 'f04887c1', 'lock add rax,rcx': 'f04801c8', 'lock mov [rax],rcx': 'f0488908', 'lock cmp [rax],1': 'f048833801',
    'lock bt [rax],1': 'f0480fba2001', 'lock nop': 'f090', 'lock movsb': 'f0a4', 'lock andn': 'f0c4e278f2c1', 'lock inc rax': 'f048ffc0',
    'lock adcx ecx,[rax]': 'f0660f38f608', 'lock adox rcx,[rax]': 'f0f3480f38f608', 'lock cmpxchg rcx,rdx': 'f0480fb1d1', 'lock lea rax,[rax]': 'f0488d00', 'lock movaps': 'f00f2800', 'lock endbr64': 'f0f30f1efa',
}

# I8: a 66, F2 or F3 prefix before VEX, or a REX right before it, is #UD whatever the instruction (a system cut, the host's vex_ud).
VEX_UD = {'rex andn': '48c4e278f2c1', 'o16 andn': '66c4e278f2c1', 'o16 vzeroupper': '66c5f877', 'f3 vaddps': 'f3c5f458c2',
          'f2 vpbroadcastb': 'f2c4e27d78ff'}

ALLOWED = {
    'vinsertps m32 zeroing its element': 'c4e37921001f',

    'insertps m32 zeroing its element': '660f3a21000f',

    # No-stop change: scalar VEX forms with VEX.L = 1 (LIG), 16-bit BSWAP (FEX zeroes the low half, as the AMD machine),
    # 16-bit SHLD/SHRD by an imm8 up to 16, RCL/RCR m by a static nonzero count.
    'vaddss L=1 (LIG)': 'c5f658c2', 'vroundss L=1 (LIG)': 'c4e3750ac204', 'vfmadd231ss L=1': 'c4e275b9c2', 'vcmpss L=1': 'c5f6c2c201',
    'bswap r9w (undefined)': '66410fc9', 'bswap ax': '660fc8', 'o16 shld': '660fa4c101', 'o16 shrd by 16': '660facc110',
    'rcl dword [rax],1': 'd110', 'rcr word [rax],5': '66c11805', 'rcr [rax],imm (c1, rex.w)': '48c11805', 'rcl qword [rax],33': '48c11021',
    # X1c: the hints PREFETCHW m8 and CLDEMOTE m8 (SDM: no #UD without their CPUID bits; CLDEMOTE "treated as a NOP")
    'prefetchw [rax]': '0f0d08', 'cldemote [rax]': '0f1c00',
    # I10: 0x67 (32-bit addresses), XLAT, the CET hint space 0f 1e as NOPs (ENDBR, RDSSP)
    'mov eax,[eax] (addr32)': '678b00', 'fs addr32 mov rax,[eax]': '6467488b00', 'addr32 lodsb': '67ac', 'addr32 vmovdqu': '67c5fe6f00',
    'addr32 lea rax,[eax+ebx*4+8]': '67488d449808', 'addr32 mov eax,[eip+8]': '678b0508000000', 'addr32 movabs eax,[moffs32]': '67a144332211',
    'addr32 lock add [esi],eax': '67f00106', 'addr32 fld dword [eax]': '67d900', 'addr32 movups xmm0,[eax]': '670f1000',
    'addr32 bt [eax],5': '670fba2005', 'addr32 crc32 eax,dword [eax]': '67f20f38f100', 'addr32 mulx': '67c4e2f3f6c0',
    'xlat': 'd7', 'addr32 xlat': '67d7', 'gs xlat': '65d7', 'ds xlat': '3ed7',
    'endbr64': 'f30f1efa', 'endbr32': 'f30f1efb', 'rdsspq rax': 'f3480f1ec8', 'rdsspd eax': 'f30f1ec8', 'nop 0f 1e [rax]': '0f1e00',
    'o16 0f 1e': '660f1ec0', 'f2 0f 1e [rax+0x100]': 'f20f1e8000010000',
    # I9: MOVBE m, MMX, AES-NI/PCLMULQDQ (legacy and VEX.128), PDEP/PEXT, FFREEP
    'movbe eax,[rax]': '0f38f000', 'movbe [rax],rcx': '480f38f108', 'movbe ax,[rax]': '660f38f000', 'paddd mm0,mm1': '0ffec1',
    'paddb mm0,[rax]': '0ffc00', 'punpcklbw mm1,[rax]': '0f6008', 'movq mm0,rax': '480f6ec0', 'movd [rax],mm1': '0f7e08', 'emms': '0f77',
    'pshufw mm1,mm2,0x1b': '0f70ca1b', 'pinsrw mm1,[rax],1': '0fc40801', 'pextrw eax,mm1,2': '0fc5c102', 'psllq mm3,7': '0f73f307',
    'movntq [rax],mm1': '0fe708', 'pshufb mm0,mm1': '0f3800c1', 'palignr mm0,[rax],3': '0f3a0f0003', 'cvtpi2ps xmm1,mm2': '0f2aca',
    'cvtpd2pi mm1,[rax]': '660f2d08', 'movq2dq xmm1,mm2': 'f30fd6ca', 'movdq2q mm1,xmm2': 'f20fd6ca',
    'aesenc xmm1,xmm2': '660f38dcca', 'aesdeclast xmm1,[rax]': '660f38df08', 'aesimc': '660f38dbca', 'aeskeygenassist': '660f3adfca07',
    'pclmulqdq': '660f3a44ca11', 'vaesenc': 'c4e271dcc2', 'vaesimc': 'c4e279dbca', 'vpclmulqdq': 'c4e37144c201',
    'vaeskeygenassist': 'c4e379dfca07', 'pdep': 'c4e2fbf5c1', 'pext': 'c4e2faf5c1', 'pext ecx,edx,[rax]': 'c4e26af508', 'ffreep st1': 'dfc1',
    # ADX (November 3, CPUID.7.0:EBX.ADX[19] advertised): ADCX/ADOX r32/r64, register and memory, 0x67, a segment prefix
    'adcx eax,ecx': '660f38f6c1', 'adcx rax,rcx': '66480f38f6c1', 'adcx r9,[rax]': '664c0f38f608', 'adox edx,[rbx+8]': 'f30f38f65308',
    'adox rax,rcx': 'f3480f38f6c1', 'adox r15d,[eax] addr32': '67f3440f38f638', 'gs adcx rcx,[rax]': '6566480f38f608',
    '66 f3 adox (66 ignored)': '66f30f38f6c1', 'f3 66 adox rcx,[rax]': 'f366480f38f608',
    # SHA (November 3, CPUID.7.0:EBX.SHA[29] advertised): all seven, register and memory, REX, 0x67, a segment prefix
    'sha1nexte': '0f38c8ca', 'sha1msg1 [rax]': '0f38c908', 'sha1msg2 xmm9,xmm2': '440f38caca', 'sha256rnds2 xmm1,xmm10': '410f38cbca',
    'sha256rnds2 xmm1,[r8+rcx*8+16]': '410f38cb4cc810', 'sha256msg1 rex.w': '480f38ccca', 'sha256msg2 [eax] addr32': '670f38cd08',
    'sha1rnds4 xmm1,xmm2,0': '0f3accca00', 'sha1rnds4 xmm15,[rip+8],0xff': '440f3acc3d08000000ff', 'fs sha1msg1': '640f38c908',
    # I6b: the resident's helper forms (`svc #X87`, `svc #STRING`)
    'pcmpistri xmm0,[rax],0x12': '660f3a630012', 'rex.w pcmpestri': '66480f3a61c14c', 'pcmpestrm xmm1,xmm2,0x40': '660f3a60ca40',
    'vpcmpistrm xmm1,[rax+1],0': 'c4e37962480100', 'vex.w1 vpcmpestri': 'c4e3f961ca01', 'fsin': 'd9fe', 'fcos': 'd9ff', 'fsincos': 'd9fb',
    'fptan': 'd9f2', 'fpatan': 'd9f3', 'f2xm1': 'd9f0', 'fyl2x': 'd9f1', 'fyl2xp1': 'd9f9', 'fscale': 'd9fd', 'fprem': 'd9f8',
    'fprem1': 'd9f5', 'fxtract': 'd9f4',
    # I8: AVX, AVX2, FMA, F16C (VEX)
    'vzeroupper': 'c5f877', 'vzeroall': 'c5fc77', 'vpxor': 'c5f9efc0', 'vmovd xmm7,esi': 'c5f96efe', 'vpbroadcastb ymm7,xmm7': 'c4e27d78ff',
    'vmovdqu ymm1,[rdi]': 'c5fe6f0f', 'vmovdqu [rdx],ymm1': 'c5fe7f0a', 'vmovaps ymm1,[rax]': 'c5fc2808', 'vmovdqa xmm1,[rax]': 'c5f96f08',
    'vpcmpeqb ymm0,ymm7,ymm1': 'c5c574c1', 'vpmovmskb eax,ymm0': 'c5fdd7c0', 'vfmadd231ps ymm0,ymm1,[rax]': 'c4e275b800',
    'vcvtph2ps ymm1,xmm2': 'c4e27d13ca', 'vcmpps ymm1,ymm2,[rax],30': 'c5ecc2081e', 'vpermq ymm1,ymm2,27': 'c4e3fd00ca1b',
    'vbroadcastss ymm1,[rax]': 'c4e27d1808', 'vpinsrd xmm1,xmm2,[rax],2': 'c4e369220802', 'vextracti128 [rax],ymm1,1': 'c4e37d390801',
    'fs vmovdqu ymm1,[rax]': '64c5fe6f08', 'vmovntdq [rax],ymm1': 'c5fde708',
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
    '66 rex.w shld cl': '66480fa5d3', '66 rex.w shrd cl': '66480fadd3',
    'o16 pushf': '669c', 'o16 leave': '66c9', 'leave': 'c9', 'shld': '0fa4c101', 'shrd': '0facc101', 'shld [rax],cl': '480fa508', 'shrd [rax],5': '0fac0805',
    'addr32 bt [eax],ecx': '670fa308', 'addr32 bts [eax],rcx': '67480fab08',
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
    # series27: Intel's class (SDM Vol. 3A 4.5.1): SS on a DS-default operand (#SS), CS/DS/ES on an RSP/RBP one (#GP)
    'ss mov rax,[rbx]': '36488b03', 'ds mov rax,[rbp]': '3e488b4500', 'cs mov [rsp+8],rcx': '2e48894c2408',
    'ss mov rax,[rip]': '36488b0500000000', 'es push [rbp+8]': '26ff7508', 'ds btr [rbp+0x540],rdx (Linux)': '3e480fb39540050000',
    # series47-seg (owner decisions, November 3; the AMD machine): null prefixes ignored, the last FS/GS applies, FS/GS with
    # no overridable operand ignored
    'fs stosb (ES:RDI only)': '64aa', 'ss lodsb': '36ac', 'fs gs movsb': '6465a4', 'ss xlat': '36d7',
    'fs mov rax,rcx': '644889c8', 'gs push rax': '6550', 'fs leave': '64c9', 'gs pushf': '659c',
    'fs cs mov rax,[rax]': '642e488b00', 'ds gs mov rax,[rax]': '3e65488b00', 'fs gs mov rax,[rax]': '6465488b00', 'fs fs mov [rax],rcx': '6464894808',
    'ss ds mov rax,[rbx]': '363e488b03', 'ss movabs rax,[moffs]': '3648a10102030405060708',
    'fld st0 (I6, reduced precision)': 'd9c0',
    # I2: string instructions without a repeat prefix (one element)
    'movsb': 'a4', 'movsq': '48a5', 'o16 cmpsw': '66a7', 'stosq': '48ab', 'stosd': 'ab', 'lodsb': 'ac', 'scasb': 'ae', 'cmpsb': 'a6',
    'fs movsq': '6448a5', 'gs lodsd': '65ad', 'fs cmpsb': '64a6', 'ds movsb': '3ea4', 'es stosb': '26aa',
}

# I3: LOCK NEG ends its block too (one compare-and-swap per pass, exiting to its own RIP on a lost race).
LOCK_NEGS = {'lock neg [rax]': 'f0f718', 'lock neg byte [rax+8]': 'f0f65808', 'lock neg qword fs:[rax]': '64f048f718'}

# I2: REP/REPE/REPNE string instructions end their block (they exit to their own RIP until RCX is 0).
REPS = {'rep movsb': 'f3a4', 'rep movsq': 'f348a5', 'rep stosq': 'f348ab', 'rep stosb': 'f3aa', 'rep lodsw': '66f3ad', 'repe cmpsb': 'f3a6',
        'repne cmpsd': 'f2a7', 'repe scasq': 'f348af', 'repne scasb': 'f2ae', 'fs rep movsq': '64f348a5', 'gs repe cmpsb': '65f3a6',
        'addr32 rep movsb': '67f3a4', 'addr32 rep stosq': '67f348ab', 'addr32 repne scasb': '67f2ae', 'fs addr32 rep movsd': '6467f3a5',
        'gs (ignored, series47-seg) rep scasq': '65f348af'}

# (name, hex, translated-instruction count): control transfers end the block with their normal exit.
BRANCHES = {
    'jmp rel8': 'eb05', 'jmp rel32': 'e905000000', 'jne rel8': '7505', 'je rel32': '0f8405000000', 'call rel32': 'e805000000',
    'ds jne rel8 (hint, ignored)': '3e7505', 'fs jmp rel8 (ignored as by processors, R2 review)': '64eb00', 'gs jb rel8': '657205', 'fs gs jmp rel32': '6465e905000000', 'gs jmp [rax+8]': '65ff6008', 'fs call [rip+0x10]': '64ff1510000000',
    'loop rel8': 'e205', 'loope rel8': 'e105', 'loopne rel8': 'e005', 'jrcxz rel8': 'e305',
    # I10: 0x67: ECX for LOOP/JECXZ, ignored by direct branches (a relaxed `addr32 call`), the memory operand's address
    'addr32 loop': '67e205', 'jecxz': '67e305', 'addr32 call rel32': '67e805000000', 'addr32 jmp rel8': '67eb05', 'addr32 jne rel32': '670f8505000000',
    'addr32 call [eax]': '67ff10', 'addr32 jmp [eip+8]': '67ff2508000000', 'addr32 ret': '67c3',
    'o16 call rax': '66ffd0', 'o16 jmp [rax]': '66ff20', 'o16 ret': '66c3', 'o16 ret imm16': '66c21000',
    # X1c: BND (F2) on near branches, ignored without MPX (owner decision, November 2)
    'bnd ret': 'f2c3', 'bnd jmp rel8': 'f2eb05', 'bnd call rel32': 'f2e805000000', 'bnd jne rel8': 'f27505', 'bnd jmp rax': 'f2ffe0',
    # 66 on a near direct branch: ignored as on Intel, rel8/rel32 kept (patch 0029; SDM Vol.2 CALL/JMP/Jcc, Appendix A "f64")
    'o16 jmp rel32': '66e905000000', 'o16 call rel32': '66e805000000', 'o16 jne rel32': '660f8505000000', 'o16 jne rel8': '667505',
    'o16 jmp rel8': '66eb05', 'o16 loop': '66e205', 'o16 jrcxz': '66e305', 'o16 rex.w call rel32': '6648e805000000', 'fs o16 jb rel32': '64660f8205000000',
    'ret': 'c3', 'gs ret (ignored, series47-seg)': '65c3', 'rep ret': 'f3c3', 'ret 8': 'c20800', 'jmp rax': 'ffe0', 'call rax': 'ffd0', 'jmp [rax]': 'ff20', 'call [rax]': 'ff10',
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

    def test_invalid_avx_encodings_are_not_gated(self):
        # I8 review: an encoding x86 makes #UD (VPGATHERDD with a register operand, VAESENC with VEX.L = 1) is refused under
        # the AVX gating too, never a gated cut (#NM under TS would be wrong); a valid refused form is gated.
        # (I8 re-review: VPGATHERDD without a SIB byte, or with two of destination, mask and index the same register: #UD too.)
        # I10: those encodings are delivered #UD (`cut_ud`), still never gated.
        for hexs, gated in (('c4e27d90c1', False), ('c4e27ddcc2', False), ('c4e27d9000', False), ('c4e27d90040f', False),
                            ('c4e26d900c08', False), ('c4e26d901410', False), ('c4e26d900408', True), ('c4e279dcc2', True)):
            with self.subTest(hexs):
                d, _ = run(hexs + '90', RIP, '--avx-gated')
                e = d['guest']['end']
                self.assertEqual((e['reason'], e.get('cut_gated', False), e.get('cut_ud', False)),
                                 ('system_instruction', True, False) if gated else ('system_instruction', False, True))

    def test_reserved_and_unadvertised_forms_are_ud(self):
        for name, x in UD.items():
            for extra in ((), ('--avx-gated',), ('--x87-gated',), ('--sse-gated',)):
                with self.subTest(name, extra=extra):
                    d, rc = run(PRE + x + PRE, RIP, *extra)
                    g = d['guest']
                    self.assertEqual((g['instructions_translated'], g['end']['reason']), (2, 'system_instruction'), name)
                    d, rc = run(x + PRE, RIP, *extra)   # at the block's RIP: no code, the cut the host answers
                    e = d['guest']['end']
                    self.assertEqual((d['status'], e['reason'], e.get('cut_ud'), e.get('cut_gated')), ('no_code', 'system_instruction', True, None), name)

    def test_ud_encodings_are_fetched_first(self):
        # I10 review: an undecoded #UD encoding cut at the end of the supplied bytes before its last byte (ModRM, SIB,
        # displacement, immediate) is partial_decode (the host fetches on: a fetch fault comes first); over 15 bytes, too long.
        for hexs, want in (('c4e27850', 'partial_decode'), ('c4e27850c0', 'system_instruction'), ('f30f38f0', 'partial_decode'),
                           ('8fe878c2c0', 'partial_decode'), ('8fe878c2c005', 'system_instruction'), ('62f17d48ef', 'partial_decode'),
                           ('62f17d48efc0', 'system_instruction'), ('2e' * 9 + '62f17d48efc0', 'system_instruction'),
                           ('2e' * 10 + '62f17d48efc0', 'instruction_too_long'), ('2e' * 9 + '8fe878c2c005', 'system_instruction'), ('2e' * 10 + '8fe878c2c005', 'instruction_too_long'),
                           ('27', 'system_instruction')):
            with self.subTest(hexs):
                d, _ = run(hexs)
                self.assertEqual(d['guest']['end']['reason'], want)

    def test_vldmxcsr_is_a_system_cut(self):
        for hexs in ('c5f8ae10', 'c4c178ae542408'):
            with self.subTest(hexs):
                d, _ = run(hexs + '90')
                self.assertIn('vldmxcsr', d['guest']['end']['detail'])
                self.assertEqual(d['guest']['end']['reason'], 'system_instruction')
        # Perf chunk 25: VSTMXCSR and STMXCSR are translated; LDMXCSR stays a cut.
        for hexs in ('c5f8ae18', 'c4c178ae5c2408', '0fae18'):
            with self.subTest(hexs):
                d, _ = run(hexs + '90')
                self.assertEqual(d['guest']['instructions_translated'], 2, hexs)
        d, _ = run('0fae1090')
        self.assertEqual(d['guest']['end']['reason'], 'system_instruction')

    def test_prefix_before_vex_is_ud(self):
        for name, x in VEX_UD.items():
            with self.subTest(name):
                self.cut_after_two(name, x, 'system_instruction')
                d, _ = run(x + PRE)
                self.assertIn('before VEX (#UD)', d['guest']['end']['detail'], name)

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

    def test_adx_follows_the_profile(self):
        # As SHA: the shim admits ADCX/ADOX because cpu_profile.json advertises ADX; clearing the bit needs the shim's cut back
        # (`Undefined0F38` cut every 0f 38 f6 before November 3).
        leaf = next(e for e in json.loads((REPO / 'tools/models/cpu_profile.json').read_text())['cpuid']
                    if e['leaf'] == '0x7' and e.get('subleaf') == 0)
        self.assertEqual({k: leaf['fields']['ebx']['19'][k] for k in ('name', 'state')}, {'name': 'ADX', 'state': 'advertised'})
        self.assertIn('adox rax,rcx', ALLOWED)

    def test_sha_follows_the_profile(self):
        # fexc has no profile input: the shim admits SHA because cpu_profile.json advertises it. Clearing the bit needs the
        # shim's #UD cut back (fexc_slice1.inc `Undefined0F38`, before November 3); the host's decode and `undefined` follow it.
        leaf = next(e for e in json.loads((REPO / 'tools/models/cpu_profile.json').read_text())['cpuid']
                    if e['leaf'] == '0x7' and e.get('subleaf') == 0)
        self.assertEqual({k: leaf['fields']['ebx']['29'][k] for k in ('name', 'state')}, {'name': 'SHA', 'state': 'advertised'})
        self.assertIn('sha1rnds4 xmm1,xmm2,0', ALLOWED)

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

    def test_o16_call_rel32_pushes_its_own_end_and_targets_rel32(self):
        # patch 0029: 66 e8 rel32 is 6 bytes; it pushes RIP + 6 (8 bytes) and exits to RIP + 6 + rel32
        d, rc = run('66e8fb000000', 0x1000)
        self.assertEqual(rc, 0)
        self.assertEqual(d['guest']['bytes_translated'], 6)
        moves = {int(r['guest_rip'], 16) for r in d['code']['relocations'] if r['kind'] == 'guest_rip_move'}
        self.assertEqual(moves, {0x1006, 0x1006 + 0xfb}, d['code']['relocations'])
        self.assertEqual([(x['kind'], x['width']) for x in d['sites'] if x['kind'] == 'push'], [('push', 8)])

    def test_decoder_failure_on_first_instruction_gives_no_code(self):
        # not FEX's NoExec/SIGSEGV block, and never an object file
        for name, x, reason in (('lone REX', '48', 'partial_decode'), ('lone 0f', '0f', 'partial_decode'), ('0f 01 without ModRM', '0f01', 'partial_decode'),
                                ('lone 66', '66', 'partial_decode'), ('f3 48', 'f348', 'partial_decode')):
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                obj = Path(tmp) / 'o.bin'
                d, rc = run(x, RIP, '--object', str(obj))
                self.assertEqual((rc, d['status']), (1, 'no_code'), name)
                self.assertEqual((d['guest']['instructions_translated'], d['guest']['end']['rip'], d['guest']['end']['reason']), (0, hex(RIP), reason), name)
                self.assertNotIn('hex', d.get('code', {}))
                self.assertFalse(obj.exists(), name)

    def test_instruction_too_long(self):
        # Over 15 bytes (SDM Vol.3 Interrupt 13: #GP(0)): 15 prefixes; a 16-byte mov (its imm32 ends past byte 15); a 16th
        # opcode byte. First: no code; mid-block: the block ends before it. Its 15-byte form translates.
        mov = '4ac7848300010000' + '00000000'
        for name, x in (('15 prefixes', '66' * 15 + '90'), ('16-byte mov', '66' * 4 + mov), ('16th opcode byte', '66' * 14 + '0f8500000000')):
            with self.subTest(name):
                d, rc = run(x)
                self.assertEqual((rc, d['guest']['end']['rip'], d['guest']['end']['reason']), (1, hex(RIP), 'instruction_too_long'), name)
                d, rc = run(PRE + x)
                self.assertEqual((rc, d['guest']['instructions_translated'], d['guest']['end']['reason']), (0, 2, 'instruction_too_long'), name)
        d, rc = run('66' * 3 + mov)
        self.assertEqual((rc, d['guest']['instructions_translated']), (0, 1))

    def test_decoder_failure_mid_block_ends_the_block_without_a_signal_block(self):
        ref, _ = run(PRE)
        for name, x, reason in (('lone REX', '48', 'partial_decode'), ('lone 0f', '0f', 'partial_decode'), ('lone 66', '66', 'partial_decode')):
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
        self.assertTrue(cut and all(k == 'enter [rsp]' or (k.startswith(('rcl', 'rcr')) and '[rax]' in k) for k in cut), cut)

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
# division's (verify_precision.py P11 checks its #DE branch) and I6b's helpers' (`svc #5`, `#6`, each right after a `movz w0`);
# there is no `bl` (no stop check: E1b unlinks instead).
def literal_exits(d):
    return sorted(r['guest_rip'] for r in d['code']['relocations'] if r['kind'] == 'guest_rip_move' and r['register'] == 0)


# AFP fallback chunk 3a: the guest forms (hex, assembled from Intel syntax once) whose DAZ/FTZ code the tests below pin.
FLUSH_IN = ['shl', 'ushr', 'cmeq', 'ushr', 'bic']                       # an operand flushed to a zero of its sign (mask in VTMP1)
FLUSH_OUT = ['shl', 'ushr', 'cmeq', 'ushr', 'and', 'fmul', 'bic']       # a result flushed, UFC and IXC raised for each lane flushed
AFP_FP = {   # name -> hex: SSE/AVX floating-point forms of every class of FlushRuleOf (register operands, so each is one block)
    'addps': '0f58ca', 'subpd': '660f5cca', 'mulss': 'f30f59ca', 'divsd': 'f20f5eca', 'sqrtps': '0f51ca', 'minps': '0f5dca', 'maxsd': 'f20f5fca',
    'haddps': 'f20f7cca', 'addsubpd': '660fd0ca', 'dpps': '660f3a40ca ff'.replace(' ', ''), 'cmpltps': '0fc2ca01', 'cmpeqss': 'f30fc2ca00',
    'comiss': '0f2fca', 'ucomisd': '660f2eca', 'cvtps2pd': '0f5aca', 'cvtpd2ps': '660f5aca', 'cvtss2sd': 'f30f5aca', 'cvtsd2ss': 'f20f5aca',
    'cvtps2dq': '660f5bca', 'cvttsd2si': 'f2480f2cc2', 'roundps': '660f3a08ca04', 'rcpps': '0f53ca', 'rsqrtss': 'f30f52ca',
    'vfmadd231ps': 'c4e269b8cb', 'vfnmsub213sd': 'c4e2e9afcb', 'vmulps ymm': 'c5ec59cb', 'vdivpd ymm': 'c5ed5ecb', 'vcvtps2ph': 'c4e3791dd100',
    'vcvtpd2ps ymm': 'c5fd5aca', 'vcvtps2pd ymm': 'c5fc5aca', 'vhaddpd ymm': 'c5ed7ccb',
}


@unittest.skipUnless(os.access(FEXC, os.X_OK), f'fexc not built: {FEXC} (set $FEXC)')
class AfpFlush(unittest.TestCase):
    """AFP fallback chunk 3a: `--daz` and `--ftz` (the host sends them only without AFP) and their software flushes. Their semantics
    are checked by differential runs against the M4's own FIZ/FZ code on the M4 (the work's gate); these pin where the code goes."""
    AFP0 = ['--feature', 'afp=0']

    def words(self, hexs, *extra):
        d, rc = run(hexs, RIP, *extra)
        self.assertEqual(rc, 0, (hexs, extra))
        lay = d['code']['layout']
        code = bytes.fromhex(d['code']['hex'])
        lits = [(r['offset'], r['size']) for r in d['code']['relocations'] if r['kind'] == 'guest_rip_literal']
        with tempfile.TemporaryDirectory() as t:
            dis = check_sites.disassemble(code, lay['code_region_end'], Path(t), lits)
        mn = [re.split(r'[. ]', x.strip())[0] for o, x in sorted(dis.items()) if o >= lay['code_region_begin']]
        self.assertEqual(lay['spill_bytes'], 0, (hexs, extra))
        return mn, region(d)

    def test_a_host_with_afp_ignores_both_keys(self):
        for name, h in AFP_FP.items():
            for extra in ([], ['--simd-unmasked', '0x20']):
                base = run(h, RIP, *extra)[0]
                for keys in (['--daz'], ['--ftz'], ['--daz', '--ftz']):
                    self.assertEqual(region(run(h, RIP, *extra, *keys)[0]), region(base), (name, keys, extra))

    def test_no_keys_no_change_without_afp(self):
        # (the common mode: the replay of the recorded streams with afp=0 added, against the previous compiler, pins the rest)
        for name, h in AFP_FP.items():
            mn, _ = self.words(h, *self.AFP0)
            self.assertFalse({'cmeq'} & set(mn), name)

    def test_daz_alone_flushes_every_fp_source_first(self):
        def before(name_or_hex, op, *extra):   # the instructions before the first `op`
            mn, _ = self.words(AFP_FP.get(name_or_hex, name_or_hex), *self.AFP0, '--daz', *extra)
            return mn[:mn.index(op)]
        self.assertEqual(before('addps', 'fadd'), FLUSH_IN * 2)
        self.assertEqual(before('0f58c9', 'fadd'), FLUSH_IN)   # addps xmm1, xmm1: one value, flushed once
        self.assertEqual(before('vfmadd231ps', 'fmla')[:15], FLUSH_IN * 3)
        self.assertEqual(before('minps', 'fcmgt'), FLUSH_IN * 2)   # row 4 covers MIN/MAX: both sources, then the compare
        self.assertEqual(before('comiss', 'fcmpe'), FLUSH_IN * 2)
        self.assertEqual(before('cvtps2dq', 'frint32x'), FLUSH_IN)   # to integer: the source only (the FRINT's integral result is not flushed again)
        self.assertEqual(self.words(AFP_FP['cvtps2dq'], *self.AFP0, '--daz')[0].count('cmeq'), 1)
        self.assertEqual(before('vcvtps2ph', 'fcvtn')[-5:], FLUSH_IN)   # VCVTPS2PH honours DAZ (the imm8 rounding-mode FPCR switch comes first)
        # DPPS: every FP op's inputs: both factors, the products, the first add level's sums
        mn, _ = self.words(AFP_FP['dpps'], *self.AFP0, '--daz')
        self.assertEqual(mn.count('shl'), 4, mn)
        self.assertEqual(mn.count('faddp'), 2, mn)

    def test_ftz_alone_flushes_the_results_that_x86_flushes_and_raises_ue_and_pe(self):
        def after(name, op, *extra):   # the instructions after the first `op`
            mn, _ = self.words(AFP_FP[name], *self.AFP0, '--ftz', *extra)
            return mn[mn.index(op) + 1:]
        self.assertEqual(after('addps', 'fadd')[:7], FLUSH_OUT)
        mn, _ = self.words(AFP_FP['mulss'], *self.AFP0, '--ftz')   # the scalar's result is inserted into element 0, then flushed
        self.assertEqual(mn[mn.index('shl'):][:7], FLUSH_OUT)
        self.assertLess(mn.index('fmul'), mn.index('shl'))
        self.assertEqual(after('vfmadd231ps', 'fmla')[:7], FLUSH_OUT)
        for name in ('minps', 'maxsd', 'sqrtps', 'cmpltps', 'comiss', 'cvtps2dq', 'cvttsd2si', 'roundps', 'vcvtps2ph', 'cvtss2sd', 'rsqrtss'):
            self.assertNotIn('cmeq', self.words(AFP_FP[name], *self.AFP0, '--ftz')[0], name)   # no FTZ on them
        for name in ('cvtpd2ps', 'cvtsd2ss'):   # the narrowing converts to single
            self.assertEqual(self.words(AFP_FP[name], *self.AFP0, '--ftz')[0].count('fmul'), 1, name)
        mn, _ = self.words(AFP_FP['rcpps'], *self.AFP0, '--ftz')   # RCP's flags are restored: a flush without them
        self.assertEqual(mn[mn.index('msr') + 1:][:5], FLUSH_IN)
        self.assertNotIn('fmul', mn)
        self.assertEqual(self.words(AFP_FP['dpps'], *self.AFP0, '--ftz')[0].count('fmul'), 4)   # products and both add levels

    def test_daz_and_ftz_together_use_fz_and_flush_only_what_min_max_select(self):
        for name, h in AFP_FP.items():
            both, _ = self.words(h, *self.AFP0, '--daz', '--ftz')
            plain, _ = self.words(h, *self.AFP0)
            if name in ('minps', 'maxsd'):
                core = both[:both.index('svc')]
                while core[-1] in ('mov', 'movk', 'msr'): core.pop()   # (the NZCV restore follows the flush)
                self.assertEqual(core[-5:], FLUSH_IN, name)   # the selected result, flushed with no flag
                self.assertEqual(both.count('cmeq'), 1, name)
            else:
                self.assertEqual(both, plain, name)

    def test_x87_instructions_are_left_alone(self):
        for h in ('dc00', 'd8c1', 'dec1', 'd9c0'):   # fadd qword [rax]; fadd st0, st1; faddp; fld st0
            plain, _ = self.words(h, *self.AFP0)
            for keys in (['--daz'], ['--ftz'], ['--daz', '--ftz']):
                self.assertEqual(self.words(h, *self.AFP0, *keys)[0], plain, (h, keys))

    def test_unmasked_pe_also_tests_ufc_under_effective_ftz(self):
        def mask(*extra):
            _, r = self.words('0f58ca', *extra, '--simd-unmasked', '0x20')
            m = [int.from_bytes(r[i:i + 4], 'little') for i in range(0, len(r), 4)]
            return [(w >> 5) & 0xffff for w in m if w & 0xffe0001f == 0x52800001][0]   # movz w1, #M
        self.assertEqual(mask(), 0x10)
        self.assertEqual(mask('--ftz'), 0x10)                     # a host with AFP: never
        self.assertEqual(mask(*self.AFP0), 0x10)
        self.assertEqual(mask(*self.AFP0, '--daz'), 0x10)
        self.assertEqual(mask(*self.AFP0, '--ftz'), 0x18)
        self.assertEqual(mask(*self.AFP0, '--ftz', '--daz'), 0x18)

    def lines(self, hexs, *extra):
        d, rc = run(hexs, RIP, *extra)
        self.assertEqual(rc, 0, (hexs, extra))
        lay = d['code']['layout']
        lits = [(r['offset'], r['size']) for r in d['code']['relocations'] if r['kind'] == 'guest_rip_literal']
        with tempfile.TemporaryDirectory() as t:
            dis = check_sites.disassemble(bytes.fromhex(d['code']['hex']), lay['code_region_end'], Path(t), lits)
        return [x.strip() for o, x in sorted(dis.items()) if o >= lay['code_region_begin']]

    @staticmethod
    def fma_sources_are_distinct(code):
        """Value-number the vector registers through the block (a move copies a value, any other write makes a value of its
        mnemonic and the values it reads; a partial write also reads the old destination); every scalar FMA must read three
        different values, which holds for `vfmadd* xmmA, xmmB, [mem]` with A, B and the load all different. A move of the upper
        bits into a register that holds a source before the FMA reads it (the defect) makes two of the three the same value."""
        val, found = {}, 0
        def v(r): return val.get(r, ('init', r))
        for ln in code:
            m = re.match(r'([a-z0-9]+)(?:\.\w+)?\s+(.*)', ln)
            if not m: continue
            op, rest = m.groups()
            regs = re.findall(r'\b[vsdqhb](\d+)\b(?!\s*,?\s*\[x)', rest)
            ops = [x.strip() for x in re.split(r',\s*(?![^\[]*\])', rest)]
            if op in ('fmadd', 'fmsub', 'fnmadd', 'fnmsub') and re.match(r'[sd]\d+$', ops[0]):
                srcs = [v(x[1:]) for x in ops[1:4]]
                found += 1
                if len(set(srcs)) != 3: return None
                val[ops[0][1:]] = (op, *srcs)
            elif re.match(r'[vsdqhb]\d+(\[\d+\])?$', ops[0]) and op not in ('str', 'stp', 'fcmp', 'fcmpe', 'cmp'):
                d = re.match(r'[vsdqhb](\d+)', ops[0]).group(1)
                ins = [v(re.match(r'[vsdqhb](\d+)', x).group(1)) if re.match(r'[vsdqhb]\d+(\[\d+\])?$', x) else x for x in ops[1:]]
                if op in ('mov', 'orr') and len(ops) == 2 or (op == 'orr' and len(set(ops[1:])) == 1):
                    val[d] = ins[0]   # mov.16b vD, vN; orr.16b vD, vN, vN
                elif op == 'mov' and '[' in ops[0] or op in ('ins', 'bsl', 'bif', 'bit', 'fmla', 'fmls'):
                    val[d] = (op, v(d), *ins)
                else:
                    val[d] = (op, *ins)
        return found

    def test_scalar_fma_with_a_memory_operand_reads_its_sources_before_they_are_overwritten(self):
        def enc(op, w, a, b):   # VEX.128.66.0F38.Wn op /r: xmm<a>, xmm<b>, [rbx]
            return bytes([0xc4, 0xe2 if a < 8 else 0x62, w << 7 | (~b & 15) << 3 | 1, op, (a & 7) << 3 | 3]).hex()
        n = 0
        # 132/213/231 x madd/msub/nmadd/nmsub x ss/sd; the destination register is one of the operands (A) and, in the second
        # block, the instruction after overwrites A, B and the loaded value's register so that all three die with the FMA
        for base in (0x99, 0xa9, 0xb9):
            for kind in (0, 2, 4, 6):
                for w in (0, 1):
                    for a, b in ((4, 0), (0, 4), (8, 1), (2, 3)):
                        h = enc(base + kind, w, a, b)
                        xor = lambda r: bytes([0xc5, 0xf8, 0x57, 0xc0 | (r & 7) << 3 | (r & 7)]).hex()   # vxorps xmm<r>, xmm0, xmm<r>... (r < 8)
                        for tail in ('', xor(a) if a < 8 else '', xor(b) if b < 8 else ''):
                            for extra in ([], ['--ftz'], ['--daz'], ['--daz', '--ftz']):
                                code = self.lines(h + tail, *self.AFP0, *extra)
                                got = self.fma_sources_are_distinct(code)
                                self.assertTrue(got, (h, tail, extra, code))
                                n += 1
        self.assertEqual(n, 3 * 4 * 2 * 4 * 3 * 4)

    def test_the_semantic_check_sees_the_clobbered_source(self):   # the defect's emitted code (i8 random-39: vfmadd213ss xmm4, xmm0, [mem])
        bad = ['ldr s2, [x6]', 'mov.16b v2, v20', 'fmadd s0, s16, s20, s2']
        good = ['ldr s2, [x6]', 'fmadd s0, s16, s20, s2', 'mov.16b v2, v20', 'mov.s v2[0], v0[0]']
        self.assertIsNone(self.fma_sources_are_distinct(bad))
        self.assertEqual(self.fma_sources_are_distinct(good), 1)

    def test_dpps_and_dppd_step_checks_follow_their_flush(self):
        # PlaceFlushes ran first, so a step's check goes after its flush (UFC and IXC are the step's own): O(p) F(lush) C(heck) per step
        def tokens(h):
            out, it = [], iter(self.lines(h, *self.AFP0, '--ftz', '--simd-unmasked', '0x20'))
            for ln in it:
                op = re.split(r'[. ]', ln)[0]
                if op == 'shl':
                    while not ln.startswith('bic'): ln = next(it)
                    out.append('F')
                elif op == 'mrs' and 'FPSR' in ln:
                    while not ln.startswith('msr'): ln = next(it)
                    out.append('C')
                elif op in ('fmul', 'faddp'): out.append('O')
            return ''.join(out)
        self.assertEqual(tokens('660f3a40caff'), 'OFC' * 3)      # DPPS: products, first and second add level
        self.assertEqual(tokens('660f3a41caff'), 'OFC' * 2)      # DPPD: products, the add
        self.assertEqual(tokens('c4e36d40caff'), 'OFC' * 6)      # VDPPS ymm: each half in turn
        self.assertEqual(tokens('c4e36941caff'), 'OFC' * 2)   # VDPPD xmm


# AFP fallback chunk 3b (rows 7, 8, 10): the software denormal tests and the FMA sanitizing are checked by running the emitted code natively
# (tools/fexc/exec_words.c, on the M4, whose FPCR the process leaves as an M2's: no AH, FZ clear) against an oracle written from the SDM.
_NATIVE = {}


def native_exe():
    if 'exe' not in _NATIVE:
        tmp = tempfile.mkdtemp(prefix='exec_words')
        exe = Path(tmp) / 'exec_words'
        subprocess.run(['clang', '-O1', '-o', str(exe), str(HERE / 'exec_words.c')], check=True, capture_output=True)
        _NATIVE['exe'] = exe
    return _NATIVE['exe']


def native_run(words, x=None, v=None, buf=b'', ptrs=(), fpcr=None):
    """Runs `words` with x[i], v[i] (128-bit ints), buf (8 KiB: x28 points at it) and x<reg> = buf + offset for `ptrs`, under FPCR `fpcr`
    (default the process's: an M2's, no AH; 6 is the M4 as natemu runs it); returns (x, v, buf) after."""
    x, v = list(x or [0] * 32), list(v or [0] * 32)
    inp = struct.pack('<I', len(words)) + struct.pack(f'<{len(words)}I', *words) + struct.pack('<I', len(ptrs))
    for reg, off in ptrs: inp += struct.pack('<II', reg, off)
    inp += struct.pack('<32Q', *x) + b''.join(int(q).to_bytes(16, 'little') for q in v) + buf.ljust(8192, b'\0')
    out = subprocess.run([str(native_exe())] + ([] if fpcr is None else [f'{fpcr:x}']), input=inp, capture_output=True, check=True).stdout
    return (list(struct.unpack_from('<32Q', out, 0)), [int.from_bytes(out[256 + 16 * i:272 + 16 * i], 'little') for i in range(32)], out[768:])


def fp_class(x, e):
    """The IEEE class of the e-byte float bits x."""
    man, ex = (23, 8) if e == 4 else (52, 11)
    exp, frac = x >> man & (1 << ex) - 1, x & (1 << man) - 1
    return dict(den=exp == 0 and frac != 0, zero=exp == 0 and frac == 0, nan=exp == (1 << ex) - 1 and frac != 0,
                inf=exp == (1 << ex) - 1 and frac == 0, snan=exp == (1 << ex) - 1 and frac != 0 and not frac >> (man - 1), neg=bool(x >> (8 * e - 1)))


# One value per class for 4- and 8-byte floats: normal, denormals (smallest, largest, negative), the smallest normal, zeros, infinities, QNaN, SNaN.
VALUES = {4: dict(one=0x3f800000, den=0x00000001, dmax=0x007fffff, dneg=0x80000001, minn=0x00800000, zero=0, nzero=0x80000000,
                  inf=0x7f800000, ninf=0xff800000, qnan=0x7fc00000, snan=0x7f800001),
          8: dict(one=0x3ff0000000000000, den=1, dmax=0x000fffffffffffff, dneg=0x8000000000000001, minn=0x0010000000000000, zero=0,
                  nzero=0x8000000000000000, inf=0x7ff0000000000000, ninf=0xfff0000000000000, qnan=0x7ff8000000000000, snan=0x7ff0000000000001)}


def de_lane(kind, ops, e):
    """x86's DE for one lane (SDM Vol.1 11.5.2, the I5a runner's rules): a denormal operand, none beside a NaN operand (IE) or, for a
    divisor of zero, ZE (the dividend), a negative square root, an inf * 0 product."""
    c = [fp_class(o, e) for o in ops]
    if any(k['nan'] for k in c): return False
    den = [k['den'] for k in c]
    if kind == 'div': return (den[0] and not c[1]['zero']) or den[1]
    if kind == 'sqrt': return den[0] and not c[0]['neg']
    if kind == 'fma' and ((c[0]['inf'] and c[1]['zero']) or (c[0]['zero'] and c[1]['inf'])): return False
    return any(den)


def ie_lane(ops, e, name, lane=0):
    """x86's IE for a fused multiply-add lane (a, b, addend; SDM Vol.1 Table 14-17): an SNaN; inf * 0 unless the addend is a NaN; an infinite
    product and addend of opposite signs (`name`: vfmadd a*b + c, vfmsub a*b - c, vfnmadd -(a*b) + c, vfnmsub -(a*b) - c)."""
    a, b, c = (fp_class(o, e) for o in ops)
    if a['snan'] or b['snan'] or c['snan']: return True
    if a['nan'] or b['nan'] or c['nan']: return False
    if (a['inf'] and b['zero']) or (a['zero'] and b['inf']): return True
    pneg = name.startswith('vfnm')
    if 'addsub' in name: cneg = lane % 2 == 0      # (vfmaddsub: a*b - c in the even lanes, + c in the odd)
    elif 'subadd' in name: cneg = lane % 2 == 1    # (vfmsubadd: the reverse)
    else: cneg = name.startswith(('vfmsub', 'vfnmsub'))
    product_inf = (a['inf'] and not b['zero']) or (b['inf'] and not a['zero'])
    return product_inf and c['inf'] and (a['neg'] ^ b['neg'] ^ pneg) != (c['neg'] ^ cneg)


def native_words(hexs, *extra):
    key = (hexs, extra)
    if key not in _NATIVE:
        d, rc = run(hexs, RIP, *extra)
        assert rc == 0, key
        lay = d['code']['layout']
        code = bytes.fromhex(d['code']['hex'])
        lits = [(r['offset'], r['size']) for r in d['code']['relocations'] if r['kind'] == 'guest_rip_literal']
        with tempfile.TemporaryDirectory() as t:
            dis = check_sites.disassemble(code, lay['code_region_end'], Path(t), lits)
        b, e = lay['code_region_begin'], lay['code_region_end']
        _NATIVE[key] = (list(struct.unpack(f'<{(e - b) // 4}I', code[b:e])), [dis[o].strip() for o in range(b, e, 4)], lay['spill_bytes'])
    return _NATIVE[key]


def soft_de_words(lines):
    """The number of words of `lines` that are the software flag's `and w1, wD, #0x80`."""
    return sum(l.startswith('and w1, w') and l.endswith('#0x80') for l in lines)


# SSE/AVX forms of the rules of DeRuleOf: (name, hex, kind, element bytes, lanes examined, source registers in operand order).
DE_FORMS = [
    ('addss', 'f30f58ca', 'bin', 4, 1, (1, 2)), ('addps', '0f58ca', 'bin', 4, 4, (1, 2)), ('addsd', 'f20f58ca', 'bin', 8, 1, (1, 2)),
    ('subpd', '660f5cca', 'bin', 8, 2, (1, 2)), ('mulps', '0f59ca', 'bin', 4, 4, (1, 2)), ('mulsd', 'f20f59ca', 'bin', 8, 1, (1, 2)),
    ('divss', 'f30f5eca', 'div', 4, 1, (1, 2)), ('divps', '0f5eca', 'div', 4, 4, (1, 2)), ('divsd', 'f20f5eca', 'div', 8, 1, (1, 2)),
    ('divpd', '660f5eca', 'div', 8, 2, (1, 2)), ('minps', '0f5dca', 'bin', 4, 4, (1, 2)), ('maxss', 'f30f5fca', 'bin', 4, 1, (1, 2)),
    ('minpd', '660f5dca', 'bin', 8, 2, (1, 2)), ('sqrtps', '0f51ca', 'sqrt', 4, 4, (2,)), ('sqrtss', 'f30f51ca', 'sqrt', 4, 1, (2,)),
    ('sqrtsd', 'f20f51ca', 'sqrt', 8, 1, (2,)), ('sqrtpd', '660f51ca', 'sqrt', 8, 2, (2,)),
    ('haddps', 'f20f7cca', 'hadd', 4, 4, (1, 2)), ('hsubpd', '660f7dca', 'hadd', 8, 2, (1, 2)),
    ('addsubps', 'f20fd0ca', 'bin', 4, 4, (1, 2)), ('addsubpd', '660fd0ca', 'bin', 8, 2, (1, 2)),
    ('cmpltps', '0fc2ca01', 'bin', 4, 4, (1, 2)), ('cmpeqss', 'f30fc2ca00', 'bin', 4, 1, (1, 2)), ('cmpnlepd', '660fc2ca06', 'bin', 8, 2, (1, 2)),
    ('comiss', '0f2fca', 'bin', 4, 1, (1, 2)), ('ucomisd', '660f2eca', 'bin', 8, 1, (1, 2)),
    ('cvtps2pd', '0f5aca', 'unary', 4, 2, (2,)), ('cvtss2sd', 'f30f5aca', 'unary', 4, 1, (2,)), ('cvtpd2ps', '660f5aca', 'unary', 8, 2, (2,)),
    ('cvtsd2ss', 'f20f5aca', 'unary', 8, 1, (2,)), ('vcvtps2ph', 'c4e3791dd100', 'unary', 4, 4, (2,)), ('vcvtps2pd ymm', 'c5fc5aca', 'unary', 4, 4, (2,)),
    ('vaddps', 'c5e858cb', 'bin', 4, 4, (2, 3)), ('vdivsd', 'c5eb5ecb', 'div', 8, 1, (2, 3)),
    ('vfmadd231ps', 'c4e269b8cb', 'fma', 4, 4, (2, 3, 1)), ('vfmadd231ss', 'c4e269b9cb', 'fma', 4, 1, (2, 3, 1)),
    ('vfmadd231pd', 'c4e2e9b8cb', 'fma', 8, 2, (2, 3, 1)), ('vfmadd231sd', 'c4e2e9b9cb', 'fma', 8, 1, (2, 3, 1)),
    ('vfmsub231ps', 'c4e269bacb', 'fma', 4, 4, (2, 3, 1)), ('vfnmadd231sd', 'c4e2e9bdcb', 'fma', 8, 1, (2, 3, 1)),
    ('vfnmsub231pd', 'c4e2e9becb', 'fma', 8, 2, (2, 3, 1)),
]


@unittest.skipUnless(os.access(FEXC, os.X_OK), f'fexc not built: {FEXC} (set $FEXC)')
class AfpDenormal(unittest.TestCase):
    """AFP fallback chunk 3b: rows 7 (SIMD DE), 8 (x87 DE) and 10 (FMA IE) without AFP, keyed on the exception being unmasked. The code is
    run natively and judged by the SDM's rules (de_lane, ie_lane), the way the runners judge the M4."""
    AFP0 = ['--feature', 'afp=0']

    @staticmethod
    def pack(lanes, e): return sum(l << 8 * e * k for k, l in enumerate(lanes))

    def check_word(self, form, extra, regs):
        """Runs the form up to its first SIMD check word (the software flag in) with xmm registers `regs` {n: 128-bit value}; x0."""
        words, lines, spills = native_words(form, *self.AFP0, *extra)
        self.assertEqual(spills, 0, form)
        i = next(k for k, l in enumerate(lines) if l.startswith('mrs x0, FPSR'))
        v = [0] * 32
        for n, bits in regs.items(): v[16 + n] = bits
        if extra == ('--simd-unmasked', '0x2'):
            self.assertTrue(lines[i + 1].startswith('and w1, w') and lines[i + 1].endswith('#0x80') and lines[i + 2] == 'orr w0, w0, w1', lines[i:i + 3])
            return native_run(words[:i + 3], v=v)[0][0]
        return native_run(words[:i + 1], v=v)[0][0]

    def test_simd_denormal_flag_follows_the_sdm(self):
        cl = ['one', 'den', 'dneg', 'zero', 'inf', 'qnan', 'snan']
        n = positive = 0
        for name, form, kind, e, lanes, srcs in DE_FORMS:
            if name.startswith(('vfmsub', 'vfnm')): continue   # (their sign-dependent IE, inf - inf, is judged against the M4's hardware below)
            val, per = VALUES[e], 16 // e
            junk = [val['snan'] if k % 2 else val['den'] for k in range(per)]   # lanes the form does not examine: never looked at
            combos = itertools.product(('one', 'den', 'zero', 'inf', 'qnan', 'snan'), repeat=3) if kind == 'fma' else itertools.product(cl, repeat=len(srcs))
            for combo in combos:
                for pos in sorted({0, lanes - 1} if kind != 'hadd' else {0, 1}):   # (hadd: the pair in a, or in b)
                    ops = [[val['one']] * lanes + junk[lanes:] for _ in srcs]
                    if kind == 'hadd':
                        ops[pos][0], ops[pos][1] = val[combo[0]], val[combo[1]]
                        want = any(de_lane('bin', [ops[s][2 * k], ops[s][2 * k + 1]], e) for s in range(2) for k in range(lanes // 2))
                    else:
                        for r, c in enumerate(combo): ops[r][pos] = val[c]
                        want = any(de_lane(kind, [ops[r][k] for r in range(len(srcs))], e) for k in range(lanes))
                    got = self.check_word(form, ('--simd-unmasked', '0x2'), {s: self.pack(ops[r], e) for r, s in enumerate(srcs)}) & 0x80 != 0
                    self.assertEqual(got, want, (name, pos, combo))
                    n += 1
                    positive += want
        self.assertGreater(n, 3000)
        self.assertGreater(positive, 500)
        self.assertGreater(n - positive, 1500)

    def test_simd_denormal_flag_is_the_m4s_idc(self):
        """The same forms compiled for a host with AFP and run under the M4's FPCR (AH, NEP: natemu's FPCR on the M4) raise IDC in hardware:
        the software flag of the code without AFP, run under an M2's FPCR, is that bit for every operand class (the plan's M2 = M4)."""
        cl = ['one', 'den', 'dmax', 'dneg', 'minn', 'zero', 'inf', 'qnan', 'snan']
        n = diff = 0
        for name, form, kind, e, lanes, srcs in DE_FORMS:
            val, per = VALUES[e], 16 // e
            junk = [val['snan'] if k % 2 else val['den'] for k in range(per)]
            words, lines, _ = native_words(form, '--simd-unmasked', '0x2')
            hw = words[:next(k for k, l in enumerate(lines) if l.startswith('mrs x0, FPSR')) + 1]
            for combo in (itertools.product(('one', 'den', 'zero', 'inf', 'ninf', 'qnan', 'snan'), repeat=3) if kind == 'fma' else itertools.product(cl, repeat=len(srcs))):
                for pos in sorted({0, lanes - 1} if kind != 'hadd' else {0, 1}):
                    ops = [[val['one']] * lanes + junk[lanes:] for _ in srcs]
                    if kind == 'hadd': ops[pos][0], ops[pos][1] = val[combo[0]], val[combo[1]]
                    else:
                        for r, c in enumerate(combo): ops[r][pos] = val[c]
                    regs = {s: self.pack(ops[r], e) for r, s in enumerate(srcs)}
                    v = [0] * 32
                    for r, bits in regs.items(): v[16 + r] = bits
                    m4 = native_run(hw, v=v, fpcr=6)[0][0] >> 7 & 1
                    got = self.check_word(form, ('--simd-unmasked', '0x2'), regs) >> 7 & 1
                    if got != m4:
                        diff += 1
                        print('differs from the M4:', name, pos, combo, 'software', got, 'hardware', m4)
                    n += 1
        self.assertEqual(diff, 0)
        self.assertGreater(n, 5000)

    def test_simd_denormal_code_exists_only_without_afp_and_with_de_unmasked(self):
        for extra in (['--simd-unmasked', '0x2'], ['--simd-unmasked', '0x3f']):
            self.assertEqual(soft_de_words(native_words('f30f58ca', *extra)[1]), 0, extra)   # a host with AFP: IDC is the hardware's
            self.assertEqual(soft_de_words(native_words('f30f58ca', *self.AFP0, *extra)[1]), 1, extra)
        for mask in ('0x0', '0x3d', '0x1', '0x20'):   # DE masked (decision B): no software test
            lines = native_words('f30f58ca', *self.AFP0, '--simd-unmasked', mask)[1]
            self.assertEqual(soft_de_words(lines), 0, mask)
            self.assertNotIn('cmeq.4s v3, v2, #0', lines, mask)
        # common mode (nothing unmasked): the plain code, with no vector compare at all
        self.assertFalse({'fcmeq', 'cmeq'} & {re.split(r'[. ]', l)[0] for l in native_words('f30f58ca', *self.AFP0)[1]})
        for name, form, kind, e, lanes, srcs in DE_FORMS:
            self.assertEqual(soft_de_words(native_words(form, *self.AFP0, '--simd-unmasked', '0x2')[1]), 1, name)
            self.assertEqual(soft_de_words(native_words(form, '--simd-unmasked', '0x2')[1]), 0, name)

    def test_half_precision_sources_have_no_software_flag(self):
        # VCVTPH2PS (xmm, ymm, memory): x86 raises no DE for a half denormal; the code is the plain check, and runs
        for form in ('c4e27913ca', 'c4e27d13ca', 'c4e2791300'):
            for mask in ('0x2', '0x3f'):
                words, lines, spills = native_words(form, *self.AFP0, '--simd-unmasked', mask)
                self.assertEqual((soft_de_words(lines), spills), (0, 0), (form, mask))
                self.assertFalse({'fmul.h'} & {l.split()[0] for l in lines}, (form, mask))
        words, lines, _ = native_words('c4e27913ca', *self.AFP0, '--simd-unmasked', '0x2')
        i = next(k for k, l in enumerate(lines) if l.startswith('mrs x0, FPSR'))
        x, _, _ = native_run(words[:i + 1], v=[0] * 18 + [0x3c000001] + [0] * 13)
        self.assertFalse(x[0] & 0x80)

    def first_de_check(self, form, extra, v, fpcr=None, soft=True, buf=b''):
        """The index of the first check (multiply, add level 1, add level 2 ...) of the form whose check word has IDC, or None: each check is
        run alone (a hit would branch to its stub), the earlier ones having passed."""
        words, lines, _ = native_words(form, *(self.AFP0 if soft else ()), *extra)
        checks = [k for k, l in enumerate(lines) if l.startswith('mrs x0, FPSR') and not lines[k + 1].startswith('orr x0, x0, #0x80')]
        for n, i in enumerate(checks):
            end = i + 3 if lines[i + 1].startswith('and w1, w') and lines[i + 1].endswith('#0x80') else i + 1
            if native_run(words[:end], v=v, fpcr=fpcr, buf=buf)[0][0] & 0x80: return n
        return None

    def test_dpps_and_dppd_add_levels_flag_denormal_products_and_sums_as_the_m4_does(self):
        rnd = random.Random(7)
        tiny32, tiny64 = 0x1c800000, 0x1f70000000000000   # 2^-70 and 2^-520: their square is an exact denormal, no UE
        hits = collections.Counter()
        for name, form, e, per in (('dpps ff', '660f3a40caff', 4, 4), ('dpps 71', '660f3a40ca71', 4, 4), ('dpps f1', '660f3a40caf1', 4, 4),
                                   ('dpps 3f', '660f3a40ca3f', 4, 4), ('dppd 33', '660f3a41ca33', 8, 2), ('dppd 31', '660f3a41ca31', 8, 2),
                                   ('dppd 3f', '660f3a41ca3f', 8, 2), ('vdpps 71', 'c4e36940cb71', 4, 4), ('vdppd ff', 'c4e36941cbff', 8, 2)):
            val = VALUES[e]
            pool = [val['one'], val['one'], val['one'], tiny32 if e == 4 else tiny64, val['den'], val['zero'], val['inf'], val['qnan'], val['neg'] if 'neg' in val else val['ninf']]
            cases = [([tiny32 if e == 4 else tiny64] + [val['one']] * (per - 1), [tiny32 if e == 4 else tiny64] + [0] * (per - 1))]   # the reviewer's: product 2^-140
            if e == 4: cases.append(([0x00800001, 0x80800000, 0, 0], [0x3f800000, 0x3f800000, 0, 0]))   # normal products whose sum is an exact denormal: level 2's input
            cases += [([rnd.choice(pool) for _ in range(per)], [rnd.choice(pool) for _ in range(per)]) for _ in range(150)]
            for a, b in cases:
                v = [0] * 32
                v[17], v[18] = self.pack(a, e), self.pack(b, e)
                if form.startswith('c4'): v[17], v[18], v[19] = 0, self.pack(b, e), self.pack(a, e)   # vdpps xmm1, xmm2, xmm3: xmm2 and xmm3 are read
                m4 = self.first_de_check(form, ('--simd-unmasked', '0x2'), v, fpcr=6, soft=False)
                sw = self.first_de_check(form, ('--simd-unmasked', '0x2'), v)
                self.assertEqual(sw, m4, (name, [hex(x) for x in a], [hex(x) for x in b]))
                hits[(name, m4)] += 1
        # VDPPS ymm: the upper halves of ymm2 and ymm3 are in the guest state (0xe0, 0xf0), each half in turn through its own levels
        pool = [0x3f800000, 0x3f800000, tiny32, 1, 0, 0x7fc00000, 0x00800001, 0x80800000]
        for imm in ('ff', '71', 'f3'):
            for _ in range(120):
                lo = [[rnd.choice(pool) for _ in range(4)] for _ in range(4)]
                v = [0] * 32
                v[18], v[19] = self.pack(lo[0], 4), self.pack(lo[1], 4)
                buf = bytearray(8192)
                buf[0xe0:0x100] = struct.pack('<8I', *lo[2], *lo[3])
                form = f'c4e36d40cb{imm}'
                m4 = self.first_de_check(form, ('--simd-unmasked', '0x2'), v, fpcr=6, soft=False, buf=bytes(buf))
                self.assertEqual(self.first_de_check(form, ('--simd-unmasked', '0x2'), v, buf=bytes(buf)), m4, (imm, lo))
                hits[('vdpps ymm', m4)] += 1
        self.assertTrue(hits[('vdpps ymm', None)] and any(hits[('vdpps ymm', k)] for k in range(1, 12)), hits)
        for name in ('dpps ff', 'dppd 33'):
            self.assertTrue(all(hits[(name, k)] for k in (None, 0, 1)), (name, hits))
        self.assertTrue(hits[('dpps ff', 2)], hits)   # a denormal sum of normal products reaches the second level

    def test_dpps_flags_its_multiplies_and_checks_each_step(self):
        words, lines, spills = native_words('660f3a40caff', *self.AFP0, '--simd-unmasked', '0x2')
        self.assertEqual(spills, 0)
        self.assertEqual(soft_de_words(lines), 1)   # the multiply's check carries the flag; the add levels set IDC in FPSR themselves
        self.assertEqual(sum(l.startswith('mrs x0, FPSR') for l in lines), 5)   # three checks and two IDC updates
        for d, want in ((1, True), (0x3f800000, False)):
            v = [0] * 17 + [self.pack([d, 0x3f800000, 0x3f800000, 0x3f800000], 4), self.pack([0x3f800000] * 4, 4)] + [0] * 13
            self.assertEqual(self.first_de_check('660f3a40caff', ('--simd-unmasked', '0x2'), v) is not None, want, d)
        # a denormal in an element the mask leaves out is not examined
        v = [0] * 17 + [self.pack([0x3f800000] * 3 + [1], 4), self.pack([0x3f800000] * 4, 4)] + [0] * 13
        self.assertIsNone(self.first_de_check('660f3a40ca7f', ('--simd-unmasked', '0x2'), v))

    # row 8
    X87 = [('fld m32', 'd903', 4, 'fld'), ('fld m64', 'dd03', 8, 'fld'), ('fadd m32', 'd803', 4, 'bin'), ('fadd m64', 'dc03', 8, 'bin'),
           ('fmul m32', 'd80b', 4, 'bin'), ('fcom m64', 'dc13', 8, 'bin'), ('fcomp m32', 'd81b', 4, 'bin'), ('fsub m64', 'dc23', 8, 'bin'),
           ('fsubr m32', 'd82b', 4, 'bin'), ('fdiv m64', 'dc33', 8, 'bin'), ('fdivr m64', 'dc3b', 8, 'divr'), ('fdivr m32', 'd83b', 4, 'divr')]

    def x87_check_word(self, form, mem, e, st0, extra=('--x87-unmasked', '0x2')):
        words, lines, spills = native_words(form, *self.AFP0, *extra)
        self.assertEqual(spills, 0, form)
        i = next(k for k, l in enumerate(lines) if l == 'ldrb w0, [x28, #0x412]')
        self.assertTrue(lines[i + 1].startswith('and w1, w') and lines[i + 1].endswith('#0x80') and lines[i + 2] == 'orr w0, w0, w1', lines[i:i + 3])
        j = next(k for k in range(i, len(lines)) if lines[k] == 'and w1, w0, w1')
        buf = bytearray(8192)
        buf[0x4b2] = 0 if st0 is None else 1               # valid tags: ST0 present or the stack empty (TOP, at 0x41b, is 0)
        if st0 is not None: buf[0x420:0x428] = st0.to_bytes(8, 'little')
        buf[0x1000:0x1000 + e] = mem.to_bytes(e, 'little')
        return native_run(words[:j + 1], buf=bytes(buf), ptrs=[(6, 0x1000)])[0][0]

    def test_x87_denormal_memory_operand_follows_the_sdm(self):
        n = 0
        for name, form, e, kind in self.X87:
            for m in ('one', 'den', 'dneg', 'dmax', 'minn', 'zero', 'inf', 'qnan', 'snan'):
                for r in (['none'] if kind == 'fld' else ['one', 'zero', 'den', 'inf', 'qnan', 'snan', 'none']):
                    st0 = None if r == 'none' else VALUES[8][r]
                    c0 = fp_class(st0, 8) if st0 is not None else None
                    want = fp_class(VALUES[e][m], e)['den'] and (kind == 'fld' or (st0 is not None and not c0['nan'] and not (kind == 'divr' and c0['zero'])))
                    x0 = self.x87_check_word(form, VALUES[e][m], e, st0)
                    self.assertEqual(bool(x0 & 0x80), want, (name, m, r, hex(x0)))   # (a double denormal in a register is an 80-bit normal: ST0 den is no DE)
                    if kind != 'fld' and r == 'none': self.assertTrue(x0 >> 8 & 1, 'the stack fault is in the word')
                    n += 1
        self.assertGreater(n, 400)

    def test_x87_denormal_flag_is_the_m4s_idc_except_where_the_m4_deviates(self):
        """As the SIMD test: the M4's x87 code (a host with AFP) under the M4's FPCR raises IDC for the same memory operands, but where the M4
        deviates from x86 (below); a denormal double in ST0 is left out: the M4 reports DE for it (deviation 4 of the boot-features plan)."""
        n = 0
        for name, form, e, kind in self.X87:
            words, lines, _ = native_words(form, '--x87-unmasked', '0x2')
            i = next(k for k, l in enumerate(lines) if l == 'ldrb w0, [x28, #0x412]')
            j = next(k for k in range(i, len(lines)) if lines[k] == 'and w1, w0, w1')
            for m in ('one', 'den', 'dneg', 'dmax', 'minn', 'zero', 'inf', 'qnan', 'snan'):
                for r in (['none'] if kind == 'fld' else ['one', 'zero', 'inf', 'qnan', 'snan', 'none']):
                    st0 = None if r == 'none' else VALUES[8][r]
                    buf = bytearray(8192)
                    buf[0x4b2] = 0 if st0 is None else 1
                    if st0 is not None: buf[0x420:0x428] = st0.to_bytes(8, 'little')
                    buf[0x1000:0x1000 + e] = VALUES[e][m].to_bytes(e, 'little')
                    hw = native_run(words[:j + 1], buf=bytes(buf), ptrs=[(6, 0x1000)], fpcr=6)[0][0] >> 7 & 1
                    got = self.x87_check_word(form, VALUES[e][m], e, st0) >> 7 & 1
                    # (the M4's conversion of a single to a double is an op of its own, which raises IDC whatever ST0 holds: m32 arithmetic
                    # beside a NaN, or FDIVR by zero, has DE there and in no x86)
                    m4_artefact = e == 4 and kind != 'fld' and fp_class(VALUES[e][m], e)['den'] and (r in ('qnan', 'snan') or (kind == 'divr' and r == 'zero'))
                    self.assertTrue(got == hw or (m4_artefact and (got, hw) == (0, 1)), (name, m, r, got, hw))
                    n += 1
        self.assertGreater(n, 400)

    def test_x87_denormal_code_only_without_afp_and_with_de_unmasked(self):
        for name, form, e, kind in self.X87:
            for extra in (['--x87-unmasked', '0x2'], ['--x87-unmasked', '0x3f']):
                self.assertEqual(soft_de_words(native_words(form, *extra)[1]), 0, name)   # a host with AFP
                self.assertEqual(soft_de_words(native_words(form, *self.AFP0, *extra)[1]), 1, name)
            for mask in ('0x0', '0x3d', '0x1'):   # DE masked: a host with AFP has none of it; without, the flag is or-ed into the pending byte (AfpX87 judges it)
                self.assertFalse(any(l.startswith('and w') and l.endswith('#0x80') for l in native_words(form, '--x87-unmasked', mask)[1]), (name, mask))
                self.assertTrue(any(l.startswith('and w') and l.endswith('#0x80') for l in native_words(form, *self.AFP0, '--x87-unmasked', mask)[1]), (name, mask))
        # not the integer forms (fiadd m32, fiadd m16, fild), the register forms: no memory operand to test
        for form in ('da03', 'de03', 'df03', 'd8c1', 'dec1', 'd9c0', 'd8d1', 'dfe9', 'd9e4'):
            self.assertEqual(soft_de_words(native_words(form, *self.AFP0, '--x87-unmasked', '0x2')[1]), 0, form)

    # row 10
    def test_fma_inf_times_zero_with_a_qnan_addend_raises_no_ie_but_the_other_cases_do(self):
        cl = ['one', 'zero', 'inf', 'ninf', 'qnan', 'snan']
        n = 0
        for name, form, kind, e, lanes, srcs in [f for f in DE_FORMS if f[2] == 'fma']:
            val, per = VALUES[e], 16 // e
            words, lines, spills = native_words(form, *self.AFP0, '--simd-unmasked', '0x1')
            self.assertEqual(spills, 0, name)
            i = next(k for k, l in enumerate(lines) if l.startswith('mrs x0, FPSR'))
            for combo in itertools.product(cl, repeat=3):
                for pos in sorted({0, lanes - 1}):
                    ops = [[val['snan']] * per for _ in range(3)]       # (lanes not examined: SNaNs, which would raise IE if looked at)
                    for r in range(3):
                        for k in range(lanes): ops[r][k] = val['one']
                        ops[r][pos] = val[combo[r]]
                    want = any(ie_lane([ops[r][k] for r in range(3)], e, name, k) for k in range(lanes))
                    v = [0] * 32
                    for r, s in enumerate(srcs): v[16 + s] = self.pack(ops[r], e)
                    x, vs, _ = native_run(words[:i + 1], v=v)
                    self.assertEqual(bool(x[0] & 1), want, (name, pos, combo))
                    n += 1
        self.assertGreater(n, 2000)

    def test_fma_ie_is_the_m4s_ioc(self):
        """The IE of the FMA forms without AFP (sanitized) is the M4's hardware IOC (AH = 1), for every combination of operand classes."""
        n = 0
        for name, form, kind, e, lanes, srcs in [f for f in DE_FORMS if f[2] == 'fma']:
            val, per = VALUES[e], 16 // e
            words, lines, _ = native_words(form, '--simd-unmasked', '0x1')
            hw = words[:next(k for k, l in enumerate(lines) if l.startswith('mrs x0, FPSR')) + 1]
            sw, swl, _ = native_words(form, *self.AFP0, '--simd-unmasked', '0x1')
            sw = sw[:next(k for k, l in enumerate(swl) if l.startswith('mrs x0, FPSR')) + 1]
            for combo in itertools.product(('one', 'den', 'zero', 'inf', 'ninf', 'qnan', 'snan'), repeat=3):
                for pos in sorted({0, lanes - 1}):
                    ops = [[val['snan']] * per for _ in range(3)]
                    for r in range(3):
                        for k in range(lanes): ops[r][k] = val['one']
                        ops[r][pos] = val[combo[r]]
                    v = [0] * 32
                    for r, s in enumerate(srcs): v[16 + s] = self.pack(ops[r], e)
                    a, b = native_run(hw, v=v, fpcr=6)[0][0] & 1, native_run(sw, v=v)[0][0] & 1
                    self.assertEqual((a, b), (a, a), (name, pos, combo))
                    n += 1
        self.assertGreater(n, 2000)

    def test_fma_code_only_without_afp_and_with_ie_unmasked(self):
        for name, form, kind, e, lanes, srcs in [f for f in DE_FORMS if f[2] == 'fma']:
            sanitized = lambda *extra: sum(l.startswith('fcmeq') for l in native_words(form, *extra)[1])   # the three quiet compares
            self.assertEqual(sanitized(*self.AFP0, '--simd-unmasked', '0x1'), 3, name)
            self.assertEqual(sanitized('--simd-unmasked', '0x1'), 0, name)          # a host with AFP
            for mask in ('0x0', '0x3c', '0x20'):
                self.assertEqual(sanitized(*self.AFP0, '--simd-unmasked', mask), 0, (name, mask))   # IE masked: the plain code (decision D)

    def test_fma_results_are_unchanged_by_the_sanitizing(self):
        # normal operands: a*b + c exactly (small integers), lane by lane; with a QNaN addend the result is a NaN
        f32 = lambda f: struct.unpack('<I', struct.pack('<f', f))[0]
        for name in ('vfmadd231ps', 'vfmadd231ss'):
            name, form, kind, e, lanes, srcs = next(f for f in DE_FORMS if f[0] == name)
            words, lines, _ = native_words(form, *self.AFP0, '--simd-unmasked', '0x1')
            end = next(k for k, l in enumerate(lines) if l.startswith('mov w0, #'))   # the block's exit: the destination is written by then
            qn = 0x7fc00000
            v = [0] * 32
            v[16 + srcs[0]], v[16 + srcs[1]], v[16 + srcs[2]] = self.pack([f32(2.0)] * 4, 4), self.pack([f32(3.0)] * 4, 4), self.pack([f32(1.0), qn, f32(1.0), qn], 4)
            x, vs, _ = native_run(words[:end], v=v)
            dst = [vs[17] >> 32 * k & 0xffffffff for k in range(4)]
            self.assertEqual(struct.unpack('<f', struct.pack('<I', dst[0]))[0], 7.0, name)
            self.assertFalse(x[0] & 1, name)
            if lanes == 4:
                self.assertEqual(struct.unpack('<f', struct.pack('<I', dst[2]))[0], 7.0, name)
                self.assertTrue(fp_class(dst[1], 4)['nan'] and fp_class(dst[3], 4)['nan'], name)



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
            test.assertIsNone(re.match(r'svc\s', t) if t not in ('svc #0x3', 'svc #0x4', 'svc #0x5', 'svc #0x6') else None, (what, o, t))
            if t in ('svc #0x5', 'svc #0x6'):   # I6b: right after the movz of its function code or control word
                test.assertRegex(lines[i - 1][1], r'^mov w0, #0x[0-9a-f]+$', (what, o))
            # no frame or stack access but a guest-state load of the FS/GS base (I7: ldr xN, [x28, #0x3e0|#0x3e8])
            if not re.fullmatch(r'ldr x\d+, \[x28, #0x3e[08]\]', t):
                test.assertIsNone(re.search(r'\[(x28|sp)\b', t), (what, o, t))
            test.assertIsNone(re.match(r'ldr\w* \w+, 0x', t), (what, o, t))         # no literal load
            if t.lower() != 'mrs x25, nzcv':
                test.assertIsNone(re.match(r'\w+ [wx]25\b', t), (what, o, t))       # x25: only the pre-NZCV save
    test.assertTrue(exits, what)
    return exits, '\n'.join(f'{o:4x}: {t}' for o, t in lines)


# No-AFP scalar ops writing their register directly (FexcPrecision::MarkUpperZero): random straight-line sequences of scalar SSE/AVX
# ops, the values that make a register's upper bits known zero (loads, zeroing idioms, MOVQ/MOVD, copies, scalar results) and the ones
# that make them unknown (live-in registers with garbage upper bits, packed and shuffle ops, MOVSS/MOVSD/MOVLPD/MOVHPS merges, 256-bit
# ops), run natively. The oracle is the code FEX compiles for a host with AFP (its scalar ops merge in hardware: FPCR.NEP) run under the
# M4's FPCR, the code without AFP running with FPCR.AH too (else its default NaN differs, a deviation of its own); with $FEXC_OLD (a
# compiler without the change), also that compiler's code without AFP, both with an M2's FPCR. Bit for bit.
UZ_GPRS = [7, 5, 6, 8, 11, 12, 13, 14, 15, 19, 29, 26, 27]   # the static GPRs (rcx, rdx, ... PF, AF) but rax and the harness's x9 x10 x16 x17
UZ_FORMS = [
    # known zero above the element
    'movsd {d}, qword ptr [rax+{o}]', 'movss {d}, dword ptr [rax+{o}]', 'movq {d}, qword ptr [rax+{o}]', 'movd {d}, dword ptr [rax+{o}]',
    'movq {d}, rcx', 'movd {d}, ecx', 'xorps {d}, {d}', 'pxor {d}, {d}', 'vxorps {d}, {d}, {d}', 'movq {d}, {s}', 'vmovsd {d}, qword ptr [rax+{o}]',
    'vmovss {d}, dword ptr [rax+{o}]',
    # copies and merges
    'movaps {d}, {s}', 'movapd {d}, {s}', 'movsd {d}, {s}', 'movss {d}, {s}', 'vmovsd {d}, {s}, {t}', 'vmovss {d}, {s}, {t}', 'vmovaps {d}, {s}',
    'movlpd {d}, qword ptr [rax+{o}]', 'movhpd {d}, qword ptr [rax+{o}]', 'movlps {d}, qword ptr [rax+{o}]', 'movhps {d}, qword ptr [rax+{o}]',
    # scalar ops
    'addsd {d}, {s}', 'addss {d}, {s}', 'mulsd {d}, qword ptr [rax+{o}]', 'mulss {d}, {s}', 'subsd {d}, {s}', 'subss {d}, dword ptr [rax+{o}]',
    'divsd {d}, qword ptr [rax+{o}]', 'divss {d}, dword ptr [rax+{o}]', 'minsd {d}, {s}', 'maxsd {d}, qword ptr [rax+{o}]', 'minss {d}, {s}',
    'maxss {d}, {s}', 'sqrtsd {d}, qword ptr [rax+{o}]', 'sqrtss {d}, dword ptr [rax+{o}]', 'cvtss2sd {d}, {s}', 'cvtsd2ss {d}, {s}',
    'cvtss2sd {d}, dword ptr [rax+{o}]', 'cvtsi2sd {d}, rcx', 'cvtsi2ss {d}, ecx', 'cvtsi2sd {d}, dword ptr [rax+{o}]', 'cmpsd {d}, {s}, {i8}',
    'cmpss {d}, {s}, {i8}', 'roundsd {d}, {s}, {i16}', 'roundss {d}, {s}, {i16}', 'rcpss {d}, dword ptr [rax+{o}]',
    'rsqrtss {d}, dword ptr [rax+{o}]', 'vaddsd {d}, {s}, {t}', 'vmulss {d}, {s}, {t}', 'vsubsd {d}, {s}, qword ptr [rax+{o}]',
    'vdivss {d}, {s}, {t}', 'vminsd {d}, {s}, {t}', 'vmaxss {d}, {s}, {t}', 'vsqrtsd {d}, {s}, qword ptr [rax+{o}]', 'vcvtss2sd {d}, {s}, {t}',
    'vcvtsd2ss {d}, {s}, {t}', 'vcvtsi2sd {d}, {s}, rcx', 'vcvtsi2ss {d}, {s}, ecx', 'vcmpsd {d}, {s}, {t}, {i32}', 'vcmpss {d}, {s}, {t}, {i8}',
    'vroundsd {d}, {s}, {t}, {i16}', 'vfmadd231sd {d}, {s}, {t}', 'vfmadd213ss {d}, {s}, {t}', 'vfnmadd132sd {d}, {s}, {t}',
    'vfmsub231ss {d}, {s}, {t}', 'vfnmsub213sd {d}, {s}, qword ptr [rax+{o}]',
    # packed, bitwise and shuffles
    'addpd {d}, {s}', 'mulps {d}, {s}', 'unpcklpd {d}, {s}', 'unpcklps {d}, {s}', 'shufpd {d}, {s}, {i4}', 'movlhps {d}, {s}', 'movhlps {d}, {s}',
    'movups {d}, xmmword ptr [rax+{o16}]', 'pshufd {d}, {s}, {i256}', 'andpd {d}, {s}', 'xorpd {d}, {s}', 'andnpd {d}, {s}', 'orps {d}, {s}',
    'andpd {d}, xmmword ptr [rax+{o16}]', 'xorps {d}, xmmword ptr [rax+{o16}]', 'vandpd {d}, {s}, {t}', 'insertps {d}, {s}, {i256}',
    'vaddpd {D}, {S}, {T}', 'vxorpd {D}, {S}, {T}', 'vzeroupper', 'vbroadcastsd {D}, qword ptr [rax+{o}]',
    # stores
    'movsd qword ptr [rax+{o}], {s}', 'movups xmmword ptr [rax+{o16}], {s}', 'vmovups ymmword ptr [rax+{o16}], {S}',
]
UZ_SCALAR = re.compile(r'^v?(add|sub|mul|div|min|max|sqrt|cvtss2|cvtsd2|cvtsi2|cmp|round|rcp|rsqrt|fn?m(add|sub)\d+)s[sd]\b')
_UZ_ASM = {}


def x86_asm(lines):
    """The bytes of `lines` (Intel syntax), assembled by clang for x86-64."""
    key = tuple(lines)
    if key not in _UZ_ASM:
        with tempfile.TemporaryDirectory() as t:
            (Path(t) / 'a.s').write_text('.intel_syntax noprefix\n' + '\n'.join(lines) + '\n')
            subprocess.run(['clang', '-c', '-target', 'x86_64-linux-gnu', str(Path(t) / 'a.s'), '-o', str(Path(t) / 'a.o')], check=True, capture_output=True)
            f = (Path(t) / 'a.o').read_bytes()
        shoff, = struct.unpack_from('<Q', f, 0x28)
        shentsize, shnum, shstrndx = struct.unpack_from('<HHH', f, 0x3a)
        secs = [struct.unpack_from('<IIQQQQIIQQ', f, shoff + i * shentsize) for i in range(shnum)]
        names = secs[shstrndx][4]
        text = next(x for x in secs if f[names + x[0]:f.index(b'\0', names + x[0])] == b'.text')
        _UZ_ASM[key] = f[text[4]:text[4] + text[5]].hex()
    return _UZ_ASM[key]


def uz_block(fexc, hexs, *extra):
    """(words up to the block's exit `svc`, their text, spill bytes) of `hexs` compiled by `fexc`; every instruction translated."""
    r = subprocess.run([fexc, '--profile', 'slice1', '--hex', hexs, '--rip', hex(RIP), *extra], capture_output=True, text=True, timeout=120)
    d = json.loads(r.stdout)
    assert r.returncode == 0 and d['guest']['end']['reason'] == 'end_of_input', (hexs, extra, d['guest']['end'])
    lay = d['code']['layout']
    code = bytes.fromhex(d['code']['hex'])
    lits = [(x['offset'], x['size']) for x in d['code']['relocations'] if x['kind'] == 'guest_rip_literal']
    with tempfile.TemporaryDirectory() as t:
        dis = check_sites.disassemble(code, lay['code_region_end'], Path(t), lits)
    b, e = lay['code_region_begin'], lay['code_region_end']
    lines = [dis[o].strip() for o in range(b, e, 4)]
    n = next(k for k, l in enumerate(lines) if l.startswith('svc'))
    words = list(struct.unpack(f'<{n}I', code[b:b + 4 * n]))
    for k, l in enumerate(lines[:n]):   # a branch to a stub (an unmasked exception's #XM, a guard) falls through: the state is compared
        m = re.match(r'(b\.\w+|cbn?z|tbn?z) .*0x([0-9a-f]+)$', l)
        if m and int(m.group(2), 16) >= b + 4 * n: words[k] = 0xd503201f
    return words, lines[:n], lay['spill_bytes']


@unittest.skipUnless(os.access(FEXC, os.X_OK), f'fexc not built: {FEXC} (set $FEXC)')
class AfpX87(unittest.TestCase):
    """AFP fallback, x87 in default mode (afp=0): the masked DE of m32/m64 memory operands and the NaN results of arithmetic. The M4
    (FPCR 6, AFP code) is the oracle: the guest state after the instruction (ST registers as stored doubles, the pending flags, TOP, tags)
    is the same as the M2's (FPCR 0, afp=0 code), and FSW.DE of both is x86's rule (below). Both the M4's code and the afp=0 code fix the
    m32 convert's IDC (a denormal single beside a NaN in ST0, or FDIVR m32 over a zero ST0: the M4 hardware raises DE, x86 does not)."""
    D = dict(one=0x3ff0000000000000, two=0x4000000000000000, zero=0, nzero=1 << 63, inf=0x7ff0000000000000, ninf=0xfff0000000000000,
             qnan=0x7ff8000000000000, nqnan=0xfff8000000000000, qnanp=0x7ff8000000001234, snan=0x7ff0000000000001, nsnan=0xfff0000000000007,
             snanp=0x7ff4000000000000)
    M4 = {4: dict(one=0x3f800000, den=1, dmax=0x7fffff, dneg=0x80000001, zero=0, nzero=0x80000000, inf=0x7f800000, ninf=0xff800000,
                  qnan=0x7fc00000, nqnan=0xffc00000, qnanp=0x7fc01234, snan=0x7f800001, nsnan=0xff800007, snanp=0x7fa00000),
          8: dict(one=D['one'], den=1, dmax=0xfffffffffffff, dneg=(1 << 63) | 1, zero=0, nzero=1 << 63, inf=D['inf'], ninf=D['ninf'], qnan=D['qnan'],
                  nqnan=D['nqnan'], qnanp=D['qnanp'], snan=D['snan'], nsnan=D['nsnan'], snanp=D['snanp'])}
    MEM = [(n + ' m' + str(8 * e), n, h, e) for e in (4, 8) for n, h in zip(('fadd', 'fmul', 'fcom', 'fcomp', 'fsub', 'fsubr', 'fdiv', 'fdivr'),
           (['d803', 'd80b', 'd813', 'd81b', 'd823', 'd82b', 'd833', 'd83b'] if e == 4 else ['dc03', 'dc0b', 'dc13', 'dc1b', 'dc23', 'dc2b', 'dc33', 'dc3b']))] + \
          [('fld m32', 'fld', 'd903', 4), ('fld m64', 'fld', 'dd03', 8)]
    # register forms (ST0 op STi, STi op ST0, the P forms), FSQRT, FRNDINT, FCOM, FXAM-free negation/abs
    REG = ['d8c1', 'd8c9', 'd8e1', 'd8e9', 'd8f1', 'd8f9', 'dcc1', 'dcc9', 'dce1', 'dce9', 'dcf1', 'dcf9', 'dec1', 'dec9', 'dee1', 'dee9', 'def1', 'def9',
           'd9fa', 'd9fc', 'd8d1', 'd8d9', 'd9c0', 'd9e0', 'd9e1', 'dfe9']

    def run_form(self, form, afp, st0, st1, mem, e):
        words, lines, spills = native_words(form, *([] if afp else ['--feature', 'afp=0']))
        self.assertEqual(spills, 0, form)
        i = max(k for k, l in enumerate(lines) if l.startswith('svc'))
        while lines[i - 1].startswith(('mov w0', 'movk w0')): i -= 1
        buf = bytearray(8192)
        buf[0x4b2] = 3 if st1 is not None else 1   # tags: ST0 (and ST1) valid, TOP 0
        buf[0x420:0x428] = st0.to_bytes(8, 'little')
        if st1 is not None: buf[0x430:0x438] = st1.to_bytes(8, 'little')
        if mem is not None: buf[0x1000:0x1000 + e] = mem.to_bytes(e, 'little')
        return native_run(words[:i], buf=bytes(buf), ptrs=[(6, 0x1000)], fpcr=6 if afp else 0)[2][:0x500]

    def test_memory_operands_follow_the_m4_and_x86(self):
        n = 0
        for name, op, form, e in self.MEM:
            vals = self.M4[e]
            for a, st0 in self.D.items():
                for m, mem in vals.items():
                    r0, r1 = self.run_form(form, False, st0, None, mem, e), self.run_form(form, True, st0, None, mem, e)
                    nan0 = fp_class(st0, 8)['nan']
                    zero0 = fp_class(st0, 8)['zero']
                    # x86: DE for a denormal operand, none beside a NaN in ST0 (IE), a memory dividend over a zero ST0 is ZE
                    de = fp_class(mem, e)['den'] and (op == 'fld' or not (nan0 or (op == 'fdivr' and zero0)))
                    self.assertEqual(bool(r0[0x4b4] & 0x80), de, (name, a, m))
                    self.assertEqual(bool(r1[0x4b4] & 0x80), de, (name, a, m, 'AFP'))
                    self.assertEqual(r0, r1, (name, a, m))
                    n += 1
        self.assertGreater(n, 2000)

    def test_register_forms_follow_the_m4(self):
        for form in self.REG:
            for (a, st0), (b, st1) in itertools.product(self.D.items(), repeat=2):
                self.assertEqual(self.run_form(form, False, st0, st1, None, 8), self.run_form(form, True, st0, st1, None, 8), (form, a, b))

    def test_integer_operands_follow_the_m4(self):
        for form, e in (('da03', 4), ('da0b', 4), ('da2b', 4), ('da33', 4), ('de03', 2), ('de3b', 2), ('da13', 4), ('db03', 4)):
            for a, st0 in self.D.items():
                for m in (0, 1, 5, 0x7fffffff, 0x80000000, 0xffff):
                    m &= (1 << 8 * e) - 1
                    self.assertEqual(self.run_form(form, False, st0, None, m, e), self.run_form(form, True, st0, None, m, e), (form, a, m))

    def test_invalid_operations_give_the_negative_indefinite(self):
        """x86's real indefinite is the negative QNaN (AH = 0's default is the positive one); the NaN-operand cases are judged by the M4 above."""
        for form, st0, st1 in (('d8f1', self.D['zero'], self.D['zero']), ('d8e1', self.D['inf'], self.D['inf']), ('d8c9', self.D['inf'], self.D['zero']),
                               ('d8c1', self.D['inf'], self.D['ninf']), ('d9fa', 0xbff0000000000000, None), ('d9fa', self.D['ninf'], None)):
            got = int.from_bytes(self.run_form(form, False, st0, st1, None, 8)[0x420:0x428], 'little')
            self.assertEqual(got, 0xfff8000000000000, (form, hex(got)))


def kind_of(name):
    return name.rstrip('0123456789psd').replace('vfnmsub', 'vfnmsub')


@unittest.skipUnless(os.access(FEXC, os.X_OK), f'fexc not built: {FEXC} (set $FEXC)')
class AfpAccurateDe(unittest.TestCase):
    """AFP accurate plan, chunk 2 (B): with `--accurate` and DE masked a host without AFP sets MXCSR.DE (FPSR.IDC) by software. The oracle is the
    M4: the same instruction compiled for a host with AFP and run under the M4's FPCR (AH, NEP) against the afp=0 accurate code run under an
    M2's FPCR (0), on random operands (denormal, NaN, zero, infinity, normal classes in every lane, in registers, memory and the upper halves):
    FPSR.IDC is identical, and so is every result bit outside the lanes whose M4 result is a NaN (chunk 3's, the NaN rules). $ORACLE_TRIALS
    trials per form (default 60)."""
    AFP0 = ['--feature', 'afp=0', '--accurate']
    NO_DE = ('roundps', 'rcpps', 'cvtps2dq', 'cvttsd2si', 'vcvtph2ps xmm', 'vcvtph2ps ymm', 'vcvtph2ps m')   # no software flag at all
    MEM = 0x1000
    # (name, hex, source element bytes, result lane bytes). [rbx] is x6 (the memory operand at buf + 0x1000); register forms use xmm1/2/3.
    FORMS = [(n, h, e, e) for n, h, _, e, _, _ in DE_FORMS if n not in ('vcvtps2ph', 'vcvtps2pd ymm', 'cvtps2pd', 'cvtss2sd', 'cvtpd2ps', 'cvtsd2ss')] + [
        ('cvtps2pd', '0f5aca', 4, 8), ('cvtss2sd', 'f30f5aca', 4, 8), ('cvtpd2ps', '660f5aca', 8, 4), ('cvtsd2ss', 'f20f5aca', 8, 4),
        ('vcvtps2ph xmm', 'c4e3791dd100', 4, 2), ('vcvtps2ph ymm', 'c4e37d1dd100', 4, 2),
        ('vcvtps2pd ymm', 'c5fc5aca', 4, 8), ('vcvtpd2ps xmm,ymm', 'c5fd5aca', 8, 4), ('vcvtph2ps xmm', 'c4e27913ca', 2, 4), ('vcvtph2ps ymm', 'c4e27d13ca', 2, 4),
        ('vcvtph2ps m', 'c4e279130b', 2, 4),
        # memory operands
        ('addps m', '0f580b', 4, 4), ('addss m', 'f30f580b', 4, 4), ('mulsd m', 'f20f590b', 8, 8), ('divps m', '0f5e0b', 4, 4), ('divsd m', 'f20f5e0b', 8, 8),
        ('sqrtpd m', '660f510b', 8, 8), ('sqrtss m', 'f30f510b', 4, 4), ('minps m', '0f5d0b', 4, 4), ('cmpltps m', '0fc20b01', 4, 4), ('comiss m', '0f2f0b', 4, 4),
        ('ucomisd m', '660f2e0b', 8, 8), ('cvtps2pd m', '0f5a0b', 4, 8), ('cvtss2sd m', 'f30f5a0b', 4, 8), ('cvtpd2ps m', '660f5a0b', 8, 4),
        ('haddps m', 'f20f7c0b', 4, 4), ('addsubpd m', '660fd00b', 8, 8), ('vaddps m', 'c5e8580b', 4, 4), ('vmulsd m', 'c5eb590b', 8, 8),
        ('vfmadd231ps m', 'c4e269b80b', 4, 4), ('vfmadd231sd m', 'c4e2e9b90b', 8, 8), ('vfnmsub231ss m', 'c4e269bf0b', 4, 4),
        # 256-bit
        ('vaddps ymm', 'c5ec58cb', 4, 4), ('vsubpd ymm', 'c5ed5ccb', 8, 8), ('vmulps ymm m', 'c5ec590b', 4, 4), ('vdivps ymm', 'c5ec5ecb', 4, 4),
        ('vdivpd ymm m', 'c5ed5e0b', 8, 8), ('vsqrtps ymm', 'c5fc51ca', 4, 4), ('vsqrtpd ymm', 'c5fd51ca', 8, 8), ('vminpd ymm', 'c5ed5dcb', 8, 8),
        ('vhaddps ymm', 'c5ef7ccb', 4, 4), ('vhsubpd ymm', 'c5ed7dcb', 8, 8), ('vaddsubps ymm', 'c5efd0cb', 4, 4), ('vcmpltps ymm', 'c5ecc2cb01', 4, 4),
        ('vfmadd231ps ymm', 'c4e26db8cb', 4, 4), ('vfmadd231pd ymm', 'c4e2edb8cb', 8, 8), ('vfmsub231ps ymm', 'c4e26dbacb', 4, 4),
        ('vfnmadd231pd ymm', 'c4e2edbccb', 8, 8), ('vfmaddsub231ps ymm', 'c4e26db6cb', 4, 4), ('vfmsubadd231pd xmm', 'c4e2e9b7cb', 8, 8),
        ('vfmaddsub231ps xmm', 'c4e269b6cb', 4, 4), ('vfmadd231ps ymm m', 'c4e26db80b', 4, 4),
        # scalars (VEX), SSE4.1 dot products
        ('vaddss', 'c5ea58cb', 4, 4), ('vsubsd', 'c5eb5ccb', 8, 8), ('vsqrtss', 'c5ea51cb', 4, 4), ('vminsd', 'c5eb5dcb', 8, 8), ('vcmpeqss', 'c5eac2cb00', 4, 4),
        ('vcvtss2sd', 'c5ea5acb', 4, 8), ('vcvtsd2ss', 'c5eb5acb', 8, 4), ('vcomisd', 'c5f92fca', 8, 8), ('vucomiss', 'c5f82eca', 4, 4),
        ('dpps ff', '660f3a40caff', 4, 4), ('dpps 71', '660f3a40ca71', 4, 4), ('dpps 3f', '660f3a40ca3f', 4, 4), ('dppd 33', '660f3a41ca33', 8, 8),
        ('dppd 31', '660f3a41ca31', 8, 8), ('dppd ff m', '660f3a410bff', 8, 8), ('dpps f1 m', '660f3a400bf1', 4, 4),
        ('vdpps 71', 'c4e36940cb71', 4, 4), ('vdpps ymm ff', 'c4e36d40cbff', 4, 4), ('vdpps ymm 7f m', 'c4e36d400b7f', 4, 4), ('vdppd ff', 'c4e36941cbff', 8, 8),
        # not tested by software at all: no DE (rounding, reciprocals, integer conversions)
        ('roundps', '660f3a08ca04', 4, 4), ('rcpps', '0f53ca', 4, 4), ('cvtps2dq', '660f5bca', 4, 4), ('cvttsd2si', 'f20f2cc2', 8, 8),
    ]

    @staticmethod
    def lane(rnd, e):
        v = VALUES[e]
        k = rnd.random()
        bits, man = 8 * e, 23 if e == 4 else 52
        if k < 0.30: return v[rnd.choice(('one', 'minn', 'zero', 'nzero', 'inf', 'ninf', 'qnan', 'snan'))]
        if k < 0.50: return v[rnd.choice(('den', 'dmax', 'dneg'))]
        if k < 0.65: return rnd.getrandbits(bits - 1 - man) << man   # a zero mantissa: normal powers of two, zero or infinity
        if k < 0.80: return rnd.getrandbits(man) | rnd.getrandbits(1) << (bits - 1)   # a random denormal or zero
        return rnd.getrandbits(bits)

    @staticmethod
    def normal(rnd, e):
        """A lane with a nonzero exponent field (a normal, an infinity or a NaN): the exponent screen's skip."""
        bits, man = 8 * e, 23 if e == 4 else 52
        v = rnd.getrandbits(bits)
        return v if v >> man & ((1 << bits - 1 - man) - 1) else v | 1 << man

    def make(self, rnd, e, mode=0):
        """mode 0: the classes above; 1: every lane of every register has a nonzero exponent (the screen skips); 2: as 1 but one lane of
        one of xmm1, xmm2, xmm3, the memory operand or the upper halves is a zero or a denormal (the screen falls into the exact test)."""
        per = 16 // e
        lane = (lambda: self.lane(rnd, e)) if mode == 0 else (lambda: self.normal(rnd, e))
        vec = lambda: sum(lane() << 8 * e * k for k in range(per))
        xmm, hi, mem = [vec() for _ in range(16)], [vec() for _ in range(16)], vec() | vec() << 128 | vec() << 256 | vec() << 384
        if mode == 2:
            k = rnd.randrange(per)
            put = lambda v: v & ~(((1 << 8 * e) - 1) << 8 * e * k) | VALUES[e][rnd.choice(('den', 'dmax', 'dneg', 'zero', 'nzero'))] << 8 * e * k
            where = rnd.choice(('x1', 'x2', 'x3', 'mem', 'hi'))
            if where == 'mem':
                k = rnd.randrange(64 // e)
                mem = put(mem)
            elif where == 'hi':
                n = rnd.randrange(16); hi[n] = put(hi[n])
            else:
                n = int(where[1]); xmm[n] = put(xmm[n])
        return xmm, hi, mem

    def run_one(self, form, afp, xmm, hi, mem):
        words, lines, spills = native_words(form, *([] if afp else self.AFP0))
        self.assertEqual(spills, 0, form)
        i = max(k for k, l in enumerate(lines) if l.startswith('svc'))
        while lines[i - 1].startswith(('mov w0', 'movk w0')): i -= 1
        words = [0xd51b443f] + words[:i] + [0xd53b4420]   # msr fpsr, xzr ... mrs x0, fpsr
        buf = bytearray(8192)
        for n in range(16): buf[0xc0 + 16 * n:0xd0 + 16 * n] = hi[n].to_bytes(16, 'little')
        buf[self.MEM:self.MEM + 64] = mem.to_bytes(64, 'little')
        v = [0] * 32
        for n in range(16): v[16 + n] = xmm[n]
        x, v, b = native_run(words, v=v, buf=bytes(buf), ptrs=[(6, self.MEM)], fpcr=6 if afp else 0)
        return x[0], v[16:], b

    @staticmethod
    def same_outside_nans(m4, ours, e):
        """The bytes of two equal-length results, equal except in the e-byte lanes where `m4` holds a NaN (the NaN rules are chunk 3's)."""
        for k in range(0, len(m4), e):
            a, b = m4[k:k + e], ours[k:k + e]
            if a != b and not fp_class(int.from_bytes(a, 'little'), e)['nan']: return False
        return True

    def test_oracle(self):
        trials = int(os.environ.get('ORACLE_TRIALS', 60))
        rnd = random.Random(2026)
        n = idc = 0
        for name, form, e, re_ in self.FORMS:
            if e == 2: e = 4   # (half-precision sources: random 4-byte lanes are random half pairs)
            for t in range(trials):
                xmm, hi, mem = self.make(rnd, e, (0, 1, 2, 2)[t % 4])
                f4, v4, b4 = self.run_one(form, True, xmm, hi, mem)
                fa, va, ba = self.run_one(form, False, xmm, hi, mem)
                ctx = (name, t, [hex(q) for q in xmm[:4]], hex(mem), 'M4 FPSR', hex(f4), 'accurate', hex(fa))
                self.assertEqual(fa & 0x80, f4 & 0x80, ctx)
                self.assertTrue(all(self.same_outside_nans(a.to_bytes(16, 'little'), b.to_bytes(16, 'little'), re_) for a, b in zip(v4, va)), ctx)
                # memory (a store form's result), the upper halves and the rest of the guest state outside the NaN lanes
                self.assertTrue(self.same_outside_nans(b4[0xc0:0x1c0], ba[0xc0:0x1c0], re_), ctx)
                self.assertTrue(self.same_outside_nans(b4[self.MEM:self.MEM + 64], ba[self.MEM:self.MEM + 64], re_), ctx)
                self.assertEqual(b4[:0xc0] + b4[0x1c0:self.MEM], ba[:0xc0] + ba[0x1c0:self.MEM], ctx)
                n += 1
                idc += bool(f4 & 0x80)
        self.assertGreater(idc, n // 10)

    def test_fast_mode_and_daz_have_no_software_flag(self):
        for name, form, e, re_ in self.FORMS:
            fast = native_words(form, '--feature', 'afp=0')[1]
            daz = native_words(form, *self.AFP0, '--daz')[1]
            unmasked = native_words(form, *self.AFP0, '--simd-unmasked', '0x2')[1]
            # (the flag is `cbz; mrs x0, fpsr; orr x0, x0, #0x80; msr FPSR, x0`: no other FPSR write is in these ops' words)
            self.assertNotIn('msr FPSR, x0', fast, name)   # fast mode: the plain code, no test
            self.assertNotIn('msr FPSR, x0', daz, name)    # DAZ: x86 sets no DE
            self.assertIn('msr FPSR, x0', native_words(form, *self.AFP0)[1], name) if name not in self.NO_DE else None
            self.assertEqual(unmasked, native_words(form, '--feature', 'afp=0', '--simd-unmasked', '0x2')[1], name)   # DE unmasked: as fast mode

class AfpNegation(unittest.TestCase):
    """AFP fallback, the free NaN-sign fixes (afp=0, fast mode): the addend of the packed VFMSUB/VFNMSUB/VFMADDSUB/VFMSUBADD and ADDSUB's
    subtrahend are negated by `fmul` by -1.0 / (-1, 1, -1, 1) (then `fmul`'s result is added) instead of `fneg` / `fcadd`, so a NaN keeps
    its sign as on x86. Run natively on the M4: the afp=0 code under an M2's FPCR against the AFP code under the M4's (AH = 1), over every
    combination of operand classes (zeros, denormals, infinities, QNaN, negative QNaN, SNaN) in every lane position, under every DAZ/FTZ key:
    the result lanes and the FPSR's flags (IOC DZC OFC UFC IXC) are identical. Not compared, as documented fast-mode deviations of
    other causes: more than one NaN operand (FMA's NaN order), a NaN first multiplicand of VFNMSUB (its fmls is one op), inf * 0 beside a NaN (class D) and invalid operations without a NaN operand
    (the default NaN's sign). Every 13th batch, all of them with $AFP_NEG_FULL=1 (18 minutes). $FEXC_OLD (the compiler before the change) must fail the same comparison, and differ from the new afp=0 code
    in the sign of NaN lanes only (flags never, IDC included)."""
    FPSR_MASK = 0x1f   # IOC DZC OFC UFC IXC against the M4. IDC (0x80) is left out there: AH = 1 sets it for a denormal, FIZ does not, FZ does; an M2 sets it with FZ only (DE is software)
    CLASSES = {4: dict(one=0x3f800000, den=1, dneg=0x80000001, zero=0, nzero=0x80000000, inf=0x7f800000, ninf=0xff800000, qnan=0x7fc00000,
                       nqnan=0xffc00000, snan=0x7f800001),
               8: dict(one=0x3ff0000000000000, den=1, dneg=1 << 63 | 1, zero=0, nzero=1 << 63, inf=0x7ff0000000000000, ninf=0xfff0000000000000,
                       qnan=0x7ff8000000000000, nqnan=0xfff8000000000000, snan=0x7ff0000000000001)}
    KEYS = ((), ('--daz',), ('--ftz',), ('--daz', '--ftz'))
    # the registers (v16, v17, v18) = (xmm0, xmm1, xmm2): the multiplicands and the addend by operand order; the kind names ie_lane's rules
    ORDERS = {'231': (1, 2, 0), '213': (1, 0, 2)}
    KINDS = ('vfmsub', 'vfnmsub', 'vfmaddsub', 'vfmsubadd')

    def valid(self, e, ops, name, lane, daz=False):
        """The combination is compared: at most one NaN operand, and with a NaN no inf * 0 (D); with none, no invalid operation (under DAZ a
        denormal is a zero)."""
        if daz: ops = [o & 1 << 8 * e - 1 if fp_class(o, e)['den'] else o for o in ops]
        k = [fp_class(o, e) for o in ops]
        nans = sum(x['nan'] for x in k)
        if nans > 1 or (name == 'vfnmsub' and k[0]['nan']): return False   # (the one-op fmls negates the first multiplicand: a NaN there still flips)
        if name == 'addsub':   # x -/+ y (v16, v17): inf - inf
            return nans > 0 or not (k[0]['inf'] and k[1]['inf'] and (k[0]['neg'] == k[1]['neg']) == (lane % 2 == 0))
        if nans: return not ((k[0]['inf'] and k[1]['zero']) or (k[0]['zero'] and k[1]['inf']))
        return not ie_lane(ops, e, name, lane)

    def exec(self, fexc, hexs, e, lanes, key, afp):
        """(v16's lanes, FPSR flags) of the code of `hexs` for the lanes' (v16, v17, v18) operand values."""
        ck = (fexc, hexs, afp, key)
        if ck not in _NATIVE:
            words, _, spills = uz_block(fexc, hexs, *([] if afp else ['--feature', 'afp=0', *key]))
            self.assertEqual(spills, 0, hexs)
            # (the flags are cleared first and stored last: msr fpsr, xzr ... mrs x1, fpsr; str x1, [x28, #0x1ff8])
            _NATIVE[ck] = [0xd51b443f] + words + [0xd53b4421, 0xf9000000 | 0x3ff << 10 | 28 << 5 | 1]
        w = _NATIVE[ck]
        v = [0] * 32
        for j in range(len(lanes[0])): v[16 + j] = sum(lanes[i][j] << (8 * e * i) for i in range(len(lanes)))
        daz, ftz = '--daz' in key, '--ftz' in key
        r = native_run(w, v=v, fpcr=(6 | daz | ftz << 24) if afp else (daz and ftz) << 24)
        return [r[1][16] >> (8 * e * i) & (1 << 8 * e) - 1 for i in range(len(lanes))], struct.unpack_from('<Q', r[2], 0x1ff8)[0] & 0x9f

    def batches(self, e, roles, name, nops, daz=False):
        """Batches of the compared combinations, one per lane of the register: a combination sits in lanes of the parity it is valid for."""
        n = 16 // e
        every = list(itertools.product(self.CLASSES[e].values(), repeat=nops))
        pools = [[c for c in every if self.valid(e, [c[r] for r in roles] if nops == 3 else c, name, par, daz)] for par in (0, 1)]
        # (every 13th batch unless $AFP_NEG_FULL: the full product is about 68000 native runs, 18 minutes)
        for i in range(0, max(map(len, pools)), (n + 1) // 2 * (1 if os.environ.get('AFP_NEG_FULL') else 13)):
            yield [pools[l % 2][(i + l // 2) % len(pools[l % 2])] for l in range(n)]

    def compare(self, fexc, forms):
        """[(form, key, lanes, new result, M4 result, new flags, M4 flags)] of the batches that differ, and the number compared."""
        bad, n = [], 0
        for name, e, roles, text, nops, kind in forms:
            hexs = x86_asm([text])
            for key in self.KEYS:
                for b in self.batches(e, roles, kind, nops, '--daz' in key):
                    a0, f0 = self.exec(fexc, hexs, e, b, key, False)
                    a1, f1 = self.exec(FEXC, hexs, e, b, key, True)
                    n += 1
                    f0, f1 = f0 & self.FPSR_MASK, f1 & self.FPSR_MASK
                    if (a0, f0) != (a1, f1): bad.append((name, key, [tuple(hex(x) for x in c) for c in b], [hex(x) for x in a0], [hex(x) for x in a1], f0, f1))
        return bad, n

    def forms(self):
        fma = [(f'{k}{o}{t}', 4 if t == 'ps' else 8, r, f'{k}{o}{t} xmm0, xmm1, xmm2', 3, k) for k in self.KINDS for o, r in self.ORDERS.items() for t in ('ps', 'pd')]
        add = [(f'{v}addsub{t}', 4 if t == 'ps' else 8, (0, 1), f'{v}addsub{t} xmm0, ' + ('xmm0, xmm1' if v else 'xmm1'), 2, 'addsub')
               for v in ('', 'v') for t in ('ps', 'pd')]
        return fma, add

    def test_negations_keep_a_nans_sign_and_every_other_result_and_flag(self):
        fma, add = self.forms()
        bad, n = self.compare(FEXC, fma + add)
        self.assertGreater(n, 500)
        self.assertEqual(bad[:3], [], f'{len(bad)} of {n} batches differ')

    @unittest.skipUnless(os.environ.get('FEXC_OLD'), 'set $FEXC_OLD to the compiler before the change')
    def test_the_old_compiler_differs_in_nan_signs_only(self):
        old = os.environ['FEXC_OLD']
        fma, add = self.forms()
        bad, n = self.compare(old, fma + add)
        self.assertGreater(len(bad), 20)
        for name, key, lanes, a0, a1, f0, f1 in bad:
            e = 4 if name.endswith('ps') else 8
            self.assertEqual(f0, f1, (name, key, lanes))
            for x, y in zip(a0, a1):
                x, y = int(x, 16), int(y, 16)
                if x != y: self.assertTrue(fp_class(x, e)['nan'] and fp_class(y, e)['nan'] and x ^ y == 1 << 8 * e - 1, (name, key, hex(x), hex(y)))
        # the new compiler against the old one: the same flags everywhere, the results the same but a NaN's sign
        for name, e, roles, text, nops, kind in fma + add:
            hexs = x86_asm([text])
            for key in self.KEYS:
                for b in self.batches(e, roles, kind, nops, '--daz' in key):
                    a0, f0 = self.exec(FEXC, hexs, e, b, key, False)
                    a1, f1 = self.exec(old, hexs, e, b, key, False)
                    self.assertEqual(f0, f1, (name, key))

    SNAN, SNAN2, QNAN, NQNAN, INF = 0x7f800001, 0x7f800002, 0x7fc00000, 0xffc00000, 0x7f800000

    def lanes(self, fexc, text, ops, afp, e=4):
        return self.exec(fexc, x86_asm([text]), e, [tuple(ops)] * (16 // e), (), afp)

    def test_nan_corner_cases_are_pinned(self):
        """The cases the sweep above skips, each against the M4's code (the x86 result). Fast mode's documented differences: the addend of the
        packed VFMSUB/VFNMSUB and the subtracted lanes of VFMADDSUB/VFMSUBADD reach the FMA quieted (by the multiply: IE is raised for an
        SNaN), so an SNaN addend with inf * 0 gives the default NaN (x86: the SNaN, quieted) and a second NaN is chosen as Arm does; the added
        lanes of VFMADDSUB/VFMSUBADD reach it as they are (exact); ADDSUB's operand is quieted too, which makes the QNaN first operand win as in x86."""
        S, S2, Q, INF = self.SNAN, self.SNAN2, self.QNAN, self.INF
        # (v16, v17, v18) = (xmm0, xmm1, xmm2); the 231 forms add xmm0, multiplying xmm1 and xmm2
        new, m4 = self.lanes(FEXC, 'vfmsub231ps xmm0, xmm1, xmm2', (S, INF, 0), False), self.lanes(FEXC, 'vfmsub231ps xmm0, xmm1, xmm2', (S, INF, 0), True)
        self.assertEqual(m4, ([S | 1 << 22] * 4, 1))
        self.assertEqual(new, ([Q] * 4, 1))   # the default NaN: a documented difference (nan-results, fma-invalid-flag)
        # addend SNaN, a multiplicand SNaN: the SNaN multiplicand is chosen (as the M4 does)
        self.assertEqual(self.lanes(FEXC, 'vfmsub231ps xmm0, xmm1, xmm2', (S, S2, 0x3f800000), False), self.lanes(FEXC, 'vfmsub231ps xmm0, xmm1, xmm2', (S, S2, 0x3f800000), True))
        # VFMADDSUB: the added lanes keep an SNaN addend (inf * 0 + SNaN: the SNaN, quieted, as x86), the subtracted lanes give the default NaN
        new, m4 = (self.lanes(f, 'vfmaddsub231ps xmm0, xmm1, xmm2', (S, INF, 0), a)[0] for f, a in ((FEXC, False), (FEXC, True)))
        self.assertEqual(m4, [S | 1 << 22] * 4)
        self.assertEqual(sorted(new), [Q, Q, S | 1 << 22, S | 1 << 22])
        # ADDSUB: QNaN first operand, SNaN second: the QNaN, as x86
        for text in ('addsubps xmm0, xmm1', 'vaddsubps xmm0, xmm0, xmm1'):
            self.assertEqual(self.lanes(FEXC, text, (Q, S), False)[0], [Q] * 4)
            self.assertEqual(self.lanes(FEXC, text, (Q, S), True)[0], [Q] * 4)
        old = os.environ.get('FEXC_OLD')
        if old:
            self.assertEqual(self.lanes(old, 'vfmsub231ps xmm0, xmm1, xmm2', (S, INF, 0), False)[0], [0xffc00001] * 4)
            lanes = self.lanes(old, 'vfmaddsub231ps xmm0, xmm1, xmm2', (S, INF, 0), False)[0]
            self.assertEqual(sorted(lanes), [S | 1 << 22, S | 1 << 22, 0xffc00001, 0xffc00001])

    @unittest.skipUnless(os.environ.get('FEXC_OLD'), 'set $FEXC_OLD to the compiler before the change')
    def test_no_exact_case_becomes_wrong(self):
        """Every combination of operand classes (zeros, one, infinity, QNaN, negative QNaN, two SNaNs) in every position, all lanes alike, for
        each changed form: where the old compiler's afp=0 code equals the M4's result and flags, the new code does too, but for the packed
        FMSUB/FNMSUB with several QNaNs of both signs (the old negation flipped the addend's sign, which matched the M4 by chance; the choice
        between two NaNs is Arm's order, a documented difference whichever sign comes out)."""
        old = os.environ['FEXC_OLD']
        fma, add = self.forms()
        worse = []
        for name, e, roles, text, nops, kind in fma + add:
            cl = [0, self.CLASSES[e]['one'], self.CLASSES[e]['inf'], self.CLASSES[e]['qnan'], self.CLASSES[e]['nqnan'], self.CLASSES[e]['snan'], self.CLASSES[e]['snan'] + 1]
            hexs = x86_asm([text])
            for ops in itertools.product(cl, repeat=nops):
                lanes = [ops] * (16 // e)
                m4 = self.exec(FEXC, hexs, e, lanes, (), True)
                o, n = self.exec(old, hexs, e, lanes, (), False), self.exec(FEXC, hexs, e, lanes, (), False)
                m4, o, n = [(r[0], r[1] & self.FPSR_MASK) for r in (m4, o, n)]
                if o == m4 and n != m4: worse.append((name, ops, e))
        for name, ops, e in worse:
            k = [fp_class(x, e) for x in ops]
            qs = [x for x, c in zip(ops, k) if c['nan']]
            self.assertTrue(kind_of(name) in ('vfmsub', 'vfnmsub') and len(qs) >= 2 and not any(fp_class(x, e)['snan'] for x in qs) and len(set(qs)) > 1, (name, [hex(x) for x in ops]))

    def test_only_the_changed_forms_change_and_the_m4_code_is_the_old_code(self):
        """The changed forms' afp=0 code differs from the AFP code (the new lowering); the one-op forms (scalar fmsub/fnmsub, packed
        fnmadd) and the rest are as they were: with $FEXC_OLD the M4's code is byte-identical to the old compiler's for every line, and the
        afp=0 code differs from the old one for the changed lines only."""
        old = os.environ.get('FEXC_OLD')
        same = ['vfmadd231ps xmm0, xmm1, xmm2', 'vfnmadd231ps xmm0, xmm1, xmm2', 'vfmsub231sd xmm0, xmm1, xmm2', 'vfnmsub231ss xmm0, xmm1, xmm2',
                'vfmadd231pd ymm0, ymm1, ymm2', 'vfnmadd231pd ymm0, ymm1, ymm2', 'haddps xmm0, xmm1', 'subps xmm0, xmm1', 'xorps xmm0, xmm1']
        changed = ['vfmsub231ps xmm0, xmm1, xmm2', 'vfnmsub231pd xmm0, xmm1, xmm2', 'vfmaddsub231ps ymm0, ymm1, ymm2', 'vfmsubadd231pd xmm0, xmm1, xmm2',
                   'addsubps xmm0, xmm1', 'vaddsubpd ymm0, ymm1, ymm2']
        for line in same + changed:
            h = x86_asm([line])
            afp, afp0 = uz_block(FEXC, h)[0], uz_block(FEXC, h, '--feature', 'afp=0')[0]
            if line in changed: self.assertNotEqual(afp0, afp, line)
            if old:
                self.assertEqual(afp, uz_block(old, h)[0], line)
                self.assertEqual(afp0 == uz_block(old, h, '--feature', 'afp=0')[0], line in same, line)


@unittest.skipUnless(os.access(FEXC, os.X_OK), f'fexc not built: {FEXC} (set $FEXC)')
class AfpUpperZero(unittest.TestCase):
    AFP0 = ('--feature', 'afp=0')

    @staticmethod
    def ins(lines):   # FEX's insert of a scalar result into the register (`mov.d v16[0], v0[0]`, `mov.s ...`)
        return sum(bool(re.match(r'mov\.[hsd] v\d+\[0\], v0\[0\]$', l)) for l in lines)

    def test_known_zero_destinations_lose_the_insert(self):
        def ins(lines, *extra): return self.ins(uz_block(FEXC, x86_asm(lines), *self.AFP0, *extra)[1])
        z = ['movsd xmm0, qword ptr [rax]']
        self.assertEqual(ins(['addsd xmm0, xmm1']), 1)   # xmm0 live in: its bits 127:64 are the guest's
        self.assertEqual(ins(z + ['cvtsd2ss xmm0, xmm1']), 1)   # bits 63:32 of the double stay
        self.assertEqual(ins(['movss xmm0, dword ptr [rax]', 'cvtsd2ss xmm0, xmm1']), 0)
        for op in ('addsd xmm0, xmm1', 'mulsd xmm0, qword ptr [rax+8]', 'sqrtsd xmm0, xmm1', 'minsd xmm0, xmm1', 'cvtss2sd xmm0, xmm1',
                   'cvtsi2sd xmm0, rcx', 'cmpsd xmm0, xmm1, 1', 'roundsd xmm0, xmm1, 4', 'vfmadd231sd xmm0, xmm1, xmm2', 'divsd xmm0, xmm0'):
            self.assertEqual(ins(z + [op]), 0, op)
            for dirty in ('movhpd xmm0, qword ptr [rax+8]', 'unpcklpd xmm0, xmm1', 'addpd xmm0, xmm1', 'shufpd xmm0, xmm1, 1', 'movaps xmm0, xmm3'):
                self.assertEqual(ins(z + [dirty, op]), 1, (dirty, op))
        self.assertEqual(ins(['xorps xmm0, xmm0', 'cvtsi2ss xmm0, ecx', 'addss xmm0, xmm1', 'cvtss2sd xmm0, xmm0', 'addsd xmm0, xmm0']), 0)
        self.assertEqual(ins(['cvtsi2ss xmm0, ecx']), 1)   # into a live-in destination
        self.assertEqual(ins(['movss xmm0, dword ptr [rax]', 'addsd xmm0, xmm1']), 0)   # (bits 63:32 of the load are zero too)
        self.assertEqual(ins(['movsd xmm0, qword ptr [rax]', 'addsd xmm0, xmm1', 'addss xmm0, xmm1']), 1)   # an sd result: bits 63:32 unknown
        self.assertEqual(ins(['movsd xmm0, qword ptr [rax]', 'movss xmm0, xmm1', 'addsd xmm0, xmm1']), 0)   # merges into a zeroed register
        self.assertEqual(ins(['movsd xmm0, qword ptr [rax]', 'movsd xmm0, xmm1', 'addsd xmm0, xmm1']), 0)
        self.assertEqual(ins(['movsd xmm0, qword ptr [rax]', 'movsd xmm0, xmm1', 'addss xmm0, xmm1']), 1)   # bits 63:32 are xmm1's
        self.assertEqual(ins(['movss xmm0, dword ptr [rax]', 'movq xmm0, xmm3', 'addss xmm0, xmm1']), 1)   # MOVQ keeps bits 63:32
        self.assertEqual(ins(['movsd xmm2, qword ptr [rax]', 'vaddsd xmm0, xmm2, xmm1', 'mulsd xmm0, xmm1']), 0)   # VEX: src1's bits
        self.assertEqual(ins(['vaddsd xmm0, xmm2, xmm1']), 1)
        self.assertEqual(ins(['movsd xmm0, qword ptr [rax]', 'cmpneqsd xmm0, xmm1']), 1)   # NEQ/UNO/ORD insert themselves
        self.assertEqual(ins(['movq xmm0, qword ptr [rax]', 'xorpd xmm0, xmmword ptr [rax+16]', 'addsd xmm0, xmm1']), 1)   # a 16-byte mask
        self.assertEqual(ins(['movq xmm0, qword ptr [rax]', 'movq xmm3, qword ptr [rax+8]', 'xorpd xmm0, xmm3', 'addsd xmm0, xmm1']), 0)
        self.assertEqual(ins(['movsd xmm0, qword ptr [rax]', 'addsd xmm0, xmm1'], '--daz', '--ftz'), 0)   # through the flushes
        self.assertEqual(self.ins(uz_block(FEXC, x86_asm(['movsd xmm0, qword ptr [rax]', 'addsd xmm0, xmm1']))[1]), 0)   # AFP: no insert at all

    def test_sequences_keep_every_bit(self):
        rnd = random.Random(11)
        old = os.environ.get('FEXC_OLD')
        flt = lambda: struct.unpack('<I', struct.pack('<f', rnd.choice((1, -1)) * rnd.uniform(0.5, 100)))[0]
        vec = lambda: sum(flt() << 32 * k for k in range(4))
        n = removed = 0
        for case in range(220):
            regs = [f'xmm{k}' for k in range(6)]
            lines = []
            for _ in range(rnd.randint(3, 12)):
                f = rnd.choice(UZ_FORMS)
                d, s, t = rnd.choice(regs), rnd.choice(regs), rnd.choice(regs)
                lines.append(f.format(d=d, s=s, t=t, D=d.replace('x', 'y', 1), S=s.replace('x', 'y', 1), T=t.replace('x', 'y', 1),
                                      o=8 * rnd.randint(0, 15), o16=16 * rnd.randint(0, 7), i8=rnd.randint(0, 7), i32=rnd.randint(0, 31),
                                      i16=rnd.randint(0, 15), i4=rnd.randint(0, 3), i256=rnd.randint(0, 255)))
            hexs = x86_asm(lines)
            extra = rnd.choice(((), (), ('--daz',), ('--ftz',), ('--simd-unmasked', '0x2')))
            x = [rnd.getrandbits(64) for _ in range(32)]
            x[4], x[7] = 0, vec() & (1 << 64) - 1   # rax: the memory operands (x4 = buf + 0x1000); rcx: two floats
            v = [0] * 16 + [vec() for _ in range(16)]
            buf = bytearray(8192)
            for k in range(0x1000, 0x1100, 4): buf[k:k + 4] = struct.pack('<I', flt())
            for k in range(0x1800, 0x1a00, 4): buf[k:k + 4] = struct.pack('<I', flt())   # (the frame's upper YMM halves and around)
            words, text, spills = uz_block(FEXC, hexs, *self.AFP0, *extra)
            self.assertEqual(spills, 0, lines)
            run = lambda w, fpcr: native_run(w, x=x, v=v, buf=bytes(buf), ptrs=[(4, 0x1000)], fpcr=fpcr)
            state = lambda r: ([r[0][k] for k in UZ_GPRS], r[1][16:], r[2])
            # the AFP code ignores the keys (the M4's FPCR flushes: FIZ for DAZ, FZ for FTZ); AH for both, so their NaNs agree
            afp = 6 | ('--daz' in extra) | ('--ftz' in extra) << 24
            self.assertEqual(state(run(words, 2)), state(run(uz_block(FEXC, hexs, *extra)[0], afp)), (lines, extra))
            if old: self.assertEqual(state(run(words, 0)), state(run(uz_block(old, hexs, *self.AFP0, *extra)[0], 0)), (lines, extra))
            n += sum(bool(UZ_SCALAR.match(l)) for l in lines)
            if old: removed += self.ins(uz_block(old, hexs, *self.AFP0, *extra)[1]) - self.ins(text)
        self.assertGreater(n, 300)
        if old: print(f'\nAfpUpperZero: {n} scalar ops, {removed} inserts removed against $FEXC_OLD', file=sys.stderr)


@unittest.skipUnless(os.access(FEXC, os.X_OK), f'fexc not built: {FEXC} (set $FEXC)')
class AfpTiny(unittest.TestCase):
    """AFP fallback, decision C (unmasked case): without AFP and with UE unmasked the SIMD stub of a multiply, divide, FMA or narrowing
    convert names its operands' registers (crates/abi `tiny`: width = meta, imm = five bits per slot, guest_offset2 = the word after the
    check's `cbnz`); the registers hold the operands at the check, and nothing is described for a host with AFP, with UE masked or for an
    instruction with an add (whose tiny result is exact)."""
    AFP0 = ['--feature', 'afp=0']
    UE = ['--simd-unmasked', '0x10']
    MUL, DIV, FMA, CVT, E8, SC, H2, NP, NA = 1, 2, 3, 4, 8, 16, 32, 64, 128
    # name, hex, meta, guest xmm registers of the slots in order (xmm1 = v17 ...)
    FORMS = [
        ('mulss', 'f30f59ca', MUL | SC, (1, 2)), ('mulsd', 'f20f59ca', MUL | E8 | SC, (1, 2)), ('mulps', '0f59ca', MUL, (1, 2)),
        ('mulpd', '660f59ca', MUL | E8, (1, 2)), ('divss', 'f30f5eca', DIV | SC, (1, 2)), ('divpd', '660f5eca', DIV | E8, (1, 2)),
        ('vmulss', 'c5ea59cb', MUL | SC, (2, 3)), ('vdivps', 'c5e85ecb', DIV, (2, 3)),
        ('vfmadd231sd', 'c4e2e9b9cb', FMA | E8 | SC, (2, 3, 1)), ('vfmadd231ps', 'c4e269b8cb', FMA, (2, 3, 1)),
        ('vfmsub231ps', 'c4e269bacb', FMA | NA, (2, 3, 1)), ('vfnmadd231sd', 'c4e2e9bdcb', FMA | E8 | SC | NP, (2, 3, 1)),
        ('vfnmsub231pd', 'c4e2e9becb', FMA | E8 | NP | NA, (2, 3, 1)),
        ('cvtsd2ss', 'f20f5aca', CVT | E8 | SC, (2,)), ('cvtpd2ps', '660f5aca', CVT | E8, (2,)),
    ]

    def sites(self, h, *extra):
        d, rc = run(h, RIP, *extra)
        self.assertEqual(rc, 0, h)
        return [s for s in d['sites'] if s['kind'] == 'simd'], d['code']['layout']['code_region_begin']

    def test_the_site_names_the_operand_registers_and_the_word_after_the_branch(self):
        for name, h, meta, guest in self.FORMS:
            sites, begin = self.sites(h, *self.AFP0, *self.UE)
            self.assertEqual(len(sites), 1, name)
            st = sites[0]
            self.assertEqual(st['width'], meta, name)
            slots = [st['imm'] >> 5 * k & 31 for k in range(6)]
            self.assertEqual(st['imm'] >> 30, 0, name)
            words, lines, spills = native_words(h, *self.AFP0, *self.UE)
            at = (st['guest_offset2'] - begin) // 4
            self.assertTrue(lines[at - 1].startswith('cbnz w1,') and lines[at].startswith('ldrb w1,'), (name, lines[at - 1:at + 1]))
            vals = {n: 0x0123456789abcdef0011223344556677 * (n + 3) & (1 << 128) - 1 for n in range(16)}
            v = [0] * 32
            for n, bits in vals.items(): v[16 + n] = bits
            after = native_run(words[:at - 1], v=v)[1]
            for k, n in enumerate(guest):
                self.assertEqual(after[slots[k]], vals[n], (name, k, 'v%d holds xmm%d at the check' % (slots[k], n)))

    def test_256_bit_forms_have_two_halves_and_adds_have_none(self):
        sites, _ = self.sites('c5ec59cb', *self.AFP0, *self.UE)   # vmulps ymm1, ymm2, ymm3
        self.assertEqual([s['width'] for s in sites], [self.MUL | self.H2])
        sites, _ = self.sites('c4e37d19d101' if False else 'c5fd5acb', *self.AFP0, *self.UE)   # vcvtpd2ps xmm1, ymm3: not an add, a narrowing of two halves
        self.assertEqual([s['width'] for s in sites], [self.CVT | self.E8 | self.H2])
        for name, h in (('addss', 'f30f58ca'), ('addps', '0f58ca'), ('sqrtsd', 'f20f51ca'), ('cvtss2sd', 'f30f5aca'), ('haddps', 'f20f7cca'),
                        ('addsubps', 'f20fd0ca'), ('minss', 'f30f5dca'), ('vcvtps2ph', 'c4e3791dd100')):
            for st in self.sites(h, *self.AFP0, *self.UE)[0]:
                self.assertEqual((st['width'], st['imm'], st['guest_offset2']), (0, 0, None), name)

    def test_dpps_describes_its_multiplies_only(self):
        sites, _ = self.sites('660f3a40caff', *self.AFP0, *self.UE)
        described = [s['width'] for s in sites if s['width']]
        self.assertEqual(described, [self.MUL])   # (the add levels are exact: no description, the check stays as it was)

    def test_nothing_with_afp_or_ue_masked_or_the_other_masks_only(self):
        for name, h, meta, guest in self.FORMS:
            for extra in ((*self.UE,), (*self.AFP0, '--simd-unmasked', '0x20'), (*self.AFP0, '--simd-unmasked', '0x2f'), ()):
                for st in self.sites(h, *extra)[0]:
                    self.assertEqual((st['width'], st['imm'], st['guest_offset2']), (0, 0, None), (name, extra))
            # (the code of a host with AFP, and of UE masked, is the code without the FexcKeep ops: the replay streams pin it, and so
            # does this: the same words with or without the description)
        w, _, _ = native_words('f30f59ca', *self.AFP0, '--simd-unmasked', '0x10')
        self.assertGreater(len(w), 0)


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
                    self.assertEqual(n, 2 if name in ('jne rel8', 'bnd jne rel8', 'ds jne rel8 (hint, ignored)', 'gs jb rel8', 'je rel32', 'loop rel8', 'loope rel8', 'loopne rel8', 'jrcxz rel8',
                                                     'addr32 loop', 'jecxz', 'addr32 jne rel32', 'o16 jne rel32', 'o16 jne rel8', 'o16 loop', 'o16 jrcxz',
                                                     'fs o16 jb rel32') else 1, name)   # taken and fall-through gates
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
class Slice1X87Commit(unittest.TestCase):
    def test_fault_site_allows_pending_flags_but_rejects_early_architectural_flags(self):
        import verify_precision as vp
        d, rc = run('df38')  # FISTP qword ptr [rax]: conversion precedes the faultable store.
        self.assertEqual(rc, 0)
        code = bytes.fromhex(d['code']['hex'])
        with tempfile.TemporaryDirectory() as t:
            self.assertEqual(vp.verify(d, Path(t))[0], [])
            dis = check_sites.disassemble(code, d['code']['layout']['code_region_end'], Path(t))
            at = next(o for o, text in dis.items()
                      if check_sites.native_access(text) == ('store', 1, 'x28', None, vp.X87_PENDING, 'none'))
            old = int.from_bytes(code[at:at + 4], 'little')
            # Change the scratch-byte store to a word store into architectural x87 exception flags.
            word = 0xb9000000 | (0x4b4 // 4) << 10 | (old & 0x3ff)
            bad = dict(d, code=dict(d['code'], hex=(code[:at] + word.to_bytes(4, 'little') + code[at + 4:]).hex()))
            self.assertTrue(any('store' in e and 'before the site' in e for e in vp.verify(bad, Path(t))[0]))


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

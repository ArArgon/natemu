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
        self.assertEqual(leaf['fields']['ebx']['19'], {'name': 'ADX', 'state': 'advertised'})
        self.assertIn('adox rax,rcx', ALLOWED)

    def test_sha_follows_the_profile(self):
        # fexc has no profile input: the shim admits SHA because cpu_profile.json advertises it. Clearing the bit needs the
        # shim's #UD cut back (fexc_slice1.inc `Undefined0F38`, before November 3); the host's decode and `undefined` follow it.
        leaf = next(e for e in json.loads((REPO / 'tools/models/cpu_profile.json').read_text())['cpuid']
                    if e['leaf'] == '0x7' and e.get('subleaf') == 0)
        self.assertEqual(leaf['fields']['ebx']['29'], {'name': 'SHA', 'state': 'advertised'})
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

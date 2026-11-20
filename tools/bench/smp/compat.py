"""M2 task 3a programs: 32-bit code at CPL 3 under a 64-bit kernel body (natemu-smp-v1, `system`, one CPU, the tables and
descriptors of segments.py). The body loads its own IDT, whose handlers log each event from the 32-bit code (the frame,
CR2, the general registers and segment selectors as the handler found them, on request XMM0-15) into a captured page,
and either return to the 32-bit code (IRETQ, SYSRETL) or end the run from CPL 0. Positive programs are compared with
pinned QEMU system mode, run as an Intel processor (natemu presents one: SYSCALL is #UD outside 64-bit mode, SYSENTER
runs in it). `cases` are single instructions judged by segment_model and syscall_model (`params.model`, as
segments.py's): what QEMU does not model (no fault through a null segment, no segment limit) is listed in
run_slice3_vm.QEMU_DIFFERS, and natemu's deviation for segments it does not run is natemu's alone.

Registers: 32-bit code leaves the upper halves of RAX..RDI zero in natemu (decided, m2-task3a-findings) and unchanged
where it did not write them on hardware and QEMU, so every program enters 32-bit code with those halves zero or has
the 32-bit code write the register; R8..R15 and XMM8..XMM15 must come back as the body left them."""
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent.parent))
import programs as P
import segments as S
import syscalls as SC
from models import delivery_model as dm, segment_model as sm, syscall_model as sc

CPU = 'max,-pcid,-smap,vendor=GenuineIntel'      # smp.SYSTEM_CPU as an Intel processor
D = P.SYS_D(0)
F = S.fields(0)                                  # g GDT, p pointers, c results (captured), u user code, us user stack
U, US, W = F['u'], F['us'], F['c']
IDT, IDTR, XMM, LOG = D + 0x1000, D + 0x1900, D + 0x1a00, D + 0x2000
REC = LOG + 0x100                                # event k's record at REC + 0x200 * k; [LOG] counts events
VPAGE, XPAGE = D + P.V, D + P.X                  # pages the fault program unmaps
PTE = lambda va: P.sys_leaf(0, 0, va)            # (va below 4 MiB)
TA, TB = P.SYS_T(0, 0), P.SYS_T(0, 1)            # table A; table B's five pages are free here:
HI_PD, HI_PT, LOW0, LOW1, HIGH = TB, TB + 0x1000, TB + 0x2000, TB + 0x3000, TB + 0x4000
TLS = W + 0x80                                   # the base of GDT 0x28
TOP = 0xFFFF_F000                                # the base of GDT 0x30; its page maps HIGH
KSTACK = P.STACK(0) - 0x800                      # SYSENTER's stack (the IDT's handlers run on the TSS's RSP0)


def gdt():
    """segments.gdt_image with two DPL 3 data segments that have a base: 0x28 (TLS) and 0x30 (the last page below 4 GiB)."""
    image = bytearray(S.gdt_image(0))
    image[0x28:0x30] = sm.data(3, base=TLS).to_bytes(8, 'little')
    image[0x30:0x38] = sm.data(3, base=TOP).to_bytes(8, 'little')
    return bytes(image)


def gate(vector, label, dpl=0):
    """Writes IDT[vector]: a 64-bit interrupt gate to `label` in CS 0x08."""
    at = IDT + 16 * vector
    return f"""    lea rax, [rel {label}]
    mov [{at:#x}], ax
    mov word [{at + 2:#x}], 0x08
    mov word [{at + 4:#x}], {0x8e00 | dpl << 13:#x}
    shr rax, 16
    mov [{at + 6:#x}], ax
    shr rax, 16
    mov [{at + 8:#x}], eax
    mov dword [{at + 12:#x}], 0
"""


REGS64 = ('rax', 'rcx', 'rdx', 'rbx', 'rsp', 'rbp', 'rsi', 'rdi', 'r8', 'r9', 'r10', 'r11', 'r12', 'r13', 'r14', 'r15')
HEAD = (S.SETUP + '    mov eax, 0x70\n    ltr ax\n').format(**F) + f'    sidt [{IDTR + 0x10:#x}]\n' + \
    gate(6, '.v6') + gate(13, '.v13') + gate(14, '.v14') + gate(0x80, '.v128', 3) + f'    lidt [{IDTR:#x}]\n'
# The user's flat data segments (`data`; 0: null) and null FS/GS (an outer IRETQ nulls a DPL 0 one), then IRETQ to
# `cs`:U on the user stack with RFLAGS = 2. RAX is free until the frame is built: `regs` then sets what the 32-bit code
# starts with.
ENTER = lambda regs='', cs=0xdb, rsp=US, data=0x1b: f"""    mov eax, {data:#x}
    mov ds, eax
    mov es, eax
    xor eax, eax
    mov fs, eax
    mov gs, eax
    push 0x1b
    mov rax, {rsp:#x}
    push rax
    push 2
    push {cs:#x}
    push {U:#x}
{regs}    iretq
"""


def handlers(dispatch='    jmp .fin\n', xmm=False):
    """The entry stubs and the common handler: the event's record (+0 vector, error code, RIP, CS, RFLAGS under 10ED5H,
    RSP, SS, CR2 for a #PF; +0x40 the 16 general registers, RSP's the frame's; +0xC0 DS, ES, FS, GS; +0x100 XMM0..15
    with `xmm`), then `dispatch` (RBX the record's offset, the frame at RSP; RAX free): `.resume` returns by IRETQ,
    `.fin` ends the run. SYSENTER (`.sysenter`, vector 100H) builds the same frame by hand: RIP its own entry, RSP the
    user's EBP."""
    rec = lambda at: f'[{REC + at:#x} + rbx]'
    text = ''.join(f".v{v}:\n{'' if v in (13, 14) else '    push 0' + chr(10)}    push {v}\n    jmp .common\n" for v in (6, 13, 14, 128))
    text += f""".sysenter:
    push rax
    sub rsp, 56
    pushfq
    pop rax
    mov [rsp+32], rax
    xor eax, eax
    mov ax, ss
    mov [rsp+48], rax
    mov ax, cs
    mov [rsp+24], rax
    mov [rsp+40], rbp
    lea rax, [rel .sysenter]
    mov [rsp+16], rax
    mov qword [rsp+8], 0
    mov qword [rsp], 0x100
    mov rax, [rsp+56]
.common:
    push rbx
    mov rbx, [{LOG:#x}]
    shl rbx, 9
"""
    text += ''.join(f'    mov {rec(0x40 + 8 * k)}, {r}\n' for k, r in enumerate(REGS64) if r not in ('rbx', 'rsp'))
    text += f'    pop rax\n    mov {rec(0x58)}, rax\n    mov rax, [rsp+40]\n    mov {rec(0x60)}, rax\n'
    for k in range(7):
        text += f'    mov rax, [rsp+{8 * k}]\n' + ('    and eax, 0x10ed5\n' if k == 4 else '') + f'    mov {rec(8 * k)}, rax\n'
    text += f'    xor eax, eax\n    cmp qword [rsp], 14\n    jne .n14\n    mov rax, cr2\n.n14:\n    mov {rec(0x38)}, rax\n'
    text += ''.join(f'    xor eax, eax\n    mov ax, {s}\n    mov {rec(0xc0 + 8 * k)}, rax\n' for k, s in enumerate(('ds', 'es', 'fs', 'gs')))
    if xmm:
        text += ''.join(f'    movdqu {rec(0x100 + 16 * k)}, xmm{k}\n' for k in range(16))
    text += f'    inc qword [{LOG:#x}]\n' + dispatch
    # (The start-up IDT and GS base again: QEMU's stub needs both at the end. The upper halves of the registers the
    # 32-bit code holds are whatever it left: the records compare them; the end state does not.)
    text += f""".resume:
    mov rax, {rec(0x40)}
    mov rbx, {rec(0x58)}
    add rsp, 16
    iretq
.fin:
    lidt [{IDTR + 0x10:#x}]
""" + S.RESTORE_GS.format(**F) + ''.join(f'    mov {r}, {r}\n' for r in ('ebx', 'ebp', 'esi', 'edi'))
    return text


def user32(text):
    return P.asm(f'org {U:#x}\nbits 32\n' + text)


def entry(address, value):
    return {'addr': f'0x{address:x}', 'data': value.to_bytes(8, 'little').hex(), 'capture': False}


# Linear 0 and 1000H (LOW0, LOW1) and FFFF_F000H (HIGH) mapped for user code: where 32-bit addresses wrap to.
WRAP = [entry(TA + 0x1018, HI_PD | P.P | P.RW | P.US | P.A), entry(HI_PD + 511 * 8, HI_PT | P.P | P.RW | P.US | P.A),
        entry(HI_PT + 511 * 8, HIGH | P.LEAF), entry(TA + 0x3000, LOW0 | P.LEAF), entry(TA + 0x3008, LOW1 | P.LEAF),
        P.zero(LOW0, 0x1000), P.zero(LOW1, 0x1000), P.zero(HIGH, 0x1000)]
# The same linear pages not present (and 1_0000_0000H): the tables' entries QEMU would otherwise read undefined.
NOT_MAPPED = [entry(TA + 0x1018, 0), entry(TA + 0x1020, 0), entry(TA + 0x3000, 0)]
# FFFF_F000H alone: an access that runs past 4 GiB finds nothing there in QEMU, wherever it goes on.
TOP_ONLY = WRAP[:3] + [entry(TA + 0x1020, 0), entry(TA + 0x3000, 0), P.zero(HIGH, 0x1000)]


def program(name, body, user, memory=(), params=None):
    """`body`: CPL 0 text after HEAD (it enters the 32-bit code `user` at U); the handlers follow it."""
    idtr = (0x80 * 16 + 15).to_bytes(2, 'little') + IDT.to_bytes(8, 'little')
    pattern = b''.join(bytes([0x10 * k + j for j in range(16)]) for k in range(16))
    mem = S.memory(0, 1, gdt=gdt()) + [
        {'addr': f'0x{U:x}', 'data': user32(user).hex()}, dict(P.zero(IDT, 0x810), capture=False),
        # The IDTR to load, room for the start-up one (QEMU's stub's, natemu's driver's: they differ), XMM patterns.
        {'addr': f'0x{IDTR:x}', 'data': (idtr + bytes(0x26)).hex(), 'capture': False},
        {'addr': f'0x{XMM:x}', 'data': pattern.hex(), 'capture': False}, P.zero(LOG, 0x900)] + list(memory)
    return {'format': 'natemu-smp-v1', 'name': name, 'ncpu': 1, 'system': True, 'cpu': CPU,
            'bodies': [P.body(P.SYS_CODE(0), P.sys_text(0, HEAD + body))], 'cpus': [dict(P.cpu(0, P.SYS_CODE(0)), cpl=0)],
            'memory': mem, 'params': params or {}}


# ---- IRETQ into 32-bit code, INT 0x80 out of it, twice: the 64 -> 32 -> 64 round trip ------------------------------------
# The body gives every general register a distinct upper half and XMM0..15 patterns, and enters with RSP's upper half set
# (IRETQ loads ESP). The 32-bit code writes each of EAX..EDI with a 32-bit instruction (the upper half is then zero on
# hardware too), runs the one-byte INC/DEC, a call, pushes and pops, a string move, PUSHFD, MOVD, prefixes of the null FS
# and GS on instructions that access nothing, and reads its selectors; the handler logs R8..R15 and XMM8..15 as it finds
# them, changes R8 and XMM8 and returns; the second INT 0x80 ends the run.
UPPER = lambda k: (0x1111_1111 * (k + 1) & 0xFFFF_FFFF) << 32 | 0x100 + k
# SSE on (CR4.OSFXSR) and XMM0..15 patterns; distinct values in R8..R15 (set after ENTER's frame is built).
XMMS = '    mov rax, cr4\n    bts rax, 9\n    mov cr4, rax\n' + ''.join(f'    movdqu xmm{k}, [{XMM + 16 * k:#x}]\n' for k in range(16))
HIGH_REGS = ''.join(f'    mov {r}, {UPPER(k):#x}\n' for k, r in enumerate(REGS64) if k >= 8)
ROUND_BODY = XMMS + ENTER(
    ''.join(f'    mov {r}, {UPPER(k):#x}\n' for k, r in enumerate(REGS64) if r != 'rsp'), rsp=0x5555_5555_0000_0000 | US) + handlers(f"""    cmp qword [{LOG:#x}], 2
    jae .fin
    inc r8
    pshufd xmm8, xmm8, 0x1b
    jmp .resume
""", xmm=True)
ROUND_USER = f"""    mov eax, eax
    inc ecx
    dec edx
    lea ebx, [ebx+1]
    movzx ebp, bp
    xor esi, 0x55
    add edi, edi
    db 0x64
    lea eax, [eax+2]
    db 0x65, 0x89, 0xc9
    mov [{W:#x}], esp
    push eax
    push ecx
    pop dword [{W + 4:#x}]
    pop dword [{W + 8:#x}]
    call .f
    pushfd
    pop dword [{W + 12:#x}]
    mov esi, .text
    mov edi, {W + 0x10:#x}
    mov ecx, 5
    rep movsb
    movd xmm0, eax
    movd xmm7, ebx
    paddd xmm1, xmm7
    mov eax, cs
    mov [{W + 0x20:#x}], eax
    mov eax, ss
    mov [{W + 0x24:#x}], eax
    mov eax, ds
    mov [{W + 0x28:#x}], eax
    mov eax, fs
    mov [{W + 0x2c:#x}], eax
    int 0x80
    add eax, 0x1000
    not ecx
    movdqa xmm2, xmm0
    int 0x80
.f:
    add ebx, 0x10
    ret
.text: db 'natem'
"""

# ---- exact page faults in 32-bit code ------------------------------------------------------------------------------------
# A store to a page not present in the middle of a block, a store to a read-only page, a PUSH onto a page not present
# (ESP unchanged in the frame), each retried after the handler maps the page; then a load at FFFF_FFFCH, where nothing is
# mapped, reached as [EBP-8] with EBP = 4: CR2 is the 32-bit address. The handler ends the run at a fault above 4 MiB.
# Each record holds R8..R15 and XMM8..15 as the fault's handler found them: the body's values.
FAULT_BODY = f"""    and qword [{PTE(VPAGE):#x}], -2
    invlpg [{VPAGE:#x}]
    and qword [{PTE(XPAGE):#x}], -2
    invlpg [{XPAGE:#x}]
""" + XMMS + ENTER(HIGH_REGS) + handlers(f"""    cmp qword [rsp], 14
    jne .fin
    mov rax, cr2
    cmp rax, 0x400000
    jae .fin
    invlpg [rax]
    shr rax, 12
    or qword [{PTE(0):#x} + rax*8], 3
    jmp .resume
""", xmm=True)
FAULT_USER = f"""    mov eax, 0x11
    mov ebx, 0x22
    mov ecx, 0x33
    mov edx, 0x44
    mov esi, 0x55
    mov edi, 0x66
    mov ebp, 0x77
    add eax, ebx
    mov [{VPAGE + 0x10:#x}], eax
    add eax, 1
    mov [{D + S.RO + 0x800:#x}], eax
    sub ecx, 1
    mov esi, esp
    mov esp, {XPAGE + 0x800:#x}
    push eax
    pop edx
    mov esp, esi
    mov ebp, 4
    mov eax, [ebp-8]
"""

# ---- SYSENTER out of 32-bit code, SYSRETL back, INT 0x80 -----------------------------------------------------------------
# The user passes its return address in EDX and its stack in EBP (Linux's convention); the handler returns by SYSRETL (CS
# 0x13, SS 0x1b from STAR; R11 = ZF PF; RCX with its upper half set: RIP := ECX), and the INT 0x80 that follows logs
# those selectors in its frame.
SYSENTER_BODY = '    ' + SC.EFER_SCE + SC.wrmsr(0xc0000081, SC.STAR) + SC.wrmsr(0x174, 0x08) + SC.wrmsr(0x175, KSTACK) + """    lea rax, [rel .sysenter]
    mov rdx, rax
    shr rdx, 32
    mov ecx, 0x176
    wrmsr
    xor edx, edx
    xor ecx, ecx
""" + ENTER() + handlers(f"""    cmp qword [rsp], 0x100
    jne .fin
    mov rax, [{REC + 0x40:#x} + rbx]
    mov rbx, [{REC + 0x58:#x} + rbx]
    mov rcx, 0x7777777700000000
    or rcx, rdx
    mov r11d, 0x46
    mov esp, ebp
    sysret
""")
SYSENTER_USER = f"""    mov eax, 0x1234
    mov ebx, 0x22
    mov ecx, 0x33
    mov esi, 0x55
    mov edi, 0x66
    mov edx, .back
    mov ebp, esp
    sysenter
.back:
    mov ecx, 0x33
    mov ebp, cs
    mov [{W:#x}], ebp
    mov ebp, ss
    mov [{W + 4:#x}], ebp
    pushfd
    pop dword [{W + 8:#x}]
    mov [{W + 12:#x}], esp
    mov ebp, 0x77
    int 0x80
"""

# ---- addresses that wrap at 4 GiB ----------------------------------------------------------------------------------------
# Segment base plus offset (GS at TLS, negative offsets as immediate and in a register, as static i386 programs reach
# thread-local data; GS at FFFF_F000H with offsets that pass 4 GiB), base register plus displacement (EBP = 4 minus 12;
# EBX = FFFF_FFF0H plus 40H), and the stack pointer (PUSH at ESP = 0; POP at ESP = FFFF_FFFCH, then stores above ESP = 0:
# first one across a page, which the resident completes itself, then one within a page).
WRAP_BODY = ENTER() + handlers()
WRAP_USER = f"""    mov ecx, 0x33
    mov edx, 0x44
    mov esi, 0x55
    mov edi, esp
    mov eax, 0x2b
    mov gs, eax
    mov dword [gs:0xfffffffc], 0x11111111
    mov eax, 0xfffffff8
    mov dword [gs:eax], 0x22222222
    mov ecx, [gs:0xfffffffc]
    mov eax, 0x33
    mov gs, eax
    mov dword [gs:0x10], 0x33333333
    mov dword [gs:0x1020], 0x44444444
    add edx, [gs:0x10]
    mov ebp, 4
    mov dword [ebp-12], 0x55555555
    mov dword [ebp+0x100], 0x5a5a5a5a
    mov ebx, 0xfffffff0
    mov dword [ebx+0x40], 0x66666666
    mov esp, 0
    push 0x77777777
    mov esi, esp
    pop eax
    mov dword [esp+0xffe], 0x99999999
    mov dword [esp+0x40], 0x88888888
    mov ebx, esp
    mov esp, edi
    int 0x80
"""


# ---- 32-bit code with a null DS and ES that never uses them --------------------------------------------------------------
# x86 faults at the first access through a null segment, not before: registers, the stack (ESP and EBP bases, an SS
# override on an EBX base), a call, REP string instructions with a zero count (no access), LEA and a DS prefix on a
# register form all run.
NULL_BODY = ENTER(data=0) + handlers()
NULL_USER = """    mov eax, 0x11
    mov ebx, esp
    mov ecx, 0x33
    mov edx, 0x44
    mov esi, 0x55
    mov edi, 0x66
    push eax
    push ecx
    mov ebp, esp
    add eax, [ebp+4]
    mov [esp], eax
    add edx, [ss:ebx-4]
    call .f
    pop esi
    pop edi
    xor ecx, ecx
    rep movsb
    rep stosd
    repe cmpsb
    lea ecx, [ebx+8]
    db 0x3e, 0x01, 0xc8
    cmp eax, eax
    int 0x80
.f:
    inc edx
    ret
"""


# ---- IRETQ from 64-bit code at CPL 3 into 32-bit code --------------------------------------------------------------------
# The body enters 64-bit user code (CS 0x23), which builds the frame itself; the 32-bit code then leaves by INT 0x80.
USER_IRET_BODY = ENTER(cs=0x23) + handlers()
USER_IRET_USER = f"""bits 64
    mov ebx, 0x22
    mov ecx, 0x33
    mov edx, 0x44
    mov esi, 0x55
    mov edi, 0x66
    mov ebp, 0x77
    push 0x1b
    push {US - 0x100:#x}
    push 0x46
    push 0xdb
    mov eax, .in32
    push rax
    iretq
bits 32
.in32:
    pushfd
    pop eax
    mov ebx, cs
    mov ecx, esp
    push eax
    pop edx
    int 0x80
"""


def positive():
    return [program('c32-roundtrip@1', ROUND_BODY, ROUND_USER), program('c32-null-ds@1', NULL_BODY, NULL_USER),
            program('c32-iret-cpl3@1', USER_IRET_BODY, USER_IRET_USER),
            program('c32-fault@1', FAULT_BODY, FAULT_USER, [P.zero(VPAGE, 0x1000), P.zero(XPAGE, 0x1000)] + NOT_MAPPED),
            program('c32-sysenter@1', SYSENTER_BODY, SYSENTER_USER),
            program('c32-wrap@1', WRAP_BODY, WRAP_USER, WRAP)]


# ---- single instructions judged by the models ----------------------------------------------------------------------------
def state():
    """segment_model's state in the 32-bit code: CS 0xdb, SS = DS = ES = 0x1b, FS and GS null; SYSCALL enabled."""
    s = SC.state(3, efer=0xd01)
    s['seg'].update(cs=(0xdb, 0xc0fb), ds=(0x1b, 0xc0f3), es=(0x1b, 0xc0f3))
    s.update(cs=0xdb, rsp=US)
    dm.place(s['memory'], F['g'], gdt())
    return s


def cases():
    """(name, user code before the instruction, the instruction, model(state) -> result, memory[, the instruction's
    address when it is not in the user code])."""
    last = dict(entry(HIGH + 0xff8, 0xb8 << 56), capture=True)      # FFFF_FFFFH: the first byte of MOV EAX, imm32
    return [
        # SYSCALL is #UD outside 64-bit mode on the Intel processor natemu presents, EFER.SCE or not.
        ('syscall-ud', '', 'syscall', lambda s: sc.syscall(s), []),
        # An access through a null FS or GS; a prefix on an instruction that accesses nothing is covered by c32-roundtrip.
        # DS and ES loaded with null in the 32-bit code, which runs on (the stack, a DS access while only ES is null)
        # until the first access through the null one: a load through DS, a string store through ES.
        ('ds-null', '    xor eax, eax\n    mov ds, eax\n    push 0xc\n    pop ecx\n    add ecx, 4\n', 'mov eax, [ecx]',
         lambda s: sm.null_access(_null(s, 'ds'), 'ds'), NOT_MAPPED),
        ('es-null', f'    xor eax, eax\n    mov es, eax\n    mov edx, [{W:#x}]\n    mov edi, 0x10\n', 'stosd',
         lambda s: sm.null_access(_null(s, 'es'), 'es'), NOT_MAPPED),
        ('fs-null', '', 'mov eax, [fs:0x10]', lambda s: sm.null_access(s, 'fs'), NOT_MAPPED),
        ('gs-null', '    mov ebx, 0x10\n', 'add [gs:ebx], eax', lambda s: sm.null_access(s, 'gs'), NOT_MAPPED),
        ('gs-null-string', f'    mov esi, {W:#x}\n    mov edi, {W + 8:#x}\n', 'gs movsd', lambda s: sm.null_access(s, 'gs'), NOT_MAPPED),
        # Across the 4 GiB limit of the flat segments: #GP(0), #SS(0) through SS (an EBP base).
        ('limit', '    mov ebx, 0xfffffffe\n', 'mov eax, [ebx]', lambda s: sm.flat_access(s, 0xFFFF_FFFE, 4), TOP_ONLY),
        ('limit-ss', '    mov ebp, 0xfffffffd\n', 'mov [ebp], eax', lambda s: sm.flat_access(s, 0xFFFF_FFFD, 4, stack=True), TOP_ONLY),
        # An instruction that runs past 4 GiB: #GP(0) at it (the JMP EAX before it completes).
        ('limit-fetch', '    mov eax, 0xffffffff\n', 'jmp eax', lambda s: sm.flat_access(dict(s, rip=0xFFFF_FFFF), 0xFFFF_FFFF, 5),
         TOP_ONLY[:-1] + [P.zero(HIGH, 0xff8), last], 0xFFFF_FFFF),
        # natemu's deviation: DS loaded with a segment that has a base, in 32-bit code: #GP(0x28) before the next instruction.
        ('ds-based', '    mov eax, 0x2b\n', 'mov ds, eax', lambda s: sm.load(s, 'ds', 0x2b), []),
    ]


def _null(s, sreg):
    s['seg'][sreg] = (0, 0)
    return s


def fault_programs():
    """Each case as a program whose 32-bit code ends with the instruction under test (segments.fault_programs' shape):
    the body enters it by IRETQ with the start-up IDT (QEMU's stub and natemu's driver log the fault)."""
    body = (S.SETUP + '    mov eax, 0x70\n    ltr ax\n' + '    ' + SC.EFER_SCE + SC.wrmsr(0xc0000081, SC.STAR)).format(**F) + \
        '    xor edx, edx\n    xor ecx, ecx\n    mov eax, 0x1b\n    mov ds, eax\n    mov es, eax\n    xor eax, eax\n    mov fs, eax\n    mov gs, eax\n' + \
        S.RESTORE_GS.format(**F) + f'    xor eax, eax\n    xor edx, edx\n    xor ecx, ecx\n    push 0x1b\n    push {US:#x}\n    push 2\n    push 0xdb\n    push {U:#x}\n    iretq\n'
    out = []
    for name, head, insn, call, memory, *elsewhere in cases():
        at = U + len(user32(head))
        s = state()
        s.update(rip=at, length=len(user32(insn)))
        r = call(s)
        at = elsewhere[0] if elsewhere else at
        zero = lambda x: x and [x[0], x[1] or 0, x[2]]
        prog = {'format': 'natemu-smp-v1', 'name': f'c32-{name}@1', 'ncpu': 1, 'system': True, 'cpu': CPU,
                'bodies': [P.body(P.SYS_CODE(0), P.sys_text(0, body))], 'cpus': [dict(P.cpu(0, P.SYS_CODE(0)), cpl=0)],
                'memory': S.memory(0, 1, gdt=gdt()) + [{'addr': f'0x{U:x}', 'data': user32(head + f'    {insn}\n    nop\n').hex()}] + memory,
                'params': {'model': {'outcome': r['outcome'], 'fault': zero(r['fault']), 'at': r.get('fault_rip') or at, 'reason': r['reason'],
                                     'deviation': r.get('deviation', False), 'rsp': r['state']['rsp'],
                                     'permitted': sorted(([o, zero(list(x)) if x else None] for o, x in r['permitted']), key=str)}}}
        out.append(prog)
    return out


def programs():
    return positive() + fault_programs()

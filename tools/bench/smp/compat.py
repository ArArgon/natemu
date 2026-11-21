"""M2 task 3a and 3b programs (3b: `positive32`, and the later `cases`): 32-bit code at CPL 3 under a 64-bit kernel body (natemu-smp-v1, `system`, one CPU, the tables and
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


def handlers(dispatch='    jmp .fin\n', xmm=False, vectors=(6, 13, 14, 128)):
    """The entry stubs and the common handler: the event's record (+0 vector, error code, RIP, CS, RFLAGS under 10ED5H,
    RSP, SS, CR2 for a #PF; +0x40 the 16 general registers, RSP's the frame's; +0xC0 DS, ES, FS, GS; +0x100 XMM0..15
    with `xmm`), then `dispatch` (RBX the record's offset, the frame at RSP; RAX free): `.resume` returns by IRETQ,
    `.fin` ends the run. SYSENTER (`.sysenter`, vector 100H) builds the same frame by hand: RIP its own entry, RSP the
    user's EBP."""
    rec = lambda at: f'[{REC + at:#x} + rbx]'
    text = ''.join(f".v{v}:\n{'' if v in (10, 11, 12, 13, 14, 17) else '    push 0' + chr(10)}    push {v}\n    jmp .common\n" for v in vectors)
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


def program(name, body, user, memory=(), params=None, log=0x900):
    """`body`: CPL 0 text after HEAD (it enters the 32-bit code `user` at U); the handlers follow it. `log`: the size of
    the captured event log (0x900: four records; 0x1000: seven)."""
    idtr = (0x80 * 16 + 15).to_bytes(2, 'little') + IDT.to_bytes(8, 'little')
    pattern = b''.join(bytes([0x10 * k + j for j in range(16)]) for k in range(16))
    mem = S.memory(0, 1, gdt=gdt()) + [
        {'addr': f'0x{U:x}', 'data': user32(user).hex()}, dict(P.zero(IDT, 0x810), capture=False),
        # The IDTR to load, room for the start-up one (QEMU's stub's, natemu's driver's: they differ), XMM patterns.
        {'addr': f'0x{IDTR:x}', 'data': (idtr + bytes(0x26)).hex(), 'capture': False},
        {'addr': f'0x{XMM:x}', 'data': pattern.hex(), 'capture': False}, P.zero(LOG, log)] + list(memory)
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

# ==== M2 task 3b: instructions the resident executes in 32-bit code ========================================================
# Each program's 32-bit code runs the resident's forms with their 32-bit sizes and ends with INT 0x80 (the record holds the
# registers); results go to R (the X page, captured) and W. Against QEMU as an Intel processor, which models these exactly
# (its faults through a null segment and at the 4 GiB limit are the exception: those are `cases`, judged by the models).
R = XPAGE
NEXT = W + 0xfc                                  # the fault handlers resume at [NEXT] when it is set (then clear it)
SAVED = W + 0xf8                                 # an ESP to come back to
VECTORS = (0, 1, 3, 4, 5, 6, 12, 13, 14, 128)
GATES = ''.join(gate(v, f'.v{v}', 3 if v in (3, 4) else 0) for v in (0, 1, 3, 4, 5, 12))
# (Resumed with RF clear: pinned QEMU clears an RF the IRETQ loads only at the end of its translation block, so a software
# interrupt soon after would carry it in its frame; x86 clears it after the first instruction.)
RESUME = f"""    and qword [rsp+32], ~0x10000
    mov eax, [{NEXT:#x}]
    test eax, eax
    jz .resume
    mov [rsp+16], rax
    mov dword [{NEXT:#x}], 0
    jmp .resume
"""
END = '    cmp qword [rsp], 0x80\n    je .fin\n'
# #DB: DR6 in the record's CR2 slot, DR6 cleared; resumed with RF set after an instruction breakpoint (B0: it would hit
# again), else clear (as RESUME).
DB = f"""    cmp qword [rsp], 1
    jne .not_db
    mov rax, dr6
    mov [{REC + 0x38:#x} + rbx], rax
    and qword [rsp+32], ~0x10000
    test al, 1
    jz .db_trap
    or qword [rsp+32], 0x10000
.db_trap:
    mov eax, 0xffff0ff0
    mov dr6, rax
    jmp .resume
.not_db:
"""
# #PF below 4 MiB: the page made present and writable, the instruction run again.
MAP = f"""    cmp qword [rsp], 14
    jne .not_pf
    and qword [rsp+32], ~0x10000
    mov rax, cr2
    invlpg [rax]
    shr rax, 12
    or qword [{PTE(0):#x} + rax*8], 3
    jmp .resume
.not_pf:
"""
REGS32 = ''.join(f'    mov {r}, {0x11 * (k + 1):#x}\n' for k, r in enumerate(('eax', 'ecx', 'edx', 'ebx', 'ebp', 'esi', 'edi')))
COPY = lambda src, dst, n: f'    mov esi, {src}\n    mov edi, {dst:#x}\n    mov ecx, {n}\n    rep movsd\n'
FLAGS = 0x3F7ED5                                 # every flag POPF may name but TF: CF PF AF ZF SF IF DF OF IOPL NT RF VM AC VIF VIP ID

# ---- the stack at 32 bits: PUSH/POP of segment registers, PUSHFD/POPFD, ENTER, PUSHAD/POPAD, and ESP wrapping ------------
# Segment pushes over slots of all ones (4-byte slots zero-extended, the 66 form 2 bytes), POP of each data segment register
# (POP SS too, and the 66 form loading GS with the TLS segment, then read through it); POPFD of every flag and POPF (66) at
# CPL 3 (IOPL, IF, VM, RF, VIF, VIP unchanged); ESP wrapping at 4 GiB for PUSH/POP of a segment register and POPFD; ENTER
# at ESP = 4 (EBP 0, ESP FFFF_FFF0H), with nesting levels 3 and 1 and the 66 form at level 2; PUSHAD/POPAD (and the 66
# forms, which keep the upper halves) at ESP, and at ESP = 0 (FFFF_FFE0H).
STACK_USER = REGS32 + f"""    mov [{SAVED:#x}], esp
    mov dword [{W + 0x80:#x}], 0x7175
    push -1
    push -1
    push -1
    push -1
    push -1
    push -1
    add esp, 24
    push es
    push cs
    push ss
    push ds
    push fs
    push gs
    o16 push ds
    mov ax, [esp]
    mov [{R:#x}], ax
    lea esi, [esp+2]
    mov edi, {R + 4:#x}
    mov ecx, 6
    rep movsd
    add esp, 2
    pop gs
    pop fs
    pop ds
    pop ss
    pop eax
    mov [{R + 0x1c:#x}], eax
    pop es
    push 0x2b
    o16 pop gs
    add esp, 2
    mov eax, [gs:0]
    mov [{R + 0x20:#x}], eax
    mov [gs:4], esp
    push {FLAGS:#x}
    popfd
    pushfd
    pop dword [{R + 0x24:#x}]
    o16 push 0x0cd5
    o16 popf
    pushfd
    pop dword [{R + 0x28:#x}]
    push 2
    popfd
    xor esp, esp
    push ds
    pushfd
    pop dword [{R + 0x2c:#x}]
    pop es
    mov [{R + 0x30:#x}], esp
    push 0x8c5
    popfd
    mov [{R + 0x34:#x}], esp
    pushfd
    pop dword [{R + 0x38:#x}]
    push 2
    popfd
    mov esp, 4
    mov ebp, 0x1234
    enter 0x10, 0
    mov [{R + 0x3c:#x}], esp
    mov [{R + 0x40:#x}], ebp
    leave
    mov [{R + 0x44:#x}], ebp
    mov [{R + 0x48:#x}], esp
    mov esp, [{SAVED:#x}]
    push 0xa1a1a1a1
    push 0xa2a2a2a2
    lea ebp, [esp+8]
    enter 8, 3
    mov [{R + 0x4c:#x}], esp
    mov [{R + 0x50:#x}], ebp
""" + COPY('esp', R + 0x54, 6) + f"""    enter 4, 1
    mov [{R + 0x6c:#x}], esp
    mov [{R + 0x70:#x}], ebp
""" + COPY('esp', R + 0x74, 3) + f"""    mov ebp, esp
    db 0x66
    enter 6, 2
    mov [{R + 0x80:#x}], esp
    mov [{R + 0x84:#x}], ebp
""" + COPY('esp', R + 0x88, 4) + f"""    mov esp, [{SAVED:#x}]
""" + REGS32 + """    pushad
""" + COPY('esp', R + 0x98, 8) + """    mov eax, 0xa0
    mov ecx, 0xa1
    mov edx, 0xa2
    mov ebx, 0xa3
    mov ebp, 0xa5
    mov esi, 0xa6
    mov edi, 0xa7
    popad
    mov eax, 0xaaaa0001
    mov ebx, 0xbbbb0002
    pushaw
""" + f"""    mov esi, esp
    mov edi, {R + 0xb8:#x}
    mov ecx, 4
    rep movsd
    mov eax, 0xcccc0000
    mov ebx, 0xdddd0000
    popaw
    mov [{R + 0xc8:#x}], eax
    mov [{R + 0xcc:#x}], ebx
    mov [{R + 0xd0:#x}], esp
    xor esp, esp
    pushad
    mov [{R + 0xd4:#x}], esp
""" + COPY('esp', R + 0xd8, 8) + f"""    popad
    mov [{R + 0xf8:#x}], esp
    mov esp, [{SAVED:#x}]
    int 0x80
"""

# ---- the 32-bit-only forms and far transfers within 32-bit code ------------------------------------------------------------
# ARPL (memory and register, raised and not), LDS/LES/LFS/LGS/LSS (LFS loads the TLS segment, read through after; LGS the
# 66 form), BOUND in bounds (32 and 16 bits), AAM (its flags), INTO with OF = 0, the selector stores and loads through
# memory (MOV, STR, SLDT, SMSW, LAR, LSL, VERR, VERW); CALL/JMP far through an immediate pointer and through m16:32, RET
# far with an immediate, a far call at ESP = 0 (its frame at FFFF_FFF8H), IRETD within 32-bit code (three pops), IRETW (66:
# three 2-byte pops) to IP 10H (linear 10H holds JMP ESI back), then IRETD to 64-bit code at CPL 3 (three pops: no stack
# switch from 32-bit code), whose INT 0x80 ends the run.
FORMS_USER = REGS32 + f"""    mov dword [{W + 0x80:#x}], 0x7175
    mov word [{R:#x}], 0x18
    mov edx, 3
    arpl [{R:#x}], dx
    setz byte [{R + 2:#x}]
    arpl [{R:#x}], dx
    setz byte [{R + 3:#x}]
    mov ecx, 0xabcd0019
    mov ebx, 2
    arpl cx, bx
    setz byte [{R + 4:#x}]
    mov [{R + 8:#x}], ecx
    lds esi, [.p1]
    mov [{R + 0x10:#x}], esi
    les edi, [.p1]
    mov [{R + 0x14:#x}], edi
    lfs eax, [.p2]
    mov [{R + 0x18:#x}], eax
    mov eax, [fs:0]
    mov [{R + 0x1c:#x}], eax
    mov ecx, 0x55550000
    lgs cx, [.p3]
    mov [{R + 0x20:#x}], ecx
    mov [{SAVED:#x}], esp
    lss esp, [.p4]
    mov [{R + 0x24:#x}], esp
    mov esp, [{SAVED:#x}]
    mov dword [{R + 0x30:#x}], -10
    mov dword [{R + 0x34:#x}], 10
    mov eax, -10
    bound eax, [{R + 0x30:#x}]
    mov eax, 10
    bound eax, [{R + 0x30:#x}]
    mov word [{R + 0x38:#x}], 5
    mov word [{R + 0x3a:#x}], 7
    mov eax, 0x12340006
    bound ax, [{R + 0x38:#x}]
    mov eax, 0x1234567f
    aam
    pushfd
    pop dword [{R + 0x40:#x}]
    mov [{R + 0x44:#x}], eax
    mov eax, 0x50
    aam 0x10
    pushfd
    pop dword [{R + 0x48:#x}]
    mov [{R + 0x4c:#x}], eax
    mov eax, 0x8f
    aam 0xff
    pushfd
    pop dword [{R + 0x50:#x}]
    mov [{R + 0x54:#x}], eax
    xor eax, eax
    into
    mov dword [{R + 0x58:#x}], -1
    mov [{R + 0x58:#x}], ds
    mov dword [{R + 0x5c:#x}], -1
    mov [{R + 0x5c:#x}], fs
    mov word [{R + 0x60:#x}], 0x1b
    mov es, [{R + 0x60:#x}]
    str [{R + 0x62:#x}]
    sldt [{R + 0x64:#x}]
    smsw [{R + 0x66:#x}]
    lar eax, [{R + 0x60:#x}]
    mov [{R + 0x68:#x}], eax
    lsl ecx, [{R + 0x60:#x}]
    mov [{R + 0x6c:#x}], ecx
    verr [{R + 0x60:#x}]
    setz byte [{R + 0x6e:#x}]
    verw [{R + 0x60:#x}]
    setz byte [{R + 0x6f:#x}]
    call 0xdb:.f1
    mov [{R + 0x70:#x}], eax
    call far [.p5]
    mov [{R + 0x74:#x}], eax
    jmp 0xdb:.j1
.j1:
    jmp far [.p6]
.j2:
    push 7
    push 8
    call 0xdb:.f3
    mov [{R + 0x78:#x}], eax
    mov [{R + 0x7c:#x}], esp
    mov [{SAVED:#x}], esp
    xor esp, esp
    call 0xdb:.f4
    mov [{R + 0x80:#x}], eax
    mov [{R + 0x84:#x}], esp
    mov esp, [{SAVED:#x}]
    pushfd
    push 0xdb
    push .i1
    iretd
.i1:
    mov [{R + 0x88:#x}], esp
    mov esi, .w1
    pushfw
    o16 push cs
    push word 0x10
    iretw
.w1:
    mov [{R + 0x8c:#x}], esp
    pushfd
    push 0x23
    push .k64
    iretd
.f1:
    mov eax, 0x1111
    retf
.f2:
    mov eax, 0x2222
    retf
.f3:
    mov eax, [esp+8]
    add eax, [esp+12]
    retf 8
.f4:
    mov eax, esp
    retf
.p1: dd 0x12345678
    dw 0x1b
.p2: dd 0x87654321
    dw 0x2b
.p3: dw 0x4321, 0x2b
.p4: dd {US - 0x40:#x}
    dw 0x1b
.p5: dd .f2
    dw 0xdb
.p6: dd .j2
    dw 0xdb
bits 64
.k64:
    mov [{R + 0x90:#x}], rsp
    lea rcx, [rel .k64]
    mov [{R + 0x98:#x}], rcx
    mov ebx, cs
    int 0x80
"""

# ---- x87 and SSE state images at 32 bits --------------------------------------------------------------------------------
# FNSTENV/FLDENV (28 bytes, and 66's 14), FNSAVE/FRSTOR (108 bytes), FLD/FSTP m80; FXSAVE/FXRSTOR and XSAVE/XRSTOR (x87,
# SSE, AVX) store and load XMM0..7 only: the fields of XMM8..15 (and YMM8..15's upper halves) keep the bytes they had
# (A5H), and XMM8..15 keep the body's values (the record logs XMM0..15). The pointer fields (FIP, FCS, FOP, FDP, FDS) are
# zeroed after each store: QEMU tracks them, natemu stores 0 (I6 deviation 8).
def _zero(at, n):
    return ''.join(f'    mov dword [{at + k:#x}], 0\n' for k in range(0, n - n % 4, 4)) + (f'    mov word [{at + n - 2:#x}], 0\n' if n % 4 else '')


X87_BODY = """    mov rax, cr4
    bts rax, 9
    bts rax, 18
    mov cr4, rax
    xor ecx, ecx
    mov eax, 7
    xor edx, edx
    xsetbv
""" + ''.join(f'    movdqu xmm{k}, [{XMM + 16 * k:#x}]\n' for k in range(16)) + ENTER() + handlers(xmm=True)
X87_USER = REGS32 + f"""    fninit
    mov word [{W:#x}], 0x027f
    fldcw [{W:#x}]
    fld1
    fldz
    fild dword [.three]
    fnstenv [{R:#x}]
""" + _zero(R + 12, 16) + f"""    mov word [{R:#x}], 0x037f
    fldenv [{R:#x}]
    fnstenv [{R + 0x20:#x}]
""" + _zero(R + 0x20 + 12, 16) + f"""    o16 fnstenv [{R + 0x40:#x}]
""" + _zero(R + 0x40 + 6, 8) + f"""    fnsave [{R + 0x60:#x}]
""" + _zero(R + 0x60 + 12, 16) + f"""    fnstcw [{W + 2:#x}]
    frstor [{R + 0x60:#x}]
    fnsave [{R + 0xd0:#x}]
""" + _zero(R + 0xd0 + 12, 16) + f"""    frstor [{R + 0xd0:#x}]
    fstp tword [{R + 0x140:#x}]
    fstp tword [{R + 0x150:#x}]
    fld tword [{R + 0x140:#x}]
    fistp dword [{R + 0x160:#x}]
    fxsave [{R + 0x200:#x}]
""" + _zero(R + 0x200 + 6, 18) + f"""    mov dword [{R + 0x200 + 176:#x}], 0x13579bdf
    fxrstor [{R + 0x200:#x}]
    mov eax, 7
    xor edx, edx
    xsave [{R + 0x400:#x}]
""" + _zero(R + 0x400 + 6, 18) + f"""    mov dword [{R + 0x400 + 192:#x}], 0x2468ace0
    xrstor [{R + 0x400:#x}]
    int 0x80
.three: dd 3
"""
# ---- x87 code entered with different stack tops (M2 task 4) ------------------------------------------------------------------
# A translation of 32-bit code is made for the x87 stack top and tags the program has when it is first needed, with a test
# before its first x87 instruction; entered with another pair, the translation is retired and made again without the guess.
# The loop calls one function with nothing on the x87 stack and with one register on it, by turns, and its body joins two
# paths that leave different tops: the function, the join and the loop's blocks each meet a pair they were not made for.
# 6 turns add 3 * 3 each to a sum in memory (54), the odd turns also count (3 turns: 3.0); then the status word and the
# environment (the top back where it began, every register empty).
DEPTH_USER = REGS32 + f"""    fninit
    fldz
    fstp qword [{R:#x}]
    fldz
    fstp qword [{R + 8:#x}]
    mov ecx, 6
.turn:
    test ecx, 1
    jz .even
    fld1
.even:
    call .square
    test ecx, 1
    jz .next
    fadd qword [{R + 8:#x}]
    fstp qword [{R + 8:#x}]
.next:
    dec ecx
    jnz .turn
    fnstsw [{R + 0x10:#x}]
    fnstenv [{R + 0x20:#x}]
""" + _zero(R + 0x20 + 12, 16) + f"""    int 0x80
.square:
    fild dword [.three]
    fmul st0, st0
    fadd qword [{R:#x}]
    fstp qword [{R:#x}]
    ret
.three: dd 3
"""
# ---- faults with x87 registers not yet in the guest state (M2 task 4) --------------------------------------------------------
# In a translation made for a known x87 stack top, the registers pushed and changed so far stay in host registers across
# instructions with memory operands; at a fault the resident rebuilds them from the instruction's record. Three registers
# pushed and one changed, then an x87 store to a page not present (the handler maps it and the store runs again), a push,
# an integer store to another page not present, more arithmetic; FNSAVE then shows every register, the top and the tags.
X87FAULT_USER = REGS32 + f"""    fninit
    fld1
    fldz
    fild dword [.three]
    fadd st0, st2
    fst qword [{VPAGE + 0x10:#x}]
    fld st0
    fmul st0, st1
    mov [{XPAGE + 0x20:#x}], eax
    fxch st2
    fsubp st1, st0
    fild dword [.three]
    fistp dword [{VPAGE + 0x20:#x}]
    fnsave [{XPAGE + 0x100:#x}]
""" + _zero(XPAGE + 0x100 + 12, 16) + f"""    int 0x80
.three: dd 3
"""
# ---- 64-bit integers through the x87 (M2 task 4) ------------------------------------------------------------------------------
# FILD m64 then FISTP m64 is how i686 code copies a 64-bit integer in one piece (atomic loads and stores). natemu's x87
# registers are doubles, so the pair is translated as a copy of the integer. Here with integers a double cannot hold
# (2^62 + 1; 2^63 - 1, which as a double is 2^63, out of range for the store): into a page not present (the handler maps it
# and the FISTP runs again, alone, with the integer the resident kept), into a mapped page, and with an integer
# instruction between the two.
INT64_USER = REGS32 + f"""    fninit
    fild qword [.odd]
    fistp qword [{VPAGE + 0x30:#x}]
    fild qword [.max]
    fistp qword [{XPAGE + 0x10:#x}]
    fild qword [.odd]
    fistp qword [{VPAGE + 0x40:#x}]
    fild qword [.max]
    mov eax, ebx
    fistp qword [{VPAGE + 0x50:#x}]
    fild qword [.odd]
    fisttp qword [{VPAGE + 0x58:#x}]
    int 0x80
.odd: dq 0x4000000000000001
.max: dq 0x7fffffffffffffff
"""
# The same store, with another address space between the fault and the instruction's second run (the security review of
# the kept integer): the #PF handler maps the page, copies the top-level table to a free page and loads CR3 with it (another
# process at the same addresses), replaces ST(0) by 2^62 itself, the double that 2^62 + 1 also converts to, and returns to
# the same FISTP. It must store 2^62, the register's value, not the integer kept for the first address space.
INT64_SPACE = f"""    cmp qword [rsp], 14
    jne .not_pf
    and qword [rsp+32], ~0x10000
    mov rax, cr2
    invlpg [rax]
    shr rax, 12
    or qword [{PTE(0):#x} + rax*8], 3
    mov rsi, cr3
    mov edi, {LOW1:#x}
    mov ecx, 512
    rep movsq
    mov eax, {LOW1:#x}
    mov cr3, rax
    fstp st0
    fild qword [rel .other]
    jmp .resume
.other: dq 0x4000000000000000
.not_pf:
"""
INT64_SPACE_USER = REGS32 + f"""    fninit
    fild qword [.odd]
    fistp qword [{VPAGE + 0x30:#x}]
    int 0x80
.odd: dq 0x4000000000000001
"""
# The review of the kept integer, two cases.
# (1) An entry that was used does not come back: the #PF handler loads CR3 again each time (a kernel with page-table
# isolation does at every return). The FISTP's second run stores the kept 2^62 + 1; later the same FISTP is reached by a
# jump, starting a block, with 2^62 in ST(0) (the double 2^62 + 1 was held as) after another fault: it stores 2^62.
_PF = f"""    cmp qword [rsp], 14
    jne .not_pf
    and qword [rsp+32], ~0x10000
    mov rax, cr2
    invlpg [rax]
    shr rax, 12
    or qword [{PTE(0):#x} + rax*8], 3
"""
INT64_AGAIN = _PF + """    mov rax, cr3
    mov cr3, rax
    jmp .resume
.not_pf:
"""
INT64_AGAIN_USER = REGS32 + f"""    fninit
    fild qword [.odd]
.store:
    fistp qword [{VPAGE + 0x30:#x}]
    cmp dword [{VPAGE + 0x40:#x}], 0
    jne .done
    mov eax, [{VPAGE + 0x30:#x}]
    mov [{VPAGE + 0x48:#x}], eax
    mov eax, [{VPAGE + 0x34:#x}]
    mov [{VPAGE + 0x4c:#x}], eax
    mov dword [{VPAGE + 0x40:#x}], 1
    mov [{XPAGE + 0x10:#x}], eax
    fild qword [.other]
    mov eax, .store
    jmp eax
.done:
    int 0x80
.odd: dq 0x4000000000000001
.other: dq 0x4000000000000000
"""
# (2) The x87 registers saved and restored between the fault and the second run (what a kernel does when the fault sleeps
# and another task runs, when it delivers a signal, when it uses the FPU itself): FXSAVE, FNINIT, a push, FXRSTOR in the
# handler. The image holds ST(0) as the 80-bit 2^62 + 1 (copied out to be compared), and the FISTP stores 2^62 + 1.
# Three ways to save and restore: FXSAVE/FXRSTOR, XSAVE/XRSTOR, XSAVEOPT/XRSTOR. (FILD of 2^62 + 1 sets PE here: FNCLEX.) Then,
# in user mode: the same double loaded again after the kept integer was used is saved as the double (FXSAVE); and an image
# with the 80-bit 2^62 + 1 in ST(0) that the program restores itself, pops and replaces by the double 2^62 is saved as
# 2^62 (a restore in user mode keeps no integer).
INT64_SAVED = lambda save, restore: _PF + f"""    mov eax, 7
    xor edx, edx
    {save} [{LOW1:#x}]
    fninit
    fldpi
    mov eax, 7
    xor edx, edx
    {restore} [{LOW1:#x}]
    mov rax, [{LOW1 + 32:#x}]
    mov [{VPAGE + 0x60:#x}], rax
    movzx eax, word [{LOW1 + 40:#x}]
    mov [{VPAGE + 0x68:#x}], rax
    jmp .resume
.not_pf:
"""
INT64_SAVED_USER = REGS32 + f"""    fninit
    fild qword [.odd]
    fistp qword [{VPAGE + 0x30:#x}]
    fnclex
    fild qword [.other]
    fxsave [{VPAGE + 0x200:#x}]
""" + _zero(VPAGE + 0x200 + 6, 18) + f"""    mov dword [{VPAGE + 0x220:#x}], 2
    mov dword [{VPAGE + 0x224:#x}], 0x80000000
    mov word [{VPAGE + 0x228:#x}], 0x403d
    fxrstor [{VPAGE + 0x200:#x}]
    fstp st0
    fild qword [.other]
    fxsave [{VPAGE + 0x400:#x}]
""" + _zero(VPAGE + 0x400 + 6, 18) + """    fninit
    int 0x80
.odd: dq 0x4000000000000001
.other: dq 0x4000000000000000
"""
A5 = lambda at, n: {'addr': f'0x{at:x}', 'data': (b'\xa5' * n).hex()}
X87_MEMORY = [P.zero(R, 0x320), A5(R + 0x320, 0x80), P.zero(R + 0x3a0, 0x180), A5(R + 0x520, 0x80), P.zero(R + 0x5a0, 0x120),
              A5(R + 0x6c0, 0x80), P.zero(R + 0x740, 0x8c0)]

# ---- faults of the resident's 32-bit forms ---------------------------------------------------------------------------------
# #BR from BOUND (32 and 16 bits; one in bounds between), #DE from AAM 0, #OF from INTO with OF = 1 and #BP from INT3 (both
# traps); then precise memory faults: PUSHAD onto a page not present and POPAD from one (ESP unchanged, each retried once
# the handler maps the page), ARPL on a read-only page (retried once writable), POP DS and LDS of the kernel's code segment
# (#GP(08H), ESP and ESI unchanged).
FAULT_USER32 = REGS32 + f"""    mov dword [{W:#x}], 10
    mov dword [{W + 4:#x}], 20
    mov ecx, 25
    mov dword [{NEXT:#x}], .b1
    bound ecx, [{W:#x}]
.b1:
    mov ecx, 15
    bound ecx, [{W:#x}]
    mov word [{W + 8:#x}], -5
    mov word [{W + 10:#x}], 5
    mov ecx, 0x1234fffa
    mov dword [{NEXT:#x}], .b2
    bound cx, [{W + 8:#x}]
.b2:
    mov eax, 0x1234
    mov dword [{NEXT:#x}], .a1
    aam 0
.a1:
    mov eax, 0x7fffffff
    add eax, 1
    into
    mov ebx, 0x99
    int3
    mov ecx, 0x77
    int 0x80
"""
MEMFAULT_USER = REGS32 + f"""    mov [{SAVED:#x}], esp
    mov esp, {XPAGE + 0x100:#x}
    pushad
    mov esp, {VPAGE + 0x100:#x}
    popad
    mov esp, [{SAVED:#x}]
    mov edx, 3
    arpl [{D + S.RO:#x}], dx
    push 0x08
    mov dword [{NEXT:#x}], .p1
    pop ds
.p1:
    mov [{W + 0x10:#x}], esp
    add esp, 4
    mov esi, 0x5555
    mov dword [{NEXT:#x}], .l1
    lds esi, [.bad]
.l1:
    int 0x80
.bad: dd 0x1234
    dw 0x08
"""
UNMAP = f"""    and qword [{PTE(VPAGE):#x}], -2
    invlpg [{VPAGE:#x}]
    and qword [{PTE(XPAGE):#x}], -2
    invlpg [{XPAGE:#x}]
"""

# ---- single-step and breakpoints in 32-bit code ----------------------------------------------------------------------------
# TF set by POPFD: a #DB after each instruction (the resident's POP ES and PUSH SS, translated ones, PUSHFD, POPFD), none
# after POP SS (its shadow holds the trap to the next instruction). Then INT3; an instruction breakpoint (DR0) on the
# resident's PUSH DS at U + 100H (a fault: the handler resumes with RF); data breakpoints on the resident's store of DS
# (DR1) and a translated store (DR2), traps after them. DR6 is in the record's CR2 slot.
STEP_USER = REGS32 + """    push ds
    pushfd
    or dword [esp], 0x100
    popfd
    pop es
    push ss
    pop ss
    mov eax, ebx
    pushfd
    and dword [esp], ~0x100
    popfd
    int 0x80
"""
BP = U + 0x100
DR7 = 0x0DD00315                                 # L0 L1 L2 LE GE; DR0 execute, DR1 and DR2 writes of 4 bytes
BREAK_BODY = f"""    mov rax, {BP:#x}
    mov dr0, rax
    mov rax, {W + 0x40:#x}
    mov dr1, rax
    mov rax, {W + 0x48:#x}
    mov dr2, rax
    mov eax, {DR7:#x}
    mov dr7, rax
"""
BREAK_USER = REGS32 + f"""    int3
    jmp .bp
    times {BP - U}-($-$$) nop
.bp:
    push ds
    mov [{W + 0x40:#x}], ds
    mov [{W + 0x48:#x}], eax
    pop es
    int 0x80
"""

# ---- INT n at the top of 4 GiB: the pushed EIP wraps to 0 -----------------------------------------------------------------
# The resident's PUSH DS at FFFF_FFFDH, then INT 0x80 at FFFF_FFFEH: the frame's RIP is 0 (EIP wraps: the code segment's
# 4 GiB limit holds the instruction; Vol.3 "Limit Checking"). Pinned QEMU pushes 1_0000_0000H: the SDM's byte is compared.
TOP_USER = REGS32 + '    mov eax, 0xfffffffd\n    jmp eax\n'
TOP_MEMORY = WRAP[:-1] + [P.zero(HIGH, 0xffd), {'addr': f'0x{HIGH + 0xffd:x}', 'data': '1ecd80'}]
TOP_SDM = {f'{REC + 0x14:#x}': 0}


FORMS_MEMORY = WRAP[:5] + [P.zero(LOW0, 0x10), {'addr': f'0x{LOW0 + 0x10:x}', 'data': 'ffe6'}, P.zero(LOW0 + 0x12, 0xfee)] + \
    WRAP[6:] + [P.zero(R, 0x100)]


def positive32():
    vec = dict(vectors=VECTORS)
    return [program('c32-stack@1', ENTER() + handlers(), STACK_USER, WRAP + [P.zero(R, 0x100)]),
            program('c32-forms@1', ENTER() + handlers(), FORMS_USER, FORMS_MEMORY),
            program('c32-x87@1', X87_BODY, X87_USER, X87_MEMORY),
            program('c32-x87-depth@1', X87_BODY, DEPTH_USER, [P.zero(R, 0x100)]),
            program('c32-x87-int64@1', GATES + UNMAP + X87_BODY[:X87_BODY.index(ENTER())] + ENTER() + handlers(END + MAP + RESUME, **vec),
                    INT64_USER, [P.zero(VPAGE, 0x1000), P.zero(XPAGE, 0x1000)], params={'whole_blocks': True}, log=0x1000),
            program('c32-x87-int64-space@1', GATES + UNMAP + X87_BODY[:X87_BODY.index(ENTER())] + ENTER() + handlers(END + INT64_SPACE + RESUME, **vec),
                    INT64_SPACE_USER, [P.zero(VPAGE, 0x1000), P.zero(XPAGE, 0x1000)], params={'whole_blocks': True}, log=0x1000),
            program('c32-x87-int64-again@1', GATES + UNMAP + X87_BODY[:X87_BODY.index(ENTER())] + ENTER() + handlers(END + INT64_AGAIN + RESUME, **vec),
                    INT64_AGAIN_USER, [P.zero(VPAGE, 0x1000), P.zero(XPAGE, 0x1000)], params={'whole_blocks': True}, log=0x1000),
            *[program(f'c32-x87-int64-saved-{save}@1', GATES + UNMAP + X87_BODY[:X87_BODY.index(ENTER())] + ENTER() +
                      handlers(END + INT64_SAVED(save, restore) + RESUME, **vec),
                      INT64_SAVED_USER, [P.zero(VPAGE, 0x1000), P.zero(XPAGE, 0x1000)], params={'whole_blocks': True}, log=0x1000)
              for save, restore in (('fxsave', 'fxrstor'), ('xsave', 'xrstor'), ('xsaveopt', 'xrstor'))],   # (XSAVEC: the pinned QEMU's run of it fails)
            program('c32-x87-faults@1', GATES + UNMAP + X87_BODY[:X87_BODY.index(ENTER())] + ENTER() + handlers(END + MAP + RESUME, **vec),
                    X87FAULT_USER, [P.zero(VPAGE, 0x1000), P.zero(XPAGE, 0x1000)], log=0x1000),
            program('c32-faults@1', GATES + ENTER() + handlers(END + RESUME, **vec), FAULT_USER32, log=0x1000),
            program('c32-memfaults@1', GATES + UNMAP + ENTER() + handlers(END + MAP + RESUME, **vec), MEMFAULT_USER,
                    [P.zero(VPAGE, 0x1000), P.zero(XPAGE, 0x1000)], log=0x1000),
            program('c32-step@1', GATES + ENTER() + handlers(END + DB + RESUME, **vec), STEP_USER, log=0x1000),
            program('c32-breakpoints@1', GATES + BREAK_BODY + ENTER() +
                    handlers('    cmp qword [rsp], 0x80\n    jne .go\n    xor eax, eax\n    mov dr7, rax\n    jmp .fin\n.go:\n' + DB + RESUME, **vec),
                    BREAK_USER, log=0x1000),
            program('c32-int-top@1', ENTER() + handlers(), TOP_USER, TOP_MEMORY, {'sdm_bytes': TOP_SDM})]


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
        # M2 task 3b: the resident's forms through a null FS or GS (#GP(0)) and across the 4 GiB limit (#GP(0), #SS(0)).
        ('fldcw-fs-null', '', 'fldcw [fs:0x10]', lambda s: sm.null_access(s, 'fs'), NOT_MAPPED),
        ('movsreg-gs-null', '', 'mov [gs:0x10], ds', lambda s: sm.null_access(s, 'gs'), NOT_MAPPED),
        ('lds-fs-null', '', 'lds eax, [fs:0x10]', lambda s: sm.null_access(s, 'fs'), NOT_MAPPED),
        ('fldcw-limit', '    mov ebx, 0xffffffff\n', 'fldcw [ebx]', lambda s: sm.flat_access(s, 0xFFFF_FFFF, 2), TOP_ONLY),
        ('fxsave-limit', '    mov ebx, 0xffffff00\n', 'fxsave [ebx]', lambda s: sm.flat_access(s, 0xFFFF_FF00, 512), TOP_ONLY),
        ('popfd-limit', '    mov esp, 0xfffffffe\n', 'popfd', lambda s: sm.flat_access(s, 0xFFFF_FFFE, 4, stack=True), TOP_ONLY),
        ('pop-ds-limit', '    mov esp, 0xfffffffe\n', 'pop ds', lambda s: sm.flat_access(s, 0xFFFF_FFFE, 4, stack=True), TOP_ONLY),
        # natemu's deviation until task 5: a far jump and a far return from 32-bit to 64-bit code: #GP(20H) at it.
        ('jmp-far-64', f'    mov ebx, {XPAGE:#x}\n', 'jmp far [ebx]', lambda s: sm.far(_at(s, XPAGE, FAR64), 'jmp', XPAGE, width=4),
         [{'addr': f'0x{XPAGE:x}', 'data': FAR64.hex()}]),
        ('retf-64', '    push 0x23\n    push 0x1000\n', 'retf',
         lambda s: sm.ret(_at(dict(s, rsp=US - 8), US - 8, FAR64[:4] + bytes([0x23, 0, 0, 0])), width=4), []),
    ]


FAR64 = (0x1000).to_bytes(4, 'little') + (0x23).to_bytes(2, 'little')     # m16:32 pointer to 23H:1000H, 64-bit user code


def _at(s, addr, data):
    """`s` with `data` at `addr` in the model's memory."""
    s['memory'].setdefault(addr >> 12, dm.page(user=True))
    dm.place(s['memory'], addr, data)
    return s


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
    return positive() + positive32() + fault_programs()

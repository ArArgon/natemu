"""X1b programs: CPL 3 -> 0 delivery through TSS.RSP0 and every IST index, IRETQ back to CPL 3, NMI blocking until IRETQ,
and handler entries shaped like a 64-bit kernel's (natemu-smp-v1, `system`, run unchanged by pinned QEMU and natemu).

Each CPU i loads its own GDT (0x08 code64 DPL 0, 0x10 data DPL 0, 0x18 data DPL 3, 0x20 code64 DPL 3, TSS at 0x30, and
at 0x78 a DPL 3 read-only expand-down data descriptor whose limit is the CPU number: the "CPU/node" descriptor that
LSL reads), its TSS (RSP0 and IST1..7, each 8 bytes off a 16-byte boundary, so the frame's alignment shows), its IDT and
CR4.FSGSBASE, then IRETQs to CPL 3 with GS.base = the user block U and IA32_KERNEL_GS_BASE = the per-CPU block K.

Gates (interrupt gates, CS 0x08): #DE 0, #UD 6, #GP 13, #PF 14 on RSP0 (no IST); #DF IST1, NMI IST2, #DB IST3, 0x12
(the #MC vector, raised by INT 0x12) IST4, #BP IST5 (DPL 3), 0x80 IST6 (DPL 3), 0x81 IST7 (DPL 3); 0x40 (DPL 3) and #NP 11
not present. Two entry shapes, each a sequence of architectural instructions (SDM Vol.2 SWAPGS, RDGSBASE/WRGSBASE, LSL):
- `normal` (#DE #UD #GP #PF #BP 0x80 0x81): SWAPGS when the frame's CS has RPL 3, again before IRETQ;
- `paranoid` (#DF NMI #DB 0x12): RDGSBASE into RBX, LSL of selector 0x7b masked with 0xfff indexes a table of the
  per-CPU bases, WRGSBASE; WRGSBASE of RBX before IRETQ. The first NMI's handler raises a second NMI (x2APIC ICR, own
  APIC ID), which must wait for its IRETQ: the second is logged with the "in NMI" flag clear and the same frame RIP.
Every handler logs at [gs:8] (0x50 bytes per event): vector, error code (-1 for none), the frame (RIP, CS, RFLAGS, RSP,
SS), the address of its RIP slot (the stack chosen and aligned), x1 and x2: normal: x1 = [gs:0] (0x100 + i), x2 = CR2
for #PF; paranoid: x1 = the GS.base it found, x2 = LSL result | [gs:0] << 16 | in-NMI flag << 32. Faults resume at R13
(#DF, an abort, too: its handler writes CS from the frame's SS RPL, as its CS:RIP are undefined, SDM Vol.3 7.15
interrupt 8); #DB clears TF in the frame.

CPL 3 runs: UD2, INT3, INT 0x80, MOV from CR0 (#GP(0)), a load of a supervisor page (#PF(5)), DIV by 0, INT1, a TF
single step (POPF), INT 0x12 (#GP(0x92): DPL 0 gate), INT 0x40 (#NP(0x202) delivered serially, its gate not present
#NP(0x5b): #DF), then LSL of 0x7b, RDGSBASE and [gs:0] into its result slots, and INT 0x81, whose handler continues at
CPL 0: a self NMI (and its second), INT 0x12, UD2 with RSP 8 off 16, INT1, and SWAPGS + INT 0x40 (#DF) + SWAPGS. RFLAGS
starts at 2 (IF = 0, IOPL 0) on both machines; the run restores the start-up IDT and GS base before its end.
`expected(i)` lists the events in order as delivery_model events (tools/run_x1.py judges each logged frame with it)."""
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent.parent))
import programs as P
from models import segment_model as sm, delivery_model as dm

# Offsets in programs.SYS_D(i) (user pages of table A, programs.sys_tables): IDT, GDT + pointers, TSS + K/U blocks, stacks,
# log (captured), user code, user stack.
IDT, GDT, TSS, STACKS, LOG, UCODE, USTACK = 0x0, 0x1000, 0x2000, 0x3000, 0x5000, 0x6000, 0xa000
PTR, SAVE = GDT + 0x100, GDT + 0x200           # GDTR, IDTR to load; start-up IDTR and GS base
K, U = TSS + 0x400, TSS + 0x600                 # kernel and user GS blocks: [0] stamp, [8] log cursor, [0x10] NMIs, [0x18] in NMI, [0x20] APIC ID
TABLE = P.SYS_D(0) + TSS + 0x800                # the per-CPU bases, indexed by CPU number (one table)
RES = LOG + 0xf00                               # the CPL 3 results
CPUNODE = 0x7b
GATES = {0: 0, 6: 0, 13: 0, 14: 0, 8: 1, 2: 2, 1: 3, 0x12: 4, 3: 5, 0x80: 6, 0x81: 7}   # vector: IST
DPL3 = {3, 0x80, 0x81, 0x40}
PARANOID = {8, 2, 1, 0x12}
ERROR = {8, 13, 14}
FAULTS = {0, 6, 13, 14}
VECTORS = 0x82


def stack(i, n):
    """RSP0 (n = 0) or ISTn of CPU i: 8 bytes below a 16-byte boundary."""
    return P.SYS_D(i) + STACKS + 0x400 * (n + 1) - 8


def gdt_image(i):
    e = {0x08: sm.code(), 0x10: sm.data(), 0x18: sm.data(3), 0x20: sm.code(3)}
    image = bytearray(0x80)
    for sel, raw in e.items():
        image[sel:sel + 8] = raw.to_bytes(8, 'little')
    image[0x30:0x40] = sm.system_descriptor(P.SYS_D(i) + TSS, 0x67, 9)
    # The CPU/node descriptor: limit = CPU i (node 0), type 5 (read-only, expand-down, accessed), DPL 3, S, P, D/B, G = 0.
    image[0x78:0x80] = (i & 0xffff | 5 << 40 | 1 << 44 | 3 << 45 | 1 << 47 | (i >> 16 & 0xf) << 48 | 1 << 54).to_bytes(8, 'little')
    return bytes(image)


def tss_image(i):
    return dm.tss(rsp=(stack(i, 0), 0, 0), ist=tuple(stack(i, n) for n in range(1, 8)))


def idt_image(i, labels):
    out = bytearray(16 * VECTORS)
    for v in range(VECTORS):
        if v in GATES:
            out[16 * v:16 * v + 16] = dm.gate(P.SYS_CODE(i) + labels[f'v{v}'], 0x08, dpl=3 if v in DPL3 else 0, ist=GATES[v])
        elif v == 0x40:
            out[16 * v:16 * v + 16] = dm.gate(P.SYS_CODE(i), 0x08, dpl=3, present=False)
    return bytes(out)


KERNEL = """    push 2
    popfq
    mov ecx, 0xc0000101
    rdmsr
    mov [{save:#x}+16], eax
    mov [{save:#x}+20], edx
    sidt [{save:#x}]
    lgdt [{ptr:#x}]
    mov eax, 0x30
    ltr ax
    mov eax, 0x10
    mov ds, eax
    mov es, eax
    mov ss, eax
    lidt [{ptr:#x}+16]
    mov rax, cr4
    bts rax, 16
    mov cr4, rax
    mov ecx, 0x1b
    mov eax, {apic_base:#x}
    xor edx, edx
    wrmsr
    mov ecx, 0x80f
    mov eax, 0x1ff
    wrmsr
    mov ecx, 0xc0000101
    mov eax, {k:#x}
    wrmsr
    mov ecx, 0xc0000102
    mov eax, {u:#x}
    wrmsr
    swapgs
    push 0x1b
    push {us:#x}
    push 2
    push 0x23
    push {uc:#x}
    iretq
kernel_after:
    mov rsp, {kstack:#x}
    mov ecx, 0x830
    mov eax, 0x400
    mov edx, {i}
    wrmsr
after_nmi:
    nop
    int 0x12
    push rax
    lea r13, [rel kk1]
    ud2
kk1:
    pop rax
    db 0xf1
    swapgs
    lea r13, [rel kk2]
    int 0x40
kk2:
    swapgs
    lidt [{save:#x}]
    mov ecx, 0xc0000101
    mov eax, [{save:#x}+16]
    mov edx, [{save:#x}+20]
    wrmsr
    jmp done
log:
    mov rax, [gs:8]
    add qword [gs:8], 0x50
    mov rdx, [rsp+40]
    mov [rax], rdx
    mov rdx, [rsp+48]
    mov [rax+8], rdx
    mov rdx, [rsp+56]
    mov [rax+16], rdx
    mov rdx, [rsp+64]
    mov [rax+24], rdx
    mov rdx, [rsp+72]
    mov [rax+32], rdx
    mov rdx, [rsp+80]
    mov [rax+40], rdx
    mov rdx, [rsp+88]
    mov [rax+48], rdx
    lea rdx, [rsp+56]
    mov [rax+56], rdx
    mov [rax+64], rbx
    mov [rax+72], rcx
    ret
normal:
    test byte [rsp+24], 3
    jz n1
    swapgs
n1:
    push rax
    push rbx
    push rcx
    push rdx
    mov rbx, [gs:0]
    xor ecx, ecx
    cmp qword [rsp+32], 14
    jne n2
    mov rcx, cr2
n2:
    call log
    mov rax, [rsp+32]
    cmp rax, 0x81
    je kernel_after
    cmp rax, 3
    je n3
    cmp rax, 0x80
    je n3
    mov [rsp+48], r13
n3:
    pop rdx
    pop rcx
    pop rbx
    pop rax
    test byte [rsp+24], 3
    jz n4
    swapgs
n4:
    add rsp, 16
    iretq
paranoid:
    push rax
    push rbx
    push rcx
    push rdx
    rdgsbase rbx
    mov eax, {cpunode}
    db 0x48
    lsl eax, eax
    and eax, 0xfff
    mov rcx, rax
    mov rax, [{table:#x}+rax*8]
    wrgsbase rax
    mov rdx, [gs:0]
    shl rdx, 16
    or rcx, rdx
    mov rdx, [gs:0x18]
    shl rdx, 32
    or rcx, rdx
    call log
    mov rax, [rsp+32]
    cmp rax, 2
    jne p3
    cmp qword [gs:0x10], 0
    jne p4
    inc qword [gs:0x10]
    mov qword [gs:0x18], 1
    mov ecx, 0x830
    mov eax, 0x400
    mov edx, [gs:0x20]
    wrmsr
    nop
    nop
    mov qword [gs:0x18], 0
    jmp p4
p3:
    cmp rax, 1
    jne p5
    and qword [rsp+64], ~0x100
    jmp p4
p5:
    cmp rax, 8
    jne p4
    mov [rsp+48], r13
    mov eax, 0x08
    mov edx, 0x23
    test byte [rsp+80], 3
    cmovnz eax, edx
    mov [rsp+56], rax
p4:
    wrgsbase rbx
    pop rdx
    pop rcx
    pop rbx
    pop rax
    add rsp, 16
    iretq
""" + ''.join(f"v{v}:\n" + ('' if v in ERROR else '    push -1\n') + f"    push {v:#x}\n    jmp {'paranoid' if v in PARANOID else 'normal'}\n"
              for v in sorted(GATES)) + "done:\n"

USER = """    lea r13, [rel u1]
    ud2
u1:
    int3
    int 0x80
    lea r13, [rel u2]
    mov rax, cr0
u2:
    lea r13, [rel u3]
    mov rax, [{kcode:#x}]
u3:
    lea r13, [rel u4]
    xor edx, edx
    xor eax, eax
    xor ecx, ecx
    div ecx
u4:
    db 0xf1
    pushfq
    or qword [rsp], 0x100
    popfq
    nop
    lea r13, [rel u5]
    int 0x12
u5:
    lea r13, [rel u6]
    int 0x40
u6:
    mov eax, {cpunode}
    lsl eax, eax
    mov [{res:#x}], rax
    rdgsbase rax
    mov [{res:#x}+8], rax
    mov rax, [gs:0]
    mov [{res:#x}+16], rax
    int 0x81
"""


def fields(i):
    d = P.SYS_D(i)
    return dict(i=i, save=d + SAVE, ptr=d + PTR, k=d + K, u=d + U, us=d + USTACK, uc=d + UCODE, kstack=P.STACK(i),
                apic_base=0xfee00d00 if i == 0 else 0xfee00c00, cpunode=CPUNODE, table=TABLE, kcode=P.SYS_CODE(i), res=d + RES)


def texts(i):
    f = fields(i)
    kernel = P.sys_text(i, KERNEL.format(**f).replace('{', '{{').replace('}', '}}'))
    return kernel, USER.format(**f)


def program(n):
    bodies, cpus, memory = [], [], []
    for i in range(n):
        d, f = P.SYS_D(i), fields(i)
        kernel, user = texts(i)
        code, labels = P.ref_assemble(kernel)
        assert len(code) < 0x1000, len(code)
        bodies.append({'addr': f'0x{P.SYS_CODE(i):x}', 'code': code.hex()})
        cpus.append(dict(P.cpu(i, P.SYS_CODE(i)), cpl=0))
        block = lambda stamp: (stamp.to_bytes(8, 'little') + (d + LOG).to_bytes(8, 'little') + bytes(16) + i.to_bytes(8, 'little')).ljust(0x40, b'\0')
        ptrs = (0x7f).to_bytes(2, 'little') + (d + GDT).to_bytes(8, 'little') + bytes(6) + (16 * VECTORS - 1).to_bytes(2, 'little') + (d + IDT).to_bytes(8, 'little')
        memory += P.sys_tables(i, 0, n, {}) + [
            {'addr': f'0x{d + IDT:x}', 'data': idt_image(i, labels).hex(), 'capture': False},
            {'addr': f'0x{d + GDT:x}', 'data': gdt_image(i).hex(), 'capture': False},
            {'addr': f'0x{d + PTR:x}', 'data': ptrs.hex(), 'capture': False},
            dict(P.zero(d + SAVE, 0x20), capture=False),
            {'addr': f'0x{d + TSS:x}', 'data': tss_image(i).hex(), 'capture': False},
            {'addr': f'0x{d + K:x}', 'data': block(0x100 + i).hex()},
            {'addr': f'0x{d + U:x}', 'data': block(0x200 + i).hex()},
            dict(P.zero(d + STACKS, 0x2000), capture=False),
            P.zero(d + LOG, 0x1000),
            {'addr': f'0x{d + UCODE:x}', 'data': P.asm(user).hex(), 'capture': False},
            dict(P.zero(d + USTACK - 0x1000, 0x1000), capture=False),
            dict(P.zero(d + P.SAVE, 8), capture=False)]
        if i == 0:
            memory.append({'addr': f'0x{TABLE:x}', 'data': b''.join((P.SYS_D(j) + K).to_bytes(8, 'little') for j in range(n)).hex(), 'capture': False})
    return {'format': 'natemu-smp-v1', 'name': f'x1-entry@{n}', 'ncpu': n, 'system': True, 'bodies': bodies, 'cpus': cpus,
            'memory': memory, 'params': {'log': [P.SYS_D(i) + LOG for i in range(n)]}}


def expected(i):
    """CPU i's events in order: (delivery_model event, length of the instruction the frame's RIP is past, or 0)."""
    exc = lambda v, e=None, cr2=None: {'kind': 'exception', 'vector': v, **({} if e is None else {'error': e}), **({} if cr2 is None else {'cr2': cr2})}
    soft = lambda v: ({'kind': 'int', 'vector': v, 'length': 2}, 2)
    return [(exc(6), 0), ({'kind': 'int3', 'length': 1}, 1), soft(0x80), (exc(13, 0), 0), (exc(14, 5, P.SYS_CODE(i)), 0),
            (exc(0), 0), ({'kind': 'int1', 'length': 1}, 1), (exc(1), 0), ({'kind': 'int', 'vector': 0x12, 'length': 2}, 0),
            ({'kind': 'int', 'vector': 0x40, 'length': 2}, 0), soft(0x81), ({'kind': 'nmi'}, 0), ({'kind': 'nmi'}, 0), soft(0x12),
            (exc(6), 0), ({'kind': 'int1', 'length': 1}, 1), ({'kind': 'int', 'vector': 0x40, 'length': 2}, 0)]


def model_state(i, entry):
    """delivery_model's state just before the event logged in `entry` (vector, error, RIP, CS, RFLAGS, RSP, SS, ...):
    the CPU's IDT, GDT and TSS images, every page of its block writable and user (table A), CR4 with FSGSBASE. RFLAGS is
    the frame's, RF included: the model pushes 1 for faults and 0 for INT n/INT3 whatever it is, and the current RF for
    INT1, a single-step trap and NMI, which is all the log shows of it (an IRETQ that loaded RF = 1 leaves it set for the
    next instruction)."""
    _, labels = P.ref_assemble(texts(i)[0])
    d = P.SYS_D(i)
    memory = {}
    for off in range(0, 0x10000, 0x1000):
        memory[(d + off) >> 12] = dm.page(user=True)
    for a, data in ((d + IDT, idt_image(i, labels)), (d + GDT, gdt_image(i)), (d + TSS, tss_image(i))):
        dm.place(memory, a, data)
    memory.setdefault((P.STACK(i) - 8) >> 12, dm.page(user=True))
    _, _, rip, cs, rflags, rsp, ss = entry[:7]
    return {'rip': rip, 'rsp': rsp, 'rflags': rflags, 'cpl': cs & 3, 'cs': cs, 'ss': ss, 'cs_l': 1,
            'cr0': 0x80010011, 'cr2': 0, 'cr4': 0x10020, 'idtr': (d + IDT, 16 * VECTORS - 1), 'gdtr': (d + GDT, 0x7f),
            'tr': (0x30, d + TSS, 0x67), 'memory': memory, 'nmi_blocked': False, 'shadow': None, 'halted': False}


def log(data):
    """The logged entries: [(vector, error, RIP, CS, RFLAGS, RSP, SS, slot, x1, x2)] up to the first empty one."""
    out = []
    for k in range(0, RES - LOG, 0x50):
        e = tuple(int.from_bytes(data[k + 8 * j:k + 8 * j + 8], 'little') for j in range(10))
        if not any(e):
            break
        out.append(e)
    return out


# ---- X1c: PUSH/POP FS/GS and the PREFETCHW/CLDEMOTE hints, run unchanged by pinned QEMU and natemu (CPL 0, table A) ------
# PUSH FS (64-bit: the selector zero-extended to 8 bytes), 66 PUSH GS (2 bytes), REX.W PUSH FS (8), then POP into RSI, 66
# POP GS (2: null), POP GS (8: 0x10), POP FS of 0x18 (DPL 3 data at CPL 0) and of null; MOV r, Sreg reads them back. The
# GS base is saved and restored around it (a selector load sets it from the descriptor; the start-up bases differ: the
# QEMU stub's is the CPU's slot, the driver's 0, so R14/R15 are cleared after). PREFETCHW and CLDEMOTE on a
# non-canonical address: no fault (SDM Vol.2 PREFETCHW, CLDEMOTE). `sreg_expected` is the SDM's register outcome.
SREG = """    mov r12, rsp
    mov rsp, {V:#x}+0x800
    mov rax, -1
    mov [rsp-8], rax
    mov [rsp-16], rax
    mov [rsp-24], rax
    mov [rsp-32], rax
    mov ecx, 0xc0000101
    rdmsr
    mov r14d, eax
    mov r15d, edx
    mov ax, 0x10
    mov fs, ax
    push fs
    db 0x66
    push gs
    db 0x48
    push fs
    mov rbx, rsp
    mov r13, [{V:#x}+0x7ee]
    mov r11, [{V:#x}+0x7f6]
    pop rsi
    db 0x66
    pop gs
    pop gs
    mov r8d, gs
    push 0x18
    pop fs
    mov r9d, fs
    push 0
    pop fs
    mov r10d, fs
    mov rbp, rsp
    mov ecx, 0xc0000101
    mov eax, r14d
    mov edx, r15d
    wrmsr
    xor r14d, r14d
    xor r15d, r15d
    mov rax, 0x8000000000000000
    prefetchw [rax]
    db 0x0f, 0x1c, 0x00
    mov rsp, r12"""


def sreg_program():
    return {'format': 'natemu-smp-v1', 'name': 'x1-sreg@1', 'ncpu': 1, 'system': True,
            'bodies': [P.body(P.SYS_CODE(0), P.sys_text(0, SREG))], 'cpus': [dict(P.cpu(0, P.SYS_CODE(0)), cpl=0)],
            'memory': P.sys_memory(0, 1), 'params': {}}


def sreg_expected():
    """{register: value} the SDM gives at the end of SREG: RBX = RSP after the pushes (8 + 2 + 8 below V + 0x800), R13 the
    REX.W PUSH FS slot (0x10 zero-extended), R11 the 66 PUSH GS slot (the null selector, 2 bytes) with the low 6 bytes of
    the first PUSH FS slot above it."""
    v = P.SYS_D(0) + P.V
    return {'rbx': v + 0x7ee, 'rsi': 0x10, 'r8': 0x10, 'r9': 0x18, 'r10': 0, 'rbp': v + 0x800, 'r13': 0x10, 'r11': 0x10 << 16}


# ---- X1c: the Reserved-NOP cells (owner decision, November 2: NOPs, as on the modelled processor; pinned QEMU agrees) ----
# Every 0f 0d (memory forms), 0f 18, 0f 19, 0f 1a, 0f 1b, 0f 1c and 0f 1d encoding with no prefix, 66, F2 and F3, ModRM.reg
# /0../7 on [rax] (non-canonical: a NOP accesses nothing) and the register forms (but 0f 0d's, #UD), plus REX, SIB,
# displacement and segment-override variants; RBX, R8 and R9 hold markers that must survive. Then R14 = 1.
def rnop_cells():
    cells = []
    for op in (0x0d, 0x18, 0x19, 0x1a, 0x1b, 0x1c, 0x1d):
        for pre in ('', '66', 'f2', 'f3'):
            for modrm in (0x00, 0x08, 0x10, 0x18, 0x20, 0x28, 0x30, 0x38, 0xc0, 0xc8, 0xf8):
                if not (op == 0x0d and modrm >= 0xc0):
                    cells.append(f'{pre}0f{op:02x}{modrm:02x}')
    return cells + ['480f1900', '4d0f1dc1', '0f1904c0', '640f1b4408f0', '650f1c8000100000', '0f1a0500000000', '360f18e0']


RNOP = """    mov rax, 0x8000000000000000
    mov r8, 0x1111
    mov r9, 0x2222
    mov rbx, 0x7f0000
    mov r14, 0
""" + ''.join(f"    db {', '.join(f'0x{x:02x}' for x in bytes.fromhex(h))}\n" for h in rnop_cells()) + "    mov r14, 1"


def rnop_program():
    return {'format': 'natemu-smp-v1', 'name': 'x1-rnop@1', 'ncpu': 1, 'system': True,
            'bodies': [P.body(P.SYS_CODE(0), P.sys_text(0, RNOP))], 'cpus': [dict(P.cpu(0, P.SYS_CODE(0)), cpl=0)],
            'memory': P.sys_memory(0, 1), 'params': {}}


# ---- X1c: the BND (F2) prefix on near branches (owner decision, November 2: ignored where MPX is not enumerated, as on
# the modelled processor; pinned QEMU agrees): BND CALL rel32 and RET, BND JMP rel8/rel32, BND Jcc rel8/rel32, BND JMP and
# CALL through a register, each to a canonical target; R8..R13 record which paths ran.
BND = """    mov r8, 0
    mov r9, 0
    mov r10, 0
    mov r11, 0
    mov r12, 0
    mov r13, 0
    db 0xf2
    call bnd_f
    jmp bnd_1
bnd_f:
    mov r8, 1
    db 0xf2
    ret
bnd_1:
    db 0xf2
    jmp short bnd_2
    mov r9, 99
bnd_2:
    db 0xf2
    jmp near bnd_3
    mov r9, 98
bnd_3:
    mov r9, 1
    xor eax, eax
    db 0xf2
    je short bnd_4
    mov r10, 99
bnd_4:
    db 0xf2
    je near bnd_5
    mov r10, 98
bnd_5:
    mov r10, 1
    lea rax, [rel bnd_6]
    db 0xf2
    jmp rax
    mov r11, 99
bnd_6:
    mov r11, 1
    lea rax, [rel bnd_g]
    db 0xf2
    call rax
    jmp bnd_7
bnd_g:
    mov r12, 1
    ret
bnd_7:
    mov r13, 1"""


def bnd_program():
    return {'format': 'natemu-smp-v1', 'name': 'x1-bnd@1', 'ncpu': 1, 'system': True,
            'bodies': [P.body(P.SYS_CODE(0), P.sys_text(0, BND))], 'cpus': [dict(P.cpu(0, P.SYS_CODE(0)), cpl=0)],
            'memory': P.sys_memory(0, 1), 'params': {}}

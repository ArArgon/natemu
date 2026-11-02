#!/usr/bin/env python3
"""R1 checks: composites that chain features already built, each judged against pinned QEMU system mode and the SDM
(boot-features plan, R1a; R1b, the SMP composites, comes later as its own mode).

    tools/with-env python3 tools/run_r1.py r1a --fexc FEXC --out DIR [--host HOST --runtime RUNTIME] [--programs a,b] [--no-negative]

`r1a`: seven single-vCPU programs (natemu-smp-v1, CPL 0 start; QEMU runs them under the smp stub with COM1 = a
`-serial file:`), each run by QEMU once and by natemu at block sizes default and 1. natemu must equal QEMU (every GPR,
RFLAGS under 0x8d5, every captured region; the TSC field of the apic.py delivery logs masked; the UART bytes), and both
must pass the program's SDM expectations (`expect`), so a defect QEMU shares is still caught.

- `r1-boot`: 64-bit boot-protocol entry into a kernel-head sequence (`K kernel/head_64.S:37-405` shape): rep stosq of
  five table pages, a 4-level identity + kernel map (0xffffffff80000000 -> 0, 2 MiB global leaves), CR4 PAE|PSE|PGE,
  CR3, a jump into the kernel map, LGDT with a kernel-map base, null data segments, the GS base onto a per-CPU page
  (stack canary at %gs:40), LRETQ to __KERNEL_CS 0x10, EFER SCE|NX, CR0_STATE, `push 0; popfq`, LIDT of an early IDT
  whose #UD handler (kernel map) fixes up a UD2, a call through the kernel map, then back to the identity map.
- `r1-demand`: the decompressor's/early head's on-demand paging: a #PF handler reads CR2, walks from CR3, takes zeroed
  table pages and data frames from pools (pools start as garbage) and maps the 4 KiB page; faulting read, write,
  neighbour write (leaf only), fetch (the frame holds code), and a REP STOSQ faulting at its start and again midway.
  The fault log (CR2, error code, RIP), the tables' A/D bits and every frame are compared.
- `r1-int3`: Linux's `int3_selftest` (`K kernel/alternative.c:1829-1855`): INT3 at `int3_selftest_ip`, the handler
  matches regs->ip - 1 and stores 1 through RDI; an INT3 elsewhere is ignored; INT3 with IF = 1 (the frame keeps IF,
  the handler runs with IF = 0); INT 3 (CD 03). Frames (CS, RFLAGS, RSP, SS) and the delivery log compared.
- `r1-poke`: `__text_poke` (`alternative.c:2086-2160`): new bytes written through a poking address that only a second
  table set maps onto the code's frame, under that CR3, INVLPG of the poking address, CR3 back, `sync_core` by IRETQ to
  self; a plain immediate patch, the `text_poke_bp` protocol (INT3 first byte, sync; the call meanwhile takes #BP and
  the handler emulates the new instruction; the tail, sync; the first byte, sync) and a static-call NOP5 -> JMP rel32.
- `r1-syscall`: SYSCALL into a PTI-shaped entry (`K entry/entry_64.S:87-166`): SWAPGS, the user RSP to the per-CPU
  page, the kernel CR3 (user tables = kernel tables + 0x8000), the kernel stack (kernel tables only), a read of a page the
  user tables do not map and of one both map to different frames; the exit: user CR3, SWAPGS, VERW of __KERNEL_DS
  (CLEAR_CPU_BUFFERS), SYSRETQ; three round trips, user-side VERW at CPL 3, a final call ending the program at CPL 0.
- `r1-timer`: the LAPIC one-shot timer (xAPIC MMIO) waking `sti; hlt` four times (the idle loop shape: check with IF = 0,
  then `sti; hlt`), the current count 0 after each; an expiry pending with IF = 0 taken after STI's shadow; a masked
  expiry never delivered.
- `r1-serial`: the 8250 THRE-interrupt transmit path: IRQ4 routed through I/O APIC pin 4 (edge, vector 44H), IER = 2,
  the handler reads IIR, writes the next byte to THR or clears IER at the end; the main line halts until done.

Negative controls (run unless `--no-negative`): each composite has a deliberately wrong program variant (or, for
`r1-timer`, a mutated QEMU expectation) that natemu must be flagged on; see NEGATIVE. Writes DIR/r1a.json; exit status 1
on any failure."""
import argparse, json, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'tools' / 'reference'), str(ROOT / 'tools' / 'bench' / 'smp')]
import smp, apic, irqs, programs as P, run_slice1_vm as vm  # noqa: E402
from models import segment_model as sm  # noqa: E402
from run_slice3_vm import vm_state  # noqa: E402
from run_l4a import run_natemu, qemu  # noqa: E402

D = P.SYS_D(0)                    # 0x280000: data of the sys_text programs
SAVE = D + P.SAVE                 # +0 CR3 (sys_text), +8 GS base, +0x10 IDTR
GDT, GDTR, TSS = D + 0x8000, D + 0xb000, D + 0xe000
KMAP = 0xffff_ffff_8000_0000
M64 = (1 << 64) - 1


def q(x): return x.to_bytes(8, 'little')


def gdt_image(tss):
    """0x08 kernel code (also the stub's gate selector), 0x10 kernel data, 0x18 user data, 0x20 user code, 0x28 TSS;
    every A bit preset (QEMU sets none on a CS load, the SDM does)."""
    return b''.join(q(x) for x in (0, sm.code(), sm.data(), sm.data(3), sm.code(3))) + sm.system_descriptor(tss, 0x67, 9)


def gdt_memory(gdt, gdtr, tss=None):
    out = [{'addr': f'0x{gdt:x}', 'data': gdt_image(tss or TSS).hex()},
           {'addr': f'0x{gdtr:x}', 'data': ((0x37).to_bytes(2, 'little') + q(gdt)).hex(), 'capture': False}]
    if tss:
        out.append({'addr': f'0x{tss:x}', 'data': (bytes(4) + q(P.STACK(0) - 0x400) + bytes(0x68 - 12)).hex()})
    return out


def own_gdt(gdtr):
    """LGDT of gdt_image, SS/DS/ES = 0x10, CS = 0x08 by LRETQ (QEMU's and natemu's start-up GDTs differ)."""
    return f"""    lgdt [{gdtr:#x}]
    mov eax, 0x10
    mov ss, eax
    mov ds, eax
    mov es, eax
    push 0x08
    lea rax, [rel r1_own_cs]
    push rax
    o64 retf
r1_own_cs:
"""


PRO = f"""    sidt [{SAVE + 0x10:#x}]
    mov ecx, 0xc0000101
    rdmsr
    mov [{SAVE + 8:#x}], eax
    mov [{SAVE + 12:#x}], edx
"""
# The start-up GS base (QEMU's capture writes through it) and IDT back; sys_text's RESTORE then puts back the controls.
EPI = f"""    mov ecx, 0xc0000101
    mov eax, [{SAVE + 8:#x}]
    mov edx, [{SAVE + 12:#x}]
    wrmsr
    lidt [{SAVE + 0x10:#x}]
"""


def sys_program(name, text, memory, params=None):
    code, labels = P.ref_assemble(P.sys_text(0, text))
    prog = {'format': 'natemu-smp-v1', 'name': name, 'ncpu': 1, 'system': True,
            'bodies': [{'addr': f'0x{P.SYS_CODE(0):x}', 'code': code.hex()}], 'cpus': [dict(P.cpu(0, P.SYS_CODE(0)), cpl=0)],
            'memory': memory + [dict(P.zero(SAVE, 0x20), capture=False)], 'params': params or {}}
    return prog, {k: P.SYS_CODE(0) + v for k, v in labels.items()}


def gate_text(idt, vector, label, selector=0x08):
    """Writes a 64-bit interrupt gate at run time (the handler's address from LEA: valid in whichever mapping runs)."""
    g = idt + 16 * vector
    return f"""    lea rax, [rel {label}]
    mov [{g:#x}], ax
    mov word [{g + 2:#x}], {selector:#x}
    mov word [{g + 4:#x}], 0x8e00
    shr rax, 16
    mov [{g + 6:#x}], ax
    shr rax, 16
    mov [{g + 8:#x}], eax
    mov dword [{g + 12:#x}], 0
"""


def lcg(addr, size, seed, capture=True):
    return {'addr': f'0x{addr:x}', 'size': size, 'fill': 'lcg', 'seed': seed, 'capture': capture}


# ---- r1-boot -----------------------------------------------------------------------------------------------------
BT = 0x290000                                     # PML4, PDPT low, PD low, PDPT kernel, PD kernel
BGDT, BIDT, BPCPU, BC = 0x295000, 0x296000, 0x297000, 0x298000
LINUX_GDT = (0, sm.code(), sm.code(), sm.data())  # 0x08 (also the stub's gate CS), __KERNEL_CS 0x10, __KERNEL_DS 0x18


def boot_program(variant=False):
    cs = 0x08 if variant else 0x10                 # negative control: LRETQ to the wrong selector
    text = PRO + f"""    cld
    mov rdi, {BT:#x}
    mov ecx, {5 * 512}
    xor eax, eax
    rep stosq
    mov qword [{BT:#x}], {BT + 0x1000 | 0x23:#x}
    mov qword [{BT + 511 * 8:#x}], {BT + 0x3000 | 0x23:#x}
    mov qword [{BT + 0x1000:#x}], {BT + 0x2000 | 0x23:#x}
    mov qword [{BT + 0x3000 + 510 * 8:#x}], {BT + 0x4000 | 0x23:#x}
    mov qword [{BT + 0x2000:#x}], 0xe3
    mov qword [{BT + 0x2008:#x}], 0x2000e3
    mov qword [{BT + 0x4000:#x}], 0x1e3
    mov qword [{BT + 0x4008:#x}], 0x2001e3
    mov eax, 0xb0
    mov cr4, rax
    mov eax, {BT:#x}
    mov cr3, rax
    lea rax, [rel r1b_high]
    mov rcx, {KMAP:#x}
    add rax, rcx
    jmp rax
r1b_high:
    add rsp, rcx
    mov rax, {KMAP + BGDT + 0x80:#x}
    lgdt [rax]
    xor eax, eax
    mov ds, eax
    mov ss, eax
    mov es, eax
    mov fs, eax
    mov gs, eax
    mov ecx, 0xc0000101
    mov rax, {KMAP + BPCPU:#x}
    mov rdx, rax
    shr rdx, 32
    wrmsr
    mov rax, 0x5ca1ab1e0badcafe
    mov [gs:40], rax
    push {cs:#x}
    lea rax, [rel r1b_kcs]
    push rax
    o64 retf
r1b_kcs:
    mov ecx, 0xc0000080
    rdmsr
    bts eax, 0
    bts eax, 11
    wrmsr
    mov eax, 0x80050033
    mov cr0, rax
    lea rax, [rel r1b_ud]
    mov rdi, {KMAP + BIDT + 6 * 16:#x}
    mov [rdi], ax
    mov word [rdi+2], 0x10
    mov word [rdi+4], 0x8e00
    shr rax, 16
    mov [rdi+6], ax
    shr rax, 16
    mov [rdi+8], eax
    mov dword [rdi+12], 0
    mov rax, {KMAP + BIDT + 0xff0:#x}
    lidt [rax]
    push 0
    popfq
    mov r14d, 0
r1b_warn:
    ud2
    lea r8, [rel r1b_kcs]
    mov r9, cs
    mov r10, ss
    mov rax, cr0
    mov [{BC:#x}], rax
    mov rax, cr4
    mov [{BC + 8:#x}], rax
    mov rax, cr3
    mov [{BC + 0x10:#x}], rax
    mov ecx, 0xc0000080
    rdmsr
    mov [{BC + 0x18:#x}], eax
    mov [{BC + 0x1c:#x}], edx
    mov rax, [gs:40]
    mov [{BC + 0x20:#x}], rax
    mov ecx, 0xc0000101
    rdmsr
    mov [{BC + 0x28:#x}], eax
    mov [{BC + 0x2c:#x}], edx
    mov rax, {KMAP + BC:#x}
    sgdt [rax + 0x30]
    sidt [rax + 0x40]
    pushfq
    pop qword [rax + 0x50]
    mov qword [rax + 0x80], 0x1234
    call r1b_fn
    mov r12, rax
    mov r13, rsp
    mov r15, [{BC + 0x80:#x}]
    lea rax, [rel r1b_low]
    mov rcx, {KMAP:#x}
    sub rax, rcx
    sub rsp, rcx
    jmp rax
r1b_fn:
    lea rax, [rel r1b_fn]
    ret
r1b_ud:
    inc r14
    mov rax, [rsp]
    mov [{BC + 0x90:#x}], rax
    mov rax, [rsp+8]
    mov [{BC + 0x98:#x}], rax
    mov rax, [rsp+16]
    mov [{BC + 0xa0:#x}], rax
    mov rax, [rsp+24]
    mov [{BC + 0xa8:#x}], rax
    mov rax, [rsp+32]
    mov [{BC + 0xb0:#x}], rax
    add qword [rsp], 2
    iretq
r1b_low:
    lgdt [{BGDT + 0xa0:#x}]
""" + EPI
    gdt = b''.join(q(x) for x in LINUX_GDT)
    gdt_page = gdt + bytes(0x80 - len(gdt)) + (0x1f).to_bytes(2, 'little') + q(KMAP + BGDT) + bytes(0x16) + (0x1f).to_bytes(2, 'little') + q(BGDT)
    memory = P.sys_tables(0, 0, 1, {}) + [
        lcg(BT, 0x5000, 11), {'addr': f'0x{BGDT:x}', 'data': gdt_page.hex()},
        P.zero(BIDT, 0xff0), {'addr': f'0x{BIDT + 0xff0:x}', 'data': ((0xff).to_bytes(2, 'little') + q(KMAP + BIDT)).hex()},
        P.zero(BPCPU, 0x100), P.zero(BC, 0x100)]
    return sys_program('r1-boot@1', text, memory)


def boot_expect(st, lab):
    k = lambda name: KMAP + lab[name]
    return [('LRETQ target (kernel map), CS, SS', [st.r('r8'), st.r('r9'), st.r('r10')], [k('r1b_kcs'), 0x10, 0]),
            ('a call through the kernel map; RSP in the kernel map', [st.r('r12'), st.r('r13')], [k('r1b_fn'), KMAP + P.STACK(0)]),
            ('#UD fixups; the store through the kernel map read through the identity map', [st.r('r14'), st.r('r15')], [1, 0x1234]),
            ('CR0, CR4, CR3, EFER, canary, GS base', [st.m(BC + 8 * j) for j in range(6)],
             [0x80050033, 0xb0, BT, 0xd01, 0x5ca1ab1e0badcafe, KMAP + BPCPU]),
            ('SGDT, SIDT', [st.m(BC + 0x30, 10), st.m(BC + 0x40, 10)], [0x1f | (KMAP + BGDT) << 16, 0xff | (KMAP + BIDT) << 16]),
            ('RFLAGS after popfq 0 (no flag-writing instruction since)', st.m(BC + 0x50), 2),
            ('#UD frame: RIP, CS, RFLAGS, RSP, SS', [st.m(BC + 0x90 + 8 * j) for j in range(5)],
             [k('r1b_warn'), 0x10, 0x10002, KMAP + P.STACK(0), 0]),   # RF set in a fault's frame
            ('tables (first entries)', [st.m(BT), st.m(BT + 0x1000), st.m(BT + 0x2000), st.m(BT + 0x2008), st.m(BT + 511 * 8),
                                        st.m(BT + 0x3000 + 510 * 8), st.m(BT + 0x4000), st.m(BT + 0x4008)],
             [BT + 0x1023, BT + 0x2023, 0xe3, 0x2000e3, BT + 0x3023, BT + 0x4023, 0x1e3, 0x2001e3])]


# ---- r1-demand ---------------------------------------------------------------------------------------------------
DT, DIDT = 0x290000, 0x293000                     # PML4, PDPT, PD (identity 0..4 MiB, 2 MiB leaves); the IDT
POOL, FRAMES, DLOG = 0x2a0000, 0x2b0000, 0x2b8000  # 11 table pages, 6 data frames, the log (+0 count, +8 entries)
NEXT_T, NEXT_F = DLOG + 0x800, DLOG + 0x808
V1, V2, V3, V4 = 0x0000_4000_0012_3000, 0x0000_4000_4000_0000, 0x0000_7fff_ffe0_0000, 0x0000_0080_0000_5000


def demand_program(variant=False):
    leaf_off = 0x1000 if variant else 0           # negative control: every leaf one frame too far
    text = PRO + f"""    cld
    mov rdi, {DT:#x}
    mov ecx, {3 * 512}
    xor eax, eax
    rep stosq
    mov qword [{DT:#x}], {DT + 0x1023:#x}
    mov qword [{DT + 0x1000:#x}], {DT + 0x2023:#x}
    mov qword [{DT + 0x2000:#x}], 0xe3
    mov qword [{DT + 0x2008:#x}], 0x2000e3
""" + gate_text(DIDT, 14, 'r1d_pf') + f"""    lidt [{DIDT + 0xff0:#x}]
    mov eax, {DT:#x}
    mov cr3, rax
    mov rbx, {V1:#x}
r1d_a1:
    mov r8, [rbx+0x18]
r1d_a2:
    mov qword [rbx+0x1008], 0x1111
    mov r9, [rbx+0x20]
    mov rbx, {V2:#x}
r1d_a3:
    mov r10, [rbx]
    mov rax, {V3:#x}
    call rax
    mov r12, rax
    mov rdi, {V4 + 0xff0:#x}
    mov ecx, 4
    mov rax, 0x5555
r1d_a5:
    rep stosq
    mov r13, rdi
    mov r15, rcx
    mov rax, [{SAVE:#x}]
    mov cr3, rax
""" + EPI + f"""    jmp r1d_end
r1d_pf:
    push rax
    push rbx
    push rcx
    push rdx
    push rdi
    mov rdx, cr2
    mov rbx, [{DLOG:#x}]
    imul rbx, rbx, 24
    add rbx, {DLOG + 8:#x}
    mov [rbx], rdx
    mov rax, [rsp+40]
    mov [rbx+8], rax
    mov rax, [rsp+48]
    mov [rbx+16], rax
    inc qword [{DLOG:#x}]
    mov rbx, cr3
    and rbx, -4096
    mov ecx, 39
r1d_level:
    mov rax, rdx
    shr rax, cl
    and eax, 511
    lea rbx, [rbx + rax*8]
    cmp ecx, 12
    je r1d_leaf
    test byte [rbx], 1
    jnz r1d_down
    mov rax, [{NEXT_T:#x}]
    add qword [{NEXT_T:#x}], 0x1000
    push rcx
    push rax
    mov rdi, rax
    mov ecx, 512
    xor eax, eax
    rep stosq
    pop rax
    pop rcx
    or rax, 3
    mov [rbx], rax
r1d_down:
    mov rbx, [rbx]
    mov rax, 0xffffffffff000
    and rbx, rax
    sub ecx, 9
    jmp r1d_level
r1d_leaf:
    mov rax, [{NEXT_F:#x}]
    add qword [{NEXT_F:#x}], 0x1000
    add rax, {leaf_off:#x}
    or rax, 3
    mov [rbx], rax
    pop rdi
    pop rdx
    pop rcx
    pop rbx
    pop rax
    add rsp, 8
    iretq
r1d_end:
"""
    frames = [lcg(FRAMES + 0x1000 * k, 0x1000, 20 + k) for k in range(6)]
    # Frame 3 holds the fetched code; frame 4 starts with code too, so the negative control's shifted fetch runs code.
    for k, imm in ((3, 0x33), (4, 0x44)):
        code = P.asm(f'mov eax, {imm:#x}\nret')
        frames[k] = {'addr': f'0x{FRAMES + 0x1000 * k:x}', 'data': (code + smp.lcg_bytes(0x1000, 20 + k)[len(code):]).hex()}
    memory = P.sys_tables(0, 0, 1, {}) + [
        lcg(DT, 0x3000, 12), P.zero(DIDT, 0xff0), {'addr': f'0x{DIDT + 0xff0:x}', 'data': ((0xfff).to_bytes(2, 'little') + q(DIDT)).hex()},
        lcg(POOL, 0xb000, 13)] + frames + [lcg(FRAMES + 0x6000, 0x1000, 26),
        {'addr': f'0x{DLOG:x}', 'data': (bytes(0x800) + q(POOL) + q(FRAMES)).hex()}]
    return sys_program('r1-demand@1', text, memory)


def demand_expect(st, lab):
    frame = lambda k: FRAMES + 0x1000 * k
    lcg_q = lambda k, off: int.from_bytes(smp.lcg_bytes(0x1000, 20 + k)[off:off + 8], 'little')
    want_log = [(V1 + 0x18, 0, lab['r1d_a1']), (V1 + 0x1008, 2, lab['r1d_a2']), (V2, 0, lab['r1d_a3']), (V3, 0x10, V3),
                (V4 + 0xff0, 2, lab['r1d_a5']), (V4 + 0x1000, 2, lab['r1d_a5'])]
    log = [tuple(st.m(DLOG + 8 + 24 * k + 8 * j) for j in range(3)) for k in range(st.m(DLOG))]
    pte = lambda base, va, shift: base + (va >> shift & 511) * 8
    # Allocation order: V1 PDPT, PD, PT (pool 0-2); V2 PD, PT (3-4); V3 PDPT, PD, PT (5-7); V4 PDPT, PD, PT (8-10).
    t = lambda k: POOL + 0x1000 * k
    return [('faults (CR2, error code, RIP)', log, want_log),
            ('loads through the new mappings: frames 0, 0, 2; the fetched code', [st.r('r8'), st.r('r9'), st.r('r10'), st.r('r12')],
             [lcg_q(0, 0x18), lcg_q(0, 0x20), lcg_q(2, 0), 0x33]),
            ('REP STOSQ done (RDI, RCX)', [st.r('r13'), st.r('r15')], [V4 + 0x1010, 0]),
            ('stores landed in frames 1, 4, 5', [st.m(frame(1) + 8), st.m(frame(4) + 0xff0), st.m(frame(4) + 0xff8), st.m(frame(5)), st.m(frame(5) + 8)],
             [0x1111, 0x5555, 0x5555, 0x5555, 0x5555]),
            ('pool used: 11 tables, 6 frames', [st.m(NEXT_T), st.m(NEXT_F)], [t(11), frame(6)]),
            ('leaves with A (read, fetch) and A|D (write) set by the retried accesses',
             [st.m(pte(t(2), V1, 12)), st.m(pte(t(2), V1 + 0x1000, 12)), st.m(pte(t(4), V2, 12)), st.m(pte(t(7), V3, 12)),
              st.m(pte(t(10), V4, 12)), st.m(pte(t(10), V4 + 0x1000, 12))],
             [frame(0) | 0x23, frame(1) | 0x63, frame(2) | 0x23, frame(3) | 0x23, frame(4) | 0x63, frame(5) | 0x63]),
            ('upper entries with A set (PML4E of V1, PDPTE of V2, PDE of V4)',
             [st.m(pte(DT, V1, 39)), st.m(pte(t(0), V2, 30)), st.m(pte(t(9), V4, 21))], [t(0) | 0x23, t(3) | 0x23, t(10) | 0x23]),
            ('a fresh table page is zeroed but its entries', st.m(t(0) + 0x10), 0)]


# ---- r1-poke -----------------------------------------------------------------------------------------------------
FV, SV, GV, PK, PIDT = D + 0x1000, D + 0x1040, D + 0x1080, D + 0x5000, D + 0x3000


def call(tag, to):
    return f'    lea r13, [rel r1p_c{tag}]\n    mov rax, {to:#x}\n    jmp rax\nr1p_c{tag}:\n'


def poke(tag, stores):
    """`__text_poke`: under the poking tables (B) store through PK, INVLPG PK, back to A, then `sync_core` (IRETQ to self)."""
    return (f'    mov rax, {P.SYS_T(0, 1):#x}\n    mov cr3, rax\n' + ''.join(f'    mov {w} [{PK + off:#x}], {v:#x}\n' for w, off, v in stores)
            + f'    mov rax, {PK:#x}\n    invlpg [rax]\n    mov rax, {P.SYS_T(0, 0):#x}\n    mov cr3, rax\n'
            + f'    mov eax, ss\n    push rax\n    push rsp\n    add qword [rsp], 8\n    pushfq\n    mov eax, cs\n    push rax\n'
            + f'    lea rax, [rel r1p_s{tag}]\n    push rax\n    iretq\nr1p_s{tag}:\n')


def poke_program(variant=False):
    text = PRO + own_gdt(GDTR) + gate_text(PIDT, 3, 'r1p_bp') + f'    lidt [{PIDT + 0x800:#x}]\n    xor r14d, r14d\n'
    text += call(0, FV) + '    mov r8, rax\n' + call(1, SV) + '    mov rbx, rax\n'
    text += poke(0, [('dword', 1, 2)]) + call(2, FV) + '    mov r9, rax\n'
    text += poke(1, [('byte', 0, 0xcc)]) + call(3, FV) + '    mov r10, rax\n'
    text += poke(2, [('dword', 1, 3)]) + poke(3, [('byte', 0, 0xb8)]) + call(4, FV) + '    mov r12, rax\n'
    text += poke(4, [('byte', 0x40, 0xe9), ('dword', 0x41, GV - SV - 5)]) + call(5, SV) + '    mov r15, rax\n    mov rsi, r14\n'
    text += EPI + f"""    jmp r1p_end
r1p_bp:
    inc r14
    push rax
    mov rax, [rsp+8]
    dec rax
    cmp rax, {FV:#x}
    jne r1p_bp_out
    mov qword [rsp], 3
    mov rax, {FV + 5:#x}
    mov [rsp+8], rax
r1p_bp_out:
    pop rax
    iretq
r1p_end:
"""
    page = bytearray(b'\xcc' * 0x100)
    for at, src in ((0, 'mov eax, 1\njmp r13'), (0x40, 'db 0x0f, 0x1f, 0x44, 0x00, 0x00\nmov eax, 7\njmp r13'), (0x80, 'mov eax, 9\njmp r13')):
        b = P.asm(src); page[at:at + len(b)] = b
    assert page[0x40:0x45] == bytes.fromhex('0f1f440000')
    # Table B (the poking mm): PK onto the code's frame (negative control: onto another frame), the code page itself unmapped.
    b = {FV: 0, PK: (D + (0x2000 if variant else 0x1000)) | P.LEAF}
    memory = P.sys_tables(0, 0, 1, {PK: 0}) + P.sys_tables(0, 1, 1, b) + gdt_memory(GDT, GDTR) + [
        {'addr': f'0x{FV:x}', 'data': page.hex()}, P.zero(D + 0x2000, 0x100), dict(P.zero(PIDT, 0x800), capture=False),
        {'addr': f'0x{PIDT + 0x800:x}', 'data': ((0xfff).to_bytes(2, 'little') + q(PIDT)).hex(), 'capture': False}]
    return sys_program('r1-poke@1', text, memory)


def poke_expect(st, lab):
    return [('results: before, patched, at the INT3 (emulated), after; static call before, after',
             [st.r('r8'), st.r('r9'), st.r('r10'), st.r('r12'), st.r('rbx'), st.r('r15')], [1, 2, 3, 3, 7, 9]),
            ('#BP taken once (only while the INT3 was in place)', st.r('rsi'), 1),
            ('final code bytes', [st.mb(FV, 8).hex(), st.mb(SV, 5).hex()], [P.asm('mov eax, 3\njmp r13').hex(), 'e93b000000'])]


# ---- r1-syscall --------------------------------------------------------------------------------------------------
W, SEC, UC, USTK, PC, KSTK = D, D + 0x3000, D + 0x9000, D + 0x6ff0, D + 0xc000, D + 0xd000
KVAL, UVAL, SVAL = 0x4b4b_0000, 0x5555, 0x5ec
USER = f"""    mov r8, [{W:#x}]
    xor r9d, r9d
    mov ebx, 3
.again:
    mov eax, 39
    syscall
    add r9, rax
    dec ebx
    jnz .again
    mov r10, [{W:#x}]
    mov ax, 0x10
    verw ax
    mov r12d, 0
    setz r12b
    mov ax, 0x1b
    verw ax
    mov r13d, 0
    setz r13b
    mov rsi, rcx
    mov rdi, r11
    mov eax, 60
    syscall
"""
USER_AGAIN = len(P.asm(USER.split('    add r9, rax')[0]))   # the RIP after the loop's SYSCALL (offset)


def syscall_program(variant=False):
    to_user = '' if variant else f"""    mov [gs:0x40], rdi
    mov rdi, cr3
    or rdi, 0x8000
    mov cr3, rdi
    mov rdi, [gs:0x40]
"""                                               # negative control: no switch to the user tables on exit
    wr = lambda idx, val: f'    mov ecx, {idx:#x}\n    mov eax, {val & 0xffffffff:#x}\n    mov edx, {val >> 32:#x}\n    wrmsr\n'
    text = PRO + own_gdt(GDTR) + f"""    mov eax, 0x28
    ltr ax
    mov ecx, 0xc0000080
    rdmsr
    or eax, 1
    wrmsr
""" + wr(0xc0000081, 0x0010_0008 << 32) + """    mov ecx, 0xc0000082
    lea rax, [rel r1s_entry]
    mov rdx, rax
    shr rdx, 32
    wrmsr
""" + wr(0xc0000084, 0x47700) + wr(0xc0000101, PC) + wr(0xc0000102, 0) + f"""    mov [{PC + 0x38:#x}], rsp
    mov qword [{PC + 0x18:#x}], {KSTK + 0x1000:#x}
    mov qword [{PC + 0x10:#x}], {USTK:#x}
    xor eax, eax
    mov ecx, {UC:#x}
    mov r11d, 2
    jmp r1s_exit
r1s_entry:
    swapgs
    mov [gs:0x10], rsp
    mov rsp, cr3
    and rsp, -0x8001
    mov cr3, rsp
    mov rsp, [gs:0x18]
    push r11
    push rcx
    inc qword [gs:0]
    cmp eax, 60
    je r1s_done
    mov rax, [{W:#x}]
    add rax, [{SEC:#x}]
    pop rcx
    pop r11
r1s_exit:
    mov rsp, [gs:0x10]
{to_user}    swapgs
    verw [rel r1s_ds]
    jz r1s_ok
    inc qword [{PC + 0x30:#x}]
r1s_ok:
    o64 sysret
r1s_ds:
    dw 0x10
r1s_done:
    mov rsp, [gs:0x38]
""" + EPI
    sup = lambda page: page | P.LEAF & ~P.US
    a = {W: (D + 0x1000) | P.LEAF, SEC: sup(SEC), PC: sup(PC), KSTK: sup(KSTK)}
    b = {W: (D + 0x2000) | P.LEAF, SEC: 0, PC: sup(PC), KSTK: 0}
    memory = P.sys_tables(0, 0, 1, a) + P.sys_tables(0, 1, 1, b) + gdt_memory(GDT, GDTR, TSS) + [
        {'addr': f'0x{D + 0x1000:x}', 'data': q(KVAL).hex()}, {'addr': f'0x{D + 0x2000:x}', 'data': q(UVAL).hex()},
        {'addr': f'0x{SEC:x}', 'data': q(SVAL).hex()}, {'addr': f'0x{UC:x}', 'data': P.asm(USER).hex()},
        dict(P.zero(USTK - 0xff0, 0x1000), capture=False), P.zero(PC, 0x100), P.zero(KSTK, 0x1000)]
    return sys_program('r1-syscall@1', text, memory)


def syscall_expect(st, lab):
    return [('user view of W before and after (user tables), the sum of three kernel results (kernel tables)',
             [st.r('r8'), st.r('r10'), st.r('r9')], [UVAL, UVAL, 3 * (KVAL + SVAL)]),
            ('VERW at CPL 3: __KERNEL_DS not writable (ZF 0), a DPL 3 data segment writable (ZF 1)', [st.r('r12'), st.r('r13')], [0, 1]),
            ('per-CPU: calls, last user RSP, VERW at CPL 0 never cleared ZF', [st.m(PC), st.m(PC + 0x10), st.m(PC + 0x30)], [4, USTK, 0]),
            ("the kernel stack: the last entry's RCX (the user code's end); RCX after the third SYSRET",
             [st.m(KSTK + 0xff0), st.r('rsi')], [UC + len(P.asm(USER)), UC + USER_AGAIN]),
            ('TSS descriptor busy after LTR', st.mb(GDT + 0x2d, 1)[0] & 0xf, 0xb)]


# ---- r1-timer (apic.build, xAPIC) --------------------------------------------------------------------------------
AGDT, AGDTR = 0x3D8000, 0x3D8080
TV, ROUNDS, COUNT = 0x40, 4, 0x80000


def apic_gdt():
    return [{'addr': f'0x{AGDT:x}', 'data': gdt_image(0).hex()},
            {'addr': f'0x{AGDTR:x}', 'data': ((0x37).to_bytes(2, 'little') + q(AGDT)).hex(), 'capture': False}]


def timer_program():
    m = 'x'
    count = f'[{apic.COUNTS(0) + 8 * TV:#x}]'
    main = '\n'.join([own_gdt(AGDTR), '    mov r12, 1', f'    mov r13d, {ROUNDS}', '    xor esi, esi', 'r1t_round:',
                      apic.wr(m, apic.DCR, 0xb), apic.wr(m, apic.LVT_TIMER, TV), apic.wr(m, apic.INIT_COUNT, COUNT),
                      '    inc rsi', 'r1t_idle:', '    cli', f'    cmp {count}, rsi', '    jae r1t_woke', '    sti', '    hlt',
                      'r1t_after_hlt:', '    jmp r1t_idle', 'r1t_woke:', apic.rd(m, apic.CUR_COUNT), '    mov [{data} + rsi*8], rax',
                      '    dec r13d', '    jnz r1t_round',
                      # An expiry while IF = 0: pending in IRR, taken after STI's shadow (after the NOP).
                      '    mov r12, 2', apic.wr(m, apic.INIT_COUNT, 0x1000), '1:', apic.rd(m, apic.IRR + 2), '    test al, 1', '    jz 1b',
                      '    sti', '    nop', 'r1t_after_nop:', '    cli',
                      # Masked: the count reaches 0, nothing becomes pending, nothing is delivered.
                      '    mov r12, 3', apic.wr(m, apic.LVT_TIMER, 0x10000 | TV), apic.wr(m, apic.INIT_COUNT, 0x1000), '2:',
                      apic.rd(m, apic.CUR_COUNT), '    test eax, eax', '    jnz 2b', apic.rd(m, apic.IRR + 2), '    mov [{data} + 0x40], rax',
                      '    sti', '    nop', '    cli', apic.rd(m, apic.CUR_COUNT), '    mov [{data} + 0x48], rax'])
    return apic.build('r1-timer', m, [main], [TV], memory=apic_gdt())


def timer_expect(st, lab):
    log = apic.log(st.mb(apic.LOG(0), apic.LOG_BYTES))
    hlt = [(TV, lab['r1t_after_hlt'], 0x40, 1, 0, 0, 1)] * ROUNDS + [(TV, lab['r1t_after_nop'], 0x40, 1, 0, 0, 2)]
    return [('deliveries (vector, RIP, PPR, ISR[127:64], IRR[127:64], TPR, marker)', [e[:7] for e in log], hlt),
            ('count of 40H', st.m(apic.COUNTS(0) + 8 * TV), ROUNDS + 1),
            ('current count after each one-shot wake', [st.m(apic.DATA(0) + 8 * k) for k in range(1, ROUNDS + 1)], [0] * ROUNDS),
            ('masked expiry: IRR[95:64] clear, count 0', [st.m(apic.DATA(0) + 0x40) & 1, st.m(apic.DATA(0) + 0x48)], [0, 0])]


# ---- r1-serial (irqs.py: I/O APIC pin 4 -> 44H) --------------------------------------------------------------------
TEXT = b'R1a serial: THRE interrupts through IRQ4\r\n'
SV_ = irqs.EDGE_V


def serial_program(variant=False):
    m = 'x'
    n = len(TEXT) - (1 if variant else 0)          # negative control: the handler stops one byte early
    idx, done, iir, buf = apic.DATA(0) + 0x10, apic.DATA(0) + 0x18, apic.DATA(0) + 0x100, apic.DATA(0) + 0x800
    main = '\n'.join([irqs.head(m), own_gdt(AGDTR), irqs.pic_init(), irqs.entry(irqs.LINE, 0x10000 | SV_, 0), irqs.entry(irqs.LINE, SV_, 0),
                      '    mov r12, 1', irqs.ier(2), 'r1u_idle:', '    cli', f'    cmp byte [{done:#x}], 1', '    je r1u_out', '    sti', '    hlt',
                      '    jmp r1u_idle', 'r1u_out:', irqs.inb(irqs.COM1 + 2), '    mov [{data}], rax', irqs.inb(irqs.COM1 + 5), '    mov [{data} + 8], rax',
                      irqs.io_rd(0x10 + 2 * irqs.LINE), '    mov [{data} + 0x20], rax'])
    body = '\n'.join([irqs.inb(irqs.COM1 + 2), f'    mov rbx, [{idx:#x}]', f'    mov [{iir:#x} + rbx], al', f'    cmp rbx, {n}', '    jae 1f',
                      f'    movzx eax, byte [{buf:#x} + rbx]', f'    mov edx, {irqs.COM1:#x}', '    out dx, al', f'    inc qword [{idx:#x}]', '    jmp 2f',
                      '1:', irqs.ier(0), f'    mov byte [{done:#x}], 1', '2:'])
    prog, labels = apic.build('r1-serial', m, [main], [SV_], {SV_: body}, memory=apic_gdt())
    data = next(r for r in prog['memory'] if r['addr'] == f'0x{apic.DATA(0):x}')
    prog['memory'].remove(data)
    prog['memory'] += [P.zero(apic.DATA(0), 0x800), {'addr': f'0x{buf:x}', 'data': (TEXT + bytes(0x800 - len(TEXT))).hex()}]
    return prog, labels


def serial_expect(st, lab):
    n = len(TEXT)
    return [('UART bytes', st.uart, TEXT),
            ('44H deliveries (one per THRE rise: IER set, then each byte sent)', st.m(apic.COUNTS(0) + 8 * SV_), n + 1),
            ('IIR in each handler: THRE (02H)', list(st.mb(apic.DATA(0) + 0x100, n + 1)), [2] * (n + 1)),
            ('IIR, LSR at the end; the entry (edge, unmasked, idle)', [st.m(apic.DATA(0)) & 0xff, st.m(apic.DATA(0) + 8) & 0xff, st.m(apic.DATA(0) + 0x20)],
             [1, 0x60, SV_])]


# ---- int3 (apic.build, xAPIC) ------------------------------------------------------------------------------------
def int3_program(variant=False):
    m = 'x'
    off = 0 if variant else 1                      # negative control: regs->ip compared without the INT3's length
    main = '\n'.join([own_gdt(AGDTR), '    mov r12, 1', '    lea rdi, [{data}]', '    mov dword [rdi], 0', 'int3_selftest_ip:', '    int3',
                      '    nop', '    nop', '    nop', '    nop', '    mov eax, [rdi]', '    mov [{data} + 8], rax',
                      '    mov r12, 2', '    lea rdi, [{data} + 16]', '    mov dword [rdi], 0', 'r1i_other:', '    int3', '    mov eax, [rdi]',
                      '    mov [{data} + 24], rax', '    mov r12, 3', '    sti', 'r1i_if:', '    int3', '    cli', '    mov r12, 4', 'r1i_cd:', '    int 3',
                      '    mov r12, 5'])
    rec = apic.DATA(0) + 0x200
    body = '\n'.join(['    mov rax, [rsp+48]', f'    sub rax, {off}', '    lea rcx, [rel int3_selftest_ip]', '    cmp rax, rcx', '    jne 1f',
                      '    mov dword [rdi], 1', '1:', f'    mov rbx, [{rec - 8:#x}]', '    imul rbx, rbx, 0x30', f'    add rbx, {rec:#x}']
                     + [f'    mov rax, [rsp+{56 + 8 * j}]\n    mov [rbx+{8 * j}], rax' for j in range(4)]
                     + ['    pushfq', '    pop rax', '    mov [rbx+32], rax', f'    inc qword [{rec - 8:#x}]'])
    return apic.build('r1-int3', m, [main], [3], {3: body}, memory=apic_gdt())


def int3_expect(st, lab):
    log = apic.log(st.mb(apic.LOG(0), apic.LOG_BYTES))
    rips = [lab['int3_selftest_ip'] + 1, lab['r1i_other'] + 1, lab['r1i_if'] + 1, lab['r1i_cd'] + 2]
    frames = [[st.m(apic.DATA(0) + 0x200 + 0x30 * k + 8 * j) & (0x3300 if j in (1, 4) else M64) for j in range(5)] for k in range(4)]
    stack = P.STACK(0)
    return [('deliveries (vector, RIP, PPR, marker)', [(e[0], e[1], e[2], e[6]) for e in log], [(3, r, 0, k + 1) for k, r in enumerate(rips)]),
            ('selftest value; the other site left alone', [st.m(apic.DATA(0) + 8), st.m(apic.DATA(0) + 24)], [1, 0]),
            ('frames (CS, RFLAGS, RSP, SS, RFLAGS in the handler)', frames,
             [[0x08, f, stack, 0x10, 0x3000] for f in (0x3000, 0x3000, 0x3200, 0x3000)])]


# ---- running -----------------------------------------------------------------------------------------------------
class State:
    """One run's end state (natemu's or QEMU's, smp.parse_dump's shape) for `expect` functions."""
    def __init__(self, got, uart=None):
        self.got, self.uart = got, uart

    def r(self, name): return self.got['cpus'][0]['gpr'][P.REGS.index(name)]

    def mb(self, at, size):
        for reg in self.got['memory']:
            if reg['addr'] <= at and at + size <= reg['addr'] + len(reg['data']):
                return reg['data'][at - reg['addr']:at - reg['addr'] + size]
        raise KeyError(hex(at))

    def m(self, at, size=8): return int.from_bytes(self.mb(at, size), 'little')


def masked(got):
    """The TSC field (bytes 56..63) of apic.py's log entries cleared."""
    out = {'cpus': got['cpus'], 'memory': []}
    for reg in got['memory']:
        data = reg['data']
        if reg['addr'] == apic.LOG(0):
            data = bytearray(data)
            for k in range(0, len(data), 0x40): data[k + 56:k + 64] = bytes(8)
            data = bytes(data)
        out['memory'].append({'addr': reg['addr'], 'data': data})
    return out


def judge(expect, state, labels):
    out = []
    for what, got, want in expect(state, labels):
        if got != want:
            fmt = lambda v: [fmt(x) for x in v] if isinstance(v, (list, tuple)) else hex(v) if isinstance(v, int) else v
            out.append(f'{what}: {fmt(got)} != SDM {fmt(want)}')
    return out


# name: (builder, expectations); NEGATIVE: what each negative control changes.
COMPOSITES = {'r1-boot': (boot_program, boot_expect), 'r1-demand': (demand_program, demand_expect), 'r1-int3': (int3_program, int3_expect),
              'r1-poke': (poke_program, poke_expect), 'r1-syscall': (syscall_program, syscall_expect),
              'r1-timer': (timer_program, timer_expect), 'r1-serial': (serial_program, serial_expect)}
NEGATIVE = {'r1-boot': 'LRETQ to 0x08 instead of __KERNEL_CS 0x10', 'r1-demand': 'the handler maps each page one frame too far',
            'r1-int3': 'the handler compares regs->ip itself (not ip - 1) with int3_selftest_ip',
            'r1-poke': 'the poking address maps another frame (the new bytes never reach the code)',
            'r1-syscall': 'the exit path keeps the kernel tables (no switch to the user CR3)',
            'r1-timer': "QEMU's expectation mutated: every wake frame's RIP at the HLT itself (a wake that re-runs HLT)",
            'r1-serial': 'the handler stops one byte early'}


def run_one(a, name, report):
    build, expect = COMPOSITES[name]
    prog, labels = build()
    labels = labels[0] if isinstance(labels, list) else labels
    want, sent, err = qemu(prog, a.out / 'qemu' / name)
    entry = {'name': name, 'runs': [], 'problems': []}
    if want is None:
        entry['problems'].append(f'QEMU run failed ({err})')
        report.append(entry)
        return entry['problems']
    entry['problems'] += [f'QEMU vs SDM: {p}' for p in judge(expect, State(want, sent), labels)]
    for limit in (None, 1):
        out, problems = run_natemu(a, prog, a.out / 'work' / f"{name}-{limit or 'd'}", [limit] if limit else [])
        if out is not None:
            got = vm_state(prog, out)
            mine = bytes.fromhex(out.get('uart') or '')
            problems += smp.compare(prog, masked(got), masked(want))
            if mine != sent: problems.append(f'UART bytes {mine!r} != QEMU {sent!r}')
            problems += [f'natemu vs SDM: {p}' for p in judge(expect, State(got, mine), labels)]
        entry['runs'].append({'max_block': limit, 'problems': problems})
        entry['problems'] += [f'max-block {limit}: {p}' for p in problems]
    if a.negative:
        if name == 'r1-timer':
            bad = {'cpus': want['cpus'], 'memory': []}
            for reg in want['memory']:
                data = bytearray(reg['data'])
                if reg['addr'] == apic.LOG(0):
                    for k in range(0, len(data), 0x40):
                        rip = int.from_bytes(data[k + 8:k + 16], 'little')
                        if int.from_bytes(data[k:k + 8], 'little') == TV and rip == labels['r1t_after_hlt']:
                            data[k + 8:k + 16] = (rip - 1).to_bytes(8, 'little')
                bad['memory'].append({'addr': reg['addr'], 'data': bytes(data)})
            vprog, vwant = prog, bad
        else:
            vprog, vwant = build(variant=True)[0], want
        out, problems = run_natemu(a, vprog, a.out / 'work' / f'{name}-negative', [], reached=False)
        flagged = list(problems)
        if out is not None:
            got = vm_state(vprog, out)
            mine = bytes.fromhex(out.get('uart') or '')
            flagged += smp.compare(vprog, masked(got), masked(vwant)) + ([f'UART {mine!r}'] if mine != sent else [])
            flagged += judge(expect, State(got, mine), labels) if vprog is prog else []
        entry['negative'] = {'change': NEGATIVE[name], 'flagged': flagged[:8]}
        if not flagged: entry['problems'].append(f'negative control not flagged ({NEGATIVE[name]})')
    report.append(entry)
    print(f"{name}: {'FAIL' if entry['problems'] else 'ok'}" + (f"; negative flagged {len(entry['negative']['flagged'])}+: {entry['negative']['flagged'][0]}"
          if a.negative and entry['negative']['flagged'] else ''), *entry['problems'][:8], sep='\n  ', flush=True)
    return [f'{name}: {p}' for p in entry['problems']]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('mode', choices=['r1a'])
    ap.add_argument('--fexc', type=pathlib.Path, required=True)
    ap.add_argument('--out', type=pathlib.Path, required=True)
    ap.add_argument('--host', type=pathlib.Path)
    ap.add_argument('--runtime', type=pathlib.Path)
    ap.add_argument('--programs', type=lambda s: s.split(','))
    ap.add_argument('--no-negative', dest='negative', action='store_false')
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    if not (a.host and a.runtime): a.host, a.runtime = vm.build()
    report, failures = [], []
    for name in COMPOSITES:
        if not a.programs or name in a.programs:
            failures += run_one(a, name, report)
    (a.out / f'{a.mode}.json').write_text(json.dumps({'report': report, 'failures': failures}, indent=1, default=str) + '\n')
    for f in failures: print('FAIL', f)
    sys.exit(1 if failures else 0)


if __name__ == '__main__': main()

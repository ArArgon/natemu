"""Per-vCPU hidden segment descriptors at one shared compatibility-mode instruction address."""
import struct

import compat as C
from compat_limits import descriptor


def cases():
    return [program(n) for n in (2, 4)]


def program(n):
    P, S = C.P, C.S
    fs_base, gs_base = C.XPAGE, C.XPAGE + 64
    fs_word, gs_word = 0x12345678, 0xabcdef01
    gp_handler, int_handler = P.SYS_CODE(0) + 0x800, P.SYS_CODE(0) + 0xb00
    barrier = C.IDTR + 16
    rounds, stride = 6, 36
    user = f"""    mov ax, 0x2b
    mov fs, ax
    mov ax, 0x33
    mov gs, ax
    lock inc dword [{barrier:#x}]
.ready:
    cmp dword [{barrier:#x}], {n}
    jne .ready
.round:
    xor esi, esi
    mov edx, 4
.access:
    mov eax, edi
    mov ebx, edi
"""
    # Three successful iterations on both segments establish the loop links. The fourth executes
    # the same ModRM bytes with ESI=8, where this CPU's FS or GS dword exceeds its loaded limit.
    fs_rip = C.U + len(C.user32(user))
    fs = '    mov eax, [fs:esi]\n'
    gs = '    mov ebx, [gs:esi]\n'
    gs_rip = fs_rip + len(C.user32(fs))
    after_gs = gs_rip + len(C.user32(gs))
    user += fs + gs + """    dec edx
    jz .report
    add [edi+32], eax
    add [edi+32], ebx
    cmp edx, 1
    jne .access
    mov esi, 8
    jmp .access
.report:
    mov [edi], eax
    mov [edi+4], ebx
    int 0x80
"""
    return_rip = C.U + len(C.user32(user))
    user += f'    add edi, {stride}\n    dec ebp\n    jmp .round\n'
    # Only the two deliberate limit checks may fault. R10 and R15 are private to the 64-bit handlers.
    gp = f"""    mov r10, [rsp+8]
    mov [rdi+8], r10d
    cmp r10, {fs_rip:#x}
    je .fs
    cmp r10, {gs_rip:#x}
    jne .bad
    mov qword [rsp+8], {after_gs:#x}
    jmp .record
.fs:
    mov qword [rsp+8], {gs_rip:#x}
.record:
    mov r10, [rsp]
    mov [rdi+12], r10d
    mov r10, [rsp+16]
    mov [rdi+16], r10d
    inc dword [rdi+20]
    add rsp, 8
    iretq
.bad:
    jmp r15
"""
    report = """    mov r10, [rsp]
    mov [rdi+24], r10d
    mov r10, [rsp+8]
    mov [rdi+28], r10d
    cmp ebp, 1
    je .done
    iretq
.done:
    jmp r15
"""

    def region(addr, data):
        return {'addr': hex(addr), 'data': data.hex()}

    idt = bytearray(0x810)
    idt[13 * 16:14 * 16] = C.dm.gate(gp_handler, 8)
    idt[0x800:0x810] = C.dm.gate(int_handler, 8, dpl=3)
    memory = [region(C.U, C.user32(user)), region(gp_handler, P.asm(gp)), region(int_handler, P.asm(report)),
              region(C.IDT, idt), region(C.IDTR, struct.pack('<HQ', len(idt) - 1, C.IDT)), P.zero(barrier, 4),
              region(fs_base, struct.pack('<III', 3, 0, fs_word)), region(gs_base, struct.pack('<III', 7, 0, gs_word))]
    bodies, cpus, expected = [], [], {barrier: struct.pack('<I', n)}
    for i in range(n):
        f = S.fields(i)
        small, roomy = 4 + i, 16 + 4 * i
        fs_small = i % 2 == 0
        gdt = bytearray(S.gdt_image(i))
        for sel, base, limit in [(0x28, fs_base, small if fs_small else roomy),
                                 (0x30, gs_base, roomy if fs_small else small)]:
            gdt[sel:sel + 8] = descriptor(base, limit).to_bytes(8, 'little')
        memory += S.memory(i, n, gdt=gdt)
        enter = f'    mov edi, {f["c"]:#x}\n    mov ebp, {rounds}\n    lea r15, [rel .done]\n'
        # Each private GDT and TSS is visible in table A of CPU 0. All CPUs run under exactly that CR3.
        body = (f'    mov eax, {C.TA:#x}\n    mov cr3, rax\n' + S.SETUP.format(**f)
                + f'    mov ax, 0x70\n    ltr ax\n    lidt [{C.IDTR:#x}]\n'
                + C.ENTER(enter, rsp=f['us']) + '.done:\n')
        bodies.append(P.body(P.SYS_CODE(i), P.sys_text(i, body)))
        cpus.append(dict(P.cpu(i, P.SYS_CODE(i)), cpl=0))
        for k in range(rounds):
            addr = f['c'] + k * stride
            expected[addr] = struct.pack('<9I', addr if fs_small else fs_word, gs_word if fs_small else addr,
                                         fs_rip if fs_small else gs_rip, 0, 0xdb, 1, return_rip, 0xdb, 3 * (3 + 7))
    return dict(name=f'c32-per-cpu-fs-gs@{n}', ncpu=n, system=True, bodies=bodies, cpus=cpus, memory=memory), expected

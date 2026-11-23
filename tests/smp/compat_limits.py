"""FS/GS resident transactions in 32-bit user mode. The handler records the fault before changing guest registers."""
import struct

import compat as C


VALUE = bytes.fromhex('112233445566778899aabbccddeeff00')
MASK = struct.pack('<4I', 0xffffffff, 0xffffffff, 0, 0)
INDEX = struct.pack('<4I', 0, 1, 2, 3)
INITIAL = bytes.fromhex('78563412') * 4
CR2 = 0xdecafbad


def descriptor(base, limit, access=0xf3, flags=4):
    return (limit & 0xffff | (base & 0xffffff) << 16 | access << 40 |
            (limit >> 16 & 15) << 48 | flags << 52 | (base >> 24 & 255) << 56)


def load_bytes(at, value):
    return ''.join(f'    mov dword [{at + j:#x}], {int.from_bytes(value[j:j + 4], "little"):#x}\n'
                   for j in range(0, len(value), 4))


def cases():
    # Each instruction follows a successful load of a real descriptor. A separate register selects the
    # control segment, so mixing the two segments or validating a based linear address fails the case.
    tls = C.W + 0x80
    cases = []

    def add(name, insn, *, base=tls, limit=3, access=0xf3, flags=4, segment='fs', prefix='', success=False,
            xmm1=INITIAL, xmm3=MASK, memory=VALUE, selector=0x2b, cpl3_mode=32, after_load='', mapped=(),
            kernel_setup='', enter_flags=2, fault=13):
        raw = descriptor(base, limit, access, flags)
        setup = (f'    mov rax, {raw:#x}\n    mov [{C.F["g"] + 0x28:#x}], rax\n'
                 '    mov rax, cr4\n    bts rax, 9\n    bts rax, 18\n    mov cr4, rax\n'
                 '    xor ecx, ecx\n    mov eax, 7\n    xor edx, edx\n    xsetbv\n'
                 f'    mov eax, {CR2:#x}\n    mov cr2, rax\n')
        for offset, data in [(0, INITIAL), (0x10, INDEX), (0x20, MASK), (0x80, VALUE)]:
            setup += load_bytes(C.W + offset, data)
        user = (f'bits {cpl3_mode}\n    mov eax, {selector:#x}\n    mov {segment}, ax\n'
                f'    movdqu xmm1, [{C.W:#x}]\n    movdqu xmm2, [{C.W + 16:#x}]\n'
                f'    movdqu xmm3, [{C.W + 32:#x}]\n    xor edi, edi\n    xor eax, eax\n'
                + after_load + prefix)
        fault_rip = C.U + len(C.user32(user))
        user += f'    {insn}\n    int 0x80\n'
        rip = C.U + len(C.user32(user)) if success else fault_rip
        saved = f'    mov rax, cr2\n    mov [{C.W + 0x40:#x}], rax\n    jmp .fin\n'
        enter = C.ENTER(cs=0xdb if cpl3_mode == 32 else 0x23).replace('    push 2\n', f'    push {enter_flags}\n')
        prog = C.program(name, C.gate(12, '.v12') + setup + kernel_setup + enter
                         + C.handlers(saved, xmm=True, vectors=(6, 12, 13, 14, 128)), user, memory=mapped)
        # Handler offsets are from compat.handlers, the target memory and CR2 are independent of the recorded frame.
        expected = {C.LOG: struct.pack('<Q', 1), C.REC: struct.pack('<QQQQ', 128 if success else fault, 0, rip,
                                                                     0xdb if cpl3_mode == 32 else 0x23),
                    C.REC + 0x110: xmm1, C.REC + 0x130: xmm3,
                    C.W + 0x40: struct.pack('<Q', CR2), tls: memory}
        cases.append((prog, expected))

    add('fs-full-word-at-limit', 'mov [fs:2], ds', success=True,
        memory=VALUE[:2] + b'\x1b\x00' + VALUE[4:])
    add('gs-word-over-limit', 'mov [gs:3], ds', segment='gs')
    add('fs-readable-only', 'mov [fs:0], ds', access=0xf1)
    add('fs-bound-second-dword', 'bound eax, [fs:0]', limit=6)
    add('gs-far-pointer-selector-overrun', 'lfs eax, [gs:0]', limit=4, segment='gs')
    add('fs-image-span', 'fxsave [fs:0]', limit=300)
    add('gs-x87-environment-span', 'fnstenv [gs:0]', limit=26, segment='gs')
    add('gs-before-unmapped-page', 'mov [gs:3], ds', segment='gs', base=C.XPAGE,
        kernel_setup=f'    and qword [{C.PTE(C.XPAGE):#x}], -2\n    invlpg [{C.XPAGE:#x}]\n')
    add('fs-before-alignment', 'mov [fs:3], ds', enter_flags=0x40002,
        kernel_setup='    mov rax, cr0\n    bts rax, 18\n    mov cr0, rax\n')
    add('gs-expand-down-first', 'mov [gs:5], ds', limit=4, access=0xf7, flags=0, segment='gs', success=True,
        memory=VALUE[:5] + b'\x1b\x00' + VALUE[7:])
    add('gs-expand-down-low', 'mov [gs:4], ds', limit=4, access=0xf7, flags=0, segment='gs')
    add('gs-expand-down-high16', 'mov [gs:0xffff], ds', limit=4, access=0xf7, flags=0, segment='gs')
    add('fs-expand-down-high32', 'mov [fs:0xffffffff], ds', limit=4, access=0xf7)
    add('fs-granular-base-wrap', 'mov [fs:0xffe], ds', base=(tls - 0xffe) & 0xffffffff, limit=0, flags=12,
        success=True, memory=b'\x1b\x00' + VALUE[2:])
    add('fs-granular-overrun', 'mov [fs:0xfff], ds', base=(tls - 0xfff) & 0xffffffff, limit=0, flags=12)
    add('fs-wrapped-base', 'mov [fs:0xfffff000], ds', base=tls + 0x1000, limit=0xfffff, flags=12,
        success=True, memory=b'\x1b\x00' + VALUE[2:])
    add('gs-ignored-limit64', 'mov [gs:4], ds', segment='gs', cpl3_mode=64, success=True,
        memory=VALUE[:4] + b'\x1b\x00' + VALUE[6:])
    set0 = '    vpxor xmm3, xmm3, xmm3\n'
    add('fs-gather-second-lane', 'vpgatherdd xmm1, [fs:eax+xmm2*4], xmm3',
        xmm1=VALUE[:4] + INITIAL[4:], xmm3=bytes(4) + MASK[4:])
    add('fs-gather-zero', 'vpgatherdd xmm1, [fs:eax+xmm2*4], xmm3', limit=0,
        prefix=set0, xmm3=bytes(16), success=True)
    add('gs-maskstore-second-lane', 'vpmaskmovd [gs:eax], xmm3, xmm1', segment='gs')
    add('gs-maskload-second-lane', 'vpmaskmovd xmm1, xmm3, [gs:eax]', segment='gs')
    add('gs-maskstore-zero', 'vpmaskmovd [gs:eax], xmm3, xmm1', segment='gs', limit=0,
        prefix=set0, xmm3=bytes(16), success=True)
    add('gs-maskload-zero', 'vpmaskmovd xmm1, xmm3, [gs:eax]', segment='gs', limit=0,
        prefix=set0, xmm3=bytes(16), xmm1=bytes(16), success=True)
    add('fs-maskmovq-zero', 'fs maskmovq mm1, mm3', prefix='    pxor mm3, mm3\n')
    add('fs-maskmovdqu-zero', 'fs maskmovdqu xmm1, xmm3', prefix=set0, xmm3=bytes(16))
    add('gs-null-maskstore-zero', 'vpmaskmovd [gs:eax], xmm3, xmm1', segment='gs', selector=0,
        prefix=set0, xmm3=bytes(16))
    add('fs-null-gather-zero', 'vpgatherdd xmm1, [fs:eax+xmm2*4], xmm3', selector=0,
        prefix=set0, xmm3=bytes(16))
    # MOVAPS also needs 16-byte alignment. A flat segment overrun wins even with page zero absent.
    add('ss-flat-range-before-simd-alignment', 'movaps xmm1, [ebp]', prefix='    mov ebp, 0xfffffff8\n',
        fault=12, mapped=C.NOT_MAPPED)
    add('ds-flat-range-before-simd-alignment', 'movaps xmm1, [ebx]', prefix='    mov ebx, 0xfffffff8\n',
        mapped=C.NOT_MAPPED)
    # Native CMPS reads the ES destination first. Fault only the named operand and leave its peer valid.
    for instruction in ('lodsd', 'movsd', 'cmpsd'):
        add(f'ss-string-source-{instruction}', f'ss {instruction}', fault=12, mapped=C.TOP_ONLY,
            prefix=f'    mov esi, 0xfffffffe\n    mov edi, {C.W:#x}\n')
    for instruction in ('movsd', 'cmpsd', 'stosd', 'scasd'):
        add(f'es-string-destination-{instruction}', f'ss {instruction}', mapped=C.TOP_ONLY,
            prefix=f'    mov esi, {C.W:#x}\n    mov edi, 0xfffffffe\n')
    add('ss-moffs-load', 'mov eax, [ss:0xfffffffe]', fault=12, mapped=C.TOP_ONLY)
    cases.append(mixed_user_wrap())
    return cases


def mixed_user_wrap():
    """A 64-bit user enters compatibility code in its own address space and checks a boundary stack and wrapped SIB."""
    high = (8 << 30) + C.XPAGE
    memory = C.WRAP + [C.entry(C.TA + 0x1000 + 8 * 8, (C.TA + 0x2000) | C.P.P | C.P.RW | C.P.US | C.P.A),
                       C.P.zero(C.XPAGE, 4)]
    # The high user VA aliases XPAGE, a different guest page from either end of the 32-bit address space.
    user = f"""bits 64
    mov rax, {high:#x}
    mov dword [rax], 0xabbacd32
    mov dword [0x24], 0x44223311
    mov eax, 0xfffffff8
    mov dword [rax], 0x98765432
    push 0x1b
    push {C.US:#x}
    push 2
    push 0xdb
    mov eax, .in32
    push rax
    iretq
bits 32
.in32:
    mov ebp, 0x30
    mov eax, [ebp-12]
    inc eax
    mov ebx, 0xfffffff0
    mov ecx, 9
    mov [ebx+ecx*4+16], eax
    xor esp, esp
    push dword [ebx+8]
    pop dword [esp+8]
    mov edi, esp
    mov esp, {C.US:#x}
    int 0x80
"""
    prog = C.program('mixed-user-wrap', C.ENTER(cs=0x23) + C.handlers(), user, memory)
    return prog, {C.LOG: struct.pack('<Q', 1), C.REC: struct.pack('<QQ', 128, 0),
                  C.REC + 0x18: struct.pack('<Q', 0xdb), C.REC + 0x78: struct.pack('<I', 0),
                  C.LOW0 + 0x24: struct.pack('<I', 0x44223312), C.LOW0 + 8: struct.pack('<I', 0x98765432),
                  C.HIGH + 0xffc: struct.pack('<I', 0x98765432), C.XPAGE: struct.pack('<I', 0xabbacd32)}

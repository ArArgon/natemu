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


def inline_base_refresh(segment):
    first, second = C.W + 0x80, C.W + 0x90
    initial, replacement, write = 0x12345678, 0x89abcdef, 0x18425a7e
    raw = descriptor(first, 3, access=0xf1)

    def wrmsr(index, value):
        return f'    mov ecx, {index:#x}\n    mov eax, {value:#x}\n    xor edx, edx\n    wrmsr\n'

    setup = f'''    mov rax, {raw:#x}
    mov [{C.F['g'] + 0x28:#x}], rax
    mov dword [{first:#x}], {initial:#x}
    mov dword [{second:#x}], {replacement:#x}
    mov eax, {CR2:#x}
    mov cr2, rax
'''
    if segment == 'gs':
        setup += wrmsr(0xc0000102, second)  # IA32_KERNEL_GS_BASE, exchanged in the first handler
    change = '    swapgs\n' if segment == 'gs' else wrmsr(0xc0000100, second)  # IA32_FS_BASE
    init = f'''    mov eax, 0x2b
    mov {segment}, ax
    xor edi, edi
    stc
    mov edx, {write:#x}
    jmp short $+2
'''
    read = f'    mov ecx, [{segment}:edi]\n    jmp short $+2\n'
    overrun = f'    mov eax, [{segment}:edi+1]\n    int 0x80\n'
    read_rip = C.U + len(C.user32(init))
    fault_rip = C.U + len(C.user32(init + read))
    store_rip = C.U + len(C.user32(init + read + overrun))
    user = init + read + overrun + f'    mov [{segment}:edi], edx\n    int 0x80\n'

    # Retry the faulting read across the base update, then revisit the same valid read and
    # finally try writing within the unchanged read-only limit.
    handler = f'''    mov rax, cr2
    mov [{C.REC + 0x100:#x} + rbx], rax
    cmp qword [rsp], 13
    jne .fin
    cmp rbx, 0
    je .change_base
    cmp rbx, 0x200
    je .read_again
    cmp rbx, 0x400
    jne .fin
    mov qword [rsp+16], {store_rip:#x}
    jmp .resume
.read_again:
    mov qword [rsp+16], {read_rip:#x}
    jmp .resume
.change_base:
{change}    mov rcx, [{C.REC + 0x48:#x} + rbx]
    mov rdx, [{C.REC + 0x50:#x} + rbx]
    jmp .resume
'''
    method = 'swapgs' if segment == 'gs' else 'wrmsr'
    prog = C.program(f'{segment}-inline-refresh-{method}', setup + C.ENTER() + C.handlers(handler), user)
    prog['production_admission'] = True
    expected = {C.LOG: struct.pack('<Q', 4), first: struct.pack('<I', initial), second: struct.pack('<I', replacement)}
    for i, (rip, word) in enumerate(((fault_rip, initial), (fault_rip, initial),
                                     (fault_rip, replacement), (store_rip, replacement))):
        rec = C.REC + 0x200 * i
        expected[rec] = struct.pack('<7Q', 13, 0, rip, 0xdb, 0x10045, C.US, 0x1b)
        expected[rec + 0x40] = struct.pack('<3Q', 0x2b, word, write)
        expected[rec + 0xd0] = struct.pack('<2Q', 0x2b if segment == 'fs' else 0, 0x2b if segment == 'gs' else 0)
        expected[rec + 0x100] = struct.pack('<Q', CR2)
    return prog, expected


def identity_switch(faults):
    """M2 checkpoint C: one instruction at one address under FS segments of both code identities, reloaded between
    passes: a flat-limit one (no operand check in its code) and a limited one. `faults`: the limited segment is too small,
    so the pass after the flat one must fault there (a block of the flat identity must not serve it); else it is roomy and
    five alternating passes all run, the resident deciding no check."""
    tls = C.W + 0x80
    word = int.from_bytes(VALUE[4:8], 'little')
    setup = (f'    mov rax, {descriptor(tls, 3 if faults else 0xff):#x}\n    mov [{C.F["g"] + 0x28:#x}], rax\n'
             f'    mov rax, {descriptor(tls, 0xfffff, flags=12):#x}\n    mov [{C.F["g"] + 0x30:#x}], rax\n')
    for offset, data in [(0x80, VALUE)]:
        setup += load_bytes(C.W + offset, data)
    head = f'    mov edx, {0x33 if faults else 0x2b:#x}\n    mov esi, 5\n    xor edi, edi\n    xor ecx, ecx\n.again:\n    mov fs, dx\n'
    insn = '    add ecx, [fs:edi+4]\n'
    tail = '    xor edx, 0x18\n    dec esi\n    jnz .again\n    int 0x80\n'
    fault_rip = C.U + len(C.user32(head))
    end_rip = C.U + len(C.user32(head + insn + tail))
    prog = C.program(f'fs-identity-switch-{"fault" if faults else "run"}', setup + C.ENTER() + C.handlers('    jmp .fin\n'), head + insn + tail)
    prog['segment_checks'] = [int(faults)]
    prog['require_link'] = not faults
    rip, ecx = (fault_rip, word) if faults else (end_rip, 5 * word & 0xffffffff)
    return prog, {C.LOG: struct.pack('<Q', 1), C.REC: struct.pack('<QQQQ', 13 if faults else 128, 0, rip, 0xdb),
                  C.REC + 0x48: struct.pack('<I', ecx), tls: VALUE}


def cases(*, reference_control=False):
    # Each instruction follows a successful load of a real descriptor. A separate register selects the
    # control segment, so mixing the two segments or validating a based linear address fails the case.
    tls = C.W + 0x80
    cases = [inline_base_refresh(segment) for segment in ('fs', 'gs')]

    def add(name, insn, *, base=tls, limit=3, access=0xf3, flags=4, segment='fs', prefix='', success=False,
            xmm1=INITIAL, xmm3=MASK, memory=VALUE, selector=0x2b, cpl3_mode=32, after_load='', mapped=(),
            kernel_setup='', enter_flags=2, fault=13, ecx=None, eax=None, reload_limit=None, retry_regs=None, rflags=None, captured=None, error=0, cr2=CR2, after_trap=False, firmware=None, refuse=None, strict=False, fills=None, undefined_af=False, flags_mask=0x10ed5, production=False, frame_alias=None, require_link=False, ymm1_high=None, fp_controls=None, checks=None, whole_blocks=False):
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
        if ymm1_high is not None:
            user += f'    vinsertf128 ymm1, ymm1, [{C.W + 16:#x}], 1\n'
        if fp_controls:
            user += '    int 0x7f\n'
        fault_rip = C.U + len(C.user32(user))
        user += f'    {insn}\n'
        trap_rip = C.U + len(C.user32(user)) if after_trap else fault_rip
        if undefined_af:
            user += '    pushfd\n    and dword [esp], -17\n    popfd\n'
        user += '    int 0x80\n'
        rip = C.U + len(C.user32(user)) if success else trap_rip
        saved = f'    mov rax, cr2\n    mov [{C.W + 0x40:#x}], rax\n    jmp .fin\n'
        if ymm1_high is not None:
            saved = f'    vextractf128 [{C.W + 0x60:#x}], ymm1, 1\n' + saved
        if firmware is not None:
            saved = f'    xor eax, eax\n    movzx eax, word [rax]\n    mov [{C.W + 0x48:#x}], eax\n' + saved
        if reload_limit is not None:
            # The first fault edits the table. The unchanged hidden descriptor faults a second time.
            # Only the second handler reloads FS; both IRETs retry the same compiled instruction.
            changed = descriptor(base, reload_limit, access, flags)
            saved = (f'    cmp qword [{C.LOG:#x}], 1\n    jne .reload\n    mov rax, {changed:#x}\n'
                     f'    mov [{C.F["g"] + 0x28:#x}], rax\n    jmp .resume\n.reload:\n'
                     f'    cmp qword [{C.LOG:#x}], 2\n    jne .done\n    mov ax, {selector:#x}\n'
                     f'    mov {segment}, ax\n    jmp .resume\n.done:\n' + saved)
        enter = C.ENTER(cs=0xdb if cpl3_mode == 32 else 0x23, flags=enter_flags)
        gates = C.gate(12, '.v12')
        vectors = [1, 6, 12, 13, 14, 128]
        if fault in (0, 1, 7, 17):
            gates += C.gate(fault, f'.v{fault}')
        if fault == 0:
            vectors.insert(0, 0)
        elif fault in (7, 17):
            vectors.append(fault)
        handlers = C.handlers(saved, xmm=True, vectors=tuple(vectors), flags_mask=flags_mask)
        if fp_controls:
            # Set the FP gate after the user seeded its YMM register. Restore it in the fault
            # handler before the logger reads XMM/YMM, preserving the faulting register bank.
            enable, restore = fp_controls
            gates += C.gate(0x7f, '.fpctl', 3)
            handlers = ('.fpctl:\n    push rax\n' + enable + '    pop rax\n    iretq\n'
                        + handlers.replace(f'.v{fault}:\n', f'.v{fault}:\n    push rax\n' + restore + '    pop rax\n'))
        prog = C.program(name, gates + setup + kernel_setup + enter + handlers, user, memory=mapped)
        # Handler offsets are from compat.handlers, the target memory and CR2 are independent of the recorded frame.
        expected = {C.LOG: struct.pack('<Q', 1), C.REC: struct.pack('<QQQQ', 128 if success else fault, error, rip,
                                                                     0xdb if cpl3_mode == 32 else 0x23),
                    C.REC + 0x110: xmm1, C.REC + 0x130: xmm3,
                    C.W + 0x40: struct.pack('<Q', cr2), tls: memory}
        if ymm1_high is not None:
            expected[C.W + 0x60] = ymm1_high
        if captured:
            expected.update(captured)
        if rflags is not None:
            expected[C.REC + 0x20] = struct.pack('<Q', rflags)
        if eax is not None:
            expected[C.REC + 0x40] = struct.pack('<I', eax)
        if ecx is not None:
            expected[C.REC + 0x48] = struct.pack('<I', ecx)
        if reload_limit is not None:
            expected.update({addr + 0x400: data for addr, data in expected.items() if C.REC <= addr < C.REC + 0x200})
            for delta in (0, 0x200):
                expected[C.REC + delta] = struct.pack('<QQQQ', 13, 0, fault_rip, 0xdb)
                for addr, value in (retry_regs or {C.REC + 0x48: struct.pack('<I', 0x12345678)}).items():
                    expected[addr + delta] = value
            expected[C.LOG] = struct.pack('<Q', 3)
        if production:
            prog['production_admission'] = True
        if whole_blocks:
            prog['whole_blocks'] = True   # not also one instruction per block
        if checks is not None:
            prog['segment_checks'] = [checks]   # operand checks the resident decides (0: all passed inline)
        if frame_alias is not None:
            prog['frame_alias'] = frame_alias
        if require_link:
            prog['require_link'] = True
        if fills is not None:
            prog['unassigned_fills'] = fills
        if firmware is not None:
            prog['firmware'] = firmware.hex()
            expected[C.W + 0x48] = firmware[:2] + bytes(2)
        if refuse is not None:
            prog['limit_refusal'] = (refuse, fault_rip)
            prog['strict_unassigned'] = strict
            expected = {C.LOG: bytes(8), **(captured or {})}
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
    # An empty VEX lane mask makes no memory access. FS/GS is null; EAX still points at a writable
    # sentinel so a stray store, a missing #GP on an active lane, or a changed load destination is visible.
    at_sentinel = f'    mov eax, {tls:#x}\n'
    off = '    movdqa xmm3, xmm2\n'   # INDEX has nonzero low bits but every dword MSB is clear
    for segment, name, insn in [
        ('gs', 'maskstore', 'vpmaskmovd [gs:eax], xmm3, xmm1'),
        ('fs', 'maskload', 'vmaskmovps xmm1, xmm3, [fs:eax]'),
        ('fs', 'gather', 'vpgatherdd xmm1, [fs:eax+xmm2*4], xmm3'),
    ]:
        null = dict(segment=segment, selector=0, captured={C.REC + (0xd0 if segment == 'fs' else 0xd8): bytes(8)})
        add(f'{segment}-null-{name}-zero', insn, **null, prefix=set0 + at_sentinel, success=True,
            xmm1=bytes(16) if name == 'maskload' else INITIAL, xmm3=bytes(16))
        add(f'{segment}-null-{name}-msbs-clear', insn, **null, prefix=off + at_sentinel, success=True,
            xmm1=bytes(16) if name == 'maskload' else INITIAL, xmm3=bytes(16) if name == 'gather' else INDEX)
        add(f'{segment}-null-{name}-first-active', insn, **null, prefix=at_sentinel)
    add('gs-null-maskstore-avx-disabled', 'vpmaskmovd [gs:eax], xmm3, xmm1', segment='gs', selector=0,
        prefix='    pxor xmm3, xmm3\n' + at_sentinel, xmm3=bytes(16), fault=6,
        kernel_setup='    mov rax, cr4\n    btr rax, 18\n    mov cr4, rax\n')
    # Force ModRM encodings (the assembler can select moffs for an absolute EAX operand).
    seed = '    mov ecx, 0x12345678\n'
    add('fs-generated-read-end', 'mov ecx, [fs:edi]', success=True, prefix=seed, ecx=0x44332211)
    add('gs-generated-read-overrun', 'mov ecx, [gs:edi+1]', segment='gs',
        prefix=seed + '    cmp al, al\n    stc\n', ecx=0x12345678, rflags=0x10045)
    add('fs-generated-store-overrun', 'mov [fs:edi+1], ecx', prefix=seed, ecx=0x12345678)
    add('gs-generated-store-end', 'mov [gs:edi], ecx', prefix='    mov ecx, 0xabcdef06\n',
        segment='gs', success=True, ecx=0xabcdef06, memory=bytes.fromhex('06efcdab') + VALUE[4:])
    add('gs-generated-store-readonly', 'mov [gs:edi], ecx', access=0xf1, segment='gs', prefix=seed, ecx=0x12345678)
    add('fs-generated-read-misaligned-word', 'mov cx, [fs:edi+3]', enter_flags=0x40002, prefix=seed, ecx=0x12345678,
        kernel_setup='    mov rax, cr0\n    bts rax, 18\n    mov cr0, rax\n')
    add('fs-generated-word-at-limit', 'mov cx, [fs:edi+2]', prefix=seed, success=True, ecx=0x12344433)
    add('gs-generated-byte-at-limit', 'mov ch, [gs:edi+3]', prefix=seed, segment='gs', success=True,
        ecx=0x12344478)
    add('fs-generated-address16-refusal', 'mov cx, [fs:bx+si+2]', prefix=seed + '    mov ebx, 0xffff\n    xor esi, esi\n',
        fault=6, ecx=0x12345678)
    add('fs-generated-retry-after-reload', 'mov ecx, [fs:edi+1]', prefix=seed, success=True,
        ecx=0x55443322, reload_limit=7)
    # Re-enter a compiled dword MOV loop with each placement of the owner's read-only table.
    # Preserve live flags across the hot store/load. Both fixture addresses are ordinary guest RAM.
    loop = (seed + '    mov edx, 5\n    jmp .again\n.again:\n    mov [fs:edi], ecx\n'
            '    mov ecx, [fs:edi]\n    dec edx\n    jnz .again\n    cmp dl, dl\n    stc\n')
    for placement, alias in [('after', 0xffff400000001000), ('before', 0xffff40003fe01000)]:
        add(f'fs-inline-linked-{placement}', 'mov eax, [fs:edi]', prefix=loop, success=True, eax=0x12345678,
            ecx=0x12345678, memory=bytes.fromhex('78563412') + VALUE[4:], rflags=0x45,
            frame_alias=alias, production=True, require_link=True)
    add('gs-inline-hot-write-watch', 'mov [gs:edi], ecx', segment='gs', prefix=seed, fault=1, after_trap=True,
        kernel_setup=f'    mov eax, {tls:#x}\n    mov dr0, rax\n    mov eax, 0x10001\n    mov dr7, rax\n',
        memory=bytes.fromhex('78563412') + VALUE[4:], production=True)
    add('fs-inline-hot-write-pf', 'mov [fs:edi], ecx', base=C.XPAGE, prefix=seed, fault=14, error=6, cr2=C.XPAGE,
        kernel_setup=f'    and qword [{C.PTE(C.XPAGE):#x}], -2\n    invlpg [{C.XPAGE:#x}]\n',
        ecx=0x12345678, production=True)
    # Compile and call a user subroutine, update its immediate through a checked FS store, then
    # call the same entry again. Its result observes normal dirty-code invalidation and relinking.
    modify = (seed + '    jmp .go\n.callee:\n    mov eax, 0x11111111\n    ret\n.go:\n    call .callee\n'
              f'    mov edi, .callee + 1 - {C.U:#x}\n')
    add('fs-inline-hot-code-store', 'mov [fs:edi], ecx\n    call .callee', base=C.U, limit=0xfff, prefix=modify,
        eax=0x12345678, success=True, production=True)
    # An in-range segment offset locates bytes on the final and first linear pages, in that order.
    high, low = C.HIGH + 4094, C.LOW0
    init = '    mov ax, 0x2211\n    mov rbx, 0xfffffffe\n    mov [rbx], ax\n'
    up = init + '    xor ebx, ebx\n    mov word [rbx], 0x4433\n'
    have = {high: bytes.fromhex('1122'), low: bytes.fromhex('3344')}
    # C6/C7 /0 have no source GPR. In particular the wrapped resident completion must preserve
    # EAX and use the same immediate bytes as the ordinary native store.
    imm_regs = '    mov eax, 0x13572468\n    cmp al, al\n    stc\n'
    imm = dict(prefix=imm_regs, eax=0x13572468)
    add('fs-generated-imm-byte', 'mov byte [fs:edi+3], 0xfe', success=True,
        memory=VALUE[:3] + bytes([0xfe]) + VALUE[4:], **imm)
    add('gs-generated-imm-word', 'mov word [gs:edi+2], 0x81c3', success=True, segment='gs',
        memory=VALUE[:2] + bytes.fromhex('c381') + VALUE[4:], **imm)
    add('fs-generated-imm-absolute-zero', 'mov dword [fs:0], 0', success=True,
        memory=bytes(4) + VALUE[4:], **imm)
    add('gs-generated-imm-limit-before-page', 'mov dword [gs:edi+1], 0x89abcdef', base=C.XPAGE, segment='gs',
        kernel_setup=f'    and qword [{C.PTE(C.XPAGE):#x}], -2\n    invlpg [{C.XPAGE:#x}]\n',
        rflags=0x10045, **imm)
    add('gs-generated-imm-indexed-write-pf', 'mov dword [gs:edi+esi*4+8], 0x89abcdef', base=C.XPAGE - 16,
        segment='gs', limit=19, fault=14, error=7, cr2=C.XPAGE, eax=0x13572468, rflags=0x10045,
        prefix=imm_regs + '    mov esi, 2\n',
        kernel_setup=f'    and qword [{C.PTE(C.XPAGE):#x}], -3\n    invlpg [{C.XPAGE:#x}]\n')
    add('fs-generated-imm-readonly', 'mov byte [fs:edi], 0xa5', access=0xf1, rflags=0x10045, **imm)
    add('fs-generated-imm-address16-refusal', 'mov byte [fs:bx+si], 0x12', fault=6, **imm)
    add('gs-generated-imm-wrapped-word', 'mov word [gs:edi], 0x81c3', base=0xffffffff, segment='gs',
        mapped=C.WRAP, limit=1, success=True, kernel_setup=up, rflags=0x45,
        captured={high: bytes.fromhex('11c3'), low: bytes.fromhex('8144')}, **imm)
    add('fs-generated-imm-wrapped-dword', 'mov dword [fs:edi], 0x89abcdef', base=0xfffffffe, mapped=C.WRAP,
        success=True, kernel_setup=up, rflags=0x45,
        captured={high: bytes.fromhex('efcd'), low: bytes.fromhex('ab89')}, **imm)
    add('gs-generated-imm-wrapped-missing', 'mov dword [gs:edi], 0x89abcdef', base=0xfffffffe, segment='gs',
        mapped=C.TOP_ONLY, fault=14, error=6, cr2=0, kernel_setup=init, rflags=0x10045,
        captured={high: bytes.fromhex('1122')}, **imm)
    # String source and destination have independent selectors. Inspect the saved element boundary, including
    # both pointers and the count, even when the first element succeeds and the next one faults.
    dest = C.W + 0xc0

    def indices(si, di):
        return {C.REC + 0x70: struct.pack('<II', si, 0), C.REC + 0x78: struct.pack('<II', di, 0)}

    def regs(si, di, count):
        return f'    mov esi, {si:#x}\n    mov edi, {di:#x}\n    mov ecx, {count}\n'

    add('fs-string-movs-word', 'fs movsw', success=True, prefix=regs(1, dest, 7),
        ecx=7, captured=indices(3, dest + 2) | {dest: VALUE[1:3]})
    add('gs-string-rep-lods-backward', 'gs rep lodsb', segment='gs', success=True,
        prefix=regs(1, dest, 2) + '    mov eax, 0xaabbccdd\n    std\n', ecx=0,
        captured=indices(0xffffffff, dest) | {C.REC + 0x40: struct.pack('<I', 0xaabbcc11)})
    add('fs-string-lods-wrapped', 'fs lodsd', success=True, base=0xfffffffe, mapped=C.WRAP,
        kernel_setup=up, prefix=regs(0, dest, 1), captured=indices(4, dest) |
        {C.REC + 0x40: struct.pack('<I', 0x44332211)})
    add('fs-string-repe-mismatch', 'fs repe cmpsb', success=True, prefix=regs(0, tls + 1, 3),
        ecx=2, rflags=0x91, captured=indices(1, tls + 2))
    add('gs-string-repne-equal', 'gs repne cmpsd', segment='gs', success=True, prefix=regs(0, tls, 3),
        ecx=2, rflags=0x44, captured=indices(4, tls + 4))
    # Intel SDM Vol. 2B, REP/REPE/REPZ/REPNE/REPNZ: "When a fault occurs during the execution of a CMPS or SCAS
    # instruction that is prefixed with REPE or REPNE, the EFLAGS value is restored to the state prior to the execution
    # of the instruction" (0x45 here, CF from STC). AMD keeps the last comparison's flags (0x44; compat-kvm records it).
    add('fs-string-repe-fault-flags', 'fs repe cmpsb', limit=0, prefix=regs(0, tls, 3) + '    stc\n',
        ecx=2, rflags=0x10045, captured=indices(1, tls + 1))
    if reference_control:
        # The same first comparison and register progress as the segment fault above. Here the
        # segment permits all elements; paging alone faults on the second FS source byte.
        missing = C.XPAGE + 4096
        setup = (f'    mov byte [{missing - 1:#x}], 0x11\n'
                 f'    and qword [{C.PTE(missing):#x}], -2\n    invlpg [{missing:#x}]\n')
        add('fs-string-repe-page-flags', 'fs repe cmpsb', base=missing - 1, limit=3, kernel_setup=setup,
            prefix=regs(0, tls, 3) + '    stc\n', ecx=2, rflags=0x10045, captured=indices(1, tls + 1),
            fault=14, error=4, cr2=missing)
    # DR0 observes the second operand of the first completed compare. The #DB frame has that
    # iteration's new flags and indices, RF set, and RIP at the unfinished string. A subsequent
    # access fault would instead restore the entry flags (the preceding test sets CF to distinguish).
    watch_dest = f'    mov eax, {dest:#x}\n    mov dr0, rax\n    mov eax, 0x30001\n    mov dr7, rax\n'
    add('fs-string-repe-watch-completed', 'fs repe cmpsb', prefix=regs(0, dest, 3) + '    stc\n',
        fault=1, ecx=2, rflags=0x10044, kernel_setup=f'    mov byte [{dest:#x}], 0x11\n' + watch_dest,
        captured=indices(1, dest + 1))
    add('gs-string-rep-movs-progress', 'gs rep movsd', segment='gs', prefix=regs(0, dest, 2), ecx=1,
        captured=indices(4, dest + 4) | {dest: VALUE[:4] + bytes(4)})
    add('gs-string-rep-movs-reload', 'gs rep movsd', segment='gs', success=True, reload_limit=7,
        prefix=regs(0, dest, 2), ecx=0, captured=indices(8, dest + 8) | {dest: VALUE[:8]},
        retry_regs=indices(4, dest + 4) | {C.REC + 0x48: struct.pack('<I', 1)})
    hole = 0x90000000
    add('gs-string-unassigned-lods', 'gs rep lodsd', segment='gs', success=True, base=C.XPAGE, limit=11,
        prefix=regs(0, dest, 3), ecx=0, captured=indices(12, dest) | {C.REC + 0x40: bytes([255]) * 4},
        kernel_setup=f'    mov eax, {hole | C.P.LEAF:#x}\n    mov [{C.PTE(C.XPAGE):#x}], rax\n    invlpg [{C.XPAGE:#x}]\n',
        fills={hole: (1, 0)})
    # MOVS reads its source in RAM. An unaligned destination straddles RAM and an APIC page.
    # Preloading the last RAM bytes independently makes any partial write observable after the refusal.
    first = C.XPAGE + 0xffe
    map_dest = (f'    mov eax, {C.HIGH | C.P.LEAF:#x}\n    mov [{C.PTE(C.XPAGE):#x}], rax\n'
                f'    mov eax, {0xfee00000 | C.P.LEAF:#x}\n    mov [{C.PTE(C.XPAGE + 0x1000):#x}], rax\n'
                f'    invlpg [{C.XPAGE:#x}]\n    invlpg [{C.XPAGE + 0x1000:#x}]\n')
    for name, di, reason in [('cross-page', first, 'crossing'), ('single-page', C.XPAGE + 0x1000, 'form')]:
        add('fs-string-device-store-' + name, 'fs rep movsd', prefix=regs(0, di, 2), refuse=reason,
            mapped=[{'addr': hex(C.HIGH + 0xffe), 'data': 'c1d2', 'capture': True}], kernel_setup=map_dest,
            captured={C.HIGH + 0xffe: bytes.fromhex('c1d2')})
    absent = f'    and qword [{C.PTE(C.XPAGE):#x}], -2\n    invlpg [{C.XPAGE:#x}]\n'
    add('fs-string-movs-source-first', 'fs movsd', prefix=regs(4, C.XPAGE, 1), kernel_setup=absent,
        ecx=1, captured=indices(4, C.XPAGE))
    add('gs-string-cmps-destination-first', 'gs cmpsd', segment='gs', prefix=regs(4, C.XPAGE, 1),
        fault=14, cr2=C.XPAGE, error=4, kernel_setup=absent, ecx=1, captured=indices(4, C.XPAGE))
    for op in ('rep movsb', 'repe cmpsw', 'rep lodsd'):
        add('fs-string-zero-' + op.replace(' ', '-'), 'fs ' + op, selector=0, success=True,
            prefix='    xor eax, eax\n    mov es, ax\n' + regs(7, dest, 0), ecx=0, captured=indices(7, dest))
    add('gs-string-null-es', 'gs movsw', segment='gs', prefix='    mov es, ax\n' + regs(0, dest, 3),
        ecx=3, captured=indices(0, dest) | {dest: bytes(2)})
    add('fs-generated-wrapped-load', 'mov ecx, [fs:edi]', base=0xfffffffe, mapped=C.WRAP, prefix=seed,
        success=True, ecx=0x44332211, kernel_setup=up, captured=have)
    for op, loaded in [('movzx', 0x00008022), ('movsx', 0xffff8022)]:
        add(f'fs-generated-wrapped-{op}-word', f'{op} ecx, word [fs:edi]', base=0xffffffff, limit=1,
            mapped=C.WRAP, prefix=seed, success=True, ecx=loaded,
            kernel_setup=init + '    xor ebx, ebx\n    mov byte [rbx], 0x80\n')
    add('gs-generated-wrapped-store', 'mov [gs:edi], ecx', base=0xfffffffe, segment='gs', mapped=C.WRAP,
        success=True, prefix=seed, kernel_setup=up, captured={high: bytes.fromhex('7856'), low: bytes.fromhex('3412')})
    # Only the second half is unassigned physical space; its two bytes read as FF.
    low_hole = [C.entry(C.TA + 0x3000, 0xa1000000 | C.P.LEAF) if n == 3 else mem for n, mem in enumerate(C.WRAP)]
    add('fs-generated-wrapped-load-unassigned-low', 'mov ecx, [fs:edi]', base=0xfffffffe, mapped=low_hole,
        prefix=seed, success=True, ecx=0xffff2211, kernel_setup=init, captured={high: bytes.fromhex('1122')})
    low_rom = [C.entry(C.TA + 0x3000, 0xfffff000 | C.P.LEAF) if n == 3 else mem for n, mem in enumerate(C.WRAP)]
    rom = bytes.fromhex('3344') + bytes(4094)
    add('gs-generated-wrapped-load-rom-low', 'mov ecx, [gs:edi]', base=0xfffffffe, segment='gs',
        mapped=low_rom, firmware=rom, prefix=seed, success=True, ecx=0x44332211, kernel_setup=init)
    add('fs-generated-wrapped-store-rom-low', 'mov [fs:edi], ecx', base=0xfffffffe, mapped=low_rom,
        firmware=rom, prefix=seed, success=True, kernel_setup=init, captured={high: bytes.fromhex('7856')})
    add('fs-generated-imm-wrapped-rom', 'mov dword [fs:edi], 0x89abcdef', base=0xfffffffe, mapped=low_rom,
        firmware=rom, success=True, kernel_setup=init, captured={high: bytes.fromhex('efcd')}, **imm)
    low_apic = [C.entry(C.TA + 0x3000, 0xfee00000 | C.P.LEAF) if n == 3 else mem for n, mem in enumerate(C.WRAP)]
    add('fs-generated-wrapped-claimed-read', 'mov ecx, [fs:edi]', base=0xfffffffe, mapped=low_apic,
        prefix=seed, refuse='crossing', kernel_setup=init, captured={high: bytes.fromhex('1122')})
    add('fs-generated-wrapped-claimed-store', 'mov [fs:edi], ecx', base=0xfffffffe, mapped=low_apic,
        prefix=seed, refuse='crossing', kernel_setup=init, captured={high: bytes.fromhex('1122')})
    add('fs-generated-wrapped-strict-unassigned', 'mov ecx, [fs:edi]', base=0xfffffffe, mapped=low_hole,
        prefix=seed, refuse='unassigned', strict=True, kernel_setup=init, captured={high: bytes.fromhex('1122')})
    for insn, write in [('mov ecx, [fs:edi]', False), ('mov [fs:edi], ecx', True)]:
        add('fs-generated-wrapped-missing-' + ('store' if write else 'load'), insn, base=0xfffffffe,
            mapped=C.TOP_ONLY, prefix=seed, ecx=0x12345678, fault=14, error=6 if write else 4, cr2=0,
            kernel_setup=init, captured={high: bytes.fromhex('1122')})
    watch_low = '    xor eax, eax\n    mov dr0, rax\n    mov eax, 0x10001\n    mov dr7, rax\n'
    # Distinct Q-register bytes span the final and first linear pages; the nonzero YMM
    # upper sentinel distinguishes a legacy commit, a VEX.128 commit and a pre-commit fault.
    head, tail = VALUE[:8], VALUE[8:]
    put_head = f'    mov ebx, 0xfffffff8\n    mov rax, {int.from_bytes(head, "little"):#x}\n    mov [rbx], rax\n'
    put_tail = f'    xor ebx, ebx\n    mov rax, {int.from_bytes(tail, "little"):#x}\n    mov [rbx], rax\n'
    quad = dict(base=0xfffffff8, limit=15)
    for op in ('movups', 'movupd', 'vmovups', 'vmovupd'):
        high_result = bytes(16) if op.startswith('v') else INDEX
        segment = 'gs' if op.endswith('d') else 'fs'
        insn = f'{op} xmm1, [{segment}:edi]'
        add(f'{segment}-packed-{op}-ordinary', insn, segment=segment, limit=15, success=True,
            xmm1=VALUE, ymm1_high=high_result)
        add(f'{segment}-packed-{op}-wrap', insn, segment=segment, **quad, mapped=C.WRAP,
            kernel_setup=put_head + put_tail, success=True, xmm1=VALUE, ymm1_high=high_result)
    add('gs-packed-vmovups-indexed', 'vmovups xmm1, [gs:edi+esi*4+8]', segment='gs', base=tls - 24, limit=39,
        prefix='    mov edi, 8\n    mov esi, 2\n', success=True, xmm1=VALUE, ymm1_high=bytes(16))
    add('fs-packed-movups-wrap-claimed', 'movups xmm1, [fs:edi]', **quad, mapped=low_apic, kernel_setup=put_head,
        refuse='crossing', captured={C.HIGH + 4088: head})
    add('gs-packed-vmovups-short', 'vmovups xmm1, [gs:edi]', segment='gs', base=C.XPAGE, limit=14,
        kernel_setup=f'    and qword [{C.PTE(C.XPAGE):#x}], -2\n    invlpg [{C.XPAGE:#x}]\n', ymm1_high=INDEX)
    add('fs-packed-movupd-wrap-pf', 'movupd xmm1, [fs:edi]', **quad, mapped=C.TOP_ONLY, kernel_setup=put_head,
        fault=14, error=4, cr2=0, ymm1_high=INDEX, captured={C.HIGH + 4088: head})
    add('gs-packed-vmovupd-wrap-watch', 'vmovupd xmm1, [gs:edi]', segment='gs', **quad, mapped=C.WRAP,
        kernel_setup=put_head + put_tail + watch_low.replace('0x10001', '0x30001'), fault=1, after_trap=True,
        xmm1=VALUE, ymm1_high=bytes(16))
    ac_on = '    mov rax, cr0\n    bts rax, 18\n    mov cr0, rax\n'
    add('fs-packed-movups-wrap-aligned8-ac', 'movups xmm1, [fs:edi]', **quad, mapped=C.WRAP, enter_flags=0x40002,
        kernel_setup=put_head + put_tail + ac_on, success=True, xmm1=VALUE, ymm1_high=INDEX)
    add('gs-packed-vmovups-wrap-misaligned-ac', 'vmovups xmm1, [gs:edi]', segment='gs', base=0xfffffff9, limit=15,
        mapped=C.TOP_ONLY, enter_flags=0x40002, kernel_setup=ac_on, fault=17, ymm1_high=INDEX)
    for name, mapping, expected_tail in [('rom', low_rom, rom[:8]), ('unassigned', low_hole, b'\xff' * 8)]:
        add(f'fs-packed-vmovups-wrap-{name}', 'vmovups xmm1, [fs:edi]', **quad, mapped=mapping, kernel_setup=put_head,
            firmware=rom if name == 'rom' else None, success=True, xmm1=head + expected_tail, ymm1_high=bytes(16))
    # The source's limit cannot mask #NM or #UD. Restore FPU access solely to save the frame.
    ts = ('    mov rax, cr0\n    bts rax, 3\n    mov cr0, rax\n', '    clts\n')
    osx = ('    mov rax, cr4\n    btr rax, 18\n    mov cr4, rax\n',
           '    mov rax, cr4\n    bts rax, 18\n    mov cr4, rax\n')
    add('fs-packed-movups-ts-before-limit', 'movups xmm1, [fs:edi]', fault=7, fp_controls=ts, ymm1_high=INDEX)
    add('gs-packed-vmovupd-disabled-before-limit', 'vmovupd xmm1, [gs:edi]', segment='gs', fault=6,
        fp_controls=osx, ymm1_high=INDEX)
    add('gs-generated-imm-wrapped-watch', 'mov dword [gs:edi], 0x89abcdef', base=0xfffffffe, segment='gs',
        mapped=C.WRAP, fault=1, after_trap=True, kernel_setup=up + watch_low,
        captured={high: bytes.fromhex('efcd'), low: bytes.fromhex('ab89')}, **imm)
    ro = up + f'    and qword [{C.TA + 0x3000:#x}], -3\n    invlpg [0]\n'
    exchange_seed = '    mov ecx, 0x12345678\n    cmp eax, eax\n    stc\n'
    add('fs-xchg-high-byte', 'xchg [fs:edi], ch', prefix=exchange_seed, success=True, ecx=0x12341178, rflags=0x45,
        memory=bytes.fromhex('56') + VALUE[1:])
    add('gs-xchg-word-index', 'lock xchg [gs:edi+esi*2], cx', segment='gs', prefix=exchange_seed + '    mov esi, 1\n',
        success=True, ecx=0x12344433, rflags=0x45, memory=VALUE[:2] + bytes.fromhex('7856') + VALUE[4:])
    add('gs-xchg-dword', 'xchg ecx, [gs:edi]', segment='gs', prefix=exchange_seed, success=True, ecx=0x44332211,
        rflags=0x45, memory=bytes.fromhex('78563412') + VALUE[4:])
    add('fs-xchg-register', 'db 0x64,0x87,0xc1', selector=0, prefix=exchange_seed, success=True,
        eax=0x12345678, ecx=0, rflags=0x45)
    add('fs-xchg-readonly', 'xchg [fs:edi], ecx', access=0xf1, prefix=exchange_seed, ecx=0x12345678, rflags=0x10045)
    add('gs-xchg-before-unmapped', 'lock xchg [gs:edi], ecx', segment='gs', base=C.XPAGE, limit=2,
        kernel_setup=f'    and qword [{C.PTE(C.XPAGE):#x}], -2\n    invlpg [{C.XPAGE:#x}]\n',
        prefix=exchange_seed, ecx=0x12345678, rflags=0x10045)
    exchange_chunks = {high: bytes.fromhex('7856'), low: bytes.fromhex('3412')}
    add('fs-xchg-wrapped-word', 'xchg [fs:edi], cx', mapped=C.WRAP, base=0xffffffff, kernel_setup=up,
        prefix=exchange_seed, success=True, ecx=0x12343322, rflags=0x45,
        captured={high: bytes.fromhex('1178'), low: bytes.fromhex('5644')})
    add('gs-xchg-wrapped-lock', 'lock xchg [gs:edi], ecx', segment='gs', mapped=C.WRAP, base=0xfffffffe, kernel_setup=up,
        prefix=exchange_seed, success=True, ecx=0x44332211, rflags=0x45, captured=exchange_chunks)
    add('fs-xchg-wrapped-watch', 'xchg [fs:edi], ecx', mapped=C.WRAP, base=0xfffffffe, kernel_setup=up + watch_low,
        prefix=exchange_seed, fault=1, after_trap=True, ecx=0x44332211, rflags=0x45, captured=exchange_chunks)
    add('gs-xchg-lower-pf', 'xchg [gs:edi], ecx', segment='gs', mapped=C.TOP_ONLY, base=0xfffffffe, kernel_setup=init,
        prefix=exchange_seed, fault=14, error=6, cr2=0, ecx=0x12345678, rflags=0x10045, captured={high: bytes.fromhex('1122')})
    add('gs-xchg-lower-readonly', 'lock xchg [gs:edi], ecx', segment='gs', mapped=C.WRAP, base=0xfffffffe,
        kernel_setup=ro, prefix=exchange_seed, fault=14, error=7, cr2=0, ecx=0x12345678, rflags=0x10045, captured=have)
    for backing, route in [('hole', low_hole), ('apic', low_apic), ('rom', low_rom)]:
        add('fs-xchg-wrapped-' + backing, 'xchg [fs:edi], ecx', base=0xfffffffe, mapped=route,
            prefix=exchange_seed, kernel_setup=init, refuse='atomic', firmware=rom if backing == 'rom' else None,
            captured={high: bytes.fromhex('1122')})
    # Non-LOCK ADD r/m,r. The dword pair sums to zero, setting CF, PF, AF and ZF.
    rmw_src = 0xbbccddef
    rmw_seed = f'    mov ecx, {rmw_src:#x}\n    cmp eax, eax\n    stc\n'
    add('fs-add-rmw-ordinary', 'add [fs:edi], ecx', prefix=rmw_seed, success=True,
        ecx=rmw_src, rflags=0x55, memory=bytes(4) + VALUE[4:])
    add('gs-add-rmw-high-byte', 'add [gs:edi], ch', segment='gs', prefix='    mov ecx, 0x7f00\n',
        success=True, ecx=0x7f00, rflags=0x894, memory=bytes([0x90]) + VALUE[1:])
    add('fs-add-rmw-readonly-segment', 'add [fs:edi], ecx', access=0xf1, prefix=rmw_seed, ecx=rmw_src, rflags=0x10045)
    add('gs-add-rmw-limit-before-walk', 'add [gs:edi], cx', segment='gs', base=C.XPAGE, limit=0, prefix=rmw_seed,
        kernel_setup=f'    and qword [{C.PTE(C.XPAGE):#x}], -2\n    invlpg [{C.XPAGE:#x}]\n', ecx=rmw_src, rflags=0x10045)
    add('fs-add-rmw-wrapped', 'add [fs:edi], ecx', base=0xfffffffe, mapped=C.WRAP, prefix=rmw_seed,
        kernel_setup=up, success=True, ecx=rmw_src, rflags=0x55, captured={high: bytes(2), low: bytes(2)})
    add('gs-add-rmw-lower-pf', 'add [gs:edi], ecx', base=0xfffffffe, segment='gs', mapped=C.TOP_ONLY,
        prefix=rmw_seed, kernel_setup=init, fault=14, error=6, cr2=0, ecx=rmw_src, rflags=0x10045,
        captured={high: bytes.fromhex('1122')})
    add('fs-add-rmw-lower-readonly', 'add [fs:edi], ecx', base=0xfffffffe, mapped=C.WRAP, prefix=rmw_seed,
        kernel_setup=ro, fault=14, error=7, cr2=0, ecx=rmw_src, rflags=0x10045, captured=have)
    add('gs-add-rmw-wrapped-watch', 'add [gs:edi], ecx', segment='gs', base=0xfffffffe, mapped=C.WRAP,
        prefix=rmw_seed, kernel_setup=up + watch_low, fault=1, after_trap=True, ecx=rmw_src, rflags=0x55,
        captured={high: bytes(2), low: bytes(2)})
    add('gs-add-rmw-word-retry', 'add [gs:edi+esi*2], cx', segment='gs', limit=1, reload_limit=3,
        prefix=rmw_seed + '    mov esi, 1\n', ecx=rmw_src, success=True,
        memory=VALUE[:2] + bytes.fromhex('2222') + VALUE[4:], retry_regs={C.REC + 0x48: struct.pack('<I', rmw_src)})
    # The zero-page half aliases the code that is currently running. Set its A/D bits clear and
    # check both the final PTE and the modified first byte of the now-invalidated instruction page.
    alias = list(C.WRAP)
    alias[3] = dict(C.entry(C.TA + 0x3000, C.U | C.P.LEAF & ~(C.P.A | C.P.D)), capture=True)
    add('fs-add-rmw-wrapped-code-page', 'add [fs:edi], ecx', base=0xfffffffe, mapped=alias,
        prefix='    mov ecx, 0x10000\n', kernel_setup=init, ecx=0x10000, success=True, rflags=4,
        captured={high: bytes.fromhex('1122'), C.U: bytes.fromhex('b92b'),
                  C.TA + 0x3000: struct.pack('<Q', C.U | C.P.LEAF)})
    for backing, route in [('hole', low_hole), ('apic', low_apic), ('rom', low_rom)]:
        add('gs-add-rmw-wrapped-' + backing, 'add [gs:edi], ecx', segment='gs', base=0xfffffffe, mapped=route,
            prefix=rmw_seed, kernel_setup=init, refuse='form', firmware=rom if backing == 'rom' else None,
            captured={high: bytes.fromhex('1122')})
    add('gs-generated-wrapped-store-watch-low', 'mov [gs:edi], ecx', base=0xfffffffe, segment='gs', mapped=C.WRAP,
        prefix=seed, fault=1, after_trap=True, kernel_setup=up + watch_low,
        captured={high: bytes.fromhex('7856'), low: bytes.fromhex('3412')})
    add('fs-resident-wrapped-word', 'mov [fs:0], ds', base=0xffffffff, mapped=C.WRAP, success=True,
        kernel_setup=up, captured={high: bytes.fromhex('111b'), low: bytes.fromhex('0044')})
    add('gs-resident-wrapped-word-missing', 'mov [gs:0], ds', segment='gs', base=0xffffffff, mapped=C.TOP_ONLY,
        kernel_setup=init, fault=14, error=6, cr2=0, captured={high: bytes.fromhex('1122')})
    add('fs-fxsave-wrapped-image', 'fxsave [fs:0]', base=0xffffff80, limit=511, mapped=C.WRAP,
        success=True, captured={low + 48: INITIAL})
    add('fs-xsave-wrapped-component', 'xsave [fs:0]', base=0xffffffc0, limit=1023, mapped=C.WRAP, success=True,
        prefix='    mov eax, 2\n    xor edx, edx\n', captured={low + 112: INITIAL})
    # The SDM's byte-to-word forms (66 B6/BE) preserve ECX[31:16]. B7/BF use dword
    # destinations here. Both source widths end exactly at offset 9 in the passing cases.
    extend_forms = (
        ('movzx', 'ecx', 'byte', 0x000000aa),
        ('movsx', 'ecx', 'byte', 0xffffffaa),
        ('movzx', 'cx', 'byte', 0x123400aa),
        ('movsx', 'cx', 'byte', 0x1234ffaa),
        ('movzx', 'ecx', 'word', 0x0000aa99),
        ('movsx', 'ecx', 'word', 0xffffaa99),
    )
    for segment in ('fs', 'gs'):
        for op, dst, src, value in extend_forms:
            name = f'{segment}-generated-extend-{op}-{dst}-{src}'
            disp = 5 if src == 'byte' else 4
            prefix = seed + '    mov edi, 4\n    cmp al, al\n    stc\n'
            add(f'{name}-ok', f'{op} {dst}, {src} [{segment}:edi+{disp}]', limit=9, segment=segment,
                prefix=prefix, success=True, ecx=value, rflags=0x45)
            # A rejected span points to an absent guest page so #GP must precede any page walk.
            add(f'{name}-limit', f'{op} {dst}, {src} [{segment}:edi+{disp + 1}]', limit=9, segment=segment,
                base=C.XPAGE, prefix=prefix, ecx=0x12345678, rflags=0x10045,
                kernel_setup=f'    and qword [{C.PTE(C.XPAGE):#x}], -2\n    invlpg [{C.XPAGE:#x}]\n')
    # Raw A0..A3 keep the accumulator-only encoding independent of NASM's ModRM choices.
    def moffs(segment, width, write, offset, address16=False):
        opcode = (0xa2 if write else 0xa0) + (width != 1)
        form = (b'\x67' if address16 else b'') + (b'\x66' if width == 2 else b'')
        form += bytes([0x64 if segment == 'fs' else 0x65, opcode]) + offset.to_bytes(2 if address16 else 4, 'little')
        return 'db ' + ','.join(hex(b) for b in form)

    accumulator = 0x12345678
    seed_ax = '    mov eax, 0x12345678\n    cmp al, al\n    stc\n'
    for segment in ('fs', 'gs'):
        for width in (1, 2, 4):
            at = 10 - width  # last byte exactly at limit 9
            mask = (1 << (width * 8)) - 1
            loaded = (accumulator & ~mask) | int.from_bytes(VALUE[at:at + width], 'little')
            stored = VALUE[:at] + accumulator.to_bytes(4, 'little')[:width] + VALUE[at + width:]
            for write in (False, True):
                name = f'{segment}-moffs-{width}-{"write" if write else "read"}'
                add(name + '-ok', moffs(segment, width, write, at), segment=segment, limit=9, prefix=seed_ax,
                    success=True, eax=accumulator if write else loaded, memory=stored if write else VALUE, rflags=0x45)
                add(name + '-limit', moffs(segment, width, write, at + 1), segment=segment, limit=9,
                    prefix=seed_ax, eax=accumulator, rflags=0x10045)
    add('gs-moffs-readonly-store', moffs('gs', 1, True, 3), segment='gs', access=0xf1,
        prefix=seed_ax, eax=accumulator, rflags=0x10045)
    add('fs-moffs-address16-refusal', moffs('fs', 2, False, 1, address16=True), fault=6,
        prefix=seed_ax, eax=accumulator)
    # Both halves of an absolute offset participate before the segment base wraps to RAM.
    add('gs-moffs-high-offset', moffs('gs', 4, False, 0x87654321), segment='gs',
        base=(tls - 0x87654321) & 0xffffffff, limit=0xfffff, flags=12, success=True,
        prefix=seed_ax, eax=0x44332211, rflags=0x45)
    for write in (False, True):
        seg = 'gs' if write else 'fs'
        name = f'{seg}-moffs-wrapped-{"write" if write else "read"}'
        insn = moffs(seg, 4, write, 3)
        add(name, insn, segment=seg, base=0xfffffffb, limit=6, mapped=C.WRAP,
            success=True, prefix=seed_ax, kernel_setup=up, rflags=0x45,
            eax=accumulator if write else 0x44332211, captured={high: bytes.fromhex('7856'), low: bytes.fromhex('3412')} if write else have)
        add(name + '-missing', insn, segment=seg, base=0xfffffffb, limit=6, mapped=C.TOP_ONLY,
            prefix=seed_ax, eax=accumulator, fault=14, error=6 if write else 4, cr2=0, rflags=0x10045,
            kernel_setup=init, captured={high: bytes.fromhex('1122')})
    # Both active dwords fit GS. One lies on each side of the linear wrap. A missing second page
    # leaves lane 0 untouched. A shorter segment faults before paging; an empty AVX mask addresses no lane.
    pair = 'vpmaskmovd [gs:eax], xmm3, xmm1'
    lane0 = '    mov ebx, 0xfffffffc\n    mov dword [rbx], 0x44332211\n'
    unchanged = {C.HIGH + 4092: VALUE[:4]}
    def mask_pair(name, mapped=C.TOP_ONLY, captured=unchanged, kernel_setup=lane0, **kwargs):
        add('gs-maskstore-wrapped-' + name, pair, segment='gs', base=0xfffffffc, mapped=mapped,
            captured=captured, kernel_setup=kernel_setup, **kwargs)

    mask_pair('gp', limit=3)
    mask_pair('page', limit=7, fault=14, error=6, cr2=0)
    mask_pair('empty', limit=0, prefix=set0, xmm3=bytes(16), success=True)
    written = {C.HIGH + 4092: INITIAL[:4], low: INITIAL[4:8] + bytes(4)}
    mask_pair('pair', C.WRAP, written, limit=7, success=True)
    mask_pair('watch-low', C.WRAP, written, lane0 + watch_low, limit=7, fault=1, after_trap=True)
    # Standard XSAVE merges XSTATE_BV, writes only selected components, and leaves reserved header bytes alone.
    flags = '    mov eax, 2\n    xor edx, edx\n'
    add('fs-xsave-wrapped-header', 'xsave [fs:0]', base=0xffffffc0, limit=1023, mapped=C.WRAP,
        kernel_setup=load_bytes(448, bytes.fromhex('0100000000000000aabbccdd')),
        prefix=flags, success=True, captured={low + 112: INITIAL, low + 448: bytes.fromhex('0300000000000000aabbccdd')})
    add('fs-xsave-wrapped-page', 'xsave [fs:0]', base=0xffffffc0, limit=1023, mapped=C.TOP_ONLY,
        kernel_setup='    mov ebx, 0xffffffd8\n    mov dword [rbx], 0x44332211\n',
        prefix=flags, fault=14, error=6, cr2=0, captured={C.HIGH + 0xfd8: VALUE[:4]})
    # Unsigned arithmetic here computes the x86 low-width result and defined status bits
    # independently of the native ALU. XOR leaves AF undefined, and the program masks it out.
    alu_initial = 0x80000080
    alu_seed = f'    mov ecx, {alu_initial:#x}\n    cmp al, al\n    stc\n'

    def alu_answer(op, width, src, carry=0, lhs=None):
        sign = 1 << (8 * width - 1)
        mask = 2 * sign - 1
        lhs = (alu_initial if lhs is None else lhs) & mask
        src &= mask
        flags = 0
        if op in ('add', 'adc'):
            wide = lhs + src + (carry if op == 'adc' else 0)
            value = wide & mask
            flags = int(wide > mask) | ((lhs ^ src ^ value) & 16) | int(bool(~(lhs ^ src) & (lhs ^ value) & sign)) << 11
        elif op in ('sub', 'sbb', 'cmp'):
            wide = lhs - src - (carry if op == 'sbb' else 0)
            value = wide & mask
            flags = int(wide < 0) | ((lhs ^ src ^ value) & 16) | int(bool((lhs ^ src) & (lhs ^ value) & sign)) << 11
        else:
            value = {'xor': lhs ^ src, 'or': lhs | src, 'and': lhs & src, 'test': lhs & src}[op]
        flags |= int((value & 255).bit_count() % 2 == 0) << 2 | int(value == 0) << 6 | int(bool(value & sign)) << 7
        return (alu_initial if op in ('cmp', 'test') else (alu_initial & ~mask) | value), flags

    # The wrapped write captures the same source register and incoming CF as the normal ALU.
    # Mask AF in the interrupt handler only for completed logic operations, where AF is undefined.
    for op in ('or', 'adc', 'sbb', 'and', 'sub', 'xor'):
        source = 0xbbccddee if op == 'adc' else 0x44332211
        logical = op in ('or', 'and', 'xor')
        for carry in ((0, 1) if op in ('adc', 'sbb') else (1,)):
            seed_rmw = f'    mov ecx, {source:#x}\n    cmp eax, eax\n' + ('    stc\n' if carry else '')
            options = dict(prefix=seed_rmw, ecx=source, flags_mask=0x10ec5 if logical else 0x10ed5)
            for width, reg in [(1, 'ch'), (2, 'cx'), (4, 'ecx')]:
                nbits = (1 << (8 * width)) - 1
                rhs = source >> 8 if reg == 'ch' else source
                result, flags = alu_answer(op, width, rhs, carry, lhs=int.from_bytes(VALUE[:width], 'little'))
                written = (result & nbits).to_bytes(width, 'little')
                add(f'fs-{op}-rmw-{reg}-carry{carry}', f'{op} [fs:edi], {reg}', success=True,
                    rflags=flags, memory=written + VALUE[width:], **options)
                if width == 1:
                    continue
                lhs = 0x3322 if width == 2 else 0x44332211
                result, flags = alu_answer(op, width, rhs, carry, lhs=lhs)
                written = (result & nbits).to_bytes(width, 'little')
                chunks = {high: b'\x11' + written[:1], low: written[1:] + b'\x44'} if width == 2 else {high: written[:2], low: written[2:]}
                add(f'gs-{op}-rmw-wrap-{reg}-carry{carry}', f'{op} [gs:edi], {reg}', segment='gs', mapped=C.WRAP,
                    base=0xffffffff if width == 2 else 0xfffffffe, kernel_setup=up, success=True, rflags=flags, captured=chunks, **options)
                if width == 4:
                    add(f'gs-{op}-rmw-watch-carry{carry}', f'{op} [gs:edi], ecx', segment='gs', mapped=C.WRAP,
                        base=0xfffffffe, fault=1, after_trap=True, kernel_setup=up + watch_low,
                        rflags=flags, captured=chunks, **options)
            # A failed write walk preserves all flags, including AF, and the complete source register.
            add(f'fs-{op}-rmw-missing-carry{carry}', f'{op} [fs:edi], ecx', mapped=C.TOP_ONLY, base=0xfffffffe,
                prefix=seed_rmw, ecx=source, kernel_setup=init, fault=14, error=6, cr2=0,
                rflags=0x10044 | carry, captured={high: b'\x11\x22'})
    add('fs-sub-rmw-readonly-limit', 'sub [fs:edi], ecx', access=0xf1, prefix=rmw_seed, ecx=rmw_src, rflags=0x10045)
    add('gs-sbb-rmw-wrapped-apic', 'sbb [gs:edi], ecx', segment='gs', base=0xfffffffe, mapped=low_apic,
        prefix=rmw_seed, kernel_setup=init, refuse='form', captured={high: bytes.fromhex('1122')})
    def cas_seed(expected, replacement=0x12345678):
        return f'    mov eax, {expected:#x}\n    mov ecx, {replacement:#x}\n    cmp edi, edi\n    stc\n'

    for width, reg in [(1, 'ch'), (2, 'cx'), (4, 'ecx')]:
        mask = (1 << (8 * width)) - 1
        source = (0x56 if width == 1 else 0x12345678) & mask
        for matches in (False, True):
            for wraps in ((False,) if width == 1 else (False, True)):
                memory = 0x3322 if wraps and width == 2 else int.from_bytes(VALUE[:width], 'little')
                expected = ((0x99880000 & ~mask) | memory) if matches else 0x80000000
                accumulator = (expected & ~mask) | memory
                written = (source if matches else memory).to_bytes(width, 'little')
                flags = alu_answer('sub', width, memory, lhs=expected)[1]
                name = f'gs-cmpxchg-{reg}-' + ('match' if matches else 'miss')
                args = dict(segment='gs', prefix=cas_seed(expected), success=True, eax=accumulator, ecx=0x12345678, rflags=flags)
                insn = f'lock cmpxchg [gs:edi], {reg}'
                if wraps:
                    chunks = {high: b'\x11' + written[:1], low: written[1:] + b'\x44'} if width == 2 else {high: written[:2], low: written[2:]}
                    add(name + '-wrap', insn, base=0xffffffff if width == 2 else 0xfffffffe, mapped=C.WRAP, kernel_setup=up,
                        captured=chunks, **args)
                    if width == 4:
                        args.update(success=False, after_trap=True, fault=1)
                        add(name + '-watch', insn, base=0xfffffffe, mapped=C.WRAP, kernel_setup=up + watch_low, captured=chunks, **args)
                else:
                    add(name, insn, memory=written + VALUE[width:], **args)
    add('fs-cmpxchg-ah-source', 'lock cmpxchg [fs:edi], ah', prefix=cas_seed(0xaa553311),
        eax=0xaa553311, ecx=0x12345678, success=True, rflags=0x44, memory=b'\x33' + VALUE[1:])
    add('fs-cmpxchg-ax-source-alias', 'lock cmpxchg [fs:edi], ax', base=0xffffffff, mapped=C.WRAP, kernel_setup=up,
        prefix=cas_seed(0x99883322), success=True, eax=0x99883322, ecx=0x12345678, rflags=0x44, captured=have)
    add('fs-cmpxchg-readonly-miss', 'lock cmpxchg [fs:edi], ecx', access=0xf1, prefix=cas_seed(0), eax=0, ecx=0x12345678, rflags=0x10045)
    for name, expected, mapping, setup, error, chunks in [
            ('lower-pf-match', 0x44332211, C.TOP_ONLY, init, 6, {high: b'\x11\x22'}),
            ('lower-readonly-miss', 0, C.WRAP, ro, 7, have)]:
        add('fs-cmpxchg-' + name, 'lock cmpxchg [fs:edi], ecx', base=0xfffffffe, mapped=mapping, kernel_setup=setup,
            prefix=cas_seed(expected), fault=14, error=error, cr2=0, eax=expected, ecx=0x12345678, rflags=0x10045, captured=chunks)
    add('gs-cmpxchg-code-page', 'lock cmpxchg [gs:edi], ecx', segment='gs', base=0xfffffffe, mapped=alias,
        kernel_setup=init, prefix=cas_seed(0x2bb82211, 0x2bb92211), success=True, eax=0x2bb82211, ecx=0x2bb92211, rflags=0x44,
        captured={high: bytes.fromhex('1122'), C.U: bytes.fromhex('b92b'), C.TA + 0x3000: struct.pack('<Q', C.U | C.P.LEAF)})
    add('gs-cmpxchg-wrapped-apic', 'lock cmpxchg [gs:edi], ecx', segment='gs', base=0xfffffffe, mapped=low_apic,
        prefix=cas_seed(0), kernel_setup=init, refuse='atomic', captured={high: bytes.fromhex('1122')})
    for seg in ('fs', 'gs'):
        for op in ('cmp', 'xor', 'sub'):
            for width, reg in ((1, 'cl'), (2, 'cx'), (4, 'ecx')):
                at = 4 - width
                name = f'{seg}-alu-{op}-{reg}'
                result, flags = alu_answer(op, width, int.from_bytes(VALUE[at:4], 'little'))
                add(name + '-ok', f'{op} {reg}, [{seg}:edi+{at}]', segment=seg, prefix=alu_seed,
                    success=True, ecx=result, rflags=flags, undefined_af=op == 'xor')
                add(name + '-limit', f'{op} {reg}, [{seg}:edi+{at + 1}]', segment=seg, base=C.XPAGE, prefix=alu_seed,
                    ecx=alu_initial, rflags=0x10045,
                    kernel_setup=f'    and qword [{C.PTE(C.XPAGE):#x}], -2\n    invlpg [{C.XPAGE:#x}]\n')
            for width, reg, base, src in ((2, 'cx', 0xffffffff, 0x3322), (4, 'ecx', 0xfffffffe, 0x44332211)):
                result, flags = alu_answer(op, width, src)
                name = f'{seg}-alu-{op}-{reg}-wrap'
                add(name, f'{op} {reg}, [{seg}:edi]', segment=seg, base=base, mapped=C.WRAP, access=0xf1,
                    prefix=alu_seed, success=True, ecx=result, rflags=flags, undefined_af=op == 'xor', kernel_setup=up, captured=have)
                add(name + '-missing', f'{op} {reg}, [{seg}:edi]', segment=seg, base=base, mapped=C.TOP_ONLY,
                    prefix=alu_seed, ecx=alu_initial, rflags=0x10045, fault=14, error=4, cr2=0, kernel_setup=init,
                    captured={high: bytes.fromhex('1122')})
    wrapped_sub, wrapped_sub_flags = alu_answer('sub', 4, 0x44332211)
    add('gs-alu-wrap-rom', 'sub ecx, [gs:edi]', segment='gs', base=0xfffffffe, mapped=low_rom, firmware=rom,
        prefix=alu_seed, kernel_setup=init, success=True, ecx=wrapped_sub, rflags=wrapped_sub_flags)
    # Both incoming carry states influence ADC/SBB; the result and the saved pre-fault
    # flags are checked separately. A read-only descriptor suffices for all these forms.
    for op, width, reg in [('add', 2, 'cx'), ('or', 1, 'cl'), ('and', 4, 'ecx'), ('adc', 2, 'cx'), ('sbb', 4, 'ecx')]:
        for carry in ((0, 1) if op in ('adc', 'sbb') else (1,)):
            prefix = alu_seed + ('    clc\n' if not carry else '')
            logical = op in ('or', 'and')
            prep = f'fs-alu-{op}-{reg}-carry{carry}'
            result, flags = alu_answer(op, width, int.from_bytes(VALUE[:width], 'little'), carry)
            add(prep + '-read', f'{op} {reg}, [fs:edi]', access=0xf1, limit=width - 1, prefix=prefix,
                success=True, ecx=result, rflags=flags, undefined_af=logical)
            add(prep + '-deny', f'{op} {reg}, [fs:edi+1]', access=0xf1, limit=width - 1,
                prefix=prefix, ecx=alu_initial, rflags=0x10044 | carry)
            if width != 1:
                word = width == 2
                src = 0x3322 if word else 0x44332211
                result, flags = alu_answer(op, width, src, carry)
                add(f'gs-alu-{op}-{reg}-wrap-carry{carry}', f'{op} {reg}, [gs:edi]', segment='gs', access=0xf1,
                    base=0xffffffff if word else 0xfffffffe, mapped=C.WRAP, kernel_setup=up, prefix=prefix, success=True,
                    ecx=result, rflags=flags, undefined_af=logical, captured=have)
                add(f'gs-alu-{op}-{reg}-pf-carry{carry}', f'{op} {reg}, [gs:edi]', segment='gs', access=0xf1,
                    base=0xffffffff if word else 0xfffffffe, mapped=C.TOP_ONLY, kernel_setup=init, prefix=prefix,
                    fault=14, error=4, cr2=0, ecx=alu_initial, rflags=0x10044 | carry, captured={high: bytes.fromhex('1122')})
    # CMP/TEST with memory first leave their non-memory operand and memory unchanged.
    # Encode the immediate-group forms explicitly, including sign extension for 83 /7.
    for seg, op, width, encoded, rhs in [
        ('fs', 'cmp', 1, '380f', alu_initial), ('gs', 'cmp', 2, '390f', alu_initial),
        ('fs', 'test', 4, '850f', alu_initial), ('gs', 'test', 1, '840f', alu_initial),
        ('fs', 'cmp', 1, '803f80', 0x80), ('gs', 'cmp', 2, '813f0080', 0x8000),
        ('fs', 'cmp', 4, '813f00000080', 0x80000000), ('gs', 'cmp', 4, '833fff', -1),
        ('fs', 'cmp', 2, '833fff', -1), ('gs', 'test', 1, 'f60780', 0x80),
        ('fs', 'test', 2, 'f707ff00', 0xff), ('gs', 'test', 4, 'f70700000080', 0x80000000),
    ]:
        prefix = ('66' if width == 2 else '') + ('64' if seg == 'fs' else '65')
        insn = 'db ' + ','.join(hex(b) for b in bytes.fromhex(prefix + encoded))
        name = f'{seg}-alu-{op}-{encoded}-{width}'
        flags = alu_answer(op, width, rhs, lhs=int.from_bytes(VALUE[:width], 'little'))[1]
        add(name + '-read', insn, segment=seg, limit=width - 1, access=0xf1, prefix=alu_seed,
            success=True, ecx=alu_initial, rflags=flags, undefined_af=op == 'test')
        add(name + '-deny', insn, segment=seg, limit=width - 1, access=0xf1, prefix='    mov edi, 1\n' + alu_seed,
            ecx=alu_initial, rflags=0x10045)
        if width != 1:
            word = width == 2
            flags = alu_answer(op, width, rhs, lhs=0x3322 if word else 0x44332211)[1]
            add(name + '-wrap', insn, segment=seg, access=0xf1, base=0xffffffff if word else 0xfffffffe,
                mapped=C.WRAP, prefix=alu_seed, success=True, ecx=alu_initial, rflags=flags,
                undefined_af=op == 'test', kernel_setup=up, captured=have)
    # Common i386 TLS spelling: ModRM 00/101 takes the 32-bit literal without a base register.
    add('gs-alu-xor-absolute-tls', 'xor ecx, [gs:0x14]', base=tls - 0x14, segment='gs', limit=0x17, prefix=alu_seed,
        success=True, ecx=alu_answer('xor', 4, 0x44332211)[0], rflags=alu_answer('xor', 4, 0x44332211)[1], undefined_af=True)
    add('fs-alu-cmp-absolute-limit', 'cmp ecx, [fs:0x14]', base=tls - 0x14, limit=0x16,
        prefix=alu_seed, ecx=alu_initial, rflags=0x10045)
    add('fs-alu-sub-indexed-pf', 'sub ecx, [fs:edi+esi*4+8]', base=C.XPAGE, limit=19,
        prefix=alu_seed + '    mov esi, 2\n', ecx=alu_initial, rflags=0x10045, fault=14, error=4, cr2=C.XPAGE + 16,
        kernel_setup=f'    and qword [{C.PTE(C.XPAGE):#x}], -2\n    invlpg [{C.XPAGE:#x}]\n')
    # Read/write DR0 on the low half traps after native subtraction and before the next instruction.
    add('fs-alu-wrap-watch-low', 'sub ecx, [fs:edi]', base=0xfffffffe, mapped=C.WRAP, prefix=alu_seed,
        kernel_setup=up + watch_low.replace('0x10001', '0x30001'), fault=1, after_trap=True,
        ecx=wrapped_sub, rflags=wrapped_sub_flags, captured=have)
    # CMOVcc fetches its source with either condition outcome. ZF, CF, PF are set.
    for op, take, seg, width, reg in [('cmove', True, 'fs', 4, 'ecx'), ('cmovne', False, 'gs', 4, 'ecx'),
                                      ('cmovp', True, 'gs', 2, 'cx'), ('cmovnp', False, 'fs', 2, 'cx')]:
        word = width == 2
        value = (alu_initial & 0xffff0000 | 0x2211) if word else 0x44332211
        wrapped = (alu_initial & 0xffff0000 | 0x3322) if word else 0x44332211
        name = f'{seg}-conditional-{op}-{reg}'
        insn = f'{op} {reg}, [{seg}:edi]'
        add(name + '-ok', insn, segment=seg, access=0xf1, prefix=alu_seed, success=True, ecx=value if take else alu_initial, rflags=0x45)
        add(name + '-limit', insn, segment=seg, limit=width - 2, base=C.XPAGE, prefix=alu_seed, ecx=alu_initial, rflags=0x10045)
        add(name + '-wrap', insn, segment=seg, access=0xf1, base=0xffffffff if word else 0xfffffffe, mapped=C.WRAP,
            prefix=alu_seed, success=True, ecx=wrapped if take else alu_initial, rflags=0x45, kernel_setup=up, captured=have)
        add(name + '-pf', insn, segment=seg, base=0xffffffff if word else 0xfffffffe, mapped=C.TOP_ONLY, prefix=alu_seed,
            fault=14, error=4, cr2=0, ecx=alu_initial, rflags=0x10045, kernel_setup=init)
    # Both retries of the old hidden descriptor fault. Reloading it makes the very same
    # CMOV commit according to the original ZF, for both the taken and untaken conditions.
    for op, value in [('cmove', 0x55443322), ('cmovne', 0x12345678)]:
        add('gs-conditional-retry-' + op, f'{op} ecx, [gs:edi+1]', segment='gs', prefix=seed + '    cmp al, al\n    stc\n',
            success=True, ecx=value, reload_limit=7)
    add('gs-conditional-false-watch-low', 'cmovne ecx, [gs:edi]', segment='gs', base=0xfffffffe, mapped=C.WRAP,
        prefix=alu_seed, kernel_setup=up + watch_low.replace('0x10001', '0x30001'), fault=1, after_trap=True,
        ecx=alu_initial, rflags=0x45, captured=have)
    # Scan/count results at zero differ by instruction; only architecturally defined flags are checked.
    for op in ('bsf', 'bsr', 'popcnt', 'tzcnt', 'lzcnt'):
        for width, reg, seg in [(2, 'cx', 'gs'), (4, 'ecx', 'fs')]:
            bits = width * 8
            insn = f'{op} {reg}, [{seg}:edi]'
            source = 0x3322 if width == 2 else 0x44332211
            mask = 0x40 if op in ('bsf', 'bsr') else 0x8d5 if op == 'popcnt' else 0x41
            for zero in (False, True):
                value = 0 if zero else source
                first = (value & -value).bit_length() - 1 if value else bits
                answer = {'bsf': first, 'tzcnt': first, 'bsr': value.bit_length() - 1, 'lzcnt': bits - value.bit_length(),
                          'popcnt': value.bit_count()}[op]
                flags = (0x40 * int(zero)) if op in ('bsf', 'bsr', 'popcnt') else int(zero) | (int(answer == 0) << 6)
                result = (alu_initial & 0xffff0000) | answer if width == 2 else answer
                add(f'{seg}-{op}-{reg}-wrap-{int(zero)}', insn, segment=seg, base=0xffffffff if width == 2 else 0xfffffffe,
                    mapped=C.WRAP, prefix=alu_seed, success=True, ecx=None if zero and op in ('bsf', 'bsr') else result,
                    flags_mask=mask, rflags=flags, kernel_setup='' if zero else up)
            add(f'{seg}-{op}-{reg}-limit', insn, segment=seg, limit=width - 2, prefix=alu_seed, ecx=alu_initial, rflags=0x10045)
    # Native multiply follows the successful load; CF and OF report signed truncation together.
    for name, insn, width, multiplier in [('two', 'imul cx, [fs:edi]', 2, alu_initial & 0xffff),
                                           ('imm8', 'imul ecx, [fs:edi], -3', 4, -3),
                                           ('fullimm', 'imul cx, [fs:edi], 0x1234', 2, 0x1234)]:
        source = 0x3322 if width == 2 else 0x44332211
        bits = 8 * width
        product = source * multiplier
        value = product & ((1 << bits) - 1)
        result = (alu_initial & 0xffff0000 | value) if width == 2 else value
        overflow = not -(1 << (bits-1)) <= product < (1 << (bits-1))
        add('fs-imul-' + name + '-wrap', insn, base=0xffffffff if width == 2 else 0xfffffffe, mapped=C.WRAP,
            kernel_setup=up, prefix=alu_seed, success=True, ecx=result, flags_mask=0x801, rflags=0x801 if overflow else 0)
        add('fs-imul-' + name + '-limit', insn, limit=width - 2, prefix=alu_seed, ecx=alu_initial, rflags=0x10045)
    def crc32c(crc, data):
        for byte in data:
            crc ^= byte
            for _ in range(8):
                crc = (crc >> 1) ^ (0x82f63b78 if crc & 1 else 0)
        return crc

    for op, width, reg, size in [('crc32', 1, 'ecx', 'byte'), ('crc32', 2, 'ecx', 'word'), ('crc32', 4, 'ecx', 'dword'),
                                  ('movbe', 2, 'cx', 'word'), ('movbe', 4, 'ecx', 'dword')]:
        insn = f'{op} {reg}, {size} [gs:edi]'
        name = f'gs-{op}-{size}'
        for wrap in ([False] if width == 1 else [False, True]):
            data = bytes.fromhex('2233') if wrap and width == 2 else VALUE[:width]
            result = crc32c(alu_initial, data) if op == 'crc32' else int.from_bytes(data, 'big')
            if op == 'movbe' and width == 2:
                result |= alu_initial & 0xffff0000
            add(name + ('-wrap' if wrap else '-read'), insn, segment='gs', access=0xf1,
                base=(0xffffffff if width == 2 else 0xfffffffe) if wrap else tls, mapped=C.WRAP if wrap else (),
                prefix=alu_seed, success=True, ecx=result, rflags=0x45, kernel_setup=up if wrap else '')
        add(name + '-limit', insn, segment='gs', limit=width-1, prefix='    mov edi, 1\n' + alu_seed, ecx=alu_initial, rflags=0x10045)
    # The same dword supplies data, a mask or a multiplier depending on the VEX form.
    bmi_memory = 0x84332210
    bmi_data = 0x5a3c12ed
    multiplier = 0x10203041
    product = multiplier * bmi_memory
    set_bits = [bit for bit in range(32) if bmi_memory >> bit & 1]
    extracted = sum((bmi_data >> bit & 1) << rank for rank, bit in enumerate(set_bits))
    deposited = sum((bmi_data >> rank & 1) << bit for rank, bit in enumerate(set_bits))
    count = 5
    word_mask = 0xffffffff
    bmi_cases = [
        # mnemonic, operands, ESI, ECX result, flags, defined flags mask
        ('andn', 'ecx, esi, MEM', bmi_data, ~bmi_data & bmi_memory, 0x80, 0x8c1),
        ('blsr', 'ecx, MEM', bmi_data, bmi_memory & (bmi_memory - 1), 0x80, 0x8c1),
        ('blsmsk', 'ecx, MEM', bmi_data, bmi_memory ^ (bmi_memory - 1), 0, 0x8c1),
        ('blsi', 'ecx, MEM', bmi_data, bmi_memory & -bmi_memory, 1, 0x8c1),
        ('bextr', 'ecx, MEM, esi', 0x0804, bmi_memory >> 4 & 255, 0, 0x841),
        ('bzhi', 'ecx, MEM, esi', 13, bmi_memory & ((1 << 13) - 1), 0, 0x8c1),
        ('pext', 'ecx, esi, MEM', bmi_data, extracted, 0x45, 0xed5),
        ('pdep', 'ecx, esi, MEM', bmi_data, deposited, 0x45, 0xed5),
        ('mulx', 'ecx, ebx, MEM', bmi_data, product >> 32, 0x45, 0xed5),
        ('shlx', 'ecx, MEM, esi', count, bmi_memory << count & word_mask, 0x45, 0xed5),
        ('sarx', 'ecx, MEM, esi', count, (bmi_memory - (1 << 32)) >> count & word_mask, 0x45, 0xed5),
        ('shrx', 'ecx, MEM, esi', count, bmi_memory >> count, 0x45, 0xed5),
        ('rorx', 'ecx, MEM, 7', bmi_data, (bmi_memory >> 7 | bmi_memory << 25) & word_mask, 0x45, 0xed5),
    ]
    bmi_bytes = struct.pack('<I', bmi_memory)
    bmi_top = f'    mov ebx, 0xfffffffe\n    mov word [rbx], {bmi_memory & 0xffff}\n'
    bmi_wrap = bmi_top + f'    xor ebx, ebx\n    mov word [rbx], {bmi_memory >> 16}\n'
    bmi_normal = C.W + 0xa0
    ebx_seed = 0xa5a5aba5

    for index, (op, operands, esi, result, status, mask) in enumerate(bmi_cases):
        seg = 'gs' if index % 2 else 'fs'
        insn = op + ' ' + operands.replace('MEM', f'dword [{seg}:edi]')
        start = (f'    mov ecx, {alu_initial}\n    mov ebx, {ebx_seed}\n    mov esi, {esi}\n'
                 f'    mov edx, {multiplier}\n    cmp edi, edi\n    stc\n')
        registers = {C.REC + 0x50: struct.pack('<I', multiplier), C.REC + 0x70: struct.pack('<I', esi),
                     C.REC + 0x58: struct.pack('<I', ebx_seed)}
        finished = dict(registers)
        if op == 'mulx':
            finished[C.REC + 0x58] = struct.pack('<I', product & word_mask)
        addr = {'base': bmi_normal, 'kernel_setup': load_bytes(bmi_normal, bmi_bytes)}
        # Exercise each opcode, alternate address routes, and run both routes for distinct operand roles.
        routes = (False, True) if op in ('blsi', 'pext', 'mulx', 'rorx') else (bool(index % 2),)
        for wrap in routes:
            loc = dict(base=0xfffffffe, mapped=C.WRAP, kernel_setup=bmi_wrap) if wrap else addr
            captured = finished | ({high: bmi_bytes[:2], low: bmi_bytes[2:]} if wrap else {bmi_normal: bmi_bytes})
            add(f'{seg}-bmi-{op}-' + ('wrap' if wrap else 'read'), insn, segment=seg, access=0xf1, prefix=start,
                success=True, ecx=result, flags_mask=mask, rflags=status, captured=captured, **loc)
        if op in ('blsi', 'pdep', 'bextr', 'mulx', 'rorx'):
            add(f'{seg}-bmi-{op}-limit', insn, segment=seg, base=C.XPAGE, limit=2, kernel_setup=absent,
                prefix=start, ecx=alu_initial, rflags=0x10045, captured=registers)
            add(f'{seg}-bmi-{op}-second-pf', insn, segment=seg, base=0xfffffffe, mapped=C.TOP_ONLY,
                kernel_setup=bmi_top, prefix=start, fault=14, error=4, cr2=0, ecx=alu_initial, rflags=0x10045,
                captured=registers | {high: bmi_bytes[:2]})
        if op == 'mulx':
            add('gs-bmi-mulx-alias-wrapped', 'mulx ecx, ecx, dword [gs:edi]', segment='gs',
                base=0xfffffffe, mapped=C.WRAP, kernel_setup=bmi_wrap, prefix=start, success=True,
                ecx=product >> 32, rflags=0x45, captured=registers)
    # SETcc writes a byte for both condition outcomes. ModRM.reg is ignored even
    # when nonzero. The condition comes from NZCV or PF and all flags survive.
    store_regs = '    mov ecx, 0x12345678\n    cmp edi, edi\n    stc\n'
    for cc, opcode, val in [('e', 0x94, 1), ('ne', 0x95, 0), ('p', 0x9a, 1), ('np', 0x9b, 0)]:
        seg = 'gs' if val else 'fs'
        insn = f'db {0x65 if val else 0x64},0x0f,{opcode},0x2f'
        add(f'{seg}-set{cc}-write', insn, segment=seg, prefix=store_regs, success=True, rflags=0x45,
            ecx=0x12345678, memory=bytes([val]) + VALUE[1:])
        add(f'{seg}-set{cc}-readonly', insn, segment=seg, access=0xf1, prefix=store_regs, rflags=0x10045, ecx=0x12345678)
    add('gs-setne-false-write-page-fault', 'setne [gs:edi]', segment='gs', base=C.XPAGE, kernel_setup=absent,
        prefix=store_regs, fault=14, error=6, cr2=C.XPAGE, rflags=0x10045, ecx=0x12345678)
    for opname, insn, data, seg in [('movbe-word', 'movbe [fs:edi], cx', bytes.fromhex('5678'), 'fs'),
                                     ('movbe-dword', 'movbe [gs:edi], ecx', bytes.fromhex('12345678'), 'gs'),
                                     ('movnti', 'movnti [fs:edi], ecx', bytes.fromhex('78563412'), 'fs')]:
        width = len(data)
        base = (1 << 32) - width // 2
        old = {high: b'\x11' + data[:1], low: data[1:] + b'\x44'} if width == 2 else {high: data[:2], low: data[2:]}
        prefix = dict(segment=seg, prefix=store_regs, ecx=0x12345678)
        add(f'{seg}-{opname}-ordinary', insn, success=True, rflags=0x45, memory=data + VALUE[width:], **prefix)
        add(f'{seg}-{opname}-wrap', insn, base=base, limit=width-1, mapped=C.WRAP, kernel_setup=up,
            success=True, rflags=0x45, captured=old, **prefix)
        add(f'{seg}-{opname}-before-page', insn, base=C.XPAGE, limit=width-2, kernel_setup=absent, rflags=0x10045, **prefix)
        add(f'{seg}-{opname}-second-pf', insn, base=base, mapped=C.TOP_ONLY, kernel_setup=init, fault=14,
            error=6, cr2=0, rflags=0x10045, captured={high: have[high]}, **prefix)
        add(f'{seg}-{opname}-wrap-rom', insn, base=base, mapped=low_rom, firmware=rom, kernel_setup=init,
            success=True, rflags=0x45, captured={high: old[high]}, **prefix)

    def xlat_regs(ebx, al):
        return f'    cmp al, al\n    stc\n    mov ebx, {ebx:#x}\n    mov eax, {0xaabbcc00 | al:#x}\n'

    xlat_absent = f'    and qword [{C.PTE(C.XPAGE):#x}], -2\n    invlpg [{C.XPAGE:#x}]\n'
    add('ds-xlat-ok', 'xlatb', prefix=xlat_regs(tls, 1), success=True, eax=0xaabbcc22, rflags=0x45)
    for seg in ('fs', 'gs'):
        insn = f'{seg} xlatb'
        add(f'{seg}-xlat-ok', insn, segment=seg, prefix=xlat_regs(2, 1),
            success=True, eax=0xaabbcc44, rflags=0x45)
        add(f'{seg}-xlat-limit', insn, segment=seg, base=C.XPAGE, prefix=xlat_regs(3, 1),
            kernel_setup=xlat_absent, eax=0xaabbcc01, rflags=0x10045)
        add(f'{seg}-xlat-page-fault', insn, segment=seg, base=C.XPAGE, prefix=xlat_regs(2, 1),
            kernel_setup=xlat_absent, fault=14, error=4, cr2=C.XPAGE + 3, eax=0xaabbcc01, rflags=0x10045)
        add(f'{seg}-xlat-unsigned-al', insn, segment=seg, base=tls - 0x80, limit=0x83,
            prefix=xlat_regs(2, 0x81), success=True, eax=0xaabbcc44, rflags=0x45)
        add(f'{seg}-xlat-offset-wrap', insn, segment=seg, prefix=xlat_regs(0xffffff80, 0x83),
            success=True, eax=0xaabbcc44, rflags=0x45)
        # This high segment base and in-range offset sum to the original low test buffer.
        add(f'{seg}-xlat-base-wrap', insn, segment=seg, base=(tls - 0x400000) & 0xffffffff, limit=0x400, flags=12,
            prefix=xlat_regs(0x400002, 1), success=True, eax=0xaabbcc44, rflags=0x45)
        add(f'{seg}-xlat-null', insn, segment=seg, selector=0, prefix=xlat_regs(2, 1), eax=0xaabbcc01, rflags=0x10045)
    add('fs-xlat-address16-refusal', 'a16 fs xlatb', prefix=xlat_regs(2, 1),
        fault=6, eax=0xaabbcc01, rflags=0x10045)
    add('gs-xlat-retry-after-reload', 'gs xlatb', segment='gs', prefix=xlat_regs(3, 1),
        eax=0xaabbcc55, success=True, reload_limit=4, retry_regs={C.REC + 0x40: struct.pack('<I', 0xaabbcc01)})
    # Single memory source, implicit AX or DX:AX. Seed the unused high halves independently;
    # signed division truncates toward zero and its remainder has the dividend's sign.
    def muldiv(name, op, width, src, a, d, *, wrapped=False, missing=False, denied=False, overflow=False, watch=False):
        bits = width * 8
        mask = (1 << bits) - 1
        seg = 'gs' if op in ('imul', 'idiv') else 'fs'
        raw = (src & mask).to_bytes(width, 'little')
        split = width // 2
        base = (1 << 32) - split if wrapped else C.W + 0xa0
        size = {1: 'byte', 2: 'word', 4: 'dword'}[width]
        pos = int(denied)
        prefix = f'    mov eax, {a:#x}\n    mov edx, {d:#x}\n    mov edi, {pos}\n    cmp edi, edi\n    stc\n'
        mapped = (C.TOP_ONLY if missing else C.WRAP) if wrapped else ()
        mem = {}
        setup = load_bytes(base, raw.ljust(4, b'\0')) if not wrapped else ''
        if wrapped:
            half = 'byte' if split == 1 else 'word'
            setup = f'    mov ebx, {base:#x}\n    mov {half} [rbx], {int.from_bytes(raw[:split], "little"):#x}\n'
            mem[high + 2 - split] = raw[:split]
            if not missing:
                setup += f'    xor ebx, ebx\n    mov {half} [rbx], {int.from_bytes(raw[split:], "little"):#x}\n'
                mem[low] = raw[split:]
        if watch:
            setup += watch_low.replace('0x10001', '0x30001')
        signed = op in ('imul', 'idiv')
        def sx(value, n):
            return value - (1 << n) if signed and value & (1 << (n - 1)) else value

        fault = 13 if denied else 14 if missing else 0 if op in ('div', 'idiv') and (not src or overflow) else None
        flags = 0x10045
        flags_mask = 0x10ed5
        if fault is None:
            operand = sx(src & mask, bits)
            if op in ('mul', 'imul'):
                product = sx(a & mask, bits) * operand
                lo_part, hi_part = product & mask, (product >> bits) & mask
                flags_mask = 0x801
                flags = 0x801 if product != sx(lo_part, bits) else 0
            else:
                dividend = (a & 0xffff) if width == 1 else ((d & mask) << bits) | (a & mask)
                dividend = sx(dividend, 2 * bits)
                q = abs(dividend) // abs(operand) * (-1 if (dividend < 0) != (operand < 0) else 1)
                lo_part, hi_part = q & mask, (dividend - q * operand) & mask
                flags = None  # successful DIV/IDIV leave arithmetic flags undefined
            if width == 1:
                a = (a & ~0xffff) | (hi_part << 8) | lo_part
            else:
                a, d = (a & ~mask) | lo_part, (d & ~mask) | hi_part
        mem[C.REC + 0x50] = struct.pack('<I', d)
        add(name, f'{op} {size} [{seg}:edi]', segment=seg, base=base, limit=width - 1, access=0xf1, mapped=mapped,
            prefix=prefix, kernel_setup=setup, success=fault is None, fault=fault, error=4 if missing and not denied else 0,
            cr2=0 if missing and not denied else CR2, eax=a, captured=mem, rflags=flags, flags_mask=flags_mask)

    for width in (1, 2, 4):
        bits = width * 8
        mask = (1 << bits) - 1
        hi_seed = 0x76543210
        for op in ('mul', 'imul', 'div', 'idiv'):
            name = f'implicit-{op}-{bits}'
            if op in ('mul', 'imul'):
                src, a, d = mask, 0xffffff83, hi_seed
            elif op == 'idiv':
                src = 3
                a = 0xabcdffef if width == 1 else 0xffffffef
                d = hi_seed if width == 1 else 0x7654ffff if width == 2 else 0xffffffff
            else:
                src = 3
                a = 0xabcd0111 if width == 1 else 0xabcd0011 if width == 2 else 17
                d = hi_seed if width == 1 else 0x76540001 if width == 2 else 1
            muldiv(name + '-read', op, width, src, a, d)
            muldiv(name + '-limit', op, width, 0, a, d, denied=True)
            if width > 1:
                muldiv(name + '-wrap', op, width, src, a, d, wrapped=True)
                muldiv(name + '-wrap-pf', op, width, 0, a, d, wrapped=True, missing=True)
            if op in ('div', 'idiv'):
                for wrap in ((False, True) if width > 1 else (False,)):
                    suffix = '-wrapped' if wrap else '-ordinary'
                    muldiv(name + '-zero' + suffix, op, width, 0, a, d, wrapped=wrap)
                    # A quotient just outside the representable width, with a nonzero divisor.
                    if op == 'div':
                        oa, od, divisor = (0xabcd0100 if width == 1 else 0), (hi_seed if width == 1 else 1), 1
                    else:
                        oa = 0xabcdff80 if width == 1 else 1 << (bits - 1)
                        od, divisor = (hi_seed if width == 1 else mask), mask
                    muldiv(name + '-overflow' + suffix, op, width, divisor, oa, od, wrapped=wrap, overflow=True)
    for width in (1, 2, 4):
        for wrap in ((False, True) if width > 1 else (False,)):
            # +17 / -3 gives -5 remainder +2. Replacing the signed source by its unsigned
            # low bits often leaves the zero/overflow checks happy but changes the quotient.
            a = 0xabcd0011 if width < 4 else 17
            d = 0x76543210 if width == 1 else 0x76540000 if width == 2 else 0
            muldiv(f'implicit-idiv-{8*width}-negative-divisor-{"wrap" if wrap else "read"}', 'idiv', width, -3, a, d, wrapped=wrap)
    muldiv('implicit-mul-word-no-overflow', 'mul', 2, 3, 0xabcd0002, 0x12345678)
    muldiv('implicit-imul-dword-signed-overflow', 'imul', 4, -1, 0x80000000, 0x12345678, wrapped=True)
    # A memory read matching DR0 still faults for a zero divisor and owes no post-instruction #DB.
    muldiv('implicit-idiv-de-before-data-trap', 'idiv', 4, 0, 8, 0, wrapped=True, watch=True)
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
    cases += [mixed_user_wrap(), mixed_user_wrap_far(), identity_switch(False), identity_switch(True)]
    # M2 checkpoint C, the generic operand check: one in-range and one out-of-range case per instruction class the
    # per-opcode checks left unchecked, under a writable expand-up segment. In range the inline check passes (the
    # resident decides none: checks=0) and the effect is the instruction's; out of range the fault is at the instruction
    # with memory, registers and flags as before it (the resident decided one).
    dword = int.from_bytes(VALUE[1:5], 'little')

    def at1(value):
        return VALUE[:1] + (value & 0xffffffff).to_bytes(4, 'little') + VALUE[5:]

    regs = '    mov eax, 0x10\n    mov ecx, 0x01020304\n    cmp al, al\n    stc\n'
    big = 2**53 + 1   # not a double: only the FILD m64, FISTP m64 pair's kept integer stores it exactly
    fild = (f'    fninit\n    mov dword [{C.W + 0x50:#x}], {big & 0xffffffff:#x}\n    mov dword [{C.W + 0x54:#x}], {big >> 32:#x}\n'
            f'    fild qword [{C.W + 0x50:#x}]\n')
    for name, insn, result, extra in [
        ('inc', 'inc dword [fs:edi+1]', at1(dword + 1), {}),
        ('add-imm', 'add dword [fs:edi+1], 5', at1(dword + 5), {}),
        ('not', 'not dword [fs:edi+1]', at1(~dword), {}),
        ('neg', 'neg dword [fs:edi+1]', at1(-dword), {}),
        ('shl', 'shl dword [fs:edi+1], 1', at1(dword << 1), {}),
        ('lock-add', 'lock add [fs:edi+1], eax', at1(dword + 0x10), {}),
        ('xadd', 'xadd [fs:edi+1], eax', at1(dword + 0x10), {}),
        ('lock-cmpxchg-miss', 'lock cmpxchg [fs:edi+1], ecx', VALUE, {}),
        # Bit 35 from offset 0: the unit is the dword at 4.
        ('bts-reg', 'bts [fs:edi], eax', VALUE[:4] + bytes([VALUE[4] | 8]) + VALUE[5:], dict(last=7, pre='    mov eax, 0x23\n', eax=0x23)),
        ('push', 'push dword [fs:edi+1]', VALUE, {}),
        ('pop', 'pop dword [fs:edi+1]', at1(0x01020304), dict(pre='    push ecx\n', esp=C.US - 4)),
        # POP computes its destination with the ESP it leaves.
        ('pop-esp-base', f'pop dword [fs:esp-{C.US - 1:#x}]', at1(0x01020304), dict(pre='    push ecx\n', esp=C.US - 4)),
        ('movss-store', 'movss [fs:edi+1], xmm1', VALUE[:1] + INITIAL[:4] + VALUE[5:], {}),
        ('movd-load', 'movd xmm0, [fs:edi+1]', VALUE, {}),
        ('paddd-mmx', 'paddd mm0, [fs:edi]', VALUE, dict(last=7)),
        ('movups-load', 'movups xmm0, [fs:edi]', VALUE, dict(last=15)),
        ('fld', 'fld dword [fs:edi+1]', VALUE, {}),
        ('fstp', 'fstp qword [fs:edi]', bytes(8) + VALUE[8:], dict(last=7, pre='    fninit\n    fldz\n')),
        ('fild-fistp', 'fistp qword [fs:edi]', big.to_bytes(8, 'little') + VALUE[8:], dict(last=7, pre=fild)),
    ]:
        last, prefix = extra.get('last', 4), regs + extra.get('pre', '')
        # (The x87 integer pair is formed in ordinary blocks only: alone in a block, FISTP converts the reduced-precision value.)
        add(f'fs-generic-{name}', insn, limit=last, success=True, prefix=prefix, memory=result, checks=0, whole_blocks=name == 'fild-fistp')
        frame = {C.REC + 0x28: struct.pack('<Q', extra['esp'])} if 'esp' in extra else None
        add(f'fs-generic-{name}-over', insn, limit=last - 1, prefix=prefix, rflags=0x10045, eax=extra.get('eax', 0x10),
            ecx=0x01020304, captured=frame, checks=1)
    # The FISTP of the pair faults at its operand check twice (the table is edited, then FS reloaded) and is retried:
    # the integer the FILD read is stored exactly.
    add('fs-generic-fild-fistp-retried', 'fistp qword [fs:edi]', limit=6, reload_limit=7, success=True, prefix=regs + fild,
        memory=big.to_bytes(8, 'little') + VALUE[8:], retry_regs={C.REC + 0x48: struct.pack('<I', 0x01020304)}, whole_blocks=True)
    # Near indirect CALL and JMP read their target through the segment before anything else.
    for name, insn in [('call', 'call [fs:edi+1]'), ('jmp', 'jmp [fs:edi+1]')]:
        add(f'fs-generic-{name}-over', insn, prefix=regs, rflags=0x10045, eax=0x10, ecx=0x01020304, checks=1)
    # BT with a register index: the unit's offset is the effective address plus the signed displacement.
    add('fs-generic-bt-negative-index', 'bt [fs:edi+4], eax', limit=7, success=True, prefix='    mov eax, -1\n', checks=0, rflags=0x44)
    add('fs-generic-bt-negative-index-over', 'bt [fs:edi], eax', limit=7, prefix='    mov eax, -1\n', checks=1)
    add('fs-generic-bt-positive-index-over', 'bt [fs:edi], eax', limit=7, prefix='    mov eax, 64\n', checks=1)
    # Every addressing form reaches the check with its offset: the last in-range and the first out-of-range one.
    forms = [('disp32', '[fs:{o:#x}]', ''), ('base', '[fs:ebx]', '    mov ebx, {o:#x}\n'),
             ('base-index', '[fs:ebx+esi*4]', '    mov ebx, {o:#x} - 8\n    mov esi, 2\n'),
             ('esp', '[fs:esp]', '    mov esp, {o:#x}\n'), ('ebp', '[fs:ebp+0]', '    mov ebp, {o:#x}\n'),
             ('wrapping-disp', '[fs:ebx-0x1000]', '    mov ebx, {o:#x} + 0x1000\n'),
             ('wrapping-sum', '[fs:ebx+0x7fffffff]', '    mov ebx, {o:#x} - 0x7fffffff\n')]
    for name, operand, setup in forms:
        for offset, ok in [(4, True), (5, False)]:
            add(f'fs-generic-address-{name}-{"in" if ok else "over"}', f'mov ecx, {operand.format(o=offset)}', limit=7,
                prefix=setup.format(o=offset), success=ok, ecx=int.from_bytes(VALUE[4:8], 'little') if ok else None, checks=int(not ok))
    add('fs-generic-address-moffs-in', 'mov eax, [fs:4]', limit=7, success=True, eax=int.from_bytes(VALUE[4:8], 'little'), checks=0)
    add('fs-generic-address-moffs-over', 'mov eax, [fs:5]', limit=7, checks=1)
    add('fs-generic-address-xlat-in', 'fs xlatb', limit=7, success=True, prefix='    mov ebx, 4\n    mov eax, 0xffffff03\n',
        eax=0xffffff00 | VALUE[7], checks=0)
    add('fs-generic-address-xlat-over', 'fs xlatb', limit=7, prefix='    mov ebx, 4\n    mov eax, 0xffffff04\n', eax=0xffffff04, checks=1)
    # Segments the record does not model are the resident's on every access: read-only (reads pass, writes fault),
    # expand-down, and a one-byte read at offset 0 under a segment whose record is 0.
    add('fs-generic-readonly-read', 'add ecx, [fs:edi]', access=0xf1, success=True, prefix='    xor ecx, ecx\n',
        ecx=int.from_bytes(VALUE[:4], 'little'), checks=1)
    add('fs-generic-readonly-write', 'inc dword [fs:edi]', access=0xf1, checks=1)
    add('gs-generic-expand-down-read', 'add ecx, [gs:edi+5]', limit=4, access=0xf7, flags=0, segment='gs', success=True,
        prefix='    xor ecx, ecx\n', ecx=int.from_bytes(VALUE[5:9], 'little'), checks=1)
    add('gs-generic-expand-down-write-low', 'inc dword [gs:edi+4]', limit=4, access=0xf7, flags=0, segment='gs', checks=1)
    add('fs-generic-readonly-byte-zero', 'mov cl, [fs:0]', access=0xf1, limit=0, success=True, prefix='    xor ecx, ecx\n',
        ecx=VALUE[0], checks=1)
    add('fs-generic-readonly-byte-zero-write', 'mov [fs:0], cl', access=0xf1, limit=0, checks=1)
    # The same forms through a flat-limit segment with a base (Linux's TLS shape): no check in the code, none decided.
    add('fs-generic-flat-limit-inc', 'inc dword [fs:edi+1]', limit=0xfffff, flags=12, success=True, memory=at1(dword + 1), checks=0)
    add('gs-generic-flat-limit-movss', 'movss [gs:edi+1], xmm1', limit=0xfffff, flags=12, segment='gs', success=True,
        memory=VALUE[:1] + INITIAL[:4] + VALUE[5:], checks=0)
    # An operand whose bytes wrap from FFFF_FFFFH to linear 0, in range of its segment: completed at the native access,
    # under a limited segment (the inline check passes: none decided) and under a flat-limit one (no check at all).
    wrap = dict(base=0xfffffffe, mapped=C.WRAP, kernel_setup=up, success=True)
    for bits, limit, flags in [('limited', 3, 4), ('flat-limit', 0xfffff, 12)]:
        add(f'fs-generic-wrapped-load-{bits}', 'add ecx, [fs:edi]', limit=limit, flags=flags, prefix='    xor ecx, ecx\n',
            ecx=0x44332211, checks=0, **wrap)
        add(f'fs-generic-wrapped-inc-{bits}', 'inc dword [fs:edi]', limit=limit, flags=flags, checks=0,
            captured={high: bytes.fromhex('1222'), low: bytes.fromhex('3344')}, **wrap)
        add(f'fs-generic-wrapped-lock-add-{bits}', 'lock add [fs:edi], eax', limit=limit, flags=flags, prefix='    mov eax, 0x01010101\n',
            checks=0, captured={high: bytes.fromhex('1223'), low: bytes.fromhex('3445')}, **wrap)
        add(f'fs-generic-wrapped-movss-load-{bits}', 'movss xmm1, [fs:edi]', limit=limit, flags=flags, checks=0,
            xmm1=bytes.fromhex('11223344') + bytes(12), **wrap)
        add(f'fs-generic-wrapped-movss-store-{bits}', 'movss [fs:edi], xmm1', limit=limit, flags=flags, checks=0,
            captured={high: INITIAL[:2], low: INITIAL[2:4]}, **wrap)
        add(f'fs-generic-wrapped-fild-{bits}', 'fild word [fs:edi+1]', limit=limit, flags=flags, checks=0, **wrap)
    # A flat segment's access across 4 GiB is still its limit fault, and the stack's is #SS whatever the operand's segment.
    add('fs-generic-flat-base-zero-straddle', 'mov ecx, [fs:0xfffffffe]', base=0, limit=0xfffff, flags=12, mapped=C.WRAP,
        kernel_setup=up, checks=0)
    add('fs-generic-push-stack-straddle', 'push dword [fs:edi]', fault=12, mapped=C.WRAP, kernel_setup=up,
        prefix='    mov esp, 2\n', checks=0)
    add('fs-generic-call-stack-straddle', 'call [fs:edi]', fault=12, mapped=C.WRAP, kernel_setup=up,
        prefix='    mov esp, 2\n', checks=0)
    # Recorded deviation (docs/restructure/m2-checkpoint-c-plan.md, "Linear straddles"): under a flat-limit segment
    # nothing tests the offset, so an operand that runs past offset FFFF_FFFFH is not the #GP x86 gives: the bytes
    # before the base are read (as main and QEMU do). The base is at least 32, so the access does not also wrap linearly.
    add('fs-generic-flat-limit-offset-overrun-deviation', 'mov ecx, [fs:0xfffffffe]', base=tls + 2, limit=0xfffff, flags=12,
        success=True, ecx=int.from_bytes(VALUE[:4], 'little'), checks=0)

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


def mixed_user_wrap_far():
    """Displacements of 4 KiB and more that wrap past 4 GiB, with the wrong page above 4 GiB warm and mapped.

    64-bit code writes sentinels at 1_0000_1000H (a page other than linear 1000H's), which maps that page's user shadow
    writable, then enters compatibility code. Added in 64 bits, EBX = FFFF_F000H plus 2000H would reach the sentinels
    without a fault, where 32-bit address arithmetic reaches linear 1000H."""
    high = 1 << 32 | 0x1000
    memory = C.WRAP + [C.entry(C.TA + 0x1000 + 4 * 8, C.HI_PD | C.P.P | C.P.RW | C.P.US | C.P.A),
                       C.entry(C.HI_PD, C.HI_PT | C.P.P | C.P.RW | C.P.US | C.P.A),
                       C.entry(C.HI_PT + 8, C.XPAGE | C.P.LEAF), C.P.zero(C.XPAGE, 12)]
    sentinels = (0x5eed0000, 0x5eed0004, 0x5eed0008)
    user = f"""bits 64
    mov rax, {high:#x}
    mov dword [rax], {sentinels[0]:#x}
    mov dword [rax+4], {sentinels[1]:#x}
    mov dword [rax+8], {sentinels[2]:#x}
    mov dword [0x1000], 0x10ad0000
    mov dword [0x1008], 0x10ad0008
    push 0x1b
    push {C.US:#x}
    push 2
    push 0xdb
    mov eax, .in32
    push rax
    iretq
bits 32
.in32:
    mov ebx, 0xfffff000
    mov eax, [ebx+0x2000]
    mov ecx, 0x5704e000
    mov [ebx+0x2004], ecx
    mov esi, 2
    mov edx, [ebx+esi*4+0x2000]
    mov esp, {C.US:#x}
    int 0x80
"""
    prog = C.program('mixed-user-wrap-far', C.ENTER(cs=0x23) + C.handlers(), user, memory)
    return prog, {C.LOG: struct.pack('<Q', 1), C.REC: struct.pack('<QQ', 128, 0),
                  C.REC + 0x18: struct.pack('<Q', 0xdb), C.REC + 0x40: struct.pack('<Q', 0x10ad0000),
                  C.REC + 0x50: struct.pack('<Q', 0x10ad0008),
                  C.LOW1: struct.pack('<3I', 0x10ad0000, 0x5704e000, 0x10ad0008),
                  C.XPAGE: struct.pack('<3I', *sentinels)}

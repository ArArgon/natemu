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
            kernel_setup='', enter_flags=2, fault=13, ecx=None, eax=None, reload_limit=None, retry_regs=None, rflags=None, captured=None, error=0, cr2=CR2, after_trap=False, firmware=None, refuse=None, strict=False, fills=None, undefined_af=False, flags_mask=0x10ed5):
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
        user += f'    {insn}\n'
        trap_rip = C.U + len(C.user32(user)) if after_trap else fault_rip
        if undefined_af:
            user += '    pushfd\n    and dword [esp], -17\n    popfd\n'
        user += '    int 0x80\n'
        rip = C.U + len(C.user32(user)) if success else trap_rip
        saved = f'    mov rax, cr2\n    mov [{C.W + 0x40:#x}], rax\n    jmp .fin\n'
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
        enter = C.ENTER(cs=0xdb if cpl3_mode == 32 else 0x23).replace('    push 2\n', f'    push {enter_flags}\n')
        prog = C.program(name, C.gate(12, '.v12') + (C.gate(1, '.v1') if fault == 1 else '') + setup + kernel_setup + enter
                         + C.handlers(saved, xmm=True, vectors=(1, 6, 12, 13, 14, 128), flags_mask=flags_mask), user, memory=mapped)
        # Handler offsets are from compat.handlers, the target memory and CR2 are independent of the recorded frame.
        expected = {C.LOG: struct.pack('<Q', 1), C.REC: struct.pack('<QQQQ', 128 if success else fault, error, rip,
                                                                     0xdb if cpl3_mode == 32 else 0x23),
                    C.REC + 0x110: xmm1, C.REC + 0x130: xmm3,
                    C.W + 0x40: struct.pack('<Q', cr2), tls: memory}
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
    add('fs-string-repe-fault-flags', 'fs repe cmpsb', limit=0, prefix=regs(0, tls, 3) + '    stc\n',
        ecx=2, rflags=0x10045, captured=indices(1, tls + 1))
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
    add('gs-generated-imm-wrapped-watch', 'mov dword [gs:edi], 0x89abcdef', base=0xfffffffe, segment='gs',
        mapped=C.WRAP, fault=1, after_trap=True, kernel_setup=up + watch_low,
        captured={high: bytes.fromhex('efcd'), low: bytes.fromhex('ab89')}, **imm)
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
    ro = up + f'    and qword [{C.TA + 0x3000:#x}], -3\n    invlpg [0]\n'
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

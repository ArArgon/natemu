"""S2b programs: descriptor tables, segment registers and far transfers in 64-bit mode (natemu-smp-v1, `system`, CPL 0
under the per-CPU tables of programs.sys_text). The reference for a run is pinned QEMU system mode (registers, captured
memory, a fault's vector, error code, RIP and CR2); every fault and refusal is also judged by tools/models/segment_model.py
from the case's own state (`params.model`: the expected outcome, computed here). QEMU's stub needs, in any GDT a body
loads, a 64-bit DPL 0 code segment at 0x08 with the accessed bit set (its IDT gates name 0x08) and its GS base (restored
before the end); its fault handler runs on the loaded TSS's RSP0 for a CPL 3 fault."""
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent.parent))
import programs as P
from models import segment_model as sm, delivery_model as dm

# Per CPU i, offsets from programs.SYS_D(i): GDT (writable), its copy on the read-only page R, LDT, TSS, pointers (P,
# not captured), results (C, captured), user code and stack (user pages: every data page is P|RW|US|A|D).
GDT, RO, LDT, TSS, PTR, CAP, UCODE, USTACK = 0x8000, P.R, 0xd000, 0xc000, 0xb000, 0xe000, 0x9000, 0xa800
LIMIT = 0xcf


def gdt_image(i):
    d = P.SYS_D(i)
    e = {0x08: sm.code(), 0x10: sm.data(), 0x18: sm.data(3), 0x20: sm.code(3), 0x28: sm.data(present=False),
         0x30: sm.code(readable=False), 0x38: sm.code(l=0, d=1), 0x40: sm.data(writable=False), 0x48: sm.data(accessed=False),
         0x50: sm.data(base=0x1234_5678 + i), 0x58: sm.code(3, conforming=True), 0xc0: sm.code(present=False),
         0xc8: sm.data(present=False)}
    image = bytearray(LIMIT + 1)
    for sel, raw in e.items():
        image[sel:sel + 8] = raw.to_bytes(8, 'little')
    for sel, raw in {0x60: sm.system_descriptor(d + LDT, 0x17, 2), 0x70: sm.system_descriptor(d + TSS, 0x67, 9),
                     0x80: sm.system_descriptor(d + TSS, 0x67, 0xB), 0x90: sm.system_descriptor(d + TSS, 0x67, 9, present=False),
                     0xa0: sm.system_descriptor(d + TSS, 0x67, 9, upper_type=1), 0xb0: dm.gate(0x1000, 0x08, gtype=0xC)}.items():
        image[sel:sel + 16] = raw
    return bytes(image)


def ldt_image():
    return bytes(8) + sm.data(accessed=False, base=0x4242).to_bytes(8, 'little') + bytes(8)


def tss_image(i):
    return (0).to_bytes(4, 'little') + (P.STACK(i) - 0x400).to_bytes(8, 'little') + bytes(0x68 - 12)


def pointers(i, ro_gdt=False):
    """At P: the GDTR to load, an IDTR value, a GDTR with a non-canonical base, the read-only copy's GDTR (16 bytes apart)."""
    d = P.SYS_D(i)
    return (LIMIT.to_bytes(2, 'little') + (d + (RO if ro_gdt else GDT)).to_bytes(8, 'little')
            + bytes(6) + (0xfff).to_bytes(2, 'little') + (0xffff_8000_0010_0000 + 0x1000 * i).to_bytes(8, 'little')
            + bytes(6) + LIMIT.to_bytes(2, 'little') + (0x0000_8000_0000_0000).to_bytes(8, 'little')
            + bytes(6) + LIMIT.to_bytes(2, 'little') + (d + RO).to_bytes(8, 'little'))


def memory(i, n, ro_gdt=False, gdt=None):
    d = P.SYS_D(i)
    a = {d + P.R: (d + P.R) | P.P | P.US | P.A}
    pointers_ = pointers(i, ro_gdt)
    gdt = gdt or gdt_image(i)
    return P.sys_tables(i, 0, n, a) + [
        {'addr': f'0x{d + GDT:x}', 'data': gdt.hex()}, {'addr': f'0x{d + RO:x}', 'data': gdt.hex()},
        {'addr': f'0x{d + LDT:x}', 'data': ldt_image().hex()}, {'addr': f'0x{d + TSS:x}', 'data': tss_image(i).hex()},
        dict({'addr': f'0x{d + PTR:x}', 'data': pointers_.hex()}, capture=False), P.zero(d + PTR + 0x80, 0x80) | {'capture': False},
        P.zero(d + CAP, 0x100), dict(P.zero(d + USTACK - 0x800, 0x800), capture=False), dict(P.zero(d + P.SAVE, 16), capture=False)]


# Format fields of a body (programs.sys_text adds d, save, ...): g = GDT, p = pointers (+0 GDTR, +0x10 an IDTR value,
# +0x20 a non-canonical GDTR, +0x80 the start-up IDTR, +0x90 a far pointer), c = results, u = user code, us = user stack.
def fields(i):
    d = P.SYS_D(i)
    return dict(g=d + GDT, p=d + PTR, c=d + CAP, u=d + UCODE, us=d + USTACK, save=d + P.SAVE)


SAVE_GS = '    mov ecx, 0xc0000101\n    rdmsr\n    mov [{save:#x}+8], eax\n    mov [{save:#x}+12], edx\n'
RESTORE_GS = '    mov ecx, 0xc0000101\n    mov eax, [{save:#x}+8]\n    mov edx, [{save:#x}+12]\n    wrmsr\n'
# rdmsr leaves the GS base in EAX: the start-up bases differ (QEMU's stub slot, natemu's 0), so it is cleared.
SETUP = SAVE_GS + '    lgdt [{p:#x}]\n    xor eax, eax\n'


def rdmsr(index, reg):
    return f'    mov ecx, {index:#x}\n    rdmsr\n    shl rdx, 32\n    or rax, rdx\n    mov {reg}, rax\n'


# The positive program: the kernel's and the decompressor's sequences (LGDT, null and flat data segment loads, LRETQ to
# __KERNEL_CS, LTR, LLDT 0, iret_to_self) plus every form once, results in registers and at C.
BASIC = SETUP + """    sgdt [{c:#x}]
    sidt [{p:#x}+0x80]
    lidt [{p:#x}+0x10]
    sidt [{c:#x}+0x10]
    lidt [{p:#x}+0x80]
    mov eax, 0x48
    mov ds, eax
    mov es, ax
    mov eax, 0x10
    mov ss, eax
    mov eax, 0x50
    mov fs, eax
    mov gs, ax
""" + rdmsr(0xc0000100, 'rbx') + rdmsr(0xc0000101, 'rsi') + """    xor eax, eax
    mov fs, eax
""" + rdmsr(0xc0000100, 'rdi') + """    mov r8, ds
    mov r9d, ss
    mov r10, -1
    mov r10w, es
    mov [{c:#x}+0x20], fs
    mov r11, -1
    mov r11d, cs
    mov ax, 0x60
    lldt ax
    sldt [{c:#x}+0x22]
    mov r12, -1
    sldt r12
    mov eax, 0x0c
    mov es, eax
    mov r13, es
    xor eax, eax
    lldt ax
    sldt [{c:#x}+0x24]
    mov eax, 0x70
    ltr ax
    str [{c:#x}+0x26]
    str r14
    lea rax, [rel .j1]
    mov [{p:#x}+0x90], rax
    mov word [{p:#x}+0x98], 0x08
    jmp far [{p:#x}+0x90]
    ud2
.j1:
    lea rax, [rel .f1]
    mov [{p:#x}+0x90], eax
    mov word [{p:#x}+0x94], 0x08
    call far dword [{p:#x}+0x90]
    mov [{c:#x}+0x30], rsp
    jmp .k0
.f1:
    mov [{c:#x}+0x28], rsp
    retf
.k0:
    push 0x08
    lea rax, [rel .k1]
    push rax
    o64 retf
.k1:
    push 0
    push 0
    push 0x08
    lea rax, [rel .k2]
    push rax
    o64 retf 16
.k2:
    mov [{c:#x}+0x38], rsp
    mov rax, rsp
    mov ecx, ss
    push rcx
    push rax
    pushfq
    mov ecx, cs
    push rcx
    lea rax, [rel .k3]
    push rax
    iretq
.k3:
    mov [{c:#x}+0x40], rsp
    xor eax, eax
    mov ss, eax
    mov ds, eax
    mov es, eax
    mov [{c:#x}+0x48], ss
    mov rax, rsp
    push 0
    push rax
    pushfq
    push 0x08
    lea rax, [rel .k4]
    push rax
    iretq
.k4:
    mov eax, 0x10
    mov ss, eax
    mov ds, eax
    mov r15d, ds
""" + RESTORE_GS


def basic(n):
    bodies, cpus, mem = [], [], []
    for i in range(n):
        bodies.append(P.body(P.SYS_CODE(i), P.sys_text(i, BASIC.format(**fields(i)))))
        cpus.append(dict(P.cpu(i, P.SYS_CODE(i)), cpl=0)); mem += memory(i, n)
    return {'format': 'natemu-smp-v1', 'name': f'seg-basic@{n}', 'ncpu': n, 'system': True, 'bodies': bodies, 'cpus': cpus,
            'memory': mem, 'params': {}}


# User code (page U of CPU i): reads its segment state, then a privileged MOV from CR0 (#GP(0)) ends the run in both.
USER = """bits 64
    mov eax, ss
    mov r8d, eax
    mov r9, cs
    mov r10, ds
    mov r11, es
    mov r12, fs
    mov r13, gs
    mov r14, rsp
    pushfq
    pop r15
    and r15d, 0x8d5
"""


def to_user(kind, tail='    mov rax, cr0\n'):
    """A body that loads the GDT and TSS, DS/ES/FS/GS with DPL 0 selectors, then LRETQ or IRETQ to CPL 3 (CS 0x23,
    SS 0x1b, user stack) into user code ending with `tail`."""
    frame = ('    push 0x1b\n    push {us:#x}\n    push 0x23\n    push {u:#x}\n    o64 retf\n' if kind == 'lret'
             else '    push 0x1b\n    push {us:#x}\n    push 0x3002\n    push 0x23\n    push {u:#x}\n    iretq\n')
    body = SETUP + """    mov eax, 0x70
    ltr ax
    mov eax, 0x10
    mov ds, eax
    mov eax, 0x50
    mov es, eax
    mov fs, eax
    mov gs, eax
""" + RESTORE_GS + frame
    return body, USER + tail


# The cited Linux v6.12.107 sequences, transcribed (registers and labels renamed; the GDT images are the sources'):
# decompressor `arch/x86/boot/compressed/head_64.S:298-306` (null data segments), `:366-374` (gdt64 fixed up, LGDT,
# LRETQ to __KERNEL_CS 0x10), `:440-443` (GDTR repointed); the AP trampoline `realmode/rm/trampoline_64.S:214-248` (LIDT
# of the zero IDT, LGDT, __KERNEL_DS 0x18 into SS/DS/ES/FS/GS, LRETQ through tr_start); the kernel head
# `kernel/head_64.S:338-353` (LGDT from the stack, null data segments) and `startup_64_setup_gdt_idt` (__KERNEL_DS into
# DS/SS/ES); `load_TR_desc` on the writable GDT, then the read-only fixmap GDT (accessed bits preset) and segment loads
# from it (`kernel/cpu/common.c:719-737`). The decompressor's descriptors have the accessed bit clear: the loads set it.
BOOT_GDT = 0xf000           # gdt64 at +0 (its base field: an offset to gdt, fixed up), gdt at +0x10, tr_start at +0x40
def boot_gdt():
    gdt = [0x0000_0000_0000_0027, 0x00cf_9a00_0000_ffff, 0x00af_9a00_0000_ffff, 0x00cf_9200_0000_ffff, 0x0080_8900_0000_0000, 0]
    return (0x27).to_bytes(2, 'little') + (0x10).to_bytes(8, 'little') + bytes(6) + b''.join(q.to_bytes(8, 'little') for q in gdt)


def kernel_gdt(i):
    image = b''.join(q.to_bytes(8, 'little') for q in (0, 0x00cf_9b00_0000_ffff, 0x00af_9b00_0000_ffff, 0x00cf_9300_0000_ffff,
                                                        0x00cf_fb00_0000_ffff, 0x00cf_f300_0000_ffff, 0x00af_fb00_0000_ffff, 0))
    return image + sm.system_descriptor(P.SYS_D(i) + TSS, 0x67, 9) + bytes(LIMIT + 1 - 0x50)


BOOT = SAVE_GS + """    cld
    xor eax, eax
    mov ds, eax
    mov es, eax
    mov ss, eax
    mov fs, eax
    mov gs, eax
    mov eax, {bg:#x}
    add [rax+2], rax
    lgdt [rax]
    push 0x10
    lea rax, [rel .on_kernel_cs]
    push rax
    o64 retf
.on_kernel_cs:
    mov eax, {bg:#x}
    lea rdx, [rax+0x10]
    mov [rax+2], rdx
    lgdt [rax]
    sidt [{p:#x}+0x80]
    lidt [{bg:#x}+0x38]
    lgdt [{bg:#x}]
    lea rax, [rel .tr_start]
    mov [{bg:#x}+0x40], rax
    mov dx, 0x18
    mov ss, edx
    mov ds, edx
    mov es, edx
    mov fs, edx
    mov gs, edx
    push 0x10
    push qword [{bg:#x}+0x40]
    o64 retf
.tr_start:
    sub rsp, 16
    mov word [rsp], 0x4f
    mov rax, {g:#x}
    mov [rsp+2], rax
    lgdt [rsp]
    add rsp, 16
    xor eax, eax
    mov ds, eax
    mov ss, eax
    mov es, eax
    mov fs, eax
    mov gs, eax
    lidt [{p:#x}+0x80]
    mov eax, 0x18
    mov ds, eax
    mov ss, eax
    mov es, eax
    mov eax, 0x40
    ltr ax
    lgdt [{p:#x}+0x30]
    mov eax, 0x18
    mov ds, eax
    mov ss, eax
    mov es, eax
    mov r8, cs
    mov r9, ss
    str r10
    sgdt [{c:#x}]
""" + RESTORE_GS


def boot(n):
    bodies, cpus, mem = [], [], []
    for i in range(n):
        f = dict(fields(i), bg=P.SYS_D(i) + BOOT_GDT)
        bodies.append(P.body(P.SYS_CODE(i), P.sys_text(i, BOOT.format(**f))))
        cpus.append(dict(P.cpu(i, P.SYS_CODE(i)), cpl=0))
        mem += memory(i, n, gdt=kernel_gdt(i)) + [{'addr': f"0x{f['bg']:x}", 'data': boot_gdt().hex()}]
    # QEMU sets no accessed bit when a far transfer loads CS; the SDM (Vol.3 3.4.5: whenever a selector is loaded into a
    # segment register) and segment_model do: the decompressor's __KERNEL_CS byte 5 is 0x9b after its LRETQ.
    sdm = {f"{P.SYS_D(i) + BOOT_GDT + 0x10 + 0x10 + 5:#x}": 0x9b for i in range(n)}
    return {'format': 'natemu-smp-v1', 'name': f'seg-boot@{n}', 'ncpu': n, 'system': True, 'bodies': bodies, 'cpus': cpus,
            'memory': mem, 'params': {'sdm_bytes': sdm}}


def program(name, body, user=None, n=1, ro_gdt=False, params=None):
    bodies, cpus, mem = [], [], []
    for i in range(n):
        f = fields(i)
        bodies.append(P.body(P.SYS_CODE(i), P.sys_text(i, body.format(**f))))
        cpus.append(dict(P.cpu(i, P.SYS_CODE(i)), cpl=0))
        mem += memory(i, n, ro_gdt)
        if user:
            mem.append({'addr': f"0x{f['u']:x}", 'data': P.asm(user).hex()})
    return {'format': 'natemu-smp-v1', 'name': name, 'ncpu': n, 'system': True, 'bodies': bodies, 'cpus': cpus,
            'memory': mem, 'params': params or {}}


def model_state(i=0, cpl=0, ro_gdt=False, ldt=False):
    """segment_model's state at the instruction under test: after SETUP (and the CPL 3 entry of to_user)."""
    d = P.SYS_D(i)
    pages = {}
    for base, data in ((GDT, gdt_image(i)), (RO, gdt_image(i)), (LDT, ldt_image()), (TSS, tss_image(i)), (PTR, pointers(i, ro_gdt))):
        pages[(d + base) >> 12] = page = dm.page(write=base != RO, user=True)
        page['data'][(d + base) & 0xfff:((d + base) & 0xfff) + len(data)] = data
    for a in (d + PTR, d + USTACK - 0x800, P.STACK(i) - 0x1000, P.STACK(i)):
        pages.setdefault(a >> 12, dm.page(user=True))
    seg = {'es': (0x10, 0xc093), 'cs': (0x08, 0xa09b), 'ss': (0x10, 0xc093), 'ds': (0x10, 0xc093), 'fs': (0, 0), 'gs': (0, 0)}
    if cpl == 3:
        seg.update(cs=(0x23, 0xa0fb), ss=(0x1b, 0xc0f3), ds=(0, 0), es=(0, 0), fs=(0, 0), gs=(0, 0))
    return dict(rip=0, length=0, rsp=(d + USTACK) if cpl else P.STACK(i), rflags=0x202, cpl=cpl, cs=seg['cs'][0],
                ss=seg['ss'][0], cr0=0x80010011, gdtr=(d + (RO if ro_gdt else GDT), LIMIT), idtr=(0, 0), tr=None,
                ldtr=(0x60, d + LDT, 0x17) if ldt else None, seg=seg, fs_base=0, gs_base=0, memory=pages)


# Fault cases: (name, prefix setting registers, the instruction under test, model call on the state, CPL, options).
# The instruction under test is the body's last (CPL 0) or the user code's last (CPL 3).
def cases():
    d = P.SYS_D(0)
    c = []
    load = lambda sreg, sel, **o: (f'    mov eax, {sel:#x}\n', f'    mov {sreg}, eax\n', lambda s: sm.load(s, sreg, sel), o)
    c.append(('fs-ldt-null', '    lldt ax\n    mov eax, 0x0c\n', '    mov fs, eax\n', lambda s: sm.load(s, 'fs', 0x0c), {}))
    for name, sreg, sel in (('ds-not-present', 'ds', 0x28), ('es-execute-only', 'es', 0x30), ('ds-rpl', 'ds', 0x13), ('gs-limit', 'gs', 0xd0), ('ss-read-only', 'ss', 0x40),
                            ('ss-not-present', 'ss', 0xc8), ('ss-code', 'ss', 0x08), ('ss-dpl3', 'ss', 0x1b),
                            ('ss-null-rpl1', 'ss', 0x1), ('ds-tss', 'ds', 0x70)):
        c.append((name,) + load(sreg, sel))
    c.append(('ds-accessed-ro-gdt',) + load('ds', 0x48, ro_gdt=True))
    for name, sel in (('busy', 0x80), ('ldt-type', 0x60), ('not-present', 0x90), ('null', 0x0), ('ti', 0x74),
                      ('upper-type', 0xa0), ('data', 0x10)):
        c.append((f'ltr-{name}', f'    mov eax, {sel:#x}\n', '    ltr ax\n', lambda s, sel=sel: sm.ltr(s, sel), {}))
    c.append(('ltr-ro-gdt', '    mov eax, 0x70\n', '    ltr ax\n', lambda s: sm.ltr(s, 0x70), {'ro_gdt': True}))
    c.append(('lldt-tss', '    mov eax, 0x70\n', '    lldt ax\n', lambda s: sm.lldt(s, 0x70), {}))
    c.append(('lldt-code', '    mov eax, 0xc0\n', '    lldt ax\n', lambda s: sm.lldt(s, 0xc0), {}))
    c.append(('lgdt-noncanonical', '', '    lgdt [{p:#x}+0x20]\n', lambda s: sm.lgdt(s, d + PTR + 0x20), {}))
    far = lambda sel, off=0x110000: (f'    mov qword [{{p:#x}}+0x90], {off:#x}\n    mov word [{{p:#x}}+0x98], {sel:#x}\n',
                                     '    jmp far [{p:#x}+0x90]\n')
    for name, sel, off in (('data', 0x10, 0x110000), ('not-present', 0xc0, 0x110000), ('dpl3', 0x20, 0x110000),
                           ('rpl3', 0x0b, 0x110000), ('tss', 0x70, 0x110000), ('null', 0x0, 0x110000)):
        c.append((f'jmp-far-{name}',) + far(sel, off) + (lambda s, sel=sel, off=off: sm.far(_ptr(s, off, sel), 'jmp', d + PTR + 0x90), {}))
    c.append(('call-far-noncanonical', '    mov rax, 0x0000800000000000\n    mov [{p:#x}+0x90], rax\n    mov word [{p:#x}+0x98], 0x08\n',
              '    call far [{p:#x}+0x90]\n', lambda s: sm.far(_ptr(s, 1 << 47, 0x08), 'call', d + PTR + 0x90), {}))
    pushes = lambda words: ''.join(f'    mov rax, {w:#x}\n    push rax\n' for w in reversed(words))
    ret = lambda words, **o: (pushes(words), '    o64 retf\n', lambda s: sm.ret(_stack(s, words)), o)
    c.append(('lret-null-ss-cpl3',) + ret([0x110000, 0x23, d + USTACK, 0x3]))
    c.append(('lret-ss-rpl',) + ret([0x110000, 0x23, d + USTACK, 0x13]))
    c.append(('lret-data-cs',) + ret([0x110000, 0x10]))
    c.append(('lret-cs-not-present',) + ret([0x110000, 0xc0]))
    c.append(('lret-noncanonical',) + ret([1 << 47, 0x08]))
    iret = lambda words: (pushes(words), '    iretq\n', lambda s: sm.iret(_stack(s, words)), {})
    c.append(('jmp-far-compat',) + far(0x38) + (lambda s: sm.far(_ptr(s, 0x110000, 0x38), 'jmp', d + PTR + 0x90), {}))
    c.append(('jmp-far-call-gate',) + far(0xb0) + (lambda s: sm.far(_ptr(s, 0x110000, 0xb0), 'jmp', d + PTR + 0x90), {}))
    c.append(('lret-compat',) + ret([0x110000, 0x38]))
    c.append(('iret-tf',) + iret([0x110000, 0x08, 0x102, P.STACK(0), 0x10]))
    c.append(('iret-null-ss-rpl',) + iret([0x110000, 0x08, 2, P.STACK(0), 0x1]))
    c.append(('iret-dpl3-ss',) + iret([0x110000, 0x08, 2, P.STACK(0), 0x18]))
    c.append(('iret-ss-not-present',) + iret([0x110000, 0x08, 2, P.STACK(0), 0xc8]))
    # CPL 3 (entered by LRETQ): privilege and null-SS faults.
    for name, insn, call in (('ds-dpl0', '    mov eax, 0x10\n    mov ds, eax\n', lambda s: sm.load(s, 'ds', 0x10)),
                             ('ss-null', '    mov eax, 0x3\n    mov ss, eax\n', lambda s: sm.load(s, 'ss', 3)),
                             ('ss-dpl0', '    mov eax, 0x13\n    mov ss, eax\n', lambda s: sm.load(s, 'ss', 0x13)),
                             ('lgdt', '    lgdt [rsp]\n', lambda s: sm.lgdt(s, d + USTACK)),
                             ('ltr', '    mov eax, 0x70\n    ltr ax\n', lambda s: sm.ltr(s, 0x70)),
                             ('lret-to-cpl0', '    push 0x08\n    push 0x110000\n    o64 retf\n', lambda s: sm.ret(_stack(s, [0x110000, 0x08])))):
        c.append((f'cpl3-{name}', None, insn, call, {'cpl': 3}))
    return c


def _ptr(s, off, sel):
    dm.place(s['memory'], P.SYS_D(0) + PTR + 0x90, off.to_bytes(8, 'little') + sel.to_bytes(2, 'little'))
    return s


def _stack(s, words):
    s['rsp'] -= 8 * len(words)
    for k, w in enumerate(words):
        dm.place(s['memory'], s['rsp'] + 8 * k, w.to_bytes(8, 'little'))
    return s


def fault_programs():
    """Each case as a program whose last instruction (in the body, or in the user code at CPL 3) is the one under test;
    `params.model` holds segment_model's outcome ('fault' with (vector, error, cr2), or 'refused') and its address."""
    out = []
    for name, prefix, insn, call, o in cases():
        cpl, ro = o.get('cpl', 0), o.get('ro_gdt', False)
        f = fields(0)
        state = model_state(cpl=cpl, ro_gdt=ro)
        if cpl == 3:
            body, user = to_user('lret', tail=insn)
            at = f['u'] + len(P.asm(USER + ''.join(insn.splitlines(True)[:-1])))
            state['rsp'] = f['us']
            prog = program(f'seg-{name}@1', body, user)
        else:
            prog = program(f'seg-{name}@1', SETUP + prefix + insn, ro_gdt=ro)
            full = P.sys_text(0, (SETUP + prefix + insn).format(**f))
            tail = P.asm((insn + P.RESTORE + '\n    cmp r11, r11').format(**f))
            at = P.SYS_CODE(0) + len(P.asm(full)) - len(tail)
        r = call(state)
        prog['params'] = {'model': {'outcome': r['outcome'], 'fault': r['fault'], 'at': at, 'reason': r['reason'],
                                    'permitted': sorted(([o, list(f) if f else None] for o, f in r['permitted']), key=str)}}
        out.append(prog)
    return out


# IRETQ from CPL 3 to CPL 3 whose frame sets VIF and VIP (SDM IRET: restored only at CPL 0): the user code reads its
# RFLAGS (masked to VIF, VIP and the arithmetic flags: the start-up IF/IOPL differ between QEMU's stub and the driver).
VIF_TAIL = """    mov rax, rsp
    push 0x1b
    push rax
    mov eax, 0x180202
    push rax
    push 0x23
    lea rax, [rel .back]
    push rax
    iretq
.back:
    pushfq
    pop rbx
    and ebx, 0x1808d5
    mov rax, cr0
"""


def ltr_race(n):
    """Every CPU loads TR from one TSS descriptor (CPU 0's GDT) after a start barrier: LTR's busy write is a
    compare-exchange, so exactly one CPU succeeds and every other takes #GP(0x70) at its LTR (natemu only: QEMU's LTR is
    not atomic). `params.race`: the LTR's address per CPU."""
    bodies, cpus, mem, at = [], [], [], []
    flags = P.SYS_D(0) + CAP + 0x80
    for i in range(n):
        f = fields(i)
        wait = ''.join(f'.w{k}:\n    cmp byte [{flags + k:#x}], 1\n    jne .w{k}\n' for k in range(n))
        head = SAVE_GS + f'    lgdt [{fields(0)["p"]:#x}]\n    mov byte [{flags + i:#x}], 1\n' + wait + '    mov eax, 0x70\n'
        text = P.sys_text(i, (head + '    ltr ax\n').format(**f))
        bodies.append(P.body(P.SYS_CODE(i), text))
        tail = P.asm(('    ltr ax\n' + P.RESTORE + '\n    cmp r11, r11').format(**f))
        at.append(P.SYS_CODE(i) + len(P.asm(text)) - len(tail))
        cpus.append(dict(P.cpu(i, P.SYS_CODE(i)), cpl=0))
        mem += memory(i, n)
    return {'format': 'natemu-smp-v1', 'name': f'seg-ltr-race@{n}', 'ncpu': n, 'system': True, 'bodies': bodies,
            'cpus': cpus, 'memory': mem, 'params': {'race': at}}


def programs(cpus=(1, 2, 4)):
    """Every S2b program: the positive ones at each CPU count, LRETQ/IRETQ to CPL 3, and the fault cases."""
    ps = [basic(n) for n in cpus] + [boot(n) for n in cpus]
    for kind in ('lret', 'iret'):
        for n in cpus:
            body, user = to_user(kind)
            ps.append(program(f'seg-{kind}-cpl3@{n}', body, user, n=n))
    body, user = to_user('lret', tail=VIF_TAIL)
    ps.append(program('seg-iret-vif-cpl3@1', body, user))
    ps += [ltr_race(n) for n in (2, 4)]
    return ps + fault_programs()

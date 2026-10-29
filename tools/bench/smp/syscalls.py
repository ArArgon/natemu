"""S3 programs: SYSCALL/SYSRET with SWAPGS and per-CPU %gs, FSGSBASE, LAR/LSL/VERR/VERW, RDRAND, the cache instructions
(natemu-smp-v1, `system`, the tables and descriptors of segments.py). Positive programs are compared with pinned QEMU
system mode (registers, captured memory, the closing fault); the LAR/LSL/VERR/VERW tables are also judged by
tools/models/syscall_model.py (`params.model_memory`). Fault cases carry syscall_model's outcome (`params.model`), as
segments.py's do. Every run ends at CPL 3 with a privileged `mov rax, cr0` (#GP(0)), logged by both."""
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent.parent))
import programs as P
import segments as S
from models import syscall_model as sc

RES = 0xf000                       # per CPU: a captured user page for the descriptor tables' results
STAR = 0x0010_0008 << 32           # SYSCALL CS 0x08, SS 0x10; SYSRET CS 0x23, SS 0x1b (segments.gdt_image)
FMASK = 0x47700                    # Linux's: TF DF IF IOPL AC NT
# The first SYSRET's R11: every bit but TF, IF, AC, VM, VIF and VIP (reserved bits 3, 5, 15, 22..63 and RF must not reach
# RFLAGS). Pinned QEMU differs from the SDM's mask (3C7FD7H) on the bits left out: it loads VM (and then does not reach the
# end) and does not load VIF/VIP (natemu, as the SDM, does: seen as 0x387cd7 against QEMU's 0x207cd7 when they were set).
SYSRET_R11 = (1 << 64) - 1 & ~0x1e0300
EFER_SCE = 'mov ecx, 0xc0000080\n    rdmsr\n    or eax, 1\n    wrmsr\n'
FSGSBASE = 'mov rax, cr4\n    bts rax, 16\n    mov cr4, rax\n'


def wrmsr(index, value):
    return f'    mov ecx, {index:#x}\n    mov eax, {value & 0xffffffff:#x}\n    mov edx, {value >> 32:#x}\n    wrmsr\n'


def enter_user(prefix=''):
    """A CPL 0 body: GDT, TSS (RSP0 for the closing fault), `prefix`, then LRETQ to the user code (CS 0x23, SS 0x1b)."""
    return S.SETUP + '    mov eax, 0x70\n    ltr ax\n' + S.RESTORE_GS + '    xor eax, eax\n    xor edx, edx\n' + prefix + \
        '    push 0x1b\n    push {us:#x}\n    push 0x23\n    push {u:#x}\n    o64 retf\n'


def program(name, body, user=None, n=1, params=None, res=False):
    p = S.program(name, body, user, n=n, params=params)
    if res:
        for i in range(n):
            p['memory'].append(P.zero(P.SYS_D(i) + RES, 0x1000))
    return p


# ---- SYSCALL -> handler -> SYSRET with SWAPGS, per-CPU %gs, STAR/LSTAR/FMASK, FSGSBASE ------------------------------------
# The kernel side (CPU i's body page): KERNEL_GS_BASE = K (its results page + 0x80): [K] counts calls, [K+8] = 0x100 + i,
# [K+0x10] the user RSP, [K+0x18] the kernel stack, [K+0x20] RAX, [K+0x28] the masked RFLAGS, [K+0x30] GS.base in the
# handler. It enters CPL 3 by SYSRETQ itself; the user code reads its bases, makes three system calls and restores the
# start-up GS base and clears DF (QEMU's stub needs both) before the closing fault.
ROUND_BODY = S.SETUP + """    mov eax, 0x70
    ltr ax
""" + S.RESTORE_GS + '    ' + EFER_SCE + wrmsr(0xc0000081, STAR) + """    mov ecx, 0xc0000082
    lea rax, [rel .entry]
    mov rdx, rax
    shr rdx, 32
    wrmsr
""" + wrmsr(0xc0000084, FMASK) + wrmsr(0xc0000102, 0) + """    mov ecx, 0xc0000102
    mov eax, {k:#x}
    wrmsr
    mov qword [{k:#x}+8], {id:#x}
    mov qword [{k:#x}+0x18], {kstack:#x}
    """ + FSGSBASE + """    mov rax, {ugs:#x}
    wrgsbase rax
    mov rax, {ufs:#x}
    wrfsbase eax
    mov rsp, {us:#x}
    mov rcx, {u:#x}
    mov r11, {r11:#x}
    o64 sysret
.entry:
    swapgs
    mov [gs:0x10], rsp
    mov rsp, [gs:0x18]
    push r11
    push rcx
    inc qword [gs:0]
    mov [gs:0x20], rax
    pushfq
    pop qword [gs:0x28]
    rdgsbase rax
    mov [gs:0x30], rax
    mov rax, [gs:8]
    pop rcx
    pop r11
    mov rsp, [gs:0x10]
    swapgs
    o64 sysret
"""
ROUND_USER = """bits 64
    pushfq
    pop r12
    rdgsbase r9
    rdfsbase r10
    mov rsi, -1
    rdfsbase esi
    mov r8, -1
    rdgsbase r8d
    mov ebx, 3
.again:
    mov eax, 39
    syscall
    dec ebx
    jnz .again
    mov r13, rcx
    mov r14, r11
    rdgsbase r15
    mov rbp, rax
    mov rdi, rsp
    cld
    mov eax, [{save:#x}+8]
    mov edx, [{save:#x}+12]
    shl rdx, 32
    or rax, rdx
    wrgsbase rax
    xor eax, eax
    xor edx, edx
    mov rax, cr0
"""


def round_trip(n):
    bodies, cpus, mem = [], [], []
    for i in range(n):
        f = dict(S.fields(i), k=S.fields(i)['c'] + 0x80, id=0x100 + i, kstack=P.STACK(i) - 0x800,
                 ugs=0x7fff_1234_5000 + 0x1000 * i, ufs=0xdead_beef_5678_9000 + 0x100 * i, r11=SYSRET_R11)
        bodies.append(P.body(P.SYS_CODE(i), P.sys_text(i, ROUND_BODY.format(**f))))
        cpus.append(dict(P.cpu(i, P.SYS_CODE(i)), cpl=0))
        mem += S.memory(i, n) + [{'addr': f"0x{f['u']:x}", 'data': P.asm(ROUND_USER.format(**f)).hex()}]
    # The handler's RFLAGS (pushed after an INC of the call count 2 -> 3): SYSCALL clears only FMASK's bits, so CF = 1
    # from the user's flags stays (0x07: CF, bit 1, PF). Pinned QEMU loses CF there (0x06) while saving it in R11.
    sdm = {f"{S.fields(i)['c'] + 0x80 + 0x28:#x}": 0x07 for i in range(n)}
    return {'format': 'natemu-smp-v1', 'name': f'sc-roundtrip@{n}', 'ncpu': n, 'system': True, 'bodies': bodies, 'cpus': cpus,
            'memory': mem, 'params': {'sdm_bytes': sdm}}


# ---- LAR/LSL/VERR/VERW over the GDT and LDT of segments.py, at CPL 0 and 3 --------------------------------------------------
# Not 0xa0 (a 64-bit TSS whose upper half has a nonzero type field): whether IA-32e LAR/LSL check the upper half is not
# settled by the SDM text at hand (syscall_model C8), so no result is asserted for it.
SELECTORS = (0x00, 0x03, 0x08, 0x10, 0x13, 0x18, 0x1b, 0x20, 0x23, 0x28, 0x30, 0x38, 0x40, 0x48, 0x50, 0x58, 0x5b, 0x60,
             0x70, 0x80, 0x90, 0xb0, 0xc0, 0xc8, 0xd0, 0x0c, 0x0f, 0x14)
# (mnemonic text with {r} the result address, model call, destination width)
FORMS = (('db 0x48\n    lar ebx, eax', 'lar', 8), ('lsl ebx, eax', 'lsl', 4), ('lar bx, ax', 'lar', 2), ('db 0x48\n    lsl ebx, eax', 'lsl', 8),
         ('lsl bx, ax', 'lsl', 2),
         ('verr ax', 'verr', None), ('verw ax', 'verw', None))


def table_text(r):
    """For every selector and form: RBX := -1, the instruction on AX, then [r] := RBX and [r+8] := ZF (16 bytes each), in a
    loop over the selector list (kept in the code page, readable at the CPL)."""
    forms = ''.join(f'    mov rbx, -1\n    {text}\n    setz cl\n    mov [rdi], rbx\n    mov [rdi+8], cl\n    add rdi, 16\n'
                    for text, _, _ in FORMS)
    sels = ', '.join(f'{x:#x}' for x in SELECTORS)
    end = r + 16 * len(FORMS) * len(SELECTORS)
    return f"""    lea rsi, [rel .sels]
    mov rdi, {r:#x}
.next:
    movzx eax, word [rsi]
    cmp eax, 0xffff
    je .done
{forms}    add rsi, 2
    jmp .next
.sels: dw {sels}, 0xffff
.done:
    mov word [{end + 32:#x}], 0x10
    mov rbx, -1
    lsl ebx, word [{end + 32:#x}]
    setz cl
    mov [{end:#x}], rbx
    mov [{end + 8:#x}], cl
    verw word [rel .sel]
    setz cl
    mov [{end + 16:#x}], cl
    jmp .past
.sel: dw 0x10
.past:
"""


def table_expect(cpl, base):
    """syscall_model's result bytes for table_text at `base`: {address: [permitted 16-byte images]} (LDTR = 0x60)."""
    state = S.model_state(cpl=cpl, ldt=True)
    state['gpr'] = {'rbx': (1 << 64) - 1}
    want, r = {}, base
    for s in SELECTORS:
        for _, op, width in FORMS:
            call = getattr(sc, op)
            res = call(state, s, 'rbx', width) if width else call(state, s)
            images = []
            for st in [res['state']] + res['alternatives']:
                rbx = st['gpr']['rbx'] if width else (1 << 64) - 1
                images.append((rbx.to_bytes(8, 'little') + bytes([st['rflags'] >> 6 & 1]) + bytes(7)).hex())
            want[f'{r:#x}'] = images
            r += 16
    return want


def tables(cpl):
    d = P.SYS_D(0)
    r = d + RES
    if cpl == 0:
        body = S.SETUP + '    mov eax, 0x60\n    lldt ax\n' + table_text(r) + '    xor eax, eax\n    lldt ax\n' + S.RESTORE_GS
        prog = program('sc-verify-cpl0@1', body, res=True)
    else:
        body = enter_user('    mov eax, 0x60\n    lldt ax\n')
        prog = program('sc-verify-cpl3@1', body, 'bits 64\n' + table_text(r) + '    mov rax, cr0\n', res=True)
    prog['params']['model_memory'] = table_expect(cpl, r)
    return prog


# ---- RDRAND, cache maintenance -------------------------------------------------------------------------------------------
# RDRAND after flags that it must clear (OF AF PF set by the CMP, CF clear); its values are not compared, only CF, the
# other flags, the merge of a 16-bit and the zero-extension of a 32-bit destination, and that values differ (an 8-sample
# test in the manner of Linux's x86_init_rdrand). The cache forms run between a store and a load of the same line.
RANDOM_USER = """bits 64
    mov eax, 0x80000000
    cmp eax, 1
    rdrand rax
    pushfq
    pop rbx
    mov eax, -1
    add eax, 1
    rdrand rax
    pushfq
    pop rcx
    or rbx, rcx
    mov eax, -1
    test eax, eax
    rdrand rax
    pushfq
    pop rcx
    or rbx, rcx
    and ebx, 0x8d5
    rdrand rcx
    setc dl
    cmp rax, rcx
    setne dh
    mov rsi, -1
    rdrand si
    setc r8b
    shr rsi, 16
    mov rdi, -1
    rdrand edi
    setc r9b
    shr rdi, 32
    mov r10d, 8
    xor r11d, r11d
    rdrand r12
.sample:
    rdrand r13
    adc r11d, 0
    cmp r13, r12
    je .same
    or r11d, 0x100
.same:
    dec r10d
    jnz .sample
    xor eax, eax
    xor ecx, ecx
    xor r12d, r12d
    xor r13d, r13d
    mov rax, cr0
"""
CACHE_BODY = """    mov qword [{c:#x}+0x50], 0x1234
    wbinvd
    mov rbx, [{c:#x}+0x50]
    mov qword [{c:#x}+0x58], 0x5678
    invd
    mov rsi, [{c:#x}+0x58]
    db 0xf3, 0x0f, 0x09
    mov rax, {c:#x}+0x50
    clflush [rax]
    mov qword [rax], 0x9abc
    clflush [rax+8]
    mfence
    mov rdi, [rax]
    clflush [rsp]
    clflush [rel $]
"""


def random(n):
    return program(f'sc-rdrand@{n}', enter_user(), RANDOM_USER, n=n)


def cache(n):
    return program(f'sc-cache@{n}', S.SETUP + CACHE_BODY + S.RESTORE_GS, n=n)


# ---- Fault cases (segments.py's shape: params.model = syscall_model's outcome at the instruction under test) ---------------
def state(cpl, efer=0xd00, cr4=0x20, gpr=None):
    s = S.model_state(cpl=cpl)
    s.update(efer=efer, cr4=cr4, msrs={'star': STAR, 'lstar': 0x110000, 'fmask': FMASK, 'kernel_gs_base': 0},
             gpr=dict(gpr or {}))
    return s


SCE = '    ' + EFER_SCE
CR4FS = '    ' + FSGSBASE
NONCANONICAL = 1 << 47


def cases():
    """(name, CPL 0 prefix, user prefix or None, instruction, model(state) -> result, CPL, stop for a refusal)."""
    u = S.fields(0)['u']
    code0 = P.SYS_CODE(0)
    c = [
        ('syscall-sce0', '', None, 'syscall', lambda: sc.syscall(state(0)), 0),
        ('syscall-sce0-cpl3', '', '', 'syscall', lambda: sc.syscall(state(3)), 3),
        ('sysret-cpl3', SCE, '', 'o64 sysret', lambda: sc.sysret(state(3, efer=0xd01)), 3),
        ('sysret-sce0', '', None, 'o64 sysret', lambda: sc.sysret(state(0, gpr={'rcx': u, 'r11': 2})), 0),
        ('sysret-noncanonical', SCE + f'    mov rcx, {NONCANONICAL:#x}\n    mov r11d, 2\n', None, 'o64 sysret',
         lambda: sc.sysret(state(0, efer=0xd01, gpr={'rcx': NONCANONICAL, 'r11': 2})), 0),
        ('sysret-tf', SCE + f'    mov rcx, {u:#x}\n    mov r11d, 0x102\n', None, 'o64 sysret',
         lambda: sc.sysret(state(0, efer=0xd01, gpr={'rcx': u, 'r11': 0x102})), 0, 'unsupported_system'),
        ('syscall-tf', SCE, None, 'syscall', lambda: sc.syscall(dict(state(0, efer=0xd01), rflags=0x102)), 0, 'unsupported_system'),
        ('sysretl', SCE + f'    mov ecx, {u:#x}\n    mov r11d, 2\n', None, 'sysret',
         lambda: sc.sysret(state(0, efer=0xd01, gpr={'rcx': u, 'r11': 2}), width=4), 0, 'unsupported_mode'),
        ('swapgs-cpl3', '', '', 'swapgs', lambda: sc.swapgs(state(3)), 3),
        ('rdfsbase-disabled', '', None, 'rdfsbase rax', lambda: sc.fsgsbase(state(0), 'rdfsbase', 'rax'), 0),
        ('wrgsbase-disabled-cpl3', '', '', 'wrgsbase rax', lambda: sc.fsgsbase(state(3, gpr={'rax': 0}), 'wrgsbase', 'rax'), 3),
        ('wrfsbase-noncanonical', CR4FS + f'    mov rax, {NONCANONICAL:#x}\n', None, 'wrfsbase rax',
         lambda: sc.fsgsbase(state(0, cr4=0x10020, gpr={'rax': NONCANONICAL}), 'wrfsbase', 'rax'), 0),
        ('wrgsbase-noncanonical-cpl3', CR4FS, f'    mov rax, {NONCANONICAL:#x}\n', 'wrgsbase rax',
         lambda: sc.fsgsbase(state(3, cr4=0x10020, gpr={'rax': NONCANONICAL}), 'wrgsbase', 'rax'), 3),
        ('rdseed', '', None, 'rdseed rax', lambda: sc.rdseed(state(0), 'rax'), 0),
        ('rdpid-cpl3', '', '', 'rdpid rax', lambda: sc.rdpid(state(3), 'rax'), 3),
        ('wbinvd-cpl3', '', '', 'wbinvd', lambda: sc.cache(state(3), 'wbinvd'), 3),
        ('invd-cpl3', '', '', 'invd', lambda: sc.cache(state(3), 'invd'), 3),
        ('clflush-noncanonical', f'    mov rax, {NONCANONICAL:#x}\n', None, 'clflush [rax]', lambda: sc.clflush(state(0), NONCANONICAL), 0),
        ('clflush-supervisor-cpl3', '', f'    mov rax, {code0:#x}\n', 'clflush [rax]', lambda: sc.clflush(_code_page(state(3)), code0), 3),
        # Unadvertised group 7 forms: #UD from the profile's feature bits (syscall_model's helpers).
        ('enclv', '', None, 'db 0x0f, 0x01, 0xc0', lambda: sc._run(state(0), lambda s, bus, alts: sc._ud_unless('7.0.ebx.SGX')), 0),
        ('xresldtrk-cpl3', '', '', 'db 0xf2, 0x0f, 0x01, 0xe9', lambda: sc._run(state(3), lambda s, bus, alts: sc._ud_unless('7.0.edx.TSXLDTRK')), 3),
        ('clflushopt', '', None, 'clflushopt [rsp]', lambda: sc.clflushopt(state(0), 0), 0),
        ('clflush-noncanonical-rbp', f'    mov rbp, {NONCANONICAL:#x}\n', None, 'clflush [rbp]', lambda: sc.clflush(state(0), NONCANONICAL, stack=True), 0),
    ]
    return [x if len(x) == 7 else x + (None,) for x in c]


def _code_page(s):
    """syscall_model's memory with CPU 0's supervisor code page (CLFLUSH at CPL 3 must #PF on it, P = 1)."""
    from models import delivery_model as dm
    s['memory'].setdefault(P.SYS_CODE(0) >> 12, dm.page(user=False))
    return s


def fault_programs():
    out = []
    f = S.fields(0)
    for name, prefix, user, insn, call, cpl, stop in cases():
        if cpl == 3:
            head = 'bits 64\n' + user
            prog = program(f'sc-{name}@1', enter_user(prefix), head + f'    {insn}\n    mov rax, cr0\n')
            at = f['u'] + len(P.asm(head.format(**f)))
        else:
            prog = program(f'sc-{name}@1', S.SETUP + prefix + f'    {insn}\n' + S.RESTORE_GS)
            full = P.sys_text(0, (S.SETUP + prefix + f'    {insn}\n' + S.RESTORE_GS).format(**f))
            tail = P.asm((f'    {insn}\n' + S.RESTORE_GS + P.RESTORE + '\n    cmp r11, r11').format(**f))
            at = P.SYS_CODE(0) + len(P.asm(full)) - len(tail)
        r = call()
        # An exception without an error code (#UD) is recorded with 0 by both QEMU's stub and the driver's log.
        zero = lambda x: x and [x[0], x[1] or 0, x[2]]
        prog['params'] = {'model': {'outcome': r['outcome'], 'fault': zero(r['fault']), 'at': at, 'reason': r['reason'],
                                    'permitted': sorted(([o, zero(list(x)) if x else None] for o, x in r['permitted']), key=str)}}
        if stop:
            prog['params']['stop'] = stop
        if name == 'syscall-tf':
            prog['cpus'][0]['rflags'] = '0x102'   # natemu only (a refusal: QEMU is not run); TF from the start
        out.append(prog)
    return out


def programs(cpus=(1, 2, 3, 4)):
    return ([round_trip(n) for n in cpus] + [tables(0), tables(3)] + [random(n) for n in (1, 2)] + [cache(n) for n in (1, 4)]
            + fault_programs())

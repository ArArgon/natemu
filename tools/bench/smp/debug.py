"""S4 programs: single-step (TF), INT1, instruction breakpoints (DR0-DR3/DR7) and DR6/DR7 semantics (natemu-smp-v1,
`system`, one CPU). Each program loads the GDT and TSS of segments.py and its own IDT, whose handlers log every event
(vector, RIP, CS, RFLAGS & LOG_FLAGS, DR6, error code) into a captured page and return by IRETQ. The #DB handler clears DR6
(unless the program sets KEEP) and sets RF in the frame when DR6 reports a breakpoint (B0-B3), as a debugger does; #UD
and #GP skip 2 bytes (UD2, WRMSR); #PF maps the page again. Expected logs (`expect`) are written from the SDM (Vol.3
18.3.1, 7.8.3, Table 7-2; tools/models/delivery_model.py), not from either implementation."""
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent.parent))
import programs as P
import segments as S

D = P.SYS_D(0)
IDT, PTRS, LOG, DATA = 0x290000, 0x290800, 0x291000, 0x293000      # PTRS: +0 IDTR, +0x10 saved IDTR, +0x20 cursor, +0x28 KEEP, +0x30 kernel RSP
LOG_BYTES, ENTRY = 0x2000, 0x30
LOG_FLAGS = 0x10fd5                                                 # CF PF AF ZF SF TF IF DF OF RF
TF, RF = 0x100, 0x10000
BS, BD = 1 << 14, 1 << 13
DR6_FIXED = 0xffff0ff0
VECTORS = (1, 3, 6, 13, 14, 0x21)
F = dict(D=D, idt=IDT, ptrs=PTRS, data=DATA, la=P.sys_leaf(0, 0, D + P.V), V=D + P.V, u=D + S.UCODE, us=D + S.USTACK, g=D + S.GDT,
         p=D + S.PTR, save=D + P.SAVE)

HEAD = """    cli
    lgdt [{p:#x}]
    mov eax, 0x70
    ltr ax
    sidt [{ptrs:#x}+0x10]
    lidt [{ptrs:#x}]
"""
TF_ON = '    pushfq\n    or qword [rsp], 0x100\n    popfq\n'
TF_OFF = '    pushfq\n    and qword [rsp], ~0x100\n    popfq\n'
TAIL = """    xor eax, eax
    mov dr7, rax
    lidt [{ptrs:#x}+0x10]
    jmp s4_end
"""
HANDLERS = """s4_common:
    push rax
    push rbx
    mov rbx, [{ptrs:#x}+0x20]
    mov rax, [rsp+16]
    mov [rbx], rax
    mov rax, [rsp+32]
    mov [rbx+8], rax
    mov rax, [rsp+40]
    mov [rbx+16], rax
    mov rax, [rsp+48]
    and rax, 0x10fd5
    mov [rbx+24], rax
    mov rax, dr6
    mov [rbx+32], rax
    mov rax, [rsp+24]
    mov [rbx+40], rax
    add rbx, 0x30
    mov [{ptrs:#x}+0x20], rbx
    mov rax, [rsp+16]
    cmp rax, 1
    jne s4_n1
    mov rax, dr6
    test eax, 0xf
    jz s4_norf
    or qword [rsp+48], 0x10000
s4_norf:
    cmp byte [{ptrs:#x}+0x28], 0
    jne s4_n1
    xor eax, eax
    mov dr6, rax
s4_n1:
    mov rax, [rsp+16]
    cmp rax, 6
    je s4_skip
    cmp rax, 13
    jne s4_n13
s4_skip:
    add qword [rsp+32], 2
s4_n13:
    cmp rax, 14
    jne s4_n14
    or qword [{la:#x}], 1
    invlpg [{V:#x}]
s4_n14:
    pop rbx
    pop rax
    add rsp, 16
    iretq
""" + ''.join(f"""s4_v{v}:
{'' if v in (13, 14) else '    push 0' + chr(10)}    push {v}
    jmp s4_common
""" for v in VECTORS) + 's4_end:\n'


# ---- programs: name -> (body, user code or None, KEEP) --------------------------------------------------------------------
STEP = TF_ON + """t_popf:
    mov eax, 1
t_mov:
    add rax, rax
t_add:
    jmp t_j
    nop
t_j:
    call t_f
t_ret:
    push rax
t_push:
    pop rcx
t_pop:
    inc qword [{data:#x}+0x100]
t_inc:
    mov ecx, 3
t_c3:
    mov rsi, {data:#x}
t_rsi:
    mov rdi, {data:#x}+0x80
t_rdi:
t_rep:
    rep movsb
t_repd:
    xor ecx, ecx
t_c0:
t_rep0:
    rep stosb
t_rep0d:
    mov ecx, 4
t_c4:
    mov rsi, {data:#x}+0x40
t_rsi2:
    mov rdi, {data:#x}+0x80
t_rdi2:
t_cmps:
    repe cmpsb
t_cmpsd:
    xor eax, eax
t_xor:
    cpuid
t_cpuid:
    xor ebx, ebx
t_x1:
    xor ecx, ecx
t_x2:
    xor edx, edx
t_x3:
    xor eax, eax
t_x4:
""" + TF_OFF + """t_off:
    nop
    jmp t_end
t_f:
    ret
t_end:
"""

POPF_IRET = TF_ON + """p_on:
    nop
p_nop:
""" + TF_OFF + """p_off:
    nop
    mov rax, rsp
    push 0x10
    push rax
    pushfq
    or qword [rsp], 0x100
    push 0x08
    lea rax, [rel i_t1]
    push rax
    iretq
i_t1:
    nop
i_n1:
    nop
i_n2:
    mov rax, rsp
i_m:
    push 0x10
i_p1:
    push rax
i_p2:
    pushfq
i_p3:
    and qword [rsp], ~0x100
i_a:
    push 0x08
i_p4:
    lea rax, [rel i_t2]
i_l:
    push rax
i_p5:
    iretq
i_t2:
    nop
"""

MOVSS = TF_ON + """m_on:
    mov eax, ss
m_rd:
    mov ss, eax
    nop
m_nop:
    nop
m_nop2:
""" + TF_OFF

SOFT = TF_ON + """s_on:
s_int3:
    int3
s_after3:
    nop
s_n1:
s_int21:
    int 0x21
s_after21:
    nop
s_n2:
s_int1:
    db 0xf1
s_after1:
    nop
s_n3:
""" + TF_OFF + """s_off:
    mov rax, 0xf
    mov dr6, rax
s_int1b:
    db 0xf1
s_after1b:
    xor eax, eax
    mov dr6, rax
"""

FAULTS = TF_ON + """f_on:
f_ud:
    ud2
f_ud_after:
    nop
f_n1:
    mov ecx, 0x1b
f_c:
    mov eax, 0xfee00a00
f_a:
    xor edx, edx
f_d:
f_wrmsr:
    wrmsr
f_wrmsr_after:
    nop
f_n2:
    and qword [{la:#x}], -2
f_and:
    invlpg [{V:#x}]
f_inv:
f_load:
    mov rbx, [{V:#x}+8]
f_load_after:
""" + TF_OFF

XBP = """    call b_fn
    lea rax, [rel b_fm]
    mov dr0, rax
    mov eax, 1
    mov dr7, rax
    call b_fn
    lea rax, [rel b_fn2]
    mov dr0, rax
    call rax
    lea rax, [rel b_fn2]
    call rax
    xor eax, eax
    mov dr7, rax
    jmp b_go
b_fn2:
    ret
b_fn:
    add rax, 1
    add rax, 2
b_fm:
    add rax, 3
    ret
b_go:
    lea rax, [rel b_b0]
    mov dr0, rax
    lea rax, [rel b_b1]
    mov dr1, rax
    lea rax, [rel b_b2+1]
    mov dr2, rax
    lea rax, [rel b_b3]
    mov dr3, rax
    mov eax, 0x65
    mov dr7, rax
    mov ecx, 2
    xor eax, eax
b_b1:
    add rax, 1
    nop
b_b0:
    mov rbx, rax
b_b2:
    mov rdx, 0x123456789
    push rax
    push rcx
    xor eax, eax
b_b3:
    cpuid
    xor ebx, ebx
    xor edx, edx
    pop rcx
    pop rax
    dec ecx
    jnz b_b1
    lea rax, [rel b_s1]
    mov dr0, rax
    lea rax, [rel b_r]
    mov dr1, rax
    lea rax, [rel b_l]
    mov dr2, rax
    lea rax, [rel b_t2]
    mov dr3, rax
    mov eax, 0x0c000055
    mov dr7, rax
    mov eax, ss
    mov ss, eax
b_s1:
    nop
    mov ecx, 3
    mov rsi, {data:#x}
    mov rdi, {data:#x}+0x80
b_r:
    rep movsb
    nop
b_l:
    nop
""" + TF_ON + """b_t1:
    nop
b_t2:
    nop
b_t3:
""" + TF_OFF + """b_off:
    xor eax, eax
    mov dr7, rax
    lea rax, [rel b_b0]
    mov dr0, rax
    nop
b_b0x:
"""
# (b_b0x: DR0 = b_b0 again with DR7 = 0: never a #DB.) LEN = 11 on breakpoint 2 (DR7 bits 27:26, b_l): matched as LEN 00.

DR = """    mov rax, 0xffffffff
    mov dr6, rax
    mov r8, dr6
    xor eax, eax
    mov dr6, rax
    mov r9, dr6
    mov rax, 0x5
    mov dr4, rax
    mov r10, dr6
    mov r11, dr4
    mov eax, 0x0f0f0003
    and eax, 0x0f0f0000
    mov dr5, rax
    mov r12, dr7
    mov r13, dr5
    xor eax, eax
    mov dr7, rax
    mov dr6, rax
    mov rax, 0x2000
    mov dr7, rax
g_mov:
    mov rbx, dr0
g_after:
    mov r14, dr7
    mov r15, dr6
    xor eax, eax
    mov dr6, rax
"""

STICKY = TF_ON + """k_on:
    nop
k_n:
    nop
k_n2:
""" + TF_OFF + """k_off:
    lea rax, [rel k_x]
    mov dr0, rax
    mov eax, 1
    mov dr7, rax
k_x:
    nop
    xor eax, eax
    mov dr7, rax
k_i:
    db 0xf1
k_after:
    xor eax, eax
    mov dr6, rax
"""

# SYSRET with TF from R11 traps before the first CPL 3 instruction; SYSCALL traps after itself when FMASK leaves TF (round
# 1), not when FMASK clears it (round 2).
SYSRET = "    cli\n" + S.SETUP + """    mov eax, 0x70
    ltr ax
""" + S.RESTORE_GS + """    sidt [{ptrs:#x}+0x10]
    lidt [{ptrs:#x}]
    mov ecx, 0xc0000080
    rdmsr
    or eax, 1
    wrmsr
    mov ecx, 0xc0000081
    xor eax, eax
    mov edx, 0x00100008
    wrmsr
    mov ecx, 0xc0000084
    xor eax, eax
    xor edx, edx
    wrmsr
    mov ecx, 0xc0000082
    lea rax, [rel y_k1]
    xor edx, edx
    wrmsr
    mov [{ptrs:#x}+0x30], rsp
    mov rcx, {u:#x}
    mov r11, 0x102
    mov rsp, {us:#x}
    o64 sysret
y_k1:
    mov rsp, [{ptrs:#x}+0x30]
y_k1m:
""" + TF_OFF + """y_off:
    mov ecx, 0xc0000084
    mov eax, 0x100
    xor edx, edx
    wrmsr
    mov ecx, 0xc0000082
    lea rax, [rel y_k2]
    xor edx, edx
    wrmsr
    mov rcx, {u:#x}
    mov r11, 0x102
    mov rsp, {us:#x}
    o64 sysret
y_k2:
    mov rsp, [{ptrs:#x}+0x30]
    mov ecx, 0xc0000080
    rdmsr
    and eax, ~1
    wrmsr
    mov ecx, 0xc0000084
    xor eax, eax
    xor edx, edx
    wrmsr
"""
USER = 'nop\nsyscall\n'
# Perf chunk 12: a run of no-operation instructions before a system instruction (CLI) is kept as one NOP_RUN record. It runs
# whole when n_fn is called plain; under TF each of its instructions traps after itself (the record is never a step: the
# resident steps its first instruction alone); an instruction breakpoint inside the run (armed after the record was made:
# the DR7 write drops this vCPU's code) faults there.
NOPS = """    call n_fn
    call n_fn
""" + TF_ON + """    call n_fn
n_r:
""" + TF_OFF + """    lea rax, [rel n_c]
    mov dr0, rax
    mov eax, 1
    mov dr7, rax
    call n_fn
    xor eax, eax
    mov dr7, rax
    call n_fn
    jmp n_go
n_fn:
    nop
n_b:
    db 0x66, 0x90
n_c:
    db 0x0f, 0x1f, 0x00
n_d:
    db 0x66, 0x2e, 0x0f, 0x1f, 0x84, 0x00, 0x00, 0x00, 0x00, 0x00
n_e:
    db 0xf3, 0x0f, 0x1e, 0xfa
n_f:
    cli
n_g:
    ret
n_go:
"""

PROGRAMS = {'s4-step': (STEP, None, 0), 's4-popf-iret': (POPF_IRET, None, 0), 's4-movss': (MOVSS, None, 0), 's4-soft': (SOFT, None, 0),
            's4-faults': (FAULTS, None, 0), 's4-xbp': (XBP, None, 0), 's4-dr': (DR, None, 0), 's4-sticky': (STICKY, None, 1),
            's4-sysret': (SYSRET, USER, 0), 's4-nops': (NOPS, None, 0)}


def program(name):
    body, user, keep = PROGRAMS[name]
    want = None
    if name in DATA_PROGRAMS:
        body, want = data_body(body)
    src = P.sys_text(0, (('' if name == 's4-sysret' else HEAD) + body + TAIL + HANDLERS).format(**F))
    code, labels = P.ref_assemble(src)
    gates = b''
    for v in range(0x22):
        off = P.SYS_CODE(0) + labels[f's4_v{v}'] if v in VECTORS else 0
        lo = off & 0xffff | 0x08 << 16 | (0x8e if off else 0) << 40 | (off >> 16 & 0xffff) << 48
        gates += lo.to_bytes(8, 'little') + (off >> 32).to_bytes(8, 'little')
    idtr = (0x22 * 16 - 1).to_bytes(2, 'little') + IDT.to_bytes(8, 'little')
    mem = S.memory(0, 1) + [
        {'addr': f'0x{IDT:x}', 'data': gates.hex()},
        {'addr': f'0x{PTRS:x}', 'data': (idtr + bytes(6 + 16) + LOG.to_bytes(8, 'little') + bytes([keep]) + bytes(0x17)).hex(), 'capture': False},
        P.zero(LOG, LOG_BYTES),
        {'addr': f'0x{DATA:x}', 'data': (bytes(range(0x40)) + bytes(range(0, 2)) + b'\xff' * 0x3e + bytes(0x80)).hex()},
        P.zero(D + P.V, 0x1000), P.zero(0x294000, 0x1000), P.zero(0x295000, 0x1000)]
    if user:
        mem.append({'addr': f"0x{F['u']:x}", 'data': P.asm(user).hex()})
    labels = {k: P.SYS_CODE(0) + v for k, v in labels.items()}
    return {'format': 'natemu-smp-v1', 'name': name, 'ncpu': 1, 'system': True, 'bodies': [{'addr': f'0x{P.SYS_CODE(0):x}', 'code': code.hex()}],
            'cpus': [dict(P.cpu(0, P.SYS_CODE(0)), cpl=0)], 'memory': mem, 'params': {'labels': labels, 'want': want}}


def log_of(prog, memory):
    """The log entries in `memory` ({address: bytes}): (vector, RIP, CS, RFLAGS & LOG_FLAGS, DR6, error)."""
    data = memory[LOG]
    out = []
    for k in range(0, len(data), ENTRY):
        e = [int.from_bytes(data[k + 8 * j:k + 8 * j + 8], 'little') for j in range(6)]
        if e == [0] * 6:
            break
        out.append(tuple(e))
    return out


def expect(prog):
    """The SDM's log for `prog`: entries (vector, RIP, CS, (RFLAGS bits TF and RF), DR6), from its labels."""
    L = prog['params']['labels']
    u = F['u']
    db = lambda at, dr6=BS, tf=True, rf=False, cs=8: (1, at, cs, (TF if tf else 0) | (RF if rf else 0), DR6_FIXED | dr6)
    # TF_OFF from `at` under TF: PUSHFQ (1 byte) and AND (8) trap after themselves, and so does the POPF that clears TF
    off = lambda at, dr6=BS: [db(at + 1, dr6), db(at + 9, dr6), db(at + 10, dr6, tf=False)]
    ev = lambda v, at, tf=True, rf=False: (v, at, 8, (TF if tf else 0) | (RF if rf else 0), None)
    name = prog['name']
    if prog['params'].get('want') is not None:
        out = []
        for where, bits, rf, tf, vector in prog['params']['want']:
            dr6 = DR6_FIXED | bits | (BS if tf else 0) if vector == 1 else None
            out.append((vector, L[where], 8, (TF if tf else 0) | (RF if rf else 0), dr6))
        return out
    if name == 's4-step':
        seq = ['t_mov', 't_add', 't_j', 't_f', 't_ret', 't_push', 't_pop', 't_inc', 't_c3', 't_rsi', 't_rdi']
        out = [db(L[k]) for k in seq]
        # REP MOVSB (RCX 3) traps after each iteration, at the REP with RF = 1 between iterations; REP STOSB with RCX 0 once;
        # REPE CMPSB stops at its third element (two equal bytes)
        out += [db(L['t_rep'], rf=True), db(L['t_rep'], rf=True), db(L['t_repd']), db(L['t_c0']), db(L['t_rep0d']), db(L['t_c4']),
                db(L['t_rsi2']), db(L['t_rdi2']), db(L['t_cmps'], rf=True), db(L['t_cmps'], rf=True), db(L['t_cmpsd']),
                db(L['t_xor']), db(L['t_cpuid']), db(L['t_x1']), db(L['t_x2']), db(L['t_x3']), db(L['t_x4'])]
        return out + off(L['t_x4'])
    if name == 's4-popf-iret':
        out = [db(L['p_nop'])] + off(L['p_nop'])
        # the TF_OFF POPF traps after itself; the IRET sequence runs untraced until the IRET loads TF = 1: a trap after the
        # first instruction at its target
        out += [db(L['i_n1']), db(L['i_n2']), db(L['i_m']), db(L['i_p1']), db(L['i_p2']), db(L['i_p3']), db(L['i_a']),
                db(L['i_p4']), db(L['i_l']), db(L['i_p5']), db(L['i_t2'], tf=False)]
        return out
    if name == 's4-movss':
        # MOV SS discards the single-step trap after it; the next instruction traps once (TF still 1)
        return [db(L['m_rd']), db(L['m_nop']), db(L['m_nop2'])] + off(L['m_nop2'])
    if name == 's4-soft':
        # INT3 / INT n / INT1 under TF: the handler runs without TF (its frame holds TF = 1), no single-step trap after
        # the INT; after its IRET the next instruction traps. INT1 is a #DB trap with DR6 unchanged (no BS).
        out = [ev(3, L['s_after3']), db(L['s_n1']), ev(0x21, L['s_after21']), db(L['s_n2']),
               (1, L['s_after1'], 8, TF, DR6_FIXED), db(L['s_n3'])]
        out += off(L['s_n3']) + [(1, L['s_after1b'], 8, 0, DR6_FIXED | 0xf)]
        return out
    if name == 's4-faults':
        # a faulting instruction owes no trap; the handler's IRET (RF = 1 in a fault's frame, TF = 1) runs it or the next
        out = [ev(6, L['f_ud'], rf=True), db(L['f_n1']), db(L['f_c']), db(L['f_a']), db(L['f_d']),
               (13, L['f_wrmsr'], 8, TF | RF, None), db(L['f_n2']), db(L['f_and']), db(L['f_inv']),
               (14, L['f_load'], 8, TF | RF, None), db(L['f_load_after'])]
        return out + off(L['f_load_after'])
    if name == 's4-xbp':
        b = lambda at, bits: (1, at, 8, 0, DR6_FIXED | bits)
        # b_fm: armed inside a block already translated and linked (the DR7 write drops this vCPU's code)
        # b_fn2: reached twice by CALL RAX (the dispatcher's L1 must never hold a block at an armed breakpoint)
        out = [b(L['b_fm'], 1), b(L['b_fn2'], 1), b(L['b_fn2'], 1)] + [b(L['b_b1'], 2), b(L['b_b0'], 1), b(L['b_b3'], 8)] * 2
        # MOV SS suppresses the breakpoint at b_s1; b_r fires once (RF kept over the REP's iterations); b_l (LEN 11) fires;
        # under TF, the trap after b_t1 lands on b_t2 (BS with B3, which matches: the handler sets RF), then no code
        # #DB at b_t2 (RF), its own trap after b_t2
        out += [b(L['b_r'], 2), b(L['b_l'], 4)]
        return out + [db(L['b_t2'], BS | 8), db(L['b_t3'])] + off(L['b_t3'])
    if name == 's4-dr':
        return [(1, L['g_mov'], 8, RF, DR6_FIXED | BD)]
    if name == 's4-sticky':
        return [db(L['k_n']), db(L['k_n2'])] + off(L['k_n2']) + [
                (1, L['k_x'], 8, 0, DR6_FIXED | BS | 1), (1, L['k_after'], 8, 0, DR6_FIXED | BS | 1)]
    if name == 's4-nops':
        # under TF: CALL, each NOP-class instruction, CLI and RET trap after themselves; then the breakpoint at n_c (a fault,
        # RF set by the handler)
        b = lambda at, bits: (1, at, 8, 0, DR6_FIXED | bits)
        steps = [db(L[k]) for k in ('n_fn', 'n_b', 'n_c', 'n_d', 'n_e', 'n_f', 'n_g', 'n_r')]
        return steps + off(L['n_r']) + [b(L['n_c'], 1)]
    if name == 's4-sysret':
        return [db(u, cs=0x23), db(u + 1, cs=0x23), db(L['y_k1']), db(L['y_k1m'])] + off(L['y_k1m']) + [db(u, cs=0x23), db(u + 1, cs=0x23)]
    raise KeyError(name)


def judge(prog, log):
    """Differences of a log from `expect` (vector, RIP, CS, TF/RF of the frame, DR6 when the entry names one)."""
    want, diffs = expect(prog), []
    for k in range(max(len(want), len(log))):
        w, g = want[k] if k < len(want) else None, log[k] if k < len(log) else None
        if w is None or g is None:
            diffs.append(f'entry {k}: want {w and tuple(map(hex, w[:2]))}, got {g and tuple(map(hex, g[:2]))}')
            continue
        got = (g[0], g[1], g[2], g[3] & (TF | RF), g[4])
        if got[:4] != w[:4] or (w[4] is not None and got[4] != w[4]):
            diffs.append(f'entry {k}: want ' + ' '.join(map(hex, (x for x in w if x is not None))) + ', got ' + ' '.join(map(hex, got)))
    return diffs


def show(prog, log):
    """Log entries with RIPs as labels (+offset), for reports."""
    labels = sorted((v, k) for k, v in prog['params']['labels'].items() if not k.startswith('s4_'))
    def where(rip):
        at = max((x for x in labels if x[0] <= rip), default=None)
        return f'{at[1]}+{rip - at[0]:#x}' if at and rip - at[0] < 0x40 else hex(rip)
    return [f'v{e[0]} {where(e[1])} cs={e[2]:#x} rf={e[3] >> 16 & 1} tf={e[3] >> 8 & 1}' + ('' if e[4] is None else f' dr6={e[4]:#x}') for e in log]


# ---- data and I/O breakpoints ---------------------------------------------------------------------------------------------
# Watched pages: W and the page after it (a store crossing into it). A body line may end with `;;E` and expected #DB
# entries, comma-separated: `next:BITS` (a trap with RIP after the line), `self:BITS` (RIP at the line: a REP iteration),
# `at:LABEL:BITS`; suffixes `:rf` (RF = 1 in the frame), `:tf` (TF = 1; BS is then in DR6 too); `vN` instead of a kind
# prefix logs another vector (`v3:LABEL`).
W = 0x294000
DR7_BITS = lambda n, rw, length: 1 << 2 * n | (rw | {1: 0, 2: 1, 4: 3, 8: 2}[length] << 2) << 16 + 4 * n


def arm(*bps):
    """DR0.. from (address, R/W, length) and DR7."""
    text = ''.join(f'    mov rax, {a:#x}\n    mov dr{n}, rax\n' for n, (a, _, _) in enumerate(bps))
    dr7 = 0
    for n, (_, rw, length) in enumerate(bps):
        dr7 |= DR7_BITS(n, rw, length)
    return text + f'    mov rax, {dr7:#x}\n    mov dr7, rax\n'


def data_body(lines):
    """`lines` with the expectation comments turned into labels: (assembly, [(kind, label or name, bits, rf, tf, vector)])."""
    out, want = [], []
    for k, line in enumerate(lines.splitlines()):
        code, _, spec = line.partition(';;E')
        if not spec.strip():
            out.append(code)
            continue
        out += [f'e{k}a:', code, f'e{k}b:']
        for item in spec.split(','):
            parts = item.strip().split(':')
            vector = 1
            if parts[0].startswith('v'):
                vector, parts = int(parts[0][1:]), ['at'] + parts[1:]
            kind = parts[0]
            if kind == 'at':
                where, bits, flags = parts[1], int(parts[2], 0) if len(parts) > 2 and parts[2] not in ('rf', 'tf') else 0, parts[2:]
            else:
                where, bits, flags = f'e{k}' + ('a' if kind == 'self' else 'b'), int(parts[1], 0), parts[2:]
            want.append((where, bits, 'rf' in flags, 'tf' in flags, vector))
    return '\n'.join(out) + '\n', want


def matrix_body(rw, length):
    """Every load and store of 1, 2, 4 and 8 bytes at the edges of two adjacent ranges (DR0 at `base`, its low bits set to
    be masked; DR1 right after it): straddling accesses meet one or both."""
    base = W + 0x40
    lines = [arm((base + length - 1, rw, length), (base + length, rw, length)).rstrip('\n')]
    regs = {1: 'al', 2: 'ax', 4: 'eax', 8: 'rax'}
    for w in (1, 2, 4, 8):
        for a in sorted({base - w, base - w + 1, base + length - 1, base + length, base + 2 * length - 1, base + 2 * length}):
            for store in (False, True):
                hits = sum(1 << n for n, lo in enumerate((base, base + length)) if a < lo + length and lo < a + w and (rw == 3 or store))
                insn = f'    mov [{a:#x}], {regs[w]}' if store else f'    mov {regs[w]}, [{a:#x}]'
                lines.append(insn + (f' ;;E next:{hits:#x}' if hits else ''))
    return '\n'.join(lines) + '\n'


X, Y, BB, CROSS = W + 0x100, W + 0x200, W + 0x300, W + 0x1000
FORMS = arm((X, 3, 8), (Y, 1, 4), (BB, 3, 1), (CROSS, 1, 4)) + f"""    mov rax, cr4
    or eax, 0x40200
    mov cr4, rax
    xor ecx, ecx
    mov eax, 7
    xor edx, edx
    xsetbv
    add qword [{X:#x}], 1 ;;E next:1
    lock add dword [{Y:#x}], 1 ;;E next:2
    xchg [{X:#x}], rax ;;E next:1
    mov eax, 0x12345678
    mov ecx, 0x9abc
    cmpxchg [{Y:#x}], ecx ;;E next:2
    mov eax, [{Y:#x}]
    cmpxchg [{Y:#x}], ecx ;;E next:2
    push qword [{X:#x}] ;;E next:1
    pop rax
    bt dword [{BB - 2:#x}], 0 ;;E next:4
    bts dword [{BB - 2:#x}], 1 ;;E next:4
    bt word [{BB - 1:#x}], 0 ;;E next:4
    bt dword [{BB - 4:#x}], 0
    mov ecx, 40
    bt dword [{BB - 4:#x}], ecx ;;E next:4
    fld qword [{X:#x}] ;;E next:1
    fstp qword [{X:#x}] ;;E next:1
    mov qword [{X - 8:#x}], 0
    mov qword [{X:#x}], 0 ;;E next:1
    fld tword [{X - 2:#x}] ;;E next:1
    fstp tword [{X - 2:#x}] ;;E next:1
    movups xmm0, [{X - 8:#x}] ;;E next:1
    movups [{Y - 12:#x}], xmm0 ;;E next:2
    movups xmm1, [{Y - 12:#x}]
    vmovdqu ymm0, [{X - 16:#x}] ;;E next:1
    vmovdqu [{Y - 28:#x}], ymm0 ;;E next:2
    vmovdqu ymm1, [{Y - 28:#x}]
    vzeroupper
    movbe eax, [{Y:#x}]
    movbe [{Y:#x}], eax ;;E next:2
    sgdt [{Y - 6:#x}] ;;E next:2
    mov rax, -1
    mov [{CROSS - 4:#x}], rax ;;E next:8
    mov [{CROSS - 8:#x}], rax
    mov rax, [{CROSS - 4:#x}]
    xor eax, eax
    xor ecx, ecx
    xor edx, edx
"""

R0, R1 = W + 0x500, W + 0x520
REPS = arm((R0, 1, 2), (R1, 3, 1)) + f"""    mov rdi, {R0 - 2:#x}
    mov ecx, 4
    mov eax, 0x5a
    rep stosb ;;E self:1:rf,next:1
    mov rsi, {R1 - 1:#x}
    mov rdi, {W + 0x580:#x}
    mov ecx, 3
    rep movsb ;;E self:2:rf
    mov rsi, {R1:#x}
    lodsb ;;E next:2
    mov rdi, {R1:#x}
    mov ecx, 2
    mov al, [{R1:#x}] ;;E next:2
    repe scasb ;;E self:2:rf
    mov rsi, {R1:#x}
    mov rdi, {W + 0x5c0:#x}
    movsq ;;E next:2
"""

Z = W + 0x700
Z2 = Z + 0x100
STACK = arm((Z, 3, 8), (Z2, 1, 8)) + f"""    mov rbp, rsp
    mov rsp, {Z + 8:#x}
    call d_f ;;E at:d_f:1,at:d_back:1
d_back:
    push rax ;;E next:1
    pop rax ;;E next:1
    push 0x2 ;;E next:1
    popfq ;;E next:1
    mov rax, rsp
    push 0x10 ;;E next:1
    push rax
    pushfq
    push 0x08
    lea rax, [rel d_t]
    push rax
    iretq ;;E at:d_t:1
d_t:
    mov rsp, {Z2 + 16:#x}
    int3 ;;E at:s4_v3:2,v3:d_i3
d_i3:
    mov rsp, rbp
    mov word [{Z:#x}], 0x10 ;;E next:1
    mov ss, [{Z:#x}]
    nop ;;E next:1
    mov ss, [{Z:#x}]
    int3 ;;E at:s4_v3:1,v3:d_i4
d_i4:
""" + TF_ON + f"""    mov [{Z:#x}], rax ;;E next:0x4001:tf
    pushfq ;;E next:0x4000:tf
    and qword [rsp], ~0x100 ;;E next:0x4000:tf
    popfq ;;E next:0x4000
    jmp d_end
d_f:
    ret
d_end:
"""

IO = """    mov rax, cr4
    or eax, 8
    mov cr4, rax
""" + arm((0x80, 2, 1), (0x84, 2, 2), (W + 0x900, 1, 1), (DATA, 3, 1)) + f"""    out 0x80, al ;;E next:1
    in al, 0x80 ;;E next:1
    mov dx, 0x7f
    out dx, ax ;;E next:1
    in al, 0x85 ;;E next:2
    out 0x86, al
    in ax, 0x83 ;;E next:2
    mov dx, 0x80
    mov rsi, {DATA:#x}
    mov ecx, 2
    rep outsb ;;E self:9:rf,next:1
    mov rdi, {W + 0x900:#x}
    mov ecx, 1
    rep insb ;;E next:5
    xor eax, eax
    xor edx, edx
    mov byte [{W + 0x900:#x}], 0 ;;E next:4
"""

DATA_PROGRAMS = {f's4-d-len{l}-rw{rw}': matrix_body(rw, l) for rw in (1, 3) for l in (1, 2, 4, 8)}
DATA_PROGRAMS.update({'s4-d-forms': FORMS, 's4-d-reps': REPS, 's4-d-stack': STACK, 's4-d-io': IO})
for _name, _text in DATA_PROGRAMS.items():
    PROGRAMS[_name] = (_text, None, 0)

# The review's regression cases (instruction breakpoints never fire between iterations of a REP string): A, MOV SS shadow
# over a REP MOVSB at an armed address: suppressed, and no #DB mid-instruction; B, REP OUTSB at an armed address: one #DB
# before it, the handler's RF covering every element (port strings commit per element).
ARM0 = lambda label: f'    lea rax, [rel {label}]\n    mov dr0, rax\n    mov eax, 1\n    mov dr7, rax\n'
R_MOVSS_REP = ARM0('a_r') + """    mov ecx, 3
    mov rsi, {data:#x}
    mov rdi, {data:#x}+0x80
    mov eax, ss
    mov ss, eax
a_r:
    rep movsb
    nop
"""
R_REP_OUTS = ARM0('o_r') + """    mov ecx, 3
    mov rsi, {data:#x}
    mov edx, 0x80
o_r:
    rep outsb ;;E self:1
    nop
"""
# (A REP OUTSB under TF: a trap after each element, RF = 1 between them.)
R_TF_OUTS = """    mov ecx, 3
    mov rsi, {data:#x}
    mov edx, 0x80
""" + TF_ON + """    rep outsb ;;E self:0x4000:tf:rf,self:0x4000:tf:rf,next:0x4000:tf
    pushfq ;;E next:0x4000:tf
    and qword [rsp], ~0x100 ;;E next:0x4000:tf
    popfq ;;E next:0x4000
"""

# The resident's own explicit accesses (data breakpoints): FXSAVE/FXRSTOR, XSAVE/XRSTOR images, LGDT/LIDT/SGDT/SIDT,
# MOV Sreg, LLDT, STR, SLDT memory operands, a far CALL's pushes (write-only: a read/write breakpoint on a slot RETF pops
# would meet the trap's own frame, which recurses as on hardware), and PCMPISTRI's memory operand (svc STRING helper; the
# operand is loaded by generated code).
FXA, XSA, G, FS, PP = W + 0x400, W + 0xc00 - 0x800 + 0x800, W + 0x600, W + 0x680, W + 0x6c0
SYS = arm((FXA + 0x20, 1, 8), (XSA + 0x200, 3, 8), (G, 3, 2), (FS, 1, 8)) + f"""    mov rax, cr4
    or eax, 0x40200
    mov cr4, rax
    xor ecx, ecx
    mov eax, 7
    xor edx, edx
    xsetbv
    fxsave [{FXA:#x}] ;;E next:1
    fxrstor [{FXA:#x}]
    mov eax, 7
    xor edx, edx
    xsave [{XSA:#x}] ;;E next:2
    xrstor [{XSA:#x}] ;;E next:2
    sgdt [{G:#x}] ;;E next:4
    lgdt [{G:#x}] ;;E next:4
    sidt [{G:#x}] ;;E next:4
    lidt [{G:#x}] ;;E next:4
    mov word [{G:#x}], 0x10 ;;E next:4
    mov ds, [{G:#x}] ;;E next:4
    mov word [{G:#x}], 0 ;;E next:4
    lldt [{G:#x}] ;;E next:4
    str [{G:#x}] ;;E next:4
    sldt [{G:#x}] ;;E next:4
    pcmpistri xmm0, [{G - 8:#x}], 0 ;;E next:4
    lea rax, [rel f_t]
    mov [{PP:#x}], rax
    mov word [{PP + 8:#x}], 0x08
    mov rbp, rsp
    mov rsp, {FS + 16:#x}
    o64 call far [{PP:#x}] ;;E at:f_t:8
f_back:
    mov rsp, rbp
    xor ecx, ecx
    xor eax, eax
    mov qword [{FS - 40:#x}], rax
    mov qword [{FS - 32:#x}], rax
    mov qword [{FS - 24:#x}], rax
    mov qword [{FS - 16:#x}], rax
    mov qword [{FS - 8:#x}], rax
    jmp f_end
f_t:
    o64 retf
f_end:
"""

# Edges: forms whose operand ends right before, starts right after, or overlaps by one byte an 8-byte read/write range:
# a hit exactly when x86's bytes meet it (FEX's native access must be no wider or narrower than x86's).
E = W + 0xb00
EDGE_FORMS = [('movss xmm0, [{a}]', 4), ('movss [{a}], xmm0', 4), ('movsd xmm0, [{a}]', 8), ('movlps xmm0, [{a}]', 8),
              ('movhps [{a}], xmm0', 8), ('pinsrw xmm0, [{a}], 1', 2), ('pextrw [{a}], xmm0, 1', 2), ('movd xmm0, [{a}]', 4),
              ('movq [{a}], xmm0', 8), ('fld dword [{a}]', 4), ('fistp word [{a}]', 2), ('fnstsw [{a}]', 2), ('fnstcw [{a}]', 2),
              ('cvtsi2sd xmm0, dword [{a}]', 4), ('add [{a}], ax', 2), ('inc byte [{a}]', 1), ('adc qword [{a}], 1', 8),
              ('movnti [{a}], eax', 4), ('cmpxchg8b [{a}]', 8, (E - 8, E, E + 8)), ('lock xadd [{a}], ecx', 4, (E - 4, E + 4, E + 8)),
              ('bswap eax\n    mov [{a}], eax', 4),
              ('movzx eax, word [{a}]', 2), ('movsx rax, byte [{a}]', 1), ('setc byte [{a}]', 1), ('vpbroadcastd xmm0, [{a}]', 4),
              ('vmovss [{a}], xmm0', 4), ('vinsertf128 ymm0, ymm0, [{a}], 1', 16), ('vpmovzxbw xmm0, [{a}]', 8)]


def edges_body():
    lines = [arm((E, 3, 8)).rstrip('\n'), '    mov rax, cr4\n    or eax, 0x40200\n    mov cr4, rax\n    xor ecx, ecx\n    mov eax, 7\n'
             '    xor edx, edx\n    xsetbv\n    fld1\n    fld1\n    fld1\n    fld1']
    for form, k, *at in EDGE_FORMS:
        # (atomics only aligned: a split atomic is a declared refusal, `unsupported_atomic`)
        for a in at[0] if at else (E - k, E - k + 1, E + 7, E + 8):
            hit = a < E + 8 and E < a + k
            text = form.format(a=f'{a:#x}')
            if form.startswith('fistp'):
                text = 'fld1\n    ' + text
            *pre, last = text.split('\n')
            lines += pre + [last + (' ;;E next:1' if hit else '')]
    return '\n'.join(lines) + '\n    fninit\n    vzeroupper\n    xor eax, eax\n    xor ecx, ecx\n    xor edx, edx\n'


DATA_PROGRAMS.update({'s4-r-movss-rep': R_MOVSS_REP, 's4-r-rep-outs': R_REP_OUTS, 's4-r-tf-outs': R_TF_OUTS,
                      's4-d-sys': SYS, 's4-d-edges': edges_body()})
for _name in ('s4-r-movss-rep', 's4-r-rep-outs', 's4-r-tf-outs', 's4-d-sys', 's4-d-edges'):
    PROGRAMS[_name] = (DATA_PROGRAMS[_name], None, 0)

# The part-2 review's cases. An external interrupt (x2APIC self-IPI 21H) whose frame store meets a write breakpoint: the
# #DB comes before the handler's first instruction (as for INT n's frame). Breakpoints on non-canonical ranges (the
# resident's guest-state page in particular: DR0..DR3 at its fields, write and read/write) never match and are not
# programmed as watchpoints; the loop touches the guest state (flags, x87, DF) and must run to its end.
X2APIC = '    mov ecx, 0x1b\n    rdmsr\n    or eax, 0xc00\n    wrmsr\n    mov ecx, 0x80f\n    mov eax, 0x1ff\n    xor edx, edx\n    wrmsr\n'
EXT_FRAME = arm((Z2, 1, 8)) + X2APIC + '    mov ecx, 0x83f\n    mov eax, 0x21\n    xor edx, edx\n    wrmsr\n' + f"""    mov rbp, rsp
    mov rsp, {Z2 + 16:#x}
    sti
    nop ;;E at:s4_v33:1,v33:i_after
i_after:
    cli
    mov rsp, rbp
    xor eax, eax
    mov qword [{Z2 - 192:#x}], rax
    mov qword [{Z2 - 184:#x}], rax
    mov qword [{Z2 - 176:#x}], rax
    mov qword [{Z2 - 168:#x}], rax
    mov qword [{Z2 - 160:#x}], rax
    mov qword [{Z2 - 152:#x}], rax
    mov qword [{Z2 - 144:#x}], rax
    mov qword [{Z2 - 136:#x}], rax
    mov qword [{Z2 - 128:#x}], rax
    mov qword [{Z2 - 120:#x}], rax
    mov qword [{Z2 - 112:#x}], rax
    mov qword [{Z2 - 104:#x}], rax
    mov qword [{Z2 - 96:#x}], rax
    mov qword [{Z2 - 88:#x}], rax
    mov qword [{Z2 - 80:#x}], rax
    mov qword [{Z2 - 72:#x}], rax
    mov qword [{Z2 - 64:#x}], rax
    mov qword [{Z2 - 56:#x}], rax
    mov qword [{Z2 - 48:#x}], rax
    mov qword [{Z2 - 40:#x}], rax
    mov qword [{Z2 - 32:#x}], rax
    mov qword [{Z2 - 24:#x}], rax
    mov qword [{Z2 - 16:#x}], rax
    mov qword [{Z2 - 8:#x}], rax
    mov qword [{Z2:#x}], rax ;;E next:1
    mov qword [{Z2 + 8:#x}], rax
    mov ecx, 0x80b
    xor eax, eax
    xor edx, edx
    wrmsr
"""
# Breakpoints on the guest-state page's EL0 alias (abi GUEST_STATE_VA, G1): DF/flag bytes, x87 C1, mm[0], FCW.
NONCANON = """    mov rax, 0xffff4000000013f8
    mov dr0, rax
    mov rax, 0xffff400000001418
    mov dr1, rax
    mov rax, 0xffff400000001420
    mov dr2, rax
    mov rax, 0xffff4000000014b0
    mov dr3, rax
    mov rax, 0xbbbb00ff
    mov dr7, rax
    mov ecx, 50
n_l:
    pushfq
    popfq
    fld1
    fld1
    faddp
    fstp qword [{data:#x}+0x100]
    std
    cld
    dec ecx
    jnz n_l
"""
DATA_PROGRAMS.update({'s4-d-ext-frame': EXT_FRAME, 's4-d-noncanon': NONCANON})
PROGRAMS['s4-d-ext-frame'] = (EXT_FRAME, None, 0)
PROGRAMS['s4-d-noncanon'] = (NONCANON, None, 0)

"""Program generators for the SMP reference: deterministic programs (a) and litmus programs (b). Output: natemu-smp-v1 programs."""
import pathlib, random, subprocess, sys, tempfile
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))
M64 = (1 << 64) - 1
REGS = ('rax', 'rcx', 'rdx', 'rbx', 'rsp', 'rbp', 'rsi', 'rdi', 'r8', 'r9', 'r10', 'r11', 'r12', 'r13', 'r14', 'r15')
BODY0 = 0x110000
STACK = lambda i: 0x3E1800 + 0x2000 * i          # per-CPU rsp (mapped, not registered, not captured)
LCG_MUL, LCG_INC = 6364136223846793005, 1442695040888963407


def asm(text):
    with tempfile.TemporaryDirectory() as d:
        src, out = pathlib.Path(d, 'b.asm'), pathlib.Path(d, 'b.bin')
        src.write_text('bits 64\ndefault abs\n' + text + '\n'); subprocess.run(['nasm', '-f', 'bin', str(src), '-o', str(out)], check=True)
        return out.read_bytes()

def cpu(i, rip, **regs):
    g = [0] * 16; g[REGS.index('rsp')] = STACK(i)
    for k, v in regs.items(): g[REGS.index(k)] = v & M64
    return {'rip': f'0x{rip:x}', 'gpr': [f'0x{x:x}' for x in g], 'rflags': 0}

def body(addr, text): return {'addr': f'0x{addr:x}', 'code': asm(text).hex()}
def zero(addr, size): return {'addr': f'0x{addr:x}', 'size': size, 'fill': 'zero'}


# ---- (a) deterministic programs: all end with `cmp r11, r11` (all six flags defined: ZF PF set, rest clear) -------------
# Parameters live in the program's "params" and are consumed by model_smp.py, which re-derives everything from them.
def lcg_iters(i): return 200000 + 10000 * i

def disjoint_lcg(n):
    text = """.l: imul rax, rsi
    add rax, rdi
    mov rdx, rax
    shr rdx, 55
    mov r9, [rbx+rdx*8]
    add r9, rax
    mov [rbx+rdx*8], r9
    add r8, r9
    dec rcx
    jnz .l
    cmp r11, r11"""
    cpus = [cpu(i, BODY0, rax=(0x9e3779b97f4a7c15 * (i + 1)) & M64, rbx=0x220000 + 0x10000 * i, rcx=lcg_iters(i), rsi=LCG_MUL, rdi=LCG_INC) for i in range(n)]
    return dict(name=f'disjoint-lcg@{n}', ncpu=n, bodies=[body(BODY0, text)], cpus=cpus, memory=[zero(0x220000 + 0x10000 * i, 4096) for i in range(n)], params={})

def shared_ro(n):
    text = """.l: mov r9, [rbx+rdx]
    imul r8, r8, 31
    add r8, r9
    add rdx, rsi
    and rdx, rdi
    dec rcx
    jnz .l
    mov [r10], r8
    cmp r11, r11"""
    cpus = [cpu(i, BODY0, rbx=0x120000, rcx=100000 + 17 * i, rsi=8 * (2 * i + 1), rdi=0xFFF8, r10=0x210000 + 0x80 * i) for i in range(n)]
    return dict(name=f'shared-ro@{n}', ncpu=n, bodies=[body(BODY0, text)], cpus=cpus,
                memory=[{'addr': '0x120000', 'size': 0x10000, 'fill': 'lcg', 'seed': 1}, zero(0x210000, 0x100)], params={})

HS_D, HS_F, HS_DONE, HS_S, HS_T = 0x230000, 0x231000, 0x231080, 0x232000, 0x233000       # slot j: +0x80*j
def handshake(n):
    check = """    xor ecx, ecx
    xor r8d, r8d
.c: mov rax, [rbx+rcx*8]
    imul r8, rsi
    add r8, rax
    inc ecx
    cmp ecx, 256
    jb .c
    mov [r10], r8"""
    cpu0 = """    xor ecx, ecx
.p: mov rax, rcx
    imul rax, rcx
    add rax, 7
    mov [rbx+rcx*8], rax
    inc ecx
    cmp ecx, 256
    jb .p
    mov qword [rbp], 1
""" + check + """
.j: cmp qword [r13], 0
    jne .n
    pause
    jmp .j
.n: add r13, 0x80
    dec r14
    jnz .j
    xor r8d, r8d
.s: add r8, [r12]
    add r12, 0x80
    dec r15
    jnz .s
    mov [r9], r8
    cmp r11, r11"""
    cons = """.f: cmp qword [rbp], 0
    jne .go
    pause
    jmp .f
.go:
""" + check + """
    mov qword [r13], 1
    cmp r11, r11"""
    cpus = [cpu(0, BODY0, rbx=HS_D, rbp=HS_F, rsi=3, r10=HS_S, r13=HS_DONE + 0x80, r14=n - 1, r12=HS_S, r15=n, r9=HS_T)]
    cpus += [cpu(j, BODY0 + 0x1000, rbx=HS_D, rbp=HS_F, rsi=3 + 2 * j, r10=HS_S + 0x80 * j, r13=HS_DONE + 0x80 * j) for j in range(1, n)]
    return dict(name=f'handshake@{n}', ncpu=n, bodies=[body(BODY0, cpu0), body(BODY0 + 0x1000, cons)], cpus=cpus,
                memory=[zero(HS_D, 0x800), zero(HS_F, 0x400), zero(HS_S, 0x200), zero(HS_T, 0x100)], params={})

TR_T, TR_LOG, TR_R = 0x240000, 0x241000, 1000
def token_ring(n):
    text = """.r: cmp [rbx], r10
    je .go
    pause
    jmp .r
.go: mov [r8], r13
    add r8, r9
    add r13, 0x100
    mov [rbx], r12
    dec rcx
    jnz .r
    cmp r11, r11"""
    cpus = [cpu(i, BODY0, rbx=TR_T, rcx=TR_R, r8=TR_LOG + 8 * i, r9=8 * n, r10=i, r12=(i + 1) % n, r13=i) for i in range(n)]
    return dict(name=f'token-ring@{n}', ncpu=n, bodies=[body(BODY0, text)], cpus=cpus, memory=[zero(TR_T, 0x100), zero(TR_LOG, 0x8000)], params={'rounds': TR_R})

XL_L, XL_C, XL_R = 0x250000, 0x250080, 5000
def xchg_lock(n):
    text = """.again:
.acq: mov eax, 1
    xchg [rbx], eax
    test eax, eax
    jz .got
    pause
    jmp .acq
.got: mov rdx, [rbp]
    add rdx, 1
    mov [rbp], rdx
    mov dword [rbx], 0
    dec rcx
    jnz .again
    xor edx, edx
    cmp r11, r11"""
    cpus = [cpu(i, BODY0, rbx=XL_L, rbp=XL_C, rcx=XL_R) for i in range(n)]
    return dict(name=f'xchg-lock@{n}', ncpu=n, bodies=[body(BODY0, text)], cpus=cpus, memory=[zero(XL_L, 0x100)], params={'iters': XL_R})

DET = (disjoint_lcg, shared_ro, handshake, token_ring, xchg_lock)
def det_all():
    out = []
    for f in DET:
        for n in (2, 3, 4):
            p = f(n); p['format'] = 'natemu-smp-v1'; out.append(p)
    return out


# ---- (b) litmus programs. One DSL for both the QEMU program and the independent model (tools/i4v_ordering_model.py) ---------
SYNC = 0x200000
SEQ = lambda i: 0x000 + 0x80 * i
DONE = lambda i: 0x200 + 0x80 * i
LOC = {'x': 0x400, 'y': 0x480, 'd0': 0x500, 'd1': 0x580}
RES = lambda i: 0x210000 + 0x10000 * i           # per CPU: 2 bytes per iteration (one per output register, in output order)
LITMUS_NAMES = ('sb', 'mp', 'lb', 'wrc', 'iriw', 'sb-mfence', 'sb-xchg', 'iriw-mfence')

def litmus_decl(name):
    import i4v_ordering_model as m
    if name in m.PROFILES: return m.PROFILES[name]
    assert name == 'iriw-mfence'
    return m.Litmus(name, ((('store', 'x', 1),), (('store', 'y', 1),), (('load', 'r0', 'x'), ('fence',), ('load', 'r1', 'y')),
                           (('load', 'r2', 'y'), ('fence',), ('load', 'r3', 'x'))), (('x', 0), ('y', 0)), ((2, 'r0'), (2, 'r1'), (3, 'r2'), (3, 'r3')))

def litmus_cpu_text(test, i, pause):
    n = len(test.programs); mem = dict(test.initial); pz = '        pause\n' if pause else ''
    regs, lines = {}, []
    def r(name):
        if name not in regs: regs[name] = f'r{8 + len(regs)}'
        return regs[name]
    L = [f'    mov ebx, 1', '.top:']
    if i == 0:                                    # CPU 0 resets the locations once everybody finished the previous iteration
        L.append('    lea rax, [rbx-1]')
        for j in range(n): L += [f'.wd{j}: cmp [rdi+{DONE(j)}], rax', f'    jae .od{j}', pz.rstrip(), f'    jmp .wd{j}', f'.od{j}:']
        L += [f'    mov dword [rdi+{LOC[a]}], {v}' for a, v in mem.items()]
    L.append(f'    mov [rdi+{SEQ(i)}], rbx')
    for j in range(n):
        if j != i: L += [f'.ws{j}: cmp [rdi+{SEQ(j)}], rbx', f'    jae .os{j}', pz.rstrip(), f'    jmp .ws{j}', f'.os{j}:']
    # random start offset 0..63 spins (xorshift64 in r14): without it the CPU that arrives last at the barrier always wins the race
    L += ['    mov r13, r14', '    shl r13, 13', '    xor r14, r13', '    mov r13, r14', '    shr r13, 7', '    xor r14, r13', '    mov r13, r14', '    shl r13, 17',
          '    xor r14, r13', '    mov r13d, r14d', '    and r13d, 63', '.dl: dec r13d', '    jns .dl']
    for op in test.programs[i]:
        if op[0] == 'store': assert type(op[2]) is int; L.append(f'    mov dword [rdi+{LOC[op[1]]}], {op[2]}')
        elif op[0] == 'load': L.append(f'    mov {r(op[1])}d, [rdi+{LOC[op[2]]}]')
        elif op[0] == 'fence': L.append('    mfence')
        else: assert op[0] == 'xchg' and type(op[3]) is int; L += [f'    mov {r(op[1])}d, {op[3]}', f'    xchg [rdi+{LOC[op[2]]}], {r(op[1])}d']
    outs = [reg for c, reg in test.outputs if c == i]
    for k, reg in enumerate(outs): L.append(f'    mov [rsi+{k}], {regs[reg]}b')
    L += [f'    mov [rdi+{DONE(i)}], rbx', '    add rsi, 2', '    inc rbx', '    cmp rbx, rdx', '    jbe .top', '    cmp r15, r15']
    return '\n'.join(x for x in L if x != '')

def litmus(name, k, pause=False):
    """-> (program, model Litmus). k iterations per run (k <= 32768); `pause` adds PAUSE to the spin loops (needed for -accel thread=single)."""
    assert 1 <= k <= 32768
    test = litmus_decl(name); n = len(test.programs)
    bodies = [body(BODY0 + 0x1000 * i, litmus_cpu_text(test, i, pause)) for i in range(n)]
    cpus = [cpu(i, BODY0 + 0x1000 * i, rdi=SYNC, rsi=RES(i), rdx=k, r14=0x9e3779b97f4a7c15 * (i + 1)) for i in range(n)]
    mem = [zero(SYNC, 0x1000)] + [zero(RES(i), 2 * k) for i in range(n) if any(c == i for c, _ in test.outputs)]
    return {'format': 'natemu-smp-v1', 'name': f'litmus-{name}' + ('-pause' if pause else ''), 'ncpu': n, 'bodies': bodies, 'cpus': cpus, 'memory': mem,
            'params': {'litmus': name, 'k': k, 'locations': {a: f'0x{SYNC + LOC[a]:x}' for a in dict(test.initial)}}}, test

def litmus_all(k):
    return [litmus(t, k, pause)[0] for t in LITMUS_NAMES for pause in (False, True)]

def litmus_outcomes(prog, test, got, k):
    """Per-iteration outcomes (tuples in the model's `outputs` order) from a parsed dump; checks every CPU finished all k iterations."""
    reg_mem = {m['addr']: m['data'] for m in got['memory']}
    sync = reg_mem[SYNC]
    for i in range(len(test.programs)):
        done = int.from_bytes(sync[DONE(i):DONE(i) + 8], 'little'); assert done == k, f'cpu {i} finished {done} of {k} iterations'
    idx = {}
    for c, reg in test.outputs: idx[(c, reg)] = sum(1 for cc, rr in test.outputs[:test.outputs.index((c, reg))] if cc == c)
    res = {c: reg_mem[RES(c)] for c, _ in test.outputs}
    return [tuple(res[c][2 * it + idx[(c, reg)]] for c, reg in test.outputs) for it in range(k)]


# ---- (c) system programs (CPL0, `system`: QEMU runs them on natemu's CPU model, smp.SYSTEM_CPU) ------------------------------
# Long-mode system instructions: MOV to/from CR0/CR3/CR4, INVLPG, RDMSR/WRMSR EFER. No expected results here: the reference
# is QEMU's run of the same bytes. Each CPU i has its own code page, its own page tables and data pages (disjoint), so the
# programs run at any N. Start-up controls (both QEMU's stub and natemu's driver): CR0 0x80010011, CR4 0x20, EFER 0xd00.
# A CPU's tables T(i, k) (k = 0 A, 1 B) identity-map 0..4 MiB in 4 KiB pages (PML4, PDPT, PD, PT0, PT1: 5 pages): every
# non-leaf entry P|RW|US|A and every leaf P|RW|US|A|D (no walk writes them), except: the stub's pages and every CPU's code
# page are supervisor (bodies may set SMEP; QEMU's fault handler runs there), and the test pages below have A/D clear (so
# a walk's A/D writes are compared) with their own rights and frames. Bodies start with a CR3 load of their table A and
# end by restoring the start-up controls; the restore makes a fall-through end legal for QEMU's capture code.
SYS_CODE = lambda i: BODY0 + 0x1000 * i
SYS_T = lambda i, k: 0x200000 + 0x10000 * i + 0x8000 * k
SYS_D = lambda i: 0x280000 + 0x10000 * i
# Test pages of CPU i (offsets from SYS_D(i)): V (the remapped linear page), its frames F1/F2, R (read-only), X (XD data),
# U (user code), C (XD code).
# SAVE: the start-up CR3 (QEMU's and natemu's differ, so it is kept out of registers and compared memory).
V, F1, F2, R, X, U, C, SAVE = 0x0, 0x1000, 0x2000, 0x3000, 0x4000, 0x5000, 0x6000, 0x7000
P, RW, US, A, D, XD = 1, 2, 4, 0x20, 0x40, 1 << 63
LEAF = P | RW | US | A | D

def sys_leaf(i, k, va):
    """Address of the PTE of `va` (below 4 MiB) in CPU i's table k."""
    return SYS_T(i, k) + 0x3000 + (va >> 12) * 8

def sys_tables(i, k, n, overrides):
    """CPU i's table k as memory regions: only the entries any program here walks (the rest of each table page is never
    read; QEMU leaves it undefined). Only the slice holding the data pages' PTEs is captured (compared)."""
    base = SYS_T(i, k)
    supervisor = set(range(0x100000, 0x110000, 0x1000)) | {SYS_CODE(j) for j in range(n)}
    entry = lambda page: overrides.get(page, page | (LEAF & ~US if page in supervisor else LEAF))
    parent = lambda table: table | P | RW | US | A
    slices = [(base, [parent(base + 0x1000)]), (base + 0x1000, [parent(base + 0x2000)]),
              (base + 0x2000, [parent(base + 0x3000), parent(base + 0x4000)])]
    for lo, hi in ((0x100000, 0x120000), (0x200000, 0x240000), (0x280000, 0x2c0000), (0x3e0000, 0x3f0000)):
        slices.append((base + 0x3000 + (lo >> 12) * 8, [entry(page) for page in range(lo, hi, 0x1000)]))
    return [{'addr': f'0x{a:x}', 'data': b''.join(e.to_bytes(8, 'little') for e in es).hex(), 'capture': a == base + 0x3000 + (0x280000 >> 12) * 8}
            for a, es in slices]

def sys_memory(i, n):
    d = SYS_D(i)
    a = {d + V: (d + F1) | P | RW | US, d + R: (d + R) | P | US | A, d + X: (d + X) | P | RW | US | A | XD,
         d + C: (d + C) | P | RW | A | XD, d + U: (d + U) | P | RW | US | A}
    b = dict(a); b[d + V] = (d + F2) | P | RW | US
    code = asm('mov eax, 0x5a5a\nadd rax, rax\njmp r13').hex()            # U and C: user / XD code (the smep program returns via r13)
    lcg = lambda o, seed: {'addr': f'0x{d + o:x}', 'size': 0x1000, 'fill': 'lcg', 'seed': seed + 16 * i}
    return sys_tables(i, 0, n, a) + sys_tables(i, 1, n, b) + [
        zero(d + V, 0x1000), lcg(F1, 1), lcg(F2, 2), lcg(R, 3), lcg(X, 4),
        {'addr': f'0x{d + U:x}', 'data': code}, {'addr': f'0x{d + C:x}', 'data': code}, dict(zero(d + SAVE, 8), capture=False)]

RESTORE = """    mov eax, 0x20
    mov cr4, rax
    mov eax, 0x80010011
    mov cr0, rax
    mov ecx, 0xC0000080
    mov eax, 0xd00
    xor edx, edx
    wrmsr
    mov rax, [{save:#x}]
    mov cr3, rax
    xor eax, eax"""

def sys_text(i, body):
    """Saves CR3 at SAVE, loads table A, runs `body` (Python format fields: the addresses below) and restores the start-up
    controls (clobbering rax, rcx, rdx: bodies keep their results elsewhere)."""
    f = dict(d=SYS_D(i), ta=SYS_T(i, 0), tb=SYS_T(i, 1), la=sys_leaf(i, 0, SYS_D(i) + V), lb=sys_leaf(i, 1, SYS_D(i) + V),
             V=SYS_D(i) + V, Vrel=SYS_D(i) + V + 0x234 - SYS_CODE(i), F2=SYS_D(i) + F2, R=SYS_D(i) + R, X=SYS_D(i) + X, U=SYS_D(i) + U, C=SYS_D(i) + C, lx=sys_leaf(i, 0, SYS_D(i) + X))
    f['save'] = SYS_D(i) + SAVE
    head = 'bits 64\n    mov rax, cr3\n    mov [{save:#x}], rax\n    mov rax, {ta:#x}\n    mov cr3, rax\n'
    return (head + body + '\n' + RESTORE + '\n    cmp r11, r11').format(**f)

SYS = {
    # Two address spaces mapping V to different frames: loads and stores through each, back to A, CR3 read back.
    'cr3-switch': """    mov rbx, [{V:#x}]
    mov [{V:#x}+8], rbx
    mov rax, {tb:#x}
    mov cr3, rax
    mov r8, [{V:#x}]
    mov [{V:#x}+16], r8
    mov rax, {ta:#x}|0x7e7
    mov cr3, rax
    mov r9, [{V:#x}+8]
    mov rsi, cr3
    mov rax, {ta:#x}
    mov cr3, rax
    mov cr3, rax
    mov rdi, [{V:#x}+16]""",
    # V's PTE rewritten to F2, INVLPG through four operand forms (and a non-canonical one, a no-op), V read after each.
    'invlpg': """    mov rbx, [{V:#x}]
    mov qword [{la:#x}], {F2:#x}|7
    mov rax, {V:#x}
    invlpg [rax]
    mov r13, [{V:#x}+8]
    mov qword [{la:#x}], {d:#x}+0x1000|7
    mov r8, {V:#x}-0x40
    invlpg [r8+0x40]
    mov r14, [{V:#x}+16]
    mov qword [{la:#x}], {F2:#x}|7
    mov rsi, {V:#x}/8
    invlpg [rsi*8]
    mov rdi, [{V:#x}+24]
    mov qword [{la:#x}], {d:#x}+0x1000|7
    invlpg [rel $$+{Vrel:#x}]
    mov r9, [{V:#x}+32]
    mov qword [{la:#x}], {F2:#x}|7
    mov r10, 0x8000000000000000
    invlpg [r10]
    invlpg [{V:#x}]
    mov r12, [{V:#x}+40]
    mov [{V:#x}+48], r12""",
    # CR0.WP: a supervisor write to the read-only page R succeeds with WP=0 (and sets D), faults (#PF 3) with WP=1.
    'wp': """    mov r13, cr0
    mov rax, r13
    btr rax, 16
    mov cr0, rax
    mov qword [{R:#x}], 0x1234
    mov rbx, cr0
    mov cr0, r13
    mov r8, [{R:#x}]
    mov r9, cr0
    mov qword [{R:#x}+8], 0x5678""",
    # EFER.NXE read back and toggled; the XD page X is readable; then with NXE=0 its XD bit is reserved (#PF 9 after a CR3 load).
    'nxe': """    mov ecx, 0xC0000080
    rdmsr
    mov r8, rax
    mov r9, rdx
    mov rbx, [{X:#x}]
    and eax, ~0x800
    wrmsr
    rdmsr
    mov r10, rax
    or eax, 0x800
    wrmsr
    mov rsi, [{X:#x}+8]
    rdmsr
    mov r12, rax
    and eax, ~0x800
    wrmsr
    mov rax, {ta:#x}
    mov cr3, rax
    mov rdi, [{X:#x}+16]""",
    # MOV from CR0/CR3/CR4 and RDMSR EFER; CR4.PGE and SMEP set and cleared with read-backs; supervisor code runs under SMEP.
    'readback': """    mov rbx, cr0
    mov r14, cr3
    mov rbp, cr4
    mov ecx, 0xC0000080
    rdmsr
    mov rsi, rax
    mov rdi, rdx
    mov rax, cr4
    bts rax, 7
    mov cr4, rax
    mov r8, cr4
    bts rax, 20
    mov cr4, rax
    mov r9, cr4
    mov r10, [{V:#x}]
    btr rax, 7
    mov cr4, rax
    mov r12, cr4
    mov r13, 0x1122334455667788
    add r13, r12""",
    # SMEP: a CPL0 fetch from the user page U faults (#PF 0x11); without SMEP it ran first.
    'smep': """    mov rax, {U:#x}
    lea r13, [rel .back]
    jmp rax
.back:
    mov rax, cr4
    bts rax, 20
    mov cr4, rax
    mov rax, {U:#x}
    jmp rax""",
    # NXE: a fetch from the XD page C faults (#PF 0x11).
    'xd-fetch': """    mov rax, {C:#x}
    jmp rax""",
    # A PTE change without INVLPG: the second read of V may see either frame (checked against i4v_coherence_model, not QEMU).
    'pte-no-invlpg': """    mov rbx, [{V:#x}]
    mov qword [{la:#x}], {F2:#x}|7
    mov r8, [{V:#x}]""",
}
def sys_program(name, n=1):
    bodies, cpus, memory = [], [], []
    for i in range(n):
        text = sys_text(i, SYS[name])
        bodies.append(body(SYS_CODE(i), text)); cpus.append(dict(cpu(i, SYS_CODE(i)), cpl=0)); memory += sys_memory(i, n)
    return {'format': 'natemu-smp-v1', 'name': f'sys-{name}@{n}', 'ncpu': n, 'system': True, 'bodies': bodies, 'cpus': cpus,
            'memory': memory, 'params': {'either': name == 'pte-no-invlpg'}}

# Fault cases: one faulting system instruction at CPL0 or CPL3, under the start-up tables and controls (values that would
# differ between QEMU and natemu, such as the start-up CR3, never reach a register before the fault).
SYS_FAULTS = {
    'cr4-reserved-15': 'mov rax, cr4\n    bts rax, 15\n    mov cr4, rax',
    'cr4-reserved-32': 'mov rax, cr4\n    bts rax, 32\n    mov cr4, rax',
    'cr4-pcide': 'mov rax, cr4\n    bts rax, 17\n    mov cr4, rax',
    'cr4-smap': 'mov rax, cr4\n    bts rax, 21\n    mov cr4, rax',
    'cr4-pae-clear': 'mov rax, cr4\n    btr rax, 5\n    mov cr4, rax',
    'cr0-pg-clear': 'mov rax, cr0\n    btr rax, 31\n    mov cr0, rax',
    'cr0-pe-clear': 'mov rax, cr0\n    btr rax, 0\n    mov cr0, rax',
    'cr0-nw-without-cd': 'mov rax, cr0\n    bts rax, 29\n    mov cr0, rax',
    'cr0-high': 'mov rax, cr0\n    bts rax, 32\n    mov cr0, rax',
    'cr3-reserved-63': 'mov rax, 0x8000000000201000\n    mov cr3, rax',
    'cr3-reserved-40': 'mov rax, 0x10000201000\n    mov cr3, rax',
    'lock-mov-cr4': 'mov rax, cr4\n    db 0xf0, 0x0f, 0x22, 0xe0',
    'lock-mov-from-cr3': 'db 0xf0, 0x0f, 0x20, 0xd8',
    'lock-invlpg': 'db 0xf0, 0x0f, 0x01, 0x38',
    'lock-rdmsr': 'mov ecx, 0xC0000080\n    db 0xf0, 0x0f, 0x32',
    'mov-cr1': 'db 0x0f, 0x22, 0xc8',
    'mov-from-cr5': 'db 0x0f, 0x20, 0xe8',
    'efer-reserved-1': 'mov ecx, 0xC0000080\n    rdmsr\n    bts eax, 1\n    wrmsr',
    'efer-reserved-edx': 'mov ecx, 0xC0000080\n    rdmsr\n    mov edx, 1\n    wrmsr',
    'efer-lme-clear': 'mov ecx, 0xC0000080\n    rdmsr\n    btr eax, 8\n    wrmsr',
    'rdmsr-invalid': 'mov ecx, 0x12345678\n    rdmsr',
    'wrmsr-invalid': 'mov ecx, 0x12345678\n    xor eax, eax\n    xor edx, edx\n    wrmsr',
}
SYS_CPL3 = {
    'cpl3-mov-to-cr0': 'mov rax, 0x80010011\n    mov cr0, rax',
    'cpl3-mov-from-cr3': 'mov rax, cr3',
    'cpl3-invlpg': 'invlpg [rsp]',
    'cpl3-rdmsr': 'mov ecx, 0xC0000080\n    rdmsr',
    'cpl3-wrmsr': 'mov ecx, 0xC0000080\n    mov eax, 0xd00\n    xor edx, edx\n    wrmsr',
}

def sys_fault(name):
    cpl3 = name in SYS_CPL3
    case = (SYS_CPL3 if cpl3 else SYS_FAULTS)[name].split('\n')
    # The flags are defined (cmp) right before the instruction under test (bts/btr/xor leave some undefined).
    lines = ['bits 64', 'mov rbx, 0x1234', 'mov rsi, 0x5678'] + case[:-1] + ['cmp rbx, rsi', case[-1]]
    # params.fault_at: the last instruction, the one under test (its prefix runs on QEMU as the fault's state reference).
    prog = {'format': 'natemu-smp-v1', 'name': f'sysfault-{name}@1', 'ncpu': 1, 'system': True,
            'bodies': [body(SYS_CODE(0), '\n'.join(lines))], 'cpus': [dict(cpu(0, SYS_CODE(0)), cpl=3 if cpl3 else 0)], 'memory': [],
            'params': {'fault_at': f"0x{SYS_CODE(0) + len(asm(chr(10).join(lines[:-1]))):x}"}}
    return prog

# ---- (d) concurrent controls (contract 33): CPU 0 rewrites CR3 / CR0.WP / EFER.NXE while its peers run on the same tables ----
# Shared tables: roots A and B (built at CPU 0's addresses) and A2 (a copy of A at CPU 1's); one data area (SYS_D(0)'s test
# pages, A/D preset); two bodies: CPU 0's, and the peers' (SYS_CODE(1), id in rbx). B differs from A: V maps to F2 and U is
# not present. Each round CPU 0 toggles its controls CTL_K times, settles in a state drawn from a fixed generator, logs its
# controls, then publishes GEN = round and waits for every peer's ACK. Peers loop on loads, stores and a fetch through the
# tables (per scenario also reads of the XD page X after an INVLPG, and stores to the read-only page R) until GEN arrives, log
# their own CR0/CR3/EFER and the frame V resolves to, and ACK. A peer that sees CPU 0's controls would log or fault early. Final
# stage: every peer ACKs and makes one access whose outcome depends only on its own controls (R store: #PF 3 with WP=1; fetch
# from the XD page C: #PF 0x11 with NXE=1; read of X: #PF 9 with NXE=0); CPU 0 holds the opposite controls, waits, delays and
# ends by faulting on its own (R store with WP=1, X read with NXE=0, U read under root B). Plain loads and stores only.
CTL_ROUNDS, CTL_K, CTL_SYNC = 10, 6, 0x8000        # SYNC page: GEN +0, ACK[i] +0x40*i, peer cell +0x100+8*i, LOG[i] +0x400+0x200*i (cr0, cr3, efer, V)
CTL_SCENARIOS = {            # name: (CPU 0 toggles, which peers clear their own WP/NXE: None/'all'/'even', CPU counts, CPU 0's last fault)
    'cr3': (('cr3',), None, (2, 4), 'u'),
    'wp-clear': (('wp',), 'all', (2, 4), 'r'),        # peers clear WP: their R stores succeed whatever CPU 0's WP is; CPU 0 faults
    'wp-set': (('wp',), None, (2, 4), 'r'),           # peers keep WP: the final R store faults on each of them
    'wp-mixed': (('wp',), 'even', (4,), 'r'),         # peer 2 clears WP (succeeds), peers 1 and 3 fault
    'nxe-keep': (('nxe',), None, (2, 4), 'x'),
    'nxe-clear': (('nxe',), 'all', (2, 4), 'x'),
    'nxe-mixed': (('nxe',), 'even', (4,), 'x'),
    'all': (('cr3', 'wp', 'nxe'), None, (2, 4), 'r'),
}
CTL_PAGE = 'mov eax, 0x5a5a\nadd rax, rax\nret'

def ctl_clears(scn, j):
    own = CTL_SCENARIOS[scn][1]
    return own == 'all' or (own == 'even' and j % 2 == 0)

def ctl_fields():
    d = SYS_D(0)
    return dict(ta=SYS_T(0, 0), tb=SYS_T(0, 1), a2=SYS_T(1, 0), V=d + V, F1=d + F1, R=d + R, X=d + X, U=d + U, C=d + C, sync=d + CTL_SYNC, cell=d + CTL_SYNC + 0x100, save=d + SAVE)

def ctl_cpu0(scn, n):
    toggles, last = CTL_SCENARIOS[scn][0], CTL_SCENARIOS[scn][3]
    rng, t = random.Random(33), ['bits 64', 'mov rax, cr3', 'mov [{save:#x}], rax', 'mov rax, {ta:#x}', 'mov cr3, rax', 'mov r15, {sync:#x}', 'lea r9, [r15+0x400]']
    peers = lambda which: [j for j in range(1, n) if which(j)]
    ack = lambda lab, r, which=(lambda j: True): ''.join(f'.{lab}{j}:\n    cmp qword [r15+{0x40 * j:#x}], {r}\n    jb .{lab}{j}\n' for j in peers(which))
    delay = lambda lab: f'    mov ecx, 300000\n.{lab}:\n    dec ecx\n    jnz .{lab}\n'
    wp = lambda w: f"    mov rax, cr0\n    {'bts' if w else 'btr'} rax, 16\n    mov cr0, rax\n"
    nxe = lambda x: f"    mov ecx, 0xC0000080\n    rdmsr\n    {'bts' if x else 'btr'} eax, 11\n    wrmsr\n"
    tog = ('    mov rax, {tb:#x}\n    mov cr3, rax\n    mov rax, {a2:#x}\n    mov cr3, rax\n    mov rax, {ta:#x}\n    mov cr3, rax\n    mov cr3, rax\n' if 'cr3' in toggles else '') \
        + ('    mov rax, cr0\n    btc rax, 16\n    mov cr0, rax\n' if 'wp' in toggles else '') \
        + ('    mov ecx, 0xC0000080\n    rdmsr\n    btc eax, 11\n    wrmsr\n' if 'nxe' in toggles else '')
    roots = ['{ta:#x}', '{tb:#x}', '{a2:#x}']
    for r in range(1, CTL_ROUNDS + 1):
        t.append(f'    mov r12d, {CTL_K}\n.t{r}:\n{tog}    dec r12d\n    jnz .t{r}')
        # the round's state (round 1: every toggled control away from its start-up value)
        rt, w, x = rng.randrange(3), rng.randrange(2), rng.randrange(2)
        if r == 1: rt, w, x = 1, 0, 0
        if 'cr3' in toggles: t.append(f'    mov rax, {roots[rt]}\n    mov cr3, rax')
        if 'wp' in toggles: t.append(wp(w).rstrip())
        if 'nxe' in toggles: t.append(nxe(x).rstrip())
        if 'wp' in toggles and not w: t.append(f'    mov qword [{{R:#x}}], {r}')            # a supervisor write to a read-only page: only with WP=0
        if 'nxe' in toggles and x: t.append('    invlpg [{X:#x}]\n    mov rsi, [{X:#x}]')   # a walk of the XD page: only with NXE=1
        t.append(f'    mov qword [{{V:#x}}+8], {r}')
        t.append('    mov rax, cr0\n    mov [r9], rax\n    mov rax, cr3\n    mov [r9+8], rax\n    mov ecx, 0xC0000080\n    rdmsr\n    mov [r9+16], rax\n'
                 '    mov rax, [{V:#x}]\n    mov [r9+24], rax\n    add r9, 32')
        t.append(f'    mov qword [r15], {r}\n' + ack(f'a{r}_', r).rstrip())
    fin = CTL_ROUNDS + 1
    if 'cr3' in toggles: t.append('    mov rax, {tb:#x}\n    mov cr3, rax')              # root B for the end (U not present)
    if 'wp' in toggles: t.append(wp(scn == 'wp-clear').rstrip())    # opposite of the peers' WP (wp-clear: peers 0, CPU 0 1; else CPU 0 0)
    if 'nxe' in toggles: t.append(nxe(0).rstrip())
    t.append(f'    mov qword [r15], {fin}\n' + ack('af', fin).rstrip() + '\n' + delay('df').rstrip())
    f2 = peers(lambda j: 'nxe' in toggles and ctl_clears(scn, j))
    if f2: t.append(nxe(1).rstrip() + f'\n    mov qword [r15], {fin + 1}\n' + ack('ag', fin + 1, lambda j: j in f2).rstrip() + '\n' + delay('dg').rstrip())
    t.append({'u': '    mov rax, [{U:#x}]', 'r': wp(1).rstrip() + '\n    mov [{R:#x}], rbp', 'x': nxe(0).rstrip() + '\n    invlpg [{X:#x}]\n    mov rax, [{X:#x}]'}[last])
    return '\n'.join(t).format(**ctl_fields()), bool(f2)

def ctl_peer(scn, n, has_f2):
    toggles = CTL_SCENARIOS[scn][0]; fin = CTL_ROUNDS + 1
    clear = {'wp': '    mov rax, cr0\n    btr rax, 16\n    mov cr0, rax\n', 'nxe': '    mov ecx, 0xC0000080\n    rdmsr\n    btr eax, 11\n    wrmsr\n'}.get(toggles[0] if len(toggles) == 1 else '', '')
    own = CTL_SCENARIOS[scn][1]
    init = '' if not own else (clear + '    xor r10d, r10d\n' if own == 'all' else '    test bl, 1\n    jnz .keep\n' + clear + '    xor r10d, r10d\n.keep:\n')
    reads_x = '    invlpg [{X:#x}]\n    mov rax, [{X:#x}]\n'
    extra = {'wp': '    test r10, r10\n    jz .w0\n    mov rax, [{R:#x}+rbx*8]\n    jmp .we\n.w0:\n    mov [{R:#x}+rbx*8], rbp\n.we:\n',
             'nxe': '    test r10, r10\n    jz .nx\n' + reads_x + '.nx:\n',
             'all': '    mov rax, [{R:#x}+rbx*8]\n' + reads_x, 'cr3': ''}[scn.split('-')[0]]
    ack = '    mov rax, rbx\n    shl rax, 6\n    mov [r15+rax], r13\n'
    walk_v = '    invlpg [{V:#x}]\n' if 'cr3' in toggles else ''       # a fresh walk of V each time: CPU 0's root would show
    walk_r = '    invlpg [{R:#x}]\n'                                       # and of R before the final store: CPU 0's WP would show
    final = {'wp': walk_r + '    mov [{R:#x}+rbx*8], rbp\n', 'all': walk_r + '    mov [{R:#x}+rbx*8], rbp\n', 'cr3': '',
             'nxe': '    test r10, r10\n    jz .s1\n    mov rax, {C:#x}\n    jmp rax\n.s1:\n'}[scn.split('-')[0]]
    f2 = (f'    inc r13\n.w2:\n    cmp qword [r15], r13\n    jb .w2\n{ack}    test r10, r10\n    jnz .s2\n    invlpg [{{X:#x}}]\n    mov rax, [{{X:#x}}]\n.s2:\n' if has_f2 else '')
    text = f"""bits 64
    mov rax, cr3
    mov [{{save:#x}}+rbx*8], rax
    mov rax, {{ta:#x}}
    mov cr3, rax
    mov r15, {{sync:#x}}
    mov r9, rbx
    shl r9, 9
    lea r9, [r9+r15+0x400]
    xor r14d, r14d
    mov r13d, 1
    mov r10d, 1
{init}.work:
{walk_v}    mov rax, [{{V:#x}}]
    cmp rax, [{{F1:#x}}]
    je .vok
    inc r14
.vok:
    mov [{{cell:#x}}+rbx*8], r13
{extra}    mov rax, {{U:#x}}
    call rax
    cmp qword [r15], r13
    jb .work
    cmp r13, {fin}
    jae .final
    mov rax, cr0
    mov [r9], rax
    mov rax, cr3
    mov [r9+8], rax
    mov ecx, 0xC0000080
    rdmsr
    mov [r9+16], rax
{walk_v}    mov rax, [{{V:#x}}]
    mov [r9+24], rax
    add r9, 32
{ack}    inc r13
    jmp .work
.final:
{ack}{final}{f2}"""
    return (text + """    mov eax, 0x20
    mov cr4, rax
    mov eax, 0x80010011
    mov cr0, rax
    mov ecx, 0xC0000080
    mov eax, 0xd00
    xor edx, edx
    wrmsr
    mov rax, [{save:#x}+rbx*8]
    mov cr3, rax
    xor eax, eax
    cmp r11, r11""").format(**ctl_fields())

def ctl_program(scn, n):
    d = SYS_D(0)
    a = {d + V: (d + F1) | P | RW | US | A | D, d + R: (d + R) | P | US | A | D, d + X: (d + X) | P | RW | US | A | D | XD,
         d + C: (d + C) | P | RW | A | D | XD, d + U: (d + U) | P | RW | US | A | D}
    b = dict(a); b[d + V] = (d + F2) | P | RW | US | A | D; b[d + U] = 0
    lcg = lambda o, seed: {'addr': f'0x{d + o:x}', 'size': 0x1000, 'fill': 'lcg', 'seed': seed}
    code = asm(CTL_PAGE).hex()
    memory = sys_tables(0, 0, n, a) + sys_tables(0, 1, n, b) + sys_tables(1, 0, n, a) + [
        zero(d + V, 0x1000), lcg(F1, 1), lcg(F2, 2), lcg(R, 3), lcg(X, 4), {'addr': f'0x{d + U:x}', 'data': code}, {'addr': f'0x{d + C:x}', 'data': code},
        dict(zero(d + SAVE, 0x20), capture=False), zero(d + CTL_SYNC, 0x1000)]
    text0, has_f2 = ctl_cpu0(scn, n); text1 = ctl_peer(scn, n, has_f2)
    for t in (text0, text1): assert len(asm(t)) < 0x1000 - 8, (scn, len(asm(t)))
    cpus = [dict(cpu(0, SYS_CODE(0), rbp=0xabc0), cpl=0)] + [dict(cpu(j, SYS_CODE(1), rbx=j, rbp=0xabc0 + j), cpl=0) for j in range(1, n)]
    return {'format': 'natemu-smp-v1', 'name': f'sys-ctl-{scn}@{n}', 'ncpu': n, 'system': True, 'bodies': [body(SYS_CODE(0), text0), body(SYS_CODE(1), text1)],
            'cpus': cpus, 'memory': memory, 'params': {'controls': scn, 'rounds': CTL_ROUNDS, 'toggles': CTL_K}}

def system_all():
    out = [sys_program(name, n) for name in SYS for n in ((1, 2, 4) if name not in ('pte-no-invlpg',) else (1,))]
    out += [sys_fault(name) for name in list(SYS_FAULTS) + list(SYS_CPL3)]
    return out + [ctl_program(scn, n) for scn in CTL_SCENARIOS for n in CTL_SCENARIOS[scn][2]]

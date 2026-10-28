"""Program generators for the SMP reference: deterministic programs (a) and litmus programs (b). Output: natemu-smp-v1 programs."""
import pathlib, subprocess, sys, tempfile
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

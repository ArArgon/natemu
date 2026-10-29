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

# I3 contention: every CPU runs LC_R rounds of LOCKed read-modify-writes on shared words of one line pair at r15 (each Arm
# atomic FEX emits: ldaddal, casal, caspal W/X, ldeoral, ldsetal, ldclral, LOCK NEG's one-attempt casal) plus CMPXCHG retry
# loops; every final word is exact (counters n * LC_R, self-inverse pairs back to 0, the OR of each CPU's bit).
LC_C, LC_R = 0x260000, 20000
def lock_counter(n):
    text = """.l: mov eax, 1
    lock xadd [r15], rax
    lock inc qword [r15+8]
    lock add qword [r15+16], 3
    lock sub dword [r15+24], 2
    lock neg qword [r15+32]
    lock neg qword [r15+32]
.cas: mov rax, [r15+40]
    lea rdx, [rax+1]
    lock cmpxchg [r15+40], rdx
    jnz .cas
    lock xor qword [r15+48], r8
    lock xor qword [r15+48], r8
    lock bts qword [r15+56], r9
    lock btr qword [r15+56], r9
    lock or dword [r15+64], r10d
.c16: mov rax, [r15+80]
    mov rdx, [r15+88]
    mov rbx, rax
    add rbx, 1
    mov rcx, rdx
    adc rcx, 0
    lock cmpxchg16b [r15+80]
    jnz .c16
.c8: mov eax, [r15+96]
    mov edx, [r15+100]
    mov ebx, eax
    add ebx, 1
    mov ecx, edx
    adc ecx, 0
    lock cmpxchg8b [r15+96]
    jnz .c8
    dec rsi
    jnz .l
    xor eax, eax
    xor ebx, ebx
    xor ecx, ecx
    xor edx, edx
    cmp r11, r11"""
    cpus = [cpu(i, BODY0, r15=LC_C, rsi=LC_R, r8=0x0123456789abcdef * (i + 1), r9=i, r10=1 << i) for i in range(n)]
    init = bytearray(0x100); init[32] = 1          # the LOCK NEG word starts at 1: its sign flips with every NEG
    return dict(name=f'lock-counter@{n}', ncpu=n, bodies=[body(BODY0, text)], cpus=cpus, memory=[{'addr': f'0x{LC_C:x}', 'data': init.hex()}],
                params={'iters': LC_R})

# I3: LOCK NEG under contention. Every CPU negates one shared qword (starting at 1) NR times, so some compare-and-swaps lose
# races and their instruction exits to its own RIP and runs again (patch 0016); `neg-private` is the same loop on a word of
# each CPU's own (no race). Final word 1 (n * NR negations, NR even). The runner (`smp`) requires the race's self-exit link
# point to have been taken: some CPU links more exits than any CPU of `neg-private` at the same n.
# A start barrier first (each CPU LOCK INCs the dword at NR_C + 0x1c0, r14, then waits for it to reach n), so the loops
# overlap and the race happens whatever the vCPUs' start skew. The wait is one loop of at least 1,000 rounds (ecx counts
# down to 0 and stays; the loop goes on while ecx or the wait is not done): every CPU takes both of its exits, so the
# barrier adds the same linked exits to every CPU and the lost-race comparison below is unaffected.
NR_C, NR_R = 0x270000, 200000
def neg_race(n, private=False):
    text = f"""    lock inc dword [r14]
    mov ecx, 1000
.b: sub ecx, 1
    adc ecx, 0
    mov eax, dword [r14]
    sub eax, {n}
    sar eax, 31
    or eax, ecx
    jnz .b
.l: lock neg qword [r15]
    dec rsi
    jnz .l
    cmp r11, r11"""
    cpus = [cpu(i, BODY0, r15=NR_C + (0x80 * i if private else 0), rsi=NR_R, r14=NR_C + 0x1c0) for i in range(n)]
    init = bytearray(0x200)
    for i in range(n if private else 1): init[0x80 * i] = 1
    return dict(name=f"{'neg-private' if private else 'neg-race'}@{n}", ncpu=n, bodies=[body(BODY0, text)], cpus=cpus,
                memory=[{'addr': f'0x{NR_C:x}', 'data': init.hex()}], params={'iters': NR_R})
def neg_private(n): return neg_race(n, True)

DET = (disjoint_lcg, shared_ro, handshake, token_ring, xchg_lock, lock_counter, neg_race, neg_private)
def det_all():
    out = []
    for f in DET:
        for n in (2, 3, 4):
            p = f(n); p['format'] = 'natemu-smp-v1'; out.append(p)
    return out


# ---- (b) litmus programs. One DSL for both the QEMU program and the independent model (tools/models/ordering_model.py) ---------
SYNC = 0x200000
SEQ = lambda i: 0x000 + 0x80 * i
DONE = lambda i: 0x200 + 0x80 * i
LOC = {'x': 0x400, 'y': 0x480, 'd0': 0x500, 'd1': 0x580}
RES = lambda i: 0x210000 + 0x10000 * i           # per CPU: 2 bytes per iteration (one per output register, in output order)
LITMUS_NAMES = ('sb', 'mp', 'lb', 'wrc', 'iriw', 'sb-mfence', 'sb-xchg', 'iriw-mfence')

def litmus_decl(name):
    from models import ordering_model as m
    if name in m.PROFILES: return m.PROFILES[name]
    assert name == 'iriw-mfence'
    return m.Litmus(name, ((('store', 'x', 1),), (('store', 'y', 1),), (('load', 'r0', 'x'), ('fence',), ('load', 'r1', 'y')),
                           (('load', 'r2', 'y'), ('fence',), ('load', 'r3', 'x'))), (('x', 0), ('y', 0)), ((2, 'r0'), (2, 'r1'), (3, 'r2'), (3, 'r3')))

# I3: the x86 form of a family's LOCKed read-modify-writes, one per Arm word FEX emits for them (LSE, acquire-release):
# ldaddal (xadd), ldclral (and), ldeoral (xor), ldsetal (or), casal (cmpxchg; neg: a load then one casal per pass, exiting
# to its own RIP on a lost race), caspal of two W (cmpxchg8b) or X registers (cmpxchg16b). (model function, operand) of each.
# `-fail`: a comparison that never succeeds (x86 still writes and orders; Arm's CAS then writes nothing).
RMW_FORMS = {'xadd': ('add', 1), 'and': ('and', 0xfffffff3), 'xor': ('xor', 5), 'or': ('or', 4), 'cmpxchg': ('cas', (0, 7)),
             'cmpxchg-fail': ('cas', (1, 7)), 'cmpxchg8b': ('cas', (0, 7)), 'cmpxchg8b-fail': ('cas', (1, 7)), 'cmpxchg16b': ('cas', (0, 7)),
             'neg': ('neg', 0)}

def rmw_text(form, op, loc, dest):
    """x86 of one LOCKed read-modify-write ('rmw', destination, location, function, operand) of form `form` on the dword at
    rdi + `loc`; the old word into `dest` (a 32-bit register name) or nowhere. Scratch: rax, rcx, r12, r15 (rbx and rdx,
    the iteration count and its bound, are saved around cmpxchg8b/16b)."""
    _, _, _, function, operand = op
    assert RMW_FORMS[form][0] == function, (form, function)
    assert dest is None or form in ('xadd', 'cmpxchg', 'cmpxchg-fail', 'cmpxchg8b', 'cmpxchg8b-fail', 'cmpxchg16b'), form   # (and/xor/or/neg return nothing)
    m = f'[rdi+{loc}]'
    if form == 'xadd': return [f'mov {dest or "r12d"}, {operand}', f'lock xadd dword {m}, {dest or "r12d"}']
    if form in ('and', 'xor', 'or'):
        return [f'lock {form} dword {m}, {operand - (1 << 32) if operand >> 31 else operand}']
    if form == 'neg': return [f'lock neg dword {m}']
    exp, des = operand
    form = form.removesuffix('-fail')
    if form == 'cmpxchg':
        return [f'mov eax, {exp}', f'mov r12d, {des}', f'lock cmpxchg dword {m}, r12d'] + ([f'mov {dest}, eax'] if dest else [])
    wide = form == 'cmpxchg16b'    # the dword is the low half of the qword (8b) or oword (16b, 16-byte aligned); the rest is 0
    a, d, b, c = ('rax', 'rdx', 'rbx', 'rcx') if wide else ('eax', 'edx', 'ebx', 'ecx')
    return ['mov r12, rbx', 'mov r15, rdx', f'mov {a}, {exp}', f'xor {d}, {d}', f'mov {b}, {des}', f'xor {c}, {c}',
            f'lock {form} {"oword" if wide else "qword"} {m}'] + ([f'mov {dest}, eax'] if dest else []) + ['mov rbx, r12', 'mov rdx, r15']

def litmus_cpu_text(test, i, pause, locs=LOC, stride=2, fences=True, delay=63, shifts={}, rmw_form=None, vector=False):
    """CPU i's body: `locs` = location -> offset from SYNC, `stride` = result bytes per iteration, `fences` = False drops
    every fence (a positive control), `delay` = mask of the random start delay (spins), `shifts` = location -> bit shift of its
    value (a plain store writes `value << shift`, a load or final read shifts it back, so the changing byte sits in the
    word's high bytes). CPU 0 also records the ('final', location) outputs once every CPU is done."""
    n = len(test.programs); mem = dict(test.initial); pz = '        pause\n' if pause else ''
    regs, lines = {}, []
    def r(name):
        assert name in regs or len(regs) < 4, 'r8..r11 only: r12 is the RMW scratch, r13/r14 the delay'
        if name not in regs: regs[name] = f'r{8 + len(regs)}'
        return regs[name]
    L = [f'    mov ebx, 1', '.top:']
    if i == 0:                                    # CPU 0 resets the locations once everybody finished the previous iteration
        L.append('    lea rax, [rbx-1]')
        for j in range(n): L += [f'.wd{j}: cmp [rdi+{DONE(j)}], rax', f'    jae .od{j}', pz.rstrip(), f'    jmp .wd{j}', f'.od{j}:']
        L += [f'    mov dword [rdi+{locs[a]}], {v << shifts.get(a, 0)}' for a, v in mem.items()]
    L.append(f'    mov [rdi+{SEQ(i)}], rbx')
    for j in range(n):
        if j != i: L += [f'.ws{j}: cmp [rdi+{SEQ(j)}], rbx', f'    jae .os{j}', pz.rstrip(), f'    jmp .ws{j}', f'.os{j}:']
    # random start offset 0..63 spins (xorshift64 in r14): without it the CPU that arrives last at the barrier always wins the race
    L += ['    mov r13, r14', '    shl r13, 13', '    xor r14, r13', '    mov r13, r14', '    shr r13, 7', '    xor r14, r13', '    mov r13, r14', '    shl r13, 17',
          '    xor r14, r13', '    mov r13d, r14d', f'    and r13d, {delay}', '.dl: dec r13d', '    jns .dl']
    for op in test.programs[i]:
        # I5a `vector`: every plain store and load is a 16-byte SSE access holding the value in its low dword: `vector` names
        # the move, MOVDQA (16-byte aligned locations: one atomic X-register pair) or MOVDQU.
        if op[0] == 'store' and vector:
            L += [f'    mov eax, {op[2] << shifts.get(op[1], 0)}', '    movd xmm0, eax', f'    {vector} [rdi+{locs[op[1]]}], xmm0']
        elif op[0] == 'store': assert type(op[2]) is int; L.append(f'    mov dword [rdi+{locs[op[1]]}], {op[2] << shifts.get(op[1], 0)}')
        elif op[0] == 'load':
            if vector: L += [f'    {vector} xmm1, [rdi+{locs[op[2]]}]', f'    movd {r(op[1])}d, xmm1']
            else: L.append(f'    mov {r(op[1])}d, [rdi+{locs[op[2]]}]')
            if shifts.get(op[2]): L.append(f'    shr {r(op[1])}d, {shifts[op[2]]}')
        elif op[0] == 'fence': L.append('    mfence' if fences else '')
        elif op[0] == 'rmw':
            # The positive control (`fences` False) drops the LOCK prefix, so the RMW is neither atomic nor a barrier.
            L += ['    ' + (x if fences else x.replace('lock ', '')) for x in rmw_text(rmw_form, op, locs[op[2]], op[1] and r(op[1]) + 'd')]
        else: assert op[0] == 'xchg' and type(op[3]) is int; L += [f'    mov {r(op[1])}d, {op[3]}', f'    xchg [rdi+{locs[op[2]]}], {r(op[1])}d']
    outs = [reg for c, reg in test.outputs if c == i]
    for k, reg in enumerate(outs): L.append(f'    mov [rsi+{k}], {regs[reg]}b')
    finals = [a for c, a in test.outputs if c == 'final'] if i == 0 else []
    if finals:                                    # every other CPU is done (its stores precede its DONE): the final values
        for j in range(1, n): L += [f'.wf{j}: cmp [rdi+{DONE(j)}], rbx', f'    jae .of{j}', pz.rstrip(), f'    jmp .wf{j}', f'.of{j}:']
        for k, a in enumerate(finals, len(outs)):
            L += [f'    mov eax, [rdi+{locs[a]}]'] + ([f'    shr eax, {shifts[a]}'] if shifts.get(a) else []) + [f'    mov [rsi+{k}], al']
    L += [f'    mov [rdi+{DONE(i)}], rbx', f'    add rsi, {stride}', '    inc rbx', '    cmp rbx, rdx', '    jbe .top', '    cmp r15, r15']
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

def litmus_outcomes(prog, test, got, k, stride=2, res=RES):
    """Per-iteration outcomes (tuples in the model's `outputs` order) from a parsed dump; checks every CPU finished all k iterations."""
    reg_mem = {m['addr']: m['data'] for m in got['memory']}
    sync = reg_mem[SYNC]
    for i in range(len(test.programs)):
        done = int.from_bytes(sync[DONE(i):DONE(i) + 8], 'little'); assert done == k, f'cpu {i} finished {done} of {k} iterations'
    idx = {}     # byte of each output in its CPU's record: CPU 0 records the ('final', location) outputs after its own
    who = lambda c: 0 if c == 'final' else c
    order = sorted(test.outputs, key=lambda o: (who(o[0]), o[0] == 'final'))
    for o in test.outputs: idx[o] = sum(1 for oo in order[:order.index(o)] if who(oo[0]) == who(o[0]))
    data = {who(c): reg_mem[res(who(c))] for c, _ in test.outputs}
    return [tuple(data[who(c)][stride * it + idx[(c, reg)]] for c, reg in test.outputs) for it in range(k)]


# ---- (b2) generated litmus families on the general path (S3e; natemu only, judged by the model) ----------------------------
# Each family is the classic shape generalised to n participants (rings for sb/lb/2+2W, a propagation chain for wrc, n-1
# readers for mp, n exchangers for xchg); the model enumerates the allowed outcomes, nothing is written per test.
# I3: sb-<form> puts one LOCKed RMW of each Arm word (RMW_FORMS) between each store and load, on a location of its own (the
# full-barrier test); `xadd` (n LOCK XADD +1 on one counter: the returned words a permutation and the counter n) and `cas`
# (n LOCK CMPXCHG 0 -> i+1: one winner) are RMW chains.
# `cas-read`/`cas8b-read`: SB where the observing load IS a failing LOCK CMPXCHG / CMPXCHG8B on the next CPU's location
# (store x_i; lock cmpxchg [x_{i+1}] expecting 7, never there): all reading 0 is forbidden. A barrier after the CAS would
# not order its read after the store; natemu's `dmb ish` before it does.
CAS_READ = {'cas-read': 'cmpxchg', 'cas8b-read': 'cmpxchg8b'}
RMW_FAMILIES = tuple(f'sb-{f}' for f in RMW_FORMS) + ('xadd', 'cas') + tuple(CAS_READ)
FAMILIES = ('sb', 'sb-mfence', 'sb-xchg', 'mp', 'lb', 'wrc', 'iriw', '2+2w', 'xchg') + RMW_FAMILIES
VECTOR_FAMILIES = ('vec-sb', 'vec-sb-mfence', 'vec-mp', 'vec-lb', 'vec-wrc', 'vec-iriw', 'vec-2+2w')   # I5a (run with --families)
VARIANTS = ('aligned', 'unaligned', 'split', 'split-hi')

def litmus_family(name, n):
    """The model Litmus of family `name` on n participants, or None when it has no n-participant shape. `vec-F` (I5a) is F
    with every plain load and store a 16-byte SSE access (`family_program`), the same model."""
    from models import ordering_model as m
    if name.startswith('vec-'):
        t = litmus_family(name[4:], n)
        return t and m.Litmus('vec-' + t.name, t.programs, t.initial, t.outputs)
    X = [f'x{i}' for i in range(n)]
    nxt = lambda i: X[(i + 1) % n]
    zero = lambda names: tuple((a, 0) for a in names)
    if name.startswith('sb-') and name[3:] in RMW_FORMS:
        function, operand = RMW_FORMS[name[3:]]
        progs = tuple((('store', X[i], 1), ('rmw', None, f'd{i}', function, operand), ('load', f'r{i}', nxt(i))) for i in range(n))
        return m.Litmus(f'{name}@{n}', progs, zero(X + [f'd{i}' for i in range(n)]), tuple((i, f'r{i}') for i in range(n)))
    if name in CAS_READ:
        progs = tuple((('store', X[i], 1), ('rmw', f'r{i}', nxt(i), 'cas', (7, 9))) for i in range(n))
        return m.Litmus(f'{name}@{n}', progs, zero(X), tuple((i, f'r{i}') for i in range(n)))
    if name == 'xadd':
        return m.Litmus(f'xadd@{n}', tuple((('rmw', f'o{i}', 't', 'add', 1),) for i in range(n)), zero(['t']),
                        tuple((i, f'o{i}') for i in range(n)) + (('final', 't'),))
    if name == 'cas':
        return m.Litmus(f'cas@{n}', tuple((('rmw', f'o{i}', 't', 'cas', (0, i + 1)),) for i in range(n)), zero(['t']),
                        tuple((i, f'o{i}') for i in range(n)) + (('final', 't'),))
    if name in ('sb', 'sb-mfence', 'sb-xchg'):
        mid = {'sb': lambda i: (), 'sb-mfence': lambda i: (('fence',),), 'sb-xchg': lambda i: (('xchg', f'o{i}', f'd{i}', i + 1),)}[name]
        progs = tuple((('store', X[i], 1),) + mid(i) + (('load', f'r{i}', nxt(i)),) for i in range(n))
        extra = [f'd{i}' for i in range(n)] if name == 'sb-xchg' else []
        return m.Litmus(f'{name}@{n}', progs, zero(X + extra), tuple((i, f'r{i}') for i in range(n)))
    if name == 'mp':
        progs = ((('store', 'x0', 1), ('store', 'x1', 1)),) + tuple((('load', 'f', 'x1'), ('load', 'd', 'x0')) for _ in range(1, n))
        return m.Litmus(f'mp@{n}', progs, zero(['x0', 'x1']), tuple(o for j in range(1, n) for o in ((j, 'f'), (j, 'd'))))
    if name == 'lb':
        return m.Litmus(f'lb@{n}', tuple((('load', f'r{i}', X[i]), ('store', nxt(i), 1)) for i in range(n)), zero(X),
                        tuple((i, f'r{i}') for i in range(n)))
    if name == 'wrc' and n >= 3:     # x0 propagates through a chain of n-2 forwarders to the last reader
        progs = ((('store', 'x0', 1),),) + tuple((('load', 'r', X[j - 1]), ('store', X[j], 1)) for j in range(1, n - 1))
        progs += ((('load', 'r', X[n - 2]), ('load', 's', 'x0')),)
        return m.Litmus(f'wrc@{n}', progs, zero(X[:n - 1]), tuple((j, 'r') for j in range(1, n)) + ((n - 1, 's'),))
    if name == 'iriw' and n == 4:
        progs = ((('store', 'x0', 1),), (('store', 'x1', 1),), (('load', 'a', 'x0'), ('load', 'b', 'x1')), (('load', 'a', 'x1'), ('load', 'b', 'x0')))
        return m.Litmus('iriw@4', progs, zero(X[:2]), ((2, 'a'), (2, 'b'), (3, 'a'), (3, 'b')))
    if name == '2+2w':
        return m.Litmus(f'2+2w@{n}', tuple((('store', X[i], 1), ('store', nxt(i), 2)) for i in range(n)), zero(X),
                        tuple(('final', a) for a in X))
    if name == 'xchg':                # one exchange chain: CPU i swaps in token i+1
        return m.Litmus(f'xchg@{n}', tuple((('xchg', f'o{i}', 't', i + 1),) for i in range(n)), zero(['t']),
                        tuple((i, f'o{i}') for i in range(n)) + (('final', 't'),))
    return None

def family_locations(test, variant):
    """Location -> offset from SYNC: `aligned` 32-bit words on lines of their own; `unaligned` 5 bytes in (inside one
    16-byte block); `split` each plain location crossing a page boundary of its own (exchanged locations stay aligned: a
    split XCHG is not supported), its changing byte (byte 0) on the first page; `split-hi` the same placement with the
    changing byte on the second page (byte 3: see `family_shifts`)."""
    xchg = {op[2] for prog in test.programs for op in prog if op[0] in ('xchg', 'rmw')}    # (I3: and LOCKed RMWs)
    wide = test.name.startswith('sb-cmpxchg16b')          # CMPXCHG16B's operand stays 16-byte aligned (else #GP)
    out = {}
    for j, (a, _) in enumerate(test.initial):
        if variant.startswith('split') and a not in xchg: out[a] = 0x1000 * (j + 1) - 2
        else: out[a] = 0x400 + 0x80 * j + (5 if variant == 'unaligned' and not (wide and a in xchg) else 0)
    return out

def family_shifts(test, variant):
    """Location -> bit shift of its value: `split-hi` stores `value << 24` (byte 3, on the second page) in every split
    location, so the half of a split store that lands on the second page is what the loads observe."""
    xchg = {op[2] for prog in test.programs for op in prog if op[0] in ('xchg', 'rmw')}
    return {a: 24 for a, _ in test.initial if variant == 'split-hi' and a not in xchg}

FAMILY_RES = lambda i: 0x220000 + 0x20000 * i
def family_program(test, variant, k, fences=True, delay=255):
    """natemu-smp-v1 program of `test` with locations by `variant`, k iterations (<= 32768 / outputs per CPU)."""
    n, locs, shifts = len(test.programs), family_locations(test, variant), family_shifts(test, variant)
    stride = max(sum(1 for c, _ in test.outputs if (0 if c == 'final' else c) == i) for i in range(n))
    assert 1 <= k and stride * k <= 0x20000
    form = test.name.split('@')[0][3:] if test.name.startswith('sb-') else {'xadd': 'xadd', 'cas': 'cmpxchg', **CAS_READ}.get(test.name.split('@')[0])
    vector = test.name.startswith('vec-') and ('movdqa' if variant == 'aligned' else 'movdqu')
    bodies = [body(BODY0 + 0x1000 * i, litmus_cpu_text(test, i, False, locs, stride, fences, delay, shifts, form, vector)) for i in range(n)]
    cpus = [cpu(i, BODY0 + 0x1000 * i, rdi=SYNC, rsi=FAMILY_RES(i), rdx=k, r14=0x9e3779b97f4a7c15 * (i + 1)) for i in range(n)]
    mem = [zero(SYNC, 0x6000)] + [zero(FAMILY_RES(i), stride * k) for i in range(n)
                                  if any((0 if c == 'final' else c) == i for c, _ in test.outputs)]
    return {'format': 'natemu-smp-v1', 'name': f'{test.name}-{variant}' + ('' if fences else '-nofence'), 'ncpu': n,
            'bodies': bodies, 'cpus': cpus, 'memory': mem, 'params': {'k': k, 'stride': stride}} | ({'sse': True} if vector else {})


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
    # A PTE change without INVLPG: the second read of V may see either frame (checked against coherence_model, not QEMU).
    'pte-no-invlpg': """    mov rbx, [{V:#x}]
    mov qword [{la:#x}], {F2:#x}|7
    mov r8, [{V:#x}]""",
}

# S2a: every CR0 bit the profile makes valid, set and read back (TS/EM with only integer code after them; CD/NW have no
# memory-type effect: V is still read); every valid CR4 bit with CPUID.1:ECX.OSXSAVE (bit 27 only: the leaves differ)
# following CR4.OSXSAVE; CR2 (any value, non-canonical too) and CR8 (bits 3:0); DR0..DR3 any value, DR6/DR7 fixed bits on
# read-back, DR4/DR5 aliasing DR6/DR7 with CR4.DE = 0, DR7 R/W, LEN, LE, GE fields without an enabled breakpoint.
SYS['cr0-bits'] = """    mov r13, cr0
    mov rax, r13
    or eax, 0x2e
    mov cr0, rax
    mov rbx, cr0
    bts rax, 18
    mov cr0, rax
    mov rsi, cr0
    bts rax, 30
    mov cr0, rax
    mov rdi, cr0
    bts rax, 29
    mov cr0, rax
    mov r8, cr0
    mov r9, [{V:#x}]
    btr rax, 3
    mov cr0, rax
    mov r10, cr0
    mov cr0, r13
    mov r12, cr0"""
SYS['cr4-bits'] = """    mov r13, cr4
    mov r14, r13
    bts r14, 3
    mov cr4, r14
    mov rsi, cr4
    bts r14, 8
    mov cr4, r14
    mov rdi, cr4
    or r14, 0x600
    mov cr4, r14
    mov r8, cr4
    bts r14, 16
    mov cr4, r14
    mov r9, cr4
    bts r14, 18
    mov cr4, r14
    mov r10, cr4
    mov eax, 1
    xor ecx, ecx
    cpuid
    shr ecx, 27
    and ecx, 1
    mov r12, rcx
    btr r14, 18
    mov cr4, r14
    mov eax, 1
    xor ecx, ecx
    cpuid
    shr ecx, 27
    and ecx, 1
    mov r15, rcx
    xor ebx, ebx
    mov cr4, r13
    mov rbp, cr4"""
SYS['cr2-cr8'] = """    mov rax, 0xffff888012345678
    mov cr2, rax
    mov rbx, cr2
    mov rax, 0x8000000000000001
    mov cr2, rax
    mov rsi, cr2
    mov rdi, cr8
    mov eax, 15
    mov cr8, rax
    mov r8, cr8
    mov eax, 5
    mov cr8, rax
    mov r9, cr8
    xor eax, eax
    mov cr8, rax
    mov r10, cr8"""
SYS['debug-regs'] = """    mov r9, dr6
    mov r10, dr7
    mov rax, 0xffffffff81000000
    mov dr0, rax
    mov rax, 0x00007fff00001000
    mov dr1, rax
    mov rax, 0x8000000000000000
    mov dr2, rax
    mov rax, {d:#x}
    mov dr3, rax
    mov rbx, dr0
    mov rsi, dr1
    mov rdi, dr2
    mov r8, dr3
    xor eax, eax
    mov dr6, rax
    mov r12, dr6
    mov eax, 0x600f
    mov dr6, rax
    mov r13, dr4
    mov eax, 0xffff0300
    mov dr5, rax
    mov r14, dr7
    xor eax, eax
    mov dr7, rax
    mov r15, dr5
    mov dr0, rax
    mov dr1, rax
    mov dr2, rax
    mov dr3, rax"""
# The kernel head's EFER and CR0 set-up (K arch/x86/kernel/head_64.S:375-401, v6.12.107), transcribed: CPUID 0x80000001
# for NX, EFER.SCE (and NX, already on) through RDMSR/WRMSR, `early_pmd_flags` as a data word, CR0 = CR0_STATE
# (PE MP ET NE WP AM PG). The `pushq $0; popfq` after it is X2's (POPF). EDI (CPUID's EDX: QEMU's CPU differs) is cleared.
SYS['head-cr0'] = """    mov eax, 0x80000001
    cpuid
    mov edi, edx
    mov ecx, 0xC0000080
    rdmsr
    mov edx, eax
    bts eax, 0
    bt edi, 20
    jnc .nonx
    bts eax, 11
    bts qword [{V:#x}+0x100], 63
.nonx:
    cmp eax, edx
    je .same
    xor edx, edx
    wrmsr
.same:
    mov eax, 0x80050033
    mov cr0, rax
    mov rbx, cr0
    mov ecx, 0xC0000080
    rdmsr
    mov rsi, rax
    xor edi, edi"""

def msr_write(index, value):
    return f'    mov ecx, {index:#x}\n    mov rax, {value}\n    mov rdx, rax\n    shr rdx, 32\n    wrmsr\n'

def msr_read(index, reg):
    return f'    mov ecx, {index:#x}\n    rdmsr\n    shl rdx, 32\n    or rax, rdx\n    mov {reg}, rax\n'

def msr_readback(cases, keep_gs=False):
    """WRMSR of each (index, value) then RDMSR of each into rbx, rsi, ... (S1: values QEMU and the profile agree on). With
    `keep_gs` the start-up GS base (QEMU's stub captures through it) is saved at SAVE+8 and restored."""
    regs = ('rbx', 'rsi', 'rdi', 'rbp', 'r8', 'r9', 'r10', 'r11', 'r12', 'r13', 'r14', 'r15')
    save = '    mov ecx, 0xc0000101\n    rdmsr\n    mov [{save:#x}+8], eax\n    mov [{save:#x}+12], edx\n' if keep_gs else ''
    text = save + ''.join(msr_write(i, v) for i, v in cases) + ''.join(msr_read(i, r) for (i, _), r in zip(cases, regs))
    restore = '    mov ecx, 0xc0000101\n    mov eax, [{save:#x}+8]\n    mov edx, [{save:#x}+12]\n    wrmsr\n' if keep_gs else ''
    return text + restore

# Per-CPU values ({d} = the CPU's data base): SYSCALL/SYSENTER targets, segment bases, TSC_AUX (FMASK within its 32 defined
# bits; SYSENTER_CS within 16: QEMU keeps only those, the profile all 64); memory types (PAT, MTRRs).
SYS['msr-syscall'] = msr_readback([
    (0xc0000081, '0x0023001000000000+{d:#x}'), (0xc0000082, '0xffffffff81000000+{d:#x}'), (0xc0000083, '0x00007fff00000000+{d:#x}'),
    (0xc0000084, '0x47700+{d:#x}'), (0x174, '0x10+({d:#x}>>16)'), (0x175, '0xffff888000000000+{d:#x}'), (0x176, '0xffffffff81800000+{d:#x}'),
    (0xc0000100, '0x00007f0000001000+{d:#x}'), (0xc0000101, '0xffff888000002000+{d:#x}'), (0xc0000102, '0x00007f0000003000+{d:#x}'),
    (0xc0000103, '{d:#x}>>16')], keep_gs=True)
SYS['msr-mtrr'] = msr_readback([
    (0x277, '0x0007010600070106'), (0x2ff, '0x806'), (0x200, '0xc0000000'), (0x201, '0xffc0000800'), (0x20e, '0x100000006'),
    (0x20f, '0xff00000800'), (0x250, '0x0606060606060606'), (0x258, '0x0606060606060000'), (0x259, '0x0101010100000000'),
    (0x268, '0x0505050505050505'), (0x26f, '0x0404040404040404')])
def sys_program(name, n=1):
    bodies, cpus, memory = [], [], []
    for i in range(n):
        text = sys_text(i, SYS[name])
        bodies.append(body(SYS_CODE(i), text)); cpus.append(dict(cpu(i, SYS_CODE(i)), cpl=0)); memory += sys_memory(i, n)
    return {'format': 'natemu-smp-v1', 'name': f'sys-{name}@{n}', 'ncpu': n, 'system': True, 'bodies': bodies, 'cpus': cpus,
            'memory': memory, 'params': {'either': name == 'pte-no-invlpg'}}

# ---- X1a: exceptions delivered through the program's own IDT, handled and returned from with IRETQ -------------------------
# CPU i (CPL 0, table A as in sys_text) loads an IDT at DLV(i) whose interrupt gates (CS 0x08, DPL 0, no IST: QEMU's stub and
# natemu's driver both have a 64-bit DPL 0 code segment at 0x08) lead to stubs that push the vector (and 0 where the CPU
# pushes no error code) and a common handler. The handler logs, at DLV(i) + 0x1000 + 0x50 * k for the k-th event, the
# vector, the error code, the frame (RIP, CS, RFLAGS & 0x10cd5: the arithmetic flags, DF and RF; RSP, SS), the frame's
# address and CR2 for #PF; then fixes up as Linux's early handlers do and IRETQs: INT3 and INT 0x21 return after the
# instruction; UD2 (WARN/BUG) is skipped; a #PF on V maps V again in table A (on-demand paging, as the decompressor and the
# kernel's early #PF handler); a WRMSR the profile and pinned QEMU refuse with #GP(0) (IA32_APIC_BASE with reserved bit 9 set,
# as a wrmsr_safe probe; QEMU accepts unknown MSRs, so rdmsr_safe's PLATFORM_INFO probe is a `run_slice3_vm.py delivery`
# case) is skipped with r13 = -1;
# DIV by 0 (#DE) is skipped. INT3 runs once more with DF set. Registers and the log are compared with QEMU;
# tools/run_slice3_vm.py checks the logged frames against delivery_model. (Pinned QEMU keeps RF after an IRETQ that loaded
# it until a later event, so an INT3 after a fault's return logs RF = 1 there; the SDM clears RF when the next instruction
# completes and INT n pushes RF = 0: no INT follows a fault's return here.)
DLV = lambda i: SYS_D(i) + 0x8000
DLV_VECTORS = (0, 3, 6, 13, 14, 0x21)
DLV_BODY = """    sidt [{idt:#x}+0x810]
    lidt [{idt:#x}+0x800]
    xor r13d, r13d
    mov r12, 0x1234
    add r12, r12
    int3
    int 0x21
    std
    int3
    cld
    ud2
    and qword [{la:#x}], -2
    invlpg [{V:#x}]
    mov rbx, [{V:#x}+8]
    and qword [{la:#x}], -2
    invlpg [{V:#x}]
    mov [{V:#x}+16], rbx
    mov ecx, 0x1b
    mov eax, 0xfee00a00
    xor edx, edx
    wrmsr
    xor ecx, ecx
    mov eax, 7
    xor edx, edx
    div rcx
    lidt [{idt:#x}+0x810]
    jmp dlv_end
dlv_common:
    mov r15, [{idt:#x}+0x820]
    mov r14, [rsp]
    mov [r15], r14
    mov r14, [rsp+8]
    mov [r15+8], r14
    mov r14, [rsp+16]
    mov [r15+16], r14
    mov r14, [rsp+24]
    mov [r15+24], r14
    mov r14, [rsp+32]
    and r14, 0x10cd5
    mov [r15+32], r14
    mov r14, [rsp+40]
    mov [r15+40], r14
    mov r14, [rsp+48]
    mov [r15+48], r14
    lea r14, [rsp+16]
    mov [r15+56], r14
    lea r14, [r15+0x50]
    mov [{idt:#x}+0x820], r14
    mov r14, [rsp]
    cmp r14, 14
    jne dlv_n14
    mov r14, cr2
    mov [r15+64], r14
    or qword [{la:#x}], 1
dlv_n14:
    cmp r14, 6
    jne dlv_n6
    add qword [rsp+16], 2
dlv_n6:
    cmp r14, 13
    jne dlv_n13
    add qword [rsp+16], 2
    mov r13, -1
dlv_n13:
    test r14, r14
    jnz dlv_n0
    add qword [rsp+16], 3
dlv_n0:
    add rsp, 16
    iretq
""" + ''.join(f"""dlv_v{v}:
{'' if v in (13, 14) else '    push 0' + chr(10)}    push {v}
    jmp dlv_common
""" for v in DLV_VECTORS) + "dlv_end:\n"

def dlv_program(n):
    """`dlv-handlers@n`: every CPU runs DLV_BODY with its own IDT, log and table A."""
    bodies, cpus, memory = [], [], []
    for i in range(n):
        d, idt = SYS_D(i), DLV(i)
        f = dict(idt=idt, la=sys_leaf(i, 0, d + V), V=d + V)
        text = sys_text(i, DLV_BODY.replace('{idt:#x}', f'{idt:#x}').replace('{la:#x}', f"{f['la']:#x}").replace('{V:#x}', f"{f['V']:#x}"))
        _, labels = ref_assemble(text)
        gates = b''
        for v in range(0x22):
            off = SYS_CODE(i) + labels[f'dlv_v{v}'] if v in DLV_VECTORS else 0
            lo = off & 0xffff | 0x08 << 16 | (0x8e if off else 0) << 40 | (off >> 16 & 0xffff) << 48
            gates += lo.to_bytes(8, 'little') + (off >> 32).to_bytes(8, 'little')
        idtr = (0x22 * 16 - 1).to_bytes(2, 'little') + idt.to_bytes(8, 'little')
        bodies.append(body(SYS_CODE(i), text)); cpus.append(dict(cpu(i, SYS_CODE(i)), cpl=0)); memory += sys_memory(i, n)
        memory += [{'addr': f'0x{idt:x}', 'data': gates.hex()},
                   # The IDTR operand, the start-up IDTR saved by SIDT (QEMU's stub's, natemu's driver's: they differ) and the
                   # log cursor (r15 holds its last value): not compared.
                   {'addr': f'0x{idt + 0x800:x}', 'data': (idtr + bytes(6 + 16) + (idt + 0x1000).to_bytes(8, 'little')).hex(), 'capture': False},
                   zero(idt + 0x1000, 0x400)]
    return {'format': 'natemu-smp-v1', 'name': f'dlv-handlers@{n}', 'ncpu': n, 'system': True, 'bodies': bodies, 'cpus': cpus,
            'memory': memory, 'params': {'delivery': [DLV(i) + 0x1000 for i in range(n)]}}

def ref_assemble(text):
    """nasm's bytes and its labels (from a listing): {label: offset}."""
    import re
    with tempfile.TemporaryDirectory() as d:
        src, out, lst = pathlib.Path(d, 'b.asm'), pathlib.Path(d, 'b.bin'), pathlib.Path(d, 'b.lst')
        src.write_text('bits 64\ndefault abs\n' + text + '\n')
        subprocess.run(['nasm', '-f', 'bin', str(src), '-o', str(out), '-l', str(lst)], check=True)
        labels, lines = {}, lst.read_text().splitlines()
        for k, line in enumerate(lines):
            m = re.match(r'\s*\d+\s+(?:([0-9A-F]{8})\s+\S+\s+)?([A-Za-z_]\w*):\s*$', line)
            if m:
                at = next(int(x.split()[1], 16) for x in lines[k + 1:] if re.match(r'\s*\d+\s+[0-9A-F]{8}\s', x))
                labels[m.group(2)] = at
        return out.read_bytes(), labels

# Fault cases: one faulting system instruction at CPL0 or CPL3, under the start-up tables and controls (values that would
# differ between QEMU and natemu, such as the start-up CR3, never reach a register before the fault).
SYS_FAULTS = {
    'cr4-reserved-15': 'mov rax, cr4\n    bts rax, 15\n    mov cr4, rax',
    'cr4-reserved-32': 'mov rax, cr4\n    bts rax, 32\n    mov cr4, rax',
    'cr4-pcide': 'mov rax, cr4\n    bts rax, 17\n    mov cr4, rax',
    'cr4-smap': 'mov rax, cr4\n    bts rax, 21\n    mov cr4, rax',
    'cr4-pae-clear': 'mov rax, cr4\n    btr rax, 5\n    mov cr4, rax',
    # Profile cases (tools/models/cpu_profile.json via system_fault_model): CR4 bits whose CPUID feature is clear, and WRMSR/RDMSR the profile refuses.
    'cr4-vme': 'mov rax, cr4\n    bts rax, 0\n    mov cr4, rax',
    'cr4-pvi': 'mov rax, cr4\n    bts rax, 1\n    mov cr4, rax',
    'cr4-mce': 'mov rax, cr4\n    bts rax, 6\n    mov cr4, rax',
    'cr4-umip': 'mov rax, cr4\n    bts rax, 11\n    mov cr4, rax',
    'cr4-la57': 'mov rax, cr4\n    bts rax, 12\n    mov cr4, rax',
    'cr4-vmxe': 'mov rax, cr4\n    bts rax, 13\n    mov cr4, rax',
    'cr4-smxe': 'mov rax, cr4\n    bts rax, 14\n    mov cr4, rax',
    'cr4-pke': 'mov rax, cr4\n    bts rax, 22\n    mov cr4, rax',
    'wrmsr-fsbase-noncanonical': 'mov ecx, 0xC0000100\n    mov eax, 0\n    mov edx, 0x8000\n    wrmsr',
    'wrmsr-lstar-noncanonical': 'mov ecx, 0xC0000082\n    mov eax, 0\n    mov edx, 0x8000\n    wrmsr',
    'wrmsr-sysenter-eip-noncanonical': 'mov ecx, 0x176\n    mov eax, 0\n    mov edx, 0x8000\n    wrmsr',
    'wrmsr-tsc-aux-high': 'mov ecx, 0xC0000103\n    mov eax, 0\n    mov edx, 1\n    wrmsr',
    'wrmsr-pat-bad-type': 'mov ecx, 0x277\n    mov eax, 2\n    mov edx, 0\n    wrmsr',
    'wrmsr-misc-enable-eist': 'mov ecx, 0x1a0\n    mov eax, 0x1801 | 1 << 16\n    mov edx, 0\n    wrmsr',
    'wrmsr-mtrr-def-type-reserved': 'mov ecx, 0x2ff\n    mov eax, 0x206\n    mov edx, 0\n    wrmsr',
    'wrmsr-mtrr-base-bad-type': 'mov ecx, 0x200\n    mov eax, 2\n    mov edx, 0\n    wrmsr',
    'wrmsr-ucode-rev-low': 'mov ecx, 0x8b\n    mov eax, 1\n    mov edx, 0\n    wrmsr',
    'wrmsr-bios-updt-trig': 'mov ecx, 0x79\n    mov eax, 0\n    mov edx, 0\n    wrmsr',
    'wrmsr-apic-base-extd-without-en': 'mov ecx, 0x1b\n    mov eax, 0xfee00400\n    mov edx, 0\n    wrmsr',
    'rdmsr-platform-info': 'mov ecx, 0xce\n    rdmsr',
    'rdmsr-tsc-deadline': 'mov ecx, 0x6e0\n    rdmsr',
    'rdmsr-bios-updt-trig': 'mov ecx, 0x79\n    rdmsr',
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
    # S1: more of the profile's MSR rules, and LOCK on the unprivileged system instructions.
    'wrmsr-mtrrcap': 'mov ecx, 0xfe\n    mov eax, 0x508\n    xor edx, edx\n    wrmsr',
    'wrmsr-platform-id': 'mov ecx, 0x17\n    xor eax, eax\n    xor edx, edx\n    wrmsr',
    'wrmsr-mtrr-mask-reserved': 'mov ecx, 0x201\n    mov eax, 0x801\n    mov edx, 0xff\n    wrmsr',
    'wrmsr-mtrr-fixed-bad-type': 'mov ecx, 0x250\n    mov eax, 0x06020606\n    mov edx, 0x06060606\n    wrmsr',
    'wrmsr-pat-bad-high-byte': 'mov ecx, 0x277\n    mov eax, 0x00070406\n    mov edx, 0x08070406\n    wrmsr',
    'wrmsr-kernel-gs-noncanonical': 'mov ecx, 0xC0000102\n    mov eax, 0\n    mov edx, 0xffff7fff\n    wrmsr',
    'wrmsr-gs-base-noncanonical': 'mov ecx, 0xC0000101\n    mov eax, 0\n    mov edx, 0x00008000\n    wrmsr',
    'wrmsr-sysenter-esp-noncanonical': 'mov ecx, 0x175\n    mov eax, 0\n    mov edx, 0x00010000\n    wrmsr',
    'wrmsr-apic-base-reserved': 'mov ecx, 0x1b\n    mov eax, 0xfee00a00\n    xor edx, edx\n    wrmsr',
    'lock-cpuid': 'xor eax, eax\n    db 0xf0, 0x0f, 0xa2',
    'lock-rdtsc': 'db 0xf0, 0x0f, 0x31',
    'wrmsr-invalid': 'mov ecx, 0x12345678\n    xor eax, eax\n    xor edx, edx\n    wrmsr',
    # S2a: CR8 reserved bits, DR8 (REX.R), LOCK, DR6/DR7 (and DR5's alias) bits 63:32, DR4/DR5 with CR4.DE, general
    # detect on a read and a write, RDPMC without counters (SYS_FAULT_CONTROLS: the state a prefix sets).
    'cr8-reserved': 'mov eax, 0x10\n    mov cr8, rax',
    'mov-from-dr8': 'db 0x44, 0x0f, 0x21, 0xc0',
    'lock-mov-from-dr7': 'db 0xf0, 0x0f, 0x21, 0xf8',
    'dr6-high': 'mov rax, 1 << 32\n    mov dr6, rax',
    'dr7-high': 'mov rax, 1 << 32\n    mov dr7, rax',
    'dr5-high': 'mov rax, 1 << 63\n    mov dr5, rax',
    'dr4-de': 'mov rax, cr4\n    bts rax, 3\n    mov cr4, rax\n    mov rax, dr4',
    'dr5-de-write': 'mov rax, cr4\n    bts rax, 3\n    mov cr4, rax\n    xor eax, eax\n    mov dr5, rax',
    'dr7-gd-read': 'mov eax, 0x2400\n    mov dr7, rax\n    mov rax, dr0',
    'dr7-gd-write': 'mov eax, 0x2400\n    mov dr7, rax\n    mov dr7, rax',
    'rdpmc': 'xor ecx, ecx\n    rdpmc',
    'lock-rdpmc': 'xor ecx, ecx\n    db 0xf0, 0x0f, 0x33',
}
# Controls a fault case's prefix sets (CR0/CR4/EFER/DR7 otherwise the start-up values), given to system_fault_model.
SYS_FAULT_CONTROLS = {'dr4-de': {'cr4': 0x28}, 'dr5-de-write': {'cr4': 0x28}, 'dr7-gd-read': {'dr7': 0x2400}, 'dr7-gd-write': {'dr7': 0x2400}}
SYS_CPL3 = {
    'cpl3-mov-to-cr0': 'mov rax, 0x80010011\n    mov cr0, rax',
    'cpl3-mov-from-cr3': 'mov rax, cr3',
    'cpl3-invlpg': 'invlpg [rsp]',
    'cpl3-rdmsr': 'mov ecx, 0xC0000080\n    rdmsr',
    'cpl3-wrmsr': 'mov ecx, 0xC0000080\n    mov eax, 0xd00\n    xor edx, edx\n    wrmsr',
    'cpl3-mov-from-cr2': 'mov rax, cr2',
    'cpl3-mov-to-cr8': 'xor eax, eax\n    mov cr8, rax',
    'cpl3-mov-from-dr7': 'mov rax, dr7',
    'cpl3-mov-to-dr0': 'xor eax, eax\n    mov dr0, rax',
    'cpl3-rdpmc': 'xor ecx, ecx\n    rdpmc',
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
    if name in SYS_FAULT_CONTROLS: prog['params']['controls'] = SYS_FAULT_CONTROLS[name]
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
    out += [dlv_program(n) for n in (1, 2, 4)]
    out += [sys_fault(name) for name in list(SYS_FAULTS) + list(SYS_CPL3)]
    return out + [ctl_program(scn, n) for scn in CTL_SCENARIOS for n in CTL_SCENARIOS[scn][2]]


# ---- (e) cross-vCPU code coherence (S3d): code written at run time by one CPU and executed by others -----------------------
# No expected results here: the reference is QEMU's run of the same bytes (QEMU keeps translated code coherent with stores).
# A page that holds run-time code (CC_PAGE(k)) has a one-byte body at +0 (natemu's driver then maps it writable and
# executable: a memory region covering a code page leaves it executable) and a captured memory region from +0x10, so its
# final bytes are compared. Run-time code is entered with `jmp` and leaves with `jmp r13` (no stack). Handshakes are plain
# stores and loads on flag words (CC_SYNC: +0x80*i per CPU and per purpose); every CPU ends with `cmp r11, r11`, and every
# register a CPU ends with is independent of how often its spin loops went round.
CC_PAGE = lambda k: 0x130000 + 0x1000 * k
CC_SYNC = 0x140000
CC_GEN, CC_ACK, CC_DONE = CC_SYNC, CC_SYNC + 0x400, CC_SYNC + 0x800      # +0x80*i each
CC_FN = 0x40                                                             # offset of the function in its page

def cc_function(r):
    """Variant r of the run-time function: rax = rbx * (7r + 3) + r, then back through r13 (3 qwords)."""
    code = asm(f'imul rax, rbx, {7 * r + 3}\nadd rax, {r}\njmp r13')
    return code + b'\x90' * (24 - len(code))

def cc_write(at, code):
    """Stores of `code` (a multiple of 8 bytes) at `at`, a qword at a time through rax."""
    return ''.join(f'    mov rax, 0x{int.from_bytes(code[k:k + 8], "little"):x}\n    mov [0x{at + k:x}], rax\n' for k in range(0, len(code), 8))

def cc_wait(flag, value, label):
    return f'.{label}:\n    cmp qword [0x{flag:x}], {value}\n    jb .{label}\n'

def cc_call(fn, label, serialize=False):
    """rax = fn(rbx) (rbx set by the caller), folded into r12; `serialize` loads CR3 with its own value first."""
    s = '    mov rax, cr3\n    mov cr3, rax\n' if serialize else ''
    return s + f'    lea r13, [rel .{label}]\n    mov rax, 0x{fn:x}\n    jmp rax\n.{label}:\n    rol r12, 7\n    xor r12, rax\n'

def cc_page(k, data=b''):
    """Body and captured region of run-time code page k (`data`: the region's first bytes, the rest zero)."""
    return [body(CC_PAGE(k), 'ret')], [{'addr': f'0x{CC_PAGE(k) + 0x10:x}', 'data': (data + bytes(0xff0 - len(data))).hex()}]

def cc_program(name, n, texts, pages, regs=None, cpl=3, memory=()):
    bodies, mem = [], [zero(CC_SYNC, 0x1000)] + list(memory)
    for k, data in pages:
        b, m = cc_page(k, data); bodies += b; mem += m
    cpus = []
    for i, t in enumerate(texts):
        bodies.append(body(BODY0 + 0x1000 * i, t + '\n    cmp r11, r11'))
        cpus.append(dict(cpu(i, BODY0 + 0x1000 * i, **(regs(i) if regs else {})), cpl=cpl))
    return {'format': 'natemu-smp-v1', 'name': f'cc-{name}@{n}', 'ncpu': n, 'bodies': bodies, 'cpus': cpus, 'memory': mem, 'params': {'coherence': name}}

CC_ROUNDS = 6
def cc_fresh(n, kind):
    """CPU 0 writes variant r of a function into a page, then publishes GEN = r; CPUs 1.. wait for it, run the function,
    acknowledge; CPU 0 waits for every acknowledgement before the next variant. `new`: the page held nothing the others
    touched; `reused`: the others first stored to and loaded from a data word on that page (their writable shadows of it
    must go when they translate code from it); `cr3`: as `new`, each CPU loading CR3 with its own value (a serialising
    instruction) before running the function (CPL0)."""
    k = {'new': 0, 'reused': 1, 'cr3': 2}[kind]
    fn = CC_PAGE(k) + CC_FN
    w = ''
    for r in range(1, CC_ROUNDS + 1):
        w += ''.join(cc_wait(CC_ACK + 0x80 * j, r - 1, f'a{r}_{j}') for j in range(1, n))
        w += cc_write(fn, cc_function(r)) + f'    mov qword [0x{CC_GEN:x}], {r}\n'
    w += ''.join(cc_wait(CC_ACK + 0x80 * j, CC_ROUNDS, f'af_{j}') for j in range(1, n))
    def jumper(i):
        t = f'    mov [0x{CC_PAGE(k) + 0x800 + 8 * i:x}], rbx\n    mov r8, [0x{CC_PAGE(k) + 0x800 + 8 * i:x}]\n' if kind == 'reused' else ''
        for r in range(1, CC_ROUNDS + 1):
            t += cc_wait(CC_GEN, r, f'w{r}') + cc_call(fn, f'b{r}', kind == 'cr3') + f'    mov qword [0x{CC_ACK + 0x80 * i:x}], {r}\n'
        return t
    return cc_program(f'fresh-{kind}', n, [w] + [jumper(i) for i in range(1, n)], [(k, b'')], regs=lambda i: {'rbx': 0x1234567 * (i + 1)},
                      cpl=0 if kind == 'cr3' else 3)

def cc_rounds(n):
    """Every CPU in turn patches one shared function (round r: CPU (r - 1) % n writes variant r, after every CPU acknowledged
    round r - 1), then all CPUs run it and acknowledge."""
    fn = CC_PAGE(3) + CC_FN
    def text(i):
        t = ''
        for r in range(1, 3 * n + 1):
            if (r - 1) % n == i:
                t += ''.join(cc_wait(CC_ACK + 0x80 * j, r - 1, f'a{r}_{j}') for j in range(n))
                t += cc_write(fn, cc_function(r)) + f'    mov qword [0x{CC_GEN:x}], {r}\n'
            t += cc_wait(CC_GEN, r, f'w{r}') + cc_call(fn, f'b{r}') + f'    mov qword [0x{CC_ACK + 0x80 * i:x}], {r}\n'
        return t
    return cc_program('rounds', n, [text(i) for i in range(n)], [(3, b'')], regs=lambda i: {'rbx': 0x9e3779b9 * (i + 1)})

def cc_loop(k, i, iters, extra=''):
    """Run-time loop code for page k (at +CC_FN): `iters` rounds of an ALU mix on r8/r9 (plus `extra` per round), then back
    through r13."""
    return asm(f"""    mov ecx, {iters}
.l: add r8, rcx
    imul r9, r8, 13
    xor r8, r9
    rol r8, 3
{extra}    dec ecx
    jnz .l
    jmp r13""")

def cc_data(n, iters=20000):
    """CPU 0 runs a hot loop from page 4 (`iters` rounds); once it announced its start, CPUs 1.. store to their own word on
    that same page until CPU 0 announces its end (data on a peer's code page: every such store that faults posts the page of
    CPU 0's running block), then acknowledge; CPU 0 waits for every acknowledgement and reads the words."""
    loop = cc_loop(4, 0, iters)
    t0 = f'    mov qword [0x{CC_GEN:x}], 1\n    lea r13, [rel .back]\n    mov rax, 0x{CC_PAGE(4) + CC_FN:x}\n    jmp rax\n.back:\n'
    t0 += f'    mov qword [0x{CC_DONE:x}], 1\n'
    t0 += ''.join(cc_wait(CC_ACK + 0x80 * j, 1, f'd{j}') + f'    add r10, [0x{CC_PAGE(4) + 0x800 + 8 * j:x}]\n' for j in range(1, n))
    def writer(i):
        return cc_wait(CC_GEN, 1, 'w') + f""".s: mov [0x{CC_PAGE(4) + 0x800 + 8 * i:x}], rbx
    cmp qword [0x{CC_DONE:x}], 0
    je .s
    mov qword [0x{CC_ACK + 0x80 * i:x}], 1
"""
    return cc_program('data', n, [t0] + [writer(i) for i in range(1, n)], [(4, bytes(CC_FN - 0x10) + loop)], regs=lambda i: {'rbx': 0x1111 * (i + 1)})

def cc_rewrite(n, iters=3000):
    """CPU 0 runs a loop from page 5; CPUs 1.. store the loop's own bytes back over it (the same values) until CPU 0
    announces its end: a page rewritten on every fetch."""
    loop = cc_loop(5, 0, iters)
    code = loop + bytes(-len(loop) % 8)
    t0 = f'    lea r13, [rel .back]\n    mov rax, 0x{CC_PAGE(5) + CC_FN:x}\n    jmp rax\n.back:\n    mov qword [0x{CC_DONE:x}], 1\n'
    at = CC_PAGE(5) + CC_FN
    def writer(i):
        body_ = ''.join(f'    mov rax, [0x{at + k:x}]\n    mov [0x{at + k:x}], rax\n' for k in range(0, len(code), 8))
        return f'.r:\n{body_}    cmp qword [0x{CC_DONE:x}], 0\n    je .r\n'
    return cc_program('rewrite', n, [t0] + [writer(i) for i in range(1, n)], [(5, bytes(CC_FN - 0x10) + code)])

def cc_mutual(n, iters=300000):
    """CPU i runs a loop from page 6 + i that stores its counter into page 6 + (i + 1) % n each round: every CPU translates
    a page another CPU writes (with a writable shadow of it), so they shoot each other's shadows down."""
    def page(i):
        return bytes(CC_FN - 0x10) + cc_loop(6 + i, i, iters, f'    mov [0x{CC_PAGE(6 + (i + 1) % n) + 0x800 + 8 * i:x}], rcx\n')
    t = lambda i: f'    lea r13, [rel .back]\n    mov rax, 0x{CC_PAGE(6 + i) + CC_FN:x}\n    jmp rax\n.back:\n'
    return cc_program('mutual', n, [t(i) for i in range(n)], [(6 + i, page(i)) for i in range(n)])

def cc_finish(n, late):
    """CPUs 1.. store to a data word on page 10 (holding a writable shadow of it), publish their flag and end (`late`: after a
    delay loop); CPU 0 waits for the flags and runs the function on page 10, translating it after the writers ended (their
    writable shadows went with their end) or, `late`, while they still run (they are shot down)."""
    fn = cc_function(1)
    t0 = ''.join(cc_wait(CC_DONE + 0x80 * j, 1, f'd{j}') for j in range(1, n)) + '    mov rbx, 77\n' + cc_call(CC_PAGE(10) + CC_FN, 'b')
    delay = '    mov ecx, 3000000\n.x: dec ecx\n    jnz .x\n' if late else ''
    writer = lambda i: f'    mov [0x{CC_PAGE(10) + 0x800 + 8 * i:x}], rbx\n    mov qword [0x{CC_DONE + 0x80 * i:x}], 1\n' + delay
    return cc_program('finish-late' if late else 'finish', n, [t0] + [writer(i) for i in range(1, n)], [(10, bytes(CC_FN - 0x10) + fn)],
                      regs=lambda i: {'rbx': i + 5})

CC_COUNT = 0x141000          # CPU 0's iteration count in `stale` (not captured: how often it went round is schedule-dependent)
def cc_stale(n, patch_after=2000):
    """CPU 0 calls a one-block function on page 11 in a loop until its result changes (counting rounds in an uncaptured
    word); CPUs 1.. store to their own data word on that page until CPU 0 is done, so CPU 0's misses there wait on their
    writable shadows while their stores keep clearing its mark; the last CPU patches the function once CPU 0 went round
    `patch_after` times. A block CPU 0 placed from a page whose mark it lost, and kept live, would never see the patch."""
    # The patch changes one instruction (an 8-byte-aligned store holds it), so a fetch racing it sees the old or the new one.
    fn, old = CC_PAGE(11) + CC_FN, 5 * 10 + 1
    variant = lambda r: (lambda c: c + b'\x90' * (24 - len(c)))(asm(f'imul rax, rbx, 10\nadd rax, {r}\njmp r13'))
    t0 = f""".l: inc qword [0x{CC_COUNT:x}]
    mov rbx, 5
""" + cc_call(fn, 'b').replace('    rol r12, 7\n    xor r12, rax\n', '') + f"""    cmp rax, {old}
    je .l
    mov qword [0x{CC_DONE:x}], 1
""" + ''.join(cc_wait(CC_ACK + 0x80 * j, 1, f'a{j}') for j in range(1, n))
    def writer(i):
        slot = CC_PAGE(11) + 0x800 + 8 * i
        store = lambda lab, until: f'.{lab}: mov [0x{slot:x}], rbx\n{until}'
        done = f'    cmp qword [0x{CC_DONE:x}], 0\n    je .t\n'
        t = ''
        if i == n - 1:
            t += store('s', f'    cmp qword [0x{CC_COUNT:x}], {patch_after}\n    jb .s\n') + cc_write(fn, variant(2)[:8])
        return t + store('t', done) + f'    mov qword [0x{CC_ACK + 0x80 * i:x}], 1\n'
    return cc_program('stale', n, [t0] + [writer(i) for i in range(1, n)], [(11, bytes(CC_FN - 0x10) + variant(1))],
                      regs=lambda i: {'rbx': 0x2222 * (i + 1)}, memory=[dict(zero(CC_COUNT, 8), capture=False)])

def coherence_all():
    out = []
    for n in (2, 4):
        out += [cc_fresh(n, k) for k in ('new', 'reused', 'cr3')] + [cc_rounds(n), cc_data(n), cc_rewrite(n), cc_mutual(n), cc_finish(n, False), cc_finish(n, True)]
    return out + [cc_stale(n) for n in (3, 4)]

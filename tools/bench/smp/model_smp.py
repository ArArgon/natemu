"""Independent expected final state of the deterministic SMP programs (a): written from the README's per-program specification,
not from the assembly and not from any emulator. `expected(prog)` -> {'cpus': [{'gpr','rflags'}], 'memory': [{'addr','data'}]} in the
shape smp.parse_dump returns; it also checks the program's initial registers against the specification."""
M = (1 << 64) - 1
MUL, INC = 6364136223846793005, 1442695040888963407
REG = {n: i for i, n in enumerate('rax rcx rdx rbx rsp rbp rsi rdi r8 r9 r10 r11 r12 r13 r14 r15'.split())}
STACK = lambda i: 0x3E1800 + 0x2000 * i
FLAGS = 0x3046            # IOPL=3, bit 1, ZF, PF: the state after `cmp r11, r11`


def lcg(size, seed):
    x, o = seed, bytearray()
    for _ in range(size):
        x = (x * MUL + INC) & M; o.append(x >> 56)
    return bytes(o)

def q(mem, addr, val): mem[addr:addr + 8] = (val & M).to_bytes(8, 'little')


def spec(name, n):
    """-> (initial regs per cpu [dict], final regs per cpu [dict], {address: bytearray} final contents of the touched memory)"""
    init, fin, mem = [], [], {}
    if name == 'disjoint-lcg':
        for i in range(n):
            x, base, it = (0x9e3779b97f4a7c15 * (i + 1)) & M, 0x220000 + 0x10000 * i, 200000 + 10000 * i
            init.append(dict(rax=x, rbx=base, rcx=it, rsi=MUL, rdi=INC))
            arr, acc, idx, v = [0] * 512, 0, 0, 0
            for _ in range(it):
                x = (x * MUL + INC) & M; idx = x >> 55; arr[idx] = (arr[idx] + x) & M; v = arr[idx]; acc = (acc + v) & M
            fin.append(dict(rax=x, rbx=base, rcx=0, rdx=idx, rsi=MUL, rdi=INC, r8=acc, r9=v))
            m = bytearray(4096)
            for j, a in enumerate(arr): q(m, 8 * j, a)
            mem[base] = m
    elif name == 'shared-ro':
        table = lcg(0x10000, 1); mem[0x120000] = bytearray(table); out = bytearray(0x100); mem[0x210000] = out
        for i in range(n):
            cnt, stride, slot = 100000 + 17 * i, 8 * (2 * i + 1), 0x210000 + 0x80 * i
            init.append(dict(rbx=0x120000, rcx=cnt, rsi=stride, rdi=0xFFF8, r10=slot))
            off = acc = v = 0
            for _ in range(cnt):
                v = int.from_bytes(table[off:off + 8], 'little'); acc = (acc * 31 + v) & M; off = (off + stride) & 0xFFF8
            fin.append(dict(rbx=0x120000, rcx=0, rdx=off, rsi=stride, rdi=0xFFF8, r8=acc, r9=v, r10=slot)); q(out, 0x80 * i, acc)
    elif name == 'handshake':
        D = [(c * c + 7) & M for c in range(256)]; S = []
        for j in range(n):
            m_, acc = 3 + 2 * j, 0
            for d in D: acc = (acc * m_ + d) & M
            S.append(acc)
        T = sum(S) & M
        dm, fm, sm, tm = bytearray(0x800), bytearray(0x400), bytearray(0x200), bytearray(0x100)
        for c, d in enumerate(D): q(dm, 8 * c, d)
        q(fm, 0, 1)
        for j in range(1, n): q(fm, 0x80 + 0x80 * j, 1)
        for j in range(n): q(sm, 0x80 * j, S[j])
        q(tm, 0, T); mem[0x230000], mem[0x231000], mem[0x232000], mem[0x233000] = dm, fm, sm, tm
        HD, HF, HS, HT, DN = 0x230000, 0x231000, 0x232000, 0x233000, 0x231080
        init.append(dict(rbx=HD, rbp=HF, rsi=3, r10=HS, r13=DN + 0x80, r14=n - 1, r12=HS, r15=n, r9=HT))
        fin.append(dict(rax=D[255], rcx=256, rbx=HD, rbp=HF, rsi=3, r8=T, r9=HT, r10=HS, r12=HS + 0x80 * n, r13=DN + 0x80 * n))
        for j in range(1, n):
            init.append(dict(rbx=HD, rbp=HF, rsi=3 + 2 * j, r10=HS + 0x80 * j, r13=DN + 0x80 * j))
            fin.append(dict(rax=D[255], rcx=256, rbx=HD, rbp=HF, rsi=3 + 2 * j, r8=S[j], r10=HS + 0x80 * j, r13=DN + 0x80 * j))
    elif name == 'token-ring':
        R, log = 1000, bytearray(0x8000)
        for r in range(R):
            for i in range(n): q(log, 8 * (r * n + i), i + 0x100 * r)
        mem[0x240000], mem[0x241000] = bytearray(0x100), log           # T ends at 0: the last holder (cpu n-1) passes the token to cpu 0
        for i in range(n):
            init.append(dict(rbx=0x240000, rcx=R, r8=0x241000 + 8 * i, r9=8 * n, r10=i, r12=(i + 1) % n, r13=i))
            fin.append(dict(rbx=0x240000, rcx=0, r8=0x241000 + 8 * i + 8 * n * R, r9=8 * n, r10=i, r12=(i + 1) % n, r13=i + 0x100 * R))
    elif name == 'xchg-lock':
        c = bytearray(0x100); q(c, 0x80, n * 5000); mem[0x250000] = c       # lock word ends 0, counter = cpus * iterations
        for i in range(n):
            init.append(dict(rbx=0x250000, rbp=0x250080, rcx=5000)); fin.append(dict(rbx=0x250000, rbp=0x250080))
    elif name == 'lock-counter':    # (I3) every LOCKed RMW of every CPU took effect exactly once, whatever the interleaving
        R, c, m32 = 20000, bytearray(0x100), (1 << 32) - 1
        q(c, 0, n * R); q(c, 8, n * R); q(c, 16, 3 * n * R); c[24:28] = (-2 * n * R & m32).to_bytes(4, 'little')
        q(c, 32, 1); q(c, 40, n * R); c[64:68] = ((1 << n) - 1).to_bytes(4, 'little'); q(c, 80, n * R); c[96:100] = (n * R).to_bytes(4, 'little')
        mem[0x260000] = c                 # +32 starts at 1, negated an even number of times; +48 (xor twice), +56 (bts then btr) end 0
        for i in range(n):
            regs = dict(r15=0x260000, r8=(0x0123456789abcdef * (i + 1)) & M, r9=i, r10=1 << i)
            init.append(dict(regs, rsi=R)); fin.append(regs)
    elif name in ('neg-race', 'neg-private'):    # (I3) n * 20000 LOCK NEGs of words starting at 1: each ends 1
        R, c = 20000, bytearray(0x200)
        for i in range(n if name == 'neg-private' else 1): q(c, 0x80 * i, 1)
        mem[0x270000] = c
        for i in range(n):
            regs = dict(r15=0x270000 + (0x80 * i if name == 'neg-private' else 0))
            init.append(dict(regs, rsi=R)); fin.append(regs)
    else: raise KeyError(name)
    return init, fin, mem


def expected(prog):
    name, n = prog['name'].split('@')[0], prog['ncpu']; init, fin, mem = spec(name, n)
    for i, c in enumerate(prog['cpus']):                          # the program's initial registers must be the specification's
        want = [0] * 16; want[REG['rsp']] = STACK(i)
        for r, v in init[i].items(): want[REG[r]] = v
        have = [int(x, 0) for x in c['gpr']]
        assert have == want, f'cpu {i}: program initial registers differ from the specification'
    cpus = []
    for i in range(n):
        g = [0] * 16; g[REG['rsp']] = STACK(i)
        for r, v in fin[i].items(): g[REG[r]] = v
        cpus.append({'gpr': g, 'rflags': FLAGS})
    memory = []
    for r in prog['memory']:
        a, size = int(r['addr'], 0), r.get('size') or len(r['data']) // 2
        if not r.get('capture', True): continue
        buf = bytearray(size)
        for base, m in mem.items():
            lo, hi = max(a, base), min(a + size, base + len(m))
            if lo < hi: buf[lo - a:hi - a] = m[lo - base:hi - base]
        memory.append({'addr': a, 'data': bytes(buf)})
    return {'cpus': cpus, 'memory': memory}

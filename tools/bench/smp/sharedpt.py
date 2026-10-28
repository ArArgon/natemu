"""Shared guest page tables (S3f): generated system programs in which several CPUs run on ONE set of page tables, and the
judges that hold their observations against tools/i4v_coherence_model.py (the independent translation/coherence model).

Every program loads the one table set TA on every CPU (CPL0, `system`), shares one code page, and ends by restoring the
start-up controls. No expected result is written by hand: the programs log what each CPU saw (plain stores, a log area per
CPU), and the judges derive the permitted histories by calling the model (`walk`, `Cache`, `access_outcomes`,
`check_observation`). What is schedule-independent (final memory, final PTE bytes) is also compared with QEMU system mode.

Checks (`PROGRAMS`):
  ad-race     N CPUs access one PTE (A/D clear) and race to set A/D; CPU 0 clears A/D with plain stores and runs its own INVLPG.
              Phase 1: synchronized rounds (CPU 0 clears + INVLPG, all CPUs INVLPG and read or write per a role table, each logs
              the PTE it then reads; after all acknowledged, CPU 0 logs the PTE: exactly the model's A/D). Phase 2: free race
              (CPU 0 clears + INVLPG in a loop while the others loop on random loads, stores, INVLPGs; every PTE sample must be a
              state the model can reach). Phase 3: more synchronized rounds, the last one all writers (final PTE bytes).
  remap       CPU 0 changes a PTE's frame each round; every CPU reads/stores V before its own INVLPG (staggered, per a mode table),
              then INVLPGs and accesses again. Pre-INVLPG: either frame, never old after new (model Cache); post: the new frame.
  large       A PDE switches between a 2 MiB leaf and a table (4 KiB pages mapping part of it elsewhere), each CPU INVLPGs at its
              own time. Same observation model; after each round the PDE/PTE A/D bytes are exact.
  alias       Two linear pages map one frame; CPUs write through different aliases and read/verify through others; both PTEs'
              A/D after each round are exact, data coherence is checked in the program (ERR) and against QEMU.
  contended   A clearer cycles a PTE through four values (software bits), 64 spins between stores, while the others INVLPG and
              access it (data), or INVLPG and run code from its page (fetch), or both: walks lose their A/D compare-exchange
              races (with Contended turned back into a halt, every variant halts within ~200 walks). `contended-max`: no pause
              between the clearer's stores: walkers are starved (~200 walks/s) but must still finish 150. Every CPU must reach
              its end (no halt, no watchdog); a last synchronized round fixes the final PTE bytes.
"""
import collections, itertools, pathlib, random, sys
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent)); sys.path.insert(0, str(HERE.parent.parent))
import programs as P, smp
import i4v_coherence_model as cm

TA = P.SYS_T(0, 0)                  # the one table set (identity map of 0..4 MiB, 4 KiB pages; PML4 TA, PDPT +0x1000, PD +0x2000, PT0 +0x3000, PT1 +0x4000)
D0 = P.SYS_D(0)                     # data area 0x280000..0x2c0000 (inside the leaf slice sys_tables provides)
V, F1, F2, V2 = D0, D0 + 0x1000, D0 + 0x2000, D0 + 0x4000      # the page under test, its frames, an alias of it
SAVE = D0 + 0x7000                  # start-up CR3 per CPU (not captured)
SYNC = D0 + 0x8000                  # +0 GEN; ACK1(j) +0x100+0x40j; ACK2(j) +0x200+0x40j; DONE(j) +0x300+0x40j; ERR(j) +0x400+8j
GEN = SYNC
GO2 = SYNC + 0x500                  # phase 2 may start (ad-race): CPU 0 has taken its last phase-1 sample
ACK1, ACK2, DONE = (lambda j, o=o: SYNC + o + 0x40 * j for o in (0x100, 0x200, 0x300))
ERR = lambda j: SYNC + 0x400 + 8 * j
ROLES = D0 + 0xa000                 # a byte per (round, CPU): ROLES + 8 * round + CPU (also MODES, DELAYS: TABLES below)
TAB = lambda k: D0 + 0xa000 + 0x4000 * k            # table k (0..3): 0x4000 bytes each, 0x2a0000 + ... (k <= 2 keeps below LOG)
LOG = lambda i: D0 + 0x20000 + 0x4000 * i           # CPU i's log (0x4000 bytes: 0x2a0000 + 0x4000 * ...)
LOGF = D0 + 0x30000                 # CPU 0's per-round samples
LEAF = P.P | P.RW | P.US | P.A | P.D        # 0x67
BASE = P.P | P.RW | P.US                    # 0x07: A/D clear
XS = '    mov r13, r14\n    shl r13, 13\n    xor r14, r13\n    mov r13, r14\n    shr r13, 7\n    xor r14, r13\n    mov r13, r14\n    shl r13, 17\n    xor r14, r13\n'
CTX = cm.Context('four', TA, cpl=0, wp=True, nxe=True)      # CR0 0x80010011, EFER NXE: the start-up controls


# ---- generator helpers ---------------------------------------------------------------------------------------------------
class Asm:
    def __init__(s): s.t, s.k = [], 0
    def __call__(s, *lines): s.t += lines
    def lab(s, base='l'): s.k += 1; return f'.{base}{s.k}'
    def text(s): return '\n'.join(s.t)
    def wait(s, addr, reg, cond='b'):
        l = s.lab('w'); s(f'{l}:', f'    cmp qword [{addr:#x}], {reg}', f'    j{cond} {l}')
    def delay(s, mask):         # 0..mask spins, from the xorshift in r14 (clobbers r13)
        l = s.lab('d'); s(XS.rstrip('\n'), '    mov r13d, r14d', f'    and r13d, {mask}', f'{l}:', '    dec r13d', f'    jns {l}')


def lcg_regions(addr, size, seed, holes=()):
    """Regions covering [addr, addr + size) with lcg contents; each hole (offset, length) is left out of the comparison."""
    data, out, pos = smp.lcg_bytes(size, seed), [], 0
    for off, ln in sorted(holes) + [(size, 0)]:
        if off > pos: out.append({'addr': f'0x{addr + pos:x}', 'data': data[pos:off].hex()})
        if ln: out.append({'addr': f'0x{addr + off:x}', 'data': data[off:off + ln].hex(), 'capture': False})
        pos = off + ln
    return out

def table_region(addr, data): return {'addr': f'0x{addr:x}', 'data': bytes(data).hex()}

def program(name, n, text, memory, params, regs=None):
    """The shared body (`rbx` = CPU id, `r14` = a per-CPU xorshift seed) on every CPU, wrapped in the start-up CR3 save and
    restore of the system programs."""
    head = f'bits 64\n    mov rax, cr3\n    mov [{SAVE:#x}+rbx*8], rax\n    mov rax, {TA:#x}\n    mov cr3, rax\n'
    tail = (f'\n    mov [{ERR(0):#x}+rbx*8], r10\n    mov eax, 0x20\n    mov cr4, rax\n    mov eax, 0x80010011\n    mov cr0, rax\n    mov ecx, 0xC0000080\n'
            f'    mov eax, 0xd00\n    xor edx, edx\n    wrmsr\n    mov rax, [{SAVE:#x}+rbx*8]\n    mov cr3, rax\n    xor eax, eax\n    cmp r11, r11')
    full = head + text + tail
    code = P.asm(full)
    assert len(code) < 0x1000 - 16, (name, len(code))
    cpus = [dict(P.cpu(i, P.SYS_CODE(0), rbx=i, r14=(0x9e3779b97f4a7c15 * (i + 1)) & P.M64, **(regs or {})), cpl=0) for i in range(n)]
    memory = list(memory) + [dict(P.zero(SAVE, 0x20), capture=False), P.zero(SYNC, 0x1000)]
    return {'format': 'natemu-smp-v1', 'name': f'sharedpt-{name}@{n}', 'ncpu': n, 'system': True, 'bodies': [P.body(P.SYS_CODE(0), full)],
            'cpus': cpus, 'memory': memory, 'params': dict(params, check=name)}

def logs(n, size=0x4000):
    return [dict(P.zero(LOG(i), size), capture=False) for i in range(n)] + [dict(P.zero(LOGF, 0x4000), capture=False)]


# ---- 1. A/D races on one PTE ---------------------------------------------------------------------------------------------
AD_CAP = 0x2000        # phase-2 samples logged per CPU (bytes, at LOG(i) + 0x1000)

def ad_rounds(a, n, first, last, leaf):
    """Synchronized rounds first..last: CPU 0 waits for every acknowledgement of the round before (and samples the PTE: the
    round's final A/D), clears the PTE with a plain store, INVLPGs, publishes the round; every CPU INVLPGs, reads (role 0) or
    writes (role 1) the page, samples the PTE and acknowledges."""
    top, pw, sk, rd, done, ok = (a.lab(x) for x in ('top', 'pw', 'sk', 'rd', 'dn', 'ok'))
    a(f'    mov r12d, {first}', f'{top}:', '    test ebx, ebx', f'    jnz {pw}', f'    cmp r12d, {first}', f'    je {sk}', '    lea r13, [r12-1]')
    for j in range(1, n): a.wait(ACK1(j), 'r13')
    a(f'    mov rax, [{leaf:#x}]', f'    mov [{LOGF:#x}+r13*8], rax', f'{sk}:',
      f'    mov qword [{leaf:#x}], {F1 | BASE:#x}', f'    invlpg [{V:#x}]', f'    mov [{GEN:#x}], r12', f'{pw}:')
    a.wait(GEN, 'r12'); a(f'    invlpg [{V:#x}]'); a.delay(31)
    a(f'    lea rdx, [{ROLES:#x}+r12*8]', '    movzx eax, byte [rdx+rbx]', '    test al, 1', f'    jz {rd}',
      f'    mov [{V + 0x100:#x}+rbx*8], r12', f'    cmp [{V + 0x100:#x}+rbx*8], r12', f'    je {done}', '    inc r10', f'    jmp {done}',
      f'{rd}:', f'    mov rax, [{V + 0x80:#x}]', f'{done}:',
      f'    mov rax, [{leaf:#x}]', '    mov rdx, rbx', '    shl rdx, 14', f'    mov [rdx+{LOG(0):#x}+r12*8], rax',
      '    mov rdx, rbx', '    shl rdx, 6', f'    mov [rdx+{ACK1(0):#x}], r12', '    inc r12d', f'    cmp r12d, {last}', f'    jbe {top}',
      '    test ebx, ebx', f'    jnz {ok}', f'    mov r13d, {last}')
    for j in range(1, n): a.wait(ACK1(j), 'r13')
    a(f'    mov rax, [{leaf:#x}]', f'    mov [{LOGF:#x}+r13*8], rax', f'{ok}:')

def ad_free(a, n, iters, leaf):
    """Phase 2: CPU 0 loops clear + INVLPG + an access until every peer is done; each peer does `iters` accesses (a random
    INVLPG before about half of them). Every iteration samples the PTE as a byte: A, D, wrote (4), INVLPG'd (8)."""
    pe, c0, go, out, skip, ok2 = (a.lab(x) for x in ('pe', 'c0', 'go', 'out', 'sk', 'o2'))
    def access():
        rd, sm, okp, nolog = (a.lab(x) for x in ('rd', 'sm', 'op', 'nl'))
        a(XS.rstrip('\n'), f'    test r14, {1 << 20}', f'    jz {rd}', '    or r8d, 4', '    lea rax, [r12+1]', '    mov rdx, rbx', '    shl rdx, 56', '    or rax, rdx',
          f'    mov [{V + 0x400:#x}+rbx*8], rax', f'    mov rdx, [{V + 0x400:#x}+rbx*8]', '    cmp rdx, rax', f'    je {sm}', '    inc r10', f'    jmp {sm}',
          f'{rd}:', f'    mov rax, [{V + 0x80:#x}]', f'    cmp rax, [{F1 + 0x80:#x}]', f'    je {sm}', '    inc r10',
          f'{sm}:', f'    mov rax, [{leaf:#x}]', '    mov rcx, rax', '    and rcx, -97', f'    cmp rcx, {F1 | BASE:#x}', f'    je {okp}', '    inc r10',
          f'{okp}:', '    shr eax, 5', '    and eax, 3', '    or eax, r8d', f'    cmp r12d, {AD_CAP}', f'    jae {nolog}',
          '    mov rdx, rbx', '    shl rdx, 14', f'    mov [rdx+{LOG(0) + 0x1000:#x}+r12], al', f'{nolog}:')
    a(f'    test ebx, ebx', f'    jnz {go}w', f'    mov qword [{GO2:#x}], 1', f'{go}w:', f'    cmp qword [{GO2:#x}], 0', f'    je {go}w')     # the peers wait for CPU 0's last phase-1 sample
    a('    xor r12d, r12d', '    test ebx, ebx', f'    jnz {pe}', f'{c0}:')
    for j in range(1, n): a(f'    cmp qword [{DONE(j):#x}], 0', f'    je {go}')
    a(f'    jmp {out}', f'{go}:', f'    mov qword [{leaf:#x}], {F1 | BASE:#x}', f'    invlpg [{V:#x}]', '    mov r8d, 8')
    access()
    a('    inc r12d', f'    jmp {c0}', f'{pe}:', '    xor r8d, r8d', XS.rstrip('\n'), f'    test r14, {1 << 21}', f'    jz {ok2}', f'    invlpg [{V:#x}]', '    or r8d, 8', f'{ok2}:')
    access()
    a('    inc r12d', f'    cmp r12d, {iters}', f'    jb {pe}', '    mov rdx, rbx', '    shl rdx, 6', f'    mov qword [rdx+{DONE(0):#x}], 1',
      f'{out}:', '    test ebx, ebx', f'    jnz {skip}', f'    mov [{LOG(0) + 0x3ff8:#x}], r12', f'{skip}:')

def ad_roles(n, rounds, rng):
    """Role byte (0 read, 1 write) per (round, CPU): rounds of all readers, all writers and random mixes; the last all writers."""
    t = bytearray(8 * (rounds + 1))
    for r in range(1, rounds + 1):
        for j in range(n): t[8 * r + j] = {0: 0, 1: 1}.get(r % 4, rng.randrange(2))
    for j in range(n): t[8 * rounds + j] = 1
    return t

def ad_race(n, r1=300, iters=10000, r3=150):
    rng = random.Random(f'ad-race-{n}')
    leaf = P.sys_leaf(0, 0, V)
    rounds = r1 + r3
    a = Asm(); ad_rounds(a, n, 1, r1, leaf); ad_free(a, n, iters, leaf); ad_rounds(a, n, r1 + 1, rounds, leaf)
    memory = (P.sys_tables(0, 0, n, {V: F1 | BASE}) + lcg_regions(F1, 0x1000, 1, [(0x400, 0x100)]) + [table_region(ROLES, ad_roles(n, rounds, rng))] + logs(n))
    return program('ad-race', n, a.text(), memory, {'r1': r1, 'iters': iters, 'r3': r3, 'rounds': rounds})


# ---- 2. remap with per-vCPU INVLPG ---------------------------------------------------------------------------------------
REMAP_K = 4        # pre-INVLPG iterations per round (a read and a store each)
MODES, DELAYS, VALUES = TAB(0), TAB(1), TAB(2)

def round_head(a, n, ack, publish):
    """Round loop head for r12 = round 0..: CPU 0 waits for the round before, runs `publish()` (its plain store), publishes
    GEN = r + 1; every CPU's mode byte goes to r9d; the round's log base to rdi."""
    top, pw = a.lab('top'), a.lab('pw')
    a('    xor r12d, r12d', f'{top}:', '    test ebx, ebx', f'    jnz {pw}', '    test r12d, r12d', f'    jz {pw}x')
    for j in range(1, n): a.wait(ack(j), 'r12')
    a(f'{pw}x:'); publish()
    a('    lea r13, [r12+1]', f'    mov [{GEN:#x}], r13', f'{pw}:', '    lea r13, [r12+1]')
    return top

def remap_text(n, rounds):
    a = Asm(); lv = P.sys_leaf(0, 0, V)
    top = round_head(a, n, ACK1, lambda: a(f'    mov rax, [{VALUES:#x}+r12*8]', f'    mov [{lv:#x}], rax'))
    # a mode (0: stale reads, 1: INVLPG before the remap can be seen, 2: no pre-INVLPG access) and a delay per (round, CPU)
    a(f'    lea rdx, [{MODES:#x}+r12*8]', '    movzx r9d, byte [rdx+rbx]', '    mov rdi, rbx', '    shl rdi, 14', '    mov rsi, r12', '    shl rsi, 8', f'    lea rdi, [rdi+rsi+{LOG(0):#x}]',
      '    cmp r9d, 1', f'    jne .nopf', f'    invlpg [{V:#x}]', '.nopf:')
    a.wait(GEN, 'r13')
    a('    cmp r9d, 2', '    je .nopre')
    for k in range(REMAP_K):
        a(f'    mov rax, [{V + 0x80:#x}]', f'    mov [rdi+{16 * k}], rax',
          '    lea rcx, [r12+1]', '    shl rcx, 16', '    mov rdx, rbx', '    shl rdx, 8', '    or rcx, rdx', f'    or rcx, {k + 1}', f'    mov [{V + 0x200:#x}+rbx*8], rcx',
          '    xor eax, eax', f'    cmp [{F1 + 0x200:#x}+rbx*8], rcx', '    sete al', '    xor edx, edx', f'    cmp [{F2 + 0x200:#x}+rbx*8], rcx', '    sete dl',
          '    lea eax, [rax+rdx*2]', f'    mov [rdi+{16 * k + 8}], rax')
        a(f'    lea rdx, [{DELAYS:#x}+r12*8]', '    movzx r13d, byte [rdx+rbx]', '    shl r13d, 1', f'.dp{k}:', '    dec r13d', f'    jns .dp{k}')
    a('.nopre:', f'    lea rdx, [{DELAYS:#x}+r12*8]', '    movzx r13d, byte [rdx+rbx]', '    shl r13d, 2', '.dq:', '    dec r13d', '    jns .dq', f'    invlpg [{V:#x}]',
      f'    mov rax, [{V + 0x80:#x}]', f'    mov [rdi+{16 * REMAP_K}], rax',
      '    lea rcx, [r12+1]', '    shl rcx, 16', '    mov rdx, rbx', '    shl rdx, 8', '    or rcx, rdx', '    or rcx, 0x80', f'    mov [{V + 0x100:#x}+rbx*8], rcx',
      '    xor eax, eax', f'    cmp [{F1 + 0x100:#x}+rbx*8], rcx', '    sete al', '    xor edx, edx', f'    cmp [{F2 + 0x100:#x}+rbx*8], rcx', '    sete dl',
      '    lea eax, [rax+rdx*2]', f'    mov [rdi+{16 * REMAP_K + 8}], rax',
      '    mov rdx, rbx', '    shl rdx, 6', '    lea r13, [r12+1]', f'    mov [rdx+{ACK1(0):#x}], r13', '    inc r12d', f'    cmp r12d, {rounds}', f'    jbe {top}',
      '    test ebx, ebx', '    jnz .end')
    a('    lea r13, [r12]')
    for j in range(1, n): a.wait(ACK1(j), 'r13')
    a('.end:')
    return a.text()

def remap(n, rounds=40):
    rng = random.Random(f'remap-{n}')
    frames = [F1, F2]
    values = b''.join(((frames[r % 2]) | LEAF).to_bytes(8, 'little') for r in range(rounds + 1))
    modes, delays = bytearray(8 * (rounds + 1)), bytearray(8 * (rounds + 1))
    for r in range(rounds + 1):
        for j in range(n):
            modes[8 * r + j] = rng.choice([0, 0, 0, 1, 2])
            delays[8 * r + j] = rng.choice([0, 1, 4, 16, 64, 255])
    memory = (P.sys_tables(0, 0, n, {V: F1 | LEAF}) + lcg_regions(F1, 0x1000, 1, [(0x200, 0x40)]) + lcg_regions(F2, 0x1000, 2, [(0x200, 0x40)])
              + [table_region(MODES, modes), table_region(DELAYS, delays), table_region(VALUES, values)] + logs(n))
    return program('remap', n, remap_text(n, rounds), memory, {'rounds': rounds, 'k': REMAP_K})


# ---- 3. 2 MiB <-> 4 KiB ---------------------------------------------------------------------------------------------------
W = 0x400000                         # PD index 2: a 2 MiB window, a large leaf onto 0x200000.. or a table (PT2) mapping the same
PDE = TA + 0x2000 + 2 * 8
PT2 = TA + 0x5000
T_VA, S_VA = W + 0x80000, W + 0x90000    # T: its frame differs between the states; S: the same in both
FA, FB = 0x280000, 0x281000          # T's frame under the large leaf (0x200000 + 0x80000), and under PT2
S_PA = 0x290000
PDE_LARGE = 0x200000 | P.P | P.RW | P.US | 0x80       # PS
PDE_TABLE = PT2 | P.P | P.RW | P.US | P.A
T_LEAF = PT2 + 8 * ((T_VA - W) >> 12)
S_LEAF = PT2 + 8 * ((S_VA - W) >> 12)
LARGE_K = 2

def large_text(n, rounds):
    a = Asm()
    def publish():
        # CPU 0, once every CPU acknowledged the round before: the PDE and PTE as the walks left them (r12 - 1), then the new PDE
        skip = a.lab('sk')
        a('    test r12d, r12d', f'    jz {skip}', '    lea r13, [r12-1]', f'    mov rax, [{PDE:#x}]', '    mov rdx, r13', '    shl rdx, 4', f'    mov [rdx+{LOGF:#x}], rax',
          f'    mov rax, [{T_LEAF:#x}]', f'    mov [rdx+{LOGF + 8:#x}], rax', f'{skip}:', f'    mov rax, [{VALUES:#x}+r12*8]', f'    mov [{PDE:#x}], rax')
    top = round_head(a, n, ACK1, publish)
    a(f'    lea rdx, [{MODES:#x}+r12*8]', '    movzx r9d, byte [rdx+rbx]', '    mov rdi, rbx', '    shl rdi, 14', '    mov rsi, r12', '    shl rsi, 8', f'    lea rdi, [rdi+rsi+{LOG(0):#x}]',
      '    cmp r9d, 1', '    jne .nopf', f'    invlpg [{T_VA:#x}]', '.nopf:')
    a.wait(GEN, 'r13')
    a('    cmp r9d, 2', '    je .nopre')
    for k in range(LARGE_K):
        a(f'    mov rax, [{T_VA + 0x80:#x}]', f'    mov [rdi+{16 * k}], rax', f'    mov rax, [{S_VA + 0x80:#x}]', f'    mov [rdi+{16 * k + 8}], rax',
          f'    lea rdx, [{DELAYS:#x}+r12*8]', '    movzx r13d, byte [rdx+rbx]', '    shl r13d, 1', f'.dp{k}:', '    dec r13d', f'    jns .dp{k}')
    a('.nopre:', f'    lea rdx, [{DELAYS:#x}+r12*8]', '    movzx r13d, byte [rdx+rbx]', '    shl r13d, 2', '.dq:', '    dec r13d', '    jns .dq', f'    invlpg [{T_VA:#x}]',
      f'    mov rax, [{T_VA + 0x80:#x}]', f'    mov [rdi+{16 * LARGE_K}], rax', f'    mov rax, [{S_VA + 0x80:#x}]', f'    mov [rdi+{16 * LARGE_K + 8}], rax',
      '    lea rcx, [r12+1]', '    shl rcx, 16', '    mov rdx, rbx', '    shl rdx, 8', '    or rcx, rdx', '    or rcx, 0x80', f'    mov [{T_VA + 0x100:#x}+rbx*8], rcx',
      '    xor eax, eax', f'    cmp [{FA + 0x100:#x}+rbx*8], rcx', '    sete al', '    xor edx, edx', f'    cmp [{FB + 0x100:#x}+rbx*8], rcx', '    sete dl',
      '    lea eax, [rax+rdx*2]', f'    mov [rdi+{16 * LARGE_K + 16}], rax',
      '    mov rdx, rbx', '    shl rdx, 6', '    lea r13, [r12+1]', f'    mov [rdx+{ACK1(0):#x}], r13', '    inc r12d', f'    cmp r12d, {rounds}', f'    jbe {top}',
      '    test ebx, ebx', '    jnz .end', '    mov r13d, r12d')
    for j in range(1, n): a.wait(ACK1(j), 'r13')
    a(f'    mov rax, [{PDE:#x}]', f'    mov [{LOGF + 16 * (rounds):#x}], rax', f'    mov rax, [{T_LEAF:#x}]', f'    mov [{LOGF + 16 * rounds + 8:#x}], rax', '.end:')
    return a.text()

def large(n, rounds=40):
    rng = random.Random(f'large-{n}')
    states = [0] + [rng.randrange(2) for _ in range(rounds)]       # 0: the large leaf, 1: the table
    states[1], states[2] = 1, 1                                      # a large -> table change, a table -> table rewrite
    states[3] = 0; states[4] = 0                                     # a table -> large change, a large -> large rewrite
    values = b''.join((PDE_TABLE if s else PDE_LARGE).to_bytes(8, 'little') for s in states)
    modes, delays = bytearray(8 * (rounds + 1)), bytearray(8 * (rounds + 1))
    for r in range(rounds + 1):
        for j in range(n):
            modes[8 * r + j] = rng.choice([0, 0, 0, 1, 2])
            delays[8 * r + j] = rng.choice([0, 1, 4, 16, 64, 255])
    pt2 = bytearray()
    for k in range(512):
        pa = 0x200000 + 0x1000 * k
        entry = (FB | BASE) if k == (T_VA - W) >> 12 else (pa | LEAF)
        pt2 += entry.to_bytes(8, 'little')
    memory = (P.sys_tables(0, 0, n, {}) + [table_region(PDE, PDE_LARGE.to_bytes(8, 'little')), table_region(PT2, pt2)]
              + lcg_regions(FA, 0x1000, 1) + lcg_regions(FB, 0x1000, 2) + lcg_regions(S_PA, 0x1000, 3)
              + [table_region(MODES, modes), table_region(DELAYS, delays), table_region(VALUES, values)] + logs(n))
    return program('large', n, large_text(n, rounds), memory, {'rounds': rounds, 'states': states})


# ---- 4. aliases across vCPUs ----------------------------------------------------------------------------------------------
def alias_text(n, rounds):
    """Round r (1..): CPU 0 clears both PTEs and INVLPGs both pages; every CPU INVLPGs both, then reads through one alias (or
    none) and writes its own word through another; after every CPU's writes, each verifies every CPU's word through a third
    alias (the identity page, or either linear page). Role byte: write alias (bits 0-1: 1 V, 2 V2), read alias (2-3), verify (4-5: 0 identity)."""
    a = Asm(); l1, l2 = P.sys_leaf(0, 0, V), P.sys_leaf(0, 0, V2)
    top, pw, sk, v1, v2, vd = (a.lab(x) for x in ('top', 'pw', 'sk', 'v1', 'v2', 'vd'))
    a('    mov r12d, 1', f'{top}:', '    test ebx, ebx', f'    jnz {pw}', '    cmp r12d, 1', f'    je {sk}', '    lea r13, [r12-1]')
    for j in range(1, n): a.wait(ACK2(j), 'r13')
    a('    mov rdx, r13', '    shl rdx, 4', f'    mov rax, [{l1:#x}]', f'    mov [rdx+{LOGF:#x}], rax', f'    mov rax, [{l2:#x}]', f'    mov [rdx+{LOGF + 8:#x}], rax', f'{sk}:',
      f'    mov qword [{l1:#x}], {F1 | BASE:#x}', f'    mov qword [{l2:#x}], {F1 | BASE:#x}', f'    invlpg [{V:#x}]', f'    invlpg [{V2:#x}]', f'    mov [{GEN:#x}], r12', f'{pw}:')
    a.wait(GEN, 'r12'); a(f'    invlpg [{V:#x}]', f'    invlpg [{V2:#x}]'); a.delay(63)
    a(f'    lea rdx, [{ROLES:#x}+r12*8]', '    movzx r9d, byte [rdx+rbx]', '    mov eax, r9d', '    shr eax, 2', '    and eax, 3')
    r1, r2, r0 = a.lab('r1'), a.lab('r2'), a.lab('r0')
    a('    cmp eax, 1', f'    jne {r1}', f'    mov rax, [{V + 0x80:#x}]', f'    jmp {r0}', f'{r1}:', '    cmp eax, 2', f'    jne {r0}', f'    mov rax, [{V2 + 0x80:#x}]', f'{r0}:')
    a('    mov rcx, r12', '    shl rcx, 8', '    or rcx, rbx', '    or rcx, 0x10000', '    mov eax, r9d', '    and eax, 3', '    cmp eax, 1', f'    jne {v1}',
      f'    mov [{V + 0x100:#x}+rbx*8], rcx', f'    jmp {v2}', f'{v1}:', f'    mov [{V2 + 0x100:#x}+rbx*8], rcx', f'{v2}:')
    a('    mov rdx, rbx', '    shl rdx, 6', f'    mov [rdx+{ACK1(0):#x}], r12')
    for j in range(n): a.wait(ACK1(j), 'r12')
    # verify through alias bits 4-5
    ve, vv1, vv2 = a.lab('ve'), a.lab('vv'), a.lab('vw')
    a('    mov eax, r9d', '    shr eax, 4', '    and eax, 3', f'    mov r8d, {F1:#x}', '    cmp eax, 1', f'    jne {vv1}', f'    mov r8d, {V:#x}', f'    jmp {ve}',
      f'{vv1}:', '    cmp eax, 2', f'    jne {ve}', f'    mov r8d, {V2:#x}', f'{ve}:')
    for k in range(n):
        ok = a.lab('ok')
        a('    mov rcx, r12', '    shl rcx, 8', f'    or rcx, {k}', '    or rcx, 0x10000', f'    mov rax, [r8+{0x100 + 8 * k}]', '    cmp rax, rcx', f'    je {ok}', '    inc r10', f'{ok}:')
    a('    mov rdx, rbx', '    shl rdx, 6', f'    mov [rdx+{ACK2(0):#x}], r12', '    inc r12d', f'    cmp r12d, {rounds}', f'    jbe {top}', '    test ebx, ebx', '    jnz .end', f'    mov r13d, {rounds}')
    for j in range(1, n): a.wait(ACK2(j), 'r13')
    a('    mov rdx, r13', '    shl rdx, 4', f'    mov rax, [{l1:#x}]', f'    mov [rdx+{LOGF:#x}], rax', f'    mov rax, [{l2:#x}]', f'    mov [rdx+{LOGF + 8:#x}], rax', '.end:')
    return a.text()

def alias(n, rounds=200):
    rng = random.Random(f'alias-{n}')
    t = bytearray(8 * (rounds + 1))
    for r in range(1, rounds + 1):
        kind = r % 5            # 0: everyone writes and verifies through V (V2 never touched), 1: the same through V2, else random
        for j in range(n):
            w = {0: 1, 1: 2}.get(kind, rng.choice([1, 2]))
            rd = 0 if kind in (0, 1) else rng.choice([0, 1, 2])
            v = 0 if kind in (0, 1) else rng.choice([0, 1, 2])
            t[8 * r + j] = w | rd << 2 | v << 4
    memory = P.sys_tables(0, 0, n, {V: F1 | BASE, V2: F1 | BASE}) + lcg_regions(F1, 0x1000, 1) + [table_region(ROLES, t)] + logs(n)
    return program('alias', n, alias_text(n, rounds), memory, {'rounds': rounds})


# ---- 5. contended walks ---------------------------------------------------------------------------------------------------
CODE = 0x118000          # the shared run-time code page of the fetch variants: `jmp r13` at its start
AVL = (0, 0x200, 0x400, 0x600)      # software-available PTE bits the clearer cycles through

def contended_text(n, variant, iters, gap):
    a = Asm(); lv, lc = P.sys_leaf(0, 0, V), P.sys_leaf(0, 0, CODE)
    data, fetch = variant in ('data', 'both'), variant in ('fetch', 'both')
    c0, pe, go, out = (a.lab(x) for x in ('c0', 'pe', 'go', 'out'))
    a('    test ebx, ebx', f'    jnz {pe}', f'{c0}:')
    for j in range(1, n): a(f'    cmp qword [{DONE(j):#x}], 0', f'    je {go}')
    a(f'    jmp {out}', f'{go}:')
    for k in range(4):
        if data: a(f'    mov qword [{lv:#x}], {F1 | BASE | AVL[k]:#x}')
        if fetch: a(f'    mov qword [{lc:#x}], {CODE | BASE | AVL[k]:#x}')
        if gap: a(f'    mov ecx, {gap}', f'.g{k}:', '    dec ecx', f'    jnz .g{k}')
    a(f'    jmp {c0}', f'{pe}:', '    xor r12d, r12d', '.it:')
    if data:
        a(f'    invlpg [{V:#x}]', '    test r12d, 1', '    jz .wr', f'    mov rax, [{V + 0x80:#x}]', '    jmp .dd', '.wr:', f'    mov [{V + 0x100:#x}+rbx*8], r12', '.dd:')
    if fetch: a(f'    invlpg [{CODE:#x}]', '    lea r13, [rel .back]', f'    mov rax, {CODE:#x}', '    jmp rax', '.back:')
    a('    inc r12d', f'    cmp r12d, {iters}', '    jb .it', '    mov rdx, rbx', '    shl rdx, 6', f'    mov qword [rdx+{DONE(0):#x}], 1', f'{out}:')
    # a last synchronized round fixes the final PTE bytes: CPU 0 clears, INVLPGs and publishes; the others INVLPG and access
    a('    test ebx, ebx', '    jnz .fp')
    if data: a(f'    mov qword [{lv:#x}], {F1 | BASE:#x}', f'    invlpg [{V:#x}]')
    if fetch: a(f'    mov qword [{lc:#x}], {CODE | BASE:#x}', f'    invlpg [{CODE:#x}]')
    a(f'    mov qword [{GEN:#x}], 1', '.fp:')
    a('.fw:', f'    cmp qword [{GEN:#x}], 1', '    jb .fw')
    if data: a(f'    invlpg [{V:#x}]', f'    mov [{V + 0x100:#x}+rbx*8], rbx')
    if fetch: a(f'    invlpg [{CODE:#x}]', '    lea r13, [rel .back2]', f'    mov rax, {CODE:#x}', '    jmp rax', '.back2:')
    a('    mov rdx, rbx', '    shl rdx, 6', f'    mov qword [rdx+{ACK1(0):#x}], 1', '    test ebx, ebx', '    jnz .end')
    for j in range(1, n): a(f'.fa{j}:', f'    cmp qword [{ACK1(j):#x}], 1', f'    jb .fa{j}')
    a('.end:')
    return a.text()

def contended(n, variant, iters=6000, gap=64):
    overrides = {}
    if variant in ('data', 'both'): overrides[V] = F1 | BASE
    if variant in ('fetch', 'both'): overrides[CODE] = CODE | BASE
    code = [P.body(CODE, 'jmp r13')]
    memory = P.sys_tables(0, 0, n, overrides) + lcg_regions(F1, 0x1000, 1, [(0x100, 0x40)])
    prog = program(f'contended-{variant}' + ('-max' if not gap else ''), n, contended_text(n, variant, iters, gap), memory, {'variant': variant, 'iters': iters, 'gap': gap})
    prog['bodies'] += code
    return prog


# ---- the programs -------------------------------------------------------------------------------------------------------
PROGRAMS = {
    'ad-race': ad_race, 'remap': remap, 'large': large, 'alias': alias,
    'contended-data': lambda n: contended(n, 'data'), 'contended-fetch': lambda n: contended(n, 'fetch'), 'contended-both': lambda n: contended(n, 'both'),
    'contended-max': lambda n: contended(n, 'data', 150, 0),       # the clearer never pauses: walkers are starved (~200 walks/s), not halted
}

def generate(names=None, cpus=(2, 4)):
    return [PROGRAMS[k](n) for k in PROGRAMS if not names or any(x in k for x in names) for n in cpus]


# ---- judging ------------------------------------------------------------------------------------------------------------
class Regions:
    """Memory regions by address: the program's initial bytes, or a run's final bytes."""
    def __init__(self, regions): self.r = {smp.num(a): bytes(b) for a, b in regions}
    @classmethod
    def initial(cls, prog): return cls((r['addr'], smp.region_bytes(r)) for r in prog['memory'])
    @classmethod
    def final(cls, out): return cls((m['addr'], bytes.fromhex(m['data'])) for m in out['cpus'][0]['memory'])
    def has(self, addr): return any(a <= addr and addr + 8 <= a + len(d) for a, d in self.r.items())
    def q(self, addr, size=8):
        for a, d in self.r.items():
            if a <= addr and addr + size <= a + len(d): return int.from_bytes(d[addr - a:addr - a + size], 'little')
        raise KeyError(f'{addr:#x} is in no region')

def model_tables(init, extra=()):
    """The model's view of the shared tables: the entries any walk of this module reads (from the program's initial memory)."""
    at = {TA, TA + 0x1000, TA + 0x2000, TA + 0x2008, TA + 0x2010, *extra}
    return {a: init.q(a) for a in at if init.has(a)}

def observed(permitted, frame, leaf=None, pte=None):
    """check_observation for a vCPU that saw `frame` (and, with `leaf`, then the leaf word `pte`): the permitted result it
    matches, else ValueError (the observation is outside the history the model permits)."""
    hit = [r for r in permitted if r.route and r.route.gpa & ~4095 == frame and (leaf is None or r.tables[leaf] == pte)]
    return cm.check_observation(hit[0] if hit else None, permitted)

def ad_state(pte): return pte >> 5 & 3          # bit 0 A, bit 1 D

def judge_ad_race(prog, out):
    """-> (diffs, histories). Phase 1/3: every CPU's PTE sample (after its own access, role read/write) is a state the model reaches
    from the cleared PTE with any subset of its peers' accesses done; after every acknowledgement CPU 0's sample is exactly the
    model's A/D after all accesses. Phase 2: every sample is a state the model can reach by walks and plain clears. ERR zero."""
    init, fin, p, n = Regions.initial(prog), Regions.final(out), prog['params'], prog['ncpu']
    leaf = P.sys_leaf(0, 0, V)
    clean = model_tables(init, [leaf]); clean[leaf] = F1 | BASE
    walk = lambda t, acc, va=V + 0x80: cm.walk(t, CTX, va, acc)
    diffs, hist = [], collections.Counter()
    role = lambda r, j: init.q(ROLES + 8 * r + j, 1)
    for r in range(1, p['rounds'] + 1):
        accs = ['write' if role(r, j) else 'read' for j in range(n)]
        for j in range(n):
            sample = fin.q(LOG(j) + 8 * r)
            others = [k for k in range(n) if k != j]
            permitted = []
            for m in range(len(others) + 1):
                for subset in itertools.combinations(others, m):
                    t = clean
                    for k in subset: t = walk(t, accs[k]).tables
                    permitted.append(walk(t, accs[j]))
            try:
                observed(permitted, F1, leaf, sample)
                hist[f'rounds: {accs[j]} then PTE {("clean", "A", "D?", "A+D")[ad_state(sample)]}'] += 1
            except ValueError as e: diffs.append(f'round {r} cpu{j} ({accs[j]}): PTE sample {sample:#x}: {e}')
        t = clean
        for acc in accs: t = walk(t, acc).tables
        final = fin.q(LOGF + 8 * r)
        if final != t[leaf]: diffs.append(f'round {r}: final PTE {final:#x}, the model has {t[leaf]:#x} after {accs}')
        hist[f'round final: {("clean", "A", "D?", "A+D")[ad_state(final)]}'] += 1
    # phase 2: the states reachable from a cleared PTE by reads and writes
    reach, todo = {F1 | BASE}, [F1 | BASE]
    while todo:
        pte = todo.pop(); t = dict(clean); t[leaf] = pte
        for acc in ('read', 'write'):
            new = walk(t, acc).tables[leaf]
            if new not in reach: reach.add(new); todo.append(new)
    allowed = {ad_state(x) for x in reach}
    for j in range(n):
        cap = min(0x2000, fin.q(LOG(0) + 0x3ff8) if j == 0 else p['iters'])
        data = fin.r[LOG(j)][0x1000:0x1000 + cap]
        for b in data:
            hist[f'free race: {"cpu0 " if j == 0 else ""}{"write" if b & 4 else "read"}{" after INVLPG" if b & 8 else ""} -> PTE {("clean", "A", "D?", "A+D")[b & 3]}'] += 1
            if b & 3 not in allowed: diffs.append(f'cpu{j}: free-race sample with A/D {b & 3} (the model reaches {sorted(allowed)})')
    diffs += [f'cpu{j}: ERR {fin.q(ERR(j))}' for j in range(n) if fin.q(ERR(j))]
    pte = fin.q(leaf)
    t = clean
    for acc in accs: t = walk(t, acc).tables
    if pte != t[leaf]: diffs.append(f'final PTE {pte:#x}, the model has {t[leaf]:#x}')
    return diffs[:10], hist


def frame_of(word, frames):
    for f, w in frames.items():
        if word == w: return f
    raise ValueError(f'a read of {word:#x} is the content of no frame')

def remap_cpu(rounds, ops, frames, tables, leaf, va, store_va, cache=None, newer=None):
    """One CPU's observations against the model. `ops`: per round r a dict {mode, pre: [(read word, store dest frame code)],
    post: (read word, store dest code)}, `frames`: {frame: its word at the read offset}, `newer(r)`: the frame the PTE maps in
    round r. A pre-INVLPG read or store may hit the cached translation (old frame) or a fresh walk (new frame) per the model's
    `access_outcomes`; a fresh walk replaces the cache; the CPU's own INVLPG drops it, after which only a fresh walk is
    permitted. -> histories Counter. Raises ValueError (check_observation)."""
    hist = collections.Counter(); cache = cache or cm.Cache()
    for r in range(rounds + 1):
        new, op = newer(r), ops[r]
        tables[leaf] = new['pte']
        if op['mode'] == 1: cache.invalidate(va & ~4095, 4096)
        for word, dest in op['pre'] if op['mode'] != 2 else []:
            frame = frame_of(word, frames)
            permitted = cm.access_outcomes(tables, CTX, va, 'read', cache)
            res = observed(permitted, frame)
            hist[f'pre-INVLPG read: {"new frame" if frame == new["frame"] else "old frame (stale translation)"}'] += 1
            if res.policy == 'fresh': cache.remember(CTX, res)
            dframe = {1: new['f1'], 2: new['f2']}.get(dest)
            permitted = cm.access_outcomes(tables, CTX, store_va, 'write', cache)
            res = observed(permitted, dframe)
            hist[f'pre-INVLPG store: {"new frame" if dframe == new["frame"] else "old frame (stale writable translation)"}'] += 1
            if res.policy == 'fresh': cache.remember(CTX, res)
        cache.invalidate(va & ~4095, 4096)
        word, dest = op['post']
        permitted = cm.access_outcomes(tables, CTX, va, 'read', cache, invalidated=True)
        res = observed(permitted, new['frame']); cache.remember(CTX, res)
        if frame_of(word, frames) != new['frame']: raise ValueError(f'round {r}: after INVLPG a read saw the old frame')
        permitted = cm.access_outcomes(tables, CTX, store_va, 'write', cache)
        observed(permitted, {1: new['f1'], 2: new['f2']}.get(dest))
        if {1: new['f1'], 2: new['f2']}.get(dest) != new['frame']: raise ValueError(f'round {r}: after INVLPG a store went to the old frame')
        hist['post-INVLPG read and store: new frame'] += 1
    return hist

def judge_remap(prog, out):
    init, fin, p, n = Regions.initial(prog), Regions.final(out), prog['params'], prog['ncpu']
    rounds, k = p['rounds'], p['k']
    leaf = P.sys_leaf(0, 0, V)
    frames = {F1: init.q(F1 + 0x80), F2: init.q(F2 + 0x80)}
    def newer(r):
        pte = init.q(VALUES + 8 * r)
        return {'pte': pte, 'frame': pte & ~0xfff & 0xffffffff, 'f1': F1, 'f2': F2}
    diffs, hist = [], collections.Counter()
    for j in range(n):
        ops = []
        for r in range(rounds + 1):
            base = LOG(j) + 0x100 * r
            pre = [(fin.q(base + 16 * i), fin.q(base + 16 * i + 8)) for i in range(k)]
            ops.append({'mode': init.q(MODES + 8 * r + j, 1), 'pre': pre, 'post': (fin.q(base + 16 * k), fin.q(base + 16 * k + 8))})
        tables = model_tables(init, [leaf])
        try: hist.update(remap_cpu(rounds, ops, frames, tables, leaf, V + 0x80, V + 0x200 + 8 * j, newer=newer))
        except ValueError as e: diffs.append(f'cpu{j}: {e}')
    diffs += [f'cpu{j}: ERR {fin.q(ERR(j))}' for j in range(n) if fin.q(ERR(j))]
    if fin.q(leaf) != init.q(VALUES + 8 * rounds): diffs.append(f'final PTE {fin.q(leaf):#x}')
    return diffs[:10], hist

def judge_large(prog, out):
    """Each CPU's observations of T (a page whose frame depends on the PDE's state) and of S (the same in both states)
    against the model's cache history, and after every round the exact A/D of the PDE (2 MiB leaf) and of T's PTE (table)."""
    init, fin, p, n = Regions.initial(prog), Regions.final(out), prog['params'], prog['ncpu']
    rounds, k, states = p['rounds'], LARGE_K, p['states']
    frames = {FA: init.q(FA + 0x80), FB: init.q(FB + 0x80)}
    sdata = init.q(S_PA + 0x80)
    base = model_tables(init, [PDE, T_LEAF, S_LEAF])
    newer = lambda r: {'pte': init.q(VALUES + 8 * r), 'frame': FB if states[r] else FA}
    diffs, hist = [], collections.Counter()
    for j in range(n):
        tables, cache, scache = dict(base), cm.Cache(), cm.Cache()
        try:
            for r in range(rounds + 1):
                new, at = newer(r), LOG(j) + 0x100 * r
                tables[PDE] = new['pte']
                mode = init.q(MODES + 8 * r + j, 1)
                if mode == 1: cache.invalidate(T_VA, 4096)
                for i in range(k if mode != 2 else 0):
                    frame = frame_of(fin.q(at + 16 * i), frames)
                    res = observed(cm.access_outcomes(tables, CTX, T_VA + 0x80, 'read', cache), frame)
                    hist[f'pre-INVLPG read of T: {"new state" if frame == new["frame"] else "old state (stale translation)"}'] += 1
                    if res.policy == 'fresh': cache.remember(CTX, res)
                    if fin.q(at + 16 * i + 8) != sdata: raise ValueError(f'round {r}: S read {fin.q(at + 16 * i + 8):#x}, not its content')
                    res = observed(cm.access_outcomes(tables, CTX, S_VA + 0x80, 'read', scache), S_PA)
                    if res.policy == 'fresh': scache.remember(CTX, res)
                cache.invalidate(T_VA, 4096)
                word, sword, dest = (fin.q(at + 16 * k), fin.q(at + 16 * k + 8), fin.q(at + 16 * k + 16))
                res = observed(cm.access_outcomes(tables, CTX, T_VA + 0x80, 'read', cache, invalidated=True), new['frame']); cache.remember(CTX, res)
                if frame_of(word, frames) != new['frame']: raise ValueError(f'round {r}: after INVLPG T read the old frame')
                if {1: FA, 2: FB}.get(dest) != new['frame']: raise ValueError(f'round {r}: after INVLPG the store went to {dest}, not the new frame')
                if sword != sdata: raise ValueError(f'round {r}: S read {sword:#x}, not its content')
                hist[f'post-INVLPG, {"table" if states[r] else "2 MiB leaf"}: T read and store hit the new frame'] += 1
        except ValueError as e: diffs.append(f'cpu{j}: {e}')
    # the exact A/D bytes after every round: the new PDE, then every CPU's walks of T (read, write) and S, in any order
    t = dict(base)
    for r in range(rounds + 1):
        t[PDE] = init.q(VALUES + 8 * r)
        for j in range(n):
            for va, acc in ((T_VA + 0x80, 'read'), (T_VA + 0x100 + 8 * j, 'write'), (S_VA + 0x80, 'read')): t = cm.walk(t, CTX, va, acc).tables
        got = (fin.q(LOGF + 16 * r), fin.q(LOGF + 16 * r + 8))
        if got != (t[PDE], t[T_LEAF]): diffs.append(f'round {r}: PDE/PTE {got[0]:#x}/{got[1]:#x}, the model has {t[PDE]:#x}/{t[T_LEAF]:#x}')
        hist[f'round final, {"table" if states[r] else "2 MiB leaf"}: PDE A+D {int(t[PDE] & 0x60 == 0x60)}, T PTE A+D {int(t[T_LEAF] & 0x60 == 0x60)}'] += 1
    diffs += [f'cpu{j}: ERR {fin.q(ERR(j))}' for j in range(n) if fin.q(ERR(j))]
    if (fin.q(PDE), fin.q(T_LEAF)) != (t[PDE], t[T_LEAF]): diffs.append('final PDE/PTE differ from the model')
    return diffs[:10], hist

def judge_alias(prog, out):
    init, fin, p, n = Regions.initial(prog), Regions.final(out), prog['params'], prog['ncpu']
    l1, l2 = P.sys_leaf(0, 0, V), P.sys_leaf(0, 0, V2)
    clean = model_tables(init, [l1, l2]); clean[l1] = clean[l2] = F1 | BASE
    diffs, hist = [], collections.Counter()
    for r in range(1, p['rounds'] + 1):
        t = clean
        for j in range(n):
            b = init.q(ROLES + 8 * r + j, 1)
            wa, ra, va = b & 3, b >> 2 & 3, b >> 4 & 3
            if ra: t = cm.walk(t, CTX, (V, V2)[ra - 1] + 0x80, 'read').tables
            t = cm.walk(t, CTX, (V, V2)[wa - 1] + 0x100 + 8 * j, 'write').tables
            if va:
                for k in range(n): t = cm.walk(t, CTX, (V, V2)[va - 1] + 0x100 + 8 * k, 'read').tables
        got = (fin.q(LOGF + 16 * r), fin.q(LOGF + 16 * r + 8))
        if got != (t[l1], t[l2]): diffs.append(f'round {r}: PTEs {got[0]:#x}/{got[1]:#x}, the model has {t[l1]:#x}/{t[l2]:#x}')
        hist[f'round final: V {("clean", "A", "D?", "A+D")[ad_state(got[0])]}, V2 {("clean", "A", "D?", "A+D")[ad_state(got[1])]}'] += 1
    diffs += [f'cpu{j}: ERR {fin.q(ERR(j))} (a word written through one alias not seen through another)' for j in range(n) if fin.q(ERR(j))]
    return diffs[:10], hist

def judge_contended(prog, out):
    init, fin, p, n = Regions.initial(prog), Regions.final(out), prog['params'], prog['ncpu']
    data, fetch = p['variant'] in ('data', 'both'), p['variant'] in ('fetch', 'both')
    lv, lc = P.sys_leaf(0, 0, V), P.sys_leaf(0, 0, CODE)
    t = model_tables(init, [lv, lc]); t[lv] = F1 | BASE; t[lc] = CODE | BASE
    for j in range(1, n):
        if data: t = cm.walk(t, CTX, V + 0x100 + 8 * j, 'write').tables
        if fetch: t = cm.walk(t, CTX, CODE, 'fetch').tables
    diffs = []
    if data and fin.q(lv) != t[lv]: diffs.append(f'data PTE {fin.q(lv):#x}, the model has {t[lv]:#x}')
    if fetch and fin.q(lc) != t[lc]: diffs.append(f'code PTE {fin.q(lc):#x}, the model has {t[lc]:#x}')
    diffs += [f'cpu{j}: ERR {fin.q(ERR(j))}' for j in range(n) if fin.q(ERR(j))]
    diffs += [f'cpu{j} did not finish its iterations (done flag {fin.q(DONE(j))})' for j in range(1, n) if fin.q(DONE(j)) != 1]
    return diffs, collections.Counter({'final PTE bytes match the model': 0 if diffs else 1})

JUDGES = {'ad-race': judge_ad_race, 'remap': judge_remap, 'large': judge_large, 'alias': judge_alias, 'contended': judge_contended}

def judge(prog, out):
    check = prog['params']['check']
    return next(f for k, f in JUDGES.items() if check.startswith(k))(prog, out)

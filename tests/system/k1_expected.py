"""The expected final state of the K1 programs that were compared with QEMU (tests/system/run_k1.py), written from the programs:
each call of V-like code (`mov eax, k; jmp r13`) leaves rax = k, which the body keeps in r8, r9, ...; the register file
then ends in programs.RESTORE (rax 0, rcx 0xC0000080, rdx 0, `cmp r11, r11`) and r13 holds the last return label. The
memory compared is the program's captured regions (the leaf PTEs of the CPU's data pages in table A, the code frames it
rewrites, the #DB log) after the stores the body makes.

    expected(name, n, k) -> the state smp.parse_dump would give (tools/bench/smp/expect.py)

Every rule here was checked against QEMU 11.1.2's answers (tests/system/test_k1_expected.py: the hash of each case's
projection equals the answer the system family locked when QEMU still decided these programs)."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'tools' / 'reference'), str(ROOT / 'tools' / 'bench' / 'smp')]
import expect, programs as P  # noqa: E402
import run_k1 as K  # noqa: E402

SS0 = 0x10                     # the stub's CPL-0 data selector (stub_smp.asm: GDT entry 0x10, loaded into SS)
PF_FETCH, PF_WRITE = 0x11, 0x3  # #PF error codes: a present page's instruction fetch (P, I/D); a present page's write (P, W/R)


def _leaf(i, va, value, t=0):
    return expect.q(P.sys_leaf(i, t, va), value)


def _rules(name, i, n, k):
    """(registers, stores) of CPU i: the values the body leaves (beyond RESTORE's) and the bytes it writes."""
    d = P.SYS_D(i)
    F1, F2, LEAF = d + K.F1, d + K.F2, P.LEAF
    pg = K.l1_pages(i)
    code = lambda at, v: (at, K.CODE(v)[:8])       # a frame's `mov eax, v; jmp r13` rewritten by one 8-byte store
    # Perf chunk 8's handshake (`hand`): CPU (i + 1) % N writes a byte of CPU i's F1 (CPU i's own at N = 1).
    peer_byte = lambda at, v: [(P.SYS_D(j) + K.F1 + at, bytes([v])) for j in range(n)]
    adv = name.startswith('adv-')
    if not adv:
        return {
            'remap-invlpg': ({'r8': 1, 'r9': 2, 'r10': 1}, []),
            'remap-invlpg-a32': ({'r8': 1, 'r9': 2, 'r10': 1}, []),
            'remap-cr3': ({'r8': 1, 'r9': 2, 'r10': 1}, []),
            'cr3-switch': ({'r8': k, 'r9': 2 * k}, []),
            'dormant-write': ({'r8': 1, 'r9': 2, 'r10': 3, 'r12': 1}, [code(F2, 3)]),
            'remap-link': ({'r8': 3, 'r9': 6}, [_leaf(i, d, F2 | LEAF)]),
            'cr3-link': ({'r8': k, 'r9': 2 * k}, []),
            'cross': ({'r8': 0x100, 'r9': 0x200, 'r10': 0x300}, [_leaf(i, d + 0x4000, (d + K.XC) | LEAF)]),
            'straddle': ({'r8': 0x100, 'r9': 0x200}, [_leaf(i, 0x200000, (d + K.XB) | LEAF)]),
            'cross-link': ({'r8': 3 * 0x100, 'r9': 3 * 0x200}, [_leaf(i, d + 0x4000, (d + K.XB) | LEAF)]),
            # T (`mov eax, 1`) three times per phase; the peer's store makes its immediate 3 before the last phase.
            'local-write': ({'r8': 3, 'r9': 3, 'r10': 9}, peer_byte(K.LW + 0x41, 3)),
            # S (`mov esi, 1; jmp T`), T `mov eax, 1`: three calls, then S's immediate made 3, three calls again.
            'kill-out': ({'r8': 3, 'r11': 1, 'r9': 3, 'rsi': 3, 'r12': 3}, peer_byte(K.LW + 1, 3)),
            # A, B, C, D run `mov eax, 1, 2, 3, 4`; A2 0x11, B2 0x22 (l1_memory).
            'l1-pair': ({'r8': k, 'r9': 2 * k, 'r10': k, 'r12': 4 * k}, []),
            'l1-triple': ({'r8': k, 'r9': 2 * k, 'r10': 3 * k, 'r12': 1, 'r14': 2}, []),
            'l1-triple-d': ({'r8': k, 'r9': 2 * k, 'r10': 4 * k}, []),
            'l1-pair-invlpg': ({'r8': 1, 'r9': 2, 'r10': 0x22, 'r12': 1, 'r14': 0x11, 'r15': 0x22}, []),
            'l1-pair-cr3': ({'r8': 1, 'r9': 2, 'r10': 1, 'r12': 0x22, 'r14': 2, 'r15': 1}, []),
            'l1-pair-write': ({'r8': 1, 'r9': 2, 'r10': 0x22, 'r12': 0x11, 'r14': 3, 'r15': 0x33},
                              [code(pg['FB'], 0x22), code(pg['FA'], 0x11), code(pg['FC'], 0x33)]),
            'l1-global': ({'r8': 1, 'r9': 2, 'r10': k, 'r12': 2 * k}, []),
            # W = `b8 00` + its next page's three bytes: XA's (1 0 0) -> 0x100; under B, PCX's `b8 07` -> 0x107; XA + 0x100 runs 0x11.
            'page-cross': ({'r8': 0x100, 'r9': 0x11, 'r10': 0x107, 'r12': 0x100}, []),
            'miss-counts': ({'r8': k, 'r9': 2 * k, 'r10': 3 * k}, []),
            'miss-step': _miss_step(i),
            # DR0 = A: the #DB is a fault taken before A's first instruction, twice (RF resumes it); the log holds A twice.
            'miss-dr7': ({'r8': 1, 'r9': 1, 'rdi': d + K.LOGP + 16}, [expect.q(d + K.LOGP, pg['A']), expect.q(d + K.LOGP + 8, pg['A'])]),
            # `mov bx, ss` (16 bits) then the calls of A after MOV SS and after STI.
            'miss-boundary': ({'rbx': SS0, 'r8': 1, 'r9': 1}, []),
        }[name]
    name = name[4:]
    N, G1 = d + 0x8000, d + 0x9000
    # The faulting call of the NX/SMEP/U-S programs: the handler (TO_R13) takes the error code into r14 and CR2 into r15
    # and resumes at the call's return label, where rax still holds V (the jump's target): r9 = V.
    pf = {'r8': 1, 'r9': d, 'r10': 1, 'r14': PF_FETCH, 'r15': d}
    code_ab = int.from_bytes(K.CODE(1)[:8], 'little')            # the qword at a frame holding `mov eax, 1; jmp r13`
    return {
        'nx-invlpg': (pf, []), 'nx-cr3': (pf, []), 'smep': (pf, []), 'us-invlpg': (pf, []),
        # The code at L stores rbx into L's PTE and invalidates it: the instruction after runs from the new frame.
        'self-invlpg': ({'rbx': (d + 0xa000) | LEAF, 'r8': 1, 'r9': 2}, [_leaf(i, d + 0x8000, (d + 0xa000) | LEAF)]),
        'self-cr3': ({'rbx': P.SYS_T(i, 0), 'r8': 1, 'r9': 2, 'r10': 1}, []),
        'self-pge': ({'rbx': (d + 0xa000) | LEAF | K.G, 'r8': 1, 'r9': 1, 'r10': 2}, [_leaf(i, d + 0x8000, (d + 0xa000) | LEAF | K.G)]),
        # F1 at V (table A) and V2 (table B): 1, 1, then 1 again added to r8; F1 rewritten to `mov eax, 3`: 3 at both.
        'xmc-shared': ({'r8': 2, 'r9': 1, 'r10': 3, 'r12': 3}, [code(F1, 3)]),
        'mixed-g0': ({'r8': 0x100, 'r9': 0x100, 'r10': 0x200}, []),
        'fa-read-code-r': ({'r8': 1, 'r9': 1, 'r10': code_ab}, []),
        'fa-read-code': ({'r8': 1, 'r9': 2, 'r10': code_ab}, [(G1 + 1, b'\x02')]),
        'fa-read-smc': ({'r8': 1, 'r9': 2, 'r10': code_ab}, [(G1 + 1, b'\x02')]),
        # N's PTE G1 | P RW US, A = D = 0: the read sets A (r8), the first write D (r9), the second changes nothing (r10).
        'fa-ad0': ({'r8': G1 | 0x27, 'r9': G1 | 0x67, 'r10': G1 | 0x67, 'r12': 0x55},
                   [_leaf(i, N, G1 | 0x67), expect.q(G1 + 8, 0x55), expect.q(G1 + 16, 0x66)]),
        # A qword store across N's page end: its high half is the next page's first dword (r12).
        'fa-rw': ({'r8': 0x1122, 'r10': 0x0123456789abcdef, 'r12': 0x01234567},
                  [expect.q(N + 8, 0x1122), expect.q(N + 0xffc, 0x0123456789abcdef)]),
        # R/W = 0: a write faults while CR0.WP = 1 (#PF 3, CR2 the store's address), succeeds while WP = 0.
        'fa-ro-wp': ({'r8': PF_WRITE, 'r9': N + 8, 'r10': PF_WRITE, 'r14': PF_WRITE, 'r15': N + 16}, [expect.q(N + 8, 0x77)]),
        'fa-wt': ({'r8': 0x1234}, [expect.q(G1 + 8, 0x1234)]),
    }[name]


def _miss_step(i):
    """TF set by POPF: the trap comes after each instruction from the one after POPF's successor on (SDM Vol.3 18.3.1.4):
    after `lea r13`, `mov rax, A` (5 bytes, nasm's `mov eax, imm32`), `jmp rax` (2), A's `mov eax, 1` (5) and its
    `jmp r13` back to .m1; the handler logs each next RIP and clears TF at the fifth."""
    d, A = P.SYS_D(i), K.l1_pages(i)['A']
    m1 = P.SYS_CODE(i) + expect.labels(P.sys_text(i, K.bodies(50, i)['miss-step']))['.m1']
    log = [m1 - 7, m1 - 2, A, A + 5, m1]
    return {'rdi': d + K.LOGP + 8 * len(log)}, [expect.q(d + K.LOGP + 8 * j, v) for j, v in enumerate(log)]


def text_of(name, i, n, k):
    return P.sys_text(i, K.adv_setup(i)[name[4:]][0] if name.startswith('adv-') else K.bodies(k, i, n)[name])


def expected(name, n, k, prog):
    """`name`: a K1 program (`adv-` + name for the adversarial ones) at n CPUs and k rounds; `prog` its program."""
    cpus, writes = [], []
    for i in range(n):
        regs, stores = _rules(name, i, n, k)
        text = text_of(name, i, n, k)
        r13 = expect.last_r13(text)
        if r13: regs = {'r13': P.SYS_CODE(i) + expect.labels(text)[r13], **regs}
        cpus.append({**expect.RESTORED, **regs})
        writes += stores
    return expect.state(prog, cpus, writes)


def names(k=50):
    """The K1 programs compared with a whole expected state (the rest are judged by run_k1.EITHER / ADV_EITHER)."""
    return ([x for x in K.bodies(k) if x not in K.L1_VARIANTS and x not in K.EITHER]
            + ['adv-' + x for x in K.adv_setup(0) if x not in K.ADV_EITHER])

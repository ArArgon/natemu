#!/usr/bin/env python3
"""K1 checks: the code cache across CR3 loads, INVLPG and control changes (boot-features plan, K1).

    tools/with-env python3 tools/run_k1.py --fexc FEXC --out DIR [--cpus 1 2 3 4] [--k 50]
        [--host HOST --runtime RUNTIME] [--base-host HOST --base-runtime RUNTIME] [--programs a,b]

Each CPU i of N runs the same body (`programs.sys_text`: CPL 0, its own tables A and B at `programs.SYS_T(i, k)`) and
calls the code at linear V = SYS_D(i) (`lea r13, ...; jmp` V; the code is `mov eax, k; jmp r13`), which table A maps to
frame F1 (k = 1) and table B to F2 (k = 2):
- `remap-invlpg`: V's PTE in A changed to F2, then INVLPG: the next call runs F2's bytes; back to F1, INVLPG: F1's.
- `remap-cr3`: the same change followed by a CR3 reload instead of INVLPG.
- `cr3-switch`: K rounds of CR3 A, call, CR3 B, call: each space's own bytes every time (r8 = K, r9 = 2K).
- `dormant-write`: F2's code rewritten through its identity address while A is loaded (the block from it idle in the
  cache): back in B the call runs the new bytes.
- `remap-link`, `cr3-link`: `remap-invlpg` (three calls each side) and `cr3-switch` through direct jumps, which the
  linker links to V's block: a link into a block whose mapping changed must not be taken.
- `cross`: an instruction crossing into a remapped page: INVLPG of its second page alone, then a third frame with a CR3
  reload (only the second page's walk tells the blocks apart).
- `straddle`: the same at 0x1ffffe, across a 2 MiB boundary (the INVLPG region filter must see the block's last byte).
These eight run at block sizes default and 1 and are compared with QEMU system mode (`-accel tcg,thread=multi`) at N CPUs.
- `no-invlpg` (natemu only, the TLB rule): the PTE changed with no invalidation: the next call may run either frame's
  bytes; after INVLPG, F2's.
- `global` (natemu only): CR4.PGE on, A's V global: after a CR3 reload (the block found global) and two loads of B the
  architecture allows either frame (MOV CR3 keeps global entries, SDM Vol.3 4.10.4.1), and natemu must keep the global
  block: r9 = r10 = 1 asserted; after a CR4.PGE toggle, F2's; A's PTE then changed to F2 with a CR3 reload: either; after
  INVLPG, F2's.
- `walk` (natemu only): the revalidation walk under B writes the A bit of a PTE on the block's own frame, the PTE being
  the instruction's immediate: the block dies in its own lookup and the new bytes run.
- `pf-remap` (natemu only): V made not present without INVLPG, a load faults, the handler maps F2 without INVLPG: the #PF
  invalidated V, so the call runs F2's bytes.
- the independent review's adversarial programs (`adv_setup`: NX, SMEP and U/S changes, code remapping its own page,
  one frame at two linear addresses rewritten, global/non-global crossings, a fetch #PF then remap), against QEMU but
  `pf-fetch` (either outcome by the TLB rules).
- `counts`: `cr3-switch` at K and 2K rounds on 1 vCPU: the resident's translation requests and the host's compiles must
  not grow with the rounds (K1: a CR3 load keeps translations); with `--base-host/--base-runtime` the same counts for
  another build (before K1) are reported beside them.

Writes DIR/k1.json; exit status 1 on any failure."""
import argparse, json, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'tools' / 'reference'), str(ROOT / 'tools' / 'bench' / 'smp')]
import run_slice1_vm as vm, run_slice3_vm as s3, smp, programs as P  # noqa: E402

F1, F2 = 0x1000, 0x2000
CODE = lambda k: P.asm(f'mov eax, {k}\njmp r13') + b'\x90' * 8
FIELDS = ('r8', 'r9', 'r10', 'r12', 'r14', 'r15', 'rbx', 'rsi', 'rdi', 'rbp')


def call(tag, to='{V:#x}'):
    return f'    lea r13, [rel .c{tag}]\n    mov rax, {to}\n    jmp rax\n.c{tag}:\n'


def direct(tag):
    """A call through a direct jump (a literal exit, linked to V's block after its first run)."""
    # (The body is assembled at 0 and placed at SYS_CODE(i): `$$ + Vrel - 0x234` is V.)
    return f'    lea r13, [rel .c{tag}]\n    jmp $$+{{Vrel:#x}}-0x234\n.c{tag}:\n'


# Per CPU i (d = SYS_D(i)): `cross` runs `mov eax, imm32; jmp r13` from d + 0x3ffe, its last three immediate bytes and the
# jmp on linear d + 0x4000, which table A maps to frame XA (imm 0x100), XB (0x200) or XC (0x300); `straddle` the same at 0x1ffffe, its
# second page 0x200000 (another 2 MiB region) mapped to XA or XB; `walk` code on frame Q (linear d + 0x6000 in both
# tables) whose 8-byte immediate is table B's leaf PTE for that page (table B's page table for the region is Q itself).
XA, XB, XC, WQ = 0x5000, 0x6000, 0x8000, 0x6000
TAIL = lambda imm: bytes([imm, 0, 0]) + P.asm('jmp r13')
def walk_at(i):
    """(Q, offset of the PTE in Q, the code's linear address)."""
    q = P.SYS_D(i) + WQ
    pte = (q >> 12 & 511) * 8
    return q, pte, q + pte - 2


def bodies(k, i=0):
    d = P.SYS_D(i)
    leaf = lambda va: P.sys_leaf(i, 0, va)
    put = lambda va, frame: f'    mov rax, {frame | P.LEAF:#x}\n    mov [{leaf(va):#x}], rax\n'
    inv = lambda va: f'    mov rax, {va:#x}\n    invlpg [rax]\n'
    q, _, wcode = walk_at(i)
    pte = lambda f: f'    mov rax, {{d:#x}}+{f:#x}|{P.LEAF:#x}\n    mov [{{la:#x}}], rax\n'
    pte_g = lambda f: f'    mov rax, {{d:#x}}+{f:#x}|{P.LEAF | 0x100:#x}\n    mov [{{la:#x}}], rax\n'
    invlpg = '    mov rax, {V:#x}\n    invlpg [rax]\n'
    cr3 = lambda t: f'    mov rax, {{{t}:#x}}\n    mov cr3, rax\n'
    pge = lambda on: f'    mov rax, cr4\n    {"bts" if on else "btr"} rax, 7\n    mov cr4, rax\n'
    code3 = int.from_bytes(CODE(3)[:8], 'little')
    return {
        'remap-invlpg': call(0) + '    mov r8, rax\n' + pte(F2) + invlpg + call(1) + '    mov r9, rax\n' + pte(F1) + invlpg + call(2) + '    mov r10, rax\n',
        'remap-cr3': call(0) + '    mov r8, rax\n' + pte(F2) + cr3('ta') + call(1) + '    mov r9, rax\n' + pte(F1) + cr3('ta') + call(2) + '    mov r10, rax\n',
        'cr3-switch': f'    mov ecx, {k}\n.l:\n' + cr3('ta') + call(0) + '    add r8, rax\n' + cr3('tb') + call(1) + '    add r9, rax\n    dec ecx\n    jnz .l\n',
        'dormant-write': (call(0) + '    mov r8, rax\n' + cr3('tb') + call(1) + '    mov r9, rax\n' + cr3('ta')
                          + f'    mov rax, {code3:#x}\n    mov [{{F2:#x}}], rax\n' + cr3('tb') + call(2) + '    mov r10, rax\n'
                          + cr3('ta') + call(3) + '    mov r12, rax\n'),
        # The same through linked direct jumps (three rounds each side, so the link exists before the change).
        'remap-link': (f'    mov ecx, 3\n.a:\n' + direct(0) + '    add r8, rax\n    dec ecx\n    jnz .a\n' + pte(F2) + invlpg
                       + f'    mov ecx, 3\n.b:\n' + direct(1) + '    add r9, rax\n    dec ecx\n    jnz .b\n'),
        'cr3-link': (f'    mov ecx, {k}\n.l:\n' + cr3('ta') + direct(0) + '    add r8, rax\n' + cr3('tb') + direct(1)
                     + '    add r9, rax\n    dec ecx\n    jnz .l\n'),
        # An instruction crossing into a remapped page: INVLPG of its second page alone; then a third frame and a CR3
        # reload (both blocks have the right first page: only the second page's walk tells them apart).
        'cross': (call(0, f'{d + 0x3ffe:#x}') + '    mov r8, rax\n' + put(d + 0x4000, d + XB) + inv(d + 0x4000) + call(1, f'{d + 0x3ffe:#x}')
                  + '    mov r9, rax\n' + put(d + 0x4000, d + XC) + cr3('ta') + call(2, f'{d + 0x3ffe:#x}') + '    mov r10, rax\n'),
        # The same across a 2 MiB boundary: an INVLPG in the second region must find the block that starts in the first.
        'straddle': call(0, '0x1ffffe') + '    mov r8, rax\n' + put(0x200000, d + XB) + inv(0x200000) + call(1, '0x1ffffe') + '    mov r9, rax\n',
        'no-invlpg': call(0) + '    mov r8, rax\n' + pte(F2) + call(1) + '    mov r9, rax\n' + invlpg + call(2) + '    mov r10, rax\n',
        'global': (pge(True) + call(0) + '    mov r8, rax\n' + cr3('ta') + call(1) + '    mov rbx, rax\n' + cr3('tb') + call(2) + '    mov r9, rax\n'
                   + cr3('tb') + call(3) + '    mov r10, rax\n' + pge(False) + pge(True) + call(4) + '    mov r12, rax\n'
                   + cr3('ta') + call(5) + '    mov rsi, rax\n' + pte_g(F2) + cr3('ta') + call(6) + '    mov rdi, rax\n'
                   + invlpg + call(7) + '    mov rbp, rax\n'),
        # The revalidation walk under table B writes the A bit of a PTE on the block's own frame: the block dies in its
        # own lookup, and the bytes run are the new ones (the walk precedes the fetch).
        'walk': (call(0, f'{wcode:#x}') + '    mov r8, rax\n' + cr3('tb') + call(1, f'{wcode:#x}') + '    mov r9, rax\n'
                 + cr3('ta') + call(2, f'{wcode:#x}') + '    mov r10, rax\n'),
        # V made not present without INVLPG; a load from it faults (no data shadow: V was only fetched), the handler maps
        # it to F2 (no INVLPG) and returns: the #PF invalidated V's translations (SDM Vol.3 4.10.4.1), so V runs F2's bytes.
        'pf-remap': (f'    lea rax, [rel .h]\n    mov [{d + IDT + 0xe0:#x}], ax\n    mov word [{d + IDT + 0xe2:#x}], 8\n'
                     f'    mov word [{d + IDT + 0xe4:#x}], 0x8e00\n    shr rax, 16\n    mov [{d + IDT + 0xe6:#x}], ax\n    shr rax, 16\n'
                     f'    mov [{d + IDT + 0xe8:#x}], eax\n    mov word [{d + IDT + 0x800:#x}], 0xfff\n    mov rax, {d + IDT:#x}\n'
                     f'    mov [{d + IDT + 0x802:#x}], rax\n    lidt [{d + IDT + 0x800:#x}]\n    xor r14d, r14d\n'
                     + call(0) + '    mov r8, rax\n' + f'    mov rax, {(d + F1) | P.LEAF & ~1:#x}\n    mov [{leaf(d):#x}], rax\n'
                     + f'    mov rbx, [{d:#x}]\n' + call(1) + '    mov r9, rax\n    jmp .end\n'
                     + f'.h:\n    mov rax, {(d + F2) | P.LEAF:#x}\n    mov [{leaf(d):#x}], rax\n    inc r14\n    add rsp, 8\n    iretq\n.end:\n'),
    }

IDT = 0x3000
# Natemu-only programs: (register, allowed values) per CPU; a function of the CPU for `walk`. In `global` the architecture
# allows either frame for r9 and r10 (a kept global entry, or a miss); natemu keeps the block found global (K1), so 1.
EITHER = {
    'no-invlpg': {'r8': {1}, 'r9': {1, 2}, 'r10': {2}},
    'global': {'r8': {1}, 'rbx': {1}, 'r9': {1}, 'r10': {1}, 'r12': {2}, 'rsi': {1}, 'rdi': {1, 2}, 'rbp': {2}},
    'walk': lambda i: {'r8': {walk_at(i)[0] | 7}, 'r9': {walk_at(i)[0] | 0x27}, 'r10': {walk_at(i)[0] | 0x27}},
    'pf-remap': {'r8': {1}, 'r14': {1}, 'r9': {2}},
}


def program(name, n, k):
    out = {'format': 'natemu-smp-v1', 'name': f'k1-{name}@{n}', 'ncpu': n, 'system': True, 'bodies': [], 'cpus': [], 'memory': [], 'params': {}}
    for i in range(n):
        d = P.SYS_D(i)
        a = {d: (d + F1) | P.LEAF | (0x100 if name == 'global' else 0), d + 0x4000: (d + XA) | P.LEAF}
        if name == 'straddle':
            a[0x200000] = (d + XA) | P.LEAF
        b = {d: (d + F2) | P.LEAF}
        out['bodies'].append(P.body(P.SYS_CODE(i), P.sys_text(i, bodies(k, i)[name])))
        out['cpus'].append(dict(P.cpu(i, P.SYS_CODE(i)), cpl=0))
        tb = P.sys_tables(i, 1, n, b)
        extra = []
        if name == 'walk':
            # Table B's page directory: region 0 as before, region 1 (0x200000-0x3fffff) through Q (A set: only the leaf's
            # A is clear); Q holds `mov rax, imm64` (imm64 = Q's own leaf PTE, A clear) and `jmp r13`.
            q, pte, _ = walk_at(i)
            base = P.SYS_T(i, 1)
            tb = [r for r in tb if r['addr'] != f'0x{base + 0x2000:x}']
            extra.append({'addr': f'0x{base + 0x2000:x}', 'data': b''.join((t | 0x27).to_bytes(8, 'little') for t in (base + 0x3000, q)).hex()})
            page = bytearray(0x1000)
            page[pte - 2:pte + 11] = b'\x48\xb8' + (q | 7).to_bytes(8, 'little') + P.asm('jmp r13')
            extra.append({'addr': f'0x{q:x}', 'data': page.hex()})
        else:
            extra += [{'addr': f'0x{d + x:x}', 'data': TAIL(k).hex()} for k, x in ((1, XA), (2, XB), (3, XC))]
        if name == 'straddle':
            extra += [{'addr': f'0x{P.sys_leaf(i, 0, 0x1ff000):x}', 'data': (0x1ff000 | P.LEAF).to_bytes(8, 'little').hex(), 'capture': False}]
            extra += [{'addr': '0x1ffffe', 'data': 'b800', 'capture': False}] if i == 0 else []   # shared by every CPU
        if name == 'pf-remap':
            extra += [dict(P.zero(d + IDT, 0x1000), capture=False)]
        out['memory'] += P.sys_tables(i, 0, n, a) + tb + [
            P.zero(d, 0x1000), {'addr': f'0x{d + F1:x}', 'data': CODE(1).hex()}, {'addr': f'0x{d + F2:x}', 'data': CODE(2).hex()},
            {'addr': f'0x{d + 0x3ffe:x}', 'data': 'b800'}, dict(P.zero(d + P.SAVE, 8), capture=False)] + extra
    return out


# ---- The independent review's adversarial programs (QEMU-compared unless in ADV_EITHER) ----------------------------------
# nx-invlpg/nx-cr3: NX set by a PTE change + INVLPG / + CR3 reload: the next fetch faults (#PF 0x11, CR2 = V); smep: SMEP set
# (a global flush): a supervisor fetch from the user page faults; us-invlpg: V made user + INVLPG under SMEP faults;
# self-invlpg/self-cr3/self-pge: code that remaps its own page then invalidates (INVLPG, CR3 reload, PGE toggle): the next
# round runs from the new frame; xmc-shared: one frame at V in A and V2 in B, rewritten: both run the new bytes;
# mixed-g0/mixed-g1: an instruction crossing between a global and a non-global page: after a CR3 load, the new bytes;
# pf-fetch: a fetch #PF elsewhere on V's page, the handler remaps V without INVLPG: V then runs F2's bytes.
XD, G, US = 1 << 63, 0x100, 4


def adv_idt(d):
    return (f'    lea rax, [rel .h]\n    mov [{d + IDT + 0xe0:#x}], ax\n    mov word [{d + IDT + 0xe2:#x}], 8\n'
            f'    mov word [{d + IDT + 0xe4:#x}], 0x8e00\n    shr rax, 16\n    mov [{d + IDT + 0xe6:#x}], ax\n    shr rax, 16\n'
            f'    mov [{d + IDT + 0xe8:#x}], eax\n    mov word [{d + IDT + 0x800:#x}], 0xfff\n    mov rax, {d + IDT:#x}\n'
            f'    mov [{d + IDT + 0x802:#x}], rax\n    lidt [{d + IDT + 0x800:#x}]\n    xor r14d, r14d\n    xor r15d, r15d\n')

# #PF handler that records the error code (r14) and CR2 (r15) and resumes at r13 (the call's return label).
TO_R13 = '.h:\n    pop r14\n    mov r15, cr2\n    mov [rsp], r13\n    iretq\n'


def adv_call(tag, to):
    return f'    lea r13, [rel .c{tag}]\n    mov rax, {to:#x}\n    jmp rax\n.c{tag}:\n'


def adv_setup(i):
    """(body text, A overrides, B overrides, extra memory) per program for CPU i."""
    d = P.SYS_D(i)
    V, F1, F2 = d, d + 0x1000, d + 0x2000
    la = lambda va: P.sys_leaf(i, 0, va)
    put = lambda va, val: f'    mov rax, {val:#x}\n    mov [{la(va):#x}], rax\n'
    inv = lambda va: f'    mov rax, {va:#x}\n    invlpg [rax]\n'
    cr3 = lambda t: f'    mov rax, {P.SYS_T(i, t):#x}\n    mov cr3, rax\n'
    cr4 = lambda op, bit: f'    mov rax, cr4\n    {op} rax, {bit}\n    mov cr4, rax\n'
    end = '    jmp .end\n'
    code_mem = lambda va, data: {'addr': f'0x{va:x}', 'data': data.hex()}
    p = {}
    # NX applied by PTE change + INVLPG / + CR3 reload: the next fetch faults (#PF 0x11, CR2 = V).
    for name, flush in (('nx-invlpg', inv(V)), ('nx-cr3', cr3(0))):
        p[name] = (adv_idt(d) + adv_call(0, V) + '    mov r8, rax\n' + put(V, F1 | P.LEAF | XD) + flush + adv_call(1, V) + '    mov r9, rax\n'
                   + put(V, F1 | P.LEAF) + inv(V) + adv_call(2, V) + '    mov r10, rax\n' + end + TO_R13 + '.end:\n',
                   {V: F1 | P.LEAF}, {}, [code_mem(F1, CODE(1))])
    # SMEP on (global flush): a supervisor fetch from the user page V faults; off again: runs.
    p['smep'] = (adv_idt(d) + adv_call(0, V) + '    mov r8, rax\n' + cr4('bts', 20) + adv_call(1, V) + '    mov r9, rax\n' + cr4('btr', 20)
                 + adv_call(2, V) + '    mov r10, rax\n' + end + TO_R13 + '.end:\n', {V: F1 | P.LEAF}, {}, [code_mem(F1, CODE(1))])
    # SMEP on, V supervisor: runs; V made user + INVLPG: faults; supervisor again + INVLPG: runs.
    p['us-invlpg'] = (adv_idt(d) + cr4('bts', 20) + adv_call(0, V) + '    mov r8, rax\n' + put(V, F1 | P.LEAF) + inv(V) + adv_call(1, V)
                      + '    mov r9, rax\n' + put(V, F1 | P.LEAF & ~US) + inv(V) + adv_call(2, V) + '    mov r10, rax\n' + cr4('btr', 20)
                      + end + TO_R13 + '.end:\n', {V: F1 | P.LEAF & ~US}, {}, [code_mem(F1, CODE(1))])
    # Code that remaps its own page then INVLPGs it (or reloads CR3, or toggles PGE): the instruction after runs from the
    # new frame, round 2, after round 1 cached it from the old one.
    L, G1, G2 = d + 0x8000, d + 0x9000, d + 0xa000
    def selfcode(k, tail):
        return P.asm(f'bits 64\n{tail}\n    mov eax, {k}\n    jmp r13\n')
    inv_tail = f'    mov [{la(L):#x}], rbx\n    mov rax, {L:#x}\n    invlpg [rax]'
    p['self-invlpg'] = ('    mov rbx, %#x\n' % (G1 | P.LEAF) + adv_call(0, L) + '    mov r8, rax\n' + '    mov rbx, %#x\n' % (G2 | P.LEAF) + adv_call(1, L)
                        + '    mov r9, rax\n', {L: G1 | P.LEAF}, {}, [code_mem(G1, selfcode(1, inv_tail)), code_mem(G2, selfcode(2, inv_tail))])
    cr3_tail = '    mov rax, rbx\n    mov cr3, rax'
    p['self-cr3'] = (f'    mov rbx, {P.SYS_T(i, 0):#x}\n' + adv_call(0, L) + '    mov r8, rax\n' + f'    mov rbx, {P.SYS_T(i, 1):#x}\n' + adv_call(1, L)
                     + '    mov r9, rax\n' + f'    mov rbx, {P.SYS_T(i, 0):#x}\n' + adv_call(2, L) + '    mov r10, rax\n',
                     {L: G1 | P.LEAF}, {L: G2 | P.LEAF}, [code_mem(G1, selfcode(1, cr3_tail)), code_mem(G2, selfcode(2, cr3_tail))])
    pge_tail = f'    mov [{la(L):#x}], rbx\n    mov rax, cr4\n    btc rax, 7\n    mov cr4, rax\n    btc rax, 7\n    mov cr4, rax'
    p['self-pge'] = (cr4('bts', 7) + '    mov rbx, %#x\n' % (G1 | P.LEAF | G) + adv_call(0, L) + '    mov r8, rax\n' + cr3(0) + adv_call(1, L) + '    mov r9, rax\n'
                     + '    mov rbx, %#x\n' % (G2 | P.LEAF | G) + adv_call(2, L) + '    mov r10, rax\n' + cr4('btr', 7),
                     {L: G1 | P.LEAF | G}, {}, [code_mem(G1, selfcode(1, pge_tail)), code_mem(G2, selfcode(2, pge_tail))])
    # One frame at V in A and at V2 in B: both blocks cached, the frame rewritten through its identity alias under A, both
    # linear addresses run the new bytes.
    V2 = d + 0x8000
    code3 = int.from_bytes(CODE(3)[:8], 'little')
    p['xmc-shared'] = (adv_call(0, V) + '    mov r8, rax\n' + cr3(1) + adv_call(1, V2) + '    mov r9, rax\n' + cr3(0) + adv_call(2, V) + '    add r8, rax\n'
                       + f'    mov rax, {code3:#x}\n    mov [{F1:#x}], rax\n' + adv_call(3, V) + '    mov r10, rax\n' + cr3(1) + adv_call(4, V2)
                       + '    mov r12, rax\n' + cr3(0),
                       {V: F1 | P.LEAF}, {V2: F1 | P.LEAF}, [code_mem(F1, CODE(1))])
    # An instruction crossing from a global page into a non-global one (and the reverse): after a CR3 load to B, which maps
    # the second page elsewhere, the new bytes (the block is not global).
    S0, S1, XA, XB = d + 0xb000, d + 0xc000, d + 0xd000, d + 0xe000
    tail = lambda k: bytes([k, 0, 0]) + P.asm('jmp r13')
    for name, g0, g1 in (('mixed-g0', G, 0), ('mixed-g1', 0, G)):
        p[name] = (cr4('bts', 7) + adv_call(0, S1 - 2) + '    mov r8, rax\n' + cr3(0) + adv_call(1, S1 - 2) + '    mov r9, rax\n' + cr3(1) + adv_call(2, S1 - 2)
                   + '    mov r10, rax\n' + cr3(0) + cr4('btr', 7),
                   {S0: S0 | P.LEAF | g0, S1: XA | P.LEAF | g1}, {S0: S0 | P.LEAF | g0, S1: XB | P.LEAF | g1},
                   [code_mem(S1 - 2, b'\xb8\x00'), code_mem(XA, tail(1)), code_mem(XB, tail(2))])
    # A fetch #PF at another RIP of V's page (V's block current): the handler maps V to F2 without INVLPG and resumes; the
    # #PF invalidated V's translations, so V then runs F2's bytes.
    resume = '.h:\n    mov rax, %#x\n    mov [%#x], rax\n    inc r14\n    add rsp, 8\n    iretq\n' % (F2 | P.LEAF, la(V))
    p['pf-fetch'] = (adv_idt(d) + adv_call(0, V) + '    mov r8, rax\n' + put(V, F1 | P.LEAF & ~1) + adv_call(1, V + 0x10) + '    mov r9, rax\n'
                     + adv_call(2, V) + '    mov r10, rax\n' + end + resume + '.end:\n',
                     {V: F1 | P.LEAF}, {}, [code_mem(F1, CODE(1) * 2), code_mem(F2, CODE(2) * 2)])
    return p

# natemu-only: allowed (r8, r9, r10, r14) tuples per CPU (a fetch #PF may or may not be taken: QEMU's TLB holds V)
ADV_EITHER = {'pf-fetch': {(1, 1, 1, 0), (1, 2, 2, 1)}}


def adv_program(name, n):
    out = {'format': 'natemu-smp-v1', 'name': f'adv-{name}@{n}', 'ncpu': n, 'system': True, 'bodies': [], 'cpus': [], 'memory': [], 'params': {}}
    for i in range(n):
        d = P.SYS_D(i)
        text, a, b, extra = adv_setup(i)[name]
        out['bodies'].append(P.body(P.SYS_CODE(i), P.sys_text(i, text)))
        out['cpus'].append(dict(P.cpu(i, P.SYS_CODE(i)), cpl=0))
        out['memory'] += P.sys_tables(i, 0, n, a) + P.sys_tables(i, 1, n, b) + [dict(P.zero(d + IDT, 0x1000), capture=False),
                                                                               dict(P.zero(d + P.SAVE, 8), capture=False)] + extra
    return out


def run(a, prog, extra, host=None, runtime=None, tag=''):
    work = a.out / 'work' / f"{prog['name']}{tag}{'-1' if extra else ''}"
    rc, out, err = s3.driver(host or a.host, runtime or a.runtime, a.fexc, s3.smp_description(prog), work, extra, timeout=300)
    problems = [] if rc == 0 else [f'driver exit {rc}: {err.strip()[-300:]}']
    for v, c in enumerate(out['cpus'] if out else []):
        end = s3.body_end(prog, smp.num(prog['cpus'][v]['rip']))
        if c['stop'] != 'reached' or smp.num(c['rip']) != end:
            problems.append(f"cpu{v} stopped {c['stop']} {c['reason']} at {c['rip']} (end {end:#x})")
        problems += [f'cpu{v} ' + p for p in s3.per_vcpu_checks(c)]
    return out, problems


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--fexc', type=pathlib.Path, required=True)
    ap.add_argument('--out', type=pathlib.Path, required=True)
    ap.add_argument('--cpus', type=int, nargs='+', default=[1, 2, 3, 4])
    ap.add_argument('--k', type=int, default=50)
    ap.add_argument('--host', type=pathlib.Path)
    ap.add_argument('--runtime', type=pathlib.Path)
    ap.add_argument('--base-host', type=pathlib.Path)
    ap.add_argument('--base-runtime', type=pathlib.Path)
    ap.add_argument('--programs', type=lambda s: s.split(','), help='only these programs (and no counts)')
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    if not (a.host and a.runtime): a.host, a.runtime = vm.build()
    stub = smp.build_stub(a.out / 'qemu-build')
    report, failures = [], []
    for n in a.cpus:
        for name in [x for x in bodies(a.k) if not a.programs or x in a.programs]:
            prog = program(name, n, a.k)
            want = None
            if name not in EITHER:
                rc, err, dump, _ = smp.run_image(prog, smp.build_image(prog, stub), a.out / 'qemu' / prog['name'], 'multi', timeout=120)
                want = smp.parse_dump(prog, dump) if rc == 1 else None
                if want is None:
                    failures.append(f"{prog['name']}: QEMU run failed ({rc}) {err[-200:]}")
                    continue
            for extra in ([], [1]):
                out, problems = run(a, prog, extra)
                if out and want:
                    problems += s3.smp.compare(prog, s3.vm_state(prog, out), want)
                seen = {}
                if out and name in EITHER:
                    for v, c in enumerate(out['cpus']):
                        allowed = EITHER[name](v) if callable(EITHER[name]) else EITHER[name]
                        got = {r: smp.num(c['gpr'][P.REGS.index(r)]) for r in allowed}
                        seen[v] = got
                        problems += [f'cpu{v} {r} = {got[r]:#x}, allowed {[hex(x) for x in sorted(ok)]}' for r, ok in allowed.items() if got[r] not in ok]
                stats = out and [{k: c['stats'].get(k) for k in ('requests', 'compiles', 'flushes', 'shadow_resets', 'links', 'unlinks', 'k1')} for c in out['cpus']]
                entry = {'name': prog['name'], 'max_block': extra[0] if extra else None, 'problems': problems, 'seen': seen, 'stats': stats}
                report.append(entry)
                print(json.dumps(entry), flush=True)
                failures += [f"{prog['name']}{' (max-block 1)' if extra else ''}: {p}" for p in problems]
    for n in a.cpus:
        for name in [x for x in adv_setup(0) if not a.programs or x in a.programs]:
            prog = adv_program(name, n)
            want = None
            if name not in ADV_EITHER:
                rc, err, dump, _ = smp.run_image(prog, smp.build_image(prog, stub), a.out / 'qemu' / prog['name'], 'multi', timeout=120)
                want = smp.parse_dump(prog, dump) if rc == 1 else None
                if want is None:
                    failures.append(f"{prog['name']}: QEMU run failed ({rc}) {err[-200:]}")
                    continue
            for extra in ([], [1]):
                out, problems = run(a, prog, extra)
                if out and want:
                    problems += s3.smp.compare(prog, s3.vm_state(prog, out), want)
                for v, c in enumerate(out['cpus'] if out and name in ADV_EITHER else []):
                    got = tuple(smp.num(c['gpr'][P.REGS.index(r)]) for r in ('r8', 'r9', 'r10', 'r14'))
                    if got not in ADV_EITHER[name]: problems.append(f'cpu{v} (r8, r9, r10, r14) = {got}')
                entry = {'name': prog['name'], 'max_block': extra[0] if extra else None, 'problems': problems,
                         'stats': out and [c['stats'].get('k1') for c in out['cpus']]}
                report.append(entry)
                print(json.dumps(entry), flush=True)
                failures += [f"{prog['name']}{' (max-block 1)' if extra else ''}: {p}" for p in problems]
    # Counts: translation requests and compiles must not grow with the rounds.
    counts = {}
    if a.programs: a.host = a.base_host = None
    for label, host, runtime in (('k1', a.host, a.runtime), ('base', a.base_host, a.base_runtime)):
        if not host: continue
        for k in (a.k, 2 * a.k):
            out, problems = run(a, program('cr3-switch', 1, k), [], host, runtime, f'-{label}-{k}')
            c = out and out['cpus'][0]
            counts[f'{label}-{k}'] = c and {x: c['stats'].get(x) for x in ('requests', 'compiles', 'flushes', 'k1')}
            if label == 'k1': failures += [f'counts k={k}: {p}' for p in problems]
    print('counts', json.dumps(counts), flush=True)
    k1 = [counts.get(f'k1-{k}') for k in (a.k, 2 * a.k)]
    if not a.programs and (not all(k1) or k1[0]['requests'] != k1[1]['requests'] or k1[0]['compiles'] != k1[1]['compiles']):
        failures.append(f'counts: requests or compiles grow with the rounds: {k1}')
    (a.out / 'k1.json').write_text(json.dumps({'report': report, 'counts': counts, 'failures': failures}, indent=1) + '\n')
    for f in failures: print('FAIL', f)
    sys.exit(1 if failures else 0)


if __name__ == '__main__': main()

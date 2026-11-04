#!/usr/bin/env python3
"""K1 checks: the code cache across CR3 loads, INVLPG and control changes (boot-features plan, K1).

    tools/with-env python3 tools/run_k1.py --fexc FEXC --out DIR [--cpus 1 2 3 4] [--k 50]
        [--host HOST --runtime RUNTIME] [--base-host HOST --base-runtime RUNTIME] [--programs a,b]

Each CPU i of N runs the same body (`programs.sys_text`: CPL 0, its own tables A and B at `programs.SYS_T(i, k)`) and
calls the code at linear V = SYS_D(i) (`lea r13, ...; jmp` V; the code is `mov eax, k; jmp r13`), which table A maps to
frame F1 (k = 1) and table B to F2 (k = 2):
- `remap-invlpg`: V's PTE in A changed to F2, then INVLPG: the next call runs F2's bytes; back to F1, INVLPG: F1's.
- `remap-invlpg-a32` (I10): the same with `invlpg [eax]` (0x67) and a garbage upper half in RAX: the truncated address.
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
- `contexts` (natemu only, perf chunk 5): one block called through the dispatcher under six code contexts in turn (MXCSR
  masks, CR0.TS), more than the dispatcher's L1 tables: each runs, or takes #NM or #XM, by its own context only; then an
  INVLPG, a code write and a CR3 reload made under one context reach the block another context's table holds.
Perf chunk 8 (links kept across TLB events):
- `place-global` (natemu only): a block placed from a global page under CR4.PGE is global at once: V remapped with no
  INVLPG and a CR3 reload, natemu keeps the old frame (the architecture allows either; r9 = 1 asserted); INVLPG: the new.
  A block crossing from a global page into a non-global one placed, its second page remapped with no INVLPG, a CR3
  reload: the new bytes (rsi = 0x200, required). PGE off: a block placed from a page with G set is not global (rbp = 1).
- `invlpg-page` (natemu only): INVLPG of one 4 KiB page leaves the blocks and links of another page in its 2 MiB region:
  three calls of N from one jump site (linked), N remapped with no INVLPG, INVLPG of V, three more calls through the kept
  link run the old frame (r9 = 3; the architecture allows 3-6); INVLPG of N: the new.
- `invlpg-large` (natemu only): code in a 2 MiB page, its PDE changed with no INVLPG of the code's page, INVLPG of another
  4 KiB address in the large page: a load and the call both see the new frame (rbx = 0x22, r9 = 2; every TLB entry of the
  large page goes, SDM Vol.3 4.10.4.1). QEMU 11.1.2 loads 0x22 but runs the old block (r9 = 1): its jump cache keeps it.
- `cross-link`: `cross` through a direct jump from the first page into the crossing instruction: the link is not local (its
  target has a page its source lacks) and goes at the INVLPG of the second page alone.
- `local-gpa` (natemu only): a link from S into T on S's page made after the page was remapped with no INVLPG (S from F1,
  T from F2); back to F1 with no INVLPG and a CR3 reload: S runs and must reach F1's T (r10 = 1): the link is not local.
- `local-write`: V global (PGE), three linked calls R -> S -> T from one jump site R (S and T on V's page); INVLPG of V
  (S -> T stays, local; R -> S goes); three calls; a CR3 reload (R -> S stays: S global); CPU (i + 1) % N rewrites T's
  immediate (CPU i's own at N = 1) and the CPUs hand over (CPUID after): three calls from R must run the new bytes.
- the independent review's adversarial programs (`adv_setup`: NX, SMEP and U/S changes, code remapping its own page,
  one frame at two linear addresses rewritten, global/non-global crossings, a fetch #PF then remap), against QEMU but
  `pf-fetch` (either outcome by the TLB rules).
Perf chunk 10 (reuse inside the resident, natemu only; aliases above 4 GiB, values and least copies asserted):
- `reuse-alias`: a frame at RV1 in table A and RV2 in B: RV2's blocks are copies, with RV2's `lea [rel]` and return address.
- `reuse-write`: the frame written after a copy: the copy dies with its template; a third alias is not copied from a dead one.
- `reuse-remap`: B's alias remapped to another frame with INVLPG: its own bytes, not a copy of the first frame's block.
- `reuse-cross`: a block crossing into a second page is copied where both frames are the template's, not where the second
  differs (a refusal counted).
Perf chunk 11 (natemu only):
- `link-full`: more links than the record area holds (a chain of 286,720 link points through 70 pages above 4 GiB),
  three passes per phase: as placed; after CPU (i - 1) % N rewrote one of its pages; after a page remapped with a CR3
  reload; back with INVLPG: every pass counts exactly, the area filled without a flush, and each fill cost at most two
  passes over it (`reclaims` at most 1 + 4 links / MAX_LINKS).
- `counts`: `cr3-switch` at K and 2K rounds on 1 vCPU: the resident's translation requests and the host's compiles must
  not grow with the rounds (K1: a CR3 load keeps translations); with `--base-host/--base-runtime` the same counts for
  another build (before K1) are reported beside them.
Perf chunk 12 (natemu only):
- `hint-reset`: the linker's hint by code word after a flush renumbers the blocks at the same code addresses (four rounds
  of a new instruction breakpoint, never reached, then three linked calls to V; odd rounds also run CLI, a kept record that
  takes a number and no code): every call runs (r8 = 12).

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


def bodies(k, i=0, n=1):
    d = P.SYS_D(i)
    leaf = lambda va: P.sys_leaf(i, 0, va)
    put = lambda va, frame: f'    mov rax, {frame | P.LEAF:#x}\n    mov [{leaf(va):#x}], rax\n'
    inv = lambda va: f'    mov rax, {va:#x}\n    invlpg [rax]\n'
    q, _, wcode = walk_at(i)
    pte = lambda f: f'    mov rax, {{d:#x}}+{f:#x}|{P.LEAF:#x}\n    mov [{{la:#x}}], rax\n'
    pte_g = lambda f: f'    mov rax, {{d:#x}}+{f:#x}|{P.LEAF | 0x100:#x}\n    mov [{{la:#x}}], rax\n'
    invlpg = '    mov rax, {V:#x}\n    invlpg [rax]\n'
    # I10: INVLPG with 0x67: EAX's upper half garbage (a 64-bit address would be non-canonical and invalidate nothing)
    invlpg32 = '    mov rax, 0xdeadbeef00000000+{V:#x}\n    invlpg [eax]\n'
    cr3 = lambda t: f'    mov rax, {{{t}:#x}}\n    mov cr3, rax\n'
    pge = lambda on: f'    mov rax, cr4\n    {"bts" if on else "btr"} rax, 7\n    mov cr4, rax\n'
    code3 = int.from_bytes(CODE(3)[:8], 'little')
    return {
        'remap-invlpg': call(0) + '    mov r8, rax\n' + pte(F2) + invlpg + call(1) + '    mov r9, rax\n' + pte(F1) + invlpg + call(2) + '    mov r10, rax\n',
        'remap-invlpg-a32': call(0) + '    mov r8, rax\n' + pte(F2) + invlpg32 + call(1) + '    mov r9, rax\n' + pte(F1) + invlpg32 + call(2) + '    mov r10, rax\n',
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
        # Code contexts (perf chunk 5): one block at linear d + CX (`addss` of an inexact sum, then `mov eax, 1`) called
        # through the dispatcher under six contexts, K rounds of A, C, A, C (two tables kept, switched between), B, then D,
        # E, F (more contexts than L1 tables: tables evicted and reused). A (MXCSR 0x1f80) runs; C (PM clear) #XM; B (CR0.TS)
        # #NM; D, E, F (UM, ZM, DM clear: nothing raised) run. A block entered under another context than its own shows as
        # a wrong count (r8 runs, r14 #NM, r15 #XM). Then V's block, kept in A's table, is made stale under C (`ctx_kept`).
        'contexts': (ctx_idt(d) + f'    mov rax, cr4\n    or rax, 0x600\n    mov cr4, rax\n    xor r8d, r8d\n    xor r14d, r14d\n    xor r15d, r15d\n'
                     f'    mov eax, 0x3f800000\n    movd xmm0, eax\n    mov eax, 0x0da24260\n    movd xmm1, eax\n    mov ecx, {k}\n.l:\n'
                     + ''.join(ctx_call(j, d, m) for j, m in enumerate((0, 1, 0, 1)))
                     + '    mov rax, cr0\n    bts rax, 3\n    mov cr0, rax\n' + ctx_call(4, d, None) + '    clts\n'
                     + ''.join(ctx_call(5 + j, d, j + 2) for j in range(3)) + '    dec ecx\n    jnz .l\n'
                     + ctx_call(8, d, 0) + ctx_kept(i) + '    jmp .end\n' + CTX_HANDLERS + '.end:\n'),
        **links_bodies(k, i, n),
        **reuse_bodies(i),
        'link-full': full_body(i, n),
        # Perf chunk 12: four rounds, each arming a new instruction breakpoint at an address never run (the DR7 write
        # flushes the cache: blocks are numbered from 0 again) and then making three linked calls to V; odd rounds first
        # run CLI, a kept record with a number and no code, so every block after it has the same code address as in the
        # round before but another number: the linker's hint by code word ([`block_at`]) then names a block that no longer
        # holds that word and must not be used.
        'hint-reset': ('    mov ecx, 4\n.l:\n    mov rax, {d:#x}+0x7f00\n    add rax, rcx\n    mov dr0, rax\n    mov eax, 1\n    mov dr7, rax\n'
                       '    test ecx, 1\n    jz .e\n    cli\n.e:\n    mov edx, 3\n.a:\n' + direct(0) + '    add r8, rax\n    dec edx\n    jnz .a\n'
                       '    dec ecx\n    jnz .l\n    xor eax, eax\n    mov dr7, rax\n'),
    }


def links_bodies(k, i, n):
    """Perf chunk 8's programs (module docstring)."""
    d = P.SYS_D(i)
    leaf = lambda va: P.sys_leaf(i, 0, va)
    put = lambda va, frame: f'    mov rax, {frame | P.LEAF:#x}\n    mov [{leaf(va):#x}], rax\n'
    put_g = lambda va, frame: f'    mov rax, {frame | P.LEAF | G:#x}\n    mov [{leaf(va):#x}], rax\n'
    inv = lambda va: f'    mov rax, {va:#x}\n    invlpg [rax]\n'
    cr3 = '    mov rax, {ta:#x}\n    mov cr3, rax\n'
    pge = lambda on: f'    mov rax, cr4\n    {"bts" if on else "btr"} rax, 7\n    mov cr4, rax\n'
    to = lambda off: f'    lea r13, [rel .c{{tag}}]\n    jmp $$+{{{{Vrel:#x}}}}-0x234+{off:#x}\n.c{{tag}}:\n'
    jump = lambda tag, off: to(off).format(tag=tag)
    rounds = lambda tag, off, reg: ''.join(jump(f'{tag}{r}', off) + f'    add {reg}, rax\n' for r in range(3))
    loop = lambda tag, reg: f'    mov ecx, 3\n.{tag}:\n    call .sub\n    add {reg}, rax\n    dec ecx\n    jnz .{tag}\n'
    pde = lambda frame: f'    mov rax, {frame | 0x80 | P.LEAF:#x}\n    mov [{P.SYS_T(i, 0) + 0x2000 + 8 * (LP >> 21):#x}], rax\n'
    j = (i + 1) % n
    hand = (f'    mov qword [{FLAGS + 8 * i:#x}], 1\n.h1:\n    pause\n    cmp qword [{FLAGS + 8 * j:#x}], 1\n    jne .h1\n'
            f'    mov byte [{P.SYS_D(j) + F1 + LW + 0x41:#x}], 3\n    mov qword [{FLAGS + 0x40 + 8 * j:#x}], 1\n'
            f'.h2:\n    pause\n    cmp qword [{FLAGS + 0x40 + 8 * i:#x}], 1\n    jne .h2\n    xor eax, eax\n    cpuid\n'
            '    xor ebx, ebx\n    xor ecx, ecx\n    xor edx, edx\n')
    return {
        'place-global': (pge(True) + call('g0') + '    mov r8, rax\n' + put_g(d, d + F2) + cr3 + call('g1') + '    mov r9, rax\n'
                         + inv(d) + call('g2') + '    mov r10, rax\n'
                         + call('g3', f'{d + 0x3ffe:#x}') + '    mov r12, rax\n' + put(d + 0x4000, d + XB) + cr3
                         + call('g4', f'{d + 0x3ffe:#x}') + '    mov rsi, rax\n'
                         + pge(False) + put_g(d, d + F3) + inv(d) + call('g5') + '    mov rdi, rax\n' + put_g(d, d + F1) + cr3
                         + call('g6') + '    mov rbp, rax\n'),
        # One jump site (`.sub`), so the second loop takes the link kept from the first.
        'invlpg-page': ('    jmp .over\n.sub:\n' + to(NP).format(tag='s') + '    ret\n.over:\n' + loop('p0', 'r8') + put(d + NP, d + NP2) + inv(d)
                        + loop('p1', 'r9') + inv(d + NP) + call('p2', f'{d + NP:#x}') + '    mov r10, rax\n'),
        'invlpg-large': (call('l0', f'{LP + LX:#x}') + '    mov r8, rax\n' + pde(LF2) + inv(LP + 0x5000) + f'    mov rbx, [{LP + LX + 0x800:#x}]\n' + call('l1', f'{LP + LX:#x}')
                         + '    mov r9, rax\n' + pde(LF1) + inv(LP + 0x1ff000) + call('l2', f'{LP + LX:#x}') + '    mov r10, rax\n'),
        'cross-link': rounds('x0', 0x3f00, 'r8') + put(d + 0x4000, d + XB) + inv(d + 0x4000) + rounds('x1', 0x3f00, 'r9'),
        'local-gpa': ('    xor r14d, r14d\n' + call('a0', f'{d + LS:#x}') + put(d, d + F2) + '    mov r14d, 1\n' + call('a1', f'{d + LS:#x}')
                      + '    mov r9, rax\n' + put(d, d + F1) + cr3 + call('a2', f'{d + LS:#x}') + '    mov r10, rax\n'),
        # One jump site (`.sub`) in every phase, so a link kept into a dead block would be taken.
        'local-write': (pge(True) + '    jmp .over\n.sub:\n' + to(LW).format(tag='s') + '    ret\n.over:\n' + loop('w0', 'r8') + inv(d)
                        + loop('w1', 'r9') + cr3 + hand + loop('w2', 'r10')),
    }

G = 0x100
F3 = 0xf000                      # `place-global`: V's third frame (`mov eax, 3`)
NP, NP2 = 0xd000, 0xe000         # `invlpg-page`: N (another page of V's 2 MiB region) and its second frame
# `invlpg-large`: linear 2 MiB page LP (table A's PDE 2) maps frame LF1 or LF2 (the images end at 4 MiB); code at LP + LX
LP, LF1, LF2, LX = 0x400000, 0x200000, 0x0, 0x1f1000
LS = 0x100                       # `local-gpa`: S at V + LS (`test r14, r14; jnz T; jmp r13`), T at V + LS + 0x40
LW = 0x200                       # `local-write`: S at V + LW (`jmp T`), T at V + LW + 0x40 (`mov eax, 1; jmp r13`)
FLAGS = P.SYS_D(0) + 0xe000      # `local-write`: per CPU a ready word, then (+ 0x40) a written word


def links_memory(name, i, n):
    """The memory perf chunk 8's programs add for CPU i (code shared by every CPU only from CPU 0)."""
    d = P.SYS_D(i)
    code = lambda va, text: {'addr': f'0x{va:x}', 'data': P.asm(text).hex()}
    if name == 'place-global':
        return [{'addr': f'0x{d + F3:x}', 'data': CODE(3).hex()}]
    if name == 'invlpg-page':
        return [code(d + NP, 'mov eax, 1\njmp r13'), code(d + NP2, 'mov eax, 2\njmp r13')]
    if name == 'invlpg-large':
        return ([{'addr': f'0x{P.SYS_T(i, 0) + 0x2000 + 8 * (LP >> 21):x}', 'data': (LF1 | 0x80 | P.LEAF).to_bytes(8, 'little').hex(), 'capture': False}]
                + ([code(LF1 + LX, 'mov eax, 1\njmp r13\ntimes 0x800-($-$$) nop\ndq 0x11'),
                    code(LF2 + LX, 'mov eax, 2\njmp r13\ntimes 0x800-($-$$) nop\ndq 0x22')] if i == 0 else []))
    if name == 'cross-link':
        return [{'addr': f'0x{d + 0x3f00:x}', 'data': b'\xe9\xf9\x00\x00\x00'.hex()}]
    if name == 'local-gpa':
        s = lambda k: P.asm(f'test r14, r14\njnz $$+0x40\njmp r13\ntimes 0x40-($-$$) nop\nmov eax, {k}\njmp r13')
        return [{'addr': f'0x{d + f + LS:x}', 'data': s(k).hex()} for k, f in ((1, F1), (2, F2))]
    if name == 'local-write':
        s = P.asm('jmp $$+0x40\ntimes 0x40-($-$$) nop\nmov eax, 1\njmp r13')
        return [{'addr': f'0x{d + F1 + LW:x}', 'data': s.hex()}] + ([dict(P.zero(FLAGS, 0x100), capture=False)] if i == 0 else [])
    return []

# Perf chunk 10 (reuse inside the resident, natemu only): per CPU, frames RL (and RL2, RLC) and linear aliases RV1 and RV3
# in table A, RV2 in table B, all of the same page offset: a block placed at one alias is copied to another of the same
# frame, rewritten for its RIP (`lea [rel]` and `call` values), never across another frame.
RL, RL2, RLC = 0x9000, 0xa000, 0xc000
# The aliases lie above 4 GiB (below it FEX's guest-RIP moves are the 32-bit form, which reuse refuses), in a 2 MiB region
# HIGH each table maps through three more table pages of its own (its PML4 entry, PDPT and PD at +0x5000, +0x6000, +0x7000).
HIGH = 0x7f12_3440_0000
RV1, RV2, RV3 = HIGH + 0xb000, HIGH + 0xd000, HIGH + 0xf000
RX = 0xff7                       # `reuse-cross`: `lea rdx, [rel 0]` at RX, then `mov eax, imm32` across the page end
LIB = lambda k: P.asm(f'mov ebx, {k}\nlea rax, [rel $$]\ncall .n\n.n:\npop rsi\njmp r13')   # rsi = alias + 17


def reuse_bodies(i):
    d = P.SYS_D(i)
    put_b = lambda va, frame: f'    mov rax, {frame | P.LEAF:#x}\n    mov [{high_leaf(i, 1, va):#x}], rax\n'
    cr3 = lambda t: f'    mov rax, {{{t}:#x}}\n    mov cr3, rax\n'
    save = lambda *regs: ''.join(f'    mov {r}, {s}\n' for r, s in regs)
    return {
        # A library frame at RV1 (A) and RV2 (B): RV2's blocks are copies of RV1's, with their own RIP's values.
        'reuse-alias': (call('r0', f'{RV1:#x}') + save(('r8', 'rax'), ('r9', 'rsi')) + cr3('tb') + call('r1', f'{RV2:#x}')
                        + save(('r10', 'rax'), ('r12', 'rsi')) + cr3('ta')),
        # The frame written after a copy (through its identity address): the copy dies with its template, and a third
        # alias placed after the write is not copied from either.
        'reuse-write': (call('w0', f'{RV1:#x}') + save(('r8', 'rbx')) + cr3('tb') + call('w1', f'{RV2:#x}') + save(('r9', 'rbx'))
                        + cr3('ta') + f'    mov byte [{d + RL + 1:#x}], 2\n' + call('w2', f'{RV3:#x}') + save(('r10', 'rbx'), ('r12', 'rax'))
                        + cr3('tb') + call('w3', f'{RV2:#x}') + save(('r14', 'rbx')) + cr3('ta')),
        # One alias remapped (B's RV2 to frame RL2, `mov ebx, 2`, with INVLPG): its block is not copied from RL's.
        'reuse-remap': (call('m0', f'{RV1:#x}') + save(('r8', 'rbx')) + cr3('tb') + call('m1', f'{RV2:#x}') + save(('r9', 'rbx'))
                        + put_b(RV2, d + RL2) + f'    mov rax, {RV2:#x}\n    invlpg [rax]\n' + call('m2', f'{RV2:#x}')
                        + save(('r10', 'rbx'), ('r12', 'rax')) + cr3('ta')),
        # A block crossing into a second page: RV1 and RV2 in A map RL then RL2 (`imm32` 0x100), RV2 in B RL then RLC
        # (0x200): A's RV2 block is a copy of RV1's, B's is not (its second frame differs).
        'reuse-cross': (call('x0', f'{RV1 + RX:#x}') + save(('r8', 'rax'), ('r9', 'rdx')) + call('x1', f'{RV2 + RX:#x}')
                        + save(('r10', 'rax'), ('r12', 'rdx')) + cr3('tb') + call('x2', f'{RV2 + RX:#x}') + save(('r14', 'rax'), ('r15', 'rdx'))
                        + cr3('ta')),
    }


# Perf chunk 11 (`link-full`, natemu only): more links than the record area holds. FM linear pages from HIGH (table A)
# map frame FC of 2048 `jz $+2` (two link points each, both to the next RIP: 2 * 2048 * FM records > MAX_LINKS; the
# pages but one are copies of the first's blocks), page FW its copy FWC, then page FE (`inc ebx; jmp r13`). A pass adds
# 1 to ebx, and 1 per `inc ebx` in the chain. Phases of three passes: (r8) as placed; (r9) FWC + 0x800 rewritten to
# `inc ebx` by CPU (i - 1) % N, so the links into its blocks die on CPU i; (r10) page FH remapped to FC2 (`inc ebx` at
# 0x400 and 0x404) with a CR3 reload; (r12) back to FC with INVLPG. Each pass runs every link point through links kept,
# dropped and remade; the record area fills in every pass, and each fill costs at most two passes over it (`reclaims`
# bounded by the links made).
FM, FW, FH = 70, 60, 35          # FW late in the chain: its links are live when it is written (a fill keeps the chain's tail)
FC, FWC, FC2, FE = 0x9000, 0xa000, 0xb000, 0xc000
MAX_LINKS = 256 * 1024                    # natemu_abi::execution::cache::MAX_LINKS
FULL_CODE = ['--code-cache', str(96 << 20)]  # 2 * 2048 * FM + blocks of FEX code: more than the default 16 MiB


def full_body(i, n):
    d, j = P.SYS_D(i), (i + 1) % n
    leaf = lambda frame: f'    mov rax, {(d + frame) | P.LEAF:#x}\n    mov [{high_leaf(i, 0, HIGH + FH * 0x1000):#x}], rax\n'
    phase = lambda tag, reg: (f'    xor ebx, ebx\n    mov ecx, 3\n.{tag}:\n    call .run\n    dec ecx\n    jnz .{tag}\n    mov {reg}, rbx\n')
    hand = (f'    mov qword [{FLAGS + 8 * i:#x}], 1\n.h1:\n    pause\n    cmp qword [{FLAGS + 8 * j:#x}], 1\n    jne .h1\n'
            f'    mov word [{P.SYS_D(j) + FWC + 0x800:#x}], 0xc3ff\n    mov qword [{FLAGS + 0x40 + 8 * j:#x}], 1\n'
            f'.h2:\n    pause\n    cmp qword [{FLAGS + 0x40 + 8 * i:#x}], 1\n    jne .h2\n    xor eax, eax\n    cpuid\n')
    return (f'    jmp .over\n.run:\n    lea r13, [rel .back]\n    mov rax, {HIGH:#x}\n    jmp rax\n.back:\n    ret\n.over:\n'
            + phase('f0', 'r8') + hand + phase('f1', 'r9') + leaf(FC2) + '    mov rax, {ta:#x}\n    mov cr3, rax\n' + phase('f2', 'r10')
            + leaf(FC) + f'    mov rax, {HIGH + FH * 0x1000:#x}\n    invlpg [rax]\n' + phase('f3', 'r12'))


def full_memory(i):
    d = P.SYS_D(i)
    base = P.SYS_T(i, 0)
    q = lambda at, value: {'addr': f'0x{at:x}', 'data': value.to_bytes(8, 'little').hex(), 'capture': False}
    tables = [q(base + 8 * (HIGH >> 39 & 511), (base + 0x5000) | 0x27), q(base + 0x5000 + 8 * (HIGH >> 30 & 511), (base + 0x6000) | 0x27),
              q(base + 0x6000 + 8 * (HIGH >> 21 & 511), (base + 0x7000) | 0x27)]
    ptes = b''.join(((d + (FWC if k == FW else FC)) | P.LEAF).to_bytes(8, 'little') for k in range(FM)) + ((d + FE) | P.LEAF).to_bytes(8, 'little')
    chain = bytearray(b'\x74\x00' * 2048)
    chain2 = bytearray(chain)
    chain2[0x400:0x402] = chain2[0x404:0x406] = b'\xff\xc3'
    return tables + [{'addr': f'0x{high_leaf(i, 0, HIGH):x}', 'data': ptes.hex(), 'capture': False},
                     {'addr': f'0x{d + FC:x}', 'data': chain.hex()}, {'addr': f'0x{d + FWC:x}', 'data': chain.hex()},
                     {'addr': f'0x{d + FC2:x}', 'data': chain2.hex()},
                     {'addr': f'0x{d + FE:x}', 'data': P.asm('inc ebx\njmp r13').hex()}] + ([dict(P.zero(FLAGS, 0x100), capture=False)] if i == 0 else [])


def high_leaf(i, k, va):
    """Address of the PTE of `va` (in HIGH's 2 MiB) in CPU i's table k."""
    return P.SYS_T(i, k) + 0x7000 + (va >> 12 & 511) * 8


def reuse_tables(name, i):
    """Per table (A, B): the linear page -> frame offset of the reuse programs' aliases."""
    if name == 'reuse-cross':
        return {RV1: RL, RV1 + 0x1000: RL2, RV2: RL, RV2 + 0x1000: RL2}, {RV2: RL, RV2 + 0x1000: RLC}
    return {RV1: RL, RV3: RL}, {RV2: RL}


def reuse_memory(name, i):
    d = P.SYS_D(i)
    q = lambda x: x.to_bytes(8, 'little').hex()
    entry = lambda at, value: {'addr': f'0x{at:x}', 'data': q(value), 'capture': False}
    tables = []
    for k, aliases in enumerate(reuse_tables(name, i)):
        base = P.SYS_T(i, k)
        tables += [entry(base + 8 * (HIGH >> 39 & 511), (base + 0x5000) | 0x27), entry(base + 0x5000 + 8 * (HIGH >> 30 & 511), (base + 0x6000) | 0x27),
                   entry(base + 0x6000 + 8 * (HIGH >> 21 & 511), (base + 0x7000) | 0x27)]
        tables += [entry(high_leaf(i, k, va), (d + f) | P.LEAF) for va, f in aliases.items()]
    if name == 'reuse-cross':
        first = bytearray(0x1000)
        first[RX:] = P.asm('lea rdx, [rel $+7]') + b'\xb8\x00'
        return tables + [{'addr': f'0x{d + RL:x}', 'data': first.hex()}, {'addr': f'0x{d + RL2:x}', 'data': TAIL(1).hex()},
                         {'addr': f'0x{d + RLC:x}', 'data': TAIL(2).hex()}]
    return tables + [{'addr': f'0x{d + RL:x}', 'data': LIB(1).hex()}, {'addr': f'0x{d + RL2:x}', 'data': LIB(2).hex()}]


# Each reuse program's values and its least resident copies and refusals (a fence, another second frame, a dead template:
# `refused[0]`) per CPU.
def reuse_either(name):
    def judge(i, k):
        d = P.SYS_D(i)
        return {
            'reuse-alias': {'r8': {RV1}, 'r9': {RV1 + 17}, 'r10': {RV2}, 'r12': {RV2 + 17}},
            'reuse-write': {'r8': {1}, 'r9': {1}, 'r10': {2}, 'r12': {RV3}, 'r14': {2}},
            'reuse-remap': {'r8': {1}, 'r9': {1}, 'r10': {2}, 'r12': {RV2}},
            'reuse-cross': {'r8': {0x100}, 'r9': {RV1 + 0xffe}, 'r10': {0x100}, 'r12': {RV2 + 0xffe}, 'r14': {0x200}, 'r15': {RV2 + 0xffe}},
        }[name]
    return judge
REUSE = {'reuse-alias': (1, 0), 'reuse-write': (1, 0), 'reuse-remap': (1, 0), 'reuse-cross': (1, 1)}

IDT = 0x3000
CX, CD = 0x9000, 0xa000                       # `contexts`: its code page and its MXCSR values (A, C, D, E, F)
CTX_MXCSR = (0x1f80, 0x0f80, 0x1780, 0x1d80, 0x1e80)


def ctx_idt(d):
    """`contexts`: an IDT at d + IDT with #NM (7, handler .h7) and #XM (19, .h19)."""
    gate = lambda v: (f'    lea rax, [rel .h{v}]\n    mov [{d + IDT + 16 * v:#x}], ax\n    mov word [{d + IDT + 16 * v + 2:#x}], 8\n'
                      f'    mov word [{d + IDT + 16 * v + 4:#x}], 0x8e00\n    shr rax, 16\n    mov [{d + IDT + 16 * v + 6:#x}], ax\n'
                      f'    shr rax, 16\n    mov [{d + IDT + 16 * v + 8:#x}], eax\n')
    return (gate(7) + gate(19) + f'    mov word [{d + IDT + 0x800:#x}], 0xfff\n    mov rax, {d + IDT:#x}\n    mov [{d + IDT + 0x802:#x}], rax\n'
            f'    lidt [{d + IDT + 0x800:#x}]\n')


def ctx_call(tag, d, mxcsr):
    """`contexts`: LDMXCSR of value `mxcsr` (an index into CTX_MXCSR; None: none), then the call; r8 += 1 if it ran."""
    load = '' if mxcsr is None else f'    ldmxcsr [{d + CD + 4 * mxcsr:#x}]\n'
    return load + f'    xor eax, eax\n' + call(f'x{tag}', f'{d + CX:#x}') + '    add r8, rax\n'

def ctx_kept(i):
    """`contexts`, after the rounds: under C (another L1 table), V is remapped to frame W1 with INVLPG, then W1's bytes are
    rewritten, then V is remapped to W2 with a CR3 reload (no INVLPG); after each, V is called under A, whose table still
    held V's entry: it must run the new bytes (r9 = 2, r10 = 3, r12 = 4)."""
    d = P.SYS_D(i)
    V, W1, W2 = d + CX, d + CW1, d + CW2
    load = lambda m: f'    ldmxcsr [{d + CD + 4 * m:#x}]\n'
    put = lambda frame: f'    mov rax, {frame | P.LEAF:#x}\n    mov [{P.sys_leaf(i, 0, V):#x}], rax\n'
    run = lambda tag, reg: load(0) + '    xor eax, eax\n' + call(f'k{tag}', f'{V:#x}') + f'    mov {reg}, rax\n'
    return (load(1) + put(W1) + f'    mov rax, {V:#x}\n    invlpg [rax]\n' + run(0, 'r9')
            + load(1) + f'    mov byte [{W1 + 1:#x}], 3\n' + run(1, 'r10')
            + load(1) + put(W2) + '    mov rax, cr3\n    mov cr3, rax\n' + run(2, 'r12'))

CW1, CW2 = 0xb000, 0xc000                     # `contexts`: V's other frames (`mov eax, 2` and `mov eax, 4`)

# #NM and #XM: counted, and the call returns (r13) with RAX 0 (its `mov eax, 1` not run).
CTX_HANDLERS = '.h7:\n    inc r14\n    xor eax, eax\n    mov [rsp], r13\n    iretq\n.h19:\n    inc r15\n    xor eax, eax\n    mov [rsp], r13\n    iretq\n'
# Natemu-only programs: (register, allowed values) per CPU; a function of the CPU for `walk`. In `global` the architecture
# allows either frame for r9 and r10 (a kept global entry, or a miss); natemu keeps the block found global (K1), so 1.
EITHER = {
    'no-invlpg': {'r8': {1}, 'r9': {1, 2}, 'r10': {2}},
    'global': {'r8': {1}, 'rbx': {1}, 'r9': {1}, 'r10': {1}, 'r12': {2}, 'rsi': {1}, 'rdi': {1, 2}, 'rbp': {2}},
    'walk': lambda i, k: {'r8': {walk_at(i)[0] | 7}, 'r9': {walk_at(i)[0] | 0x27}, 'r10': {walk_at(i)[0] | 0x27}},
    'pf-remap': {'r8': {1}, 'r14': {1}, 'r9': {2}},
    # K rounds of five runs, one #NM and two #XM, then a last run under A.
    'contexts': lambda i, k: {'r8': {5 * k + 1}, 'r14': {k}, 'r15': {2 * k}, 'r9': {2}, 'r10': {3}, 'r12': {4}},
    # Perf chunk 8: natemu's choice where the architecture allows either (r9 of `place-global`, `invlpg-page`, `local-gpa`).
    'place-global': {'r8': {1}, 'r9': {1}, 'r10': {2}, 'r12': {0x100}, 'rsi': {0x200}, 'rdi': {3}, 'rbp': {1}},
    'invlpg-page': {'r8': {3}, 'r9': {3}, 'r10': {2}},
    'local-gpa': {'r9': {2}, 'r10': {1}},
    'invlpg-large': {'r8': {1}, 'rbx': {0x22}, 'r9': {2}, 'r10': {1}},
    **{name: reuse_either(name) for name in REUSE},
    'link-full': {'r8': {3}, 'r9': {6}, 'r10': {12}, 'r12': {6}},
    'hint-reset': {'r8': {12}},
}


def program(name, n, k):
    out = {'format': 'natemu-smp-v1', 'name': f'k1-{name}@{n}', 'ncpu': n, 'system': True, 'bodies': [], 'cpus': [], 'memory': [], 'params': {}}
    for i in range(n):
        d = P.SYS_D(i)
        a = {d: (d + F1) | P.LEAF | (0x100 if name in ('global', 'place-global', 'local-write') else 0), d + 0x4000: (d + XA) | P.LEAF}
        if name == 'place-global':
            a[d + 0x3000] = (d + 0x3000) | P.LEAF | G
        if name == 'straddle':
            a[0x200000] = (d + XA) | P.LEAF
        b = {d: (d + F2) | P.LEAF}
        out['bodies'].append(P.body(P.SYS_CODE(i), P.sys_text(i, bodies(k, i, n)[name])))
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
        if name == 'contexts':
            extra += [dict(P.zero(d + IDT, 0x1000), capture=False), {'addr': f'0x{d + CX:x}', 'data': P.asm('addss xmm0, xmm1\nmov eax, 1\njmp r13').hex()},
                      {'addr': f'0x{d + CD:x}', 'data': b''.join(m.to_bytes(4, 'little') for m in CTX_MXCSR).hex(), 'capture': False},
                      {'addr': f'0x{d + CW1:x}', 'data': P.asm('mov eax, 2\njmp r13').hex()}, {'addr': f'0x{d + CW2:x}', 'data': P.asm('mov eax, 4\njmp r13').hex()}]
        out['memory'] += P.sys_tables(i, 0, n, a) + tb + [
            P.zero(d, 0x1000), {'addr': f'0x{d + F1:x}', 'data': CODE(1).hex()}, {'addr': f'0x{d + F2:x}', 'data': CODE(2).hex()},
            {'addr': f'0x{d + 0x3ffe:x}', 'data': 'b800'}, dict(P.zero(d + P.SAVE, 8), capture=False)] + (extra + links_memory(name, i, n) + (reuse_memory(name, i) if name in REUSE else [])
            + (full_memory(i) if name == 'link-full' else []))
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
    options = FULL_CODE if prog['name'].startswith('k1-link-full@') else []
    rc, out, err = s3.driver(host or a.host, runtime or a.runtime, a.fexc, s3.smp_description(prog), work, options + extra, timeout=300)
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
                if name == 'contexts':  # its blocks are translated once per context, not once per RIP (the check's count)
                    problems = [p for p in problems if 'host requests' not in p]
                    # The L1 tables of the A/C alternation are kept and switched between, not cleared at every change.
                    for v, c in enumerate(out['cpus'] if out else []):
                        k1 = c['stats']['k1']
                        if not k1['l1_clears'] < k1['l1_switches'] - a.k:
                            problems.append(f"cpu{v} L1 tables cleared {k1['l1_clears']} times of {k1['l1_switches']} switches")
                if out and want:
                    problems += s3.smp.compare(prog, s3.vm_state(prog, out), want)
                # Perf chunk 10: the copies the reuse programs must make, and the refusals.
                for v, c in enumerate(out['cpus'] if out and name in REUSE else []):
                    reuse = c['stats']['k1']['reuse']
                    if reuse['copies'] < REUSE[name][0] or reuse['refused'][0] < REUSE[name][1]:
                        problems.append(f"cpu{v} reuse {reuse}, at least {REUSE[name]} (copies, refused[0])")
                # Perf chunk 11: the record area filled (more links made than it holds, no flush but the driver's first)
                # and each fill cost at most two passes over it, each followed by at least a quarter of the area's links.
                for v, c in enumerate(out['cpus'] if out and name == 'link-full' else []):
                    count, links, flushes = c['stats']['k1']['reclaims'][0], c['stats']['links'], c['stats']['flushes']
                    if not (links > MAX_LINKS and count >= 1 and count <= 1 + 4 * links // MAX_LINKS and flushes == 1):
                        problems.append(f'cpu{v} links {links}, full {count} times (at most {1 + 4 * links // MAX_LINKS}), flushes {flushes}')
                seen = {}
                if out and name in EITHER:
                    for v, c in enumerate(out['cpus']):
                        allowed = EITHER[name](v, a.k) if callable(EITHER[name]) else EITHER[name]
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

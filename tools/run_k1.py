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
  INVLPG, a code write and a CR3 reload made under one context reach the block another context's table holds. Perf
  chunk 19: two RIPs sharing one L1 entry (the same low 16 bits) called in turn each run their own code, and a CR3 reload
  under another context reaches the entry's last owner. Two-way L1: three such RIPs (one set, three RIPs: they evict each
  other) run under every context of every round, tables cleared and reused around them.
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
- `pf-large` (natemu only, perf chunk 19): `invlpg-large` with a #PF instead of the INVLPG: the PDE made not present (no
  INVLPG), a load from another 4 KiB page of the large page faults, the handler maps LF2 (no INVLPG): the call runs LF2's
  code (r14 = 1, r9 = 2; a #PF invalidates the entries for the faulting address, every one of a larger page).
- `large-alias` (natemu only, perf chunk 19): a large block at LP + 2 GiB (the same bit of `Control::large_regions`) placed
  before LP's: an INVLPG of another page of LP still cuts LP's block (r9 = 2).
- `large-reval` (natemu only, perf chunk 19): LP mapped by 4 KiB pages, then by the large page of the same frames with a CR3
  reload: the block is revalidated as large, and an INVLPG of another page of LP cuts it (r9 = 2).
- `large-64`, `large-65` (natemu only, perf chunk 19): large blocks in 64 and 65 regions (as many as `Control::large_list`
  holds, and one more): an INVLPG of another page of the last region cuts its block (r8 = 64 or 65, r9 = 2).
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
PIC study stage 3 (shared translations, natemu only; the program driver shares every privilege level's templates, so these
CPL-0 programs run shared code; values asserted, and the least instances, checked and local links): frame RL holds Y
(`mov ebx, k; lea rax, [rel $$]; jmp .b; .b: call .n; .n: pop rsi; jmp r13`: rax = the alias, rsi = alias + YN, a local
link inside it) and, at 0x800, X (`jmp` to Y); frame RT a trampoline page (`jmp` RV3 at 0, `jmp` RV1 at 0x100):
- `pic-two`: RL at RV1 and RV3 of one address space: Y placed at RV1, shared at RV3 (an instance, its local link), entered
  at RV3 through an L1 entry and through a checked link from RT, then at RV1 again through RT and through L1.
- `pic-fork`: Y shared, its instance at RV1; the same address RV1 in table B (a forked child's): the instance found again.
- `pic-invlpg`: instances at RV1 and RV3; RV3 remapped to RL2 (`mov ebx, 2`) with INVLPG: RV3 runs RL2's code, RV1 RL's.
- `pic-write`: RL rewritten (`mov ebx, 3`) while both instances have live L1 entries: both run the new bytes.
- `pic-abs`: X absolute at RV1 linked into Y's instance there (an absolute block into shared code on its own page: checked);
  in table B at RV2, X shared too (a second process at another address): its link into Y is local.
- `pic-pge`: checked links live through two global TLB flushes (CR4.PGE off and on): every call runs its own code.
- `pic-gmap`: CR4.PGE on, RV3's PTE made global with INVLPG: the walk finds a global mapping for Y's instance, which goes;
  RV3 then runs an absolute block (the same values).
- `pic-ctx`: X (absolute) checked-linked into Y's instance under one MXCSR context, then four other contexts take every
  L1 table (the first one's given to another context: its dispatcher slot misses meanwhile), back under the first: X runs
  Y (values).
- `pic-twosrc`: the trampoline page at RVT and RVT2 (shared: two live instances of the source at different pages), each
  jumping to its own alias of Y (RV3, RV5): the one checked link (a dispatcher probe at each instance's x0) reaches each.
- `pic-cow`: Y shared in table A (RV1, RV3); table B maps RV1 to RL2 (a fork whose child wrote the page): B's RV1 runs
  RL2's code, A's both RL's after.
- `pic-twoframe`: as `pic-twosrc`, RV5 mapping RL2: the one checked link reaches RV3's instance and RV5's other frame.
- `pic-xm`: Z (`divss` by zero) shared, reached by an absolute block's and a shared trampoline's checked links under
  MXCSR A (masked); four other contexts take the L1 tables, one (0x1d80) unmasking ZE: there the same links raise #XM
  (the handler counts), back under A they run Z.
- `pic-flush`: `pic-twosrc`, a flush (a new instruction breakpoint, DR7), `pic-twosrc` again: the same values.
- `pic-write2`: RT linked into Y while Y is absolute at RV1, Y then shared through RV3 (the absolute block stays), RL
  rewritten: both the link's target and RV3 run the new bytes.
- `pic-pfault`: W (on RL) local-links to a block that page-faults (a zeroed PTE): the handler sees the alias's fault RIP
  and CR2, at RV3 (shared) and RV1.
- `pic-xwrite`: Y's instances live with L1 entries; CPU (i + 1) % N rewrites CPU i's RL (CPU i's own at N = 1) and the
  CPUs hand over (CPUID after): both aliases run the new bytes.
- `pic-rep` (review of stage 3, finding 2): R (on RL) copies 64 bytes with REP MOVSB to the CPU's own page (DST), placed
  at RV1 and shared at RV3: every run completes the string (RCX 0, RDI past it), under `run_pic.py`'s stops too.
- `pic-repexit` (the BAD_LINK the slices hit): RE (on RL) starts with REP MOVSB, its points the REP's self-exit and
  the block after it (N, `mov ebx, 13; jmp r13`). N placed at RV5 first (absolute there); RE at RV1, then at RV3 with
  RCX = 0 (shared: the point to N linked locally, N an instance there); then at RV5 with RCX = 1: the self-exit
  finishes the string and goes on to N, absolute at RV5, which the linker must not link the point to N for (the exit
  was not through it): values, no halt.
- `pic-again`: `pic-two` twice, its values from the second run, #GP and #PF resuming at the call's return (`run_pic.py`'s
  forge modes: forged only in the first entries, whose faults the handler skips; the second run exact).
- `pic-cross`: a block crossing into a second page (`reuse-cross`'s: `lea rdx, [rel]` then `mov eax, imm32` whose
  immediate ends on the next page) placed at RV1, shared at RV3; RV3's second page remapped to RLC (0x200) with INVLPG of
  that page only, then unmapped (INVLPG) and mapped back to RL2 (INVLPG): each run reads the second page's current frame.
- `pic-now`: Y placed at RV3 first (absolute there), X at RV1, then X at RV3 (shared): its link into Y at RV3, an absolute
  block on its own page and frame, shares Y at link time and is local; RL then rewritten: both aliases run the new bytes.
- `pic-user`: `pic-two` with `NATEMU_PIC=user`: CPL-0 code is never shared (SHARES 0), the same values.
Instance eviction has no case: instances are freed with the arena at a flush (stage 3), which the flush tests cover.
Perf chunk 11 (natemu only):
- `link-full`: more links than the record area holds. By default a test capacity of 16 Ki records (NATEMU_LINK_CAP: a
  chain of 20,480 link points through 5 pages above 4 GiB); `--full-links`: the production 512 Ki (a chain of 573,440 link
  points through 140 pages above 4 GiB), about 30 of the K1 run's 35 minutes. Three passes per phase: as placed; after CPU (i - 1) % N rewrote one of its pages; after a page remapped with a CR3
  reload; back with INVLPG: every pass counts exactly, the area filled without a flush, and each fill cost at most two
  passes over it (`reclaims` at most 1 + 4 links / capacity).
- `counts`: `cr3-switch` at K and 2K rounds on 1 vCPU: the resident's translation requests and the host's compiles must
  not grow with the rounds (K1: a CR3 load keeps translations); with `--base-host/--base-runtime` the same counts for
  another build (before K1) are reported beside them.
Perf chunk 12 (natemu only):
- `hint-reset`: the linker's hint by code word after a flush renumbers the blocks at the same code addresses (four rounds
  of a new instruction breakpoint, never reached, then three linked calls to V; odd rounds also run CLI, a kept record that
  takes a number and no code): every call runs (r8 = 12).

The two-way L1 (docs/restructure/l1-two-way-plan.md, chunk 1; blocks A, B, C, D at W, W + 64 KiB, W + 128 KiB, W + 32 KiB,
each `mov eax, k; jmp r13`: A, B, C share RIP bits 15:0, D differs in bit 15, so all four share an L1 set: RIP bits 14:0;
W = `L1W(i)`, set 0 of the index). Values are compared with QEMU unless noted; the natemu counts (dispatcher misses, a
difference of two runs of one program, from a `perf` resident: `--perf-runtime`, built here when not given) are judged
beside them:
- `l1-pair`: K rounds A, B then K rounds A, D: values; no dispatcher miss per round after the first (the pair fits).
- `l1-triple`: K rounds A, B, C, then A, B once after C: exactly 3 misses per round (fill-order replacement); that tail
  costs exactly 2 misses more than the same tail before the triples (A replaces the older of the set's two, B the other;
  the tail's own return labels are cold either way) and a longer tail adds none; the variants `l1-triple-t0` (the tail
  first) and `l1-triple-t3` (three tails) are only measured, never listed. `l1-triple-d`: A, B, D in
  turn: 3 misses a round, and `L1_NEW_CONFLICT` counts them (replacements whose victim differs in bit 15).
- `l1-pair-invlpg`: A in way 0, B in way 1; B's page remapped and INVLPG: the new frame's bytes; then A's, the roles swapped.
- `l1-pair-cr3`: A, B warm under table A; CR3 to table B, where B maps other bytes: B's own; back under A: A's.
- `l1-pair-write`: B's frame rewritten through its data alias, then A's (way 0); then C replaces A in way 0 (the set's
  next-victim bit now names way 1) and C's frame is rewritten: the new bytes each time.
- `l1-global` (+ `l1-global-x`, only measured): B (not global) fills way 0, A (G set, PGE) way 1; each round a CR3 reload
  empties way 0 and A runs again before B: no miss for A (the program against the one calling B twice: no difference).
- `l1-dup` (natemu only; + `l1-dup-x`): each round CR3 reload, A, B (way 1), then a CPUID at B - 2 whose next RIP is B: the
  resident fills L1 for B while B is in way 1 (a CPUID is never in L1: one miss a round of its own), then A: no miss for it
  (against B called again).
- `l1-pic` (natemu only): two instances of one shared translation at aliases in one L1 set, each with its own page base.
Writes DIR/k1.json; exit status 1 on any failure."""
import argparse, json, os, pathlib, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'tools' / 'reference'), str(ROOT / 'tools' / 'bench' / 'smp')]
import run_slice1_vm as vm, run_slice3_vm as s3, smp, shard, programs as P  # noqa: E402

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


# The two-way L1 programs (module docstring). CPU i's blocks and frames lie in 0x2c0000 + 0x30000 i ..: above every page
# `sys_tables` gives PTEs (their PTEs are written here, into both tables), clear of the next CPU's.
L1W = lambda i: 0x2c0000 + 0x30000 * i
L1_VARIANTS = ('l1-triple-t0', 'l1-triple-t3', 'l1-global-x', 'l1-dup-x')   # measured, never run beside the others
L1_NAMES = ('l1-pair', 'l1-triple', 'l1-triple-d', 'l1-pair-invlpg', 'l1-pair-cr3', 'l1-pair-write', 'l1-global', 'l1-dup', 'l1-pic')
L1_ALL = L1_NAMES + L1_VARIANTS


def l1_pages(i):
    """CPU i's blocks A, D, B, C (linear), the frames FA.. they map (also mapped at their own addresses: the data aliases),
    second frames A2, B2 and PB (the page before B: a CPUID ends it, so its next RIP is B)."""
    w = L1W(i)
    return dict(A=w, D=w + 0x8000, B=w + 0x10000, C=w + 0x20000, PB=w + 0xf000, FA=w + 0x1000, FD=w + 0x9000, FB=w + 0x11000,
                FC=w + 0x21000, A2=w + 0x2000, B2=w + 0x12000)


def l1_memory(name, i):
    """CPU i's PTEs (both tables, by `sys_leaf`) and frames for an L1 program: A, D, B, C map FA, FD, FB, FC (table B maps B to
    B2); the frames and PB, A2, B2 are mapped at their own addresses (the data aliases). A is global in `l1-global`."""
    if name == 'l1-pic':
        return []
    pg = l1_pages(i)
    code = lambda at, v: {'addr': f'0x{at:x}', 'data': CODE(v).hex(), 'capture': False}
    pte = lambda t, va, frame, bits=0: {'addr': f'0x{P.sys_leaf(i, t, va):x}', 'data': (frame | P.LEAF | bits).to_bytes(8, 'little').hex(), 'capture': False}
    maps = {pg['A']: pg['FA'], pg['D']: pg['FD'], pg['B']: pg['FB'], pg['C']: pg['FC'], **{pg[x]: pg[x] for x in ('FA', 'FB', 'FC', 'FD', 'PB', 'A2', 'B2')}}
    out = []
    for t in (0, 1):
        m = {**maps, **({pg['B']: pg['B2']} if t else {})}
        out += [pte(t, va, frame, 0x100 if name.startswith('l1-global') and va == pg['A'] else 0) for va, frame in m.items()]
    return out + [code(pg['FA'], 1), code(pg['FB'], 2), code(pg['FC'], 3), code(pg['FD'], 4), code(pg['A2'], 0x11), code(pg['B2'], 0x22),
                  {'addr': f"0x{pg['PB'] + 0xffe:x}", 'data': '0fa2', 'capture': False}]   # (a CPUID ends PB: its next RIP is B)


def l1_bodies(k, i, n):
    pg = l1_pages(i)
    A, B, C, D, PB = (pg[x] for x in ('A', 'B', 'C', 'D', 'PB'))
    tag = iter(range(10_000))
    c = lambda va, reg=None: call(f'l{next(tag)}', f'{va:#x}') + (f'    mov {reg}, rax\n' if reg else '')
    add = lambda va, reg: call(f'l{next(tag)}', f'{va:#x}') + f'    add {reg}, rax\n'
    def rounds(count, body, counter='ecx'):
        label = f'.r{next(tag)}'
        return f'    mov {counter}, {count}\n{label}:\n{body}    dec {counter}\n    jnz {label}\n'
    leaf = lambda va: P.sys_leaf(i, 0, va)
    put = lambda va, frame: f'    mov rax, {frame | P.LEAF:#x}\n    mov [{leaf(va):#x}], rax\n'
    inv = lambda va: f'    mov rax, {va:#x}\n    invlpg [rax]\n'
    cr3 = lambda t: f'    mov rax, {{{t}:#x}}\n    mov cr3, rax\n'
    pge = lambda on: f'    mov rax, cr4\n    {"bts" if on else "btr"} rax, 7\n    mov cr4, rax\n'
    # (8 bytes of `mov eax, v; jmp r13`: one store rewrites a block's code.)
    rewrite = lambda frame, v: f'    mov rax, {int.from_bytes(CODE(v)[:8], "little"):#x}\n    mov [{frame:#x}], rax\n'
    def tail(m):
        return rounds(m, add(A, 'r12') + add(B, 'r14'), 'edx') if m else ''
    def triple_with_tail(m, first=False):
        # (`first`: the tail's code runs before the triples instead, so its own return labels are cold in both variants.)
        return (tail(m) if first else '') + rounds(k, add(A, 'r8') + add(B, 'r9') + add(C, 'r10')) + ('' if first else tail(m))
    def glob(with_a):
        # B (not global) into way 0, A (global) into way 1; each round a CR3 reload empties way 0 (and every return label's
        # entry: each call costs its label a miss), then A, then B. The variant calls B twice instead of A then B: the same
        # labels, and the same misses if A hits.
        return (pge(True) + c(B, 'r9') + c(A, 'r8')
                + rounds(k, cr3('ta') + (add(A, 'r10') if with_a else add(B, 'r10')) + add(B, 'r12')))
    def dup(with_a):
        # (CPUID clobbers ecx: the counter is r14d.) Each round A (way 0), B (way 1), then the CPUID at B - 2 enters B through
        # the resident, which fills its L1 entry again while B is in way 1; A runs again: it must still be in L1 (the variant
        # calls B again: a hit as well, with the same return label).
        return rounds(k, cr3('ta') + add(A, 'r8') + add(B, 'r9') + add(B - 2, 'r10') + add(A if with_a else B, 'r12'), 'r14d')
    pic = lambda: rounds(k, c(RV1) + '    mov r8, rax\n    mov r9, rsi\n' + c(RV6) + '    mov r10, rax\n    mov r12, rsi\n')
    return {
        'l1-pair': rounds(k, add(A, 'r8') + add(B, 'r9')) + rounds(k, add(A, 'r10') + add(D, 'r12')),
        'l1-triple': triple_with_tail(1),
        'l1-triple-t0': triple_with_tail(1, first=True),
        'l1-triple-t3': triple_with_tail(3),
        'l1-triple-d': rounds(k, add(A, 'r8') + add(B, 'r9') + add(D, 'r10')),
        # A in way 0, B in way 1: B remapped (INVLPG) and run; A run; then A remapped (the way-0 block) and run, B again.
        'l1-pair-invlpg': (c(A, 'r8') + c(B, 'r9') + put(B, pg['B2']) + inv(B) + c(B, 'r10') + c(A, 'r12')
                           + put(A, pg['A2']) + inv(A) + c(A, 'r14') + c(B, 'r15')),
        'l1-pair-cr3': (c(A, 'r8') + c(B, 'r9') + cr3('tb') + c(A, 'r10') + c(B, 'r12') + cr3('ta') + c(B, 'r14') + c(A, 'r15')),
        # B's frame rewritten (B in way 1), then A's (way 0); C then replaces A in way 0 and sets the set's next-victim bit
        # (the bit is in way 0's owner word): C's frame rewritten: its slot must go, whatever the word holds.
        'l1-pair-write': (c(A, 'r8') + c(B, 'r9') + rewrite(pg['FB'], 0x22) + c(B, 'r10') + rewrite(pg['FA'], 0x11) + c(A, 'r12')
                          + c(C, 'r14') + rewrite(pg['FC'], 0x33) + c(C, 'r15')),
        'l1-global': glob(True),
        'l1-global-x': glob(False),
        'l1-dup': dup(True),
        'l1-dup-x': dup(False),
        'l1-pic': pic(),
    }


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
                     f'    xor r11d, r11d\n    mov eax, 0x3f800000\n    movd xmm0, eax\n    mov eax, 0x0da24260\n    movd xmm1, eax\n    mov ecx, {k}\n.l:\n'
                     + ''.join(ctx_call(j, d, m) for j, m in enumerate((0, 1, 0, 1)))
                     + '    mov rax, cr0\n    bts rax, 3\n    mov cr0, rax\n' + ctx_call(4, d, None) + '    clts\n'
                     + ''.join(ctx_call(5 + j, d, j + 2) for j in range(3)) + '    dec ecx\n    jnz .l\n'
                     + ctx_call(8, d, 0) + ctx_kept(i) + '    jmp .end\n' + CTX_HANDLERS + '.end:\n'),
        **links_bodies(k, i, n),
        **reuse_bodies(i, n),
        **l1_bodies(k, i, n),
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
    pde = lambda frame, j=0, present=1: f'    mov rax, {frame | 0x80 | P.LEAF & ~(1 - present):#x}\n    mov [{P.SYS_T(i, 0) + 0x2000 + 8 * ((LP >> 21) + j):#x}], rax\n'
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
        # Perf chunk 19 (the large-region test made exact). A load from another 4 KiB page of the 2 MiB code page, its PDE
        # made not present with no INVLPG, faults; the handler maps LF2 (no INVLPG): the #PF took the large page's entries.
        'pf-large': (adv_idt(d) + call('f0', f'{LP + LX:#x}') + '    mov r8, rax\n' + pde(LF2, present=0)
                     + f'    mov rbx, [{LP + 0x5000:#x}]\n' + call('f1', f'{LP + LX:#x}') + '    mov r9, rax\n    jmp .end\n'
                     + '.h:\n' + pde(LF2) + '    inc r14\n    add rsp, 8\n    iretq\n.end:\n'),
        # LP's region shares its bit with LP + 2 GiB (1024 regions apart), whose large block is placed first: LP's own
        # large block must still be found by an INVLPG of another page of LP.
        'large-alias': (call('a0', f'{LA + LX:#x}') + '    mov r10, rax\n' + call('a1', f'{LP + LX:#x}') + '    mov r8, rax\n' + pde(LF2)
                        + inv(LP + 0x5000) + call('a2', f'{LP + LX:#x}') + '    mov r9, rax\n'),
        # LP first mapped by 4 KiB pages (a block not large), then by the large page of the same frames with a CR3 reload:
        # the block, revalidated, is large now, and an INVLPG of another page of LP must cut it.
        'large-reval': (call('r0', f'{LP + LX:#x}') + '    mov r8, rax\n' + pde(LF1) + cr3 + call('r1', f'{LP + LX:#x}') + '    mov r10, rax\n'
                        + pde(LF2) + inv(LP + 0x5000) + call('r2', f'{LP + LX:#x}') + '    mov r9, rax\n'),
        # Large blocks in m regions, exactly as many as the exact list holds and one more: an INVLPG of another page of
        # the last region cuts its block (the list's last slot, then the first region it cannot hold).
        **{f'large-{m}': (f'    xor edx, edx\n.m:\n    mov rsi, rdx\n    shl rsi, 21\n    lea rax, [rsi + {LP + LX:#x}]\n'
                          '    lea r13, [rel .cm]\n    jmp rax\n.cm:\n    add r8, rax\n    inc edx\n'
                          f'    cmp edx, {m}\n    jb .m\n' + pde(LF2, m - 1) + inv(LP + (m - 1 << 21) + 0x5000)
                          + call('m1', f'{LP + (m - 1 << 21) + LX:#x}') + '    mov r9, rax\n') for m in LM},
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
LA, LM = LP + (1 << 31), (64, 65)  # perf chunk 19: `large-alias`'s second region (LP's bit); `large-64`/`-65`'s regions
                                   # from LP (64: Control::large_list's length)
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
    q = lambda at, values: {'addr': f'0x{at:x}', 'data': b''.join(v.to_bytes(8, 'little') for v in values).hex(), 'capture': False}
    pd, large = P.SYS_T(i, 0) + 0x2000 + 8 * (LP >> 21), LF1 | 0x80 | P.LEAF
    if name == 'pf-large':
        return links_memory('invlpg-large', i, n) + [dict(P.zero(d + IDT, 0x1000), capture=False)]
    if name == 'large-alias':
        # LA's GiB (PDPT entry 2) through a page directory of its own at table A + 0x5000.
        t = P.SYS_T(i, 0)
        return links_memory('invlpg-large', i, n) + [q(t + 0x1000 + 8 * (LA >> 30), [(t + 0x5000) | 0x27]), q(t + 0x5000 + 8 * (LA >> 21 & 511), [large])]
    if name == 'large-reval':
        # LP's page table at table A + 0x6000: LP + o -> LF1 + o.
        t = P.SYS_T(i, 0)
        return links_memory('invlpg-large', i, n)[1:] + [q(pd, [(t + 0x6000) | 0x27]), q(t + 0x6000, [(LF1 + (o << 12)) | P.LEAF for o in range(512)])]
    if name in (f'large-{m}' for m in LM):
        return links_memory('invlpg-large', i, n) + [q(pd, [large] * int(name[6:]))]
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
RV6 = RV1 + 0x8000                # `l1-pic`: RV1's partner in the two-way L1 (the same RIP bits 14:0, bit 15 differs)
RX = 0xff7                       # `reuse-cross`: `lea rdx, [rel 0]` at RX, then `mov eax, imm32` across the page end
LIB = lambda k: P.asm(f'mov ebx, {k}\nlea rax, [rel $$]\ncall .n\n.n:\npop rsi\njmp r13')   # rsi = alias + 17
# PIC study stage 3 (module docstring): Y (rsi = alias + YN), X at XO, the trampoline page at RVT.
Y = lambda k: P.asm(f'mov ebx, {k}\nlea rax, [rel $$]\njmp .b\n.b:\ncall .n\n.n:\npop rsi\njmp r13')
YN = len(Y(1)) - len(P.asm('pop rsi\njmp r13'))
XO, RT, RVT = 0x800, 0xe000, HIGH + 0x11000
RVT2, RV5 = RVT + 0x4000, RV3 + 0x4000
# Z (`pic-xm`) at ZO, X2 (`jmp` Z) at X2O, W (`pic-pfault`) at WO of RL; RT + 0x200 `jmp` Z at RV1; PF_VA's PTE zero.
ZO, X2O, WO, PF_VA = 0x400, 0xc00, 0x600, HIGH + 0x1ff000
Z = P.asm('movaps xmm2, xmm0\ndivss xmm2, xmm1\nmov ebx, 5\nlea rax, [rel $$]\njmp r13')
W_HEAD = f'mov ebx, 7\nlea rax, [rel $$]\njmp .b\n.b:\nmov ecx, 1\nmov rdx, {PF_VA:#x}\n'
W = P.asm(W_HEAD + 'mov rdx, [rdx]\njmp r13')
W_FAULT = WO + len(P.asm(W_HEAD))
REPO, DST = 0x700, 0x7c00         # `pic-rep`: R at REPO of RL, its destination at d + DST
R = lambda d: P.asm(f'mov ecx, 0x40\nlea rsi, [rel $$]\nmov rdi, {d + DST:#x}\nrep movsb\nmov ebx, 9\njmp r13')
REO = 0xb00                       # `pic-repexit`: RE at REO of RL, its destination at d + 0x7d00
RE = P.asm('rep movsb\nmov ebx, 13\njmp r13')
XFLAGS = P.SYS_D(0) + 0x7800     # `pic-xwrite`: per CPU a ready word, then (+ 0x40) a written word
JMP = lambda at, to: b'\xe9' + (to - at - 5).to_bytes(4, 'little', signed=True)
PIC = ('pic-two', 'pic-fork', 'pic-invlpg', 'pic-write', 'pic-abs', 'pic-pge', 'pic-gmap', 'pic-ctx', 'pic-twosrc', 'pic-cow',
       'pic-twoframe', 'pic-xm', 'pic-flush', 'pic-write2', 'pic-pfault', 'pic-xwrite', 'pic-user', 'pic-rep', 'pic-repexit', 'pic-again', 'pic-cross', 'pic-now',
       'l1-pic')
# Least instances, checked links and local (same-page shared) links per CPU.
PIC_MIN = {'pic-two': (2, 1, 1), 'pic-fork': (1, 0, 1), 'pic-invlpg': (1, 0, 1), 'pic-write': (1, 0, 1), 'pic-abs': (2, 1, 1),
           'pic-pge': (2, 1, 1), 'pic-gmap': (1, 0, 1), 'pic-ctx': (1, 1, 1), 'pic-twosrc': (3, 1, 1),
           'pic-cow': (1, 0, 1), 'pic-twoframe': (2, 1, 1), 'pic-xm': (2, 2, 0), 'pic-flush': (6, 2, 2), 'pic-write2': (1, 0, 1),
           'pic-pfault': (1, 0, 1), 'pic-xwrite': (1, 0, 1), 'pic-user': (0, 0, 0),
           'pic-rep': (1, 0, 0), 'pic-repexit': (2, 0, 1), 'pic-again': (2, 1, 1), 'pic-cross': (1, 0, 0), 'pic-now': (1, 0, 1),
           'l1-pic': (1, 0, 1)}
# Further `pic` counters per CPU: (counter, least, most): `pic-gmap` refuses the global instance (INSTANCE_REFUSED),
# `pic-user` shares nothing (SHARES).
PIC_COUNT = {'pic-gmap': [(27, 1, None)], 'pic-user': [(25, 0, 0)]}
# The resident's NATEMU_PIC per program (else the program driver's `all`).
PIC_ENV = {'link-full': 'off', 'pic-user': 'user'}


def reuse_bodies(i, n=1):
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
        **pic_bodies(i, n),
    }


def pic_bodies(i, n=1):
    """PIC study stage 3's programs (module docstring)."""
    d, j = P.SYS_D(i), (i + 1) % n
    put_a = lambda va, frame, g=0: f'    mov rax, {frame | P.LEAF | g:#x}\n    mov [{high_leaf(i, 0, va):#x}], rax\n'
    cr3 = lambda t: f'    mov rax, {{{t}:#x}}\n    mov cr3, rax\n'
    save = lambda *regs: ''.join(f'    mov {r}, {s}\n' for r, s in regs)
    pge = lambda on: f'    mov rax, cr4\n    {"bts" if on else "btr"} rax, 7\n    mov cr4, rax\n'
    calls = lambda tag, to, k=3: ''.join(call(f'{tag}{r}', f'{to:#x}') for r in range(k))
    mx = lambda m: f'    ldmxcsr [{d + RT + 0x800 + 4 * m:#x}]\n'
    two = lambda t: (call(f'{t}0', f'{RV1:#x}') + calls(f'{t}1', RV3) + save(('r8', 'rax'), ('r9', 'rsi')) + calls(f'{t}2', RVT)
                     + save(('r10', 'rax')) + calls(f'{t}3', RVT + 0x100) + save(('r12', 'rax'), ('r14', 'rsi')) + calls(f'{t}4', RV1)
                     + save(('r15', 'rbx')))
    twosrc = lambda t, ra, rb: (call(f'{t}0', f'{RV3:#x}') + call(f'{t}1', f'{RV1:#x}') + call(f'{t}2', f'{RVT:#x}')
                                + ''.join(call(f'{t}3{r}', f'{RVT:#x}') + save((ra, 'rax')) + call(f'{t}4{r}', f'{RVT2:#x}') + save((rb, 'rax'))
                                          for r in range(3)))
    out = {
        'pic-two': two('t'),
        # (#GP and #PF resume at the call's return label: a forged slot's addresses fault.)
        'pic-again': (again_idt(d) + two('a') + two('t') + '    jmp .end\n.h13:\n.h14:\n    add rsp, 8\n    mov [rsp], r13\n    iretq\n'
                      '.end:\n'),
        'pic-fork': (call('f0', f'{RV3:#x}') + calls('f1', RV1) + save(('r8', 'rax')) + cr3('tb') + calls('f2', RV1)
                     + save(('r9', 'rax'), ('r10', 'rsi')) + cr3('ta')),
        'pic-invlpg': (call('i0', f'{RV1:#x}') + calls('i1', RV3) + save(('r8', 'rbx')) + put_a(RV3, d + RL2)
                       + f'    mov rax, {RV3:#x}\n    invlpg [rax]\n' + calls('i2', RV3) + save(('r9', 'rbx'), ('r12', 'rax'))
                       + calls('i3', RV1) + save(('r10', 'rbx'))),
        'pic-write': (call('w0', f'{RV1:#x}') + calls('w1', RV3) + calls('w2', RV1) + save(('r8', 'rbx'))
                      + f'    mov byte [{d + RL + 1:#x}], 3\n' + calls('w3', RV3) + save(('r9', 'rbx')) + calls('w4', RV1)
                      + save(('r10', 'rbx'))),
        'pic-abs': (call('a0', f'{RV3:#x}') + call('a1', f'{RV1:#x}') + calls('a2', RV1 + XO) + save(('r8', 'rax'), ('r9', 'rsi'))
                    + cr3('tb') + calls('a3', RV2 + XO) + save(('r10', 'rax'), ('r12', 'rsi')) + cr3('ta')),
        'pic-pge': (call('p0', f'{RV1:#x}') + calls('p1', RV3) + calls('p2', RVT) + pge(False) + pge(True) + calls('p3', RVT)
                    + save(('r8', 'rax'), ('r9', 'rsi')) + pge(False) + calls('p4', RVT + 0x100) + save(('r10', 'rax'))),
        'pic-gmap': (pge(True) + call('g0', f'{RV1:#x}') + calls('g1', RV3) + save(('r8', 'rax')) + put_a(RV3, d + RL, 0x100)
                     + f'    mov rax, {RV3:#x}\n    invlpg [rax]\n' + calls('g2', RV3) + save(('r9', 'rax'), ('r10', 'rsi'))
                     + pge(False)),
        'pic-ctx': ('    mov rax, cr4\n    or rax, 0x600\n    mov cr4, rax\n' + mx(0) + call('c0', f'{RV3:#x}') + call('c1', f'{RV1:#x}')
                    + calls('c2', RV1 + XO) + ''.join(mx(m) + call(f'c3{m}', f'{RV1:#x}') for m in range(1, 5)) + mx(0)
                    + calls('c4', RV1 + XO) + save(('r8', 'rax'), ('r9', 'rsi'))),
        'pic-twosrc': (call('s0', f'{RV3:#x}') + call('s1', f'{RV1:#x}') + call('s2', f'{RVT:#x}')
                       + ''.join(call(f's3{r}', f'{RVT:#x}') + save(('r8', 'rax')) + call(f's4{r}', f'{RVT2:#x}') + save(('r9', 'rax'))
                                 for r in range(3))),
        'pic-cow': (call('o0', f'{RV1:#x}') + calls('o1', RV3) + cr3('tb') + calls('o2', RV1) + save(('r8', 'rbx'), ('r9', 'rax'))
                    + cr3('ta') + call('o3', f'{RV1:#x}') + save(('r10', 'rbx')) + call('o4', f'{RV3:#x}') + save(('r12', 'rbx'))),
        'pic-twoframe': (call('s0', f'{RV3:#x}') + call('s1', f'{RV1:#x}') + call('s2', f'{RVT:#x}')
                         + ''.join(call(f's3{r}', f'{RVT:#x}') + save(('r8', 'rax'), ('r10', 'rbx')) + call(f's4{r}', f'{RVT2:#x}')
                                   + save(('r9', 'rax'), ('r12', 'rbx')) for r in range(3))),
        'pic-xm': ('    mov rax, cr4\n    or rax, 0x600\n    mov cr4, rax\n' + ctx_idt(d) + '    xor r14d, r14d\n    xor r15d, r15d\n'
                   '    mov eax, 0x3f800000\n    movd xmm0, eax\n    xorps xmm1, xmm1\n' + mx(0) + call('z0', f'{RV3 + ZO:#x}')
                   + call('z1', f'{RV1 + ZO:#x}') + calls('z2', RV1 + X2O) + call('z3', f'{RVT + 0x200:#x}') + calls('z4', RVT2 + 0x200)
                   + calls('z5', RVT + 0x200, 2) + save(('r8', 'rbx')) + ''.join(mx(m) + call(f'z6{m}', f'{RV1 + ZO:#x}') for m in range(1, 5))
                   + mx(3) + '    xor ebx, ebx\n' + call('z7', f'{RV1 + X2O:#x}') + call('z8', f'{RVT2 + 0x200:#x}') + call('z9', f'{RVT + 0x200:#x}')
                   + save(('r9', 'r15'), ('r10', 'rbx')) + mx(0) + call('za', f'{RV1 + X2O:#x}') + save(('r12', 'rbx'))
                   + call('zb', f'{RVT2 + 0x200:#x}') + save(('rdi', 'rax')) + '    jmp .end\n' + CTX_HANDLERS + '.end:\n'),
        'pic-flush': (twosrc('s', 'r10', 'r12') + f'    mov rax, {d + 0x7f00:#x}\n    mov dr0, rax\n    mov eax, 1\n    mov dr7, rax\n'
                      '    xor eax, eax\n    mov dr7, rax\n' + twosrc('u', 'r8', 'r9')),
        'pic-write2': (calls('v0', RVT + 0x100, 2) + call('v1', f'{RV3:#x}') + call('v2', f'{RVT + 0x100:#x}') + save(('r8', 'rbx'))
                       + f'    mov byte [{d + RL + 1:#x}], 3\n' + call('v3', f'{RVT + 0x100:#x}') + save(('r9', 'rbx'))
                       + call('v4', f'{RV3:#x}') + save(('r10', 'rbx'))),
        'pic-pfault': (adv_idt(d) + call('q0', f'{RV1 + WO:#x}') + calls('q1', RV3 + WO) + save(('r8', 'rdi'), ('r9', 'r15'), ('r10', 'r14'))
                       + calls('q2', RV1 + WO, 2) + save(('r12', 'rdi')) + '    jmp .end\n'
                       + '.h:\n    pop r14\n    mov r15, cr2\n    mov rdi, [rsp]\n    mov [rsp], r13\n    iretq\n.end:\n'),
        'pic-xwrite': (call('x0', f'{RV1:#x}') + calls('x1', RV3) + calls('x2', RV1, 2) + save(('r8', 'rbx'))
                       + f'    mov qword [{XFLAGS + 8 * i:#x}], 1\n.h1:\n    pause\n    cmp qword [{XFLAGS + 8 * j:#x}], 1\n    jne .h1\n'
                       f'    mov byte [{P.SYS_D(j) + RL + 1:#x}], 3\n    mov qword [{XFLAGS + 0x40 + 8 * j:#x}], 1\n'
                       f'.h2:\n    pause\n    cmp qword [{XFLAGS + 0x40 + 8 * i:#x}], 1\n    jne .h2\n    xor eax, eax\n    cpuid\n'
                       + calls('x3', RV3) + save(('r9', 'rbx')) + calls('x4', RV1, 2) + save(('r10', 'rbx'))),
    }
    out['pic-rep'] = (call('e0', f'{RV1 + REPO:#x}') + calls('e1', RV3 + REPO) + save(('r8', 'rcx'), ('r9', 'rdi'), ('r10', 'rbx'))
                      + calls('e2', RV1 + REPO, 2) + save(('r12', 'rdi')))
    rep = lambda tag, va, n: (f'    mov ecx, {n}\n    mov rsi, {RV1:#x}\n    mov rdi, {d + 0x7d00:#x}\n' + call(tag, f'{va:#x}'))
    out['pic-repexit'] = (call('n0', f'{RV5 + REO + 2:#x}') + rep('n1', RV1 + REO, 0) + rep('n2', RV3 + REO, 0) + rep('n3', RV3 + REO, 0)
                          + rep('n4', RV5 + REO, 1) + save(('r8', 'rbx'), ('r9', 'rcx'), ('r10', 'rdi')) + rep('n5', RV5 + REO, 1)
                          + save(('r12', 'rdi')))
    inv = lambda va: f'    mov rax, {va:#x}\n    invlpg [rax]\n'
    leaf0 = lambda va: f'    xor eax, eax\n    mov [{high_leaf(i, 0, va):#x}], rax\n'
    out['pic-cross'] = (call('k0', f'{RV1 + RX:#x}') + save(('r8', 'rax')) + calls('k1', RV3 + RX) + save(('r9', 'rax'), ('r10', 'rdx'))
                        + put_a(RV3 + 0x1000, d + RLC) + inv(RV3 + 0x1000) + calls('k2', RV3 + RX, 2) + save(('r12', 'rax'), ('r14', 'rdx'))
                        + leaf0(RV3 + 0x1000) + inv(RV3 + 0x1000) + put_a(RV3 + 0x1000, d + RL2) + inv(RV3 + 0x1000)
                        + calls('k3', RV3 + RX, 2) + save(('r15', 'rax')) + call('k4', f'{RV1 + RX:#x}') + save(('rbx', 'rax')))
    out['pic-now'] = (call('h0', f'{RV3:#x}') + call('h1', f'{RV1 + XO:#x}') + calls('h2', RV3 + XO) + save(('r8', 'rax'), ('r9', 'rsi'))
                      + f'    mov byte [{d + RL + 1:#x}], 3\n' + call('h3', f'{RV3 + XO:#x}') + save(('r10', 'rbx'))
                      + call('h4', f'{RV1 + XO:#x}') + save(('r12', 'rbx')))
    out['pic-user'] = out['pic-two']
    return out


# Perf chunk 11 (`link-full`, natemu only): more links than the record area holds. FM linear pages from HIGH (table A)
# map frame FC of 2048 `jz $+2` (two link points each, both to the next RIP: 2 * 2048 * FM records > MAX_LINKS; the
# pages but one are copies of the first's blocks), page FW its copy FWC, then page FE (`inc ebx; jmp r13`). A pass adds
# 1 to ebx, and 1 per `inc ebx` in the chain. Phases of three passes: (r8) as placed; (r9) FWC + 0x800 rewritten to
# `inc ebx` by CPU (i - 1) % N, so the links into its blocks die on CPU i; (r10) page FH remapped to FC2 (`inc ebx` at
# 0x400 and 0x404) with a CR3 reload; (r12) back to FC with INVLPG. Each pass runs every link point through links kept,
# dropped and remade; the record area fills in every pass, and each fill costs at most two passes over it (`reclaims`
# bounded by the links made).
MAX_LINKS = 512 * 1024                    # natemu_abi::execution::cache::MAX_LINKS
# `link-full` overflows a test-only capacity by default (the host's NATEMU_LINK_CAP -> `cache::Control::link_cap`): the
# same linker paths (full area, local prune, unlink-all, reclaim bookkeeping), at a chain 28 times shorter. `--full-links`
# runs the production size (cap MAX_LINKS, no env). Each of the FM pages holds 4096 link points. At MAX_LINKS FM = cap/4096
# + 12: the first fill leaves a 12-page tail and FW lies in it, so its links are live when it is written. At a small cap
# FM = cap/4096 + 1 (5 at 16 Ki: the chain is 1.25 x the cap, 1.09 x at full size). A link-by-link model of the linker's
# fill (clear everything when links + 4 > cap; three passes) that tolerates 0-60 further links from other blocks leaves
# pages FM-3 .. FM-1 live at the end of a phase, so FW = FM-2 is live then, as at full size (FM-10 is not, at a small cap).
LINK_CAP = 16 * 1024
FM = FW = FH = CAP = 0


def set_link_cap(cap):
    """Sizes the `link-full` chain for a record capacity `cap` (MAX_LINKS, or a multiple of 4096 of at least 16 Ki: FM >= 5; at 8 or 12 Ki FW == FH == FM // 2 and the r10 remap breaks the geometry)."""
    global CAP, FM, FW, FH
    assert cap == MAX_LINKS or (cap % 4096 == 0 and 16 * 1024 <= cap < MAX_LINKS), cap
    CAP = cap
    assert 2 * 2048 * (cap // 4096 + 1) > cap
    FM = cap // 4096 + (12 if cap == MAX_LINKS else 1)   # 140 at 512 Ki (76 at 256 Ki): 2 * 2048 * FM records > cap
    FW, FH = (FM - 10 if cap == MAX_LINKS else FM - 2), FM // 2   # FW late in the chain: a fill keeps the chain's tail
set_link_cap(LINK_CAP)
FC, FWC, FC2, FE = 0x9000, 0xa000, 0xb000, 0xc000
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
    if name == 'pic-cross':
        return {RV1: RL, RV1 + 0x1000: RL2, RV3: RL, RV3 + 0x1000: RL2}, {}
    if name == 'pic-fork':
        return {RV1: RL, RV3: RL}, {RV1: RL}
    if name == 'l1-pic':
        return {RV1: RL, RV6: RL}, {}
    if name in ('pic-two', 'pic-pge', 'pic-write2', 'pic-user', 'pic-again'):
        return {RV1: RL, RV3: RL, RVT: RT}, {}
    if name in ('pic-twosrc', 'pic-flush', 'pic-xm', 'pic-repexit'):
        return {RV1: RL, RV3: RL, RV5: RL, RVT: RT, RVT2: RT}, {}
    if name == 'pic-twoframe':
        return {RV1: RL, RV3: RL, RV5: RL2, RVT: RT, RVT2: RT}, {}
    if name == 'pic-cow':
        return {RV1: RL, RV3: RL}, {RV1: RL2}
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
    if name in ('reuse-cross', 'pic-cross'):
        first = bytearray(0x1000)
        first[RX:] = P.asm('lea rdx, [rel $+7]') + b'\xb8\x00'
        return tables + [{'addr': f'0x{d + RL:x}', 'data': first.hex()}, {'addr': f'0x{d + RL2:x}', 'data': TAIL(1).hex()},
                         {'addr': f'0x{d + RLC:x}', 'data': TAIL(2).hex()}]
    if name in PIC:
        page = bytearray(0x1000)
        page[:len(Y(1))] = Y(1)
        page[XO:XO + 5] = JMP(XO, 0)
        page[ZO:ZO + len(Z)], page[X2O:X2O + 5], page[WO:WO + len(W)] = Z, JMP(X2O, ZO), W
        page[REPO:REPO + len(R(d))] = R(d)
        page[REO:REO + len(RE)] = RE
        tramp = bytearray(0x1000)
        tramp[:5], tramp[0x100:0x105], tramp[0x200:0x205] = JMP(RVT, RV3), JMP(RVT + 0x100, RV1), JMP(RVT + 0x200, RV1 + ZO)
        tramp[0x800:0x800 + 4 * len(CTX_MXCSR)] = b''.join(m.to_bytes(4, 'little') for m in CTX_MXCSR)
        extra = [entry(high_leaf(i, 0, PF_VA), 0)] if name == 'pic-pfault' else []
        extra += [dict(P.zero(d + IDT, 0x1000), capture=False)] if name in ('pic-pfault', 'pic-xm', 'pic-again') else []
        extra += [dict(P.zero(XFLAGS, 0x100), capture=False)] if name == 'pic-xwrite' and i == 0 else []
        return tables + extra + [{'addr': f'0x{d + RL:x}', 'data': page.hex()}, {'addr': f'0x{d + RL2:x}', 'data': Y(2).hex()},
                                 {'addr': f'0x{d + RT:x}', 'data': tramp.hex()}]
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
            'pic-two': {'r8': {RV3}, 'r9': {RV3 + YN}, 'r10': {RV3}, 'r12': {RV1}, 'r14': {RV1 + YN}, 'r15': {1}},
            'pic-fork': {'r8': {RV1}, 'r9': {RV1}, 'r10': {RV1 + YN}},
            'pic-invlpg': {'r8': {1}, 'r9': {2}, 'r12': {RV3}, 'r10': {1}},
            'pic-write': {'r8': {1}, 'r9': {3}, 'r10': {3}},
            'pic-abs': {'r8': {RV1}, 'r9': {RV1 + YN}, 'r10': {RV2}, 'r12': {RV2 + YN}},
            'pic-pge': {'r8': {RV3}, 'r9': {RV3 + YN}, 'r10': {RV1}},
            'pic-gmap': {'r8': {RV3}, 'r9': {RV3}, 'r10': {RV3 + YN}},
            'pic-ctx': {'r8': {RV1}, 'r9': {RV1 + YN}},
            'pic-twosrc': {'r8': {RV3}, 'r9': {RV5}},
            'pic-cow': {'r8': {2}, 'r9': {RV1}, 'r10': {1}, 'r12': {1}},
            'pic-twoframe': {'r8': {RV3}, 'r9': {RV5}, 'r10': {1}, 'r12': {2}},
            'pic-xm': {'r8': {5}, 'r9': {4}, 'r10': {0}, 'r12': {5}, 'rdi': {RV3 + ZO}},
            'pic-flush': {'r8': {RV3}, 'r9': {RV5}, 'r10': {RV3}, 'r12': {RV5}},
            'pic-write2': {'r8': {1}, 'r9': {3}, 'r10': {3}},
            'pic-pfault': {'r8': {RV3 + W_FAULT}, 'r9': {PF_VA}, 'r10': {0}, 'r12': {RV1 + W_FAULT}},
            'pic-xwrite': {'r8': {1}, 'r9': {3}, 'r10': {3}},
            'pic-rep': {'r8': {0}, 'r9': {d + DST + 0x40}, 'r10': {9}, 'r12': {d + DST + 0x40}},
            'pic-repexit': {'r8': {13}, 'r9': {0}, 'r10': {d + 0x7d01}, 'r12': {d + 0x7d01}},
            'pic-again': {'r8': {RV3}, 'r9': {RV3 + YN}, 'r10': {RV3}, 'r12': {RV1}, 'r14': {RV1 + YN}, 'r15': {1}},
            'pic-now': {'r8': {RV3}, 'r9': {RV3 + YN}, 'r10': {3}, 'r12': {3}},
            'pic-cross': {'r8': {0x100}, 'r9': {0x100}, 'r10': {RV3 + 0xffe}, 'r12': {0x200}, 'r14': {RV3 + 0xffe}, 'r15': {0x100},
                          'rbx': {0x100}},
            'pic-user': {'r8': {RV3}, 'r9': {RV3 + YN}, 'r10': {RV3}, 'r12': {RV1}, 'r14': {RV1 + YN}, 'r15': {1}},
            # Two instances of one translation in one L1 set: each with its own page base (rax) and return address (rsi).
            'l1-pic': {'r8': {RV1}, 'r9': {RV1 + YN}, 'r10': {RV6}, 'r12': {RV6 + YN}},
        }[name]
    return judge
# (`l1-pic` shares one translation across two aliases; it asserts no copies: it is not a reuse program.)
REUSE = {'reuse-alias': (1, 0), 'reuse-write': (1, 0), 'reuse-remap': (1, 0), 'reuse-cross': (1, 1), **{n: (1, 0) for n in PIC if n != 'l1-pic'}}

IDT = 0x3000
CX, CD = 0x9000, 0xa000                       # `contexts`: its code page and its MXCSR values (A, C, D, E, F)
CTX_MXCSR = (0x1f80, 0x0f80, 0x1780, 0x1d80, 0x1e80)


def again_idt(d):
    """`pic-again`: an IDT at d + IDT with #GP (13, handler .h13) and #PF (14, .h14)."""
    return ctx_idt(d, (13, 14))


def ctx_idt(d, vectors=(7, 19)):
    """`contexts`: an IDT at d + IDT with #NM (7, handler .h7) and #XM (19, .h19)."""
    gate = lambda v: (f'    lea rax, [rel .h{v}]\n    mov [{d + IDT + 16 * v:#x}], ax\n    mov word [{d + IDT + 16 * v + 2:#x}], 8\n'
                      f'    mov word [{d + IDT + 16 * v + 4:#x}], 0x8e00\n    shr rax, 16\n    mov [{d + IDT + 16 * v + 6:#x}], ax\n'
                      f'    shr rax, 16\n    mov [{d + IDT + 16 * v + 8:#x}], eax\n')
    return (''.join(gate(v) for v in vectors) + f'    mov word [{d + IDT + 0x800:#x}], 0xfff\n    mov rax, {d + IDT:#x}\n    mov [{d + IDT + 0x802:#x}], rax\n'
            f'    lidt [{d + IDT + 0x800:#x}]\n')


def ctx_call(tag, d, mxcsr):
    """`contexts`: LDMXCSR of value `mxcsr` (an index into CTX_MXCSR; None: none), then the call; r8 += 1 if it ran."""
    load = '' if mxcsr is None else f'    ldmxcsr [{d + CD + 4 * mxcsr:#x}]\n'
    return load + f'    xor eax, eax\n' + call(f'x{tag}', f'{d + CX:#x}') + '    add r8, rax\n' + ctx_triple(tag)


def ctx_triple(tag):
    """`contexts` (two-way L1): Q1, Q2, Q3 (CQ1, CQ2, CQ3: one L1 set, three RIPs, so they evict each other) called in turn
    under the context just loaded: each runs its own code (r11 += 0x1 + 0x10 + 0x300 a time)."""
    return ''.join(call(f'y{tag}{j}', f'{LP + at:#x}') + '    add r11, rax\n' for j, at in enumerate((CQ1, CQ2, CQ3)))

def ctx_kept(i):
    """`contexts`, after the rounds: under C (another L1 table), V is remapped to frame W1 with INVLPG, then W1's bytes are
    rewritten, then V is remapped to W2 with a CR3 reload (no INVLPG); after each, V is called under A, whose table still
    held V's entry: it must run the new bytes (r9 = 2, r10 = 3, r12 = 4)."""
    d = P.SYS_D(i)
    V, W1, W2 = d + CX, d + CW1, d + CW2
    load = lambda m: f'    ldmxcsr [{d + CD + 4 * m:#x}]\n'
    put = lambda frame: f'    mov rax, {frame | P.LEAF:#x}\n    mov [{P.sys_leaf(i, 0, V):#x}], rax\n'
    run = lambda tag, reg: load(0) + '    xor eax, eax\n' + call(f'k{tag}', f'{V:#x}') + f'    mov {reg}, rax\n'
    # Perf chunk 19 (L1 indexed by 16 RIP bits): Q1 and Q2 (LP + 0x1f1000 and + 0x1d1000, one entry of every table) called
    # in turn under A three times: each runs its own code (rbx = 3, rsi = 0x30); under C, LP's large page remapped to
    # LF2 with a CR3 reload (no INVLPG); Q2 called under A must run LF2's code (rdi = 0x20): the reload reaches A's entry.
    q = lambda tag, at, reg: call(f'q{tag}', f'{LP + at:#x}') + f'    add {reg}, rax\n'
    pde = f'    mov rax, {LF2 | 0x80 | P.LEAF:#x}\n    mov [{P.SYS_T(i, 0) + 0x2000 + 8 * (LP >> 21):#x}], rax\n'
    return (load(1) + put(W1) + f'    mov rax, {V:#x}\n    invlpg [rax]\n' + run(0, 'r9')
            + load(1) + f'    mov byte [{W1 + 1:#x}], 3\n' + run(1, 'r10')
            + load(1) + put(W2) + '    mov rax, cr3\n    mov cr3, rax\n' + run(2, 'r12')
            + load(0) + '    xor ebx, ebx\n    xor esi, esi\n    xor edi, edi\n    xor ebp, ebp\n'
            + ''.join(q(f'{r}a', CQ1, 'rbx') + q(f'{r}b', CQ2, 'rsi') + q(f'{r}c', CQ3, 'rbp') for r in range(3))
            + load(1) + pde + '    mov rax, cr3\n    mov cr3, rax\n' + load(0) + q('c', CQ2, 'rdi') + q('d', CQ3, 'rbp'))

CW1, CW2 = 0xb000, 0xc000                     # `contexts`: V's other frames (`mov eax, 2` and `mov eax, 4`)
CQ1, CQ2, CQ3 = 0x1f1000, 0x1d1000, 0x1b1000  # `contexts`: Q1, Q2 and Q3 in LP (the same low 16 bits: one L1 set of the two-way table, three RIPs)

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
    # (Two-way L1: rbp = Q3's three runs (0x300) and its run after the remap (0x400); r11 = Q1 + Q2 + Q3 (0x311) after each of the
    # 8 K + 1 calls under a context.)
    'contexts': lambda i, k: {'r8': {5 * k + 1}, 'r14': {k}, 'r15': {2 * k}, 'r9': {2}, 'r10': {3}, 'r12': {4}, 'rbx': {3}, 'rsi': {0x30}, 'rdi': {0x20},
                              'rbp': {0xd00}, 'r11': {(8 * k + 1) * 0x311}},
    # Perf chunk 8: natemu's choice where the architecture allows either (r9 of `place-global`, `invlpg-page`, `local-gpa`).
    'place-global': {'r8': {1}, 'r9': {1}, 'r10': {2}, 'r12': {0x100}, 'rsi': {0x200}, 'rdi': {3}, 'rbp': {1}},
    'invlpg-page': {'r8': {3}, 'r9': {3}, 'r10': {2}},
    'local-gpa': {'r9': {2}, 'r10': {1}},
    'invlpg-large': {'r8': {1}, 'rbx': {0x22}, 'r9': {2}, 'r10': {1}},
    'pf-large': {'r8': {1}, 'r14': {1}, 'r9': {2}},
    'large-alias': {'r10': {1}, 'r8': {1}, 'r9': {2}},
    'large-reval': {'r8': {1}, 'r10': {1}, 'r9': {2}},
    **{f'large-{m}': {'r8': {m}, 'r9': {2}} for m in LM},
    **{name: reuse_either(name) for name in (*REUSE, 'l1-pic')},
    # CPUID leaves differ from QEMU's: natemu only. Per round (K rounds): A once (and again at the end unless -x), B, and the
    # CPUID's way into B (eax = 2 from B's own code).
    'l1-dup': lambda i, k: {'r8': {k}, 'r9': {2 * k}, 'r10': {2 * k}, 'r12': {k}},
    'l1-dup-x': lambda i, k: {'r8': {k}, 'r9': {2 * k}, 'r10': {2 * k}, 'r12': {2 * k}},
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
                      {'addr': f'0x{d + CW1:x}', 'data': P.asm('mov eax, 2\njmp r13').hex()}, {'addr': f'0x{d + CW2:x}', 'data': P.asm('mov eax, 4\njmp r13').hex()},
                      {'addr': f'0x{P.SYS_T(i, 0) + 0x2000 + 8 * (LP >> 21):x}', 'data': (LF1 | 0x80 | P.LEAF).to_bytes(8, 'little').hex(), 'capture': False}]
            extra += [{'addr': f'0x{f + at:x}', 'data': P.asm(f'mov eax, {k}\njmp r13').hex()}
                      for f, at, k in ((LF1, CQ1, 1), (LF1, CQ2, 0x10), (LF2, CQ2, 0x20), (LF1, CQ3, 0x300), (LF2, CQ3, 0x400))] if i == 0 else []
        out['memory'] += P.sys_tables(i, 0, n, a) + tb + [
            P.zero(d, 0x1000), {'addr': f'0x{d + F1:x}', 'data': CODE(1).hex()}, {'addr': f'0x{d + F2:x}', 'data': CODE(2).hex()},
            {'addr': f'0x{d + 0x3ffe:x}', 'data': 'b800'}, dict(P.zero(d + P.SAVE, 8), capture=False)] + (extra + links_memory(name, i, n) + (reuse_memory(name, i) if name in REUSE or name == 'l1-pic' else [])
            + (full_memory(i) if name == 'link-full' else []) + (l1_memory(name, i) if name in L1_ALL else []))
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


# The two-way L1 counts (module docstring): per program, what the difference of its dispatcher misses over rounds must be.
COUNT_CHECKS = ('l1-pair', 'l1-triple', 'l1-triple-d', 'l1-global', 'l1-dup', 'l1-pic')


def perf_runtime(a):
    """The resident with its `perf` feature (the dispatcher-miss count), built into its own target directory."""
    if a.perf_runtime:
        return a.perf_runtime
    target = ROOT / 'target' / 'perf'
    out = target / 'aarch64-unknown-none/release/natemu-runtime'
    if not (os.environ.get('NATEMU_PREBUILT') == '1' and out.exists()):
        subprocess.run(['cargo', '+1.97.0', 'build', '--offline', '--locked', '--release', '-p', 'natemu-runtime', '--target', 'aarch64-unknown-none',
                        '--features', 'perf'], cwd=ROOT, env=dict(os.environ, CARGO_TARGET_DIR=str(target)), check=True)
    a.perf_runtime = out
    return out


def l1_counts(a, n, name, report):
    """`name`'s dispatcher misses on each vCPU, as differences of runs of the program (and its variants) at K and 2K rounds:
    the setup, the tails and the glue misses cancel. Returns the failures."""
    k, failures, seen = a.k, [], {}
    def m(prog, rounds, field=0):
        if (prog, rounds) not in seen:
            out, problems = run(a, program(prog, n, rounds), [], None, perf_runtime(a), f'-m{rounds}')
            failures.extend(f'{prog}@{n} k={rounds}: {p}' for p in problems)
            k1 = [c['stats']['k1'] for c in out['cpus']] if out else []
            seen[prog, rounds] = [(x['dispatch_misses'], x['l1_counts'][6]) for x in k1] or [(None, None)] * n
        return [x[field] for x in seen[prog, rounds]]
    def expect(label, got, want):
        if any(g is None for g in got) or not any(m(name, k)):
            failures.append(f'{name}@{n} {label}: no counts (is the resident built with `perf`?)')
        elif not all(want(g) for g in got):
            failures.append(f'{name}@{n} {label} = {got}')
    diff = lambda p, r1, p2, r2, f=0: [x - y if x is not None and y is not None else None for x, y in zip(m(p, r1, f), m(p2, r2, f))]
    if name in ('l1-pair', 'l1-pic'):
        expect(f'misses over {k} more rounds (want 0: the pair fits)', diff(name, 2 * k, name, k), lambda g: g == 0)
    if name == 'l1-triple':
        expect(f'misses over {k} more rounds of A, B, C (want {3 * k}: three a round)', diff(name, 2 * k, name, k), lambda g: g == 3 * k)
        expect('misses of the tail A, B after C against the same tail before the triples (want 2)', diff(name, k, 'l1-triple-t0', k), lambda g: g == 2)
        expect('misses of two more tails (want 0)', diff('l1-triple-t3', k, name, k), lambda g: g == 0)
        expect('L1_NEW_CONFLICT over more rounds (want 0: A, B, C agree in bit 15)', diff(name, 2 * k, name, k, 1), lambda g: g == 0)
    if name == 'l1-triple-d':
        expect(f'misses over {k} more rounds of A, B, D (want {3 * k})', diff(name, 2 * k, name, k), lambda g: g == 3 * k)
        expect('L1_NEW_CONFLICT over more rounds (want some: D differs in bit 15)', diff(name, 2 * k, name, k, 1), lambda g: g > 0)
    if name in ('l1-global', 'l1-dup'):
        expect('misses of the call of A a round against a call of B (want 0: A hits)', diff(name, k, name + '-x', k), lambda g: g == 0)
    report.append({'name': f'k1-{name}@{n}', 'max_block': None, 'stats': None, 'counts': {f'{p}/{r}': v for (p, r), v in seen.items()}, 'problems': failures})
    print(json.dumps(report[-1]), flush=True)
    return [f'counts {f}' for f in failures]


def run(a, prog, extra, host=None, runtime=None, tag=''):
    work = a.out / 'work' / f"{prog['name']}{tag}{'-1' if extra else ''}"
    full = prog['name'].startswith('k1-link-full@')
    options = FULL_CODE if full else []
    # PIC study stage 3: link-full's one frame at FM addresses would be one shared translation linked once; the record
    # area's capacity is tested on absolute code (each address its own blocks and links).
    mode = PIC_ENV.get(prog['name'][3:].split('@')[0])
    env = dict(os.environ, **({'NATEMU_PIC': mode} if mode else {}))
    env.pop('NATEMU_LINK_CAP', None)   # (an inherited export must not shrink the full size, or any other program)
    if full and CAP < MAX_LINKS:   # (the host's NATEMU_LINK_CAP: unset = production capacity)
        env['NATEMU_LINK_CAP'] = str(CAP)
    rc, out, err = s3.driver(host or a.host, runtime or a.runtime, a.fexc, s3.smp_description(prog), work, options + extra, timeout=a.timeout, env=env)
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
    ap.add_argument('--perf-runtime', type=pathlib.Path, help="a resident built with `--features perf` for the two-way L1 programs' dispatcher-miss counts (default: built into target/perf here)")
    shard.add(ap)
    ap.add_argument('--full-links', action='store_true', help=f'link-full at the production link capacity ({MAX_LINKS} records: ~6 min at 1 vCPU, ~20 at 4) instead of the test capacity {LINK_CAP} (the end-of-round suite runs this once)')
    ap.add_argument('--timeout', type=int, default=300, help='seconds a run may take before it is killed as hung (the suite gives parallel shards more: link-full is slow under load)')
    a = ap.parse_args()
    set_link_cap(MAX_LINKS if a.full_links else LINK_CAP)
    a.out.mkdir(parents=True, exist_ok=True)
    if not (a.host and a.runtime): a.host, a.runtime = vm.build()
    stub = smp.build_stub(a.out / 'qemu-build')
    report, failures = [], []
    listed = [x for x in bodies(a.k) if x not in L1_VARIANTS]
    cases = [(n, name, adv) for adv, names in ((0, listed), (1, adv_setup(0))) for n in a.cpus for name in names if not a.programs or name in a.programs]
    mine = set(shard.take(cases, a.shard))   # (CPU count, program) cases are independent; the counts below run on shard 1
    for n in a.cpus:
        for name in [x for x in bodies(a.k) if (n, x, 0) in mine]:
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
                if name in ('pic-ctx', 'pic-xm'):  # (as `contexts`: once per context)
                    problems = [p for p in problems if 'host requests' not in p]
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
                # PIC study stage 3: the least instances, checked links and local shared links (natemu_abi::pic::count).
                for v, c in enumerate(out['cpus'] if out and name in PIC else []):
                    pic = c['stats']['k1']['pic']
                    got = (pic[26], pic[28], pic[10])
                    if any(g < w for g, w in zip(got, PIC_MIN[name])):
                        problems.append(f'cpu{v} (instances, checked links, local links) {got}, at least {PIC_MIN[name]}')
                    for k, least, most in PIC_COUNT.get(name, []):
                        if pic[k] < least or most is not None and pic[k] > most:
                            problems.append(f'cpu{v} pic counter {k} = {pic[k]}, want {least}..{most}')
                    if name == 'pic-flush' and c['stats']['flushes'] < 2:
                        problems.append(f"cpu{v} flushes {c['stats']['flushes']}, want the driver's and the program's")
                # Perf chunk 11: the record area filled (more links made than it holds, no flush but the driver's first)
                # and each fill cost at most two passes over it, each followed by at least a quarter of the area's links.
                for v, c in enumerate(out['cpus'] if out and name == 'link-full' else []):
                    count, links, flushes = c['stats']['k1']['reclaims'][0], c['stats']['links'], c['stats']['flushes']
                    if not (links > CAP and count >= 1 and count <= 1 + 4 * links // CAP and flushes == 1):
                        problems.append(f'cpu{v} links {links}, full {count} times (at most {1 + 4 * links // CAP}; capacity {CAP}), flushes {flushes}')
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
    # The two-way L1 programs' dispatcher-miss counts (differences of runs at different round counts, from a perf resident).
    # (Not under a PIC test mode: `svc` fills no L1 at all.)
    for n in a.cpus:
        for name in [x for x in COUNT_CHECKS if (n, x, 0) in mine and not os.environ.get('NATEMU_PIC_TEST')]:
            failures += l1_counts(a, n, name, report)
    for n in a.cpus:
        for name in [x for x in adv_setup(0) if (n, x, 1) in mine]:
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
    if a.programs or a.shard[0] != 1: a.host = a.base_host = None
    for label, host, runtime in (('k1', a.host, a.runtime), ('base', a.base_host, a.base_runtime)):
        if not host: continue
        for k in (a.k, 2 * a.k):
            out, problems = run(a, program('cr3-switch', 1, k), [], host, runtime, f'-{label}-{k}')
            c = out and out['cpus'][0]
            counts[f'{label}-{k}'] = c and {x: c['stats'].get(x) for x in ('requests', 'compiles', 'flushes', 'k1')}
            if label == 'k1': failures += [f'counts k={k}: {p}' for p in problems]
    print('counts', json.dumps(counts), flush=True)
    k1 = [counts.get(f'k1-{k}') for k in (a.k, 2 * a.k)]
    if not a.programs and a.shard[0] == 1 and (not all(k1) or k1[0]['requests'] != k1[1]['requests'] or k1[0]['compiles'] != k1[1]['compiles']):
        failures.append(f'counts: requests or compiles grow with the rounds: {k1}')
    (a.out / 'k1.json').write_text(json.dumps({'report': report, 'counts': counts, 'failures': failures}, indent=1) + '\n')
    for f in failures: print('FAIL', f)
    sys.exit(1 if failures else 0)


if __name__ == '__main__': main()

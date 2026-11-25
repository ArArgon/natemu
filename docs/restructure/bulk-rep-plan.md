# Bulk REP MOVS inside a page: plan

November 23, 2026, on `m2-sampler` (written at d3f2fe4d, revised after review at 7c96a2a5). The first cut is built
(November 24): what task 0 measured, what was built and where it departs from this design are in section 13. It is the "REP string bodies >= 3%" row of the [EL0 sampler findings](el0-sampler-findings.md)
(section 7, verdict **build**), one of the fixes the owner pooled: no A/B of its own, one combined A/B and profile round at
the end, with the gate. It keeps its own kill-test (section 8) and correctness tests (section 9). The model is chunk 26's
bulk REP STOS ([perf plan](perf-plan.md):2928-3141), whose design, proofs and tests this plan reuses wherever the
argument is the same.

**Scope (owner: lightweight).** The first cut is REP MOVSB and MOVSQ in 64-bit code, forward (section 3). 32-bit `rep movsd`,
the hottest single form, follows only if task 0 shows enough of its copies would be eligible (section 3.2).

## 1. Which REP forms are hot

The findings give the REP share by kind only (`rep_movs`, `rep_stos`; [sample_report.py](../../tests/bench/sample_report.py):356).
To see forms, the kept outputs of the round were reduced again with the report's own reader, classifying each sampled REP
instruction by its bytes: prefix (F3, F2), opcode (A4-AF: MOVS, CMPS, STOS, LODS, SCAS), width (66, REX.W), 0x67, code
mode, privilege and symbol. **This is an experiment**: a scratch script over the `.samples` and `.objects` of runs
20261123-213409-4d92 and 20261123-213500-1086 (M2 Max, natemu a7cd8e82), not a framework case. Task 0 (section 10) puts the
per-form split into the report, so the end-of-work profile round gives these numbers from cases.

% of the phase's EL0 samples, run1 (run2 agrees within half a point):

| Case, phase | Form, privilege | % of EL0 | Where |
| --- | --- | ---: | --- |
| `guest-startup32-sample` run1 | `rep movsd`, 32-bit, user | 7.38 | `speedtest1-32`'s static i386 glibc (6.53 in blocks too short to name, 0.85 named) |
| | `rep movsb`, 32-bit, user | 0.31 | |
| `guest-zstd-sample` run1 | `rep movsb`, 64-bit, user | 5.27 | busybox in the verify phase (51.9% of that phase's EL0) |
| | `rep movsq`, kernel | 0.91 | `rep_movs_alternative` (10.7% of the decompress phase) |
| `guest-hackbench-sample` run1 | `rep stosq`, kernel | 9.50 | `clear_page_rep`: already bulk (chunk 26) |
| | `rep movsq`, kernel | 2.68 | `rep_movs_alternative` |
| `io5-solo32-sample` i386-make | `rep movsd`, 32-bit, user | 3.27 | (io5 user samples stay at block level) |
| | `rep stosd`, 32-bit, user | 0.91 | not bulk today: chunk 26 is 64-bit code only |
| `io5-solo32-sample` manifest, fetch-python | `rep movsq`, kernel | 3.47, 3.29 | `rep_movs_alternative` |
| `guest-build-sample` run1 | `rep movsq`, 64-bit, user | 1.68 | blocks too short to name (gcc's inline struct copies, by the review's cc1 counts) |
| | `rep stosq`, kernel / user | 0.89 / 0.27 | |
| `io5-solo32-sample` make | all REP | 1.04 | `clear_page_rep` 0.36, user `rep stosq` 0.27, user `rep movsq` 0.21 |
| `guest-{luajit,sqlite,scimark32-sse2}-sample` | all REP | 0.0-0.8 | `clear_page_rep` |

Over every case's whole run the only forms sampled are F3 MOVS and STOS: no CMPS, SCAS or LODS sample, no F2 prefix, no
0x67 form, in any phase or privilege. The kernel's `clear_page_rep` is already bulk; its samples sit on the loop's `stp`
(hackbench's top block: 331 of 353 samples on one store word), the memory itself.

The samples hold no registers, so neither sizes nor pointer alignment are known. From the code: `rep_movs_alternative`
uses `rep movsq` only for 64 bytes and more (shorter copies take its byte and quadword loops; ERMS is not advertised);
hackbench's messages are 100 bytes (12 quadwords and a 4-byte tail); file reads copy up to a page. i386 glibc's `rep movsd`
copies need not have 8-aligned pointers, and memcpy-style code ends with a 1-3 byte `rep movsb` tail. Task 0 measures both
before anything is built.

What changed since chunk 26 decided not to build MOVS ([perf plan](perf-plan.md):2946-2949: "under 3% on every
workload"): 32-bit programs run now, and the sampler places zstd's `rep movsb` at 5.3% of EL0.

## 2. What runs today

- One element per pass ([OpcodeDispatcher.cpp](../../third_party/FEX/FEXCore/Source/Interface/Core/OpcodeDispatcher.cpp):3303-3424,
  FEX at the pinned 13a1bfcf): RCX = 0 ends the instruction; else the element's load and store (the sites), RSI/RDI step
  by DF, RCX - 1, and a literal exit to the instruction's own RIP, which the resident links to the block starting there. So
  every element after the first runs a block that is the REP alone, through a linked `b`: about one cycle per element.
  **Every element re-enters at the REP's RIP**, so whatever the bulk part adds before the element pass is paid again by
  every element of a copy the bulk part declines (section 4.3). Chunk 26 measured (M4): 4 KiB `rep movsq` 166 ns, `rep
  movsb` 1,049 ns, against a native 16-bytes-a-round copy loop of 74 ns (L1) to 139 ns (a 256 MiB stream)
  ([perf plan](perf-plan.md):2942-2946).
- REP STOS bulk (chunk 26): FEX's `FexcRepStos` runs before the one-element pass, with `--bulk`
  ([fexc_hook.h](../../tools/fexc/shim/fexc_hook.h):250-253), which the host sends for every translate request that is not
  a step and not alignment-gated ([execution.rs](../../crates/host/src/execution.rs):4659). The JIT op is natemu's
  ([fexc_sites.inc](../../tools/fexc/shim/fexc_sites.inc):716-766): fast-string byte, DF, RDI 8-aligned for w >= 2,
  x1 = min(RCX's whole 16-byte chunks, the chunks before RDI's page end), then `stp; add; sub; sub; cbnz`. Its `stp` is a
  store-pair site (fexc_sites.inc:1314-1316) and not a committed effect (fexc_sites.inc:1373-1376, which names
  `OP_FEXCREPSTOS` only), and a guest-memory op for the precision passes
  ([fexc_precision.h](../../tools/fexc/shim/fexc_precision.h):26-28). Admission pins its words exactly
  ([structure.rs](../../crates/host/src/admission/structure.rs):541-631, 1124-1165), exempts its store from the split check
  (structure.rs:2061-2081) and clears the loop's registers at its back edge in the stack-check dataflow
  (structure.rs:2141-2145).
- The site recorder finds one memory word per IR op, scanning back from the op's end for the last load/store-class word
  (fexc_sites.inc:1338-1356). A load-pair kind exists (`FEXC_SITE_LOAD_PAIR`, fexc_hook.h:47; admission treats it as a load,
  structure.rs:1703-1706).

## 3. What to build

### 3.1 The first cut: REP MOVSB and MOVSQ, 64-bit code, forward

F3 A4 and F3 REX.W A5 in 64-bit code; no 0x67, no segment-override prefix, no F2; DF = 0; the fast-string bit set; for
MOVSQ both RSI and RDI 8-aligned (any for bytes); RDI - RSI outside [1, 16] modulo 4096 (section 5.1). Otherwise the
instruction runs as today. Covers zstd's `rep movsb` (5.3% of EL0), the kernel's `rep_movs_alternative` (hackbench 2.7,
io5's file copies 3.3-3.5) and build's inline `rep movsq` (1.7), if task 0 shows their copies eligible.

### 3.2 After task 0, only if its numbers ask

- **`rep movsd` in 32-bit code** (startup32 7.4%, i386-make 3.3%), if task 0 shows most of its copies, by bytes, are at
  least one round long with both pointers 4-aligned. Its round pairs two W registers: `ldp w, w; stp w, w`, 8 bytes, so it
  needs only 4-alignment, each element a naturally aligned single 4-byte access (no FEAT_LSE2 argument needed), with
  32-bit pointer and count updates (section 4.4). Then REP STOS in 32-bit code (0.9% of i386-make) the same way, and the
  32-bit real-x86 cases (9.3).
- **MOVSQ with pointers not 8-aligned**, if task 0 shows many: a proof that an `ldp`/`stp` X half at a misaligned address
  has the single-copy atomicity of today's one-element `ldr`/`str` at that address (FEAT_LSE2), or a W-pair round.

Not planned: DF = 1, 0x67 forms, CMPS, SCAS, LODS (none sampled); a second loop for the bytes before a page end that are not
a whole round.

**A separate option, flagged, not planned: advertising ERMS or FSRM.** With FSRM the kernel's copy_user and `memcpy` take
`rep movsb` for every size (instead of `memcpy_orig` and `rep_movs_alternative`'s loops); with ERMS `clear_page_erms` takes
`rep stosb` and glibc its `_erms` paths for large sizes. Once `rep movsb` is bulk, this would move more copies onto the bulk
path. It changes guest-visible CPUID (the profile's model, [boot-features plan](boot-features-plan.md):93 and 718-720:
ERMS/FSRM stay unadvertised): an owner decision, after the first cut and its profile, with its own A/B.

## 4. The lowering

### 4.1 Where the code goes

FEX's dispatcher calls a new IR op `FexcRepMovs` (width) before the one-element pass when `Repeat && Kind == MOVS &&
Is64BitMode && !A32 && no segment prefix && fexc_bulk` (beside OpcodeDispatcher.cpp:3335), with the op's entry in FEX's
`IR.json`; the JIT form is in natemu's shim, [fexc_sites.inc](../../tools/fexc/shim/fexc_sites.inc), next to
`FexcRepStos`. The pass after it keeps chunk 26's ending: RCX = 0 ends the instruction, else one element, then the
instruction's end by a branch if RCX is now 0 (an exit to its own RIP with RCX = 0 is never linked: chunk 26's linker
lesson, [perf plan](perf-plan.md):3071-3075), else the exit to its own RIP.

The FEX fork is natemu's own, and agents change it under the same review and test rules ([AGENTS.md](../../AGENTS.md),
"The FEX fork"). This is the next fork commit with a real FEX change, so it also replaces the "AI must not be used..."
line in the fork's `CLAUDE.md` with a pointer to that AGENTS.md section (task 2).

### 4.2 Two sites from one op

The loop has two memory words, the `ldp` and the `stp`, and the recorder records one per op (fexc_sites.inc:1338-1356). The
recorder gains a case for `OP_FEXCREPMOVS`: the scan finds the `stp` (the op's last memory word), recorded as a store-pair
site of 16 bytes; the word before it is the `ldp`, recorded as a second site, `FEXC_SITE_LOAD_PAIR`, 16 bytes, read intent,
base RSI's register. (Two IR ops would split the loop's body across ops, and its back edge with it.) The op is also added
to `fexc_movs` (so the load is never paired into a read-modify-write, fexc_sites.inc:1360-1370) and to the committed-effects
exemption beside `OP_FEXCREPSTOS` (fexc_sites.inc:1375-1376): its stores leave the registers at an iteration boundary.

### 4.3 `FexcRepMovs` words

RCX x7, RDI x11 are the static registers chunk 26's admission requires (structure.rs:578-579); RSI's is read from the same
map (task 1). S is the sequence's end. No word sets flags (REP MOVS changes none, and FEX keeps NZCV live across it).
The order puts the cheapest test that most often fails first, because a declined copy pays the words before its exit on
every element (section 2):

```
  lsr x1, x7, #(4 - log2 w); cbz x1, S              under one round left: 2 words (memcpy tails, short copies, a string's last element)
  [w = 8: orr x0, xSI, x11; and x0, x0, #7; cbnz x0, S]    both 8-aligned: 3 more
  sub x0, x11, xSI; sub x0, x0, #1; and x0, x0, #0xff0; cbz x0, S     RDI - RSI in [1, 16] mod 4096: 4 more (5.1)
  ldrb w0, [x28, #fast_strings]; cbz w0, S          IA32_MISC_ENABLE bit 0 (chunk 26 review, item 1)
  ldrb w0, [x28, #df];           tbnz w0, #7, S     DF = 1
  and x0, x11, #0xfff; movz w2, #0x1000; sub x0, x2, x0; lsr x0, x0, #4      rounds before RDI's page end
  sub x2, x1, x0; and x2, x2, x2, asr #63; add x1, x0, x2                    x1 = min(x1, x0)
  and x0, xSI, #0xfff; movz w2, #0x1000; sub x0, x2, x0; lsr x0, x0, #4      rounds before RSI's page end
  sub x2, x1, x0; and x2, x2, x2, asr #63; add x1, x0, x2                    x1 = min(x1, x0)
  cbz x1, S
L: ldp x2, x3, [xSI]          load-pair site, 16 bytes, read intent
  stp x2, x3, [x11]           store-pair site, 16 bytes
  add xSI, xSI, #16; add x11, x11, #16; sub x7, x7, #16/w; sub x1, x1, #1; cbnz x1, L
S:
```

About 30 words of setup and 8 a round. What a declined copy pays per element, on top of today's pass of about 7 executed
words: 2 words when under a round remains (every short copy, and the one-element pass after a bulk part); for MOVSQ with a
misaligned pointer 5; with the overlap skip 6 (MOVSB) or 9 (MOVSQ); with DF = 1 or fast strings off about 13; with a page
end within a round of either pointer, up to the whole setup for the few elements before that page end. The pointer tests
are invariant along a copy (RSI and RDI step together), so a copy declined by them is declined at every element: that is
the regression the kill-test's control rows bound (section 8). Each min is chunk 26's branch-free form, valid because both
inputs are below 2^63. The scratch registers (x0-x3 here; the real choice is FEX's TMP set, task 1) are in the captured
frame at any fault or shadow miss, so a retried access loses nothing.

### 4.4 32-bit code (only after task 0, section 3.2)

The round is `ldp wA, wB, [xSI]; stp wA, wB, [xDI]`, 8 bytes, with `add wSI, wSI, #8; add wDI, wDI, #8; sub w7, w7, #2`:
32-bit writes zero-extend, so ESI/EDI/ECX wrap at 4 GiB as the element pass makes them (OpcodeDispatcher.cpp:3324-3327) and
x86 does. The addresses are ESI and EDI themselves, which holds only while DS and ES are flat (base 0): today's precondition
for all 32-bit code ([execution.rs](../../crates/host/src/execution.rs):4650-4652, [M2 design](m2-32bit-design.md):109-110,
171-172). Admission enforces it, not prose: the 32-bit loop is admitted only in an object whose request's code identity says
flat DS and ES, and only with W-register updates (so the upper halves stay zero); M2 task 6 (non-flat segments, deferred,
m2-32bit-design.md:178) inherits that refusal.

## 5. Why it is exact

The SDM's REP MOVS is element by element: element i reads DS:[RSI], writes ES:[RDI], RSI/RDI step by w and DF, RCX counts
down; a fault, trap or interrupt between iterations leaves RCX/RSI/RDI at that boundary, RIP at the REP, RF set (Vol.2 MOVS,
REP; Vol.3 RF). Grouping iterations is the fast-string operation, allowed only with IA32_MISC_ENABLE bit 0 and WB or WC
memory (Vol.1 7.3.9.3; Vol.3 11.2.4.1). Chunk 26's arguments carry over where noted; what is new is two pointers and their
overlap.

### 5.1 Overlap

Element i reads src + i after the stores of every element j < i. A round reads 16 source bytes, then stores 16. It misses
exactly the stores of earlier elements in the same round, which land where a later element in the round reads only if
dst + j = src + i with j < i, so dst - src = i - j in [1, 15]. Stores of earlier rounds were all made before the round's
load. When RDI <= RSI a store of element j can only hit the source of a later element, which the round has already read,
as x86 has. So rounds give the element-by-element result exactly when RDI - RSI is not in [1, 15].

The check is on physical bytes, not only the linear distance: two linear pages may map one frame. A page's offset is the
same in every mapping, so physical dst - src within one frame is (RDI - RSI) mod 4096. The skip `((RDI - RSI) - 1) & 0xff0
== 0` (RDI - RSI in [1, 16] mod 4096) is conservative by one and covers every aliasing; large pages keep the offset mod
4096 too. Memcpy and page copies never meet it; `memmove`'s forward case with dst just above src does and runs element by
element, the replicated-byte semantics such code relies on.

### 5.2 Faults

- Each round's source and destination lie inside one page each (the two room counts), so a page fault (not present, U/S,
  read-only, reserved bit) or a canonical fault is the same for every element of the round as for its first. RCX/RSI/RDI are
  updated after the store, so at either access they are the boundary before the round's first element, and every element
  before it is complete.
- The order is the SDM's: an element reads, then writes. The round's `ldp` comes before its `stp`, so a round whose source
  and destination both fault reports the source (a read, CR2 = RSI). If only the destination faults: #PF with W, CR2 = RDI,
  after a read of the round's source with no effect (WB RAM, a readable page). Nothing of the round is written (the page's
  translation refuses the `stp`: chunk 26, [perf plan](perf-plan.md):2994-2996).
- The `ldp` is recorded as a read (4.2); a test pins that a read-only source does not fault (`movs-src-readonly`).
- A non-canonical RSI or RDI, or one in the frame alias or the former private window, faults at the round's first access,
  at its page, as the element's own access would: the bulk accesses use the same addresses, page-granular, so every
  page-level protection applies equally. A string that walks off the last canonical page faults at the first non-canonical
  element after a round ends at the page end (as chunk 26's `into-noncanonical`).
- A shadow miss retries the access; the loop's registers are in the captured frame.

### 5.3 Interrupts, stops, traps

The loop runs at most 256 rounds (x1, written by nothing else, is at most the rooms) and has no exit, so events arm at the
next exit and are taken at iteration boundaries only, later by at most one page's elements (chunk 26,
[perf plan](perf-plan.md):2999-3001). TF: steps never get `--bulk` (execution.rs:4659), so each iteration traps. Data
breakpoints (read or write) and alignment checking: `--bulk` is off while `alignment_gated`, part of the code context, so
such blocks never run the loop; a breakpoint on the source or destination traps at its element as today.

### 5.4 Memory types and devices

The fast-string byte is tested before any access, so with bit 0 clear every iteration is a boundary. WB and WC run
natively. A store to UC, WT or WP RAM is never made through a block's shadow (chunk 26 review, item 1): the instruction runs
alone, one element per step. The source side is new: what a block's load from a UC, WT, WP or device page does, and what a
device page does with a load-pair site (chunk 26: a device page refuses a pair site "as a REP string's element was",
[perf plan](perf-plan.md):3004-3005). **Task 1 is a stop condition**: if any of those source reads is not already the
element-by-element step (UC, device) or a native read with the element's semantics (WT, WP: shadowed for reads), the plan
stops there and comes back with that finding; no code is written first.

### 5.5 Code writes, ordering, atomicity

- A store into a page holding translations aborts (shadowed read-only); into the block's own source page the instruction is
  stepped from the store's state, as for STOS.
- Ordering: the rounds load and store in element order, under the hardware's TSO mode as every other block access; a second
  vCPU sees the string's stores before any store after the instruction (chunk 26's `tso` case, for MOVS too).
- Atomicity: for MOVSQ both pointers are 8-aligned, so each 8-byte half of the `ldp` and `stp` is a naturally aligned,
  single-copy atomic access holding one element; bytes are atomic anyway.

## 6. Admission

New rules, in object validation as chunk 26's (structure.rs:1124-1165), so a defective compiler cannot get a loop past them:

1. **The back edge** of a `FexcRepMovs` loop is admitted only if the words before and after it are exactly section 4.3's
   sequence for the width of the REP MOVS holding them, in a 64-bit object (`bits32` false, structure.rs:670). The width
   comes from the guest bytes (`x86_rep_movs`, new beside `x86_rep_stos` at structure.rs:541: F3 among the legacy prefixes,
   no F2, no 0x67, no segment prefix, A4 or REX.W A5, the opcode the instruction's last byte).
2. **Its two sites**: the `ldp` is that instruction's recorded load-pair site of 16 bytes with read intent, the `stp` its
   store-pair site of 16 bytes; every skip goes to the sequence's end; no branch from outside enters it.
3. **What follows from the words**: x1 is the min of RCX's rounds and both pages' rooms, at most 256, decremented once a
   round and written by nothing else, so the loop ends; RSI and RDI each advance 16 a round within their rooms, so every
   `ldp` stays inside RSI's page and every `stp` inside RDI's; the `stp` is exempt from the split check on that ground
   (structure.rs:2061-2081 extended to the new range). The overlap and alignment skips are pinned words, so a loop without
   them is refused.
4. **The flag**: an object holding the loop from a request without `--bulk` is refused, as today (admission.rs:292-293).
5. **Stack-check dataflow**: the back edge clears what its loop writes (x0-x3, RCX, RSI, RDI) in `aligned`
   (structure.rs:2141-2145), as the STOS edge does.

Containment is unchanged in kind: bounded, pinned word for word, loads and stores only through RSI and RDI at EL0, like the
element's own accesses.

## 7. Interactions

- **Split checks and page-crossing probes**: the loop's stores need none (each inside its page); the one-element pass after
  it keeps its own split check for a quadword that crosses a page (structure.rs:42-44). No probing stub is in the sequence.
- **Stack checks** (`--push-run`): the op writes neither RSP nor anything its alignment fact rests on; rule 5 keeps the
  dataflow exact.
- **Gating and caches**: `--bulk` is already gating bit 11; fexc reads it now for REP MOVS too, so only answers holding one
  depend on it. fexc's identity changes, so cached answers recompile.
- **Precision passes**: `FexcRepMovs` is a guest-memory op (fexc_precision.h:26-28, 1198); `verify_precision`'s REP
  exemption (the `cbz x7`) is extended to it.
- **The sampler's roles**: `sample_roles.py` counts a bulk STOS loop as FEX body with its guest instruction in the REP share
  ([sample_roles.py](../../tests/bench/sample_roles.py):9-10); the MOVS loop gets the same treatment, so the end profile
  reads comparably.

## 8. Kill-test

A guest micro in natemu, as a framework case, before and after, on the M2 Max:

- **`repmicro`**, a new static program in the bench image ([build.sh](../../tools/bench/image/scripts/build.sh):97-104
  builds `coldtrip` and the `-m32` programs the same way), 64-bit and `-m32` builds. Each row repeats its copy for about 50
  ms, three rounds, and prints the median and range in ns per copy and a checksum of the destination, which the judge
  compares with the program's own `--sums` (as `coldtrip`).
  - Hot rows (64-bit): `rep movsb` at 16, 64, 128, 512, 4,096 and 65,536 bytes; `rep movsq` at 64, 100, 128, 512 and 4,096
    bytes; pointers 64-byte aligned.
  - Short rows (64-bit, the 2-word cost): `rep movsb` at 1, 3, 7 and 15 bytes; `rep movsq` of one quadword.
  - Declined rows (64-bit, the invariant-test cost): `rep movsq` at 4 KiB with the source at +4; `rep movsb` at 4 KiB with
    dst = src + 8 (the overlap skip); `rep movsq` at 4 KiB with the source at page offset 0x808 (rooms differ).
  - 32-bit rows, for section 3.2's decision and as controls (the first cut does not change them): `rep movsd` of 1, 2 and 3
    elements; at 4 KiB with both pointers 8-aligned and with both 4 mod 8.
- **`guest-repmicro`**, a new case in `tests/bench/family.toml` beside `guest-coldtrip-profile`, run through
  `tools/bench/run.py`. It lands with task 0, with no change to natemu.
- **Runs**: three times before (at task 0's commit) and three times after (the end of task 7), alternating as the machine
  queue allows. The hot rows' result is the median across the three runs; a short, declined or 32-bit row is a regression
  if its after-median exceeds the before-maximum by more than the before spread (maximum minus minimum across the three).
- **Kill if** `rep movsb` at 4 KiB is not at least 4x faster, or `rep movsq` at 4 KiB not at least 1.2x faster, or any short,
  declined or 32-bit row regresses by the rule above. Expected from chunk 26's native loops (section 2): 4 KiB `movsb` about
  1,050 to 75-140 ns, `movsq` 166 to the same.

## 9. Tests

Every new check is shown to fail on a mutation of the code (9.4), as chunk 26's negative controls.

### 9.1 Host (`crates/host`, with `FEXC`)

- `bulk_rep_movs_only_when_asked`: the loop with `--bulk` for movsb and movsq in 64-bit code; none for movsw, movsd, 0x67, an
  FS/GS or other segment prefix, F2, 32-bit code, or without the flag; a bulk object refused for a request without it; both
  sites present in the object, the `ldp` as a load-pair site with read intent.
- `bulk_rep_movs_loops_are_pinned`, fixtures `rep-movsq-bulk`, `rep-movsb-bulk`: admitted with and without the code shape;
  refused with: either room count dropped, the overlap skip removed or its mask changed, the alignment test dropped, the
  count test moved after the others, the back edge to the `stp`, the `ldp` off its site or with write intent, RCX stepped
  by another amount, the loop in a 32-bit object, a branch from outside into the loop.

### 9.2 System (`tests/system/run_k2.py repbulk`, the `k2-repbulk` case; natemu only, judged by the SDM, block sizes default and 1)

Chunk 26's STOS cases (run_k2.py:1404-1666) are the model; the new ones judge against an element-by-element Python model of
the SDM's MOVS:

- `movs-fill-wN` (movsb, movsq): source and destination offsets 0, 3, 8, 0xff0, 0xff8, 0x808 in all pairings; counts 0, 1, a
  round less one, a round, one more, 100, two pages and 3; DF = 1 twice; 0x67 once; an FS source once: RCX 0, RSI/RDI the ends,
  exactly the string's bytes.
- `movs-overlap`: dst - src in -17, -16, -15, -1, 0, 1..17, 31, 32, 4095, 4097 at both widths and counts 24, 100, 300: equal to
  the model (the replicated-byte patterns for 1..15).
- `movs-alias`: one frame mapped at two linear pages 0x10000 apart, dst - src = 0x10000 + d for d in 1..17: equal to the model.
- Faults: `movs-src-unmapped` (source page 2 not present: #PF without W, CR2 the source element, RCX/RSI/RDI there, every
  earlier element written), `movs-dst-unmapped`, `movs-dst-readonly`, `movs-both` (both pages fault: the source is reported),
  `movs-src-readonly` (no fault), `movs-first` (nothing written), `movs-cpl3-us`, `movs-noncanonical` (each pointer),
  `movs-into-noncanonical`, `movs-frame-alias` (#GP(0), nothing changed).
- `movs-kicks-w1/w8` and `movs-kicks-w1/w8-misc-off`: random interrupts over 64 KiB copies. At each one taken at the REP,
  RCX + (RDI - DST)/w is the count and RSI - SRC = RDI - DST; the element before RDI holds this round's value and the one at
  RDI the last round's; RF is set. With fast strings on, no boundary taken lies in a page's interior, for **both** pointers:
  with the source and destination at different page offsets (0 and 0x808), a taken boundary is within 16 bytes of RSI's
  page end or start, or of RDI's (a pass stores in bulk to the nearer page end, then elements until the nearer pointer
  crosses); with the bit cleared, more than half are interior.
- `movs-tf`, `movs-watch-src` and `movs-watch-dst` (read and write breakpoints), `movs-ac` (CPL 3, AC and CR0.AM, a misaligned
  quadword: #AC at it), `movs-uc-src`, `movs-uc-dst`, `movs-wt-wp` (source and destination), `movs-smc-other`, `movs-smc-own`,
  `movs-linked` (`rep_movs_alternative`'s shape, `rep movsq` then a byte tail, called N and 2N times: linker entries do not
  grow with N), `movs-tso`.
- Devices (`m2` case, `types`/`devices` groups): a REP MOVSB and MOVSQ from and to a device BAR, the device's accesses
  counted and ordered, equal with and without `--bulk`.

### 9.3 Against QEMU (answers the framework locks)

- The I2 string corpus ([gen_i2.py](../../tools/reference/gen_i2.py)) is not a framework input today (tests/programs/family.toml
  has no `i2`). Add `[input.i2]` and a suite with `reference = "qemu"` at `engine.max_block` 0 and 1, as the `fix` and `i10`
  suites. Extend its overlap programs (gen_i2.py:157-163: deltas -9, -1, 1, 3, 8, 17 at count 24) with deltas -17, -16, -15,
  15, 16, 31, 32 and counts 100 and 300, so bulk and declined copies both run against QEMU's element-by-element answers,
  locked by `natemu-test lock`.
- Faults mid-string: a `loopfaults-i2` case running `tests/faults/run_slice2_faults.py --programs` on gen_i2's `--flow`
  corpus (QEMU's signal context gives RCX/RSI/RDI and memory at the faulting element), at block sizes default and 1.
- The 32-bit real-x86 cases (compat programs with overlap, wrap and mid-string faults added to `run_compat_kvm.py`'s
  `SELECTED`, tests/smp/run_compat_kvm.py:21, locked by the AMD KVM box) come with the 32-bit work, not the first cut.
- **Built (November 24).** `[input.i2]` and the `i2` suite (tests/programs/family.toml, `reference = "qemu"`,
  `engine.max_block` 0 and 1): gen_i2's overlap programs now take deltas -17, -16, -15, -9, -1, 1, 3, 8, 15, 16, 17, 31, 32
  at counts 24, 100 and 300, both directions and all four widths (312 programs; the longer downward ones start at
  DATA + 0x1800, and the longer ones cross the page at DATA + 0x1000), 748 programs in all, 1,496 cases, every one with
  QEMU's answer (`natemu-test lock tests/programs i2`). `loopfaults-i2@1..6` (tests/faults/family.toml): run_slice2_faults.py
  `--programs` on gen_i2's `--flow` corpus (input root `i2-corpus/flow/programs.json`, `testkit.inputs.corpus('gen_i2.py',
  '--flow')`), 6,732 program x mutation cases (each page of the data and stack regions absent or read-only, and none) at
  block sizes default and 1 per shard, 13,470 results; seeded twice (`TESTKIT_SEED_ANSWERS=1`) and locked from both runs
  (16,136 answers, none differing between the seeds). On the M2 Max at a6c85008 plus these changes: `i2` 1,496 passed,
  `loopfaults-i2` 13,470 passed against the lock (1 min 23 s; QEMU faulted in 1,844 of the 6,732 cases); no case disagrees
  with QEMU. Mutation (9.4's first row, a scratch build of the shim and the admission template, reverted): the overlap
  skip removed turns 24 `i2` cases red, `overlap-movs8-{1,3,8,15}-{24,100,300}-up` at both block sizes; MOVSQ stays green
  there, its overlapping copies being declined by the alignment test (sources at DATA + 0x800 plus 0-7 bytes).
- Unchanged families that must stay green: `faults` (slice 1, I7, I10 string forms), `m2`, `compat32`, `k2-repbulk`'s STOS
  cases, the gate.

### 9.4 Mutations (each a scratch build, reverted; each must turn its tests red)

| Mutation | Red |
| --- | --- |
| overlap skip removed | `movs-overlap` d = 1..15; i2 `overlap-movs8-1/3/8` against QEMU |
| overlap on the linear distance only (no mod 4096) | `movs-alias` |
| RSI/RDI/RCX stepped before the store | the fault cases (registers a round ahead) |
| the destination's room dropped | `movs-dst-unmapped`, `movs-fill` at dst 0xff8 (a store straddling the page) |
| the source's room dropped | `movs-src-unmapped`, `movs-kicks` (an interior boundary on RSI's page) |
| `stp` before `ldp` | `movs-both` (the destination reported), `movs-overlap` d < 0 |
| fast-string test removed | `movs-kicks-*-misc-off` |
| bulk in steps too | `movs-tf`, `movs-uc-*` |
| the `ldp` with write intent | `movs-src-readonly` |
| DF test removed | `movs-fill` DF = 1 |

## 10. Order of work and estimate (agent hours, the first cut)

| Task | Hours |
| --- | ---: |
| 0. Measure before building. (a) `sample_report.py`: the REP share by form (prefix, opcode, width, mode, privilege), with a unit test. (b) The census plugin ([indirect.c](../../tests/bench/plugins/indirect.c), which already reads registers: indirect.c:176) counts each REP MOVS/STOS instance (its first iteration) by form, privilege, RCX bucket (0, under a round, 1-16 rounds, more), RSI and RDI mod 8, and RDI - RSI mod 4096 in [1, 16]; `census_report.py` reduces it; run in the `guest-{startup32,zstd,hackbench,build}-census` cases (QEMU, 1 vCPU). (c) The `repmicro` program and `guest-repmicro` case; its three "before" runs. Decides 3.2 and whether the first cut is worth building: if under half of the 64-bit MOVSB/MOVSQ bytes are eligible, stop and report | 5 |
| 1. Check the source-side paths (UC, WT, WP and device pages as a block's load source, the load-pair site's handling), FEX's static register for RSI and the free TMP registers. A stop condition (5.4) | 2 |
| 2. FEX fork: the IR op and the dispatcher's condition; replace the fork `CLAUDE.md`'s no-AI line with a pointer to AGENTS.md "The FEX fork" | 1 |
| 3. Shim: `FexcRepMovs`, the recorder's two sites (4.2), `fexc_movs`, the committed-effects exemption (fexc_sites.inc:1375-1376), the precision barrier, `verify_precision` | 4 |
| 4. Admission: `x86_rep_movs`, the two templates, two sites, split exemption, dataflow; fixtures and the two host tests | 4 |
| 5. System cases (9.2) | 4.5 |
| 6. QEMU cases (9.3): `i2` input and suite, gen_i2's new deltas, `loopfaults-i2` | 2 |
| 7. Mutations (9.4) | 2 |
| 8. Independent review of design and code, fixes | 2.5 |
| 9. The three "after" kill-test runs; this plan updated with the result | 1 |
| **Total** | **28** (24-34) |

The 32-bit `rep movsd` and REP STOS work (3.2), if task 0 asks for it, is a separate estimate then (about 10-12 h: the W-pair
template, the identity-tied admission rule, 32-bit cases, the KVM lock). The gate, the combined A/B and the profile round
are the pooled end of the owner's list, not counted here.

## 11. Expected gain (first cut)

Share x (1 - 1/speedup), with chunk 26's 4 KiB speedups (`movsq` 1.2-2.2x, `movsb` 7.5-14x), applied to the sampled shares
(section 1) as if every copy were eligible: upper bounds, which task 0's eligibility rate scales down.

| Case | Saved, % of EL0 samples | As % of the vCPU's time (EL0's share of samples) |
| --- | ---: | ---: |
| `guest-zstd` run1 | 4.8-5.4 | 4.4-4.9 (91.3%) |
| `io5-solo32` manifest, fetch-python | 0.6-1.9 | |
| `guest-build` | 0.3-1.0 | 0.15-0.5 (47.7%) |
| `guest-hackbench` | 0.5-1.5 | not read (its EL0 table is a minority of its EL0 time: findings section 1) |
| `guest-startup32`, i386-make | 0 (32-bit: section 3.2 would add up to 2.4-3.2 and 1.6-2.1) | |

## 12. Main correctness risks

1. **Overlap**: a round that reads before an earlier element's store in the same round lands. The skip on (RDI - RSI) mod
   4096 in [1, 16] is the whole defence, including physical aliasing; the SDM model and QEMU test it.
2. **Fault order and state**: two accesses per round. The source must be reported before the destination, as a read, with
   the registers at the round's start. Pinned by the fault cases, the recorder's two sites and the `ldp`/`stp` order rule.
3. **The source side of memory types**: UC, WT, WP and device pages as a load source were never a bulk path before; task 1
   checks them and stops the work if they are not already exact.
4. **Declined copies get slower**: the skip words are paid per element. The ordering in 4.3 and the kill-test's short and
   declined rows bound it.
5. **Element atomicity** across vCPUs: the 8-alignment rule for MOVSQ; relaxing it needs its own proof.

## 13. The first cut, built (November 24)

At b0be221c (task 0) and a23206f2 (tasks 1-7; FEX fork 39eb0db on 13a1bfc). Task 8 (the independent review) is not
done; see the end of this section. The QEMU cases of 9.3 were built afterwards (9.3, "Built").

**Task 0: eligibility** (census cases, QEMU 11.1.2 with the plugin, the M2 Max; bytes are each instance's first RCX times
the width; "moved in bulk" simulates every pass of 4.3 from the first iteration's registers, with the plan's [1, 16]
overlap rule):

| Case | 64-bit MOVSB + MOVSQ, MiB | eligible | moved in bulk | 32-bit MOVSD, MiB | its eligible (3.2's rule) |
| --- | ---: | ---: | ---: | ---: | ---: |
| `guest-zstd-census` | 432.8 | 77.1% | 77.0% | 0 | |
| `guest-hackbench-census` | 1,376.5 | 49.2% | 44.7% | 0 | |
| `guest-build-census` | 993.4 | 99.1% | 97.4% | 0 | |
| `guest-startup32-census` | 8.1 | 98.7% | 94.9% | 1,628.3 | 32.2% |
| pooled | 2,810.8 | 71.3% | 68.4% | | |

Go (over half). Where it falls short: hackbench's kernel `rep movsq` (`rep_movs_alternative` on 100-byte messages) has
only 49% of its bytes with both pointers 8-aligned; zstd's kernel `rep movsq` has 22% of its bytes at RDI - RSI in
[1, 16] mod 4096 (page-cache pages copied to a buffer 16 bytes into its page, as glibc's mmap'd chunks are), which the
built rule's exact [1, 15] no longer declines. 32-bit MOVSD does not qualify for 3.2 (32% eligible: 18% 8-aligned, 25% with
DF set, 22% overlapping): not built. The sample report's form split, over the kept outputs of runs 20261123-213409-4d92
and 20261123-213500-1086 (an experiment, not a case: the report's code over files read in place) gives section 1's
numbers again, e.g. `guest-zstd-sample` run1: `rep movsb 64-bit user` 5.27, `rep movsq 64-bit kernel` 0.91 % of EL0.

**Task 1: no stop.** A UC source page is never shadowed for a block but the step block's ([`translate`](../../crates/runtime/src/execution.rs)):
the `ldp` aborts and the instruction is stepped, one element (steps never get `--bulk`). WT and WP pages are shadowed for
reads: the `ldp` is a native read, as the element's `ldr` is. A device page: the pair site is refused (`mmio::device`,
`status::SITE`) before any device effect, with the state at the instruction, as the element's load is (no completion
record, `FORM`): the same stop. A 16-byte X-register load pair was already an admitted site kind (I5a). RSI is x10, RCX x7,
RDI x11; x0-x3 (FEX's TMP1-4) are free inside an op.

**What departs from the design.**
- The overlap skip is exact: `sub x0, x11, x10; sub x0, x0, #1; and x0, x0, #0xfff; sub x0, x0, #15; tbnz x0, #63, S`
  declines RDI - RSI in [1, 15] mod 4096 (section 5.1's exact condition), one word more than the [1, 16] mask, so the
  zstd copies above are bulk.
- Segment prefixes: FEX's decoder records none of ES/CS/SS/DS in 64-bit mode (they do nothing there), so only FS and GS
  decline; admission's `x86_rep_movs` refuses 64 and 65 and lets the others be.
- Stack-check dataflow (rule 5) needed no change: the back edge already clears every register its loop writes.

**Tests** (all green at a23206f2): host `bulk_rep_movs_only_when_asked`, `bulk_rep_movs_loops_are_pinned` (fixtures
`rep-movsq-bulk`, `rep-movsb-bulk`); `k2-repmovs` (tests/system/run_k2.py `repmovs`, 44 cases at block sizes default and 1:
fill at eight offset pairings and seven counts, DF, 0x67, FS; overlap at 26 distances and three counts; one frame at two
pages; source/destination unmapped, read-only, both, crossing, first; kicks with fast strings on and off; TF; UC and WT
sources; linked); `k2-repbulk` unchanged; `census-check` with ten REP instances of known counts.

**Mutations** (each a scratch build of the shim and the admission template together, `k2-repmovs` run, reverted):

| Mutation | Red |
| --- | --- |
| overlap skip removed | `movs-overlap-w1/w8`, `movs-alias-w1/w8` |
| overlap on the linear distance (no `and #0xfff`) | `movs-alias-w1/w8` |
| RCX stepped before the store | `movs-dst-unmapped`, `movs-first`, `movs-uc-src` |
| the destination's room dropped | `movs-dst-unmapped` |
| the source's room dropped | none, and wrongly called equivalent here (review B2, section 14): `movs-src-cross-w1/w8` had destinations their overlap skip declined, so no round ran; a round's `ldp` crossing into a missing page delivers #PF from the round's start (the runtime walks both pages; only UC pages are stepped). Their destinations moved, they are red under this mutation (section 14) |
| fast-string test removed | `movs-kicks-w1/w8-misc-off` |
| DF test removed | `movs-fill-w1/w8` |
| the `ldp` with write intent | `movs-first` (#PF with W) |
| bulk in steps too | `movs-tf`, `movs-uc-src` |

Not run: `stp` before `ldp` (the recorder would describe the wrong word; admission refuses it).

**Kill-test: killed.** Bench case `guest-repmicro`, M2 Max, three runs before (natemu b0be221c, `TESTKIT_BENCH_AT` for two)
and three after (a23206f2), ns per copy (run 2's median of three rounds; a row's figure the median over the runs, with
their range; section 8's rule, `tests/bench/repmicro_report.py compare`):

| row | before | after | speedup | verdict |
| --- | ---: | ---: | ---: | --- |
| movsb-16 | 8.8 (8.8-8.8) | 4.2 (4.1-4.3) | 2.12x | hot |
| movsb-64 | 23.5 (23.2-23.9) | 5.6 (5.5-5.6) | 4.22x | hot |
| movsb-128 | 51.5 (50.8-51.8) | 6.8 (6.7-6.9) | 7.58x | hot |
| movsb-512 | 176.5 (174.4-177.5) | 14.5 (14.2-14.5) | 12.20x | hot |
| movsb-4096 | 1494.2 (1470.7-1497.1) | 98.2 (96.7-99.6) | 15.21x | ok |
| movsb-65536 | 23825.1 (23681.0-24076.3) | 1740.1 (1709.0-1750.7) | 13.69x | hot |
| movsq-64 | 6.5 (6.4-6.5) | 5.3 (5.1-5.3) | 1.23x | hot |
| movsq-100 | 10.4 (10.4-10.8) | 9.3 (9.1-9.4) | 1.12x | hot |
| movsq-128 | 10.7 (10.6-10.9) | 6.5 (6.4-6.5) | 1.64x | hot |
| movsq-512 | 47.3 (45.8-48.7) | 14.0 (13.8-14.1) | 3.38x | hot |
| movsq-4096 | 402.0 (399.5-424.4) | 96.0 (94.4-97.7) | 4.19x | ok |
| movsb-1 | 3.7 (3.6-3.8) | 3.7 (3.6-3.8) | 0.99x | ok |
| movsb-3 | 5.1 (5.0-5.3) | 5.8 (5.6-5.9) | 0.87x | regressed: 5.84 > 5.56 |
| movsb-7 | 6.3 (6.2-6.3) | 8.0 (7.8-8.2) | 0.78x | regressed: 7.99 > 6.46 |
| movsb-15 | 8.5 (8.5-8.9) | 13.1 (12.7-13.1) | 0.65x | regressed: 13.07 > 9.38 |
| movsq-8 | 3.6 (3.6-3.8) | 3.5 (3.3-3.5) | 1.05x | ok |
| movsq-4096-src4 | 409.4 (400.8-416.0) | 484.0 (475.9-490.8) | 0.85x | regressed: 484.03 > 431.19 |
| movsb-4096-overlap8 | 1503.7 (1498.2-1518.8) | 2829.4 (2810.6-2859.9) | 0.53x | regressed: 2829.36 > 1539.46 |
| movsq-4096-src808 | 401.8 (399.0-410.1) | 111.7 (103.9-113.6) | 3.60x | ok |
| movsd-4 | 5.8 (5.4-5.9) | 5.7 (5.6-5.8) | 1.01x | ok |
| movsd-8 | 6.7 (6.4-6.8) | 6.7 (6.5-6.7) | 1.00x | ok |
| movsd-12 | 7.3 (7.0-7.5) | 7.3 (7.1-7.4) | 1.00x | ok |
| movsd-4096 | 890.3 (881.4-925.0) | 917.9 (889.3-919.0) | 0.97x | ok |
| movsd-4096-off4 | 896.2 (880.0-925.4) | 919.7 (888.7-924.9) | 0.97x | ok |

The hot rows pass by far (4 KiB `movsb` 15.2x, `movsq` 4.2x; 64 KiB `movsb` 13.7x), but five rows regress beyond the
before spread, as risk 4 feared: the short `movsb` rows (3, 7, 15 bytes) pay the count test's two words on every
element (about 0.3 ns each), and the declined rows pay the words before their skip on every element (`movsq` with a
source 4 mod 8: five, +18%; `movsb` with RDI = RSI + 8: seven, +88%). The 32-bit rows are unchanged. What would
remove them is not in this design: a bounded element loop inside the page for what the rounds decline (tails,
misaligned and overlapping copies), so declined copies stop re-entering the setup per element. Owner decision:
drop the first cut, keep it with the rule relaxed for these rows, or add that loop.

**Not done** (first cut): 9.3's QEMU corpus (built since: 9.3); 9.2's
`movs-cpl3-us`, `movs-noncanonical`, `movs-into-noncanonical`, `movs-frame-alias`, `movs-watch-src/dst`, `movs-ac`,
`movs-wt-wp` as a destination, `movs-smc-*`, `movs-tso` and the M2 device cases; the independent review (task 8).
`fexc-replay` fails by design (64-bit output changes for every REP MOVSB/MOVSQ compiled with `--bulk`).

## 14. Second cut: eligibility once per instruction (November 24, owner)

**What.** The first cut paid its tests on every element of a copy it declined (and of every short copy), because each
element re-entered the REP and its bulk setup. `FexcRepMovs` now decides once per pass and moves every element it can prove
safe inside the block, so a declined or short copy re-enters only at a page crossing:

```
  lsr x0, x7, #1; cbz x0, S                                     RCX < 2: today's pass alone
  tbnz x7, #63, S                                               RCX >= 2^63: today's pass (the mins are signed; review B1)
  ldrb w0, [x28, #fast_strings]; cbz w0, S; ldrb w0, [x28, #df]; tbnz w0, #7, S
  x1 = min(RCX, the whole elements before RDI's page end, before RSI's)   (2 x 7 words, branch-free as before)
  cbz x1, S                                                     an element that crosses a page: today's pass
  lsr x0, x1, #(4 - log2 w); cbz x0, E                          under one round: elements only
  [w = 8: orr x2, x10, x11; and x2, x2, #7; cbnz x2, E]         rounds only with both pointers 8-aligned
  sub x2, x11, x10; sub x2, x2, #1; and x2, x2, #0xfff; sub x2, x2, #15; tbnz x2, #63, E    and no overlap in [1, 15]
  sub x1, x1, x0, lsl #(4 - log2 w)                             the elements left after the rounds
B: ldp x2, x3, [x10]; stp x2, x3, [x11]; add x10, x10, #16; add x11, x11, #16; sub x7, x7, #16/w; sub x0, x0, #1; cbnz x0, B
E: cbz x1, S
L: ldr(b) x2, [x10]; str(b) x2, [x11]; add x10, x10, #w; add x11, x11, #w; sub x7, x7, #1; sub x1, x1, #1; cbnz x1, L
S: (today's pass: RCX = 0 ends the instruction; else one element, which crosses a page, then the exit to its own RIP)
```

**Why it is exact.** The element loop is today's element, repeated: it loads RSI's element and stores RDI's, in that order,
and steps RCX, RSI and RDI after the store, so at each access the registers are an iteration boundary's and every earlier
element is complete; overlap of any distance is the SDM's (each element reads after the previous one's store). Every element
lies wholly inside the current page of both pointers (x1 counts whole elements before each page end), so no access crosses
a page: a fault is the element's own, with the element's CR2, error code and state; a misaligned MOVSQ element is one
unaligned `ldr`/`str` inside a page, as today's element pass makes it. The rounds are section 5's, now taken only from the
elements already proven inside both pages. Interrupts and stops are taken at the next exit, at most one page's elements
later (x1 <= 4096 / w since RCX < 2^63 there, written by nothing but the loops), the bound chunk 26's loop has; the fast-string bit and DF are
tested first, so with bit 0 clear or DF = 1 every element is still a boundary of its own. What the loops cannot prove stays
on today's path: an element that crosses a page (`cbz x1`), RCX < 2, FS/GS, 0x67, F2, MOVSW/MOVSD, 32-bit code; UC and
device pages (the first access aborts and the instruction is stepped, or refused, as today); steps, TF, data breakpoints
and alignment checking (no `--bulk`). Admission pins the whole sequence for the width, both backward branches (to the
`ldp` and to the element `ldr`), the two forward targets (E and S) and all four sites (load pair, store pair, element load
and store, read and write intent): `bulk_movs_loop`.

**Built** at 84ddcdef, fexc-replay's bound after it (FEX fork 7e03132 on 39eb0db: the op's description, and `fexc_no_bulk_movs`). The recorder records
the op's four accesses as sites; admission's `bulk_movs_loop` pins the sequence per width (both back edges, the forward
targets E and S, all four sites).

**Kill-test: kept.** Bench case `guest-repmicro`, M2 Max, three runs a side alternating (before: 18483e79's code through
`TESTKIT_BENCH_AT`, no bulk at all; after: 84ddcdef), ns per copy, section 8's rule:

| row | before | after | speedup | verdict |
| --- | ---: | ---: | ---: | --- |
| movsb-16 | 8.9 (8.6-9.0) | 4.6 (4.6-4.6) | 1.92x | hot |
| movsb-64 | 23.8 (22.8-29.6) | 5.8 (5.8-5.8) | 4.10x | hot |
| movsb-128 | 51.6 (50.2-52.1) | 7.1 (7.0-7.1) | 7.28x | hot |
| movsb-512 | 179.3 (177.4-182.6) | 14.6 (14.4-14.7) | 12.29x | hot |
| movsb-4096 | 1499.6 (1452.0-1504.1) | 99.0 (98.6-99.3) | 15.15x | ok |
| movsb-65536 | 23988.6 (23402.5-24207.1) | 1611.2 (1607.5-1629.4) | 14.89x | hot |
| movsq-64 | 6.5 (6.4-6.5) | 5.8 (5.8-5.8) | 1.12x | hot |
| movsq-100 | 10.7 (10.6-11.0) | 8.9 (8.9-8.9) | 1.20x | hot |
| movsq-128 | 10.9 (10.4-10.9) | 7.2 (7.1-7.4) | 1.51x | hot |
| movsq-512 | 47.9 (45.1-49.0) | 14.8 (14.7-15.1) | 3.25x | hot |
| movsq-4096 | 421.9 (390.6-424.9) | 98.6 (97.6-102.1) | 4.28x | ok |
| movsb-1 | 3.8 (3.6-3.9) | 3.8 (3.7-3.8) | 1.00x | ok |
| movsb-3 | 5.3 (5.1-5.3) | 5.3 (5.3-5.4) | 0.99x | ok |
| movsb-7 | 6.6 (6.2-6.6) | 6.5 (6.5-6.7) | 1.01x | ok |
| movsb-15 | 8.9 (8.3-9.0) | 9.1 (9.1-9.2) | 0.98x | ok |
| movsq-8 | 3.8 (3.5-3.8) | 3.4 (3.4-3.5) | 1.11x | ok |
| movsq-4096-src4 | 421.0 (391.2-421.5) | 183.0 (181.1-183.1) | 2.30x | ok |
| movsb-4096-overlap8 | 1556.3 (1464.0-1575.2) | 1335.3 (1318.6-1373.1) | 1.17x | ok |
| movsq-4096-src808 | 414.8 (392.2-422.3) | 104.9 (104.3-106.3) | 3.95x | ok |
| movsd-4 | 5.7 (5.6-6.1) | 6.0 (5.6-6.2) | 0.95x | ok |
| movsd-8 | 6.6 (6.5-7.0) | 6.9 (6.6-7.5) | 0.95x | ok |
| movsd-12 | 7.3 (7.2-7.5) | 7.6 (7.2-9.0) | 0.96x | ok |
| movsd-4096 | 911.1 (870.2-926.4) | 914.1 (909.0-916.6) | 1.00x | ok |
| movsd-4096-off4 | 911.9 (866.9-922.1) | 907.5 (904.3-915.4) | 1.00x | ok |

Every control row is within the rule; the declined rows now gain (MOVSQ from a source 4 mod 8 2.3x, MOVSB with RDI = RSI
+ 8 1.17x), the short ones are unchanged.

**Tests** (84ddcdef): `k2-repmovs` 78 cases, adding to section 13's: faults inside the element loop and of a misaligned
element crossing a page, a declined MOVSQ under interrupts at random times (`movs-kicks-w8-src4`: no boundary inside
both pages' interiors), `movs-cpl3-us-src/dst`, `movs-noncanonical-src/dst`, `movs-into-noncanonical`,
`movs-watch-src/dst`, `movs-ac`, `movs-smc-other/own`, `movs-tso`, `movs-unassigned-src/dst` (an unassigned page as the
source reads all ones; as the destination it is refused, `unsupported_mmio`, before any effect). Host tests and fixtures
updated. fexc-replay: `replay.py`'s `only_bulk_movs` bounds the expected difference (NEW's reply holds FexcRepMovs and NEW's
reply with `--no-bulk-movs` is OLD's, or differs from it only as `only_probes` allows; `only_bulk_read`: a reply that is
OLD's byte for byte but for args_read's bulk bit, which NEW reads for every REP MOVSB/MOVSQ it decodes); a request neither
explains still counts as a difference. At 84ddcdef: 54,998 requests, 0 differences, 138 explained.

**Mutations** (scratch builds of the shim and the admission template together, `k2-repmovs`, reverted): overlap skip
removed (overlap, alias red); overlap on the linear distance (alias); RCX stepped before the store in both loops
(`movs-loop-dst-readonly`, `movs-noncanonical-dst`, `movs-smc-own`, `movs-unassigned-dst`); RDI's room dropped
(`movs-dst-unmapped`); fast-string test removed (both `misc-off` kicks); DF test removed (fill); the `ldp` with write
intent (`movs-first`, `movs-unassigned-src`); bulk in steps (`movs-tf`, `movs-uc-src`); `stp` before `ldp` (39 cases);
the element loop's store before its load (37 cases). RSI's room dropped stayed green there; that was a test gap, not an
equivalence (review B2: see the review fixes below).

**Not done in that round:** 9.3's QEMU corpus, built since (9.3, "Built").

**Review fixes** (independent review of 84ddcdef; FEX fork 934032e on 7e03132: the dispatcher's comment only).
- B1: the branch-free min is signed, valid only below 2^63, and the second cut had dropped the first cut's shift of RCX:
  with RCX = 2^64 - 1 or 2^63 + 4096, x1 stayed RCX through both mins (unbounded loops, rounds and elements across page
  ends without a split check). FexcRepMovs now declines RCX >= 2^63 (`tbnz x7, #63, S`, pinned; the old sequence is
  refused); `movs-rcx-ones` (MOVSB, RCX = 2^64 - 1) and `movs-rcx-2e63` (MOVSQ, 2^63 + 4096) at misaligned offsets with the
  destination's next page missing pin it. The mutation removing the test turns `movs-rcx-ones` red (rounds straddle the
  page); `movs-rcx-2e63` stays green under it (its misaligned MOVSQ runs in the element loop, whose crossing store faults
  from that element's own boundary), but the loop's bound is gone there too, which admission's pinned word now refuses.
- B2: RSI's room is not equivalent: a crossing load in RAM is completed or faulted from the site's state (the runtime walks
  both pages), so with RSI's room dropped a round's `ldp` into a missing page delivers #PF with RSI/RCX at the round's start.
  The source-crossing cases' destinations now sit at 0x100 into their page (RDI - RSI outside [1, 15] mod 4096), so rounds
  would run but for RSI's room; that mutation turns all four (`movs-src-cross-w1/w8`, both block sizes) red.
- Final checks at the review commit: `k2-repmovs` (87 cases), `k2-repbulk`, unit (Rust and Python), `fexc-replay`
  (55,186 requests, 0 differences, 137 explained) and `boot-smoke` pass.
- Minor: the TSO cases (`tso`, `movs-tso`) run the writer until the observer has scanned 20 times (no fixed round count to
  race at block size 1); `movs-uc-dst`, `movs-wt-wp-dst` and `movs-src-into-noncanonical` added.

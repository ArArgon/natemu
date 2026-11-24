# Bulk REP MOVS inside a page (and REP STOS in 32-bit code): plan

November 23, 2026, on `m2-sampler` at d3f2fe4d. Design only: nothing here is built or measured. It is the "REP string
bodies >= 3%" row of the [EL0 sampler findings](el0-sampler-findings.md) (section 7, verdict **build**), one of the fixes
the owner pooled: no A/B of its own, one combined A/B and profile round at the end, with the gate. It keeps its own
kill-test (section 8) and correctness tests (section 9). The model is chunk 26's bulk REP STOS
([perf plan](perf-plan.md):2928-3141), whose design, proofs and tests this plan reuses wherever the argument is the same.

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
| | `rep stosq`, kernel | 0.17 | `clear_page_rep` |
| `io5-solo32-sample` i386-configure | `rep stosd`, 32-bit, user / `rep movsd` | 0.65 / 0.30 | |
| `io5-solo32-sample` manifest, fetch-python | `rep movsq`, kernel | 3.47, 3.29 | `rep_movs_alternative` |
| `guest-build-sample` run1 | `rep movsq`, 64-bit, user | 1.68 | blocks too short to name (gcc's inline struct copies, by the review's cc1 counts) |
| | `rep stosq`, kernel / user | 0.89 / 0.27 | |
| `io5-solo32-sample` make | all REP | 1.04 | `clear_page_rep` 0.36, user `rep stosq` 0.27, user `rep movsq` 0.21 |
| `guest-{luajit,sqlite,scimark32-sse2}-sample` | all REP | 0.0-0.8 | `clear_page_rep` |

Over every case's whole run, the only forms sampled are F3 MOVS and STOS. No CMPS, SCAS or LODS sample, no F2 prefix and
no 0x67 form appears in any case (all phases, all privileges). So:

- **The hot forms are REP MOVS, forward**: `rep movsd` in 32-bit user code (startup32, i386-make), `rep movsb` in 64-bit
  user code (zstd), `rep movsq` in the kernel's `rep_movs_alternative` (hackbench, io5's file copies) and in 64-bit user
  code (build). The 32-bit form is the hottest single one.
- **REP STOS in 32-bit code** is the second piece: `rep stosd` is 0.9% of i386-make and 0.65% of i386-configure, and chunk
  26's bulk path excludes it (`Is64BitMode && !A32`,
  [OpcodeDispatcher.cpp](../../third_party/FEX/FEXCore/Source/Interface/Core/OpcodeDispatcher.cpp):3335).
- **Not CMPS, SCAS, LODS** (no samples), **not DF = 1, not 0x67** (none sampled).
- **The kernel's `clear_page_rep` is already bulk**; its samples sit on the loop's `stp` (hackbench's top block: 331 of
  353 samples on one store word), the memory itself. Nothing here changes it.

The sizes are not in the samples (they hold no RCX). From the code: `rep_movs_alternative` uses `rep movsq` only for 64
bytes and more (shorter copies take its byte and quadword loops; ERMS is not advertised); hackbench's messages are 100
bytes (12 quadwords and a 4-byte tail); file reads copy up to a page. The user-side sizes are unknown; the kill-test
covers 16 bytes to 64 KiB.

What changed since chunk 26 decided not to build MOVS ([perf plan](perf-plan.md):2946-2949: "under 3% on every
workload"): 32-bit programs run now (startup32 and the i386 toolchain did not exist then), and the sampler places
zstd's `rep movsb` at 5.3% of EL0.

## 2. What runs today

- One element per pass ([OpcodeDispatcher.cpp](../../third_party/FEX/FEXCore/Source/Interface/Core/OpcodeDispatcher.cpp):3303-3424,
  FEX at the pinned 13a1bfcf): RCX = 0 ends the instruction; else the element's load and store (the sites), RSI/RDI step
  by DF, RCX - 1, and a literal exit to the instruction's own RIP, which the resident links to the block starting there. So
  every element after the first runs a block that is the REP alone (the samples' "too short to name" blocks), through a
  linked `b`: about one cycle per element on an M-series core. Chunk 26 measured (M4): 4 KiB `rep movsq` 166 ns, `rep
  movsb` 1,049 ns, against a native 16-bytes-a-round copy loop of 74 ns (L1) to 139 ns (a 256 MiB stream)
  ([perf plan](perf-plan.md):2942-2946). `rep movsd` was not measured; by its element count it is about twice `movsq`.
- 32-bit code: ESI/EDI/ECX step in 32 bits (they wrap at 4 GiB, the upper halves stay zero), the destination is ES:EDI with
  ES's base added (OpcodeDispatcher.cpp:3324-3327, 3353). 32-bit code is compiled only at CPL 3 with flat CS/DS/ES/SS (base 0,
  4 GiB limit; [execution.rs](../../crates/host/src/execution.rs):4650-4652, [M2 design](m2-32bit-design.md):109-110,
  171-172).
- REP STOS bulk (chunk 26): FEX's `FexcRepStos` runs before the one-element pass, with `--bulk`
  ([fexc_hook.h](../../tools/fexc/shim/fexc_hook.h):250-253), which the host sends for every translate request that is not
  a step and not alignment-gated ([execution.rs](../../crates/host/src/execution.rs):4659: so 32-bit requests get it too,
  and FEX ignores it there). The JIT op is natemu's ([fexc_sites.inc](../../tools/fexc/shim/fexc_sites.inc):716-766):
  fast-string byte, DF, RDI 8-aligned for w >= 2, x1 = min(RCX's whole 16-byte chunks, the chunks before RDI's page end),
  then `stp; add; sub; sub; cbnz`. Its `stp` is a store-pair site (fexc_sites.inc:1314-1316), not a committed effect
  (fexc_sites.inc:1373-1376), and a guest-memory op for the precision passes
  ([fexc_precision.h](../../tools/fexc/shim/fexc_precision.h):26-28). Admission pins its words exactly
  ([structure.rs](../../crates/host/src/admission/structure.rs):541-631, 1124-1165), exempts its store from the split check
  (structure.rs:2061-2081) and clears the loop's registers at its back edge in the stack-check dataflow
  (structure.rs:2141-2145).

## 3. What to build, smallest first

1. **Bulk REP MOVS, forward, inside both pages** (the smallest useful version: every hot form above). F3 A4/A5 at every
   width, in 64-bit code (no 0x67) and in 32-bit code (no 0x67: ESI/EDI/ECX), no segment-override prefix, DF = 0, the
   fast-string bit set, for w >= 2 both RSI and RDI 8-aligned (any for bytes), RDI - RSI outside [1, 16] modulo 4096
   (section 5.1). Otherwise the instruction runs as today.
2. **REP STOS in 32-bit code**: chunk 26's loop with 32-bit register updates (section 4.3). Small; it shares step 1's
   32-bit proof.

Deferred, each only if a later profile asks: DF = 1 (the decompressor's `std; rep movsq` and memmove's backward copies:
not sampled); 0x67 forms (not sampled); `rep movsd`/`movsw`/`movsq` with a pointer not 8-aligned (relaxing to natural
alignment needs a proof of FEAT_LSE2's single-copy atomicity for each element inside an `ldp`/`stp` half: task 0's
report and the kill-test's misaligned row say whether it matters); a second loop for the bytes before a page end that
are not a whole chunk; CMPS, SCAS, LODS (no samples).

**A separate option, flagged, not planned: advertising ERMS or FSRM.** With FSRM the kernel's copy_user takes `rep movsb`
for every size and `memcpy` takes `rep movsb` (instead of `memcpy_orig` and `rep_movs_alternative`'s loops); with ERMS
`clear_page_erms` takes `rep stosb` and glibc takes its `_erms` paths for large sizes. Once `rep movsb` is bulk, this would
move more copies onto the bulk path. It changes guest-visible CPUID (the profile's model,
[boot-features plan](boot-features-plan.md):93 and 718-720: ERMS/FSRM stay unadvertised): an owner decision, after step 1
and its profile, with its own A/B.

## 4. The lowering

### 4.1 Where the code goes

As for STOS: FEX's dispatcher calls a new IR op `FexcRepMovs` (width, mode) before the one-element pass when
`Repeat && Kind == MOVS && !A32 && no segment prefix && fexc_bulk` (OpcodeDispatcher.cpp:3335 widened), with the op's entry in
FEX's `IR.json`; the JIT form is natemu's, in [fexc_sites.inc](../../tools/fexc/shim/fexc_sites.inc) next to
`FexcRepStos`. The pass after it keeps chunk 26's ending: RCX = 0 ends the instruction, else one element, then the
instruction's end by a branch if RCX is now 0 (an exit to its own RIP with RCX = 0 is never linked: chunk 26's linker
lesson, [perf plan](perf-plan.md):3071-3075), else the exit to its own RIP.

**Owner note:** [third_party/FEX/CLAUDE.md](../../third_party/FEX/CLAUDE.md) says AI must not be used to generate code
for contributions to the FEX project. Chunk 26's fork commits were made the same way. The FEX-side part here is kept to
the IR op's declaration and the dispatcher's condition. Whether the fork's natemu commits count as contributions there is
the owner's call; the JIT op itself lives in natemu's shim.

### 4.2 `FexcRepMovs` words (64-bit code)

RCX x7, RDI x11 and RAX x4 are the static registers chunk 26's admission requires (structure.rs:578-579); RSI's is read
from the same map. S is the sequence's end. No word sets flags (REP MOVS changes none, and FEX keeps NZCV live across it).

```
  ldrb w0, [x28, #fast_strings]; cbz w0, S          IA32_MISC_ENABLE bit 0 (chunk 26 review, item 1)
  ldrb w0, [x28, #df];           tbnz w0, #7, S     DF = 1: nothing
  [w > 1: orr x0, xSI, x11; and x0, x0, #7; cbnz x0, S]    both 8-aligned
  sub x0, x11, xSI; sub x0, x0, #1; and x0, x0, #0xff0; cbz x0, S    RDI - RSI in [1, 16] mod 4096: nothing (5.1)
  lsr x1, x7, #(4 - log2 w)                          RCX's whole 16-byte chunks
  and x0, x11, #0xfff; movz w2, #0x1000; sub x0, x2, x0; lsr x0, x0, #4      chunks before RDI's page end
  sub x2, x1, x0; and x2, x2, x2, asr #63; add x1, x0, x2                    x1 = min(x1, x0)
  and x0, xSI, #0xfff; movz w2, #0x1000; sub x0, x2, x0; lsr x0, x0, #4      chunks before RSI's page end
  sub x2, x1, x0; and x2, x2, x2, asr #63; add x1, x0, x2                    x1 = min(x1, x0)
  cbz x1, S
L: ldp x2, x3, [xSI]          a load site, 16 bytes, read intent
  stp x2, x3, [x11]           a store-pair site, 16 bytes
  add xSI, xSI, #16; add x11, x11, #16; sub x7, x7, #16/w; sub x1, x1, #1; cbnz x1, L
S:
```

About 30 words of setup and 8 a round (16 bytes), against about one linked pass per element today. Each min is chunk
26's branch-free form, valid because both inputs are below 2^63 (a page's room is at most 256; RCX shifted right by at
least 1). The scratch registers (x0-x3 here; the real choice is FEX's TMP set, as `FexcRepStos` uses TMP1-TMP3) are in the
captured frame at any fault or shadow miss, so a retried access loses nothing (as chunk 26).

### 4.3 32-bit code

The same words with the pointer and count updates in 32 bits: `add wSI, wSI, #16; add w11, w11, #16; sub w7, w7, #16/w`.
A 32-bit register write zero-extends, so ESI/EDI/ECX wrap at 4 GiB exactly as the element pass makes them (OpcodeDispatcher.cpp:3324-3327)
and x86 does: a string that ends at the last page below 4 GiB leaves EDI = 0, not 2^32. The addresses are ESI and EDI
themselves: 32-bit code is compiled only with flat DS and ES (base 0), the host's precondition (execution.rs:4650-4652),
so the linear address is the offset; the element pass's ES-base add is 0 there. When non-flat default segments come (M2
task 6, deferred: [M2 design](m2-32bit-design.md):178), that task must turn 32-bit bulk off for non-flat code (or load and
check the bases): written here so it is not missed.

REP STOS in 32-bit code (step 2) is chunk 26's `FexcRepStos` with the same two 32-bit updates; the dispatcher's condition
drops `Is64BitMode` and keeps `!A32`.

## 5. Why it is exact

The SDM's REP MOVS is element by element: element i reads DS:[RSI], writes ES:[RDI], RSI/RDI step by w and DF, RCX counts
down; a fault, trap or interrupt between iterations leaves RCX/RSI/RDI at that boundary, RIP at the REP, RF set (Vol.2 MOVS,
REP; Vol.3 RF). Grouping iterations is the fast-string operation, allowed only with IA32_MISC_ENABLE bit 0 and WB or WC
memory (Vol.1 7.3.9.3; Vol.3 11.2.4.1). Chunk 26's arguments carry over where noted; what is new is two pointers and their
overlap.

### 5.1 Overlap

Element i reads src + i after the stores of every element j < i. A round reads 16 source bytes, then stores 16. It misses
exactly the stores of earlier elements in the same round. Such a store lands where a later element in the same round reads
only if dst + j and src + i are the same byte with j < i in one round, so (dst - src) = i - j, in [1, 15]. Stores from
earlier rounds were all made before the round's load, so they are seen. When RDI <= RSI, a store of element j can only hit
the source of a later element (j > i), which the round has already read, as x86 has (it reads element i before writing
any later one). So the rounds give the element-by-element result exactly when RDI - RSI is not in [1, 15].

The check is on the physical bytes, not only the linear distance: two linear pages may map one frame (a page aliased
twice), and then bytes far apart linearly overlap physically. A page's offset is the same in every mapping, so physical
dst - src within one frame is (RDI - RSI) mod 4096. The skip `((RDI - RSI) - 1) & 0xff0 == 0`, i.e. RDI - RSI in [1, 16]
mod 4096, is conservative by one (16) and covers every aliasing. Large pages keep the offset mod 4096 too. Common
copies (memcpy, page copies, `rep_movs_alternative`) never meet the skip; `memmove`'s forward case with dst just above src
does, and runs element by element as today, which is the semantics such code relies on (a byte replicated forward).

### 5.2 Faults

- Each round's source and destination lie inside one page each (the two room counts). A page fault (not present, U/S,
  read-only, reserved bit) and a canonical fault are the same for every element of the round as for its first. RCX/RSI/RDI
  are updated after the store, so at either access they are the boundary before the round's first element, and every
  element before it is complete.
- The order is the SDM's: x86's first element reads, then writes. The round's `ldp` comes before its `stp`, so a round
  whose source and destination both fault reports the source (the read, error code without W, CR2 = RSI), as x86 does. If
  only the destination faults: #PF with W, CR2 = RDI, after a read of the round's source with no effect (WB RAM, the same
  readable page). Nothing of the round is written (the page's translation refuses the `stp`; chunk 26's argument,
  [perf plan](perf-plan.md):2994-2996).
- The `ldp` must be recorded as a read: the site recorder pairs a load with a following store into a read-modify-write
  (write intent) unless the instruction is a MOVS (fexc_sites.inc:1360-1370, `fexc_movs`). The bulk op is one, and the
  pairing rule there tests STORE and ATOMIC kinds only, but the op is added to `fexc_movs` anyway and a test (9.2,
  `movs-src-readonly`) pins that a read-only source does not fault.
- A non-canonical RSI or RDI (or one inside the frame alias or the former private window) faults at the round's first
  access, at its page, as the element's own access would: the bulk accesses use the same addresses, page-granular, so every
  page-level protection applies equally. A string that walks off the last canonical page faults at the first non-canonical
  element after a round ends at the page end (as chunk 26's `into-noncanonical`).
- A shadow miss retries the access; the loop's registers are in the captured frame.

### 5.3 Interrupts, stops, traps

The loop runs at most 256 rounds (x1, written by nothing else, is at most the rooms) and has no exit, so events arm at
the next exit and are taken at iteration boundaries only, later by at most one page's elements (chunk 26,
[perf plan](perf-plan.md):2999-3001). TF: steps never get `--bulk` (execution.rs:4659), so each iteration traps. Data
breakpoints and alignment checking: `--bulk` is off while `alignment_gated` (a data breakpoint, read or write, armed or
AC), part of the code context, so such blocks never run the loop; a breakpoint on the source or destination traps at its
element as today.

### 5.4 Memory types and devices

The fast-string byte is tested first, so with bit 0 clear every iteration is a boundary. WB and WC run natively. A store
to UC, WT or WP RAM is never made through a block's shadow (chunk 26 review, item 1): the instruction runs alone, one
element per step. The source side is new: whether a block's load from a UC page aborts to the same one-element step, and
what a device page does with a load-pair site, is checked first in task 1 (chunk 26 says a device page refuses a pair
site "as a REP string's element was": [perf plan](perf-plan.md):3004-3005); tests `uc-src`, `uc-dst`, `wt-wp`, and a
device source and destination (9.2) pin that a REP MOVS touching them gives the element-by-element accesses, in order,
with and without `--bulk`.

### 5.5 Code writes, ordering, atomicity

- A store into a page holding translations aborts (shadowed read-only); into the block's own source page the instruction
  is stepped from the store's state, as for STOS (chunk 26).
- Ordering: the rounds load and store in element order, under the hardware's TSO mode as every other block access; a
  second vCPU sees the string's stores before any store after the instruction (chunk 26's `tso` case, for MOVS too).
  x86's fast strings may even reorder stores within the string (Vol.3 "fast-string operation and out-of-order stores");
  natemu does not.
- Atomicity: for w >= 2 both pointers are 8-aligned, so each 8-byte half of the `ldp` and `stp` is single-copy atomic and
  holds whole, naturally aligned elements; bytes are atomic anyway (chunk 26's argument, applied to both accesses).

### 5.6 32-bit code

Section 4.3: 32-bit updates wrap at 4 GiB as the element pass's do, and the round's room counts keep each access inside
one page below the wrap. Flat DS and ES make the offset the linear address. No segment-override prefix is bulk (an FS or
GS source keeps the element pass, with its base and, at checkpoint C, its limit check: [fexc_hook.h](../../tools/fexc/shim/fexc_hook.h):137-142).

## 6. Admission

New rules, in object validation as chunk 26's (structure.rs:1124-1165), so a defective compiler cannot get a loop past them:

1. **The back edge** of a `FexcRepMovs` loop is admitted only if the words before and after it are exactly the sequence
   of section 4.2 for the width of the REP MOVS holding them, and for the block's mode (`bits32`, structure.rs:670): the
   64-bit words in a 64-bit object, the 32-bit ones (W-register updates) in a 32-bit object; either in the other mode is
   refused. The width comes from the guest bytes (`x86_rep_movs`, new beside `x86_rep_stos` at structure.rs:541: F3 among
   the legacy prefixes, no F2, no 0x67, no segment prefix, A4/A5 with 66 or REX.W, the instruction's last byte its opcode).
2. **Its two sites**: the `ldp` is that instruction's recorded load site of 16 bytes with read intent, the `stp` its
   store-pair site of 16 bytes; every skip goes to the sequence's end; no branch from outside enters it.
3. **What follows from the words** (the proof the rules give): x1 is the min of RCX's chunks and both pages' rooms, at
   most 256, decremented once a round and written by nothing else, so the loop ends; RSI and RDI each advance 16 a round
   from their room counts, so every `ldp` stays inside RSI's page and every `stp` inside RDI's; the `stp` is exempt from the
   split check on that ground (structure.rs:2061-2081 extended to the new range). The overlap and alignment skips are part
   of the pinned words, so a loop without them is refused.
4. **REP STOS in 32-bit code**: `bulk_words` gains the 32-bit variant, selected by `bits32`; the 64-bit form in a 32-bit
   object is refused (it would not wrap EDI at 4 GiB).
5. **The flag**: an object holding either loop from a request without `--bulk` is refused, as today (admission.rs:292-293).
6. **Stack-check dataflow**: the new back edge clears what its loop writes (x0-x3, RCX, RSI, RDI) in `aligned`
   (structure.rs:2141-2145), as the STOS edge does.

Containment is unchanged in kind: the loop is bounded, pinned word for word, and loads and stores only through RSI and RDI
at EL0, like the element's own accesses.

## 7. Interactions

- **Split checks and page-crossing probes**: the loop's stores need none (each inside its page); the one-element pass
  after it keeps its own split check for a wide element that crosses a page (structure.rs:42-44). Probes (M2 task 4) are
  not involved: no probing stub in the sequence.
- **Stack checks** (`--push-run`): the op writes neither RSP nor anything the RSP alignment fact rests on; the back edge
  rule above keeps the dataflow exact.
- **Gating and caches**: `--bulk` is already gating bit 11; fexc reads it now for REP MOVS and 32-bit REP STOS too, so
  only answers holding those instructions depend on it. fexc's identity changes, so cached answers recompile.
- **Precision passes**: `FexcRepMovs` is a guest-memory op (fexc_precision.h:26-28, 1198), its stores not committed
  effects (fexc_sites.inc:1373-1376); `verify_precision`'s REP exemption (the `cbz x7`) is extended to the new op.
- **The sampler's roles**: `sample_roles.py` counts a bulk STOS loop as FEX body with its guest instruction in the REP share
  ([sample_roles.py](../../tests/bench/sample_roles.py):9-10); the MOVS loop gets the same treatment, so the end profile
  reads comparably.

## 8. Kill-test

A guest micro in natemu, one run before and one after, on the M2 Max, as a framework case (so its numbers can be cited):

- **`repmicro`**, a new static program in the bench image ([build.sh](../../tools/bench/image/scripts/build.sh):97-104
  builds `coldtrip` and the `-m32` programs the same way), 64-bit and `-m32` builds. Rows: `rep movsb` (64-bit), `rep movsq`
  (64-bit), `rep movsd` and `rep movsb` (32-bit), `rep stosd` (32-bit), each at 16, 64, 128, 512, 4,096 and 65,536 bytes with
  both pointers 64-byte aligned; controls that must stay on today's path: `rep movsd` with the source at +4 (not
  8-aligned), `rep movsb` with dst = src + 8 (the overlap skip), `rep movsq` with the source at page offset 0x808 (rooms
  differ). Each row repeats its copy enough for about 50 ms, three rounds, and prints the median ns per copy and a
  checksum of the destination, which the judge compares with the program's own `--sums` (as `coldtrip`).
- **`guest-repmicro`**, a new case in `tests/bench/family.toml` beside `guest-coldtrip-profile`, run through `tools/bench/run.py`.
- **Order**: the program and the case land first, with no change to natemu; one run there ("before"), one at the end of
  step 2 ("after").
- **Kill if**, at 4 KiB aligned, `rep movsd` (32-bit) is not at least 2x and `rep movsb` (64-bit) not at least 4x faster; or
  any control row is more than 10% slower. Expected from chunk 26's native loops (section 2): 4 KiB `movsb` about 1,050 to
  75-140 ns, `movsd` about 330 to the same, `movsq` 166 to the same. At 128 bytes, the setup words weigh more; that row says
  whether `rep_movs_alternative`'s hackbench sizes gain.

## 9. Tests

Every new check is shown to fail on a mutation of the code (9.4), as chunk 26's negative controls.

### 9.1 Host (`crates/host`, with `FEXC`)

- `bulk_rep_movs_only_when_asked`: the loop with `--bulk` for movsb, movsw, movsd, movsq in 64-bit code and movsd, movsb in
  32-bit code; none for 0x67, an FS/GS or other segment prefix, F2, or without the flag; a bulk object refused for a request
  without it.
- `bulk_rep_movs_loops_are_pinned`, fixtures `rep-movsq-bulk`, `rep-movsb-bulk`, `rep-movsd32-bulk`, `rep-stosd32-bulk`:
  admitted with and without the code shape; refused with: either room count dropped, the overlap skip removed or its mask
  changed, the alignment test dropped, the back edge to the `stp`, the `ldp` off its site or recorded with write intent,
  RCX stepped by another amount, a 64-bit update in a 32-bit object and the reverse, a branch from outside into the loop.

### 9.2 System (`tests/system/run_k2.py repbulk`, the `k2-repbulk` case; natemu only, judged by the SDM, block sizes default and 1)

Chunk 26's STOS cases (run_k2.py:1404-1666) are the model; the new ones judge against an element-by-element Python model
of the SDM's MOVS:

- `movs-fill-wN`: every width; source and destination offsets 0, 3, 8, 0xff0, 0xff8, 0x808 in all pairings; counts 0, 1, a
  chunk less one, a chunk, one more, 100, two pages and 3; DF = 1 twice; 0x67 once; an FS source once: RCX 0, RSI/RDI the
  ends, exactly the string's bytes.
- `movs-overlap`: dst - src in -17, -16, -15, -1, 0, 1..17, 31, 32, 4095, 4097 at every width and counts 24, 100, 300: equal to
  the model (the replicated-byte patterns for 1..15).
- `movs-alias`: one frame mapped at two linear pages 0x10000 apart, dst - src = 0x10000 + d for d in 1..16, and 0x10000 + 17:
  equal to the model (physical overlap).
- Faults: `movs-src-unmapped` (source page 2 not present: #PF without W, CR2 the source element, RCX/RSI/RDI there, every
  earlier element written), `movs-dst-unmapped`, `movs-dst-readonly`, `movs-both` (both pages fault: the source is reported),
  `movs-src-readonly` (no fault), `movs-first` (nothing written), `movs-cpl3-us`, `movs-noncanonical` (each pointer),
  `movs-into-noncanonical`, `movs-frame-alias` (#GP(0), nothing changed).
- `movs-kicks-wN` and `movs-kicks-w1/w4-misc-off` (random interrupts: at each one taken at the REP, RCX + (RDI - DST)/w the
  count and RSI - SRC = RDI - DST; the element before RDI this round's, the one at RDI the last round's; RF set).
- `movs-tf`, `movs-watch-src` and `movs-watch-dst` (read and write breakpoints), `movs-ac` (CPL 3, AC and CR0.AM, a
  misaligned element: #AC at it), `movs-uc-src`, `movs-uc-dst`, `movs-wt-wp`, `movs-smc-other`, `movs-smc-own`,
  `movs-linked` (Linux's `rep_movs_alternative` shape with an odd count: linker entries do not grow with N), `movs-tso`.
- `stos32-*`: chunk 26's `fill`, `fault`, `kicks` and `linked` cases in 32-bit code, plus `stos32-wrap` and `movs32-wrap`
  (a string ending at the last page below 4 GiB: EDI = 0 after it, and the next element at 0).

32-bit cases need a 32-bit start in run_k2's program driver; if it has none, they go in the `compat32` case's programs
([compat.py](../../tools/bench/smp/compat.py), which already copies with `rep movsd`: compat.py:430) instead.

### 9.3 Against QEMU and real x86 (answers the framework locks)

- **QEMU, 64-bit**: the I2 string corpus ([gen_i2.py](../../tools/reference/gen_i2.py)) is not a framework input today
  (tests/programs/family.toml has no `i2`). Add `[input.i2]` and a suite with `reference = "qemu"` at `engine.max_block` 0
  and 1, as the `fix`/`i10` suites. Extend its overlap programs (gen_i2.py:157-163: deltas -9, -1, 1, 3, 8, 17 at count 24)
  with deltas -17, -16, -15, 15, 16, 31, 32 and counts 100 and 300, so bulk and skipped rounds both run against QEMU's
  element-by-element answers, locked by `natemu-test lock`.
- **QEMU, faults mid-string**: a `loopfaults-i2` case running `tests/faults/run_slice2_faults.py --programs` on gen_i2's
  `--flow` corpus (QEMU's signal context gives RCX/RSI/RDI and memory at the faulting element), at block sizes default and 1.
- **Real x86, 32-bit**: new compat programs with `rep movsd`/`movsb` overlap patterns (deltas 1, 3, 8, 15, 16, 17, -1), a
  wrap at 4 GiB and a fault mid-string, added to `run_compat_kvm.py`'s `SELECTED` (tests/smp/run_compat_kvm.py:21), whose
  answers the AMD KVM box locks (`supplier = amd-kvm`).
- Unchanged families that must stay green: `faults` (slice 1, I7, I10 string forms), `m2` (memory types, devices),
  `compat32`, `k2-repbulk`'s STOS cases, the gate.

### 9.4 Mutations (each a scratch build, reverted; each must turn its tests red)

| Mutation | Red |
| --- | --- |
| overlap skip removed | `movs-overlap` d = 1..15; i2 `overlap-movs8-1/3/8` against QEMU; compat overlap against KVM |
| overlap on the linear distance only (no mod 4096) | `movs-alias` |
| RSI/RDI/RCX stepped before the store | the fault cases (registers a round ahead) |
| the destination's room dropped | `movs-dst-unmapped`, `movs-fill` at dst 0xff8 (a store straddling the page) |
| the source's room dropped | `movs-src-unmapped` |
| `stp` before `ldp` | `movs-both` (the destination reported), `movs-overlap` d < 0 |
| 64-bit updates in 32-bit code | `movs32-wrap`, `stos32-wrap` |
| fast-string test removed | `movs-kicks-*-misc-off` |
| bulk in steps too | `movs-tf`, `movs-uc-*` |
| the `ldp` with write intent | `movs-src-readonly` |
| DF test removed | `movs-fill` DF = 1 |

## 10. Order of work and estimate (agent hours)

| Task | Hours |
| --- | ---: |
| 0. `sample_report.py`: the REP share by form (prefix, opcode, width, mode, privilege), with a unit test; the `repmicro` program and `guest-repmicro` case; the "before" run | 4 |
| 1. Check the source-side paths: a block's load from UC, WT, WP and device pages; the load-pair site record; FEX's static register for RSI; the TMP registers free in a JIT op | 2 |
| 2. FEX: the IR op and the dispatcher's condition (MOVS; STOS in 32-bit code) | 1.5 |
| 3. Shim: `FexcRepMovs` (64- and 32-bit), the 32-bit `FexcRepStos` variant, sites, `fexc_movs`, precision barrier, `verify_precision` | 5 |
| 4. Admission: `x86_rep_movs`, the templates by width and mode, two sites, split exemption, dataflow; fixtures and the two host tests | 6 |
| 5. System cases (9.2) | 7 |
| 6. QEMU and KVM cases (9.3): `i2` input and suite, gen_i2's new deltas, `loopfaults-i2`, compat programs and their KVM lock | 5 |
| 7. Mutations (9.4) | 3 |
| 8. Independent review of design and code, fixes | 4 |
| 9. The "after" kill-test run; the plan updated with the result | 1 |
| **Total** | **38.5** (30-48) |

The gate, the combined A/B and the profile round are the pooled end of the owner's list, not counted here.

## 11. Expected gain

Share x (1 - 1/speedup), the speedups at 4 KiB from section 2 (`movsq` 1.2-2.2x, `movsd` 2.4-4.5x, `movsb` 7.5-14x, 32-bit
`stosd` about 3.5x), applied to the sampled shares (section 1) as if every pass were eligible: upper bounds, before
misaligned and small copies.

| Case | Saved, % of EL0 samples | As % of the vCPU's time (EL0's share of samples) |
| --- | ---: | ---: |
| `guest-startup32` | 4.6-6.0 | 2.4-3.2 (52.5%) |
| `guest-zstd` run1 | 4.8-5.4 | 4.4-4.9 (91.3%) |
| `io5-solo32` i386-make | 2.5-3.3 | 1.6-2.1 (63.8%) |
| `io5-solo32` manifest, fetch-python | 0.6-1.9 | |
| `guest-build` | 0.3-1.0 | 0.15-0.5 (47.7%) |
| `guest-hackbench` | 0.5-1.5 | not read (its EL0 table is a minority of its EL0 time: findings section 1) |

## 12. Main correctness risks

1. **Overlap**: a round that reads before an earlier element's store in the same round lands. The skip on (RDI - RSI) mod
   4096 in [1, 16] is the whole defence, including physical aliasing; three references test it (the SDM model, QEMU, KVM).
2. **Fault order and state**: two accesses per round. The source must be reported before the destination, with read
   intent, and the registers must be the round's start at either. Pinned by the fault cases and the `ldp`/`stp` order rule.
3. **32-bit wrap and segments**: W-register updates are the exactness at 4 GiB; flat DS/ES is a precondition of all 32-bit
   code today and must be revisited by M2 task 6.
4. **The source side of memory types**: UC, WT, WP and device pages as a load source were never a bulk path before; task 1
   checks them before any code.
5. **Element atomicity** across vCPUs: the 8-alignment rule for w >= 2; relaxing it needs its own proof.

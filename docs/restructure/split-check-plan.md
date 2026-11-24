# Page-crossing stores: fewer checks, and no trip when the probes pass (plan)

November 23, 2026, on `m2-sampler` at d3f2fe4d. Design only: nothing here is built. One of the fixes the owner pooled
after the [EL0 sampler findings](el0-sampler-findings.md) ("Split checks and probes >= 3%": **build**): no A/B of its
own, one combined A/B and profile round at the end, correctness first, a kill-test per step.

Where the numbers come from. Shares of EL0 samples, bucket times and counters are from the sample cases of run
20261123-213409-4d92 (M2 Max, natemu a7cd8e82), as the findings report them. Where this page divides two of a case's
counters, or reads a block's words out of the case's objects file (disassembled with `llvm-objdump`), that is a reading
of the case's output, labelled so; none of it is a page number. The kill-tests below are framework cases.

## 1. The cost

Every store wider than a byte checks whether it crosses a 4 KiB page: `[add x1, base, #imm | index;] add x0, A, #w-1;
eor x0, x0, A; tbnz w0, #12, stub` (`tools/fexc/shim/fexc_sites.inc:1391`, emitted at `:1609-1627`). A store that
crosses branches to its two-word stub, `adr x0, R; b <shared probe>`, R the word after the check (`:1655-1680`); the
probe writes one byte of each page with `staddb wzr` and returns with `br x0` (`crates/abi/src/execution.rs:2394-2460`).
`--push-run` drops the check for a store of 2, 4 or 8 bytes at base + k*width, no index, while the base is known
slot-aligned; only the guest stack pointer gets the alignment check that makes it known (`fexc_sites.inc:1588-1606`,
admission `crates/host/src/admission/structure.rs:2061-2080, 2141-2250`).

Split checks and probes, % of EL0 samples, raw / one-word skid correction: `scimark32-sse2` 10.4 / 17.7, `sqlite`
5.0 / 6.0, `luajit` 4.4 / 7.0, `startup32` 3.6 / 6.6, `build` 2.3 / 4.1 (findings section 3).

`scimark32-sse2`'s split-check samples by block (reading of the case's report, top 50 blocks, 19,390 raw split-check
samples of 181,749 block samples):

| Block | Function | split-check samples | the stores (x86) |
| --- | --- | ---: | --- |
| `0x8049ed0`, `0x8049e70`, `0x8049e00`, `0x804a020` | FFT | 6,742 + 2,305 + 1,010 + 606 | `movsd [edx]`, `movsd [edx+8]`, `movups [eax]` |
| `0x804c480`, `0x804c450` | SOR_execute | 3,772 + 236 | `movsd [eax+esi*8]`, `movsd [eax+esi*8+8]` |
| `0x804befa`, `0x804bf55` | Random_nextDouble | 2,867 + 809 | `mov [eax+ecx*4]`, `mov [eax+0x48]`, `mov [eax+0x4c]`; `movsd [esp]` |
| `0x804ca11`, `0x804c9f9` | MonteCarlo_integrate | 203 + 232 | `fstp qword [esp+k]` |
| `0x804cec0` | LU_factor | 148 (and 116 in its probe and stub) | `movups [eax-16]` |

The SOR loop as served (reading of the objects file): 56 words per iteration, 7 of them the two checks.

```
0x60  add  w20, w4, w10, lsl #3      ; eax + esi*8, 32-bit (wraps at 4 GiB): the store's own address
0x64  add  x0, x20, #7  / eor x0, x0, x20 / tbnz w0, #12, stub
0x70  str  d16, [x20]
0xbc  add  w20, w4, w10, lsl #3
0xc0  add  x1, x20, #8               ; only for the check
0xc4  add  x0, x1, #7  / eor x0, x0, x1 / tbnz w0, #12, stub
0xd0  str  d16, [x20, #8]
```

In 32-bit code FEX computes every address into a temporary with a 32-bit add, so the store's base is never the guest
register itself; in 64-bit code the store uses `[xB, #imm]` and the check pays an `add x1` of its own. FEX's address
forms are base plus an immediate or a register extended and scaled by 1 or the access size (FEX `MemoryOps.cpp`
`GenerateMemOperand`, lines 592-618 in the main checkout's `third_party/FEX`).

## 2. Why crossing stores still trip (the "97% in the resident" question)

In `guest-sampler-check-sample`'s cross phase (`sampler_check.c:100-115`: `mov [rcx], rax` at offset 4092 of a static
page pair, in a four-instruction loop) under 1% of samples are EL0 (277 and 248 of about 32,000 per run). It is none of
the suspects:

- not the probes faulting: the whole boot has 326 k `abort` and 331 k `shadow` entries against 47.6 M crossing stores
  (23,789,568 + 23,855,104 iterations, the phase's console lines);
- not the one-instruction path (711 steps in the boot), uncached memory (563 UC walks), alignment checking (off).

It is the linker, once per iteration: 48.70 M `link` entries, 47.76 M `patch` entries, 48.78 M `enter`, and only 73.8 k
links made (the case's perf log). About 336 ns per iteration (8.006 s / 23.79 M).

The cause is the hot/cold layout (`translated-code-layout.md` section 4). A block is entered in its hot copy, and the
hot copy's `tbnz` goes to the stub in the cold code (`crates/runtime/src/cache.rs:117-160`, `map`: a stub is "anything
else: its code"). The stub's `adr x0, R` is PC-relative, so R is the cold copy's store, and the probe's `br x0` returns
there. The rest of the iteration runs in the cold code, whose exits are never patched: a link patches the hot copy's
trampoline and leaves "its code's word ... as admitted" (`crates/abi/src/execution.rs:1386-1397`). So every crossing
store ends its block at an `svc #LINK`; the linker finds the link already made (`cache.rs:2292`, `link`: nothing
unlinked, `Linked(0)`) and enters the hot copy again. The x87 run end had the same problem and was fixed by re-pointing
its return at the hot copy (`cache.rs:152-171`, `run_return`).

It matters for real code. `guest-scimark32-sse2-sample` has 7.87 M linker entries but 74.8 k links made, with 6.93 M
`patch` entries (the same signature); `link` + `patch` + `enter` hold about 1.8 s of the two runs (0.96 + 0.29 +
0.52 s, part of it genuine links). LU's `movups` stores over rows that malloc put at 8 mod 16 cross a page once per
page per pass: by a rough count about 300 crossings per factorisation of the 100 x 100 matrix, at about 300 ns each
against about 260 us per factorisation at the measured 2,550 Mflops (sampled run), so roughly a third of LU's time
(an estimate). The 1.6-1.7 times gain of the shared probes (m2-task4-findings, "Built: page-crossing stores without the
trap") removed the trap but not this trip. Any memcpy-like loop with unaligned vector stores pays it once per page.

What remains avoidable after the fix (section 4.1): nothing of the probe path. A crossing store still enters the
resident when a probe faults (the page is not writable in this vCPU's shadow: first write, copy-on-write, a clear D bit,
after a shadow reset or a CR3 load, uncached or device memory, a page of translated code), when a breakpoint is armed,
for a 64-bit wrap or a 32-bit address above 4 GiB, and in a step block. Those are the walks any first store pays, except
one thing: `split` (`crates/runtime/src/execution.rs:4508-4630`) resumes at the cold store's next word
(`b.code_va() + access.native_offset + 4`), which again leaves through the linker. That second trip goes too.

## 3. What a check must guarantee

A store that crosses a page must complete exactly across a fault: if the second page faults, nothing is written to the
first (Arm may commit the first part, `fexc_sites.inc:1554-1557`). The check exists only to send such a store to code
that tests both pages first. So a store needs no check exactly when its bytes cannot lie on two pages, and the probe
path needs no trip exactly when nothing between the probes and the store can change this vCPU's shadow tables (the
load-bearing sentence at `fexc_sites.inc:1671-1676`).

The proof used below: if A is a multiple of w (w a power of two up to 16), then [A, A + w) lies inside one aligned
w-byte block, and w divides 4096, so it lies inside one page. In 32-bit code the same holds for the x86 address
A mod 2^32, since 2^32 is a multiple of w. A = N + (M << s) + imm is a multiple of w when N is a multiple of w, s >=
log2 w (whatever M is) and imm is a multiple of w.

## 4. The steps, smallest first

### 4.1 Step A: the probe returns to the hot copy (resident only)

When `split` (`cache.rs:117`) builds a block's hot copy, it rewrites each probing split stub's `adr x0, R` in the cold
code to `adr x0, R'`, R' = `hot_body + (R - code)`, when R lies in `[code + ENTRY_OFFSET, code + hot_size)` (the hot copy
holds the word). The hot zone and the cold code are in one 32 KiB segment, so `adr` (+-1 MiB) reaches; if
`probe::adr_x0` (`execution.rs:2492`) says it does not, the word stays. `split` in `execution.rs:4508` likewise resumes
at the hot copy's word after the store when it holds one (one helper, `cache::hot_at(b, offset)`).

Why it is exact: R' holds the same store word as R (`moved_word` copies a store unchanged: it is not PC-relative), so
the probe returns to the same instruction with the same registers; the probes and the store are the same words in the
same order, with no resident entry between them; every resident return into the sequence still goes to the stub's first
word (`execution.rs:3993-3998`), which now re-runs the rewritten `adr`. Nothing else reads R as a code offset:
`probe::stub_of` (`execution.rs:2514`) compares the `adr`'s target with `d0 - code`, both relative to the cold code, so a
hot R' matches as R did; `cache::block_at` finds a block from a hot address (`cache.rs:3438-3447`); `probe::returns_to`
and `rearm_at` arm the stop in the block found from R' (`execution.rs:2478-2490`).

What must restore the admitted word, as each does for `run_return` and the probe branch today (a copy that kept the
rewritten `adr` would return into another block's hot copy, the main risk of this step):

- `copy` (`cache.rs:1590-1615`, and its `original` at `:1312-1321`): the copy gets the admitted `adr`, its own `split`
  re-points it;
- `unlinked_word` (`cache.rs:1468-1482`), which `share` and the PIC proof read: the admitted `adr`. (`share` makes
  `run_return` blocks unshareable, `cache.rs:1289-1295`; with the word restored here that is not needed for this one);
- the reuse check (`crates/runtime/src/execution.rs:1660-1680`): a rewritten `adr` compares as its admitted word;
- the host's audit of placed stubs (`crates/host/src/execution.rs:1538-1556`): accepts the admitted target or exactly
  the hot copy's word for it, nothing else.

The pool is not involved: the host publishes fexc's object, not placed code (`cache.rs:1694`).

A counter, `cold_exits`: linker entries whose `svc` lies in the cold code of a block that has a hot copy
(`execution.rs:707`, `linker`). It is the kill-test's number, and it tells whether any other cold return (an FS/GS
operand check's resume at `b.code_va() + site.imm`, `execution.rs:3966-3979`, is one) is worth the same treatment.

Work: half a day to a day.

### 4.2 Step B: alignment guards on any base

Generalise `--push-run`'s aligned registers from "the stack pointer, slot-aligned, no index" to "any base register,
aligned to 2, 4, 8 or 16 bytes, with an index scaled at least as far":

- **The guard.** At a store wider than a byte whose address is N + (M << s) + imm (N a register x2-x30, M none or s >=
  log2 w, imm a multiple of w, w <= 16, not a pair), when N is not known aligned to w, fexc emits `and x0, xN, #(w-1);
  cbnz x0, K` instead of the split check, K a new stub kind `BASE` (`brk #15`), linked to the store like the stack
  check. N is the register the address is computed from, not FEX's temporary: fexc takes it from the address's
  defining IR op (an add of a guest register and a scaled index or a constant). A guard costs 2 words where the check
  costs 3 or 4, so no store pays more than today; every later store on N in the unit pays nothing.
  Only where the instruction has stored nothing yet (`!FexcStoreSeen`, the stub's `completed_effects` 0,
  `fexc_sites.inc:1423`), so the state at K is the state before the instruction, as at a split or stack stub. Never in
  a step unit. RSP keeps its stack check for pushes; a guard on RSP is for the stores the stack check does not cover
  (an 8-byte store in 32-bit code, a 16-byte one).
- **What is known aligned** (one forward dataflow over the words, in fexc, admission and `verify_precision.py`, each
  register at a level 0-4: aligned to 1, 2, 4, 8, 16): a guard's fall-through sets N to log2 w; the stack check's sets
  the stack pointer to the slot; an I5a alignment check's fall-through (`and x0, xA, #15|#31; cbnz`) sets A to 4 (the
  "prior aligned access to the same base", free in this lattice); `add|sub D, N, #imm` gives min(N, tz(imm));
  `add|sub D, N, M{, lsl|uxtw|sxtw #s}` gives min(N, M + s); `mov` copies; `and D, N, #mask` gives max(N, tz(mask))
  (values derived by masking, `and rdi, -32`); a writeback of delta gives min(base, tz(delta)); the 32-bit `w` forms
  the same (mod 2^32 keeps the low bits). Any other write clears the register, `svc` clears all, a branch target meets
  the minimum over its sources, the entry and a backward target know nothing (as `aligned()` does now,
  `structure.rs:2146-2216`; fexc `fexc_sites.inc:55-79, 1141-1163`, carried between IR blocks by `FexcCarried`).
- **The elided store.** A store wider than a byte may have no split check when its address, read through the register's
  reaching definition (the forms `address_in` already decodes for the split rule, `structure.rs:2081-2100`, plus the
  32-bit `add wD, wN, wM, lsl #s` and `add wD, wN, #imm` FEX emits in 32-bit code), is N + (M << s) + imm with N at
  level >= log2 w before the store, N and M unwritten between that definition and the store, s >= log2 w, imm a
  multiple of w. The proof is section 3.

On the SOR loop: one guard on x4 (EAX) before `0x70`; `0x70` and `0xd0` lose their checks and `0xc0`'s `add x1`. 56 to
51 words per iteration. FFT's loop (46 words, checks 3 + 4 + 3): guards on x5 and x4, 40 words. Random_nextDouble's
first part (73 words, checks 3 + 4 + 4 on EAX's struct): one guard, 64 words. LU's `movups [eax-16]` would get a 16-byte
guard that fails half the time (rows at 8 mod 16): the policy below takes it back after the first failure.

**When a guard fires.** The base is not aligned: the stores may cross. The resident (`aborted`, a `BASE` arm beside
`STACK`'s, `execution.rs:4002`) notes the unit's RIP and the instruction's in a per-vCPU bitmap (`base_varied`, hashed
as `x87_varied_bit`, `crates/abi/src/execution.rs:639`), discards the unit as `x87_guess_miss` does
(`execution.rs:4027-4045`), and steps the instruction (`step_from`, as the stack check does): its step unit has
per-store checks, so the instruction completes or faults exactly, and progress does not depend on which unit is served
next. A noted RIP is requested without guards from then on: `ask` (`execution.rs:1145-1157`) sets a request field the
host turns into the absence of fexc's new `--base-guards`, which is a gating argument, so the answers table keys on it
(`crates/host/src/execution.rs:2724-2728, 3130-3145`) and an answer that read no guard is shared by both. The pool keys
only on context, place and lead bytes (`crates/abi/src/pool.rs:137-158`): the resident's pool check refuses an entry
with a `BASE` site for a noted RIP. So a misaligned base costs one step and one compile per unit RIP, once, and the
unit then runs today's code (as the x87 guess's miss, m2-task4-findings "Built: 32-bit code compiled for the stack top
it starts with"). A per-vCPU count like the stack check's (`STACK_TRAP_LIMIT`, context bit 33) is not used: misaligned
heap bases are common, and one misaligned unit must not take the guards from all code.

Where the fexc side lives: the guard and the elision in `fexc_sites.inc` (compiled into FEX's JIT), the levels carried
between IR blocks in the fork's `FexcCarried` (`third_party/FEX` `JITClass.h:597`, `JIT.cpp:1084-1087`: a mask today),
and the guard's base taken from the address's IR op. `third_party/FEX` is natemu's own fork (owner ruling, AGENTS.md at
4743c184): changes there follow the same review and test rules as the rest.

Work: three to four days (fexc emission and dataflow one; admission, `verify_precision.py` and `check_sites.py` one;
the resident's stub, the request field, the host argument and the pool refusal half to one; tests one).

### 4.3 Considered, not planned

- **One check per loop iteration over a strided span** (one crossing check on [A, A + 16) for SOR's two stores). Exact
  when the span does not cross; when it does, some store in it may, and the fallback must complete each store exactly.
  A probing stub over the span is exact only if nothing between the probes and the last store can enter the resident,
  and SOR has loads in between (a load's shadow miss may reset the shadow). The exact fallbacks are a step (about 12 us,
  once per 256 iterations for a 16-byte span: costlier than the checks it saves) or a second copy of the body with the
  checks. Not lightweight; the guard covers SOR better.
- **Alignment carried across iterations** (a loop induction with a constant stride needing no guard per iteration). A
  natemu block is one iteration and its entry knows nothing; carrying facts across the back edge needs back edges inside
  blocks (the multiblock contract change in the guest-code review's section 6). Within an iteration the lattice already
  follows `add N, N, #16`.
- **A cheaper check.** Chunk 28's finding stands (`perf-plan.md:3253`): `add; eor; tbnz` is the shortest flag-free test
  of "crosses 4 KiB". The forms tried here are no shorter: `and #0xfff` then a compare needs flags; the 8-aligned 16-byte
  case (crosses iff A & 0xff8 == 0xff8) takes `add #8; and #0xff8; cbz`, three words. The only word a check wastes is
  the `add x1` of `[xB, #imm]`, which the guard removes. LU's 16-byte stores at 8-aligned addresses keep their check.
- The 32-bit stack check at each unit's first push (5.1% on i386-make): the same 2-word guard; nothing cheaper exactly
  while a unit's entry knows nothing. Not in this plan.

## 5. Admission's new rules

In `structure.rs`, the code shape check (debug, on in the test suites; production admission does not run it, as for
today's split rule, so the proof is checked on the corpora, `structure.rs:38-56`):

1. `Verifier::aligned` returns a level per register (four masks: aligned to 2, 4, 8, 16; the meet is their AND) with the
   transfers of section 4.2. A register's level rises only at a guard's, stack check's or alignment check's
   fall-through, or through the listed words from a register already at that level.
2. A `BASE` stub (new `object::kind`, `crates/abi/src/code_object.rs:45-110`) is `brk #15`, reached by exactly one
   branch: `cbnz x0, K` after `and x0, xN, #m` with m one of 1, 3, 7, 15 and N >= x2 the stub's `base_reg`, its width
   m + 1; only x0/x1 written from the `and` to the store it is linked to, no branch entering; its site has
   `completed_effects` 0 and no store site of its instruction precedes it.
3. A store wider than a byte is admitted without a split check (the `natural` branch, `structure.rs:2068-2080`,
   generalised) iff it is not a pair, not bulk, w <= 16, and its address, through the reaching definition of its base
   register, is N + (M << s) + imm with s >= log2 w, imm a multiple of w, N and M unwritten between that definition and
   the store, and N at level >= log2 w before the store: from a guard linked to this store, or by the dataflow. Anything
   else keeps the rule as it is: a split check on exactly its address, or "store with neither a split check nor a stack
   alignment check covering its base".

Proof: (1) is sound because each transfer preserves "the register's value is a multiple of 2^level" (an add of two
multiples is one, mod 2^32 and mod 2^64 keep the low bits, an AND cannot set bits) and the meet keeps only what holds on
every path; a guard's fall-through is reached only when `xN & m == 0`. (3) then gives A a multiple of w, section 3
gives one page. (2) makes the stub's state the state before the instruction, which `step_from` needs.

`verify_precision.py` (P8, `tools/fexc/verify_precision.py:415-431, 523-535, 611-631`) gets the same three rules,
written again from this text, not ported from the Rust (two independent checkers). `check_sites.py` lists `base` among
the stubs that are not accesses (`tools/fexc/check_sites.py:55`). The static cases run fexc with `--base-guards`
(`fexc-precision-*` add it beside `--push-run`, `verify_precision.py:1263`; `fexc-sites-slice1`).

`fexc-replay` and `fexc-replay-order` replay recorded requests, which carry no `--base-guards`: step B leaves their
replies as they are, and step A changes no fexc output.

## 6. Kill-tests

Step A: one run of `guest-sampler-check-sample` (bench family). The cross phase's iterations in its 8 s window rise at
least ten times (from about 23.8 M), `cold_exits` in the boot falls under 1% of the iterations, and judge c still passes.
Then one run of `guest-scimark32-sse2-sample`: `cold_exits` under 1% of the 6.9 M `patch` entries of section 2, LU's
Mflops in the case's output up 15% or more against this page's run (a sampled run against a sampled run on the M2 Max;
the pooled A/B measures it). Failing either: stop and report.

Step B, without a VM: a new static case `fexc-split-words` (fexc suite of `tests/static/family.toml`, a test in
`tools/fexc/test_slice1.py`) compiles the x86 bytes of `scimark32-sse2`'s hot loops (SOR `0x804c480`, FFT `0x8049ed0`,
Random `0x804befa`, LU `0x804cec0`, MonteCarlo `0x804ca11`; kept as hex in the test, with the binary and symbol they
came from) at their RIPs with the host's flags, with and without `--base-guards`, and prints each block's words to its
first exit and its split-check words, before and after. It fails unless SOR, FFT and Random each lose at least 4 words
and LU is unchanged except for its guard. Then one run of `guest-scimark32-sse2-sample`: split and probe words at or
under 3% of EL0 samples raw (from 10.4), and the new `base_retired` counter at or under 50 unit RIPs. Failing either:
stop and report.

## 7. Tests

Rerun (crossing stores, m2-task4-findings "Built: page-crossing stores without the trap", "Tests"): faults family
`faults@1-5` (page not present, read-only, supervisor, reserved bits on either side; 2 to 32 bytes; PUSH and a CALL's
push; read-modify-write forms; the canonical-gap straddles; first-page-first order), `loopfaults-*`, `probes@1-2`;
system `m2` (uncached memory on either side), `m3` (a device page on either side), `s4` (crossing stores with a write
breakpoint on the second page), `compat32` (the 32-bit wrap cases, `c32-x87-int64-cross`), `compat32-resident-limits`;
smp `vector-ac`, `vector-split-smp`; static `fexc-sites`, `fexc-sites-slice1`, `fexc-precision-0..3`,
`fexc-precision-32`, `fexc-replay`, `fexc-replay-order`, `fexc-unit`; the admission unit tests
(`split_checks_are_verified`, `push_run_alignment_checks_are_verified`, `probing_split_stubs_are_verified`,
`structure.rs:3026-3080, 3796-3880`); bench `guest-sampler-check-sample`. Then the gate once for the pooled change set.

New, each shown to fail on a mutation before it counts:

- A1 (faults family, QEMU-compared): a loop of crossing 8- and 16-byte stores in a block with a hot copy, the second
  page unmapped by the guest mid-loop, then mapped again: #PF exact, first page unwritten, the loop's result after.
  Mutation: the rewritten `adr` aimed at R' + 4 (skips the store).
- A2 (static or unit, host audit): a block copied, shared and reused after its hot copy was made; the audit accepts only
  the admitted or the hot `adr`. Mutation: `copy` keeps the rewritten word (the copy returns into the source's hot copy).
- B1 (admission unit tests, `structure.rs`): accepted, the SOR, FFT and Random shapes; refused, one mutation each of
  them: the guard's mask one bit short (`#3` for an 8-byte store), the index shift short (`lsl #2` for 8 bytes), imm not
  a multiple (`[x20, #4]`), N written between guard and store (`add x4, x4, #4`), a merge where one path has no guard,
  the store after a backward target, an `svc` between, the guard after a store of the same instruction, a `BASE` stub
  reached twice.
- B2 (`fexc-unit` or `test_slice1.py`): fexc's guard and elision shapes on x86 snippets (64-bit `[rbx+8]`, `[rbx+rcx*8]`;
  32-bit `[eax+esi*8]`, `[eax+0x4c]`; a 16-byte `movups [rax]`); `verify_precision.py` on their output. Mutation: fexc
  marks N aligned after a guard of the wrong width (verify_precision must report it).
- B3 (faults family, QEMU-compared): a misaligned base (rbx = 4 mod 8) whose 8-byte store crosses into a not-present
  page: the guard fires, the instruction is stepped, #PF exact, nothing written; a base aligned for the first calls
  and misaligned later; a guard at the second instruction of a unit after the first instruction's store (state at the
  stub); 32-bit `movsd [eax+esi*8]` with eax = 4 mod 8 crossing into a read-only page; 32-bit stores at 0xfffffff8 (no
  crossing, aligned) and 0xfffffffc (guard fires, the wrap path). Mutation: fexc's guard `and x0, xN, #3` for 8-byte
  stores (partial write on the fault); the resident's `BASE` arm without the step and without noting the RIP (the case
  times out).
- B4 (host unit test): an answer compiled with `--base-guards` is not served to a request without it; the pool refuses
  a `BASE` entry for a noted RIP. Mutation: the argument left out of the gating set.

## 8. Expected gain and estimate

Estimates, to be measured by the pooled A/B. Step A: LU in both SSE builds 1.3-1.6 times (the crossings' linker trips,
section 2), the other kernels unchanged; elsewhere unknown until `cold_exits` is counted (any unaligned vector store loop
that crosses pages). Step B on `scimark32-sse2`: the guards cover about 95% of the split-check samples (all but LU's);
chunk 28's ceiling gained about the checks' held share on `build`, `zstd` and `luajit` (`perf-plan.md:3229-3238`), so
about 5-9% of the benchmark's time if the guards hold (SOR and FFT most, 9-13% fewer words in their loops). On
`sqlite`, `luajit`, `startup32` and `build` the stores left after `--push-run` are mostly heap and struct stores at
aligned bases: perhaps half their split share, 1-3%.

Effort: step A half a day to a day, step B three to four days, together about four to five days with tests.

## 9. Correctness risks

1. Step A: a path that moves block code (copy, share, reuse, an audit) keeping the rewritten `adr`, which then returns
   into another block's hot copy. All four are listed in 4.1 and tested by A2.
2. Step B: a guard that does not cover an elided store on some path (a merge, a backward target, an `svc`, N or the
   address's index rewritten): a crossing store that writes its first part when the second page faults. The dataflow's
   meet, three independent checkers and B1/B3 are the defence; production does not run the code shape check.
3. Step B: a guard after a store of its own instruction: the step would apply a committed store twice (a
   read-modify-write twice). fexc emits only before the first store; admission checks it (rule 2).
4. Step B: a retired unit served again from the pool or the answers table: not inexact (the step makes progress), but a
   step per arrival. B4.
5. 32-bit code: alignment through FEX's 32-bit address adds and the 4 GiB wrap (section 3: the low bits survive mod
   2^32, an aligned w-block never straddles 2^32).

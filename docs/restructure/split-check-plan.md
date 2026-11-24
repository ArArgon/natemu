# Page-crossing stores: no linker trip when the probes pass (plan)

November 23, 2026, on `m2-sampler`; revised after review the same day (step B deferred, step A's restore points
completed). Design only: nothing here is built. One of the fixes the owner pooled
after the [EL0 sampler findings](el0-sampler-findings.md) ("Split checks and probes >= 3%": **build**): no A/B of its
own, one combined A/B and profile round at the end, correctness first, a kill-test.

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
else: its code"). The stub's `adr x0, R` is PC-relative, R being the word after the check (admission: the `adr`'s
target is the word after the `tbnz`, `crates/host/src/admission/structure.rs:2103-2106`; between it and the store only
x0/x1 are written), so R is the cold copy's word and the probe's `br x0` returns there. The rest of the iteration runs
in the cold code, whose exits are never patched: a link patches the hot copy's trampoline and leaves "its code's word
... as admitted" (`crates/abi/src/execution.rs:1386-1397`). So every crossing store ends its block at an `svc #LINK`;
the linker finds the link already made (`cache.rs:2292`, `link`: nothing unlinked, `Linked(0)`) and enters the hot copy
again. The x87 run end had the same problem and was fixed by re-pointing its return at the hot copy (`cache.rs:152-171`,
`run_return`).

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

For the deferred step B: if A is a multiple of w (w a power of two up to 16), then [A, A + w) lies inside one aligned
w-byte block, and w divides 4096, so it lies inside one page. In 32-bit code the same holds for the x86 address
A mod 2^32, since 2^32 is a multiple of w. A = N + (M << s) + imm is a multiple of w when N is a multiple of w, s >=
log2 w (whatever M is) and imm is a multiple of w.

## 4. The steps

Step A is the plan. Step B is deferred (review, November 23): its exactness argument holds, but the design is
incomplete and not lightweight; it is decided again after step A lands and `cold_exits` and the split share are counted
in the pooled profile round.

### 4.1 Step A: the probe returns to the hot copy (resident only)

**The hot word of an offset.** One helper, `cache::hot_at(b, o)`: for a block with a hot copy, the hot copy's address
of code offset o, `hot_body + o`, when ENTRY_OFFSET <= o < `hot_size`, or o == `hot_size` and an exit's trampoline
stands there in place (the case `split`'s `map` handles with `exit_at_h`, `cache.rs:120-130`: a store that is the last
word before the exit resumes at that trampoline); else none, and the cold word is used.

**The rewrite.** In `split` (`cache.rs:117`), after the point where it can no longer return false (after the trampolines
are written, beside the `run_return` rewrite at `cache.rs:158-162`), each probing split stub's `adr x0, R` in the cold
code becomes `adr x0, hot_at(b, R - code)` when that exists and `probe::adr_x0` (`crates/abi/src/execution.rs:2492`)
reaches it (the hot zone and the cold code share one 32 KiB segment, so it always does); otherwise the word stays. A
probing stub is a SPLIT site whose first word decodes as `adr x0` and whose second is a probe branch (as
`point_probes` finds them, `cache.rs:2148-2160`). `split` in `execution.rs:4508` resumes at `hot_at` of the word after
the store when it exists.

Why it is exact: the hot word at R' is the word at R moved by `moved_word`, which copies a non-branch word unchanged
(R is the word after the check: an x0/x1 write or the store, neither PC-relative), so the probe returns to the same
instruction with the same registers, and the words from R to the store are the same words in the same order, with no
resident entry between the probes and the store. Every resident return into the sequence still goes to the stub's
first word (`execution.rs:3993-3998`), which re-runs the rewritten `adr`. What reads R: `probe::stub_of`
(`crates/abi/src/execution.rs:2514`) compares the `adr`'s target with `d0 - code`, both relative to the cold code, so a
hot R' matches as R did; `cache::block_at` finds a block from a hot address (`cache.rs:3438-3447`); `probe::returns_to`
and `rearm_at` arm the stop in the block found from R' (`crates/abi/src/execution.rs:2478-2490`).

**The admitted word.** A rewritten `adr` stands for its admitted form `adr x0, code + (t - hot_body)`, t its decoded
target (in the hot copy); an `adr` whose target lies in the cold code is admitted as it is. Everything that moves or
compares a block's words must use that form (a path that kept the rewritten word would return into another block's
hot copy, or into whatever is at that distance: the main risk of this step):

- `share` (`cache.rs:1288-1345`) copies the template's raw words (`copy_record`, `:1324-1328`) and restores only the
  link points and the probe branches (`:1329-1335`) before `pic::eligible` and `pic::rewrite`: it must restore each
  rewritten `adr` in `at[]` there too. Without it every split block with a probing stub would carry a target outside
  its translation (`pic::proved` would mark it UNPROVED, or worse place it).
- `copy` (`cache.rs:1590-1650`) copies raw words and restores the `svc` words and the `run_return` word
  (`:1619-1636`): the same restore beside them. Its own `split` re-points the copy's word.
- `unlinked_word` (`cache.rs:1468-1482`), what the PIC proof reads: the admitted `adr`.
- the reuse check (`crates/runtime/src/execution.rs:1660-1680`): a rewritten `adr` compares as its admitted word, as the
  `run_return` word does there.
- the host's audit (`crates/host/src/execution.rs:1538-1556`) today checks only a probing stub's branch word; it takes
  any `adr x0` and checks no target (`:1549`). It gains a target check: map a hot target back to the code offset as
  above, then require the admitted target to be the word after the one `tbnz` that branches to the stub (or the
  admitted `adr` itself, for a block without a hot copy or one the rewrite left).

The pool is not involved: the host publishes fexc's object, not placed code (`cache.rs:1694`).

**A counter**, `cold_exits`: linker entries whose `svc` lies in the cold code of a block that has a hot copy
(`execution.rs:707`, `linker`). It is the kill-test's number, the input to step B's re-decision, and it tells whether
another cold return (an FS/GS operand check's resume at `b.code_va() + site.imm`, `execution.rs:3966-3979`) is worth the
same treatment.

Work: about a day with its tests.

### 4.2 Step B, deferred: alignment guards on any base

The design, kept for the re-decision. A store wider than a byte whose address is N + (M << s) + imm (N a register
x2-x30, s >= log2 w, imm a multiple of w, w <= 16, not a pair) gets, when N is not known aligned to w, `and x0, xN,
#(w-1); cbnz x0, K` (K a new stub kind `BASE`) instead of the split check, 2 words against 3-4; later stores on N in the
unit get nothing. Alignment levels per register (1-16 bytes) flow forward through guards, the stack check, I5a alignment
checks, `add|sub` by multiples and scaled registers, `mov`, `and` with a mask and writebacks, met by minimum at branch
targets, nothing at the entry or a backward target (generalising `aligned()`, `structure.rs:2146-2216`, and fexc's
`FexcAlignedAfter`, `fexc_sites.inc:55-79, 1141-1163`, carried by the fork's `FexcCarried`, `third_party/FEX`
`JITClass.h:597`). A fired guard steps the instruction and notes the RIP; noted RIPs are compiled without
`--base-guards`. Readings of the case's objects file: SOR's loop 56 to 51 words, FFT's 46 to 40, Random_nextDouble's
first part 73 to 64; LU's 16-byte stores at 8 mod 16 would retire their guard. Estimated gain on `scimark32-sse2` 5-9%
if the guards hold; 1-3% on `sqlite`, `luajit`, `startup32`, `build`. Three to four days.

What must be solved before it is built (the review's findings):

1. **Retirement must cover the resident's own templates.** A noted RIP can be served a guarded block again with no host
   request at all: `reused` finds a template (`crates/runtime/src/execution.rs:1246-1299`, `cache::template`,
   `cache.rs:1568-1578`) and places it by `copy`, `share` or an instance. That is one step per arrival, for ever; and
   under `REUSE_CHECK` a guarded copy compared with an unguarded reply halts (`execution.rs:1672-1679`). It needs a rule
   that refuses a template or translation holding a `BASE` site for a noted RIP, in every reuse path, the pool's
   included, and a kill-test that a retired unit is never entered again by any of them.
2. **fexc must find N from the emitted words.** Production admission does not run the code shape check
   (`structure.rs:38-56`), so the guard's base must be derived (or asserted) by fexc from the address words it emitted,
   as `address_in` reads them (`structure.rs:2081-2100`), not taken from the IR op: an IR shape that FEX lowers another
   way would otherwise guard the wrong register with nothing to catch it.
3. Smaller: `is_stub_kind` (`crates/abi/src/code_object.rs:45`) must include `BASE`; the noted-RIP bitmap (4,096 bits,
   hashed as `x87_varied_bit`) collides, so a collision takes guards from an unrelated unit (performance only, but to be
   sized); stepping the instruction at a fired guard re-executes a device load the unit already made before it, unless
   the guard is placed before the instruction's first access, not only its first store; and a fexc-level test of a
   forward merge where one path has no guard (fexc's own dataflow, not only admission's).

### 4.3 Considered, not planned

- **One check per loop iteration over a strided span** (one crossing check on [A, A + 16) for SOR's two stores). Exact
  when the span does not cross; when it does, some store in it may, and the fallback must complete each store exactly.
  A probing stub over the span is exact only if nothing between the probes and the last store can enter the resident,
  and SOR has loads in between (a load's shadow miss may reset the shadow). The exact fallbacks are a step (about 12 us,
  once per 256 iterations for a 16-byte span: costlier than the checks it saves) or a second copy of the body with the
  checks. Not lightweight.
- **Alignment carried across iterations** (a loop induction with a constant stride needing no guard per iteration). A
  natemu block is one iteration and its entry knows nothing; carrying facts across the back edge needs back edges inside
  blocks (the multiblock contract change in the guest-code review's section 6).
- **A cheaper check.** Chunk 28's finding stands (`perf-plan.md:3253`): `add; eor; tbnz` is the shortest flag-free test
  of "crosses 4 KiB". The forms tried here are no shorter: `and #0xfff` then a compare needs flags; the 8-aligned 16-byte
  case (crosses iff A & 0xff8 == 0xff8) takes `add #8; and #0xff8; cbz`, three words. The only word a check wastes is
  the `add x1` of `[xB, #imm]`, which only an elision (step B) removes. LU's 16-byte stores keep their check.
- The 32-bit stack check at each unit's first push (5.1% on i386-make): nothing cheaper exactly while a unit's entry
  knows nothing. Not in this plan.

## 5. Kill-test (step A)

One run of `guest-sampler-check-sample` (bench family): the cross phase's iterations in its 8 s window rise at least ten
times (from about 23.8 M), `cold_exits` over the boot falls under 1% of the phase's iterations, and judge c still
passes. Then one run of `guest-scimark32-sse2-sample`: `cold_exits` under 1% of the 6.9 M `patch` entries of section 2.
LU's Mflops in that case's output are reported beside this page's run as an indication only (one sampled run each, the
M2 Max; not an A/B): the pooled A/B measures the change. Failing the counter thresholds: stop and report.

## 6. Tests (step A)

Rerun (crossing stores, m2-task4-findings "Built: page-crossing stores without the trap", "Tests"): faults family
`faults@1-5` (page not present, read-only, supervisor, reserved bits on either side; 2 to 32 bytes; PUSH and a CALL's
push; read-modify-write forms; the canonical-gap straddles; first-page-first order), `loopfaults-*`, `probes@1-2`;
system `m2` (uncached memory on either side), `m3` (a device page on either side), `s4` (crossing stores with a write
breakpoint on the second page), `compat32` (the 32-bit wrap cases, `c32-x87-int64-cross`), `compat32-resident-limits`;
smp `vector-ac`, `vector-split-smp`; the PIC and reuse cases that run with the hooks host (`k1`, `pic-*`: delete
`target/hooks` first, m2-task4-findings "the two-word stub and shared probes"); bench `guest-sampler-check-sample`.
fexc's output does not change, so the static fexc cases and `fexc-replay` are unaffected. Then the gate once for the
pooled change set.

New, each shown to fail on a mutation before it counts:

- A1 (faults family, QEMU-compared): a loop of crossing 8- and 16-byte stores in a block with a hot copy, the second
  page unmapped by the guest mid-loop, then mapped again: #PF exact, first page unwritten, the loop's result after; and
  a crossing store whose next word is the block's exit (the `hot_at` edge at `hot_size`). Mutation: the rewritten `adr`
  aimed one word past R'.
- A2 (with the hooks host, as the reuse and PIC cases run): a split block with a probing stub, its hot copy made and the
  `adr` rewritten, then copied (`copy`), shared (`share`: the translation's copied words, `pic::proved` passing, and an
  instance of it entered and crossing a page), and checked by the reuse check; the host audit passes after each and the
  crossing store completes in every copy. Mutations: `share` without the restore (the translation's `adr` aims outside
  it: the audit or the proof refuses, or the crossing store returns to the wrong code), `copy` without it, the audit
  accepting any `adr` target.
- A3 (the counter): `cold_exits` in `guest-sampler-check-sample`'s judge, below 1% of the cross phase's iterations.
  Mutation: the rewrite switched off (the judge fails).

## 7. Expected gain and estimate

Estimates, to be measured by the pooled A/B. LU in both SSE builds 1.3-1.6 times (the crossings' linker trips, section
2), the other SciMark kernels unchanged; elsewhere unknown until `cold_exits` is counted (any loop of unaligned vector
stores across pages). Effort: about a day with its tests.

## 8. Correctness risks (step A)

1. A path that moves or compares block words keeping the rewritten `adr` (`share`, `copy`, `unlinked_word`, the reuse
   check, the audit): a probe that returns into another block's code. All five are listed in 4.1 and covered by A2.
2. The rewrite done before `split` can still fail: a block placed whole with an `adr` aimed at a hot zone it does not
   own. Done after the last `return false`, as `run_return` is.
3. A hot copy that does not hold R (R at or past `hot_size` without a trampoline there): `hot_at` returns none and the
   word stays cold, as today.

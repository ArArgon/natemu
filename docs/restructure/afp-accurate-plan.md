# Accurate floating-point mode on hosts without FEAT_AFP (M1, M2)

Status: plan, November 16, 2026, branch `afp-accurate` off `afp-fallback` (63a38db). Revised after an independent review
("accept with required changes", §9). No code yet. Background and evidence: [the AFP fallback plan](afp-fallback-plan.md)
(its §2 table, §3 owner notes of November 15 and 16, §6 measurements) and [the inventory](afp-inventory.md).

**What the owner decided (November 16).** natemu gets two floating-point modes:
- **fast**, the default. On a host without AFP it keeps only the cheap exact fixes (done: rows 2-10 of the AFP plan,
  known-zero upper bits, C unmasked, x87 masked DE for m32/m64, x87 NaN results) and documents the rest as deviations.
- **accurate**, opt-in at launch. It is part of the compile key, so fast-mode code is not changed by its existence.
  On a host without AFP it makes the remaining SSE/AVX cases exact:
  - **A:** NaN results, with **D** (the masked FMA inf×0 + QNaN IE flag) and RSQRT's NaN sign;
  - **B:** the SSE masked DE flag;
  - **C masked:** the masked UE flag in the MIN_NORMAL band (unless the M2 has usable FP traps);
  - **C under DAZ + FTZ:** the wrong result (0 instead of ±MIN_NORMAL).

  Later, accurate 80-bit x87 joins it on every host. Accurate mode's cost is measured and documented, not gated.

**This plan covers:** the test hygiene that comes first (chunk 0); accurate mode's frame (the option, the key, how it
reaches the host, fexc and the template pool, the startup output); and A, B, C masked and C under DAZ + FTZ. 80-bit x87
is a later plan of its own. The frame leaves room for it (§1) but this plan doesn't design it.

**Gates:**
- On the M4, both modes keep generated code, request bodies and compile keys byte-identical to today, step requests
  included.
- In M2 emulation (an M4 with `NATEMU_FEXC_FEATURE_OFF=afp`), fast mode's code is byte-identical to today. Each
  documented deviation must come out exactly as documented (chunk 0).
- In M2 emulation in accurate mode, every runner case and every QEMU corpus program passes. The only exception is the
  seven known QEMU departures in i8b, which are QEMU's errors, not natemu's.

## 1. The frame

**The option.**
- `--fp fast|accurate` on `natemu`, with `NATEMU_FP` as fallback. As with every clap `env` option, the flag wins over the
  variable. The default is `fast`.
- The test driver `natemu-host` (run-program and boot-linux) reads `NATEMU_FP`, so every runner selects the mode the
  same way.
- Any other value is refused with one line. The mode is fixed for the whole process.

**Where it lives.** A new `Cpu` field, set once per vCPU by the host in the same place and the same way as `no_afp`
(before the first lookup). Every copy of the bank carries it, and the resident never writes it. It has to be in `Cpu`
because the resident computes `code_context` for its own lookups. The field and the new stub kind (§3) are recorded in
the abi contract.

**The key.** `code_context` bit 32 is "accurate without AFP", that is, the mode bit AND `no_afp`.
- It follows the rule the DAZ and FTZ keys already follow: a bit is set only where the code differs.
- The word is 64 bits wide everywhere it travels: the resident's cache, the template pool's context and the publication
  tag.
- Within one process the bit is redundant, because the mode doesn't change at run time. It is kept for three reasons:
  it keeps the invariant the identity test checks (every argument that changes code is derived from `Cpu` and keyed); it
  costs nothing; and it keeps replay and comparison tools that run both modes in one process honest.

**How fexc hears it.**
- `--accurate` and, for steps, `--exact-step` (§3) are sent only when bit 32 is set. So accurate mode changes no request
  on an AFP host, step requests included.
- Both are gating arguments, so the host's in-memory answer cache keys on them. The persistent compile cache keys on the
  request body, so it keys on them automatically.
- fexc refuses either argument when `afp=1`, and resets both flags in `ResetGlobals`. Otherwise its serve mode would
  carry one request's mode into the next and poison the persistent cache across modes.
- The identity test covers block requests and step requests.

**Startup output.** Under the owner's new direction (built separately, on branch `startup-lines`), `; host -afp` goes
away and each decision gets its own line.
- This plan sets the value of the fp line: `natemu: fp accurate`, or `natemu: fp fast` with the degraded note where AFP
  is missing.
- The degraded note's wording is taken from chunk 0's deviation list, so the README, the tests and the startup line name
  the same deviations.
- The program driver's JSON result gains two fields, `afp` (what the probe decided, after `NATEMU_FEXC_FEATURE_OFF`) and
  `fp`. Chunk 0's runners judge from these fields, not from their own environment.

**Accurate mode on the M4 changes no code.** All four items are already exact in hardware when AFP is present, so bit 32
stays clear. Refusing the option on an AFP host would make scripts depend on the host, so it is accepted, and the fp line
reports the chosen mode.

**How 80-bit x87 joins later.** It adds its own key bit (bit 33, "accurate x87": the mode bit alone, on every host) and its
own fexc argument. From that point accurate mode changes M4 code too. Nothing in this plan has to move for it.

## 2. Chunk 0: test hygiene

The owner's words: "Can you disable the expected discrepancies or test them only in accurate mode? We need test
hygiene." In M2 emulation, fast mode today reports failures that are documented deviations (latest run
`afp-cheap-check-20261116-01`):
- run_i5c: 21 of 417;
- run_i5a_de: 98 of 351;
- run_i8_faults: 8 of 846.

All 127 runner failures are the SSE masked DE flag. In the QEMU corpora, i8 fails 39 of 136 and i8b 77 of 336. All 116
are NaN-result causes: the default NaN's sign, negations flipping a NaN's sign, the FMA NaN order, and FMA inf×0 + QNaN.

**One registry of deviation classes.** A small Python module beside the runners is the single machine source. It lists
fast mode's documented deviations on hosts without AFP by short name:
- `masked-sse-de` (B);
- `nan-results` (A, including RSQRT's sign);
- `fma-invalid-flag` (D);
- `band-ue-flag` (C masked);
- `daz-ftz-band` (C under DAZ + FTZ).

For each class the module holds a one-line description and its judging rule. The README gains a short list "Floating point
on Macs without AFP (fast mode)" using the same names, and that list is the human record. A unit test, run in the suite's
existing pytests step, checks that the two lists name the same classes. The startup line's degraded note is taken from
these descriptions. A class with no tagged case today still appears in both lists.

**Runner cases are tagged mechanically, never by hand.**
- run_i5c has many cases with custom check functions over literal MXCSR values. They are restated so that every SIMD case
  carries a structured expected MXCSR, and the custom checks keep only the parts that aren't MXCSR.
- One registry function, called by run_i5c, run_i5a_de and run_i8_faults, tags a case `masked-sse-de` when all of these
  hold:
  - it is an SSE/AVX case;
  - its expected MXCSR has DE set;
  - DM is masked;
  - the case's starting MXCSR has DE clear.

  The last condition matters: with DE preset, "DE missing" would let through a bug that clears the sticky flag.
- This matches today's 127 cases (chunk 0 checks the count), and future cases tag themselves. Cases with DE unmasked are
  exact in fast mode and are never tagged.

**Corpus programs are listed by name, keyed by corpus directory.** For example `i8-corpus-20261030-01`: names repeat
between i8 and i8b, and the key also ties the list to one corpus version.
- The list comes from the offline model, which reproduces natemu's M2-emulation output on all 472 programs. A program is
  tagged when every cause the model finds for it is a NaN class (`nan-results`, plus `fma-invalid-flag` where that
  applies).
- The model lives only in an old session's scratch directory today. Chunk 0 copies it into the evidence directory and has
  it write, per tagged program, its predicted fast-mode final state.
- Chunk 0 checks that the tagged sets are exactly today's 39 and 77 failures.

**The rule (the plan's choice; the owner may overrule).** It applies only when a run's own report says `afp` false and
`fp` fast. On the M4, in either mode, and in accurate mode anywhere, every case is judged as today. When the rule applies:
- A tagged case is judged against its documented deviated result. For runner cases that is the exact MXCSR with DE
  cleared, everything else exact. For corpus programs it is the model's predicted state.
- Anything else fails, including an exact result. In fast mode without AFP an exact result means fast-mode code changed,
  which is itself a finding.

This is stricter than skipping, and it stays within the owner's "disable or test only in accurate mode". The deviated
cases are still not counted as failures, and accurate mode judges them fully.

A driver error or a stop still fails. Each runner prints one more count, for example
`417 cases, 396 as expected, 0 not, 21 deviated as documented (masked-sse-de)`. The exit status depends only on the
failures.

The i8b judge keeps its separate list of seven QEMU departures. `run_suite.py` needs no mode logic of its own:
environment variables already pass through to every step, and it copies each step's "deviated" line into its results
note. The `m4_bug` marks in run_i5c go away with the concurrent M4 fix, and this chunk leaves them alone.

**Gate:**
- On the M4, every runner and corpus passes unchanged.
- In M2 emulation, fast mode: every runner exits 0, and the deviated counts are exactly 21, 98, 8, 39 and 77.
- A unit test of the judging function: an untagged failure, an extra DE, a cleared preset DE, a wrong value in a tagged
  case and an exact tagged case all fail.

## 3. The shared mechanism: test the result, step the instruction exactly

The measurements rule out cheap per-block detection:
- reading FPSR once per block costs +72% on c-ray's hot block, and per operation ×53;
- a single never-taken branch per FP operation already costs +77%;
- the inline guard with a skipped fix-up cost +96% on the cray benchmark.

The cheapest exact shape measured is the reference branch's NaN guard with out-of-line stubs (`afp-c4a-stub`, not merged):
- `fcmp` + `b.vs` where NZCV is dead, else an integer test after `fmov`;
- packed results reduced with `fmaxp`/`fmaxv`;
- at the stub, the resident steps the instruction, compiled differently.

Natively it costs:
- c-ray's hot block: +170% (2.72 → 7.35 ns);
- a packed block: +139%;
- a LuaJIT-like scalar block: +66%.

Of that, +38% is the temporary and move that keep the state from before the instruction. Every guarded form keeps them,
including VEX three-operand forms whose destination is separate. Without them the destination would be written before a
step that can still fault or run other bytes:
- another vCPU unmapping a memory operand's page gives a #PF with the destination already written;
- with cross-modifying code, the step runs the new bytes over a destination the old instruction wrote.

All four items share this mechanism.

**The invariant: no guest register is written before the instruction's last test.** A stub must see the state from
before the instruction, so the invariant is stated and tested per form:
- 256-bit forms: the second half's result is not written before the second half's test;
- scalar forms: the upper bits;
- FMA with destination = addend: chunk 3a found FEX's scalar FMA writing a register that still held a source;
- DPPS/DPPD and HADD/HSUB: later steps.

The gate puts NaN or band values only in a high half, or only in the last step, and makes the destination alias each
source in turn.

**The exact step.** In accurate mode every step is compiled with `--exact-step`. That compile has no result test. It has:
- the fix-ups inline (NaN rules, §4 A);
- the one operation's FPSR isolated: saved, cleared, read and merged afterwards, as x87 instructions already do;
- the band decision (§4 C);
- under DAZ + FTZ, its own FPCR (§4).

A step is one instruction, so the fix-ups use ordinary allocator values instead of the hot block's VTMP1/VTMP2 budget;
register pressure is low. Steps also run for self-modifying code, shadow misses and plain steps. A step that cannot be
compiled stops the guest. So before chunk 3 merges, an exhaustive test compiles `--exact-step` for every guarded form
under every key combination (DAZ, FTZ, each unmasked set, AVX on/off) and admits it, with no spill.

**A stub hit costs a host round trip, about 1.15-1.5 µs.** The stub banks the state, `step` requests the instruction
from the host (a hypervisor call), and the step block is placed uncached in the step slot. An FP operation costs about
1 ns. Hits are rare for NaN results in ordinary code, but not for zero and flushed results (§4 C). Under DAZ + FTZ every
correctly flushed result hits, so the round trip sits on the normal path of gcc `-Ofast` code.

So **a resident-cached exact step is in scope** (chunk 3). A result stub's step block is kept in the cache, and its key
works both ways:
- **The step marker is part of the lookup key.** The stub looks up only step-marked blocks, and normal dispatch, linking
  and the template pool never find them. Otherwise the stub would find the guarded block at that RIP and loop, or
  dispatch would run a one-instruction block.
- **The step lookup goes through the same mapped-source check as any block,** so a remap to another source page misses.
- **The block keeps the STEP flag:** it is never linked, `code_written` never re-steps it, and a NaN stub inside it halts.

A second hit avoids the host round trip but is still a stub: a breakpoint, resident entry, bank, lookup and entry, about
0.1-0.4 µs by the earlier measurement of kept system instructions. Self-modifying-code steps stay uncached, because their
bytes are about to change. Measuring first was rejected: under DAZ + FTZ the storm is by design, not an accident.

**The range test.** Multiplies, divides, FMAs and narrowing conversions are the only operations that can raise UFC
without AFP (§4 C). For them one comparison catches NaN or tiny. Take the magnitude's bits u. Then
u − (MIN_NORMAL + 1), wrapping, is at least INF − MIN_NORMAL exactly when u is 0, a denormal, ±MIN_NORMAL or a NaN.
- Packed, this is `shl` #1 (which drops the sign, so both constants double), `sub`, `cmhs`, then the same reduction and
  branch: one or two SIMD operations more than the NaN test.
- **Zero exclusion (decided):** a ±0 result lane is not a hit when one of these holds, because the result is then exact
  and raises no UFC:
  - a multiplicand lane is ±0 (MUL, FMA's product, DPPS products);
  - the dividend is ±0 (DIV);
  - the source is ±0 (narrowing conversions).

  That costs about two SIMD operations more on those ops.
- Other operations keep the NaN test.

The per-operation-class fix-up table of the AFP plan (§2.1) must be reviewed before A is built. It now applies inside
`--exact-step` compiles.

## 4. The four items

| Item | Exact design | Where the code goes | Cost estimate (hot path) | Admission |
| --- | --- | --- | --- | --- |
| A, NaN results | result test on ADD SUB MUL DIV SQRT, HADD/HSUB, ADDSUB, FMA, DPPS/DPPD steps, RSQRT; the step applies the fix-up table | fexc shim (the reference branch's `FexcNanCheck`, rebased), resident stub handler and step cache, host step arguments | measured: +66% to +170% on FP-dense blocks | the stub kind and its exact test (as on the reference branch); the exhaustive step test |
| D, masked FMA inf×0 + QNaN IE | row 10's sanitizing of multiplicands with a NaN addend, emitted also with IE masked | fexc shim (existing row 10) | measured 7-8 instructions per FMA | none new |
| RSQRT's NaN sign | RSQRT joins A's guarded set, unary rule (the input's NaN quieted, else the negative default NaN) | fexc op table | inside A's figure | none new |
| B, masked SSE DE | row 7's software denormal-operand test, emitted also with DE masked (not under DAZ); a hit skips forward into `mrs`, `orr` IDC, `msr fpsr` | fexc shim (existing row 7), shaped like fast mode's x87 masked DE | measured 8-20 instructions per op, FMA 44: about 1.5-3× on FP-dense code (estimate) | an FPSR write inside a forward-skipped span, under x87's fix-up rule (no branch may target an access) |
| C masked, MIN_NORMAL band | range test with zero exclusion; UFC removed before banking; the step decides UFC with fast mode's tininess model; the x86 UE kept in MXCSR, FPSR.UFC kept clear | fexc shim (range test, step's isolated check), resident (masked disposition in the existing tininess path, UFC removal) | estimate +10-30% over A on multiply-heavy blocks, plus stub hits on tiny results | the range test's shape |
| C under DAZ + FTZ | hardware FZ stays in the block; the range test catches every flushed result; the step writes FPCR with FZ = 0 itself and uses the software flushes (rows 4 and 5) | fexc (step's `msr fpcr` and rows 4 + 5 together), admission | the test is C masked's; every flushed result is a stub hit, cheap only with the cached step | `msr fpcr` only in `--exact-step` compiles, restored before every exit |

**A.** These are the ops the reference branch guarded, plus RSQRT, which it left out. MIN/MAX, conversions, compares
and ROUND are not guarded; they are already right (AFP plan §2.1). D's spurious IE has to be prevented before the
operation, not repaired at the stub. By the time the stub runs, FPSR already holds the operation's IOC, and an IOC set
earlier can't be told apart from it. Row 10's sanitizing prevents it, and the NaN guard then fixes the result value.

**B.** Without AFP, IDC is never raised with FZ = 0, so writing it into FPSR is exactly the M4's state. The existing flag
mapping then reports DE with no other change. Row 7's test already carries x86's precedence: no DE beside a NaN operand,
a ZE, a negative SQRT, inf×0 or inf−inf, and never for half-precision sources. It is reviewed and passes on the M4.

The research's load-only shape (+43-55%) is not exact as it stands: values live at block entry and denormal results
produced inside the block would also need tests. A complete version is a later optimization, not part of this plan.

**C masked.** Without AFP, Arm judges tininess before rounding and x86 after. A wrong UE can therefore only come from a
result that rounds to exactly ±MIN_NORMAL. A correct UFC comes only with a result that is 0 or a denormal, and only
from multiplies, divides, FMAs and narrowing conversions: adds and subtracts give exact tiny results, square root never
gives a tiny one, and RCP and RSQRT run quietly.

FPSR is sticky, so the stub couldn't tell this operation's UFC from an earlier one. In this mode FPSR.UFC is therefore
kept clear:
- **Removal before banking.** Banking ORs the frame's FPSR into MXCSR; under FTZ, row 3 also turns UFC into PE. So at
  every stub that can follow a UFC-raising operation, the resident removes UFC from the frame's FPSR before banking. That
  means the result stub, and the unmasked-exception check stub. The range test comes before the instruction's check, so a
  check stub after a range-tested operation never sees an unexamined UFC.
- **The step decides.** The step's isolated FPSR gives this operation's UFC alone. The integer model of tininess after
  rounding decides each lane, as fast mode's unmasked path already does. The difference is the outcome: UE goes into the
  MXCSR flags the resident keeps, the step ends with FPSR.UFC clear, and the instruction resumes with no #XM.

The chunk verifies two assumptions:
- generated STMXCSR and the resident's flag reads OR the stored MXCSR flags with FPSR, as the resident's do today;
- LDMXCSR and FXRSTOR put a set UE into MXCSR, not into FPSR.

**C under DAZ + FTZ.** With FZ = 1 the band result is flushed to 0, and a correct flush also gives 0 with UFC. The range
test catches both. The hot block still runs under FZ = 1: every entry sets FPCR from MXCSR.

So the exact step writes FPCR itself: `msr fpcr` with FZ = 0 before the operation, and the block's value again before
every exit. Stubs need no restore, because the resident refills FPCR at every entry. That is the same shape fexc already
emits for an instruction's own rounding mode around ROUND. Admission allows it only in `--exact-step` compiles. It pins
the exact words: the value read with only FZ cleared, so no trap-enable bit can be set.

The step then recomputes the result with the software flushes (rows 4 and 5), which FTZ alone already uses correctly, and
its UFC goes through the band decision. Rows 4 and 5 have never run together, so their combination is tested natively
against the M4's AFP code before it is relied on.

Dropping FZ = 1 from the hot code was considered. It measured at least +90%, with a research proxy of +528%/+969%
(overstated), and it buys little once the cached step exists.

**Not in this plan:** the x87 UE band, and the x87 two-NaN choice (an existing recorded deviation on every host). Both
belong with accurate 80-bit x87.

**Overall estimate.** FP-dense SSE/AVX code roughly 3-8× slower than fast mode on a host without AFP, A and B together
being the upper end. Integer code is unchanged. Code that produces many NaNs or tiny results pays for stub hits on top
of that. Chunk 6 measures both.

## 5. If the M2 has usable FP traps (the owner's pending test)

On the M4 with AH = 0, FPCR trap enables are precise: the destination and FPSR are untouched. They cost +4.3% when
enabled. The M2's records list synchronous FP exceptions as M4-only, so the trap path may not exist there. The owner's
run of the two trap tests in the research directory decides it, on the M2 and inside HVF.
- **No traps:** this plan as written.
- **Traps:** per the owner's November 16 note, C masked moves to fast mode with a design of its own (a precise UFE trap;
  the resident replays the one Arm instruction with traps off and decides the band lane). Chunk 4 then shrinks to what
  chunk 5 still needs, because FZ = 1 flushes never trap: the range test for DAZ + FTZ code, UFC removal and the band
  decision in steps.

Traps could also cut A's cost. The invalid-operation trap catches every default NaN and SNaN case. Only the FMA forms and
the lowerings that negate a quiet NaN without raising IOC would still need the result test. That is a separate design,
asked as decision 1.

## 6. Chunks, gates and order

Every chunk keeps the standing rules:
- the M4 stays byte-identical (replay of both recorded streams against the new fexc, 0 differences);
- the identity unit test (every MXCSR value × FCW.RC × FCW masks) is extended with the mode and with step requests: with
  AFP, `code_context`, `x87_args`, the step arguments, the gating values, `fpcr` and `mxcsr_flags` are identical in fast
  and accurate mode;
- fast mode in M2 emulation stays byte-identical (replay with `afp=0` and no `--accurate`, 0 differences);
- benchmarks only at completion, VMs only from the main session, the full suite once before merge.

Native oracle tests compare accurate `afp=0` code with the M4's AFP code on M4 hardware: random operands with NaN,
denormal, band and zero classes, results and flags bit-identical. The fast-mode x87 work used the same method.

| Chunk | Content | Gate |
| --- | --- | --- |
| 0. Test hygiene | registry, README list and parity test; run_i5c's structured MXCSR; driver reports `afp` and `fp` (`fp` always fast until chunk 1); the three runners and corpus judging; the model and its predicted states copied to evidence | §2 |
| 1. Frame | option and variable, `Cpu` field, key bit 32, `--accurate` and `--exact-step` (accepted, no effect yet), `ResetGlobals`, the fp line's value, driver's `fp`, abi contract | identity test with the mode and steps; M4 replay 0 differences; M2 emulation fast replay 0 differences; a boot in each mode prints the right fp line |
| 2. B | row 7's test with DE masked, the FPSR write, admission rule | native oracle for DE; in M2 emulation, accurate: run_i5a_de, run_i5c and run_i8_faults pass with no deviation counted |
| 3. A with D and RSQRT | the reference branch's stub rebased; the per-form invariant; fix-up table reviewed first, then the fix-ups in `--exact-step` with allocator values; the cached exact step; row 10 with IE masked; RSQRT guarded | the exhaustive step-admission and no-spill test; invariant cases (high half only, last step only, destination aliasing each source); native oracle (NaN classes); a run_i5c loop taking the stub many times (second hit from the step cache; a guarded block at the same RIP is never found by the step lookup, nor a step block by dispatch); in M2 emulation, accurate: i8 and i8b fully pass (i8b: only the seven departures) |
| 4. C masked | range test with zero exclusion, UFC removal before banking, masked disposition at the stub, the STMXCSR, LDMXCSR and FXRSTOR checks; starts after the owner's trap test | run_i5c band cases (every rounding mode, f32 and f64, UE set earlier by LDMXCSR, a packed op with a NaN in one lane and a band result in another, a band lane beside real underflow); zero-exclusion cases; native oracle |
| 5. C under DAZ + FTZ | the step's own FPCR and its admission rule; steps with rows 4 + 5 combined | native oracle of the combination; run_i5c DAZ + FTZ band and flush cases; the corpora's DAZ/FTZ programs in accurate mode |
| 6. Cost, records, M2 run | one benchmark round in M2 emulation, fast vs accurate (cray, luajit, luajit-interp, zstd, sqlite, avx; 5 interleaved runs), plus an existing FP benchmark program built with `-Ofast` (DAZ + FTZ, flush-heavy), with stub hits counted; README: the option, its cost, fast mode's deviation list; the owner's M2 run of the probe and runner subset in both modes | numbers recorded here and in the investigation record's progress log; the M2 matches M2 emulation |

**Order and why.**
- Chunk 0 is first (owner).
- The frame comes next, because every item needs it.
- B is next because it is mostly reuse. It exercises the frame end to end, and it turns three runners fully green in
  accurate mode early.
- A is the largest. It needs the fix-up table review, and it brings the stub, the cached exact step and the invariant
  that C needs.
- C masked waits for the owner's trap test.
- C under DAZ + FTZ uses C masked's test and decision.
- Chunks 2 and 3 are independent and may swap.
- Chunk 1's fp-line gate needs branch `startup-lines` merged first.

## 7. Decisions for the owner

1. **If your M2 test shows usable FP traps:** should accurate mode also use the invalid-operation trap to cut A's cost?
   That would be a separate plan: the result test would stay only on FMA forms and on lowerings that negate a NaN. C
   masked moving to fast mode is already your decision.
2. **The standing suite:** should the full suite gain two FP groups in M2 emulation? Both would run run_i5c, run_i5a_de,
   run_i6_faults, run_i8_faults, the FEX ASM suite, i8 and i8b: one in fast mode, judged with deviations, and one in
   accurate mode, judged fully. That adds their run time to every merge. Without them, accurate mode is checked only by
   its own chunks.

Decided here, with reasons in the text; the owner may overrule any of them:
- the hygiene rule: tagged cases judged against their documented deviated result;
- the option's spelling and precedence;
- no M4 code change;
- key bit 32 kept although it is redundant within one process;
- the stub design with a cached exact step;
- the zero exclusion;
- B with the existing test;
- hardware FZ kept in hot code under DAZ + FTZ, with the step setting its own FPCR.

## 8. Risks

- **Stub hits.** Even cached, each hit is a resident entry, a bank and a lookup (about 0.1-0.4 µs). Code with many NaN or tiny results that survive the
  zero exclusion pays per hit. Chunk 6 counts them.
- **The UFC-clear rule** depends on every UFC-raising operation being range-tested. A missed lowering (a new FEX op that
  multiplies) would make the band decision wrong without any sign. Mitigations: the op table is checked against FEX's
  IR ops at build time (an op that can raise UFC must be listed), and the native oracle covers each class.
- **The cached step** is new resident machinery beside the self-modifying-code step. It must never serve a block whose
  bytes changed: it is invalidated with its page like any block.
- **Fork changes.** All shim and FEX changes go on the fork's `natemu` branch, under the same rules as the fast-mode
  work (owner-authored commits, nothing offered upstream).
- **The reference branch has drifted:** it predates C unmasked and the x87 fixes. Rebasing it is part of chunk 3, and
  its earlier review doesn't carry over.

## 9. Review response (independent review, November 16)

| # | Finding | Answer |
| --- | --- | --- |
| 1 | A stub hit is a host round trip (≈1.15-1.5 µs); zero, flushed and NaN results storm; the estimates ignore it; A×B may reach ~8× | §3: the cached exact step is in scope (the DAZ + FTZ storm is by design); the zero exclusion is decided; chunk 6 adds an `-Ofast` workload and counts hits; estimate now 3-8× |
| 2 | Nothing sets the step's FPCR: every entry sets FZ = 1 | §4: the step writes FPCR itself (precedent: ROUND's own rounding mode), admitted only in `--exact-step` compiles and restored before every exit |
| 3 | Banking ORs the spurious UFC into MXCSR (and PE under FTZ) before the step | §4 C: UFC removed from FPSR before banking at the result and check stubs; the gate has a packed op with a NaN lane and a band lane |
| 4 | "No guest register written before the last test" must hold per form | §3: stated per form; the gate has high-half-only, last-step-only and aliasing cases |
| 5 | A step that needs a spill stops the guest; steps also serve SMC, shadows and plain steps | §3: an exhaustive admission and no-spill test of `--exact-step` before chunk 3 merges; fix-ups use allocator values in steps |
| 6 | `--exact-step` on the M4; step requests in the identity test; `ResetGlobals`; flag vs variable | §1: both arguments only with bit 32; identity test covers steps; flags reset; `--fp` wins |
| 7 | Tag only with DE clear at the start; structured expected MXCSR in run_i5c | §2: both adopted; the tags stay derived |
| N1 | A tagged case that comes out exact should fail | §2: adopted |
| N2 | Judge tagged corpus programs against the model's prediction | §2: adopted; predicted states stored as evidence |
| N3 | `shl #1` doubles both constants | §3 |
| N4 | The temporary and move are needed only when the destination is a source | §3; reverted in round 2 (R2-1) |
| N5 | Records: the abi contract and the investigation record | §1, chunk 6 |
| Q1 | Make the hygiene rule a plan decision | §2, §7 |
| R2-1 | Second round: skipping the temporary breaks the invariant (a #PF from an unmapped page, or cross-modifying code, after the destination is written) | §3: every guarded form keeps the temporary |
| R2-2 | The cached step's key must work both ways | §3: the step marker is part of the key, both directions; the mapped-source check; the STEP flag kept |
| R2-N | Pin the step's `msr fpcr` words; stubs need no restore; a cached hit is ~0.1-0.4 µs, not a plain branch; chunk 1 depends on `startup-lines` | §4 C under DAZ + FTZ, §3, §6 order |
| Owner | Startup output becomes one line per decision | §1: this plan sets the fp line's value; the degraded note uses chunk 0's descriptions |

## 10. Progress

| Date | Chunk | State | Evidence |
| --- | --- | --- | --- |
| Nov 16 | plan | written; reviewed (accept with required changes); revised (§9) | this file |
| Nov 16 | 0. Test hygiene | **done, merged into afp-fallback** (independent review accepted; its one gap fixed: the tagged known i8b departures must be deviated without AFP). Tagged: 21 (run_i5c), 98 (run_i5a_de), 8 (run_i8_faults), i8 39, i8b 73 (four of i8b's 77 are known QEMU departures with no M2 cause) | VM `afp-accurate-c0-check-20261116-01`: M4 all pass; M2 emulation 0 failures, the tagged counts deviated as documented. Full suite on afp-fallback 171/173 (`afp-fallback-suite-20261116-01`/`-02`) |
| Nov 16 | 1. Frame | **done** (independent review accepted, no findings; mode separation traced through every key, mutations caught). `--fp`/`NATEMU_FP`, `Cpu::accurate_fp`, key bit 32 only for accurate mode without AFP, fexc `--accurate`/`--exact-step` (no effect yet, reset per request, refused with AFP), the `fp:` startup line, the driver's real `fp`, contract 67. On a host without AFP accurate mode prints `accurate mode` though chunks 2-5 are not built: until then accurate runs fail the tagged cases strictly (intended). For chunk 3: in-memory answers match on the gating bits fexc reports read, so fexc must mark `--exact-step` read wherever it changes code (test it then) | fexc `natemu-raw/afp-acc-c1-fexc-20261116-01` (SHA-256 cfa2582b...); replay 0 differences (M4 and `afp=0`, both streams); host 405, abi 115, test_slice1 71, test_serve. VM `afp-acc-c1-check-20261116-01`: boots print the right line in all four settings (M4 fast/accurate, M2 emulation fast/accurate); run_i5c in M2 emulation accurate reports `afp` false, `fp` accurate and fails the 21 masked-DE cases strictly (this is the only test that catches a missing `Cpu::accurate_fp` assignment) |
| | 2. B (masked SSE DE) | | |
| | 3. A with D and RSQRT | | |
| | 4. C masked | | |
| | 5. C under DAZ + FTZ | | |
| | 6. Cost, records, M2 run | | |

# Accurate floating-point mode on hosts without FEAT_AFP (M1, M2)

Status: plan, November 16, 2026, branch `afp-accurate` off `afp-fallback` (63a38db). No code yet. To be reviewed
independently before chunk 0 starts. Background and evidence: [the AFP fallback plan](afp-fallback-plan.md) (its §2
table, §3 owner notes of November 15 and 16, §6 measurements) and [the inventory](afp-inventory.md).

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
reaches the host, fexc and the template pool, the startup line); and A, B, C masked and C under DAZ + FTZ. 80-bit x87 is
a later plan of its own. The frame leaves room for it (§1) but this plan doesn't design it.

**Gates in one paragraph:**
- On the M4, both modes keep generated code, request bodies and compile keys byte-identical to today.
- In M2 emulation (an M4 with `NATEMU_FEXC_FEATURE_OFF=afp`), fast mode's code is byte-identical to today, and its
  documented deviations are counted, not failed (chunk 0).
- In M2 emulation in accurate mode, every runner case and every QEMU corpus program passes. The only exception is the
  seven known QEMU departures in i8b, which are QEMU's errors, not natemu's.

## 1. The frame

**The option.** `--fp fast|accurate`, environment fallback `NATEMU_FP`, default `fast`, on `natemu`. The test driver
`natemu-host` (run-program and boot-linux) reads the same variable, so every runner selects the mode the same way. Any
other value is refused with one line. The mode is fixed for the whole process.

**Where it lives.** It is a new `Cpu` field, set once per vCPU by the host in the same place and the same way as `no_afp`
(before the first lookup). Every copy of the bank carries it, and the resident never writes it. It has to be in `Cpu`
because the resident computes `code_context` for its own lookups.

**The key.** `code_context` bit 32 is "accurate without AFP", that is, the mode bit AND `no_afp`. It follows the rule the
DAZ and FTZ keys already follow: a bit is set only where the code differs. The word is 64 bits wide everywhere it travels
(the resident's cache, the template pool's context, the publication tag). Today the bit is redundant within one process,
because the mode doesn't change at run time. It is kept for three reasons:
- it keeps the invariant the identity test checks: every argument that changes code is derived from `Cpu` and keyed;
- it costs nothing;
- it keeps replay and comparison tools that run both modes in one process honest.

**How fexc hears it.**
- `x87_args` adds `--accurate` exactly when bit 32 is set. It also becomes a gating argument, so the host's in-memory
  answer cache keys on it. The persistent compile cache keys on the request body, so it keys on the mode automatically.
- fexc refuses `--accurate` when `afp=1` (the same rule as `--daz` and `--ftz`).
- Steps compiled in accurate mode also carry `--exact-step` (§3). That is a gating argument too, so a step answer is never
  reused for a block.

**The startup line.** In accurate mode the boot line ends `; fp accurate`, after `; host -afp` where that is printed. Fast
mode prints nothing new, so today's line and the network runner's assertion stay byte-identical. The program driver's JSON
result gains two fields, `afp` (what the probe decided after `NATEMU_FEXC_FEATURE_OFF`) and `fp`. Chunk 0's runners
judge from these fields, not from their own environment.

**Accurate mode on the M4 changes no code.** All four items are already exact in hardware when AFP is present, so bit 32
stays clear and no request carries `--accurate`. Refusing the option on an AFP host would make scripts depend on the
host, so it is accepted, and the startup line says the mode was chosen.

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
existing pytests step, checks that the two lists name the same classes. A class with no tagged case today still appears
in both lists.

**How a case or program carries its class.**
- *Runner cases* are derived, not hand-tagged. The registry has one function, called by run_i5c, run_i5a_de and
  run_i8_faults: an SSE/AVX case whose expected MXCSR has DE set while DM is masked is tagged `masked-sse-de`. This
  matches all 127 cases, and future cases tag themselves. Cases with DE unmasked are exact in fast mode (row 7) and are
  never tagged.
- *Corpus programs* are listed by name, keyed by corpus directory (for example `i8-corpus-20261030-01`). Names repeat
  between i8 and i8b, and the key also ties the list to one corpus version. The list comes from the offline model, which
  reproduces natemu's M2-emulation output on all 472 programs. A program is tagged `nan-results` when every cause the
  model finds for it is a NaN class (`fma-invalid-flag` where that applies too). Chunk 0 checks that the tagged sets are
  exactly today's 39 and 77 failures. The model lives only in an old session's scratch directory today, so chunk 0 copies
  it into the evidence directory, and the list can then be regenerated.

**The rule.** It applies only when a run's own report says `afp` false and `fp` fast. On the M4, in either mode, and in
accurate mode anywhere, every case is judged as today.

When the rule applies, my recommendation is:
- *Runner cases:* judge against the documented deviated result. The case passes if its result is either exact or
  exact-except-DE-missing. An extra DE, any other flag, a wrong fault or a wrong value is still a failure. This keeps
  everything else in those cases tested; most of them check other flags and faults too.
- *Corpus programs:* not judged. A deviated final state can't be computed without the model, and accurate mode judges
  these programs fully anyway.

In both cases a driver error or a stop still fails. Each runner prints one extra count, for example
`417 cases, 396 as expected, 0 not, 21 deviated as documented (masked-sse-de)`, and also "tagged but exact" when a tagged
case matched exactly. That signals a tag to retire, not a failure. The exit status depends only on the untagged and
judged failures.

The i8b judge keeps its separate list of seven QEMU departures. It compares the judged failures with that list, leaving
out any program that was not judged. `run_suite.py` needs no mode logic of its own: environment variables already pass
through to every step, and it copies each step's "deviated" line into its results note. The `m4_bug` marks in run_i5c
go away with the concurrent M4 fix, and this chunk leaves them alone.

**Gate:**
- On the M4, every runner and corpus passes unchanged.
- In M2 emulation, fast mode: every runner exits 0, and the deviated counts are exactly 21, 98 and 8, and 39 and 77 not
  judged.
- A unit test of the judging function shows that an untagged failure, an extra DE and a wrong value in a tagged case
  still fail.

## 3. The shared mechanism: test the result, step the instruction exactly

The measurements rule out cheap per-block detection:
- reading FPSR once per block costs +72% on c-ray's hot block, and per operation ×53;
- a single never-taken branch per FP operation already costs +77%;
- the inline guard with a skipped fix-up cost +96% on the cray benchmark.

The cheapest exact shape measured is the reference branch's NaN guard with out-of-line stubs (`afp-c4a-stub`, not merged):
- `fcmp` + `b.vs` where NZCV is dead, else an integer test after `fmov`;
- packed results reduced with `fmaxp`/`fmaxv`;
- the result kept in a temporary until the test passes, so a stub has the state from before the instruction;
- at the stub, the resident steps the instruction, compiled differently.

Natively, against today's code, it costs:
- c-ray's hot block: +170% (2.72 → 7.35 ns);
- a packed block: +139%;
- a LuaJIT-like scalar block: +66%.

Of that, +38% is the temporary and move alone.

That cost is acceptable for accurate mode (owner: measured, not gated), and nothing cheaper is exact without hardware
traps (§5), so accurate mode builds on it. All four items share it:
- **One result test, one stub kind, one resident path.** Generated code tests the final result of each guarded
  operation, after any software flush. A hit branches to the stub. The resident restores the state from before the
  instruction and steps it.
- **The exact step.** In accurate mode the host compiles every step with `--exact-step`. That compile has no result
  test. It has the fix-ups inline (NaN rules, §4 A), the FPSR of the one operation isolated (saved, cleared, read and
  merged afterwards, as x87 instructions already do), and the band decision (§4 C). A step is one instruction and runs
  rarely, so its cost doesn't matter, but it obeys admission like any code: forward branches only, temporaries VTMP1 and
  VTMP2, no spills. A step compile that would need a spill is refused with a stop, never compiled inexactly.
- **The range test.** For multiplies, divides, FMAs and narrowing conversions (the only operations that can raise UFC
  without AFP, §4 C), the test catches NaN or tiny in one comparison. Take the magnitude's bits u. Then
  u − (MIN_NORMAL + 1), wrapping, is at least INF − MIN_NORMAL exactly when u is 0, a denormal, ±MIN_NORMAL, or a NaN.
  Packed, that is `shl` #1 (which drops the sign), `sub`, `cmhs`, then the same reduction and branch. That is one or two
  SIMD operations more than the NaN test. Other operations keep the NaN test.

The per-operation-class fix-up table of the AFP plan (§2.1) must be reviewed before A is built. It is unchanged, except
that it now applies inside `--exact-step` compiles rather than inside the hot block.

## 4. The four items

| Item | Exact design | Where the code goes | Cost estimate (hot path) | Admission |
| --- | --- | --- | --- | --- |
| A, NaN results | result test on ADD SUB MUL DIV SQRT, HADD/HSUB, ADDSUB, FMA, DPPS/DPPD steps, RSQRT; the step applies the fix-up table | fexc shim (the reference branch's `FexcNanCheck`, rebased), resident stub handler, host step arguments | measured: +66% to +170% on FP-dense blocks | the stub kind and its exact test (as on the reference branch) |
| D, masked FMA inf×0 + QNaN IE | row 10's sanitizing of multiplicands with a NaN addend, emitted also with IE masked | fexc shim (existing row 10) | measured 7-8 instructions per FMA | none new (row 10's shape is admitted) |
| RSQRT's NaN sign | RSQRT joins A's guarded set, unary rule (the input's NaN quieted, else the negative default NaN) | fexc op table | inside A's figure | none new |
| B, masked SSE DE | row 7's software denormal-operand test, emitted also with DE masked (not under DAZ); a hit skips forward into `mrs`, `orr` IDC, `msr fpsr` | fexc shim (existing row 7), shape like fast mode's x87 masked DE | measured 8-20 instructions per op, FMA 44: about 1.5-3× more on FP-dense code (estimate) | an FPSR write inside a forward-skipped span, under x87's fix-up rule (no branch may target an access) |
| C masked, MIN_NORMAL band | range test on mul/div/FMA/narrowing converts; the stub steps the instruction with its UFC decided by fast mode's tininess model; the x86 UE flag kept in MXCSR, FPSR.UFC kept clear | fexc shim (range test, step's isolated check), resident (masked disposition in the existing tininess path) | estimate +10-30% over A on multiply-heavy blocks; extra stubs on zero and tiny results | the range test's shape |
| C under DAZ + FTZ | hardware FZ stays; the range test catches every flushed result; the step is compiled with FZ = 0 and the software input and output flushes (rows 4 and 5) | host (step's FPCR), fexc (rows 4 + 5 together in steps) | nothing beyond C masked's test | none new |

**A.** These are the ops the reference branch guarded, plus RSQRT, which it left out. MIN/MAX, conversions, compares
and ROUND are not guarded; they are already right (AFP plan §2.1). D's spurious IE has to be prevented before the
operation, not repaired at the stub. By the time the stub runs, FPSR already holds the operation's IOC, and an IOC set
earlier can't be told apart from it. Row 10's sanitizing does that, and the NaN guard then fixes the result value.

**B.** Without AFP, IDC is never raised with FZ = 0, so writing it into FPSR is exactly the M4's state. The existing flag
mapping then reports DE with no other change. Row 7's test already carries x86's precedence: no DE beside a NaN operand,
a ZE, a negative SQRT, inf×0 or inf−inf, and never for half-precision sources. It is reviewed and passes on the M4.

The research's cheaper shape is not exact as it stands. It tests only the loads, at +43-55%. Values that are live at
block entry, and denormal results produced inside the block, would also need tests. A complete version is a later
optimization, not part of this plan.

**C masked.** Without AFP, Arm judges tininess before rounding and x86 after. A wrong UE can therefore only come from a
result that rounds to exactly ±MIN_NORMAL. A correct UFC comes only with a result that is 0 or a denormal, and only
from multiplies, divides, FMAs and narrowing conversions:
- adds and subtracts give exact tiny results;
- square root never gives a tiny result;
- RCP and RSQRT run quietly.

The stub can't tell this operation's UFC from an earlier one, because FPSR is sticky. So in this mode FPSR.UFC is kept
clear:
- every result the range test catches goes to the stub;
- at the stub, the resident moves the operation's exact UE into the MXCSR flags it already keeps, and clears UFC.

The step decides UE as fast mode's unmasked path already does: the operands are kept live to a check, and the integer
model of tininess after rounding decides each lane. The one difference is the outcome: the flag is recorded and the
instruction resumes, with no #XM. This plan assumes that generated STMXCSR and the resident's flag reads OR the stored
MXCSR flags with FPSR, as the resident's do today. The chunk verifies that, and also verifies where LDMXCSR and FXRSTOR
put a set UE (it must go to MXCSR, not to FPSR).

The known cost risk is exact zero results, for example a multiply by zero. They hit the stub every time. The chunk
counts steps on the benchmarks. If zero results show up, the test leaves out lanes whose multiplicand (or dividend) is
±0, which costs about two SIMD operations more on those ops.

**C under DAZ + FTZ.** With FZ = 1 the band result is flushed to 0, and a correct flush also gives 0 with UFC. The range
test catches both, and the step recomputes the result exactly with FZ = 0 and the software flushes. FTZ alone already
gives the right result that way. The step's UFC goes through the same band decision. Rows 4 and 5 have never run
together, because today DAZ + FTZ uses the hardware flush, so their combination is tested natively against the M4 (AFP
code) before it is relied on. The alternative was to drop FZ = 1 and flush in software in the hot code. That shape
measured at least +90%, with a research proxy of +528%/+969% (overstated), and it buys nothing once C masked's test is
there.

**Not in this plan:** the x87 UE band, and the x87 two-NaN choice (an existing recorded deviation on every host). Both
belong with accurate 80-bit x87.

**Overall estimate:** FP-dense SSE/AVX code roughly 3-6× slower than fast mode on a host without AFP; integer code
unchanged. This is an estimate, and chunk 6 measures it.

## 5. If the M2 has usable FP traps (the owner's pending test)

On the M4 with AH = 0, FPCR trap enables are precise: the destination and FPSR are untouched. They cost +4.3% when
enabled. The M2's records list synchronous FP exceptions as M4-only, so the trap path may not exist there. The owner's
run of the two trap tests in the research directory decides it, on the M2 and inside HVF.
- **No traps:** this plan as written.
- **Traps:** per the owner's November 16 note, C masked moves to fast mode with a design of its own (a precise UFE trap;
  the resident replays the one Arm instruction with traps off and decides the band lane). Chunk 4 then shrinks to what
  chunk 5 still needs, because FZ = 1 flushes never trap: the range test for DAZ + FTZ code and the band decision in
  steps.

Traps could also cut A's cost. The invalid-operation trap catches every default NaN and SNaN case. Only the FMA forms and
the lowerings that negate a quiet NaN without raising IOC would still need the result test. That is a separate design,
asked as decision 2.

## 6. Chunks, gates and order

Every chunk keeps the standing rules:
- the M4 stays byte-identical (replay of both recorded streams against the new fexc, 0 differences);
- the identity unit test (every MXCSR value × FCW.RC × FCW masks) is extended with the mode: with AFP, `code_context`,
  `x87_args`, the gating values, `fpcr` and `mxcsr_flags` are identical in fast and accurate mode;
- fast mode in M2 emulation stays byte-identical (replay with `afp=0` and no `--accurate`, 0 differences);
- benchmarks only at completion, VMs only from the main session, the full suite once before merge.

Native oracle tests compare accurate `afp=0` code with the M4's AFP code on M4 hardware: random operands with NaN,
denormal, band and zero classes, results and flags bit-identical. The fast-mode x87 work used the same method.

| Chunk | Content | Gate |
| --- | --- | --- |
| 0. Test hygiene | registry, README list and parity test, driver reports `afp` and `fp` (`fp` always fast until chunk 1), the three runners and corpus judging, the model copied to evidence | §2 |
| 1. Frame | option and variable, `Cpu` field, key bit 32, `--accurate` and `--exact-step` (accepted, no effect yet), startup line, driver's `fp` | identity test with the mode; M4 replay 0 differences; M2 emulation fast replay 0 differences; a boot in each mode prints the right line |
| 2. B | row 7's test with DE masked, the FPSR write, admission rule | native oracle for DE; in M2 emulation, accurate: run_i5a_de, run_i5c and run_i8_faults pass with no deviation counted |
| 3. A with D and RSQRT | the reference branch's stub rebased; fix-up table reviewed first, then the fix-ups in `--exact-step`; row 10 with IE masked; RSQRT guarded | native oracle (NaN classes); a run_i5c loop taking the stub many times; in M2 emulation, accurate: i8 and i8b fully pass (i8b: only the seven departures) |
| 4. C masked | range test, the UFC-clear rule, masked disposition at the stub, the STMXCSR, LDMXCSR and FXRSTOR checks; starts after the owner's trap test | new run_i5c band cases (every rounding mode, f32 and f64, packed lanes mixing band and real underflow, UE set earlier by LDMXCSR); native oracle; step counts on the benchmarks |
| 5. C under DAZ + FTZ | steps with FZ = 0 and rows 4 + 5 combined | native oracle of the combination; run_i5c DAZ + FTZ band cases; the corpora's DAZ/FTZ programs in accurate mode |
| 6. Cost, records, M2 run | one benchmark round in M2 emulation, fast vs accurate (cray, luajit, luajit-interp, zstd, sqlite, avx; 5 interleaved runs); README: the option, its cost, fast mode's deviation list; the owner's M2 run of the probe and runner subset in both modes | numbers recorded here; the M2 matches M2 emulation |

**Order and why.**
- Chunk 0 is first (owner).
- The frame comes next, because every item needs it.
- B is next because it is mostly reuse. It exercises the frame end to end, and it turns three runners fully green in
  accurate mode early.
- A is the largest. It needs the fix-up table review, and it brings the stub and the exact step that C needs.
- C masked waits for the owner's trap test.
- C under DAZ + FTZ uses C masked's test and decision.
- Chunks 2 and 3 are independent and may swap.

## 7. Decisions for the owner

1. **Test hygiene rule (chunk 0).** In fast mode on a host without AFP, the recommendation is: runner cases are judged
   against the documented deviated result (exact, or exact except that DE is missing), and corpus programs are not
   judged. Counts are printed either way. Or would you rather not judge tagged runner cases at all ("disable"), as you
   suggested?
2. **If your M2 test shows usable FP traps:** should accurate mode also use the invalid-operation trap to cut A's cost?
   That would be a separate plan: the result test would stay only on FMA forms and on lowerings that negate a NaN. C
   masked moving to fast mode is already your decision.
3. **The standing suite:** should the full suite gain two FP groups in M2 emulation? One would run run_i5c, run_i5a_de,
   run_i6_faults, run_i8_faults, the FEX ASM suite, i8 and i8b in fast mode, judged with deviations; the other would run
   the same in accurate mode, judged fully. That adds their run time to every merge. Without them, accurate mode is
   checked only by its own chunks.

Decided here, with reasons in the text:
- the option's spelling;
- the startup line;
- no M4 code change;
- key bit 32 kept although it is redundant within one process;
- the stub design over the inline guard;
- B with the existing test;
- hardware FZ kept under DAZ + FTZ;
- zero-result exclusion only if the step counts call for it.

## 8. Risks

- **Stub storms.** Any code that produces many NaN, zero or tiny results takes the stub at every one. The step answer is
  cached, but each stub is a resident entry. Chunks 4 and 6 count steps.
- **The UFC-clear rule** depends on every UFC-raising operation being range-tested. A missed lowering (a new FEX op that
  multiplies) would make the band decision wrong without any sign. Mitigations: the op table is checked against FEX's
  IR ops at build time (an op that can raise UFC must be listed), and the native oracle covers each class.
- **Fork changes.** All shim and FEX changes go on the fork's `natemu` branch, under the same rules as the fast-mode
  work (owner-authored commits, nothing offered upstream).
- **The reference branch has drifted:** it predates C unmasked and the x87 fixes. Rebasing it is part of chunk 3, and
  its earlier review doesn't carry over.

## 9. Progress

| Date | Chunk | State | Evidence |
| --- | --- | --- | --- |
| Nov 16 | plan | written; independent review pending | this file |
| | 0. Test hygiene | | |
| | 1. Frame | | |
| | 2. B (masked SSE DE) | | |
| | 3. A with D and RSQRT | | |
| | 4. C masked | | |
| | 5. C under DAZ + FTZ | | |
| | 6. Cost, records, M2 run | | |

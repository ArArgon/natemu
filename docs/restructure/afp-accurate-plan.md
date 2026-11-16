# Accurate floating-point mode on hosts without FEAT_AFP (M1, M2)

Status: plan, November 16, 2026, branch `afp-accurate` off `afp-fallback` (63a38db). Revised after two review rounds
(§9). Revised again the same day for precise FP traps, which the M1 Pro, M2 Max and M4 all report (§3). Chunks 0-1 are
done and chunk 2 is built (§10). Background and evidence: [the AFP fallback plan](afp-fallback-plan.md)
(its §2 table, §3 owner notes of November 15 and 16, §6 measurements) and [the inventory](afp-inventory.md).

**What the owner decided (November 16).** natemu gets two floating-point modes:
- **fast**, the default. On a host without AFP it keeps only the cheap exact fixes (done: rows 2-10 of the AFP plan,
  known-zero upper bits, C unmasked, x87 masked DE for m32/m64, x87 NaN results) and documents the rest as deviations.
- **accurate**, opt-in at launch. It is part of the compile key, so fast-mode code is not changed by its existence.
  On a host without AFP it makes the remaining SSE/AVX cases exact:
  - **A:** NaN results, with **D** (the masked FMA inf×0 + QNaN IE flag) and RSQRT's NaN sign;
  - **B:** the SSE masked DE flag;
  - **C masked:** the masked UE flag in the MIN_NORMAL band (moving into fast mode if its cost gate passes, §4);
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

## 3. The shared mechanism: precise FP traps, handled in the resident

**The evidence (November 16).** The owner ran the trap probe on an Apple M2 Max (macOS 26.6.2). It reports
`FP_SyncExceptions` 1, and trapped FP exceptions are implemented and precise there, exactly as on the M4:
- **What traps.** UFE traps every result that is tiny before rounding, exact ones and the band tie included, scalar and
  vector (fmul, fmadd, fcvt, fcvtn, fdiv, fsub). It never traps under FZ = 1. IOE traps every IOC case.
- **What a trap leaves.** The destination and FPSR are unchanged; ESR EC 0x2C, ISS TFV plus the flag; ELR is the
  instruction. The ISS never names the lane.
- **Cost inside a VM** (EL0 to the guest's EL1 vector): 37-39 ns per trap with a bare handler, 63-70 ns re-running the
  instruction. Enabling traps costs nothing measurable when none fire. The earlier native research saw +4.3% on c-ray's
  hot block with UFE enabled (runs +3.8% to +6.9%); chunk 6 settles it.

Evidence: `afp-accurate-research-20261116-01` (README, `m2-max-fp_trap_probe.txt`, `hvf_trap-m4.txt`).

Generated code runs at EL0, so an FP trap is a synchronous exception into the resident's existing lower-EL vector. This
replaces the previous revision's result stubs, exact steps, cached step and range test, which are no longer needed (§9).

**The handler.** It works on the one Arm instruction, not on the guest instruction. Nothing is banked and no step is
compiled.

1. **Recognise the trap.** EC 0x2C from EL0, at an ELR inside an admitted block (or the step block), is classified first,
   before the site lookup. An EC 0x2C taken at EL1, or at an ELR outside admitted code, halts with a named stop and never
   reaches the decoder. The resident runs with FPCR = 0, so it never traps legitimately: `save_native` zeroes FPCR before
   resident code, and the x87 helpers run in the resident.

2. **Save the operands, then re-run the word in place at EL0 (decided).** The handler copies the word's source registers
   from the frame into its own save area. It then returns to ELR with the trap enables cleared in the frame's FPCR (the
   value captured at the trap, so ROUND's own rounding mode is kept), FPSR cleared, and the hardware single-step armed. The
   word runs once in place, and the software-step exception (an exception class the resident already handles) brings it
   back. FPSR then holds this word's flags alone.

   The resident already single-steps for data watchpoints, and that code assumes every step is a watchpoint step: it
   re-arms the monitor-debug enable (MDE) unconditionally and restores nothing in FPCR. So:
   - a per-vCPU "FP re-run in flight" marker tells the two steps apart, and the step handler dispatches on it;
   - the trap enables are put back into the frame's FPCR when the FP step returns, and also when a pending step is
     cancelled. Otherwise a later entry could resume with the trap-free FPCR captured for the re-run, and accurate mode
     would silently run without traps;
   - MDE is left as it was;
   - the performance log counts FP re-run steps separately from watchpoint steps;
   - a unit test takes an FP trap in the step block while a watchpoint is armed.

   The alternative was a per-vCPU table of executable slots, filled with the trapped word. That needs writable-and-
   executable resident memory and I-cache maintenance. Re-running in place costs one more exception, about 37 ns, and
   needs neither.

3. **Patch by mode, never by cause.** One word can raise IOC in one lane and UFC in another, while the ISS names only one
   exception, so the cause in ESR is never used. What the handler patches depends only on the mode's enabled traps:
   - **Accurate mode, NaN lanes.** Each lane whose result is a NaN is recomputed from the saved operands by AH = 1's
     rules (the AFP plan's §2.1 table, per Arm operation): binary ops take source 1's NaN quieted, else source 2's, else
     the negative default NaN; FMA takes Rn, Rm, then the addend, and negation leaves a NaN alone. IOC is taken from the
     re-run, and removed only for lanes where AH = 1 raises none: FMA inf×0 with a QNaN addend (D).
   - **Both modes where UFE is on: UFC.** UFC is recomputed per lane by the existing integer model of tininess after
     rounding. The handler decodes the word into the model's description (multiply, divide, FMA, convert; element size;
     lanes).
   - **Fast mode patches only UFC.** Patching NaN lanes there would make fast-mode NaN results exact only in words that
     happened to trap. That would be nondeterministic, and it breaks chunk 0's tagged-but-exact rule.
   - **Under DAZ + FTZ, the per-word flush** (§4).
   - Every other class (compares, conversions to integer, MIN/MAX) keeps the re-run as it is, which is exactly what the
     hardware does without traps. An Arm FP word the handler's table doesn't know halts with a named stop.

4. **Finish.** The patched flags are ORed into the FPSR from before the trap (the trap left it untouched, so attribution
   is exact). The trap enables are restored, and execution continues after the word.

**What else this touches.**
- **Banking and #XM.** The existing check stubs and banking are unchanged. They run after the handler returned, and see
  exactly what an M4 would have produced. Fast mode's unmasked-UE model at the #XM stub then finds no spurious UFC and
  agrees.
- **x87.** x87 operations use the same Arm words as scalar SSE, so the handler can't tell them apart, and x87 has its own
  NaN fix-up after `FexcX87End`. Whenever trap enables are on, the x87 FPCR word is therefore always explicit (the
  existing `X87_FPCR_EXPLICIT` path) and has no trap bits, so x87 never traps. This changes x87 code only where traps are
  on; it adds one FPCR switch per x87 instruction, small next to the FPSR save, clear, read and restore each x87
  instruction already does. It is measured in chunk 3.
- **The resident.** It runs with FPCR = 0, so it never traps itself. A trap in the step block is handled the same way.
- **Admission** needs nothing for IOE and UFE, because no code changes. The FMA check (§4 A) is a new admitted shape with
  a site.

**Which bits, where.** `fpcr()` adds, without AFP:
- UFE wherever FZ = 0 and the mode gets C masked (§4);
- IOE in accurate mode.

`sse::fill` already writes `fpcr()` at every entry, so it carries them. The trap bits change no SSE/AVX code in fast
mode. The only fast-mode code they change is x87's trap-free FPCR word (above), and only if UFE moves into fast mode;
that change is measured as part of that move's gate. In accurate mode, code that relies on a trap (the FMA check, the
input flush of DAZ + FTZ) is keyed by bit 32 like everything else in accurate mode. Bit 33 stays reserved for accurate
x87.

**Traps are a required host feature (owner).** M1 Pro, M2 Max and M4 all report `FP_SyncExceptions` 1. The owner made
trapped FP exceptions a Required host item: startup refuses a host whose FPCR trap-enable bits read back 0. The readback
is done in the vCPU, like the TSO bit, and is being built on branch `fptraps-core`. Both modes may therefore rely on
traps, and there is no fallback. Trap availability needs no `Cpu` field, key bit or fexc argument; trap-reliant code is keyed by
bit 32 alone. Precision and in-VM delivery are
measured on the M2 Max and the M4; the owner's M1 run of the probe is a gate in chunk 6.

The registry's classes gain "fixed by": fast mode or accurate mode.

## 4. The four items

| Item | Design | Where the code goes | Cost estimate | Admission |
| --- | --- | --- | --- | --- |
| A without FMA order: default NaN sign, SNaN choice, D, RSQRT's sign | IOE trap; the handler's NaN patch | resident (handler, in-place re-run), host (`fpcr`, probe) | 0 when nothing traps (measured in a VM); ~100 ns per invalid or SNaN result (63-70 ns measured with a re-run, plus the single-step exception, ~37 ns) | none |
| A, negations | the free codegen fixes of fast mode (branch `afp-negfree`): `fmul` by −1 instead of `fneg` in VFMLS, VFNMLS and VFMADDSUB; ADDSUB as `fmul` by a sign mask plus `fadd` | fexc / FEX fork (fast mode, both modes) | measured free (same issue count) | none new |
| A, FMA order and one-instruction negating FMAs | FMA forms only: the result kept in a temporary; `fcmge` of the result (a signalling compare: any NaN lane raises IOC, so IOE traps; no NZCV, no branch, no reduction); the handler, told by the site, recomputes the NaN lanes from the FMA's operands | fexc shim (check plus site), resident (site kind) | one SIMD op per FMA plus the temporary (a full-register `mov`, measured free): estimate ≤2× on pure FMA streams, ~0 on code without FMA; fallback, the measured `fmaxv`/`fcmp`/`b.vs` shape, 2.3-2.9× on pure FMA | the check's shape and its site |
| B, masked SSE DE | unchanged: chunk 2 (row 7's test with DE masked, behind the cheap per-op screen being added) | as built | chunk 2's numbers | as built |
| C masked, MIN_NORMAL band | UFE trap; the handler decides UFC per lane with the tininess model | resident (handler) | 0 to +4.3% enabled; ~100 ns per tiny result | none |
| C under DAZ + FTZ | FZ = 0, UFE on, and the software input flush of DAZ (row 4, already built); the handler gives x86's flushed result and flags (row 5's rule) for each trapping tiny result | host (`fpcr`), fexc (row 4 under accurate DAZ + FTZ), resident (handler) | row 4's input flush (fast mode's DAZ-only cost) plus ~100 ns per tiny result | none new |

**A.** IOE catches every case of A that raises IOC: the default NaN's sign, the SNaN choice, D (inf×0 + QNaN), and RSQRT's
sign (FEX's RSQRT is `fsqrt` + `fdiv` with FPSR restored around it, so the patched negative NaN flows through and no flag
leaks). The research shows that only two cases raise nothing:
- an FMA with more than one quiet NaN (Arm without AH takes the addend first; x86 takes Rn first);
- a negation of a quiet NaN.

The `afp-negfree` fixes remove the separate `fneg` and the `fcadd`. What remains is the one-instruction negating FMAs
(`fmls`, `fnmsub`, ...) and the FMA order, both FMA forms. So the result test is on FMA forms only. Every other operation
either traps or is already right.

The FMA check:
- **Shape.** `fcmge` of the result with zero into a JIT temporary. It is a vector or scalar SIMD compare, so it leaves
  NZCV alone. Being a signalling compare, it raises IOC for any NaN lane, and IOE turns that into a trap. There is no
  branch and no reduction.
- **The temporary.** The FMA writes into it, and the guest register is written only after the check. So "no guest
  register written before the instruction's last test" (previous review) still holds, for the same reasons: 256-bit
  halves, scalar upper bits, destination = addend.
- **The site** names the FMA's word and all three operand registers: Rn, Rm and the original addend. They are kept live to
  the check. Without that, register allocation could reuse a register that dies at the FMA, such as a memory operand's
  load register. They are kept on the check op itself, the way fast mode's C-unmasked check keeps its operands. As
  there, a compile with spill slots gets no description; the FMA check then can't be built, and the compile is refused
  and retried without the FMA in the block, never emitted unchecked.
- **No stop and no cliff.** The exhaustive admission test covers every FMA form under every key, 128- and 256-bit, compiled
  alone with no spill slots, so the retry without the FMA can never end in a stop. Keeping three operands live raises
  register pressure, most for 256-bit. If refusals were frequent they would cut FMA loops into one-instruction blocks, so
  chunk 4's gate counts spill-slot compiles among FMA blocks on the recorded streams and on the avx benchmark.
- **The check's destination** is a JIT temporary that admission pins as never read: the handler resumes after the
  `fcmge`, so that register is stale.
- **At a check trap,** the handler recomputes the NaN lanes from the three named operands, writes the temporary, adds no
  flag for the compare itself, and returns.
- **Native evidence** that the check is a true signalling test: `fcmge` with a QNaN raises IOC (scalar and `.4s`); with a
  denormal and FZ = 0 it raises nothing; `fcmeq` with a QNaN raises nothing.
- **Order of traps.** An SNaN FMA traps first at the FMA itself, and then again at the check; both give the same answer.
- **Cost.** It is unmeasured. Chunk 4 measures it natively first. If it is worse than the measured branch shape, the
  branch shape (to a stub that is the same handler) is used.

**C masked.** The trap is precise and FPSR is untouched, so this operation's UFC is known exactly. The previous revision's
"FPSR.UFC kept clear" rule and "UFC removed before banking" are no longer needed. A UFE trap per tiny result costs
~100 ns. Real x86 also takes a microcode assist of a similar size on denormal results, so denormal-heavy code is slow on
both.

**C under DAZ + FTZ (decided now).** With FZ = 0 and UFE on, every Arm word that produces a tiny result traps, exact
results and adds/subtracts included. The flush is therefore applied by the handler per Arm word, only while
`cpu.mxcsr` has DAZ and effective FTZ (constant within a block); under FTZ alone, row 5's software flush stays as built.

The handler's word table says which words flush:
- arithmetic results (add, subtract, multiply, divide, FMA, DPPS steps, narrowing conversions to single or double):
  flushed to ±0, with UE + PE, x86's FTZ result;
- `fcvtn` to half (VCVTPS2PH ignores FTZ): UE decided by the model, no flush;
- RCP's quiet `fdiv`: flushed (its flags are restored afterwards anyway);
- row 5's own flush `fmul` and `afp-negfree`'s `fmul` by −1: never flushed by the handler. Row 4 flushes inputs before the
  new negating `fmul` words, so they never see a denormal under DAZ. Otherwise they would produce a tiny result and a
  wrong UE + PE.

The gate cases run under DAZ + FTZ and under DAZ alone:
- VFMLS, VFNMLS, VFMADDSUB and ADDSUB with denormal inputs;
- an add or subtract with an exact tiny result under FTZ, checked against real x86 (the AMD machine used for the DPPS
  check).

**Why this shape.** The alternative was the range test plus a step per flushed result. Its hot-path test is cheaper than
row 4's input flush, but it needs all the stub, step and cached-step machinery, which nothing else needs any more. Its
per-flush cost is 0.1-1.5 µs against a trap's ~100 ns, and under `-Ofast` flushes are routine. The trap-based shape
reuses built code (row 4) and the one handler. Its hot-path cost is the class fast mode already pays for DAZ alone.
Chunk 2's per-op screen could later skip the input flush when no source is denormal.

The `-Ofast` workload in chunk 6 reports the trap count, so this decision is checked, not just argued.

**B.** Chunk 2 is not changed here. **Could UFE replace the result side of a later dataflow variant of B?** Yes, in
principle. With UFE on, every tiny result traps, exact ones included, so no denormal produced inside a block goes unseen.
A dataflow B would then test inline only the loads and the values live at block entry, and the handler would cover
produced denormals (for example by routing the consuming instruction through the full test). That is not planned here;
it is a candidate once chunk 2's screen is measured.

**Moving work into fast mode.** The owner's rule is "whatever is best for performance within each mode's contract", and
fast mode's contract is exact fixes that cost nothing measurable.
- **C masked** (UFE) moves into fast mode if the benchmark gate passes. The gate is the AFP plan's standing one: in M2
  emulation, five interleaved runs, cray, luajit and luajit-interp at most 3% slower geometric mean and at most 5% on
  any one, and zstd, sqlite and avx with no regression.
- **IOE** would make A's trapped cases free in fast mode too. Its traps also fire where the result is already right
  (COMISS with a quiet NaN, a conversion to integer of a NaN or an out-of-range value), each ~100 ns. The same gate plus
  trap counts decides it, but it changes your November 16 split (decision 1).

Fast mode's FMA order and negating FMAs stay documented deviations; the FMA check stays in accurate mode.

**Not in this plan:** the x87 UE band, and the x87 two-NaN choice (an existing recorded deviation on every host). Both
belong with accurate 80-bit x87.

**Cost per block.** These are estimates against fast mode on a host with traps:
- c-ray's and LuaJIT's hot blocks have no FMA, so A and C add about 0-4%. B dominates: chunk 2 measured c-ray's hot block
  at 2.6 → 42.5 ns, about 16×, before its screen.
- FMA-heavy code (the avx benchmark) pays the FMA check, ≤2× on FMA issue by estimate.
- `-Ofast` code pays row 4's input flush in place of B, plus traps.
- Every invalid, SNaN or tiny result costs ~100 ns.

Chunk 6 measures all of it.

## 5. The superseded design

Before the trap results, this plan's §3-§5 held a trap-free design: result stubs, exact steps compiled with
`--exact-step`, a cached step, the range test with its zero exclusion, the UFC-clear rule and the step's own FPCR. Its two
review rounds are in §9. It is dropped, not kept as a fallback: every supported host reports traps. `--exact-step`
(chunk 1, accepted and unused) is removed in chunk 4.

## 6. Chunks, gates and order

Every chunk keeps the standing rules:
- the M4 stays byte-identical (replay of both recorded streams against the new fexc, 0 differences);
- the identity unit test (every MXCSR value × FCW.RC × FCW masks) is extended with the mode, the trap availability and
  step requests: with AFP, `code_context`, `x87_args`, the gating values, `fpcr` and `mxcsr_flags` are identical in every
  mode;
- fast mode in M2 emulation stays byte-identical in code (`fpcr` gains trap bits only where §4 says);
- benchmarks only at completion, VMs only from the main session, the full suite once before merge.

Native oracle tests compare accurate `afp=0` code (with the handler, run natively under a signal harness that calls the
same patch code) with the M4's AFP code on M4 hardware: random operands with NaN, denormal, band and zero classes,
results and flags bit-identical.

| Chunk | Content | Gate |
| --- | --- | --- |
| 0. Test hygiene | done | §2 |
| 1. Frame | done | §1 |
| 2. B | built (screen in progress) | as in §10 |
| 3. Traps and C masked | `fpcr` trap bits (after `fptraps-core` merges); x87 trap-free FPCR; the EL0 EC 0x2C handler (named stop for EL1 or outside admitted code), in-place re-run by single-step, UFC patch; registry "fixed by"; C masked in accurate mode | handler unit tests per word class; native oracle (band, tiny, exact tiny, packed lanes mixing band and real underflow, every rounding mode); run_i5c band cases pass in M2 emulation, accurate; x87 runners unchanged; trap counts and VM trap cost |
| 4. A | the `afp-negfree` fixes merged first; IOE and the NaN patch (D, SNaN, RSQRT); the FMA check and its site; `--exact-step` removed | NaN classes in the native oracle; check-trap cases (NaN only in a high half, destination aliasing each FMA source, an SNaN FMA trapping twice); the FMA check's native cost against the branch shape; spill-slot compiles among FMA blocks (recorded streams, avx); an FP trap in the step block with a watchpoint armed; in M2 emulation, accurate: i8 and i8b fully pass (i8b: only the seven departures) |
| 5. C under DAZ + FTZ | FZ = 0 with UFE under accurate DAZ + FTZ; row 4's input flush emitted there; the handler's FTZ result | native oracle; run_i5c DAZ + FTZ band and flush cases; the corpora's DAZ/FTZ programs in accurate mode |
| 6. Cost, fast-mode gate, records, M2 run | one benchmark round in M2 emulation: fast before and after UFE (and IOE), accurate; cray, luajit, luajit-interp, zstd, sqlite, avx (5 interleaved runs), plus an existing FP benchmark program built with `-Ofast`; trap counts, split by the fast-mode traps that fire if UFE or IOE move to fast mode (UFE: `fmul` by −1 of a denormal, exact denormal add/subtract, row 5's flush `fmul` under FTZ alone; IOE: COMISS with a QNaN, scalar MIN/MAX `fcmpe`, `fcvtzs` of a NaN); README and registry for whatever moved into fast mode; the owner's M2 run of the runner subset in both modes | the fast-mode gate (§4) decides what moves; numbers recorded here and in the investigation record; the M2 matches M2 emulation; the owner's M1 run of `tools/fp_trap_probe/run.sh` confirms precise, in-VM traps on the M1 |

**Order and why.**
- Chunk 3 builds the trap machinery with its simplest consumer. The tininess model and its tests already exist, so the
  handler is proven on UFC before NaN patching is added.
- A comes next. It depends on the `afp-negfree` merge and on the handler.
- DAZ + FTZ reuses both.
- Chunk 6's benchmark round decides the fast-mode moves, so they ship with their numbers.

## 7. Decisions for the owner

1. **IOE in fast mode.** If it passes the same gate as C masked, should fast mode also take IOE, making the default NaN's
   sign, the SNaN choice, D and RSQRT's sign exact by default? That moves part of A out of accurate mode, against your
   November 16 split. FMA order stays accurate-only either way.
2. **The standing suite:** should the full suite gain two FP groups in M2 emulation? Both would run run_i5c, run_i5a_de,
   run_i6_faults, run_i8_faults, the FEX ASM suite, i8 and i8b: one in fast mode, judged with deviations, and one in
   accurate mode, judged fully. That adds their run time to every merge.

Decided here, with reasons in the text; the owner may overrule any of them:
- the hygiene rule;
- the option's spelling and precedence;
- no M4 code change;
- key bit 32;
- traps instead of stubs;
- the FMA check on FMA forms only;
- C masked moving into fast mode subject to the gate;
- DAZ + FTZ by FZ = 0, UFE and the input flush;
- no trap-free design (traps are a required host feature);
- x87 never traps.

## 8. Risks

- **Trap storms.** Code with many invalid, SNaN or tiny results pays ~100 ns each, in fast mode too if C masked or IOE
  move there. Chunk 6 counts traps on every benchmark, and the gate is measured with UFE and IOE on.
- **Handler coverage.** Every Arm FP word FEX emits must decode to a class, and an unknown word stops the guest rather
  than being patched wrongly. A build-time check lists FEX's FP ops against the handler's table, and the native oracle
  covers each class.
- **Trap availability.** The M1 reports traps, but their precision and delivery in a VM are confirmed only on the M2 Max
  and the M4 (chunk 6 gates the M1). A host or macOS version that hides the enables is refused at startup (`fptraps-core`).
- **The in-place re-run** depends on single-step and the trap-enable restore being exact; a wrong restore would leave
  traps off silently. The handler unit tests and the native oracle check that each trapped word ends with the original
  FPCR.
- **Fork changes.** All shim and FEX changes go on the fork's `natemu` branch, under the same rules as the fast-mode work
  (owner-authored commits, nothing offered upstream).

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
| T1 | New evidence (owner, M2 Max; M1 Pro reports traps too): precise FP traps in the VM, 63-70 ns per trap with a re-run, free when not firing | §3: traps replace the result stubs, exact steps, cached step and range test (§5 superseded); no trap-free fallback |
| T7 | Owner: trapped FP exceptions become a Required host feature | §3: no fallback, refusal or trap-availability key; both modes rely on traps; chunk 3 follows `fptraps-core` |
| T2 | Owner: "whatever is best for performance within each mode's contract" | §4: C masked (UFE) moves into fast mode if the standing benchmark gate passes; IOE in fast mode under the same gate is decision 1 |
| T3 | Which A cases raise no IOC | §4 A: only FMA multi-QNaN order and the one-instruction negating FMAs remain after the `afp-negfree` fixes; an `fcmge` check that traps, on FMA forms only |
| T4 | DAZ + FTZ: range test with steps, or FZ = 0 with UFE | §4: FZ = 0, UFE and row 4's input flush (no stub machinery left; ~100 ns per tiny result against 0.1-1.5 µs per step) |
| T5 | x87 runs on the same Arm words | §3: the x87 FPCR word is explicit and trap-free whenever traps are on |
| T6 | B and UFE | §4 B: chunk 2 unchanged; UFE could later cover the result side of a dataflow B |
| R3-1 | Patch by mode, never by the cause in ESR; fast mode must patch only UFC | §3 step 3 |
| R3-2 | The FMA check's operands may die before the check; its destination is stale after a trap | §4 A: Rn, Rm and the addend kept live on the check op and named; a compile with spill slots is refused, not emitted unchecked; admission pins that the check's destination is never read; native evidence that `fcmge` signals on a QNaN |
| R3-3 | Under DAZ + FTZ every tiny word traps; which words flush | §4: the per-word flush table, only under DAZ + FTZ; row 4 before the negating `fmul`s; gate cases incl. real x86 |
| R3-4 | EC 0x2C at EL1 or outside admitted code | §3 step 1: named stop, decoder never runs |
| R3-N | IOC from the re-run; in-place re-run instead of a W+X slot; fast-mode trap list in chunk 6; chunk 2's 16× | adopted: §3 step 2 (in place, one more exception, no W+X memory), §3 step 3, chunk 6, §4 cost |
| R4-1 | The FP re-run's single step must be told apart from a watchpoint step | §3 step 2: an in-flight marker; trap enables restored on return and on cancel; MDE untouched; separate perf counts; a unit test with a watchpoint armed |
| R4-2 | The FMA check's refusal must not become a stop or a performance cliff | §4 A: the exhaustive no-spill test over FMA forms × keys × 256-bit; chunk 4 counts spill-slot compiles among FMA blocks |
| Owner | Startup output becomes one line per decision | §1: this plan sets the fp line's value; the degraded note uses chunk 0's descriptions |

## 10. Progress

| Date | Chunk | State | Evidence |
| --- | --- | --- | --- |
| Nov 16 | plan | written; reviewed (accept with required changes); revised (§9) | this file |
| Nov 16 | 0. Test hygiene | **done, merged into afp-fallback** (independent review accepted; its one gap fixed: the tagged known i8b departures must be deviated without AFP). Tagged: 21 (run_i5c), 98 (run_i5a_de), 8 (run_i8_faults), i8 39, i8b 73 (four of i8b's 77 are known QEMU departures with no M2 cause) | VM `afp-accurate-c0-check-20261116-01`: M4 all pass; M2 emulation 0 failures, the tagged counts deviated as documented. Full suite on afp-fallback 171/173 (`afp-fallback-suite-20261116-01`/`-02`) |
| Nov 16 | 1. Frame | **done** (independent review accepted, no findings; mode separation traced through every key, mutations caught). `--fp`/`NATEMU_FP`, `Cpu::accurate_fp`, key bit 32 only for accurate mode without AFP, fexc `--accurate`/`--exact-step` (no effect yet, reset per request, refused with AFP), the `fp:` startup line, the driver's real `fp`, contract 67. On a host without AFP accurate mode prints `accurate mode` though chunks 2-5 are not built: until then accurate runs fail the tagged cases strictly (intended). For chunk 3: in-memory answers match on the gating bits fexc reports read, so fexc must mark `--exact-step` read wherever it changes code (test it then) | fexc `natemu-raw/afp-acc-c1-fexc-20261116-01` (SHA-256 cfa2582b...); replay 0 differences (M4 and `afp=0`, both streams); host 405, abi 115, test_slice1 71, test_serve. VM `afp-acc-c1-check-20261116-01`: boots print the right line in all four settings (M4 fast/accurate, M2 emulation fast/accurate); run_i5c in M2 emulation accurate reports `afp` false, `fp` accurate and fails the 21 masked-DE cases strictly (this is the only test that catches a missing `Cpu::accurate_fp` assignment) |
| Nov 16 | 2. B (masked SSE DE) | **built with the screen, not yet VM-checked** (the main session runs the three runners). In accurate mode without AFP, DE masked, no DAZ: row 7's test before each op of `DeRuleOf`, then a new FEX IR op `FexcSimdDe` (fork `807033d`: `cbz; mrs; orr #0x80; msr fpsr`) sets FPSR.IDC where the M4 raises it; DPPS/DPPD add levels via `fexc_simd_de_masked`. Silent in §4, chosen: the write sits before its op, per op (no flag kept live to a check); fast mode, AFP, DAZ and DE-unmasked code unchanged; reads `--accurate`/`--daz` only for an SSE/AVX FP op without AFP, so fast-mode answers differ only in `args_read` (27 of 245572 requests) | fexc `natemu-raw/afp-acc-c2-fexc-20261116-01` (SHA-256 590b69ff...). Native oracle `AfpAccurateDe` (about 110 forms incl. memory, 256-bit, DPPS/DPPD, FMA, conversions, VCVTPH2PS; 400 random trials each vs the M4): IDC and all result bits outside M4-NaN lanes identical; fails on the chunk-1 fexc. Replay vs chunk-1 fexc: M4 0 differences, `afp=0` fast 0 beyond args_read, accurate 23 changed (all gain the IDC span). Admission: the exhaustive test now covers accurate x unmasked sets x DAZ/FTZ keys, 0 spills. Cost (native, ns/iter fast -> accurate): cray hot block 2.6 -> 42.5, packed 1.26 -> 13.2, LuaJIT-like 1.00 -> 6.3: B alone is far above the 1.5-3x estimate **Screen (chunk 2b, fork `720fe1c`, `93350e2`):** per source `cmtst` with the exponent mask, `and` of the sources, `uminv`, `fmov`, `add #1`, `cbz` over the test, `FexcSimdDe` and the FPSR write (scalar ops: lane 0, no reduction; 64-bit-operand ops and blocks with spill slots keep the unscreened test); a zero or denormal lane falls into the exact code, so the flag is exact. Admission accepts the vector ops, the nested `cbz` and the FPSR write in the skipped span (forward branches only); refusal test: the screen `cbz` retargeted into the unmasked-IE check window is refused. Grouping over independent ops not done (reviewer: 4.29 to 3.13 ns, only register-only ops with all exceptions masked; not worth the proof). fexc `natemu-raw/afp-acc-c2b-fexc-20261116-02` (SHA-256 fcf654ac...). Oracle `AfpAccurateDe` + the reviewer's 17 extra forms pass; cost (ns/iter, fast / chunk-2 accurate / screened, normal operands): c-ray block 2.56 / 42.3 / 17.7, packed 1.29 / 12.9 / 5.1, LuaJIT-like 1.00 / 6.3 / 2.27 (the screen skips every op; a denormal or zero lane pays the old cost). Replay vs chunk-2 fexc: M4 0, `afp=0` fast 0, accurate 23 changed (all gain the screen), exhaustive admission 0 spills. | Review (independent, accept): the screen is exact (it tests exactly the zero-exponent lanes the exact test needs; scalar forms lane 0; f64 masks for PD/SD and CVTPD2PS; every op in cray screened); the admission refusal test fixed to retarget the screen's own `cbz w0`. Remaining cray cost (18.4 vs 2.7 ns): ~7.5 ns is the 39 taken branches, ~8 ns the screen's SIMD work. Later levers: the exact test out of line (needs an admission change for a backward return), the exponent mask once per block, one screen shared by consecutive ops; CVTPS2PD/VFCVTL2 could be screened with a 64-bit compare
| | 3. Traps and C masked | | |
| | 4. A (IOE, FMA check, negation fixes) | | |
| | 5. C under DAZ + FTZ | | |
| | 6. Cost, fast-mode gate, records, M1/M2 runs | | |

# AFP fallback: x86-exact floating point on hosts without FEAT_AFP, at no cost to the common case

Status: revised after independent review (November 15, 2026; §7), branch `afp-fallback` off `cpu-features` (43b553e).
No code yet. Owner direction (November 15): plan, review, then build; it must be performant; keep this plan with a
progress table. Evidence: [the inventory](afp-inventory.md). It checks each claim against Arm's pseudocode (Sail
translation of the Armv9.4 ASL), the code, and the SDM text natemu already quotes; "inventory 1.2" points into it.
Owner decisions are open in §3. Chunks 1-3 don't depend on them.

**Goal:** on a host whose vCPU lacks FEAT_AFP (the M1 and M2), natemu's SSE, AVX, FMA and x87 results and flags are
what the M4 gives today. Where that costs too much, the owner decides (§3). "Performant" is a gate:
- **The M4:** generated code, request bodies and compile-cache keys stay byte-identical. Replay gate, plus a unit test
  over every MXCSR and FCW.RC value (§4).
- **A host without AFP, in the common mode** (MXCSR 1F80H: everything masked, no DAZ, no FTZ, as Linux and most
  programs run): every fix costs nothing, except decision A's NaN guard, which is measured before it ships.
- **Rare modes:** costs fall only in code compiled for them (DAZ, FTZ, an unmasked exception), which is keyed
  separately, so other code never pays for them.

**Non-goals:** changing the M4 in any way (COR-09, the x87 larger-significand NaN rule, stays as it is on the M4); 80-bit
x87 precision; RPRES; guest feature changes (the guest CPU is the same on every host).

## 1. The enabler: an M4 can emulate a host without AFP

Every AFP behaviour in the Arm pseudocode depends on FPCR.AH, FIZ or NEP being set. All 36 `HaveAltFP()` uses are
gated that way (inventory 0, confirmed by the review). So an M4 with those three clear, and fexc told `afp=0`, is
architecturally the M2's floating-point machine.

Chunk 1 adds one input both sides share: a `no_afp` bit in `Cpu`. `fpcr`, `x87_fpcr`, `code_context` and the host's x87
argument read it from chunk 1. `mxcsr_flags` and `simd_fault_flags` take it in chunks 2 and 3, where they change: chunk
1 must only reproduce the M2.

As built: without AFP, a legitimate x87 FPCR word can be 0 (round to nearest), which collided with the "0 = same as
MXCSR's" contract. The x87 word without AFP therefore carries `X87_FPCR_EXPLICIT` (bit 32), and the shim loads its low
32 bits. M2 emulation needs a fexc built at or after `afp-c1-fexc-20261115-01`.

The polarity is chosen so that zero is today's behaviour. Many places build a `Cpu` before the probe runs: the loader,
`ap_wake`, the driver programs and the host's `test_cpu`. With this polarity none of them silently gets M2 FPCR words.

The host sets the bit once, from the same `supported` value fexc is told, after `NATEMU_FEXC_FEATURE_OFF`, and before
the first lookup. `x87_args` asserts that the bit agrees with `supported`'s afp bit. The resident's copy-and-restore
paths carry the bit along, and the template pool keys on `code_context`. With it,
`NATEMU_FEXC_FEATURE_OFF=afp` on the M4 is full M2 emulation:
- All development, tests and benchmarks run on the M4 that way.
- One confirming run on the real M2 (the owner) closes it.
- The replay tool gains its body-rewrite hook, so recorded streams can be replayed with `afp=0` added.

## 2. What differs without AFP, and what this plan does about each

Costs are in Arm instructions per affected x86 instruction, with constants hoisted per block. "Common mode" is MXCSR
1F80H with x87 exceptions masked.

| # | Difference without AFP (inventory section) | Fix | Common mode | Elsewhere |
| --- | --- | --- | --- | --- |
| 1 | `fpcr` writes RES0 bits; can't emulate an M2 (1) | the shared AFP bit (§1) | 0 | 0 |
| 2 | MINSS/MAXSS/MINSD/MAXSD miss IE for a QNaN: FEX's non-AFP scalar path uses the quiet `fcmp` (1.5) | `fcmpe` in that path (fork patch) | 0 | 0 |
| 3 | DAZ + FTZ (gcc `-Ofast`): an FZ flush sets UFC without IXC, so PE is missing (1.7, 1.8) | under effective FTZ, also report UFC as PE (UE already comes from UFC) in `mxcsr_flags` and in `FexcMxcsr`. `FexcMxcsr` tests MXCSR at run time and is emitted only when `afp=0`. Under unmasked PE, add UFC to the check mask and report PE in `simd_fault_flags` | 4 per STMXCSR (a rare instruction) | the same; keyed code only for unmasked PE |
| 4 | DAZ alone does nothing, because FIZ is RES0 (1.7) | FZ = 0 and a software input flush per FP source (`cmtst`, `orr`, `and`: signed zero), in code keyed DAZ-only, following an op table (§2.1) | 0 | 3 per source |
| 5 | FTZ alone also flushes inputs, and the flush lacks IXC (1.7) | FZ = 0 and a software output flush; an `fmul` by 1−2^-p raises UFC and IXC exactly for flushed lanes (verified by the review); code keyed FTZ-only; op table §2.1 | 0 | ~5 per result |
| 6 | MIN/MAX under DAZ return the raw denormal (1.5 C) | under DAZ + FTZ, flush the selected result (3); under DAZ alone, row 4's input flush covers it | 0 | 3 per MIN/MAX |
| 7 | Unmasked SIMD DE never fires (1.6) | software denormal-input test with x86's precedence (none with a NaN operand, IE or ZE), ORed into x0 in `FexcSimdCheckWords` as FPSR bit 7. The SIMD stub path then reads `f.x[0]` as the x87 path does, not `f.fpsr` (`runtime/execution.rs:3519`); on the M4 x0 is FPSR, so it is unchanged there. The I5c check-shape rule is updated | 0 | ~10-14 per checked op |
| 8 | x87 DE for m32/m64 operands is never set (1.6) | integer test of the memory operand's bits with x87's precedence (no DE if ST0 is a NaN, if FCOM sees a QNaN (IE), or FDIVR has ST0 = 0 (ZE)). Covers FLD, the arithmetic and FCOM/FCOMP m32/m64 forms; not the FI* integer forms. Keyed on x87 DE unmasked. Masked FSW.DE falls under decision B | 0 | ~6-9 per x87 memory-operand FP instruction, keyed only |
| 9 | Unmasked UE: a result rounding up to ±MIN_NORMAL raises a spurious #XM (1.8) | **decision C** | 0 | — |
| 10 | Unmasked IE: FMA inf×0 + QNaN raises a spurious #XM (1.3) | sanitize the multiplicands where the addend is a NaN, under unmasked IE | 0 | ~5 per FMA |
| 11 | NaN results: the default NaN is +qNaN, not x86's negative indefinite ("nan" for 0/0 where x86 prints "-nan"); QNaN with SNaN picks the SNaN; FMA picks NaNs in another order; FMA and FCADD negations flip a NaN's sign (1.1-1.4) | **decision A**: an inline guard with a forward-skipped fix-up (§2.2) | **4 scalar, 5 packed, ~7 for 256-bit AVX** (2 halves), plus one copy of src1, which legacy SSE always needs | same |
| 12 | Masked DE: MXCSR.DE (SSE) and FSW.DE (x87 m32/m64) are never set (1.6) | **decision B** | 0 if accepted | ~10-14 per SSE op and ~6-9 per x87 memory op if exact |
| 13 | The masked UE flag in the MIN_NORMAL band (1.8) | **decision C** | 0 | — |
| 14 | Masked FMA inf×0 + QNaN leaves a spurious IE flag (1.3) | **decision D** | 0 if accepted | ~8 per FMA if exact |

Already right without AFP (inventory 5, last list):
- MIN/MAX vector NaN, signed-zero and IE behaviour;
- scalar-insert semantics;
- single-NaN propagation and two NaNs of the same kind;
- IE, ZE, OE and PE in general;
- inexact tiny UE outside the band;
- conversions with FZ = 0;
- every resident helper (the resident runs with FPCR = 0 on every host).

### 2.1 Op tables for rows 4, 5 and 11

Each fix applies per Arm floating-point op, from a table in the FEX shim (`FexcPrecision`-style). The table states for
every lowered op whether it gets the input flush, the output flush and the NaN guard:
- **VCVTPS2PH:** honours DAZ, ignores FTZ (`crates/abi/src/half.rs:3`).
- **Conversions to integer and ROUND:** no output flush.
- **DPPS/DPPD and HADD/HSUB:** every intermediate step.
- **MIN/MAX:** no NaN guard. They return a raw SNaN as x86 does, and `fcmeq` on it would raise IOC.
- **Scalar ops:** the guard tests lane 0 only; upper lanes often hold NaN bit patterns.
- **Conversions get no NaN guard:** FCVT, FCVTN, FCVTL, FRINT*, SCVTF and the conversions to integer.
  `FPConvert` returns `FPConvertNaN` unless FPCR.DN is set, which doesn't depend on AH, so a guard would be wrong
  across formats.

**Fix-up rules per op class (required before chunk 4b, reviewed with it).** The fix-up is defined per FEX IR op, on the
op's sources before lowering, with AH = 1's `FPNeg` keeping a NaN's sign. This matters because some lowerings negate in
a separate word: VFMLS on ASIMD is `fneg` of the addend, then `fmla`.

| Op class | Fix-up rule |
| --- | --- |
| Binary (FADD, FSUB, FMUL, FDIV, …) | source 1's NaN quieted, else source 2's, else the negative default NaN |
| Unary FSQRT | the input's NaN quieted, else the negative default NaN |
| FMA family | Rn, Rm, addend order; negations leave a NaN alone |
| FADDP (HADD/HSUB) | the even and odd elements of the concatenated sources (`uzp1`/`uzp2`) |
| FCADD #90 (ADDSUB) | operand 2's element swap plus its negation |
| By-element FMLA/FMUL | the broadcast lane |
| DPPS/DPPD | each intermediate product and sum |

The table states, for each class, the temporaries it needs within the VTMP1/VTMP2 budget. Where the destination
aliases a source, VTMP1 holds the result and the guard sits before the final `mov`, using only VTMP2. An op class that
doesn't fit the budget is refused rather than spilled.

### 2.2 The NaN guard's placement and cost (decision A)

Admission accepts only forward direct branches, and every path ends at an exit or a check stub
(`admission/structure.rs:15-20, 49-54`). An out-of-line cold path that returns into the block is a back edge, so it is
refused. Routing through the resident instead would need the state before the instruction and an SSE completion path
that doesn't exist.

The guard is therefore inline:
1. Keep a copy of each source the op overwrites.
2. Run the op.
3. Test the result: `fcmeq t, r, r`, then `uminv`, `fmov`, `cbnz` forward past the fix-up.
4. The fix-up runs only when some lane is a NaN. It applies AH = 1 semantics to the Arm op's own operands:
   - binary ops: a NaN in source 1 gives source 1 quieted, else a NaN in source 2 gives source 2 quieted, else the
     negative default NaN;
   - FMA: Rn, Rm, addend, and the negations leave a NaN's sign alone;
   - x87: the first-NaN rule.

   That gives M2 = M4 by construction, with no per-x86-instruction rules for HADD, DPPS or ADDSUB.

**Cost.** The hot path is the copy plus four instructions, and the forward branch is taken. The fix-up is about 10
words of code size per op, executed only for NaN results. Temporaries use the emitter's VTMP1/VTMP2, never
allocator values, so a guarded op needs no spill (a spill at one instruction is #UD in admission).

**x87 is cheaper:** both of x87's wrong NaN cases under AH = 0 raise IOC, and `FexcX87End` already reads FPSR, so a
single `tbnz` selects the fix-up. `FexcX87End` is generic and runs after the stores, so the fix-up is emitted per x87
instruction, with its op and a copy of the source it overwrites. That copy is a hot-path cost too, so x87 costs about 2
instructions, not 1.

IOC is also raised where the result is already right (FCOM on a NaN, FIST invalid, an SNaN load); for those
instructions the fix-up leaves the result alone.

**The rejected alternative:** detecting through FPSR.IOC for SSE too. Reading FPSR waits for every in-flight FP op, and
the FMA cases with quiet NaNs raise no IOC.

## 3. Decisions for the owner

The requirement is "x86-exact". Each of the following is either an exact fix at the stated cost or a declared
deviation from the SDM on hosts without AFP. The M4 is unaffected either way.

**A. Exact NaN results in the common mode (row 11).**
- **Exact:** the guard of §2.2, 4-7 instructions on every FP arithmetic op on hosts without AFP.
- **Deviation:** the default NaN's sign, the QNaN/SNaN choice, FMA's NaN order and NaN signs (SDM Vol.1 4.8.3.5,
  Table 4-7).
- **Recommendation:** build it in two steps (chunk 4).
  1. Measure the guard with an empty fix-up.
  2. Ship it on by default if, in M2 emulation, the floating-point-heavy existing benchmarks (`cray`, `luajit`,
     `luajit-interp`; 5 interleaved runs each) slow down by at most 3% geometric mean and at most 5% on any one, and
     `zstd`, `sqlite` and `avx` show no regression.

  Otherwise it returns to the owner with the numbers.

**B. The masked denormal flags (row 12)**, SSE's MXCSR.DE and x87's FSW.DE for memory operands.
- **Exact:** ~10-14 instructions on every SSE FP op, ~6-9 on every x87 memory-operand op.
- **Deviation:** the SDM's DE flag (Vol.1 11.5.2.2, 8.5.2) is not set by hosts without AFP while DE is masked. Unmasked
  DE stays exact (rows 7 and 8).
- **Recommendation:** accept the deviation. The flag is sticky and only observable by reading MXCSR or FSW, and the cost
  falls on all floating-point code. If needed later, an opt-in strict key can add it.

**C. The MIN_NORMAL band (rows 9 and 13).** A result whose exact value lies just below ±MIN_NORMAL and rounds to it.
- **Deviation:** without AFP, Arm judges tininess before rounding (x86: after, SDM Vol.1 4.9.1.5). So:
  - masked: the UE flag is wrongly set;
  - unmasked: a spurious #XM.
- **Exact:** needs exact recomputation in a resident slow path, a small SSE emulator, which natemu doesn't have.
- **Recommendation:** accept both as declared deviations. They need a result within half an ulp below MIN_NORMAL.

**D. The masked FMA inf×0 + QNaN IE flag (row 14).**
- **Deviation:** x86 returns the QNaN with no IE (SDM Vol.1 Table 14-17); hosts without AFP also set IE.
- **Exact:** ~8 instructions per FMA in the common mode (inventory 5.3).
- **Recommendation:** accept. It is a flag-only difference in one invalid-operand combination.

## 4. Chunks, gates and performance checks

Every chunk keeps the M4 byte-identical:
- replay of both recorded streams (`natemu-raw/cpu-c0-stream-20261115-02`) against the new fexc, 0 differences;
- a unit test that, with AFP on, `code_context`, `fpcr`, `x87_fpcr`, `mxcsr_flags` and `x87_args` are unchanged for
  every MXCSR value (all 16 bits), every FCW.RC and every combination of FCW's six mask bits. The replay gate can't see
  request bodies the host builds, so the test has to.

Every functional gate runs in M2 emulation on the M4. Each chunk includes the admission and check-shape updates its
code needs (`admission/structure.rs`). The full suite runs once, before merge.

| Chunk | Content | Gate |
| --- | --- | --- |
| 1. Emulation and baseline | the shared AFP bit; `fpcr`, `x87_fpcr` and `code_context` follow it (`mxcsr_flags` and `simd_fault_flags` in chunks 2-3); the replay body-rewrite hook; unit tests updated | M4 byte-identity (both checks above). In M2 emulation, run the AFP-dependent runners (`run_i5a_de.py`, `run_i5c.py`, `run_i6_faults.py`, `run_i8_faults.py`, `run_i5b.py images`, the FEX ASM suite) and the QEMU corpora (`gen_i5a`/`gen_i6`/`gen_i8` comparisons): record the failing cases. This is the work list, and checks the inventory's predictions |
| 2. Common-mode zero-cost fixes | rows 2, 3 | M4 byte-identity; in M2 emulation, the affected runner cases pass |
| 3. Keyed rare modes | the key bits on hosts without AFP (DAZ, effective FTZ; x87 DE unmasked already keyed), reaching both sides and fexc's arguments; rows 4, 5, 6, 7, 8, 10 and row 3's unmasked-PE part; op tables §2.1 | M4 byte-identity; in M2 emulation, `run_i5c.py` DAZ, FTZ and unmasked cases, `run_i6_faults.py` x87 DE, and the corpora's DAZ/FTZ groups pass. A common-mode replay in M2 emulation shows these rows add no code there |
| 4. NaN guard (decision A) | 4a: the guard with an empty fix-up, measured. 4b, which starts only after the per-op-class fix-up table (§2.1) has been reviewed: the fix-ups, the x87 path, op tables | 4a: the benchmark gate of decision A. 4b: in M2 emulation, the NaN cases of the corpora and runners pass, and the gate is re-measured |
| 5. Records and the M2 run | declared deviations (per the owner's decisions) in the README, the contracts and the startup line's `host -afp`; the owner runs the probe tool and the runner subset on the M2 | the M2 run matches M2 emulation |

**Performance method.** This follows the standing rules:
- benchmarks only at the end of a chunk;
- only the main session runs VMs;
- at least 5 interleaved runs per build before claiming any change under 5%.

Rows 2-10's common-mode cost is checked structurally, by a common-mode replay in M2 emulation, not by benchmark.

## 5. Risks

- **Fork changes.** Rows 2, 3-8, 10 and 11 touch FEX's JIT or natemu's FEX shim, on the fork's `natemu` branch
  (ArArgon/natemu-fex). Commits there are authored by the owner and reviewed like natemu's. Upstream FEX's contribution
  policy does not accept AI-generated code, so none of it is offered upstream.
- **Keys and arguments reaching both sides** (review finding 2). A key the host doesn't see would let the template pool
  share code across modes, which is wrong code. The shared `Cpu` bit, the unit test and a mixed-mode test in M2
  emulation (LDMXCSR switching DAZ on and off) cover it.
- **M2 emulation missing a real-M2 difference.** The pseudocode says there is none. The owner's M2 run checks it.
- **Side finding outside AFP** (inventory 1.4): FEX's ADDSUBPS fallback without FCMA flips NaN signs on every host. FCMA
  moves to the exactness class in the cpu-features records. Every supported chip has FCMA.

## 6. Progress

| Date | Chunk | State | Evidence |
| --- | --- | --- | --- |
| Nov 15 | research | done: [inventory](afp-inventory.md) | read-only; Arm ASL (Sail), code, SDM |
| Nov 15 | plan | reviewed twice; second round: accept with required changes, nothing blocks chunks 1-4a; answered (this text). The per-op-class fix-up table (§2.1) is reviewed before chunk 4b | §7 |
| Nov 15 | 1. Emulation and baseline | **code done, reviewed and accepted** (afp-c1-emulation ece3057): `Cpu::no_afp`, host-conditional `fpcr`/`x87_fpcr`, `X87_FPCR_EXPLICIT`, the probe follows `NATEMU_FEXC_FEATURE_OFF` (startup line `; host -afp`), replay hook `NATEMU_REPLAY_ADD_ARGS`. VM baseline: runners done, QEMU corpora running | exhaustive identity test (65536 MXCSR × FCW.RC × 64 masks) vs verbatim old functions; abi 110, host 387; fexc `natemu-raw/afp-c1-fexc-20261115-01` (SHA-256 89947bf8...); replay vs the cpu-c3 fexc: installer 124,024 and build 245,572 requests, 0 differences (`replay.txt` there); baseline `natemu-raw/afp-c1-baseline-20261115-02`: installer boots in both modes (M2 emulation prints `; host -afp`); in M4 mode every runner passes (i5a-de, i5c, i6-faults, i8-faults, i5b images, FEX ASM); in M2 emulation the work list (`worklist.txt`): i5a-de 98 of 351 (DE across SSE forms), i5c 39 of 255 (DE, MAXSS QNaN IE, FTZ PE, DAZ, FMA inf×0+QNaN, VCVTPS2PH denormal, x87 FLD/FST m64 DE), i6-faults 2 of 302 (x87 FLD m64 DE, FXAM after a denormal load), i8-faults 8 of 846 (VEX DE), FEX ASM 1 (DAZTest); i5b images pass. Matches the inventory's predictions; the three unpredicted names (fstp-m64-ue-exact ×2, fxam-denormal-double) are the same x87 memory-operand DE |
| Nov 15 | 1. baseline (corpora) | QEMU-compared corpora in M2 emulation: 16 of 18 steps pass (fix, seed, i9, i9-big, sha, adx, xsave, i10, i7, i6b, i6b-cases, flow, corpora-c2/c3/c4); **i8 56 of 136 and i8b 111 of 336 fail** (AVX/FMA forms; classification by cause running) | `afp-c1-baseline-20261115-02/corpora-m2` |
| Nov 15 | 2. Common-mode zero-cost fixes | **done, merged** (review accepted; FEX `13e80cf` pushed to the fork's `natemu` branch as a fast-forward): row 2 `fcmpe` in FEX's non-AFP scalar MIN/MAX; row 3 UFC→PE under effective FTZ in `mxcsr_flags` and `FexcMxcsr` (4 instructions, emitted only when `afp=0`) | fexc `natemu-raw/afp-c2-fexc-20261115-01` (SHA-256 06628fcc...); replay vs afp-c1 fexc 0 differences on both streams; identity test over 65536 MXCSR × 256 FPSR; run_i5c M4 255/255; M2 emulation 220/255: exactly `ftz-ue-masked` and the three `maxss-qnan` cases fixed, nothing new (`afp-c2-check-20261115-01`) |

## 7. Review response (independent review, November 15)

| # | Finding | Answer |
| --- | --- | --- |
| 1 | Blocking: the out-of-line cold path is a back edge, which admission refuses; the cost is too low; the x87 fix "for both hosts" breaks M4 byte-identity | §2.2: the guard is inline with a forward-skipped fix-up and costs 4/5/~7 plus the copy; the fix-up applies AH = 1 to the Arm op's operands; COR-09 on the M4 is out of scope |
| 2 | Blocking: the AFP bit and the keys must reach the host's own `code_context` and fexc's arguments | §1: a shared `Cpu` bit; §4: a unit test over every MXCSR and FCW.RC with AFP on; §5: a mixed-mode test |
| 3 | Blocking: the SIMD stub path reads `f.fpsr`, so a software IDC is lost | row 7: read `f.x[0]`; check-shape rule updated |
| 4 | Unmasked PE under DAZ + FTZ is missed | row 3: UFC added to the mask under unmasked PE, keyed |
| 5 | Row 8's precedence, forms and cost; it cost something in the common mode | row 8: precedence and forms stated, ~6-9, keyed on unmasked x87 DE; masked FSW.DE moved into decision B |
| 6 | Row 3 needs the FTZ key from chunk 3 | row 3: a run-time MXCSR test in `FexcMxcsr`, emitted only when `afp=0`; the unmasked-PE part moves to chunk 3 |
| 7 | Cheaper designs: flush MIN/MAX's result; x87 `tbnz`; no separate negation handling | rows 6 and 11, §2.2 |
| 8 | Op tables needed | §2.1 |
| 9 | Register pressure | §2.2: VTMP1/VTMP2 at the emitter level, no allocator values |
| 10 | Decision A's gate is diluted by non-FP benchmarks; measure the guard first | decision A: gate on `cray`, `luajit`, `luajit-interp`; chunk 4a |
| 11 | Row 9's slow path hides an SSE emulator | decision C |
| 12 | Concessions decided without the owner; decision B argued from one guest | §3: decisions B, C and D; each states the SDM deviation and the exact cost |
| 13 | Admission is never mentioned | §4: each chunk includes its admission and check-shape updates |
| R1 | Second round: `Cpu` bit polarity and when it is set | `no_afp` (zero = today); set once from `supported` after the override; asserted in `x87_args` |
| R2 | The test must cover FCW masks and `x87_args` | §4: all MXCSR bits × FCW.RC × the six masks, including `x87_args` |
| R3 | "AH = 1 on the Arm op's operands" is undefined across multi-word lowerings, FADDP/FCADD/by-element/DPPS, FSQRT and conversions | §2.1: per-IR-op fix-up rules with the temporaries budget, reviewed before chunk 4b; FSQRT's rule; conversions get no guard |
| R4 | x87 fix-up needs the operands; IOC also arises where the result is already right | §2.2: per-instruction fix-up with a copy (~2 instructions); IOC-but-correct cases left alone |
| R5 | Row 3's common-mode cost | ~3 per STMXCSR |

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

**Owner (November 15, after chunk 3b):** B, C and D were never decided; they are recommendations only. The owner's
direction: make the simple ones exact (D, and C's unmasked case), then look for efficient ways to make the rest exact
(B, C's masked flag, C's DAZ + FTZ result); only if their cost stays too high do they become documented deviations.
The NaN guard of A is being measured (§6). D and RSQRT's NaN sign ride on A's out-of-line stub (chunk 4b); C's
unmasked case is a resident recomputation at the existing #XM stub.

**Owner (November 16, after the second NaN-guard measurement and the exactness research, §6):** two floating-point
modes, **fast** (the default) and **accurate**; accurate (80-bit) x87 joins accurate mode. Fast mode keeps the cheap
exact fixes (on hosts without AFP: C unmasked; x87 masked DE for m32/m64; x87 NaN results; C's masked flag if the M2
has FP traps) and documents the rest as deviations on hosts without AFP. Accurate mode (a launch option, part of the
compile key, so fast-mode code is unchanged by its existence) holds A (with D and RSQRT's NaN sign), B for SSE, C's
masked flag (if no traps), C's DAZ + FTZ result, and accurate x87 on every host. It gets its own reviewed plan; its
cost is measured and documented, not gated.

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
  - unmasked: a spurious #XM;
  - under DAZ + FTZ together (gcc `-Ofast`), which runs on the hardware flush (FZ = 1), the result itself is wrong:
    0 instead of ±MIN_NORMAL (chunk 3a review). FTZ alone flushes in software and gets the result right.
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
| Nov 15 | 1. baseline (corpora) | QEMU-compared corpora in M2 emulation: 16 of 18 steps pass (fix, seed, i9, i9-big, sha, adx, xsave, i10, i7, i6b, i6b-cases, flow, corpora-c2/c3/c4); **i8 56 of 136 and i8b 111 of 336 fail** (AVX/FMA forms). All 167 are explained (an offline model reproduces natemu's M2-emulation output on all 472 programs; `i8-m2-classes.md` there). Programs per cause, overlapping, i8/i8b: DAZ ignored or FTZ flushing inputs 34/68 (chunk 3); default NaN sign 20/36, NaN sign flipped by a negation 31/41, FMA three-NaN order 16/32, two-NaN choice 0/1 (decision A); FMA inf×0 + QNaN 2/1 (its result is fixed by decision A's guard, its flag is decision D). No flag-only cause and no instance of the tininess band | `afp-c1-baseline-20261115-02/corpora-m2` |
| Nov 15 | 2. Common-mode zero-cost fixes | **done, merged** (review accepted; FEX `13e80cf` pushed to the fork's `natemu` branch as a fast-forward): row 2 `fcmpe` in FEX's non-AFP scalar MIN/MAX; row 3 UFC→PE under effective FTZ in `mxcsr_flags` and `FexcMxcsr` (4 instructions, emitted only when `afp=0`) | fexc `natemu-raw/afp-c2-fexc-20261115-01` (SHA-256 06628fcc...); replay vs afp-c1 fexc 0 differences on both streams; identity test over 65536 MXCSR × 256 FPSR; run_i5c M4 255/255; M2 emulation 220/255: exactly `ftz-ue-masked` and the three `maxss-qnan` cases fixed, nothing new (`afp-c2-check-20261115-01`) |
| Nov 15 | 3a. Keyed DAZ/FTZ | **done, merged** (review accepted after one round of fixes; FEX `7e8f5ed` pushed to the fork's `natemu` branch as a fast-forward): `daz_key`/`ftz_key` in `code_context` bits 30/31 and fexc `--daz`/`--ftz` (no AFP only); FPCR.FZ only with DAZ + effective FTZ; row 4 input flush (5 instructions per source), row 5 output flush with UFC+IXC per flushed lane (7 per result), row 6 MIN/MAX result flush, row 3 unmasked PE under FTZ. Review fixes: DPPS/DPPD step checks follow their flush; FEX's scalar FMA wrote the upper bits into a register still holding a source when the flush moved its result (also a latent M4 miscompile for destination = addend, never hit by the recorded streams). Intermediate commits pin the pre-amend FEX `e9d27c7`, which is not on the fork | fexc `natemu-raw/afp-c3a-fexc-20261115-02` (SHA-256 c13a1de0...); replay vs afp-c2 fexc: M4 0 differences on both streams; `afp=0` common mode installer 0, build 23 differing only in `args_read`; abi 112, host 388, test_slice1 48. VM `afp-c3a-check-20261115-02`: M4 all runners and FEX ASM pass (i5c 257 incl. two new mixed DAZ/FTZ cases); M2 emulation i5c 224/257 (`daz-de-unmasked`, `daz-ze-unmasked` fixed), FEX ASM all pass (DAZTest fixed), i5a-de/i6/i8-faults unchanged (3b); corpora i8 39/136 and i8b 77/336 fail (from 56/111), every remaining failure has a NaN cause (decision A) |
| Nov 15 | 3a. real x86 check | DPPS intermediates on the AMD Ryzen 7 8845HS (user mode; `afp-c3a-check-20261115-02/dpps-amd.c`): under DAZ a denormal product enters the add as zero, with no flag (2^-70 × 2^-70 gives 0, MXCSR 1FC0; without DAZ 0x200 and DE); under FTZ the product is flushed with UE+PE (MXCSR 9FB0). Chunk 3a does the same (DAZ: the first add level's inputs are flushed; FTZ: each step's output) | |
| Nov 15 | 3b. Keyed unmasked DE and FMA IE | **done, merged** (review accepted after two rounds of fixes; FEX `62713e6`, `8254266` pushed to the fork's `natemu` branch as a fast-forward). No new key: code only when `no_afp` and the exception is unmasked. Row 7: software denormal-operand test with x86's precedence (none beside a NaN, ZE, negative SQRT, inf×0, inf−inf; half-precision sources never), flag ORed into the check word (`FexcSimdCheckDe`), the resident's SIMD stub reads `x0`; DPPS/DPPD add levels test products and sums (`FexcDenormalStep`). Row 8: x87 arithmetic, FCOM/FCOMP, FLD m32/m64 (`FexcX87CheckDe`). Row 10: non-NaN multiplicands zeroed in lanes with a NaN addend. Keyed cost above the plan: row 7 8-20 instructions (FMA 44), row 8 20-30, row 10 7-8. Review fixes: VCVTPH2PS (half source) got invalid code; DPPS/DPPD missed DE for an exact denormal product. Worklist sorted: of 141 failing lines after 3a, 10 were rows 7/8/10 and 131 are masked DE (decision B). **Found on the M4 (not AFP):** x87 FADD/FDIV/FMUL m32 with a QNaN in ST0 sets DE (FEX converts the single in a separate op); real x86 sets nothing (AMD, `afp-c3b-check-20261115-01/x87de-amd.c`); M2 emulation is right; needs its own fix | fexc `natemu-raw/afp-c3b-fexc-20261115-03` (SHA-256 83972fb1...); replay vs afp-c3a fexc 0 differences on all four streams (M4 and `afp=0` common mode); abi 112, host 389, test_slice1 61 (native runs vs M4 hardware IDC/IOC). VM `afp-c3b-check-20261115-03`: M4 all runners pass (i5c 305 incl. 48 new cases), FEX ASM pass; M2 emulation i5c 282/305 (10 fixed, the rest masked DE), i5a-de/i6/i8-faults unchanged (masked DE), corpora unchanged (i8 39, i8b 77, all NaN causes) |
| Nov 15 | 4a. NaN guard, first measurement | **gate failed; redesign** (afp-c4a-guard ea32e2a, not merged). One round in M2 emulation (`afp-c4a-diag-20261115-01/bench-round1.jsonl`, warm run): cray **+96%**, luajit +7.2%, luajit-interp +0.4%, zstd -0.5%, sqlite +0.6%, avx +1.4%; guard switched off = baseline. Cause (native loop of cray's hot block, `afp-c4a-diag-20261115-01`): FP/SIMD issue throughput, not latency or branches; the guard's `fcmeq` + `fmov` double the block's FP/SIMD ops. **Found:** without AFP every scalar op already costs an extra `ins` to keep bits 127:64 (no NEP), which makes the block 1.9x slower than the AFP code (4.57 vs 2.42 ns). Measured shapes: integer-side test +60%, `fcmp`+`b.vs` +49%, `ins` elided where the upper half is known zero + `fcmp`/`b.vs` +2%. Inline fix-ups cost even when skipped, so 4b's fix-up moves out of line (stub exit, the resident steps the instruction compiled with the fix-up). Owner (Nov 15): I drive the fix: first the known-zero-upper `ins` elision (a general speedup for hosts without AFP), then the guard in the cheap shape, re-measured | |
| Nov 15 | Known-zero upper bits (no `ins`) | **done, merged** (independent review accepted, no findings; FEX `726f3c0` pushed to the fork's `natemu` branch as a fast-forward). Without AFP, a scalar SSE/AVX op whose upper-bits source is known zero above the element writes its register directly instead of computing into a temporary and inserting. Per-block forward analysis on FEX's IR before register allocation (`FexcPrecision::MarkUpperZero`): per value, the lowest byte from which bits are known zero; known zero comes from scalar loads, `movd`/`movq`, zeroing idioms, copies, merges into known-zero registers, logic ops and the results of such scalar ops; a guest register read uses the value stored earlier in the block, nothing is known at block entry. Applies to any scalar FP code whose values start in the same block; values from an earlier block keep their `ins` | fexc `natemu-raw/afp-upper-zero-fexc-20261115-01` (SHA-256 902c53c7...); replay vs afp-c3b fexc: M4 0 differences on both streams. Native differential tests (220 random blocks + adversarial cases; the review's fuzzer 1,660 cases): bit-identical to the old compiler and to the AFP code. cray's hot block 4.57 -> about 2.4 ns (AFP code 2.42). Benchmarks in M2 emulation, 5 interleaved rounds (`afp-upper-zero-bench-20261115-01`): cray -19.6%, luajit -1.8%, luajit-interp -0.9%, zstd -0.8%, sqlite +0.2%, avx -0.2%. VM `afp-upper-zero-check-20261115-01`: M4 all runners and FEX ASM pass; M2 emulation identical failure sets to 3b (runners and the i8/i8b corpora); all other QEMU corpora pass |
| Nov 16 | 4a. NaN guard, second design | **fails the gate; not merged** (afp-c4a-stub 7f24813, FEX `cc11e48` local). Cheapest exact shape: `fcmp`+`b.vs` where NZCV is dead (FEX flag liveness), else an integer-side test after `fmov`; packed results reduced with `fmaxp`/`fmaxv`; branch to a new out-of-line stub (site kind NAN) where the resident steps the instruction compiled without the guard. Native, against the known-zero-upper code: cray hot block **+170%** (2.72 -> 7.35 ns), a packed block +139%, a LuaJIT-like scalar block +66%. The result's temporary and move alone cost +38% (the guard needs the pre-instruction state at the stub); slice1 keeps NZCV live at memory instructions, so the `fmov` shape dominates. Any exact per-instruction test costs at least one FP/SIMD op plus the move | `natemu-raw/afp-c4a-stub-fexc-20261115-01` (native/, replay: M4 0 differences) |
| Nov 16 | Exactness research (B, C masked, C under DAZ + FTZ) | `natemu-raw/afp-exactness-research-20261116-01`. Per-block FPSR read + branch +72% on cray's hot block (FPSR schemes ruled out); a never-taken branch per FP op +77%. B (SSE masked DE): exact needs integer tests of every FP input not produced by an FP op; the loads alone cost +43-55%. x87 masked DE for m32/m64: about 5% on x87 memory ops only (x87 already reads FPSR). C under DAZ + FTZ: exact needs the software flush, at least +90%. C masked flag: the only cheap design found uses FPCR.UFE hardware traps (precise on the M4 with AH = 0, +4% when enabled), but the M2's records list synchronous FP exceptions as M4-only (cpu-features appendix), so it likely cannot run on M1/M2; the owner's M2 test (`trap_test`, `trap_vec` there) decides |
| Nov 16 | Fast mode: C unmasked (exact tininess after rounding) | **done, merged** (independent review accepted; its two non-blocking findings fixed and re-reviewed). Without AFP and with MXCSR.UM clear, a new IR op `FexcKeep` keeps the operands of a checked multiply, divide, FMA or narrowing convert live to the check; the stub site names their registers, and at the stub the resident decides per lane with an integer model of x86's tininess after rounding (`crates/abi/src/tiny.rs`; all rounding modes, f32/f64). No tiny lane and nothing else unmasked: the instruction resumes after the check with PE and no UE; other unmasked flags: #XM without UE. Add/sub and square root never give the spurious case (reviewed). Review fixes: no description in a compile with spill slots (a fill could reuse a named register; the check then keeps its old behaviour); the list is cleared on a JIT restart. Test fix: band cases use normal operands only (a denormal operand correctly sets masked DE on the M4). Code only in that keyed mode | reviewer's fractions reference vs the model: ~935,000 cases, 0 mismatches; vectors vs M4 hardware (`tools/models/tiny_vectors_hw.c`). VM `afp-tiny-check-20261116-03`: M4 i5c 361/361, M2 emulation 338/361 (all band cases pass; the rest masked DE) |
| Nov 16 | Fast mode: x87 masked DE (m32/m64) and x87 NaN results | **done, merged** (independent review accepted). Without AFP: chunk 3b's software denormal test also runs with DE masked (x87 arithmetic, FCOM/FCOMP, FLD m32/m64), setting FSW.DE; a fix-up after `FexcX87End`, skipped by `tbz` on IOC, gives FADD/FSUB/FSUBR/FMUL/FDIV/FDIVR (all forms), FSQRT and FLD m64 the M4's NaN result (first NaN operand quieted, else the negative real indefinite). The two-NaN choice stays the M4's first-operand rule (an existing recorded x87 deviation on every host). Helper ops (FPREM, FSCALE, transcendentals) unaffected. Admission: the fix-up's forward branches inside an x87 slot's mask-to-access span are admitted, but none may target the access itself (a security review found that the first relaxation let a branch skip the address `add`; fixed with a refusal test). The first commit crashed fexc (IR read after register allocation); fixed. Cost: a dependent chain of 8 m64 ops +1.4%, register forms +1-2%; the masked DE test adds ~13-17 instructions per x87 memory-operand op, so an independent-op loop may pay more (research estimate ~5% on x87 memory ops) | native oracle `AfpX87` (afp=0 code vs AFP code on M4 hardware, 12 operand classes): identical except the known M4 m32 DE bug. VM `afp-x87-check-20261116-01`: M4 all pass (10 m32 cases of the known M4 bug skipped); M2 emulation i6-faults 302/302 (fixed: FLD m64 DE, FXAM after a denormal load), i5c fixed the two FSTP m64 cases |
| Nov 16 | Both merged together | FEX `791857f` (`FexcKeep`, the `FexcX87NanOp` declaration, the restart clear; on `726f3c0`) pushed to the fork's `natemu` branch as a fast-forward; gitlink and pin bumped | fexc `natemu-raw/afp-cheap-fexc-20261116-01` (SHA-256 a30de48d...; a later commit changes a comment only); replay vs afp-upper-zero fexc 0 differences (M4 and `afp=0`, both streams); abi 114, host 390, test_slice1 71. VM `afp-cheap-check-20261116-01`: M4 every runner passes (i5c 411/411), FEX ASM pass; M2 emulation nothing new fails (i5c 396/417, i5a-de 253/351, i8-faults 838/846: all masked DE; i6-faults 302/302); corpora i8 39, i8b 77 unchanged (all NaN causes, accurate mode) |
| Nov 16 | M4 (AFP) x87 m32 DE | **done, merged** (independent review accepted, no findings). On hosts with AFP, FEX converts an m32 operand in a separate `fcvt` whose input-denormal flag reached FSW.DE where x86 sets nothing: a NaN in ST0, or FDIVR m32 over a zero ST0 (ZE only). Fix in the shim only: after the arithmetic or compare of an instruction with an m32 source, one `tbz` on the pending DE bit; with DE set it is dropped when the op raised divide-by-zero or the result (compare: either operand) is a NaN. Forms: m32 FADD/FSUB/FSUBR/FMUL/FDIV/FDIVR, FCOM/FCOMP; FLD m32 and m64 already right. Code without AFP unchanged. Cost within noise (one never-taken branch). run_i5c's `m4_bug` skips removed | fexc `natemu-raw/afp-m4x87-fexc-20261116-01` (SHA-256 c6788581...); replay vs the afp-cheap fexc 0 differences (M4 and `afp=0`; the streams hold no x87 m32 ops); native oracle `AfpX87` holds the M4 to x86's rule (fails on the old fexc). VM `afp-m4x87-check-20261116-01`: M4 i5c 417/417, i6-faults 302/302; M2 emulation nothing new (i5c 396/417, masked SSE DE; i6-faults 302/302) |
| Nov 16 | Startup lines and test hygiene | **done, merged** (each independently reviewed; required fixes made and re-reviewed). Startup output (owner's format): one line per decision, `natemu: cpu:`, `fp:` (`fast mode`, `, degraded (host lacks FEAT_AFP)` without AFP), `acceleration:` (+/- per speed feature, Arm names), `meltdown:`, `code cache:` (pool, disk size and path); `; host -afp` removed; tools recognise only these five topics (a review found error lines of the same shape). Test hygiene (accurate-mode plan chunk 0, [its plan](afp-accurate-plan.md)): one registry of fast-mode deviations, the same list in the README; on a host without AFP in fast mode a tagged case must show exactly the documented deviated result (runner cases: MXCSR with DE cleared; corpus programs: the offline model's state, which matches QEMU on all 472 programs in its QEMU mode); tagged-but-exact fails; the M4 judges everything exactly | VM `afp-accurate-c0-check-20261116-01`: M4 every runner and corpus passes; M2 emulation 0 failures: i5c 21, i5a-de 98, i8-faults 8 deviated as documented, i8 39, i8b 73 (plus its 7 known QEMU departures); boots print the five lines in both modes |
| Nov 16 | full suite | **171/173** at the branch tip (rebased on cpu-features): first run 165/173 (`natemu-raw/afp-fallback-suite-20261116-01`): besides the known `io-coverage`/`io-coverage-pci` `host-full` case, `i9-faults`, `i10-faults` and `maskmovq` (and their `-1` shards) failed because test hygiene changed `run_i8_faults.judge`'s return type and those runners import it; fixed (`judge_kind` for the pair), the six steps rerun and pass (`-02`); the other 165 steps do not use the changed function | ready
| Nov 16 | completion benchmark round | cpu-features (b5702bd, now main) vs afp-fallback, 11 benchmarks, N=1, 5 interleaved rounds, 220 boots all verified, none contended. M4: every median within ±1.5% (cold: build +0.9, zstd +0.9, hackbench −0.3, locks −1.5, luajit +0.7, luajit-interp −0.3, mem +1.1 (one +10% outlier run), cray +0.1, 7zip −0.9, sqlite +0.1, avx +0.2) — no change, as the byte-identical replays predict. M2 emulation: cray −20.6%, luajit −9.3%, sqlite −4.6%, hackbench −3.9%, locks −3.3%, luajit-interp −3.0%, mem −1.7%, build/zstd/7zip/avx within ±0.3%. cray and luajit: the known-zero upper bits. sqlite/hackbench/locks are mostly integer work and their 3-5% is unexplained; cpu-features' M2-emulation runs keep FPCR.AH = 1 (its emulation predates chunk 1), so that side is not a strict M2 model | `natemu-raw/afp-bench-20261116-01` (bench-ab.sh, runs/bench.jsonl, tables.txt) |

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

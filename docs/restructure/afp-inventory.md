# AFP fallback inventory (natemu on hosts without FEAT_AFP)

Read-only research, November 15. Worktree `cpu-features` (natemu), FEX at `third_party/FEX` (commit 1df70f0, the
submodule's pin). Nothing was run except reading code and the Arm pseudocode.

**Sources and how far each claim goes.**
- **Arm rules** come from the DDI0487 shared pseudocode (ASL). A local copy wasn't available, so I read Sail's
  machine translation of the Armv9.4 ASL, `rems-project/sail-arm`, `arm-v9.4-a/src/v8_base.sail` and `instrs64.sail`.
  They're fetched to `scratchpad/armxml/`, and line numbers below are lines in that file. They're cited as `ASL <function>`.
- **Intel rules** come from the SDM text as natemu already quotes it, from natemu's tests (the SDM-judged runners), and
  from natemu's records. Where neither covers a case, it is marked *inferred*.
- **Tags:** "verified" means read in code or the pseudocode; "inferred" means reasoned from what was read but not
  observed on hardware.

## 0. The key fact: an M4 with AH, FIZ and NEP cleared behaves as an M2 (verified in the ASL)

All 36 `HaveAltFP()` uses in `v8_base.sail` behave like a machine without AFP when FPCR.AH, FIZ and NEP are clear:
- most are combined with `fpcr[AH] == 0b1` (`FPProcessDenorm(s)`, `FPProcessNaNs`, `FPRoundBase` 15693, `FPMax/Min`,
  `FPMaxNum/MinNum`, `FPMulAdd` 16955, the estimates, `FPToFixed`, `FPRoundInt`);
- one with `fpcr[FIZ]` (`FPUnpackBase` 15952);
- `IsMerging` uses NEP;
- `FPDefaultNaN`'s sign is `fpcr[AH]` itself (16131);
- `FPNeg`/`FPAbs` test AH inside (16100, 17223).

So the M4 with FPCR.{AH,FIZ,NEP} = 0 and fexc `afp=0` (chunk 2's `NATEMU_FEXC_FEATURE_OFF=afp`) is architecturally
the M2's floating-point machine. The fallback can be built, tested and benchmarked on the M4, with one confirming run on
the M2.

Today, forcing `afp` off changes only FEX's code. The resident still writes AH and NEP (`execution::fpcr`), which is why
`cpu-features-plan.md:217` calls that combination unreal. Making `fpcr`/`x87_fpcr` depend on the host fixes that.

RPRES plays no part: natemu's fexc table has `rpres=false` (`tools/fexc/fexc.cpp:70`), so FRECPE/FRSQRTE are never
emitted. RCP*/RSQRT* are FEX's exact division with FPSR saved around it (`fexc_sites.inc:66-72`).

## 1. How AH = 1, FIZ and NEP change results and flags, op by op (ASL verified unless marked)

FPCR as natemu sets it today (`crates/abi/src/execution.rs:1894-1897`):
- AH = 1, NEP = 1, FIZ = MXCSR.DAZ;
- FZ = FTZ & (UE masked);
- RMode from RC;
- DN = 0, no trap enables.

The x87 FPCR (`execution.rs:1979-1982`) has AH and NEP, RMode from FCW.RC, and no FZ/FIZ.

On a host without AFP, AH/FIZ/NEP are RES0, so the M2 runs with AH = 0, FIZ ignored (DAZ has no effect at all), and
FZ = FTZ & UM-masked.

### 1.1 NaN propagation with two operands (ADD/SUB/MUL/DIV, HADD/HSUB pairs, DPP steps)

- **ASL** (`FPProcessNaNs__1`, 16205-16240):
  - AH = 1: if both are NaNs, return op1 quieted, IOC if either is an SNaN.
  - AH = 0: op1 SNaN, else op2 SNaN, else op1 QNaN, else op2 QNaN.
- **x86 SSE:** SDM Vol.1 Table 4-7 returns the first source operand, quieted, for two NaNs. natemu's records:
  `boot-features-plan.md:999`, `run_i8_faults.py:175`.
- **Difference:** AH = 0 differs from AH = 1 (and from x86) only when op1 is a QNaN and op2 is an SNaN. Verified case by
  case:
  - op1 SNaN: both give op1;
  - op1 non-NaN: both give op2;
  - both the same kind: both give op1.

  AH = 0 then returns quiet(op2). IOC is set either way, so the flags are equal; only the result differs.
- **x87:** the rule is different again (SNaN with QNaN gives the QNaN; two of the same kind give the larger significand).
  natemu uses AH = 1's first-operand rule as a recorded deviation (`boot-features-plan.md:1298` item 6; COR-09 open in
  `docs/restructure/correctness-backlog.md:26`). AH = 0 is a different wrong answer: QNaN with SNaN gives the SNaN,
  opposite to x87.
- **Reliance:** natemu's tests (`nan2-*` in `run_i8_faults.py`) only use two QNaNs, so they pass under AH = 0. The
  generated code does rely on AH = 1 for the QNaN-with-SNaN case.

### 1.2 The default NaN's sign: a large, guest-visible difference in the default mode

- **ASL** `FPDefaultNaN__1` (16127-16136): sign = FPCR.AH. AH = 0 gives +qNaN (0x7FC00000 / 0x7FF8...); AH = 1 gives
  −qNaN (0xFFC00000 / 0xFFF8...).
- **x86:** the "real indefinite" is negative, for both SSE and x87 (natemu's constants: `x87.rs` `INDEFINITE =
  0xfff8...`; `run_i6_faults.py:148`).
- **Which operations:** every invalid operation without a NaN operand: inf−inf, 0×inf, 0/0, inf/inf, sqrt of a negative,
  FMA's invalid cases, and also x87 arithmetic.
- **Guest-visible:** glibc prints "-nan" on x86 for `0.0/0.0`, and would print "nan" on an M2.
- **Status:** not listed in the M2 failure note, probably because the QEMU-referenced corpora never ran there (they need
  the M4's natemu-raw). Inferred.

### 1.3 FMA (VFMADD/VFMSUB/VFNMADD/VFNMSUB, scalar and packed)

- **Three-NaN selection** (`FPProcessNaNs3__1`, 16863-16918; the positions are (addend, Rn, Rm)):
  - AH = 1: with two or more NaNs, Rn first, then Rm, then the addend, regardless of SNaN or QNaN.
  - AH = 0: SNaNs first in the order addend, Rn, Rm, then QNaNs in the same order.

  natemu's results matched QEMU with AH = 1 (`boot-features-plan.md:2237`), so x86's rule is AH = 1's for FEX's operand
  mapping. Under AH = 0 the result differs whenever the addend and a multiplicand are both NaNs, or an SNaN comes after a
  QNaN. Two QNaNs are enough, so no IOC is raised and an IOC-only fix-up would miss it.
- **inf×0 + QNaN** (`FPMulAdd__1` 16955-16963): AH = 0 gives the default NaN (+) and IOC; AH = 1 returns the QNaN addend
  with no IOC. natemu's expectation (`run_i5c.py` `rv-fma-inf0-qnan-ie`, "no IE: SDM Vol.1 Table 14-17") equals AH = 1.
  On an M2 this means a wrong result and a spurious IE: masked it is a wrong flag, unmasked a spurious #XM.
- **Negations and NaN sign** (`instrs64.sail` float_arithmetic_mul_add_sub 17467ff, `FPNeg` 17220):
  - FMSUB/FNMADD/FNMSUB and vector FMLS apply `FPNeg` to Ra and/or Rn. Under AH = 1, FPNeg leaves a NaN unchanged;
    under AH = 0 it flips the NaN's sign.
  - FEX's lowerings (`JIT/VectorOps.cpp`): VFMLS vector uses `fneg` + `fmla` (4655-4660); scalar `fnmsub` (4638);
    VFNMLA uses `fmls`/`fmsub`; VFNMLS uses `fneg` + `fmls` / `fnmadd`.
  - natemu's VFMADDSUB/VFMSUBADD patch (`OpcodeDispatcher/AVX_128.cpp:2077-2082`, `FexcNegate`) chose `fneg`
    *because* AH = 1 keeps a NaN's sign.
  - On AH = 0, a NaN in a negated operand comes out with its sign flipped. x86 returns the NaN operand as is (natemu's
    record; QEMU matched on the M4).

### 1.4 ADDSUBPS/ADDSUBPD with FCMA (`fcadd #90`, `JIT/VectorOps.cpp:4477-4516`)

- **ASL:** FCADD negates one element of operand 2 with `FPNeg`.
- **AH = 0:** a NaN in the subtracted lane of src2 has its sign flipped; x86 doesn't negate a NaN. Inferred from the ASL;
  the same class as 1.3.
- **The FCMA-off path** (XOR of the sign mask, `Vector.cpp:3581`) flips the NaN's sign on every host. This is a side
  finding for chunk 2's FCMA-off gate, not an AFP item.

### 1.5 FMIN/FMAX versus FEX's non-AFP min/max

- **AH = 1 FMIN/FMAX** (`FPMin__1`/`FPMax__1` 16626ff): any NaN gives op2 and IOC; zeros of opposite sign give op2; the
  output is never flushed. That is x86 MINPS/MAXPS.
- **FEX without AFP:**
  - vector: `fcmgt` + `bif`/`bit` (`VectorOps.cpp:1481-1499`, `1540-1556`);
  - scalar: `fcmp` + `fcsel` (`VectorOps.cpp:443-466`, `470-492`).
- **What already matches x86:** NaN → src2 (returned raw, so an SNaN src2 is not quieted, as x86 does), equal values or
  zeros → src2. Vector IE for a QNaN also matches: FCMGT signals on any NaN (`FPCompareGT` 16403).
- **Difference A (verified in code), the scalar forms:** `fcmp` is the quiet compare (`FPCompare(signal_nans=false)`:
  IOC only for an SNaN), so MINSS/MAXSS/MINSD/MAXSD with a QNaN operand miss IE. x86 lists "Invalid (including QNaN
  source operand)". `run_i5c.py` `maxss-qnan` expects IE.
- **Difference B, DE:** FCMGT/FCMP call `FPProcessDenorms` only when AH = 1, so there is no DE (see 1.6).
- **Difference C, DAZ:** the compare flushes (FZ = 1 under AH = 0), but `bif` returns the *raw* denormal bits. x86 DAZ
  returns a signed zero (natemu's decision: `boot-features-plan.md:1000-1001`, `gen_i5a.py:156`). With DAZ alone on an
  M2 nothing flushes at all.

### 1.6 The denormal-input flag IDC (DE)

- **ASL:**
  - `FPProcessDenorm(s)(3)` (16141, 16463, 16851) raise IDC for an unflushed denormal input only when AH = 1, and are
    called after NaN processing and outside the invalid paths. That gives x86's precedence: none with a NaN, IE or ZE.
  - The `FPUnpackBase` flush (15997-16006, 16041-16050; flush branch `fz | fiz`) raises IDC only when `fz`, i.e. FZ = 1 and AH = 0. FIZ never
    raises it.
  - `FPToFixed`, `FPRoundInt` and the estimates unpack with `fpexc = !altfp` (17848-17849, 17472-17473): under AH = 1 they raise no
    IDC even when flushing; under AH = 0 with FZ = 1 they raise IDC for a flushed input.
- **AH = 0, FZ = 0** (the default mode on an M2): **IDC is never set**, so MXCSR.DE (SSE) and FSW.DE (FLD m32/m64,
  generated x87) are never reported.
- **AH = 0, FZ = 1** (FTZ today on an M2): IDC means a flushed input, for every op including conversions and rounding,
  where x86 has no DE.
- **x86 SSE:** DE for a denormal operand unless DAZ, with the precedence above; none for CVT-to-integer, ROUND*, RCP*,
  RSQRT* (`tools/run_i5a_de.py` docstring).
- **x87:** FLD m32/m64 of a denormal and m32/m64 arithmetic operands give DE. A double denormal held in a register is
  an 80-bit normal and gives no DE: natemu's deviation 4, `boot-features-plan.md:1289-1292`, which is the AH = 1
  artefact. So under AH = 0 x87 register arithmetic is actually *closer* to x87; only the memory operands lose DE.
- **Reliance:** natemu relies on AH = 1 for every DE it reports (`mxcsr_flags` doc, `execution.rs:1951-1956`).

### 1.7 Flushing inputs versus outputs (FZ, FIZ)

| Mode | AH = 1 (M4) | AH = 0 (M2) |
| --- | --- | --- |
| FZ | flushes outputs only (`FPRoundBase` 15764-15772; `FPUnpackBase` `fz` false) | flushes inputs **and** outputs (`FPUnpackBase` 15953, `FPRoundBase` 15694) |
| FIZ | flushes inputs only, no IDC | RES0 |

x86 behaviour:
- DAZ: flush inputs, no DE.
- FTZ (UE masked): flush tiny-after-rounding outputs to zero and set UE and PE (SDM Vol.1 10.2.3.3; `run_i5c.py:71`).

What each MXCSR mode gives on an M2:
- **DAZ only:** nothing flushes. Results for denormal inputs are wrong; x86's lack of DE matches (none either way).
- **FTZ only:** inputs are flushed (wrong), IDC → DE for conversions and ROUND (wrong), outputs flushed before rounding
  with UFC only.
- **DAZ + FTZ** (gcc `-Ofast`/crtfastmath): inputs flushed (right), outputs flushed before rounding, **PE missing** on a
  flush, IDC suppressed by DAZ (right), MIN/MAX return the raw denormal (wrong).

### 1.8 Tininess before or after rounding (UFC, and the FZ flush decision)

- **ASL `FPRoundBase__1`:**
  - AH = 0 detects underflow *before* rounding: `biased_exp == 0 && error != 0` (15716). The FZ flush tests
    `exponent < minimum_exp` before rounding and sets **UFC only, no IXC** (15694-15705).
  - AH = 1 detects it *after* rounding with an unbounded exponent (15764-15775). Its flush sets UFC **and** IXC.
- **x86:** after rounding, for SSE and x87 (SDM Vol.1 4.9.1.5, cited in `crates/abi/src/half.rs:4` and
  `correctness-instruction-forms.md:23`).
- **Difference A, the band:** only for results whose exact value is below MIN_NORMAL but rounds (bounded) up to
  ±MIN_NORMAL, within half an ulp under round-to-nearest. There AH = 0 sets UFC (a spurious UE: a masked flag in the
  default mode, a spurious #XM under unmasked UE) and, with FZ = 1, flushes to zero where x86 returns MIN_NORMAL.
- **Difference B:** the missing IXC on an FZ flush (DAZ + FTZ and FTZ modes).

### 1.9 FRECPE/FRSQRTE (RPRES)

- **Not emitted:** fexc has `rpres=false`; RCP*/RSQRT* are `fdiv` with FPSR saved around it (`FexcQuietOp`), so AFP
  doesn't matter except through the general rules above.
- **If ever enabled:** AH = 1 makes the estimates flag-free, round to nearest, and flush with FIZ/FZ = 11
  (`FPRecipEstimate` 17262-17273); RPRES needs AH. That can't work without AFP. No item.

### 1.10 Conversions

| Conversion | Behaviour (ASL) | Difference under AH = 0 |
| --- | --- | --- |
| FCVT narrowing (CVTSD2SS, CVTPD2PS, VCVTPS2PH) | `FPConvert` 16504: NaN keeps its sign (`FPConvertNaN`), as x86 | DE (`FPProcessDenorm`); tininess band (1.8); FZ input/output flush (`FPUnpackCV` only clears FZ16) |
| VCVTPS2PH | x86 ignores FTZ for it | AH = 0 with FZ = 1 also flushes its single-precision input (+IDC). Wrong under FTZ alone; under DAZ + FTZ right only if x86 honours DAZ there (inferred, unverified) |
| FCVTL widening (CVTSS2SD, CVTPS2PD) | — | DE only |
| FCVTZS/FCVTNS (CVT(T)S*2SI, CVT(T)P*2DQ) | `FPToFixed` 17838: IOC/IXC the same | under FZ = 1 a flushed input (value, and a spurious IDC → DE unless DAZ) |
| SCVTF | — | nothing |
| FRINT* (ROUND*) | `FPRoundInt`: IXC from frintx unchanged | under FZ = 1 a flushed input + IDC |

### 1.11 Compares (FCMEQ/FCMGE/FCMGT, FCMP/FCMPE)

- **Same under both:** NZCV, masks and IOC (`FPCompare*` 16319-16427).
- **Differs:** only IDC (DE) and FZ input flushing. natemu's COMISS fcmpe (`ALUOps.cpp:1605-1608`) and the AVX predicate
  path (`Vector.cpp:2919-2944`) get their IE right on either host; their DE "with x86's precedence" needs AH = 1.

### 1.12 FNEG/FABS on NaN

- **ASL:** AH = 1 leaves a NaN unchanged (`FPNeg` 17220, `FPAbs` 16097).
- **natemu's x87 FCHS/FABS:** integer XOR/ANDN (`x87StackOptimizationPass.cpp:1241-1244, 1262-1265`), so independent of
  AH.
- **FEX's 3DNow! RSqrt:** uses VFAbs (`Vector.cpp:688`). Only matters if 3DNow! is advertised.
- **Inside the FMA and FCADD negations:** see 1.3 and 1.4.

### 1.13 NEP (scalar results keep the upper lanes)

- **Who handles it:** FEX alone (`HostSupportsAFP` in `VFScalarOperation`/`VFScalarUnaryOperation`/
  `VFScalarFMAOperation`, `VectorOps.cpp:282-560`: an extra `ins` or `mov`). That path is upstream-tested, and the M2
  run passed everything but FP flags.
- **natemu's own code:** no natemu-emitted scalar FP op was found that relies on merging. FexcX87 ops are scalar doubles
  whose upper half is never read; the tiny test is integer. Inferred from the reading, not exhaustive over every patch
  line.

## 2. natemu code that assumes AFP semantics, and what breaks without it

| # | Where | Assumption | What breaks without AFP |
| --- | --- | --- | --- |
| A1 | `crates/abi/src/execution.rs:1890-1897` `fpcr` | writes AH, NEP and FIZ = DAZ; FZ = FTZ "an output flush only" | RES0 bits written (harmless on the M2, but it blocks emulating an M2 on the M4); DAZ inert; FTZ flushes inputs too |
| A2 | `execution.rs:1977-1982` `x87_fpcr` (and the host's `--x87-fpcr`, `crates/host/src/execution.rs:2481-2483`; `FexcX87Begin`, `fexc_sites.inc:106-115`) | AH and NEP | as A1; x87 uses the first-NaN rule (deviation 6) and a negative indefinite |
| A3 | `execution.rs:1951-1970` `mxcsr_flags` (resident `sse::bank`, `crates/runtime/src/execution/sse.rs:33`) and its inline twin `FexcMxcsr` (`fexc_sites.inc:183-202`) | IDC = DE unless DAZ; UFC = UE judged after rounding; a flush sets IXC | DE never set (FZ = 0) or set for flushed conversion inputs (FZ = 1); band UE; PE missing on an FZ flush |
| A4 | `execution.rs:1909-1924` `fpsr_bits`/`FPSR_PRE`/`simd_fault_flags`; `fexc_precision.h:181,189` (DE → IDC in the check mask), `fexc_sites.inc:170-178` `FexcSimdCheckWords`, `FexcSimdStep` 258-265, `FexcPendingCheck` 285-299 | IDC is DE with x86's precedence | unmasked DE never faults (`run_i5c` `*-de`); the x87 DE check (mask bit 7, `fexc_precision.h:181`) never fires |
| A5 | `fexc_sites.inc:300-363` `FexcTinyCheck`/`FexcTinyLanes` | the software test covers exact tiny results; UFC covers inexact ones, after rounding | exact tiny: fine on either host; inexact tiny: band spurious #XM under AH = 0 |
| A6 | `OpcodeDispatcher/X87F64.cpp:72-76` FLD m64 = fmul by 1.0 | DE from IDC (AH = 1); SNaN quieting with IE | DE lost (the quieting stays right) |
| A7 | `AVX_128.cpp:2077-2082` `FexcNegate` | `fneg` keeps a NaN's sign under AH = 1 | NaN sign flipped (the bug the patch fixed returns) |
| A8 | `Vector.cpp:2919-2944` (CMPPS predicates), `ALUOps.cpp:1605` (COMISS fcmpe), `Vector.cpp:4647-4673` (DPPS masking) | DE "with x86's precedence" from the hardware | IE stays right; DE lost |
| A9 | x87 two-NaN rule (`boot-features-plan.md:1298`) | AH = 1's first operand | QNaN with SNaN gives the SNaN (further from x87) |
| A10 | generated SSE/x87 invalid results | the default NaN is negative (AH = 1) | +qNaN (1.2) |
| A11 | FEX's non-AFP MINSS/MAXSS (`VectorOps.cpp:443-492`, upstream code) | (upstream FEX accepts it) | IE missing for a QNaN; DAZ raw denormal (1.5) |

Things that are independent of AFP (verified):
- FCHS/FABS (integer);
- `QuietSNaN` for FST m64 and the exact-tiny bit of FST m64 (`x87StackOptimizationPass.cpp:438-446, 249-251`, integer);
- FIST's NaN and range checks (`X87F64.cpp:115-143`, integer);
- `FexcRoundBegin`/frintx PE;
- RCP/RSQRT's FPSR save;
- the resident's x87 helpers and half conversions (software integer code, and the resident runs with FPCR = 0 on every
  host: `crates/runtime/src/gates.S:79-82`).

**The I5c machinery on an M2:**
- **IE:** works, except for FMA inf×0 + QNaN (spurious IOC) and MINSS/MAXSS with a QNaN (missing IOC).
- **ZE, OE, PE:** work.
- **DE:** never fires.
- **UE:** works through the tiny test plus UFC, apart from the band.
- **x87 DE (FLD m32/m64):** never fires.
- **Precedence:** `simd_fault_flags`/`x87_fault_flags` assume the hardware already applied x86's IDC precedence. A
  software DE must apply it itself: none in a lane with a NaN operand, an IE or a ZE.

## 3. Tests with AFP-dependent expectations, and how they fail on an M2

The recorded M2 run (`docs/restructure/io-benchmarks.md:1571-1574`): asm, i5c, i6-faults, i8-faults and i5b-images fail
FP cases (denormal and NaN flags). Below, mechanisms are verified and per-case outcomes inferred. One run on the M4 in
M2 emulation (section 0) gives the exact list.

**`tools/run_i5a_de.py`** (all SDM-judged DE):
- **Fail:** every case expecting DE = 1: the "src/dst denormal" cases of the 49 forms that list Denormal, the HADD/HSUB
  "QNaN and denormal" cases, and the DPPS/DPPD selected-element cases.
- **Pass:** the cases expecting 0 (DAZ, normal, beside a QNaN, denormal/0, the estimates, conversions, logic).

**`tools/run_i5c.py`:**
- **Fail, DE expected:** `addss-de`, `divps-de-ze`, `minpd-de`, `cvtps2pd-de`, `addps-de-oe`, `mulps-figure-11-9`,
  `rv-ps2ph-denormal-*`, both masked and unmasked runs.
- **Fail, other reasons:**
  - `maxss-qnan`: no IE (fcmp).
  - `ftz-ue-masked`: UE without PE.
  - `rv-fma-inf0-qnan-ie`: spurious IOC → #XM.
  - `daz-de-unmasked`: DAZ inert, so 1+den is inexact → PE, where 0 is expected.
  - `daz-ze-unmasked`: no flush, so 1/den overflows rather than ZE; no #XM.
  - Any x87 unmasked-DE case: no #MF.
- **Pass:** the tiny-exact cases (software test).

**`tools/run_i6_faults.py`:** `denormal-fld-m64` fails (FSW.DE expected, line 210). The indefinite cases pass (integer
constants).

**`tools/run_i8_faults.py`:**
- **Fail:** `de-vfmadd231ps`, `de-vaddps-upper`, `de-vcmpps-eq`, `de-vcvtps2ph` (MXCSR 1F82H/1FB2H with DE), and
  `de-vcmpps-{30,3,14,19}` (DE from lane 1).
- **Pass:** `nan2-*` (two QNaNs: the same under AH = 0).

**`tools/run_i5b.py images`:**
- The prelude loads `DOUBLES[7] = 2^-1060` (`run_i5b.py:44, 58-62`) by FLD m64 when `n = 8` (e.g.
  `fxsave-full-stack`), so the image's FSW lacks DE.
- The other image cases pass unless they load that double.

**FEX ASM suite (`tools/fexc/upstream_tests/run_asm.py`):**
- It records `HostFeatures` but doesn't apply them (`run_asm.py:21`), so `DAZTest.asm` (`"HostFeatures": ["AFP"]`)
  fails: DAZ is inert.
- Other failures are the recorded run's (not available here). Likely candidates: tests storing MXCSR after
  denormal/NaN work. Inferred.

**Corpora against QEMU** (`gen_i5a.py`, `gen_i6.py`, `gen_i8.py`): not run on the M2. Predicted failures:
- DAZ groups (DAZ inert);
- FTZ groups with denormal inputs;
- invalid results' NaN sign (QEMU gives the negative indefinite);
- QNaN with SNaN pairs;
- MINSS/MAXSS IE;
- FMA NaN order, negation sign and inf×0 + QNaN;
- PE on FTZ flushes.

DE is masked out of the corpus comparison.

**Unit tests:** `execution.rs` tests (`x87_fpcr_differs_only_when_it_must` 3917, `unmasked_exceptions_and_flush_to_zero`
3935-3938) assert today's FPCR words. They'd change if `fpcr` becomes host-conditional.

## 4. MXCSR/FCW state that compiled code is keyed on (`Cpu::code_context`, `execution.rs:2211-2226`)

Keyed today:
- bit 14, plus FCW.RC in 15-16: x87 rounding, when `x87_fpcr != 0`. This is nonzero whenever FCW.RC ≠ MXCSR.RC **or**
  DAZ/FTZ make the SSE FPCR differ, since its FZ/FIZ do;
- bits 18-23: `simd_unmasked` (DE dropped under DAZ);
- bits 24-29: `x87_unmasked`.

**Not keyed:** MXCSR.RC, DAZ, FTZ. They become FPCR at every block entry (`sse::fill`, `sse.rs:23`) and the code doesn't
depend on them. The I5c checks and the tiny test are compiled only under unmasked sets, so their cost is already
confined to rare modes.

**For the fallback:** add two key bits on non-AFP hosts only, so M4 code and caches are unchanged:
- DAZ;
- FTZ effective, i.e. FTZ & UE masked, the same condition `fpcr` uses.

That allows mode-specific fix-ups at zero cost to the default mode (Linux: 1F80H, no DAZ/FTZ, all masked). LDMXCSR,
FXRSTOR and XRSTOR are already resident transactions, and the context is recomputed at every lookup.

## 5. Candidate implementations and their cost (Arm instructions per affected x86 instruction)

Counts are what the sequence emits, with constants hoisted per block. I didn't measure anything.

### 5.1 Free or nearly free

1. **Host-conditional `fpcr`/`x87_fpcr`** (no AH/NEP/FIZ without AFP): 0 per instruction.
   - The resident needs the host's AFP bit.
   - This enables M2 emulation on the M4 for development and benchmarks.
2. **MINSS/MAXSS non-AFP: `fcmp` → `fcmpe`:** 0 extra. It's a FEX patch in the non-AFP lambda, and the IE then matches
   x86. The NZCV clobber is already handled (`OpcodeDispatcher.h:1311-1320`).
3. **DAZ + FTZ mode:** FZ = 1 in hardware handles the inputs.
   - In `mxcsr_flags` and `FexcMxcsr`, under an effective FTZ, map UFC → UE | PE. Under FZ = 1, UFC is set only by a
     flush (ASL 15694 returns before the second underflow check), and x86's flush sets UE + PE.
   - Cost: +1-2 instructions in the inline STMXCSR only.
   - Residual: the band, where Arm flushes a value that rounds up to MIN_NORMAL.

### 5.2 Rare keyed modes (zero cost to the default mode)

1. **DAZ only:** FZ = 0, plus a software input flush on each FP source operand.
   - Sequence: `cmtst m, x, #expmask; orr m, m, #signmask; and x', x, m`, giving a signed zero for denormals (the SDM
     keeps the sign).
   - Cost: **3 per source**, so +3 unary, +6 binary, +9 FMA.
   - Exact. No IDC under FZ = 0, so no DE, as x86.
2. **FTZ only:** FZ = 0, plus a software output flush.
   - Flush: `cmtst m, r, #exp; orr m, m, #sign; and r, r, m`.
   - Flags for an exact denormal result: `bic t, r, mExp; fmul t, t, c`. With c = 1−2^-p (0x3F7FFFFF /
     0x3FEFFFFFFFFFFFFF), a nonzero denormal always multiplies inexactly to a tiny value, so UFC + IXC are raised exactly
     when a lane flushes. ±0 lanes raise nothing.
   - Cost: **~5 per result.**
   - Residual: the band, where x86's tininess for a bounded result of MIN_NORMAL depends on the unbounded rounding.
3. **MIN/MAX under DAZ** (either DAZ mode): flush the inputs before `fcmgt`/`bif`, **+6 per MIN/MAX**.
4. **Unmasked DE (SSE):** software denormal detection with x86 precedence.
   - Per lane: (den(a) | den(b)) & ¬NaN(a) & ¬NaN(b) [& divisor ≠ 0; sqrt: a positive operand].
   - Cost: ~3-4 per operand + 2-3 for precedence + `umaxv`/`fmov`, ORed into x0 inside `FexcSimdCheckWords` before
     the mask, as FPSR bit 7. This needs no `msr`; the bank, `simd_fault_flags` and STMXCSR stay unchanged.
   - Total: **~10-14 per checked binary op**, only under DE unmasked.
5. **x87 DE:** an integer test of the m32/m64 operand bits (`lsl` + exponent test, ~3-4 GPR instructions), ORed into
   the pending byte that `FexcX87End` already loads and stores.
   - It's cheap enough for the **default mode** too.
   - With AH = 0's lack of DE from register arithmetic, x87 DE becomes **more exact than on the M4** (deviation 4 goes
     away).
6. **Unmasked UE band:** when the check sees UFC and some result lane equals ±MIN_NORMAL (`cmeq` on |r| + reduce, ~3-4
   instructions under UE unmasked), defer to a slow path that decides by exact recomputation, or accept the band.
   - The task asked about dropping UFC in favour of the tiny test. That doesn't work: an underflow that rounds to zero
     needs UFC, and only the band is wrong.
7. **Unmasked IE, FMA inf×0 + QNaN:** sanitize the multiplicands where the addend is a NaN.
   - Per lane: a/b → (NaN ? itself : 0). This keeps IE for an SNaN multiplicand and drops the 0×inf IOC.
   - Cost: ~5 per FMA, IE-unmasked mode only. In the masked default this IE is a stale-flag residual (5.3).

### 5.3 Default-mode NaN exactness (two-NaN corner, default-NaN sign, FMA order, inf×0 + QNaN, negation signs): owner decision

Every one of these produces a NaN result, so one guard covers them all: a "NaN result" check after each SSE/x87
arithmetic op.
- Sequence: `fcmeq t, r, r; uminv s, t; fmov w, s; cbz w, cold`.
- Cost: 4 packed / 3 scalar, plus 1 `mov` when the destination aliases a source (the cold path needs the operands).
- The cold path holds the out-of-line exact rule, ~6-12 instructions:
  - SSE: first source, then second source, then the negative indefinite;
  - FMA: Rn, Rm, addend;
  - x87: COR-09's larger-significand rule, which fixes COR-09 on both hosts.
- Residual: the spurious IE of masked FMA inf×0 + QNaN. A NaN-input guard (~8 instructions per FMA) would fix it.

Inline alternatives (no branch):

| Fix | Cost per op |
| --- | --- |
| Two-NaN rule only: `fcmeq m, a, a; orr q, a, Q; bif r, q, m` | 3 |
| Default-NaN sign, exact (needs the input tests, since glibc's NAN constant is +0x7FC00000) | ~6 |
| FMA three-NaN order | ~9 |
| A negated FMA operand (integer conditional negate replacing `fneg`) | +2 |

Option to accept: these are NaN payload and sign details. Their most visible effect is printf's "-nan" versus "nan".

### 5.4 Default-mode masked DE: owner decision

- **Exact:** software detection on every FP op, ~10-14 per binary op. There is no flag-free accumulator: FPSR.QC is
  polluted by FEX's saturating integer ops, and writing FPSR serializes.
- **Visibility:** DE is not in glibc's `FE_ALL_EXCEPT`, so standard fenv never reads it.
- **Options:** accept and state it on the startup line, or compile it under an opt-in strict key.

### 5.5 Default-mode tininess band (masked UE flag)

Accept. It needs a result rounding to exactly ±MIN_NORMAL from below.

### Where FEX upstream's non-AFP behaviour already matches x86

- MIN/MAX vector NaN, signed-zero and IE behaviour;
- scalar-insert semantics (NEP through `ins`);
- single-NaN propagation;
- two NaNs of the same kind;
- IE/ZE/OE/PE in general;
- inexact tiny UE outside the band;
- conversions with FZ = 0.

FEX upstream ignores DAZ without AFP (`MiscOps.cpp:142`), puts FTZ in FZ (input and output), and does none of the NaN or
DE fixes.

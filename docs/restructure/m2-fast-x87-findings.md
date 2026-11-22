# M2: fast x87 on Macs without AFP

November 21, 2026. Follows [task 4](m2-task4-findings.md), which made 32-bit x87 code fast on Macs with FEAT_AFP (M4)
and left Macs without it (M1, M2) on the old per-operation wrapper: 150 to 190 times slower on x87-heavy 32-bit code.
The owner's decision: on such a Mac, fast mode (the default) runs x87 with task 4's machinery and takes documented
deviations, as SSE code already does there; accurate mode keeps the exact behaviour and the wrapper; Macs with AFP do
not change. The user-facing list is in [floating point](../floating-point.md).

Speed numbers are from an Apple M3 with AFP hidden (`NATEMU_FEXC_FEATURE_OFF=afp`), so the code is what an M2 runs, 1
vCPU, through `tools/natemu-test run --tier bench`, one boot per row unless said, run 1 / run 2. The M3's second runs
are noisy in SciMark (FFT, sparse and LU sometimes halve); the first runs are the ones to read.

## What changed

A host without AFP kept the wrapper because two fix-ups read each x87 operation's own flags: the NaN fix-up (Arm
without AFP returns the positive default NaN and takes a signalling NaN before a quiet one) and the software test for
a denormal memory operand (Arm without AFP has no flag for it). Fast mode now does without both:

- The three conditions (`FexcX87Runs`, `FexcX87Lazy`, `FexcFldcwInline`) and the unit's entry run state ask
  `FexcX87Exact()` (no AFP and accurate mode) instead of "no AFP". So fast mode gets runs, lazy flags, the stack-top
  guess, the fault-time maps and the translated FLDCW; accurate mode keeps the wrapper, the keyed x87 control register
  and FLDCW in the shim.
- A run's control register on such a host has no AH or NEP bit (reserved there; on an M4 with AFP hidden they would
  really switch AFP behaviour on).
- The host no longer keys code on the x87 rounding control in fast mode (`abi::x87_fpcr` is 0), as with AFP.
- FEX fork (`natemu-m2-fast-x87`, a75d79f): task 4's FLD m64 without its multiply (a unit with no x87 arithmetic)
  tested its operand with integer instructions and set DE. Without AFP it no longer does, so a load behaves the same
  whatever else its unit holds. The state test `fexc-slice1-x87-commit` had shown the difference.

Nothing else in the shim, the host's admission or the checkers depends on AFP for x87.

## Speed

| | before (`5c23e3f9`) | now | now, AFP visible |
| --- | --- | --- | --- |
| `cray32-small` (s) | 111.4 / 111.4 | 0.66 / 0.56 | 0.72 / 0.68 |
| `cray32-strict-small` (s) | 66.3 / 66.3 | 0.63 / 0.57 | 0.58 / 0.60 |
| `cray32-sse2` (s; SSE2 code calling libm's x87 `pow`) | 8.09 / 7.98 | 5.16 / 4.99 | 4.52 / 4.51 |
| SciMark 32-bit x87, composite (Mflops) | 32.7 / 32.7 | 1176 / 942 | 1145 / 886 |
| SciMark 32-bit SSE2, composite | 1596 / 1184 | 1932 / 1293 | 2195 / 2186 |
| SciMark 32-bit SSE2, Monte Carlo | 54 / 54 | 210 / 210 | 219 / 219 |
| `cray`, 64-bit (control) (s) | 4.83 / 4.76 | 4.81 / 4.73 | 3.59 / 6.29 |

x87 c-ray is 170 to 200 times faster and SciMark's x87 build about 36 times; both now run at the speed the same
machine has with AFP. Every picture's hash is the pinned one.

## What exactness would cost, measured

Each variant was built and measured against the fast version above (same machine, AFP hidden).

| | fast (deviations) | NaN results exact | DE exact, the wrapper's vector test | DE exact, a lean integer test |
| --- | --- | --- | --- | --- |
| extra instructions | none | 3 to 4 per arithmetic operation | 10 per FLD m64, 16 per arithmetic with a memory operand | 3 to 4 per memory operand |
| `cray32-small` (s) | 0.66 / 0.56; 0.72 / 0.63; 0.64 / 0.54 | 1.24 / 1.19 | 2.60 / 2.48 | 1.05 / 0.94; 1.02 / 0.95 |
| `cray32-strict-small` (s) | 0.63 / 0.57; 0.57 / 0.56; 0.59 / 0.53 | 1.25 / 1.13 | 3.38 / 3.29 | 1.10 / 0.99; 1.09 / 1.06 |
| SciMark 32-bit x87, composite | 1176 / 942; 1120 / 1042; 1091 / 1066 | 776 / 800 | 445 / 457 | 1039 / 1056; 941 / 937 |

- **NaN results** (a quiet compare of every result, then the old fix-up): about 1.9 times the time on c-ray, a third off
  SciMark. The 64-bit control read 20% slow during that one round, so the column is disturbed by about that much. The
  owner accepted the deviation; FEX does the same (below).
- **The denormal flag.** The lean test (`fmov x0, d; ubfx x1, x0, #52, #11; cbnz x1, done` in front of the operation,
  the rest on the rare path, the flag set in the status register inside the run) is exact by the native oracle and in
  the VM cases, and costs about 55% on `cray32-small`, 85% on `cray32-strict-small` and 7 to 14% on SciMark (two boots
  each). The owner's bound for keeping DE exact was about 15%, so the flag is dropped in fast mode. The test's
  implementation is in the history (commit `35ba3e90`, reverted by the next one) should the bound change.

## The differences, found and checked

A native comparison ran 31,611 one-instruction cases (every arithmetic, compare, load and store form with special
values, band values and three non-default rounding modes) on the compiled code of fast mode, the wrapper and a Mac with
AFP. Fast mode differs from the wrapper in exactly these, and in nothing else (no stored value, condition bit, C1,
rounding-mode result, or IE, ZE, OE, UE, PE flag):

1. `x87-nan-results`, the default NaN's sign (222 cases): an invalid operation gives +NaN where x86 gives -NaN. IE is set.
2. `x87-nan-results`, the choice (250 cases): when the operation's first operand is a quiet NaN and its second a
   signalling one, the result is the signalling one, quieted, where x86 keeps the quiet one. IE is set. The first
   operand is ST(0), or the memory operand of FSUBR and FDIVR. The signalling NaN is an m64 operand, or sits in a
   register: FLD m80, FRSTOR and FXRSTOR load one unchanged (`fld tbyte [SNaN]; fld qword [QNaN]; fadd st(0), st(1)`
   is the VM case `nan-qnan-st0-snan-reg-fadd`). A signalling m32 operand never shows it: its conversion to a double
   quiets it before the operation.
3. `masked-x87-de` (312 cases): no DE for a denormal m32 or m64 operand.

With AFP hidden the 32-bit system cases and the i386 campaign pass (177 passed, 12 expected failures, the same twelve),
and `torture32` passes (3,433 passed, 10 expected failures). Accurate mode's cases (`i5c-m2a`, `i6-faults-m2a`) pass
unchanged.

### What FEX itself does

The owner's reason for accepting the NaN deviations was that FEX does the same. Checked in upstream FEX at the commit
our fork is based on (48d71752):

- FEX sets FPCR.AH only on a host with AFP (`FEXCore/Source/Interface/Core/ArchHelpers/Arm64Emitter.cpp`,
  `FillSpecialRegs`: `if (EmitterCTX->HostFeatures.SupportsAFP)`). Without it, arithmetic gives Arm's NaN results.
- Its reduced-precision x87 lowers each arithmetic instruction to the plain double operation with nothing after it
  (`FEXCore/Source/Interface/IR/Passes/x87StackOptimizationPass.cpp`, `case OP_F80ADDSTACK:
  HandleBinopStack(OP_VFADD, ...)` and its neighbours; `OpcodeDispatcher/X87F64.cpp` has no NaN handling).
- It does not model DE, or any x87 exception flag but IE: the status word is built from the stack top, C0 to C3 and one
  invalid bit (`OpcodeDispatcher/X87.cpp`, `ReconstructFSW_Helper`).

So on all three FEX is the same or coarser. natemu keeps more than FEX does: IE, ZE, OE, UE and PE are right in fast mode.

### Two older differences, now written down

Found by the same comparison; neither comes from this change and neither mode changes them.

- `x87-band-ue-flag` (Macs without AFP): a result just below the smallest normal number of its format that rounds up to
  it sets UE (12 of the cases): a product just below the smallest normal double, and FST or FSTP m32 of a double just
  below the smallest normal float (the narrowing conversion judges "too small" before rounding there). The wrapper has
  it too. On real x86 (the same Ryzen) the store `fstp m32` of 0x380FFFFFFFFFFFFF stores 0x00800000 with PE and no UE,
  and the register product sets no flag at all (80 bits).
- `x87-register-de` (Macs with AFP): the M4's code sets DE whenever an x87 operation meets a denormal double in a
  *register* (3,597 cases), because it takes the CPU's own flag. Checked on real x86 (Ryzen 7 8845HS, a small C program,
  32- and 64-bit): 2^-1023 computed in the x87 and then used by FADD, FMUL, FSQRT, FCOMP and FST m64 sets no DE; the
  same value loaded from memory does. So x86 sets DE for a denormal memory operand only, the code without AFP is right
  here, and the M4 is the one that deviates. Fixing it on the M4 means not taking the CPU's flag and testing memory
  operands in software, which is the lean test above at its measured cost; documented instead.

## Tests

- `tools/fexc/test_slice1.py`: the tests that pinned the wrapper's code and results without AFP now compile with
  `--accurate` (`AfpX87`, `AfpDenormal`); a new test compares fast mode with accurate mode on every case, under each of the
  four rounding modes, and accepts only the three differences above, each seen more than 100 times; the state test (`Slice1X87Commit`) also runs
  accurate mode's code. The admission unit test of the NaN fix-up's shape compiles with `--accurate`.
- `tests/lib/fp_deviations.py`: the classes `x87-nan-results` and `masked-x87-de` and one rule for x87 cases
  (`x87_verdict`); the other x87 deviations by name (`X87_OTHER`).
- `tests/faults/run_i5c.py`, `run_i6_faults.py`: 33 cases carry a class and their deviated result (19 `masked-x87-de`
  and 12 `x87-nan-results` in `i5c`, 2 `masked-x87-de` in `i6-faults`). On a Mac without AFP in fast mode only the
  deviated result passes; everywhere else, accurate mode included, only x86's.

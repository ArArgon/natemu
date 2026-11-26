# Floating point: fast and accurate modes

The README's "Floating point on M1 and M2" is the user summary; this page has the details.

## Host checks and startup lines (all Macs)

The minimum is macOS 15: natemu uses Hypervisor.framework's interrupt-controller API (`hv_gic_*`), which is linked as a required import, so an older macOS refuses to load the binary. There is no runtime check for it. At startup natemu checks the vCPU for the hardware features it needs (FlagM, LSE, LSE2, the crypto extensions, Apple's TSO bit) and fails with one line naming everything missing, for example `natemu: host lacks lse2 pmull128`. Trapped floating-point exceptions are checked separately, by the resident at its first run (after the Linux path's startup lines): a host without them stops with `natemu: host lacks fptraps`, not merged into that line. A boot prints startup lines of the form `natemu: <topic>: <value>`, one decision each: `cpu` (the x86-64-v3 level plus what the guest CPU adds, computed from the host's features), `fp` (`fast mode`; on a Mac without FEAT_AFP, M1 or M2, `fast mode, degraded (host lacks FEAT_AFP)`: a few x86 floating-point corner cases differ, listed below; `--fp accurate` or `NATEMU_FP=accurate` prints `accurate mode`, which makes them exact (below)), `acceleration` (each host feature natemu can use for speed, `+` used and `-` the slower fallback code), `meltdown` (`speculation barriers` or `none`), and `code cache` (`pool on, disk 4096 MiB at ~/Library/Caches/natemu/fexc`: the shared template pool and the compile cache on disk, or `disk off`). Tools recognise these lines by those topics only: errors and warnings can also read `natemu: disk lock: ...`. If the compile cache cannot be opened, a later `natemu: code cache: disk off (reason)` line corrects it.

## Fast mode on a Mac without FEAT_AFP

On a Mac without FEAT_AFP (M1, M2) natemu's default floating-point mode, fast, keeps x86 results exact except for these
documented deviations (the M4 has none; the names are the ones `tests/lib/fp_deviations.py` and the test runners use):

- `masked-sse-de`: with DE masked, an SSE or AVX instruction on a denormal operand does not set MXCSR.DE (the result is right).
- `nan-results`: a NaN result can differ from x86 in sign or choice: the default NaN is positive, the one-instruction negated FMA forms (the scalar ones, the packed FNMADD and the first multiplicand of FNMSUB) flip a NaN's sign, two-NaN and three-NaN choices follow Arm's order (the addend of the packed FMSUB and FNMSUB and the subtracted lanes of FMADDSUB and FMSUBADD arrive quieted, so an SNaN there takes no priority), RSQRT's NaN has the other sign.
- `fma-invalid-flag`: an FMA of infinity times zero plus a QNaN, or plus an SNaN addend of the packed FMSUB, FNMSUB or the subtracted lanes of FMADDSUB and FMSUBADD, returns the default NaN and sets MXCSR.IE (x86: the NaN, quieted; no flag for a QNaN).
- `band-ue-flag`: with UE masked, a result just below the smallest normal number that rounds up to it can set MXCSR.UE (x86 judges tininess after rounding).
- `daz-ftz-band`: with DAZ and FTZ both set, a result just below the smallest normal number that rounds up to it is 0 instead of the smallest normal.
- `ftz-band`: with FTZ set and DAZ clear, a result whose exact value lies just below the smallest normal number, which x86 flushes to 0 (tiny after rounding to the destination precision) but Arm's denormal rounding carries up to the smallest normal, is the smallest normal instead of 0 (the flags are right).
- `x87-nan-results`: an x87 NaN result can differ from x86 in sign or choice: an invalid operation (0/0, infinity minus infinity, infinity times 0, the square root of a negative number) gives the positive default NaN where x86 gives the negative one, and an operation whose first operand is a quiet NaN and whose second is a signalling NaN returns the signalling one quieted where x86 keeps the quiet one (the first operand is ST(0), or the memory operand of FSUBR and FDIVR; a signalling NaN reaches an operation as an m64 operand or in a register loaded by FLD m80, FRSTOR or FXRSTOR; a signalling m32 operand is quieted when it is converted and never shows it; the flags are right).
- `masked-x87-de`: with DE masked, an x87 instruction on a denormal m32 or m64 operand does not set the status word's DE flag (the result is right).

The first six are about SSE and AVX code, the last two about x87 code (what 32-bit programs built for i686 use for all
their arithmetic). A program sees `x87-nan-results` when it prints such a NaN: `printf` shows `nan` where x86 shows
`-nan`. The two x87 items exist because keeping them exact costs a lot there: natemu would have to switch and read the
CPU's floating-point status around every single x87 operation, which makes x87-heavy 32-bit code more than a hundred times
slower (the table under "Accurate mode"). FEX-Emu, whose x87 translation natemu uses, behaves the same way on these Macs:
its faster x87 mode does not model the x87 exception flags other than "invalid" at all and takes Arm's NaN results as they
come ([findings](restructure/m2-fast-x87-findings.md)). With an x87 exception unmasked (rare: Linux programs run with all
of them masked) x87 code is exact in fast mode too, at the slow speed.

## x87 deviations on every Mac: registers hold doubles (`x87-doubles`, both modes)

natemu's x87 registers are 64-bit doubles, not the x87's 80-bit format: 53 bits of precision instead of 64, and the
double's exponent range. 64-bit programs rarely notice (their floating point is SSE). 32-bit programs use the x87 for
everything, and these are the differences from hardware measured with Debian's i386 programs (M2 checkpoint A,
[findings](restructure/m2-checkpoint-a-findings.md)):

- `long double` has the precision and range of `double`: `1 + LDBL_EPSILON == 1`, and `LDBL_MAX` loads as infinity.
- A 64-bit integer does not survive a trip through an x87 register above 2^53: `(long long)(long double)x`, and gcc's
  64-bit atomic load and store on a 32-bit target without SSE (FILD m64 then FISTP m64), which moves 8 bytes through
  the x87 and loses the low bits.
- An unsigned 64-bit integer above 2^63 converted to `double` can be one unit in the last place off (it is rounded
  twice).
- Double code that relies on the extended exponent range overflows early: glibc's i386 `hypot` squares its arguments
  in the x87, so `hypot(5e298, 0)` is infinite.
- glibc's i386 `exp`, `expm1`, `sinh`, `cosh` and `tgamma` are off by tens to hundreds of units in the last place for
  large arguments; for ordinary arguments results agree with hardware to ten digits and more.
- The precision-control field of the x87 control word does nothing. A program that sets it to double (CPython does,
  around float formatting) gets the same results as on hardware.

- A computation whose intermediate result leaves the double's range or precision but fits the x87's sets the flags a
  double computation sets (UE, OE, PE) where hardware, working in 80 bits, sets none until the value is stored.

Ordinary double arithmetic, casts to integers, the four rounding modes, comparisons, exception flags, and saving and
restoring the x87 state (signal frames included) match hardware, apart from the items in the next section and, in fast
mode on a Mac without FEAT_AFP, the two x87 items in the list above.

## Other x87 deviations, by Mac (both modes)

Two smaller differences in the x87 status word's flags that neither mode changes. Both come from how the Mac's own
floating-point unit reports flags, and both were measured against a real x86 processor (an AMD Ryzen 7 8845HS):

- `x87-band-ue-flag` (Macs without FEAT_AFP: M1, M2): with UE masked, a result just below the smallest normal number of
  its format that rounds up to it sets the status word's UE flag: FST or FSTP m32 of a value just below the smallest
  normal `float`, and arithmetic whose result lies just below the smallest normal double. Arm judges "too small" before
  rounding, x86 after: the real processor sets only PE for that store, and for the arithmetic no flag at all (it works
  in 80 bits, `x87-doubles`). Accurate mode fixes the same case for SSE code (`band-ue-flag`), not for x87.
- `x87-register-de` (Macs with FEAT_AFP: M4): an x87 instruction on a register that holds a denormal double sets the
  status word's DE flag. On x86 that register holds an ordinary 80-bit number and DE stays clear. A Mac without FEAT_AFP
  does not set it, which is x86's behaviour. Removing it on an M4 would mean testing every memory operand in software
  instead of taking the CPU's flag, which was measured at 55 to 85% more time on x87-heavy code
  ([findings](restructure/m2-fast-x87-findings.md)), so it stays.

The complete list of x87 deviations is therefore: `x87-doubles` everywhere; `x87-register-de` on Macs with FEAT_AFP;
`x87-band-ue-flag` on Macs without it; and there, in fast mode only, `x87-nan-results` and `masked-x87-de`.

## Accurate mode

`--fp accurate` (or `NATEMU_FP=accurate`; the flag wins) makes every item in the fast-mode list above exact on a Mac without AFP, so x86 floating-point
results and flags match what real x86 gives (the x87 items of the two sections before this one stay). It does this by letting the CPU trap on the cases that differ and correcting the result in natemu's
small EL1 runtime, so it needs a host that reports precise floating-point traps (M1 Pro, M2 Max and M4 do; this is a required host feature).
natemu prints `natemu: fp: accurate mode` at startup. On an M4 (or any Mac with AFP) accurate mode changes nothing: no code, no cost.

The price is speed, only where a program really hits those cases or is built to (measured in M2 emulation, accurate against fast mode):

| Benchmark | Extra time |
| --- | --- |
| cray (built with `-ffast-math`, so DAZ and FTZ) | +163% (was +623% before the DAZ screen) |
| cray-strict (the same without `-ffast-math`: DAZ and FTZ clear) | +273% (was +473% before the denormal-input screen's hoisted constants, zero tier and in-block skip; the rest is mostly screens of values entering a block) |
| luajit | +29.3% |
| luajit-interp | +16.7% |
| sqlite | about +5.5% (not the x87 mode switching once thought: removing it changed nothing; cause not yet attributed) |
| avx | about +2.5% |
| zstd | about 0 |
| c-ray built as 32-bit x87 code, 320x240 (`cray32-small`, `cray32-strict-small`) | about 170 and 110 times the time (111 s against 0.66 s, 66 s against 0.63 s; measured on an M3 with AFP hidden) |
| SciMark 2.0 built as 32-bit x87 code | about 1/36 of the speed (33 against 1,176 Mflops; same machine) |

The last two rows are x87 code: accurate mode keeps the x87's NaN results and DE flag exact by switching and reading the
floating-point status around every x87 operation, where fast mode does it once per run of x87 instructions.

Fast mode stays the default: making only the underflow or only the NaN fixes always-on cost +16% to +18% on average, which was too much.

To check a Mac, run `sh tools/fp_check/run.sh` from a checkout (after the setup below): it runs the trap probe and the test
framework's floating-point and fault families (`tests/fp`, `tests/faults`) in both modes, and writes `$TMPDIR/fp_check/report.txt`.

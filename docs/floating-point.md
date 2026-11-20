# Floating point: fast and accurate modes

The README's "Floating point on M1 and M2" is the user summary; this page has the details.

## Host checks and startup lines (all Macs)

The minimum is macOS 15: natemu uses Hypervisor.framework's interrupt-controller API (`hv_gic_*`), which is linked as a required import, so an older macOS refuses to load the binary. There is no runtime check for it. At startup natemu checks the vCPU for the hardware features it needs (FlagM, LSE, LSE2, the crypto extensions, Apple's TSO bit) and fails with one line naming everything missing, for example `natemu: host lacks lse2 pmull128`. Trapped floating-point exceptions are checked separately, by the resident at its first run (after the Linux path's startup lines): a host without them stops with `natemu: host lacks fptraps`, not merged into that line. A boot prints startup lines of the form `natemu: <topic>: <value>`, one decision each: `cpu` (the x86-64-v3 level plus what the guest CPU adds, computed from the host's features), `fp` (`fast mode`; on a Mac without FEAT_AFP, M1 or M2, `fast mode, degraded (host lacks FEAT_AFP)`: a few x86 floating-point corner cases differ, listed below; `--fp accurate` or `NATEMU_FP=accurate` prints `accurate mode`, which makes them exact (below)), `acceleration` (each host feature natemu can use for speed, `+` used and `-` the slower fallback code), `meltdown` (`speculation barriers` or `none`), and `code cache` (`pool on, disk 512 MiB at ~/Library/Caches/natemu/fexc`: the shared template pool and the compile cache on disk, or `disk off`). Tools recognise these lines by those topics only: errors and warnings can also read `natemu: disk lock: ...`. If the compile cache cannot be opened, a later `natemu: code cache: disk off (reason)` line corrects it.

## Fast mode on a Mac without FEAT_AFP

On a Mac without FEAT_AFP (M1, M2) natemu's default floating-point mode, fast, keeps x86 results exact except for these
documented deviations (the M4 has none; the names are the ones `tests/lib/fp_deviations.py` and the test runners use):

- `masked-sse-de`: with DE masked, an SSE or AVX instruction on a denormal operand does not set MXCSR.DE (the result is right).
- `nan-results`: a NaN result can differ from x86 in sign or choice: the default NaN is positive, the one-instruction negated FMA forms (the scalar ones, the packed FNMADD and the first multiplicand of FNMSUB) flip a NaN's sign, two-NaN and three-NaN choices follow Arm's order (the addend of the packed FMSUB and FNMSUB and the subtracted lanes of FMADDSUB and FMSUBADD arrive quieted, so an SNaN there takes no priority), RSQRT's NaN has the other sign.
- `fma-invalid-flag`: an FMA of infinity times zero plus a QNaN, or plus an SNaN addend of the packed FMSUB, FNMSUB or the subtracted lanes of FMADDSUB and FMSUBADD, returns the default NaN and sets MXCSR.IE (x86: the NaN, quieted; no flag for a QNaN).
- `band-ue-flag`: with UE masked, a result just below the smallest normal number that rounds up to it can set MXCSR.UE (x86 judges tininess after rounding).
- `daz-ftz-band`: with DAZ and FTZ both set, a result just below the smallest normal number that rounds up to it is 0 instead of the smallest normal.
- `ftz-band`: with FTZ set and DAZ clear, a result whose exact value lies just below the smallest normal number, which x86 flushes to 0 (tiny after rounding to the destination precision) but Arm's denormal rounding carries up to the smallest normal, is the smallest normal instead of 0 (the flags are right).

## Accurate mode

`--fp accurate` (or `NATEMU_FP=accurate`; the flag wins) makes every item in the list above exact on a Mac without AFP, so x86 floating-point
results and flags match what real x86 gives. It does this by letting the CPU trap on the cases that differ and correcting the result in natemu's
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

Fast mode stays the default: making only the underflow or only the NaN fixes always-on cost +16% to +18% on average, which was too much.

To check a Mac, run `sh tools/fp_check/run.sh` from a checkout (after the setup below): it runs the trap probe and the test
framework's floating-point and fault families (`tests/fp`, `tests/faults`) in both modes, and writes `$TMPDIR/fp_check/report.txt`.

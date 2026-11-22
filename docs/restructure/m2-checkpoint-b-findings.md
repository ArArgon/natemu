# M2 checkpoint B: a Debian i386 userland builds CPython; speed against QEMU

November 21 and 22, 2026. Checkpoint B of [the M2 plan](m2-32bit-design.md), on [checkpoint A](m2-checkpoint-a-findings.md),
[task 4](m2-task4-findings.md) and [fast x87 without AFP](m2-fast-x87-findings.md). The roadmap's exit, read literally: a
stock i386 userland runs under the Debian guest, including a 32-bit toolchain build, and performance is recorded against
QEMU. There is no speed bar (the owner's decision); costs are named and labelled instead.

(The flaw this page names, 32-bit code compiled again in every process, is fixed since: the i386 configure takes
106.7 s and the make 114.7 s; [32-bit code reuse](m2-reuse32-findings.md). The numbers below are this checkpoint's.)

**Summary.**

- A complete Debian 13 i386 userland, installed in the guest by Debian's own `debootstrap`, configures and builds CPython
  3.13 with `make -j4` under natemu, and the interpreter it builds gives the answers of the same build on real x86. It
  worked at the first attempt: no natemu defect was found and no emulator code changed in this checkpoint.
- natemu is slow at it, for one reason. 32-bit code is compiled again in every process: 69% of the i386 configure and 49%
  of the i386 make is spent waiting for the compiler, almost all of it on bytes already compiled earlier in the same boot.
  **The i386 configure takes 374 s under natemu and 111 s under QEMU.** This is a flaw (a wasteful design), not a gap; the
  owner moved its fix out of this checkpoint into the task that follows it (branch `m2-reuse32`).
- Everywhere else natemu is ahead of QEMU on 32-bit code, by 1.7 to 140 times, except system calls made by INT 0x80,
  where the two are level.
- One accepted deviation is now visible in a stock program: the i386 python computes `2.0 ** 0.5` as
  `1.414213562373095`; real x86 gives `1.4142135623730951`.

Every number is from one Apple M4 (4 performance cores, 16 GiB), each case alone on the machine through
`tools/natemu-test run --tier bench`, one boot per case, none contended. natemu is `m2-32bit` at 213e3566 (this
checkpoint's commits on top change tests only). QEMU is 11.1.2, `-accel tcg,thread=multi`, machine `pc-i440fx-11.1`, the
CPU model matched to natemu's features. Real x86 is a Ryzen 7 8845HS.

## The workload: CPython built by a Debian i386 userland

Bench case `io5-solo32` (`tests/io/run_io5.py --i386`), beside the existing `io5-solo`. The stock Debian 13 cloud image
boots with 4 vCPUs, 8 GiB, a disk, the network and `mitigations=off`. In one boot:

1. apt installs the 64-bit build tools (untimed: it depends on Debian's servers).
2. The 64-bit userland runs CPython's `./configure` and `make -j4`, with nothing else running.
3. `debootstrap --arch=i386 --variant=buildd` installs a Debian i386 userland in `/i386`, from the same snapshot of the
   archive (untimed). Its second stage already runs i386 dpkg, perl and package scripts. In that userland every program
   is a 32-bit one: shell, make, sed, perl, gcc, the assembler and the linker.
4. That userland runs the same `./configure` and `make -j4` on the same source (`linux32 chroot /i386`, so configure
   sees an i686 machine).
5. The 32-bit python it built runs a fixed check and eight of CPython's own test modules.

A second boot then checks that every built file reads back unchanged. The run passes only if every step succeeds, the
guest powers off cleanly, the filesystem check is clean and the kernel logged nothing unexpected.

**Verification against real x86.** The same disk, kernel and script boot on the x86 machine under KVM (case
`io5-solo32-reference`; Debian's QEMU 10.0.13 in the builds' container, as the `ltp32` campaign's reference boots).
What its python printed is pinned in `run_io5.py` as a hash:

- The check prints sizes and limits of the build, big-integer arithmetic, floating-point formatting, fifteen libm
  values (sin, cos, tan, atan, erf), decimal and fraction arithmetic, SHA-256, zlib, lzma and bz2, JSON, a regular
  expression, an SQLite query, eight threads, and the OpenSSL version; then the result lines of `test_struct`,
  `test_long`, `test_float`, `test_json`, `test_re`, `test_hashlib`, `test_zlib` and `test_sqlite3` (1,183 tests).
- natemu and QEMU both give exactly the reference's hash. The package lists of the 64-bit and the i386 userland are
  identical on all three too.
- Ten more values (log, exp and lgamma at three arguments, and `2.0 ** 0.5`) are compared with the reference's within
  16 units in the last place instead of exactly. See the deviation below.

A unit test (`tests/io/test_io_convert.py`) shows the judge fails on a wrong hash, a wrong package list, a value too
far from the reference's and a missing value.

## The deviation that shows: x87 registers hold doubles

glibc's i386 `log`, `exp`, `lgamma` and `pow` are x87 code that relies on the registers' 80 bits. natemu's fast mode
keeps x87 registers as doubles (accepted, and listed in [floating point](../floating-point.md)), so their last digits
can differ from real x86. In the check, four of the ten values differ:

| | real x86 and QEMU | natemu | units in the last place |
| --- | --- | --- | ---: |
| `2.0 ** 0.5` | 1.4142135623730951 | 1.414213562373095 | 1 |
| `log(37.25)` | 3.6176519448255684 | 3.617651944825568 | 1 |
| `lgamma(37.25)` | 96.6198845882781 | 96.61988458827811 | 1 |
| `exp(37.25)` | 1.504767666846084e+16 | 1.5047676668460856e+16 | 8 |

CPython's `test_math` runs 86 tests in this build. On real x86 it passes, with 6 skipped: CPython notices the x87's
double rounding and skips tests that cannot pass with it. Under natemu there is no double rounding to notice, so 3 of
those run, and 2 tests fail. The run records this as a note and does not judge it. Which two tests fail was not kept
(the run keeps only the totals); that is a small follow-up.

This is the same class as the `libm32` campaign's expected failures. It is new only in where it shows: a wrong last
digit from ordinary arithmetic in an ordinary program. QEMU computes x87 in software at 80 bits and matches real x86
here.

## Times of the build

Seconds; `io5-solo32`, `io5-solo32-qemu`, `io5-solo32-reference`; 4 vCPUs.

| Step | natemu | QEMU | natemu's speed-up over QEMU | real x86 (KVM) | natemu / real x86 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 64-bit configure | 58.8 | 94.9 | 1.6 | 13.3 | 4.4 |
| 64-bit `make -j4` | 89.6 | 357.6 | 4.0 | 33.0 | 2.7 |
| i386 configure | 374.2 | 110.9 | **0.30** | 17.5 | 21 |
| i386 `make -j4` | 223.2 | 464.0 | 2.1 | 38.2 | 5.8 |

What i386 costs over 64-bit, same boot:

| | configure | make |
| --- | ---: | ---: |
| real x86 | 1.3 | 1.2 |
| QEMU | 1.2 | 1.3 |
| natemu | 6.4 | 2.5 |

So building for i386 costs real hardware and QEMU 20 to 30% more than building for 64 bits. Under natemu it costs 2.5
to 6.4 times. The difference is natemu's, and the profile says where it is.

The reference's times are from three runs that agree within 0.3 s; the last was taken with the x86 machine idle (load
0.15, no other container), which the run records. The existing `io5-solo` case was run once as well, since its script
is shared: it passed, with its solo configure at 62.1 s and solo make at 89.4 s.

## Profile: where the i386 build's time goes

One profile of the first natemu run (case `io5-solo32-profile`: natemu's timing log and the shim built with its
accounting; `tests/io/io5_compiles.py` makes the summary). Shares are of vCPU time that is not halted.

| Step | waiting for the host: compiling | in translated code | installing new blocks (link, place, patch) |
| --- | ---: | ---: | ---: |
| 64-bit configure | 8% | 39% | 9% |
| 64-bit make | 3% | 70% | 6% |
| i386 configure | **69%** | 8% | 10% |
| i386 make | **49%** | 35% | 6% |

The request log covers the first 62% of the i386 configure (the log stops at its size limit, 1 GiB per vCPU). In that
part:

| Compiles | count | seconds |
| --- | ---: | ---: |
| all | 14,344,170 | 182.4 |
| bytes not compiled before in this boot | 91,365 (0.6%) | 1.1 |
| the same bytes, at another address | 12,956,393 (90%) | 166.9 |
| the same bytes, at the same address | 1,063,457 (7%) | 12.9 |

For comparison, the whole 64-bit configure made 121,262 compiles (2.2 s). A compile costs about 12.7 microseconds: 9.6
in the compiler's round trip, 2.3 in admitting its answer. debootstrap shows the same picture (8.1 million compiles, of
which 110 thousand were new bytes).

Why: natemu keeps the compiler's answers for 64-bit code and moves them to another address when the same bytes turn up
there. For 32-bit code it does neither, so a request for 32-bit code is always compiled. Debian's i386 programs and
libraries are position-independent and load at a random address in every process, and a configure run starts thousands
of processes, each of which compiles its shell, its libc and its tools again.

## Costs above 10% of the toolchain build, labelled

| Cost | Share | Label |
| --- | --- | --- |
| 32-bit code compiled again in every process | 69% of the i386 configure, 49% of the i386 make | **Flaw.** A wasteful design: identical work repeated. Not fixed here (the owner's decision); see below |
| Installing those blocks again (link, place, patch) | 10% of the i386 configure, 6% of the make | Follows from the flaw; falls with it |
| Running translated code | 35% of the i386 make, 8% of the configure | The build's own work, not an overhead |

Nothing else reaches 10% of either i386 step. Without the compiles the i386 configure would be about 120 s of vCPU time
against the 64-bit configure's 58 s. What that remainder consists of cannot be attributed while the compiles dominate
the profile: measure again once the reuse exists.

**The flaw's fix is its own task**, right after this checkpoint (branch `m2-reuse32`): kept answers for 32-bit code and
their relocation. The plan's estimate was 11 to 21 agent hours for those two, 15 to 29 with the shared pool. This
profile adds one fact to it: relocation is the part that matters. 90% of the compiles are the same bytes at another
address; keeping answers without moving them would remove only 7%. The persistent cache and sharing between vCPUs for
32-bit code come after that, and stay in the backlog.

## 32-bit code against QEMU

1 vCPU unless said; seconds, run 1 of one boot each. The natemu column was measured in this round (two fixes to 32-bit
SSE code went in after the round table of task 4). Both emulators ran the same image and printed the same output
hashes. "Real x86" is the same binary on the Ryzen, from task 4's page, where recorded.

| Case | natemu | QEMU | speed-up | real x86 |
| --- | ---: | ---: | ---: | ---: |
| CPython, i386 configure (4 vCPUs) | 374.2 | 110.9 | 0.30 | 17.5 |
| CPython, i386 `make -j4` (4 vCPUs) | 223.2 | 464.0 | 2.1 | 38.2 |
| `hackbench32` | 10.68 | 17.79 | 1.7 | |
| `hackbench32-int80` | 25.29 | 24.48 | 0.97 | |
| `sqlite32` | 7.86 | 33.70 | 4.3 | |
| `startup32` | 7.18 | 29.15 | 4.1 | |
| c-ray, one item (below) | | | 86 | |
| SciMark, one item (below) | | | 22 | |
| **Geometric mean, 8 items** | | | **3.7** | |
| **Without the x87 rows** | | | **3.0** | |

Without the x87 rows, the c-ray item is its SSE2 build alone (36) and the SciMark item its SSE2 build alone (10). QEMU
runs x87 in software, which is why those rows are so large; they say more about QEMU than about natemu.

c-ray's variants count as one item, the geometric mean of their ratios. Under QEMU only the small scene (320x240) was
run, to bound its time:

| c-ray, 32-bit | natemu | QEMU | speed-up | real x86 |
| --- | ---: | ---: | ---: | ---: |
| x87, fast-math, small (`cray32-small`) | 0.645 | 80.92 | 125 | 0.58 |
| x87, strict, small (`cray32-strict-small`) | 0.606 | 86.50 | 143 | 0.61 |
| SSE2, small (`cray32-sse2-small`) | 0.285 | 10.27 | 36 | 0.25 |
| x87, fast-math, full scene (`cray32`) | 9.18 | not run | | 9.19 |
| x87, strict, full scene (`cray32-strict`) | 8.72 | not run | | 9.63 |
| SSE2, full scene (`cray32-sse2`) | 3.91 | not run | | 3.91 |

SciMark counts as one item: the geometric mean of its five kernels' ratios over both 32-bit builds (ten ratios). Its
own composite is not used: it is an arithmetic mean of Mflops, which LU dominates. Mflops, higher is better, run 1:

| SciMark, 32-bit | FFT | SOR | Monte Carlo | sparse | LU |
| --- | ---: | ---: | ---: | ---: | ---: |
| x87: natemu | 902 | 1698 | 293 | 1084 | 1901 |
| x87: QEMU | 10.9 | 30.6 | 15.9 | 26.2 | 21.6 |
| x87: speed-up | 82 | 56 | 18 | 41 | 88 |
| x87: real x86 | 1869 | 1226 | 852 | 4378 | 4571 |
| SSE2: natemu | 1525 | 4080 | 257 | 2912 | 4291 |
| SSE2: QEMU | 149 | 312 | 33 | 297 | 434 |
| SSE2: speed-up | 10 | 13 | 7.9 | 9.8 | 9.9 |
| SSE2: real x86 | 4098 | 2887 | 796 | 5541 | 10276 |

**FFT varies from run to run.** In this round natemu's FFT read 902 then 1119 (x87) and 1525 then 1807 (SSE2) in the
two runs of one boot; task 4 saw up to 40%. It is not explained. Read the FFT column, and anything built on it, with
that in mind.

**Against real x86**, over the four items that have a recorded x86 number, natemu takes this many times as long:
configure 21, make 5.8, c-ray 1.02 (six variants), SciMark 2.0 (ten kernels). Geometric mean 4.0, and 4.0 again without
the x87 rows (c-ray SSE2 1.07, SciMark SSE2 1.9). The two build rows carry it; c-ray is at parity.

**`hackbench32-int80`: level with QEMU.** A 32-bit program that makes every system call by INT 0x80 (Go's 386 runtime
does) runs at QEMU's speed, 2.4 times slower than the same program going through the kernel's helper page. Task 4
measured where that time goes and built no fast path; it is on the owner's list from that task. A gap, known.

## 64-bit code against QEMU

Nothing in this checkpoint changes 64-bit code, so its numbers are reused, as the owner asked, and labelled. 1 vCPU,
seconds, first run.

| Case | natemu | from | QEMU | from | speed-up |
| --- | ---: | --- | ---: | --- | ---: |
| `build` | 9.47 | Nov 14 | 29.34 | Nov 14 | 3.1 |
| `zstd` | 3.06 | Nov 14 | 29.78 | Nov 14 | 9.7 |
| `hackbench` | 10.61 | Nov 21 | 16.34 | Nov 14 | 1.5 |
| `locks` | 3.90 | Nov 14 | 11.08 | Nov 14 | 2.8 |
| `luajit` | 3.60 | Nov 14 | 18.38 | Nov 14 | 5.1 |
| `luajit-interp` | 4.08 | Nov 14 | 30.34 | Nov 14 | 7.4 |
| `mem` | 7.87 | Nov 14 | 16.64 | Nov 14 | 2.1 |
| `7zip` | 3.91 | Nov 14 | 20.80 | Nov 14 | 5.3 |
| `sqlite` | 6.22 | Nov 21 | 22.82 | Nov 14 | 3.7 |
| `avx` | 8.54 | Nov 14 | 45.12 | Nov 14 | 5.3 |
| c-ray, one item: `cray` | 3.06 | Nov 21 | 130.35 | Nov 14 | 43 |
| c-ray, one item: `cray-strict` | 3.32 | Nov 21 | 132.40 | this round | 40 |
| SciMark, one item (below) | | Nov 21 | | this round | 11 |
| **Geometric mean, 12 items** | | | | | **5.3** |
| CPython, 64-bit configure (4 vCPUs) | 58.8 | this round | 94.9 | this round | 1.6 |
| CPython, 64-bit `make -j4` (4 vCPUs) | 89.6 | this round | 357.6 | this round | 4.0 |
| **Geometric mean with the two build rows, 14 items** | | | | | **4.8** |

- "Nov 14": [the benchmarks page](../benchmarks.md), main as of November 14, median of 3 boots. "Nov 21": task 4's
  final round on `m2-32bit`, one boot. natemu has become faster on several Nov 14 rows since (checkpoint A measured
  `build` at 5.97 and `zstd` at 2.49 in warm runs), so this table understates today's natemu where it says Nov 14.
- There are no x87 rows in this table, so its mean is the same with and without them. QEMU's floating point is
  software for SSE too, which is what the c-ray and SciMark items show.
- The benchmarks page's 4.8 for "11 benchmarks, 1 vCPU" counted `cray` alone as its floating-point row and had no
  SciMark; with these reused numbers the same eleven give 5.0.

| SciMark, 64-bit | FFT | SOR | Monte Carlo | sparse | LU |
| --- | ---: | ---: | ---: | ---: | ---: |
| natemu (Nov 21) | 2474 | 4205 | 850 | 3201 | 3616 |
| QEMU (this round) | 167 | 293 | 80 | 322 | 458 |
| speed-up | 15 | 14 | 11 | 9.9 | 7.9 |
| real x86 | 4213 | 2877 | 983 | 5499 | 11639 |

## What was built

Tests and their tools only.

- `tests/io/run_io5.py`: `--i386` (the workload above) and `--backend kvm` (the same boots on the x86 machine).
- `tests/bench/family.toml`: `io5-solo32`, `io5-solo32-profile`, `io5-solo32-qemu`, `io5-solo32-reference`, and QEMU
  cases for the 32-bit guest benchmarks and for `cray-strict` and `scimark`.
- `tests/io/io5_compiles.py`: the profile summary used above.
- `tools/bench/run.py`: a QEMU record names its version, machine type and CPU model; the machine type is pinned; a boot
  stopped by its cap is recorded with what it reached.
- `testkit/harness/bench.py`: a case may set its own timeout, and a case marked `remote = true` (the reference boot)
  takes one core instead of the whole machine and runs only when seeding. This is a change to the framework, so the
  gate applies when the branch merges.

Runs: `io5-solo32` under natemu twice (once with the profile), under QEMU once, on the x86 machine three times while the
check was settled; `io5-solo` once; the eleven QEMU guest cases once; the twelve 32-bit natemu guest cases once. QEMU
took 38 minutes in all, and no case reached its cap (45 minutes for the build, 10 for each guest case). No gate,
campaign or family-wide run was made: by the testing page those come once, on the final commit, before the merge.

## Found on the way

- **The benchmark image's recipe no longer builds.** `tools/bench/image` installs named package versions from Debian's
  live mirror, and two of them (libc6 2.41-12+deb13u4, zlib1g) are gone from it. The round used the cached image that
  task 4's round used, built earlier on 192.168.12.11, so both emulators ran the same image. A Mac without that cache
  entry cannot run the guest benchmarks until the recipe takes its packages from a snapshot, as the campaign recipes do.
- **The second boot's console shows "orphan cleanup on readonly fs"** under natemu and under KVM alike. It comes from
  the guest's shutdown, not from an emulator; the offline filesystem check is clean. The console-only check would flag
  it, so the reference run leaves that line out.

## Backlog

- **Reuse of translated 32-bit code.** Kept answers and relocation are the next task (`m2-reuse32`). After them: the
  persistent cache for 32-bit code, and sharing between vCPUs, if a profile still shows compile or lookup cost.
  Then `io5-solo32` again under natemu, with its profile, to attribute what is left.
- **INT 0x80 fast path** (`hackbench32-int80` is level with QEMU).
- **`cpyb32`**: the air-gapped CPython build campaign in i386 form, with every object file's hash compared with the
  x86 machine's. It is the stronger correctness check of the 32-bit compiler, assembler and linker; this checkpoint
  compares the built interpreter's behaviour only.
- **More 32-bit benchmark programs**: `build32`, `7zip32`, `luajit32` (new 32-bit code written at run time), `zstd32`.
- **A C++ program** in the gate's `i386` suite (exceptions, iostreams, threads).
- **Building a toolchain itself** (binutils or GCC) in the i386 userland.
- **Three-way ratios** (32-bit against 64-bit under natemu, QEMU and real x86) for every program, beyond the build's.
- The names of `test_math`'s two failing tests under natemu; the benchmark image recipe's snapshot pin; the FFT
  kernel's run-to-run variance.
- Native timing of the build on the second x86 machine (192.168.12.11) was optional and not done: the KVM boot on the
  Ryzen reproduces the 64-bit build's recorded native times (13.2 and 33.5 s) within 0.3 s.

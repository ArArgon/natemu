# natemu benchmarks

natemu compared with QEMU TCG, both running the same x86 Linux guest on the same Mac, an Apple M4.

| | natemu speed-up over QEMU |
| --- | --- |
| 64-bit programs, 12 items, 1 vCPU | **5.9×** (geometric mean; 1.5× to 41×) |
| 64-bit programs, 11 items, 4 vCPUs | **5.0×** (1.6× to 41×) |
| 64-bit, the 1-vCPU items with the CPython configure and build (4 vCPUs), 14 items | **5.4×** |
| 32-bit programs, 8 items, 1 vCPU (the CPython build at 4) | **5.3×** (geometric mean; 0.95× to 85×; **4.3×** without the x87 builds) |
| Debian cloud image: package installs and a build (io5), 4 vCPUs, November 14 | **1.7×** |
| OpenGL on the CPU (glmark2 on llvmpipe), 4 vCPUs, the glmark2 score | **14×** (AVX2); **23×** (SSE4.1) |
| Web browsing (Speedometer 3.1 in Firefox), 4 vCPUs, the Speedometer score | **2.3×** |

32-bit and 64-bit code are summarised separately. A program measured in several builds or kernels (c-ray's builds,
SciMark's five kernels) counts as one item, the geometric mean of its ratios. At 4 vCPUs the c-ray item is `cray`
alone and SciMark, a 1-vCPU program, is not counted. The 32-bit items are the CPython configure and build, `hackbench32`,
`hackbench32-int80`, `sqlite32`, `startup32`, c-ray (its three small scenes, the ones QEMU ran) and SciMark (ten kernels:
the x87 and the SSE2 build); without the x87 builds, the c-ray and SciMark items are their SSE2 builds alone. Every item
is a row of a table below.

This page shows each benchmark's latest numbers. Each table names its bench case (`tests/bench/family.toml`, run by
`tools/natemu-test run --tier bench`), the machine, the natemu commit and the date. QEMU and real x86 are the control
group: QEMU was measured on the same M4 and real x86 on an x86 reference machine, at the dates the tables give. Numbers
are compared only within one machine, so every emulator number here is from the M4. What moved a number, and by how
much, is in the commit messages and the pages of the work that moved it.

## Setup

- **Host:** Apple M4 (4 performance cores, 16 GiB), macOS 26. Each case ran alone on the machine, with no other VM.
- **natemu:** each table names its commit: ecb5717e (runtime 91fe2a1b, November 27, 2026) for the moved 64-bit and
  32-bit rows (`locks`, 1-vCPU `luajit-interp`, `mem`, `sqlite`, `sqlite32`, `startup32`), the 32-bit CPython build
  (`io5-solo32`), Paper, OpenGL on the CPU, Web browsing (cold and warm) and Desktop experience; 3b13c1d0 (November 24,
  2026) for the remaining 64-bit and 32-bit programs (including SciMark) and the display; 670fab60 (November 23, 2026)
  for the 32-bit segment cases. Where `ecb5717e` ran a single boot without a same-day `be88e684` control and stayed
  within 1–4% (plus `glmark2`, whose 5-boot spreads overlap `535ca1e9`'s and whose bench image was rebuilt), those tables
  either keep `3b13c1d0`'s three-boot medians / `670fab60` pending confirmation or note the overlap;
  [frame-keyed-plan.md](restructure/frame-keyed-plan.md#l6-standing-set-and-regression-attribution) lists all raw single-boot
  readings.
- **QEMU:** 11.1.2, `qemu-system-x86_64 -accel tcg,thread=multi`, on the same M4. Its CPU model is matched to the
  features natemu offers, so both guests run the same code paths. QEMU's `-cpu max` was slower on 9 of 10 tests we
  compared, so it is not used. Its numbers are older than natemu's; each table says when they were measured.
- **Real x86:** a Ryzen 7 8845HS (4 cores), the same binaries run natively, or the same guest under KVM for the CPython
  build.
- **Guest:** Debian 13 kernel 6.12.107 with `mitigations=off nokaslr` on both emulators. See
  [mitigations](#cpu-vulnerability-mitigations) for their cost.

## 64-bit programs

Every program is an existing, unmodified open-source program, run from a RAM-only Debian userland.
Each is timed on the host between markers it prints on the serial console, so boot time is
excluded, and its output is checked. A bench case boots the guest once and runs its program twice; for the
`3b13c1d0` rows its time here is the median of the first runs of 3 boots, and for the moved `ecb5717e` rows one boot's
first run, with natemu's translation cache off (`--compile-cache off`), so the first run translates everything it runs.
A second run in the same boot is up to 7% faster on natemu at 1 vCPU and up to 17% at 4 vCPUs, and 20% and 40% faster
for `build` (22% and 43% at `ecb5717e`).

| Program | What it runs |
| --- | --- |
| `build` | `make -jN` of libzstd (zstd 1.5.7) with gcc 14 at `-O0`: processes, page faults, new code |
| `zstd` | zstd 1.5.7 compressing 64 MiB of the Silesia corpus at level 12 |
| `hackbench` | rt-tests 2.8 `hackbench -p -g 4 -l 5000`: wake-ups, context switches |
| `locks` | a threaded mutex, atomic and futex contention test |
| `luajit` | LuaJIT 2.1 running 16 programs from its benchmark suite |
| `luajit-interp` | the same with the JIT off (`-joff`) |
| `mem` | stress-ng 0.19.02 `vm`, `mmap` and `fork` stressors with `--verify` |
| `cray` | c-ray 1.1 multithreaded ray tracer, 1280×960 (the release's `-O3 -ffast-math`, so it links crtfastmath and runs with MXCSR DAZ+FTZ set) |
| `cray-strict` | the same renderer built without `-ffast-math` (DAZ and FTZ clear, ordinary IEEE FP): the FP-heavy program for the non-DAZ mode that FP gates need; it renders the same picture as `cray` |
| `7zip` | 7-Zip 25.01 LZMA2 compression of 12 MiB of Silesia, then a test pass |
| `sqlite` | SQLite 3.46.1 `speedtest1 --memdb --size 160` (single-threaded) |
| `avx` | x264 0.164 encoding 300 CIF frames on its AVX2 paths |
| `scimark` | SciMark 2.0's five kernels (FFT, SOR, Monte Carlo, sparse matrix multiply, LU), each for at least 2 s, in Mflops |

Bench cases `guest-build` to `guest-scimark` (at 4 vCPUs with `TESTKIT_BENCH_VCPUS=4`); Apple M4; natemu 3b13c1d0
(November 24, 2026, the median of 3 boots' first runs), except the four moved rows (`locks`, 1-vCPU `luajit-interp`,
`mem`, `sqlite`), which are natemu ecb5717e (runtime 91fe2a1b, November 27, 2026, one boot's first run). QEMU 11.1.2
on the same M4: November 14, 2026, the median of 3 boots' first runs
(`tools/bench/run.py`, the script the bench cases run, before they existed); `cray-strict` and SciMark November 21-22,
2026, one boot's first run (cases `guest-cray-strict-qemu` and `guest-scimark-qemu`, 1 vCPU). Seconds, lower is better:

| Program | natemu, 1 vCPU | QEMU, 1 vCPU | speed-up | natemu, 4 vCPUs | QEMU, 4 vCPUs | speed-up |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `build` | 7.252 | 29.34 | 4.0× | 3.732 | 12.35 | 3.3× |
| `zstd` | 2.635 | 29.78 | 11.3× | 1.321 | 11.09 | 8.4× |
| `hackbench` | 11.099 | 16.34 | 1.5× | 3.267 | 5.39 | 1.6× |
| `locks` | 3.639 | 11.08 | 3.0× | 7.308 | 27.55 | 3.8× |
| `luajit` | 2.983 | 18.38 | 6.2× | 1.083 | 5.32 | 4.9× |
| `luajit-interp` | 3.688 | 30.34 | 8.2× | 1.036 | 9.05 | 8.7× |
| `mem` | 6.489 | 16.64 | 2.6× | 2.226 | 4.99 | 2.2× |
| `cray` | 3.077 | 130.35 | 42.4× | 1.035 | 42.14 | 40.7× |
| `cray-strict` | 3.274 | 132.40 | 40.4× | 1.053 | not run | |
| `7zip` | 3.595 | 20.80 | 5.8× | 1.612 | 6.44 | 4.0× |
| `sqlite` | 6.095 | 22.82 | 3.7× | 6.147 | 22.76 | 3.7× |
| `avx` | 8.004 | 45.12 | 5.6× | 3.245 | 16.56 | 5.1× |

SciMark (`guest-scimark`, 1 vCPU) times its own kernels, each for at least 2 s, so its result is the Mflops it prints,
not its run time: higher is better, per kernel the median of the 3 boots' first runs. QEMU's is one boot's first run;
real x86's is the same binary on the Ryzen (November 20-21, 2026):

| SciMark, 64-bit | FFT | SOR | Monte Carlo | sparse | LU |
| --- | ---: | ---: | ---: | ---: | ---: |
| natemu | 3163 | 4294 | 863 | 3290 | 6775 |
| QEMU | 167 | 293 | 80 | 322 | 458 |
| speed-up | 18.9× | 14.7× | 10.8× | 10.2× | 14.8× |
| real x86 | 4213 | 2877 | 983 | 5499 | 11639 |

FFT varies from run to run: its first runs read 3163, 3198 and 1777 Mflops in the three boots, and its second runs
1716 to 3105.

The gap is largest for floating-point and vector code (`cray`, `zstd`, `luajit-interp`), which
natemu runs as native ARM64 instructions. It is smallest where the guest kernel does most of the
work (`hackbench`, `mem`): context switches, page faults and system calls still cost natemu a trip
through its in-VM runtime. `sqlite` is single-threaded, so it does not speed up with more vCPUs.
`locks` slows down with more vCPUs on both emulators, as it does on real hardware: it measures
lock hand-off between cores.

## 32-bit programs

Some of the same programs built as 32-bit code (i686), the way Debian builds its i386 packages, with x87 floating point
unless the case says SSE2, run by the same 64-bit kernel at 1 vCPU:

| Program | What it runs |
| --- | --- |
| `hackbench32` | `hackbench`, making its system calls through the kernel's 32-bit vDSO (SYSENTER) |
| `hackbench32-int80` | the same, booted with `vdso32=0`, so every system call is an INT 0x80, as in Go's 386 runtime |
| `sqlite32` | speedtest1, as `sqlite` |
| `startup32` | speedtest1 at `--size 5` run 40 times, one process each: mostly program start |
| `cray32`, `cray32-strict`, `cray32-sse2` | c-ray built as `cray` and as `cray-strict` (x87), and for SSE2; `-small`: the scene at 320×240, 1/16 of the pixels |
| `scimark32`, `scimark32-sse2` | SciMark, the x87 and the SSE2 build |

Bench cases `guest-hackbench32` to `guest-scimark32-sse2`; Apple M4; natemu 3b13c1d0 (November 24, 2026, the median of 3
boots' first runs), except `sqlite32` and `startup32`, which are natemu ecb5717e (runtime 91fe2a1b, November 27, 2026,
one boot's first run). QEMU 11.1.2 on the same M4 (the cases with `-qemu`): November 21-22, 2026, one boot's first run,
the small c-ray scenes only. Real x86: the same binary on the Ryzen, November 20-21, 2026, where measured:

| Program | natemu | QEMU | speed-up | real x86 |
| --- | ---: | ---: | ---: | ---: |
| `hackbench32` | 11.006 | 17.79 | 1.6× | |
| `hackbench32-int80` | 25.671 | 24.48 | 0.95× | |
| `sqlite32` | 7.928 | 33.70 | 4.3× | |
| `startup32` | 7.055 | 29.15 | 4.1× | |
| `cray32-small` | 0.643 | 80.92 | 126× | 0.58 |
| `cray32-strict-small` | 0.600 | 86.50 | 144× | 0.61 |
| `cray32-sse2-small` | 0.308 | 10.27 | 33.3× | 0.25 |
| `cray32` | 9.230 | not run | | 9.19 |
| `cray32-strict` | 8.791 | not run | | 9.63 |
| `cray32-sse2` | 3.915 | not run | | 3.91 |

QEMU runs x87 code in software, which is why the x87 rows are so large; they say more about QEMU than about natemu.
`hackbench32-int80` is level with QEMU: natemu has no fast path yet for system calls made by INT 0x80.

SciMark, as for 64-bit code (Mflops, higher is better; per kernel the median of the 3 boots' first runs; QEMU's one
boot's first run; real x86 the same binaries on the Ryzen, November 20-21, 2026):

| SciMark, 32-bit | FFT | SOR | Monte Carlo | sparse | LU |
| --- | ---: | ---: | ---: | ---: | ---: |
| x87: natemu | 1298 | 1643 | 292 | 1186 | 1938 |
| x87: QEMU | 10.9 | 30.6 | 15.9 | 26.2 | 21.6 |
| x87: speed-up | 119× | 54× | 18× | 45× | 90× |
| x87: real x86 | 1869 | 1226 | 852 | 4378 | 4571 |
| SSE2: natemu | 2649 | 3887 | 257 | 2911 | 6693 |
| SSE2: QEMU | 149 | 312 | 33 | 297 | 434 |
| SSE2: speed-up | 18× | 12× | 7.8× | 9.8× | 15× |
| SSE2: real x86 | 4098 | 2887 | 796 | 5541 | 10276 |

## Whole-system workloads

### Debian cloud image: package installs and a build (io5)

The stock Debian 13 nocloud image (20260914-2601), unchanged, with 4 vCPUs, 8 GiB, a disk, the
network and `mitigations=off`. The first boot installs packages with apt, grows the root filesystem,
then configures, builds (`make -j4`) and tests CPython 3.13. While the build runs, one loop downloads
the Linux source from kernel.org and another writes, drops from cache and re-reads 2 GiB of data.

Apple M4; natemu's main branch as of November 14, 2026; QEMU the same day; one run each (`tests/io/run_io5.py`, before
the bench cases existed):

| | Time |
| --- | ---: |
| natemu | 522 s |
| QEMU | 901 s |

The download and disk loops run for as long as the build does, so their share of the work is not
identical between the two emulators.

### 32-bit (i386) CPython build

The same CPython 3.13 source as the workload above, configured and built (`make -j4`) first by the 64-bit
Debian userland and then by a Debian i386 userland in a chroot, in one boot of the cloud image (bench case `io5-solo32`,
4 vCPUs, 8 GiB). Apple M4; natemu ecb5717e (runtime 91fe2a1b); November 27, 2026; one run. QEMU: case `io5-solo32-qemu`
on the same M4, one run, November 21-22, 2026. Real x86: case `io5-solo32-reference`, the same disk and kernel under KVM
on the Ryzen, November 21-22, 2026 (three runs within 0.3 s). Seconds:

| | i386 configure | i386 make | 64-bit configure | 64-bit make |
| --- | ---: | ---: | ---: | ---: |
| natemu | 45.0 | 100.2 | 43.3 | 81.1 |
| QEMU 11.1.2 TCG | 110.9 | 464.0 | 94.9 | 357.6 |
| speed-up | 2.5× | 4.6× | 2.2× | 4.4× |
| real x86-64, Ryzen 7 8845HS | 17.5 | 38.2 | 13.3 | 33.0 |

The M1 changes (which include cross-page guard stubs, checked links, shadow fault-around and the disk and answer-map caps;
individual mechanisms were not isolated on this workload) moved these times. The i386 steps come this close to the
64-bit ones because natemu reuses translated 32-bit code across processes and addresses, shares it between processes,
and walks only current blocks on a page fault ([32-bit reuse](restructure/m2-reuse32-findings.md),
[sharing](restructure/m2-pic32-findings.md), [page-fault chains](restructure/m2-pfchain-findings.md)).

## 32-bit code: FS and GS accesses, and switching modes

Two small timing programs for the parts of 32-bit support that no stock program above exercises. Neither boots Linux:
the framework's test driver starts a few hundred bytes of x86 code directly in a natemu VM with 1 vCPU, and checks the
program's final memory before it records a time. Neither runs on natemu builds before checkpoint C of the 32-bit work
(they fault), so there are no older natemu numbers, and QEMU has not been measured on them.

**FS and GS accesses** (bench case `compat-tls-natemu`). 32-bit programs reach their thread's private data through the
FS or GS segment register. Linux gives that segment a base address and the full 4 GiB limit; Windows gives its FS
segment a 4 KiB limit, so every access through it must be checked against the limit. A warmed 32-bit loop reads and
writes the same eight words through each kind of segment in turn:

- **flat DS:** the ordinary data segment, base 0, nothing to add or check: the baseline.
- **FS, 4 GiB limit:** the shape Linux uses for thread data. natemu adds the base and compiles no check.
- **FS or GS, 4 KiB limit:** every instruction's operand is checked against the limit inside the translated code.

The dword loop is a load and a store per word; the mixed loop is five other instruction forms per word (increment and
add to memory, compare with a constant, a byte load, an SSE load). Each is timed by the guest's clock over 800,000
accesses (2,000,000 for the mixed loop), the median of 3 samples. The loop's accesses overlap in the processor, so these
are averages per access, not the time one access takes.

Bench case `compat-tls-natemu`; Apple M4; natemu 670fab60; November 23, 2026. Nanoseconds per access, lower is better:

| Loop | flat DS | FS, 4 GiB limit | FS, 4 KiB limit | GS, 4 KiB limit |
| --- | ---: | ---: | ---: | ---: |
| dword load and store | 0.173 | 0.179 | 0.564 | 0.557 |
| mixed forms | 0.219 | 0.254 | 0.626 | not run |

Checking the limit costs about 0.4 ns per access. Through Linux's 4 GiB FS, which compiles to the same
code as on main (no check, the base added from the loaded segment), the dword loop is level with flat DS and the mixed
loop about 15% slower, beyond the samples' spread: that is the base addition, not the limit check. A read-only or expand-down FS or GS segment (no known program uses one) is
checked by natemu's in-VM runtime on every access instead, which is far slower; [the checkpoint C
findings](restructure/m2-checkpoint-c-findings.md#performance) say by how much.

**Switching between 32-bit and 64-bit code** (bench case `mixed-far-natemu`). One process loops between 64-bit and
32-bit code, as Windows' 32-bit layer does to make system calls. Each iteration makes four far transfers (a far CALL
from 64-bit into 32-bit code, one from there back into 64-bit code, and the two far RETs) and runs a dozen ordinary
instructions. It is timed on the host as the difference between 1 and 2 million iterations, so start-up and translation
drop out; the median of 3 such pairs.

Bench case `mixed-far-natemu`; Apple M4; natemu 670fab60; November 23, 2026:

| | Time |
| --- | ---: |
| One iteration (4 far transfers) | 1,813 ns |
| Per far transfer, at most | about 450 ns |

The three pairs read 1,793 to 1,823 ns per iteration.

## Paper (Minecraft server) start

A Paper 26.3 server (build 257) on OpenJDK 25 starts in the stock Debian 13 cloud image: the JVM interprets, then
compiles Java with its JIT while the server loads, so natemu translates a stream of freshly written guest code. 4 vCPUs,
2 GiB, a fixed 1 GiB Java heap, PTI off (`--speculation-barriers`), natemu's translation cache off. An untimed setup boot installs the pinned
packages and generates a world with a fixed seed; then 3 boots, each with the network cut and two timed starts. Before
each start the server folder is restored from a copy of the setup's and the page cache dropped, so every start loads
the same files. A start is timed from `java -jar` to Paper's "Done" line, after which the server is killed. The guest
has no internet at any point it is measured, and Paper needs none: its launcher finds Mojang's server jar already in
place. `tests/bench/run_paper.py` describes the case in full.

Bench case `guest-paper`; Apple M4; natemu ecb5717e (runtime 91fe2a1b); November 27, 2026; seconds, the median of 3 boots:

| Start in the boot | Time | The 3 boots | Paper's own "Done" | The 3 boots |
| --- | ---: | ---: | ---: | ---: |
| first | 21.65 | 21.99, 21.65, 21.32 | 19.40 | 19.70, 19.40, 19.09 |
| second | 21.35 | 21.40, 20.77, 21.35 | 19.54 | 19.69, 19.04, 19.54 |

The M1 runtime changes (cross-page guard stubs, checked links, shadow fault-around and the caps; not isolated
individually on Paper) moved these times.

## Display

The Bochs display (`[display] device = "bochs"`): the guest's `bochs` DRM driver sets the 1024 x 768 mode, and
`kms_pattern bench` (`tests/devices/kms_pattern.c`) copies 1 GiB of full frames into the framebuffer through the dumb
buffer's own mapping, then the same amount into a buffer in guest RAM. The framebuffer is mapped device memory, so the
guest writes it natively: it runs at the speed of RAM. Then the program holds the pattern on the screen, and under
natemu the runner takes 100 PNG snapshots of it through the control socket (`snapshot`, timed as a client sees it: the
copy, the PNG, the base64 JSON line and the client's decoding). The control group is QEMU's `bochs-display` on the same
Mac (TCG), the same guest and program. 1 vCPU, 2 GiB, one run each (`tests/devices/run_display.py bench`).

Bench cases `display-fill` and `display-fill-qemu`; Apple M4; November 24, 2026; natemu 3b13c1d0, QEMU with natemu 45bdaa88's
run:

| | natemu | QEMU |
| --- | ---: | ---: |
| 1 GiB into the framebuffer | 0.026 s (38.3 GiB/s) | 0.301 s (3.3 GiB/s) |
| 1 GiB into guest RAM | 0.027 s (37.6 GiB/s) | 0.289 s (3.5 GiB/s) |
| framebuffer / RAM | 0.98 | 1.04 |
| a 1024 x 768 snapshot | 4.6 ms | |

The pattern's PNG is 27 KB; a busier picture compresses less and takes longer to encode.

## OpenGL on the CPU (llvmpipe)

glmark2 2023.01, a widely used OpenGL benchmark, drawn by Mesa 25.0.7's llvmpipe: OpenGL rendered entirely by the CPU,
the way a Linux desktop draws when it has no graphics card (a GNOME desktop on natemu's display does). llvmpipe turns
each shader into x86 vector code with LLVM and spreads the frame over the vCPUs. glmark2 draws off-screen at 800 x 600
under Xvfb (an X server without a screen), ten of its scenes for 5 seconds each, and reports each scene's frames per
second and a score, the mean of the scenes' FPS. By default llvmpipe uses AVX2 (256-bit vectors; it fetches texture
pixels with AVX2's gather instructions); the SSE4.1 columns hold it to SSE4.1 (`GALLIUM_OVERRIDE_CPU_CAPS=sse4.1`,
128-bit vectors, no gathers). The guest checks llvmpipe's vector width each boot, and the first boot also runs glmark2's
own check of the pictures (`--validate`), which passes on every scene that has one.

Bench cases `glmark2-natemu` and `glmark2-natemu-sse41` (4 vCPUs, 4 GiB, translation cache off); Apple M4; natemu
ecb5717e (runtime 91fe2a1b); November 27, 2026. QEMU 11.1.2 on the same M4 (TCG, the CPU model of the other guest benchmarks): cases
`glmark2-qemu` and `glmark2-qemu-sse41`, November 25, 2026. Real x86: cases `glmark2-kvm` and `glmark2-kvm-sse41`, the same
kernel and pinned Debian userland recipe under KVM on the Ryzen with the same CPU model, November 25, 2026 (each bench
run built its initramfs from the pinned package recipe: `ecb5717e` `userland_sha256` `98e93b19`, `535ca1e9` `1af94d90`,
Ryzen KVM `deca0638`). FPS, higher is better, the median of 5 boots:

| Scene | natemu, AVX2 | natemu, SSE4.1 | QEMU, AVX2 | QEMU, SSE4.1 | real x86, AVX2 | real x86, SSE4.1 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `build` (geometry, vertex buffers) | 681 | 886 | 54 | 52 | 1311 | 1136 |
| `texture` (linear filtering) | 1095 | 1907 | 93 | 95 | 2342 | 2055 |
| `shading` (Phong) | 314 | 522 | 21 | 21 | 687 | 541 |
| `bump` (normal maps) | 1099 | 1729 | 80 | 77 | 2107 | 1840 |
| `effect2d` (3 x 3 convolution) | 433 | 762 | 19 | 18 | 1344 | 979 |
| `pulsar` (blending) | 742 | 1193 | 58 | 53 | 1635 | 1358 |
| `desktop` (blurred windows) | 70 | 134 | 6 | 7 | 213 | 169 |
| `jellyfish` | 140 | 250 | 11 | 12 | 342 | 282 |
| `refract` | 26 | 35 | 2 | 2 | 47 | 42 |
| `conditionals` (shader branches) | 720 | 879 | 50 | 44 | 1063 | 932 |
| **score** | **527** | **833** | **38** | **37** | **1109** | **931** |

natemu's 5 scores were 518 to 556 (AVX2) and 823 to 853 (SSE4.1); QEMU's 33 to 42 and 36 to 40; real x86's 1104 to 1117
and 925 to 947. The first boot is a little faster in the first scene because its validation run has already translated
glmark2's start-up; the median is not affected.

Real x86 gains 19% from AVX2; natemu runs each 256-bit AVX2 operation as two 128-bit halves, so AVX2 brings it little
here (it scores lower than its own SSE4.1 run), and its gathers, now run inside the translated code, are still a load
per element. (Both `glmark2` cases at `ecb5717e` overlap `535ca1e9`'s five-boot spreads and used a rebuilt bench image;
see [frame-keyed-plan.md](restructure/frame-keyed-plan.md#l6-standing-set-and-regression-attribution).)

## Web browsing (Speedometer 3.1)

Speedometer 3.1, the browser benchmark that the WebKit, Chromium and Firefox engine teams maintain together, in Firefox
ESR 140 without a screen (headless). Speedometer drives 20 small web apps the way a user would (adding and completing
to-do items in the same app written with React, Angular, Vue, jQuery and others, laying out news pages, typing into code
and rich-text editors, drawing charts) and times each; an iteration's score is 1000 divided by the geometric mean of its
20 suites' times in milliseconds, higher is better. The guest is Debian 13 with Firefox, Speedometer's files and a small
web server that serves them from inside the guest, which has no outside network: a results hook in the page sends the numbers to
that server, which prints them on the console. Firefox starts with a fresh profile each boot, so every boot loads and
translates the browser from nothing. Each boot runs Speedometer's 20 suites 5 times (`iterationCount=5`); its score is
the mean of the 5 iterations' scores.

Bench case `speedometer-natemu` (4 vCPUs, 4 GiB, translation cache off); Apple M4; natemu ecb5717e (runtime 91fe2a1b);
November 27, 2026. QEMU 11.1.2 on the same M4 (TCG, the CPU model of the other guest benchmarks): case `speedometer-qemu`,
November 25, 2026. Real x86: case `speedometer-kvm`, the same kernel and guest under KVM on the Ryzen with the same CPU
model, November 25, 2026. The median of 3 boots:

| | Speedometer score | the 3 boots |
| --- | ---: | ---: |
| natemu | **3.33** | 3.27 to 3.35 |
| QEMU | **1.43** | 1.39 to 1.45 |
| real x86 | **16.1** | 16.1 to 16.2 |

natemu is 2.3 times as fast as QEMU here (the real x86 control on the Ryzen is shown for reference, not as a cross-machine
ratio). The M1 changes (cross-page guard stubs, checked links, shadow fault-around and the caps; not isolated
individually on Speedometer) moved natemu's numbers. Within a boot the 5 iterations differ more than the boots do
(Speedometer's own spread is 19 to 20% on natemu, 6 to 8% on QEMU, 5 to 7% on real x86). Each suite's time per
iteration, in ms (lower is better; the median of the 3 boots' means):

| Suite | natemu | QEMU | speed-up | real x86 |
| --- | ---: | ---: | ---: | ---: |
| `TodoMVC-JavaScript-ES5` | 278 | 832 | 3.0× | 73 |
| `TodoMVC-JavaScript-ES6-Webpack-Complex-DOM` | 266 | 770 | 2.9× | 73 |
| `TodoMVC-WebComponents` | 145 | 348 | 2.4× | 30 |
| `TodoMVC-React-Complex-DOM` | 327 | 609 | 1.9× | 62 |
| `TodoMVC-React-Redux` | 350 | 675 | 1.9× | 68 |
| `TodoMVC-Backbone` | 340 | 538 | 1.6× | 49 |
| `TodoMVC-Angular-Complex-DOM` | 363 | 682 | 1.9× | 62 |
| `TodoMVC-Vue` | 220 | 399 | 1.8× | 35 |
| `TodoMVC-jQuery` | 695 | 2564 | 3.7× | 215 |
| `TodoMVC-Preact-Complex-DOM` | 108 | 209 | 1.9× | 19 |
| `TodoMVC-Svelte-Complex-DOM` | 109 | 189 | 1.7× | 18 |
| `TodoMVC-Lit-Complex-DOM` | 145 | 296 | 2.0× | 26 |
| `NewsSite-Next` | 608 | 1815 | 3.0× | 154 |
| `NewsSite-Nuxt` | 501 | 1410 | 2.8× | 124 |
| `Editor-CodeMirror` | 284 | 349 | 1.2× | 30 |
| `Editor-TipTap` | 496 | 1143 | 2.3× | 103 |
| `Charts-observable-plot` | 347 | 711 | 2.0× | 65 |
| `Charts-chartjs` | 476 | 1306 | 2.7× | 95 |
| `React-Stockcharts-SVG` | 639 | 1275 | 2.0× | 103 |
| `Perf-Dashboard` | 407 | 1635 | 4.0× | 132 |

**With the translation cache warm** (bench case `speedometer-natemu-warm`, the same M4, natemu ecb5717e, runtime 91fe2a1b,
November 27, 2026): natemu keeps its translated code on disk across boots, as a desktop keeps it across sessions. One
untimed boot fills the cache, then 3 timed boots reuse it. The score is **3.33** (3.29 to 3.39), the same as cold.
The disk cache no longer looks up or stores code on pages the guest writes at run time (such as what Firefox's JavaScript
compiler writes), and an answer cached for one address is moved to the address that asks for it; the debug tier's
`speedometer-profile-warm` shows where the warm boots' time goes.

A browser runs JavaScript compiled by its own JIT, in several processes per vCPU, so natemu translates a stream of
freshly written code and switches address spaces often; its lead over QEMU is far smaller here than on the compiled
programs above. The debug tier's `speedometer-profile` ([testing](testing.md#the-debug-tier)) shows where natemu's
time goes in this workload.

## Desktop experience

A Debian 13 KDE Plasma desktop session on natemu's `bochs` display (`1920 x 1080`), timed with natemu's ordinary
resident (no request log), 4 vCPUs, 8 GiB, `--speculation-barriers` and natemu's translation cache off (`desktop-kde`
and `desktop-kde-browser` in `tests/bench/family.toml`). Each starts an application cold, closes it and starts it a
second time in the same boot; `desktop-kde-browser` ends with Speedometer 3.1 in Chromium (5 iterations).

Bench cases `desktop-kde` and `desktop-kde-browser`; Apple M4; natemu ecb5717e (runtime 91fe2a1b); November 27, 2026;
one run each (no QEMU desktop control has been measured yet):

| Phase or application | Metric | `desktop-kde` | `desktop-kde-browser` |
| --- | --- | ---: | ---: |
| Session start | `shell_mapped`, s | 20.945 | 21.617 |
| | `idle_desktop`, s | 24.85 | 24.88 |
| Konsole | 1st start (total), s | 1.319 (5.24) | |
| | 2nd start (total), s | 0.994 (5.29) | |
| Dolphin | 1st start (total), s | 1.638 (5.34) | |
| | 2nd start (total), s | 0.976 (4.36) | |
| Chromium | 1st start (total), s | | 7.787 (11.46) |
| | 2nd start (total), s | | 3.754 (10.70) |
| | 3rd start (Speedometer boot launch; total includes Speedometer), s | | 4.062 (95.97) |
| Firefox ESR | 1st start (total), s | | 8.150 (32.67) |
| | 2nd start (total), s | | 4.030 (24.77) |
| Chromium (Speedometer 3.1) | score (5 iterations) | | **3.31** (2.24 to 3.88) |

The M1 changes (cross-page guard stubs, checked links, shadow fault-around and the caps; not isolated individually on
the desktop) moved session start, warm browser starts and Speedometer. UI frame rates stay in the diagnostic record until
a presentation counter replaces today's display notifications, which are capped at one per 50 ms.

## CPU vulnerability mitigations

The numbers above use `mitigations=off`, except the Paper start and the KDE desktop, which keep the default kernel
command line and turn PTI off with `--speculation-barriers`. On a default kernel command line natemu reports its virtual
CPU as affected by Meltdown, so Linux turns on page table isolation (PTI). natemu has no fast path
for PTI's page-table switches yet. The cloud-image workload above, on the M4 with an earlier natemu build
(November 13, 2026):

| Guest configuration | Time |
| --- | ---: |
| Default command line (PTI on) | 1,656 s |
| Default command line + `--speculation-barriers` (PTI off) | 703 s |
| `mitigations=off` | 705 s |

The PTI run used an earlier version of the test script. On individual programs, Linux's other
mitigations cost about 1% once PTI is off. See the
[README](../README.md#performance-and-cpu-vulnerability-mitigations) for how to avoid the cost.

## Reproducing

Every benchmark is a case of the bench family: `tools/natemu-test run --tier bench` runs them all, each alone on
the machine, and `-k guest-paper` (any case name) one. The framework builds or downloads everything they need into
its cache on first use (the guest initramfs from `tools/bench/image`, the Debian kernel and cloud image, the Paper jar:
pinned in `tests/resources.toml`), so a fresh clone on any Mac can run them. Results stay per machine: compare
numbers only within one machine.

# natemu benchmarks

natemu compared with QEMU TCG, both running the same x86 Linux guest on the same Mac, an Apple M4.

| | natemu speed-up over QEMU |
| --- | --- |
| 64-bit programs, 12 items, 1 vCPU | **5.8×** (geometric mean; 1.5× to 41×) |
| 64-bit programs, 11 items, 4 vCPUs | **5.1×** (1.7× to 41×) |
| 64-bit, the 1-vCPU items with the CPython configure and build (4 vCPUs), 14 items | **5.2×** |
| 32-bit programs, 8 items, 1 vCPU (the CPython build at 4) | **5.1×** (geometric mean; 0.97× to 83×; **4.1×** without the x87 builds) |
| Context-switch-heavy Debian workload (cloud image), 4 vCPUs, November 14 | **1.7×** |

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
- **natemu:** 9f35eb96, measured November 22, 2026, in every natemu column below unless a table says otherwise.
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
excluded, and its output is checked. A bench case boots the guest once and runs its program twice; its time here is
the median of the first runs of 3 boots, with natemu's translation cache off (`--compile-cache off`), so the first
run translates everything it runs. A second run in the same boot is up to 6% faster on natemu at 1 vCPU and up
to 13% at 4 vCPUs, and 20% and 40% faster for `build`.

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

Bench cases `guest-build` to `guest-scimark` (at 4 vCPUs with `TESTKIT_BENCH_VCPUS=4`); Apple M4; natemu 9f35eb96;
November 22, 2026. QEMU 11.1.2 on the same M4: November 14, 2026, the median of 3 boots' first runs
(`tools/bench/run.py`, the script the bench cases run, before they existed); `cray-strict` and SciMark November 21-22,
2026, one boot's first run (cases `guest-cray-strict-qemu` and `guest-scimark-qemu`, 1 vCPU). Seconds, lower is better:

| Program | natemu, 1 vCPU | QEMU, 1 vCPU | speed-up | natemu, 4 vCPUs | QEMU, 4 vCPUs | speed-up |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `build` | 7.325 | 29.34 | 4.0× | 3.769 | 12.35 | 3.3× |
| `zstd` | 2.617 | 29.78 | 11.4× | 1.287 | 11.09 | 8.6× |
| `hackbench` | 10.910 | 16.34 | 1.5× | 3.201 | 5.39 | 1.7× |
| `locks` | 3.359 | 11.08 | 3.3× | 5.812 | 27.55 | 4.7× |
| `luajit` | 2.957 | 18.38 | 6.2× | 1.087 | 5.32 | 4.9× |
| `luajit-interp` | 3.400 | 30.34 | 8.9× | 1.030 | 9.05 | 8.8× |
| `mem` | 7.316 | 16.64 | 2.3× | 2.432 | 4.99 | 2.1× |
| `cray` | 3.072 | 130.35 | 42.4× | 1.035 | 42.14 | 40.7× |
| `cray-strict` | 3.276 | 132.40 | 40.4× | 1.141 | not run | |
| `7zip` | 3.502 | 20.80 | 5.9× | 1.564 | 6.44 | 4.1× |
| `sqlite` | 5.972 | 22.82 | 3.8× | 6.036 | 22.76 | 3.8× |
| `avx` | 7.973 | 45.12 | 5.7× | 3.244 | 16.56 | 5.1× |

SciMark (`guest-scimark`, 1 vCPU) times its own kernels, each for at least 2 s, so its result is the Mflops it prints,
not its run time: higher is better, per kernel the median of the 3 boots' first runs. QEMU's is one boot's first run;
real x86's is the same binary on the Ryzen (November 20-21, 2026):

| SciMark, 64-bit | FFT | SOR | Monte Carlo | sparse | LU |
| --- | ---: | ---: | ---: | ---: | ---: |
| natemu | 1228 | 4136 | 846 | 3089 | 3671 |
| QEMU | 167 | 293 | 80 | 322 | 458 |
| speed-up | 7.4× | 14.1× | 10.6× | 9.6× | 8.0× |
| real x86 | 4213 | 2877 | 983 | 5499 | 11639 |

FFT varies from run to run: its first runs read 1083, 1432 and 1228 Mflops in the three boots, and its second runs up
to 3073.

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

Bench cases `guest-hackbench32` to `guest-scimark32-sse2`; Apple M4; natemu 9f35eb96; November 22, 2026; seconds, the
median of 3 boots' first runs. QEMU 11.1.2 on the same M4 (the cases with `-qemu`): November 21-22, 2026, one boot's
first run, the small c-ray scenes only. Real x86: the same binary on the Ryzen, November 20-21, 2026, where measured:

| Program | natemu | QEMU | speed-up | real x86 |
| --- | ---: | ---: | ---: | ---: |
| `hackbench32` | 10.733 | 17.79 | 1.7× | |
| `hackbench32-int80` | 25.278 | 24.48 | 0.97× | |
| `sqlite32` | 7.887 | 33.70 | 4.3× | |
| `startup32` | 7.140 | 29.15 | 4.1× | |
| `cray32-small` | 0.657 | 80.92 | 123× | 0.58 |
| `cray32-strict-small` | 0.644 | 86.50 | 134× | 0.61 |
| `cray32-sse2-small` | 0.302 | 10.27 | 34.0× | 0.25 |
| `cray32` | 9.249 | not run | | 9.19 |
| `cray32-strict` | 8.795 | not run | | 9.63 |
| `cray32-sse2` | 3.912 | not run | | 3.91 |

QEMU runs x87 code in software, which is why the x87 rows are so large; they say more about QEMU than about natemu.
`hackbench32-int80` is level with QEMU: natemu has no fast path yet for system calls made by INT 0x80.

SciMark, as for 64-bit code (Mflops, higher is better; per kernel the median of the 3 boots' first runs; QEMU's one
boot's first run; real x86 the same binaries on the Ryzen, November 20-21, 2026):

| SciMark, 32-bit | FFT | SOR | Monte Carlo | sparse | LU |
| --- | ---: | ---: | ---: | ---: | ---: |
| x87: natemu | 1037 | 1653 | 292 | 1187 | 1939 |
| x87: QEMU | 10.9 | 30.6 | 15.9 | 26.2 | 21.6 |
| x87: speed-up | 95× | 54× | 18× | 45× | 90× |
| x87: real x86 | 1869 | 1226 | 852 | 4378 | 4571 |
| SSE2: natemu | 2624 | 3897 | 256 | 2875 | 4241 |
| SSE2: QEMU | 149 | 312 | 33 | 297 | 434 |
| SSE2: speed-up | 18× | 12× | 7.8× | 9.7× | 9.8× |
| SSE2: real x86 | 4098 | 2887 | 796 | 5541 | 10276 |

## Context-switch-heavy Debian workload

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

## 32-bit (i386) CPython build

The same CPython 3.13 source as the context-switch workload, configured and built (`make -j4`) first by the 64-bit
Debian userland and then by a Debian i386 userland in a chroot, in one boot of the cloud image (bench case `io5-solo32`,
4 vCPUs, 8 GiB). Apple M4; natemu 9f35eb96; November 22, 2026; one run. QEMU: case `io5-solo32-qemu` on the same M4,
one run, November 21-22, 2026. Real x86: case `io5-solo32-reference`, the same disk and kernel under KVM on the Ryzen,
November 21-22, 2026 (three runs within 0.3 s). Seconds:

| | i386 configure | i386 make | 64-bit configure | 64-bit make |
| --- | ---: | ---: | ---: | ---: |
| natemu | 58.3 | 107.1 | 53.1 | 88.6 |
| QEMU 11.1.2 TCG | 110.9 | 464.0 | 94.9 | 357.6 |
| speed-up | 1.9× | 4.3× | 1.8× | 4.0× |
| real x86-64, Ryzen 7 8845HS | 17.5 | 38.2 | 13.3 | 33.0 |

The i386 steps come this close to the 64-bit ones because natemu reuses translated 32-bit code across processes and
addresses, shares it between processes, and walks only current blocks on a page fault ([32-bit
reuse](restructure/m2-reuse32-findings.md), [sharing](restructure/m2-pic32-findings.md), [page-fault
chains](restructure/m2-pfchain-findings.md)).

## Paper (Minecraft server) start

A Paper 26.3 server (build 257) on OpenJDK 25 starts in the stock Debian 13 cloud image: the JVM interprets, then
compiles Java with its JIT while the server loads, so natemu translates a stream of freshly written guest code. 4 vCPUs,
2 GiB, a fixed 1 GiB Java heap, PTI off (`--speculation-barriers`), natemu's translation cache off. An untimed setup boot installs the pinned
packages and generates a world with a fixed seed; then 3 boots, each with the network cut and two timed starts. Before
each start the server folder is restored from a copy of the setup's and the page cache dropped, so every start loads
the same files. A start is timed from `java -jar` to Paper's "Done" line, after which the server is killed. The guest
has no internet at any point it is measured, and Paper needs none: its launcher finds Mojang's server jar already in
place. `tests/bench/run_paper.py` describes the case in full.

Bench case `guest-paper`; Apple M4; natemu 9f35eb96; November 22, 2026; seconds, the median of 3 boots:

| Start in the boot | Time | The 3 boots | Paper's own "Done" | The 3 boots |
| --- | ---: | ---: | ---: | ---: |
| first | 23.86 | 24.20, 23.86, 23.82 | 21.37 | 21.62, 21.37, 21.34 |
| second | 23.77 | 23.77, 23.67, 24.22 | 21.76 | 21.76, 21.60, 22.12 |

## Display

The Bochs display (`[display] device = "bochs"`): the guest's `bochs` DRM driver sets the 1024 x 768 mode, and
`kms_pattern bench` (`tests/devices/kms_pattern.c`) copies 1 GiB of full frames into the framebuffer through the dumb
buffer's own mapping, then the same amount into a buffer in guest RAM. The framebuffer is mapped device memory, so the
guest writes it natively: it runs at the speed of RAM. Then the program holds the pattern on the screen, and under
natemu the runner takes 100 PNG snapshots of it through the control socket (`snapshot`, timed as a client sees it: the
copy, the PNG, the base64 JSON line and the client's decoding). The control group is QEMU's `bochs-display` on the same
Mac (TCG), the same guest and program. 1 vCPU, 2 GiB, one run each (`tests/devices/run_display.py bench`).

Bench cases `display-fill` and `display-fill-qemu`; Apple M4; natemu 45bdaa88; November 24, 2026:

| | natemu | QEMU |
| --- | ---: | ---: |
| 1 GiB into the framebuffer | 0.057 s (17.5 GiB/s) | 0.301 s (3.3 GiB/s) |
| 1 GiB into guest RAM | 0.053 s (18.7 GiB/s) | 0.289 s (3.5 GiB/s) |
| framebuffer / RAM | 1.07 | 1.04 |
| a 1024 x 768 snapshot | 4.6 ms | |

The pattern's PNG is 27 KB; a busier picture compresses less and takes longer to encode.

## CPU vulnerability mitigations

The numbers above use `mitigations=off`, except the Paper start, which keeps the default kernel command line and turns
PTI off with `--speculation-barriers`. On a default kernel command line natemu reports its virtual
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

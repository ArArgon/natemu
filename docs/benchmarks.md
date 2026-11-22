# natemu benchmarks

natemu compared with QEMU TCG, both running the same x86-64 Linux guest on
the same Mac.

| | natemu speed-up over QEMU |
| --- | --- |
| 64-bit programs, 12 items, 1 vCPU | **5.3×** (geometric mean; c-ray's two builds count as one item, SciMark as one) |
| 64-bit, the same with the CPython configure and build (4 vCPUs), 14 items | **4.8×** |
| 32-bit programs, 8 items | **5.1×** (geometric mean; **4.1×** without the x87 builds; c-ray and SciMark one item each) |
| Context-switch-heavy Debian workload (cloud image), 4 vCPUs | **1.7×** |

32-bit and 64-bit code are summarised separately, and a program measured in several builds or kernels (c-ray's
variants, SciMark's five kernels) counts as one item, the geometric mean of its ratios. The 64-bit items are those of
[the M2 checkpoint B findings](restructure/m2-checkpoint-b-findings.md#64-bit-code-against-qemu) (its table says which
row is from which date). The 32-bit items are that page's too, except the CPython configure and build, which are this
page's [32-bit CPython build](#32-bit-i386-cpython-build) at eb4ef43b (configure 0.30× at checkpoint B, 1.9× now; build
2.1×, now 4.3×). The tables below are the November 14 round, one item per row.

Where the numbers come from: each table names its bench case (`tests/bench/family.toml`, run by
`tools/natemu-test run --tier bench`), the machine, the natemu commit and the date. The tables from November 14
were measured before the bench cases existed (with `tools/bench/run.py` directly) and are to be measured again
through them; numbers from experiments elsewhere in `docs/` are labelled as such and are not this page's numbers.

32-bit (i386) programs: [32-bit CPython build](#32-bit-i386-cpython-build) below; 32-bit programs against QEMU are in
[the M2 checkpoint B findings](restructure/m2-checkpoint-b-findings.md).

## Setup

- **Host:** Apple M4, macOS 26. One emulator ran at a time, with no other VM on the machine.
- **natemu:** the main branch as of November 14, 2026.
- **QEMU:** 11.1.2, `qemu-system-x86_64 -accel tcg,thread=multi`. Its CPU model is matched to the
  features natemu offers, so both guests run the same code paths. QEMU's `-cpu max` was slower on 9
  of 10 tests we compared, so it is not used.
- **Guest:** Debian 13 kernel 6.12.107 with `mitigations=off nokaslr` on both emulators. See
  [mitigations](#cpu-vulnerability-mitigations) for their cost.

## Benchmarks

Every program is an existing, unmodified open-source program, run from a RAM-only Debian userland.
Each is timed on the host between markers it prints on the serial console, so boot time is
excluded, and its output is checked. Times are the median of 3 boots, from the first run in each
boot with natemu's translation cache off. A second run in the same boot is usually 1–5% faster on
natemu, and up to 30% faster for `build`.

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

Seconds, lower is better:

| Program | natemu, 1 vCPU | QEMU, 1 vCPU | speed-up | natemu, 4 vCPUs | QEMU, 4 vCPUs | speed-up |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `build` | 9.47 | 29.34 | 3.1× | 4.66 | 12.35 | 2.7× |
| `zstd` | 3.06 | 29.78 | 9.7× | 1.47 | 11.09 | 7.5× |
| `hackbench` | 11.90 | 16.34 | 1.4× | 3.40 | 5.39 | 1.6× |
| `locks` | 3.90 | 11.08 | 2.8× | 7.72 | 27.55 | 3.6× |
| `luajit` | 3.60 | 18.38 | 5.1× | 1.33 | 5.32 | 4.0× |
| `luajit-interp` | 4.08 | 30.34 | 7.4× | 1.22 | 9.05 | 7.4× |
| `mem` | 7.87 | 16.64 | 2.1× | 2.60 | 4.99 | 1.9× |
| `cray` | 3.17 | 130.35 | 41.1× | 1.12 | 42.14 | 37.6× |
| `7zip` | 3.91 | 20.80 | 5.3× | 1.73 | 6.44 | 3.7× |
| `sqlite` | 7.31 | 22.82 | 3.1× | 7.33 | 22.76 | 3.1× |
| `avx` | 8.54 | 45.12 | 5.3× | 3.35 | 16.56 | 4.9× |

The gap is largest for floating-point and vector code (`cray`, `zstd`, `luajit-interp`), which
natemu runs as native ARM64 instructions. It is smallest where the guest kernel does most of the
work (`hackbench`, `mem`): context switches, page faults and system calls still cost natemu a trip
through its in-VM runtime. `sqlite` is single-threaded, so it does not speed up with more vCPUs.
`locks` slows down with more vCPUs on both emulators, as it does on real hardware: it measures
lock hand-off between cores.

## Context-switch-heavy Debian workload

The stock Debian 13 nocloud image (20260914-2601), unchanged, with 4 vCPUs, 8 GiB, a disk, the
network and `mitigations=off`. The first boot installs packages with apt, grows the root filesystem,
then configures, builds (`make -j4`) and tests CPython 3.13. While the build runs, one loop downloads
the Linux source from kernel.org and another writes, drops from cache and re-reads 2 GiB of data.

| | Time |
| --- | ---: |
| natemu | 522 s |
| QEMU | 901 s |

The download and disk loops run for as long as the build does, so their share of the work is not
identical between the two emulators.

## 32-bit (i386) CPython build

The same CPython 3.13 source as the context-switch workload, configured and built (`make -j4`) first by the 64-bit
Debian userland and then by a Debian i386 userland in a chroot (bench case `io5-solo32`, 4 vCPUs, 8 GiB). Times in
seconds; each natemu row is one timed run on an Apple M4, November 22, 2026.

| natemu | i386 configure | i386 make | 64-bit configure | 64-bit make |
| --- | ---: | ---: | ---: | ---: |
| Checkpoint B (213e3566) | 374.2 | 223.2 | 58.8 | 89.6 |
| 32-bit code reused across addresses, pool inside the guest (72114fb1) | 106.7 | 114.7 | 57.5 | 90.0 |
| 32-bit library code shared between processes (44d480e2) | 82.5 | 108.1 | 58.0 | 89.5 |
| page faults walk only current blocks (eb4ef43b, in main) | **59.5** | **107.0** | 54.5 | 89.0 |
| QEMU 11.1.2 TCG, same M4 (1 run, Checkpoint B) | 110.9 | 464.0 | 94.9 | 357.6 |
| real x86-64, Ryzen 7 8845HS (1 run, Checkpoint B) | 17.5 | 38.2 | 13.3 | 33.0 |

The 64-bit steps moved within the spread of single runs. How each change works and what its profile shows:
[the 32-bit reuse findings](restructure/m2-reuse32-findings.md), [the 32-bit sharing
findings](restructure/m2-pic32-findings.md), [the page-fault chain findings](restructure/m2-pfchain-findings.md).

## Paper (Minecraft server) start

A Paper 26.3 server (build 257) on OpenJDK 25 starts in the stock Debian 13 cloud image: the JVM interprets, then
compiles Java with its JIT while the server loads, so natemu translates a stream of freshly written guest code. 4 vCPUs,
2 GiB, a fixed 1 GiB Java heap, PTI off (`--speculation-barriers`), natemu's translation cache off. An untimed setup boot installs the pinned
packages and generates a world with a fixed seed; then 3 boots, each with the network cut and two timed starts. Before
each start the server folder is restored from a copy of the setup's and the page cache dropped, so every start loads
the same files. A start is timed from `java -jar` to Paper's "Done" line, after which the server is killed. The guest
has no internet at any point it is measured, and Paper needs none: its launcher finds Mojang's server jar already in
place. `tests/bench/run_paper.py` describes the case in full.

Bench case `guest-paper`; Apple M2 Max; natemu 41e78e44; November 21, 2026; median of 3 boots:

| Start in the boot | Time | The 3 boots | Paper's own "Done" |
| --- | ---: | ---: | ---: |
| first | 29.7 s | 29.4, 30.4, 29.7 s | 26.6 s |
| second | 29.0 s | 29.3, 28.9, 29.0 s | 26.5 s |

Experiment (an earlier version of the case: 8 GiB and Java's default heap, one boot, with internet, November 20, M2 Max): without
`--speculation-barriers`, so with PTI on, the two starts took 191.7 s and 170.8 s, against 32.6 s and 35.1 s with it.

## System calls: hackbench through the boot-time interpreter's branch

`hackbench` (above) makes its system calls by SYSCALL. `hackbench32` is the same program built as 32-bit code (i686),
which makes them through the kernel's 32-bit vDSO (SYSENTER); `hackbench32-int80` boots with `vdso32=0`, so every system
call is an INT 0x80. All at 1 vCPU. Measured before `boot-interp` was rebased onto main (November 22), on its pre-rebase
commits (tag `boot-interp-prerebase` and its ancestors), so the ids below are theirs; [the commit
map](restructure/commit-map-boot-interp-2026-11-22.txt) gives each one's id after the rebase. The commits: 78f60965, the
old base (`m2-32bit`, before the rebase), before the boot-time interpreter; be626aa8, after its plumbing (d7bc0885 moved
the resident's code into its own 2 MiB block, 0624b341 and be626aa8 added the interpreter's classification and entry);
e21b0209, after CPU start-up (INIT and SIPI) and the interpreter itself; 9fd9f346, the branch's final tip (the mode
transitions, the firmware boot mode, the real-code suites).

Bench cases `guest-hackbench`, `guest-hackbench32` and `guest-hackbench32-int80` (`TESTKIT_BENCH_AT=<commit>
tools/natemu-test run --tier bench -k "guest-hackbench and not profile and not qemu"`); Apple M2 Max; November 22,
2026. Seconds, the mean of 3 boots of two runs each (`wall_s`, `wall2_s`), the two commits of a table booted
alternately; the spread is the largest minus the smallest of the 3 boots' means, as a share of the mean.

The final tip, 9fd9f346, against 78f60965:

| Case | 78f60965 | spread | 9fd9f346 | spread | change |
| --- | ---: | ---: | ---: | ---: | ---: |
| `hackbench` | 13.34 | 2.8% | 13.24 | 1.7% | −0.8% |
| `hackbench32` | 13.27 | 0.8% | 12.67 | 1.3% | −4.5% |
| `hackbench32-int80` | 31.51 | 2.5% | 29.89 | 2.2% | −5.1% |

The plumbing, be626aa8, against 78f60965 (a later set):

| Case | 78f60965 | spread | be626aa8 | spread | change |
| --- | ---: | ---: | ---: | ---: | ---: |
| `hackbench` | 13.41 | 5.7% | 13.30 | 1.5% | −0.8% |
| `hackbench32` | 13.20 | 4.4% | 12.73 | 2.3% | −3.6% |
| `hackbench32-int80` | 31.83 | 4.2% | 30.54 | 0.2% | −4.1% |

An earlier set, e21b0209 against 78f60965:

| Case | 78f60965 | spread | e21b0209 | spread | change |
| --- | ---: | ---: | ---: | ---: | ---: |
| `hackbench` | 13.57 | 5.3% | 13.13 | 3.9% | −3.3% |
| `hackbench32` | 13.43 | 1.1% | 12.66 | 3.3% | −5.7% |
| `hackbench32-int80` | 31.41 | 3.8% | 30.63 | 0.9% | −2.5% |

`hackbench32`'s and `hackbench32-int80`'s gains at the final tip are larger than the noise: no boot of one commit comes
near the other's (`hackbench32` 13.21 to 13.31 s against 12.58 to 12.74 s; `hackbench32-int80` 31.00 to 31.78 s against
29.63 to 30.30 s). `hackbench`'s change is within the spread between boots (in the earlier set, only `hackbench32`'s
gain was beyond it). The same gains are there at be626aa8, so they arrive with the plumbing (d7bc0885 to be626aa8), and
the later slices add nothing beyond the noise: separate sets of the same commit differ by about 1% (78f60965's
`hackbench32-int80` took 31.51 s in one set and 31.83 s in the other). Their likely source is the code move (d7bc0885):
the resident's code is now mapped by one 2 MiB block entry instead of 4 KiB pages, and its instructions stayed the same
(an experiment in that commit's message: the same instructions in all 449 functions but one literal). The classification
and entry are meant to leave the fast system-call path's instructions as they were. This is likely, not shown: d7bc0885
was not measured alone.

## CPU vulnerability mitigations

The numbers above use `mitigations=off`. On a default kernel command line natemu reports its virtual
CPU as affected by Meltdown, so Linux turns on page table isolation (PTI). natemu has no fast path
for PTI's page-table switches yet. The cloud-image workload above, measured on an earlier natemu build
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

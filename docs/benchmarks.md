# natemu benchmarks

natemu compared with QEMU TCG, both running the same x86-64 Linux guest on
the same Mac.

| | natemu speed-up over QEMU |
| --- | --- |
| 11 benchmarks, 1 vCPU | **4.8×** (geometric mean; 1.4× to 41×) |
| 11 benchmarks, 4 vCPUs | **4.5×** (geometric mean; 1.6× to 38×) |
| Context-switch-heavy Debian workload (cloud image), 4 vCPUs | **1.7×** |

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

`tools/bench/run.py` boots one benchmark on natemu or QEMU and records its time. Its documentation
lists the options. The benchmark guest image is not published yet.

# M2 checkpoint A: 32-bit Linux programs run correctly

November 20, 2026. Checkpoint A of [the M2 plan](m2-32bit-design.md), on tasks D, 0, 1, 2, [3a](m2-task3a-findings.md)
and [3b](m2-task3b-findings.md): static and dynamic 32-bit glibc programs with thread-local data, signals, threads and
x87 run under the 64-bit Debian 13 kernel at 1, 2 and 4 vCPUs, and print what the x86 reference machine prints.
Correct, not yet fast (task 4).

**Summary.** No natemu defect was found: this checkpoint changes no emulator code. Every difference from the reference
machine is the accepted deviation "x87 registers hold doubles", now measured in real programs and listed below. One of
its forms is the plan's open item and needs the owner's decision: 64-bit integers moved through the x87.

## What was built

All of it is tests, through the framework.

- **Suite `correctness/i386`** (in the gate; two boots per vCPU count, about 2 minutes together). 46 tests in one image (`hello.static` alone is
  the smoke part):
  - ten C programs (`tools/correctness/recipes/i386/progs/`), each built twice, static (no PIE) and dynamic (PIE,
    through `ld.so`):

    | Program | What it runs |
    | --- | --- |
    | `hello` | start-up, arguments, the auxiliary vector, a system call through INT 0x80 and one through the kernel's helper (SYSENTER here), libc's formatting, sorting, allocation, 64-bit integer arithmetic |
    | `tls` | thread-local data through GS: data below the thread pointer (negative offsets), a register offset, errno, the stack protector's value, a variable reached through `___tls_get_addr` |
    | `threads` | eight threads, each with its own TLS segment, thread-local data and x87 rounding mode kept across context switches; a mutex, 32- and 64-bit atomics, a condition variable, a barrier, thread keys |
    | `signals` | `SA_SIGINFO` frames for #PF, #DE, #UD, INT3 and a single-step trap (saved EIP, error code, trap number, fault address; the handler steps over the instruction); the x87 and SSE state in the frame, changed by the handler and loaded by sigreturn; an alternate stack; nesting; blocked and pending signals; both of the kernel's own return trampolines; `siglongjmp`; SIGCHLD; a restarted and an interrupted system call; timer signals during x87 code |
    | `sys` | fork, vfork, exec of a 64-bit and of a 32-bit program, pipes, mmap/mremap/mprotect/brk, files with 64-bit offsets, directories, sockets, a process-shared semaphore (futex), select, the vDSO's clocks |
    | `x87` | casts to integers (the FNSTCW, FLDCW, FISTP, FLDCW sequence), 64-bit FILD and FISTP below 2^53, the four rounding modes, comparisons with NaN, the register stack eight deep, constants, integer operands, exception flags, libm |
    | `segs` | DS, ES, FS and GS loaded with a thread-area segment whose descriptor is then cleared: the kernel's reload faults inside the kernel and leaves the register null (the last test of the kernel's `ldt_gdt` selftest, with a flat segment) |
    | `cpuint`, `cpufp` | an integer and a floating-point workload with fixed answers, also built for x86-64 (`NAME.64`): the same program in both modes, timed by the runner |
    | `x87ext`, `atomic64` | what natemu's x87 does not do (expected failures, below) |

  - Debian's i386 BusyBox (`busybox-static`: its shell, pipes, awk, hashes, gzip) and CPython 3.13
    (`python3.13-minimal`: big integers, floats and their printing, threads, lock timeouts);
  - 17 modules of CPython's own regression tests (`pytests.txt`: int, long, float, math, cmath, struct, array, thread,
    threading, signal, time, fork1, posix, mmap, select, json, re). `test_math` and `test_cmath` run without the three
    cases that meet the x87 deviation and must pass; those cases run alone as two expected failures;
  - `mix`: three 32-bit programs, a 64-bit Python and a 64-bit `sha256sum` at once, three rounds, each output equal
    to the program's output alone.
- **Suite `correctness/torture32`** (campaign tier, 4 vCPUs, about 2 minutes): the existing torture suite's 1,721 C
  programs that compile for i686, at -O0 and -O2: 3,442 programs (the ieee ones with `-ffloat-store`, as gcc's own
  harness builds them on ia32).
- **The kernel's x86 selftests' 24 32-bit programs** in the existing `x86st` campaign (49 parts now).
- **The replay check** (`static/fexc/fexc-replay`) now records a boot that also runs `boot-i386`'s 32-bit program, so
  its stream mixes both modes. The new compiler answers all of it; the compiler of `main` answers the 64-bit requests
  (it refuses 32-bit code until M2 merges). Equal 64-bit replies also show that a 64-bit reply does not depend on
  32-bit requests answered before it by the same compiler process, which task 2 asked for.

### Decisions (the session's; all reversible)

1. **The programs are built by the framework's amd64 builder with gcc's 32-bit multilib** (`gcc-14-multilib` added to
   the sysroot, pinned), for i686 with x87 floating point: the flags Debian's i386 packages are built with (task 0).
   The guest's 32-bit libraries are Debian's i386 multiarch packages (`libc6:i386` and what the programs need, pinned),
   unpacked into the image's root. `tools/correctness/i386.sh` holds the three helpers; the `i386`, `torture32` and
   `x86st` recipes use it. The older campaign recipes and their images are untouched.
2. **Answers come from the x86 reference machine and are files in the repo.** The Mac's builder (Rosetta) cannot run
   32-bit code, so a build cannot record them. Whichever builder made the image, it is run on the reference machine: with
   `TESTKIT_SEED_ANSWERS=1 NATEMU_X86_HOST=user@host` the campaign harness unpacks the image on that machine and runs the
   guest's own runner in it (`chroot`), and the verdict writes each test's output to `tests/correctness/i386/` (whole
   text, so a difference shows as a diff) or its exit status and output hash to `tests/correctness/torture32.txt`.
   [The testing page](../testing.md#runner-families) has the command.
3. **`i386` is in the gate, not the campaign tier:** it is quick and is the only real-program check of 32-bit code.
4. **Output is made the same on any x86 machine:** nothing printed depends on scheduling, addresses, process ids or
   the processor's vendor (the reference machine is AMD, natemu presents Intel: the kernel's helper uses SYSCALL
   there and SYSENTER here, with the same results). The exact x87 section runs with the precision control set to
   double, where hardware computes what natemu computes; libm results and extended-precision sums are printed to ten
   digits.

## Results

| Suite | Result |
| --- | --- |
| `i386` at 1, 2 and 4 vCPUs | each: 40 of 46 tests print the reference machine's output; 6 are expected failures (the x87 deviation) |
| `torture32` at 4 vCPUs | 3,432 of 3,442 programs end as on the reference machine, where all pass; 10 are expected failures (the x87 deviation) |
| `x86st` at 4 vCPUs | of the 24 32-bit programs 20 pass, 1 reports unsupported and 3 are expected failures (recorded segment deviations, below); the 25 64-bit programs are as before |
| `fexc-replay` with the 32-bit program | 54,839 requests, 82 of them for 32-bit code, no 64-bit reply differs |

## The x87 deviation, measured

The plan accepts that x87 registers hold doubles. These are the places real programs showed it. They are now listed
on [the floating-point page](../../floating-point.md), which had no x87 entry.

| Where | On hardware | Under natemu |
| --- | --- | --- |
| `atomic64`: gcc's 64-bit atomic load and store on i686 (FILD m64, FISTP m64) | 8 bytes copied exactly | a value above 2^53 loses its low bits: `0x123456789abcdef1` becomes `0x123456789abcdf00`, `0x7fffffffffffffff` becomes `0x8000000000000000` |
| `x87ext` | `1 + LDBL_EPSILON > 1`; 2^62+1 survives `long double`; FLDPI's 64-bit significand | equal to 1; 2^62; pi rounded to 53 bits |
| CPython `test_math` (2 of 86 cases, run alone as `py-test_math-files`) | passes | `exp`, `sinh`, `cosh` near 709.78, `expm1` from 27 up and `gamma` near its limits are 17 to 324 units in the last place off; `expm1(1.79e308)` reports a domain error instead of an overflow |
| CPython `test_cmath` (1 of 33 cases, run alone as `py-test_cmath-values`) | passes | `acos(-1e299)` has an infinite imaginary part: glibc's i386 `hypot` squares in the x87's exponent range |
| torture `ieee/rbug` | passes | an unsigned 64-bit integer above 2^63 converted to double is rounded twice and is one unit off |
| torture `ieee/inf-2`, `inf-3`, `pr36332`, `pr39228` | pass | `LDBL_MAX` loads as infinity (already known from the 64-bit suite) |

The design's statement that Debian's 32-bit math library "loses many units in the last place for large arguments" was
marked inferred; it is now measured, and it is confined to large arguments: CPython's `test_float` and the other 84
`test_math` cases pass, and the `x87` program's libm results agree with hardware to ten digits.

### For the owner: 64-bit integers through the x87

The plan's open item was whether FILD/FISTP of 8-byte integers corrupts real programs.

- **The idiom corrupts, as predicted.** `atomic64` is gcc's own code for `__atomic_load_n` and `__atomic_store_n` of a
  `long long` on i686; four of its six values change.
- **CPython, which has 27 such pairs, showed no symptom:** its interpreter, 17 regression modules (threads, signals,
  time and lock timeouts among them) and the `mix` runs print the reference machine's output apart from the libm
  cases above. Its 64-bit atomics evidently held values below 2^53 in what ran.
- Nothing was built. The options are the design's: (a) leave it, a documented deviation; (b) the narrow fix, compiling
  a FILD m64 directly followed by a FISTP m64 in one block as an 8-byte move; (c) Box64's way, keeping the loaded
  integer beside the double and storing it back when the value is unchanged, which also covers a pair that is not
  adjacent. Until decided, `atomic64` is an expected failure that names this item.

## What found no defect

Recorded because the plan budgeted 10 to 25 agent hours for bugs here and none appeared:

- start-up of static and dynamic programs (INT 0x80 before TLS exists, then the kernel's SYSENTER helper), `ld.so`
  loading CPython's libraries and extension modules;
- per-thread TLS segments (`set_thread_area` through clone) and per-thread x87 rounding modes on 1, 2 and 4 vCPUs;
- rt and legacy signal frames, the x87 and SSE state in them, both return trampolines, a system call restarted
  through the helper, single-step from POPF;
- 32-bit and 64-bit processes sharing the vCPUs;
- 3,442 compiler torture programs (64-bit arithmetic helpers, varargs, bit-fields, structure passing, alloca);
- 20 of the kernel's 32-bit selftests: single-stepping through system calls, SYSRET's SS attributes, the NT flag,
  MOV SS traps, the vDSO's unwind information and signal restorer, initial register state, `ioperm`/`iopl`,
  `sigaltstack`, FCMOV/FCOMI/FISTTP.

Mistakes made on the way were all in the new tests and are fixed there: handler-written variables that were not
`volatile` (glibc declares `raise` and `sigprocmask` as calling nothing in the program, so gcc kept stale copies), an
x87 register compared with its stored double, `mremap` across a split mapping, and a payload copied through the root's
BusyBox links (the image's `cp` now replaces links: `i386.sh`, `image32`).

## Kernel selftests, 32-bit

The `x86st` campaign now has 49 parts. Of the 24 32-bit programs, 20 pass and `entry_from_vm86_32` reports
unsupported (a 64-bit kernel has no vm86 mode). Three fail, each at a deviation the plan already records, each passing natively on the reference machine:

| Program | Where it ends | Deviation |
| --- | --- | --- |
| `sigreturn_32` (and `sigreturn_64`) | the sigreturns into 64-bit and into 32-bit code pass; the third, into a 16-bit code segment, takes #GP(34H) | no 16-bit code segments until Part 2 |
| `test_syscall_vdso_32` | its far jump from 32-bit code to the 64-bit code segment takes #GP(30H) | no far transfers between the modes until task 5 |
| `ldt_gdt_32` | its last test loads DS with a read-only, expand-down thread-area segment (what an earlier test left in the descriptor) and takes #GP(6BH) at the next instruction; a SIGSEGV handler left from an earlier test then jumps back for ever, and the runner's 60 s timeout for this program ends it | 32-bit code runs only with flat DS and ES until task 6 |

Task 3a's two notes, rechecked: `sigreturn_64` does run its 32-bit part now, as predicted, but still fails, at the
16-bit segment; `sysret_ss_attrs_64` still needs task 5's far return. Both stay expected failures, with those reasons.

`ldt_gdt_32` was the one case that looked like a defect (a hang, and a kernel running on a user GS base in the
inspection report, which was only an interrupt arriving in the entry code). A ptrace tracer in the guest gave the
faulting instruction and DS = 6BH; the `segs` program is that test with a flat segment, and passes. The program
is not on the skip list because it does not stop the VM; the runner gives it 60 s instead of 300.

## The gate and the benchmark round

**Gate** (`tools/natemu-test run --gate`, commit 522e142b, on the M4): passed. 85,816 passed, none failed; 909 skipped
(variants this machine does not run), 18 expected failures (the six x87 tests of `i386` at three vCPU counts), 2
known-bug results that were on `main` before.

**64-bit speed, `main` against this branch.** The standing 11 benchmarks, warm run (the second of each boot), 1 and
4 vCPUs, three interleaved rounds (all of A, then all of B, per round), same image, kernel and settings, every run
alone on the machine; no run was contended. A is `main` at f0feda92 (`TESTKIT_BENCH_AT`, natemu and its compiler built
from that commit), B this branch. Seconds: median (lowest to highest of three).

| Benchmark | A, 1 vCPU | B, 1 vCPU | B/A | A, 4 vCPUs | B, 4 vCPUs | B/A |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `build` | 6.28 (6.13-6.89) | 6.15 (6.07-6.19) | 0.98 | 2.26 (2.26-2.27) | 2.29 (2.25-2.33) | 1.02 |
| `zstd` | 2.53 (2.51-2.54) | 2.49 (2.48-2.50) | 0.99 | 1.17 (1.13-1.20) | 1.17 (1.14-1.22) | 1.01 |
| `hackbench` | 10.73 (10.71-11.02) | 11.62 (10.91-11.76) | **1.08** | 3.05 (3.04-3.58) | 3.47 (3.41-3.52) | **1.14** |
| `locks` | 3.29 (3.28-3.36) | 3.39 (3.36-3.46) | 1.03 | 5.82 (5.81-6.34) | 5.64 (5.62-5.82) | 0.97 |
| `luajit` | 3.07 (3.04-3.13) | 3.10 (3.04-3.10) | 1.01 | 1.12 (1.11-1.14) | 1.08 (1.06-1.09) | 0.96 |
| `luajit-interp` | 3.37 (3.37-3.42) | 3.44 (3.32-3.45) | 1.02 | 0.95 (0.89-0.97) | 0.96 (0.87-0.96) | 1.01 |
| `mem` | 6.99 (6.82-7.85) | 7.02 (6.81-7.03) | 1.00 | 2.15 (2.13-2.23) | 2.15 (2.08-2.18) | 1.00 |
| `cray` | 3.01 (2.96-3.03) | 3.03 (3.02-3.05) | 1.01 | 0.99 (0.96-1.02) | 0.98 (0.97-1.02) | 1.00 |
| `7zip` | 3.35 (3.33-3.38) | 3.44 (3.30-3.45) | 1.03 | 1.41 (1.40-1.48) | 1.42 (1.38-1.73) | 1.01 |
| `sqlite` | 6.18 (6.05-6.43) | 6.16 (6.04-6.18) | 1.00 | 6.24 (6.18-6.51) | 6.24 (6.17-6.54) | 1.00 |
| `avx` | 8.18 (8.04-8.54) | 8.15 (8.01-8.15) | 1.00 | 3.12 (3.10-3.16) | 3.10 (3.10-3.11) | 1.00 |
| geometric mean | | | 1.013 | | | 1.008 |

- **M2 costs `hackbench` time, beyond its spread:** 8% at 1 vCPU and 14% at 4 in the warm run (5% and none in the
  cold run of the same boots, so the size is uncertain; the direction at 1 vCPU is not). `locks` at 1 vCPU is 3%
  slower, just outside its spread. Everything else is within spread. So `main`'s dispatcher work did help `hackbench`
  (10.7 s against the 11.9 s of November 16), and M2 gives a part of it back, which is why task 4's baseline on this
  branch looked unchanged.
- **Where:** `hackbench` and `locks` alone at 1 vCPU, three more interleaved rounds over the tasks' heads (warm
  medians): `main` 10.76 and 3.28 s; tasks D and 1 integrated (766e37af) 11.14 and 3.39; task 2 (43e6159c) 10.93 and
  3.34; task 3a (47aafca2) 11.18 and 3.44; task 3b (c659a43a) 11.11 and 3.38. The step is there at tasks D and 1
  (the diagnostics' per-exit work, the code-mode split, the new decoder for instructions the resident runs) and
  does not clearly grow afterwards, but single runs of one build vary by 4%, so three rounds cannot separate the
  later tasks' candidates (task 3a's mode tests on entry and state save, the null-segment bits of the code
  identity, the system-call fast path's extra compare). Nothing was tuned.

**32-bit against 64-bit, the same program** (`cpuint` and `cpufp` of the `i386` suite, the runner's wall seconds for
the whole process, 1 vCPU; the natively run numbers are the reference machine's, an AMD Ryzen 7 8845HS, and too
short to be more than an order of magnitude):

| Program | native, 64-bit | native, 32-bit | natemu, 64-bit | natemu, 32-bit |
| --- | ---: | ---: | ---: | ---: |
| `cpuint` (hashing, sorting, 64-bit arithmetic) | 0.20 | 0.22 | 0.25 | 0.35 |
| `cpufp` (double arithmetic, square roots, casts to integers) | 0.03 | 0.06 | 0.04 | 4.19 |

Integer 32-bit code runs at about 1.4 times its 64-bit build's time. x87 code is about a hundred times slower than
the SSE build: every cast traps twice and every x87 operation pays its wrapper, the plan's first two items for task 4.

## 64-bit regression

November 20, branch `m2-regress` off `m2-32bit` at 631b26a3. The cost was per system call, not per block: hackbench
runs about 32 million SYSCALL and 32 million SYSRET per boot through the resident's fast path, locks fewer; the
compile-heavy benchmarks and CPython's configure and make never showed it.

**Where.** All numbers here are warm medians of six interleaved rounds at 1 vCPU (each build in turn, per round), in
seconds, hackbench / locks. Within the integration of tasks D and 1, the step is all in the first branch, the task 1
plumbing (ef0c1206 and 7fdda4dd): `main` (f0feda92) 10.60 / 3.28; the plumbing 10.97 / 3.34; with the iced-x86 decoder
(226ca66d) 11.07 / 3.32; with the diagnostics (89f76ede) 11.01 / 3.34; integrated (766e37af) 10.99 / 3.41. The decoder
(compile time only) and the diagnostics (two stores per exit as before, a 100 ms watchdog wake-up) add nothing
measurable.

**Not the state layout.** The plumbing made the CPU state 16 bytes smaller (FS.base and GS.base moved into the
segments), so every field from `cr2` on moved. Restoring every offset with 16 bytes of padding changed nothing: 7fdda4dd
10.96 / 3.33 without, 10.96 / 3.34 with; 631b26a3 11.12 / 3.41 without, 11.13 / 3.40 with (`main` 10.66 / 3.28 in the same
rounds).

**What.** The resident's own time accounting (its `perf` feature, three rounds, whole boot) put the difference in the
system-call fast path: the system-instruction bucket 48.7 ns per instruction on `main`, 53.1 on the plumbing; the fast
path's own part (`sys_fast`) 54.9 against 59.5 ns; each block lookup 8.3 against 9.3 ns. The disassembly shows why. The
resident is built for size (`opt-level = "s"`), and the plumbing replaced `Cpu::control` with `controls`, an out-of-line
function returning the walk controls: it calls `Cpu::paging` into a temporary and copies the 40-byte result to its
caller through a call to `memcpy`, and `set_cpl` and `commit`, which discard the result, call it on every SYSCALL and
SYSRET. `main`'s `set_cpl` called `Cpu::control` and tested its tag, with no copy. Second, `Cpu::code_mode`'s `match`
became a chain of three branches in `Cpu::code_context` (the resident has no jump tables), which every lookup computes,
two per system call; before the plumbing that field was a constant.

**The fix.** `controls` is `#[inline(always)]` (no copy where the result is discarded, none in the callers that use it),
and `Cpu::code_mode` reads a four-entry table indexed by CS.L and CS.D instead of matching (the same values; its unit
test covers all four). Behaviour is identical. With the resident's accounting, the fix's system-call buckets are back to
`main`'s (system 51.0 ns against `main`'s 49.8 and 631b26a3's 55.8; `sys_fast` 56.7 against 55.6 and 60.8; the vCPU's
time in the guest 23.41 s against 23.51 and 24.26). Without it, six rounds of each candidate against `main` and
631b26a3 in the same rounds: `main` 10.66 / 3.29; 631b26a3 10.91 / 3.34; the inlining alone 11.07 / 3.36; both 10.72 /
3.29. The inlining alone measured 10.90 against 631b26a3's 11.05 in an earlier set; wall time between sets of rounds
moves by 1-2%, more than the inlining's share, so its effect rests on the accounting above. Both are kept.

**The fix against `main`** (6a498f33), the standing 11 benchmarks, warm runs, two interleaved rounds per vCPU count,
same image, kernel and settings, no run contended. Seconds: median (lowest to highest).

| Benchmark | `main`, 1 vCPU | fix, 1 vCPU | fix/`main` | `main`, 4 vCPUs | fix, 4 vCPUs | fix/`main` |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `build` | 5.98 (5.91-6.06) | 5.97 (5.92-6.02) | 1.00 | 2.29 (2.25-2.33) | 2.28 (2.25-2.30) | 0.99 |
| `zstd` | 2.49 (2.42-2.55) | 2.49 (2.45-2.54) | 1.00 | 1.13 (1.12-1.13) | 1.17 (1.15-1.19) | 1.04 |
| `hackbench` | 10.72 (10.60-10.84) | 10.51 (10.46-10.56) | 0.98 | 3.44 (3.44-3.45) | 3.37 (3.22-3.51) | 0.98 |
| `locks` | 3.31 (3.27-3.35) | 3.32 (3.30-3.34) | 1.00 | 5.90 (5.74-6.07) | 5.78 (5.74-5.83) | 0.98 |
| `luajit` | 3.03 (2.99-3.08) | 3.09 (3.09-3.10) | 1.02 | 1.11 (1.08-1.14) | 1.10 (1.07-1.14) | 1.00 |
| `luajit-interp` | 3.33 (3.32-3.34) | 3.33 (3.31-3.35) | 1.00 | 0.91 (0.89-0.93) | 0.91 (0.90-0.92) | 1.00 |
| `mem` | 6.81 (6.77-6.84) | 6.81 (6.79-6.83) | 1.00 | 2.13 (2.10-2.16) | 2.14 (2.14-2.15) | 1.01 |
| `cray` | 2.99 (2.98-2.99) | 2.99 (2.97-3.02) | 1.00 | 0.99 (0.97-1.01) | 1.01 (1.00-1.01) | 1.02 |
| `7zip` | 3.27 (3.23-3.30) | 3.25 (3.25-3.26) | 1.00 | 1.38 (1.36-1.41) | 1.39 (1.37-1.41) | 1.01 |
| `sqlite` | 6.04 (5.95-6.13) | 6.03 (6.02-6.04) | 1.00 | 6.14 (6.13-6.16) | 6.14 (6.14-6.15) | 1.00 |
| `avx` | 8.01 (7.92-8.09) | 8.01 (7.98-8.04) | 1.00 | 3.03 (2.99-3.06) | 3.05 (3.04-3.06) | 1.01 |
| geometric mean | | | 1.000 | | | 1.002 |

`zstd` at 4 vCPUs (4%) is the one ratio outside the spread of two rounds; it is no system-call workload, and checkpoint
A's three rounds had it equal (1.17 both). `luajit` at 1 vCPU, 2%, the same.

**CPython** (`io5-solo`, new in the bench family: the Debian cloud-image workload, then `./configure` and `make -j4` of
CPython 3.13 again alone, 4 vCPUs, `mitigations=off`; seconds of the solo steps): `main` 55.8 and 88.2 (one run);
631b26a3 55.9 and 88.5 (one run); the fix 55.3 and 88.0, 55.8 and 86.9 (two runs). CPython never showed the regression:
it makes far fewer system calls per second of guest time than hackbench. (The owner's recorded `main` figures, 58.4 s
and 95.1 / 93.8 s, came from the older pool script; this setup is about 5% faster on both builds, so `main` was run here.)

**Tests** on the fix: `tests/unit` (1,344 passed), `tests/static` stack and barriers, `tests/diag`, `tests/system -k
compat32`, `tests/smp -k system`, `tests/correctness -k i386` (126 passed, 18 expected failures, the x87 ones).

**What remains.** Nothing measurable: hackbench and locks are at `main` at both vCPU counts. A lead for later, not
measured: the fast path still computes `code_context` twice per system call, longer since task 3a (in 64-bit code one
more branch skips the null-segment bits); a context kept per CPL change would remove both.

## Not done

- **Speed** (task 4): nothing was measured or tuned.
- **C++ and larger userlands** (a 32-bit toolchain build) are checkpoint B's.
- **The stream of a whole 32-bit userland boot** for the replay check: the recorded boot runs one static program.
  The campaign image would give a stream of several GiB; not worth a gate case.

## Effort

Approximate, from the session that did the work (it has no clock): reading the plan, the findings and the framework
about 1.5 agent hours; the recipes, the seeding path and the verdicts about 2; the eight programs and their own
mistakes about 2; CPython's tests, torture32 and the replay change about 1.5; the kernel selftests and the `ldt_gdt_32`
diagnosis about 2; this page and the floating-point page about 1. About 10 in all, most of the wall time being image
builds (3 to 10 minutes each through Rosetta; 1.5 on the x86 machine once it could build). The plan's 10 to 25 hours
for bugs were not needed.

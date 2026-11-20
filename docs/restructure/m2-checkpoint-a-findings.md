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

## Not done

- **Speed** (task 4): nothing was measured or tuned.
- **A 32-bit benchmark.** The checkpoint's benchmark round and the full gate wait for the review (the owner's order).
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

# M2 task 4: speed of 32-bit code, one change at a time

November 20, 2026. Task 4 of [the M2 plan](m2-32bit-design.md), on [checkpoint A](m2-checkpoint-a-findings.md). Every
number here is from this Mac (an M4, 4 performance cores), 1 vCPU, through `tools/natemu-test run --tier bench`, each
benchmark alone on the machine. A benchmark boot runs its program twice; both times are given (run 1 / run 2, seconds).
One boot per side unless said; where a side was measured in several boots the range is given.

## The benchmarks

The benchmark image (`tools/bench/image`) now also holds 32-bit builds of three of its programs, built the way Debian
builds its i386 packages (gcc `-m32`: i686, x87 floating point, no SSE): hackbench, c-ray and SQLite's speedtest1.

| Case | What it runs |
| --- | --- |
| `guest-hackbench32` | hackbench, system calls through the kernel's helper page (SYSENTER) |
| `guest-hackbench32-int80` | the same with the kernel's 32-bit helper page off (`vdso32=0`), so every system call is INT 0x80, as in Go's 386 runtime |
| `guest-cray32` | c-ray at 1280x960, as the 64-bit `cray`: x87 arithmetic and casts |
| `guest-cray32-small` | the same program at 320x240 (1/16 of the pixels), for A/B: full size did not finish before the fixes |
| `guest-sqlite32` | speedtest1, as the 64-bit `sqlite` |
| `guest-startup32` | speedtest1 at `--size 5` run 40 times, one process each: mostly program start |
| `guest-*-profile` | a profile, not a timing: the shim built with its `perf` feature and natemu's timing log |

The 32-bit programs are not run when the image is built (the Mac's builder cannot run 32-bit code), so their outputs
have no pinned hash: the guest prints c-ray's picture hash and speedtest1's verification hash, and they are compared
between runs here. natemu also counts, in every build, the system instructions its shim ran by kind and the stack
check's firings; `run.py` records both.

## Profile before any change

Checkpoint A's code plus the counters. Times are of the whole profile boot (both runs).

| | cray32-small | startup32 | hackbench32 |
| --- | --- | --- | --- |
| Time, run 1 / run 2 (timing build) | 123.0 / 122.7 | 7.76 / 7.51 | 13.92 / 13.70 |
| In translated code | 245.6 s (99%) | 12.5 s (71%) | 10.3 s (34%) |
| In the shim, all of it | 1.3 s | 3.8 s | 19.1 s |
| of which system instructions | 0.22 s | 0.20 s | 2.7 s |
| of which entering and leaving blocks for them (link, lookup, enter) | 0.5 s | 0.6 s | 15.7 s |
| of which the string-compare helper (PCMPxSTRx, `svc`) | 0.001 s | 2.08 s (36.3 M calls) | 0 |
| In the host | 1.2 s | 1.3 s | 1.1 s |
| of which compiling | 0.74 s (58,693 compiles) | 0.87 s (67,916 compiles) | 0.71 s |
| FLDCW traps | 925,704 | 368,320 | 0 |
| Stack-check traps | 0 | 0 | 0 |
| SYSENTER / INT n instructions | 61 / 135 | 3,520 / 4,400 | 32.0 M / 12 |
| Code identity switches / lookup tables cleared | 1.06 M / 35 | | |

The full-size `cray32` was stopped by its 1,800 s cap inside run 1, with 6.69 million FLDCW traps counted (the 64-bit
`cray` takes 3.2 s). Sixteen times `cray32-small` gives about 1,970 s as an estimate of the full size.

What the profile says:

- **The x87 operation wrapper is the cost, not the FLDCW traps.** 99% of `cray32-small` is inside translated code.
  The 925,704 FLDCW traps cost 74 ms in their handler and well under a second with their block entries. The design
  page's guess that the cast's identity switch evicts lookup tables is ruled out: 1.06 million switches cleared a table
  35 times and recompiled nothing (two user identities fit the two user tables).
- Each x87 operation was wrapped in a save, clear and restore of Arm's floating-point status register, so that x87's
  exception flags stay apart from SSE's. Measured natively on this M4 (a loop around one `fmul`): writing the status
  register costs about 11 ns, reading it 2 ns, the `fmul` under 1 ns. The wrapper is about 25 ns per x87 operation,
  and about 20 ns more when x87 runs under its own control register, which c-ray does all the time: `-ffast-math` sets
  SSE's flush-to-zero bits, which x87 must not follow. With the wrapper removed (wrong flags; an experiment, not
  kept) `cray32-small` took 16.8 s: the wrapper is 86% of the run.
- `hackbench32`: 2.0 s slower than the 64-bit build (11.97 / 11.66), all of it the slow path of SYSENTER and SYSRETL.
- `startup32`: a helper served by the shim is 12% of the time. It is not x87: the binary has no such instruction. It
  is PCMPISTRI and its relatives (13 places in the binary), which glibc's i386 string functions pick when the CPU
  reports SSE4.2, and which natemu runs in the shim at about 57 ns a call. Compiles are 5% (about 850
  per program start, none reused, since 32-bit code is neither cached nor moved to another address).
- The stack check never fired because it was off in 32-bit code: its cost there was the ordinary inline check on
  every push.

So the order of work became: the x87 wrapper (not in the plan's list, but its lead "one status-register save per run
of x87 operations"), the SYSENTER fast path, the stack check, then the cast fix.

## Results, in the order the changes were made

Each row is one boot (two runs). A is the row above it in the same column.

| Build | hackbench32 | hackbench32-int80 | sqlite32 | startup32 | cray32-small |
| --- | --- | --- | --- | --- | --- |
| Checkpoint A (the baseline) | 13.92 / 13.70 | 28.62 / 28.53 | 8.50 / 8.19 | 7.76 / 7.51 | 122.99 / 122.70 |
| + stack check in 4-byte slots | 13.99 / 13.70 | 28.48 / 28.43 | 8.15 / 7.88 | 7.44 / 7.24 | 122.90 / 122.75 |
| + SYSENTER and SYSRETL on the fast path | 11.00 / 10.83 | 29.27 / 29.85 | | | |
| + x87 runs | | | 8.41 / 8.08 | 7.60 / 7.41 | 33.97 / 33.93 |
| + the cast fix (FLDCW translated) | | | 8.37 / 8.09 | 7.63 / 7.36 | 33.75 / 33.67 |

The 64-bit `hackbench` measured 11.97 / 11.66 beside the baseline and 11.17 / 10.98 beside the fast-path row: it varies
by several percent between boots, so a difference of that size between two hackbench rows is not a result.

### The stack check (change 1)

In 32-bit code the stack slot is 4 bytes, so "aligned" now means 4-byte aligned there: the check is `and x0, xS, #3`, it
covers 2- and 4-byte stores at a 4-byte aligned base, and an 8-byte store (an x87 double on the stack) keeps the
ordinary inline check. A 32-bit add, subtract or move of a multiple of 4 keeps a register aligned (FEX computes
`sub esp, n` in 32 bits). The same rule is in fexc, in the host's admission check and in the precision checker; the
host now asks for the option in 32-bit code too. 64-bit output is byte-identical (`static/fexc/fexc-replay`).

Measured: `sqlite32` and `startup32` 4% faster; `hackbench32` and `cray32-small` unchanged. The check fired 0 times
in every benchmark and in the i386 campaign's boots, so the per-mode trap counter the plan held back is not needed.

### SYSENTER in, SYSRETL out (change 3)

The existing fast path of SYSCALL and SYSRET now also takes SYSENTER (when IA32_SYSENTER_CS is not null) and SYSRETL
(always canonical: it returns to ECX). After a SYSRETL whose target has segments translated code cannot run, the full
path takes over and raises its fault as before. `hackbench32` went from 13.99 / 13.70 to 11.00 / 10.83 (21%), which
is the 64-bit build's time.

### x87 runs (new; the profile's first item)

Before, every x87 floating-point operation was wrapped: save Arm's status register, clear it, run the operation, read
the operation's flags into the x87 state, restore the register; and, when x87 needed another control register than
SSE's, two writes of that as well. Now, while every x87 exception is masked (as Linux programs run) on a Mac with AFP
(an M4), the switch is made once per *run* of x87 instructions:

- A run starts at the first x87 floating-point operation. SSE's flags so far move from the status register into the
  byte the shim already ORs into MXCSR at every entry (if there are any: otherwise nothing is written), and the x87
  control register is loaded.
- Inside a run each x87 operation only reads the status register (2 ns) into the instruction's pending flags, as
  before. Flags are sticky, and each instruction commits its flags to the x87 status word when it ends, so reading the
  run's earlier flags again commits nothing new.
- A run ends before the next thing that reads or writes the status or control register and is not x87's: an SSE or
  AVX floating-point operation, an exception check, STMXCSR. In 64-bit code it also ends at the end of its block, so
  SSE code is entered exactly as before and no SSE code changes. In 32-bit code, where x87 is the ordinary floating
  point, a run stays open across blocks: a byte in the guest state says whether one is open, and each block's first
  x87 operation (or first SSE operation) asks it. That is a load and a branch per block.
- The shim knows from the same byte that the status register holds x87 flags: when it takes over inside a run (a page
  fault on an x87 operand, an interrupt at a block boundary) it leaves the register out of MXCSR. Every entry from the
  shim starts with no run open, and every change of mode or of code identity goes through the shim.

Exactness, case by case: a fault on a memory operand inside a run (new cases `run-fault-load` and `run-fault-store` in
`faults/i6-faults`: MXCSR keeps only the SSE flag, the x87 word only the x87 flag, a narrowing store's own inexact flag
is dropped with the instruction); SSE between x87 instructions (the existing `rounding-*` cases); FNSTSW reads the x87
word, which is current after every instruction; FNSTENV, FXSAVE and the like are the shim's and end the block. An
instruction whose operand is a 32-bit float has a fix-up that reads its own operation's flags, so it clears the status
register first if the run left one of the two flags it reads (rare). A stack fault drops the faulting instruction's
own flags; inside a run they stay in the register, but the only flag such an instruction can raise is "invalid", which
the stack fault sets anyway. Kept on the old per-operation wrapper: Macs without AFP (M1, M2: their extra fix-ups read
each operation's flags) and programs that unmask an x87 exception. So the accurate mode, which exists only without
AFP, is untouched.

64-bit output changes only in blocks that hold an x87 floating-point operation. The replay check's recorded boot
holds none, so the check still passes byte for byte; a recorded stream with x87 arithmetic would differ by design.

Measured (`cray32-small`): 122.9 / 122.7 before; 38.3 / 37.9 with runs ending at every block; 34.0 / 33.9 with runs
kept open across blocks in 32-bit code. The picture's hash is unchanged. `sqlite32` and `startup32` are within 1% to 3%
of their rows above (8.41 / 8.08 and 7.60 / 7.41): they have little x87 arithmetic.

Two experiments (not kept) to see what is left: with no per-operation status read at all the run takes the same 34.1 s,
and with no status or control register write at all (wrong results) 33.9 s. So the register traffic is gone, and the
34 s are the translation of the x87 instructions themselves: about 48 Arm instructions per x87 instruction (the
register stack, its top and its tag bits are kept in memory and updated by every instruction). The very first
experiment's 16.8 s had x87 running under SSE's flush-to-zero setting, which is not x87's behaviour.

### The cast fix (change 2), and x87 rounding "per run"

The plan's question was where x87 arithmetic checks the x87 rounding mode once FLDCW no longer traps: in every
operation, or once per block. The runs above gave a third answer that costs less than either and needs no check:
**a run makes its control register from the x87 control word when it starts.**

- FLDCW is translated when the loaded word keeps the exception masks: it ends the run and stores the word in the guest
  state. The next x87 operation starts a run, which reads the rounding bits and sets Arm's rounding mode from them.
  Nothing remembers "who wrote last": after a preemption, a trap or a move to another CPU the shim enters with no run
  open, and the next run reads the control word again.
- A FLDCW that changes the exception masks (or would unmask a set flag) still goes to the shim, through a new stub
  that has the shim run that one instruction: pending x87 exceptions stay exact.
- FIST, FISTP and FRNDINT need no change: they round under the run's control register, which now always is the x87
  rounding mode. Their flags, the 64-bit store's inexact flag included, are computed as before.
- The x87 rounding mode and "SSE flushes to zero but x87 must not" are no longer part of the code identity on these
  Macs: one translation serves every rounding mode. On Macs without AFP and under unmasked x87 exceptions the old
  scheme stays whole (keyed identity, FLDCW in the shim).
- The shim reads the control word's rounding and precision bits back from the guest state whenever it takes over (the
  mask bits stay its own).

So the two numbers the plan asked for are both zero here: no per-operation check, no per-block check and no trap when
x87 and SSE rounding differ. The cost that remains is at the mode change itself: a FLDCW ends the run (one status and
one control register write, about 20 ns) and the next operation starts one (one control register write, about 10
ns), so a cast (two FLDCWs around a FISTP) costs about 60 ns where it cost two traps and two identity switches.
The "accuracy blast radius" question is moot: x87 arithmetic under an x87-only rounding mode is exact and costs
nothing extra.

Measured: FLDCW traps went from 925,704 to 0 in `cray32-small`, from 284,120 to 0 in `sqlite32` and from 368,320 to 0 in
`startup32`. The times moved by under 1% (33.97 to 33.75 s for `cray32-small`), as the profile said they would: the traps
were never where the time went. Outputs are unchanged.

A defect found on the way, fixed: when the compiler retried an extended block with fewer branches, it forgot that the
host has no AFP, so on such a Mac (M1, M2) a retried block was compiled as for an M4: no NaN or denormal fix-up for
its x87 operations and no software flush. It showed when such a block translated FLDCW on an emulated M2
(`faults/i6-faults-m2f`, `rekey-fldcw`).

### INT 0x80: measured, not built

With every system call through INT 0x80 (`hackbench32-int80`) the run takes 28.5 to 29.8 s against 10.8 to 11.0 through
SYSENTER: about 0.55 microseconds more per call (32 million calls). That is what a program that uses INT 0x80 for
everything pays (Go's 386 runtime). Ordinary glibc programs barely use it: `startup32` makes 55 INT 0x80 calls per
program start (static glibc's start-up) against 44 SYSENTER calls, about 30 microseconds of a start that takes 190 ms;
`cray32-small` makes 135 in all. A dynamically linked start adds the loader's calls (54 call sites, each used once or
a few times per library): a few hundred calls, still under a millisecond.

## 64-bit integers through the x87: two prototypes for the owner's choice

Compilers copy a 64-bit integer on i686 with FILD m64 then FISTP m64 (atomic loads and stores); natemu's x87 registers
are doubles, so a value above 2^53 lost its low bits ([checkpoint A](m2-checkpoint-a-findings.md)). The survey
(`m2-x87-int64-survey.md` on the integration branch) found the pair in 27 of 640 Debian packages, 81% of the places
adjacent and 19% with plain integer instructions between, none through x87 stack shuffles. Both ways to fix it are
built as prototypes, each on its own branch off this one (and its own branch of the FEX fork), so either can be dropped:

| | (b) the pair, found when compiling | (c) the integer kept beside the register |
| --- | --- | --- |
| Branch (natemu / FEX fork) | `m2-speed-int64-b` / `natemu-m2-int64-b` (2cd8c37) | `m2-speed-int64-c` / `natemu-m2-int64-c` (ddd4103) |
| How | The compiler remembers the last FILD m64 of the block it is translating. A FISTP m64 in the same block with no x87 or MMX instruction between stores that loaded integer; no conversion is emitted for it. | FILD m64 also writes the integer into the unused upper half of the register's 16-byte slot; every other write of an x87 register writes 0 there; FISTP m64 stores the integer when it is not 0, else converts as before. |
| Catches | The adjacent pair and the pair with any non-x87 instructions between: all the survey's places. Not a pair split across blocks (a branch or call between), nor one through FXCH or FLD ST(i) (the survey found none). | Those, and a pair split across blocks, a FISTP restarted after a page fault, and any order of other instructions that do not write the register. Not through FXCH or FLD ST(i) (they write the register: the integer is dropped, as in Box64), and not across FXSAVE/FXRSTOR (the shim's writes clear it) unless the 80-bit image is made to carry the exact integer, which is not built. |
| When the FISTP's store faults (first touch, copy-on-write) | The kernel restarts at the FISTP alone, in a new block that knows nothing of the FILD: the value is converted from the double, so it is rounded. A gap. It could be closed for adjacent pairs by probing the destination for writing before the FILD commits (the fault would then be reported at the FILD), not for separated ones. | Exact: the integer is still beside the register. |
| x87 stack overflow at the FILD | The loaded integer is stored although hardware would store the indefinite value (a broken program). | As hardware: the register holds the indefinite value and the conversion path stores it. |
| Cost in translated code | None outside the pair; the pair itself gets shorter (the FISTP is a plain 8-byte store). | Three Arm instructions for every write of an x87 register (a zero, an address, a store), plus one more store per FILD m64 and about 12 instructions per FISTP m64. On c-ray's main block (85 x87 instructions): 17,680 bytes of code against 16,496, 7% more. |
| Other code changed | The FEX fork only (about 25 lines in the x87 dispatcher). | The FEX fork (about 40 lines in the dispatcher and the x87 stack pass), admission and the site checker (the slot's upper half may be accessed), the shim (its register writes clear the upper half). |
| 64-bit code | The same change applies (the pair is an 8-byte copy there too, rare). | The extra store on every x87 register write applies there too. |

Neither changes the flags of the FILD itself: converting a 64-bit integer above 2^53 to a double sets the x87
precision flag, which hardware (where the load is exact) does not; that is part of the recorded "x87 registers are
doubles" deviation.

## Paused (November 20, about 14:50)

The work was paused on the owner's order while checkpoint A, which this branch sits on, investigates a 64-bit slowdown;
its result may change the baseline here. State at the pause:

**Built and committed on `m2-speed`:** the benchmark cases and counters; the stack check in 4-byte slots; SYSENTER and
SYSRETL on the fast path; x87 runs; the cast fix (FEX fork 9b270ad on 0e96493, local branch `natemu-m2-speed` of the
fork, not pushed); the compiler fix for hosts without AFP. The two 64-bit-integer prototypes are on their own branches
(table above).

**Tests that passed at the last code commit (a31c6bac):** `tools/natemu-test run tests/unit tests/static tests/faults
tests/programs -k 'rust or fexc or i6 or i5c'`: 4,846 passed (the Rust unit tests, the compiler's checks with the
replay check, the x87 reference corpora, the x87 and FP fault cases with the two new mid-run cases, on the M4 and on
the emulated Mac without AFP). `tools/natemu-test run tests/correctness/family.toml -k i386` passed at the commit
before it (de4dcfaf, which differs only in the no-AFP fix and tests): 99 passed, 18 expected failures, at 1, 2 and 4
vCPUs. The kernel's 32-bit selftests (`--tier campaign -k x86st`) passed at the fast-path commit and were not rerun
after x87 runs and the cast fix. `tools/natemu-test run` (the affected tier) and the gate were not run: they wait for
the review.

**Measurements complete** (the tables above): the baseline and each change's A/B for `hackbench32`,
`hackbench32-int80`, `sqlite32`, `startup32` and `cray32-small`; the profile; one full-size `cray32` at the head of
the branch: **526.9 / 523.2 s**, against "more than 1,800 s" before (run 1 alone did not finish in 1,800 s; the
estimate from the small scene was 1,970 s). So at least 3.4 times faster, about 3.7 by the estimate.

**Measurements cut off by the pause, to rerun:**

- the final rows at the head for `hackbench`, `hackbench32`, `hackbench32-int80`, `sqlite32`, `startup32`,
  `cray32-small` in one round (each was measured after its own change, but not all together at the head);
- 64-bit code before and after, which x87 runs and the cast fix may touch where 64-bit programs use the x87: only the
  baseline's `sqlite` was measured (6.28 / 6.06 s); `cray`, `hackbench` and the head's `sqlite` were not;
- both prototypes: nothing was measured in the VM (their benchmark rounds and the i386 campaign with `atomic64`
  expected to pass were queued and stopped). Only the static numbers in the table above exist, and neither prototype
  has run a guest program yet;
- the x86 selftests' campaign at the head.

## Not done, and leads

- **INT 0x80 fast path:** not built; the numbers are above. It matters for programs that make every call that way
  (Go's 386 runtime: about 0.55 microseconds more per call), not for glibc programs.
- **Macs without AFP (M1, M2)** keep the per-operation x87 wrapper and FLDCW in the shim: x87-heavy 32-bit code is as
  slow there as it was here before. Their NaN and denormal fix-ups read each operation's own flags, which a run does
  not separate. A lead, not started.
- **x87 instructions cost about 48 Arm instructions each** (the register stack, its top and its tag bits live in memory
  and are updated by every instruction). After the changes above this is all of `cray32`'s time: 525 s against 3.2 s
  for the 64-bit SSE build. FEX's x87 stack pass can keep the stack in registers across a block; natemu flushes it
  at every instruction so that a fault finds exact state. The next lead for x87-heavy code.
- **PCMPxSTRx through the shim** is 12% of `startup32` (glibc's i386 string functions with SSE4.2; 57 ns a call).
- **32-bit code is compiled again in every process** (about 850 compiles per start of a small static program, 5% of
  `startup32`): relocation and the persistent cache are still refused for it. For checkpoint B.
- **A FLDCW ends its run** with a status-register write although the run could stay open; about 20 ns per cast could
  go. Not measured as worth it.
- The stack check's per-mode trap counter: not needed (0 firings).

## Effort

Approximate, from the session that did the work (it has no clock): reading the plan, the findings and the code about
2 agent hours; benchmark cases, counters and profiles about 2; the stack check 1; the fast path 0.5; x87 runs, with
the experiments that explained the remaining time, about 4; the cast fix, with the no-AFP compiler defect, about 3;
the two prototypes about 2; this page 1. About 15 in all, against the plan's 16 to 30 for a narrower list. Most of
the wall time was the test queue.

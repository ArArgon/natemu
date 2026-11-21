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

## After the pause: the x87 register stack in host registers

The remaining cost was the translation of the x87 instructions themselves: about 48 Arm instructions each. An independent
look at our FEX fork found why. FEX's x87 pass keeps the register stack, its top and its tag bits in host registers
across a block; our fork had switched that off (every operation loaded the top, the tag byte and its operands from the
guest state and wrote everything back), so that a fault anywhere finds the guest state exact. And every operation
computed the answer to "is this register empty?" in full, although real programs never read an empty register.

What is built (FEX fork, the x87 pass; both 32- and 64-bit code; every Mac):

- **The stack lives in host registers across the instructions of a block.** What is pending is written to the guest
  state before every instruction that can fault, stop or call the shim: every instruction but an x87 instruction with
  register operands only. So at each such point the guest state is complete, as before. Blocks with unmasked x87
  exceptions and single-stepped instructions keep the old code.
- **What the block knows needs no test.** A register the block pushed is not empty; one it popped is. Only a register
  the block inherits is tested, once.
- **That test is two instructions, and a stack fault leaves the block.** It sits at the start of its instruction. When
  it fails (a read of an empty register, a push onto a full stack: a program bug) the shim runs that one instruction
  alone, in the old form, which computes x87's masked response exactly as before. Nothing is approximated; the rare
  case just takes a trap.
- **Exception flags stay in Arm's status register** (on Macs with AFP, all x87 exceptions masked) until something reads
  the x87 status word: FNSTSW, the end of the run, or the shim when it takes over. Before, every operation copied them
  (four instructions) and every instruction folded them (five more). The two cases where an instruction's own flags must
  be dropped keep their exact behaviour: a store that converts before it can fault (FIST, FST to a float) saves the
  flags so far in a byte first, and the shim takes that byte when the store faults; a stack fault in a single-stepped
  instruction puts the saved flags back.
- Smaller: a value the block computed is never a signalling NaN, so storing it as a double skips the quieting (nine
  instructions per FSTP); the condition bit C1 is written once while the block knows it is 0; consecutive MMX
  instructions set the x87 top and tags once, not after each.
- The caches use scratch registers, of which 64-bit code has half as many. Past a budget everything is written and the
  registers are given up, so a block never needs a spill area (which the shim does not allow).

On c-ray's hottest block (85 x87 instructions) the code went from 4,070 Arm instructions to 494: 48 per x87 instruction
to 5.8. A register-only multiply is one `fmul`.

| | cray32-small, run 1 / run 2 (s) |
| --- | --- |
| Checkpoint A (measured again, same day) | 128.2 / 124.4 |
| Before this change (x87 runs and the cast fix) | 33.75 / 33.67 |
| With it | **1.95 / 1.83** |

The picture's hash is the same in all three (2cf47b1e...). That is 17 times faster than before the change and 66 times
faster than checkpoint A.

How it is checked: a new test runs a block natively up to each of its fault points and compares the x87 state in the
guest-state area with what the instructions before it leave when each runs alone (`fexc-slice1-x87-commit`: 7 programs
in 32- and 64-bit code, from an empty and a part-filled stack, with underflow and overflow). The existing x87 suites pass unchanged, on the M4 and on the emulated
Mac without AFP: the x87 reference corpora against QEMU, the stack-fault and mid-run fault cases, the unmasked-exception
cases, admission and the precision checker on every compile. The i386 campaign passes at 1, 2 and 4 vCPUs (126 passed, 18
expected failures, as before).

What changes elsewhere:

- **64-bit code** gets the same pass, with the smaller register budget. Its x87 output changes in every block that has
  an x87 instruction; blocks without one are byte-identical (the replay check's recorded boot has none and passes).
- **Macs without AFP (M1, M2)** get the stack in registers and the two-instruction test, but keep copying the flags
  after each operation: their NaN and denormal fix-ups read each operation's own flags. So x87-heavy 32-bit code is
  faster there too, by less. Not measured (no such Mac here; the emulated one runs the tests, not benchmarks).
- One guess of the software denormal test for those Macs had to be made exact: it took "the other operand is a
  constant" to mean "this is FLD's own multiply by one", which a constant pushed by an earlier instruction (FLDZ) now
  also looks like. It now checks that the constant is 1.0.

### Where c-ray stands now, and the floor

Full-size `cray32` at this head: **26.8 / 27.1 s** (526.9 / 523.2 before the change; more than 1,800 at checkpoint A).

The same two binaries run natively on the x86 reference machine (a Ryzen 7 8845HS, one thread, three runs each, all
within 0.01 s):

| | 64-bit c-ray (SSE) | 32-bit c-ray (x87) | x87 / SSE |
| --- | --- | --- | --- |
| Real x86, 1280x960 | 3.62 s | 9.19 s | 2.5 |
| natemu on the M4, 1280x960 | 3.17 s | 26.8 s | 8.5 |
| natemu / real x86 | 0.88 | 2.9 | |
| Real x86, 320x240 (x87 build) | | 0.58 s (natemu 1.83 to 1.95) | |

So a factor of 2.5 between the two builds is the program's own (x87 code is slower on real hardware too), and the
x87 build's picture is bit for bit the real machine's at both sizes (hashes 564b520c... and 2cf47b1e...). What is ours
is the remaining factor of about 3.3: natemu runs the SSE build at 0.88 of the Ryzen's time and the x87 build at 2.9.

A profile of `cray32-small` (the whole boot, both runs; 5.7 s): 3.92 s in translated code, 1.17 s in the host (0.75 s
of it compiling: 65,200 compiles, because 32-bit code is compiled again at every program start and the benchmark runs
with the disk cache off), about 0.55 s in the shim (links 0.14, system instructions 0.11, the rest under 0.06 each).
No floating-point trap, no stack-check trap. So the runs themselves are about 95% translated code.

What the translated code is made of, counted on c-ray's hottest block (477 Arm instructions for 85 x87 instructions; a
static count of one block, not weighted over the run, which the tools at hand cannot do cheaply):

| | Instructions | Share |
| --- | --- | --- |
| The work itself: arithmetic, the program's loads and stores, FLD's quieting multiply, page-crossing checks of stores | 131 | 27% |
| Writing x87 registers to the guest state before instructions that can fault | 98 | 21% |
| Tag bits: the masks, the tag byte, the two-instruction tests | 129 | 27% |
| The stack top: its byte and the register numbers derived from it | 43 | 9% |
| Flags, condition bits, starting the run, the rest | 76 | 16% |

### Would predicting the stack top pay? The hit rate

Measured with a probe (not kept: a call into the shim at the first x87 instruction of each block piece, which
compares the stack top and the tag byte with what the same place saw the first time):

| Workload | Entries with the top as first seen | Different | Hit rate | Tags also as first seen |
| --- | --- | --- | --- | --- |
| cray32-small (two runs) | 273,059,516 | 551,156 | 99.80% | in every hit |
| sqlite32 | 1,685,617 | 0 | 100% | in every hit |
| startup32 | 2,535,587 | 0 | 100% | in every hit |
| CPython's regression modules (i386 campaign, 1 vCPU) | 89,720,685 | 501,311 | 99.44% | in every hit |

So the top is predictable. What a known top would still save, as an **estimate** from the block above: the register
numbers become constants (15 instructions), the address arithmetic of each write goes (49), and the tag masks become
constants (about 73 of 129): about 137 of 477 instructions, 29%, less an entry check of three or four instructions per
block. The tag tests themselves are already cheap (16 instructions here). It needs a second version of a block for the
misses (a trap per miss would cost more than it saves at c-ray's 0.2%). Built since: see below.

The larger lead, also an **estimate**: not writing the x87 state before instructions that can fault at all, and
letting the shim rebuild it at a fault from a record of where each register lives (as it already does for the
general registers). That would remove most of the 98 write instructions and of the tag and top traffic that goes with
them: about 200 of 477 instructions, 40%. It touches the site records, the shim, admission and the checkers; a guess
of 15 to 25 agent hours. Keeping the x87 registers in fixed host registers across blocks would add to either.

### Built: 32-bit code compiled for the stack top it starts with

The owner asked for it. When the shim asks for a translation of 32-bit code it now sends the x87 stack top and tag byte
the program has at that moment. The compiler builds the block for that pair: each x87 register is a field at a fixed
place, the top and the tag byte are constants. The first x87 instruction of each piece of the block tests the pair (five
instructions). When the program arrives with another pair, that test leaves to the shim, which throws the block away,
remembers its address, and goes on at that instruction; the address is translated without a guess from then on, which
is exactly the code from before. So a miss costs one trap once per block, and runs what ran before. 64-bit code and
single-stepped instructions get no guess.

| | c-ray's hottest block | cray32-small (s) | cray32 (s) | sqlite32 (s) | startup32 (s) |
| --- | --- | --- | --- | --- | --- |
| Before (stack in host registers) | 494 instructions | 1.95 / 1.83 | 26.8 / 27.1 | not measured | not measured |
| With the guess | 372 | 1.22 / 1.16 | 18.9 / 18.7 | 8.02 / 7.76 | 7.16 / 6.94 |

Pictures and hashes unchanged. Against real x86's 9.19 s the x87 build is now at 2.0 times (it was 2.9). The estimate
had been 29% fewer instructions on the hot block; measured 25%, and 36% less time on c-ray.

Checked by the same test as before, now also on guessed blocks (the guess's test is one more point where the state must
be complete), the precision checker on x87 programs compiled for four different pairs, and the i386 campaign (126
passed, 18 expected failures), where real programs take the miss path.

What is left in the hot block after this, counted the same way (361 instructions): the work itself about 131; writing
x87 registers to the guest state before instructions that can fault 49; the constants and stores that keep the top and
tag byte current there about 104; condition bits, flags and the rest about 77. So the second lead (not writing the state
before instructions that can fault, the shim rebuilding it at a fault) would now remove about 150 of 361 instructions,
around 40%, as an **estimate**.

### Built: no x87 state written before instructions that can fault

Also on the owner's order. Until here the pending x87 registers, the top and the tag byte were written to the guest
state before every instruction with a memory operand, so that a fault there finds them. Now, in a block compiled for a
known stack top, they are not. Instead the compiler leaves a small record per such instruction: which host register
holds which x87 register, and the top and tag byte as plain numbers. When an instruction faults (or one of its checks
fails), the shim applies that record to the guest state before it does anything else. Applying it twice changes
nothing, so it does not matter whether the block is resumed or left. The state is still written by the code itself
before the last instruction of each block piece, before an instruction that reads the x87 state directly (FXAM, FNSTSW,
MMX, the shim's helpers), and at the exit.

| | c-ray's hottest block | cray32-small (s) | cray32 (s) |
| --- | --- | --- | --- |
| With the guess | 372 instructions | 1.22 / 1.16 | 18.9 / 18.7 |
| And no writes before faultable instructions | 238 | 0.62 / 0.57 | 9.01 / 8.93 |
| Real x86 (Ryzen 7 8845HS), the same binary | | 0.58 | 9.19 |

Pictures unchanged. The x87 build now runs as fast under natemu on the M4 as on the real x86 machine; from checkpoint A's
more than 1,800 s that is a factor of 200. (The estimate was 40% fewer instructions; measured 36%, and half the time.)

How it is checked, since the guest state is no longer complete in memory at a fault:

- The native test (`fexc-slice1-x87-commit`) runs each block up to every fault point, applies the instruction's record
  exactly as the shim does, and compares with what the instructions before it leave when each runs alone; 32- and 64-bit
  code, with and without the guess, also the code for Macs without AFP. It also shows that without the record the memory
  state is wrong at more than half of those points, so the comparison does depend on the record.
- The precision checker has a new rule: from the start of an instruction to its last fault point, no word writes a host
  register that the instruction's record names (and, as before, none writes x87 state in memory there).
- Admission checks each record's shape. A wrong record could only give the guest a wrong x87 state of its own.
- A new 32-bit VM case against QEMU (`c32-x87-depth` in `system/compat32`) calls one x87 function at two stack depths and
  joins paths with different tops: the guessed translations are thrown away and made again without a guess.
- The i386 campaign (126 passed, 18 expected failures) and the narrow suites (4,881 passed) at this head.

What is left in the hot block (228 instructions for 85 x87 instructions): arithmetic 54; the program's loads and stores
29; the page-crossing checks of its 8-byte stores 30; FLD's quieting multiply 18; flags and the run 17; condition bits 13;
state writes 12; the rest 55.

### More 32-bit floating point: c-ray strict, c-ray with SSE2, SciMark

Added at the owner's request to the benchmark image: c-ray as x87 code without `-ffast-math` (`cray32-strict`), c-ray as
32-bit code with SSE2 floating point (`cray32-sse2`, the way i386 programs for newer baselines than Debian's are built),
each also at 320x240, and SciMark 2.0 from NIST (ANSI C: FFT, SOR, Monte Carlo, sparse matrix multiply, LU), built for
64 bits, for 32-bit x87 and for 32-bit SSE2. SciMark was chosen as the established suite: one small zip from NIST,
plain C, no input files, and each kernel runs for at least two seconds and reports a rate (Mflops, higher is better),
so its result does not depend on how long a run is. First numbers, 1 vCPU, one boot (two runs) under natemu and two or
three native runs on the x86 reference machine (Ryzen 7 8845HS), before the final round:

| c-ray (s) | real x86 | natemu on the M4 | natemu / x86 |
| --- | --- | --- | --- |
| 64-bit, strict | 3.81 | (final round) | |
| 32-bit x87, fast-math (`cray32`) | 9.19 | 9.01 / 8.93 | 0.98 |
| 32-bit x87, strict (`cray32-strict`) | 9.63 to 9.66 | 8.35 / 8.33 | 0.86 |
| 32-bit SSE2 (`cray32-sse2`) | 3.89 to 3.93 | 3.96 / 3.90 | 1.0 |
| x87 strict at 320x240 | 0.61 | 0.60 / 0.55 | |
| SSE2 at 320x240 | 0.25 | 0.30 / 0.25 | |

The strict x87 picture under natemu is bit for bit the real machine's at both sizes (md5 460bdb17... and f356a121...), and
it is the same picture as the 64-bit SSE build's. So for this program the x87's extra precision, which natemu does not
keep (its x87 registers are doubles), changes nothing in the output: c-ray stores every intermediate result in a
double. That is a result about c-ray, not a proof for other programs.

| SciMark 2.0 (Mflops) | composite | FFT | SOR | Monte Carlo | sparse | LU |
| --- | --- | --- | --- | --- | --- | --- |
| 64-bit, real x86 | 5042 | 4213 | 2877 | 983 | 5499 | 11639 |
| 64-bit, natemu | 2751 / 2625 | 2043 / 1692 | 4313 | 962 / 955 | 4142 / 3890 | 2295 / 2275 |
| 32-bit x87, real x86 | 2579 | 1869 | 1226 | 852 | 4378 | 4571 |
| 32-bit x87, natemu | 1310 / 1345 | 1239 / 1439 | 1823 / 1813 | 296 / 295 | 1197 / 1181 | 1995 / 1994 |
| 32-bit SSE2, real x86 | 4719 | 4098 | 2887 | 796 | 5541 | 10276 |
| 32-bit SSE2, natemu | 2427 / 2296 | 2614 / 1905 | 4042 / 4040 | 59 / 59 | 2558 / 2573 | 2862 / 2904 |

What stands out (profiled in the next section; Monte Carlo in the SSE2 build is fixed there):

- **Monte Carlo in the 32-bit SSE2 build is 13 times slower than on real x86** (59 against 796). The likely cause, not
  yet verified by a profile: on i386 a function returns a double on the x87 stack whatever the floating-point
  instruction set, so SSE2 code does a FLD and a FSTP around every call. FLD of a double is the one x87 instruction
  here that needs the x87 run (its quieting multiply raises x87 flags), so each call opens a run and the next SSE
  instruction closes it: four writes of Arm's status and control registers per call. A FLD that tests its operand with
  integer instructions where no other x87 arithmetic is near would avoid the run altogether.
- **LU is four to five times slower than real x86 in both SSE builds** (2,295 against 11,639; 2,862 against 10,276), while
  SOR is faster on the M4 than on the Ryzen. LU is not x87 code; this is a 64-bit finding too.
- **Monte Carlo and the sparse multiply in the x87 build** are three to four times slower than real x86, where c-ray's
  x87 code is now at parity.

### SciMark profiled: Monte Carlo fixed, the other gaps explained

One profile group (`guest-scimark-profile`, `guest-scimark32-profile`, `guest-scimark32-sse2-profile`; the timing
build, sliced by kernel), the hot loops' translations read next to their x86 code, and SciMark built natively for the
M4 (arm64, clang -O3) as the ceiling of this hardware:

| SciMark 2.0 (Mflops) | composite | FFT | SOR | Monte Carlo | sparse | LU |
| --- | --- | --- | --- | --- | --- | --- |
| native arm64 on the M4 | about 7090 | 5555 | 5370 | 1930 | 6500 | 16070 |

**Monte Carlo, 32-bit SSE2: the cause confirmed and fixed (59 to about 260 Mflops in the profile build; the final
round has the number).** The disassembly shows what was guessed: the random-number function ends in
`movsd [esp],xmm0; fld qword [esp]; ret` and its caller does `fstp qword [esp]; movsd xmm0,[esp]`. The FLD's quieting
multiply by 1.0 opened an x87 run (status and control registers switched) that the next SSE instruction closed, at
every call. Now a FLD of a double in a unit with no computing x87 instruction loads without the multiply and without a
run: the signalling NaN is quieted and IE set with integer instructions, a denormal sets DE. Exact, and generic: every
i386 function that returns a double computed with SSE does this. The same form for a returned `float` (FLD m32, which
converts) is not built. What is left of the gap to real x86 (796) is the next item.

**Monte Carlo, both 32-bit builds (about 260 to 290 against 800 to 850): calls and returns.** The kernel is one small
function called twice per sample; each CALL and RET leaves the translated code through the dispatcher's lookup, and in
the x87 build each block starts with the five-instruction test of its stack-top guess and ends with the write of the
top, tags and slots. 64-bit Monte Carlo (962 against 983 on x86) does not have this: its compiler inlined the call. A
fix is return prediction for 32-bit code; the earlier experiment with pairing returns lost time on 64-bit code
(see the translated-code experiments), so this is a design question, not an afternoon. Estimate: two times on this
kernel if returns chain like direct jumps; not measured.

**Sparse multiply, x87 build (1,197 against 4,378).** All of its time is in translated code. The inner loop's seven x86
instructions (`mov; fld qword; fmul qword; add; faddp; cmp; jne`) run about 42 Arm instructions per iteration. The
extras: the loop is a block that jumps to itself, so every iteration tests the guess (5), writes the top, tags and one
slot twice (10, before the FADDP's flush and at the block's end) and reloads; the FLD keeps its multiply by 1.0 (4,
the unit computes); the stack check of FADDP is done with two constants at run time (4) where the compiler knows the
answer. A fix is to let a block that jumps to itself with the top it started with skip the test and keep the stack in
registers around the loop (a loop-carried x87 map): estimate 42 to about 15 instructions, two to three times on this
kernel and on any tight x87 loop (c-ray's blocks are long, which is why it did not show there). Estimate of work: a
day, with the same exactness questions as the map (state complete at every fault site). Not started.

**LU, both SSE builds (2,300 and 2,900 against 11,600 and 10,300): unaligned 16-byte stores that cross a page.** Only
50 to 57% of the LU phase is in translated code; the rest is the trap path (abort 14 to 17%, shadow 14 to 17%, link,
enter, patch). The inner loop is a `movups` load, multiply, subtract, `movups` store over rows that malloc put at any
8-byte address, so in a row that straddles a 4K boundary one store per pass crosses it, and a crossing store takes the
split-store stub into the resident, about 650 ns each, against about 30 ns for the rest of the row. The x87 build's LU
(8-byte stores at 8-byte alignment never cross) is 100% in translated code. So yes, LU pays for page crossing: twice,
by the four-instruction crossing check in front of each 16-byte access (4 of the loop's about 14 Arm instructions for
7 x86), and by the trap when the check fires. A fix is a crossing store done in translated code out of line (probe
both pages for write, then store), with the exactness rule that nothing is written when the second page faults;
estimate: the phase back to nearly all translated code, LU about 1.8 to 2 times faster in both SSE builds and in
64-bit code generally where `movups`/`movdqu` stores cross pages (memcpy-like loops). The rest of the gap to x86 would
remain. Estimate of work: half a day to a day. Not started.

**FFT, 64-bit (2,043 against 4,213; native arm64 5,555).** 100% in translated code. The inner loop's 27 x86
instructions become about 46 Arm instructions (1.7 per instruction). The extras: page-crossing checks in front of the
16-byte accesses (about 22% of the loop), a `ldr d2; mov v16,v2` pair where one load into the target register would
do (scalar loads that must zero the upper half), and the AF flag's `eor` after integer arithmetic whose flags nothing
reads. FFT's result also varies from run to run under natemu (1,692 to 3,182 across boots); not explained (the
transform's arrays' alignment against pages is the first suspect, given the crossing checks). No fix made: none of it
is the Monte Carlo fix.

### Built: page-crossing stores without the trap

Approved by the owner after the LU finding; the design is the one an independent design review chose (two page probes
in the store's out-of-line stub), not the "second page first, then roll back" idea. Every store wider than a byte
already tests whether it crosses a 4K page and, if so, branched to a stub that trapped into the shim, which made the
store itself (about 650 ns). The stub now writes one byte of each page with an atomic add of zero and branches back to
the store:

    staddb wzr, [A]         the store's first byte
    add    x0, A, #width-1
    staddb wzr, [x0]        its last byte
    b      <the store>

An add of zero always writes, so a page that this vCPU's shadow tables do not hold writable (not present, read-only,
not yet dirty, a page of translated code, uncached or device memory) faults at the probe; the shim then does exactly
what it did at the trap (its checks in x86's order, both walks, the store through its own mapping) and resumes after
the store. If both probes pass, the store cannot fault, because the shadow tables are this vCPU's own and change only
when it enters the shim, which it does not between the probes and the store (an interrupt there only arms a stop). That
sentence is what the design rests on; it is written at the code. Details that follow from it or guard it:

- Any return from the shim into the stub goes to the stub's first word, never to the second probe.
- One-instruction blocks keep the trap, so single-stepping and uncached-memory ordering are untouched.
- While a breakpoint is armed (DR7), the stub takes the trap as before: it first reads a byte the shim keeps for it
  (found by the code review: the sentence above is about faults, and a store that crossed a page and then met a
  watchpoint might have written its first part before the instruction is run again alone).
- The stub also takes the trap for a store that wraps at the top of the address space (64-bit code; the review again)
  and, in 32-bit code, for an address above 4 GiB (FEX computes addresses in 64 bits; a store running across 4 GiB),
  since the page tables may map something up there for the same task's 64-bit code and the probe would pass. The whole
  stub is eleven Arm instructions, all behind the block.
- With x86 alignment checking on, the probes (bytes) pass and the misaligned store itself takes Arm's alignment fault,
  which was already delivered as #AC.
- A probe that hits a hardware watchpoint goes to the same handler, which tests the debug registers as before.
- The three checkers (the host's object validation and code-shape check, `verify_precision.py`, `check_sites.py`)
  accept exactly this stub, and its branch back as the one backward branch besides the REP STOS loop: only to a word
  at or before the store, from a stub that nothing after that word can reach.

| SciMark LU (Mflops), one boot each, two runs | before | after | real x86 |
| --- | --- | --- | --- |
| 64-bit | 2,106 / 2,137 | 3,608 / 3,621 | 11,639 |
| 32-bit SSE2 | 2,645 / 2,638 | 4,215 / 4,213 | 10,276 |
| 32-bit x87 (8-byte stores, never crossed) | 1,938 / 1,938 | 1,933 / 1,933 | 4,571 |

1.6 to 1.7 times on LU in both SSE builds, as estimated (1.8 to 2 was the estimate; the profile to confirm that the
phase is now all translated code was not rerun). The other kernels did not move outside their run-to-run spread. The
rest of LU's gap to real x86 is translated-code quality (the four-instruction check in front of each 16-byte access
among them).

`fexc-replay` shows that a compiler change left the code of a recorded 64-bit boot alone, and this change alters every
block with a store wider than a byte. The check now bounds the intended change instead of failing on it
(`replay.py`, `only_probes`): a reply may differ only in that split stubs are the eleven probing words where the old
compiler has the trap word, with what follows from their size (later stubs and FEX's tail at other offsets, the
branches to moved stubs with other displacements, the sizes in the header and the tail). Everything else is compared
byte for byte: every word of every block before its first stub, the other stubs, every site record, the PC table, the
relocations, the report, the extent and the arguments read. Result on the recorded boot: 54,940 requests, 10,728
replies differ, all of them only in that way, 0 otherwise. The check's by-product, that a 64-bit reply does not depend
on the 32-bit requests the same compiler process answered before it, is now its own case (`fexc-replay-order`: the new
compiler against itself, one process answering every request and one only the 64-bit ones): 0 differences.

Tests: no new family. The existing cases of crossing stores now run through the probes in ordinary blocks and through
the trap in their one-instruction variants, and all pass: the fault families (page not present, read-only, supervisor,
reserved bits on either side; 2 to 32 bytes; PUSH and a CALL's push; read-modify-write forms; the canonical-gap
straddles; first-page-first order), uncached memory on either side (`m2`), a device page on either side (`m3`), three
vCPUs with crossing vector stores and split locks (`vector-split-smp`), #AC (`vector-ac`), the 32-bit wrap cases
(`compat32`), and a crossing FILD/FISTP pair (`c32-x87-int64-cross`, new, which also found that the kept integer was
not kept when the fault was taken at a stub: fixed). Added after the code review: crossing stores with a write
breakpoint on bytes of the second page (`s4-d-forms`: ADD to 4 and 8 bytes and a 16-byte MOVUPS, the breakpoint on the
last byte and on interior bytes only; the memory compared with QEMU), and the checker's refusals as a unit test (a
return to a word after the store or before the code, a probe on another register, a stub without its tests or its
trap word, a probe outside a stub, a second way into a stub). Not written: a store that wraps at 2^64 or crosses next
to the guest-state page's alias, a return into a literal slot (the rule is one line; no block here has a literal to
aim at), and a multi-vCPU run with mprotect or munmap racing a crossing read-modify-write.

For whoever runs the gate: the hold cases of `k1` and the `pic-*` cases use a second host built with test hooks in
`target/hooks`, and reuse it when it exists (`NATEMU_PREBUILT=1`). After a change to the host's admission that binary
is stale and refuses new code ("object refused: check stub ..."); delete `target/hooks/release/natemu-host` and the
resident beside it and they are built again. Not fixed here (a change to the test framework).

### INT 0x80 again: where its time goes

Profiles of `hackbench32` both ways (timing build, the whole boot with two runs; 32 million system calls):

| Per system call, in the shim (ns) | SYSENTER | INT 0x80 |
| --- | --- | --- |
| The system instructions themselves (in and out) | 68 | 262 (INT and IRET) |
| Delivering the interrupt (the IDT entry, the kernel stack, five pushes) | | 227 |
| A read of the local APIC by the kernel, once per call | | 267 |
| Leaving and entering blocks (link, lookup, enter, a dispatcher miss) | 290 | about 490 |
| Translated code | 290 | 440 |
| Run time | 10.3 to 10.4 s | 27.1 to 27.4 s |

Two things were not known before. The kernel reads the local APIC on every INT 0x80 system call: since Linux 6.7 its
handler checks the in-service register to tell a real `int 0x80` from an external interrupt on that vector, and with
the APIC in memory-mapped mode that read is a device access the shim serves (a quarter of a microsecond). And every
call takes one dispatcher miss that the SYSENTER path does not take (not yet explained). A one-pass path for INT and
IRET would save the double pass of each, perhaps 0.2 to 0.3 of the 1.25 microseconds a call spends in the shim (an
estimate); the delivery, the IRET and the APIC read stay. Unlike SYSCALL and SYSENTER, INT writes guest memory (the
kernel stack) and can fault while it does, which the existing fast path is built never to do, so it is not a small
change. The profile case `guest-hackbench32-int80-profile` is added.

Tried after the x87 work, and parked on the branch `m2-speed-int80` until the owner decides (it is not on the head):
INT n and IRET take the one-pass path when a block exits straight into them. That turned out to need no new machinery: everything the path does after the instruction is what the shim does
after any system instruction, so a fault inside the INT or IRET is simply delivered by the instruction itself and the
path goes on from wherever that leaves the processor. Measured: `hackbench32-int80` 27.90 / 27.64 s, against 28.4 to
29.8 in earlier boots; in the profile the exit handling per call fell from 97 to 64 ns and one copy of the state record
per call went away. About 3%, which is inside the difference between two boots, so it is not a result one can see in the
benchmark. The larger costs are untouched: the delivery itself, the kernel's APIC read, and the kernel's IRET, which
this kernel reaches through an indirect jump, so it arrives by a dispatcher miss and not by a block exit. The same
change would serve every IRET that a block exits into directly, in 64-bit kernels too. With it the system and SMP
families pass (35,323 cases).

Built after the owner's decision ("cheaper APIC, more efficient IRET"; merged, with the one-pass path above):

- **The APIC read in place.** Linux does not use x2APIC here although natemu offers it: without interrupt remapping it
  enables x2APIC only under a hypervisor it recognizes, and natemu presents a bare machine. So the read stays a
  memory-mapped one, and what could go was what followed it: the shim banked the whole state, ended the block and
  dispatched at the next instruction. Now a 4-byte load of a local APIC register puts the value in the Arm load's
  register and the block goes on, as after a load from RAM (a register read has no effect; with a data breakpoint
  armed, or any other access form, the old path runs). Generic: every kernel that reads its local APIC through memory
  (the timer's current count, the in-service register) gets it.
- **IRET reached by an indirect jump.** Linux's return path jumps to its IRET through a register, so it arrives as a
  dispatcher miss, not as a block's exit. The miss path, which already has made every check the one-pass path needs,
  now runs a kept IRET itself instead of declining to the full path.

| `hackbench32-int80` (s), one boot each, same hour | run 1 | run 2 |
| --- | --- | --- |
| Before (the head without the three changes) | 28.75 | 28.47 |
| After | 24.91 | 24.93 |

13% faster. 64-bit `hackbench`, four variants in three alternating rounds (the head; with the one-pass path; with the
APIC read only; with all): 10.99, 10.95, 10.89 and 10.98 s on average, the rounds themselves differing by 0.3 to 0.8 s,
so no change that can be told from a boot's noise. (A first comparison of two pairs had suggested 1 to 2% slower; the
rounds did not confirm it.) The system, SMP and fault families pass with it (77,181 cases). Added after its review: the
APIC register program also reads with ADD and CMP (forms the old path refused; the load in place serves any 4-byte
load) and reads a register that does not exist (0, and the error register's illegal-register bit through the old
path); a copy of the priority program whose handlers return through a computed jump to their IRETQ, with interrupts
pending (`apic-prio-jmp`). Not written: a faulting IRETQ reached by a computed jump (tried in the segment cases; the
position-independent jump disturbed what their model compares), an IRET in an NMI handler with a second NMI pending, the
APIC page read at CPL 3, with a data breakpoint armed, and in x2APIC or disabled mode through this path. Still there per call: the
delivery itself (the IDT entry, the stack switch, five pushes) and the APIC read's trap and page walk.

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

Measured (one boot each, two runs per boot, 1 vCPU, seconds), both rebased onto the head with the x87 stack in host
registers, the guess and the records:

| | cray32 | cray32-small | sqlite32 | startup32 | i386 campaign |
| --- | --- | --- | --- | --- | --- |
| No prototype | 9.01 / 8.93 | 0.62 / 0.57 | 8.02 / 7.76 | 7.16 / 6.94 | 126 passed, 18 expected failures |
| (b) | 9.05 / 8.98 | 0.62 / 0.57 | 8.04 / 7.78 | 7.21 / 6.99 | 132 passed, 12 expected failures |
| (c) | 11.21 / 11.11 | 0.76 / 0.71 | 8.03 / 7.76 | 7.24 / 6.99 | 132 passed, 12 expected failures |

Under both, the `atomic64` test passes, static and dynamic, at 1, 2 and 4 vCPUs (the six expected failures that went away).
(b) costs nothing that can be measured. (c) costs 24% on c-ray: every write of an x87 register to the guest state now also
writes the integer (or 0) beside it, and a register that still has its integer beside it is written before the next
instruction that can fault, because the record for the shim names only the double's host register. The second part could
be removed by a record that names the integer's register too; the first part is what (c) is. The "7% more code" in the
table above was measured before the stack moved into host registers and no longer describes (c).

Neither changes the flags of the FILD itself: converting a 64-bit integer above 2^53 to a double sets the x87
precision flag, which hardware (where the load is exact) does not; that is part of the recorded "x87 registers are
doubles" deviation.

### (b) as merged: the faulting store, and what its review found

The owner chose (b). When the pair's store faults (the first touch of a page, copy on write), the instruction runs again
alone and would convert the double; the shim keeps the integer, the instruction's address, the register's double and the
address space at the fault, and a FISTP m64 that starts a block of 32-bit code stores the kept integer when all of them
match. An independent review found three defects, fixed in one change:

- **A used entry came back at the next load of CR3** (a kernel with page-table isolation loads it at every return): the
  translated code clears only the field it reads, and the shim recomputed that field from its own copy. Now the shim
  treats an entry whose field is clear in its own address space as gone, and drops it at the load. Test
  `c32-x87-int64-again`: after the kept integer is used, the same FISTP is reached again with 2^62 in the register (the
  double 2^62 + 1 was held as) after another fault and CR3 load; it stores 2^62. It stored 2^62 + 1 before the fix.
- **A full x87 stack at the FILD with any x87 exception unmasked:** there the FILD's stack check is in line, the push
  makes the indefinite, and the paired FISTP still stored the integer read. The pair is now made only where a full
  stack leaves the block (every exception masked), so both instructions then run alone, exactly. Checked by a compiler
  test (QEMU has no x87 stack faults, so a VM case has nothing to compare with).
- **The kept integer was lost whenever the kernel saved and restored the x87 registers between the fault and the second
  run** (the fault sleeps and another task runs, the task moves to another vCPU, a signal is delivered on the way back,
  the kernel uses the FPU itself), so first-touch and copy-on-write were covered only when none of that happened. Built
  as the reviewer proposed: while the entry stands and the register still holds its double, FXSAVE and the XSAVE family
  write that register as the integer itself (the 80-bit format holds every 64-bit integer, so this is what hardware's
  image holds); FXRSTOR and XRSTOR, finding in ST(0) an integer that no double holds, make the entry again, for the
  instruction and address space of the next return to user mode, which the kernel makes right after it restores a
  task's registers. The entry so travels in the save image, also to another vCPU. Test `c32-x87-int64-saved`: the fault
  handler does FXSAVE, FNINIT, a push and FXRSTOR; the image's ST(0) is the 80-bit 2^62 + 1 and the FISTP stores
  2^62 + 1 (it stored 2^62 without the change). Not measured under a real kernel with a fault that sleeps; the
  mechanism is the same instruction sequence.

Still not covered, recorded here as the limits of (b), none of them fixed:

- 64-bit code: the pair is made there too, but a pair whose store faults converts the double on the second run (the
  kept integer is used by 32-bit code only; 64-bit code has no reason to copy integers through the x87).
- A fault at a non-x87 instruction between the FILD and the FISTP ends the block there; the FISTP then starts a block
  without a kept integer and converts.
- A pair split by a block boundary or a jump target between the two is not a pair.
- FNSAVE/FRSTOR and FSTP m80 do not carry the integer (Linux uses neither to switch tasks on these processors).
- Translated code never retires an entry a restore made. Only the kernel's restores make one (a restore a program
  runs itself in user mode does not: the second review). After such a restore, a program that pops the register and
  loads the very double the integer rounds to has a register the shim still takes for the integer: a FISTP m64 at the
  bound address stores the integer, and, new with this change, a save image written then shows the integer too, where
  hardware shows the double. It needs the kernel to have restored that exact integer and the program to produce the same
  double again without returning to the FISTP.
- Not tested: XSAVEC with the compacted XRSTOR (the pinned QEMU fails to run that case; the code path is the same
  legacy area), a restore whose top is not 0 or whose tag is empty, binding by SYSRET or SYSEXIT instead of IRET.
- FILD m64 of an integer above 2^53 still sets the precision flag (see above); it shows only with that exception
  unmasked.

## The final round (November 21)

One round at the head of the branch, alone on the machine, with the starting point (`4d7ea93e`, checkpoint A) measured
in the same hour through the same build route. 1 vCPU, one boot per case, two runs per boot, seconds unless Mflops.

| | start | head | |
| --- | --- | --- | --- |
| `hackbench` (64-bit) | 10.59 / 10.51 | 10.61 / 10.63 | unchanged |
| `hackbench32` | 13.46 / 13.42 | 10.43 / 10.39 | 1.29 times |
| `hackbench32-int80` | 28.52 / 28.30 | 25.53 / 25.35 | 1.12 times |
| `sqlite` (64-bit) | 6.16 / 5.95 | 6.22 / 6.01 | unchanged |
| `sqlite32` | 8.54 / 8.25 | 8.20 / 7.87 | 1.04 times |
| `startup32` | 7.76 / 7.57 | 7.42 / 7.22 | 1.05 times |
| `cray` (64-bit) | 3.02 / 3.00 | 3.06 / 2.98 | unchanged |
| `cray-strict` (64-bit) | 3.22 / 3.19 | 3.32 / 3.25 | within the hour's spread (see below) |
| `cray32-small` | 122.8 / 122.7 | 0.65 / 0.58 | about 200 times |
| `cray32-strict-small` | 69.1 / 69.1 | 0.63 / 0.58 | about 115 times |
| `cray32-sse2-small` | 0.53 / 0.48 | 0.29 / 0.28 | 1.8 times |
| `cray32-sse2` | 7.13 / 7.03 | 3.98 / 3.92 | 1.8 times (real x86: 3.91) |
| `cray32` | (does not finish in 1,800 s) | 9.20 / 9.13 | real x86: 9.19 |
| `cray32-strict` | (not run) | 8.76 / 8.65 | real x86: 9.63 |

c-ray at 4 vCPUs at the head (real x86 with 4 threads in brackets): `cray` 1.02 / 0.91, `cray-strict` 1.03 / 0.99
(1.05), `cray32` 2.83 / 2.84 (2.61), `cray32-strict` 2.89 / 2.84 (2.71), `cray32-sse2` 1.43 / 1.32 (1.12).

| SciMark 2.0 (Mflops, run 1 / run 2) | composite | FFT | SOR | Monte Carlo | sparse | LU |
| --- | --- | --- | --- | --- | --- | --- |
| 64-bit, start | 2732 / 2473 | 3105 / 1814 | 4254 / 4259 | 852 / 852 | 3225 / 3218 | 2224 / 2222 |
| 64-bit, head | 2869 / 2674 | 2474 / 1488 | 4205 / 4217 | 850 / 844 | 3201 / 3212 | 3616 / 3611 |
| 32-bit SSE2, start | 2269 / 2290 | 709 / 814 | 4065 / 4080 | 62 / 62 | 3650 / 3651 | 2861 / 2844 |
| 32-bit SSE2, head | 2699 / 2479 | 2595 / 1478 | 3945 / 3950 | 256 / 257 | **2539 / 2542** | 4159 / 4168 |
| 32-bit x87, start | 32 / 32 | 17 / 17 | 45 / 45 | 23 / 23 | 39 / 39 | 36 / 36 |
| 32-bit x87, head | 1285 / 1239 | 1302 / 1041 | 1702 / 1732 | 293 / 293 | 1190 / 1186 | 1940 / 1941 |

Read with these in mind:

- **The machine was about 2 to 4% slower this night than when the earlier tables were made** (`cray32` read 9.01 /
  8.93 the day before and 9.17 / 9.11 now at the very commit it was first measured at, rerun to check; 64-bit Monte
  Carlo read 962 then and 852 now at the starting commit too). No thermal warning was recorded and nothing else was
  running. So compare columns of this table with each other, not with earlier sections.
- **One regression, found by this round: the sparse multiply in the 32-bit SSE2 build is 30% slower than at the start
  (3,650 to 2,540).** The cause is the x87 runs of this task: in 32-bit code a run may stay open from one block to the
  next, so every block tests "is an x87 run open?" before its first SSE floating-point instruction (a load of a byte and
  a branch). The sparse loop is one block of seven instructions entered again for every element, and those two
  instructions are a sixth of it; the starting commit's translation of the loop is the same code without them. SOR in
  that build shows the same, smaller (3%). 64-bit code does not have the test (its runs end with their block). It is a
  cost of a design choice, on tight 32-bit SSE loops only. Ways out, none built: give each block a second entry behind
  the test for predecessors that know no run is open (the linker would choose; a day's work with the checkers), or end
  runs at the block's end in 32-bit code too wherever the block's exit does not lead to x87 code (which gives back part
  of c-ray's gain where it guesses wrong). Not decided.
- FFT in the two SSE builds varies between two runs of one boot by up to 40% (and did so at the start); still not
  explained.
- LU's gain is the page-crossing stores; Monte Carlo's in the SSE2 build is the integer FLD.

The x86 selftests' and the i386 campaigns at the head, run after the table: 221 cases passed, 18 expected failures, 3
unsupported (the same expected failures as before; `atomic64` passes).

### Macs without AFP (M1, M2): the same round's 32-bit cases with AFP hidden

A separate step, at the owner's request. AFP is the Arm feature (M4 has it; M1 and M2 do not) that lets Arm arithmetic
produce x86's NaN and denormal results directly. Hidden here with the test switch `NATEMU_FEXC_FEATURE_OFF=afp` on the
M4, so the code is the code an M2 would run, on a faster core.

Correctness: `compat32` (every 32-bit system case, the kept-integer cases among them) and the i386 campaign pass with
AFP hidden (175 passed, 12 expected failures, the same twelve); the floating-point family, whose cases run with and
without AFP in both modes, passes (5,096). The fault families' no-AFP variants (x87 faults among them) ran with every
fault-family run of this task.

Speed, 1 vCPU, seconds (run 1 / run 2) or Mflops:

| | with AFP (the round above) | AFP hidden |
| --- | --- | --- |
| `cray32-small` | 0.65 / 0.58 | 104.9 / 104.7 |
| `cray32-strict-small` | 0.63 / 0.58 | 61.9 / 61.7 |
| `cray32` | 9.20 / 9.13 | not finished in 17 minutes when stopped; 16 times the small scene is about 1,680 s |
| `cray32-sse2` | 3.98 / 3.92 | 6.86 / 6.74 |
| `cray` (64-bit) | 3.06 / 2.98 | 4.21 / 4.18 |
| `cray-strict` (64-bit) | 3.32 / 3.25 | 4.95 / 4.85 |
| SciMark 32-bit x87, composite | 1285 | 35 |
| SciMark 32-bit SSE2: composite, FFT, SOR, Monte Carlo, sparse, LU | 2699, 2595, 3945, 256, 2539, 4159 | 2106, 746, 1758, 61, 3567, 4399 |

So almost nothing of this task's x87 speed reaches a Mac without AFP. Exactly what runs there: the x87 stack is kept in
host registers across a block, the block is compiled for its starting stack top, and no state is written before
instructions that can fault, as with AFP (the pass's `FexcCache()` has no AFP condition; the state test runs the no-AFP
code too). What stays per operation is the wrapper: each x87 operation switches Arm's control and status registers to
the x87's and back and copies its flags, and FLDCW stays in the shim. Those are the parts with the AFP condition:
`FexcX87Runs()` in `tools/fexc/shim/fexc_sites.inc`, `FexcX87Lazy()` and `FexcFldcwInline()` in
`tools/fexc/shim/fexc_hook.h` (each has `!fexc_no_afp`). The wrapper is what the time goes to: `cray32-small` takes
105 s there against 123 s at the start, so the register caching alone is worth 15%. (An earlier note of this page's
author said hosts without AFP "get the register caching but keep the per-operation flag copy": that is the same
state; this section's first wording, "nothing reaches", was too strong.) 32-bit x87 programs are about 150 times
slower on such a Mac than on an M4-class core, beyond the cores' own difference. The page-crossing stores and the
system-call work do not depend on AFP (LU: 4,399). The sparse multiply does not have the regression there (no runs,
so no test). What it would take: the same caches with the NaN and denormal fix-ups done per operation inside a run; a
design of its own, not estimated here.


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
- **x87 instructions cost about 48 Arm instructions each:** done after the pause, see "the x87 register stack in host
  registers" above (now about 6).
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

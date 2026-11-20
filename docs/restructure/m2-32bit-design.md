# M2: running 32-bit code — design notes

Discussion record of November 19, 2026, between the owner and the main session, with an independent read-only verifier
checking the code. Nothing here is built.

The page has two parts with different standing:

- **Part 1, 32-bit programs in a 64-bit OS,** is the priority. It is designed for speed, with *acceptable* inaccuracy:
  mostly what FEX gives us, written down as deviations. It is meant to be built.
- **Part 2, boot-time 16-bit and 32-bit code,** is high-level thinking only. Nothing in it is pinned; it is open to
  discussion before any of it is designed in detail.

Each point is marked:

- **Decided (owner)**: the owner's direction.
- **Recommended**: the session's and verifier's recommendation, not yet decided.
- **Open**: waiting for the owner.
- **Inferred**: not checked in code or by a run; verify before relying on it.

The general rule (owner): be lazy. Copy what FEX, Box64, VirtualBox, Linux or others already do, unless we have a reason
to differ. When we copy, still check the choice against our goals: fast by default, no silent failures, faults and
interrupts handled correctly, Windows later.

**Tests and benchmarks (owner):** every new test, benchmark or measurement in this work goes through the test framework
(`tools/natemu-test`, cases in families under `tests/`; see `docs/testing.md`), and is a configuration entry rather than
a new script wherever the framework allows. No standalone runners, and no VMs started outside the framework's queue.

**How much to run:** as [the testing page](../testing.md#how-much-to-run) says, with this plan's cadence:

- the narrow run while iterating; `tools/natemu-test run` (smoke and affected families) at the end of every task and
  after any change to a shared path;
- the full gate at task D, at task 1 (its changes reach every 64-bit run) and at each checkpoint, per [when the gate is
  needed](../testing.md#when-the-gate-is-needed) (the user may explicitly bypass it);
- **a benchmark round at each checkpoint (A, B, C) (owner),** as a reminder of where speed stands, with checkpoint B's
  the full round against QEMU (the roadmap's exit); within task 4, each speed change measured on its own;
- 64-bit speed between checkpoints: task 1 is host-side, so the gate and checkpoint A's benchmark cover it. For the
  compiler-side changes of tasks 2-4 (the fork, fexc's shim code), the compiler's replay check (`tools/fexc/README.md`,
  the replay gate) shows in minutes whether 64-bit output changed. It only feeds recorded requests to the old and new
  compiler, so it says nothing about host-side changes. Since task 2 it is a framework case, `static/fexc/fexc-replay`
  (one recorded boot; the old compiler is the one of the checkout's merge base with `main`).

**Reference answers:** for prototyping, prefer the real x86 machine (owner), which the framework already supports per
suite (`reference = "x86"`, `NATEMU_X86_HOST`; `tools/natemu-test xref` compares it with QEMU). It runs 32-bit user
programs natively and has none of QEMU's model gaps (QEMU 7.2 is wrong on several x87 and SSE cases). Two limits:

- It runs user-level programs only. Cases that need kernel mode (IRET into 32-bit code, mode switches, exact page
  faults) still take QEMU's answers, unless a KVM backend on the x86 machine is added (hardware answers for kernel-mode
  cases; new work, not planned).
- The machine is AMD, and we present an Intel CPU. For 32-bit system calls they differ: AMD allows SYSCALL from 32-bit
  code and refuses SYSENTER in 64-bit mode, Intel the reverse. Those cases take QEMU's Intel answers.

Costs are in agent hours of active work, tests and review rounds included, converted from the earlier engineer-day
estimates at about 1.5 agent hours per engineer-day (an assumption, not calibrated). Time waiting for the VM test queue
comes on top and often dominates.

The November 15 cost note (`docs/research/32-bit-execution-cost.md`, in the main checkout) predates the translated-code
changes on this branch; this page supersedes it where they differ.

## Where the code stands

These were read in the code on this branch:

- FEX already decodes and compiles 32-bit x86 (`--bitness 32`; one FEX context per argument list). Three places refuse
  it: our compiler's system-safe profile, the host's admission check, and the allowed-instruction list, which has only
  64-bit rows (it already marks segment push/pop and moves, far transfers, IRET and INT as system by opcode, so the
  32-bit work is mostly the 32-bit-only forms).
- The code identity of every translated block already has a "64-bit code" bit; it is always set. Making it follow the
  code segment's mode separates 32-bit from 64-bit everywhere: lookup tables, links between blocks (refused across
  identities), the block pool and the persistent cache. Every mode switch is a system instruction the shim already
  handles, so no link can cross modes.
- One check, `Cpu::control`, answers both "four-level paging?" and "64-bit code segment?"; about fifteen places stop the
  VM when it says no. 32-bit user code is the first code segment that is not 64-bit.
- Still 64-bit-only: the host's decoder for the instructions the shim executes (bytes 40-4f are prefixes there,
  increment/decrement in 32-bit code), jump-target rules (32-bit targets wrap at 4 GiB), the admission rule that allows
  only two segment-base loads, and relocation.
- Segment loads keep the descriptor base for FS and GS (in `fs_base`/`gs_base`) but store base 0 for every segment's
  hidden copy, so CS/DS/ES/SS bases are lost. Far transfers to 32-bit user code raise a deliberate #GP; returns to it by
  SYSRET/SYSEXIT deliberately fault after the return.
- The boot CPU starts directly in 64-bit mode, and the other CPUs start in 64-bit mode through the ACPI wake-up mailbox.
  INIT and start-up signals stop the VM. No 16-bit or 32-bit code runs anywhere today.
- We present an Intel CPU, so 32-bit Linux uses SYSENTER (through the kernel's helper page) and INT 0x80.

# Part 1: 32-bit programs in a 64-bit OS

**Decided (owner):** this comes first, built for performance, with acceptable inaccuracy, mostly what FEX gives us.

It needs nothing from Part 2: 32-bit user code under a 64-bit kernel always runs inside long mode, with the same paging,
interrupt table and task segment as 64-bit code (verifier checked the code).

## Accuracy: what we accept and what we do not

**Recommended:** take FEX's 32-bit behaviour as the accuracy level, and list each deviation on the floating-point and
deviation pages.

Accepted (FEX's behaviour, or ours already):

- **x87 values are doubles** (53 bits, not 64), as today in fast mode. `long double` overflows and underflows at the
  double's range; Debian's 32-bit math library loses many units in the last place for large arguments of power,
  exponent and logarithm (**inferred**, from memory); the precision-control setting does nothing; bit-exact output tests and lockstep games may
  differ. Ordinary double code compiled for x87 is fine, and saving and restoring x87 state is lossless.
- **Decimal x87 instructions** (FBLD/FBSTP) stay refused. Today a refused or unlisted form stops the VM, even from user
  code. **Decided (owner): whatever works best for development**, which is keeping that stop: it is loud, records the
  instruction and the state, and shows every gap at once, where a #UD would turn gaps into scattered program crashes.
  Revisit before a release, when a #UD at CPL 3 would serve users better.
- **A null FS or GS selector** (FS is null in every i386 Linux process, which uses GS for thread-local data): an access
  through it must #GP, not read at base 0. **Recommended (owner: gate it):** a translation-time gate, like the existing
  feature gates in the code identity: in 32-bit code, "FS null" and "GS null" are identity bits, and an FS- or
  GS-prefixed access under them compiles to a #GP stub. Exact, nothing at run time; bits 3-8 of the identity are free.
  The flat check covers CS/DS/ES/SS only.
- **Default segments are flat and limits are never checked**, as in FEX. That matches 32-bit Linux exactly.
- **x87 arithmetic keeps honouring the x87 rounding mode** after the cast fix (see the cast problem), so the fix adds
  no deviation.

**To investigate (owner: find the downside of keeping x87 in doubles; special handling only if normal programs are
heavily affected):** 64-bit integers through x87. FILD/FISTP of an 8-byte integer loses bits above 2^53 with doubles;
FEX's double mode converts on load the same way (`X87F64.cpp`, `FILDF64`). gcc uses that pair for 64-bit atomic loads
and stores on i386 targets without SSE2, which Debian's baseline is (**inferred**), so `std::atomic<int64_t>` could
corrupt silently. Task 0 counts the pair in Debian's i386 libraries and common programs and checks what Box64 does. If
special handling is needed, the likely form is narrow: the atomic idiom is a FILD m64 directly followed by a FISTP m64
in one block, which can be compiled as one 8-byte integer move (unlike the cast sequence, it does not vary).

Not accepted, because they break real programs or fail silently, and the fix costs nothing at run time:

- **Segment bases.** FS and GS already keep their base, so thread-local data works. CS/DS/ES/SS need their real base
  too, for FEX's explicitly prefixed accesses, and the admission rule that allows only two segment-base loads must
  widen.
- **Code FEX would get silently wrong:** a default segment that is not flat, or a 16-bit code or stack segment. Any
  program can make one with `modify_ldt` (Wine's 16-bit support, kernel self-tests), so it must not stop the VM. Follow
  the existing rule "no CPL-3 transfer stops the machine": #GP at CPL 3 as a recorded deviation, so the OS ends the
  program loudly; stop only at CPL 0. Check where the code identity is chosen (entering 32-bit code, or a segment load
  while in it), not at every segment load: a based DS loaded in 64-bit code is legal and ignored. Nothing in translated
  code.
- **Rounding of x87 integer stores** across preemption (the cast fix below).
- **SYSCALL from 32-bit code** must raise #UD, as on the Intel CPU we present.

**Decided (owner, November 19):** "we have no immediate plan to make x87 exact. In the fast mode, x87 stays fast."
Accurate mode covers the exact behaviours hosts without Arm's alternate floating-point feature (AFP) need, only; accurate
80-bit x87 is optional. The roadmap's M1 item listing the 80-bit x87 path as P1 was a mistake and is corrected.

**To do:** list x87 precision among the deviations on the floating-point page; it is not there today.

## Translation and registers

**Recommended (copy FEX):** run 32-bit code through the existing FEX path with FEX's own compact 32-bit register plan,
the combination FEX tests upstream.

- The compact plan doubles FEX's scratch registers (14 general and 22 vector, against 7 and 14), which mainly helps
  x87-heavy blocks.
- What depends on it: the shim's state save after 32-bit code must skip the upper eight general and vector registers
  (their Arm registers are FEX scratch in this plan), and the host's register-map check needs a second, 32-bit map. Plus
  tests that those registers survive a 64 -> 32 -> 64 round trip.
- What does not: the trap entry and exit save every register wholesale; helpers and stubs use only scratch registers;
  fault recovery reads registers by number from its site records; our checking tools read the map from the compiler.
- Cost: about 8-15 agent hours (it touches the admission register-map check, the shim's state save and hooks with
  hard-coded registers), plus keeping two maps in step. Mode switches cost nothing extra: each already goes through a full
  state save and restore.
- Checked in task 2 ([findings](m2-task2-findings.md)): FEX's 32-bit code does assume the upper halves of the general
  registers are zero. Real hardware keeps the upper half of a register 32-bit code does not write and zeroes it on a
  32-bit write. Task 3a saves them as zero after 32-bit code (the session's decision, reversible; the findings give the
  alternatives).

## Segments

**Recommended:** FEX's behaviour plus real bases, as listed under Accuracy.

- FEX adds a segment's base only when the instruction names a segment, plus fixed cases such as string destinations.
- **Keep the real hidden base and limit for all six segments** at every load, with `fs_base` and the FS base kept as one
  value (they are one register on x86). It costs nothing in translated code, and the flat check needs it (task 1).
- **"Flat"** means base 0 and a 4 GiB limit, so limit checks on IRET and SYSRETL into 32-bit code are trivially met
  until checkpoint C.
- **Admission:** in 32-bit mode FEX loads the ES base for every string destination, even without a prefix, so admission
  must allow the CS/DS/ES/SS base loads (held at 0 while flat-only) from task 2, or every MOVS/STOS block is refused.
- **To test in task 3 (checkpoint A needs it):** base plus offset wraps at 32 bits (static i386 programs reach thread-local data at `%gs:`
  with negative offsets), and the reported fault address is 32-bit.
- **Windows' 32-bit layer (later, not M2):** the FS segment holds the thread information block with a 4 KiB limit. A
  limit check on FS- and GS-prefixed accesses to size-limited segments, about two instructions on those accesses only,
  if Windows turns out to need it.
- **Non-flat default segments: deferred (owner), scheduled after checkpoint C and before Part 2** (task 6). Until then
  they raise #GP at CPL 3. Who uses them: Win16 programs under Wine, DOS emulators, rare old software with based LDT
  segments; 32-bit Linux and 32-bit Windows programs do not. Deferred because they are uncommon and costly in
  translated code: FEX adds a base only for prefixed accesses, so every memory access changes in the fork, the segment
  shape joins the code identity (more lookup-table pressure), and each access pays about one instruction for the base
  and two or three for the limit. 16-bit code segments cannot be translated at all (FEX has no 16-bit mode) and stay
  #GP until Part 2.

## Mode switches and system calls

- Far jump, call and return, interrupt return, and SYSRET/SYSEXIT to 32-bit code become real loads of a 32-bit code
  segment, with limit checks instead of address-validity checks. Extend our segment reference model first; it is the
  oracle. The roadmap asks for this path done properly, not flat-only, because Windows' 32-bit layer switches modes with
  far jumps; flat-only is a checkpoint on the way (see Order of work).
- SYSENTER and delivery through the 64-bit interrupt table already fit, including events from 32-bit code.
- Split `Cpu::control` into a paging check and a code mode taken from CS.L and CS.D, rather than loosening its code
  segment test; and make the "64-bit" bit of the code identity a mode field (bits 3-8 next to it are free).

**Fast paths for 32-bit system calls.** The existing fast path serves only 64-bit SYSCALL/SYSRET. Linux's return paths
below are inferred from memory of its 32-bit entry code.

- **SYSENTER in, SYSRETL out:** a direct copy of the 64-bit fast path, about 5-8 agent hours. Dynamically linked programs use
  this.
- **INT 0x80 in, IRET out:** reads the interrupt table and task segment and writes a stack frame. A fast path needs
  cached copies, dropped when the guest writes those pages: about 11-15 agent hours, riskier. Static programs and hand-written
  assembly use this.
- **Decided (owner, left to the session):** SYSENTER first; INT 0x80 only after a measurement (an i386 system-call
  microbenchmark or hackbench against the 64-bit build) shows it matters.

## Decoding the instructions the shim executes

fexc cuts a block at every instruction its allowed-instruction list marks as system, an explicit list, not "whatever
FEX cannot translate" ("system" here also covers gathers, masked moves, the x87 state forms and memory RCL/RCR). In
32-bit mode FEX does translate POP DS/ES/SS, MOV to a segment register, INT, ARPL and indirect far calls, its userland way,
so the 32-bit rows must mark these as system, or the shim does not see every segment load. Other 32-bit-only rows to
decide: LES/LDS (C4/C5), BOUND, INTO, PUSHA/POPA, and 40-4f as increment/decrement.

fexc reports only the opcode map and opcode, with no operands and no length. The host decodes the operands (`crates/host/src/system.rs`: about 1,000 lines of decoding and 27 tests) into the record the shim
keeps and executes. That decoder is 64-bit only.

- **Why not FEX's decoder:** FEX never executes these instructions, so its table rows for them are wrong or missing: the
  debug-register moves have no ModRM, the control-register move is fixed at 64-bit, the 32-bit far call and jump have no
  immediate, LAR is undecoded, LSS/LFS are invalid. (FEX's decode is reused where it is right: the device-access record
  fexc builds for MOV forms.)
- **Decided (owner):** iced-x86 as an ordinary crates.io dependency in the host crate only: decoder feature only, Intel
  decoder behaviour, exact version pinned in the lockfile. No fork; a decode bug goes upstream, with a temporary Cargo
  `[patch]` only if we cannot wait. Our recorded behaviour (ignored segment prefixes, REX placement, LOCK and
  unadvertised-feature #UD, refused forms) stays in our mapping code.
- **When:** with this work, for 32-bit system instructions. Widen the record to carry any segment and the address size
  (and room for 16-bit address forms).
- **Decided (owner): replace the hand-written decoder if cheap.** Write the mapping for every mode; run both decoders on
  the existing test cases and compare. If they agree (or the differences are bugs in the old one), delete the old one in
  the same change; if 64-bit shows many disagreements about recorded behaviour, keep 64-bit on the old one and report.
  iced-x86's per-instruction CPU-feature information may also replace the hand-kept unadvertised-feature tables.
- Hot system instructions are compiled inside FEX only when measured (today CLI/STI, RDTSC, SWAPGS, MOV from CR2, VERW).

## Lookup tables

There are four lookup tables: two for kernel code, two for user code, one code identity each.

- A 32-bit and a 64-bit process take one user table each, so no new tables are needed.
- On Linux one process is either 32-bit or 64-bit; mixing happens between processes. Exceptions inside one process:
  Wine's newer mode for 32-bit Windows programs, some emulators and JITs. On Windows, every 32-bit program has 64-bit
  user code and switches on every system call.
- Eviction starts at a third user identity. The likely third one was the x87 rounding mode during casts, fixed below.
- **Decided (owner):** no tagging of table entries until a measurement justifies it; simple fixes first. If eviction
  shows up: more user tables first (the memory block holding them has room for two more).

## The stack-alignment check

The translated-code speed-up skips the page-crossing check of stores relative to the stack pointer once that pointer is
known to be 8-byte aligned. 32-bit code pushes 4-byte arguments, so the check would trap at nearly every push, and after
16 traps the CPU drops the optimisation. The count resets at every page-table switch, so this repeats at every context
switch: 16 traps of about 300 ns, plus any recompiles (the earlier figure of 200 us per switch is not explained).

**Recommended (simple first):** keep the single "aligned" bit, with the slot width set by mode: in 32-bit code it means
4-byte aligned, and 8-byte stores (common: x87 doubles passed on the stack) keep the ordinary inline check, three
instructions and no trap. 64-bit output stays byte-identical, and the admission checker needs only the width.

- A 32-bit add or subtract of a multiple of 4 must keep the bit in 32-bit code (today any 32-bit write clears it).
- **Only if measured:** one trap counter per mode (the counter already resets at every page-table switch, so this may
  be unnecessary with the width fix).
- **Only if measured:** the general version tracking 1, 2, 4 or 8-byte alignment per register. It changes 64-bit output
  and must be repeated in the admission checker.

## The cast problem (x87 rounding)

For each `(int)x` of a double, gcc and clang on a 32-bit target without SSE3 save the x87 control word, load a copy set
to truncate (FLDCW), store the integer (FISTP), and load the saved word back. Debian's 32-bit packages are built for a
baseline without SSE3, as far as we know (**to verify**). Our advertised SSE3 doesn't matter: the choice was made at
compile time. Old MSVC programs call a helper doing the same; modern ones and Go use SSE2.

In natemu each FLDCW traps into the shim (about 300 ns), and the x87 rounding mode is part of the code identity, so each
one also moves to another set of translations and another lookup table: two traps and two identity switches per cast.

Why FLDCW traps today: not because it can raise anything unusual. The control word feeds the code identity (the
rounding mode when it differs from SSE's, and the unmasked x87 exceptions, which translations check for), and only the
shim can switch identities. FLDCW's own exceptions are ordinary: the memory faults of a 2-byte load (#PF, #GP, #SS, #AC),
#NM when the x87 is disabled (already handled by compiling such code as gated), and #UD with LOCK. It never raises #MF
itself: unmasking a flag that is already set makes the exception pending until the next waiting x87 instruction (SDM
FLDCW), which the shim handles. So FLDCW can be inlined whenever the identity need not change, which is what the fix
below does.

What others do:

- **FEX (read in its source):** FLDCW is inline and writes Arm's floating-point control register, which SSE shares
  ("whichever was set last").
- **Box64 (from memory):** FLDCW is an inline store; the integer-store and round-to-integer instructions read the stored
  rounding mode and set Arm's rounding around that one instruction.

FEX's way, copied literally, fails silently: if the guest kernel preempts a thread between its truncating FLDCW and the
integer store, the kernel restores both control words but not which was written last. Our traps and moving a thread
between CPUs have the same problem.

**Recommended (close to Box64, about as fast as FEX):**

- FLDCW stays in translated code when the exception masks do not change (rounding or precision bits only), and just
  stores the control word in the guest state. No hidden "last writer" state. Only the rounding bits leave the code
  identity; its x87 bit stays keyed for SSE's flush-to-zero and denormals-are-zero.
- The integer-store and round-to-integer instructions (FIST, FISTP, FRNDINT) read the rounding mode from the stored
  control word at run time and use Arm's explicit-direction rounding instructions (nearest, toward zero, up, down). They
  never depend on Arm's control register, so they need no control-register write even inside a cast, and faults,
  interrupts, preemption and CPU moves cannot change their result. They are excluded from the arithmetic wrapper's
  control-register switch below.
- A FLDCW that changes the exception masks, or unmasks a flag already set, still goes to the shim, so pending x87
  exceptions stay exact.
- The integer store's invalid and inexact flags are computed as today. The 64-bit form needs the explicit inexact
  comparison the 16/32-bit forms already use.
- x87 arithmetic still honours the x87 rounding mode. This is not new machinery: today every x87 operation is already
  wrapped by `FexcX87Begin`/`FexcX87End` (`tools/fexc/shim/fexc_sites.inc`), which save and clear the status register
  and, under an identity keyed on a different x87 rounding mode, write the control register around the operation. Only
  the decision moves from compile time (the identity, chosen after FLDCW's trap) to run time. FEX also honours it, the
  cheap and loose way: its FLDCW writes the shared control register, so x87 and SSE follow whichever was set last.
- **Speed, common case (both modes round to nearest):** a load, a compare and a never-taken branch added to each x87
  operation's existing wrapper. Unmeasured. The nearest measurement is unfavourable: a never-taken branch per SSE
  operation cost +77% on cray's hot block, though x87 operations already pay the wrapper, so the relative cost is
  smaller. Measure on an x87-heavy 32-bit benchmark before choosing.
- **Speed, rare case (modes differ):** two control-register writes per x87 operation, exactly what keyed blocks do
  today; removing that switching from sqlite's accurate-mode run changed nothing measurable.
- **If the per-operation check costs too much:** check once per block instead, at entry of a block with x87
  arithmetic and after each inline FLDCW in it; when the modes differ, trap to the shim, which switches to the keyed
  identity as today. Traps happen only when the program really runs x87 arithmetic under its own rounding mode; the
  common cast sequence (FLDCW, FISTP, FLDCW, no arithmetic between) never traps. About 8-11 agent hours.
- Cost of the per-operation version: about 5-8 agent hours, mostly admission and tests. The x87 reference tests
  (`tools/reference/gen_i6.py`, random rounding modes under arithmetic, locked against QEMU) keep passing with either.
- Rejected: letting x87 arithmetic follow SSE's rounding mode (silent last-bit errors in code that sets the x87 mode
  alone, such as i386 glibc's internal round-to-nearest sections, **inferred**; it would also break `gen_i6.py`'s
  groups).
- Not pattern matching in FEX: the sequence varies too much (registers, offsets, scheduling, a precomputed truncating
  word, MSVC's helper).
- First measurement: count FLDCW traps on the 32-bit c-ray build.

## Expected performance problems, roughly worst first

1. The cast problem (fixed above).
2. The stack-alignment check (fixed above).
3. No sharing of translated code between processes: relocation stays off for 32-bit code at first, so each process
   recompiles its randomly placed libraries. Measure compile requests per program start in a 32-bit `configure`.
4. 32-bit system calls without a fast path: two full traps each.
5. Position-independent 32-bit code calls a helper to find its own address in most functions; each return goes through
   the dispatcher, about 9 ns.
6. Small costs: 8-byte page-crossing checks; the prefixed access that reads the stack-protection value; segment reloads
   at context switches; one more compiler setup per mode; fewer guest registers, which is inherent to 32-bit code.
7. x87 is the default floating point of i386 code (doubles are returned on the x87 stack), so every floating-point
   operation pays the x87 wrapper's status-register save and restore; and the i386 math library's assembly routines
   use the remainder, scale, logarithm and arctangent instructions, each a trap into the shim. Task 0 counts them.
8. Windows' far-jump mode switches (Windows milestone).

## Optimisation leads (not committed)

Ideas to try only if a measurement points at them; none is part of the plan.

- **Value tracking of the x87 control word inside a block.** Follow the word through FNSTCW, moves, bit operations and
  stack-slot stores and loads. An FLDCW whose value is provably "the entry word with only the rounding bits changed",
  or the entry word itself, needs no identity change, and FIST/FISTP/FRNDINT then know their rounding at compile time.
  Safe as a prediction: the inline FLDCW compares the value it loads with the predicted one and traps on a mismatch, so
  a miss costs speed, never correctness. Misses: a truncating word kept in a global variable, a cast spread over
  several blocks (old MSVC's helper).
- **One status-register save per run of consecutive x87 operations,** instead of one per operation.
- **Placement:** the analysis on FEX's SSA IR, where a stack-slot store and its reload are plain values. It would be a
  block-local forward pass next to FEX's own (`FEXCore/Source/Interface/IR/Passes/`), with the code in natemu's tree
  like the existing alignment tracking (`tools/fexc/shim/fexc_sites.inc`, `FexcAlignedAfter`) and a one-line hook in
  the fork. The decisions (identity, trap, emitted check) stay in fexc's hooks. Not in FEX proper: the policy is ours.
- **Cost:** none at run time beyond the one compare per inline FLDCW. At compile time, one linear pass over a block's IR
  (one compile is about 27 us); unmeasured. Cross-block analysis is not worth it: blocks are compiled on demand and
  invalidated by code writes.

## Order of work: tasks and checkpoints

**Decided (owner):** flat-only segments are fine as a checkpoint inside the plan, and the work may be split into
smaller tasks. Each task below ends with something that runs and a test in the framework; each checkpoint is a state
worth merging.

D. **Diagnostics first (owner: yes, better for diagnostics).** Today a Linux boot's stop prints only the stop reason
   and its codes (`crates/cli/src/main.rs`), and the 60 s watchdog reports an Arm address, not a guest instruction; a
   vCPU waiting in the host is not detected, and nothing dumps a running VM.
   - Every stop prints the guest RIP, mode and CPL, the instruction bytes, the registers and the code identity.
   - A hang becomes a quick exit with the same report: the watchdog's Arm address mapped back to the guest
     instruction through the block's site records (approximate between sites, and marked so), and a host-side progress
     check for a vCPU stuck waiting in the host.
   - **Inspect without stopping, as a feature on by default (opt-out):** a signal makes every vCPU exit (the watchdog's
     forced exit already does this), the host prints each vCPU's state and where it was (guest, shim, host wait), and
     the vCPUs run on; Hypervisor.framework resumes a forced-out vCPU unchanged. Nothing on the hot path, so no reason
     to have it off except a user's choice.
   Done when a deliberately unsupported instruction, a guest spin and a host-side wait each produce the report, as
   cases in the framework.
0. **First check (2-3 agent hours plus VM time, no code):** the Debian 13 guest kernel has 32-bit program support enabled; Debian's
   i386 packages exist; Debian 13's i386 baseline (whether it requires SSE2, which would make the 8-byte FILD/FISTP
   worry mostly moot); count FISTTP against FLDCW, the 8-byte FILD/FISTP pair, and the trapping x87 instructions
   (remainder, scale, logarithm, arctangent) in Debian's 32-bit C and math libraries; which programs use INT 0x80
   rather than the kernel's SYSENTER helper (from memory: Go, signal return, hand-written assembly; static glibc uses
   the helper); run FEX's own 32-bit tests, which our runner skips today.
1. **Plumbing, no 32-bit code runs yet:** split `Cpu::control` into a paging check and a code mode; make the code
   identity's 64-bit bit a mode field; keep the real hidden base and limit at every segment load (translated code does
   not use them yet); iced-x86 for the instructions the shim executes, mapped for every mode into the widened record
   (32-bit decode included), compared against the hand-written decoder on the existing cases (replace it if cheap).
   Done when the 64-bit gate passes unchanged.
2. **Translate 32-bit code:** fexc and admission accept `--bitness 32` with FEX's compact register plan and its second
   register map; the 32-bit allowed-instruction rows, with the segment-loading forms marked system; the CPU state's
   CS/DS/ES/SS base fields and admission allowing their loads; 32-bit jump-target wrap; relocation and the persistent
   cache refused for 32-bit code at first; 32-bit code compiled without the push-run stack option until task 4 (the
   option assumes 8-byte slots); our precision and site checkers on 32-bit code. Done when 32-bit blocks compile and
   pass admission offline.
3. **Run 32-bit code, kernel-driven mode switches, flat segments.** The largest task; estimate it after task 2.
   - **3a, the mechanism, against system-family cases:** extend the segment reference model for IRET and SYSRETL into
     32-bit code first (it is the oracle), then make them real loads of a 32-bit code segment, replacing today's
     deliberate faults; events and SYSENTER/INT 0x80 out of 32-bit code; SYSEXIT stays a fault (Linux never uses it)
     until task 5; #UD for SYSCALL from 32-bit code; #GP at CPL 3 for a non-flat default segment or a 16-bit segment;
     the shim's state save after 32-bit code skipping the upper eight general and vector registers; the 32-bit wrap of
     base plus offset and a 32-bit fault address. Test: system-family cases for IRET into 32-bit code, an exact page
     fault, the 64 -> 32 -> 64 round trip of the upper registers (otherwise 64-bit processes lose r8-r15 and xmm8-15 at
     the first context switch), answers locked against QEMU.
   - **3b, shim-executed instructions with 32-bit semantics:** EIP and ESP truncation, 32-bit addressing, 32-bit
     PUSHF/POPF/IRET sizes, the x87 state-save formats. Test: 32-bit cases of each in the system family, then a small
     static 32-bit program under the Debian guest.

   **Checkpoint A, flat 32-bit Linux programs:** static, then dynamic with thread-local data, signals, threads and x87,
   at 1, 2 and 4 vCPUs. Correct but not yet fast.
4. **Speed, one change at a time, each measured:** the stack-check width; the cast fix (with x87 arithmetic rounding
   kept through the existing bracket); the SYSENTER/SYSRETL fast path. INT 0x80 only if a measurement asks for it.

   **Checkpoint B, flat and fast:** the roadmap's exit measured here: a stock i386 userland including a 32-bit toolchain
   build, performance recorded against QEMU.
5. **Segments done properly:** translated code using real CS/DS/ES/SS bases (already kept since task 1), the segment
   reference model extended to far jump, call and return and SYSEXIT, far jump, call and return between 32-bit and 64-bit user code with limit checks, SYSEXIT
   to 32-bit code. Test: a program that far-jumps 64 -> 32 -> 64 in one process, takes an exact fault in each mode and
   returns, locked against QEMU.

   **Checkpoint C, mixed 32/64 in one process:** what the roadmap's far-transfer item asks for, and what Windows' 32-bit
   layer will need later.
6. **Non-flat 32-bit segments (deferred; after C, before Part 2):** translated code adds the base of a non-flat
   default segment on every access and checks its limit, with the segment shape in the code identity; about 15-23
   agent hours. The trigger to start it is a workload that needs it (Win16 under Wine, DOS emulators). 16-bit code
   segments still wait for Part 2.

Checkpoints A and B use only what the kernel does to switch modes; Linux's 32-bit programs never far-jump between
modes themselves, so the far-transfer path can wait until C without blocking them.

### Time estimate

Agent hours of active work, tests and review rounds included; the session's own estimate, not yet reviewed. Time
waiting for the VM test queue comes on top. "Bugs" rows are the least certain: they cover what real programs find.

| Task | Agent hours | Running total |
| --- | --- | --- |
| D. Diagnostics (stop report 2-4; hang to exit 2-4; inspect without stopping 3-6) | 7-14 | 7-14 |
| 0. First check | 2-3 | 9-17 |
| 1. Plumbing (control split, mode field, hidden bases 3-5; iced-x86 mapping and comparison 5-12; other 3-5) | 11-22 | 20-39 |
| 2. Translate 32-bit code (register plan 8-15; allowed-instruction rows 4-8; admission, bases, wrap, checkers 6-11). **Done: about 9** ([findings](m2-task2-findings.md)); the register plan cost nothing in the compiler and moves to task 3a's state save | 18-34 | 38-73 |
| 3a. Mode-switch mechanism (segment model and IRET/SYSRETL 6-10; register save 3-5; rest and tests 9-15) | 18-30 | 56-103 |
| 3b. Shim instructions with 32-bit semantics | 11-20 | 67-123 |
| Checkpoint A: bugs from real programs (static, dynamic, threads, signals, x87) | 10-25 | **77-148** |
| 4. Speed (stack check 3-6; cast fix 5-11; SYSENTER fast path 5-8; measuring 3-5) | 16-30 | 93-178 |
| Checkpoint B: bugs from the toolchain build, the QEMU comparison | 8-20 | **101-198** |
| 5. Segments done properly | 12-20 | **113-218** (checkpoint C) |
| 6. Non-flat segments (deferred, starts on demand) | 15-23 | 128-241 |
| Part 2 (not pinned) | 60-90, probably optimistic | |

The roadmap's 4-8 engineer-weeks (about 30-60 agent hours) covered flat segments only, before the far-transfer path,
the decoder and the cast fix were added. After task 2, replace these figures with ones measured on tasks 0-2 (task 2's
is in its row; tasks D, 0 and 1 are reported by their own sessions).

# Part 2: boot-time 16-bit and 32-bit code (not pinned)

**Decided (owner):** this part is high-level thinking, explicitly not pinned and open to discussion. Nothing below is
a design yet; it records the leans and the questions so far.

## What it is for

- The firmware's climb from the reset vector to 64-bit mode: 16-bit code, a far jump to 32-bit code with paging off,
  page-table building, then the switch to 64-bit.
- Starting the other CPUs through INIT and start-up signals: each starts in 16-bit mode at the given page and climbs on
  its own. Firmware does this; as we understand it, Windows needs it; Linux uses it when no wake-up mailbox exists.
- The short 32-bit stretch between turning paging on and the jump to 64-bit code.
- No performance expectation, correctness only. A few thousand to about 10,000 instructions per climb (**inferred**,
  no boot-stub source read yet).
- The first user is the firmware milestone (M3). The roadmap lists this under M2 with a reset-vector exit test.
  **Undecided (owner):** M2 or the start of M3, depending on each milestone's workload.

## Leans so far (not pinned)

- **An interpreter rather than a translator.** Extending FEX to 16-bit code saves milliseconds per boot (one compile is
  about 27 us; most boot code runs once) for a large permanent fork change, and would not remove the need for something
  to run the 32-bit paging-off stretches. Worth reconsidering if a workload runs long 16/32-bit stretches (legacy BIOS,
  option ROMs, DOS-era software). The owner leaned this way.
- **16-bit and 32-bit together:** 16-bit boot code is full of 32-bit operand and address prefixes.
- **In the shim, with the host decoding** each instruction once with iced-x86 (the same decoder as Part 1) and the shim
  executing it, as it already does for system instructions. A switch between interpreter and translator is then a change
  of code identity at block lookup, with one copy of the CPU state. A host-side interpreter would copy state at every
  switch and need a coherence protocol with running CPUs.
- **On the hardware memory system, not a simulation** (the owner leaned this way): an identity-mapped address space filled by the existing
  shadow machinery (device pages fault into the device path, translated code pages are read-only), with Arm's "access as
  user" loads and stores plus a small fault-fixup table, as Linux reads user memory. Only segment base and limit stay in
  software.

## Questions to discuss

- **Hand-offs:** how many ways back into the interpreter (turning protection or paging off, far jumps to 16/32-bit
  kernel segments, INIT, reset), and whether "a change of code identity" really covers them all.
- **Memory model:** whether the VM's x86-style ordering covers the shim's own accesses (**inferred**; a litmus test
  settles it); x86 starts with caching disabled, which a barrier after each interpreted store would give.
- **Several CPUs:** one CPU interpreting while others run translated 64-bit code; atomics shared between the two; a CPU
  rewriting a start-up page another CPU will run; warm resets.
- **Interrupt delivery:** real-mode delivery and 32-bit interrupt and trap gates are new formats next to today's 64-bit
  delivery; task gates probably stop the VM.
- **Testing:** generated per-instruction 16/32-bit tests with QEMU-locked answers, plus a cross-check of flat 32-bit code
  between interpreter and translator, plus a few integration tests (a reset-vector image, CPU start-up at 1-64 vCPUs,
  warm resets, CPU offline/online). Not integration-only: interpreter bugs, especially in flags, surface far from their
  cause.
- **Firmware choice:** builds whose early stage is entirely 32-bit mean long interpreted stretches; the 64-bit build of
  the standard UEFI firmware avoids that.
- **Cost:** a first guess of 60-90 agent hours, low confidence and probably optimistic (real-mode delivery and
  per-instruction tests). A short spike (reading the real boot stubs, the litmus
  test) should replace it.

## What Part 1 does that keeps Part 2 open

These are done in Part 1 for its own reasons and happen to leave Part 2 free:

- the paging check split from the code-mode check;
- the code identity's mode field;
- real bases and limits kept for all six segments;
- iced-x86 for the instructions the shim executes, with a record that can carry 16-bit address forms;
- the "this segment shape cannot be translated" decision kept where code identity is chosen, so an interpreter could
  later take that code instead of a stop.

## Open decisions for the owner

- Part 1, x87 arithmetic rounding after the cast fix: **owner leans per block; settled in task 4 by performance and the
  accuracy blast radius.** Task 4 measures each option's cost on an x87-heavy 32-bit benchmark, and the blast radius
  as how often real programs and libraries run x87 arithmetic under an x87-only rounding mode (task 0's counts, and the
  per-block version's trap count on the checkpoint workloads). Accepting the inaccuracy stays possible if the radius is
  negligible.
- Part 2: undecided (owner); Part 1 is the priority. Revisit around checkpoints A and B, when M2's size is known.
- Part 2: everything under "Leans" and "Questions" above.

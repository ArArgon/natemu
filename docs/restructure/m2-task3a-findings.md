# M2 task 3a: 32-bit code runs (the mode-switch mechanism, flat segments)

November 20, 2026. Task 3a of [the M2 plan](m2-32bit-design.md), on [task 2](m2-task2-findings.md): 32-bit user code now
runs under a 64-bit kernel. The kernel enters it by IRET or SYSRETL; exceptions, interrupts, INT 0x80 and SYSENTER
leave it through the 64-bit interrupt table, as before. Only flat segments run. What the shim does itself when it
executes an instruction from 32-bit code (operand and address sizes, the 32-bit forms) is task 3b; the list is at the
end.

## What was built

- **The segment reference model first** (`tools/models/segment_model.py`, `syscall_model.py`). IRET to a 32-bit or
  16-bit code segment at privilege level 3 completes: the return address must lie below 4 GiB (else #GP(0)), the stack
  pointer is loaded zero-extended. SYSRETL completes. The model also says which segment shapes natemu does not run
  (`refused_shape`) and gives natemu's fault for them; and it has the two faults a 32-bit access must still raise (a
  null FS or GS, the 4 GiB limit). Far JMP, CALL and RET into 32-bit code stay as they were (a fault at the instruction)
  until task 5.
- **The resident** (`crates/runtime`):
  - IRET and SYSRETL load the 32-bit code segment for real. SYSEXIT to 32-bit code is unchanged (the return completes
    and its target faults; Linux never uses it).
  - The stop for a code segment that is not 64-bit remains only at privilege level 0. At level 3 the code runs, or
    takes the fault below.
  - SYSCALL from 32-bit code raises #UD (the Intel behaviour; also kept off the system-call fast path).
  - Entering 32-bit code loads EAX..EDI zero-extended and XMM0-7; the Arm registers that hold R8-R15 and XMM8-15 in
    64-bit code are entered as zero. Leaving it saves EAX..EDI (upper halves zero) and XMM0-7 only, so R8-R15 and
    XMM8-15 keep the values the 64-bit side left.
  - The instruction pointer is cut to 32 bits wherever the next block is looked up.
  - Addresses that should wrap at 4 GiB (see "Wrapping" below).
  - An instruction that runs past 4 GiB raises #GP(0); the host's fetch window never crosses 4 GiB.
- **The host** (`crates/host`): system instructions cut from 32-bit code are decoded in 32-bit mode (task 1's decoder);
  the two "FS null" and "GS null" request flags; the stop report reads only the eight registers 32-bit code holds.
- **The compiler** (`tools/fexc`): `--fs-null` and `--gs-null`. No change to the FEX fork.
- **The code identity** gained two bits (5 and 6): FS null, GS null, in 32-bit code only.

## Decisions

Each is the session's unless marked; all are reversible.

1. **Where the "cannot run this segment" fault is raised: before the first instruction, in one place.** The plan asks
   for #GP at level 3 for a non-flat CS/DS/ES/SS or a 16-bit code or stack segment, "checked where the code identity is
   chosen". The check is one function (`Cpu::compat_refused`) called where the resident looks up the next block. So
   whatever produced the state (IRET, SYSRETL, SYSEXIT, a segment load inside 32-bit code), the transfer or load
   completes and the next instruction takes #GP with the offending selector as error code (0 for a null one). Before,
   IRET to 32-bit code faulted at the IRET itself, in the kernel. Linux turns both into a SIGSEGV for the program.
2. **What counts as flat.** CS: 32-bit, base 0, limit 4 GiB. SS: the same, a writable expand-up data segment, 32-bit
   stack. DS and ES: base 0, limit 4 GiB, present, writable, expand-up data. A read-only or expand-down DS is refused
   too, because translated code checks neither.
3. **A null DS or ES is refused, eagerly.** On hardware 32-bit code with a null DS runs until it first uses DS. Here
   it faults before its first instruction. Linux gives every 32-bit process a real DS and ES, so this does not affect
   them. It does affect a 64-bit process that jumps into 32-bit code without loading DS (the kernel's `sigreturn_64`
   self-test does; task 5's far jumps will). **For the owner or task 5:** the exact alternative is the same gate as
   for FS and GS: two more identity bits ("DS null", "ES null"; bits 7 and 8 are the last free ones below bit 9) and
   the compiler cutting every access whose segment is DS or ES. About 20 lines in the compiler; not done here because
   the plan names only FS and GS.
4. **The null FS/GS gate is a cut, not a new stub.** Under `--fs-null` the compiler ends the block before any
   instruction that accesses memory through FS, exactly as it does for an SSE instruction when SSE is switched off, and
   the resident raises #GP(0) there. This reuses the existing "gated instruction" path end to end: nothing new in
   generated code, in admission or in the fork. A prefix on an instruction that accesses nothing through the segment
   (LEA, a register form, STOS, a branch) is ignored, as on hardware.
5. **Upper halves of RAX..RDI are zero after 32-bit code** (task 2's decision, implemented). Hardware and QEMU keep
   the upper half of a register the 32-bit code never wrote. The tests therefore never compare such a half.
6. **Scratch registers are entered as zero**, not with R8-R15's values. Either is correct; zero means the round-trip
   test cannot pass by accident.
7. **An access that runs across 4 GiB raises #GP(0)** (#SS(0) through the stack segment). This is the manual's rule
   for a segment limit; QEMU does not check limits.
8. **IRET to 32-bit code is real from level 3 too**, not only from the kernel. It is the same instruction and the
   same code. The other user-level ways into 32-bit code (far JMP, CALL, RET) wait for task 5.

## Wrapping at 4 GiB

FEX computes most 32-bit addresses with 32-bit arithmetic, including segment base plus offset, so thread-local accesses
such as `%gs:0xfffffffc` run natively and need nothing. Two forms use 64-bit arithmetic (task 2 found them): a small
displacement folded into the Arm addressing mode (`[ebp-8]`), and the stack pointer update of PUSH and POP.

- Such an access lands above 4 GiB, where a 32-bit process has nothing mapped, and aborts into the resident.
- The resident then moves the access's base register by the multiple of 4 GiB that puts the access at the wrapped
  address, and runs the same Arm instruction again. A 32-bit register's value is its low half, which this does not
  change; the upper half is cut again at the next save. If the wrapped address is not mapped either, the ordinary page
  fault follows, with the 32-bit address.
- A page-crossing store, which the resident completes itself, is made at the wrapped address.
- **Limit:** in a process that has user memory above 4 GiB (a 64-bit process running 32-bit code, checkpoint C), the
  first access would not abort and would silently reach the wrong address. Task 2 already put that decision at
  checkpoint C (32-bit address arithmetic for these two forms in translated code, one more instruction each).

## Tests

All through the framework. New case `system/compat32` (`tests/smp/run_slice3_vm.py compat`, programs in
`tools/bench/smp/compat.py`), each program at the default block size and at one instruction per block:

| Program | What it shows | Judged by |
| --- | --- | --- |
| `c32-roundtrip` | IRETQ into 32-bit code, INT 0x80 out, back in, out again: R8-R15 and XMM8-15 as the kernel left them; the kernel's changes to R8 and XMM8 survive the second stretch; ESP zero-extended; one-byte INC/DEC, call, push/pop, string move, PUSHFD, MOVD; an FS or GS prefix on an instruction that accesses nothing does not fault | QEMU |
| `c32-fault` | three page faults retried after the handler maps the page (a store mid-block, a store to a read-only page, a PUSH with ESP unchanged in the frame), then a fault at FFFF_FFFCH reached as `[ebp-8]` with EBP = 4: the fault address is the 32-bit one | QEMU |
| `c32-sysenter` | SYSENTER out of 32-bit code, SYSRETL back, INT 0x80 with the SYSRETL selectors in its frame | QEMU |
| `c32-wrap` | `%gs:` with negative offsets (immediate and register); a GS base just below 4 GiB with offsets past it; `[ebp-12]` with EBP = 4; EBX = FFFF_FFF0H plus 40H; PUSH at ESP = 0; POP at ESP = FFFF_FFFCH and stores above the wrapped ESP, one across a page | QEMU |
| `c32-syscall-ud` | SYSCALL from 32-bit code with SYSCALL enabled: #UD | model and QEMU |
| `c32-fs-null`, `c32-gs-null`, `c32-gs-null-string` | a load, a read-modify-write and a string source through a null FS or GS: #GP(0) | model (QEMU does not check) |
| `c32-limit`, `c32-limit-ss`, `c32-limit-fetch` | a load, a stack-segment store and an instruction that run across 4 GiB: #GP(0), #SS(0), #GP(0) | model (QEMU does not check) |
| `c32-ds-based`, `c32-es-null` | natemu's deviation: DS loaded with a based segment, ES with null, inside 32-bit code: #GP before the next instruction | model (natemu only) |

QEMU runs these as an Intel processor (`vendor=GenuineIntel`; a program may now name its QEMU CPU model), because its
default model follows AMD for SYSCALL and SYSENTER.

Existing cases that changed (`smp/system`): `seg-iret-compat-cpl3`, `seg-iret-compat16-cpl3` and `seg-cpl3-iret-compat`
now expect the IRET to complete and the fault at its target; new `seg-iret-compat-rip` (a return address above 4 GiB:
#GP(0) at the IRET by the manual; QEMU loads it and faults at the fetch, so the model judges);
`sc-sysretl` became `sc-sysretl-null-ds` (SYSRETL with a null DS: the fault at its target; SYSRETL itself is in
`c32-sysenter`).

Unit tests: the model's own (`tools/models/test_segment_model.py`, `test_syscall_model.py`); `compat_segments_that_run`
in the ABI crate (every refused shape, the two identity bits); the host's 32-bit request test (32-bit decode of a cut, the
null-segment cut and its arguments); the compiler's `test_32_bit_null_fs_gs` (19 accessing
forms cut, 9 non-accessing forms untouched, the other segment, 64-bit code, prefix order, SSE gating first) and four new
argument sets in its serve test.

Task 2 asked for a replay check that mixes both modes. The compiler's serve test already answers 64-bit and 32-bit
requests from one server process in shuffled orders and compares each with a fresh one-shot compile; it now includes
the null-segment arguments. A recorded mixed stream from a real 32-bit boot is better taken at checkpoint A, when such
a boot exists.

Results are in the task's report (commands and counts); the gate has not run (the owner's order: review first).

## Not done

- **The diagnostics report's instruction bytes under 32-bit paging** (the `ponytail:` note in
  `crates/host/src/execution/diag.rs`). That is paging with long mode off, which only Part 2's boot code has. 32-bit
  programs under a 64-bit kernel use the same 4-level tables, and the report already reads their bytes (with the 4 GiB
  wrap). Left for Part 2: nothing can run in that mode yet, so the walk could not be tested.
- **Non-flat and 16-bit segments** (tasks 5 and 6), **SYSEXIT to 32-bit code**, **far JMP/CALL/RET into 32-bit code**
  (task 5).
- **The kernel self-test expectations** (`sigreturn_64`, `sysret_ss_attrs_64` in the campaign's expected list) were not
  rerun; their 32-bit parts now get further before they fail (decision 3) and the entries' wording may need an update.

## For task 3b

The shim executes these with 64-bit rules today; in 32-bit code each needs its 32-bit behaviour. None is needed by
task 3a's tests, which use register forms and the kernel side's 64-bit instructions.

- **The effective address of a memory operand** of any instruction the shim executes: 32-bit wrap of base + index +
  displacement and then of segment base + offset; the CS/DS/ES/SS base (0 while flat); **#GP(0) through a null FS or
  GS** (the compiler's gate covers translated instructions only; a shim-executed instruction with an FS or GS prefix
  is cut earlier and still uses the base); #GP/#SS at the 4 GiB limit instead of the canonical check.
- **Stack width:** PUSH/POP of segment registers, PUSHF/POPF, ENTER, far CALL, INT's frame are 4-byte and move ESP with
  a 32-bit wrap.
- **IRET executed in 32-bit code** (IRETD): 32-bit frame, and SS:ESP popped only on a privilege change. Today it uses
  the 64-bit rule (always five pops).
- **The instruction pointer after a shim-executed instruction** is cut to 32 bits at the next lookup (done here); a
  frame pushed for INT n at the very top of 4 GiB still holds the uncut sum.
- **The 32-bit-only instructions** the compiler cuts: ARPL, LES/LDS, BOUND, PUSHA/POPA, AAM, INTO (INTO is #UD today),
  POP DS/ES/SS, PUSH CS/SS/DS/ES, far CALL/JMP with an immediate pointer.
- **x87 and SSE state images:** FNSAVE/FRSTOR/FNSTENV/FLDENV and FXSAVE/XSAVE in their 32-bit formats.
- **Register operands above 7** cannot occur, but a few handlers write 64-bit results (MOV from a control register is
  privileged, so not reachable; RDTSC, CPUID, RDRAND, SMSW, SLDT/STR and MOV from a segment register write the low
  half zero-extended, which is right).
- **Single-step and breakpoints** in 32-bit code are untested.

## Effort

Approximate, from the session that did the work (it has no clock): reading the plan, the findings and the code about
2.5 agent hours; the model about 0.5; resident, host and compiler about 2; the test programs, the runner entry and the
QEMU seeding about 2; unit tests, this page and review of the diff about 1. About 8 in all, against the plan's 18-30
for 3a. The register save and the model were each a few lines; most of the saving came from reusing the existing
"gated instruction" path for the null FS/GS gate and the existing S2b test machinery for the new programs.

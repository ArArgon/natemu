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
- **The compiler** (`tools/fexc`): `--fs-null`, `--gs-null`, `--ds-null`, `--es-null`. No change to the FEX fork.
- **The code identity** gained four bits (5 to 8): FS, GS, DS, ES null, in 32-bit code only. Bits 3 to 8 are now all
  used.

## Decisions

Each is the session's unless marked; all are reversible.

1. **Where the "cannot run this segment" fault is raised: before the first instruction, in one place.** The plan asks
   for #GP at level 3 for a non-flat CS/DS/ES/SS or a 16-bit code or stack segment, "checked where the code identity is
   chosen". The check is one function (`Cpu::compat_refused`) called where the resident looks up the next block. So
   whatever produced the state (IRET, SYSRETL, SYSEXIT, a segment load inside 32-bit code), the transfer or load
   completes and the next instruction takes #GP with the offending selector as error code. Before, IRET to 32-bit
   code faulted at the IRET itself, in the kernel. Linux turns both into a SIGSEGV for the program. The fault comes
   after anything the boundary owes first (a single-step trap from the IRET, a pending interrupt it unmasked). One
   visible consequence: an IRET whose return address lies outside a code segment limit below 4 GiB gets #GP(selector)
   at the target, where hardware gives #GP(0) at the IRET; both are inside this recorded deviation.
2. **What counts as flat.** CS: 32-bit, base 0, limit 4 GiB. SS: the same, a writable expand-up data segment, 32-bit
   stack. DS and ES: null, or base 0, limit 4 GiB, writable, expand-up data. A read-only or expand-down DS is refused
   too, because translated code checks neither.
3. **A null DS or ES is exact (the orchestrator's direction, from the owner's rule "exact first").** 32-bit code with a
   null DS runs until the first access through DS, which raises #GP(0), as on hardware. It is the same gate as for FS
   and GS: two more identity bits, and the compiler cuts every access whose segment is the null one. For DS that is
   every memory operand without a prefix whose base is not EBP or ESP, plus the string source; for ES every string
   destination. The first version of this task refused a null DS or ES before the first instruction; that is gone.
   Cost: none in 32-bit Linux processes (DS and ES are never null there); under a null DS nearly every block is one
   instruction long, which is right for code about to fault. A REP string instruction whose count is 0 makes no
   access, so under the gate the resident completes it instead of faulting.
4. **The null-segment gate is a cut, not a new stub.** Under `--fs-null` the compiler ends the block before any
   instruction that accesses memory through FS, exactly as it does for an SSE instruction when SSE is switched off, and
   the resident raises #GP(0) there. This reuses the existing "gated instruction" path end to end: nothing new in
   generated code, in admission or in the fork. A prefix on an instruction that accesses nothing through the segment
   (LEA, a register form, STOS, a branch) is ignored, as on hardware.
5. **Upper halves of RAX..RDI are zero after 32-bit code** (task 2's decision, implemented). Hardware and QEMU keep
   the upper half of a register the 32-bit code never wrote. The tests therefore never compare such a half.
6. **Scratch registers are entered as zero**, not with R8-R15's values. Either is correct; zero means the round-trip
   test cannot pass by accident.
7. **An access that runs across 4 GiB raises #GP(0)** (#SS(0) through the stack segment). The manual's limit rule
   gives that fault, but for a limit of exactly 4 GiB it says such accesses "may or may not" fault, depending on the
   processor (Vol. 3A, "Limit Checking"). So the fault is natemu's choice among the permitted outcomes, and the model
   lists both. It is the choice that never touches unexpected memory, and it is what the AMD reference machine does
   (appendix). QEMU makes the access wrapped.
8. **IRET to 32-bit code is real from level 3 too**, not only from the kernel. It is the same instruction and the
   same code. The other user-level ways into 32-bit code (far JMP, CALL, RET) wait for task 5.

## Wrapping at 4 GiB

At task 3a, FEX computed most 32-bit addresses with 32-bit arithmetic, including segment base plus offset, so thread-local accesses
such as `%gs:0xfffffffc` run natively and need nothing. Two forms use 64-bit arithmetic (task 2 found them): a small
displacement folded into the Arm addressing mode (`[ebp-8]`), and the stack pointer update of PUSH and POP.

- Such an access lands above 4 GiB, where a 32-bit process has nothing mapped, and aborts into the resident.
- The resident then moves the access's base register by the multiple of 4 GiB that puts the access at the wrapped
  address, and runs the same Arm instruction again. A 32-bit register's value is its low half, which this does not
  change; the upper half is cut again at the next save. If the wrapped address is not mapped either, the ordinary page
  fault follows, with the 32-bit address.
- A page-crossing store, which the resident completes itself, is made at the wrapped address.
- **Limit:** in a process that has user memory above 4 GiB (a 64-bit process running 32-bit code, checkpoint C), the
  first access would not abort and would silently reach the wrong address. The same holds for pure 32-bit code under
  an OS that maps a user page in the 32 KiB just above 4 GiB (or at the very top of the address space): Linux does
  not. Task 2 already put that decision at
  checkpoint C (32-bit address arithmetic for these two forms in translated code, one more instruction each).

### Checkpoint C implementation, November 22

FEX fork `fc4270de` computes compatibility-mode scalar and paired addresses before the native memory instruction.
A PUSH computes its wrapped ESP before the split check, stores at that address, and commits ESP after the store.
A POP reads before its 32-bit ESP increment. PUSH and POP retain their stack site kinds for recovery.
The resident preserves its previous abort fallback. To enforce the flat segment end on a native read, it removes
the first user shadow page above 4 GiB before entering or resuming compatibility code if a 64-bit access populated it.
The supervisor has its own shadow. A later 64-bit user access can populate its page again.

The M4 framework case `static/fexc-address32` checks the emitted native operands, their 32-bit address definitions,
and the increment after a POP. The same check detects the old compiler's folded displacements and stack writeback
without executing its code. The case also checks which implicit operands yield a stack fault. MOVS reads the
overridable source before storing through ES. CMPS reads ES first, then the overridable source.
The linked stubs inherit each access's segment.

In `system/compat32-resident-limits`, the string and moffs fixtures check the saved fault frame, vector state and CR2.
The mixed-user-wrap fixture uses one vCPU and CR3. A 64-bit user writes low and high pages and enters 32-bit code
through IRETQ. The 32-bit code uses a wrapped SIB store, pushes a memory operand across the ESP update and
pops to memory addressed with the incremented ESP. Both block sizes check the low values, the stack word,
the final ESP and the unchanged high sentinel. This high page has an independent address; a warm mapping
at exactly the old compiler's unintended address remains outside the guest execution evidence.
Based FS/GS bounds and legal wrapped linear spans have their own checkpoint C requirements.

## Tests

All through the framework. New case `system/compat32` (`tests/smp/run_slice3_vm.py compat`, programs in
`tools/bench/smp/compat.py`), each program at the default block size and at one instruction per block:

| Program | What it shows | Judged by |
| --- | --- | --- |
| `c32-roundtrip` | IRETQ into 32-bit code, INT 0x80 out, back in, out again: R8-R15 and XMM8-15 as the kernel left them; the kernel's changes to R8 and XMM8 survive the second stretch; ESP zero-extended; one-byte INC/DEC, call, push/pop, string move, PUSHFD, MOVD; an FS or GS prefix on an instruction that accesses nothing does not fault | QEMU |
| `c32-iret-cpl3` | IRETQ executed by 64-bit user code into 32-bit code (decision 8), INT 0x80 out | QEMU |
| `c32-fault` | three page faults retried after the handler maps the page (a store mid-block, a store to a read-only page, a PUSH with ESP unchanged in the frame), then a fault at FFFF_FFFCH reached as `[ebp-8]` with EBP = 4: the fault address is the 32-bit one; R8-R15 and XMM8-15 as the kernel left them in every fault's record | QEMU |
| `c32-sysenter` | SYSENTER out of 32-bit code, SYSRETL back with RCX's upper half set (only ECX is the return address), INT 0x80 with the SYSRETL selectors in its frame | QEMU |
| `c32-wrap` | `%gs:` with negative offsets (immediate and register); a GS base just below 4 GiB with offsets past it; `[ebp-12]` with EBP = 4; EBX = FFFF_FFF0H plus 40H; PUSH at ESP = 0; POP at ESP = FFFF_FFFCH and stores above the wrapped ESP, the first across a page (the resident's own store, made wrapped) | QEMU |
| `c32-syscall-ud` | SYSCALL from 32-bit code with SYSCALL enabled: #UD | model and QEMU |
| `c32-null-ds` | IRETQ into 32-bit code with DS and ES null that never uses them: registers, the stack (ESP and EBP bases, an SS override), a call, REP string instructions with a zero count, LEA, a DS prefix on a register form, INT 0x80 | QEMU |
| `c32-fs-null`, `c32-gs-null`, `c32-gs-null-string` | a load, a read-modify-write and a string source through a null FS or GS: #GP(0) | model (QEMU does not check) |
| `c32-ds-null`, `c32-es-null` | DS, ES loaded with null inside 32-bit code, which runs on (stack use; a DS access while only ES is null) until a load through DS, a string store through ES: #GP(0) there | model (QEMU does not check) |
| `c32-limit`, `c32-limit-ss`, `c32-limit-fetch` | a load, a stack-segment store and an instruction that run across 4 GiB: #GP(0), #SS(0), #GP(0) (natemu's choice; the model also permits the access, decision 7) | model (QEMU makes the access) |
| `c32-ds-based` | natemu's deviation: DS loaded with a based segment inside 32-bit code: #GP before the next instruction | model (natemu only) |

QEMU runs these as an Intel processor (`vendor=GenuineIntel`; a program may now name its QEMU CPU model), because its
default model follows AMD for SYSCALL and SYSENTER.

Existing cases that changed (`smp/system`): `seg-iret-compat16-cpl3` now expects the IRET to complete and the fault at
its target; `seg-iret-compat-cpl3` and `seg-cpl3-iret-compat` are gone (an IRET into 32-bit code with a null DS now
runs it, which `c32-null-ds` covers); new `seg-iret-compat-rip` (a return address above 4 GiB:
#GP(0) at the IRET by the manual; QEMU loads it and faults at the fetch, so the model judges);
`sc-sysretl` is gone (SYSRETL is in `c32-sysenter`).

Unit tests: the model's own (`tools/models/test_segment_model.py`, `test_syscall_model.py`); `compat_segments_that_run`
in the ABI crate (every refused shape, the four identity bits); the host's 32-bit request test (32-bit decode of a cut, the
null-segment cut and its arguments); the compiler's `test_32_bit_null_fs_gs` (19 accessing
forms cut, 9 non-accessing forms untouched, the other segment, 64-bit code, the DS default and the EBP/ESP bases that
are SS, ES string destinations, prefix order, SSE gating first) and four new
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
  rerun. `sigreturn_64`'s 32-bit part (a sigreturn into 32-bit code with a null DS) should now run as on hardware, so
  its entry may become an unexpected pass; `sysret_ss_attrs_64` uses a far return, which still faults (task 5).

## For task 3b

The shim executes these with 64-bit rules today; in 32-bit code each needs its 32-bit behaviour. None is needed by
task 3a's tests, which use register forms and the kernel side's 64-bit instructions.

- **The effective address of a memory operand** of any instruction the shim executes (segment loads and stores to
  memory, the x87 and SSE control and state forms, CLFLUSH, MASKMOVQ/MASKMOVDQU/VMASKMOV, the gathers, INS/OUTS, BT
  under alignment checking, memory RCL/RCR): 32-bit wrap of base + index +
  displacement and then of segment base + offset; the CS/DS/ES/SS base (0 while flat); **#GP(0) through a null FS,
  GS, DS or ES** (the compiler's gate covers translated instructions only; a shim-executed instruction with a memory
  operand is cut earlier, as a system instruction, and does not check); #GP/#SS at the 4 GiB limit instead of the canonical check.
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

## Appendix: the 4 GiB limit on the reference machine

AMD Ryzen 7 8845HS, Linux 7.0. A 32-bit Linux process cannot map the page just below 4 GiB, so the check ran from a
static 64-bit program that maps FFFF_F000H and 1_0000_0000H (page 0 left unmapped), far-jumps to the 32-bit code segment
0x23, loads DS with 0x2b and reads four bytes at FFFF_FFFEH, once as `mov eax, [0xfffffffe]` and once through SS
(`mov eax, [ss:esi]`). A control without the read returns normally.

```
DS:  SIGSEGV, trap 13 (#GP), error 0, at the load
SS:  SIGBUS,  trap 12 (#SS), error 0, at the load
```

So this processor raises the limit fault even with mapped memory on both sides. Intel hardware was not checked.

## Effort

Approximate, from the session that did the work (it has no clock): reading the plan, the findings and the code about
2.5 agent hours; the model about 0.5; resident, host and compiler about 2; the test programs, the runner entry and the
QEMU seeding about 2; unit tests, this page and review of the diff about 1. About 8 in all, against the plan's 18-30
for 3a; the null DS/ES gate and the review's fixes added about 2. The register save and the model were each a few lines; most of the saving came from reusing the existing
"gated instruction" path for the null FS/GS gate and the existing S2b test machinery for the new programs.

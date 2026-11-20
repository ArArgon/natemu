# M2 task 3b: the instructions the shim executes, in 32-bit code

November 20, 2026. Task 3b of [the M2 plan](m2-32bit-design.md), on [task 3a](m2-task3a-findings.md): the instructions
the resident executes itself (the compiler cuts a block before them) now follow 32-bit rules when 32-bit code runs
them. A small static 32-bit program now runs under the Debian kernel. 64-bit code behaves as before: every change
applies only when the code segment is not 64-bit.

## What was built

- **Memory operands** (`crates/runtime/src/execution/segments.rs`, `address`): in 32-bit code the offset is added to
  the base of the operand's segment, whichever segment it is (CS/DS/ES/SS are flat, so their base is 0, but the real
  value is used). The sum wraps at 4 GiB. An access through a null FS, GS, DS or ES raises #GP(0) when the address is
  computed, at the point where 64-bit code would raise a non-canonical fault. Every instruction the resident executes
  with a memory operand goes through this one function. Two forms add their own segment base, the gathers and INS/OUTS;
  they now call the same two helpers.
- **The 4 GiB limit:** an explicit access of 32-bit code that runs across 4 GiB raises #GP(0), or #SS(0) through SS.
  One helper (`segments::outside`) makes the canonical test and this one; `pages`, which every access passes through,
  and every check that comes before #AC use it, so the limit fault comes before an alignment-check fault. Implicit
  accesses keep 64-bit rules: the descriptor tables and the TSS, and the interrupt frame written when 32-bit code is
  interrupted (a 64-bit frame: it is written under the new code segment, even at privilege level 3, where an IST stack
  lies above 4 GiB). FXSAVE/FXRSTOR and XSAVE/XRSTOR check their whole operand (512 bytes, or the area XCR0 sizes)
  against the limit, though they touch fewer bytes in 32-bit code. OUTS with an SS override raises #SS(0).
- **Stack and instruction pointer:** ESP wraps at 4 GiB in PUSH/POP of segment registers, POPF, ENTER (its nesting
  reads through EBP too), far CALL and RET, IRET and the new PUSHA/POPA. The return address a far CALL pushes and the
  EIP an INT n frame holds wrap at 4 GiB, as does the instruction pointer after every instruction the resident runs.
- **IRET from 32-bit code** pops EIP, CS and EFLAGS. It pops ESP and SS only on a return to an outer privilege level,
  which cannot happen from privilege level 3. This is the SDM's same-level rule for IRET outside 64-bit mode. The
  16-bit form (IRETW, 66 prefix) pops 2-byte words.
- **Far transfers within 32-bit code:** JMP and CALL through an immediate pointer (`ea`, `9a`, decoded now) or an
  m16:32 operand, and RET far, run when the target is 32-bit code. A transfer between 32- and 64-bit code raises
  #GP(selector) at the instruction, the existing deviation, until task 5. IRET to 64-bit code at the same privilege
  level runs: it is an IRET, which task 3a's rule already lets switch modes.
- **The 32-bit-only instructions** (new module `execution/compat.rs`, kinds 0x53..0x58 in the ABI, decoded by the host
  outside 64-bit mode only):
  - ARPL: memory or register operand, 16 bits.
  - LDS/LES/LSS/LFS/LGS: the pointer is read, its selector loaded as MOV Sreg loads one, then the offset written.
  - BOUND: #BR when the index is out of bounds.
  - PUSHA/POPA: all eight slots are made as one checked access, so a fault changes no register and no memory.
  - AAM: #DE for a zero immediate.
  - INTO: delivers #OF like INT 4 when OF is set (it was always #UD).
  - POP SS sets the interrupt shadow that MOV SS sets.
  - PUSH/POP ES, CS, SS and DS use the existing segment push and pop at 4 bytes.
- **State images:** FXSAVE/FXRSTOR and XSAVE/XRSTOR in 32-bit code store and load XMM0-7, and the upper halves of
  YMM0-7, only. The fields for registers 8 to 15 are neither written nor read, so those registers keep the 64-bit
  side's values, which the bank keeps. The x87 images (FNSTENV/FLDENV in 28 and 14 bytes, FNSAVE/FRSTOR) needed no
  change. Their 32-bit protected-mode layout is also the one 64-bit code uses. Their pointer fields are 0 in both
  modes (I6 deviation 8), so no 32-bit FIP/FCS/FDP/FDS layout arises.
- **The segment model** (`tools/models/segment_model.py`): far JMP/CALL and RET in 32-bit code follow the same rules
  (4-byte slots, ESP wrapping, the frame across 4 GiB is #SS(0), the deviation between modes), and IRET from 32-bit
  code pops three words. Unit test `test_32_bit_far_transfers`.
- **The host decoder** (`crates/host/src/system.rs`): LDS..LGS, ARPL, BOUND, PUSHA/POPA, AAM and the immediate far
  pointers, with cases in `other_modes`.

## Decisions

Each one is the session's; all are reversible.

1. **One limit check, on the linear address.** The check is in `pages`, so every explicit access has it and no caller
   needed changing. For the flat CS, DS, ES and SS, the linear address equals the offset, so the check is exact. For
   an FS or GS with a base, the SDM applies the limit to the offset: an access whose offset is inside the limit but
   whose linear address wraps past 4 GiB raises #GP here, where x86 wraps it. The opposite case also differs: an
   offset past the limit whose linear address does not cross 4 GiB is accessed. The code marks this with a `ponytail:`
   comment. Translated code (task 3a) checks the same way.
2. **Multi-slot stack accesses are one access.** PUSHA/POPA, a far CALL's frame, and the pops of RET and IRET are each
   checked as one range. For RET and IRET that is the SDM's rule: their pseudocode checks the whole frame against the
   limit. For PUSHA and a far CALL, when ESP is so close to 0 that the frame would wrap across 4 GiB (ESP below 32 for
   PUSHAD), natemu raises #SS(0) where x86 makes each push at its wrapped address. No program puts ESP there.
3. **PUSH of a segment register at 32 bits stores the selector zero-extended,** as QEMU does and as the 64-bit forms
   already did. The SDM also allows a 16-bit store, which recent Intel processors make. Software cannot rely on either.
4. **ARPL with a memory operand always writes it back,** even when the RPL is unchanged, as QEMU does. A read-only page
   therefore faults either way. The access is walked for writing first (the existing read-modify-write path of the
   memory RCL/RCR), so the error code is the write's.
5. **AAM's undefined flags:** CF, OF and AF are cleared, as QEMU and FEX's logical-flag code do.
6. **IRET from 32-bit to 64-bit code at the same level runs, with three pops.** Far JMP/CALL/RET between the modes
   stays refused until task 5, as the plan says.
7. **LSS/LFS/LGS are decoded only outside 64-bit mode.** The 64-bit forms (REX.W m16:64 included) stay refused, as
   before. Lifting that is one guard plus a test, if 64-bit code ever needs it.
8. **The instruction pointer is also cut at the commit,** not only at the next lookup (task 3a). A single-step trap
   delivered between the two would otherwise push the uncut value.
9. **Fault order inside one instruction:** segmentation comes first. A misaligned access that also crosses 4 GiB raises
   #GP(0) or #SS(0), not #AC: the checks before #AC include the limit (decision of the review).

## QEMU's mistakes the tests avoid

- **INT n at the top of 4 GiB:** pinned QEMU pushes 1_0000_0000H as the return address of an INT 0x80 at FFFF_FFFEH.
  The SDM's value, 0, is compared instead (the runner's `sdm_bytes`). A 32-bit Linux process cannot map that page, so
  the reference machine cannot check it.
- **RF after IRET:** pinned QEMU clears an RF loaded by IRET only at the end of a translation block, so a software
  interrupt shortly after it carries RF = 1 in its frame. The tests' fault handlers resume with RF clear, except after
  an instruction breakpoint, where RF is needed.
- **No null-segment or limit checks:** as in task 3a, those cases are judged by the model and listed in
  `QEMU_DIFFERS`.

## Tests

All through the framework. The `system/compat32` case gained these programs (`tools/bench/smp/compat.py`; each runs at
the default block size and at one instruction per block):

| Program | What it shows | Judged by |
| --- | --- | --- |
| `c32-stack` | PUSH of each segment register over slots of all ones (4 bytes zero-extended, the 66 form 2), POP of each, including POP SS, and the 66 POP GS of the TLS segment, read through after; POPFD of every flag and the 66 POPF at CPL 3; PUSH/POP of a segment register and POPFD at ESP = 0 (FFFF_FFFCH); ENTER at ESP = 4 (EBP 0, ESP FFFF_FFF0H), with nesting levels 3 and 1, and the 66 form; PUSHAD/POPAD, the 66 forms keeping the upper halves, PUSHAD at ESP = 0 | QEMU |
| `c32-forms` | ARPL (memory and register, raised and not), LDS/LES/LFS/LGS (66)/LSS, BOUND in bounds (32 and 16 bits), AAM with its flags, INTO with OF = 0, selector stores and loads through memory (MOV, STR, SLDT, SMSW, LAR, LSL, VERR, VERW); CALL/JMP far through immediate and memory pointers, RET far with an immediate, a far call at ESP = 0, IRETD and IRETW within 32-bit code, IRETD to 64-bit code | QEMU |
| `c32-x87` | FNSTENV/FLDENV (28 and 14 bytes), FNSAVE/FRSTOR, FLD/FSTP m80; FXSAVE/FXRSTOR and XSAVE/XRSTOR (x87, SSE, AVX) leaving the fields of registers 8-15 as they were; XMM8-15 keep the body's values | QEMU |
| `c32-faults` | #BR from BOUND (32 and 16 bits), #DE from AAM 0, #OF from INTO, #BP from INT3 | QEMU |
| `c32-memfaults` | PUSHAD onto a page not present and POPAD from one, ARPL on a read-only page (each retried after the handler maps the page), POP DS and LDS of the kernel code selector: #GP(08H) with ESP and ESI unchanged | QEMU |
| `c32-step` | single-step after the resident's POP ES, PUSH SS, POPFD and after translated instructions; none after POP SS (its shadow); DR6 per trap | QEMU |
| `c32-breakpoints` | INT3; an instruction breakpoint (DR0) on the resident's PUSH DS; data breakpoints on the resident's store of DS (DR1) and on a translated store (DR2) | QEMU |
| `c32-int-top` | the resident's PUSH DS at FFFF_FFFDH, INT 0x80 at FFFF_FFFEH: the frame's RIP is 0 | QEMU, but the SDM's byte |
| `c32-fldcw-fs-null`, `c32-movsreg-gs-null`, `c32-lds-fs-null` | FLDCW, MOV m16 from DS, LDS through a null FS or GS: #GP(0) | model (QEMU does not check) |
| `c32-fldcw-limit`, `c32-fxsave-limit`, `c32-popfd-limit`, `c32-pop-ds-limit` | across 4 GiB: #GP(0), #GP(0), #SS(0), #SS(0) | model (QEMU does not check) |
| `c32-jmp-far-64`, `c32-retf-64` | far JMP and RET far from 32- to 64-bit code: #GP(20H) | model (natemu's deviation until task 5) |

The Debian guest: new case `boot/boot-i386` (`tests/boot/run_boot_smoke.py --only i386`). It boots the pinned Debian
kernel with the installer initrd, appends a small static 32-bit program to the initrd (`tests/boot/i386_static.asm`,
assembled by the runner with nasm), and runs it from busybox at 1 vCPU. The program checks its own results. It makes
system calls through INT 0x80 and through the kernel's SYSENTER entry (AT_SYSINFO), uses thread-local data through GS
after `set_thread_area`, PUSH/POP of segment registers, PUSHAD/POPAD, ARPL, BOUND, LDS, x87 (FSQRT, FISTP,
FNSAVE/FRSTOR), signals from #BR, #OF and #DE (the handler checks the saved EIP: the instruction's for the faults, the
next one's for INTO's trap) whose handlers step past the instruction through sigreturn, SIGUSR1 from
`kill`, and a far call to its own code segment. The case passes when the program prints its lines and exits 42. The
same binary prints the same lines and exits 42 on the x86 reference machine (AMD Ryzen, Ubuntu 26.04).

Results are in the task's report; the full gate has not run (the owner's order: review first).

## What checkpoint A needs

- Static and dynamic real programs (glibc i386 from Debian multiarch: `ld.so`, TLS through GS, `__kernel_vsyscall`),
  threads (`clone` with `CLONE_SETTLS`: a GDT TLS entry per thread), signals with `SA_SIGINFO` (rt frames, `ucontext`
  with the FPU state through FXSAVE in 32-bit layout) and x87-heavy code, at 1, 2 and 4 vCPUs. The first static program
  above is the base: the same initrd append can carry a Debian i386 busybox-static or glibc test binaries.
- The kernel self-test campaign's 32-bit programs (`*_32`, built with `-m32`; the recipe builds the 64-bit ones only),
  and the `sigreturn_64` and `sysret_ss_attrs_64` expectations task 3a left (the first may now pass; the second needs
  task 5).
- A recorded compile-request stream from a 32-bit boot for the replay check (task 2's request).
- The two limits above (decisions 1 and 2) if a workload meets them; neither is expected in Linux programs.

## Effort

Approximate, from the session that did the work (it has no clock): reading the plan, the findings and the code about
1.5 agent hours; the resident, the ABI and the host decoder about 2; the model and its test about 0.5; the system-family
programs and their seeding about 2; the Debian program and its case about 0.5; this page and checking the diff about
0.5. About 7 in all, against the plan's 11-20.

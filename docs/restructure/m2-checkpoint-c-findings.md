# M2 checkpoint C: 32-bit and 64-bit code in one process, and FS/GS limits

November 22 and 23, 2026. Checkpoint C of [the M2 plan](m2-32bit-design.md) (its task 5), built to [the checkpoint C
plan](m2-checkpoint-c-plan.md), on [checkpoint B](m2-checkpoint-b-findings.md) and the work after it. The final code is
natemu 670fab60 on branch `m2-checkpoint-c`, on top of `main` 035b243e. The plan holds the design, with an "As built"
list for each part; this page says what came out of it.

**Summary.**

- One user process can now run 32-bit and 64-bit code and move between them with far JMP, CALL and RET, as Windows'
  32-bit layer and some Linux test programs do. The kernel can return to 32-bit code with SYSEXIT. The two Linux kernel
  self-tests that were expected to fail for want of this, `test_syscall_vdso_32` and `sysret_ss_attrs_64`, are no
  longer listed as expected failures.
- 32-bit code that reaches memory through FS or GS now gets the segment's limit enforced, as x86 does. Linux's
  thread-data segment (a 4 GiB limit) needs no check and gets none, so ordinary 32-bit Linux programs run the code they
  ran before. A segment with a smaller limit, as Windows uses, costs about 0.4 ns per access.
- 32-bit addresses now wrap at 4 GiB exactly in a process that also has 64-bit code, without the slower code the first
  fix needed: 32-bit code runs under page tables that map only the first 4 GiB.
- Five bugs were found and fixed on the way, one of them in upstream FEX.
- Nothing on [the benchmarks page](../benchmarks.md) moved beyond its noise. Two new bench cases time FS/GS accesses
  and mode switches.

Benchmarks are from one Apple M4 (4 performance cores, 16 GiB), each case alone on the machine through
`tools/natemu-test run --tier bench`. Tests ran on M-series Macs through the framework; the x86 answers come from an AMD
Ryzen 7 8845HS under KVM.

## What was built

### Moving between 32-bit and 64-bit code

Far JMP, CALL and RET between flat 32-bit and 64-bit user code, in both directions. Each keeps the frame width of the
instruction that runs (a 32-bit far CALL pushes two dwords whichever mode it goes to) and checks the target against the
destination's code segment before anything is committed. The SYSEXIT form that returns to 32-bit code builds Intel's
fixed user code and stack segments. The independent segment and system-call models were extended first and judge the
fault cases. natemu's in-VM runtime picks the translated code for the destination mode at the next instruction, so a
32-bit and a 64-bit block at the same address are kept apart.

### FS and GS limits in 32-bit code

- **One check per instruction, in the translated code.** For an instruction that reaches memory through a limited FS
  or GS, FEX's compiler computes the operand's offset once and emits one check of the whole operand (offset and width)
  against a read-only copy of the segment's base and limit, at the point where FEX adds the segment's base. The access
  then uses the base the check returned. A failing check, or a segment kind the inline test does not handle, goes to
  natemu's in-VM runtime, which decides by the x86 rule and raises #GP at the instruction.
- **Two bits of the code's identity.** Each translated block is compiled for a known state of FS and GS: "FS limited"
  and "GS limited". A segment that is present, writable, expand-up and 4 GiB long (Linux's thread-data segment) is not
  limited, and code for it has no check at all. Every change of FS or GS goes through the runtime, which looks the code
  up again, so a block compiled without the check never runs under a limited segment.
- **Admission does not trust the compiler.** natemu's host decodes the x86 instructions independently and refuses a
  block if any FS or GS access lacks its check, or the check's width or read/write intent differs from the
  instruction's, or (since the code review) any FS or GS access computes its address without the segment's base. There
  is no list of instruction forms anywhere: the old per-instruction approach, built first, was replaced because it
  left every unlisted form unchecked.
- **Accesses that run across 4 GiB.** A 4-byte read at a segment's linear address FFFF_FFFEH reads two bytes at the top
  of memory and two at address 0 on x86. natemu completes such accesses exactly, for any instruction, at the native
  access that aborts there.

### 32-bit addresses in a mixed process

FEX folds small displacements into Arm's 64-bit address arithmetic, so a 32-bit address can come out with bits above
4 GiB. In a pure 32-bit process nothing is mapped up there: the access aborts and the runtime repairs it to the wrapped
address. In a mixed process the 64-bit code can map that high address, and the wrong access would succeed. The first
fix formed every displaced 32-bit address with a separate 32-bit add, which cost 12 to 14% on 32-bit code (below). The
fix kept is a third pair of page-table roots for 32-bit user code that maps only the first 4 GiB (sharing the 64-bit
roots' tables there), so every native address above 4 GiB aborts again, whatever produced it, and the runtime's repair
makes it exact. FEX's displacement folding and `main`'s PUSH/POP code came back, and 32-bit code without a limited
segment compiles byte for byte as on `main` (49 addressing, stack and FS/GS forms compared; an experiment, see the plan).

## Found and fixed

- **Seven vector forms ignored the segment base.** MOVD m32 (from an MMX or XMM register), VMOVD m32, MOVNTPS, MOVNTPD,
  MOVNTDQ and MOVNTDQA through FS or GS accessed the address without the segment's base, in every mode, so 64-bit code
  and Linux's thread data were affected too. This is upstream FEX's code; the bug is still in upstream FEX's main
  branch (bce85d994, November 20, 2026). Fixed in natemu's FEX fork, and admission now refuses any 32-bit FS/GS access
  that does not use the base.
- **SHLD and SHRD by CL left bits above 31** in the 32-bit destination register, which a following memory access
  without a displacement then used whole. Fixed in the fork; a static test scans the translated code of 168 single
  instructions and FEX's 245 32-bit tests for any write that could leave such bits.
- **Upper register halves after a same-level 64 to 32 transfer.** After a far JMP, CALL or RET from 64-bit into 32-bit
  code, the runtime's own accesses at the first 32-bit instruction used the full 64-bit RSP: POP DS, POPA or POPFD
  there took a stack fault when RSP was above 4 GiB. A transfer that ends in 32-bit code now clears the upper halves.
- **fexc crashed** on a floating-point instruction through a limited segment when an x87 or SIMD exception was
  unmasked: its precision pass took the check for a commit. Found by the new pending-#MF case.
- **fexc's native test harness truncated its buffer's address** to 32 bits once 32-bit addresses were formed in 32
  bits, so the x87-commit test died. The harness, not the compiler, was wrong.

## Deviations and differences recorded

- **An overrun at the end of a based 4 GiB segment.** With the "limited" bit clear nothing tests the offset, so an
  operand that starts closer to the end of a 4 GiB segment with a nonzero base than its own size (`mov eax,
  gs:[0xfffffffe]`) wraps to the bottom of the segment where x86 raises #GP. `main` and QEMU do the same. Closing it
  would put the check on every thread-data access of every 32-bit Linux program. The case `fs-generic-flat-limit-offset-overrun-deviation` pins it.
- **REPE/REPNE CMPS that faults part-way.** natemu restores the flags from before the instruction, as the Intel SDM
  says it must; the AMD reference machine keeps the last comparison's flags. natemu presents an Intel processor, so the
  SDM decides; the AMD answer is recorded beside the case (`fs-string-repe-fault-flags`).
- **Read-only, expand-down and code-segment FS or GS** are decided by the runtime on every access, about 183 ns each
  (an experiment on an M3 with the earlier per-access check, [the inline A/B note](m2-checkpoint-c-inline-ab.md)). No
  known program uses such a segment. A performance gap, not a flaw.

## Tests

All in existing families, through `tools/natemu-test`:

- `tests/unit` `rust-fs-sweep`: every 32-bit memory form through a limited FS or GS, in several addressing forms and
  with floating-point exceptions unmasked (55,531 translated units), is either admitted with its check or refused; none
  is translated only without the check. The only refusals, VPCMPESTRI/VPCMPESTRM with VEX.W = 1, are refused with or
  without the check and predate this work. Admission's mutation tests show each refusal of the rule.
- `tests/system` `compat32-resident-limits`, `compat32-smp-limits` (different limits per vCPU at the same instructions,
  2 and 4 vCPUs) and `mixed-far` (the far-transfer forms in both directions, faults in each mode, SYSEXIT, stepping
  across the transfers, the high-RSP cases). The mixed-process address cases (`mixed-user-fold`, `mixed-user-shld`,
  `mixed-user-shld-6g`, each with the wrong high page warm from 64-bit code) were shown to fail on the builds before the
  translation root.
- `tests/smp` `compat-kvm`: the AMD processor's answers for 149 FS/GS operand cases and the earlier compatibility
  cases; natemu's expectations match every one.
- `tests/static` `fexc-address32` (the folded operands, the unchecked forms' code, the upper-half scan) and
  `fexc-asm32`. 64-bit code is unchanged except for the FS- and GS-prefixed forms of the seven fixed vector instructions
  (`fexc-replay` at the translation root's commit: no difference over 54,946 requests).
- `tests/correctness`: the two kernel self-tests' expected-failure entries are removed, so they must pass.

## Gate and campaigns

**Placeholder: the orchestrator adds the gate and campaign results here when they finish.**

## Performance

### Existing benchmarks: no change (A/B)

Bench cases `guest-zstd`, `guest-luajit`, `guest-mem`, `guest-7zip`, `guest-hackbench` (1 vCPU) and `guest-paper` (4
vCPUs); Apple M4; `main` 035b243e against 670fab60, three alternating rounds, November 23, 2026. Seconds, the range of
the three rounds (the Paper start: its first start, two rounds for `main`, whose first round's untimed setup boot did not
finish):

| Case | `main` 035b243e | 670fab60 |
| --- | --- | --- |
| `guest-zstd` | 2.660 to 2.701 | 2.670 to 2.696 |
| `guest-luajit` | 3.022 to 3.074 | 3.041 to 3.074 |
| `guest-mem` | 7.449 to 7.587 | 7.481 to 7.562 |
| `guest-7zip` | 3.624 to 3.641 | 3.628 to 3.652 |
| `guest-hackbench` | 10.986 to 11.328 | 11.083 to 11.670 |
| `guest-paper` | 24.07, 24.35 | 24.25 to 24.49 |

No difference is beyond the spread. A second A/B, `guest-hackbench32` and `guest-hackbench`, `main` against 86859a99
(the same natemu code as 670fab60) and against a variant that published the FS/GS records only on entry into 32-bit
code, three rounds each, found none either; the variant was dropped (670fab60 reverts it).

The benchmarks page's numbers (natemu 9f35eb96, November 22) read 2 to 4% faster than today's on some 64-bit cases
(`guest-mem`, `guest-hackbench`, `guest-zstd`). `main` measured the same as 670fab60 today, so this is drift between
days on the machine, not this work; the page keeps its numbers.

### 32-bit code: what the first address fix cost, and how it was removed (A/B)

Bench cases `io5-solo32` (the i386 CPython build, 4 vCPUs), `guest-sqlite32` and `guest-startup32` (1 vCPU); Apple M4;
three alternating rounds of `main` 035b243e, 30793c6b (the first fix, a 32-bit add per displaced address) and 5e028c91
(the branch with it, before the translation root); November 23, 2026. Seconds:

| Case | `main` | 30793c6b and 5e028c91 |
| --- | ---: | ---: |
| `io5-solo32`, i386 `make -j4` | 109.0 | 122 to 123 |
| `guest-sqlite32` | 7.97 | 9.03 to 9.08 |
| `guest-startup32` | 7.31 | 8.21 to 8.30 |

`io5-solo`, the 64-bit build, did not change. With the translation root and folding back, one round at 86859a99 read
109.2, 7.94 and 7.31 s (one round, not an A/B), and the bench sweep at 670fab60 109.3, 7.93 and 7.28 s, `main`'s
numbers.

### New benchmarks

`compat-tls-natemu` and `mixed-far-natemu` are on [the benchmarks
page](../benchmarks.md#32-bit-code-fs-and-gs-accesses-and-switching-modes) (Apple M4, natemu 670fab60, November 23,
2026). In short: through a 4 KiB-limited FS or GS an access costs about 0.56
ns against 0.17 ns through flat DS, so the inline check adds about 0.4 ns; through Linux's 4 GiB FS the dword loop is
level with flat DS and the mixed loop about 15% slower (that FS code is main's: the base addition, no check). A far transfer between the modes costs at most about 450 ns.
Neither can run before this work, so they are first-correct baselines. That a clear "limited" bit costs nothing rests
on the byte comparison with `main`'s compiler and on the 32-bit numbers above matching `main`'s.

### Experiments

- The resident check of a read-only or expand-down segment: about 183 ns per access, measured on an Apple M3 with the
  per-instruction check built first ([the inline A/B note](m2-checkpoint-c-inline-ab.md)). Not a bench case.
- The byte comparison of 49 forms with `main`'s compiler (in the plan's section 1, "As built").

## Left open

- **Non-flat CS, DS, ES and SS** (a code or data segment with a base or a limit) still raise #GP in 32-bit user code.
  That is task 6 of the M2 plan, deferred until a workload needs it (16-bit Windows programs under Wine, DOS emulators).
- **Read-only and expand-down FS or GS are slow** (the runtime decides each access). A gap; one more record per access
  kind would close it, and nothing known uses them.
- **INT 0x80 has no fast path**: `hackbench32-int80` is still level with QEMU.
- **The cost of mode switches in a real workload is unmeasured.** The far transfer itself is timed (`mixed-far-natemu`),
  but no stock program switches modes, and QEMU's counterpart case (`mixed-far-qemu`) has not run.

## Review

The plan, the design of the generic FS/GS check and the design of the 4 GiB translation root were each reviewed
independently before they were built. The translation root's first version (two small unmapped windows) was
rejected: its review showed that the upper halves of 32-bit registers cannot be relied on. The revised design was
reviewed again before it was built. The code had an independent review, which found the seven forms
without a base and the first version of the upper-half scan's blind spots. Each finding was resolved by a fix or a test
before the gate.

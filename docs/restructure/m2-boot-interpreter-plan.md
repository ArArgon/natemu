# M2 Part 2: the boot-time 16/32-bit interpreter — plan

Status: built, November 22, 2026, on branch `boot-interp` (from `m2-32bit` at 78f60965). It develops Part 2 of
[the M2 design notes](m2-32bit-design.md) and takes the 32-bit survey of [the S3g plan](s3g-plan.md) as input. Two
independent reviews, three rechecks and an independent replication of the upstream census were folded in before the
build (section 12); the results of the build are folded into each section ("Built"), and the decisions taken are in
section 11. B0-B5 are built; B5's full gate and benchmark round on the final tip: see section 12. B6 stays deferred.
Results, all framework cases on the M2 Max:

- the interpreter matches QEMU 11.1.2 and KVM on all 1,716 per-instruction cases in R, P16, P32 and C32 (`tests/interp`
  `natemu`), and the models on 306 segment and 21 mode-transition fault cases (`natemu-faults`);
- CPU start-up by INIT and SIPI: the model's scenarios (`tests/system` `initsipi*`), Debian's kernel with the wake-up
  mailbox left out at 2 and 4 vCPUs through CPU offline/online and shutdown (`tests/boot` `boot-startup`),
  kvm-unit-tests' smptest and dummy at 1-4 vCPUs (`kut-natemu`), the exit test's AP program at 1, 2 and 4 CPUs
  (`exit-ap-natemu`);
- the M2 exit test, our firmware from the reset vector at 1, 2 and 4 CPUs (`exit-fw-natemu`), and OVMF's reset vector
  to SEC from both entries (`ovmf-natemu`);
- no cost to a running 64-bit OS: the hot paths are identical by disassembly (an experiment, not a framework case; M2
  Max) but for `helpers::x87`, which keeps one outlined call per `svc #X87` (the code-generation units' partitioning,
  [interp-transitions.md](interp-transitions.md)), and, with the firmware boot mode, a register rename in
  `segments::copy` and the page walker inlining its table read ([firmware-mode.md](firmware-mode.md) section 6); and the
  system-call bench cases are no slower: at the final tip against 78f60965, `guest-hackbench32` −4.5% and
  `guest-hackbench32-int80` −5.1%, beyond the noise and from the plumbing commits (d7bc0885 to be626aa8),
  `guest-hackbench` −0.8%, within it ([benchmarks](../benchmarks.md)).

Design notes of the build: [interp-integration.md](interp-integration.md), [initsipi-resident.md](initsipi-resident.md),
[interp-transitions.md](interp-transitions.md), [firmware-mode.md](firmware-mode.md); test contracts:
[interp-cases.md](interp-cases.md), [m2-exit-firmware.md](m2-exit-firmware.md),
[real-code-suites.md](real-code-suites.md).

Marks: **Decided** (with who decided), **Recommended** (this plan's choice where nobody needed to decide),
**Inferred** (not checked in code, upstream source or by a run).

## 1. What it is for, and its scope

The roadmap's item: "narrow boot-time 16- and 32-bit execution", correctness only, no performance expectation, needed by
M3 (firmware) and M4 (Windows' CPU start-up), with an M2 exit test running a firmware-style reset vector to 64-bit mode.
The interpreter runs only what the translator cannot take; 32-bit user code under a 64-bit kernel stays translated (M2
Part 1) and never reaches it.

**Modes:**

- **R:** real mode (CR0.PE = 0), with the hidden descriptor cache: fetches at reset go through CS base FFFF0000h, not
  selector × 16; big real mode comes with the cache (unused by the surveyed paths).
- **P16:** protected mode, paging off, 16-bit code segment: only the instructions every stub runs between setting PE and
  its far jump under the stale real-mode CS (that far jump; kvm-unit-tests' `lgdtl`).
- **P32:** protected mode, paging off, 32-bit code segment, CPL 0.
- **C32 (and C16) at CPL 0:** compatibility mode under 4-level paging, the few instructions between enabling paging and
  the far jump to 64-bit code, and back.

**Instruction scope (decided by the session; the owner left it to it, November 21: "i don't decide what an os needs";
the aim is just enough to boot an OS).** The first version implements what real start-up code uses, from the census
(section 9, independently replicated), and nothing for completeness. Everything else decodes (the decoder and record
cover every instruction) and is a typed stop: the VM stops with the diagnostics report naming the instruction bytes,
mode and state. Each stop a real boot hits becomes a case and a small addition. Most firmware and boot-loader code
never reaches the interpreter: under x64 UEFI the firmware after SEC, every boot loader (GRUB's x86_64-efi build,
systemd-boot, Windows' boot manager and `winload.efi`, FreeBSD's `loader.efi`) and the hand-off to the kernel run in
64-bit mode, through the translator.

| Group | Forms in the first version | Used by |
| --- | --- | --- |
| Data movement | MOV r/m↔r, MOV r←imm, MOV r/m←imm (C6/C7), MOV with an absolute address (A0-A3), MOVZX, LEA | all (C6/C7: OVMF's page-table build) |
| Arithmetic and logic | the eight ALU operations (ADD OR ADC SBB AND SUB XOR CMP) in all encodings, TEST, INC/DEC (with the one-byte 40-4F forms), SHL/SHR/SAR | all |
| Bit operations | BT/BTS/BTR/BTC with an immediate or a register, including the memory bit-string form | OVMF, EDK2, Linux |
| Atomics | LOCK BTS (Linux's trampoline lock), LOCK XADD (kvm-unit-tests' AP stack claim) | Linux, kvm-unit-tests |
| Control flow | Jcc (all 16), JMP short and near (relative), LOOP, CALL near (relative), RET near | all |
| Far transfers | far JMP ptr16:16 and ptr16:32, far JMP m16:32, far RET (32-bit) | all |
| Stack | PUSH/POP register, PUSH immediate, PUSHF/POPF | Linux, the PVH entry, kvm-unit-tests |
| Segments and tables | MOV to and from segment registers, LGDT/LIDT (16- and 32-bit operand size) | all |
| Control registers, MSRs | MOV to and from CR0, CR3, CR4; RDMSR/WRMSR (EFER, IA32_MISC_ENABLE and IA32_MTRR_DEF_TYPE in real mode, GS_BASE in 32-bit mode: the existing MSR transaction); CPUID | all |
| Other | CLI, CLD, HLT, PAUSE, WBINVD (no effect), NOP including 0F 1F | various |
| Prefixes | 66, 67, segment overrides, LOCK | all |

Where real code uses one member of an opcode group, the whole group comes along when it shares the decode and flag
code (all eight ALU operations, all sixteen conditions, the three shifts); members with semantics of their own do not.
Cheap candidates to add the first time anything needs them: SETcc, NOT/NEG, MOVSX.

**Out of the first version (typed stops):** exception and interrupt delivery in interpreted modes (section 6); port I/O
and device memory from interpreted code (only OVMF's debug builds write a port); MUL/DIV, BCD, rotates, string
instructions, PUSHA/POPA, ENTER/LEAVE, XCHG, segment push/pop, far CALL, INT n, IRET, STI, LMSW/SMSW, debug-register
moves, LLDT/LTR; x87, MMX, SSE; gates, task switches, privilege changes, CPL 1-3; 16-bit protected code beyond the
stubs' far-jump stretch; legacy and PAE paging without long mode; SMM, S3 resume, legacy BIOS or CSM boot; A20 masking; IRET from 64-bit code into CPL-0 compatibility mode.

What this gives up: kvm-unit-tests' `realmode.flat` leaves the first version (it needs BCD, MUL/DIV, IVT delivery, a
PIC interrupt, port I/O and x87 status, and a stop ends its whole run); its SMP tests stay. Windows' CPU start-up stub is
not public, so its gaps appear in M4 as stops.

## 2. Where it runs

**Recommended: the resident executes; the host decodes once per instruction (or per straight run).** The resident
owns everything the interpreter touches: the CPU state (`State.cpu`), the local APIC (`execution/lapic.rs`), pending
events, the CPU start-up wait (`events::wait_mailbox`), the software page walk and the EL1 alias of all guest RAM
(`segments::linear`/`copy`, `GUEST_RAM_VA`). It already caches host-decoded system instructions as `SYSTEM` records keyed
like blocks and runs them with no host exit (`cache::place_system`; `find` → `system()` → `transaction()` →
`commit()`), and it runs under Apple's TSO bit (set by the host, `crates/host/src/vm.rs:445`; checked at EL1,
`crates/runtime/src/main.rs:76`; that it orders the resident's own accesses is **Inferred**: no case checks it, since
the litmus test of section 8 was not built).

What does not carry over:

- **Decode is tied to fexc today:** the host decodes a system instruction only after fexc cut it (`translation()` →
  `compile` → `Answer::Cut` → `system::decode`, `crates/host/src/execution.rs:3257`), for bitness 32 or 64, and fexc has no
  16-bit mode. The interpreter gets **a new request, `REQUEST_DECODE`,** that bypasses fexc, the answer cache and
  admission. Built (`natemu_abi::interp`, request 0x37): the resident supplies the bytes it fetched through its own
  fetch checks, with CS.D; the host decodes them with iced alone (it reads no guest memory) and replies in the staging
  area (not in `State`, whose copies on slow paths are rationed: `execution.rs:101, 915`) with one record or a straight
  run of at most 16, each carrying its bytes; the resident reads the reply in place.
- **The resident is small:** its code window is 256 KiB (`crates/abi/src/resident.rs`: text 0xa0000-0xe0000, enforced by
  `crates/runtime/linker.ld`); an existing build uses about 225 KB of it (experiment: `llvm-size` of the main checkout's
  November 20 build). It is built for size, without jump tables (`.cargo/config.toml`), under a static stack check
  (`tests/static/check_resident_stack.py`). **Decided (owner, November 21): the window may grow as much as needed.**
  Done (B1 (a)): the window is its own 2 MiB block, 0x200000-0x400000 (one level-2 block); at 78f60965 it held 239,560
  of 262,144 bytes, and the stack check's gate path had 240 bytes of headroom.

**Decided (owner, November 21: "do everything in software ... i just want it to be simpler"): EL1, with the
software memory path.** Translated code runs at EL0 (`crates/runtime/src/gates.S:108`) over the shadow mappings, which
are EL0 mappings (`crates/abi/src/shadow.rs:59`). An interpreter at EL0 would hand paging, permissions, device pages,
code-write detection and atomics to that machinery, but most interpreted code runs with paging off, which has no
shadow space; uncached power-on RAM is not shadowed (`execution.rs:3638-3645`); and EL0 cannot check a page before
storing, so multi-store instructions would need buffering. The executor core is written over a memory trait, so an
EL0 backend stays possible if a workload ever needs speed.

Alternatives rejected:

| Option | Against |
| --- | --- |
| Resident decodes too | iced-x86 in the resident breaks the no-registry-dependency rule for the resident; a hand-written decoder was already rejected; its tables would crowd the window. |
| Interpreter on the host | The APIC, events, INIT handling and the CPU state are the resident's: state copies at every switch, a coherence protocol with running vCPUs, host stores into guest RAM under the code-write protocol, host threads not TSO. Loses on the Linux trampoline's cross-CPU lock and on INIT. |
| Extend FEX to these modes | Below. |

**Interpreter or FEX: most of the work is the same either way.** FEX never executes system instructions (fexc cuts
every block before one), so both designs need the resident's 16/32-bit system instructions, segmentation, mode
transitions and hand-off, INIT/SIPI, the power-on state, the firmware entry and the per-mode tests. Only the ordinary
instructions differ. FEX's share would be a 16-bit mode it does not have (inverted size prefixes, 16-bit ModRM forms,
IP wrap), a run-time segment base and limit on every access (the design notes priced the 32-bit version alone, task 6,
at 15-23 hours), 32-bit code at CPL 0 (fexc refuses `--cpl0` with `--bitness 32`) and identity shadows, all as permanent
fork changes upstream never exercises, for speed boot code does not need (a climb is some tens of thousands of
instructions, almost all of them OVMF's page-table loops). A flat P32 path through the existing translator stays a later
option for long 32-bit stretches.

## 3. Records, executor and segmentation

- **Record:** an interpreter record in `crates/abi` beside `System`: our own operation number (not iced's `Code`; the
  resident stays registry-free), operand and address size, segment, up to three operands (register; memory with
  base/index/scale/displacement in 16- or 32-bit form; immediate), LOCK, length, condition code, and the instruction
  bytes. Nothing IP-absolute: branch targets stay relative; IP wrap and the CS limit are checked at execution. The host
  maps iced's decode to it; a unit test runs the mapping over every iced code it accepts. In interpreted modes the
  existing `System` kinds also get the new validated record (section 5), and keep their transactions. The bytes are the
  snapshot the host decoded, returned in the reply, never re-read by the resident (a torn read would be cached as
  valid); a #UD record carries every byte that decided it (up to 15); fetch faults and truncated decodes are not cached
  (a reply of 0 records is a truncated decode: the resident raises what stopped its fetch and caches nothing).
- **Executor core in `crates/abi`, as pure functions over a memory trait** (as `paging.rs` and `lapic.rs` already are):
  ALU and flags, effective address, segment limit and rights checks. It is unit-tested on the host and linked into the
  resident, which supplies memory and events. Built: `exec::step(cpu, Context, &Insn, &mut impl Memory)`, checked
  against 5,408 vectors from the reference machine's Ryzen 7 8845HS (`tools/models/interp_vectors_hw.c`).
- **Undefined flags** (for example OF after a shift by more than one, AF after a logic operation): one fixed documented
  choice, masked in comparisons (the implementation plan's rule): AF is 0 after logic operations and non-zero shifts;
  a shift's OF follows the one-bit rule and its CF is the last bit out, zeros past the width; BT* leave OF, SF, AF and
  PF unchanged.

**Segmentation is a capability of the VM, not of generated code.** Translated code stays flat-only.

- **Load-bearing for booting:** real-mode segmentation (base = selector × 16 after a load, DS = CS, 64 KiB limits,
  #GP/#SS at the limit); the hidden descriptor cache (`Segment` holds selector, attributes, limit and a 64-bit base, and
  `cpl` is separate, so the reset CS and the stale real-mode CS after PE are representable); descriptor loads with their
  checks (LGDT, MOV Sreg, far JMP/RETF into flat protected and long-mode segments).
- **Not load-bearing for booting:** non-flat protected-mode segments; none of the surveyed stubs uses one.
- **In a booted OS they appear at CPL 3** (Win16 and Wine's 32-bit FS under Wine, dosemu2, Windows' 32-bit FS limit,
  Linux's 16-bit stacks). Today natemu honours FS and GS bases without their limits and stops every other shape at CPL 3
  with a recorded #GP deviation. **Decided (owner, November 21: low return on investment): the interpreter as their
  exact fallback (B6) is deferred; the #GP deviation stays.** If it is ever built: behind an option, off by default until
  the kernel's `ldt_gdt` and `sigreturn_32` self-tests pass, preferably on an EL0 backend (hardware-enforced user page
  permissions), with the risks recorded in the November 21 discussion (silent wrong results instead of a loud kill;
  guest privilege isolation; SMP liveness; speed).
- **Hardware segment windows** (the owner's idea, November 21): mapping each non-flat segment as a window in the native
  address space's x86-non-canonical part (room exists: the private region sits at TTBR0 L0 index 256 and the rest of the
  gap is unmapped, `crates/abi/src/shadow.rs`), so the MMU enforces the limit. It does not pay for the interpreter
  (paging is off, real mode reloads segments constantly, byte-granular limits and the SDM's fault order need software
  checks anyway). It is the design lead for fast translated non-flat code (the design notes' task 6) if a workload ever
  asks.

## 4. Memory access

**What exists (the resident's software path, used by every system instruction today).** `segments::linear` → `copy`
→ `pages` (`crates/runtime/src/execution/segments.rs:273-411`): it checks the range, walks every page the access touches
with the x86 walker (setting A and D by compare-and-swap as the CPU does, giving a #PF with the exact error code and
CR2), fences if a page is uncached and waits while another vCPU holds a split lock on those pages (`split_wait`). Only
then does any byte move: a write into a page holding translations invalidates them first (`code_write`), then
`copy_in` stores through the EL1 alias of guest RAM (an aligned 2-, 4- or 8-byte access as one single-copy-atomic store,
`crates/abi/src/guest_memory.rs:121-145`). It is exact because every fault is decided before any effect, and simple
because nothing faults natively mid-instruction and no mapping state needs keeping coherent.

**The interpreter's memory model, in five layers:**

1. **Address:** effective address from the record, wrapped at the address size (16 or 32 bits); the segment check
   against the hidden descriptor (limit, expand-down, read/write/execute rights; real mode uses the hidden limit too),
   #GP(0) or #SS(0) for SS-based accesses; linear = base + offset, wrapped at 32 bits in protected mode (no A20 masking,
   so real mode reaches up to 10FFEFh). Real mode checks the hidden limit alone, as expand-up.
2. **Linear to physical:** paging off, the identity: after the segment check, one native load or store at
   `GUEST_RAM_VA + linear` through the resident's fixed alias (the resident runs with the Arm MMU on, `gates.S:6-17`;
   the alias maps the whole guest-physical span, `crates/abi/src/execution.rs:149-205`), so the hardware does the
   translation and nothing needs upkeep. Compatibility mode: the existing 4-level walk. Implicit accesses (GDT, IDT,
   TSS) are supervisor accesses through the same path.
3. **Physical to backing, classified in software (decided, owner, November 21: simplicity first):** guest RAM; the
   firmware region (section 7: reads and fetches; writes ignored, as x86 ignores writes to ROM); unassigned addresses,
   including the legacy hole 0xA0000-0xFFFFF, read all-ones and ignore writes (the existing L3 rule); device pages are a
   typed stop in the first version. A few compares (`ram_contains`) decide it before anything commits. Making non-RAM
   pages inaccessible and serving them from an abort handler was rejected: the resident has no recoverable EL1 abort
   (any exception it takes halts, `gates.S`), and a re-entry point inside its Rust code would add control flow to save
   a few compares. (User-space emulators use the host MMU and a SIGSEGV handler because they have no devices and the OS
   gives them a recoverable fault path; QEMU's system mode checks a software TLB with MMIO flags on every access, the
   model followed here.) The alias's unmapped areas stay a safety net: an interpreter bug there halts loudly.
4. **One instruction is check, then commit.** Check computes every address the instruction will touch (operands; the
   stack slots of PUSH, POP and far RET) and runs layers 1-3 for each, before anything changes: a fault leaves memory
   and registers as they were (and is reported in a typed stop, section 6). Commit performs the accesses in program
   order and updates registers, flags and IP.
5. **Between CPUs:** the same physical memory translated code uses, with the same guarantees:
   - order: the TSO bit (**Inferred** that it covers the resident's accesses at EL1; not checked by a case);
   - single-copy atomicity: aligned 2/4/8-byte accesses as single Arm accesses, and a misaligned one inside 16 bytes as
     one unaligned Arm access, atomic within 16 bytes on Apple's cores (FEAT_LSE2; an unaligned `casal` inside 16 bytes runs at EL1: self-test `p32-lock-unaligned`),
     not `copy_in`'s byte-by-byte fallback; crossing 16 bytes may tear, the deviation translated code already has;
   - LOCKed read-modify-writes: Arm LSE atomics on the alias; crossing 16 bytes, under the VM's split lock as translated
     code does (`execution.rs:4334`); every access joins `split_wait`;
   - code: the interpreter's stores into pages holding translations go through `code_write`; other CPUs' stores into
     code the interpreter runs are caught by byte validation (section 5).

**The exact checks live only in the interpreter's memory layer (review, November 22).** The shared helpers the
resident uses for 32-bit user code (`segments::address`, `linear_of`, `outside`, `wrap`) add a base and check only the
4 GiB edge, ignoring FS and GS limits as translated code does (the comment at `segments.rs:248-262`). Making them exact
in place would make a 32-bit program with a small-limit FS fault in resident-executed instructions while the same
access in translated code succeeds. They keep today's behaviour.

Per access the cost is a few calls, a walk in compatibility mode and `copy_in`'s barrier after each store; whether that
`dsb ish` is needed for RAM under the TSO bit was not checked (**Inferred** not; the barrier stays).

**Memory type at power-on (decided by the session; the owner asked for "whatever reasonable", November 22): unchanged
in M2.** With IA32_MTRR_DEF_TYPE.E = 0, the reset value, `mtrr_type` makes all RAM UC (`crates/abi/src/cpu.rs:109`),
and natemu honours UC (the resident fences each access; translated code on UC pages runs stepped). This costs nothing
in M2: the exit-test firmware's BSP and AP stubs both enable the MTRRs as their first step (a never-started AP has the
reset value), and CPUs started by SIPI under an OS keep the MTRRs the OS set (INIT leaves them). Keeping it also leaves
every running OS as today (Linux clears E briefly while it reprograms the MTRRs). The alternative, MTRRs-disabled RAM as
write-back (a recorded deviation; **Inferred:** what KVM does), is decided in M3 against a bench case of OVMF's 64-bit
code before it programs the MTRRs.

## 5. Classification, entry, far transfers and cached records

**`code_mode()` and `code_context()` stay untouched.** Every place that reads them (block lookup, `enter`, `bank`,
`sys_commit`, `access_aborted`, the split path, `linear_of`/`wrap`/`outside`) runs only in translatable state, where
`Cpu::paging()` already required EFER.LMA (`crates/abi/src/execution.rs:2720-2733`). The interpreter never calls
`code_context()`: its records use their own context (below). The 64-bit and 32-bit-user code identities therefore stay
bit-identical by construction.

**A separate classification** (`Cpu::interpreted_mode`: R, P16, P32, C16, C32, or none) serves the interpreter, from
CR0.PE, EFER.LMA, the hidden CS.L/CS.D and the CPL. **The interpretable predicate is "EFER.LMA = 0, or CPL 0 with a
non-64-bit code segment"**, which is never true for CPL-3 code under a 64-bit OS. Every widened rule anywhere in the
resident is gated on it, never on "not 64-bit code": the existing code already reads `code_mode() != Bits64` as "32-bit
user code" (`same_mode`, `far_return`, `wrap`, `outside`, `linear_of`), so a rule under that test would land on user
code.

**Entry only from the cold points where state can become interpretable,** and only once a transaction has completed.
State changes only inside a system transaction, so:

- in `commit` and `set_cpl`, the arm of `controls()` that halts today records "interpretable" and returns instead.
  `commit` calls `controls()` before it completes the instruction, flushes and advances RIP (`execution.rs:3223-3232`),
  and `set_cpl` runs mid-transaction (inside `far_return`, `segments.rs:825`), so entering the interpreter there would
  leave the transaction half-committed. The entry happens at the next boundary, after the transaction has completed,
  with no new test on any hot path (built, after review): `interpret::record` sets `debug::INTERPRET` in `State::debug`
  (a DR6 bit no trap uses, inside the pending-trap mask), so the existing trap tests divert the vCPU (`sys_fast`
  declines on `State::debug != 0`; `find_from`'s loop looks at the trap bits first; the cold `trap_due` hands off to
  `interpret::boundary`, which runs the interpreter and returns to that loop, so no recursion and a shallow stack). A
  first version tested on `system()`'s exit, which is on the fast system-call path, and nested the interpreter under
  `system`'s frame;
- `start()`, and INIT taken in the boundary prologue, enter directly; `refuse_compatibility`'s CPL-0 arm (B4) records
  "interpretable" like `commit`;
- `translate`, `pages` and `fetch_walk` cannot reach the failing arms in translatable state, so there the arm stays a
  halt (an assertion of the invariant).

The arms of `controls()` that return for 64-bit and flat 32-bit user state stay exactly today's two match arms, and
`controls()` stays inline: the fast system-call path reaches it (`sys_commit` → `system` → `commit` → `controls`, and
SYSCALL/SYSRET also call `set_cpl`, `execution.rs:854`, `syscalls.rs:100-174`), and an out-of-line `controls()` was the
cause of the task 1 regression (`ec84a1e6`). `find`'s hit path is unchanged (it tests only CS.L, `execution.rs:211`).

**Far transfers in interpreted modes are the interpreter's own code (decided, owner, November 22).** The far JMP and
far RET forms of section 1, in R, P16, P32 and C32 at CPL 0, are executed by the interpreter: a real-mode load sets CS
to selector × 16 with no descriptor; a protected-mode one uses the existing descriptor helpers (`descriptor`, `fields`,
`accessed`, `far_target`, `load_cs`), which return `Result<_, Exception>` and commit nothing on `Err`, so the
protected-mode checks stay in one place. A transfer that leaves interpreted state (C32 → a 64-bit CS with LMA set)
hands the vCPU to the translator after the transaction completes. The far-transfer functions 64-bit kernels and
32-bit user programs use today (`same_mode`, `far_transfer`, `far_return`, `segments.rs:619-740`) stay byte-identical, so
the recorded #GP(selector) for user-mode far transfers between 32- and 64-bit code is not touched. The one existing edit
is `refuse_compatibility`'s CPL-0 halt arm (`segments.rs:605-615`): a far transfer from 64-bit kernel code to a 32-bit
kernel segment records "interpretable" instead of halting. Only Ubuntu 6.8's AP route, Linux's LA57 switch and
kvm-unit-tests' 5-level tests do that. B4 built: a CPL-0 far JMP, CALL or RET to L = 0 code completes and the
interpreter runs it; L = D = 1, CPL 1-2 and IRET still halt; its CPL-3 arm stays. The price: the protected-mode
far-jump decision exists twice, for interpreted and for translated modes, each small, over shared descriptor checks.

**User-mode far transfers and the other user-mode gaps are outside this work (owner, November 22):** M2 Part 1's
task 5 (far JMP/CALL/RET between 32- and 64-bit user code) designs its own change later, and the CPL-3 non-flat
fallback stays deferred.

Two 64-bit-only rules the interpreter would otherwise inherit get their non-long-mode behaviour in the interpreter's
own path, with the existing callers unchanged: `load_data` accepts a null SS at CPL < 3 (`segments.rs:523-528`), and
`far_target` rejects L = D = 1 although L is ignored when LMA = 0 (`:591`).

**What of M2 task 3b's 32-bit transactions the interpreter reuses:** the descriptor helpers above and the far-pointer
decode. The 3b bodies that touch the stack or an operand (PUSHA/POPA, far CALL, far RET, IRET, ENTER, BOUND) assume a
32-bit ESP, SS base 0, no limit and a 4-level walk, so they would compute wrong addresses in real mode and on 16-bit
stacks: the interpreter does not call them (far RET is its own; the rest stay typed stops).

**The interpreter loop:**

- It repeats `find`'s boundary prologue in its order (`io_checkpoint`, `take_invalidations`, `lapic::poll`, `trap_due`,
  `events::take`; `execution.rs:217-232`). Delivery (`events::take` → `deliver`, 64-bit-IDT-only) is unchanged: in
  interpreted modes the loop takes INIT and SIPI itself, and any event that would be delivered (an exception, an NMI,
  an external interrupt with IF = 1, a debug trap) is a typed stop naming it. INIT and SIPI live in the existing pending
  word, so `events::none`, tested at every boundary and in `sys_commit`, stays one load.
- One instruction per pass. TF set or an instruction breakpoint armed while interpreting is a typed stop; the MOV SS
  shadow is ignored (no event can be taken). Interpreted modes never use `step_here`/`REQUEST_STEP`, which compile one
  instruction with fexc (`execution.rs:603-640`).
- New `State` fields: none (built); the wait state and the latched SIPI with its vector live in the pending word
  (`Events::pending`, formerly `Events::nmi`: bits N, I, W, S and the vector), so `events::none` stays one load.

**Cached records:**

- Keyed by linear address (CS.base + EIP) and a context with the interpreted mode and CPL, through the existing
  `lookup` and `revalidated`. The new kind is folded into `find_from`'s existing `SYSTEM` flag test on the hit path
  (`execution.rs:273`) and split inside the taken arm; it is never filled into the L1.
  Built: keyed by linear address and `interpreted_key` (mode, CPL, CS.D), filed as SYSTEM | INTERPRETED and found by
  their own L2 probe (`cache::interpreted_at`; the byte check is their revalidation), never in the L1; no code context
  equals their key, so translated lookups are unchanged.
- **Validated by their bytes, not by write-watching:** each execution compares the record's bytes with memory (15 bytes
  at most) and re-decodes on a mismatch: equivalent to fetching every time, stronger than the SDM's self-modifying-code
  rule, and old-or-new for unserialised cross-modifying code. Interpreted records never set the code-write mark, and
  `find_from`'s "mark cleared after placement → kill and step" check is skipped for them; the comparison reads through
  the current translation and checks the CS limit at every execution (execute rights are checked when CS is loaded;
  the INIT and reset CS is "Present, R/W, Accessed", SDM Table 12-1); a stale record is replaced in
  place or cut (`cut_one`) before the new one is filed; a record stale at its first execution runs uncached from a fresh
  decode (no livelock).
  A decode fetches to RIP's page end; the next page only for an instruction running into it, and a fault there or
  past the CS limit is raised only when an instruction reaches it.
- This reduces, not removes, the shared-page cost: a stub page that also holds translated 64-bit code is write-watched
  once any CPU translates there, and stores to a counter on it go through `code_write` and the re-mark wait (`mark`,
  `execution.rs:1793-1820`) as today. The exit test bounds decode exits and shootdowns (section 8).

## 6. Transitions, events, CPU start-up and reset

- **Transitions** (checked against `tools/models/modes_model.py`, whose 32-bit programs were parked for this phase):
  MOV CR0 setting and clearing PE and PG at CPL 0 outside 64-bit code (in 64-bit mode clearing PG stays #GP); EFER.LME
  under PG = 0; IA-32e activation and deactivation; far JMP and far RET into and out of 16-, 32- and 64-bit code segments
  at CPL 0 (the interpreter's own, section 5); MOV CR4 with PAE and the boot CPU's feature bits under PG = 0 (Linux, EDK2); LIDT and
  near RET in compatibility mode (OVMF, kvm-unit-tests). Out of the interpreter: a far transfer to a 64-bit CS with LMA
  set.
- **Exceptions and interrupts in interpreted code: typed stops.** The interpreter computes the exception exactly
  (vector, error code, the SDM's order of checks) and the stop reports it, with nothing committed. No surveyed stub runs
  with interrupts enabled or a handler installed (IF = 0 throughout, from INIT or `cli`; independently replicated), and
  on hardware a fault there ends in a triple fault; the real-mode IVT and 32-bit gates wait until a workload needs them.
- **INIT and SIPI: a state machine written as a model first** (as `tools/models/mailbox_model.py` was for the mailbox):
  - sent like NMI today: a word in the target's `Events` plus its doorbell (`lapic.rs` `send`), with the destination
    shorthands (kvm-unit-tests sends INIT and SIPI to all-but-self, `lib/x86/smp.c:290-293`); an INIT or SIPI whose shorthand selects no CPU is a no-op; kvm-unit-tests sends INIT and one SIPI, vector 0, through the xAPIC MMIO ICR before it enables x2APIC;
  - INIT pending → the INIT state (SDM "Processor State After Reset", INIT column; section 9) → wait-for-SIPI, a `wfi`
    loop like `wait_mailbox`;
  - a SIPI arriving before the target has taken its INIT is latched and honoured after it (kvm-unit-tests sends them back
    to back); a SIPI to a CPU neither waiting nor INIT-pending is ignored (Linux sends two, and the second arrives while
    the CPU already runs its trampoline, `smpboot.c:718-772`);
  - in wait-for-SIPI an NMI is blocked and held, the first event after the SIPI (a typed stop in the trampoline; SDM
    3C 28.2/29.7.2, AMD APM 16.5, KVM; QEMU's TCG delivers it); no fixed vector is pending, since INIT leaves the APIC
    software-disabled and a disabled APIC accepts none (architecturally; natemu's two-step fixed send can still leave an
    IRR bit set briefly, as delivery latency, so the resident never asserts IRR = 0 there); an INIT in wait-for-SIPI drops a latched SIPI and a held NMI;
    HLT in interpreted code is woken by INIT, an NMI there is a typed stop, and a SIPI does not wake it
    (`tools/models/initsipi_model.py`);
  - the target publishes W (waiting for SIPI) in the pending word, so the sender latches or drops a SIPI in one atomic
    update; taking INIT turns I into W in one update; an INIT clears a latched SIPI;
  - INIT taken with IF = 0, in HLT, during the mailbox wait, while running translated code, and while the target is
    inside a host request;
  - what INIT resets in the resident: shadows and the L1 context, the LAPIC per the SDM, `Events::halted`, and the host's
    rule that ends the run when every vCPU is halted (a wait-for-SIPI CPU is idle for that rule, publishing `halted`
    like the mailbox wait and rechecking INIT and SIPI before it parks, as `park()` does, and is reported as waiting for
    a SIPI, never as halted);
  - repeated INIT-SIPI-SIPI rounds to the same CPU (EDK2 does this for every MP-services call), and Linux bringing an offlined CPU back
    (`boot-startup`). Linux v6.12 parks no CPU with INIT at shutdown: `native_stop_other_cpus` stops the others with its
    reboot vector and they stay in `stop_this_cpu`'s HLT (v6.12.107's source; `boot-startup` reports them halted).
- **NMIs and IPIs from a running Linux to a CPU still in its trampoline** (replicated at v6.12): none on the normal path.
  Targeted senders use the online mask, which a booting CPU joins only after its IDT is set; broadcast shorthands are
  enabled only once every present CPU has booted. A crash-time broadcast NMI during a CPU hotplug re-online could reach
  a trampoline: a typed stop there is accepted (it happens only in a panic).
- **The ACPI mailbox stays** for direct Linux boot; `Boot::mailbox` (boot-linux `--no-mailbox`, on by default) leaves
  it out of the MADT and starts each AP in wait-for-SIPI (Linux then uses INIT/SIPI); a Multiboot
  kernel gets no ACPI tables and its APs always start so.
- **Power-on state:** a builder for the SDM's reset state (CR0 = 60000010h, CS = F000h/FFFF0000h/FFFFh, EIP = FFF0h,
  EDX = the CPU profile's signature, DR6/DR7, MSR reset values). Built: `natemu_abi::power_on` (`power_on`, `init`,
  `sipi`; `cpu::power_on_msrs`, MTRRs disabled), to Table 12-1 (CS attributes 9BH as KVM loads them; FCW 0040H as the
  SDM says, where QEMU and KVM use 037FH).
- **Platform reset that restarts the machine** (decided by the session): M3; today every reset ends the run.

## 7. Platform pieces

- **Firmware boot mode** (built, [firmware-mode.md](firmware-mode.md); `--firmware IMAGE`, `[boot.firmware]`): the image at
  [4 GiB − size, 4 GiB) is read and fetched as memory and its stores are dropped; it is kept out of `guest_ram()` and
  reached only on the paths that find a page outside RAM, and the per-page tables stay at the end of RAM (stores the
  device path refuses stop as `unsupported_mmio`, a recorded deviation). The plan's first version of this bullet: fetch requires `guest_ram().range()`
  (`execution.rs:1692`), so the image becomes a guest-RAM region (`GuestRam` has four region slots,
  `guest_memory.rs:55-73`; **Inferred** that one is free) in the alias's unused 3-4 GiB slot, the per-page tables must
  span up to 4 GiB (`cache.rs:3238`), and writes must be ignored: the alias is writable, so `copy`, `atomic_byte` and the
  shadows each need a read-only check. The BSP starts at the reset vector with the MTRRs at their power-on values (not today's `MTRR_INITIAL`), the
  others in wait-for-SIPI, no mailbox.
- **Multiboot v1 loader** (`--kernel` with a Multiboot image; built: `boot::multiboot`, which `load_kernel` picks for a
  non-bzImage with a Multiboot header; ELF32 and address-field forms): 32-bit protected mode, paging off, EAX 2BADB002H,
  EBX the information (memory map, command line, modules), flat 08H/10H. kvm-unit-tests (a2891d95) reads `NR_CPUS` from
  its one module, since this board has no fw_cfg, and argv[0] first from the command line. Under a test option only
  (decided by the session), isa-debug-exit at F4H-F7H.
- **The exit test's firmware is ours** (`tests/boot/exit_fw.asm`; contract
  [m2-exit-firmware.md](m2-exit-firmware.md)), proven at 1, 2 and 4 CPUs on QEMU 11.1.2 and KVM (`exit-fw-*`); its AP
  part is also a 64-bit program for B2 (`exit_prog.asm`, `exit-ap-*`). It is a small nasm image built by the framework from the repository: 16-bit reset
  code that enables the MTRRs first, a far jump to flat 32-bit code with paging off, page tables, long mode, 64-bit code;
  then INIT-SIPI-SIPI to every other CPU, each AP stub also enabling the MTRRs and climbing on its own past a shared
  counter on its stub page; a serial report and power-off.

## 8. Testing

All through `tools/natemu-test`, as cases in families; every input built or downloaded by the framework from a pin in
the repository; every number from a framework case (or labelled an experiment).

- **Executor unit tests on the host:** the abi core against a small independent reference over generated operands,
  every flag; the interpreter's far-transfer rules; the classification.
- **Per-instruction cases in each mode** (R, P32, C32 at CPL 0, and the P16 far-jump stretch) for exactly the first
  version's list: generated programs from known states; registers, flags and memory compared. natemu's program driver
  gains start states for these modes (built: machine descriptions set a case's state directly in any mode; case
  `natemu-machine` in 64-bit mode).
  References: the pinned QEMU 11.1.2 system emulator (a variant of the SMP stub that leaves the CPU in the case's mode),
  and real hardware through the KVM reference boot on the x86 machine (instructions run on the AMD processor; QEMU only
  loads the image; port I/O, MMIO, CPUID, MSRs and task switches are KVM's code there). Vendor differences from the Intel
  CPU natemu presents are masked per case or answered by QEMU. QEMU's TCG does not set a code descriptor's accessed bit
  on far transfers, so KVM answers those windows. B0 showed that the AMD machine runs real-mode and P16 code natively
  under KVM (experiment, not a framework case, B0's spike on the x86 reference machine: 0 instruction emulations over
  470M instructions; the machine is itself a KVM guest, so this is nested SVM). Built: the `interp` family, its format
  in [interp-cases.md](interp-cases.md).
- **Fault cases** against the SDM models: the typed stop names the exception and error code the model gives, with
  nothing committed (`segment_model.py` extended to far JMP and far RET in real and 16/32-bit protected mode at CPL 0,
  `modes_model.py`'s parked 32-bit programs). Built: `tests/interp` `natemu-faults` (`interp_faults.py`: 306 segment
  cases, 21 `modes_model` programs).
- **The interpreter-versus-translator cross-check:** flat 32-bit programs, already answered, run through the interpreter
  (a debug switch forcing it) and must agree. **Dropped (session, November 22):** the interpreter runs only CPL-0 code and the first version's
  list, so the answered 32-bit programs (CPL 3, with MUL/DIV, string and SSE forms) would mostly stop; the per-instruction
  cases against QEMU and real hardware in every mode cover the same semantics.
- **32-bit user code stays as it is:** lock cases that 32 → 64 and 64 → 32 far JMP, CALL and RET at CPL 3 still give
  today's #GP(selector), with the existing `compat32` cases and `tools/bench/smp/segments.py` and `compat.py` programs,
  run at every commit that touches `segments.rs` (a regression guard: the functions they exercise are not edited).
- **kvm-unit-tests** (pinned source, built by the framework in the pinned amd64 container with gcc `-m16` and
  `ld -m elf_i386`, which work there both natively and under Rosetta: B0 spike, kvm-unit-tests a2891d95): `smptest` plus one trivial x86_64 test (the 32-bit entry, the
  climb, INIT + SIPI to every other CPU through a 16-bit trampoline) at 1 to 4 vCPUs. Each prints its own pass/fail; the
  lock records an answer hash per case, seeded from the KVM control run. Built: `tests/boot` `kut-qemu`/`kut-kvm` (smptest, dummy; [real-code-suites.md](real-code-suites.md)).
- **Real OS and firmware code:** Debian's kernel with the mailbox left out, at 2 and 4 vCPUs, through boot, CPU
  offline and online (refused by Linux with the mailbox) and shutdown (the APs left in HLT; built: `tests/boot`
  `boot-startup`); OVMF edk2-stable202608 (OvmfPkgX64, DEBUG, no SMM or Secure Boot; `ovmf-qemu`/`ovmf-kvm`: registers at SEC's entry, no device access before it) through its reset
  vector (16-bit and `Main32` entries) to SEC in 64-bit mode; Ubuntu 6.8's compatibility-mode route when that kernel is
  in the framework (roadmap P3).
- **The M2 exit test** (section 7) at 1, 2, 4 and more vCPUs, asserting bounds on decode exits and shootdowns with N APs
  both in and past the 16-bit part; and a litmus test between an interpreting CPU and one running translated code.
  Built: `exit-fw-natemu` at 1, 2 and 4 vCPUs with those bounds. Not built (later): the runs at more vCPUs and the
  litmus test.
- **No effect on a running 64-bit OS** (section 9c): the gate, the compiler's replay check (`static/fexc/fexc-replay`)
  and the benchmark cases `guest-hackbench`, `guest-hackbench32` and `guest-hackbench32-int80` (system-call paths), with a
  benchmark round, at the end of every slice that touches a hot path, against the parent commit.
- **Not SeaBIOS:** its power-on code needs an i440FX or Q35 chipset, fw_cfg and CMOS sizing natemu does not have.

## 9. What the real stubs use (census, independently replicated)

First read by a research subagent (November 21); replicated from upstream source by an independent subagent
(November 22): Linux `v6.12` (Debian 13's kernel) and master (`9fcdf40e0d7c`), edk2 master `556dcfb466e9`,
kvm-unit-tests master `a2891d9528ef`. Also read once: SeaBIOS `28aa46349b97`, ReactOS `303d27d62f80`, cloud-hypervisor
`c0eda579d28b`, rust-hypervisor-firmware `01e9a847e1c1`, ACPI 6.5, the Intel SDM (an older mirror; section titles).

| Stub | 16-bit | 32-bit, paging off | Compatibility mode | Notes |
| --- | --- | --- | --- | --- |
| OVMF X64 reset vector (Vtf0) | 13 instructions | ~27,000, 99% two page-table loops | LIDT, a few, far JMP | `o32 lgdt [cs:bx]` through the reset CS base; one far JMP under the stale real-mode CS; MOV r/m←imm (C6/C7) in the page-table build; `bt [mem],ecx` bit string. If PE is already set at FFFFFFF0h it skips 16-bit (`Main32`; shown off TDX on QEMU and KVM). No port I/O, SSE or stack on the default path; SEC entered in 64-bit mode. No device access before SEC (QEMU trace, both references). |
| OVMF CloudHv (`XenResetVector`, PVH) | none | as above, plus `push; retf` | 32-bit RETF | The VMM starts the BSP in 32-bit protected mode, paging off (read once). |
| EDK2 AP stub (`MpInitLib/X64/MpFuncs.nasm`) | 19 | ~25 | far JMP m16:32 | Re-run at every INIT-SIPI-SIPI round. MUL and LOCK forms only after 64-bit mode. |
| Linux trampoline + `verify_cpu` (v6.12 = master) | ~45: CPUID, MISC_ENABLE RDMSR/WRMSR, PUSHF/POPF, `lock btsl` + `pause` | ~20 | far JMP | Only without the MADT wake-up mailbox. |
| kvm-unit-tests (`cstart64.S`, `trampolines.S`) | the AP trampoline | Multiboot entry, `prepare_64`, `lock xaddl`, GS_BASE WRMSR | near RET, far JMP / `lretl` | `realmode.flat` is out of the first version. |
| Windows x64 (`HalpRMStub` → `HalpPMStub` → `HalpLMStub`, reverse-engineered) | unknown | unknown | — | INIT-SIPI-SIPI; no public sign it reads the mailbox. |

Replication verdict: the first-version list covers Linux, EDK2's AP stub and kvm-unit-tests completely; OVMF's reset
vector needed MOV r/m←imm, now in the list. IF = 0 and no exception expected on every path. Avoidable, kept out: Linux's
BIOS reboot and ACPI S3 resume (offer no `\_S3`), SMM (build OVMF without it), OVMF's Ia32X64 build. INIT state (SDM,
INIT column): caches, MSRs, MTRRs, x87/SSE/AVX, TSC, SYSENTER/STAR/LSTAR unchanged; EFER, FS/GS base cleared, XSS unchanged (Table 12-1); CR0 =
60000010h with CD/NW unchanged, CR2-CR4 = 0, EFLAGS = 2, CS = F000h/FFFF0000h/FFFFh, EIP = FFF0h, EDX = signature,
DR6/DR7 = FFFF0FF0h/400h; TLBs invalidated. SIPI: CS VV00h, base VV000h, IP 0. A20: not modelled (hypervisors never had
it; Intel parts since Haswell lack the pin, a QEMU maintainer's statement; ACPI requires A20 on at the waking vector).

## 9b. Can firmware avoid 16- and 32-bit code?

- **BSP:** OVMF skips its 16-bit part when started with PE set (`Main32`), leaving ~27,000 32-bit instructions; EDK2's
  Universal Payload (`UefiPayloadPkg`) is entered in 64-bit mode with a hand-off block list from the boot loader, with
  natemu as the boot loader.
- **The other CPUs, in firmware:** every stock EDK2 configuration uses INIT-SIPI-SIPI and the 16-bit stub, repeatedly;
  avoiding it needs a uniprocessor build (`MpInitLibUp`), MpInitLib's private hand-off blocks (fragile), or a port of
  OVMF's TDX mailbox path (**Inferred** feasible; our patch to carry).
- **The other CPUs, in the OS:** Linux uses the mailbox whenever present; Windows, the BSDs and pre-5.16 Linux use
  INIT-SIPI-SIPI with their own real-mode stubs. No firmware choice removes this, so M4 needs the interpreter.

For M3, a long-mode firmware entry is still worth considering: it takes the ROM mapping at 4 GiB, the 27,000-instruction
stretch and the power-on memory-type question off M3's critical path. The choice is M3's (section 11).

## 9c. Effect on a running 64-bit OS

The interpreter runs only for state the translator cannot take, but integration edits sit on paths a running 64-bit OS
uses all the time. Precedent: M2 task 1's plumbing cost hackbench 3-4% and locks 2% at 1 vCPU through one function that
stopped being inlined (`controls()`, fixed in `ec84a1e6`; [checkpoint A findings](m2-checkpoint-a-findings.md)).

| Edit | Reaches a running OS through | Safeguard |
| --- | --- | --- |
| Interpreter entry: the halting arms of `controls()` in `commit`/`set_cpl` (and later `refuse_compatibility`'s CPL-0 arm) record "interpretable" (a bit in `State::debug`), the next boundary's existing trap test enters; `start()` and INIT enter directly | `controls()` runs on every system transaction (through `commit` and `set_cpl`, twice per SYSCALL/SYSRET on the fast path), every shadow miss (`translate`) and every resident access (`pages`) | The two returning arms stay exactly today's; the new work is only in arms that halt today; `controls()` stays inline; no new instruction on the fast system-call path or `find`'s hit path (disassembly identical to 78f60965, literals aside). |
| The paging-off walk variant | `pages`, `fetch_walk` and `revalidated` | Reached only from interpreted state; the returning arms and the 4-level walk unchanged. |
| The interpreted classification | Only the cold points above | A separate function; `code_mode()` and `code_context()` untouched, so the code identities, the cache keys and every hot reader stay as today. |
| Far transfers and segment loads | 64-bit kernels' far returns and segment loads; 32-bit user programs' segment loads and far transfers (M2 task 3b) | The interpreter's own far transfers and its non-long-mode rules live in its own path over the shared descriptor helpers; `same_mode`, `far_transfer`, `far_return`, `load_data` and `far_target` stay byte-identical for their existing callers; CPL-3 deviation lock cases at every commit touching `segments.rs`. |
| The shared address helpers | 32-bit user programs' resident-executed accesses | Unchanged; exact checks only in the interpreter's memory layer. |
| Interpreter's resident operations through `transaction` | `system` on the fast system-call path | `transaction` `#[inline(always)]`; `system` identical by disassembly (built). |
| The byte-validated record kind in `cache.rs` | Lookup, kill and revalidation, shared with translated blocks | New code paths only for the new kind, folded into the existing `SYSTEM` test on the hit path; translated blocks' paths unchanged. |
| INIT and SIPI bits in `Events` | `events::take`/`events::none` at every boundary and in `sys_commit` | Kept in the existing pending word: the no-event test stays one load. |
| `State` growth | Slow-path copies of `State` | At most one word; the decode reply lives in the staging area. |
| INIT and SIPI in `lapic.rs` | Only a guest that sends them | Debian with the mailbox never sends them (today it would stop the VM). |
| The end-when-halted rule | Every run's shutdown and hang detection | Only vCPUs in the new wait-for-SIPI state are treated differently; none exist on the mailbox path. |
| The larger resident code window | Every run (a layout change) | Its own commit; B0 lists the constants it moves and shows the replay check unchanged (if generated code embeds a moved address, it is re-baselined in that commit alone). Done in B1 (a); `fexc-replay` fails identically at the parent (it compares against `main`'s compiler). |
| Power-on memory type, interrupt delivery, the default boot path | — | Unchanged (sections 4, 6, 7). |

Checks, all framework cases, at the end of every slice that touches a hot path (B1's commits, B2) and again at B5,
against the parent commit: the full gate; the replay check; the hackbench cases and a benchmark round, each difference
beyond noise explained before merge. Built: the full gate ran at B1 (b) (8b022e58, on the branch 0624b341: its three
failures as at the parent) and on the final tip (section 12); B2 had no gate of its own (its author ran its CPU start-up
cases; the system, smp, interp and unit families ran at e21b0209, after the interpreter's merge); the hackbench cases
ran at be626aa8, e21b0209 and the final tip, each against 78f60965 ([benchmarks](../benchmarks.md)).

## 10. Order of work

Each slice ends with something that runs and cases in the framework. A thin vertical slice comes right after the
plumbing, so the risky integration is proven on real code before the breadth work.

| Slice | What runs at the end | Agent hours (guess, low confidence) |
| --- | --- | --- |
| **B0 spike** | As framework cases or labelled experiments: the resident's size and stack headroom, and the constants the window move changes with the replay result; the cost of an iced-only decode request; a system-call cost baseline (the hackbench cases) and a benchmark baseline of the parent commit; TSO at EL1 (litmus); unaligned single-copy atomicity within 16 bytes at EL1; whether `copy_in`'s `dsb ish` is needed for RAM; ~~the SDM's wait-for-SIPI NMI rule~~ (answered, section 6); ~~KVM real mode on the AMD machine; gcc `-m16` in the amd64 container~~ (answered, section 8). Replaces the estimates below. Built: TSO at EL1 and the `dsb ish` were not spiked (both stay **Inferred**, section 4); unaligned atomicity within 16 bytes at EL1 is self-test `p32-lock-unaligned`. | 8-12 |
| **B1 plumbing** (three commits, each with the section 9c checks) | (a) The resident's code window moved and grown. (b) The interpreted classification and the entry plumbing (the halting arms of `controls()`, the test on `system()`'s exit, `start()`, `find_from`'s flag test for the new record kind), the paging-off walk variant, and the CPL-3 deviation lock cases. (c) New code only: `REQUEST_DECODE` and the record; the abi executor core skeleton with host unit tests; byte-validated records; driver start states in each mode; a handful of instructions with QEMU-locked cases. | 16-26 |
| **B2 vertical slice: CPU start-up** | INIT/SIPI state machine (model first), wait-for-SIPI, power-on/INIT state; the instructions the Linux trampoline and the exit-test AP stub use; INIT and SIPI taken by the interpreter loop, every other event while interpreting a typed stop; real mode → P32 → long mode → hand-off to the translator (the interpreter's far JMP from C32 to the 64-bit CS); Debian with the mailbox left out at 2 vCPUs, then 4, through shutdown (built, `boot-startup`: no typed stop; its warm-reset writes, CMOS 0x0F = 0AH and the vector at 0x467, made and restored at every AP start, mailbox or not, are benign: a plain CMOS byte and guest RAM); the exit test's program variant (its AP part, a shared counter on the stub page) at 1, 2 and 4 vCPUs through a new wait-for-SIPI driver item. | 20-30 |
| **B3 the rest of the list** | The remaining forms of section 1's list in R/P32/C32, exact segmentation and the typed stops' exception reports; generated cases against QEMU and hardware; the interpreter-versus-translator cross-check. | 8-15 |
| **B4 transitions** | The remaining interpreter far transfers and transitions of section 6 (both directions of PE/PG/LME, far RET, MOV CR4 under PG = 0, LIDT in compatibility mode); `refuse_compatibility`'s CPL-0 arm for 64-bit kernel code entering a 32-bit segment; the `segment_model.py` extension, `modes_model` programs and fault cases. | 8-15 |
| **B5 platform and suites** | The firmware boot mode and the full exit test (BSP from the reset vector) at 1 to N vCPUs; the Multiboot loader and port 0xF4 (test option); kvm-unit-tests' SMP tests; OVMF's reset vector to SEC; the gate and a benchmark round against the parent commit. | 12-20 |
| **Total** | | **72-118** |
| B6 CPL-3 non-flat fallback | Deferred (owner, November 21: low return on investment); not in the total. | — |

The design notes' first guess for Part 2 was 60-90 hours. Part 1's tasks 2-3b came in at half their low estimates or
less, but they reused FEX and an existing test rig; this builds a new executor and a new reference rig, so that
calibration is not applied. VM-queue waiting comes on top. Code, as a rough guess: 2,500-4,000 new lines in the abi core
and the resident, 800-1,200 in the host, 1,500-3,000 of test generators, test firmware and models; 800-1,500 changed
lines in about 15 existing files.

Later options, each on a workload's demand: fast non-flat code in translation (task 6, or hardware segment windows); a
flat P32 translator path for long 32-bit stretches; platform reset that restarts the machine (M3); the interpreted-mode
IVT and 32-bit gates, string instructions and the other typed stops, as real boots hit them.

## 11. Decisions

By the owner (November 21-22):

- The resident's code window may grow as much as needed.
- The CPL-3 non-flat fallback (B6) is deferred; the #GP deviation stays.
- Memory classification and access in software, at EL1, for simplicity.
- M2 or the start of M3: either is fine.
- Far transfers in interpreted modes are the interpreter's own code; the existing far-transfer functions stay as they
  are (November 22).
- User-mode gaps (task 5's far transfers between 32- and 64-bit user code, the non-flat fallback) are addressed later,
  outside this work (November 22).
- The instruction scope and the power-on memory type are the session's to decide ("i don't decide what an os needs";
  "whatever reasonable").

By the session, under that delegation:

- Instruction scope: section 1's first-version list, typed stops for the rest, gaps found by running real boots.
- Exceptions and interrupts in interpreted modes: typed stops in the first version.
- Power-on memory type: unchanged exact UC in M2; the write-back alternative decided in M3 against OVMF.
- Legacy two-level and PAE paging without long mode: a typed stop.
- CR4.MCE writes are accepted as a no-op without presenting MCE in CPUID (a recorded deviation; OVMF's reset vector
  sets it unconditionally, and natemu never raises a machine check). Decided by the session, under the owner's
  delegation; the owner later confirmed minor deviations are fine as long as OS boots are unaffected.
- Platform reset that restarts the machine: M3.
- kvm-unit-tests' debug-exit port behind a test option only.

Open, for M3: the firmware build (section 9b).

## 12. Review record

All reviews by independent subagents (model Fable, read-only, against the code), separate from the author.

**First review (November 21), of the first draft:** the same architecture would be chosen (resident executor fed by host
decode, segmentation in the VM, translated code flat-only), but the draft overstated three reuse claims and missed three
constraints. Accepted, each checked in the code: decode reached only through a fexc cut (→ `REQUEST_DECODE`); no single
dispatch point, and step/shadow/TF through fexc (→ section 5); power-on MTRRs make RAM UC (→ section 4); the scope widened
a roadmap line (→ now the narrow list of section 1); resident size, jump tables and stack (→ sections 2, 10); split locks
no longer stop (→ section 4); INIT/SIPI under-specified (→ section 6, model first); code and data on one page (→
byte-validated records, a different remedy the recheck endorsed with conditions); cache keying details, the firmware
region, test-plan conformance (→ sections 5, 7, 8). **Recheck:** no blockers.

**Second review (November 22), requested by the owner, of the narrowed revision, with an addendum on 32-bit user code.**
Verdict: ready for B0; conditions for B1, all now in the plan and checked in the code:

| Finding | Disposition |
| --- | --- |
| The fast system-call path reaches `controls()` (`sys_commit` → `system` → `commit`; SYSCALL/SYSRET call `set_cpl`) | Accepted (verified: `controls()`'s own comment says each SYSCALL and SYSRET passes through both): section 5, 9c; hackbench cases at every hot-path slice. |
| Hot readers of `code_mode()` (`enter`, `bank`, ...); the LMA-first edit unnecessary | Accepted: `code_mode()`/`code_context()` untouched, a separate classification (section 5). |
| `find`'s hit path tests only CS.L; classify only at cold points | Accepted (verified, `execution.rs:211`): entry from the halting arms, `start()`, far transfers, INIT. |
| NMI and broadcast IPIs to wait-for-SIPI CPUs undefined | Accepted: section 6's model rules; B0 confirms the SDM text. |
| `Events` bits, `State` growth, the window move reach every run | Accepted: section 9c rows; decode reply in the staging area; B0 checks the replay. |
| Safeguards too late (B5 only) | Accepted: checks at every hot-path slice, B1 split into three commits. |
| Never-started APs have MTRRs disabled; cache hit path; consistency | Accepted: sections 4, 5, 7, and the rewrite. |
| Addendum: widened rules gated on "not 64-bit" would land on 32-bit user code | Accepted (verified, `segments.rs:620-628, 704-740`): the interpretable predicate (section 5). |
| Addendum: the CPL-3 #GP deviation sits in the lines the widening edits | Accepted, then resolved differently (owner, November 22): the interpreter owns its far transfers, so those lines are not edited; the CPL-3 lock cases stay as a guard (sections 5, 8). |
| Addendum: the shared address helpers must not become exact in place | Accepted (verified, `segments.rs:248-262`): section 4. |
| Addendum: `load_data`/`far_target` apply 64-bit rules | Accepted (verified, `segments.rs:523-528`): the interpreter's own path applies the non-long-mode rules; existing callers unchanged. |
| Addendum: design the far-transfer decision once with task 5 | Superseded (owner, November 22): interpreter-owned far transfers; task 5 designs its own change later. |
| Addendum: task 3b's stack and operand bodies unsafe in real mode | Accepted: only the descriptor helpers and the far-pointer decode are reused; far RET is the interpreter's own. |

**Recheck of the folded plan (November 22):** the dispositions state the findings as meant; one substantive point,
now in section 5 and 9c: entry cannot happen inside `commit` or `set_cpl`, which run before the transaction completes
(verified, `execution.rs:3223-3232`, `segments.rs:825`), so those arms record "interpretable" and the entry is one test
after `system()` completes. Also added: the interpreter never calls `code_context()`; a 9c row for the paging-off walk; the
classification plumbing and the new code in separate commits so a benchmark difference is attributable. (Its point on
reusing far RET's existing transaction is moot since far RET became the interpreter's own.) Verdict: ready for B0 and
B1; no blockers.

**Census replication (November 22):** an independent subagent re-derived the instruction lists from upstream source
(Linux v6.12 and master, edk2, kvm-unit-tests). One gap (MOV r/m←imm, OVMF), now in the list; the mode notes (P16 for
the far-jump stretch, LIDT and near RET in compatibility mode, MSRs beyond EFER) are in sections 1 and 6.

**Reviews during the build (November 22), each by an independent subagent separate from the author:** the executor
core (stop records not well formed, the fetch contract's execute check: fixed), the models (INIT state incomplete, an
uncovered NMI rule: fixed), the start states and Multiboot loader (default boots confirmed unchanged), the plumbing (the
first entry nested under `system` on the fast system-call path: redesigned to enter at the next boundary), the plumbing,
INIT/SIPI and interpreter together (ready; the INIT-before-TF order fixed), and the resident operations (a frame on
`refuse_compatibility`'s common path: made a tail call; a far RET into compatibility code past the CS limit: a typed
stop; TLB-flush tests added). The test pieces (reference rig, exit-test firmware, real-code suites) and INIT/SIPI were
also reviewed by reviewers their authors briefed.

**Final checks (B5, on the final tip 9fd9f346, M2 Max, November 22):** the full gate (`tools/natemu-test run --gate`):
90,971 tests, 3 failures, none of them this branch's and the same as at 78f60965 (B1 (b)'s gate): `static` `fexc-replay`
(it compares the compiler at the merge base with `main` against the checkout's) and `system` `i5b-images`'
`fxsave-full-stack` (with its suite). The benchmark round: the system-call bench cases against 78f60965, as in the
status above ([benchmarks](../benchmarks.md)).

**Commit ids.** Some commit messages on `boot-interp` cite the commit an agent made on its own branch, which is not on
`boot-interp`. Each is the same change (equal `git patch-id`), cherry-picked as:
cb7cd541 → d4bf265d; 8b022e58 → 0624b341; f29042cf → ce8cea18; bdb5dc09 → 40a888db; 315860f3 → d7bc0885;
75cfdd22 → be626aa8. A number measured at one of those ids was measured on the agent's tree, not on `boot-interp`'s.

# Correctness pass: no guest instruction stops the machine

This is the November 6 implementation record, not a claim that every possible VM stop was eliminated.
The [November 7 CPU/RAM follow-up](correctness-cpu-ram.md) supersedes its remaining-form inventory: MASKMOVQ,
INSERTPS/VINSERTPS memory sources, all memory RCL/RCR counts, defined 16-bit SHLD/SHRD CL counts and 16-bit PUSHF/LEAVE
now execute. It also fixes vector alignment aborts, a shared-page synchronization race, BT memory width and x87 flags.

Status: **done** (November 6, 2026, branch `no-stop` from `correctness` 4b26a5a: §1-§3 are its three commits; the split
locks of §3 were added to the lane's scope by the coordinator, from the stress lane's finding). Owner rule: no instruction a guest
program can execute may stop the whole machine; the guest gets x86 behaviour or, where natemu cannot provide it yet, a
guest-visible recorded deviation. Evidence and scripts: `natemu-raw/nostop-20261106-01/` (README there).

## 1. Compatibility-mode entry at CPL 3 is contained (owner decision, November 6)

Found by the kernel x86 selftests ([correctness-x86-litmus.md](correctness-x86-litmus.md), finding 2): four 64-bit tests
reach 32-bit code at CPL 3 and stopped the machine with `unsupported_mode`.

**Design.** Compatibility mode stays unbuilt (owner decision 3). A transfer that would run CPL-3 code in a 32-bit or 16-bit
code segment (CS.L = 0, or L = D = 1) now ends the program, not the machine:

- JMP/CALL far at CPL 3, and RET far or IRET/IRETQ whose new CS has RPL 3 (from CPL 3 or CPL 0): **#GP(new CS selector) at
  the instruction, before any change**, after the descriptor checks x86 makes first (null, limit, type, privilege,
  present). The OS sees a bad transfer. From CPL 0 the fault is the kernel's own, at its IRET: Linux 6.12's
  `native_irq_return_iret` is covered by `error_entry`'s bad-IRET check ("Non-paranoid faults on return to userspace are
  handled by fixup_bad_iret. These include #SS, #GP, and #NP", `arch/x86/entry/entry_64.S`), which switches to the kernel GS
  and CR3 and re-raises the #GP as if from user mode right after the IRET: the program gets SIGSEGV, the kernel no oops.
- SYSRET and SYSEXIT with a 32-bit operand size (SYSRETL is how Linux's compat SYSENTER/SYSCALL paths return when they can):
  **the return completes** (RFLAGS from R11, SS, RSP, CPL 3, RIP its target) **and the first instruction at the target
  takes #GP(the new CS selector) at CPL 3**. Not at the instruction: an OS executes SYSRET with the user's RSP and GS base
  already loaded, so a CPL-0 fault there cannot be handled (Linux has no fixup for `sysretl`: a #GP there would run its
  handler on the user stack with the user GS base). A fault right after a completed SYSRET is what AMD processors give for
  a non-canonical RCX. A TF the return loads gives no single-step trap before the fault.
- A call gate at CPL 3 (not built): #GP(its selector), the same containment (Linux installs none). At CPL 0 call gates
  stay the typed stop `unsupported_system`.
- CPL-0 code entering compatibility mode (a far transfer at CPL 0 or a return to RPL 0) keeps the typed stop
  `unsupported_mode`: only the kernel can do it.

Code: `crates/runtime/src/execution/segments.rs` (`refuse_compatibility`, `far_target`), `syscalls.rs` (`compatibility`).
Recorded in boot-handoff §4 ("Compatibility mode at CPL 3") and §6a.

**Tests.** `segment_model` and `syscall_model` give the deviation as a `Deviation` fault (natemu only: x86 would run the
code); `run_slice3_vm.py system` runs them at block sizes default and 1, judging the fault (vector, error, RIP) and RSP
(unchanged: before any change; after SYSRETL the user's): CPL 3 `seg-cpl3-jmp-far-compat` (32-bit CS 0xdb),
`-call-far-compat16` (16-bit CS 0xd3), `-lret-compat`, `-iret-compat`, `-jmp-far-call-gate`; CPL 0 `seg-iret-compat-cpl3`,
`seg-iret-compat16-cpl3`, `seg-lret-compat-cpl3` (#GP at CPL 0, nothing changed); `sc-sysretl` (#GP(0x10) at RIP = ECX,
CPL 3, RSP the user's); the CPL-0 stops (`seg-jmp-far-compat`, `seg-lret-compat`, `seg-jmp-far-call-gate`) unchanged. The
segments' GDT gained the two DPL-3 compatibility code segments (limit 0xdf; `gs-limit` and the LAR/LSL selector list moved
past it). All 108 segment and system-call fault programs pass (`seg1/`, `seg2/`).

The four kernel selftests (Debian 6.12.107 source, the correctness lane's image `x86st.cpio.zst`, one test per boot, N = 1
and N = 4, `runs/x86st-n*-{13,15,16,20}`; with the output straight to the console, `runs/x86con-n1-*`): every boot ends
normally (CT-END, the VM running on). `sigreturn_64`: every subtest before matches native (`set_thread_area` refusals,
"Valid sigreturn: 64-bit CS ... all registers okay"), then "Valid sigreturn: 32-bit CS (23)" ends with the program killed
(SIGSEGV, rc 139). `sysret_ss_attrs_64`: "Syscalls followed by SS validation" (`call32_from_64`, a far return to the 32-bit
CS) kills the program (rc 139). `single_step_syscall_64` and `syscall_arg_fault_64` finish with exit 0 and 5 `[OK]` each:
their SYSENTER subtests (executed on natemu's Intel profile; the AMD reference has #UD in long mode and skips them) return
through Linux's compat path to the 32-bit CS, take the #GP at the kernel's IRET, and the test's own SIGSEGV handler
recovers ("Set TF and check SYSENTER" prints neither OK nor FAIL; the "invalid state" subtests print "Seems okay"); every
other subtest equals native.

## 2. A refused form is never a machine stop

**Cause.** A form fexc refuses is cut before; at a block's first instruction it is `Code::None`, which ended the vCPU's run
(`NoCode`) unless the host mapped the bytes to an SDM exception (`system::undefined`, `system::branch`). The same held for a
system instruction the host does not decode, a decoder or dispatcher failure and a compiler limit at one instruction
(`spill_area`, `code_too_large`).

**The generic path** (`crates/host/src/execution.rs` `Compiler::refused`): every no-code answer but the fetch-limited ones
(`partial_decode`, `end_of_input`: the host fetches the next page or delivers its fault) is now **#UD at the instruction**
(`system::REFUSED`, kind 0x7b, counted in the run's decodes; the resident raises it as UD), never a stop; the first 32 per
process are logged on standard error ("natemu: unsupported_form at RIP (bytes): delivered as #UD"). This is the recorded
deviation for forms natemu does not run yet; SDM-#UD encodings keep their exact #UD (`cut_ud`, `undefined`).

**Forms now executed** (compiler `series69-nostop`: FEX `natemu-nostop` 20ff20257, this tree's shim):

- **The masked moves**, run by the resident element by element (`execution/maskmov.rs`, host decode `system::MASKMOV`, a
  system cut gated first as AVX or SSE): VMASKMOVPS/PD and VPMASKMOVD/Q loads and stores (VEX.128, VEX.256), MASKMOVDQU and
  VMASKMOVDQU. SDM-defined: a masked-off element is never accessed and causes no fault; a load zeroes it and the
  destination's upper bytes; no #AC (Type 6). Where the SDM leaves room, the reference AMD machine (`probe/mprobe.c`,
  `probe/mm2.c`, user mode, a page boundary to a PROT_NONE or read-only page): a faulting store writes no element (all or
  nothing); CR2 is the first faulting byte of the first faulting masked-in element, also with masked-in elements below it on
  the good page; MASKMOVDQU/VMASKMOVDQU are one 16-byte write access whatever the mask (every mask, the all-zero one included,
  faults at the bad page's start; only masked-in bytes are written). Masked-off elements set no A or D bit (implementation
  dependent in the SDM); the non-temporal hint is dropped. natemu's outputs of both probes in a Linux guest equal the AMD
  machine's at N = 1, 2, 4 (118 + 24 lines; `runs/probe-n*`, `probe/compare.py`).
- **Scalar VEX forms with VEX.L = 1** (LIG: the SDM ignores L): run as with L = 0. FEX ignores L for all of them
  (`FLAGS_VEX_L_IGNORE`) once VROUNDSS/SD gain it (FEX commit 20ff20257); the AMD machine gives identical results for L = 0
  and 1 (vaddss, vsqrtsd, vcvtsi2ss, vcomiss, vmovss, vfmadd231ss, vcvtss2sd, vcmpss, vroundss), and natemu equals it.
- **16-bit BSWAP** (SDM: undefined result): FEX's, which zeroes the low 16 bits as the AMD machine does (`probe/mprobe.c`).
- **16-bit SHLD/SHRD with an imm8 count up to 16** and **RCL/RCR m with a static nonzero count** (D0/D1; C0/C1 whose imm8,
  masked, is not 0): FEX's (it makes the access for a static nonzero count; it skipped it, losing a #PF, only for a zero or CL
  count). Results equal the AMD machine's for every count tested (`probe/sh16.c`, 108 lines at N = 1, 2, 4: every value
  equal); OF after a count above 1 (SDM: undefined; 27 lines) is FEX's, not the AMD machine's, exactly as for the register
  forms, which were already admitted (one more line differs in PF at a count of 0, which leaves the flags the probe never
  set before it).
- **MOVSX r16, m16** (`66 0f bf` with a memory operand; found by the sweep below): its device-access completion record had
  the sign-extension bit set for a 16-bit move, which admission refused, ending the run with a host error; the shim
  (`fexc_mmio.inc`) now sets it only when the load widens.

**Left as #UD at the November 6 snapshot** (superseded where noted above; the recorded deviation, in order of how plausible each is in real code; each is a candidate for a later
change): VCVTPS2PH with a memory destination (F16C stores: FEX raises the MXCSR flags before a store that may fault; needs
its own lowering or a resident conversion); INSERTPS/VINSERTPS m32 whose zero mask clears the inserted element (FEX drops the
load and its #PF); BT*/BTS/BTR/BTC m, r with 0x67 (the bit offset with a 32-bit address: needs the AMD machine); RCL/RCR m with
a CL count or a count masking to 0; 16-bit SHLD/SHRD with a CL count or a count above 16 (results not the AMD machine's);
MASKMOVQ (an MMX masked store: the resident would need MMX state); ENTER with a nesting level; 66 on PUSHF, ENTER, LEAVE, near
RET, indirect CALL/JMP, RET far/IRET (16-bit forms) and CMPXCHG8B/16B; FBLD/FBSTP (BCD; FEX calls a host helper); FENI/FDISI/
FSETPM, the undocumented x87 aliases and the SAL /6 and TEST /1 aliases (to follow the AMD machine: not probed); the
prefix combinations the SDM reserves (REPNE on MOVS/STOS/LODS, REP/REPNE on forms that define neither, 66 with F2/F3 on an SSE
form, F2/F3 on a system instruction the host does not decode: to follow the AMD machine: not probed); and anything fexc or the
host cannot translate (decoder or dispatcher failures, a spill area or an oversized translation at one instruction: none
seen).

**Tests.** `tools/run_nostop.py` (new; programs with their own logging IDT that resume after each fault):
- `refused`: 22 of the forms above at CPL 3 and CPL 0: #UD at the instruction, 22 REFUSED decodes.
- `maskmov`: 140 cases per CPL (14 forms x 10 mask/offset/page kinds: all, none, half, last, sparse, random, a masked-off
  element on the bad page, the bad page read-only) at CPL 3 and 0, block sizes default and 1, against `maskmov_model`
  (memory, destination, fault vector/error/CR2/RIP); gating (#NM with CR0.TS, #UD with CR0.EM or without CR4.OSXSAVE).
- `sweep`: 221,184 encodings at CPL 3 (every one- and two-byte opcode x ModRM.reg 0..7 x memory and register operand x 15
  prefix sets; the VEX maps 1-3 x pp x L x W x 256 opcodes), each slot resetting the registers to an unmapped address: every
  program reaches its end (outcomes: #UD, #GP, #PF, #DB, #DE through the IDT, or completion); before the MOVSX fix the
  two-byte part ended with the host error above. 30,709 decodes were REFUSED (#UD); by vector: #UD 154,439, #PF 64,262, #GP 2,236, #DB 129 (ICEBP), #DE 32.
- Host unit test `system::tests::masked_moves`; `execution::tests` updated (a spill area at one instruction, and STMXCSR with
  a prefix, are REFUSED); `test_slice1.py` (the masked moves are system cuts; the LIG, BSWAP, SHLD/SHRD and RCL/RCR forms
  admitted); the runners that expected a refusal's stop (`run_i8_faults.py`, whose masked-move cases moved to `run_nostop.py`,
  `run_i6_faults.py`, `run_l3.py` `f3-in`, `run_slice1_vm.py` `declared_limit`, `run_slice1_faults.py`'s straddle probes) now
  expect the REFUSED #UD at the instruction; the ASM runner (`run_asm.py`) counts a REFUSED #UD at a listed form as
  unsupported, elsewhere a failure (its REFUSED list shrunk to what stays refused).
- Negative controls (`nc/summary.txt`, `mutate.py`, on the final tree): REFUSED back to a stop; the masked moves walking
  masked-off elements, a store not checked before its writes, the load destination's masked-off elements kept, MASKMOVDQU
  checking only its masked-in bytes: each red.

## 3. Split locks run, atomically (was the stop `unsupported_atomic`)

Found by the stress lane (`natemu-raw/correct-stress-20261106-01/repro/splitlock/`): a LOCKed read-modify-write, an XCHG
with memory or a CMPXCHG8B/16B whose operand crosses 16 bytes stopped the machine (`unsupported_atomic`, a typed refusal
since the boot phase), also inside one cache line (offset 14 of a 64-byte block) and across one (offset 62). Hardware runs
all of these atomically (a split lock across a cache line is slow, and Linux may warn; never an error).

**Design** (`crates/runtime/src/execution.rs` `split_lock`, `cache.rs` `split_begin`/`split_end`/`split_wait`). FEX lowers each
such instruction to one Arm atomic (SWPAL, LDADDAL/LDCLRAL/LDEORAL/LDSETAL, CASAL, CASPAL: admission's closed set), which takes
an alignment fault when its operand crosses 16 bytes (LSE2). The resident then makes that Arm instruction itself with plain
loads and stores, under the VM's split lock: its owner names the operand's guest-physical pages, then asks every peer that
may hold a shadow (a per-vCPU flag set before any shadow is installed, cleared by a reset) to drop all its shadows (the
existing `SHADOWS` request and kick) and waits for each reset. From then on any peer access to those pages misses its shadow
and waits in its resident (`split_wait`, before installing a shadow, and in `segments::pages` before any access of the
resident's own) until the owner releases; every such wait serves the shadow-drop requests of others (only those: a block of
the waiting vCPU may be live, so code invalidations wait for its next resident entry), which is all any vCPU waits on another
for, so two vCPUs waiting for each other both progress. A shadow eviction for capacity marks the vCPU again before it installs. The read-modify-write is therefore atomic against every access of every vCPU, and it orders as a full fence (each
peer's reset completes its earlier stores; the owner's stores complete before the release), as x86's locked instructions do.
The Arm result registers are written as the instruction would and the block resumes after it: FEX's code around it computes
the x86 results and flags as for an aligned operand. Faults come first: both pages are walked for write in address order
(x86's locked access is a write whatever it computes: #PF with the first faulting byte's address, error W | U), #AC was
checked before, a device page is refused as any atomic there (`unsupported_mmio`), a data breakpoint on the operand runs
the instruction alone so its trap follows it. CMPXCHG16B on an operand that is not 16-byte aligned stays #GP(0).

**Cost** (only split locks pay it): `run_nostop.py splitlock` `cost`, the slope over 20,000 LOCK XADDs: 0.27 us per split one
at N = 1 against 0.007 us aligned; 0.38 us against 0.09 us at N = 4 with three peers looping on their own data (each split
lock resets their shadows, which they then refault).

**Tests.** `run_nostop.py splitlock`: 282 cases per CPL (ADD OR ADC SBB AND SUB XOR, INC DEC NEG NOT, ADD imm, XADD, XCHG,
CMPXCHG equal and not, CMPXCHG8B locked and not, at widths 2, 4, 8; operands crossing 16 bytes, 64 bytes and a page; CF in 0
and 1) at CPL 3 and 0, block sizes default and 1: memory, RAX, RSI, RDX and RFLAGS equal the same instruction on an aligned
operand (FEX's own atomic path; AF after AND/OR/XOR, undefined, not compared); 18 fault cases per CPL (the second page
unmapped or read-only, the first read-only; LOCK ADD and XCHG at each width): #PF with CR2 and error as x86 gives them, the
first page unwritten; CMPXCHG16B misaligned #GP(0); `smp-1/2/4`: each vCPU 20,000 LOCK XADDs on one qword crossing 64 bytes
while vCPU 1 also makes 20,000 aligned LOCK ADD WORDs on its bytes 2-3: the sum exact. In a Linux guest (`probe/slprobe.c`,
every form, width and offset with the result and flags: 252 lines identical to the AMD machine's at N = 1, 2, 4; 1, 2 and 4
threads doing the same concurrent increments: exact at N = 1, 2, 4; the stress lane's `splitlock 8 14 62`: completes).
Negative controls (`nc/`): no shootdown (`smp-4` red: the sum short), CASAL's comparison inverted, the walk made for read
(the fault cases red).

## Checks on the branch head (all three commits)

Compiler **series69-nostop** (`obj/fexc` SHA-256 `bfc17e9a...6d46`; FEX `natemu-nostop` 20ff20257, this tree's shim).
`cargo +1.97.0 test --offline --locked` with and without `FEXC`: host 120 + 2 ignored, abi 67. `run_suite.py --fexc
series69-nostop` in batches under ten minutes each (`suite.sh`; `suite/summary.txt`): every step passes but `pytests`'
`test_check_setup` (the worktree's path layout, as in the gather lane); FEX's ASM suite from series69-nostop's FEX tree at
block sizes default and 1: 2,270 tests, 2,055 pass, 48 precision deviations, 167 unsupported, no failure (1,995 / 48 / 227 at
`correctness`: the 45 split locks, the masked moves, the LIG, BSWAP, SHLD/SHRD and RCL/RCR tests now pass; the unsupported
ones are the forms left #UD, profile #UDs and FEX-only expectations). The kernel selftests of §1 and the probe image of §2-§3
rerun on the final build (`runs/f-*`): as above. `run_slice1_faults.py`'s split-lock probes now expect completion against the
QEMU full run (CMPXCHG8B's: ZF only, the pinned QEMU's full runs keeping stale flags; natemu's flags are the SDM's,
`cx8flags.py`), `run_x1.py` `split-lock-rip` completion.

**Still a machine stop at CPL 3** (outside this change; each needs its own decision): an access by a vector, RMW, atomic,
string or page-crossing form to a device page (`unsupported_mmio`: only if the OS maps device memory to a program); a 16- or
32-byte vector access under alignment checking that is 8-byte aligned (`unsupported_mode`); a pending x87 exception with
CR0.NE = 0 (`unsupported_ferr`, an OS setting); ICEBP after MOV SS with a data breakpoint pending (the x86-selftest lane's
finding 1, a watchdog); a guest-side livelock is contained by the watchdog only.

# M2 checkpoint C: mixed 32/64-bit user code and FS/GS limits

Implementation plan for checkpoint C of [the M2 design](m2-32bit-design.md). The owner added FS and GS limits to
checkpoint C. This plan covers 32-bit user code under the existing 64-bit kernel and four-level page tables.

## Behavior

One process can switch between flat 32-bit and 64-bit code in both directions using far JMP, CALL and RET.
The kernel can use SYSEXIT to return to 32-bit user code. When 32-bit code addresses memory through FS or GS,
the CPU's loaded descriptor determines which bytes it may access. A rejected access faults at its own
instruction, with earlier completed work visible. An eligible access uses the existing guest paging and
memory paths. In 64-bit code, FS and GS keep their base behavior and their limits are ignored.

CS, DS, ES and SS retain the existing compatibility-mode restrictions, including the supported null DS/ES
cases. General non-flat defaults, 16-bit user code, call gates, and Windows boot and application acceptance
remain outside this slice. The [boot interpreter](m2-boot-interpreter-plan.md) already owns boot-time
transfers. Leave its mode routing intact and share existing descriptor checks where the rules agree.

## 1. Make the 32-bit memory paths safe in a mixed address space

[Task 3a](m2-task3a-findings.md#wrapping-at-4-gib) records the current repair. FEX folds some small
displacements into 64-bit Arm memory operands and can update ESP with 64-bit arithmetic across PUSH/POP.
The resident corrects the address on an abort. In a mixed process an incorrect address can be mapped, so
the access can complete before the resident sees it.

- Audit the pinned FEX lowering and emitted sites for every effective-address and ESP update that relies on
  this repair. The pinned fork's `OpcodeDispatcher::DecodeAddress` leaves SIB and GPR displacements in
  `AddressMode` for lowering. Start there and with the scalar PUSH/POP IR, including memory operands and a POP that uses
  the updated stack pointer. Form the 32-bit effective offset before memory, and apply ESP updates at 32-bit
  width at their existing commit points. The Arm address, recovery site, split probes and emulated access
  must agree. Prove that no remaining admitted path needs high-address retry before removing that fallback.
- Keep two distinct checks. Effective-address arithmetic wraps at its address size. An operand whose byte
  range extends beyond the segment limit is a separate check. Preserve natemu's documented fault choice
  for an access beyond the end of a flat 4 GiB segment. For a based segment, the eventual linear-address
  wrap is separate again. An in-bounds segment access whose linear bytes straddle 4 GiB needs routing through
  the existing precise split access path, extended to the two wrapped spans if required. Settle the expected
  behavior with the Intel contract and reference cases before enabling that path.
- Keep the current split-store, atomic, fault and watchpoint contracts, including the checks before any
  native probe that can touch memory. In a single vCPU and address space, use 64-bit user code to touch
  and warm the Arm user shadow for the wrong canonical high address with the permission the tested
  access needs. Then enter compatibility mode with the existing CPL-3 IRETQ path. A present guest
  PTE alone leaves the old abort-and-repair path working and gives a false pass. Populate both low and
  high pages and prove the unmodified runtime fails the case. Check the selected value and the untouched
  sentinel as appropriate. Test both address formation and the next access after a stack update.

## 2. Enforce FS and GS limits at the point of access

Use the loaded FS/GS descriptor already in the CPU bank. A write to the GDT or LDT does not change this
descriptor until a load. A base-only write changes the base without changing the limit or attributes.
For each real memory operand in 32-bit user code, form the effective **offset** at the instruction's
address size, including register-index adjustments such as BT's bit-string addressing. Test the entire
operand at that offset. For an expand-up segment the inclusive last byte must fit the effective limit.
For an expand-down segment the first byte must be above the limit and the last must fit the B-selected
upper bound. Use widened arithmetic for the last byte. Use the loaded type to check the access direction
where applicable. Preserve null-selector handling and report a range or access-right fault as #GP(0).
Only then compute the wrapped 32-bit linear address from base plus offset. Use the Intel SDM's
[limit checking and type rules](https://cdrdv2-public.intel.com/843856/253668-sdm-vol-3a-dec-24.pdf).

### Generated code

Add the check where FEX still has the offset and knows the architectural access width, segment, and
effect order. Start with `LoadSource_WithOpSize` and `StoreResult_WithOpSize`. Audit callers of
`MakeSegmentAddress` and `AppendSegmentOffset` separately, including strings and x87. The latter helper
does not know the access width, and also serves address formation that may have no memory effect.
The same instruction can lower into several Arm operations, so a check on the first
native load's width alone is insufficient. Use the existing branch-to-fault-stub and precise site
mechanism. Place the check after earlier-priority instruction faults such as #UD/#NM and before the
access's alignment or paging checks, probes and architectural effects. The check must preserve guest
flags and the roots the fault path needs.

Keep limits out of translation keys. First make the resident check authoritative. An unconditional
pre-access stub can carry the offset, segment, width and intent, with the existing precise site
record and a continuation that performs that access once. The resident checks its banked
descriptor and returns to the matching access only on success. A fault abandons that
continuation. Retries after changing descriptor state execute the check again. Use the same
base to form the operand's linear address, including on a shadow-abort retry. Resume using
the checked address and access site. An interruption that executes guest code discards the
continuation and rechecks from guest state on reentry. Establish this path on a scalar access first.

For inline checks, the candidate is to publish the two bases, bounds and access attributes in
reserved slots in the existing per-vCPU guest-state page. This page is writable, so the inline
path depends on three proofs. First, every guest-controlled memory access while running
compatibility code is confined to the low 4 GiB, including split paths, helpers and host-served
accesses. The high alias is outside that range and its backing is excluded from guest RAM and
peer/device writes. Second, mandatory admission forbids generated state stores, including
overlapping wide stores, from changing the slots. Third, the resident refreshes both base and
bounds from the bank after leaving 64-bit code and before a checked compatibility block can
run. A 64-bit store to the old alias cannot override those refreshed values. Publish only the
current compatibility user's fields. Clear them before entering any other mode so a 64-bit
read of the existing alias cannot see descriptor state from the previous task. Test all three
invariants, including a prior 64-bit alias store, read after leaving compatibility code and
refresh from banked GS state after a supervisor SWAPGS. Retain the existing neighbor-page
fault tests. Use the resident check where the inline proof does not close. A new EL0 mapping
would change the guest's visible address space and is outside this plan.

No FS/GS descriptor load or mode change runs inside a block. Linked runs use the current
vCPU's view. Refresh on each route that changes descriptor state, including returns from
helpers or events where execution resumes at a new boundary. Mid-instruction restart keeps the
descriptor for that access until a guest event transfers control. Keep the null-segment
identity bits. Version the compiler/object contract so an old compiler cannot supply unchecked
blocks to the new runtime. The persistent cache already keys on the fexc binary digest.
Verify that admission, in-memory compiler answers, relocated objects and the shared pool all
carry or enforce the new contract, including when optional shape checking is disabled.

Stage simple reads, writes and read-modify-write forms first. Inventory every admitted FS/GS form by
where the access actually happens before declaring coverage. Include x87 and SIMD sizes, masked
lanes, bit strings, XLAT and moffs, string sources, split/atomic operations, and any native helper
or host-served MMIO path. Dispatch complex forms to an existing checked resident implementation where
one exists. An unsupported stop before any effect is acceptable on a staging branch only for a
form that a later slice must wire up. Checkpoint C requires every previously admitted form to
complete or fault correctly. List pre-existing unsupported forms separately. Unchecked
fallthrough is a failure. Register-only instructions, LEA and the existing
no-access hints have no check. A segment prefix does not redirect an implicit stack access or the
ES string destination. A zero-count REP makes no access at the instruction's address size.
Generated 32-bit code still refuses 67h before the null-segment cut, so this path uses ECX.
Already supported resident forms retain their address-size rules. Check REP per iteration
and preserve completed iterations. Gather keeps its earlier
destination and mask progress. VMASKMOV and VPMASKMOV preflight all active lanes before any
destination or memory write. MASKMOVQ and [V]MASKMOVDQU retain the existing full-operand
check, including the all-zero mask. Prove the null-segment and limit decisions for zero-mask
forms separately against their specified or existing documented behavior. Add one test where
the second active lane faults for each different commit rule, plus an all-zero case for each.

### Resident accesses

In `execution/segments.rs`, `address` currently returns a linear address and `pages` tests its
end. Separate address formation from segment validation so the code that knows the width and
read/write intent validates the original segment and offset before paging. Use one resident
operand rule for this and apply it in the actual helper paths, including per-lane gather/masked
accesses and string iterations, where one access can fault after another has completed. Audit
callers that work directly with `linear_of` or `pages`. Descriptor-table and other implicit
supervisor accesses keep their existing rules. Delivery consumes the checked fault at the
offending instruction with the proper saved state and error code. #GP does not write CR2.

## 3. Admit mixed-mode transfers

- Extend the independent [segment model](../../tools/models/segment_model.py) before changing
  `far_transfer` or `far_return`. Read immediate or memory far pointers under the source
  mode and address size. Check the selector, type, presence and privilege, and validate the
  target offset using the destination's mode and limit. Treat a fault fetching the first
  target instruction as a fault after the transfer. Check the exact #GP/#NP/#SS/#PF and
  permitted descriptor accessed-bit or stack effects when a transaction faults. Use the
  individual instruction rules in [Intel SDM Vol. 2A for CALL/JMP](https://cdrdv2-public.intel.com/868140/253666-089-sdm-vol-2a.pdf)
  and [Vol. 2B for RET and SYSEXIT](https://cdrdv2-public.intel.com/782151/253667-sdm-vol-2b.pdf).
  The same-level FAR CALL pseudocode truncates the offset for a compatibility destination
  before checking its limit. The corresponding JMP pseudocode has no explicit truncation
  there. Model and test m16:64 CALL, JMP, and RETFQ separately with paired low and high offsets.
- CALL pushes a return frame on the source stack, and RET reads its frame using the size and
  stack rules of the executing instruction. Check the 16-, 32- and 64-bit forms admitted by
  the decoder, including RET with an immediate, high offsets, split frames and descriptor
  faults. Establish the Intel rules for each form instead of using the destination code size
  for either operation. Keep the existing IRET distinction and the CPL-0 interpreter path.
  Check architectural target and transaction faults at the transfer before committing it, with
  only the effects that x86 allows on failure. For otherwise valid compatibility states whose
  default segments this checkpoint cannot execute, retain the `Cpu::compat_refused` fault
  before the first instruction. Test both classes and any differing stack effects.
- Complete narrow SYSEXIT alongside the existing wide form in `execution/syscalls.rs`. Use
  the Intel synthetic CS/SS descriptors, low EDX/ECX for EIP/ESP, and the existing commit
  and CPL-change path. Test the synthesized descriptor attributes, unchanged flags, a
  null SYSENTER selector, executing outside CPL 0 and the first user fault.
- Bank the state with the source mode and let dispatch select the translation under the
  destination's code context. Its mode and null-segment bits already separate the contexts.
  Test both modes at the same low virtual address, including cache reuse and linking.
  Check R8–R15 and XMM8–XMM15, stack state, flags and precise fault/retry even when an
  interruption falls at the transfer boundary. Keep the existing documented choice for
  undefined upper halves of the legacy GPRs.

## Execution and acceptance

1. **Address formation.** Extend `system/compat32` and the existing compiler and fault
   families. Use positive and negative displacements, stack updates with a second access,
   a first/last-byte boundary, and ordinary and single-instruction blocks. Run affected tests
   and the mixed-mode compiler replay. Stop this slice when the mapped-high negative control
   detects the old behavior and the corrected cases pass.
2. **FS and GS.** In the same families, cover each segment independently, base zero and nonzero,
   the last byte and a multi-byte overrun, granularity, both expand-down bounds, null and
   read-only descriptors, base addition that wraps, and a limit failure on an absent or
   misaligned page. Test descriptor rewrite without reload, reload with a different limit at
   the same RIP and bytes, repeated switches at 2 and 4 vCPUs with different bounds, and
   the first fault after retry or interruption. Add cases for helper paths and instructions
   that make multiple accesses, including masked and zero-count cases. Run a 64-bit control
   using the same FS/GS prefixes. The form-to-check mapping must show coverage for every
   previously admitted FS/GS path.
3. **Transfers and integrated check.** Exercise real 64 → 32 → 64 execution in one address
   space, both directions of each supported transfer, a fault and retry in each mode, and
   SYSEXIT. Rerun the two `x86st` expected failures named for task 5
   (`test_syscall_vdso_32`, `sysret_ss_attrs_64`) and remove their expectations only on a
   passing run. Preserve the existing i386, IRET/SYSRET and boot checks. Judge ordinary
   cases against pinned QEMU, Intel behavior against the SDM and independent model, and use
   reference hardware where QEMU's limit checks or frame behavior differ. Keep any
   vendor-specific or architecturally undefined outcomes separate from exact comparisons.
4. **Checkpoint gate and speed.** Run affected families while iterating and the full
   framework gate at the integration commit. Run the checkpoint benchmark round, including
   `guest-hackbench32` and `io5-solo32`, and add a mixed-mode and an FS/GS access case to
   separate dispatch from per-access cost. Run existing workloads and any in-bounds FS/GS
   benchmark that completes identically on both builds in at least three alternating A/B
   rounds on the same machine. The pre-C binary faults on mixed-mode far transfers, so record
   a first-correct natemu baseline and compare with QEMU where it runs the same case. Any
   FS/GS timing case that depends on new fault behavior likewise starts at first-correct.
   Update the benchmark page as required by the repo's measurement rules.
   Optimize a measured regression only after the correctness checks pass.

The first implementation slice is the mapped-high wrap regression and the lowering it exercises.
Bound that slice to one workday of active work before checking the new evidence and refining the
remaining estimates. The earlier 12–20 agent-hour estimate covered task 5 before the FS/GS extension
and the audit of generated checks. Estimate the rest after enumerating the emitted forms and
the resident paths. The planning checkout's submodule is unpopulated. Source inspection used
the main checkout's submodule at the same gitlink, `4eba851e53919c3cac5db2dfb35ed6390e3d9256`.
Use that pin when preparing the first slice.

## Other system emulators

- **QEMU TCG 11.1.2.** `gen_lea_v_seg_dest` truncates the effective offset before adding the segment
  base if the address size is narrower, then truncates the sum at the code mode's width. Ordinary translated
  address formation does not enforce descriptor bounds there, so use QEMU for mixed transfers and wrapping
  that it models, and use the SDM/model plus hardware for limit faults. The protected far return goes through
  the shared RET/IRET helper with different flags. This supports fixing address width at lowering and keeping
  transfer-specific checks explicit in the resident. [Address formation](https://github.com/qemu/qemu/blob/v11.1.2/target/i386/tcg/translate.c#L558-L587),
  [transfer helpers](https://github.com/qemu/qemu/blob/v11.1.2/target/i386/tcg/seg_helper.c#L2137-L2170).
- **Bochs.** Its read/write helpers take a cached descriptor, offset and length. They distinguish normal and
  expand-down ranges and cache the common flat case. Its far branch separates canonical targets from
  compatibility targets. Use these as independent examples for the model and access rule. Where an
  emulator uniformly truncates compatibility branch targets, retain the Intel instruction-specific
  checks and discriminating tests. [Operand checks](https://github.com/bochs-emu/Bochs/blob/master/bochs/cpu/access.cc#L45-L175),
  [far branch](https://github.com/bochs-emu/Bochs/blob/master/bochs/cpu/ctrl_xfer_pro.cc#L116-L151).
- **VirtualBox IEM.** `iemMemApplySegment` takes the access intent and byte count, checks against cached
  segment state, and returns an address for the page-mapping path. A cross-page access goes through a
  separate buffer path. This is the closest model for natemu's resident interface and for keeping
  split routing below the segment check. Its far return stages the frame and the target descriptor
  checks as separate operations. Use the structural choices as references. Validate edge behavior
  against the manual and reference cases. [Segment and mapping path](https://github.com/VirtualBox/virtualbox/blob/main/src/VBox/VMM/VMMAll/target-x86/IEMAllMem-x86.cpp),
  [far return](https://github.com/VirtualBox/virtualbox/blob/main/src/VBox/VMM/VMMAll/target-x86/IEMAllCImpl-x86.cpp#L2067-L2164).

## State of the work

Built on branch `m2-checkpoint-c` (natemu and the FEX fork): section 1, section 3, the resident rule of section 2,
and generated checks for part of the FS/GS forms. [The form inventory](m2-checkpoint-c-fs-gs-forms.md) lists every
previously admitted form and says which are checked and which remain open; checkpoint C needs all of them.
[The inline A/B note](m2-checkpoint-c-inline-ab.md) is the one measurement so far, an experiment on an M3.
The checkpoint gate and the benchmark round of "Execution and acceptance" step 4 have not run.

## Review

**Independent review accepted, November 22, 2026.** GPT-6.2 Astra reviewed the draft and
rechecked the revised plan against the pinned compiler, host and resident paths, reference models
and framework cases. The review found no remaining planning blockers.

The revisions require a warm user shadow in the old-code negative control, preserve all previously
admitted FS/GS forms, distinguish gather progress from masked stores, use comparable benchmark
baselines, and avoid an extra mapping that would break the existing neighbor-page fault contract.
The reviewer agreed these choices address the intended mixed-mode user workload. Legal linear
straddles, per-opcode high-offset behavior, emitted access coverage, and any inline confinement and
refresh proof still need implementation evidence. Review acceptance covers the plan. Runtime
verification belongs to the implementation slices above.

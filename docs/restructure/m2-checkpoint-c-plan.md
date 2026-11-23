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

**Superseded (November 23, 2026) by [the generic check](#generated-code-one-generic-check-and-two-identity-bits)
below.** The text of this subsection is kept as the record of the per-opcode approach that was built first; do not
implement from it.

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

### Generated code: one generic check and two identity bits

**Design of November 23, 2026, revised after its independent review, and built the same day ("As built" at the end of
this subsection says where the implementation differs from the reviewed text and what remains). It supersedes "Generated code" above** (owner's decision after the
independent review of the per-opcode work). Paths below are in the `m2-checkpoint-c` worktree; `FEX/` is
`third_party/FEX/FEXCore/Source/Interface/Core/`, `rt/` is `crates/runtime/src/`, `host/` is `crates/host/src/`.

#### Why the per-opcode approach is replaced

The review found three defects that come from the approach, not from its details:

1. **Unchecked forms.** `FexcCheckSegmentAddress` (`FEX/OpcodeDispatcher.cpp:4722`) returns without a check for any
   opcode outside its list (`:4759`), and FEX then adds the base the plain way. Admission's list has the same shape:
   `scalar_operands` ends its per-form `match` with `_ => continue` (`host/admission/structure.rs:855`). So
   `inc dword fs:[x]`, `movss fs:[x], xmm0`, `lock add`, `push fs:[x]`, `fld dword fs:[x]`, shifts, `bt*`,
   `cmpxchg`/`xadd` and every SSE/MMX/x87 form but four MOVUPS-class loads run with no limit check and are admitted.
2. **Every checked form traps.** All listed forms but dword MOV 89/8B lower to an unconditional branch to a resident
   stub (`tools/fexc/shim/fexc_sites.inc:346`), about 183 ns per access ([the A/B note](m2-checkpoint-c-inline-ab.md),
   an experiment on an M3), also under Linux's TLS segment, which cannot fail.
3. **Four lists.** Each form is named in the FEX predicate (`FEX/OpcodeDispatcher.cpp:4723-4757`), fexc's `JoinSites`
   (`tools/fexc/fexc.cpp:250-262`), admission's iced list (`host/admission/structure.rs:781-855`) and the resident's
   per-form straddle completion (`rt/execution.rs:4082-4215`).

#### The design in one paragraph

A segment that is present, a writable expand-up data segment and 4 GiB long ("flat-limit"; Linux's TLS segment) needs
no check: nothing an in-range offset can do fails. Two new code-identity bits say that FS, respectively GS, is *not*
flat-limit. With a bit clear the block is compiled as this branch's FEX compiled it before any segment-check commit
(fork commit `fc4270d`): FEX adds `fs_cached` / `gs_cached`, no check, no record. That is `main`'s code plus section
1's 32-bit address formation, which is not on `main` and changes every displaced 32-bit access, with or without a
segment prefix. With a bit set, FEX cannot obtain that segment's base except from one new IR
operation, emitted once per guest instruction before any of its effects, which checks the whole operand (offset,
architectural width) inline against a read-only per-vCPU record and yields the base; a failing check, or a segment the
inline test does not model, branches to the existing resident stub, where the reviewed operand rule decides. An access
whose linear bytes run across 4 GiB is not the check's business: the native access aborts (the page at 4 GiB is never
mapped for 32-bit user code) and the resident completes that one *native* access over the two wrapped pages, whatever
x86 instruction it belongs to. Admission verifies the rule from an independent iced decode, with no form list.

This is QEMU's shape for the common case (one translation flag, `ADDSEG`, says whether a base is added at all, and the
base is added in one place, `gen_lea_v_seg_dest`) and Bochs's for the checked case (the access layer tests a
precomputed "this segment needs no check" bit first, then offset, length and direction against the cached
descriptor). We differ from QEMU by having a limit check at all (QEMU's translated code has none) and from Bochs by
checking once per instruction rather than per access (reason under "Where the width comes from").

#### Identity bits

- **Definition** (new `Cpu::limited_segment(sreg)` next to `null_segment`, `crates/abi/src/execution.rs:3208`): true
  when the code mode is `Compat32`, CPL is 3, the segment is present, and it is *not* (`attributes & 0x9e == 0x92` and
  `limit == u32::MAX`), the same "data" test `compat_refused` uses for DS/ES (`:3153-3154`) without the base. A null
  segment is not "limited": the null bit already cuts every access through it (`tools/fexc/shim/fexc_slice1.inc:763-775`).
  64-bit mode ignores FS/GS limits, so no bit there and no new contexts for kernel or 64-bit user code.
- **Where they live**: `Cpu::code_context` (`crates/abi/src/execution.rs:3227-3254`), two free bits (34, 35), beside
  the null bits 5-8. Everything keyed by the context word follows without further code: the resident's lookups
  (`rt/execution.rs:305, 639, 830, 911, 1010`), the per-context L1 dispatcher tables (`Control::l1_contexts`,
  `crates/abi/src/execution.rs:710-715`), the shared pool's key (`pool::Key::new(context, …)`) and the publisher's
  context tag.
- **When they are computed**: never stored; `code_context()` reads the bank each time a block is looked up. A block
  with a bit clear can run under a limited segment only if native code could change the loaded descriptor without
  passing a lookup. It cannot: every FS/GS load in 32-bit code (MOV/POP to a segment register, LFS/LGS, far
  transfers, IRET) is a system cut the resident runs, after which it dispatches by a fresh lookup; SWAPGS and
  WRFSBASE/WRGSBASE are #UD in compatibility mode and change no limit anyway; a write to the GDT/LDT changes nothing
  until a load; the kernel's loads, MSR writes and SWAPGS happen in 64-bit contexts and the return to 32-bit user code
  (IRET, SYSRET, SYSEXIT) is a full `enter` after a lookup under the context of the bank at that moment. Links and the
  fast miss path stay inside one context's table (`rt/execution.rs:2114-2116` records the same argument for the
  published record). This is the lifetime the null bits already have; the implementation adds the limited bits to the
  null bits' unit test (`crates/abi/src/execution.rs:6089-6127`) and cites each route there.
- **Per-vCPU differences**: each vCPU computes the context from its own bank, so two threads with different FS
  descriptors at the same EIP use different blocks when one is flat-limit and the other is not, and the same
  bit-set block with different published records when both are limited.
- **A bit-set block is safe under any descriptor**: it checks against the record of the vCPU running it, published
  from the bank at every full `enter` (`rt/execution.rs:2117`). So the bits need only one property, *clear implies
  flat-limit*; different limits do not need different code.
- **The host**: `Compiler` gets `limited_segments: [bool; 2]` beside `null_segments` (`host/execution.rs:2800`), set at
  `:4597` from the bank; sent as `--fs-limited` / `--gs-limited` beside `--fs-null` (`:3606`), so they are in the
  persistent cache's key, which hashes the request body (`:3659-3663`); added to `tag32` (`:2263-2269`, two bits above
  the guess field) so an in-memory kept answer is never served to the other identity; passed to admission (below).
  fexc refuses the arguments without `--bitness 32` (as `--cpl0` with 32, `tools/fexc/fexc.cpp:512`).
- **Contract version**: `COMPAT_VERSION` 6 -> 7 (`crates/abi/src/code_object.rs:14`, checked at `host/admission.rs:569`):
  an object from the per-opcode compiler is refused. Relocated objects pass admission like any other; the persistent
  cache also keys on the fexc binary digest.
- **The CPL guard**: today `cpl0` is forced false for 32-bit code (`host/execution.rs:4599`), so FEX's
  `!fexc_cpl0` (`FEX/OpcodeDispatcher.cpp:4782`) and admission's `!c.cpl0` (`host/admission/structure.rs:879`) test
  nothing. The record lives in the *user* dispatcher table, so the real condition is CPL 3. It is now part of the
  bit's definition, and admission gets `cpl3` (from `compiler.cpl3`, `:4601`): a 32-bit block that is not CPL 3 and
  holds any FS/GS memory operand is refused, whatever the bits say.

#### The record

`compat_dword_segments` (`crates/abi/src/execution.rs:3214-3226`) becomes `compat_segment_records`: per segment
`(limit + 1) << 32 | base` when 32-bit CPL-3 code runs and the segment is a present, writable, expand-up data segment
with `limit < u32::MAX`; otherwise 0. Storage (two reserved read-only slots of the user dispatcher table,
`area::SEGMENT_SLOTS`) and `publish_segment_view` at every full `enter` (`rt/cache.rs:590-598`, `rt/execution.rs:2117`)
are kept as reviewed. Storing `limit + 1` removes the zero-record trap the reviewer found: with the old encoding a
zero record passes a one-byte access at offset 0. Read-only data, readable code, expand-down and execute-only
segments publish 0 and are decided by the resident on every access (about 183 ns each). That is a performance gap
for descriptors no known workload uses, not a correctness gap; one record per intent would close it and is not
planned.

#### The inline sequence

One IR operation per guest instruction, `FexcSegmentBase(Offset, Segment, Bytes, Write) -> base`, replacing
`FexcSegmentAddress` (`FEX/../IR/IR.json:2992`, lowering `tools/fexc/shim/fexc_sites.inc:306-349`). It is declared
side-effecting, so no pass removes or moves it.

```
and  x1, x28, #~0x1fffff    ; address of the read-only record, exactly today's six instructions
ubfx x2, x28, #21, #9       ;   (fexc_sites.inc:326-331)
add  x2, x2, #1
lsr  x2, x2, #9
add  x1, x1, #512, lsl #12
sub  x1, x1, x2, lsl #22
ldr  x1, [x1, #48 | #80]    ; (limit + 1) << 32 | base; 0 = decided by the resident
lsr  x2, x1, #32
sub  x2, x2, #W
sub  x2, x2, wOffset, uxtw  ; (limit + 1) - W - offset, 64-bit, no overflow (all terms < 2^33)
tbnz x2, #63, stub          ; negative: out of range, or record 0 (-W - offset < 0 for every W >= 1)
mov  wBase, w1              ; continuation: the stub resumes here with x1 = base
```

(When the stub is beyond `tbnz`'s range the compile restarts with the far form, `tbz x2, #63, +8; b stub`. The stub is
a bare `brk #14`: the resident reads the offset from its register, which the site names. A literal offset, moffs, is an
ordinary constant in a register.)

- Width: any `W` from 1 to 32; `offset + W <= limit + 1` is the SDM's expand-up test with a widened last byte.
- Read versus write, expand-down, code segments, a 4 GiB read-only segment (whose `limit + 1` does not fit): record 0,
  so the stub; the resident's `segments::operand` already implements each rule. Null: the null bit's cut.
- Flags: no flag-setting instruction. Registers: x0-x2 only (FEX's temporaries, dead between IR operations, as the
  existing guard assumes, `fexc_sites.inc:322-323`) and the allocated result. The guest roots are intact at the stub
  (`FexcStub(FEXC_SITE_SEGMENT, true)`).
- Stub protocol, kept (`brk #14`, site kind `SEGMENT`): the site's `base_reg` names the offset's register; the
  resident calls `segments::operand(cpu, site.segment, offset, site.width, write)`; `Err` is the guest fault at the
  instruction; `Ok` resumes at `site.imm` with `x1 = base`. The call to `wrapped_segment_access` goes away.
- No straddle test inline (today's `add x3, x2, #3; tbnz x3, #32`): see "Linear straddles".
- Cost on the passing path, per guest instruction with an FS/GS operand in a bit-set block: 12 instructions, one of
  them the record load. Against the bit-clear code that is 11 instructions more (the record load replaces the
  `fs_cached` load; the offset is computed once, see below, and the handler's 32-bit add of the base stays). The
  measured dword guard was 16 instructions and about 0.6 ns per access over flat DS (the M3 experiment); the new
  number comes from the end benchmark. Zero in a bit-clear block. Hoisting the record address per block is possible
  and is done only if that benchmark asks for it.

#### Where the check is emitted, and where the width comes from

- **Emission**: at the handler's first decode of the memory operand. The instruction loop only says, before the handler
  runs (`FEX/Core.cpp`, `FexcSegBegin`), whether this instruction accesses memory through a limited FS or GS (fexc's
  `FexcSegLimited`, the generic "does it access memory through its segment" decision the null-segment cut already made:
  a ModRM memory operand that is not LEA's, a prefetch's or a hint NOP's; moffs; XLAT). `DecodeAddress`
  (`FEX/OpcodeDispatcher.cpp`) then, for that operand (a ModRM memory form, or moffs's literal; never a register or
  immediate operand, whose `Segment` stays empty), forms the 32-bit effective offset without a base, emits the check
  on it (`FexcSegmentCheck`: the `FexcSegmentBase` op) and returns `{Base = offset, Segment = checked base}`. A second
  decode of the operand in the same instruction (the store of a read-modify-write) returns the same pair, so the offset
  is computed once, as x86 computes one effective address, and every access adds the checked base to the checked
  offset by construction (the fork has no CSE pass that could merge two computations).
- **The base comes from nowhere else.** `GetSegment`, the only place FEX reads `fs_cached`/`gs_cached` for 32-bit code,
  returns the checked base for a limited segment (`FexcSegCheckedBase`): nothing for an instruction that accesses
  nothing through it; the check's result in the check's own IR block (FEX's register allocation is block-local);
  otherwise the compile fails (the dispatcher's `DecodeFailure`). That covers `AppendSegmentOffset` and every
  `MakeSegmentAddress` caller without touching them. The sweep found no admitted form that fails this way.
- **Hand-written offsets**, two forms: XLAT (EBX + AL) and BT/BTS/BTR/BTC (the effective address plus the signed
  bit-string displacement; this fork already accesses the whole operand-sized unit there). Each computes its offset,
  calls `FexcSegmentCheck` and adds the base itself. FS/GS string sources stay the resident's cut.
- **POP m needs nothing special.** Its handler reads the stack and then decodes its destination, so the check follows
  the stack read, as x86 orders the faults, and uses the ESP the pop leaves. `Pop()` writes the new ESP to FEX's
  register cache before that, but fexc's precision passes keep every state commit of an instruction after its last
  site, and the check is a site (`tools/fexc/shim/fexc_precision.h`): in the emitted code ESP's register is written
  after the store. A #GP at the check banks the ESP from before the pop (`fs-generic-pop-over`,
  `fs-generic-pop-esp-base-over` assert it).
- **Width and intent come from the instruction's own accesses**, not from a table and not from arguments threaded
  through FEX's callers. fexc's `JoinSites` (`tools/fexc/fexc.cpp`) gives the check the widest access of the
  instruction through that segment and write intent if any of them writes (a read-modify-write's and a LOCK form's
  load is recorded with write intent), and writes the width into the check's `sub x2, x2, #W`. This is what FEX
  really accesses, for every form at once. **It is not trusted**: admission compares both with iced's operand size
  and access for the instruction, so a form where FEX touches fewer bytes than the operand, or forgets a write, is a
  refused block. The offline sweep (below) found none among the 22,197 translated units: there is no table to keep.
- **x87.** A stop at the check banks through `bank_at`, which applies the instruction's x87 map for any site kind, so
  TOP, tags and the registers of an x87 run are right at a segment #GP. The FILD m64 / FISTP m64 pair stays formed
  under a limited segment; the resident keeps the pair's integer at the FISTP's operand check as it does at its
  store's fault (the store's register holds the integer since the FILD), so a retried FISTP stores it exactly
  (`fs-generic-fild-fistp-retried`).

#### Linear straddles across 4 GiB

Reference: x86 wraps the linear address; the AMD reference machine completes an in-limit operand whose bytes lie on
both sides of 4 GiB (`tests/smp` suite `compat-kvm`, case `fs-resident-wrapped-word`). `main` gives #GP from the abort
path for every such access (`rt/execution.rs:4245-4247`, written for flat DS, where it is the limit fault).

Decision (confirmed after review: exact, not a deviation): exact with the bit set and with it clear, by one mechanism
at the level of the *native access*, replacing the per-form `wrapped_segment_access`.

- **Sites must say FS or GS first; today they do not.** `JoinSites` gives load, store, swap, atomic and pair sites
  only 0 (DS) or 1 (SS): `s.segment = i.seg ? i.seg == 2 : base is ESP/EBP` (`tools/fexc/fexc.cpp:266-271`; the
  implicit moffs/XLAT/string branch `:274-281`), and admission refuses any other value unless the kind is `SEGMENT`
  (`host/admission.rs:886`). So an FS operand's access site reads "DS" and the abort path would still give #GP. The
  change, for 32-bit code only (64-bit objects stay byte-identical): `JoinSites` tags the sites of the explicit
  memory operand, and of moffs and XLAT, with 4 or 5 when the instruction's last segment prefix is FS or GS
  (`SegmentOverride`, `:232-237`); push and pop kinds stay SS, so `push fs:[x]` with ESP = FFFF_FFFEH still gets #SS
  from its stack access; split, guard and alignment stubs take the tag of the access they cover, as they take its
  other fields (`:286-`). Admission accepts 4/5 on those kinds in 32-bit objects and checks every site's tag against
  iced's `memory_segment()` for the instruction (push/pop kinds: SS), with the bit set or clear, under `JoinSites`'
  own mapping: FS and GS are 4 and 5; an SS prefix, or no FS/GS/SS prefix with an ESP or EBP base, is SS; everything
  else (ES, CS and DS prefixes included, which `JoinSites` ignores, `:266-281`) is DS. The resident's only
  reader of the tag is `noncanonical` (`rt/execution.rs:3643-3654`: SS gives #SS, anything else #GP), which is right
  for 4/5 unchanged; the host's two constructors of `SEG_SS` sites are test fixtures (`host/admission.rs:1947, 2182`).
- FEX forms the 32-bit sum of base and offset (`FEX/Addressing.cpp:60-66`), so the first byte's address is already
  wrapped. `select_guest_space` keeps the page at 4 GiB unmapped while 32-bit user code runs (`rt/execution.rs`,
  `enter_compat_user` in `crates/abi/src/shadow.rs`), so a native access running across 4 GiB always aborts, or is
  caught by its split-store stub first.
- At the three places that give #GP today for `address + width > 4 GiB` (`access_aborted` `:4245`, `split` `:4509`,
  and the path into `split_lock`), the resident asks: is the site's tag FS or GS, and does
  `segments::operand(cpu, segment, (address - base) as u32, width, write)` pass? If not, the fault that rule returns
  (flat DS/SS: today's #GP/#SS). If so, the access continues with the page pair (last page below 4 GiB, page 0).
- Completion per native site kind, all existing but one: stores and store pairs in `split` (`:4484-4600`, already
  generic over the data register); LOCK atomics and XCHG in `split_lock`/`split_atomic` (`:4627`, `:4726`);
  16- and 32-byte vector accesses as `vector_aligned` does (`:4357`); **new**: general and narrow vector loads and
  load pairs, modelled on `vector_aligned` (walk both pages before any effect, write the transfer register as the
  native word does, resume after it). Each keeps the existing rules: both pages walked first, #AC under alignment
  checking, a data breakpoint steps the instruction, device pages refused or read as ones as
  `wrapped_segment_access` does today (`:4119-4135`; `segments::copy_ram`/`copy_mixed` are reused). A site kind
  outside these (`OTHER`) stops loudly.
- The code around the native access computes the x86 result and flags as for any access, so no x86 form is named.
- (a) bit set: the inline check passed (or the resident did); the native access aborts; the rule above passes again;
  completed. (b) bit clear: no check ran; the same abort; the rule passes for a nonzero base and fails for base 0.
  Both exact, both off the hot path.

**One deviation is recorded, with a test that pins it (`fs-generic-flat-limit-offset-overrun-deviation`), and one
ordering note.**

- With the bit clear nothing tests the offset, so an operand that starts in the last `W - 1` bytes of a 4 GiB based
  segment (`mov eax, gs:[0xfffffffe]`) runs past the limit and x86 gives #GP, while natemu accesses the wrapped
  bytes, as `main` and QEMU do. Closing it means the inline check on every TLS access, which the owner's decision
  excludes. The pinning case uses a base of at least 32: with a base below `W` the access also straddles 4 GiB
  linearly and the abort path gives the exact #GP.
- An instruction's check comes where its handler first decodes the operand. No admitted handler has an effect or
  another access before that except POP m's stack read, which x86 also makes first (the AMD answers of the 85
  generic cases agree on every fault's state), so no deviation is known here; it is listed because the order is
  FEX's, not enforced by admission beyond "before every access through the segment".

#### Admission's rule (`host/admission/structure.rs`, replacing `scalar_operands` and `scalar_segment_checks`)

Mandatory for every 32-bit object, with or without the optional shape check (as `:712` does today):

1. Decode the whole source with iced (as `:771-777`); a based string in a block is refused (kept).
2. For each instruction, take the memory operands whose segment is FS or GS from iced's
   `InstructionInfoFactory::info(..).used_memory()` (the `instr_info` feature is already enabled, `Cargo.toml:28`;
   today's code uses only `memory_segment()` and `memory_size()`, `:777`). `NoMemAccess` (LEA, hint NOPs) is no
   access; `Read` and `CondRead` are reads; `Write`, `CondWrite`, `ReadWrite` and `ReadCondWrite` are writes. If
   there is an access and the block is not CPL 3: refuse.
3. If that segment's bit is set: require exactly one `SEGMENT` site for the instruction with that segment,
   `width == memory_size`, `intent ==` the class above, its native words exactly the sequence above (W, slot offset,
   the test's branch to a `brk #14` stub, `site.imm` at the final `mov`), inside its instruction, with no branch into
   it and no relocation in it, and placed before every access of the instruction through that segment (the stack
   accesses of PUSH, POP and CALL are SS's and may precede it). A `SEGMENT` site with no such instruction is refused.
4. In a block with the FS (GS) bit set, any load of `frame::FS_BASE` (`GS_BASE`) is refused (the frame-field rule at
   `:2082-2104`; fixtures at `:5670-5682`). So a FEX path that adds the base the plain way is refused whatever its
   opcode. The read-only record is loaded only inside the verified sequence (today's `segment_view_reads`).
5. With the bit clear: no `SEGMENT` site may exist.
6. Bit set or clear: an access site says FS or GS exactly when its instruction's operand is through that segment
   by iced's decode (push and pop kinds never do). The DS/SS choice of the other sites is fexc's, as before.

Admission still cannot prove that the offset register of the sequence holds the operand's effective address; the
single computation above removes the two-values risk, and the per-addressing-form cases cover the rest.

#### What happens to the per-opcode code

Deleted, by new commits (no history rewrite on either branch):

- FEX fork: `FexcCheckSegmentAddress`, `FexcRmwAddress` and their eight call sites (`OpcodeDispatcher.cpp:257, 303,
  1159, 3041, 4710-4785, 4824, 4963, 5101`; `AVX_128.cpp:47`), `FexcStoreImmediate`, the BMI/XCHG/CMPXCHG/MOVUPS
  helpers the family commits added. Kept: the 32-bit address width change (`fc4270d`).
- fexc: `JoinSites`' `SEGMENT` branch and its form fields (`fexc.cpp:250-262`), the inline-dword lowering.
- Admission: `ScalarOperand`, the iced form list, the per-form native shape checks (`structure.rs:742-900` and
  `scalar_segment_checks`).
- Resident: `wrapped_segment_access` (`rt/execution.rs:4082-4215`) and the `SEGMENT_*` result flags
  (`crates/abi/src/code_object.rs:15-`).

Kept: `segments::operand` and every resident caller; the stub and resume protocol; the record storage and its publish;
the string cut; every test (cases that named the cold path now assert the counter below).

#### Tests (existing families, no new scripts)

- **Offline sweep** (`tools/fexc/test_slice1.py`, suite `fexc` in `tests/static`): every admitted 32-bit memory form,
  with an FS and a GS prefix, compiled with the bit set and passed through admission: each is checked or refused,
  none is admitted without a record, no admitted form is `unknown`, and the forms that fail the same-block rule are
  listed. Admission unit tests: a wrong W, wrong intent, missing record, stray record, raw `fs_cached` load, altered
  sequence word, a check placed after an access, a wrong site tag, and a non-CPL-3 block are each refused.
- **Bit clear is the pre-check code** (`tests/static`, a case beside `check_compat_address`): the Linux TLS forms
  (`mov %gs:0x14` moffs both ways, `sub`/`xor`/`cmp %gs:0x14`, `cmpl $0,%gs:0xc`, `call *%gs:0x10`, `mov %gs:(%eax)`,
  a scalar SSE load, `lock cmpxchg`) compiled without the bits: no `SEGMENT` site, no record load, and native words
  equal to a listing kept in the test, generated by the fexc of FEX `fc4270d` (the test header says how to regenerate
  it). Site tags are not code bytes and are compared separately. The 64-bit byte-identical gate of the fork is
  unchanged.
- **Every class is checked when the bit is set** (`tests/system` `compat32-resident-limits`, `tests/smp/compat_limits.py`):
  one in-range and one out-of-range case per class, the fault leaving memory, registers and flags unchanged:
  `inc dword fs:[x]`, `add fs:[x], imm`, `not`/`neg`, a shift, `push fs:[x]`, `pop fs:[x]`, `call`/`jmp fs:[x]`,
  `lock add`, `lock cmpxchg` failing its compare, `xadd`, `bts fs:[x], reg`, `movss fs:[x], xmm0`, a 16-byte load, an
  MMX form, `fld dword fs:[x]`, `fstp qword fs:[x]`; a read-only and an expand-down segment for one read and one
  write; W = 1 at offset 0 under a segment the record encodes as 0; pending #MF before the segment #GP; a #GP in the
  middle of an x87 run with TOP, tags and every ST register intact; FILD m64 / FISTP m64 of a value above 2^53 with
  the FISTP faulting once and retried; POP m whose stack read faults (#PF) while its destination is out of range.
  The answers for about 20 of these classes, in and out of range, are seeded on the AMD reference machine through
  the existing `compat-kvm` suite (QEMU checks no limits).
- **Every addressing form reaches the check with the right offset** (same cases file): the last in-range and first
  out-of-range offset through disp32 only, a base, base + index * scale, an ESP base, an EBP base, a displacement
  that wraps the 32-bit sum, moffs, XLAT, BT with a positive and a negative register index, POP m with an ESP base.
- **Inline, not cold**: a new resident counter of `SEGMENT` stubs taken, reported with the per-CPU results (where
  `compat_dword_segments` is reported today, `crates/driver/src/drivers/program.rs:3130`): 0 for in-range cases under a
  writable expand-up segment, exactly the expected count for read-only and expand-down ones. Existing cases that
  named the cold path (`*-generated-*-wrapped-*`, the resident-wrapped cases) are rewritten to assert this counter
  and the abort-path completion instead.
- **Straddles**: load, store, read-modify-write, LOCK, 16-byte vector and x87 operands across 4 GiB with the bit set
  and with it clear (4 GiB limit, base just below 4 GiB), against the `compat-kvm` answers; the base-0 control still
  faults; `push fs:[x]` and `call fs:[x]` with ESP = FFFF_FFFEH give #SS; the two recorded deviations' cases.
- **Stale bits**: the same EIP and bytes run under a flat-limit FS, then after a reload with a small limit (must
  fault), then flat again (must run with stub count 0), in ordinary and one-instruction blocks, across a link, and
  after a round trip through 64-bit code that reloads FS; a GDT rewrite without a reload changes nothing;
  `compat32-smp-limits` at 2 and 4 vCPUs with one vCPU flat-limit and the others with different small limits at the
  same instructions; the persistent-cache and kept-answer unit tests (`host/execution.rs:5747-5798, 6651`) extended
  with the bits.
- Test-only and natemu-altering parts go through the gate rule in [the testing page](../testing.md); the gate runs
  once, at the end, after review.

#### Benchmarks at the end (named, not run now)

On the machine that holds the control group, three arms, alternating, three rounds: `main`; this branch's natemu
with FEX at `fc4270d` and no segment code (section 1 alone, which changes every displaced 32-bit access); the final
branch. The second arm separates section 1's cost from this design's, and is where "bit clear costs nothing" is
measured (arm 3 against arm 2). Cases: `io5-solo32` (the 32-bit CPython configure and make; steps `i386-configure`,
`i386-make`), `guest-hackbench32`, `guest-hackbench32-int80`, `guest-startup32`, `guest-sqlite32`; plus `io5-solo`
and the 64-bit round as the no-change control. `compat-tls-natemu` extended to a limited-segment loop over several
forms (dword and byte MOV, `add m, r`, `cmp m, imm`, `inc m`, a scalar SSE load) and a flat-limit loop over the same
forms; it cannot run on `main` (first-correct baseline), so its comparison is flat DS against flat-limit FS (expected
equal) against limited FS (the inline cost). The benchmark page is updated from these cases only.

Not planned: sharing one compile of a block that has no FS/GS access between the limited and flat identities
through fexc's args-read mechanism (`host/execution.rs:2251`). 32-bit answers are keyed by `tag32`, the null bits do
not share either, and the resident's pool is keyed by the whole context anyway; it is taken up only if
`guest-startup32` shows the extra compiles.

#### Estimate

| Part | Agent hours |
| --- | --- |
| FEX hook, the single offset through `DecodeAddress`, `GetSegment`, IR operation, lowering, POP m, the x87 pair rule; fexc arguments and the width function | 11-16 |
| Site tags 4/5 in `JoinSites`, admission's tag check, the resident audit | 3-4 |
| Identity bits, record, host arguments and keys, contract version | 3-5 |
| Admission's generic rule and its unit tests | 6-9 |
| Resident: stub arm, wrapped native completion (one new load path, three widened), counter | 6-9 |
| Deleting the per-opcode code on both branches | 2-3 |
| Tests: sweep, classes, addressing forms, straddles, stale bits; rewriting the cases that named the cold path | 11-16 |
| AMD reference answers for about 20 classes, in and out of range | 3-5 |
| Review rounds and fixes | 7-10 |
| **Total** | **52-77**, plus queue time for the gate and the benchmark round (the build took far less: no width table, no per-form resident code) |

Largest uncertainty: the width function's SSE/AVX rows and the forms that fail the same-block rule (the sweep bounds
both), and the native load words the new completion must decode (sign-extending loads, vector element loads).

#### Changes after the first review (November 23, 2026)

- Access sites carry FS/GS tags (they did not; the straddle rule depended on them), checked by admission.
- "Compiles as `main`" is stated against FEX `fc4270d`; the benchmark has three arms.
- The offset is computed once and consumed by `DecodeAddress`; cases per addressing form; passing-path cost restated.
- x87: the map at a segment stop confirmed; the FILD/FISTP m64 pair is not formed under a limited segment.
- Admission: placement rule, conditional accesses, the info factory; the IR operation is side-effecting and its base
  is used only in its own IR block.
- POP m is checked after its stack read; the offset deviation's test uses a base of at least 32.
- Straddles stay exact (the reviewer's alternative, keeping `main`'s #GP as a deviation, was not taken).
- Estimate 37-57 -> 52-77 agent hours.

#### As built (November 23, 2026)

Built on `m2-checkpoint-c` (natemu) and the fork's `m2-checkpoint-c` (FEX `b9e9add`, `3ef751a`, `2cf6c8e`). Where the
implementation differs from the reviewed text, the text above has been corrected; the differences were:

- The check is emitted at the operand's decode, not in the instruction loop (so POP m needs no exception and no handler
  sees a base before its operand is decoded); the offset is still computed once.
- Width and intent are derived from the instruction's accesses and verified by admission, instead of a decode table
  in fexc: nothing to keep per opcode, and the sweep shows no form where the two disagree.
- The stub is a bare `brk`; the resident reads the offset from its register (12 instructions on the passing path).
- The FILD/FISTP m64 pair is kept under a limited segment and its integer is kept at the check.
- fexc ended a block at a LOCK CMPXCHG through FS or GS (a rule of the per-opcode check): removed, which made the
  bit-clear code of all 28 pinned forms byte-identical to the fexc of FEX `fc4270d`.
- A wrapped store that meets only RAM and firmware pages is made in the split path; a read-modify-write's wrapped
  load is walked for writing; data breakpoints see both spans.

Results (M-series Mac, through `tools/natemu-test`, narrow selections only; the gate and the benchmarks wait for the
code review):

- The sweep (`crates/host` `every_limited_fs_form_is_checked_or_refused`): 35,861 encodings, 22,197 units translated
  with a limited FS, all admitted with their check except VPCMPESTRI/VPCMPESTRM with VEX.W = 1, which admission
  refuses with or without the check (16 encodings; their helper call's shape, not this work). No form is translated
  only without the check.
- `tests/system` `compat32-resident-limits` 1,423 passed, `compat32-smp-limits` 21 passed; `tests/static`
  `fexc-address32` and `fexc-precision-32` passed; `tests/smp` `compat-kvm` `amd-generic`: the 85 generic cases have
  the fixture's expectations on an AMD Ryzen 7 8845HS under KVM.

After the code review (November 23, 2026):

- **Seven forms passed the check and then accessed memory without the segment's base**: MOVD m32 from an MM or XMM
  register, VMOVD m32, MOVNTPS, MOVNTPD, MOVNTDQ and MOVNTDQA. Their handlers took the operand's address without a
  base (upstream FEX's code, for every mode, so 64-bit code and Linux's TLS segment were affected as well as limited
  segments). FEX `50ab9a6` gives them the base; that changes the code of the FS- and GS-prefixed forms of these
  instructions in 64-bit code too.
- **Admission now makes that hole impossible for 32-bit code** (`Verifier::segment_bases`): every access of an
  instruction through FS or GS must compute its address from the segment's base, the check's `mov wB, w1` under a
  limited segment or the load of the segment's field otherwise, through a chain of adds and moves. The design's
  claim that "the base comes from the check alone" said nothing about an access that adds no base at all.
- The check is a fault point for fexc's precision pass, not a commit (fexc died on a floating-point instruction
  through a limited segment with an x87 or SIMD exception unmasked; found by the pending-#MF case, which exists now).
- A missing check stops the compile instead of failing the decode; the far form of the check's test is not admitted
  (no admitted block is long enough to need it).
- The sweep also covers `[disp32]`, `[ebx+ecx*4+disp32]`, GS, the instruction second in its unit and unmasked
  floating-point exceptions (55,531 units), asserts that fexc answers every request, that nothing is translated only
  without the check, and that the unchecked `[ebx]` unit is admitted too.
- The wrapped load completion accepts only zero-extending loads and the widths that can occur; straddle cases for
  8-, 16- and 32-byte vector loads, a 16-byte store and the x87 integer loads; the kept x87 integer's register is tied
  to the store's by admission.

Not done, or not verified:

- No case reaches the completion's `SITE_MISMATCH` stops: every site kind and width admission allows for an access
  that can run across 4 GiB is handled (a byte cannot; aligned 16- and 32-byte accesses cannot; stores go through
  their split stubs), so the stops are reachable only by a compiler change.
- A check placed after an access of its instruction cannot be forged from a real object without moving code, so the
  mutation test does not have it; the other refusals of the check's rule are each exercised.
- 64-bit code is outside admission's source decode: the seven fixed forms there rest on the FEX fix and three VM cases.

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
and [the generic FS/GS operand check](#generated-code-one-generic-check-and-two-identity-bits), which replaced the
per-opcode checks built first (the inventory below is the record of that state). [The form inventory](m2-checkpoint-c-fs-gs-forms.md) lists every
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

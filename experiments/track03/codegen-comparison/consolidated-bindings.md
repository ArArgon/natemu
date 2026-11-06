# Consolidated fixture bindings: Box64 and TCG

September20, `/root/comparison_sources`, bounded independent source/byte review
of root-produced `box64-consolidated-01` and `tcg-consolidated-01`. No guest
execution or export modification. All14 native-file hashes (six cases plus NOP
per candidate) match their sealed SHA256SUMS. The new
`consolidated-bindings.json` records raw identities, exact slice bytes/hashes,
and required changes; it does not accept an execution adapter.

Raw root: `/private/tmp/natemu-track03-comparison-raw`.
Box64 `.log` files contain instruction words and guest/native boundaries. TCG
`.disassembly.log` contains IR, native words and helper-pointer pools. Native
bytes were additionally wrapped unchanged in a temporary MachO object and decoded
by `xcrun otool -tvV`: `/private/tmp/natemu-consolidated-bindings.disassembly.txt`.
All offsets below are **original native-file offsets**, not relocated offsets.
Ranges are half-open. The original entry RIP is0x401000.

## Box64

`x0` is the resident state base. Guest RAX/RCX/RDX/RBX/RDI are
`x10/x11/x12/x13/x17`; guest XMM0/XMM1 are memory at+0x90/+0xa0. The branch
and FP bodies load scratch native q0/q1 and write q0 back before successful exit.
These snippets do not admit arbitrary missing guest state or a C helper ABI.

| Fixture | Range | Concrete work and binding |
| --- | --- | --- |
| fp_addsub |[0,28)|Two vector loads, FADD/FSUB d24 and two INS into q0, final STR q0.|
| fp_muldiv |[0,28)|Same with FMUL/FDIV.|
| fp_sqrt |[0,20)|Two vector loads, FSQRT d24,d1, INS and STR q0.|
| branch_zero |[0,44)|Actual XOR RCX,1; scalar load; PXOR; TEST; B.NE at36; taken-path vector publication at40.|
| branch_carry |[0,72)|Actual ADD; scalar load; PXOR; SETZ; flag publication; B.CC at64; taken-path vector publication at68.|
| integer_div |[0,16) plus[92,132)|Actual XOR EDX,EDX/MOV RAX,RCX/CBZ, then actual native UDIV/MSUB and flag handling. Restricted fast-path adaptation required.|

The three FP slices have no PC-relative instruction, call or guest-memory access.
They clobber q0/q1/q24 and update only XMM0 low64, preserving XMM0 high64;
XMM1, native guest GPRs and NZCV remain unchanged. Configure FPCR explicitly and
declare whether FPSR/MXCSR exceptional behavior is measured. Do not infer full
SSE exception semantics from finite ordinary inputs.

Branch-zero's B.NE36 targets64, where the original false path stores q0; its
true guest RIP is0x401020 and false RIP0x401010. Branch-carry's B.CC64 targets92,
likewise the false vector-store path; true RIP0x40101f, false RIP0x40100f. A compact
adapter may retain the listed prefixes, relocate only those conditional branch
displacements to a false veneer, and supply separate fixed-RIP exits that both
publish q0. Preserve the actual condition, not a predicate computed by the
oracle. The omitted original dispatches contain Linux data-link addresses and
must not execute. Scalar fault sites are raw4/24 respectively. Branch scratch is
x3, plus x1/x26 in carry's flag-publication prefix, q0/q1 and NZCV.

Branch-zero stores deferred kind0x3f at+0x44c and TEST result at+0x460;
decode logical ZF/SF/PF, CF=OF=0. TEST AF is architecturally undefined and must be
excluded from a common flag oracle. Carry stores kind7 and original C/D/sum at
+0x450/+0x458/+0x460; its prefix additionally publishes native ZF/CF into x26.
Decode the ADD recipe for full defined arithmetic flags. As in existing bindings,
old x26 alone does not contain every current deferred flag.

Integer-div's CBZ at12 targets92 because the actual first XOR zeroes x12.
For nonzero RBX, unsigned high-half0 prevents quotient overflow. The proposed
restricted adapter retains the two ranges, retargets CBZ to the copied fast path,
and traps its impossible false path. Raw16..92 otherwise contains a generic
helper call (BLR x7 at56, literal load52), state spill/reload and an external
literal; that helper is **unbound**, not implicitly executable. Output quotient
is x10, remainder x12, scratch x2; x26 and deferred-kind memory are changed.
DIV arithmetic flags are undefined, so compare the guest result and unchanged
state instead of requiring another candidate's flag values. Divide-by-zero is
outside this restricted probe and must be rejected before entry.

## TCG

`x19` is env. Existing offsets remain RAX0/RCX8/RDX16/RBX24, RIP256,
cc_dst272/cc_src280/cc_op296, XMM0 at960 and XMM1 at1024. Allocate the leading
16 bytes because generated code also writes `can_do_io` at env-12. These are
Linux-user direct-memory exports, not system-MMU lowering. The leading [0,12)
poll is excluded from the branch slices and its cost is not silently supplied.

| Fixture | Range | Actual branch and required exit relocation |
| --- | --- | --- |
| branch_zero |[12,108)|CBZ x20 at72 targets92. False RIP store is0x401010, exit B88; true0x401020, exit B104.|
| branch_carry |[12,140)|CMP x20,x22 at100; B.LO104 targets124. False RIP0x40100f, exit B120; true0x40101f, exit B136.|

Copying each contiguous range preserves its internal conditional branch.
Relocate its two unconditional exits to the approved wrapper; do not execute
the original branches to the absent shared prologue/dispatcher. Both paths retain
the actual emitted guest RIP materialization. Zero clobbers x20/x21,q0/q1;
carry clobbers x20..x23,q0/q1,NZCV. Canonical guest state is in env on either
exit. Zero records cc_op27 (logic-Q), carry cc_op11 (ADD-Q) and original D in
cc_src. Scalar fault offsets are32/44 (relative sliced body20/32). TEST AF and
the native NZCV limitations stated above apply.

The four other TCG cases are **not yet executable slices**:

| Fixture | Raw helper call(s) | Literal pool | Binding needed |
| --- | --- | --- | --- |
| fp_addsub |BLR x16 at32,64|96,104|helper_addsd/subsd|
| fp_muldiv |BLR x16 at32,64|96,104|helper_mulsd/divsd|
| fp_sqrt |BLR x16 at36|64|helper_sqrtsd|
| integer_div |BLR x16 at52|88|helper_divq_EAX|

FP argument setup is x0=env, x1=destination env+960, x2=old destination env+960,
x3=source env+1024. `target/i386/ops_sse.h:513` defines arithmetic calls through
`float64_*` using `env->sse_status`; `helper_sqrtsd` at561 does the same and
preserves the upper lane. Exact helpers, their SoftFloat callees and status
layout/control need binding; replacing the calls with handwritten native FP
would measure a different producer. These BLRs clobber x30 and require a real
caller-clobber contract, unlike the branch leaves.

DIV arguments are x0=env,x1=RBX. `target/i386/tcg/int_helper.c:367`
`helper_divq_EAX` reads RAX/RDX, calls static `div64` at296, writes quotient/
remainder and raises `EXCP00_DIVZ` using GETPC on zero/overflow. Importing the
actual helper with a constrained domain and contained exception exit is a
possible next adaptation; a native UDIV substitution is not this output's
helper cost. No unknown retained Linux helper pointer is an admitted VM target.

## Adaptation under queued claim19

September20/21 bounded source review by `/root/comparison_sources`; no new build,
run, download or framework. This applies the **current** INV03-01 claim19,
not claim14's superseded universal per-instruction polling. Sources below are
retained under the three `*-consolidated-01/upstream` trees at the same pinned
revisions already listed in the investigation. Line numbers refer to those trees.

**Decision:** the measured cached-body advantage can justify treating FEX as the
performance-led implementation candidate; this source review establishes neither
complete conformance nor a whole-generator winner. No examined requirement
forces normal FEX/Box64 execution to spill every register or canonicalize every
instruction. Conversely, cold native-frame retention alone does not make either
application translator a system emulator. The concrete adaptation differences
are recovery-root production, compound-effect lowering and system context;
source familiarity or the number of previously audited fields is not a burden
metric. Box64 remains a credible cached-state alternative; TCG has materially
more directly reusable system/REP semantics, independently of which backend wins.

### One concrete common resident binding

Keep a reserved native frame (GPRs, NZCV, native vectors/FP control, SP and the
bounded live stack area), pinned immutable code/metadata, and a typed operation
record. Retaining those references relinquishes active RAM authority only at a
safely detachable phase. The record binds admitted guest decode/context, current
instruction, completed effects, next subaccess and admitted routes. It must never
infer that CPUState/x64emu slots supersede live registers. Bind architectural
values to registers, explicit resident slots or bounded recipes valid at the
actual fault/checkpoint; keep native PC-relative anchors distinct from guest RIP.
This is an implementation of the already accepted contract, not a new frame ABI.

Every external target uses protected admission/pin handover. Every internal CFG
cycle and long helper/REP path gets a bounded legal stop opportunity; finite code
size is insufficient. On queued service after mutation, the resident semantic
owner finishes only the saved instruction's remaining effects, then dispatches
current source. A retained interior continuation is usable only when it cannot
enter stale following instructions. Admitted route obligations, completed MMIO,
atomic units and helper-held locks remain explicit. This can share a permanent
semantic service among generators; it does not require converting whole blocks
between two IRs or importing a Linux signal/longjmp runtime.

| Surface | FEX | Box64 | TCG |
| --- | --- | --- | --- |
| Native/deferred roots | Core.cpp:643 flushes SRA before an instruction; OpcodeDispatcher.h:1177 distinguishes full/SRA-only and upper-state publication. Preserve actual roots through optimization/allocation and emit site maps; final carry convention is not every mid-block convention. | dynarec_arm64_arch.c:271 adjust_arch consumes prior-completed-instruction records, reconstructs native/deferred flags and vector caches. Retain this machinery, replace Linux ucontext input with the resident frame, and extend phase coverage where needed. | Keep side-effect global coherence from tcg.c:3735/3964/4210 and the resulting native stores. tcg-cpu.c:85 restores PC/CC selector, not arbitrary lost registers. Removing that coherence needs a replacement root contract. |
| Metadata limitation | IR.json:193 GuestOpcode carries a guest offset and side-effect marker, no value roots; JIT.cpp:1005/1117 writes PC correspondence. RegisterAllocationPass.cpp:280/342/349 spills and frees registers. A PC map or before-access marker cannot keep a killed old value alive. Claim02-28's allocation-visible fault-root seam remains an actual compiler obligation. | adjust_arch:286 explicitly selects ninst-1. That is useful boundary metadata, not an exact record of which subaccess of the current instruction completed. Native-PC phases plus scratch-value/progress descriptions are needed for queued compound faults. | insn_start/search records and restore_state_to_opc identify instruction/CC state; modified access/helper lowering still needs phase/effect records. Existing system fault restoration does not itself implement the new FIFO drain owner. |
| Calls and exits | JIT/BranchOps.cpp:46 and JIT.cpp:570 provide entry/link seams; warmed branches bypass the linker callback. Replace target/link policy with admitted veneers, preserve fixed roots, and bind actual helper clobbers/stack/effects. | dynarec_arm64_helper.c:542 call_c explicitly saves selected GPR/flags and FP cache around BLR. Price this actual call ABI where used; it is not a universal spill rule. Secondary entries/return caches require declared references or adapted external tail transfers. | Actual scalar-FP and DIV exports call helpers, unlike the FEX/Box native FP leaves. Bring in the actual helper and status semantics or report the cell unbound. Replace host exits with resident targets and bounded cold service. |
| System context/startup32 | Core.cpp:564 chooses LONG_64 or LEGACY_32; this supplies a 32-bit decoder path, not CPL/control/segment/privileged semantics. Bind those selected semantic inputs and context changes explicitly. | dynarec_native_pass.c:98/290 accepts is32bits and prefixes. This likewise supplies decoding, not protected-mode system semantics. | translate.c:3425 initializes a system DisasContext with CPL/IOPL, PE, CS/SS widths, LMA, segmentation and MMU index. Those semantic inputs are reusable; resident memory/events/helper ownership still replace ambient QEMU runtime assumptions. |

Required startup32 remains in scope; the accepted direct-boot architecture does
not require real-mode execution. Neither a FEX LEGACY_32 switch nor Box64's
is32bits flag proves startup32. This is an integration obligation for the chosen
system semantic owner, not a reason to acquire an Intel host or a count of
missing opcodes. Conversely, retaining QEMU system semantics does not inherently
select TCG as the ordinary generator.

### A concrete compound-effect counterexample and its bounded remedy

FEX `OpcodeDispatcher.cpp:3250` explicitly identifies unsupported partial-faulting
REP in STOS. Its REP STOS path uses MemSet, then writes RCX=0 and final RDI;
MOVS at3277 uses MemCpy and writes final RCX/RSI/RDI after it returns. Reusing
these paths unchanged cannot claim queued system REP fault completion. This is
a **blocker to unchanged lowering**, with a concrete remedy: route REP to a
bounded resident semantic service or change that lowering to publish precise
iteration/subaccess progress. It does not block using FEX for ordinary generated
work, and the exceptional/REP cost must be measured at its actual frequency.

Box64 `dynarec_arm64_00.c:2025` REP MOVSB includes4/8-byte batching, postindexed
RSI loads, postindexed RDI stores, then RCX decrement and a loop. A destination
fault can occur after the source load and source-pointer advance. The queued
record must retain that loaded temporary and phase; ninst-1 metadata alone
cannot justify replaying the source access, especially MMIO. Its count-controlled
loop also needs a bounded stop opportunity. Wider batching must be limited to
accesses whose checked RAM/semantic contract permits it; it cannot silently
replace byte-sized device effects. REP STOS at2251 has the analogous batching
and stop-latency issue. These are concrete Box64 lowering changes, not a claim
that its existing metadata is useless.

TCG `translate.c:1304` do_gen_rep already maintains iteration count and current
CC selector, records dynamic CC state and limits its internal loop through
REP_MAX before its gen_eob path. That is a directly reusable progress/bounded-exit
seam. Its memory subaccess and route decisions must still join the resident owner;
upstream system-mode coverage is not automatic conformance to claim19.

**Disposition:** no examined source counterexample rules out a FEX-based or
Box64-based architecture after these named adaptations. There are real blockers
to claiming either unchanged frontend meets the contract, and real implementation
work for all three. A preference for FEX should therefore be explicitly provisional
and performance-led, with these ordinary obligations and cold-service costs
attached. Do not promote finite leaf PASS to general recovery acceptance, require
full-ISA implementation before making a provisional choice, or assign synthetic
all-register publication to every normal boundary. The expensive x87 accuracy
path remains a separate cost/compatibility decision under the owner's priority.

### Follow-up: ordinary SSE status and preserved before-images

Root identified, and this reviewer checked, an additional concrete mismatch:
FEX `OpcodeDispatcher/Vector.cpp:3164` GetMXCSR masks with0xFFC0, explicitly
keeping controls/masks/DAZ but discarding sticky exception bits0..5.
RestoreMXCSRState at3293 applies the same mask before writing CPUState.mxcsr.
The exact-valued scalar-FP fixtures do not test this behavior. Lower-priority
x87 accuracy does **not** waive required SSE/MXCSR status or enabled exceptions.
This blocks importing these paths unchanged as the system SSE owner. It does
not establish that Box64's corresponding status behavior is correct; no such
conformance evidence is supplied by its exact-valued leaf outputs either.

A native status/control adapter or a checked inherited FP service could repair
that contract, but neither its complete semantics nor its cost is established
by the current timings. In particular, an enabled SSE exception may require the
old architectural destination after native arithmetic computed a candidate
result. The native frame captures only values that survived. The adapter must
retain the needed before-image, aggregate/translate status and commit the result
only at the proper semantic phase. Calling all such changes cold would be
incorrect: classification, status maintenance or additional preserved roots may
add ordinary-path work and can change the measured FP comparison.

FEX allocation makes this concrete, not hypothetical bookkeeping.
`RegisterAllocationPass.cpp:640` marks the last backward-seen argument killed;
its forward pass explicitly skips GuestOpcode at~670 and handles register
replacement/eviction before later uses. Claim02-28 therefore requires recovery
uses to keep old values live **through** a fault-bearing operation and the map
at its actual allocated native site, not at a later reload or merely at the
source instruction marker. For compound instructions, keep precisely the
current phase's before-images and completed-effect temporaries; do not demand
all historical values forever. Those extended lifetimes can force spills or
limit scheduling. Their number and generated cost are unmeasured beyond the
finite accepted examples. Saving all native registers after an overwritten
value has died cannot recover it, and a pure recipe may not replay a guest
memory read to manufacture it.

Consequently, the current evidence supports prioritizing FEX's measured ordinary
integer paths for implementation investigation, but **does not establish FEX as
the preferred generator across required FP/status and queued-recovery behavior**.
The preference must remain provisional until those named adaptation costs are
bounded against the competing implementations. This is a finite unresolved
cost/contract, not a request for full-ISA implementation or a new general audit.

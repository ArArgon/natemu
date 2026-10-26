# ARM64 execution ABI — provisional draft 0

This is the proposed contract between natemu's host translator and its resident
ARM64 runtime. It includes the experiment needed to test that contract. It is
self-contained for design review; source records at the end explain provenance.
The normative rules below are **proposed design choices (Inferred)**, not observed
runtime conformance. Generator selection remains deferred.

Owner: [INV-03-01](../investigations/INV-03-01.md). Status: independently reviewed
and accepted for owner delivery as a provisional design/method proposal on
September 28, 2026; the subsequent paging amendment also passed focused review.
I1–I3 now implement finite Rust/assembly entry/capture, admission, actual FEX
requests, publication/cancellation, allocated recovery, paging, helpers and
partial/FP paths. I4 adds finite N=1/2/3/4 concurrent faults, remap/source
publication, pending-event handoff and sustained maintenance profiles; independent
completion review is recorded in INV-03-01. These use one resident coordinator
with ordered ticket handoff, real FEX execution between at most eight protected
heads, all-participant invalidation acknowledgements and guarded retained-code
reuse. Event input is an identified level-request fixture, not native IRQ or full
APIC/IDT delivery. The [I4 setup/profile](../../implementation.md#i4-concurrent-composition)
records capacity and unsupported behavior. Exploratory development results are
recorded in INV-03-01. I4-V extends those profiles with reviewed kernel-address
load/stack and concurrent alias paths; its
[current coverage and next slices](../investigations/INV-03-01.md#i4-v-active-development--first-v1-slice-october-25-2026)
remain distinct from the original experiment matrix below. The full
ABI below remains provisional and unimplemented outside that scope. The original
review accepts a concrete experimental design, not full runtime conformance.

**Current implementation direction, October25:** develop this provisional ABI,
the Rust runtime and host-side FEX adapter together using the
[implementation plan](../../implementation.md). Generated ARM64 fragments, not
FEX itself, execute inside the VM. Connect actual translation requests early;
manual fixture maps are only bootstrap evidence. All shared state and coordination
are designed for configured N vCPUs. Use two for bring-up, then three/four for
adverse interactions before broad ISA expansion. Performance feedback starts on
the supported paths without waiting for boot or device completion.

The technical invariants below remain requirements; representation changes update
consumers and relevant tests together. Section7 retains the original 35-cell core
and names the additional producer/N-vCPU coverage required by the revised plan.
Section8 now governs per-increment capacity and resource declarations; the earlier
six-hour/480-agent-minute proposal is historical, not an active allowance.

## 1. The behavior you should expect

The host translates x86 instructions into ARM64 code. That code runs inside a
virtual machine, alongside a small resident runtime. Ordinary guest loads and
stores use native memory mappings. The runtime owns those mappings, handles
faults and interrupts, and decides when generated code is safe to enter or retire.

An **ABI** is the agreement that lets the translator and runtime work together:
where values live, what a call may change, and how execution can stop and resume.
It does not define x86 instruction semantics. The producer must supply correct
instruction behavior and truthful metadata; the runtime cannot recover a value
that the producer has already destroyed.

The proposed behavior is:

- **Keep ordinary execution cheap.** Guest values can stay in native registers
  across blocks and resident helper calls. We do not write the entire virtual CPU
  back to memory after every instruction. A helper is a runtime routine called
  by generated code; most such calls stay inside the VM.
- **Never guess after a fault.** At each faultable native instruction, metadata
  says which guest instruction is in progress, where its current values are,
  and which effects have already happened. Capture native state before using it.
  Repair an internal mapping miss, complete a saved operation, or report the
  guest exception as distinct outcomes.
- **Coordinate changes before making them visible.** A mapping change or a write
  to translated source waits for existing memory users to stop safely. A stopped
  CPU may retain registers and code references; it need not erase its work or
  finish canonicalizing every value before another CPU can proceed.
- **Do each visible effect once.** If a copy operation has read a source byte and
  then faults writing its destination, keep that byte. Resume the pending write;
  do not reread a device or replay completed iterations.
- **Deliver events at valid guest boundaries.** A native interrupt may arrive in
  the middle of a translated instruction. It requests runtime attention; it does
  not itself authorize an x86 interrupt at that point.
- **Keep old code alive while somebody still references it.** Invalidation blocks
  new entry immediately under the coordination protocol. It does not free a fault
  frame's metadata or a pending return address. Resumption after a source change
  completes only the saved instruction and then looks up current code.

For example, CPU A faults while writing the source of the next instruction B.
Its write remains pending. CPU B finishes a bounded interval under the old view.
The runtime then commits the write once, revokes the old translated B, completes
A's current instruction and dispatches the new B. It never resumes the stale
fallthrough simply because A's saved native PC still points there.

**Paging is part of the runtime contract.** The guest keeps ordinary x86 page
tables. The runtime reads them and builds ARM shadow mappings that send the same
guest virtual address to the correct owned RAM. A shadow is a cached translation,
not a second set of guest-visible page tables. Once a mapping is admitted, ordinary
loads/stores use the ARM MMU without a software walk on every access. If a native
access faults, the runtime distinguishes a missing shadow from a real x86 page
fault. It also maintains the guest's accessed/dirty bits and honors address-space
changes and invalidations before acknowledging them. Guest physical addresses
never grant access to private runtime memory.

The milestone starts with small hand-written ARM64 controls and real FEX-generated
fragments, then an actual producer loop and two/three/four-vCPU checks. It tests this interface, including failures
and interleavings. It will not boot Linux, implement a full translator or device
set, or demonstrate workload speedup. Those remain project obligations.

### Decisions and tradeoffs

| Choice | Benefit | Cost or limit we must expose |
| --- | --- | --- |
| Live registers plus recovery maps | Avoid full-state publication on ordinary paths | Producer must retain otherwise-dead recovery values; metadata and register pressure can grow |
| Resident helper gateways | Avoid a host exit for ordinary runtime work | Gateways must preserve live roots and native FP state correctly |
| One coordinator, queued faults | Reuses accepted ordering/lifetime design | Mutations serialize; ordinary work must get bounded admission between batches |
| Concurrent shadow hits, coordinated miss walks | Keeps ordinary RAM access native while making table updates and fills coherent | Translation misses serialize; workloads with frequent misses may pay heavily |
| Immutable native objects with guarded links | Clear publication and reclamation rules | Entry guards and data-link indirection cost instructions |
| Cold completion after coordinated mutation | Prevents replay and stale continuation | Requires a bounded resident handler for the admitted instruction |
| Fixed finite experiment profile | Makes the contract executable and falsifiable | Does not prove every instruction, emitter or platform fits |

Universal per-instruction spills/polls, unrestricted native calls between objects,
and a fully generic compiler interface are not selected. These would change the
cost or lifetime problem without answering the immediate question. Report any
need to change compatibility, isolation, these tradeoffs or the budget to the
owner before dependent work. Routine equivalent register/layout choices remain
the implementer's responsibility.

## 2. Execution and trust

Generated code and runtime execute at native EL1. Emulated x86 privilege is data;
it gives the guest no authority over the resident runtime. The compiler and
resident code are trusted to obey this contract. The design does not sandbox
malicious native compiler output inside the VM.

Guest RAM is non-executable as native code. Only admitted translation objects
and permanent runtime text may execute. Private stacks, state banks, metadata and
page tables occupy runtime-owned addresses outside the guest canonical ranges.
Guest physical addresses are resolved to owned backing; they are never native
pointers or installation addresses.

For an ordinary direct guest access of length n > 0 at address A, after x86
address-size/segment processing, require E=A+n-1 without unsigned overflow and
either A,E <= 0x00007fffffffffff or A,E >= 0xffff800000000000. Otherwise enter
checked semantic access handling. This predicate excludes private addresses;
it does not decide every x86 fault or authorize a split/atomic access. Shadow
permissions, access width/type and current execution grant must also admit it.
Each legal subaccess is checked when due; a compound instruction is not globally
preflighted to erase its permitted partial effects.

Guest RIP, indirect branches and returns go through guest dispatch. Native
targets come only from validated object handles, permanent helper entries or
pinned typed continuations. Helpers classify parameters as guest addresses,
resident handles or scalar values. A pointer-sized guest value remains guest data.

### Paging interface

The runtime owns x86 translation semantics and native shadows. The producer owns
the effective-address expression and supplies the architectural intent and address
form of each access, including implicit helper accesses. The ordinary paged path is:

```text
x86 instruction computes a guest linear address
    -> admitted ARM shadow hit -> owned guest RAM
    -> native fault -> classify the registered access
         -> coordinated x86 page-table walk
              -> eligible RAM: publish a shadow, retry the unperformed access
              -> serviced route: perform that subaccess once, return its result
              -> x86 fault: report the precise exception and retained effects
```

The walk yields a guest physical address (GPA); the machine's physical resolver
maps that GPA to owned RAM, a device endpoint or a defined hole response. Only
the resolver supplies native backing. The ARM MMU walks resident-owned shadow
tables, not the guest's x86 tables. Native shadow hits remain concurrent; a new
miss enters the existing coordinator before its first guest paging-entry read
and retains that authority through required A/D updates and access completion,
fault or eligible shadow publication. This intentionally serializes misses, not
every guest access. Walks cannot hold that authority while waiting for disk,
network, compilation or guest progress.

For an admitted guest paging-off mode, apply the required segmentation,
address-width and A20 rules, then resolve the resulting guest physical address
directly. A native mapping miss can still require repair; it must not trigger an
x86 page-table walk, PTE A/D update or fabricated #PF. Physical ownership and
native access permissions remain enforced. Guest mode/context identity belongs
to compiled code and shadow validity even when there is no guest paging root.
Finite V2 profiles admit flat protected32 and selected segment/A20/service paths;
unsupported segment and address policies remain explicit coverage obligations.

Equal decode width does not imply equal address-space identity. The mixed V2
profile uses 32-bit FEX output for both protected32 paging-off and IA-32e
compatibility32 owners. It admits the complete fixed control/segment profile
before resolving accesses and entering code; each request binds VM, owner,
incarnation, source/context generations, architectural EIP, mode and source GPA.
Each owner publishes into its private native code slots. Shared-source changes
invalidate both modes, while an x86 PTE remap changes only paged routes.
An intentionally read-only native identity mapping may trap for internal repair
without creating a guest dirty bit. This fixed-context profile does not establish
mode-transition or context-generation reuse correctness.

In protected32, architectural EIP is a segment offset. Keep it distinct from the
CS-derived linear fetch address and the resolved source GPA, and bind all three
to the admitted context. Exception state and FEX's logical compilation PC use
the architectural offset; source ownership uses the resolved physical identity.
Likewise, a registered native operand may carry a segment offset rather than a
linear address. The semantic adapter must declare that form, and the runtime
applies the admitted segment rights/limit/base before physical resolution. Native
FAR alone is not the architectural linear fault address. The per-instruction V2
direct-mapping profile admits only matching native/GPA page offsets; other bases
need explicit address formation or service rather than dropping low address bits.

| Interface item | Mandatory information |
| --- | --- |
| Address-space context | VM/vCPU and context incarnation; paging mode; CR3 and applicable hidden root state; permission controls including CPL, CR0.WP, EFER.NXE and CR4.SMEP; invalidation and memory-policy generations. Numeric CR3 alone is not identity. |
| Access request | Operation/site identity; original linear address; exact subaccess width and span; fetch/read/write intent; architectural access privilege; atomicity class and retained progress. A native EL1 load is not automatically a supervisor x86 data read. |
| Successful translation | GPA plus owned route/incarnation; guest leaf extent; effective permissions; required/completed A/D effects; table/source dependencies; memory type/cache mode; direct-or-service eligibility and lifetime identity. |
| Fault result | Guest vector, error information and original faulting linear address, with any legal earlier effects. Address-formation #GP/#SS precede paging where required; host/transport failure is not #PF. |

The runtime's committed guest control state is authoritative; requests bind its
generation rather than trusting a producer-supplied alternate CR3 or permissions.
Fetch, read and write are distinct. User accesses require user permission and
user writes require writable permission; supervisor writes respect CR0.WP.
Fetch respects NX and supervisor SMEP restrictions. Source translation uses
guest fetch permission, even though admitted ARM code later executes elsewhere.
Guest RAM remains native NX; guest execute permission never makes x86 bytes
directly executable by the ARM CPU.

**A/D and outcome ownership.** The walker performs required accessed (A) and
write-dirty (D) mutations through the same tracked-writer authority as guest,
helper and backend writes. A read fill with D clear cannot grant native write
permission. The first write returns to the runtime to establish its actual write
permission and D effects before writable shadow admission. A genuine failing
walk preserves its permitted earlier effects; it is not rolled back wholesale.
An internal shadow repair does not modify guest CR2 or inject #PF.

For one access serial the memory owner returns exactly one of `RETRY_ACCESS`,
`ACCESS_COMPLETED(value/effect receipt)` or `GUEST_FAULT`. Retry means the operand
effect has not happened. ACCESS_COMPLETED means this subaccess already happened;
it advances the saved instruction's progress and is never retried. It is distinct
from section5's COMPLETE for the whole instruction. Its remaining subaccesses
may still fault or require service. MMIO, incompatible/non-WB RAM, no-fill mode,
and split/atomic cases lacking direct eligibility use typed checked service.
Already-completed device reads are not repeated to recreate a value or a walk.

**Changes and invalidation.** Tracked page-table writes eagerly retire dependent
shadows under the accepted conservative policy. Clearing A/D can therefore force
a rewalk. Guest CPU, alias, helper, backend and walker A/D writes all participate;
no host writer can bypass tracking. Changing PTE bytes is not itself execution
of remote guest INVLPG. CR3 reloads, INVLPG and paging/permission/type-control
changes use typed resident operations that validate, commit required guest state,
invalidate affected native translations and incompatible code contexts, and
redispatch. Already-admitted route obligations complete before a conflicting
change is acknowledged. Ordinary cached hits may be evicted more broadly where
x86 permits, but required guest invalidation semantics still hold.

Use per-context shadow roots and tagged native address spaces or a conservative
flush on switching; draft 0 selects the latter for this finite experiment.
Before reusing a native tag, root or backing allocation, close admission, drain
the covered old accesses, perform native TLB maintenance and its barriers, then
publish/acknowledge the new identity. The two-vCPU shootdown test must show that
no covered old access can execute after that acknowledgement. Guest software
still sends its architectural remote shootdowns; runtime coordination makes each
required invalidation effective and does not invent a different guest protocol.

**Inherited coverage versus this experiment.** The selected machine uses 48-bit
linear and 40-bit guest-physical addresses, four-level long-mode paging with
4-KiB/2-MiB leaves, and no LA57, PCID/INVPCID or 1-GiB pages. Larger guest leaves
can use 4-KiB native shadow pieces, split at permission, type and physical-route
boundaries. Paging-off startup, legacy32 and legacy PAE remain required inherited
contracts; the original P1–P8 experiment tests four-level paging. I4-V's separately
bounded V2/V3 slices extend execution-mode coverage. In particular, legacy PAE's
four hidden PDPTEs are guest architectural state: shadow eviction or INVLPG cannot
reload them; a required CR3/control transition performs their validated load.
This differs from a disposable shadow cache and must remain representable in
the context even while its execution test is deferred.

## 3. Native register and state contract

**I3 profile adaptation:** real allocation assigns guest R12 to x16 at entry and
RDI to x30 at the store/return map. The former x16 branch-scratch and x30-only-link
conventions are therefore not universal ABI rules. Full ERET entry preserves x16;
a checked direct branch leaves the body without destroying x30. Return capture
uses SP_EL1 because an ordinary stack capture below SP_EL0 would overwrite the
producer's still-live 768-byte spill frame. Fault and helper captures use the same
separate stack; all GPR/Q/native flags/FP controls are restored before continuation.
The two 35-root maps are emitted by the actual FEX allocator. This finite profile
admits exact generated body/map artifacts, binds fresh worker output to them, and
consumes the delivered maps. It does not claim arbitrary FEX-program coverage or
an efficient final spill strategy. The typed bank retains unchanged YMM high,
x87, MXCSR, control and segment fields; including them in the catalog does not
implement instructions that modify all of those fields.


Draft 0 is a same-build experimental ABI, not a stable plugin or on-disk format.
One versioned runtime header will define record offsets for both producer adapters
and runtime; generated assembly offsets must come from it. A mismatched version,
profile, size or field catalog rejects the object before entry. I1/I2 supply the shared Rust records in `crates/abi` and assembly offsets through
`offset_of!`/`global_asm!`; its exact-byte fixture profiles are not a general object
or producer metadata format. Current I3 uses ABI version3 and the same bounded
128-byte register packet, with request-bound chunks for a 1168-byte code/map
export. Historical I2 runs used version2. Extend records and consumers together.

| Native resource | Draft 0 rule |
| --- | --- |
| x28 | Producer state-bank base. Its layout is identified by the producer profile; this is not permission to treat upstream FEX offsets as natemu's runtime structure. |
| SP | Private resident stack, 16-byte aligned; never guest RSP. Each object declares its maximum frame and exact spill-base relation to entry SP. |
| x30 | Profile-declared resource. It may carry an architectural root: the I3 FEX store map places guest RDI here. Such profiles enter helpers and return gates with direct branches and registered continuations, preserving x30. A BL/RET adapter is permitted only when its maps prove the overwritten link value is not live or preserve it first. |
| Other GPRs, Q0–Q31, NZCV | Available according to the producer's declared entry map and clobber set. No assumption that platform C calling conventions preserve guest values. |
| FPCR/FPSR | Explicit native control/status owned by the executing profile, captured before runtime FP work and restored for native resumption. Guest MXCSR/x87 state is separate. |
| Stack header | Resident-owned vCPU handle, frame incarnation and allocation bounds, reachable from the declared entry frame. Generated spill addresses cannot overwrite it. |

The vector landing uses a preallocated per-vCPU exception stack and a resident
context anchor, installed at bring-up. The proposed anchor is TPIDR_EL1;
generated code and helpers cannot change it. After architectural exception entry
selects SP_EL1, read interrupted SP from SP_EL0 and retain its frame identity
before repurposing either stack. This anchor's use must be checked
in the implementation's native control-state admission.
The proposed execution profile uses EL1t with SP_EL0 for generated/helper frames
and SP_EL1 for exception landing, retaining native EL1 authority in both paths.
The landing saves scratch registers before reading the context anchor. Native
SVE/SME use is excluded from this profile; Q-register capture must cover all
enabled native vector state. Other required native controls are profile inputs.
Arm's exception model sections 5.2/5.6 specify software register preservation and
the separate exception stack; see the primary reference in section 10.

The starting FEX-friendly binding for the selected fragments is RAX=x4, RCX=x7,
RDX=x5, RBX=x6, RDI=x11, XMM0=Q16 and XMM1=Q17. x26/x27 carry deferred PF/AF
information; the carry interpretation of NZCV is site-specific. Other guest fields
use their declared current bank locations. This is a fixture binding, not an
invented complete FEX register map. The profile must reject any fragment that
clobbers x28 or violates its stack/entry convention without a declared adapter.

### One authoritative value per field

Each vCPU has a backing state bank and a current **root map**. A root is the
current value needed to reconstruct a guest field or unfinished operation. A
backing slot is authoritative only when the map says it is current. Full native
capture after a fault cannot restore an old value destroyed before that fault.

Every map accounts for all 16 guest GPRs, RIP, defined RFLAGS, 16 full YMM values
(low and high 128-bit halves), MXCSR, x87 register/control/status/tag/pointer
state, and the admitted CPU profile's segment/control/system state. Fields not
modified by this experiment remain initialized, current bank fields. Upper vector
halves cannot be omitted merely because the fragment uses only XMM. Runtime-owned
events, clocks and committed memory are live owners, never rewound from an entry
snapshot. The finite experiment changes only its declared subset of these fields.

A field location is one of: native GPR; native vector lane; initialized private
spill or state-bank range; literal; or a bounded, named pure reconstruction recipe
with explicitly located inputs. A map may reference shared unchanged map entries;
that is metadata compression, not an implicit assertion that entry state is current.
No recipe reads guest memory or performs I/O. Unsupported recipes reject admission.
The initial recipe set is copies/lane assembly, 64-bit ADD flags from preserved
operands/result, and the checked FEX PF/AF/NZCV conventions for the selected sites.
Each map declares defined/undefined flag masks and carry inversion explicitly.
For ADD64, let S=(A+B) modulo 2^64: CF=(S<A), ZF=(S=0), SF=S[63],
OF=(~(A xor B) and (A xor S))[63], AF=(A xor B xor S)[4], and PF is even
parity of S's low byte. The admitted FEX endpoint recipe instead takes PF from
even parity of x26's low byte, AF=(x27 xor x26)[4], N/Z/V from NZCV and CF
from C or its inverse as explicitly tagged. Do not apply that endpoint recipe
to an intermediate site whose producer used a different convention.

Maps are keyed by object incarnation and the exact native offset/phase after
allocation. They include staged destination values and completed subaccesses.
Interrupted asynchronous code can retain a raw native frame and resume unchanged;
it need not have an architectural boundary at every native PC. Every synchronous
faultable site, helper transfer, bounded stop and external-transfer phase does
need its applicable metadata. Unknown synchronous fault PCs contain the VM and
fail conformance; they are not guessed guest faults.

### Rust and foreign code integration

The resident implementation uses `no_std` Rust behind assembly entry/capture/
restore gates; the host uses Rust with `std`, and the FEX adapter retains C++.
Ordinary Rust helpers use an explicit C-compatible calling convention behind
those gates. Their compiler clobbers do not change the live-root preservation
contract. Save native flags, full enabled vector state and FP controls before
Rust can use them, including during involuntary exceptions. Compiler-generated
vector instructions remain possible even when source uses only integer types.

Shared layouts use fixed-width fields, explicit representation/alignment and
versioned generated offsets checked by Rust, C++ and assembly. An outer C-layout
record does not fix a nested Rust-default layout. Exchange handles/offsets and
validated bytes, not Rust references, collections or host pointers. No unwind
crosses these seams; expected failures return typed outcomes and a panic contains
the VM with bounded diagnostics. Preallocated fault/interrupt resources must not
reenter allocation while holding execution/coordination authority.

Guest RAM and mappings are external shared memory, not an exclusive Rust borrow
spanning guest execution. Encapsulate raw accesses, aliasing, atomicity and barriers
in audited interfaces. Memory safety does not establish guest semantics or the
correctness of a shootdown. The first build must admit its actual Rust target,
linker/stack layout, code generation and new EL1t capture profile.

## 4. Objects, entry and helper calls

The following records are the logical interface. All handles contain an index
and incarnation within a VM lifetime. Counts, offsets, widths, alignment and
addition overflow are checked before use. Host messages contain offsets and
bounded bytes, never resident pointers. Record arrays are bounded by section 8.

| Record | Required content |
| --- | --- |
| Object | ABI/profile identity; VM/allocation incarnation; guest entry/decode context; immutable source bytes and source/route generations; native bytes/relocations; frame bound; entry/root/phase maps; external-link cells; resident completion-handler IDs |
| Site | Native offset/phase; guest RIP and admitted instruction identity; current root map; access kind/width; completed effects and next subaccess; permitted retry/completion/fault outcomes; stop and transfer classification |
| Helper | Permanent helper ID; typed inputs/outputs; native clobbers and stack bound; affected guest fields; effect/commit phases; fault behavior and legal stop points |
| Operation | VM/vCPU/operation incarnation; admitted decode; captured frame and maps; retained source/route references; saved operands/results/progress; completed effects; next subaccess and completion handler |
| Transfer | Old object, pending destination and operation-origin references; phase naming any loaded but not yet entered target |

**Enter.** A permanent resident entry gate validates current source/context and
open admission, reserves the vCPU's frame/operation capacity, then acquires or
transfers the execution reference. Before changing maps, reconcile the outgoing
current values with the destination entry map using a bounded parallel copy
(save cycle inputs before overwriting) or explicit materialization. Include flags,
vector halves and spill/frame lifetimes; a metadata change alone never moves a
value. Reject an unsupported conversion before transfer. Entry-adapter phases
retain both maps and references until handover completes. Then install the entry
map, profile state-bank pointer and private stack. A warmed link uses the same guard. A code
miss goes to permanent resident service; it does not retain a guest-RAM grant
while waiting for host compilation.

**Call a helper.** Enter a site-specific gateway using the profile-declared
transfer. The I3 allocated profile uses a direct B and registered continuation;
BL is valid only after preserving any live x30 root. The call-site map describes the state before argument setup and
every temporary that setup could destroy. The gateway first saves live roots in
the helper's declared clobber set, including flags and full vector widths, then
passes a resident-owned call-frame pointer in x0 to the helper. Other arguments
are typed fields of that frame. It does not publish the whole guest CPU by default.
The helper writes staged outputs and a tagged outcome into that frame. The gateway
first restores non-output roots and native controls, then installs only the
outputs/flags allowed by that outcome under the post-call map. Once an output is
committed, an old saved destination cannot overwrite it. On an outcome that does
not commit that destination, its prior current value remains authoritative.
Publish the new map only after its values are installed; retain staged results
and phase metadata through any intervening capture. Restore/install NZCV after
any flag-changing gateway work. Gateway text has its own phase/return metadata.

Permanent helpers cannot recursively enter generated code, wait for compilation,
or block on host I/O while holding an active RAM grant. A helper that can access
guest memory uses the same admission/progress rules as generated code. Pure
helpers declare no guest memory effects. A C function's ordinary ABI is not a
preserve-all guarantee; the adapter owns any extra saves, including the full
width of live Q registers. Reference: AAPCS64 section 6.1, linked in section 10.
The gateway installs the helper's declared native FP-control environment before
the call and restores the caller's afterward; guest MXCSR/x87 status changes
only through declared semantic outputs, never by copying arbitrary native FPSR.

**Leave.** Normal dispatch supplies a current root map and next guest RIP.
Control-register, mode, segment and relevant FP-control transitions leave through
a typed semantic helper: validate first, commit state/context identity together,
then redispatch. An old specialized object cannot continue after that transition.
Unsupported native lowering transfers to a pre-admitted resident handler; lack
of a handler rejects that experimental object, not an otherwise valid x86 instruction.

## 5. Faults, completion and event delivery

The permanent exception landing captures X0–X30, original SP and native PC,
PSTATE/NZCV, Q0–Q31, FPCR/FPSR and exception syndrome/address information before
using their values. It retains the referenced stack/spills and code/metadata.
Capture happens with native nesting controlled; an unexpected fault in the
capture/maintenance path is contained failure. Completion records never allocate
memory on this emergency path.

| Outcome | What may happen next |
| --- | --- |
| `RETURN` | A completed helper call with no outstanding effect returns to its registered native continuation and post-call map, only while code/route/context remain eligible. If they changed, use the saved instruction's cold completion instead. |
| `RETRY_ACCESS` | Only the unperformed access may execute again; its instruction phase, route/code identity and before-images still match, and no effect would be replayed. Restore the qualified native retry frame. |
| `COMPLETE` | Apply the saved instruction's remaining effects once, using retained decode/progress. Publish the post-completion state, then dispatch current source. A precommit map cannot describe this state. |
| `GUEST_FAULT` | Publish the correct instruction state and permitted prefix effects, with vector/error/address information. Transfer to the guest exception owner; no following guest instruction retires first. |

Queued service is a suspended operation, not another architectural success or
fault. The experiment's guest exception owner records a canonical exception
packet for its oracle. Building full x86 delivery/IRET machinery is outside this
milestone; the ABI includes the required handoff and state, not a claim that the
test packet is a Linux-compatible exception implementation.

A native interrupt records a durable pending request. If capture is not at a
legal guest/coordination boundary, restore the native frame unchanged and reach
the next declared bounded stop under the old view. Guest IF does not suppress
coordination. Guest interrupt masking/shadows/priority still govern x86 delivery.
Events arriving during helpers, translation waits or drain remain pending and
are checked again before guest execution resumes. Wait entry publishes its wait
state and rechecks the event sequence before sleeping; a coalesced wake cannot
erase the durable request. Native IRQ masking inside maintenance is bounded;
guest CLI never turns into indefinitely masked native control interrupts.

The host vCPU-owner thread alone reads/writes its vCPU state. A forced HVF exit
only wakes that owner; arbitrary native state is preserved. It may arrange the
configured resident control interrupt and resume that state to reach a resident
stop. It cannot treat the exit as quiescence or overwrite transfer registers.
Only the permanent register-transfer service stop admits bounded code/request
chunks after guest roots are saved. Request IDs and cancellation remain checked
there. This inherits the accepted owner-thread/forced-exit contract, not a new
host-write publication path.

## 6. Coordination, publication and lifetime

One resident coordinator owns `OPEN → CLOSING → EXCLUSIVE → RELEASING → OPEN`.
Each vCPU is ACTIVE, DRAINING, PAUSED or PARKED. ACTIVE/DRAINING may access the
old memory view. PAUSED has no native memory effect in flight but retains a
frame and possibly an already-admitted route obligation. PARKED additionally
has canonical state and no native continuation references. Host exit is none
of these acknowledgements by itself.

1. Queue the pending protected effect, close new entry and publish a stop request.
   Peers reach bounded exits; another fault queues another saved operation.
   Nobody waits for queue service while retaining a detachable active grant or
   holding the coordinator mutex.
2. Each peer acknowledges only after its memory work is complete with the
   accepted completion barrier. An unfinished indivisible atomic unit cannot
   publish a prefix and acknowledge. Such operations use checked service before
   starting unless their bounded native unit has a proven completion path.
3. EXCLUSIVE begins after all active grants drain. Before a conflicting mutation,
   finish any already-admitted route obligation against its retained old route,
   or prove effect-free cancellation. A saved pointer is not permission to
   silently rebind an admitted access. Only an unadmitted next subaccess resolves
   afresh. Nested maintenance completes inside this transaction, without waiting
   on a second transaction that needs the first to finish.
4. Commit the head operation, update affected identities/mappings and complete
   invalidation acknowledgements. Cold-complete affected saved instructions,
   then use current-source dispatch. Preserve every completed prefix.
5. Reopen admission. After at most eight queued heads, admit a waiting ordinary
   vCPU round-robin for one real bounded interval before closing again. Unlocking
   a mutex without permitting execution is not fairness.

Every external entry tests admission. Every internal cycle has a bounded stop;
the experiment caps straight-line work between stops at 256 native instructions
and REP-like service at one completed iteration. This is a work bound under
healthy scheduling, not a real-time latency guarantee. Instruction-specific
debug/trap/shadow boundaries remain precise through object termination or service.

Installation copies complete code/metadata into an unreachable resident slot.
Under the same coordinator authority, revalidate the request/source/context,
register dependencies, perform required code-cache and executor synchronization,
then publish. Native text and recovery maps stay immutable; only owned data-link
cells change. Hardware TSO does not replace publication, TLBI or completion
barriers. The implementation must bind the inherited platform maintenance sequence
to its actual mappings and independently check that binding before evidence runs.

Invalidation removes eligibility and incoming links before reuse. A transfer
stopped after loading a target but before entering it still retains that target.
Census old object, destination and operation-origin references (at most three per
vCPU), queued operations, frames, return PCs and outstanding deliveries. Free an
object only when all these references are gone. Counters never wrap into a reused
incarnation; drain and start a new lifetime instead. Teardown closes admission,
cancels requests, waits for active accesses/deliveries and drops retained references
before storage reuse. Late replies fail their lifetime check.

### Configured vCPU count

All vCPU state, participant/acknowledgement sets, operation slots, queue bounds,
stacks and code-reference accounting derive from admitted N. Validate host/API
limits and reserve capacity before native entry. No operation assumes a unique
"other CPU" or simultaneous scheduling. Retain one pending guest-operation slot
and the declared reference bound per vCPU, plus separately bounded host/backend
work. Recompute total reserves and trace bounds for each configuration.

Two-vCPU cases isolate interleavings; three/four-vCPU cases must add simultaneous
mutation, retained continuation and new entry/publication, with a delayed peer
and multiple queued faults. Check every participant's acknowledgements, progress
and reference release. Eight queued heads followed by round-robin ordinary work
is the existing fairness policy, not a vCPU-independent latency guarantee. Keep
its safety/progress obligations while measuring latency as N grows.

## 7. Experiment and independent oracle

**Question:** can this interface preserve live state, exact effects and bounded
coordination across the declared vCPU configurations, while admitting actual
generated fragments and producer requests without universal full-state publication?

Passing supports a scoped ABI candidate. A failure identifies which interface or
producer obligation must change. Failure to obtain actual fragments or metadata
leaves fit unestablished. Neither outcome selects FEX over TCG or proves speedup.

Use the M4 host of record, macOS 26, the recorded SDK/toolchain and HVF entitlement,
one owner thread per admitted vCPU, hardware TSO enabled/read back, private runtime mappings
and a synthetic guest state/RAM image. Record exact model/OS/SDK, native feature
profile, signing, placement and host limits at run time; abort admission on a
missing required capability. No M2 extrapolation or CPU placement guarantee.

Reuse the retention/build supervision in the current Track 03 probes and their
read-only Track 01 retention helper. Build each run from its captured sources;
use a fresh external `NATEMU_RAW_ROOT` identity with hashes, full commands, inputs,
stdout/stderr and partial failures. Raw data never enters Git. Check applicability
of reused code; its old PASS does not qualify this new ABI.

An independent author writes the expected guest-state/effect model from this
contract and finite instruction semantics, not from runtime output. Compare full
declared state, preserved sentinel fields/upper lanes, exception packets, memory,
effect counts, operation/route identity, and reference/admission transitions.
Runtime assertions alone are insufficient. The producing author checks the
independent oracle; a separate reviewer checks the method and producing code.

| Test | Required result and decisive negative/control case |
| --- | --- |
| T1 — Entry and guards | Valid native RAM access; private/noncanonical/overflowing range and guest-derived native target rejected before effect; helper accesses use the same checks. |
| T2 — Live helper | GPRs, flags, full vector state and controls survive an intentional declared clobber; only staged outputs change, including an output overlapping a saved/clobbered register. Cross-block entry maps swap live registers and move a vector/spill root without losing old values. A false clobber/map declaration is caught by sentinels. |
| T3 — Fault outcomes | Recover current pre-fault state, repair/retry one unperformed access, cold-complete one instruction or emit the exact fault packet. Postcommit state is not replaced by its precommit map. |
| T4 — Two queued faults | Both CPUs detach during closure; FIFO service makes progress, with no active waiter deadlock and at most one outstanding operation per CPU. |
| T5 — Remap and retained route | A started read/modify/write or synthetic admitted subaccess finishes its old route before conflicting remap acknowledgement; a subsequent unadmitted access sees the new route. |
| T6 — Source write and stale reply | Writing the following instruction rejects old fallthrough/link and stale compiler output; current-source dispatch executes the replacement once. Exercise mutation before and after publication. |
| T7 — Event/wait/control | Pending events survive helper/drain/wait/translation-wait paths, obey the fixture's IF/shadow rules and deliver at an eligible boundary. Force a host exit mid-fragment; transfer-register reuse is rejected there. Change control context and reject its old entry. |
| T8 — REP-like prefix | Fault after a source read and before destination write; retain the loaded value, count each read/write once, expose completed iteration progress and interrupt only at its legal point. |
| T9 — References and resources | Invalidate with a saved frame or loaded link target; reuse stays blocked until release. Exhaust object/frame slots, cancel and deliver a late reply; no stale storage access and no false-success result. |
| T10 — Work and fairness | An internal loop stops within its declared work bound; eight queued heads are followed by actual ordinary admission. Non-detachable atomic prefix is rejected before execution. |
| T11 — Generated fit | Real FEX live_load, live_alias_load and branch_carry execute through the same gates/maps. Induce load faults before/after earlier flag/vector changes and cross an inserted live-state helper gateway without losing roots. |
| T12 — Paging | Real synthetic x86 four-level tables drive the runtime walker and resulting ARM shadow accesses in P1–P8 below. Manually changing an ARM mapping without consulting guest tables does not satisfy these tests. |

Freeze exact input vectors, expected outcomes, schedule points and native sites
before decision-evidence execution. Each T1–T10 has a positive and an adverse
schedule cell (20 cells); T11 has successful and fault/helper variants for each
of its three fragments (6 cells). One combined two-vCPU stress cell repeats T4–T10
under seeds 0,1,2 for 4096 transactions each. These account for 27 cells;
T12 adds eight paging cells, for **35 cells in the original two-vCPU core**.
Retain this core and its failures; incremental subsets report their exact coverage. Forced schedules establish the named races;
stress widens finite coverage and cannot prove their absence generally.

The revised milestone also requires the following extensions. Define exact vectors,
schedules, oracles and counts in the executing increment before acceptance runs;
the 35-cell arithmetic does not include them. Do not label the revised milestone
complete on the original core alone.

| Extension | Required behavior |
| --- | --- |
| Live producer | Actual FEX request/generation/admission/resume, not fixed-export replay; changed source/context rejects stale output, cancellation and finite capacity preserve lifetime. |
| Automatic recovery | Actual allocated-site maps under register pressure recover spills and overwritten destinations; partial-effect and FP-status paths preserve admitted semantics. |
| N-vCPU composition | Three/four-vCPU schedules combine remap, queued faults and retained/new code entry; delay a peer, exhaust capacity, deliver pending events and verify every participant's effects/acknowledgements and eventual progress. |
| Rust integration | Check shared offsets, malformed/versioned records, full-state assembly capture/helper restore and bounded panic/failure paths. |

Each run records its configured/tested N and resource profile. Higher counts extend
applicability only after checks at those counts; design for N alone is not observed
N-vCPU correctness. These extensions can develop alongside the core.

The initial semantic fixture is flat 64-bit supervisor execution, with dedicated
user-permission and IF/shadow variants declared in the input manifest. Arithmetic
inputs include (0,0), (2^64-1,1), (2^63-1,1) and (1,2), covering zero, carry,
signed overflow and ordinary results. Give every preserved field/vector half a
distinct nonzero sentinel. Use distinct old/new route data and source versions;
fault cells distinguish not-present from protection, read from write, and guest
fault from internal repair. The independent oracle fixes exact packet flags and
expected prefixes for those inputs before running. These finite semantics do not
stand in for the full virtual CPU profile.

The paging cells use owned RAM pages containing actual x86 paging entries, plus
a synthetic side-effect-counted device endpoint. They compare the result of the
real native access, the guest page-table bytes/A-D effects and exception packet
against the independent model. The same runtime gates/maps handle these faults:

| Cell | Required paging distinction |
| --- | --- |
| P1 | Valid 4-KiB translation: first native miss walks guest tables and fills a shadow; subsequent hit performs no new walk. Guest CR2 and pending exception state stay unchanged. |
| P2 | Read with A/D initially clear, then first write: A is established, the read fill stays nonwritable until write/D admission, and the write effect occurs exactly once. |
| P3 | Separate fixed vectors for not-present, R/W denial, user/supervisor protection, CR0.WP, NX and SMEP fetch, and a reserved present-entry bit. Compare exact #PF error/address and retained table effects; guest fetch permission never enables native execution of guest RAM. |
| P4 | Modify a tracked PTE through a different guest alias, then clear A/D: dependent shadows retire and rewalk; the alias and walker writes also trigger source-code tracking where applicable. |
| P5 | Two CR3 contexts use the same linear address for different backing. Switch/reload, invalidate with INVLPG and exercise reuse of native translation identity; the later access cannot silently use the old route. |
| P6 | Two-vCPU covering shootdown with an old admitted access: the old access completes before acknowledgement; the first covered access admitted afterward uses the updated translation. Exercise a faulting peer during drain. |
| P7 | Cross-page operation with a valid first subaccess and faulting second, plus a serviced device subaccess: preserve the allowed prefix and counted read value, without whole-operation replay. |
| P8 | A 2-MiB guest leaf split into native pieces at a RAM/device or memory-policy boundary: only eligible WB RAM fills directly; non-WB/no-fill/device pieces stay serviced. Check guest leaf permissions and invalidation extent. |

Use a finite pool of 16 guest page-table pages, 32 native shadow-table pages and
64 installed native 4-KiB leaf mappings. A large guest leaf is filled on demand;
this cap does not require prepopulating every piece. Pool exhaustion is tested
through T9's existing capacity path. Legacy32, paging-off and hidden-PAE behavior
are not implied by P1–P8; their actual I4-V coverage is recorded in INV-03-01.

The oracle's synthetic self-check must reject a lost vector high half, incorrect
flag recipe, duplicated effect, early remap acknowledgement, stale entry and
premature release. Trace each admitted/completed effect and reference transition
in bounded per-vCPU buffers with sequence counters and coordinator epochs.
Missing/overflowed traces, impossible transitions, unexpected native faults or
timeouts fail the affected cell; no silent exclusions or retry-until-pass.

### Actual fragment acquisition and binding

Use FEX pin `48d71752e2a363d5535181f803b0b0e9666c1825` and the existing export
tools/fixture definitions. Some cited external native files are currently absent;
binding prose is not executable evidence. First verify retained bytes and their
producer manifests. If unavailable, allow one bounded re-export of exactly these
three fixtures from pinned inputs/toolchain, with one focused build correction
inside the acquisition allowance. No new generator search or additional ISA suite.

For each fragment, retain x86/native bytes, hashes, producer options, disassembly,
copied ranges, inserted gates/guards, relocations, native clobbers, bank/frame
layout, and exact fault/commit/root maps. Preserve emitted guest computation;
declare every adapter change. Manually derived maps checked against actual bytes
are allowed for this finite test; they do not demonstrate automated metadata
generation. The live fixture changes XMM0 before its load; branch_carry loads
before its vector update. Their load-fault maps must therefore differ. The
alias load must retain the overwritten destination's address/before-image.

The inserted helper tests runtime interoperability with live generated state;
it is not evidence for upstream helper lowering. The original three-fragment check does not establish general FEX allocator/exporter
adaptation, TCG helper integration, FP arithmetic/status fidelity or x87 policy.
The extensions above add narrowly declared automatic-map and FP-status cases;
full coverage remains explicit work. We preserve their state and keep their broader obligations
with INV-03-02. If these three fragments cannot satisfy the specified interface,
report that result rather than replacing them with hand-written arithmetic.

### Evidence sequence and cost reporting

Ordinary development builds and regression runs follow the workflow's lightweight
loop. When accepting an architectural or performance claim, review the actual
producer, independent expected outcomes, controls and resource profile, then
collect a fresh primary and independently rebuilt replica. Substantive corrections
invalidate affected results; recheck and rerun the affected evidence within the
current allowance. Keep failures and report insufficiency at its bound. Historical
four-bundle campaign limits are not development limits or a fresh allowance.

Report instruction counts, spill/reload bytes, map/object size, helper calls,
native/host exits, queue lengths and transactions. Measure supported body/helper,
compile/dispatch and fault/coordination paths when they can guide a choice; a
kernel or full device set is not required. Correctness qualifies timed samples,
and qualified comparisons require Track05's controls. Diagnostic wall times or
unmeasured frequency assumptions do not establish performance superiority.

## 8. Finite capacity and proposed budget

Capacity is an admitted profile, not a two-vCPU architectural constant. Reserve
one operation descriptor, native capture and emergency completion storage per
vCPU, plus three persistent object references and one bounded outstanding compiler
delivery per vCPU. Host/backend work has separate finite reserves. Total code,
queue, trace, mapping and stack capacity must cover admitted N and forced failure
cases before entry. Resource exhaustion must return a declared failure/wait
without an active grant; overflow must contain the VM rather than fabricate success.

Draft0's starting profile remains a useful bring-up reference: eight native
objects, 16 links and64 sites per object, 64 producer spill slots of32 bytes,
2KiB producer spill frame, private gateway/exception stacks bounded to16KiB per
vCPU, 16 guest page-table pages,32 shadow-table pages and64 native leaf mappings.
Declare any revised profile and test its failure path. Do not silently increase
N while retaining insufficient reserves or omit cases to fit a pool.

Before each increment, record total work allowance and subprocess, VM-cell,
whole-run, RSS, output and disk limits with INV03-01's next assignment. Reuse
external supervision and retained snapshots for experiments; preserve timeout/
partial-failure records. Bound compilation concurrency separately from vCPU count
and allow host scheduling headroom. SDK/signing/platform admission is checked
for the actual build. Do not introduce CPU-limit setters into HVF workers.

The original six-wall-hour/480-agent-minute proposal and its fixed two-vCPU
bundles are superseded as execution scheduling; their historical review does not
fund new runs. The executor selects reasonable bounds within the assigned slice
without another approval round for routine details. Material scope/resource
extensions use owner alignment. Stop on the declared result, concrete falsifier
or exhausted allowance; report passed, failed and untested behavior.

Milestone acceptance requires independent review and replication of the retained
core and declared producer/Rust/N-vCPU extensions, with no unresolved defect
invalidating that scope. Development may continue incrementally before that
acceptance; no whole-architecture or performance claim follows from it.

## 9. What remains provisional

The ABI makes its ownership and continuation choices now. I1/I2 supply finite record layouts, machine-code entry/capture gates, an
initial mapping/publication path and an actual bounded compiler request loop. General instruction-site recovery, helper
transitions, interrupt bring-up, paging and concurrent capacity/lifetime protocols
still require implementation and review before their evidence execution. They are explicit conformance work, not
permission to leave guest-visible behavior unspecified.

This experiment does not establish full exception delivery, arbitrary compound
instructions, full SIMD/FP semantics, stock boot/devices, complete compiler fit,
M2 behavior or economics. Existing accepted scoped contracts remain requirements
when those paths are added; a fixture-only exclusion cannot silently narrow the
product. Plan subsequent executable slices under the current build-first workflow.

## 10. Evidence and review scope

The draft reuses these scoped architectural contracts and identifies its new
choices above; it introduces no new observation:

- [INV-03-01 claims 14, 15 and 19](../investigations/INV-03-01.md): immutable
  objects, guarded EL1 authority, owner-thread/register transport, queued drain,
  admitted-route completion, cold dispatch and eight-head fairness.
- [INV-03-02 claims 36–37 and 56–59](../investigations/INV-03-02.md): current
  recovery roots, helper outcomes, candidate-specific state/status constructions
  and bounded producer frames. Their acceptance does not prove this ABI's fit.
- [INV-03-04 current contracts](../investigations/INV-03-04.md): memory and
  all-writer ordering, admitted routes, publication and retained references.
- [FEX fixture bindings](../../../experiments/track03/codegen-comparison/README.md),
  [comparison bindings](../../../experiments/track03/codegen-comparison/consolidated-bindings.md)
  and [checkpoint limitations](../../../experiments/track03/codegen-checkpoint/README.md):
  finite producer/register evidence and the distinction between a root-map
  proposal and an actual validated exporter.
- [Arm AAPCS64, 2025Q4, section 6.1](https://github.com/ARM-software/abi-aa/blob/main/aapcs64/aapcs64.rst):
  ordinary procedure-call register preservation does not preserve every live
  generated-code value; this ABI adds its own gateway obligations.
- [Arm Exception model, version 1.0, sections 5.2/5.6](https://developer.arm.com/-/media/Arm%20Developer%20Community/PDF/Learn%20the%20Architecture/Exception%20model.pdf):
  exception entry saves return state and selects the exception stack; software
  must preserve working registers. The EL1t profile here is a proposed use of
  that contract, not an observed HVF result.
- [Apple hv_vcpus_exit](https://developer.apple.com/documentation/hypervisor/hv_vcpus_exit(_:_:)):
  forced exit prompts host-owner handling; the stronger resident quiescence and
  state-ownership requirements come from the accepted natemu contract.

Independent draft review and any correction are recorded in the existing
[INV-03-01 findings thread](../reviews/INV-03-01-findings.md). That review assesses
the proposed contract, method, decision relevance and budget. Review of actual
experimental code/controls and observed findings remains future work.

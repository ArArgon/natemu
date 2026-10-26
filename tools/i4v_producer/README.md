# I4-V bounded FEX producer

This adapter compiles retained guest bytes with their actual guest RIP, including
upper-canonical x86 addresses. The input buffer stays at host address `0x10000`;
the native object has a separate address. It never executes guest code in FEX.
`producer.json` pins the upstream commit, source/dependency archives, adapter
inputs, Linux image and final executable. Build from a fresh populated source
tree with `build.sh SOURCE BUILD`; the script reuses the existing raw exporter
in `../i3_producer/fex_export.py` and applies `adapt.py`.

The retained final build is
`/Users/liaozping/orca/natemu-raw/i4v-producer-20261026-02`.
The preceding `i4v-producer-20261026-01` retains a failed initial compile
(incorrect return type in the new decoder guard), its corrected build, an
overstrict probe-log check, and successful exploratory outputs. The final fresh
build additionally removes pointer arithmetic on logical guest addresses and
guards resolver failures in diagnostic/SMC paths. Both builds stayed within two
jobs, 8 GiB memory and a 30-minute per-build deadline; together their retained
trees occupy about 1.9 GiB. Container inspection and cleanup records are retained
in the final build directory.

## Runtime admission

Run one fresh request directory:

```sh
python3 tools/i4v_producer/compile.py \
  --directory "$NATEMU_RAW_ROOT/new-v1-request" \
  --producer "$NATEMU_RAW_ROOT/i4v-producer-20261026-02/build/Bin/CodeSizeValidation" \
  --case fex_memory_data64 --rip 0xffff800000102000
```

Additional admitted cases are `fex_push_immediates64`, `fex_alias_xor64`, and
`fex_alias_or64`. The alias cases preserve the existing I4 44-byte fragment
and bind it to the actual low/high guest RIP using the same source-window seam.
Their final-producer probes are retained in
`/Users/liaozping/orca/natemu-raw/i4v-alias-producer-20261026-01`.
`../i4v_alias_worker.py` consumes the resident's profile-3 packet and returns
the generated body with an explicit RIP/mode/profile trailer; its verifier
reparses the original packet and actual producing artifacts.
The concurrent-stack replacement case `fex_push_immediates64_changed` changes
source byte 7 from `0x52` to `0x53` (the pushed word becomes `0x5153`). Fresh
low/high-RIP probes retain the resulting native byte change at body offset 12
under `i4v-stack-concurrent-producer-20261026-01`. The stack worker admits both
exact sources, preserves positive incarnation/generation/serial identities and
bounded vCPU IDs, and emits three chunks from one compilation. N=1 resident
admission remains strict; concurrent admission compares the stored full context.
The protected32 case `fex_moffs_load32` admits its complete 64-byte body from
37 source bytes. Case selection supplies its required bitness; a conflicting
explicit `--bitness` rejects before compilation. The paging-off worker carries
all source bytes plus VM/owner/generation/RIP/mode identity, and stages three
chunks from one fresh compilation. Its finite first-store recovery map does not
claim all nine guest/native instruction boundaries: the upstream PC table is sparse.
The `fex_moffs_load32_step0` through `step8` cases (step6 reuses step3) instead
compile each guest instruction separately. Retained probes at two RIP bases and
LLVM disassembly establish their exact bodies and memory sites. The step worker
compiles at architectural EIP, binds CS-derived linear fetch and source GPA plus
the admitted context, and checks the exact one-instruction reply before publishing
that slot. A rejected later reply preserves earlier committed instructions.
`--source HEX` and
`--instructions N` can be supplied with a case and must match it. Case admission
requires the exact expected host instruction count. The worker retains source,
packed input, commands, container outcome, full object/relocations, compiler log,
and emits `body.bin` plus `export.json`. It uses a fresh FEX context in one
bounded, network-disabled container per request. `export.verify(directory,
expected={...})` reparses those artifacts and compares the actual output and
requested case/RIP; `compile.compile_source(...)` and
`export.export_program(directory, case)` are the corresponding Python APIs.

`export.json` carries source/RIP/mode identity, hashes, raw object metadata,
extraction boundaries, FEX guest/native PC associations, static entry GPR
bindings, and memory sites. Site native offsets are relative to `body.bin`;
FEX's retained PC offsets are relative to the full native object. Sites include
guest instruction offset, width/direction, address register and displacement,
native continuation, and current guest RSP register. They are a finite profile
contract checked against every emitted instruction, not a general allocator
recovery-map format.

The load body is `ldar x4,[x6]; nop`: RBX is x6 and RAX is x4. The stack body
uses preindexed stores through x8 (guest RSP), then loads through x20. On a
synchronous store fault, recovery expects x8 to retain the pre-instruction RSP;
the runtime must test that assertion. Completed pushes advance x8 by -8, -10,
then -12 relative to entry. The supplied cases preserve flags. Native x20 is
scratch; canonical guest banks, vectors and flag registers retain their entry
authority. Only ordinary RAM accesses contained in one page are admitted;
cross-page partial effects and device writes need separate handling.

## Source-window and mode limits

Upstream CodeSizeValidation compiles from a fixed host buffer, uses a permissive
executable range, and treats instruction-count mismatches as test failures. The
adapter provides an immutable bounded source window, resolves decoder and
diagnostic/SMC code reads, and calls CompileRIPCount with the actual guest RIP.
It exports the JIT entry and guest/native PC table. Partial instruction decoding
is rejected. Excluded FEX header/tail instructions access its own frame and
dispatcher; they are retained for provenance but are not installed in the
resident runtime.

The existing input header already supports 32-bit protected-mode compilation.
FEX asserts a fixed mode within each context, so each request uses a fresh
context. This does not implement runtime mode switching. The generic compile
probe accepts `--bitness 32 --source HEX --instructions N --expected-host N`,
and body admission includes the five finite 64-bit cases, `fex_moffs_load32`,
its per-instruction exports, the two mixed-mode register-addressed variants and
the control-profile MOV-immediate/load/store exports.

Final-build controls retain high/low load compilations, high/low RIP-relative
LEA (the emitted value is the requested RIP + 7), a 32-bit moffs-load case, and
a truncated-prefix negative control. These prove compilation/address handling,
not execution, memory ordering, boot support or complete recovery semantics.
FEX's count test is not a semantic correctness oracle. The alias cases update
flags and are admitted through the existing I4 native-state contract; their
site locations are not general allocated recovery maps.

## Checks

```sh
python3 -B -m unittest discover -s tools/i4v_producer -p 'test_*.py' -v
```

The checked-in testdata is actual final-producer output with hashes and retained
origin in `testdata/provenance.json`. Negative checks alter the native opcode,
guest/native PC association, actual guest RIP, packed source, delivered body,
compiler lifecycle and requested identity. Runtime execution and independent
architectural expectations are owned by the I4-V runner and `fixtures/i4v/`.

## First native validation slice

From the repository root:

```sh
./tools/with-env python3 -B tools/run_i4v.py \
  --name NEW-RETAINED-RUN \
  --producer /Users/liaozping/orca/natemu-raw/i4v-producer-20261026-02/build/Bin/CodeSizeValidation
```

This rebuilds and signs the runtime, tests low/high guest RIP and operands,
and checks precise missing-page, permission, NX/SMEP and private-address faults.
The live cells compile only after a resident request, then publish the verified
reply through the resident runtime. Sixteen malformed-reply controls check that
every request identity/body field is enforced before publication. Exact requests,
compiler artifacts, native reports and independent checks are retained together.
Run independent rebuilds serially with other builds to preserve the two-job bound.

Add `--stack` to include the five-instruction push case, instruction-prefix
recovery, WP/CPL permissions, late #SS/#GP/#PF and all48 malformed chunk replies.
This publishes original FEX instruction segments in separate slots with resident
returns at guest-instruction boundaries. Every transfer is retained and bound to
its one actual compilation; later rejection must leave all slots blank.

Add `--aliases --concurrent-stack --mixed-aliases` for the reviewed N=1/2/3/4
alias/remap/source and stack-pressure profiles. Add `--paging-off` for the first
live flat protected32/A20-on profile: two RIPs, cold/read-only/warm native data
mappings, zero guest page walks, unchanged poisoned tables and48 malformed chunk
replies. The runtime verifies physical ownership and repairs native mappings
without synthesizing guest PTE A/D state. It executes the complete body without
inserted instruction-boundary returns.

Add `--paging-off-steps` for per-instruction segment/A20 tests: nonzero bases,
precise CS/DS faults, linear base addition wrapping, distinct A20-selected RAM,
bounded device/hole service and25 late-reply rejection controls. Only the active
source page contains valid code; the inactive page stays poisoned. These fixtures
use page-aligned bases and do not establish general address-size or maximum-limit
crossing semantics. Every instruction prefix and physical page is independently
checked, alongside unchanged guest page tables and CR2.

Add `--mixed-modes` for both paging-mode arrangements at N=1/2/3/4. Each owner
compiles its old, deferred-old and replacement requests independently. The shared
source is `mov ecx,eax; mov [ebx],ecx; mov eax,[edx]`; replacement stores EDX.
Each body is20 bytes, with store/load at native offsets8/12 and guest offsets2/4.
Private code slots retain owner/context identity even though the two paging modes
produce identical bytes. Shared data/table mutations use the existing coordinator;
paging-off identity routes must remain unchanged when paged routes move.
`probe_mixed32.py` retains count discovery, checked exports and disassembly.

Add `--controls` for the single-vCPU protected32/legacy32/PAE/IA-32e programs. The
resident runtime fetches and handles MOV-to-CR0/CR3/CR4; ordinary instructions
between those controls are freshly compiled by FEX at their actual EIP. Cases
`fex_mov_eax_HEX8`, `fex_store_eax_ebx32` and `fex_load_eax_ebx32` admit the
specific immediates in `export.py`, with 32-bit operand zero-extension checked
against actual native instructions. `probe_controls.py` retains count discovery,
checked exports and disassembly. A compile reply binds VM, source, mode, context,
EIP and serial; a return to numerically identical controls still has a new
context generation. The worker retains actual delayed replies across that return.

The runtime tests paging off/on/off, CR3 A/B/A and same-value reload, WP/PGE,
illegal/privileged encodings and a next-fetch fault after successful commit.
The unchanged FEX userspace privileged-op handler does not implement those system
semantics. The independent checker compares every guest-instruction prefix,
full guest tables/data/source, native fault sites and canonical state. Shadow
eviction covers the admitted operand leaf; this profile does not establish a
general all-address invalidation path or concurrent control transitions.

Nine PAE programs extend those thirteen legacy programs. Three additional
MOV-immediate exports (`620`, `6a0`, `1020`) select PAE/PGE and a root that is
32-byte aligned but not page aligned. Wire mode2 keeps FEX's32-bit decode width
while distinguishing PAE context from legacy paging and paging off. Guest
INVLPG remains a resident semantic operation. Hidden roots are compared with
their independent memory image through source fetches and real generated data
accesses. Failed-active-reload cases use an explicit one-shot handler fixture;
the next generated load must still use old hidden roots after native eviction.
The PAE A/B/A case also retains an actual deferred compilation across reloads.
Native PAE coverage is4-KiB/NXE-off; larger-page/NX walker unit tests do not imply
native coverage. Concurrent PAE composition is described below; full exception
delivery remains unimplemented.

Seventeen IA-32e programs add direct paging-off entry, descriptor-checked far
transfers, return to compatibility mode and paging-off, plus a separate legacy
PAE composition. WRMSR EFER and far jumps use resident system semantics. The
worker selects an export by source bytes and decoding width: wire mode 3 is
compatibility32, while mode 4 selects the 64-bit producer. New actual probes cover
32-bit MOV-immediate 0x100/0x6000 and 64-bit-decoded MOV EAX,0x10011; the retained 64-bit
load checks full-width state. Identical emitted bytes never justify substituting
a 32-bit compilation for a 64-bit request. Illegal transitions, pointer/descriptor
faults, postcommit fetch faults and stale-context replies have independent checks.

Add `--concurrent-controls` for five control-program families at N=1/2/3/4 with
every actor position: legacy ABA, hidden-PAE ABA, IA-32e ABA, direct IA-32e entry/exit
and rejected CR0. Each owner requests actual FEX output for its private source
window and dynamic decoding mode. Its incarnation/source/context identity is
checked independently from other owners; source and data backing remain shared.
Program revision 1 adds ordinary instructions needed to compose sustained table
maintenance with actual guest progress, pending-event handoff and source retirement.
The rejected-control supplement checks prior stores and retained peers separately.
Reports retain all per-owner compiler requests, deliveries, control/hidden state,
physical metadata reads, native effects and one ordered shared trace. The checker
replays shared table changes and A/D effects in that order.

Exact results and independent acceptance live in INV-03-01; implementation alone
does not establish concurrent acceptance. Broader segment/address forms, remaining
V1 boundaries and the memory-model campaign remain further work. These finite
profiles do not complete V1, V2, V3 or I4-V.

The protected32 address-form slice compiles each instruction separately with
`probe_address.py`. `i4v-address-producer-20261026-01` retains count discovery and
checked emissions at EIP 0x2000/0x2002/0x2005; the separate immutable
`i4v-address-export-20261026-01` contains checked exports and independent LLVM
body disassembly. No producer rebuild was needed. The first discovery bundle
uses the generic controls probe snapshot; the export bundle additionally retains
the address-probe invocation script and its exact selected sources.

Address-size16 stores emit ADD W20,W6,W10; UXTH W20,W20; NOP; STLR W7/W5,[X20].
The load uses the same address prefix, LDAR W4,[X20], then NOP. Only the effective
address is narrowed to16bits; the data operation remains32bit. Store/load sites
are12/8 and their continuations16/12. Ordinary32 store/load sites are4/0 with
continuations8/4. The finite exporter retains these exact sequences; it does not
infer missing instruction boundaries from the old three-instruction block.

`probe_ordering.py` retains whole ordering/atomic participants at RIP0x102000 and
0x102100 using 64-bit decode with DWORD data and full-width pointers. The fresh
compile-only `i4v-ordering-producer-20261026-01` and checked
`i4v-ordering-export-20261026-01` retain all24 checked exports, original count
discovery, independent LLVM disassembly and the relevant pinned FEX emitter
sources. Small producing inputs live in `testdata/order-*`; adapted upstream
provenance lives in `fixtures/i4v/ordering`.

The new exports label memory sites as load/store/atomic_exchange and state the
already committed guest-byte prefix. In XCHG followed by a result store, SWPAL is
at body0 and the later STLR at body8: a fault at8 must never restart the exchange.
Observed barriers and native register writes are explicit metadata, separate
from FEX's sparse PC map. These exact-byte admissions neither qualify LSE as the
complete x86 locked-operation implementation nor turn DMB LD into the selected
LFENCE completion construction. Those are resident-contract/review obligations.

`probe_lfence.py` retains the generated live-result candidate at both RIPs in
`i4v-lfence-producer-20261026-01`, with external supervision in its sibling
`-supervision` directory. The 25-byte input produces 120 native bytes; the
following four STMXCSR bytes are a separately declared semantic suffix. Small
checked inputs, original objects, disassembly and exports are in `testdata/lfence-*`.
No producer rebuild was needed.

The actual prefix computes ADDPS into Q16, ADD into X7/X26, XOR-of-ADD-inputs into
X27, and NZCV/FPSR before DMB LD at native16. LAHF/SETO after the fence consume
X26/X27 and NZCV, including deferred AF/PF and OF. Stores at88/100/112 publish
RCX, AX and XMM0; final CFINV at116 changes the carry representation. The finite
export labels these roots separately from FEX's sparse guest-PC map. The MODE11
resident adaptation preserves DMB LD and all generated words, inserts a direct
branch to the full-frame dependency sink, completes its private stores with
DSB SY/ISB, and resumes the shifted suffix. This is a bounded admission, not a
general FEX liveness analysis. Pinned FEX GetMXCSR masks exception-status bits;
the semantic STMXCSR suffix instead uses the existing masked-RN FPSR mapping
and preserves the admitted incoming guest sticky flags.

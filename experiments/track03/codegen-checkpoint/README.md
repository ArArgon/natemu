# FEX checkpoint extraction preparation

**Unbuilt research artifacts; no observed allocation map, native code or runtime
result.** Both patches target FEX `48d71752e2a363d5535181f803b0b0e9666c1825`.
They are **standalone alternatives against pristine source**, not stacked patches.
The diagnostic is not a production recovery implementation or generator selection.

## Observation baseline

`fex-observation.patch` preserves claim 30’s two-file observation-only attempt.
When `NATEMU_CHECKPOINT_TRACE` is present, it logs allocation bookkeeping before
nodes and after destination reservation, before merging. Reservations are not
computed values; assigned spill slots alone are not proof of fault-site validity.
Its raw INFO tee retains text before the count tool’s trimming/sanitization, not
raw machine-code bytes. It provides no architectural root identities or native-PC
association. It has not been built or shown to leave emission unchanged.

## One-fixture joint diagnostic patch

`fex-root-preparation.patch` extends claim 31’s six-target preparation with the working32-v2 diagnostic. Presence of
`NATEMU_CHECKPOINT_ROOT_PREP` enables its narrow frontend hook. The compile-only
tool accepts only the exact 17 `live_load` bytes/five instructions from
`fixtures.json`, or its existing one-byte NOP control. The other five fixtures
are rejected in this mode. `root-fixture.json` is the exact one-test subset for
this diagnostic, checked against the original fixture file. The future command
shape is `python3 <FEX>/Scripts/InstructionCountParser.py root-fixture.json
root-fixture.bin`, then `NATEMU_CHECKPOINT_ROOT_PREP=1 <build>/CodeSizeValidation
root-fixture.bin counts.json`. These commands have **not** run; the parser needs
NASM and the tool needs a complete supported build. A count failure against the
default zero is not recovery conformance. The new patch does not include the observation patch’s
raw INFO tee; it cannot be silently combined with that overlapping patch.

The frontend captures all 16 GPRs and 16 low XMM halves before the load,
including post-ADD RCX and post-PXOR XMM0. It appends `NatemuRecoveryUse` after
the scalar LoadMemTSO and before committing RAX. Thirty-three SSA operands carry
the 32 working roots and the access result; non-SSA fields receive allocation descriptors.
The opcode is marked side-effecting and participates in backwards SSA liveness.
All new diagnostic gates use unconditional stderr + `abort`, independent of
upstream `ASSERTIONS_ENABLED`. Review found that the initial draft used assertion
macros which could compile out; those guards were replaced before submission.
Post-access lifetime is conservative and may prevent safe fault-only coalescing;
its extra cost is not a minimum bound. Capturing roots may itself add IR loads.

The allocator’s special handler:

- Requires the previous iterated node and previous nontrivial node to be the same
  referenced LoadMemTSO. Optimizer replacement/motion is rejected, not handled by
  an invented stable-label scheme. Load/checkpoint merging is disabled here.
- Captures all 32 locations before consuming kills: an occupied register with
  matching SSA ownership, or an already assigned unique spill slot. At this exact
  position, the spill operation was inserted before the load, and the live roots
  cannot be its newly reserved destination. These are source-level premises for
  review, not observed allocation results. Constants/other location kinds reject.
- Consumes the allocator’s next-use stream and kill bits without inserting fills
  for metadata-only uses. It guards invalid physical assignments before checking
  occupancy, then clears SSA arguments after storing the descriptors.

The backend maps register descriptors using the actual `GetReg`/`GetVReg`
helpers, including fixed-register classes. It reports spill byte offsets relative
to native SP using `MaxSpillSlotSize`. `NATEMU_SITE` is emitted immediately before
the selected 64-bit LDAR, with the access node ID and code-object-relative cursor
offset; the following NOP is not the fault site. The native SVE, RCpc and TSOImm9
alternatives are rejected by the frontend hook. A marker contributes no native
instruction; it reports 32 `NATEMU_ROOT` records keyed by entry/access ID.

`NATEMU_BEGIN`/`NATEMU_END` bracket an emission attempt. A **future** trace consumer
must discard records from an abandoned/restarted BEGIN and require one matching
site, five distinct roles and END before accepting a map; no such consumer or
trace exists yet. Offsets start at the temporary code-object base and survive its
copy to the final code cache. This is compile-time diagnostic text, not a resident
immutable recovery map, relocation/lifetime mechanism or runtime consumer.

The five roles do not cover the whole guest state. `expected-recovery.json`
retains inverse-ADD flag recipes and the required unchanged/upper-state bindings;
those bindings and recipes are not exported or checked by this patch. Claim 33
corrects the requirements: checkpoint RF is zero at the admitted data-access
phase, while a subsequent page-fault frame saves RF as one. Other instruction
phases require their own map; entry flags are not blindly restored. No fault
injection, reconstruction, repair, guest exception or service completion ran.
Only the `live_load` software-TSO/non-SVE/RCpc-off path is attempted. The tool’s
forced LSE/preserve-all settings still require reconciliation before any matched
comparison. The qualified hardware-TSO variant, other fixtures, raw code export,
map validator and annotated/unmodified count comparison remain unimplemented.

## Actual static checks

```sh
python3 experiments/track03/codegen-checkpoint/check.py /path/to/pinned/FEX
```

This checks both manifests’ pristine source hashes/lengths, six fixture shapes,
requirements shape and separate `git apply --check` for each patch. It does not
apply patches or run NASM, FEX, allocation, native generation or guest code.

The pinned stdlib-only `json_ir_generator.py` was separately run on a temporary
copy of the patched schema, producing IR headers and dispatcher source. It exited
0 with empty stdout/stderr. The generated operation, six-SSA overload and NoOp
dispatch were inspected; the backend’s explicit handler intercepts that operation.
This validates schema generation, **not C++ compilation or adapter correctness**.
The retained command, input hashes and generated-output hashes are identified in
`schema-check.json`; no generated headers are hand-edited or product files.
The fresh nonoverwriting run is
`/private/tmp/natemu-track03-checkpoint-static-20260920T094625Z`; all 16 listed
file hashes/lengths were checked. An earlier temporary manifest accidentally
included its own stale hash and was overwritten before the review return. Its
failed original was not retained. `schema-check.json` records the reviewer-observed
failure and the distinct fresh run; it does not retroactively validate the old
retention record.

`fixtures.json` uses the upstream parser shape; ExpectedBytes/FaultInstructionIndex
are review annotations, not assembler observations. No expected instruction-count
baseline is supplied, so the upstream parser’s default zero is not an oracle.
`expected-recovery.json` contains requirements, not observed registers or slots.
There is no build/run launcher. Any eventual execution needs a complete retained
source/toolchain snapshot and the research workflow’s separately reviewed method.

Before a larger emission campaign, the full-fixture map contract must identify
actual FEX upper-state representation under native non-SVE conditions and whether
additional vector lowering is needed for the exposed profile. A scalar map of
low XMM lanes cannot establish full AVX/AVX2 preservation or lowering; hiding native
SVE does not remove exposed guest-state obligations. Broader ISA/FP compatibility
can change the component decision even if this one-fixture diagnostic succeeds.

Independent review Return 21 accepts this corrected static-preparation/reporting
scope and closes the guard/retention findings. It does not accept a C++ build,
observed map, full recovery, emitted cost or runtime conformance. No further patch
or execution is part of this checkpoint; the investigation records the next bounded
non-SVE upper-state applicability check.

## Descriptor preparation (partial; no observed maps)

`recovery-map.py` now provides a supplied-witness L/H descriptor serializer and
consistency checker, plus a consumer of the **versioned working32-v2 producer text ABI**, while retaining the historical
five-root parser. It has no FEX dependency and does not compile or run generated code.

```sh
python3 -B recovery-map.py --self-check /fresh/external/test-directory
python3 -B recovery-map.py --input /path/to/supplied-witness.json
python3 -B recovery-map.py --map /path/to/serialized-model-map.json
python3 -B recovery-map.py --trace /path/to/NATEMU-stderr.txt
```

The final command deliberately exits **2** even when a complete working-root attempt
parses: it exports a partial report with `publishable: false` and missing producer
inputs. BEGIN resets an abandoned attempt; END requires a matched access/site and
the version’s exact role set. Unfinished attempts, malformed/rejected markers, wrong fixture origin,
wrong register class, duplicate roles/sites and a live GPR root aliasing the load
destination reject. Empty completed attempts are ignored (the upstream NOP
control); at least one complete working-root attempt is required. The parser has only
synthetic tests; no observed FEX trace is bundled.

Model examples are explicitly invented locations, not allocation observations.
The checker verifies consistency of supplied entry/version/phase/pin witnesses,
role provenance, root occupancy/spill initialization declarations and exact field
dispositions. It cannot establish that those declarations are true in native code.
L exports inverse-ADD recipe descriptors, H exports NZCV/PF/AF recipe descriptors
with a carry-inversion tag; recipes are named contracts, not an arithmetic
execution oracle. RF=0 belongs to this precommit data phase and saved-#PF RF=1
belongs to subsequent exception semantics. `ENTRY` does not become a location.
Dirty H YMM0.high must be a current completed-VPADDD root. Live time/APIC/events/
memory owners are retained, not rewound to entry. Opaque semantic banks require
the profile owner's full catalog/validity; this diagnostic catalog is not a
complete product CPU profile.

The working32-v2 producer extends the real frontend, IR schema, allocator and
backend with all 32 working roles for L; its 64-bit location-kind field covers
two bits per role. BEGIN names the format and AUTHORITY explicitly reports
`status=missing`. The consumer requires this boundary and still refuses complete
map publication. The producer does not supply upper-half entry currency,
semantic/live-owner validity or actual frame/code lifetime records. H remains
rejected by the byte gate. No synthetic model fills those missing inputs.

The historical five-root snapshots remain unchanged externally. The current
patch is unbuilt; schema generation and synthetic parsing do not establish an
observed allocation, native lifetime or backing currency. This remains partial
exporter preparation, not a complete recovery implementation or a cost estimate.

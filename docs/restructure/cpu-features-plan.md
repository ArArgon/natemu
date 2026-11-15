# CPU features and profiles: one source for what the guest sees and what the host offers

Status: accepted by independent review after three rounds (November 15, 2026; §7), then revised for the owner's
decisions of the same day (§6): the guest CPU is x86-64-v3 plus easily translated extensions, the host floor is the
M2, and host features determine guest features, with no cherry-picking. Branch `cpu-features` off `main` at 333b7b6;
no code yet. Owner direction (November 15): build a centralized CPU feature/profile mechanism before deciding which new
guest features to implement; the M2 (no FEAT_AFP) floating-point work comes after this plan.

**Goal:** every CPU feature decision comes from one place, with tool-checked consistency, and reaches every consumer
from that place. This covers decisions the guest sees (x86 CPUID, MSRs, control-register bits, instruction forms) and
host-side ones (the Arm features that generated code, admission and the resident rely on). Changing a guest feature
becomes an edit to its catalogue entry plus the feature's own implementation, not a hunt through hand-written tables.
The default launch stays byte-for-byte as it is.

**Non-goals:**
- Implementing any new guest feature (RDPID, VAES, the deadline timer, ...).
- Changing the default guest CPU.
- The M2 floating-point differences.
- Any user-facing guest feature selection (owner: host features determine guest features).
- Per-vCPU differing CPUs (every vCPU presents the same CPU, as on a real machine).
- AMD identity.
- Rewriting the reference generators (§5).

## 1. Where feature decisions live today

The guest side already has a source: `tools/models/cpu_profile.json`, the accepted Intel profile of the decision record
with every value sourced. `tools/models/cpu_profile.py` judges it, and it is generated into
`crates/abi/src/cpu_profile.rs` (CPUID table, MSR table, CR0/CR4/EFER/XCR0 valid masks). The resident's CPUID and MSR
transactions and the host's SIMD decode table (`crates/host/src/system.rs`, `advertised()`) follow it, and tests tie
them to it. Each CPUID bit already has a `fields` entry with its name and state, and `feature_names` maps bits to names.

Everything else restates the profile by hand, with nothing checking agreement:

| Fact | Where it is stated | Tie |
| --- | --- | --- |
| x86 forms the code generator accepts | `tools/fexc/shim/fexc_slice1.inc`. It is an allowlist, so unlisted forms are #UD. Baseline forms are allowed by rows that name no feature, or pass through FEX's decoder. Rows that do name a feature mostly refuse clear ones ("VAES's L1 not advertised", AVX-VNNI, GFNI, RTM, CET, WAITPKG, SSE4a, 3DNow). | none: fexc takes no profile input; `test_slice1.py` only checks that the JSON still says "advertised" |
| System forms that are #UD because a feature is clear | `system.rs`: RDSEED, RDPID, XSAVES/XRSTORS, INVPCID, group-7 register forms, WRSS... all unconditional #UD | comments only |
| FEX's guest-facing switch `x86_avx` | `fexc.cpp` FeatureTable default. It selects FEX's VEX table (Frontend.cpp:84-89; without it BMI goes too), and changes code for non-VEX XSAVE/XRSTOR | none |
| Arm features the generated code may use | `fexc.cpp` FeatureTable (32 entries; defaults are the M4 view, plus pins kept from the old producer: lrcpc, lrcpc2, rpres, wfxt off) and `crates/host/src/admission.rs` FEATURES (the same names in the same order) | only the count and a positional FlagM index |
| Hard host requirements | FlagM stated four times (vm.rs vCPU creation, the admission index, the admission table, fexc's refusal). LSE stated four times (the resident's `+lse` build flag, fexc's default, admission accepting only LSE atomics, the resident's completion code). Apple's TSO bit is written in vm.rs | none |
| Host features read by name | `host_feature("FEAT_CSV3")`, `host_feature("FEAT_SB")` (sysctl), although both are fields of ID registers admission already reads (PFR0[63:60], ISAR1[39:36]) | none |
| Floating-point assumptions | `fpcr()` writes AH and NEP whatever the host has | none |
| Encodings admission accepts | `admission/structure.rs`: LSE-only atomics, the no-CSSC POPCNT sequence, CNTVCT or CNTVCTSS by ECV | a host feature flipped on (CSSC) would emit code admission refuses |
| Compile cache | key = fexc binary hash + request arguments | the guest profile reaches cached code only through the shim's hand-written rows, i.e. through the binary hash |

Reference corpora pin QEMU `-cpu` strings per generator. Where QEMU and the profile differ, the corpora record it
explicitly (for example, `-cpu max` implements RDSEED and RDPID, which the profile does not advertise). That is left
as it is (§5).

## 2. Design: one catalogue, consumers generated from it (compile time)

This part meets the goal on its own: one place, tool-checked, default unchanged. The host-derived mask (§3) is added
on top of it with the first optional feature.

### 2.1 Guest catalogue: the existing per-bit entries, extended

There is no parallel feature list. Each CPUID bit's `fields` entry in `cpu_profile.json` gains these fields:

- `requires`: x86 features it depends on, per the SDM's enumeration text. For example, VAES requires AVX and AES;
  AVX-VNNI requires AVX.
- `msrs`: MSRs that exist only with it. The hand-written `FEATURE_MSRS` and `ABSENT_WHEN_CLEAR` maps move here.
- `cr4`: CR4 bits it validates.
- `subleaf`: the CPUID subleaf it needs. AVX-VNNI is in leaf 7 subleaf 1, so leaf 7's maximum subleaf (7.0 EAX, 0
  today) is computed from the highest enabled subleaf.
- `resident`: behaviour in the resident or host that depends on it. Examples:
  - STAC/CLAC #UD without SMAP.
  - Page-walk reserved bits for NX and 1 GiB pages (`crates/abi/src/paging.rs`).
  - The XSAVEC/XSAVEOPT paths compiled into `xsave.rs`.
  - ACPI's MADT form under x2APIC.
- `gates`: the generated constants that consumers test (§2.3).
- `host`: the Arm features its implementation uses, in two classes.
  - `required`: without it the feature can't be executed (FlagM and LSE for everything).
  - `exactness`: it runs, but with a known, reported difference. FEAT_AFP for SSE and x87 is this class: M1 and M2
    lack it.

  `required` is judged over natemu's whole pipeline: FEX's lowering, plus admission accepting what it emits. FEX has
  no software path for SHA or AES (its dispatcher marks them unimplemented without the Arm feature), so SHA requires
  `sha`, AES requires `aes`, and PCLMULQDQ requires `pmull128`.
- `layout`: true when the feature changes XCR0's valid values, leaf 0xD or the XSAVE image, or whose presence the
  resident tests as a compile-time constant (today XSAVEC and XSAVEOPT; a resident transaction alone, such as RDRAND's or
  CLWB's, does not make a feature `layout`). That covers AVX, MPX, AVX-512, PKU, CET, AMX, XSAVES/XFD, XSAVEC and XSAVEOPT.
- `source`: the SDM section and the decision that set its state. The existing `source` strings stay.

`state` keeps its meaning: `advertised`, `clear` or `dynamic`. A new status, `not-built`, marks a feature with no
implementation; it can never be advertised.

**Host features determine guest features (owner decisions, November 15).** There is no per-launch selection: no user
picks guest features, and nothing cherry-picks them. The guest CPU on a host is computed from that host:

- **Host floor:** the Arm feature set of the M2. It's recorded once from a real M2 (chunk 2) as the `floor` flag on
  the entries of `admission::FEATURES`. The host probe checks features, not chip names. A host that lacks a floor
  feature some core feature requires is refused at startup, with every missing item listed. Apple's TSO bit and the
  interrupt-controller API are part of the floor too, though they aren't ID-register features.
- **Core (mandatory)** features are every `built` feature whose `host` `required` Arm features all lie in the floor.
  That is derived, not hand-marked, so it can't drift. It must contain all of x86-64-v3: the validator checks this. Core
  then covers:
  - x86-64-v3;
  - the x86 extensions that translate directly onto floor Arm features (today AES, PCLMULQDQ, SHA, ADX; when built,
    candidates such as VAES, VPCLMULQDQ and AVX-VNNI);
  - the system and platform features natemu implements itself without any Arm feature (FSGSBASE, SMEP, UMIP, RDTSCP,
    x2APIC, CLFLUSHOPT, CLWB, RDRAND, the XSAVE family, ...). These are kept, so nothing advertised today is removed.

  Core features are advertised on every supported host.
- **Optional** features are `built` features whose required Arm features go beyond the floor. On a host that has
  them, they are advertised automatically; on a host that doesn't, they are absent. None exists today. The first one
  brings the host-derived mask of §3.
- **`exactness` Arm features** never change the guest CPU. A host missing one runs, and the startup line names the
  difference (AFP on the M2).

Core status applies to capability bits only. `dynamic` bits are exempt, because their value tracks guest state, not
the CPU's capability: OSXSAVE follows CR4.OSXSAVE, APIC follows APIC_BASE, and HTT follows the vCPU count.

The validator (`cpu_profile.py`, run by the generator and the tests) refuses:
- an advertised feature whose `requires` is not advertised;
- a `not-built` feature that is advertised;
- an x86-64-v3 feature that is not core (OSXSAVE, a `dynamic` bit, is exempt);
- a `state` that disagrees with the derivation: a feature is `advertised` exactly when it is built and core (optional
  features are `clear` in the JSON and set per host by the mask);
- a core feature that requires an optional one;
- an optional `layout` feature (layout features must be core, so XCR0, leaf 0xD and the XSAVE image are the same on
  every host);
- an optional feature whose `resident` behaviour is not gated on the mask;
- a `host` `required` entry that names an Arm feature `admission::FEATURES` doesn't know.

### 2.2 Host Arm features: keep the existing table as the source

`admission::FEATURES` stays the source. It gains two fields per entry:

- a **policy**: one of
  - `use when present`;
  - `off` (with a reason; for rpres, wfxt, lrcpc and lrcpc2: "kept off to match the reference producer"; the ISAR2
    class can't be read from the vCPU and stays off until a feature needs it);
  - `required` (FlagM, LSE);
- `floor`: whether the M2 has it (§2.1). Code-generation policy is separate: a feature above the floor (AFP on the M4)
  is still used for generated code when present;
- the admission encoding classes that depend on it (LSE-only atomics, the POPCNT sequence, CNTVCT or CNTVCTSS).

The tie to fexc is a host test, not a generated header. fexc already reports its table in order with each value, in
every reply (`features.host_features`). The test compares those names, their order and their defaults with FEATURES
and their policies. fexc keeps its defaults, which the CLI tools and corpus runners rely on. The request arguments are
unchanged: only features the vCPU lacks are sent, as `name=0`.

One host probe at startup does three things:
1. Read the ID registers admission already reads, through the hypervisor's vCPU view.
2. Check the hard requirements once: FlagM, LSE, the TSO bit, the interrupt-controller API, and every floor feature a
   core feature requires. It fails with one message listing everything missing. This replaces the duplicate FlagM checks in vm.rs and admission (fexc's own
   refusal stays, as its CLI guard).
3. Answer CSV3 and SB from PFR0 and ISAR1 instead of sysctl. This is the vCPU's view, which matters for SB because the
   resident runs it.

The probe's record also holds the exactness facts the M2 work will need, such as whether AFP is present. Startup prints
one stderr line naming the host's missing optional features and what that changes. There is no separate report
command.

### 2.3 Generated gate constants

The generator writes two files from the JSON:
- `GF_*` constants as a C++ header for the shim;
- the same constants as a Rust module in `crates/abi`, next to `cpu_profile.rs`.

**Shim (`fexc_slice1.inc`).** Every row that tests a feature uses its `GF_*` constant: VAES's ymm form,
VPCLMULQDQ's ymm form, AVX-VNNI, GFNI, RTM, CET, WAITPKG, SSE4a, 3DNow, plus the SHA and ADX rows. Rows for core
features need no gate. FEX's `x86_avx` follows this rule: 1 whenever any VEX feature is advertised (AVX, BMI1/2,
FMA, F16C), with the shim rejecting the AVX forms if AVX itself were clear. `x86_aes256`, `x86_3dnow` and `x86_sse4a`
only feed FEX's CPUID.cpp, which natemu never calls; they stay pinned.

**Host decode (`system.rs`).** Every unconditional "#UD, not advertised" becomes `advertised(feature)` on the same
constants.

**Cross-check test.** It reads the JSON, the shim and `system.rs`. It fails when:
- a gate names a feature the catalogue lacks;
- a feature with a `gates` entry has no gate in a consumer it lists.

**Building a feature** is then an edit to the JSON and a rebuild. That changes the fexc binary, so the persistent
compile cache is invalidated by the binary hash, as with every fexc rebuild today. The request arguments do not change.
While every built feature is core, which is the case today and stays so for floor-translatable additions, the guest
CPU is the same on every supported host, and these compile-time constants are exact.

## 3. The host-derived mask (built with the first optional feature)

The first optional feature is one whose Arm needs lie above the M2 floor. With it, the guest CPU differs between hosts,
for example an M2 and an M4. The host then computes a guest feature mask at startup from the probe: core, plus each
optional feature whose required Arm features are present. No option or environment variable changes the mask. It needs
the following:

- **Resident tables: one compiled superset plus a mask.** The MSR array's length and its slot numbers are computed at
  compile time (`cpu.rs` `Msrs`, `msr()`, the reset and MTRR slots), and the host and driver share that layout. So the
  tables can't come from a page. The generator compiles one superset covering every `built` feature, and tags each
  feature-gated CPUID bit, MSR and CR4 bit with its feature. The host writes one guest feature mask into
  `cache::Control`, the record it already writes and the resident already trusts (beside `speculation_barriers`). The
  resident ANDs the superset with that mask. The compiled-in consumers that move to reading the mask:
  - `cpu.rs`'s CPUID answers.
  - `CR4_VALID` in `execution.rs`.
  - MSR existence. RDMSR and WRMSR find a slot with `cpu::msr()` and go straight to the table, so `msr()` and the
    read/write checks test the mask: a masked-off superset MSR is #GP. The judge vectors gain a masked case.
  - The host decode. `system.rs`'s `advertised()` reads the effective mask, not the compiled CPUID table.
  - The shim. From chunk 5 it reads `--guest-features`. The `GF_*` constants of §2.3 become bit numbers into that mask
    instead of compile-time truth values.

  Layout features are always core, so `XSAVEC`, `XSAVEOPT`, XCR0 and leaf 0xD stay compiled constants.
- **fexc.** One compact argument, `--guest-features HEX`, carries the mask. It goes into the request arguments and so
  into the persistent key (adding about 20 bytes). The object header carries only host feature bits, so admission
  can't see the guest mask. That is safe only because the persistent key includes the arguments, and this plan states
  it as a contract. The mask is the same on every vCPU; `set_cpu` asserts it.
- **Gating.** Only optional features need mask gates in the shim, in `system.rs` and in the resident. Each is gated
  from the start, when it is built. Core forms keep their unnamed rows. The startup line names the optional features
  this host adds or lacks.

## 4. Chunks and gates

The default launch is byte-for-byte unchanged in every chunk. The full suite runs once, before merge; targeted gates
run between chunks.

**Replay gate.** No recorded compile stream exists today: the earlier streams were deleted, and they were captured by
ad-hoc `tee`. Chunk 0 records one installer boot's and one `build` boot's request stream at 333b7b6, kept under
`natemu-raw`. The ignored test `channel_replay_matches_the_pipe` then replays them, comparing status, code, object,
extent and the guest end fields.
- Compile-time chunks don't change request bodies, so the replay sends the same bodies to the old and the new fexc.
- Chunk 5 adds a body-rewrite step: the old body goes to the old fexc, the body plus the baseline mask goes to the new
  fexc.

| Chunk | Content | Gate |
| --- | --- | --- |
| 0. Stream | Record the two request streams at 333b7b6, with the fexc SHA-256 beside each; the streams fall under the disk-hygiene rule (several GB each); make the replay a documented tool (stream path, body rewrite hook) | the replay of each stream against today's fexc is identical (a self-check) |
| 1. Catalogue | Per-bit entries extended (`requires`, `host`, `msrs`, `cr4`, `subleaf`, `resident`, `gates`, `layout`, `not-built`); the derived core/optional classification; the validator | `cpu_profile.rs` regenerates identically; every advertised feature classifies as core; negative controls refused: a missing requirement, an advertised `not-built` feature, an x86-64-v3 feature made non-core by a required Arm feature above the floor, an optional layout feature |
| 2. Host table and probe | Policy, `floor` and encoding-class fields in FEATURES; the floor from the appendix (sysctl, recorded) confirmed by the vCPU ID registers natemu reads, on the M2; the fexc-reply cross-check test; one probe with the requirements checked once; CSV3/SB read from the vCPU's registers; the startup line | `test_serve.py`: replies unchanged; startup refusal tested with FlagM, LSE or a floor feature simulated missing; on the M4, CSV3/SB from registers equal sysctl's; on the M2, the probe passes and names AFP as an exactness difference |
| 3. Gate constants | Generated `GF_*` for C++ and Rust; gated shim rows; the `x86_avx` rule; `system.rs` checks; cross-check test | replay identical on both streams and the standing corpora; a scratch build with SHA and ADX cleared in the JSON makes their forms #UD in fexc and the host decode alike (the old hand cut, now derived; not committed); negative control: a scratch optional entry with a deliberately missing shim gate fails the cross-check |
| 4. Records | Contracts and README; the adding-a-feature checklist; the decision record's row | review |
| 5. (with the first optional feature) Host-derived mask | Superset tables plus mask in `cache::Control`, computed from the probe; `--guest-features`; the vCPU-identity assertion | replay with the body rewrite identical; judge vectors identical for the all-core mask; a host lacking the optional feature's Arm feature answers per the judge without it |

Chunks 1, 2 and 0 are independent of each other. Chunk 3 depends on 0 and 1. Chunk 5 depends on all of them.

**Adding a feature afterwards:**
1. Extend its entry, including its required Arm features.
2. Build its resident or host parts.
3. Gate its rows.
4. Add its tests.
5. Mark it advertised.

It classifies itself: core when its Arm needs are in the floor, otherwise optional, which needs chunk 5. The validator
refuses the entry until each step is done. The cheap candidates of November 14 are core candidates, because their Arm
needs (AES, PMULL, dotprod or i8mm) are expected in the M2 floor, to be confirmed by chunk 2's record:
- VAES and VPCLMULQDQ need steps 1, 3 and 4 only;
- AVX-VNNI needs steps 1, 3 and 4 plus leaf 7 subleaf 1.

ERMS/FSRM is a hint with no Arm feature behind it. RPRES is a host code-generation policy, not a guest feature.

## 5. Deferred

- **Reference tooling.** Many `-cpu` strings are for benchmark or system runs where parity isn't claimed, and the
  frozen corpora reproduce standing evidence. Rewriting them adds risk for no stated goal. New corpora may use a small
  `qemu_cpu(features)` helper in `cpu_profile.py`, added with the first feature that needs one.
- **Reading the ISAR2 class or DCZID through the resident at boot.** Every such feature is off by policy today. Build
  it with RPRES or the M2 work.
- **RDCL_NO.** It stays the five-line coupling in `execution.rs`; a declarative condition for one case isn't worth the
  machinery.

## 6. Owner decisions (November 15)

1. **Guest CPU:** x86-64-v3, plus the x86 extensions that translate easily onto Arm features. Read here as follows:
   - every built feature whose Arm needs are in the host floor is core;
   - the system features natemu implements itself stay;
   - nothing advertised today is removed.

   This plan admits such extensions when they are built. It does not build them: VAES, VPCLMULQDQ, AVX-VNNI and the
   rest are decided one at a time afterwards (the non-goal above). Owner, November 15: "they are optional and dependent
   on host's cpu features unless they are universally supported". That is the derivation of §2.1. Against the recorded
   floor (appendix) all three are universally supported, so each would be core once built:
   - VAES on AES;
   - VPCLMULQDQ on PMULL;
   - AVX-VNNI on DotProd, with I8MM also present.
2. **Host floor:** the M2's Arm feature set and above. M1 and anything else below the floor isn't a target. A host is
   refused only for missing a feature something requires, and the check is by feature, never by chip name.
3. **Host features eagerly determine guest features:** there is no guest cherry-picking. No option selects guest
   features. The guest CPU is a function of the host (§2.1, §3). One consequence follows once an optional feature
   exists: the same guest disk image sees different CPUID on an M2 and an M4. Software that chose code paths at
   install time could find a feature missing after moving to an M2. Natemu has no saved VM state, so a guest always
   boots afresh and probes again. That limits the effect to install-time choices, and it is accepted.

Decided as routine:
- The catalogue extends the JSON's per-bit entries.
- The policy pins from the old producer are kept as `off` with their reason, and revisited when a feature needs them.
- Core and optional are derived from each feature's Arm needs and the floor, not marked by hand.

## 7. Review response (independent review, November 15)

Verdict: accept with required changes. Every finding is accepted. The same reviewer re-reviewed the revision
(second round, rows R2-*): accept with required changes. Findings 3-12 were resolved, and the second round's findings
are answered below.

| # | Finding | Answer |
| --- | --- | --- |
| 1 | Blocking: a boot-page MSR table breaks the compile-time MSR layout and its consumers | The boot page is dropped. §3 uses a compiled superset plus a mask in `cache::Control`, and names the consumers that move |
| 2 | Blocking: `-FEATURE` would leave baseline forms running (the shim is an allowlist) | Added `selectable` with the converse check, `layout`, and the `resident` field. Subtraction is owner decision B |
| 3 | Blocking: the replay gate can't run (no stream; bodies change) | Chunk 0 records the streams; a body-rewrite step is added for chunk 5; chunk 2 uses `test_serve.py` |
| 4 | CSV3 and SB are register fields already read; the EL1 ISAR2 read would run the resident before the feature set is known | Both are read from the vCPU's registers; the EL1 read is deferred (§5) |
| 5 | `host_features.json` and the generated header are more than needed; dropping fexc's defaults breaks the CLI | FEATURES stays the source; the tie is a reply cross-check test; the defaults are kept; the arguments are unchanged |
| 6 | Reference-tooling chunk is over-scoped | Deferred (§5) |
| 7 | Named profiles, the report command and the RDCL_NO condition are over-scoped | Named profiles dropped; a startup line replaces the report command; RDCL_NO is left as it is |
| 8 | `x86_avx` gates VEX and XSAVE code; the other switches are dead | The `x86_avx` rule is in §2.3; the others stay pinned |
| 9 | A parallel `features` list duplicates the per-bit entries | The per-bit entries are extended; there is no parallel list |
| 10 | Leaf 0xD, XSAVE and leaf 7's maximum subleaf | `layout` covers the listed features; the maximum subleaf is computed |
| 11 | Missed risks: header feature bits, ACPI x2APIC, the per-vCPU mask, cache invalidation per rebuild | Each is stated in §2.3 and §3 |
| 12 | Blocking: §5 lacked the real decisions | §6 now lists decision A (runtime selection) and B. B was "subtraction allowed?"; after the owner's core-set direction it became "core membership", which settles subtraction: only optional features can be removed. The routine decisions are recorded as decided |
| R2-1 | Blocking (second round): the core text would refuse M1 and M2 (AFP), and the `host` field had been lost | `host` restored with `required` and `exactness`; AFP is exactness; decision B's sentence corrected |
| R2-2 | Blocking: dropping optional features per host needs the per-launch mask | §2.1 says so: without chunk 5, the required Arm features of every advertised feature are hard requirements |
| R2-3 | Blocking if A: the list of consumers that move missed MSR existence, the host decode and the shim; XSAVEC is not selectable | §3's list corrected, with a masked judge case |
| R2-4 | "x86-64-v3 smallest core" is wrong; XSAVEOPT's `layout` reason | Corrected to x86-64-v2 plus AVX and the XSAVE family; `layout` includes "transaction compiled in" |
| R2-5 | The converse check relies on each entry's own list | Negative control added to chunk 3 |
| R2-6 | Replay design sound; record the fexc hash; disk hygiene | Added to chunk 0 |
| R2-7 | `core` versus dynamic bits | `core` applies to capability bits; dynamic bits are exempt |
| R3-1 | Third round, verdict accept. `layout`'s "transaction compiled in" was too broad: it would force every resident-implemented feature into the core set | Narrowed to "presence tested as a compile-time constant" (today XSAVEC and XSAVEOPT) |
| R4 | Fourth round, on the owner revision: SHA, AES and PCLMULQDQ have no FEX fallback, contrary to the second round's note; stale wording; `state` duplicated; OSXSAVE versus the v3 rule; build-now versus admit-when-built | `required` judged over the whole pipeline and listed for the three; wording fixed; validator checks `state` against the derivation; OSXSAVE exempt; §6 makes admit-when-built explicit and notes the CPUID-differs-by-host consequence; chunk 2 names a fallback source for the floor |
| Owner | Decisions after the review (§6): no guest selection; the M2 floor; core derived from the floor | `selectable`, the `--cpu` option and the hand-marked `core` flag are removed. Core and optional are derived from Arm needs and the floor. §3 became the host-derived mask, built with the first optional feature. Earlier rows that mention selection, `-FEATURE` or decisions A and B describe the text as it was then |

## Appendix: the host floor (recorded November 15, 2026)

**Source.** `sysctl hw.optional.arm` on the owner's M2 Max, and the same on this M4. Chunk 2 confirms the floor through
the vCPU's ID registers on the M2. Sysctl describes the host, and the vCPU's view can differ (§2.2).

**The floor** is the Arm features both machines report. The M4 lacks one feature the M2 has (SSBS), so "the M2 and
above" is the intersection of the two, not the M2's list:
- AdvSIMD, AdvSIMD_HPFPCvt, FP16;
- AES, PMULL, SHA1, SHA256, SHA3, SHA512;
- BF16, I8MM, DotProd, FHM, RDM, FCMA, JSCVT, FRINTTS;
- CRC32, FlagM, FlagM2;
- LSE, LSE2, LRCPC, LRCPC2;
- ECV, SB, CSV2, CSV3, DIT;
- DPB, DPB2, PAuth, PAuth2, PACIMP, FPAC, BTI.

Apple's TSO bit and the interrupt-controller API are part of the floor too, though they aren't ID-register features.

**Above the floor** (M4 only): AFP, RPRES, WFxT, FPACCOMBINE, SME/SME2 and its variants, and synchronous FP exceptions.

**M2 only:** SSBS. Natemu does not use it.

**Against fexc's feature table.** Every Arm feature fexc uses when present is in the floor except AFP: atomics_lse,
flagm, flagm2, fcma, crc32, aes, sha, pmull128, frintts, i8mm, dotprod, ecv. lrcpc and lrcpc2 are in the floor but off by
policy. AFP is the one exactness feature. Neither machine reports RNG, CSSC, MOPS or SVE.


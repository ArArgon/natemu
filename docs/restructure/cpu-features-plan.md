# CPU features and profiles: one source for what the guest sees and what the host offers

Status: revised after independent review (November 15, 2026), branch `cpu-features` off `main` at 333b7b6. No code
yet. Owner direction (November 15): build a centralized CPU feature/profile mechanism before deciding which new guest
features to implement; there is a core (mandatory) set of features (§2.1); the M2 (no FEAT_AFP) floating-point work
comes after this plan. The review's findings and how
each is answered are in §7. Two owner decisions are open (§6).

**Goal:** every CPU feature decision comes from one place, with tool-checked consistency, and reaches every consumer
from that place. This covers decisions the guest sees (x86 CPUID, MSRs, control-register bits, instruction forms) and
host-side ones (the Arm features that generated code, admission and the resident rely on). Changing a guest feature
becomes an edit to its catalogue entry plus the feature's own implementation, not a hunt through hand-written tables.
The default launch stays byte-for-byte as it is.

**Non-goals:**
- Implementing any new guest feature (RDPID, VAES, the deadline timer, ...).
- Changing the default guest CPU.
- The M2 floating-point differences.
- Making layout-changing features selectable (§2.1).
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

This part meets the goal on its own: one place, tool-checked, default unchanged. Runtime selection (§3) is an owner
decision on top of it.

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
- `layout`: true when the feature changes XCR0's valid values, leaf 0xD, or the XSAVE image. That covers AVX, MPX,
  AVX-512, PKU, CET, AMX, XSAVES/XFD, XSAVEC and XSAVEOPT.
- `selectable`: false by default. It may be set only when the feature is not `layout` and every consumer it touches
  tests its gate (§3).
- `source`: the SDM section and the decision that set its state. The existing `source` strings stay.

`state` keeps its meaning: `advertised`, `clear` or `dynamic`. A new status, `not-built`, marks a feature with no
implementation; it can never be advertised.

**Core set (owner direction, November 15).** A `core: true` entry marks a mandatory feature. Core features:
- are always advertised and are never `selectable`, on every host, in every build and launch;
- have every Arm feature they need for correctness (`host` required) become a hard host requirement, so the host probe
  (§2.2) refuses to start a VM without it rather than drop the feature.

Every other advertised or `built` feature is optional. An optional feature whose required Arm feature is missing is not
offered on that host. Proposed membership is decision B (§6): today's baseline, all of it.

The validator (`cpu_profile.py`, run by the generator and the tests) refuses:
- an advertised feature whose `requires` is not advertised;
- a `not-built` feature that is advertised;
- `selectable` on a `layout` feature;
- `selectable` on a feature whose `resident` behaviour is not gated;
- a core feature that is clear, `not-built` or `selectable`;
- a core feature that requires a non-core feature;
- a `layout` feature that is optional (layout features are core or clear).

### 2.2 Host Arm features: keep the existing table as the source

`admission::FEATURES` stays the source. It gains two fields per entry:

- a **policy**: one of
  - `use when present`;
  - `off` (with a reason; for rpres, wfxt, lrcpc and lrcpc2: "kept off to match the reference producer"; the ISAR2
    class can't be read from the vCPU and stays off until a feature needs it);
  - `required` (FlagM, LSE);
- the admission encoding classes that depend on it (LSE-only atomics, the POPCNT sequence, CNTVCT or CNTVCTSS).

The tie to fexc is a host test, not a generated header. fexc already reports its table in order with each value, in
every reply (`features.host_features`). The test compares those names, their order and their defaults with FEATURES
and their policies. fexc keeps its defaults, which the CLI tools and corpus runners rely on. The request arguments are
unchanged: only features the vCPU lacks are sent, as `name=0`.

One host probe at startup does three things:
1. Read the ID registers admission already reads, through the hypervisor's vCPU view.
2. Check the hard requirements once: FlagM, LSE, the TSO bit and the interrupt-controller API. It fails with one
   message listing everything missing. This replaces the duplicate FlagM checks in vm.rs and admission (fexc's own
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
VPCLMULQDQ's ymm form, AVX-VNNI, GFNI, RTM, CET, WAITPKG, SSE4a, 3DNow, plus the SHA and ADX rows. Rows for baseline
`fixed` features need no gate. FEX's `x86_avx` follows this rule: 1 whenever any VEX feature is advertised (AVX, BMI1/2,
FMA, F16C), with the shim rejecting the AVX forms if AVX itself were clear. `x86_aes256`, `x86_3dnow` and `x86_sse4a`
only feed FEX's CPUID.cpp, which natemu never calls; they stay pinned.

**Host decode (`system.rs`).** Every unconditional "#UD, not advertised" becomes `advertised(feature)` on the same
constants.

**Cross-check test.** It reads the JSON, the shim and `system.rs`. It fails when:
- a gate names a feature the catalogue lacks;
- a feature with a `gates` entry has no gate in a consumer it lists.

**Flipping a feature** is then an edit to the JSON and a rebuild. That changes the fexc binary, so the persistent
compile cache is invalidated by the binary hash, as with every fexc rebuild today. The request arguments do not change.

## 3. Option: guest features selected per launch (owner decision A)

Compile-time centralisation (§2) leaves one guest CPU per build. Selecting features per launch adds two things:
- a feature can be tested while it is off by default;
- a feature that needs an Arm feature the host lacks can be refused on that host.

It costs the following:

- **Resident tables: one compiled superset plus a mask.** The MSR array's length and its slot numbers are computed at
  compile time (`cpu.rs` `Msrs`, `msr()`, the reset and MTRR slots), and the host and driver share that layout. So the
  tables can't come from a page. The generator compiles one superset covering every `built` feature, and tags each
  feature-gated CPUID bit, MSR and CR4 bit with its feature. The host writes one guest feature mask into
  `cache::Control`, the record it already writes and the resident already trusts (beside `speculation_barriers`). The
  resident ANDs the superset with that mask. The compiled-in consumers that move to reading the mask:
  - `XSAVEC` in `xsave.rs`;
  - `CR4_VALID` in `execution.rs`;
  - `cpu.rs`'s CPUID answers.

  Layout features are not selectable, so XCR0 and leaf 0xD stay constant.
- **fexc.** One compact argument, `--guest-features HEX`, carries the mask. It goes into the request arguments and so
  into the persistent key (adding about 20 bytes). The object header carries only host feature bits, so admission
  can't see the guest mask. That is safe only because the persistent key includes the arguments, and this plan states
  it as a contract. The mask is the same on every vCPU; `set_cpu` asserts it.
- **Command line.** `--cpu +FEATURE,-FEATURE` (and `NATEMU_CPU`) adjusts the baseline; there are no named profiles.
  The validator refuses non-`selectable` features. Without the option the mask is the baseline, and the replies
  are byte-identical (§4).
- **Core features can't be removed.** `-FEATURE` applies only to optional features. Removing a feature means every one
  of its forms must be gated: in the shim, where baseline forms are allowed by unnamed rows or by FEX's own decoder, and
  in `system.rs` and the resident. That is why core membership (decision B) sets most of this option's cost. With the
  whole baseline as core, only features added later are optional, and they are gated from the start.

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
| 0. Stream | Record the two request streams at 333b7b6; make the replay a documented tool (stream path, body rewrite hook) | the replay of each stream against today's fexc is identical (a self-check) |
| 1. Catalogue | Per-bit entries extended (`requires`, `msrs`, `cr4`, `subleaf`, `resident`, `gates`, `layout`, `core`, `selectable`, `not-built`); the validator | `cpu_profile.rs` regenerates identically; negative controls refused: a missing requirement, an advertised `not-built` feature, `selectable` on a layout feature, a clear or selectable core feature, a core feature requiring an optional one |
| 2. Host table and probe | Policy and encoding-class fields in FEATURES; the fexc-reply cross-check test; one probe with the requirements checked once; CSV3/SB read from the vCPU's registers; the startup line | `test_serve.py`: replies unchanged; startup refusal tested with FlagM or LSE simulated missing; on the M4, CSV3/SB from registers equal sysctl's |
| 3. Gate constants | Generated `GF_*` for C++ and Rust; gated shim rows; the `x86_avx` rule; `system.rs` checks; cross-check test | replay identical on both streams and the standing corpora; a scratch build with SHA and ADX cleared in the JSON makes their forms #UD in fexc and the host decode alike (the old hand cut, now derived; not committed) |
| 4. Records | Contracts and README; the adding-a-feature checklist; the decision record's row | review |
| 5. (decision A) Runtime selection | Superset tables plus mask in `cache::Control`; `--guest-features`; `--cpu`; the vCPU-identity assertion | replay with the body rewrite identical; judge vectors identical for the baseline mask; a test profile answering per the judge |

Chunks 1, 2 and 0 are independent of each other. Chunk 3 depends on 0 and 1. Chunk 5 depends on all of them.

**Adding a feature afterwards:**
1. Extend its entry.
2. Build its resident or host parts.
3. Gate its rows.
4. Add its tests.
5. Mark it advertised, or, with chunk 5, `selectable`.

The validator refuses the entry until each step is done. The cheap candidates of November 14 (VAES/VPCLMULQDQ,
AVX-VNNI, ERMS/FSRM) then need steps 1, 3 and 4 only. RPRES is a host policy change in FEATURES, plus a way to read
ISAR2.

## 5. Deferred

- **Reference tooling.** Many `-cpu` strings are for benchmark or system runs where parity isn't claimed, and the
  frozen corpora reproduce standing evidence. Rewriting them adds risk for no stated goal. New corpora may use a small
  `qemu_cpu(features)` helper in `cpu_profile.py`, added with the first feature that needs one.
- **Reading the ISAR2 class or DCZID through the resident at boot.** Every such feature is off by policy today. Build
  it with RPRES or the M2 work.
- **RDCL_NO.** It stays the five-line coupling in `execution.rs`; a declarative condition for one case isn't worth the
  machinery.

## 6. Owner decisions

**A. Do we need guest features selected per launch (§3, chunk 5)?**
- Without it, every guest feature is fixed per build, and changing one is a JSON edit plus a rebuild.
- With it, a feature can be shipped off by default and switched on for testing, and refused on hosts that lack its Arm
  support.
- The M2 work doesn't need it: SSE is baseline, and the M2's differences are host-side.

Recommendation: build §2 now (chunks 0-4), and add chunk 5 when a feature first needs to ship off by default.

**B. Which features are core (mandatory)?** The core set itself is decided (owner, November 15); its membership is
open. Recommendation: today's whole baseline. It is what every corpus, boot and benchmark has run on. Making any of it
optional means gating forms that are allowed today by unnamed shim rows and FEX's own decoder (§3). Every Apple chip so
far has the Arm features it needs, given FlagM and LSE. The alternative is a smaller core, such as x86-64-v2 or v3,
with later additions (SHA, ADX, CLFLUSHOPT, CLWB, UMIP) optional. That buys the ability to switch those off, at the
gating cost. Core membership can shrink later only by doing that gating. Whatever is chosen, the layout features
advertised today (AVX, XSAVE, XSAVEOPT, XSAVEC) must stay core, so x86-64-v3 is the smallest practical core.

Decided as routine, no longer open:
- The catalogue extends the JSON's per-bit entries.
- The policy pins from the old producer are kept as `off` with their reason, and revisited when a feature needs them.

## 7. Review response (independent review, November 15)

Verdict: accept with required changes. Every finding is accepted.

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

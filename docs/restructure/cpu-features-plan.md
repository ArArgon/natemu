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

  `required` is a list of alternatives: each item is one Arm feature or an any-of group. AVX-VNNI is the case today. It
  requires DotProd or I8MM: FEX lowers it with USDOT under I8MM, else with SDOT plus a correction. I8MM is listed as
  `speed` for it, not as required. A third class, `speed`, records Arm features that only make a feature faster
  (ECV for RDTSC, FCMA for ADDSUBPS, FRINTTS, FlagM2, I8MM for AVX-VNNI). It never affects classification or startup.
- `layout`: true when the feature changes XCR0's valid values, leaf 0xD or the XSAVE image, or whose presence the
  resident tests as a compile-time constant (today XSAVEC and XSAVEOPT; a resident transaction alone, such as RDRAND's or
  CLWB's, does not make a feature `layout`). That covers AVX, MPX, AVX-512, PKU, CET, AMX, XSAVES/XFD, XSAVEC and XSAVEOPT.
- `source`: the SDM section and the decision that set its state. The existing `source` strings stay.

`state` keeps its meaning: `advertised`, `clear` or `dynamic`. A new status, `not-built`, marks a feature with no
implementation; it can never be advertised.

**Host features determine guest features (owner decisions, November 15).** There is no per-launch selection: no user
picks guest features, and nothing cherry-picks them. The guest CPU on a host is computed from that host:

- **Host floor:** the Arm features common to the M1, the M2 and the M4 (owner, November 15; appendix), with ECV
  left out until a real M1 settles it. It's recorded as the `floor` flag on the entries of `admission::FEATURES`.
  The floor is the planning set that core is derived from. The probe doesn't check the whole floor: it checks each
  feature some entry declares as `required`. A floor feature nothing requires never refuses a host, and code may not
  rely on a floor feature without declaring it. The host probe checks features, not chip names. A host that lacks a floor
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

### 2.2a Host feature classes: what each means and what the plan does for it

Every Arm feature (and each non-ID-register host property) gets exactly one class in `admission::FEATURES`. The class
says how it is detected, what happens on a host without it, and what keeps it honest.

| Class | Meaning | Members (floor: M1 ∩ M2 ∩ M4, ECV out) | Without it | Kept honest by |
| --- | --- | --- | --- | --- |
| **required** | a core guest feature, or the machine itself, cannot run without it | FlagM, LSE, LSE2, CRC32, AES, PMULL, SHA1+SHA256, Armv8.0 SIMD/FP; non-register: Apple TSO bit; the interrupt-controller API (a minimum-macOS fact, not a probe: see below) | startup refuses, listing every missing item | the probe (chunk 2); a refusal test per item with the item simulated missing |
| **exactness** | runs without it, but some guest-visible result differs from x86 | AFP | runs; startup line names the difference; tests that depend on it are tagged with it | M2 work (after this plan): tagged test expectations; the AFP-dependent FPCR bits and flag mapping made conditional |
| **speed** | a fallback exists; only performance changes | FlagM2, FCMA, FRINTTS, ECV, I8MM (for AVX-VNNI); above the floor RPRES (off by policy); neither chip CSSC, MOPS | fexc gets `name=0`; generated code takes FEX's fallback | the fallback gate (below) |
| **option** | used only when a command-line option asks for it | SB (`--speculation-barriers`), CSV3 (RDCL_NO with that option) | the option is refused (SB) or the bit stays clear (CSV3) | existing refusal; probe reads both from the vCPU's registers |
| **guest-optional** | backs an optional guest feature, advertised only where present (§3) | none built; first candidate WAITPKG on WFxT (M4 only) | the guest feature is absent on that host | the host-derived mask's tests (chunk 5) |
| **off by policy** | present, but not used | LRCPC, LRCPC2 (hardware TSO makes them pointless), WFxT and RPRES until a feature needs them | nothing | the reply cross-check (FEATURES' policy matches fexc's table) |
| **unused** | nothing in natemu or fexc uses it | SME/SME2, synchronous FP exceptions (precise exceptions are done in software by design), BF16, FHM, RDM, JSCVT, SHA3, SHA512, PAuth/PAuth2/FPAC, BTI, DIT, DPB/DPB2, SSBS | nothing | listed so a new use has to change its class |

**The fallback gate (new, chunk 2).** The speed class is the one most likely to break unnoticed: every machine that
runs the suite today has all of it. No tool today forces a feature off in a boot or a corpus run: the host builds its
`--feature NAME=0` arguments only from the vCPU's ID registers.

Chunk 2 adds a test-only override, `NATEMU_FEXC_FEATURE_OFF=name,...`, which removes the named features from
`supported` before the arguments are built. Admission's subset check is unaffected, because clearing bits keeps the
set a subset.

On the M4, the standing corpora runners and one boot run with each speed feature forced off in turn (FlagM2, FCMA,
FRINTTS, ECV). Each row uses a scratch compile cache, because the arguments change the key. The rules:
- admission must accept every object;
- results must equal the all-on run.

What the gate can't do:
- **I8MM.** It is used only by AVX-VNNI, which isn't built, so forcing it off exercises nothing until then. It joins
  the gate with AVX-VNNI.
- **AFP.** Forcing it off changes only FEX's code. The resident still sets FPCR.AH and NEP, and the M4 obeys them, a
  combination that exists on no real machine. Exactness is covered only on a real M2 (the owner has one) and in the M2
  work.

**Non-register host properties** are recorded in the same table with a probe of their own:
- the TSO bit is written and read back;
- the interrupt-controller API: the `hv_gic_*` functions are strong imports, so on a macOS without them the binary
  doesn't load at all. Its minimum macOS is stated as a build and deployment fact (README and the binary's
  deployment target), not as a runtime check;
- the IPA size is already checked against the backing at VM creation, with a clear error;
- the counter frequency: the inline-TSC fast path is used only at 24 MHz, else the transaction path; speed class.

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
| 2. Host table and probe | Policy, `floor` and encoding-class fields in FEATURES; the floor from the appendix (sysctl, recorded) confirmed by the vCPU ID registers natemu reads, on the M2; the fexc-reply cross-check test; one probe with the requirements checked once; CSV3/SB read from the vCPU's registers; the startup line | `test_serve.py`: replies unchanged; startup refusal tested with FlagM, LSE or a floor feature simulated missing; on the M4, CSV3/SB from registers equal sysctl's; on the M2, the probe passes and names AFP as an exactness difference; the fallback gate (§2.2a) on the M4 |
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

### 4a. What natemu assumes today without checking (audit, November 15)

These are host properties the code relies on with no check before this plan. Each one gets its class and a check.

| Assumption | Where | Class | Action |
| --- | --- | --- | --- |
| LSE2 alignment behaviour for split locks | resident split-lock handling, code-object contract | required | add to the probe (chunk 2). It is ID_AA64MMFR2_EL1, which admission doesn't read yet: add MMFR2 to the registers read through the hypervisor if it exposes it, else take sysctl `FEAT_LSE2` |
| AFP: FPCR.AH/NEP/FIZ, Arm IDC = x86 DE, Arm UFC judged after rounding, the x87 first-operand NaN rule | `execution::fpcr`, `mxcsr_flags`, the tiny-result checks, x87 patches; FP tests | exactness | startup line now; tagging and conditional code in the M2 work |
| fexc's table defaults are the M4's view, and the off paths are never exercised: speed (FlagM2, FCMA, FRINTTS, ECV; DotProd/I8MM none today) and required (CRC32, AES, SHA, PMULL) | `fexc.cpp` FeatureTable | speed or required | fallback gate (§2.2a); required ones have no off path by definition, so the probe refuses instead |
| Admission accepts only the encodings the M4 configuration emits (LSE atomics; CNTVCT or CNTVCTSS; the no-CSSC POPCNT) | `admission/structure.rs` | follows the class of each feature | fallback gate proves admission accepts every fallback; a new speed feature must list its encoding class |
| The interrupt-controller API exists | vm.rs (GIC calls, strong imports) | required | stated as the minimum macOS (build and deployment fact); dyld refuses an older system before any probe could run |
| The resident is built with `+lse` | `.cargo/config.toml` | required | covered by LSE in the probe |

Nothing else was found: the host binaries and fexc are built for the default Apple target, which runs on the M1, and
the timer rate and IPA size are already checked.

**Answer to the owner's question ("does natemu's assumption demand optional features?"):**
- **AFP:** yes, as exactness only. Natemu runs without it, but its floating-point contracts and tests assume it.
- **LSE2:** yes, as a hard need. It is in every chip's floor, so it becomes a stated requirement rather than an
  optional feature.

No other optional feature is demanded.

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
2. **Host floor:** first "the M2's feature set and above"; then (same day) the features common to the M1, the M2 and
   the M4. The M1 is supported, but labelled unverified until natemu has run on one. A host is refused only for missing
   a feature something requires, and the check is by feature, never by chip name. ECV stays out of the floor while
   the M1 record and the owner disagree about it; it is speed-only either way, and nothing may become `required` on
   it.
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
| R5 | Fifth round, on the host classes: classes verified; the fallback gate had no override tool and its AFP and I8MM rows tested nothing real; LSE2 is in MMFR2, which isn't read; the GIC API is a strong import | Test-only override added; AFP and I8MM rows dropped with reasons; MMFR2 or sysctl named for LSE2; the minimum macOS stated as a deployment fact |
| R6 | Owner question "move the M1, M2 and M4's common features to core", assessed independently: yes; ECV out until confirmed; the probe checks only declared requirements, not the whole floor; check the M1's IPA and interrupt-controller limits | Floor is M1 ∩ M2 ∩ M4 minus ECV; M1 supported and labelled unverified; the probe stays requirement-based (this revises my own suggestion to check the whole floor); the non-feature M1 checks are listed |
| R7 | Owner: reports that Hypervisor.framework hides host features | Measured on the M4 (ECV hidden, SPECRES shown, ISAR2 unmodelled before macOS 27, SME shown) and recorded with online sources; the floor is defined over vCPU views; probe tool added |
| Owner | Decisions after the review (§6): no guest selection; the M2 floor; core derived from the floor | `selectable`, the `--cpu` option and the hand-marked `core` flag are removed. Core and optional are derived from Arm needs and the floor. §3 became the host-derived mask, built with the first optional feature. Earlier rows that mention selection, `-FEATURE` or decisions A and B describe the text as it was then |

## Appendix: the host floor (recorded November 15, 2026)

**Source.** `sysctl hw.optional.arm` on the owner's M2 Max, the same on this M4, and the published M1 Pro record below. Chunk 2 confirms the floor through
the vCPU's ID registers on the M2. Sysctl describes the host, and the vCPU's view can differ (§2.2).

**What the guest's virtual CPU sees differs from sysctl (measured November 15 on this M4, macOS 26.6.2).**
`tools/hvf_id_probe/run.sh` creates a Hypervisor.framework vCPU, reads its ID registers, and compares them with sysctl.
The vCPU configuration's feature registers and the created vCPU's registers agreed.
- **ECV is hidden.** The vCPU's ID_AA64MMFR0.ECV is 0, though the host has it. So natemu already never uses the
  self-synchronising counter read: admission sends `ecv=0`.
- **SPECRES is shown** in the vCPU's ID_AA64ISAR1, though sysctl reports 0. Natemu doesn't use it.
- **ID_AA64ISAR2 is not modelled before macOS 27.** The macOS 26 SDK has no feature-register ID for it; the macOS 27
  SDK adds HV_FEATURE_REG_ID_AA64ISAR2_EL1 (see the reports below). So RPRES, WFxT, CSSC and MOPS can't be seen by a
  guest, whatever the chip has. That blocks the WAITPKG-on-WFxT candidate until then.
- **SME is shown** on the M4 (Hypervisor.framework has exposed it since macOS 15.2). Natemu doesn't use it.
- Everything else that natemu or fexc uses matched sysctl: AFP, LSE2, I8MM, BF16, the crypto features, FlagM/FlagM2,
  FCMA, FRINTTS, LRCPC/2, SB, CSV2/3.
- The vCPU's ASIDBits is 16, and PARange is 40 bits on the M4.

Reports found online agree:
- Hypervisor.framework doesn't model ID_AA64ISAR2_EL1 before macOS 27.
- QEMU masked SME on M4 hosts after macOS 15.2 exposed it, and SME2 support for QEMU's HVF backend is in review.

No source lists which fields Hypervisor.framework masks, so the probe is the evidence. Sources:
https://github.com/pH14/harmony/pull/373, https://miniswift.run/whats-new/hypervisor/,
https://www.mail-archive.com/qemu-devel@nongnu.org/msg1097189.html,
https://ratatoskr.run/qemu-devel/2026/02/14429600/t.

**What Hypervisor.framework masks (reverse-engineered, macOS 12).** utmapp's hand decompilation of
Hypervisor.framework (https://github.com/utmapp/Hypervisor/blob/main/hv.c, from macOS 12.0b1, updated for 12.5.1)
builds the guest's ID registers from the kernel's capabilities with fixed masks:
- ISAR0 keeps everything but TME.
- ISAR1 keeps bits 43:0 only, which hides BF16, I8MM, XS and LS64.
- MMFR0 clears ECV and FGT and forces PARange to 36 bits.
- MMFR1 keeps bits 31:0 only, which hides AFP.
- MMFR2 keeps AT (LSE2).
- PFR0 clears SVE, SEL2, AMU, MPAM and RME.
- PFR1 keeps only SSBS.
- ISAR2 isn't modelled at all.

Our macOS 26 measurement on the M4 shows the masks have since been widened: I8MM, BF16, AFP, BTI and SME are now
visible, and PARange is 40 bits. ECV is still cleared and ISAR2 is still absent. So:
- **The vCPU view depends on the macOS version as well as the chip.** The floor is stated for natemu's minimum macOS
  (15, the first with the interrupt-controller API), and the startup probe is the authority.
- **Every `required` feature lies in fields that even the macOS 12 masks pass through:** FlagM, LSE, CRC32, AES, PMULL
  and SHA in ISAR0, and LSE2 in MMFR2. They are visible on every macOS natemu can run on.
- **The macOS-dependent features are all speed or exactness:** I8MM, BF16 and AFP.
- ECV is masked in every version seen, which is one more reason it stays out of the floor.

QEMU reads the same values through `hv_vcpu_config_get_feature_reg` without creating a vCPU
(https://www.mail-archive.com/qemu-devel@nongnu.org/msg1148454.html), as the probe here does.

**So the floor is defined over vCPU views, not sysctl.** Sysctl records are a first estimate only. Each record is
kept with its chip and macOS version, because the view can change with macOS (ISAR2 in macOS 27). The probe at startup
is the real check. The M4's vCPU view is recorded above. The M2's is pending: the owner runs
`sh tools/hvf_id_probe/run.sh` there. The M1's is unknown: only a sysctl record exists.

**The floor** (owner, November 15) is the Arm features the M1, M2 and M4 records all report, with ECV left out
(unsettled for the M1, and speed-only):
- AdvSIMD, AdvSIMD_HPFPCvt, FP16;
- AES, PMULL, SHA1, SHA256, SHA3, SHA512;
- DotProd, FHM, RDM, FCMA, JSCVT, FRINTTS;
- CRC32, FlagM, FlagM2;
- LSE, LSE2, LRCPC, LRCPC2;
- SB, CSV2, CSV3, DIT;
- DPB, DPB2, PAuth, PACIMP.

The earlier M2 ∩ M4 floor also had I8MM, BF16, PAuth2, FPAC, BTI and ECV. Nothing requires any of them, so no core
guest feature changes. A future feature that strictly needs I8MM or BF16 becomes optional rather than core. A lowering
that prefers I8MM must keep its DotProd or plain-SIMD path to stay core, as FEX's VPDPBUSD does.

Apple's TSO bit and the interrupt-controller API are part of the floor too, though they aren't ID-register features.

**Above the floor** (M4 only): AFP, RPRES, WFxT, FPACCOMBINE, SME/SME2 and its variants, and synchronous FP exceptions.

**M2 only:** SSBS. Natemu does not use it.

**The M1** (owner, November 15: first best-effort, then part of the floor). It is supported, and labelled unverified
in the startup line until natemu has run on one.

**Record.** `sysctl hw.optional` from an M1 Pro (MacBookPro18,1), as published at
https://gist.github.com/Mizux/fc3309b1efeb1454cc7b093c5f23992e. The owner pointed to it; the macOS version isn't
stated. That macOS has no FEAT_CRC32 or FEAT_AFP keys: CRC32 comes from `hw.optional.armv8_crc32: 1`, and a missing key
counts as absent.

**Second record** (owner, November 15): https://github.com/lelegard/arm-cpusysregs/blob/main/docs/apple-m1-features.md.
Its M1, M3 and M4 tables read the real ID registers at EL1 through a kernel extension (`sysregs -s`); its comparison
table uses sysctl. It doesn't state the chip variant or the macOS version.
- **Agrees with the first record** on every feature natemu requires: FlagM, FlagM2, LSE, LSE2, CRC32, AES, PMULL,
  SHA1/256, DotProd, FRINTTS, FCMA, LRCPC/2, SB, CSV3. All present on the M1.
- **Settles ECV:** the M1's register reads give ECV absent, though sysctl (its comparison table, and the first record)
  says present. ECV stays out of the floor in any case; on the M4 the hypervisor hides it anyway.
- **Shows sysctl under-reporting SPECRES:** the registers give it present on the M1, M3 and M4, while sysctl gives 0
  on the M2 and the M4. This agrees with the M4 vCPU measurement above.
- **Not fully reliable:** its M4 table gives I8MM absent, but the M4's sysctl and its vCPU's ID_AA64ISAR1 both give
  I8MM present (measured here). The M3 table also contradicts the same page's comparison table on I8MM. So its tables
  are an estimate, like sysctl, and the vCPU probe stays the evidence.

**Gaps against the floor:** I8MM, BF16, PAuth2, FPAC and BTI, plus everything above the floor (AFP, RPRES, WFxT, SME).
The M1 Pro has every required feature: FlagM, FlagM2, LSE, LSE2, CRC32, AES, PMULL, SHA1/SHA256 and DotProd. The record
lists ECV as present. The owner understands the M1 to lack it, so this is unsettled, but ECV is speed only and the
outcome doesn't depend on it. So the M1 gets the same guest CPU as the floor, and I8MM is speed only for AVX-VNNI,
whose any-of group DotProd satisfies.

Because the M1 is in the floor, the core derivation itself keeps any future core feature within the M1's means. No
separate warning is needed.

**Before claiming the M1, check the non-feature limits**, which could refuse it whatever its features:
- the hypervisor's maximum IPA size against natemu's address-space layout and default RAM (VM creation already checks
  it; the M1's limit is believed smaller and is unconfirmed);
- interrupt-controller (`hv_gic_*`) support on the M1 at the minimum macOS.

**Against fexc's feature table.** Every Arm feature fexc uses when present is in the floor except AFP (exactness),
I8MM and ECV (both speed): atomics_lse, flagm, flagm2, fcma, crc32, aes, sha, pmull128, frintts, dotprod. lrcpc and lrcpc2 are in the floor but off by
policy. AFP is the one exactness feature. Neither machine reports RNG, CSSC, MOPS or SVE.


**What the core guest features rely on** (from FEX's dispatcher and natemu's admission; chunk 1 records these as each
entry's `host` field). A feature is required when there is no fallback: FEX marks the instruction unimplemented without
it, or admission accepts only the encodings that use it.

| Arm feature | Class | Core guest features that use it |
| --- | --- | --- |
| FlagM | required | every flag-producing instruction (CFINV and friends; the code generator's settings require it) |
| LSE | required | LOCK prefixes, XCHG with memory, CMPXCHG8B/16B (admission accepts only LSE atomics; the resident is built with LSE) |
| Apple TSO bit, interrupt-controller API | required | the whole machine (x86 memory ordering; interrupts) |
| CRC32 | required | SSE4.2's CRC32 |
| AES | required | AES-NI |
| PMULL (128-bit) | required | PCLMULQDQ |
| SHA1, SHA256 | required | SHA-NI |
| AdvSIMD, FP, FP16 conversions (Armv8.0 base) | required | SSE through AVX2, FMA, F16C |
| AFP | exactness | SSE and x87 NaN choice, denormal flag, flush settings, underflow rounding |
| FlagM2 | speed only | flag conversions |
| FCMA | speed only | ADDSUBPS/ADDSUBPD |
| FRINTTS | speed only | floating-point-to-integer conversions |
| ECV | speed only | RDTSC (a self-synchronising counter read, else ISB and a counter read) |
| SB, CSV3 | option only | `--speculation-barriers` and the RDCL_NO bit |
| DotProd, or I8MM | none in core yet | AVX-VNNI when built: either one satisfies it (DotProd on the M1); I8MM gives the shorter sequence |
| LRCPC, LRCPC2 | none (off by policy) | unused under hardware TSO |

Bases with no extra Arm feature: BMI1/2, LZCNT, POPCNT (no CSSC: a vector sequence), MOVBE, ADX, AVX, AVX2. RDRAND
uses the resident's host-entropy path, not FEAT_RNG. Natemu-implemented system features (FSGSBASE, SMEP, UMIP, x2APIC,
RDTSCP, CLWB, CLFLUSHOPT, XSAVE family) need none. The resident's split-lock handling also assumes LSE2's alignment
behaviour without checking it. LSE2 is in the floor; chunk 2 adds it to the probe's required list.

# CPU features and profiles: one source for what the guest sees and what the host offers

Status: draft for independent review (November 15, 2026), branch `cpu-features` off `main` at 333b7b6. No code yet.
Owner direction (November 15): build a centralized CPU feature/profile mechanism before deciding which new guest
features to implement; the M2 (no FEAT_AFP) floating-point work comes after this plan.

**Goal:** every CPU feature decision, guest-visible (x86 CPUID, MSRs, control-register bits, instruction forms) or
host-side (Arm features the generated code, the resident and the admission checks rely on), is made in one place, is
checked for consistency by tools, and reaches every consumer from that place. Turning a guest feature on or off becomes
an edit to one catalogue entry plus the feature's own implementation, never a hunt through hand-written tables.

**Non-goals:** implementing any new guest feature (RDPID, VAES, the deadline timer, ...); changing the default guest
CPU; the M2 floating-point differences; selecting features that change register-state layout (AVX, XSAVE components,
AVX-512); per-vCPU differing CPUs (every vCPU presents the same CPU, as on a real machine); AMD identity.

## 1. Where feature decisions live today

The guest side already has a source: `tools/models/cpu_profile.json` (the accepted Intel profile of the decision record,
every value with its source), judged by `tools/models/cpu_profile.py` and generated into `crates/abi/src/cpu_profile.rs`
(CPUID table, MSR table, CR0/CR4/EFER/XCR0 valid masks). The resident's CPUID and MSR transactions and the host's SIMD
decode table (`crates/host/src/system.rs`, `advertised()`) follow it, and tests tie them to it.

Everything else restates the profile by hand, with nothing checking agreement:

| Fact | Where it is stated | Tie |
| --- | --- | --- |
| Which x86 instruction forms the code generator accepts | `tools/fexc/shim/fexc_slice1.inc`: hand-written rows ("VAES's L1 not advertised", AVX-VNNI, GFNI, RTM, CET, WAITPKG, SSE4a, 3DNow refused) | none: fexc takes no profile input; `test_slice1.py` only checks the JSON still says "advertised" |
| System-instruction forms that are #UD because a feature is clear | `system.rs`: RDSEED, RDPID, XSAVES/XRSTORS, INVPCID, group-7 register forms, WRSS... unconditional #UD | comments only |
| FEX's guest-facing switches | `fexc.cpp` FeatureTable: `x86_avx`, `x86_aes256`, `x86_3dnow` on, `x86_sse4a` off. FEX's decoder drops its whole VEX table without `x86_avx` (BMI included) | none |
| Arm features the generated code may use | `fexc.cpp` FeatureTable (32 entries, defaults "Apple M4 view" plus pins kept from the old producer: lrcpc, lrcpc2, rpres, wfxt off) and `crates/host/src/admission.rs` FEATURES (the same 32 names in the same order, with ID-register fields or "unverifiable") | only the count (32) and a positional FlagM index |
| Hard host requirements | FlagM in four places (vm.rs vCPU creation, admission index, admission table, fexc refusal); LSE in four (resident `+lse` build flag, fexc default, admission accepting only LSE atomics, resident completion code); Apple's TSO bit written in vm.rs | none |
| Host features read by name | `host_feature("FEAT_CSV3")`, `host_feature("FEAT_SB")` (sysctl) | none |
| Floating-point assumptions | `fpcr()` writes AH and NEP whatever the host has; the flag mapping assumes FEAT_AFP | none (see the AFP discussion of November 14) |
| Encodings admission accepts | `admission/structure.rs`: LSE-only atomics, the no-CSSC POPCNT sequence, CNTVCT vs CNTVCTSS by ECV | an Arm feature flipped on (CSSC, a non-LSE host) would emit code admission refuses |
| Reference CPUs | about ten generators and runners each hard-code a QEMU `-cpu` string (`qemu64,+bmi1,...`, `Haswell,-pcid,...`, `max,-pcid,-smap`) with comments claiming parity | none |
| Compile cache | key = fexc binary hash + request arguments (features the vCPU lacks, as `name=0`; gating flags) | the guest profile is not in the key; it reaches cached code only through the shim's hand-written rows, i.e. the fexc binary hash |

There is one guest profile and no way to select another; `--feature` exists only on the fexc command line.

## 2. Design

Three records, each with one owner file, and generators that carry them to every consumer.

### 2.1 Guest feature catalogue (x86)

The catalogue extends `cpu_profile.json`; it does not start a second file. The JSON's per-bit `fields` entries already
name every CPUID bit and its state. A feature gains the following fields:

- `id`: a stable short name (`VAES`, `RDPID`, `TSC_DEADLINE`).
- `bits`: the CPUID bits it sets. Usually one; VAES and VPCLMULQDQ are one each.
- `requires`: x86 features it depends on (VAES requires AVX and AES; AVX-VNNI requires AVX). The source is the SDM's
  enumeration text.
- `parts`: what else changes with it.
  - MSRs that exist only with it. The existing hand-written `FEATURE_MSRS` and `ABSENT_WHEN_CLEAR` maps move here.
  - CR4 or XCR0 bits it validates.
  - The CPUID subleaves it needs (AVX-VNNI needs leaf 7 subleaf 1, which raises leaf 7's maximum subleaf).
  - The instruction-form groups it gates (§2.4).
- `host`: Arm features its implementation needs, split into required (refuse to advertise without them) and
  exactness (advertise, but report a known difference).
- `status`:
  - `built`: implemented and tested.
  - `not-built`: never advertised.
  - `fixed`: always on or always off, because it changes register-state layout or the machine's identity.
- `tests`: the corpora or runners that cover it (required for `built`).
- `qemu`: QEMU's flag name, when QEMU has one.
- `source`: the SDM section and the decision that set its state.

A **profile** is a name plus a set of enabled features. `baseline` is today's CPU, exactly. Any other profile, and any
per-launch adjustment, is `baseline` plus or minus features. The generator computes CPUID values from the enabled set
over the baseline's non-feature fields (identity, cache and topology leaves, the dynamic rules). Validation (in
`cpu_profile.py`, run by the generator and the tests) refuses:

- a feature whose `requires` is not enabled;
- a feature that is `not-built`;
- a `fixed` feature that was flipped;
- a `built` feature without tests;
- a CPUID bit that no catalogue entry or explicit "clear, no feature" entry covers.

**Host-conditional bits stay as they are, but declared.** ARCH_CAPABILITIES' RDCL_NO, set when the speculation barriers
are on and the host has CSV3, becomes a catalogue condition instead of a special case in `cpu.rs` and `execution.rs`.

### 2.2 Host capability record (Arm)

A single file, `tools/models/host_features.json`, replaces the two hand-kept 32-entry tables. Each Arm feature has:

- its fexc/FEX field name;
- how it is detected: an ID-register field, a sysctl name, or "not detectable";
- a **policy**: one of
  - `use when present`;
  - `off` (with its reason, e.g. "kept off to match the reference producer" for rpres, wfxt and lrcpc);
  - `required` (FlagM, LSE);
- the admission encoding classes that depend on it (LSE-only atomics, the POPCNT sequence, CNTVCT/CNTVCTSS).

The generator writes a C++ header that `fexc.cpp` includes (the table, without defaults) and a Rust table for
`crates/host`. The test of the header and the Rust table replaces the count check.

At startup a single probe builds the **host record**:

- Read the vCPU's ID registers through the hypervisor, as admission does today.
- Read sysctl for features those registers cannot show (CSV3, SB, and the ID_AA64ISAR2 class: RPRES, WFxT, CSSC,
  MOPS).
- Check the hard requirements in one place, with one error listing every missing item (FlagM, LSE, the TSO bit, the
  interrupt-controller API).
- Compute the code-generation feature set as policy ∩ present. The host passes it to fexc explicitly as the full set,
  so fexc has no defaults left to drift.

The host record also carries the exactness facts the guest catalogue asks about (AFP present or not), so the M2 work
has one place to read them. It is printed by a new `natemu --cpu-report` together with the selected guest profile.

Open point, to verify in chunk 2 rather than assume: whether the hypervisor's vCPU shows the same features as sysctl
reports for the host. For example, the M4's sysctl lists SME, and we do not know whether the vCPU presents it. The probe
prefers the vCPU's view and, for registers the host cannot read through the hypervisor, can have the resident read them
at EL1 during boot (`mrs ID_AA64ISAR2_EL1`, DCZID_EL0). Chunk 2 compares the two views on the M4 and records any
difference.

### 2.3 The effective CPU at launch

`--cpu <profile>[,+FEATURE|,-FEATURE...]` (and `NATEMU_CPU`) selects the guest CPU. Without it the guest gets
`baseline`. The host validates the request against the catalogue (§2.1) and the host record:

- a feature whose required Arm features are absent is refused, with the reason;
- an exactness difference is printed by `--cpu-report` and at startup.

The host then builds the **effective CPU tables**: CPUID entries, MSR table, CR0/CR4/EFER/XCR0 valid masks and the
guest feature mask. It writes them to a read-only boot page that the resident reads. Today these tables are compiled
into the resident from the single generated profile.

The tables are small (27 CPUID entries, 50 MSRs). CPUID and MSR transactions are not hot, so reading from a page costs
nothing measurable. A new fixed-layout record goes into `crates/abi` with layout tests, like the other boot records.
`cpu_profile.rs` stays generated and becomes the `baseline` instance, used by tests and by the host as the default.

The CPU is fixed for the VM's lifetime: no CPUID value changes during a run. Natemu has no live migration or saved VM
state today; when it gets one, that state records the effective CPU.

### 2.4 Consumers driven by the effective CPU

- **Code generator (fexc).** A new argument passes the guest feature mask to fexc. The shim's hand-written verdict
  rows become rows gated on generated feature constants (`GF_VAES`, `GF_AVX_VNNI`, ...). A form whose feature is clear
  is #UD, as today. A form whose feature is set is decoded through FEX's existing lowering. FEX's guest-facing switches
  (`x86_avx`, `x86_aes256`, `x86_3dnow`, `x86_sse4a`) are derived from the mask instead of defaults. Each shim row names
  its feature, and a test checks every named feature exists in the catalogue.
- **Host system-instruction decode.** Every unconditional "#UD because not advertised" in `system.rs` becomes
  `advertised(feature)` against the effective CPU. A feature that is advertised but has no resident transaction cannot
  occur, because `not-built` features are refused at launch. This turns those comments into checks without changing
  behaviour.
- **Compile cache.** The guest feature mask and the full code-generation feature set are added to the request
  arguments, hence to both the in-memory and on-disk keys. The persistent cache is invalidated once.
- **Reference tooling.** `cpu_profile.py` gains `qemu_cpu(features, base)`. Each generator and runner declares the
  features its corpus needs instead of a free-text `-cpu` string. A check refuses a corpus that needs a feature the
  profile does not advertise, or a QEMU model missing one. Where QEMU lacks a feature, the catalogue says so, and the
  reference must come from real x86 (the tiebreaker rule).

## 3. Chunks

Each chunk keeps the default launch byte-for-byte as it is: same CPUID and MSR answers, and every fexc reply identical
on the recorded request streams (installer and `build` boot) and the standing corpora. The full suite runs once, before
merge. Targeted gates run between chunks.

| Chunk | Content | Gate |
| --- | --- | --- |
| 1. Catalogue | Feature entries in `cpu_profile.json` for every CPUID bit; `requires`, `parts`, `status`, `tests`, `qemu`; the validator; the profile mechanism with `baseline` only; generated feature IDs for Rust and C++ | `baseline` regenerates today's `cpu_profile.rs` exactly; validator negative controls (a missing requirement, a `not-built` feature, an uncovered bit) each refused |
| 2. Host record | `host_features.json`; generated fexc header and Rust table; one probe; requirements checked once; FlagM/LSE duplicates removed; `--cpu-report`; vCPU-versus-sysctl comparison on the M4 | fexc replies byte-identical with the full feature set passed explicitly; startup refusal tested by simulating a missing FlagM or LSE |
| 3. Effective CPU in the resident | Boot-page record; the resident's CPUID, MSR, CR4 and XSETBV checks read it; `--cpu` parsing and validation | CPUID/MSR judge vectors identical for `baseline`; a test profile (`baseline,-SHA`) answers per the judge |
| 4. Code generator and decode | Guest mask to fexc; gated shim rows; derived FEX guest switches; `system.rs` checks; mask in the cache key | default replies byte-identical; under `baseline,-SHA,-ADX` the SHA and ADX forms are #UD in fexc and the host decode alike (the old hand cut, now derived); a shim-row/catalogue cross-check test |
| 5. Reference tooling | `qemu_cpu()`; generators and runners declare features; corpus/profile check | every existing corpus reproduces with the generated `-cpu` string (identical expected outputs) |
| 6. Records | Contracts and README pages; the catalogue's adding-a-feature checklist; INV-03-01 row | review |

Size: chunks 1, 3 and 4 are the substance. Chunk 2 is mostly moving tables. Chunk 5 touches many scripts but each
change is small. Chunk 4 depends on 1. Chunk 3 depends on 1. Chunks 2 and 1 are independent.

**Adding a feature afterwards** (what this plan buys) is a fixed list:

1. Write its catalogue entry.
2. Implement its resident or host parts, if any.
3. Gate its shim rows on its constant.
4. Add its tests.
5. Set its status to `built`.

Validation refuses the entry until each step is done. The cheap candidates of November 14 (VAES/VPCLMULQDQ, AVX-VNNI,
ERMS/FSRM, RPRES) then need steps 1, 3 and 4 only.

## 4. Review focus

- Does the catalogue capture every place a guest feature acts? In particular CPUID subleaves and maximum-leaf values,
  XSAVE-dependent leaf 0xd values, CR4/XCR0 validity, MSR existence, and #UD versus refusal for forms of clear
  features.
- Is the byte-identical gate for chunks 2 and 4 sufficient, given that passing the full feature set changes the request
  arguments (and so the cache key) but must not change code?
- Boot-page tables versus compiled-in profiles: correctness of the resident trusting a host-written page (it is host
  data, not guest data; the page is mapped read-only to the resident and outside guest RAM).
- Is "vCPU view first, resident EL1 read for registers the host cannot read" sound, and what if the hypervisor hides a
  feature the host has?
- Are `fixed` features (AVX, XSAVE components) correctly excluded from selection, and is anything else layout-changing?
- The reference tooling: is declaring per-corpus features enough to stop the parity comments drifting again?

## 5. Owner decisions

1. **Catalogue location:** extend `cpu_profile.json` (recommended: it is already the accepted, sourced, judged profile)
   or a separate catalogue file.
2. **Selection surface:** `--cpu <profile>` with `+FEATURE/-FEATURE` adjustments (recommended), or named profiles
   only.
3. **Resident tables:** host-built boot page (recommended: any profile, one code path) or every profile compiled into
   the resident.
4. **Policy pins kept from the old producer** (lrcpc, lrcpc2, rpres, wfxt off): keep as `off` with that reason now, and
   decide each one when a feature asks for it (recommended), or revisit now.

## 6. Risks

- **The shim's gated rows change instruction lengths or stop shapes for clear features.** The byte-identical gate on
  recorded streams and corpora catches it.
- **The hypervisor's vCPU view and sysctl disagree.** Chunk 2 measures this before anything relies on it.
- **QEMU cannot express a feature.** The catalogue's `qemu` field is empty and the real-x86 rule applies, as today.
- **Scope creep into implementing features.** Every chunk's gate is "default unchanged". New features come afterwards,
  one decision at a time.

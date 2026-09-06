# BP-01-11-start — licensing breadth pass

> Historical question-opening coverage. Findings and current work belong to
> [INV-01-11](../investigations/INV-01-11.md); this pass is not a live status
> record and new work need not update it.


## Metadata

- Breadth-pass ID: `BP-01-11-start`
- Question: `Q-01-11`
- Date: 2026-09-03
- Executor/session: Claude Code authoring subagent `t01_platform_author`

This pass organizes questions and establishes no evidence or conclusion.

## Coverage

- **Product assumptions:** `Q-01-11` asks whether every contemplated
  dependency and source of study is license-compatible with the project's
  intended distribution — but `INV-00-05` ("Method, controls, falsifiers, and
  limitations") explicitly keeps the distribution form itself, along with
  signing/notarization and entitlement feasibility, as mandatory `Q-01-01`/
  `Q-01-11`/`Q-06-03` research, not a settled input. This pass therefore
  assumes no single distribution model; every license-compatibility question
  below must be asked conditionally against at least two candidate models —
  permissive open-source binary distribution, and proprietary (closed-source)
  binary distribution — since the models can flip an otherwise-identical
  dependency's compatibility verdict (a GPL-2.0 dependency linked into the
  binary is a different question under each model).
- **Adjacent systems:** the project's mandatory comparator, QEMU TCG, is
  GPL-2.0-only upstream; whether NatEmu's own experiment code or eventual
  implementation studies QEMU's algorithms, links QEMU code, or merely
  exercises QEMU as an external baseline binary is itself a use-mode question
  this pass raises but does not answer — "study only" (reading source to
  understand behavior), "link" (compiling against or embedding a library), and
  "redistribute" (shipping the dependency's code or a derivative in NatEmu's
  own binary) are three different obligations under most of the licenses
  below, and each contemplated dependency needs its use mode stated before its
  license can be judged.
- **Alternatives:** where a copyleft dependency proves incompatible with a
  candidate distribution model, a permissively-licensed substitute may exist
  (for example an MIT-licensed disassembler in place of a GPL one) — this pass
  records that substitution is a live alternative for `Q-01-11`'s future
  investigation to weigh, not a decision made here.
- **Negative and boundary cases:** the following sources of study and
  candidate dependencies are enumerated as licenses to be audited, not
  conclusions about their compatibility:
  - **QEMU** — GPL-2.0 (the decision baseline itself; study-only versus
    linkage/redistribution use modes matter here more than for any other
    entry, since QEMU is mandatory to the research regardless of distribution
    model).
  - **Linux kernel** — GPL-2.0, with the kernel's own documented syscall/UAPI
    exception for userspace programs that merely invoke the kernel interface;
    the audit must distinguish "the kernel as a guest artifact studied and
    booted" from "kernel source or headers incorporated into NatEmu."
  - **Intel SDM** and **AMD APM** — proprietary documentation terms
    (copyright and, potentially, use/redistribution restrictions on the
    specification text itself, separate from any patent question about
    implementing the architecture they describe).
  - **Arm Architecture Reference Manual (ARM)** — proprietary Arm documentation
    terms, parallel to the Intel/AMD case.
  - **OASIS Virtio** specification — OASIS's standard IP/patent-policy terms
    (reciprocal patent non-assert or RAND-style commitments are typical of
    OASIS standards and must be checked, not assumed absent).
  - **ACPI** specification and **UEFI Forum** materials — the ACPI/UEFI Forum's
    own specification-license and patent-policy terms.
  - **LLVM/Clang** — Apache-2.0 "with LLVM exceptions"; the exception clause
    itself (not just the base Apache-2.0 grant) needs its own audit line since
    it changes copyleft/attribution obligations relative to plain Apache-2.0.
  - **Instruction-decoder libraries** — Capstone, Zydis, and Intel XED are
    named as candidate x86 decoders to study or depend on; each carries its
    own license (to be confirmed at audit time, not assumed here) and must be
    audited individually rather than as a group.
  - **SoftFloat** — a candidate floating-point emulation source; different
    published SoftFloat versions/forks carry different license terms, so the
    audit must pin an exact version/fork, not the project name alone.
  - **libslirp** and **lwIP** — candidate user-mode network-stack sources for
    a headless network device backing; each has its own license to confirm.
  - **Rust crates** — `rustc 1.97.0` is present in this session's toolchain
    (per the brief's host-facts table), making a Rust implementation a live
    candidate; a Rust dependency tree carries transitive crate licenses that
    need an inventory method (not a per-crate manual check) before any
    compatibility claim, since crate counts in a real dependency tree are
    typically far larger than the top-level crates chosen deliberately.

  None of the above is asserted compatible or incompatible with either
  candidate distribution model by this pass; each is a line item the future
  investigation must resolve with a captured license text at a pinned
  version/commit.
- **Host and guest matrix:** license compatibility does not vary by Apple
  Silicon generation or macOS release, but Apple's own Developer Program
  license terms governing code-signing and the restricted
  `com.apple.security.hypervisor` entitlement (used by `INV-01-01`) are a
  distinct legal-terms question this pass flags as adjacent to, but not
  identical with, open-source dependency licensing — `Q-01-01` and `Q-01-11`
  jointly own distribution/signing/entitlement research per `INV-00-05`, and
  this pass does not duplicate `Q-01-01`'s coverage of Apple's own terms.
- **Multicore interactions:** not applicable — license terms do not vary by
  vCPU topology, core placement, or SMP configuration.
- **Falsifiers and stop paths:** this question tests `KA-10` ("No contemplated
  dependency or source of study is license-incompatible with the intended
  distribution"), evaluated by `INV-01-11`; if false for a given dependency
  under a given candidate distribution model, `INV-00-06`'s family-kill item 4
  ("dependency or license terms are incompatible with the accepted use,
  linkage, or distribution model") applies to any family that uniquely
  depends on that dependency under that model. Since the distribution model is
  itself undecided, a dependency found incompatible with only one candidate
  model is not yet a kill under either model alone; `INV-01-11` must resolve
  per-model, not assume a single outcome, and must record which
  dependency/model combinations would constitute a family kill so that
  outcome is legible once `Q-01-01`/`Q-06-03` narrow the distribution model.
- **Measurement failure modes:** treating "study only" as license-free when
  an eventual implementation in fact copies structure or algorithms from a
  copyleft source (a clean-room requirement question, not resolved by reading
  a license text alone); treating Intel SDM/AMD APM/Arm ARM as unrestricted
  because they describe a public instruction set, when the specification text
  itself typically carries its own copyright and use terms independent of any
  patent question; under-auditing a Rust dependency tree by checking only the
  top-level crates a developer chose rather than the full transitive set; and
  citing a license from memory or a secondary summary (a package registry
  badge, a Wikipedia table) rather than the primary license text at a pinned
  version, which the evidence policy does not accept as source-backed.

## Backlog updates

The distribution-model dependency is already routed to `Q-01-01`/`Q-06-03` by
`INV-00-05`; the family-kill disposition is already owned by `INV-00-06`
(family-kill item 4) and `Q-01-02`'s family register; and Apple's own
Developer Program/entitlement terms are already within `Q-01-01`'s scope. Every
enumerated dependency and source of study above is a line item for `Q-01-11`'s
own future investigation, not a new question. No new question is required.

`no new issues found`

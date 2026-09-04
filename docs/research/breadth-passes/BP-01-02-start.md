# BP-01-02-start — architecture-families breadth pass

## Metadata

- Breadth-pass ID: `BP-01-02-start`
- Question: `Q-01-02`
- Date: 2026-09-03
- Executor/session: Claude Code authoring subagent `t01_platform_author`

This pass organizes questions and establishes no evidence or conclusion. It
covers the six candidate architecture families named by the Track 01 brief and
the whole kill-assumption register as things to be questioned; it screens
nothing and selects no family. `Q-01-02`'s investigation (`INV-01-02`) is
written later by another agent and inherits this pass unchanged.

## Coverage

- **Product assumptions:** family screening must happen before any family is
  preferred or compared (`Q-01-02`'s decision relevance: "Research must screen
  families before comparing or preferring one"). The six families are: `F1`
  interpretation with software MMU; `F2` dynamic binary translation with
  software MMU; `F3` dynamic translation whose translated arm64 code runs at
  EL1 inside a Hypervisor.framework VM so the hardware MMU implements guest
  paging; `F4` ahead-of-time/static translation with dynamic fallback; `F5`
  hardware x86 virtualization (expected killed by `KA-02`, since Apple Silicon
  has no x86 VMX/SVM host mode); `F6` user-mode-only emulation, which is
  outside the full-system envelope and is recorded, not screened. No family is
  assumed admissible or inadmissible by this pass.
- **Adjacent systems:** QEMU TCG (the decision baseline) mixes single-thread
  interpretation and dynamic binary translation with a software MMU, resembling
  `F1`/`F2`; QEMU's HVF accelerator uses Hypervisor.framework for same-ISA
  (arm64-on-arm64) guests only, not cross-ISA x86-on-arm64, so it is a partial
  precedent for `F3`'s VM-entry mechanism but not for `F3`'s translated-code
  requirement; Apple's Rosetta 2 is an AOT-plus-JIT x86-64-to-arm64 user-mode
  translator, a partial precedent for `F4`'s ahead-of-time-plus-fallback shape
  but not a full-system analog; and Apple's Virtualization.framework is an
  adjacent system whose guest-architecture support this pass **questions**
  rather than settles: whether it can host an x86-64 Linux guest on Apple
  Silicon at all, and therefore whether it is a seventh family or no family, is
  for `Q-01-02`'s investigation to establish from primary sources. This pass
  takes no position on it. (Corrected at the `REV-114` S5 return: the bullet
  previously asserted as fact that the framework "cannot itself execute an
  x86-64 guest and is not a seventh family" — an unsourced documentary
  conclusion in a pass whose own preamble establishes none, and one that
  pre-empted the screen it was supposed to leave open. No claim depended on it:
  `INV-01-02` derives the exclusion from its own captures, `SRC-151`/`SRC-152`,
  rather than inheriting this pass's assertion, so this is a wording correction
  and not a re-screen.)
- **Alternatives:** the families are not mutually exclusive in the way this
  register might imply — a design could combine `F2`/`F4` translation with
  `F3`'s hardware-MMU EL1 execution mode; screening at the family level records
  admissibility of each named boundary, not a forced single choice, and
  `Q-03-01` (not this question) compares surviving families on Track 03
  dimensions.
- **Negative and boundary cases:** `F5` is expected killed by `KA-02`
  (Apple Silicon exposes no host x86 virtualization extension), but "expected"
  is not "confirmed" before `INV-01-01`'s controlled falsifier check runs;
  `F6` is out of the full-system envelope per Track 00 and must be recorded as
  such rather than silently screened in or out. A family that requires a
  private API, an unavailable entitlement, an unsupported toolchain behavior,
  or a prohibited distribution condition for a mandatory host/OS cell is a
  candidate kill only under `INV-00-06`'s family-kill item 1, and only after an
  eligible reviewed finding maps the exact applicability — this pass does not
  make that finding.
- **Host and guest matrix:** family admissibility can differ by Apple Silicon
  generation, not just by macOS release: `F3`'s hardware-MMU-EL1 mechanism
  depends on `ID_AA64MMFR0_EL1` translation-granule and PARange fields that
  `INV-01-01`'s falsifier check must read per generation, so a `P2` (M4) result
  does not establish `P1` (M1 Pro) admissibility for `F3` without replication,
  per the executor guide's incremental-replication rule.
- **Multicore interactions:** the track's required prototype needs translation,
  paging, exceptions, and interrupts composed with at least two concurrently
  executing vCPUs (`KA-11`, tested by `INV-01-12`); `F2`/`F3`/`F4` must each
  preserve x86-TSO ordering semantics on the Arm memory model while at least
  two vCPUs run (`KA-06`, tested by `INV-01-08`/`INV-01-12`); `F1`
  (interpretation) has a simpler multicore admissibility question but its
  scaling behavior is a Track 04 performance question, not a Track 01
  admissibility one.
- **Falsifiers and stop paths:** the whole kill-assumption register is the set
  of things to be questioned by this family screen, not yet resolved by it:
  `KA-01` (`MAP_JIT`/`pthread_jit_write_protect_np` availability) — kills
  translation families if false; `KA-02` (Hypervisor.framework x86-64
  execution) — kills only `F5`; `KA-03` (≥8 GiB host-process guest-RAM
  backing) — a project-stop input if false for every family; `KA-04` (precise,
  resumable fault delivery in generated code) — kills families relying on lazy
  faults unless `INV-01-12` shows explicit-check composition; `KA-05` (bounded
  timer/interrupt-latency host facilities) — a project-stop input; `KA-06`
  (x86-TSO on the Arm memory model with ≥2 concurrent vCPUs) — a project-stop
  input if false for the SMP boundary; `KA-07` (guest memory-modification
  tracking at bounded cost) — kills mechanisms needing finer hardware tracking
  than available; `KA-08` (unprivileged public device backings) — a
  project-stop input per failing device class; `KA-09` (sustainable public
  toolchain) — a family kill under `INV-00-06` item 1; `KA-10` (no
  license-incompatible dependency) — a family kill under item 4; `KA-11`
  (translation/paging/exceptions/interrupts/≥2-vCPU composition) — a
  project-stop input if no family composes; `KA-12` (guest machine contract
  satisfiable by stock Linux drivers without firmware) — a project-stop input;
  `KA-13` (enforceable per-instance containment budgets) — a project-stop
  input. `INV-01-02` must maintain this register and cite the falsifier(s)
  each future finding tests; this pass only inventories it.

  This pass also carries forward the `BP-00-end` implication assigned to
  `Q-01-02`: a dependency on an installed privileged helper is treated as a
  reopenable scope boundary in `INV-00-05` ("Claim inventory and resolution")
  and in `INV-00-08` ("Falsifiers, controls, and limitations"), but the same
  fact pattern reads as a family kill under `INV-00-06`'s family-kill item 1
  ("prohibited distribution condition"). Nothing in Track 00 decides which
  disposition applies first when a family is found to uniquely require a
  privileged helper. `Q-01-02`'s later investigation must resolve this
  ordering — or explicitly state it remains open — before it can rely on
  either disposition to kill or spare a family; this pass records the
  unresolved implication and does not adjudicate it.
- **Measurement failure modes:** treating "expected killed by `KA-02`" as
  already established before `INV-01-01` runs would overstate confidence;
  screening a family as admissible from documentation analysis alone, without
  the family's controlled falsifier check(s), is insufficient under the
  track's method ("primary-source feasibility analysis and controlled
  falsifier checks"); and declaring the family register closed before a
  breadth-search closure rationale is published (required by `INV-00-06`'s
  dependency table) would let a project-wide "no family survives" conclusion
  rest on an incomplete enumeration.

## Backlog updates

Every issue this pass surfaced maps to an existing backlog question: `F5`/
`KA-02` admissibility to `Q-01-01` and `Q-01-02` itself; translation/paging/
exception/timer/device mechanism questions to `Q-01-04`–`Q-01-07`, `Q-01-09`;
SMP composition to `Q-01-08` and `Q-01-12`; toolchain and licensing family
kills to `Q-01-10` and `Q-01-11`; the machine contract and containment budgets
to `Q-01-13` and `Q-01-14`; family comparison to `Q-03-01`; and the
privileged-helper disposition-order conflict to the existing `BP-00-end`
routing already owned by `Q-01-02` (and `Q-00-05`). No existing mapping is
displaced and no new question is required: `no new issues found`.

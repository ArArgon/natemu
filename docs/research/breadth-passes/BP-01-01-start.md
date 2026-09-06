# BP-01-01-start — platform-facts breadth pass

> Historical question-opening coverage. Findings and current work belong to
> [INV-01-01](../investigations/INV-01-01.md); this pass is not a live status
> record and new work need not update it.


## Metadata

- Breadth-pass ID: `BP-01-01-start`
- Question: `Q-01-01`
- Date: 2026-09-03
- Executor/session: Claude Code authoring subagent `t01_platform_author`

This pass organizes questions and establishes no evidence or conclusion. It is
the first Track 01 breadth pass; Track 01's remaining areas had no backlog
question yet when it ran.

## Coverage

- **Product assumptions:** "platform facts" means what public macOS APIs,
  entitlements, and code-signing configurations actually permit an unprivileged
  emulator process to do — established from primary sources and controlled
  observation, never folklore or the preservation checkpoint (`AGENTS.md`;
  executor guide). It assumes no architecture family yet; `Q-01-02` screens
  families only after platform facts are accepted. A fact observed on one host
  (this session's `Mac16,10`/macOS 26.6.2/`25G83`, per the brief's host-facts
  table) is not yet a platform-wide fact.
- **Adjacent systems:** Apple's own JIT-using processes (JavaScriptCore,
  Rosetta 2 as an AOT+JIT translator precedent) and QEMU's existing Hypervisor
  Accelerator Framework (HVF) backend are prior public users of `MAP_JIT` and
  Hypervisor.framework respectively and bound what "public API" usage looks
  like without asserting NatEmu will resemble either. Apple's
  Virtualization.framework is a higher-level, guest-OS-managed API distinct
  from the raw Hypervisor.framework surface this track needs; the two must not
  be conflated when auditing admissibility.
- **Alternatives:** if `MAP_JIT`/`pthread_jit_write_protect_np` (`KA-01`) or
  Hypervisor.framework x86-64 execution (`KA-02`) prove inadmissible on some
  cell, interpretation-only families (`F1`) remain a fallback whose own
  platform-fact needs (ordinary `mmap`/`mprotect`, no executable-generated-code
  requirement) are a distinct, smaller admissibility question than translation
  families need.
- **Negative and boundary cases:** private API use, an entitlement available
  only to Apple-approved App Store distribution, App Sandbox restrictions that
  would apply only under one distribution form, Gatekeeper/notarization
  requirements for non-ad-hoc signing, and revocation or macOS-version removal
  of a currently available API are all boundary conditions this question must
  characterize rather than assume stable. `INV-00-05` already requires Track 01
  to analyze distribution form, signing/notarization, and entitlements as
  mandatory (not deferred) research; this pass keeps that scope live for
  `Q-01-01`.
- **Host and guest matrix:** platform facts must be checked against both named
  admissible pairs from `INV-00-02` — `P1` (MacBook Pro 14-inch 2021, M1 Pro,
  macOS Sequoia 15) and `P2` (Mac mini 2024, M4, macOS Tahoe 26) — and this
  session's host is `P2`'s generation/release line but not its exact model, so
  any Observed claim from this session states that applicability limit and
  leaves `P1` replication owed, per the brief's host-facts section and the
  executor guide's incremental-replication rule.
- **Multicore interactions:** entitlement checks, JIT write-protect toggling,
  and Hypervisor.framework vCPU creation must be checked for behavior that
  could vary by requesting thread's QoS class or P-core/E-core placement, since
  the track's required prototype needs at least two concurrently executing
  vCPUs (`INV-01-12`) and thread affinity/QoS admissibility is itself part of
  this question's scope (per the Phase B method's thread QoS/affinity probes).
- **Falsifiers and stop paths:** this question is the falsifier check for
  `KA-02` (Hypervisor.framework x86-64 execution on Apple Silicon) and a partial
  input to `KA-01` (`MAP_JIT` + `pthread_jit_write_protect_np`, jointly tested
  with `KA-04`'s translation investigation `INV-01-04`); it also underlies
  `KA-08` (device backings), `KA-09` (toolchain), and every family's admissible-API
  premise in `INV-00-06`'s family-kill item 1 ("private API, unavailable
  entitlement, unsupported toolchain behavior, or prohibited distribution
  condition"). A negative finding on any of these does not by itself kill a
  family; `INV-00-06` requires the finding to be mapped to exact
  host/OS-cell applicability before it counts.
- **Measurement failure modes:** a capability probe that only succeeds under
  ad-hoc signing cannot be generalized to a notarized, Developer-ID-signed
  distribution without a separate check; SDK-version skew between build-time
  headers and runtime OS behavior (four SDKs coexist under this session's
  Command Line Tools: `MacOSX26.4.sdk`, `MacOSX26.sdk`, `MacOSX15.4.sdk`,
  `MacOSX15.sdk`) can mask a real capability difference; and a single-host,
  single-OS observation must not be reported as matrix-wide without the `P1`
  replication the executor guide requires.

## Backlog updates

This pass, run over `Q-01-01` when Track 01's remaining areas had no backlog
question yet, created the following seven Track 01 questions (already
recorded in `docs/research/question-backlog.md`; not edited here):

- `Q-01-03` (address spaces) — representing guest physical/virtual address
  space within a host process, and its limits, is a distinct admissibility
  question from whether generated code can be made executable at all, and
  needed its own resolution method (reservation/aliasing/page-size probes).
- `Q-01-04` (translation and executable memory) — auditing which public APIs
  are admissible surfaced `MAP_JIT`/`pthread_jit_write_protect_np` and its
  write/execute transition cost as a specific, falsifiable sub-question
  (`KA-01`) that general platform-fact review does not resolve on its own.
- `Q-01-05` (paging and invalidation) — once address-space representation and
  executable-memory admissibility are separately scoped, honoring guest
  page-table semantics and tracking guest memory modification (including
  self-modifying code) needed its own mechanism-and-cost question (`KA-07`).
- `Q-01-06` (exceptions) — precise, restartable guest fault delivery (signals,
  Mach exceptions, or explicit checks) is a distinct admissible-mechanism
  question from translation and paging, with its own falsifier (`KA-04`).
- `Q-01-07` (timers and interrupts) — timekeeping and asynchronous-event
  facilities are a platform-facts sub-area whose resolution method is
  measurement (resolution/latency), not documentation review alone, and needed
  its own question (`KA-05`).
- `Q-01-09` (devices) — the minimal headless device set's per-device
  entitlement and privilege status could not be resolved as one fact; it
  needed a dedicated question stating admissibility per device class
  (`KA-08`), dependent on the not-yet-existing machine contract (`Q-01-13`).
- `Q-01-10` (toolchain) — build/test/debug/profiling tool existence and
  sustainability is a distinct question from runtime-API admissibility: it
  gates experiment-code eligibility for every other Track 01 question
  (`KA-09`) and needed its own resolution method (observed tool inventory).

This pass also mapped one surfaced concern — that platform facts must be
checked across the full accepted host/OS/guest/vCPU/placement coverage, not
just this session's single host — to the existing `Q-05-02` rather than
creating a new question; that mapping is already recorded in `Q-01-01`'s
backlog row.

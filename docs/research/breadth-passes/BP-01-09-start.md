# BP-01-09-start — devices breadth pass

> Historical question-opening coverage. Findings and current work belong to
> [INV-01-09](../investigations/INV-01-09.md); this pass is not a live status
> record and new work need not update it.


## Metadata

- Breadth-pass ID: `BP-01-09-start`
- Question: `Q-01-09`
- Date: 2026-09-03
- Executor/session: Claude Code authoring subagent `t01_machine_author`

This pass organizes questions and establishes no evidence or conclusion.

## Coverage

- **Product assumptions:** the minimal headless device set fixed by
  [Scope and success](../tracks/00-scope-and-success.md) — timer/APIC, serial,
  block, network, entropy, shutdown — is assumed complete; `Q-01-09` asks only
  whether each class has an unprivileged public host backing, not whether the
  set itself is sufficient (that is `Q-00-02`/`Q-01-13`'s scope) and not which
  backing a production design picks (`Q-01-13`/Track 03). Timer/APIC's host
  side is owned by `Q-01-07`; this question covers the remaining five classes
  plus the entitlement/privilege status of the host-clock mechanism `Q-01-07`
  selects. `KA-08` states the assumed backing per class: serial (pty/stdio),
  block (POSIX file I/O), network (BSD sockets or an available `vmnet` mode),
  entropy (`getentropy`/`SecRandomCopyBytes`), shutdown (process exit).
- **Adjacent systems and alternatives:** network has two candidate host
  backings with different privilege profiles — `vmnet.framework`, gated by the
  `com.apple.vm.networking` entitlement Apple documents as restricted, versus
  an ordinary user-space TCP/IP stack built over `BSD` `SOCK_STREAM`/`SOCK_DGRAM`
  sockets (no special entitlement, but no L2 bridging either) — and the pass
  must not assume `vmnet` is the only path before Phase B measures both.
  `SOCK_RAW` is a third network primitive expected to need root (`EPERM`
  unprivileged), relevant only as a negative control. Block storage has
  alternative backing-store shapes (plain sparse file via `ftruncate`, `dd`-style
  preallocated file, APFS clone via `clonefile`) with different growth and
  copy-on-write cost; `Q-01-14` picks the storage budget, `Q-01-09` only
  establishes that POSIX file I/O plus `F_NOCACHE`/`F_FULLFSYNC` is admissible
  without privilege. Serial has `pty`/`posix_openpt` versus a raw stdio pipe as
  alternative host backings; a virtio-console transport over either is a
  `Q-01-13` machine-contract choice, not a `Q-01-09` admissibility question.
  Entropy has three candidate host APIs (`getentropy`, `SecRandomCopyBytes`,
  `arc4random`) that must be compared for admissibility, per-call byte limits,
  and blocking behavior rather than assumed interchangeable.
- **Negative and boundary cases:** the entitlement-restriction boundary for
  `vmnet.framework` is not simply "works" or "fails" — Apple's restricted-
  entitlement mechanism is normally enforced by App Review/provisioning for
  distributed builds, and it is a live open question whether an ad-hoc-signed
  local binary carrying `com.apple.vm.networking` in its entitlements plist
  fails at `vmnet_start_interface` call time, fails at code-signing/launch
  time, or silently succeeds locally while only distribution enforcement
  differs; the brief instructs Phase B to record both the unentitled and the
  ad-hoc-signed-with-entitlement outcomes as distinct results, expecting the
  entitled ad-hoc case to fail because the entitlement is restricted, and
  that expectation itself must be checked rather than assumed. Other boundary
  cases: `getentropy`'s documented per-call maximum (256 bytes per the
  `getentropy(2)` man page) forces looping for larger seed requests; `pty`
  allocation exhaustion (`posix_openpt` returning `EAGAIN`/`ENXIO`) is a
  resource boundary, not an entitlement boundary, and belongs with `Q-01-14`
  descriptor budgets rather than here; `F_PUNCHHOLE`/sparse-file behavior on
  APFS must be checked for silent unavailability (feature absent) versus an
  explicit error, since a silent no-op would misrepresent block-device
  admissibility.
- **Host and guest matrix:** every device-backing claim from this session is
  Observed on `Mac16,10` (M4) / macOS 26.6.2 (`25G83`) / SDK 26.4 only; `P1`
  (M1 Pro / Sequoia 15) replication is owed before any platform-wide use, per
  the executor guide's incremental-replication rule and the brief's host-facts
  section. `vmnet.framework`'s documented availability and entitlement
  behavior may differ by macOS release; Source-backed claims from Apple
  documentation may state documented cross-release availability, but the
  Observed entitlement-failure mode is single-host until replicated. Guest-side
  driver consumption of whichever backing is chosen is `Q-01-13`'s and
  `Q-02-03`'s scope, not this question's.
- **Multicore interactions:** device-backend I/O (network reader thread,
  block I/O, serial I/O) runs on host threads distinct from vCPU threads;
  whether `vmnet.framework`, POSIX file I/O, and `pty` I/O are safe to drive
  concurrently from multiple host threads without additional locking, and
  whether their completion/readiness signaling (`kqueue`, `dispatch_source`,
  blocking read/write) delivers with the cross-thread latency `Q-01-07`
  measures for timer/APIC delivery, is in scope for the per-device
  entitlement/privilege inventory but the latency measurement itself remains
  `Q-01-07`'s. Two concurrently live guest instances (the `INV-00-08`
  containment oracle's peer scenario) sharing one host's network path is a
  `Q-01-14`/`Q-02-04` budget-and-containment question, not an admissibility
  question, but this pass notes that `vmnet.framework`'s shared-mode/host-mode
  interface allocation semantics under two simultaneous callers are unverified
  and should be recorded as a Phase B limitation if not tested.
- **Falsifiers and stop paths:** this question is the direct falsifier check
  for `KA-08`. Per `KA-08`'s row, a device class with no unprivileged public
  backing at all is a project stop input 3 for that failing class specifically
  — not a project-wide stop by itself, since the envelope requires the whole
  minimal set. Network is the only class with two independent candidate
  backings (`vmnet` and BSD sockets); it fails `KA-08` only if both are
  inadmissible unprivileged, not merely if `vmnet` alone requires root or a
  restricted entitlement. Block, serial, entropy, and shutdown are expected
  admissible under ordinary POSIX/Darwin syscalls with no entitlement; a
  finding that any of them requires an entitlement or root would be a
  falsifier worth an explicit claim, not an assumed-safe default. `KA-08`
  itself also gates `Q-01-13`, since a machine contract cannot require a
  device backing this question shows inadmissible.
- **Measurement failure modes:** an admissibility result observed only under
  ad-hoc signing does not generalize to a Developer-ID-signed or
  notarized/App-Store-distributed build without a separate check (per
  `BP-01-01-start`'s identical caution for `Q-01-01`); this applies directly
  to the `vmnet` entitlement result. A probe run as an interactively logged-in
  user may have different privilege than one run under CI/`sudo`, so the exact
  executing UID/privilege must be recorded for every unprivileged/`EPERM`
  claim (notably `SOCK_RAW`). A capability that "succeeds" only because the
  probe process happened to hold ambient privilege (for example, a
  developer's Terminal session with prior Full Disk Access or Local Network
  permission grants) would misrepresent baseline admissibility; the probe
  must record and disclose the host account's prior permission grants.

## Backlog updates

no new issues found

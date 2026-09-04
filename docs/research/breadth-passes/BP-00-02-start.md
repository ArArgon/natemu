# BP-00-02-start — compatibility-boundary breadth pass

## Metadata

- Breadth-pass ID: `BP-00-02-start`
- Question: `Q-00-02`
- Date: 2026-09-03
- Executor/session: Codex `/root`, goal thread

This pass organizes questions and establishes no evidence or conclusion.

## Coverage

- **Product:** distinguish “boots” from a supported guest contract covering
  kernel, userspace, CPU feature exposure, memory, devices, and failure mode.
- **Architecture and alternatives:** state guest-visible requirements without
  choosing how interpretation, translation, or host assistance provides them.
- **Correctness and boundaries:** include privileged state, paging, exceptions,
  atomics, timers, interrupts, direct boot, devices, unsupported-feature
  rejection, and self-modifying code; do not equate QEMU behavior with the ISA.
- **Host/guest matrix:** parameterize exact Apple generation, macOS/SDK, guest
  kernel/distribution, x86-64 level or CPUID profile, memory, page-table mode,
  device configuration, and 1/2/4/8+ vCPUs. SMP remains baseline.
- **Negative cases and falsifiers:** a guest outside the boundary must fail
  observably rather than corrupt silently; inability to boot and sustain the
  accepted SMP baseline is a candidate project kill for `Q-00-06`.
- **Measurement failures:** a performance result is inapplicable outside the
  exact accepted boundary and invalid when the exercised correctness status is
  unresolved or failing.
- **Adjacent concerns:** `Q-00-08` owns retained isolation/crash containment;
  `Q-00-09` owns analysis-only UEFI and Windows implications; `Q-02-01` owns
  the detailed verification surface.

## Backlog updates

no new issues found


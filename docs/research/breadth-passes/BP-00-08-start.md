# BP-00-08-start — retained-security breadth pass

## Metadata

- Breadth-pass ID: `BP-00-08-start`
- Question: `Q-00-08`
- Date: 2026-09-03
- Executor/session: Codex `/root`, goal thread

This pass organizes questions and establishes no evidence or conclusion.

## Coverage

- **Product:** define isolation and crash containment as observable research
  constraints while explicitly deferring a production threat model,
  hardening, hostile-guest assurance, and certification.
- **Architecture:** express the constraint without selecting sandbox, process,
  hypervisor, entitlement, or memory-protection mechanisms.
- **Correctness and negative cases:** guest failure, malformed device input,
  resource exhaustion, and translator faults must not be mistaken for guest
  correctness; define which host effects are unacceptable.
- **Host/guest/SMP:** account for concurrent vCPUs and helper components sharing
  a process or resources; applicability remains per host/OS and configuration.
- **Alternatives/falsifiers:** a family that necessarily violates the retained
  boundary may be killed; absence of a production security proof is not itself
  a research-phase kill.
- **Measurement failures:** isolation controls and process structure that affect
  performance must be present or their absence clearly limits applicability.

## Backlog updates

no new issues found


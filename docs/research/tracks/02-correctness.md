# Correctness — retired into Track 03

Track 02 was folded into [Architecture and implementation direction](03-architecture-options.md)
by the owner's September 19, 2026 workflow revision. Correctness is part of each
architectural decision and its interactions. This page preserves existing links;
there is no separate Track 02 workstream, completion requirement or gate.

The existing identifiers are retained as redirected obligations, not accepted
findings:

| Identifier | Current owner |
| --- | --- |
| `Q-02-01` — correctness surface and coverage | Track 00 retains the compatibility boundary; Track 03 establishes relevant behaviors and evidence for each design choice. The workflow governs measurement validity. |
| `Q-02-02` — SMP correctness | Track 03 memory, translation, interrupt and synchronization choices and their interactions. |
| `Q-02-03` — CPU/machine/clock contract | Track 03 CPU profile, virtual machine, boot and time decisions. |
| `Q-02-04` — containment | Track 03 execution boundaries, device placement, failures and resource lifetimes, against retained scope requirements. |

Existing Track 01 findings retain their original applicability. Retirement of
this track supplies no new correctness evidence or experimental authorization.

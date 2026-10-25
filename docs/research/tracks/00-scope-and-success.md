# Scope and success

This track defines what research must settle first. It is a roadmap, not a
finding: nothing here is evidence. Work on it follows the [research workflow](../README.md).

## Initial target

The starting envelope, subject to revision by this track's own outcomes:

- Apple silicon and macOS capabilities, documented or undocumented, including hardware controls
  reachable from a Hypervisor.framework guest, on Apple silicon under macOS 26;
  the host of record for research is the M4, with an M2 Pro/Max as the second
  generation when available; no App Store or distribution constraint (experiment
  project); a second macOS release is not a research or product requirement; assess M2/M4
  discrepancies when key features depend on them, without a general M2 parity gate;
- unmodified modern x86-64 Linux guests with direct boot;
- SMP as a baseline, not an extension; and
- a minimal headless device set — timer/APIC, serial, block, network,
  entropy, and shutdown, as enumerated in the
  [research governance](../README.md).

Design direction: a thin EL1 shim inside a Hypervisor.framework VM that uses the
native stage-1 page tables, exceptions, timer/interrupt controller and TSO switch,
with translation on the host. Feasibility is judged for this design against QEMU
TCG; other families are comparators, not co-equal candidates. A mechanism missing
on one chip generation narrows applicability; it does not kill the design.

This envelope is an input to be questioned, not a settled scope. Every
narrowing or widening must be recorded as a question in the
[question backlog](../question-backlog.md) and resolved through the applicable
gates.

Licensing implications are recorded as warnings, not candidate-selection blockers.

**Owner clarification, September 20 — x87 accuracy versus cost:** x87 numerical
accuracy is secondary when its performance or adaptation cost is too high.
Full80 numerical fidelity is not an unconditional acceptance requirement.
Compare cheaper inherited implementations using measured cost, numerical limits
and effects on target workloads; disclose the selected accuracy/compatibility
tradeoff. No specific error tolerance or cost threshold is fixed by this ruling.
This is permission to trade x87 numerical accuracy, not a blanket relaxation of
CPU state, memory safety or unrelated instruction semantics. INV-03-02 owns the
component assessment and Q-00-02 owns the resulting compatibility boundary.

## Required definitions

The [backlog's scope record](../question-backlog.md#scope) identifies historical
acceptance labels whose supporting records are unavailable. Before relying on
them, bind an extant applicable record or resolve the missing definition under
its existing question. ABI drafting may proceed within the current envelope;
fixture coverage does not narrow product scope. Concrete workload, compatibility,
quantitative success and economic inputs remain required before dependent
comparisons or synthesis use them. Raise consequential scope choices at the
workflow's [owner checkpoints](../README.md#owner-alignment).

The track must produce, with evidence labels, and with review where
applicable:

- **Users and workloads.** Who the emulator is for and which concrete,
  diverse workloads represent them. A single benchmark-shaped workload does
  not qualify.
- **Compatibility boundary.** Which guest kernels, configurations, features,
  and device expectations are in scope, which are out, and how the boundary
  is tested rather than asserted.
- **Fair baselines.** Which existing systems the project is compared against,
  in which configurations, and what makes a comparison fair: same guest,
  same workload, same host conditions, tuning effort disclosed on both sides.
  QEMU TCG is a required baseline, fixed by the decision question in the
  [research governance](../README.md); the track selects any additional
  baselines.
- **Measurable "materially better."** A quantitative definition of what
  materially better than the baselines means — metrics, magnitudes,
  workloads, and confidence — agreed before collecting evidence intended to
  judge product success, so success cannot be redefined after the fact. Bounded
  component comparisons may proceed with their own predeclared decision criteria;
  they do not establish that the product is materially better.
- **Non-goals.** What the project explicitly will not attempt in this phase,
  including deferred platforms, device classes, and security objectives
  beyond isolation and crash containment.
- **Stop conditions.** Concrete, checkable conditions under which the honest
  synthesis outcome is stop rather than continue targeted research or
  authorize planning, stated before feasibility or performance work begins.

## Exit criteria

The track is complete when each required definition above has an accepted,
independently reviewed resolution, the end-of-track breadth pass is recorded,
and every surfaced question is filed in the question backlog. Downstream tracks
inherit this scope; they may not silently widen or narrow it.

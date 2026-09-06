# Scope and success

This track defines what research must settle first. It is a roadmap, not a
finding: nothing here is evidence. Work on it follows the [research workflow](../README.md).

## Initial target

The starting envelope, subject to revision by this track's own outcomes:

- public macOS APIs only, on at least two Apple Silicon generations and at
  least two supported macOS releases;
- unmodified modern x86-64 Linux guests with direct boot;
- SMP as a baseline, not an extension; and
- a minimal headless device set — timer/APIC, serial, block, network,
  entropy, and shutdown, as enumerated in the
  [research governance](../README.md).

This envelope is an input to be questioned, not a settled scope. Every
narrowing or widening must be recorded as a question in the
[question backlog](../question-backlog.md) and resolved through the applicable
gates.

## Required definitions

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
  workloads, and confidence — agreed before any performance evidence is
  collected, so success cannot be redefined after the fact.
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

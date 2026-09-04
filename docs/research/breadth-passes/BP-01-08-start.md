# BP-01-08-start — multicore coordination breadth pass

## Metadata

- Breadth-pass ID: `BP-01-08-start`
- Question: `Q-01-08` — Can x86-64 SMP ordering, atomics, invalidation, and
  cross-vCPU events be represented on the supported hosts?
- Date: 2026-09-03
- Executor/session: Claude Code authoring subagent `t01_events_author`

This pass organizes questions and establishes no evidence or conclusion. Every
architectural property, instruction, and divergence named below is named as
something that must be sourced from the Arm Architecture Reference Manual, the
Intel SDM, the SDK headers, or peer-reviewed memory-model literature, and
corroborated by controlled experiment — never asserted from recollection.

## Product assumptions

- SMP is a baseline, not an extension, so no uniprocessor result can support
  feasibility. The assumption under test is `KA-06`: that x86-TSO semantics for
  guest ordinary accesses and atomics can be preserved on the Arm memory model
  with public instructions while at least two vCPUs run concurrently.
- "x86 is TSO and Arm is weaker, so add barriers" is an assumption with three
  unexamined parts: which x86 reorderings are actually architecturally
  permitted (store→load only, per the SDM's ordering section), which Arm
  reorderings are permitted, and whether the mapping is per-access decoration,
  per-region barriers, or something coarser. Each must be sourced at a stated
  revision, with the cited text captured.
- "Guest atomics map to host atomics" assumes agreement on granularity,
  alignment, and width that does not obviously hold. x86 permits a `LOCK`
  prefixed read-modify-write on an unaligned operand, including one crossing a
  cache line; Arm's LSE atomics are specified with an alignment requirement.
  This is the sharpest predicted divergence and must be tested, not argued.
- Width divergence is separate from alignment: guest `CMPXCHG16B` is a 16-byte
  atomic; whether an equivalent exists on the host without a lock, and under
  which Arm feature (LSE, LSE2), must be established per host generation.
- The assumption that a vCPU is a host thread is itself a product assumption.
  A design that multiplexes vCPUs onto one thread has a different ordering
  exposure entirely. This pass records the alternative; `Q-01-02` screens it.
- Correctness is a prerequisite for every timing sample. Any throughput number
  produced while investigating this question is capability evidence under the
  `Q-05-01` staging, not a performance result.

## Adjacent systems

- QEMU's multi-threaded TCG is the nearest adjacent system for the ordering
  problem and the mandatory comparator for the product, but its implementation
  choices are not a source for an architectural fact; the Arm ARM and the SDM
  are.
- Apple's Intel-binary translation is widely believed to use a hardware total
  store ordering mode. Whether any such mode is reachable by an unprivileged
  process through a documented public API is exactly a `Q-01-01` admissibility
  question, and until it answers, no design may assume it exists and no
  measurement of it may support a `Q-01-08` resolution.
- Linux's own arm64 atomics and barrier selection, and the Arm-provided
  `<arm_acle.h>` intrinsics, locate the available instruction repertoire.
- Peer-reviewed memory-model work is citable as primary: Sewell et al.,
  "x86-TSO: A Rigorous and Usable Programmer's Model" (CACM 2010) for the guest
  model, and Pulte et al., "Simplifying ARM concurrency" (POPL 2018) for the
  host model. They are models of the architectures, not of this host silicon,
  and the distinction must be stated wherever they are cited.

## Alternatives

- Preservation strategies to be questioned, not chosen here: acquire/release
  decoration of every guest load and store (`LDAR`/`STLR`); explicit `DMB ISH`
  at selected points; barriers only around guest atomics and fences, accepting
  a weaker model and testing whether real guests can observe the difference;
  serializing vCPU execution, which preserves ordering by removing concurrency;
  and any hardware ordering mode, if `Q-01-01` finds one admissible.
- Alternatives for the misaligned or cache-line-crossing locked RMW: trap and
  emulate under a global lock; a striped or address-hashed lock; a
  page-granular lock; or declaring the case outside the compatibility boundary,
  which is a `Q-00-02`/`Q-02-03` boundary change and not a local decision.
- Alternatives for cross-vCPU invalidation: a shared epoch counter with
  acknowledgment, per-vCPU generation counts, or host memory-protection changes
  that the other threads must observe. `Q-01-05` owns the paging and
  invalidation mechanism; this question owns whether the cross-thread
  handshake it needs is representable and bounded.
- Selecting among these is `Q-01-02` screening and `Q-03-01` comparison, not
  this pass and not this question's resolution.

## Negative and boundary cases

- A litmus experiment that never observes the relaxed outcome under plain
  loads and stores proves nothing about the barrier that follows it. The
  relaxed outcome under plain accesses is the mandatory positive control: it
  establishes that the harness can expose the weakness at all. Store-buffering
  and message-passing are the minimum shapes; load-buffering, IRIW, and
  read-from-untaken-branch shapes are boundary cases whose omission must be
  stated.
- Mixed-size and overlapping accesses — a byte store overlapping a word
  compare-and-swap — are where the two models are hardest to align and where
  litmus coverage is weakest. Their exclusion from a first experiment is a
  limitation to record, not a gap to leave silent.
- Misaligned atomic boundary cases: an atomic straddling a 16-byte boundary and
  one straddling a 64-byte cache line are different cases, because at least one
  Arm feature level defines single-copy atomicity within a 16-byte granule.
  Both must be probed, and the observed failure signal (alignment fault,
  `SIGBUS`, or silent non-atomicity) distinguished — silent non-atomicity is
  the dangerous outcome, because it produces no fault to catch.
- x86 fence semantics that must be represented: `MFENCE`, `LFENCE`, `SFENCE`,
  the implicit full fence of a `LOCK`-prefixed operation and of `XCHG` on
  memory, and non-temporal stores (`MOVNTI`, `MOVNTDQ`) whose ordering is
  weaker than ordinary stores and whose visibility rules differ.
- Cross-modifying code: the SDM specifies a synchronization sequence for one
  processor modifying code another is executing. It is a multicore boundary
  case that couples this question to `Q-01-05` (tracking) and `Q-01-04`
  (executable memory), and it must not be assumed covered by either.
- Cross-vCPU invalidation boundary: a guest `INVLPG` or `CR3` write on one vCPU
  must become visible to the others. Whether host mapping changes made by one
  thread are ordered with respect to other threads' accesses must be sourced
  from the host documentation, not assumed from the guest's semantics.
- Oversubscription boundary: 8 vCPU threads on a 4 P + 6 E host, with a vCPU
  descheduled while holding a guest spinlock. The guest reacts with soft-lockup
  and RCU-stall reporting, and the accepted cell-pass rule counts an
  unexplained warning or stall as a failure. This is a host-scheduling
  behaviour failing a correctly implemented emulator; no existing question owns
  it, and this pass files `Q-01-23`.
- Guest `PAUSE` in a spin loop and its host analogue: whether spinning vCPU
  threads starve their peers, and whether a yield or wait primitive changes it.
- Negative case for the experiment itself: a "zero relaxed outcomes observed"
  result produced by a broken harness — the compiler hoisted or removed the
  load, the threads never overlapped, or the iteration count was too small.
  Every such result is indistinguishable from success without controls.

## Host and guest matrix

- Host dimensions: Apple Silicon generation, exact macOS build and SDK, thread
  count relative to 4 P + 6 E, core type of each participating thread, QoS
  class, oversubscription ratio, and the Arm architectural feature level the
  chip implements. Feature availability is generation-dependent, so an atomics
  result on M4 does not transfer to M1 Pro even for a documented instruction;
  this must be stated explicitly rather than covered by a general caveat.
- Any observation this session can produce is observed on `Mac16,10` (M4) /
  macOS 26.6.2 (25G83) / SDK 26.4 only; `P1` (M1 Pro / Sequoia 15) replication
  is owed before any platform-wide use, per the executor guide's incremental
  replication rule. The accepted minimum pairs remain `P1` and `P2` from
  `INV-00-02`. Statements drawn from the Arm ARM or the SDM may state
  architectural scope; measured outcomes may not.
- Guest dimensions: 2, 4, and 8 vCPUs at 2 GiB and 8 GiB; kernels 6.12 and
  6.18; workloads that stress ordering and contention (parallel kernel builds,
  concurrent database or filesystem work) versus single-threaded workloads that
  would hide every ordering defect.
- Core placement is a stratum `INV-00-04` freezes into its denominator while
  `INV-00-08` limits coverage to instrumentable strata. If host affinity is not
  controllable, `Q-01-08` must report that with a source and report what its
  QoS sampling actually varies, so `Q-05-06` can normalize rather than inherit
  an unmeasurable stratum.

## Multicore interactions

This is the question's own subject; the `BP-00-end` multicore items owned by
`Q-01-08`, `Q-02-02`, and `Q-05-06` are addressed individually:

| Inherited item | Disposition in this pass | Owner |
| --- | --- | --- |
| All-vCPU-online admission | Sets the minimum concurrency any ordering evidence must exercise: an ordering result from fewer threads than the admitted topology does not cover that topology. | `Q-01-08` for the mechanism, `Q-02-02` for the evidence set |
| Cross-vCPU faults and pressure | A fault taken on one vCPU while others execute is both an ordering case (what the faulting thread's stores are ordered against) and a composition case. | `Q-01-06`, `Q-01-12` |
| Oversubscription | 8 vCPUs on a 10-core host is a required case, not an edge case; it changes both ordering-outcome frequency and liveness. | `Q-01-08`, new `Q-01-23`, `Q-05-06` |
| Two live instances | Doubles thread count and contention; affects observation frequency in litmus work and must be a declared confound. | `Q-05-06`, `Q-05-02` |
| Placement classes | Sampled by QoS at best; whether `thread_policy_set` with `THREAD_AFFINITY_POLICY` has any effect must be reported as a platform fact feeding the stratum question. | `Q-05-06` |
| 1 vCPU diagnostic only | No uniprocessor result may be reported as SMP evidence here. | `Q-00-02`, `Q-02-02` |

- The `BP-00-end` correctness-row conflict over required vCPU topologies
  (2/4/8 mandatory versus 8 conditional) is not resolved here. This question
  treats 2, 4, and 8 as the questioned range and defers the conflict to
  `Q-05-02` and `Q-00-02` as `BP-00-end` assigns it.
- The interrupt hand-off between vCPUs splits cleanly: `Q-01-07` owns delivery
  latency and its distribution, `Q-01-08` owns the ordering and atomicity of
  the state handed over, and `Q-01-13` owns the guest APIC semantics built on
  both. A result from one must not be reported as covering another.

## Falsifiers and stop paths

- `KA-06` is the assumption this question tests, jointly with `INV-01-12`.
  It is falsified if relaxed, TSO-forbidden outcomes remain observable under
  every admissible public preservation strategy; or if a guest-legal atomic —
  notably a misaligned or line-crossing locked RMW — has no correct host
  representation and no correct software fallback; or if cross-thread
  invalidation cannot be made to complete with a bounded, observable
  acknowledgment. Failure routes to project stop input 3 (the SMP boundary) and
  to family kill 2 for any family that cannot meet the accepted SMP boundary.
- `KA-11` (composition of translation, paging, exceptions, interrupts, and two
  concurrent vCPUs) depends on this question's mechanism surviving alongside
  the `KA-04` fault mechanism and the `KA-07` tracking mechanism; `Q-01-12`
  owns the composed test.
- `KA-07` interacts through cross-modifying code: a tracking mechanism that is
  correct on one vCPU may not be correct when another vCPU is executing the
  page being modified.
- Cost is not a `KA-06` falsifier. A preservation strategy that is correct but
  expensive is a `Q-04-01` scaling input, and a negative performance result
  cannot kill a family before `Q-04-02`'s maturity predicate is accepted.
  Routing cost into a feasibility kill here would evade that rule.
- Non-observation is not proof of prohibition. Zero relaxed outcomes over any
  finite iteration count bounds a probability; it does not establish that the
  architecture forbids the outcome. The architectural claim must come from the
  cited Arm ARM section, with the experiment as corroboration; an investigation
  that inverts this has an unsupported Source-backed claim.
- A divergence confined to one host generation, one core-type pairing, or one
  QoS stratum narrows applicability to that stratum until replicated; it is not
  by itself a kill.

## Measurement failure modes

- Litmus tests are probabilistic and harness-sensitive. The iteration count,
  the per-iteration synchronization that makes threads actually overlap, the
  randomization of thread start offsets, and the observed outcome histogram
  must all be recorded; a bare "not observed" is uninterpretable.
- Compiler transformation is the dominant defect mode. Without inline assembly
  and a disassembly check of the emitted sequence, the experiment measures the
  optimizer rather than the hardware.
- Memory layout changes outcome frequency: whether the two litmus variables
  share a cache line, and their alignment within it, must be fixed and stated,
  or a layout change silently becomes the result.
- Thread placement asymmetry — P–P, P–E, and E–E pairings — plausibly changes
  observation rates. Sampling QoS classes does not control placement, so any
  P-versus-E statement is an inference with stated uncertainty, never an
  observed placement result.
- Contention throughput across 2, 4, and 8 threads is affected by frequency
  scaling, thermal drift, and arm order; arms must be interleaved and the
  environment captured. It remains capability evidence, not a comparison.
- The invalidation round-trip measurement inherits every instrument limitation
  of `Q-01-07`; the clock characterization must precede it or the reported
  latency includes an uncharacterized instrument error.
- The misaligned-atomic probe installs a fault handler. A handler that swallows
  unrelated faults, or that leaves the process in a state where a real defect
  no longer faults, converts a negative result into a false positive; the
  handler's scope and restoration must be part of the method.
- A crash, a debugger-intercepted signal, and a correctly caught alignment
  fault are three different outcomes that must be distinguished in the recorded
  output rather than collapsed into "faulted".
- No number produced under this question may enter a `Q-00-04` aggregate or a
  baseline comparison; the `Q-05-01` staging admits it as capability evidence
  only.

## Backlog updates

Issues surfaced by this pass that an existing question already owns are mapped
rather than duplicated, after checking the Scope, Platform, Correctness,
Architecture, Performance, Benchmarking, Integration, and Deferred-security
tables:

| Surfaced issue | Owner |
| --- | --- |
| Admissibility of any hardware ordering mode, LSE/LSE2 feature reliance, and each named API | `Q-01-01` |
| Screening of preservation strategies into architecture families | `Q-01-02`, `Q-03-01` |
| Cross-vCPU page-table invalidation and cross-modifying-code tracking mechanism | `Q-01-05` |
| Faults taken while peers execute; composed multi-vCPU prototype | `Q-01-06`, `Q-01-12` |
| Guest-visible atomics, fence, and topology contract, including any excluded locked-RMW case | `Q-02-03`, `Q-00-02` |
| SMP litmus, invariant, and stress evidence set for correctness | `Q-02-02` |
| Cost and scaling of the chosen preservation strategy; maturity before any performance kill | `Q-04-01`, `Q-04-02` |
| Helper-thread, CPU-availability, and placement normalization when affinity control is incomplete | `Q-05-06` |
| Required vCPU topology conflict inherited from `BP-00-end` | `Q-05-02`, `Q-00-02` |
| Cross-vCPU interrupt delivery latency and its distribution | `Q-01-07` |

One issue has no existing owner. This pass creates it in the Platform table:

- `Q-01-23` — What vCPU-thread scheduling contract (preemption, QoS class, P/E
  migration, and oversubscription at 2/4/8 vCPUs on 8–10-core hosts) does the
  host provide, and which guest-visible liveness failures — lock-holder
  preemption, guest soft lockup, RCU stall, spin-wait starvation — can it
  induce?

No claim, source, method, or result work was performed during this pass.

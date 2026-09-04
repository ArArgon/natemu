# BP-01-18-start — shared-code publication and quiescence breadth pass

## Metadata

- Breadth-pass ID: `BP-01-18-start`
- Question: `Q-01-18`
- Date: 2026-09-04
- Executor/session: Codex `/root`, Track 01 continuation session

This pass organizes questions. It establishes no platform fact, protocol,
performance result, architecture preference, or implementation decision.
Mechanisms named below are candidates to source or test under later gates.

## Coverage

### Product assumptions

- The product envelope requires 2, 4, and 8 guest vCPUs. A protocol that is
  safe only with one translating/executing thread cannot satisfy that envelope
  merely because the underlying executable-memory mechanism passed a
  single-thread probe.
- The shipped signing configuration matters. Ad-hoc/no-hardened-runtime
  observations from `INV-01-04` do not establish behavior for Developer ID,
  Hardened Runtime, notarized, callback-allowlist, or sandboxed forms.
- Do not assume a shared mutable code cache is required. Immutable publication,
  per-vCPU caches, patch-free indirection, and interpretation remain candidate
  families or within-family dimensions until evidence excludes them.
- “Safe” includes instruction-fetch coherence, data-race freedom, reclamation,
  guest invalidation semantics, bounded progress, and explicit failure. A
  protocol that usually publishes the right bytes but can execute a partial or
  reclaimed block fails correctness regardless of its median cost.

### Adjacent systems and primary-source targets

- Candidate primary sources are Apple's JIT guidance and SDK declarations for
  `pthread_jit_write_protect_np`, callback-based JIT writing,
  `sys_icache_invalidate`, memory mapping, thread QoS, and synchronization;
  the Arm Architecture Reference Manual's self-/cross-modifying-code and
  cache-maintenance requirements; the C memory model for publication through
  atomics; and upstream Linux x86 documentation for guest instruction-cache,
  TLB, and self-modifying-code expectations.
- Existing emulator and language-runtime protocols may locate alternatives,
  but their source trees cannot establish macOS or Arm architectural behavior.
  If used, pin exact upstream revisions and cite only what their code actually
  does.
- `INV-01-08` carries the guest/host memory-ordering evidence; `INV-01-05`
  carries guest-memory tracking; `INV-01-06` carries fault delivery. This
  investigation must compose those boundaries without relabelling their
  claims.

### Alternatives

- Immutable block construction followed by one atomic entry-table publication;
  never patch published code.
- Immutable blocks plus patchable branch/link slots, with an architecture-
  specified cache-maintenance and synchronization sequence.
- Stop-the-world publication or mapping changes: request every vCPU to stop,
  prove arrival at a safe point, publish/patch, then release.
- Epoch/RCU-style quiescence: publish a generation, wait for every vCPU to
  cross a boundary, then reclaim old blocks or mappings.
- Per-vCPU code caches, avoiding cross-thread publication at the cost of
  duplicate translation and more invalidation work.
- Patch-free indirection through atomic data pointers or entry tables.
- One dedicated writer thread or callback-allowlisted writer, with executing
  vCPUs permanently write-protected.
- Process-wide mapping replacement or aliasing, if public APIs and signing
  policy permit it; this is a candidate to test, not an assumed capability.

### Negative and boundary cases

- Reader enters while a block is only partly written; entry is published before
  cache maintenance; or metadata becomes visible before code bytes.
- Reader executes a block while another thread patches the same instruction,
  including a patch straddling an instruction or cache-line boundary.
- A vCPU is preempted indefinitely after advertising entry into an epoch or
  while holding the protocol's writer state.
- A fault, signal, cancellation, or thread exit occurs during publication,
  patching, safe-point entry, or release. Fault-state questions are owned by
  `Q-01-19`; this investigation must expose the required handoff.
- Nested publication, two simultaneous writers, writer starvation, generation
  wraparound, stale acknowledgements, lost wakeups, and vCPU creation/removal.
- Reclaim or remap while a vCPU retains a stale entry pointer; ABA reuse of a
  code-cache address; and invalidation concurrent with translation.
- Zero, full, fragmented, and near-resource-limit code caches; 1/2/4/8 threads;
  oversubscription; background versus user-interactive QoS; and forced delays
  at every protocol transition.
- Guest cross-modifying code, guest page-table invalidation, and executable
  guest-page writes must be represented as explicit stimuli rather than a
  synthetic writer loop alone.

### Host and guest matrix

- Host cells: at minimum the accepted P1 and P2 generation/OS pairs, recorded
  SDK, signing/hardening configuration, power state, QoS, and observable host
  load. A one-host result stays one-host evidence.
- Thread topology: 1 as a diagnostic control and 2/4/8 as product cells;
  oversubscribed cells where runnable vCPU threads exceed available performance
  cores; placement sampled but never claimed as controlled without evidence.
- Guest dimensions: x86-64 code publication by the emulator, guest
  self-modifying/cross-modifying code, invalidation frequency, basic-block size,
  and vCPU topology. No full guest is required for the first mechanism probe,
  but a synthetic protocol result cannot establish guest correctness.
- Signing cells must include the forms the surviving distribution models in
  `INV-01-11` permit. An unavailable signing cell is recorded as unavailable,
  not silently replaced with ad-hoc signing.

### Multicore interactions

- Separate write visibility, instruction-cache visibility, entry publication,
  safe reclamation, and guest invalidation acknowledgement. Passing one does
  not imply the others.
- Ask whether cache maintenance performed by the writer has the documented
  scope required for readers on other processing elements, and whether readers
  require their own synchronization action before fetching new code.
- Safe-point progress depends on host scheduling. A protocol that waits for all
  vCPUs can deadlock or exceed guest liveness bounds when a vCPU is preempted;
  this connects directly to `Q-01-23`.
- Process-wide mapping changes may interact with every executing thread even if
  write protection is per-thread. Map-change atomicity and stale-translation
  behavior must be tested separately from code-byte publication.

### Falsifiers and stop paths

- Family-level falsifier: no protocol using public APIs can publish and reclaim
  translated code without partial/stale execution or unbounded stop-the-world
  dependence at 2/4/8 vCPUs. This contributes to `KA-01`, `KA-04`, and `KA-06`
  and must be routed through the `INV-01-02` family register; it is not itself
  a project stop.
- Correctness failure—wrong bytes executed, use-after-reclaim, missed
  invalidation, or protocol deadlock—invalidates all cost samples from that run.
- A cost miss cannot kill a family in Track 01. It becomes an input to
  `Q-04-01` and is kill-eligible only under the accepted `Q-04-02` maturity
  rule.
- Failure in one protocol must not be generalized to immutable, per-vCPU,
  indirection, or interpretation alternatives that do not share its premise.

### Measurement failure modes

- A checksum after threads join can miss transient partial execution. Readers
  must report every executed version or an oracle-protected result per
  iteration, with sequence/generation consistency checked online or in raw
  output.
- A writer throughput loop without delayed/preempted readers does not exercise
  quiescence. Deterministic delay/fault injection is required at each state.
- Median publication cost hides correctness-critical tails. Report bounded
  completion, timeout counts, p50/p99/max only after correctness passes, and
  keep per-topology distributions distinct.
- Timer validity, core placement, QoS, host load, thermal state, first-touch,
  cache warmth, and code-size distribution can dominate timing; these route to
  `Q-05-06` and `Q-05-08` and remain explicit limitations here.
- Compiler transformations can invalidate a source-level litmus. Inspect the
  disassembly of writer, reader, cache-maintenance, atomic-publication, and
  safe-point paths at the code gate.
- Reusing one random schedule can hide an interleaving. Fix seeds in the raw
  artifact, vary deterministic delay positions, and predeclare repetitions.
- A process crash with no durable raw record is not a pass. The runner must
  preserve arm identity, exit/signal status, and partial output outside Git.

## Backlog updates

Every surfaced issue maps to an existing owner:

- fault/signal state during patching → `Q-01-19`;
- vCPU preemption, oversubscription, and safe-point progress → `Q-01-23`;
- guest SMP/self-modifying-code correctness → `Q-02-02`;
- containment of crashes and unclassifiable faults → `Q-02-04`;
- cost and maturity → `Q-04-01`, `Q-04-02`;
- benchmark topology and measurement instruments → `Q-05-02`, `Q-05-06`,
  `Q-05-08`;
- architecture-family disposition → `Q-01-02`, then `Q-03-01`.

`no new issues found`

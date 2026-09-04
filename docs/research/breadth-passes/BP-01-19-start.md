# BP-01-19-start — fault delivery during JIT writing and patching breadth pass

## Metadata

- Breadth-pass ID: `BP-01-19-start`
- Question: `Q-01-19`
- Date: 2026-09-04
- Executor/session: Codex `/root`, Track 01 continuation session

This pass organizes questions. It establishes no host behavior, API guarantee,
fault-handler discipline, performance result, architecture preference, or
implementation decision.

## Coverage

### Product assumptions

- Every product vCPU may execute translated code and may fault while another
  vCPU publishes, invalidates, or retires code. A one-thread handler result
  cannot establish safety at 2/4/8 vCPUs.
- Do not assume that signal or Mach-exception entry preserves the calling
  thread's JIT write-protection state, that return restores it, or that the
  state is inspectable. Each is a separate proposition.
- Do not assume that a handler may allocate, lock, log, change mappings, call
  JIT APIs, or perform cache maintenance. Signal-context legality and
  process-state correctness are different questions and both matter.
- The shipped signing and entitlement cell matters. Ad-hoc/no-hardened-runtime
  behavior cannot stand in for Developer ID, Hardened Runtime, notarized,
  sandboxed, callback-allowlisted, or late-freeze configurations.
- A process crash, silently writable executable region, wrong-thread resume,
  lost fault, stale code execution, or unclassifiable fault is a correctness
  failure and suppresses every cost sample from that run.

### Adjacent systems and primary-source targets

- Candidate primary sources are Apple's JIT guidance and SDK `pthread.h`,
  `sigaction(2)`, `sigaltstack(2)`, `signal-safety(7)` if present, `setjmp(3)`,
  `ucontext` and arm thread-state headers, Mach exception declarations, and
  the relevant POSIX/C clauses for signal handlers and non-local transfer.
- `Q-01-04` owns executable-memory acquisition and normal write transitions;
  `Q-01-06` owns basic fault delivery and resume; `Q-01-18` owns publication,
  patching, and quiescence. This question owns only their composition while a
  fault or signal interrupts the thread.
- Guest exception meaning and generated-code commit discipline remain
  `Q-02-03` and `Q-02-01`. Loud containment of an unclassifiable host fault is
  `Q-02-04`.
- Thin SDK declarations are not automatically behavioral guarantees. Their
  cross-release stability and admissibility remain explicit limits under
  `Q-01-01` and `Q-05-02`.

### Alternatives

- Keep all fault-prone work outside JIT write callbacks; callbacks validate
  fixed input and copy bytes only, returning an error normally.
- Use immutable blocks and patch-free entry-table publication, reducing but
  not eliminating faults during callback execution.
- Stop and quiesce vCPUs before any patch or mapping transition.
- Use per-vCPU code caches so a faulting writer cannot expose a partially
  changed shared block.
- Use Mach exception ports rather than a signal handler, or explicit checks
  that avoid host faults on translated paths.
- Treat any host fault while write permission is active as fatal and rebuild
  the code cache after process restart; this is a candidate containment rule,
  not an accepted product behavior.

### Negative and boundary cases

- Deliver a guest-classified protection fault before entering the callback,
  while writable, after byte copy but before cache maintenance, after cache
  maintenance but before return, and immediately after executable state is
  restored.
- Exercise host-bug faults at the same points and require them not to be
  misclassified as guest faults.
- Exercise nested faults, a fault on the alternate stack, stack exhaustion,
  masked signals, simultaneous faults on multiple vCPUs, and a fault on a
  non-vCPU helper thread.
- Include invalid callback input, recursive callback entry, attempted
  `longjmp`, exception-style non-local transfer where the language permits it,
  cancellation, thread exit, and termination during a write window.
- Cover write-protection-supported false/true paths, direct toggle versus
  callback path, allowlist absent/present, late-freeze absent/present, and
  callback images present at startup versus loaded later.
- Separate signal delivery, Mach exceptions, debugger attachment, crash
  reporter behavior, and explicit-check paths. One mechanism cannot
  implicitly validate another.

### Host, guest, and signing matrix

- Required platform cells are P1 (M1 Pro / Sequoia 15 / recorded SDK) and P2
  (M4 / Tahoe 26 / SDK 26.4), with exact build, SDK, and header digests.
- Thread cells are 1 as a diagnostic and 2/4/8 as product topologies. Record
  QoS, observable placement, oversubscription, power state, host load, and
  debugger/crash-reporter state.
- Signing cells include every form allowed by the surviving distribution
  models. An unavailable Developer ID, notarization, sandbox, or entitlement
  cell is reported unavailable, never substituted with ad-hoc signing.
- Guest stimuli distinguish a guest page fault, guest self-modifying-code
  invalidation, emulator bug, resource exhaustion, and external asynchronous
  signal. Synthetic cases cannot establish full-guest correctness.

### Multicore composition

- JIT write protection is documented as per-thread on the callback path;
  process mappings, signal dispositions, and exception-port configuration can
  affect the whole task. Their scopes must not be conflated.
- Simultaneous writers and faulters can invert locks, wait for a stopped vCPU,
  or observe stale quiescence generations. Deterministic interleaving points
  must cover every protocol transition.
- If a handler returns while another vCPU can enter the modified block, the
  `Q-01-18` publication and cache-maintenance rules still apply. State
  restoration alone is not publication correctness.
- A Mach exception server may serialize concurrent vCPU faults; signal paths
  may run concurrently. Both interact with oversubscription and lock-holder
  preemption owned by `Q-01-23`.

### Falsifiers and stop paths

- A family-level falsifier is that every public-API discipline for a retained
  translation family can leave the JIT region writable, resume the wrong
  state, lose or misclassify faults, execute partial/stale code, or deadlock at
  2/4/8 vCPUs. Any disposition must pass through `INV-01-02`.
- Failure of a live-patching protocol does not kill immutable, per-vCPU,
  explicit-check, or interpreter alternatives that do not share its premise.
- High handler or quiescence latency is a later `Q-04-01` input, not a Track 01
  family kill without the accepted `Q-04-02` maturity rule.
- Any correctness or integrity failure stops the run and suppresses all
  performance output.

### Measurement failure modes

- Observing only the post-return protection state can miss a transient write
  window or stale execution. Record state transitions and executed generations
  with an online oracle or durable per-event trace.
- A negative arm that merely crashes does not prove the positive oracle can
  distinguish wrong protection, wrong PC, bad stack, lost fault, or stale code.
  Inject each fault independently and require the checker to name it.
- Async-signal-unsafe instrumentation can create the behavior under test.
  The capture path itself needs review and a minimal handler surface.
- Compiler movement, lazy symbol binding, stack growth, first-touch, debugger
  interception, scheduler gaps, QoS, host load, thermal state, and placement
  can dominate both correctness and latency; disassembly and environment
  capture are required before measurement.
- Timeout and partial-output behavior must preserve arm identity, exit/signal
  status, and the last committed protocol state in the external raw artifact.

## Backlog updates

Every surfaced issue maps to an existing owner:

- publication, cache maintenance, quiescence, and reclamation → `Q-01-18`;
- basic fault delivery and resume → `Q-01-06`;
- executable-memory/signing states → `Q-01-04` and `Q-01-01`;
- scheduling, lock-holder preemption, and oversubscription → `Q-01-23`;
- guest exception semantics and translation commit discipline → `Q-02-03`
  and `Q-02-01`;
- loud containment and fault-injection oracle → `Q-02-04`;
- costs and maturity → `Q-04-01` and `Q-04-02`;
- platform coverage and instruments → `Q-05-02`, `Q-05-06`, `Q-05-08`;
- family disposition and final contradiction exposure → `Q-01-02` and
  `Q-06-01`.

`no new issues found`

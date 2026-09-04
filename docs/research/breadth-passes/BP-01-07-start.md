# BP-01-07-start — timers and asynchronous-event breadth pass

## Metadata

- Breadth-pass ID: `BP-01-07-start`
- Question: `Q-01-07` — What timekeeping and asynchronous-event facilities exist
  on the supported hosts, at what resolution and latency, and can guest timer
  and interrupt delivery be built on them?
- Date: 2026-09-03
- Executor/session: Claude Code authoring subagent `t01_events_author`

This pass organizes questions and establishes no evidence or conclusion. Every
API, mechanism, and property named below is named as something that must be
questioned and sourced, not as something known to hold.

## Product assumptions

- The envelope makes timer/APIC a mandatory device class but states no
  resolution, latency, or jitter requirement. `Q-01-07` must derive the
  requirement from guest need — a periodic tick at `CONFIG_HZ` 100/250/1000, a
  one-shot TSC-deadline or APIC-timer programming model, and `NOHZ`/`NOHZ_FULL`
  behavior in the candidate 6.12/6.18 kernels — before measuring what the host
  offers, so the bar is not set retroactively by the measurement.
- "A monotonic clock exists" conflates properties that must be questioned
  separately: monotonicity, behavior across system sleep, rate stability, rate
  scaling (`mach_timebase_info` numerator/denominator must not be assumed
  1:1), cross-core and cross-cluster coherence, resolution as reported by
  `clock_getres`, and per-read cost. `mach_absolute_time`,
  `mach_continuous_time`, and `clock_gettime` with `CLOCK_MONOTONIC`,
  `CLOCK_MONOTONIC_RAW`, `CLOCK_UPTIME_RAW`, and `CLOCK_UPTIME_RAW_APPROX`
  differ on at least the sleep and cost axes; each must be sourced from Apple
  documentation, the man pages, and the SDK headers rather than folklore.
- Read cost is a product assumption in its own right. A guest may execute
  `RDTSC` at high frequency, so whether a host clock read is a user-space
  read or a system call bounds the achievable cost of one emulated `RDTSC`.
  Resolution alone does not answer this; per-read cost must be measured.
- `CLOCK_REALTIME` is assumed unusable as a guest time base because it is
  steppable by the time daemon and by the user; that assumption must be stated
  and sourced, not silently applied.
- The containment oracle inherited from `INV-00-08` presumes host timing it
  does not own: heartbeats sampled "at most every 100 ms" and cap enforcement
  "within 1 s of the declared cap breach". `Q-01-14` and `Q-02-04` own those
  values; `Q-01-07` owns whether the host wakeup facilities can meet them at
  all, and must report the interaction rather than assume it.
- Timer wakeups are assumed free. Eight vCPUs at a 1 kHz tick is 8000 host
  wakeups per second before any IPI analogue; the energy, thermal, and
  scheduling cost of that wakeup rate is a scaling input owned by `Q-04-01`
  and instrumented under `Q-05-08`, and must be surfaced here, not absorbed.

## Adjacent systems

- QEMU's timer and clock subsystem and KVM's guest-clock handling are the
  nearest full-system designs and can locate mechanisms and failure modes, but
  neither is a citable source for a host platform fact; `Q-01-01` decides which
  host facilities are admissible at all.
- Hypervisor.framework exposes its own vCPU run-deadline and exit facilities.
  Whether they exist in a form usable by a family that runs translated code
  under a VM (`F3`) is a `Q-01-01`/`Q-01-02` admissibility matter; this pass
  only records that a family choice changes which timer facility is even
  reachable, so `Q-01-07` must not measure one family's mechanism and report it
  as the platform's.
- Apple's Intel-binary translation and the Rosetta-based container workflows
  are user-mode, not full-system, and expose no guest timer contract; they are
  not comparators for this question (`Q-00-03` owns comparator relevance).
- `Q-01-09` owns whether a timer/APIC device class has an unprivileged public
  backing at all, and `Q-01-13` owns the guest-visible machine contract that
  such a backing must satisfy. `Q-01-07` supplies the host capability both
  depend on and must not pre-empt either.

## Alternatives

- Time-base alternatives: slaving guest time to a host monotonic clock;
  slaving with a fixed rate scale; or driving guest time from execution
  progress (instruction or translation-block counts) so guest time is virtual.
  These have incompatible consequences for guest timeout code and for whether
  an in-guest measurement means anything. No existing question owns the choice;
  this pass files `Q-01-22`.
- Wakeup alternatives: `mach_wait_until` on an absolute deadline in
  `mach_absolute_time` units; `nanosleep`/`usleep` on a relative interval;
  `dispatch_source` timers with an explicit leeway; `dispatch_after`;
  `kqueue` `EVFILT_TIMER`; `pthread_cond_timedwait`; a spin-then-park hybrid;
  and no host wakeup at all, with deadlines checked at translated-block
  boundaries. The last is not a timer facility and must be evaluated as a
  distinct alternative, because it converts timer latency into a code-generation
  and exit-frequency problem owned by `Q-01-04`.
- Cross-thread delivery alternatives: `pthread_kill` with a handler, which is
  the only listed mechanism that interrupts a thread already executing
  generated code; `dispatch_semaphore_signal`; `pthread_cond_signal` with a
  mutex; `os_unfair_lock` for the state the two sides share; and a polled flag
  checked at block boundaries. Each has a different answer to "does the target
  have to be asleep to receive this", which is the property a guest IPI needs.
- Any `__ulock`/futex-style primitive is presumed non-public; `Q-01-01` must
  rule before any such mechanism is measured, and a measurement of a
  non-admissible mechanism cannot support a `Q-01-07` resolution.
- Choosing among these alternatives is not this pass's or this question's job;
  `Q-01-02` screens families and `Q-03-01` compares them.

## Negative and boundary cases

- Requested-versus-delivered divergence at short intervals: a 100 µs target, a
  1 ms target, and a 10 ms target may behave qualitatively differently. Whether
  a zero-leeway `dispatch_source` timer is still subject to coalescing, and
  whether any documented mechanism opts out, must be sourced.
- App Nap, Low Power Mode, background QoS, display sleep, and battery power are
  each a boundary case that can multiply wakeup latency without an error being
  returned. A silent slowdown with no diagnostic is worse than a failure and
  must be treated as a first-class negative case.
- System sleep and wake: whether a pending deadline fires late, fires
  immediately on wake, or is lost; and whether the chosen clock advanced during
  sleep. A guest that observes hours of elapsed time in one tick and a guest
  that observes none are two different failure modes.
- Deadlines already in the past, deadlines beyond the representable range, zero
  and negative intervals, and 64-bit tick overflow after rate scaling.
- Signal delivery boundaries: delivery to a thread executing generated code
  with an unusual register or stack state; `sigaltstack` interaction with the
  fault handler `KA-04` needs; blocked-signal windows; `EINTR` and restart
  semantics on an interrupted host call; re-entrancy when an asynchronous
  interrupt signal arrives inside the synchronous fault handler.
- Signal coalescing is a correctness boundary: repeated `pthread_kill` of the
  same signal number collapse, while a guest APIC needs per-vector state and a
  pending count. A mechanism that loses interrupts is not a candidate.
- Guest boot-time clock calibration is a boundary case that couples this
  question to `Q-01-13`: Linux calibrates the TSC against a second timer, so an
  emulated TSC and an emulated PIT/HPET/ACPI-PM timer that disagree produce a
  guest that mis-calibrates or rejects the clocksource, even though each device
  is individually plausible.
- Late-wakeup policy: dropped ticks versus catch-up storms. Linux reacts to
  both; the guest-visible consequence of each must be stated before a mechanism
  is called sufficient.
- QoS demotion of a thread that holds an emulator-internal lock inverts
  priority and turns a timer question into a liveness question (`Q-01-23`).

## Host and guest matrix

- Host dimensions: Apple Silicon generation, exact macOS build and SDK, core
  type and cluster (P versus E), QoS class of the requesting and target
  threads, thread count relative to 4 P + 6 E, AC versus battery, Low Power
  Mode, thermal state, and foreground versus background application state.
- The accepted minimum pairs are `P1` (M1 Pro / Sequoia 15) and `P2`
  (M4 / Tahoe 26) from `INV-00-02`. Any observation this session can produce is
  observed on `Mac16,10` (M4) / macOS 26.6.2 (25G83) / SDK 26.4 only; `P1`
  (M1 Pro / Sequoia 15) replication is owed before any platform-wide use, per
  the executor guide's incremental replication rule. Documented availability
  from Apple sources may be stated across the matrix; measured latency may not.
- Guest dimensions: 2, 4, and 8 vCPUs at 2 GiB and 8 GiB; candidate kernels
  6.12 and 6.18; the guest's selected clocksource (`tsc`, `hpet`, `acpi_pm`)
  and whether it selects TSC at all, which depends on the invariant-TSC CPUID
  advertisement owned by `Q-02-03`; `CONFIG_HZ` and tickless configuration.
- Core placement is a declared stratum in `INV-00-04` but `INV-00-08` limits
  coverage to strata the accepted instrumentation can reach. If placement is
  not directly controllable, `Q-01-07` must say so with a source and report
  what its QoS sampling actually varies, feeding `Q-05-06` rather than claiming
  a placement result.

## Multicore interactions

- Per-vCPU timer state multiplies every latency number by the vCPU count. One
  timer thread serving `n` vCPUs and `n` self-timing vCPU threads fail
  differently under load; both must be within the questioned space even though
  neither is selected here.
- Cross-cluster clock coherence is an SMP correctness question, not a
  performance one: two vCPU threads on different clusters reading the guest TSC
  must not observe time moving backwards. Whether the host clock guarantees
  this must be sourced, not inferred from a sampled comparison.
- The IPI analogue is where `Q-01-07` and `Q-01-08` meet. `Q-01-08` owns
  ordering and atomicity of the shared state an interrupt hands over;
  `Q-01-07` owns the delivery latency and its distribution. Neither owns the
  guest APIC semantics built on top, which is `Q-01-13`.
- Delivery latency must be questioned against a target that is spinning in
  generated code, a target blocked in a host wait, and a target descheduled
  under oversubscription — three different mechanisms with three different
  bounds.
- Inherited from `BP-00-end` (multicore row, owners `Q-01-08`, `Q-02-02`,
  `Q-05-06`): all-vCPU-online admission, cross-vCPU pressure, oversubscription,
  two live instances, and placement classes each multiply host wakeup and
  signal load. `Q-01-07` must state its wakeup-rate assumptions per vCPU so
  those owners can compose them; it must not resolve the 2/4/8 topology
  conflict that `BP-00-end` records against `Q-05-02` and `Q-00-02`.

## Falsifiers and stop paths

- `KA-05` is the assumption this question tests: that host timekeeping provides
  a monotonic clock and timer/wakeup facilities whose resolution and
  cross-thread delivery latency are bounded and measured, sufficient for guest
  timer and IPI semantics.
- `KA-05` is falsified if no available clock is simultaneously monotonic and
  cheap enough to back a high-frequency guest clock read; or if median wakeup
  error at the 1 ms target is not well under the guest tick period; or if
  cross-thread delivery has no bounded, measurable tail; or if coalescing, App
  Nap, or QoS can multiply latency with no public opt-out and no diagnostic.
  Its failure routes to project stop input 3 (the timer/APIC device
  requirement) and to family kill 2 for any family that cannot meet the
  accepted timer contract by other means.
- `KA-08` (timer/APIC device class has an unprivileged public backing) rests on
  the same evidence; `KA-11` (composition) rests on the asynchronous
  interrupt mechanism coexisting with the `KA-04` fault mechanism, since both
  are plausibly signal-based.
- Thresholds must be predeclared against guest need before data. A threshold
  chosen after seeing the distribution is not a falsifier.
- A failure confined to background QoS, Low Power Mode, or battery power
  narrows applicability to the affected stratum; it is not by itself a kill,
  and must be recorded as a stratum limitation.
- No kill on one host: an adverse result on `P2` alone narrows applicability to
  `P2` until independently replicated, per the executor guide.
- Cost is not a `KA-05` falsifier. If a mechanism works but is expensive, that
  is a `Q-04-01` scaling input; misrouting cost into a feasibility kill would
  kill a family on an immature performance argument that `Q-04-02` has not yet
  authorized.

## Measurement failure modes

- The clock under test is also the instrument. Resolution, `clock_getres`
  reporting, and per-read cost must be characterized before any latency
  distribution is interpreted, or the reported jitter is partly the
  instrument's.
- Reporting a mean or a median alone hides the tail that interrupt delivery
  lives in. Minimum, median, p99, and maximum with a predeclared sample size
  must be fixed before the run, and the sample size justified against the
  tail being claimed.
- Warm-up, DVFS ramp, thermal drift across a long run, and order effects
  between arms (for example user-interactive before background) can produce the
  entire apparent effect. Arms must be interleaved or randomized and the order
  recorded.
- Background host load — indexing, backup, update daemons, and the harness
  itself — changes coalescing behavior. Because coalescing depends on other
  processes' timers, an uncaptured environment makes the run irreproducible;
  load and thermal state must be captured with the run.
- A busy-wait arm changes core residency and power state and is a control, not
  a candidate mechanism; labelling it otherwise would smuggle an inadmissible
  design into a capability result.
- Wall-clock latency of a descheduled thread measures the scheduler, not the
  timer. The two must be separated, or attributed jointly and said so.
- Sampling QoS classes is not controlling core placement. Any statement about P
  versus E cores from a QoS-sampled run is an inference with stated uncertainty,
  never an observation of placement.
- This question produces capability evidence only. Under the `Q-05-01` staged
  progression, no number here is a performance result or a comparison, and none
  may be carried into a `Q-00-04` aggregate.

## Backlog updates

Issues surfaced by this pass that an existing question already owns are mapped
rather than duplicated, after checking the Scope, Platform, Correctness,
Architecture, Performance, Benchmarking, Integration, and Deferred-security
tables:

| Surfaced issue | Owner |
| --- | --- |
| Admissibility of each named clock, wakeup, and signalling API, and of any non-public wait primitive | `Q-01-01` |
| Unprivileged public backing for the timer/APIC device class | `Q-01-09` |
| Guest-visible timer, APIC-mode, and ACPI-table contract, including boot-time TSC calibration against a second timer | `Q-01-13` |
| Invariant-TSC and clocksource-related CPUID/MSR advertisement | `Q-02-03` |
| Coexistence of asynchronous interrupt signals with the synchronous fault mechanism | `Q-01-06`, `Q-01-12` |
| Host wakeup and signal rate as a cost and scaling input | `Q-04-01` |
| Energy, thermal, and placement instrumentation validity for the above | `Q-05-08`, `Q-05-06` |
| Sampling cadence and enforcement window feasibility for the containment oracle | `Q-01-14`, `Q-02-04` |
| SMP ordering of the state an interrupt hands between threads | `Q-01-08` |

Two issues have no existing owner. This pass creates them in the Platform
table:

- `Q-01-21` — Which host power-state and background-execution transitions
  (system sleep/wake, App Nap, Low Power Mode, thermal throttling, and
  background QoS demotion) can a running emulator instance experience on the
  supported hosts, and what liveness, timekeeping continuity, and guest-visible
  correctness guarantees survive each?
- `Q-01-22` — Which guest time-base policies (host-monotonic-slaved,
  rate-scaled, or virtual/execution-driven) are admissible given the host clock
  and wakeup facilities, and what does each imply for guest timeout-sensitive
  code, correctness oracles, and the validity of in-guest performance
  measurement?

No claim, source, method, or result work was performed during this pass.

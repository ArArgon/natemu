# BP-01-06-start — exception delivery and restartability breadth pass

## Metadata

- Breadth-pass ID: `BP-01-06-start`
- Question: `Q-01-06`
- Date: 2026-09-03
- Executor/session: Claude Code authoring subagent `t01_memory_author`

This pass organizes questions and establishes no evidence or conclusion. Host
mechanisms named below must be established from Apple documentation, macOS
manual pages, or the versioned SDK headers, or probed under a reviewed method;
guest exception semantics come from the Intel SDM at a stated revision and are
owned by `Q-02-03`.

## Coverage

### Product assumptions

- An unmodified guest requires precise, restartable exceptions: on a fault the
  guest sees an architecturally correct exception vector, error code, faulting
  address, and a saved instruction pointer from which execution resumes with no
  partially applied side effects. This question owns whether the **host** can
  deliver a fault to the emulator with enough state and let it resume; it does
  not own the guest exception manifest (`Q-02-03`) or the correctness tests
  (`Q-02-01`).
- **Restartability has two halves and only one is a host property.** (a) Can
  the host hand over full register state and honor a modified resume PC and
  register set? (b) Does the generated code leave no partially committed guest
  state at the fault point? (b) is a property of the translation's commit
  discipline, not of macOS; a "yes" on (a) must not be reported as precise
  exceptions. Routed to `Q-02-01` and `Q-01-12`.
- **Fault-cause recovery is a separate requirement from fault delivery.** To
  synthesize a guest `#PF` the emulator needs the faulting guest address, the
  access type (read/write/execute/user/supervisor), and enough to restart the
  access. If the host delivers only a faulting address, the handler must decode
  the faulting arm64 instruction to recover access type, width, and destination
  register — a per-family burden that must be surfaced now, not discovered in a
  prototype.
- Question the assumption that host faults are needed at all. Explicit checks in
  generated code raise no host exception; that family trades throughput for not
  depending on this question, and the kill-assumption register admits it
  (`KA-04`).

### Adjacent systems

- The candidate mechanisms are POSIX signals with `ucontext_t`, Mach exception
  ports, and no-host-fault software checks. Primary sources: `sigaction(2)`,
  `<sys/ucontext.h>` and `<sys/_types/_ucontext.h>`, `<mach/arm/_structs.h>`
  (`_STRUCT_ARM_THREAD_STATE64` with `__pc`, `__sp`, `__x[]`;
  `_STRUCT_ARM_EXCEPTION_STATE64` with `__far` and `__esr`, and the `_V2`
  variant that widens `__esr`), `<mach/exc.h>`, `<mach/task.h>`
  (`task_set_exception_ports`), and any Apple documentation on Mach exceptions.
- The SDK header itself raises two structural questions. First, there are two
  `_STRUCT_ARM_EXCEPTION_STATE64` variants (the `_V2` form widens `__esr`):
  which one a handler receives, and whether the choice is stable across the two
  supported macOS releases. Second, `_STRUCT_ARM_THREAD_STATE64` has an opaque
  form selected by `__DARWIN_OPAQUE_ARM_THREAD_STATE64`, which the header ties
  to the pointer-authentication feature; in that form `__pc`, `__sp`, `__lr`,
  and `__fp` are opaque and must be reached through accessor macros. Whether
  resuming at a *chosen* PC requires producing a correctly signed pointer, and
  what a handler may legally construct, is a first-order question for
  restartability on Apple Silicon and must be established before any resume
  probe is written.
- An adjacency worth asking explicitly: much of what a handler needs (`__esr`
  contents, `si_code` values, whether `ucontext` writeback is honored) is
  exposed by public headers but may be thinly documented. Whether a
  header-exposed, undocumented behavior is admissible at all, and how drift
  across macOS releases would be detected, is an admissibility-policy question
  owned by `Q-01-01`, with the recurring detection cost owned by `Q-06-03`.

### Alternatives

- Signals: per-thread delivery, `SA_SIGINFO` for `si_addr`/`si_code`,
  `sigaltstack` per vCPU thread, `ucontext_t` for state, return-from-handler as
  the resume mechanism.
- Mach exception ports: a catch server thread receiving `EXC_BAD_ACCESS` and
  friends, replying with a thread state to resume. Different concurrency shape
  (one server, N faulting threads) and different failure modes.
- Both together: the ordering between a Mach exception and the signal that the
  kernel would otherwise synthesize must be established, since a handler
  installed for one may never see faults consumed by the other.
- Explicit software checks with no host fault, for families that reject lazy
  faults entirely.
- A hybrid: software checks on the paths where fault frequency is high, host
  faults on the rare paths.

### Negative and boundary cases

- Fault classes to exercise separately: unmapped access (`SIGSEGV`),
  protection violation, `SIGBUS` (macOS distinguishes these and the
  distinction may carry information the handler needs — or may not be stable),
  undefined instruction (`SIGILL`), `brk` (`SIGTRAP`), and division/FP traps.
  A scheme that handles one and assumes the rest is untested.
- A `SIGBUS` from a truncated file-backed mapping is a *host* failure, not a
  guest fault; a handler that cannot distinguish host from guest faults will
  either mask real bugs or synthesize spurious guest exceptions. Misclassifying
  must fail loudly — silent resumption is a containment failure under
  `INV-00-08`.
- Faults inside the handler; nested faults; faults while the thread holds JIT
  write permission (see the proposed row below); faults while another vCPU
  thread is mid-`mprotect`.
- Async-signal-safety: anything the handler does (allocation, locks, logging,
  re-protection) must be admissible in a signal context, and the emulator's
  handler will want to do all of them.
- Delivery under a debugger (`lldb`) or with the crash reporter interposed
  changes which faults reach the process at all; this affects whether the
  toolchain can debug the product (`Q-01-10`).
- A handler that resumes successfully but with wrong register or memory state:
  the probe must verify post-resume state, or it cannot tell a working resume
  from a silently corrupting one.
- Masked or blocked signals, signal delivery during `fork`/`posix_spawn`, and
  threads not owned by the emulator (library threads) taking faults.

### Host and guest matrix

- `P1` (M1 Pro / Sequoia 15) and `P2` (M4 / Tahoe 26); SDK 26.4 on the
  available host. Structure layouts and `si_code` semantics are per-release
  facts and must be re-observed on each pair, not generalized.
- Dimensions: fault class; delivery mechanism (signal, Mach port, software
  check); thread count 1, 2, 4, 8; signing configuration and hardened runtime
  (whether entitlements affect exception-port operations on the process's own
  task must be established, not assumed); debugger attached or not.
- Guest dimension enters only through which x86 exceptions must be synthesized,
  which is `Q-02-03`'s manifest.

### Multicore interactions

- Simultaneous faults on several vCPU threads must each be delivered to the
  faulting thread with that thread's own state. Signals are per-thread by
  construction; a Mach exception server is a single point that serializes N
  faulting threads and whose latency under concurrency must be measured, not
  assumed.
- Guest exception delivery must interleave correctly with guest interrupt
  delivery (`Q-01-07`) and with cross-vCPU invalidation (`Q-01-05`,
  `Q-01-18`): an exception taken during a pending invalidation or interrupt
  must produce an architecturally admissible ordering.
- A handler that needs to stop other vCPUs (to patch code or re-protect a page)
  can deadlock against another vCPU's handler doing the same. The quiescence
  protocol is `Q-01-18`; the deadlock risk is why it must exist before the
  prototype.
- Per-thread `sigaltstack` allocation scales with vCPU count and consumes the
  virtual-address budget of `Q-01-03`.

### Falsifiers and stop paths

- Primary kill assumption: **`KA-04`** — a fault raised inside generated code
  can be caught with full register state and the vCPU resumed at a chosen PC
  (signal/`ucontext`, Mach exception, or explicit checks). Falsified by: no
  mechanism delivering the faulting PC and full register set; a resume path
  that ignores modified state; or delivery that is not per-thread-correct under
  concurrency.
- Contributing: **`KA-07`** (a protection-fault tracking scheme depends
  entirely on `KA-04`), **`KA-01`** (faults arising inside `MAP_JIT` regions),
  and **`KA-11`** (composition).
- Disposition, per the kill-assumption register: `KA-04` failing kills families
  that rely on lazy faults; explicit-check families survive **only if**
  `Q-01-12` shows composition. A project stop input 3 arises only if no family
  survives.
- Predeclared non-kill: high fault latency is a cost finding for `Q-04-01`, not
  a kill before `Q-04-02`. Precision failures are different — an imprecise or
  unrestartable fault is a correctness impossibility, not a cost.

### Measurement failure modes

- "Fault latency" measured from the faulting instruction to the first line of
  the handler excludes the return path and the emulator's classification work;
  measured end-to-end it includes bookkeeping the design might avoid. The
  interval must be defined before the run and reported with its definition.
- Single-threaded latency is not the SMP latency, and a Mach-port server's
  latency is expected to degrade with concurrent faulters in a way a
  single-thread number cannot show.
- The tail is the product-relevant statistic: a p99 fault latency spike shows up
  as guest timer drift and missed interrupt deadlines. Median-only reporting
  hides exactly the failure this question exists to find.
- Handler code paths, alternate stacks, and any lazily bound symbol are cold on
  the first faults; warm-up must be declared.
- An attached debugger, a crash-reporter interposition, or a different signing
  state changes delivery; the environment capture must record all three.
- Timer validity (`Q-05-08`), P/E-core placement (`Q-05-06`), and thermal drift
  apply as elsewhere.
- A resume probe with no post-resume state verification, and no negative
  control that a defect would fail, measures nothing about precision.

## Backlog updates

One issue surfaced that no existing row owns. `Q-01-04` owns obtaining
executable memory and per-transition cost; `Q-01-06` owns fault delivery and
resumption by fault class and mechanism; neither owns what happens when the two
meet — whether the per-thread JIT write-protection state is preserved across
signal or Mach-exception delivery and across the return path, and what handler
discipline is admissible when the faulting thread was mid-code-patch. It is
filed in the reserved Track 01 range as `Q-01-19` (row text below; the main
session applies it — this pass does not edit the backlog).

| Identifier | Question | Why it matters | Dependencies | Downstream decisions | Applicability dimensions | Resolution method | Evidence status | Review status | Newly surfaced questions — breadth pass | Newly surfaced questions — source/method/execution/result work |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `Q-01-19` | Is per-thread JIT write-protection state preserved across host fault delivery and return, and what fault-handler discipline (async-signal-safety, alternate stacks, nested faults, mid-patch faults, host-versus-guest fault classification) is admissible for a handler that may run while a vCPU thread holds write permission or is patching live code? | A translation family composes executable-memory management with fault handling on every vCPU thread; if the W^X state is not preserved across delivery or return, or the handler cannot safely act mid-patch, a mechanism that passes `Q-01-04` and `Q-01-06` separately still fails when combined, and the failure surfaces as silent corruption rather than a crash. | `Q-01-04`, `Q-01-06`, `Q-01-18` | Translation-family admissibility, prototype method (`Q-01-12`), containment classification of unclassifiable faults (`Q-02-04`) | Host generation / macOS and SDK / delivery mechanism (signal, Mach port) / fault class / thread count / signing and entitlement configuration | Apple documentation and SDK header analysis plus controlled fault-during-write, fault-during-patch, nested-fault, and state-preservation probes | unstarted | proposed | `BP-01-06-start` | no new issues found |

Remaining surfaced issues are routed to existing rows: the guest exception,
`INVLPG`/`CR3`, and error-code manifest to `Q-02-03`; the generated-code commit
discipline that makes a fault restartable, and its tests, to `Q-02-01` and
`Q-01-12`; admissibility of header-exposed but thinly documented behavior and
its cross-release drift to `Q-01-01`, with recurring detection cost to
`Q-06-03`; debugger and crash-reporter interference with delivery to `Q-01-10`;
loud failure on an unclassifiable fault to `Q-02-04`; interrupt-delivery
interleaving and cross-thread notification latency to `Q-01-07` and `Q-01-08`;
per-thread stack and structure budget to `Q-01-03` and `Q-01-14`; fault
latency as a performance component to `Q-04-01` and its kill eligibility to
`Q-04-02`; and instrument, placement, and thermal validity to `Q-05-08` and
`Q-05-06`.

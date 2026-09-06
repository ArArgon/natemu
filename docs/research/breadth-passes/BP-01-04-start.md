# BP-01-04-start — translation and executable-memory breadth pass

> Historical question-opening coverage. Findings and current work belong to
> [INV-01-04](../investigations/INV-01-04.md); this pass is not a live status
> record and new work need not update it.


## Metadata

- Breadth-pass ID: `BP-01-04-start`
- Question: `Q-01-04`
- Date: 2026-09-03
- Executor/session: Claude Code authoring subagent `t01_memory_author`

This pass organizes questions and establishes no evidence or conclusion. Every
API, entitlement, and mechanism named below is named as something to establish
from Apple documentation, macOS manual pages, or the versioned SDK headers, or
to probe under a reviewed method — never as a fact, and never as a design.

## Coverage

### Product assumptions

- **The measured artifact must match the shipped artifact.** `INV-00-05`
  retains a deployment boundary of routine operation without root, without an
  installed privileged helper, and without private APIs or undeclared
  entitlements. A capability observed on an ad-hoc-signed command-line binary
  built locally does **not** transfer to a Developer ID-signed, hardened-runtime,
  notarized distribution without separate evidence. Signing configuration is a
  first-class applicability dimension of every claim under this question, and
  any result that omits it is inapplicable to the product.
- Question the assumption that "the project needs a JIT". It needs executable
  generated code only if a translation family is selected. `F1`
  (interpretation) does not depend on this question at all; that asymmetry must
  be stated so a failure here is not over-read as a project stop.
- Question the assumption that one write/execute transition cost characterizes
  the design. Cost depends on transition frequency, region size, thread count,
  and whether the transition is per-block, per-batch, or per-safepoint. A
  single toggle latency is an input to a cost model (`Q-04-01`), not an answer.

### Adjacent systems

- Browsers, managed-language runtimes, and existing emulators all generate code
  on this platform; they locate the mechanism but cannot be cited. The
  candidate primary sources are Apple's "Porting just-in-time compilers to
  Apple silicon" documentation (captured as its JSON content rendition, since
  the HTML is a script shell), the Hardened Runtime entitlement documentation
  for `com.apple.security.cs.allow-jit` and its neighbours, `mmap(2)`, and the
  SDK headers `<sys/mman.h>` (`MAP_JIT`), `<pthread.h>`
  (`pthread_jit_write_protect_np`, `pthread_jit_write_protect_supported_np`,
  `pthread_jit_write_with_callback_np`, `pthread_jit_write_freeze_callbacks_np`)
  and `<libkern/OSCacheControl.h>` (`sys_icache_invalidate`).
- The `<pthread.h>` header text itself refers to an entitlement whose adoption
  *disallows* use of the toggle. Which entitlement, and what the resulting
  behavior is, must be established rather than inferred from the header comment.

### Alternatives

- `MAP_JIT` region plus the per-thread write-protect toggle.
- Write-side callback form (`pthread_jit_write_with_callback_np` /
  `..._freeze_callbacks_np`), which constrains *where* writes may happen rather
  than toggling a thread-global state; its interaction with a multi-threaded
  emulator is different and must be questioned separately.
- Dual mapping: two host virtual aliases of the same physical pages, one RW and
  one RX (`mach_vm_remap`). Whether this is permitted, and under which signing
  and entitlement configuration, is exactly the kind of thing that must be
  sourced rather than assumed to work because it works elsewhere.
- Ahead-of-time translation into a signed dylib loaded at run time (`F4`),
  which converts the problem from W^X into a code-signing and lifecycle problem
  — including whether a distributed product may sign artifacts on the user's
  machine at all.
- No generated code (`F1`), or generated code that never needs re-writing
  (write once, freeze, never patch), which trades translation quality for
  avoiding every transition cost.
- A separate writer thread or writer process that owns all code-cache writes.

### Negative and boundary cases

- `MAP_JIT` allocation limits: whether 256 MiB, 1 GiB, and 4 GiB regions are
  obtainable, what fails and with which `errno`, and whether a large JIT
  reservation competes with the guest-RAM reservation of `Q-01-03` for the same
  virtual-address budget. The two must be probed against the same budget.
- `pthread_jit_write_protect_supported_np` returning false: a host or
  configuration where the toggle does not exist at all is a boundary case, not
  an error, and the fallback path must be identified.
- Writing to the region without toggling: the expected outcome is a fault. That
  fault is simultaneously a negative control here and a data point for
  `Q-01-06` (which signal, what `si_addr`, is it resumable).
- Executing freshly written code without `sys_icache_invalidate`: the failure is
  *intermittent stale execution*, which a smoke test passes and a real workload
  fails. Any probe that does not deliberately construct the stale case has not
  tested it.
- Patching code that is currently executing (branch chaining, invalidation
  stubs, self-modifying translated code) while other vCPU threads are inside it.
- Signing configurations that must each be treated as a distinct cell:
  unsigned, ad hoc, ad hoc plus entitlements, Developer ID plus hardened
  runtime, notarized, and the App Sandbox case from `INV-00-08`.
- Toolchain boundary: `codesign` and `codesign -d --entitlements` must exist on
  a Command Line Tools-only host with no Xcode, or the evidence cannot be
  produced at all (`Q-01-10`).

### Host and guest matrix

- `P1` (M1 Pro / Sequoia 15) and `P2` (M4 / Tahoe 26); SDK 26.4 on the
  available host. `MAP_JIT` and the toggle have documented availability
  histories that may be cited across the matrix; observed toggle *cost* is
  per-host only.
- Explicit dimensions: signing configuration; hardened runtime on/off;
  entitlement set; thread count (1, 2, 4, 8, matching the vCPU topologies);
  region size; transition frequency; P-core versus E-core placement.
- The guest dimension is weak here — generated code is host code — but guest
  code density (basic-block size, self-modifying frequency) drives transition
  frequency and therefore belongs in the cost model, not in this question.

### Multicore interactions

- The write-protect toggle is documented as a **per-thread** facility. With 2 to
  8 vCPU threads sharing one code cache, ask: while thread A holds the region
  writable, can thread B execute from it, or does B fault? Both answers have
  consequences — if B faults, every write serializes the machine; if B executes,
  B may execute a half-written block.
- Instruction-cache coherence is not implied by store visibility on this
  architecture. Ask what makes generated code visible *and* fetch-coherent to a
  different core: whether `sys_icache_invalidate` on the writer thread suffices
  for other cores, and what memory ordering must accompany publication.
- Patching live code while other vCPUs execute it needs a quiescence or
  safepoint discipline, and so does any process-wide mapping change from
  `Q-01-05`. Nothing in the current backlog owns that protocol; see the backlog
  updates below.
- Toggle cost measured on one thread is not the cost under contention, and a
  per-thread toggle may still hit shared kernel state.

### Falsifiers and stop paths

- Primary kill assumption: **`KA-01`** — an unprivileged process signed ad hoc
  or with Developer ID, using only public APIs, can obtain memory that is
  alternately writable and executable on the supported hosts. Falsified by: no
  public path in the *distributed* signing configuration; a required
  entitlement that is unavailable to the project's distribution model; or a
  path that exists only with root or an installed privileged helper.
- Contributing: **`KA-09`** (the signing/inspection toolchain must exist on the
  supported hosts) and **`KA-03`** (shared virtual-address budget).
- Disposition: `KA-01` failing kills every translation family
  (`INV-00-06` family-kill item 1, private API / unavailable entitlement /
  prohibited distribution condition) and is a project stop input 1 **only if no
  registered family survives**. `F1` survives `KA-01` by construction, so a
  `KA-01` failure must not be reported as a project stop unless `F1` is
  separately killed.
- A weaker falsifier worth predeclaring: the mechanism works but the transition
  cost is so high that no plausible transition frequency is affordable. That is
  *not* a kill at this track — cost routes to `Q-04-01` and cannot kill a family
  before the `Q-04-02` maturity predicate is accepted.

### Measurement failure modes

- A toggle measured in a tight loop measures the loop, not the emulator. The
  amortized cost depends on transition frequency, which this question cannot
  choose; report cost per transition *and* the frequency assumptions separately.
- Timebase: `mach_absolute_time` ticks require `mach_timebase_info` conversion,
  and the timer itself must be validated (`Q-05-08`) before its numbers mean
  nanoseconds.
- Warm-up: first-touch of JIT pages, icache and branch-predictor cold state,
  and first-call lazy binding are all in the first iterations.
- P-core versus E-core placement changes both toggle latency and executed-code
  throughput; unpinned threads migrate. Placement is `Q-05-06`.
- Thermal drift over a long iteration count biases late samples.
- Measuring a debug build, or a binary whose signing state differs from the one
  described in the record, invalidates the applicability rather than the number.
  The manifest must capture `codesign -d --entitlements -` of the exact binary.
- A single-thread measurement presented as the multi-vCPU cost is the specific
  overreach this question is most likely to commit.

## Backlog updates

One issue surfaced that no existing row owns. `Q-01-04` owns the mechanism and
the per-transition cost (its applicability dimensions include thread count, but
its question text is about obtaining executable memory and the cost of a
transition); `Q-01-05` owns tracking of *guest* memory modification; `Q-01-08`
owns representing *guest* SMP ordering and cross-vCPU events. None of them owns
the host-side protocol by which one thread publishes or patches code and
mappings that other concurrently executing vCPU threads are using. It is filed
in the reserved Track 01 range as `Q-01-18` (row text below; the main session
applies it — this pass does not edit the backlog).

| Identifier | Question | Why it matters | Dependencies | Downstream decisions | Applicability dimensions | Resolution method | Evidence status | Review status | Newly surfaced questions — breadth pass | Newly surfaced questions — source/method/execution/result work |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `Q-01-18` | What host-side cross-thread protocol makes a shared code cache and process-wide mapping changes safe while other vCPU threads execute — code publication and instruction-cache coherence, live-code patching, and quiescence/safepoint entry and exit — and at what latency and cost? | Per-thread write protection, process-wide mapping changes, and a shared code cache compose into a concurrency protocol that no mechanism question owns; without it a translation family that works on one thread can be unsound or unusable at 2–8 vCPUs. | `Q-01-04`, `Q-01-05`, `Q-01-06` | Translation-family admissibility under SMP, prototype method (`Q-01-12`), invalidation and translation cost model | Host generation / macOS and SDK / thread count (1, 2, 4, 8) / core placement / code-cache size / patch and invalidation frequency | Apple documentation, Arm architecture specification at a pinned revision, and SDK header analysis plus controlled multi-thread publication, patching, and quiescence probes | unstarted | proposed | `BP-01-04-start` | no new issues found |

Remaining surfaced issues are routed to existing rows: distribution form,
hardened runtime, notarization, and entitlement admissibility to `Q-01-01`
(with the deployment boundary in `Q-00-05` and licensing in `Q-01-11`);
`codesign` availability without Xcode to `Q-01-10`; the virtual-address budget
shared with guest RAM to `Q-01-03` and `Q-01-14`; transition cost as a
performance component to `Q-04-01` and its kill eligibility to `Q-04-02`;
timer, placement, and thermal instrument validity to `Q-05-08` and `Q-05-06`;
and the fault raised by writing without toggling to `Q-01-06`.

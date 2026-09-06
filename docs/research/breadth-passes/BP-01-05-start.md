# BP-01-05-start — paging, invalidation, and modification-tracking breadth pass

> Historical question-opening coverage. Findings and current work belong to
> [INV-01-05](../investigations/INV-01-05.md); this pass is not a live status
> record and new work need not update it.


## Metadata

- Breadth-pass ID: `BP-01-05-start`
- Question: `Q-01-05`
- Date: 2026-09-03
- Executor/session: Claude Code authoring subagent `t01_memory_author`

This pass organizes questions and establishes no evidence or conclusion. Guest
architectural requirements named below must be established from the Intel SDM
(or AMD APM) at a stated revision, not from an emulator's behavior; host
mechanisms must be established from Apple documentation, macOS manual pages, or
the versioned SDK headers, or probed under a reviewed method.

## Coverage

### Product assumptions

- The accepted boundary requires four-level paging with 4 KiB and 2 MiB pages,
  precise exceptions, and SMP as a baseline. An unmodified guest writes its own
  page tables at arbitrary times, expects x86 TLB semantics (permitted implicit
  caching, `INVLPG`, `MOV CR3`, `PGE` global pages, and the ordering rules that
  make those visible to other logical processors), and may execute code it has
  just written. None of this is optional.
- Question the assumption that "TLB invalidation" is one problem. It is at
  least three: (a) invalidating cached guest-virtual-to-physical translations;
  (b) invalidating *translated code* whose validity depended on the guest
  mapping or on the guest bytes; (c) making both invalidations visible to other
  vCPU threads at the point the guest architecture requires. A mechanism that
  solves (a) and not (b) or (c) is not a solution.
- Question the assumption that host-page protection is the tracking mechanism.
  It is one candidate family; explicit software checks in generated code are
  another, and the kill-assumption register (`KA-07`) deliberately admits both
  at different granularities (16 KiB hardware, 4 KiB software).
- Cost is in scope, kills are not. High tracking cost feeds `Q-04-01` and can
  kill a family only after the `Q-04-02` maturity predicate is accepted.

### Adjacent systems

- Software MMU designs (per-vCPU translation caches keyed on guest virtual
  address), invalidate-on-write code caches, and hardware second-stage
  translation inside a hypervisor are the adjacent solution space. They may
  locate mechanisms; the guest-visible requirement must come from the SDM at a
  pinned revision (Vol. 3, the caching-of-translation-information and
  `INVLPG` sections) or from Linux kernel source at a tag, whichever can be
  captured and quoted verbatim.
- Host-side adjacency: `mprotect(2)`, `mach_vm_protect`, `madvise(2)`,
  `mincore(2)`, `sigaction(2)`, and whether any public macOS facility reports
  page dirtiness without a fault (the absence of one is itself a finding worth
  establishing).

### Alternatives

- Protection faults: map guest RAM read-only, take a host fault on the first
  guest write, record the page, re-protect, resume. Granularity 16 KiB; cost is
  a fault round trip plus two protection changes.
- Explicit checks in generated code: every guest store consults or sets a
  software dirty bitmap. Granularity 4 KiB (or finer); cost is inline and paid
  on every store whether or not anything is dirty.
- Invalidate-on-write at translation time: the translated store path itself
  checks whether the target page holds translated code.
- Coarse invalidation: any write to a page containing translated code discards
  all translations for that page (or more). Cheap to implement, unbounded
  re-translation cost under a guest that writes near its code.
- Hardware-assisted dirty tracking inside a Hypervisor.framework VM (`F3`),
  which moves the question to whether that framework exposes such tracking
  through public API at all.
- Hybrid: coarse host-page tracking as a filter, software checks to resolve the
  4 KiB sub-page within a dirtied 16 KiB host page.

### Negative and boundary cases

- **Granularity mismatch.** One 16 KiB host page covers four 4 KiB guest pages.
  A write to one guest page dirties three innocent neighbours (false sharing),
  and a protection change intended for one guest page affects three others.
  Whether a sub-16 KiB `mprotect` is rejected or silently rounded — and in which
  direction — decides whether this is a cost problem or a correctness problem.
- A guest page table and translated-code bytes sharing one host page: writing
  the page table triggers code invalidation and vice versa.
- Writes that straddle a page boundary, unaligned writes, and writes performed
  by the emulator itself (device DMA, loader, debugger) that bypass the
  translated store path and therefore bypass software checks.
- A fault taken while the thread holds JIT write permission, or inside another
  fault handler (nested faults, handler re-entry) — see `BP-01-06-start`.
- Fault storms: a guest that touches millions of distinct pages (boot, page
  cache fill, `memset` of guest RAM) turns first-write tracking into a
  first-touch storm whose cost is not the steady-state cost.
- Re-protect races: a handler that re-protects a page another vCPU thread is
  mid-write on; and the reverse, a page unprotected by one thread while another
  relies on it being protected.
- A guest mapping the same physical page at many virtual addresses, and a guest
  using `PGE` global pages that survive a `CR3` reload.
- Reclaim interactions: whether `madvise(MADV_FREE_REUSABLE)`, purgeable
  memory, or compressor activity resets protection or residency state that the
  tracking scheme depends on.
- Silent failure is the worst case: a missed dirty page produces stale
  translated code, which is a wrong-answer correctness failure, not a crash.
  Any probe must have a positive control that would catch a *missed*
  notification, not only one that catches a spurious one.

### Host and guest matrix

- `P1` (M1 Pro / Sequoia 15) and `P2` (M4 / Tahoe 26); SDK 26.4 on the
  available host. The 16 KiB host page must be observed per host, not assumed
  from the Apple Silicon family.
- Guest dimensions: 4 KiB and 2 MiB page sizes; four-level paging; `PGE` on and
  off; guest RAM 2 GiB and 8 GiB; 2, 4, and 8 vCPUs.
- Workload dimension: tracking cost is entirely workload-shaped (boot versus
  steady state versus a self-modifying-code workload). A single synthetic write
  pattern characterizes nothing; the pattern must be declared with the result.

### Multicore interactions

- **TLB shootdown analogue.** On x86 the invalidating processor must ensure
  other logical processors observe the invalidation before it relies on it; the
  guest kernel implements this with IPIs. The emulator must provide an
  equivalent across vCPU threads, which needs a cross-thread notification with
  bounded latency (`Q-01-07`) and a quiescence protocol (`Q-01-18`).
- The host has no per-thread page table, so `mprotect` is process-wide: one
  vCPU thread's tracking operation changes what every other vCPU thread sees,
  immediately and without asking. This is simultaneously convenient (no
  shootdown needed for the host mapping itself) and dangerous (a vCPU can fault
  because of another vCPU's bookkeeping, and must distinguish that from a real
  guest fault).
- Mapping operations plausibly serialize on a process-wide VM lock; a
  protection-fault tracking scheme may therefore not scale with vCPU count at
  all. Concurrent-thread fault and `mprotect` cases are mandatory, not optional.
- Two threads faulting on different pages concurrently, and two threads faulting
  on the *same* page concurrently, are different tests with different failure
  modes (the second can double-handle or lose a notification).

### Falsifiers and stop paths

- Primary kill assumption: **`KA-07`** — modification of guest memory,
  including self-modifying code, can be tracked by public mechanisms at host
  page granularity (16 KiB) or by software checks at guest page granularity
  (4 KiB), at bounded cost. Falsified by: no mechanism that reports
  modification without unbounded cost; or a mechanism that misses modifications
  (unsound at any cost).
- Depends on **`KA-03`** (representation and page-size relationship) and
  **`KA-04`** (a protection-fault scheme is worthless if faults are not
  deliverable and resumable). Contributes to **`KA-11`** (composition).
- Disposition: failure kills only the mechanisms that need finer hardware
  tracking; software-check families survive and must then be shown to compose
  (`Q-01-12`). A project stop input 3 arises only if *no* family can honor guest
  paging and invalidation.
- Predeclared non-kill: a high per-fault or per-`mprotect` cost is a cost
  finding. It routes to `Q-04-01` and cannot kill a family before `Q-04-02`.

### Measurement failure modes

- Single-threaded fault round-trip time is a lower bound on the SMP cost and
  must never be reported as the cost. The SMP case is the product case.
- Median-only reporting hides the failure mode that matters: a p99 fault
  latency spike delays guest timer and interrupt deadlines (`Q-01-07`). Report
  median and tail with the sample count.
- `mprotect` cost depends on VM-map fragmentation and on how many entries the
  process already has; a microbenchmark on one clean region under-reports the
  cost in a running emulator, monotonically and in the flattering direction.
- First-touch commit faults mixed into a "write-tracking fault" sample measure
  the wrong thing entirely.
- Handler cost accounting: whether the measured interval includes the handler's
  own bookkeeping, the `mprotect` calls, and the return path must be declared
  before the run; each choice yields a different number and all three get called
  "the fault cost".
- Timer validity, P/E-core placement, thermal drift, and the memory compressor
  apply here exactly as in `BP-01-03-start`; results must record which cores ran.
- A tracking probe with no missed-notification control cannot distinguish a
  cheap mechanism from a broken one.

## Backlog updates

This pass surfaced no issue that an existing backlog row cannot own, and it
creates no new question identifier. `Q-01-18` (proposed in `BP-01-04-start`)
already covers the cross-thread quiescence and publication protocol that the
invalidation path needs; this pass adds a second, independent reason for that
row rather than a second row. Routing:

| Surfaced issue | Owning question |
| --- | --- |
| Cross-vCPU quiescence for a process-wide protection change; invalidation visibility protocol | `Q-01-18` (proposed), `Q-01-08` |
| Cross-thread notification latency for an invalidation IPI analogue | `Q-01-07` |
| Fault delivery, precision, and resumability that a protection-fault scheme depends on | `Q-01-06` |
| Page-size relationship, aliasing, and reclaim interactions with tracking state | `Q-01-03` |
| Guest-visible `INVLPG`/`CR3`/`PGE`/exception semantics to be enumerated | `Q-02-03` |
| SMP litmus and shared-page-table-mutation correctness evidence | `Q-02-02` |
| Tracking and invalidation as performance components and scaling failure modes | `Q-04-01`; kill eligibility `Q-04-02` |
| Workload-shaped write patterns and their pinned corpus | `Q-05-03` |
| Timer, placement, and thermal instrument validity | `Q-05-08`, `Q-05-06` |
| VM-map entry count as a per-instance kernel resource budget | `Q-01-14` |

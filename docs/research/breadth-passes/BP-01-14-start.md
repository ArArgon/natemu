# BP-01-14-start — per-instance resource budgets breadth pass

## Metadata

- Breadth-pass ID: `BP-01-14-start`
- Question: `Q-01-14`
- Date: 2026-09-03
- Executor/session: Claude Code authoring subagent `t01_machine_author`

This pass organizes questions and establishes no evidence or conclusion.

## Coverage

- **Product assumptions:** `Q-01-14` must propose finite CPU, memory, storage,
  descriptor, subprocess, and network budgets that preserve the containment
  properties `INV-00-08` already defined as an oracle contract, without
  excluding any workload `INV-00-01` accepted as mandatory. This is a limit
  *proposal* grounded in that oracle and those workloads, reviewed through
  exhaustion/fault experiments — not a design choice of sandbox architecture
  (`Q-01-01`/`Q-01-02` territory) and not the fault-injection method itself
  (`Q-02-04`'s job, which consumes these numbers).
- **Adjacent systems and alternatives:** macOS has no cgroups-style hierarchical
  resource-control facility; the candidate public mechanisms are `setrlimit`
  (per-process, inherited by children, `SRC-035`), `dispatch_source`
  `DISPATCH_SOURCE_TYPE_MEMORYPRESSURE` (host-wide pressure-level
  notification, not a per-process cap), and `task_info(TASK_VM_INFO)`
  self-accounting (`phys_footprint` and related fields) for a process to
  measure and self-enforce a budget it cannot get the kernel to enforce
  directly. These are complementary, not substitutable: `setrlimit` supplies
  hard enforcement for the limits it covers, the memory-pressure source
  supplies host-wide early warning, and `task_info` self-accounting is the
  only path to a budget `setrlimit` does not cover at all (there is no
  per-process resident-set enforcement rlimit on Darwin distinct from
  `RLIMIT_RSS`'s documented non-enforcement, and no process-count `setrlimit`
  equivalent to Linux's `RLIMIT_NPROC` is assumed present without checking the
  local `<sys/resource.h>`). Conflating a host-wide pressure notification with
  a per-instance memory cap would misstate what the mechanism actually
  enforces, and Phase B's method must keep the two separate.
- **Negative and boundary cases:** `RLIMIT_AS`/`RLIMIT_DATA` enforcement on
  Darwin's `mmap`/`malloc` path is a candidate boundary case rather than an
  assumed fact — historical Darwin behavior has not enforced every BSD-
  inherited rlimit the way Linux does, so Phase B's probe must attempt an
  over-limit `mmap`/`malloc` and record the actual outcome rather than assume
  it fails. `RLIMIT_CPU` is documented to raise `SIGXCPU`, but is process-wide
  (summed across all threads), so an 8-vCPU guest busy-looping on every vCPU
  thread accumulates CPU-seconds roughly 8x faster than wall-clock time — a
  workload-informed CPU-seconds budget must be stated per unit of wall-clock
  time and vCPU count, not as a bare CPU-seconds constant, or it will
  systematically under-budget high-vCPU-count guests relative to
  low-vCPU-count ones for the same wall-clock allowance. `RLIMIT_NOFILE`
  exhaustion (`EMFILE`) is a boundary case for the descriptor budget; Mach
  ports used internally by `vmnet.framework`/XPC-backed APIs (`Q-01-09`) are a
  distinct resource class from POSIX file descriptors and may need their own
  accounting if any chosen device backing consumes them at scale.
- **Host and guest matrix:** this session's only host is 16 GiB RAM / 10 cores
  (4 P + 6 E); the brief specifies budgets must be checked against an 8 GiB
  guest with 8 vCPUs on exactly this host. An 8 GiB per-instance memory budget
  leaves roughly 8 GiB for host OS, supervisor, and any concurrent peer
  instance; `INV-00-08`'s oracle contract requires a clean peer process to
  keep progressing during a fault-injection case, so a same-sized second
  8 GiB-budgeted instance cannot coexist on this host without overcommit,
  swap, or a peer budget smaller than the workload's own — Phase B's limit
  proposal must state this arithmetic explicitly rather than leave a
  two-8-GiB-peer scenario implicitly assumed feasible on a 16 GiB host. Every
  numeric budget proposed and every rlimit-enforcement finding is Observed on
  `Mac16,10` (M4) / macOS 26.6.2 (`25G83`) only until `P1` (M1 Pro / Sequoia
  15) replication, per the executor guide's incremental-replication rule.
- **Multicore interactions:** CPU-second budgets interact with `Q-05-06`'s
  open P/E-core placement and helper-thread normalization question — a
  guest's 8 vCPU threads scheduled onto a mix of P- and E-cores accumulate
  wall-clock progress at different rates for the same CPU-seconds consumed,
  which affects whether a CPU budget breach reflects genuine runaway guest
  behavior or host scheduling variance; this pass records the interaction but
  leaves core-placement normalization itself to `Q-05-06`. Network and
  storage I/O budgets must also account for concurrent device-backend threads
  (`Q-01-09`) potentially issuing I/O on behalf of multiple vCPUs
  simultaneously, which can make a naive single-threaded token-bucket design
  under-count actual throughput.
- **Falsifiers and stop paths:** this question is the direct test for
  `KA-13` ("Per-instance containment budgets can be enforced with public
  mechanisms without excluding accepted workloads"); a false finding routes to
  project stop input 3 (containment requirement). The unresolved `BP-00-end`
  cross-record implication explicitly owned here must be addressed with a
  proposed rule in Phase B, not merely restated: `INV-00-08`'s oracle contract
  terminates an offending instance within 1 s of a declared cap breach, while
  `INV-00-01` requires every mandatory workload to run to accepted completion
  under its own budget; whether a budget breach during a *measured, accepted*
  workload run counts as containment success (the mechanism worked as
  designed) or as a correctness/method failure (the budget was mis-sized for
  that workload, invalidating the sample) is currently unassigned between the
  two records, and Phase B's resolution must state which applies, and under
  what condition a breach instead reopens the budget itself rather than
  invalidating the run.
- **Measurement failure modes:** sampling `task_info`/CPU/RSS at the cadence
  `INV-00-08`'s oracle contract specifies (at most every 100 ms) itself
  consumes host CPU and could, at the margin, push a tightly-budgeted instance
  over its own CPU-seconds cap purely from instrumentation overhead — a
  budget proposal that does not reserve headroom for its own measurement
  apparatus risks an artifactual breach. A memory-pressure notification is a
  host-wide signal that fires identically regardless of which process (or
  none) is responsible for the pressure, so treating a pressure-source firing
  as evidence that *this* instance breached *its* budget would be a
  measurement-attribution error; `phys_footprint` from `task_info(TASK_VM_INFO)`
  is the correct per-instance instrument and the two must not be substituted
  for each other in the method or in result claims.

## Backlog updates

no new issues found

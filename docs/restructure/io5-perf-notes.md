# IO5 performance round: decisions and reasoning

Living notes for the CPU/memory performance round that followed IO5 combined acceptance (November 13, 2026, branch
`perf-real-workload`). Each entry says what was decided, why, and the evidence. Plans with their own reviews are linked;
this file is the index of decisions and the test/benchmark discipline used to move fast.

## Workload and method

- **Workload:** the stock Debian 13 cloud image (`tools/run_io5.py`), profiled one step per boot from a shared snapshot
  (`natemu-raw/io5-snapshot-20261113-01`: packages installed, root grown, CPython 3.13.18 configured and built), 4 vCPUs,
  4 GiB, `mitigations=off`, cold compile cache. Two steps carry the round: `configure` (serial, fork/exec heavy:
  thousands of short sed/sh/gcc/cc1 processes) and `make -j4` (parallel compiles).
- **Why these two:** they bracket the real-use spectrum the acceptance run exposed: process churn (configure) and
  sustained parallel CPU (make). Benchmarks from the earlier performance phase used `nokaslr` and small programs; IO5
  showed costs those never exercised (address-space switches, kernel non-global mappings, link capacity).
- **Breakdown:** the `perf` resident's buckets (exclusive: each tick charged to one bucket) per step window
  (`tools/io5_breakdown.py`, `analyze24.py`), host-side parts per translation request, and scratch counters when a
  question needs one (kept in `natemu-raw/io5-bins/*.diff`, never committed).

## Test and benchmark tiers (how we move fast without losing rigour)

| Tier | What | Time | When |
| --- | --- | --- | --- |
| 0 | host unit tests (`cargo test -p natemu -p natemu-abi`), resident checkers (barriers, stack), mutation of the new check | 1-3 min | every change |
| 1 | `run_k1.py --cpus 1 4` (CR3/INVLPG/links/PIC vs QEMU) under the VM lock; `link-check` resident for link changes | 15-25 min | every resident change |
| 2 | one `run_io5.py profile --steps configure make` judged on **bucket vCPU-seconds** against the last baseline | ~5 min | each chunk's gate |
| 3 | three interleaved rounds (order rotated) of base vs a **group** of changes, wall times, contention recorded | ~45 min | per group of 3-5 changes |
| 4 | full standing suite (`run_suite.py`), benchmark suite (3 rounds), IO5 full boots natemu + QEMU at 8 GiB, independent replication | 3-4 h | end of round |

Rules learned in this round:
- **Timed runs need the 4 performance cores to themselves.** One unlocked K1 run silently contaminated a QEMU timing
  and two natemu runs. Every VM launch holds `natemu-raw/.vm.lock` (`run_k1.py` takes none: wrap it in `lockf`);
  `run_io5.py` records any other VM overlapping a run (`contended`). Builds during timed runs go to the efficiency
  cores (`taskpolicy -c background`). Agents in git worktrees must export `NATEMU_RAW_ROOT`.
- **Small gains are real but invisible in single runs** (wall spread 1-2.5 s): a change is kept when its buckets show
  the work removed; wall time is judged per group (owner, November 13).
- **QEMU runs once per workload/version** and its numbers are reused (owner).
- **Untimed work in parallel:** reviews start when a commit exists, K1 overlaps with other untimed work under the lock's
  shared use, implementers work in separate worktrees on different files; only timed gates serialise.
- **Every resident change:** an independent reviewer (separate from the author), tests that a deliberate mutation turns
  red, and the chunk's measured gate.

## Decisions (chronological)

1. **Fair comparison is `mitigations=off` on both** (natemu and QEMU), not stock: QEMU TCG cannot advertise
   RDCL_NO, so stock compares different guest kernel work. Stock + speculation barriers is the user-facing comparison
   (natemu gets PTI off honestly); both are reported.
2. **Answer cache sized for a build's working set** (`41edbb5`): two generations of 1 Mi answers / 512 MiB. Evidence:
   78% of make's compiles were repeats of answers dropped by generation flips. make -6.7%, compiles -65%; costs about
   0.7 GB host memory (owner accepted).
3. **DISCARD/WRITE_ZEROES** (`69db2ed`): the image grew to 15 GiB holding 3 GiB; verified by fstrim, checksums and fsck.
4. **Kept-half list** (`45f0172`, retention plan chunk 0b): Linux maps part of its direct map non-global, so 58-68% of
   CR3 loads dropped every kernel shadow. A CR3 load now removes only the listed non-global shadows. configure -15%.
   The guest's own G bit was checked (natemu reads it correctly); the cause is the guest, the waste was natemu's.
5. **Inbound link lists** (`bf8f9a8`, `a624ef4`, chunk 1): undo a cut block's links through its own list instead of a
   scan of every record (35-82 us per event). Small wall gain (configure -1.9%, make -1.1% over 3 rounds); also enables
   1b and any relinking scheme.
6. **Link capacity 512 Ki** (`7d314e9`, chunk 1b): reclaims ~100 -> 1 per vCPU; make busy 669 -> 577 vCPU-s, step
   161 -> 139 s (single run; to confirm in the group round). `link-check` feature (`0281518`) catches a cut leaving a
   non-local inbound link at the moment it happens.
7. **Request body built once** (`0a814e4`): byte-identical keys (tested), fewer allocations per compile.
8. **Root retention (slots) NOT built** (chunks 2-4 of [root-retention-plan](root-retention-plan.md)): chunk 0's
   model gave make 6.5-43.9 vCPU-s and configure -0.7-5.7 s against gates of 20 and 8: only about 31% of
   revalidations would be saved, because short-lived processes (most of configure's and make's work) are new
   address spaces each time and shared library code is revalidated per process. The comparison machinery was proven
   exact (0 stale in 106 M). Revisit for long-lived, switch-heavy workloads (hackbench/locks model pending).
9. **Rejected after measurement:** XOR-folded L1 index (misses -6%, no wall gain in clean interleaved runs: misses are
   capacity/emptying, not hash quality); tag check on every linked jump (5-30% of guest time estimated); write-trapping
   guest page tables (225-405 us per load); entry-patch invalidation (O(blocks) per switch with slots); kernel code in
   the persistent cache (KASLR; measured November 5).
10. **Cross-vCPU reuse** ([cross-vcpu-reuse-plan](cross-vcpu-reuse-plan.md), accepted after two review rounds): 81% of
    configure's host cache hits are answers another vCPU compiled. A host-published, content-keyed in-VM pool (seqlock
    ring; positions in the index; containment rests on the position check) and option (d) (skip re-admission on hits
    when CPL-0, bulk and relocation match). Counting chunk running; the pool's 272 MiB and default-on are the owner's
    call after the count.
11. **Dispatcher and linking are not near-optimal** (Fable review): a current-block miss pays ~60% avoidable state copying
    and barriers; a link pays three full synchronizations; others use a call, not a trap, for misses and QEMU chains
    only within a page. In progress: IDC-gated code synchronization and L1 counters; next: a fast miss path (like
    `sys_fast`) and relink-on-revalidation (design under way).

## Configure's busy time (clean round, 119 vCPU-s; buckets exclusive)

Guest 30.8%; translation requests ~16% without the timing log (compile 4.7 s, admission 3.6, lookup/fetch/put/relocate
3.5, other 1.6, VM exit+entry 4.4, placement 0.8); linking + patching 10.4%; faults (abort, shadow, delivery) 10.5%;
dispatcher misses 6.5%; lookups/entries/revalidation/fetch/reuse 11%; TLB at CR3 loads 3.4%; system/interrupt/helpers
5%; boot CPU start-up 2.8%. No single overhead above 8.3%: progress is a sum of medium fixes, measured in groups.

## Reference points (same snapshot steps, cold, 4 vCPUs)

| | configure | make -j4 |
| --- | --- | --- |
| natemu, start of round (main) | 89.5-92.6 s | 177.9 s |
| natemu after decisions 2-7 | 76.7 s | 139.0 s (single run) |
| QEMU 11.1.2 TCG | 100.0 s | 531.7 s |
| native (Apple VZ, arm64) | pending | pending |

# Performance phase 2 plan, and the work after it

Status: November 3, 2026, after performance batch 1 (see [perf-plan.md](perf-plan.md), "Measured" sections; decisions in
[INV-03-01](../research/investigations/INV-03-01.md), "Performance" rows). Owner decisions are marked (owner).

## Where batch 1 ended

Installer boot to "Select a language", 2 GiB, `mitigations=off nokaslr`, cache off, wall time: natemu 49.2 s → 8.6 s at
N = 1 (10.98 → 9.37 s at N = 4 after the last step); QEMU TCG multi-threaded on the like-for-like setup 6.3 s (N = 1),
4.9 s (N = 4). The batch-end suite and full comparison record the final numbers in [boot-handoff.md](boot-handoff.md) §5.
Remaining natemu cost at N = 1 is spread over 1-3 s items (host requests for new code, compiles, linker, address-space
work, guest code); at N = 4 the extra vCPUs mostly idle and per-vCPU caches duplicate work.

## Phase 2: multi-core first (owner)

1. **Benchmarks** (`natemu-raw/bench-20261103-01/`, README for exact commands and pins). From an initramfs userland, timed
   between serial markers so boot is excluded, same total work at N = 1-4, outputs verified against native:
   - `build`: `make -jN libzstd.a` (zstd 1.5.7, Debian gcc 14.2, `-O0`): fork/exec, page faults, CR3 switches, fresh code;
   - `zstd`: `zstd -9 -B4194304 -TN` on 64 MiB of Silesia: compute scaling;
   - `hackbench`: rt-tests 2.8 `-p -g 4 -l 5000`: wakeups, IPIs, context switches;
   - `locks`: a threaded mutex/atomic/futex test: contention.
   Native ceiling on the reference AMD machine (itself a VM) is recorded there. First task: three clean runs each at N = 1-4
   on natemu and QEMU (multi- and single-threaded), one emulator at a time, plus the installer at N = 1-4. A full kernel
   build is too slow for routine use (owner).
2. **Shared host-side answer cache across vCPUs**: one (sharded) map instead of one per vCPU; removes the duplicate
   compiles (about 318 k compiles at N = 4 against 128 k at N = 1 on the installer).
3. **Shared in-VM code cache**, only if the multi-threaded benchmarks justify it (placement, links, invalidation and
   revalidation become shared, so synchronisation cost must be measured).
4. **The N = 4 slowdown**: per-vCPU breakdown, code re-placed after a process migrates between vCPUs, whether vCPU threads
   land on efficiency cores (QoS).

Then, as the numbers dictate:

5. **Compile ahead on spare cores, then tiering**: candidates from static successors (direct targets, both branch sides,
   return sites) and dynamic signals the resident already sees (lookup/link hotness, observed indirect targets, pages already
   translated in another address space); a bounded low-priority queue that real requests overtake; results are ordinary
   cache entries keyed by the bytes read and admitted at use; accuracy (fraction used) measured. Also fixes Linux's quick PIT
   calibration. Tiering: quick first translation, a better one for hot blocks.
6. **PCID and INVPCID on Arm ASIDs** (owner: a later phase; this is where it fits), with the shared kernel (global) shadow
   entries merged into it (owner) and accurate IA32_ARCH_CAPABILITIES "not vulnerable" bits (owner: both). Measured cost of
   Linux's default mitigations today: installer 11.96 → 24.93 s on natemu (2.1×) vs 6.53 → 8.92 s on QEMU (1.4×), almost all
   PTI address-space work (`natemu-raw/mitig-20261103-01/`). The ordinary benchmark case is "not vulnerable"; once PCID is
   built, a PTI-on boot joins the standing benchmarks (owner).
7. **Remaining exits and per-request costs**: fast paths for the commonest system instructions (~2.5 M entries per
   installer boot), fexc's fixed per-request setup (argument parsing, feature table and Config per request), in-process FEX
   (removes the pipe, gives up process isolation; admission stays), an in-VM random-number generator or the Arm RNDR if the
   VM sees it (owner: after batch 1), a cheaper disk-cache key (fast hash plus stored request bytes), huge pages for the code
   area.
8. **Generated-code quality** (guest code ~2.3 s on the installer at batch-1 start): larger FEX compile regions, careful
   relaxation of precision rules where nothing observes the difference. Last, and only if steady-state benchmarks point here.

Not in this phase: virtio devices and interrupt coalescing (integration); the correctness pass (after performance).
Measurement rules: one VM at a time; routine per-step set (N = 1 cache off, three interleaved runs, plus N = 4 once), the
full matrix at batch end; cold cache for comparisons with QEMU; per-case outputs deleted as each runner finishes.

## Correctness pass (after the performance phase, owner)

- Large-scale differential fuzzing against QEMU and the reference AMD machine; real workloads in the standing suite.
- Re-verify every decision taken on QEMU's word or inferred processor behaviour (owner rule: SDM-defined → SDM; open or
  reserved → the AMD machine). Most are confirmed (`natemu-raw/hwcheck-20261103-01/`, `-02/`); lowest-priority interrupt
  delivery and undefined MTRR overlaps need bare metal.
- Judging models written from memory (segment, syscall, delivery, ACPI) re-derived with SDM page citations.
- i8b: coverage after the first two-NaN operation; MIN/MAX under DAZ.
- Narrow the library's public interface (about 190 items).

## Integration (separate phase)

Disks, network and console input as virtio devices with interrupt coalescing.

## Owner-deferred items

Raising and testing RAM beyond 3 GiB and vCPUs beyond 4.

## CPU profile additions (owner, November 3)

SHA-NI (in progress), then ADX, CLFLUSHOPT/CLWB, XSAVEOPT/XSAVEC, and SMEP, SMAP, UMIP: each exact per the SDM, tested
against the reference AMD machine, added only with adequate Arm-side support and without severe regression (measured
before/after on the installer and the phase-2 benchmarks); the like-for-like QEMU control's `-cpu` flags follow every
profile change. Order is the orchestrator's: ADX, CLFLUSHOPT/CLWB and XSAVEOPT/XSAVEC first (cheap); SMEP and UMIP next
(fetch-walk and resident checks); SMAP last and only if its cost is acceptable — translated kernel code runs at EL0, so
Arm PAN does not apply directly and STAC/CLAC (around every user copy) would switch shadow spaces.

## FEX maintenance (owner, November 4: later)

Move the FEX patch series (tools/fexc/patches, 0001-0029 applied with `git am`) to a FEX fork with a `natemu` branch, one
commit per patch, rebased onto upstream on a fixed cadence with the standing suite after each rebase; the submodule pins the
fork's commit; the shim and fexc front end stay in natemu; the first fork build must be byte-identical to the current compiler;
general fixes offered upstream. Needs the owner to create the fork (this session's GitHub access covers arargon/natemu only).
After the multi-core phase.

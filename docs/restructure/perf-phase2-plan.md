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
   compiles (about 318 k compiles at N = 4 against 128 k at N = 1 on the installer). Built in perf chunk 13
   ([perf-plan.md](perf-plan.md), "Measured (chunk 13)"): installer N = 4 compiles 307.6 k -> 130.9 k, wall 9.17 -> 6.63 s;
   `build` N = 4 12.95 -> 9.26 s; an in-flight table was measured too and not kept (no wall gain).
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
   installer boot; done for the syscall path in perf chunk 12: `hackbench` N = 1 51.2 -> 30.1 s), fexc's fixed per-request setup (argument parsing, feature table and Config per request), in-process FEX
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
Built November 4 (boot-features plan, Built (ADX), Built (CLFLUSHOPT, CLWB), Built (XSAVEOPT, XSAVEC)): ADX, CLFLUSHOPT/CLWB and
XSAVEOPT/XSAVEC, each judged by the AMD machine; installer N = 1 cache off 9.51 -> 8.93 s, zstd N = 1 unchanged; the QEMU control
gains `+adx,+clflushopt,+clwb` (its TCG has no XSAVEC). Built November 4 (boot-features plan, Built (SMEP, UMIP)): SMEP was
already advertised and enforced by the walker (tests added); UMIP advertised, SGDT/SIDT/SLDT/SMSW/STR #GP(0) at CPL > 0 in the
resident, Linux activates it; installer N = 1 cache off 8.42 -> 8.32 s, hackbench N = 1 29.9 -> 30.0 s; the QEMU control gains
`+smep,+umip`. Next: SMAP (only if its cost is acceptable).

## FEX maintenance (owner, November 4: later)

Move the FEX patch series (tools/fexc/patches, 0001-0029 applied with `git am`) to a FEX fork with a `natemu` branch, one
commit per patch, rebased onto upstream on a fixed cadence with the standing suite after each rebase; the submodule pins the
fork's commit; the shim and fexc front end stay in natemu; the first fork build must be byte-identical to the current compiler;
general fixes offered upstream. Needs the owner to create the fork (this session's GitHub access covers arargon/natemu only).
Not scheduled (owner, November 4): deferred until the owner or the orchestrator judges it necessary.

## Baseline (November 4)

HEAD 1be744d (clean), the `natemu` stub (`-p natemu`, signed), series49-sha, cache off, `mitigations=off nokaslr`, 2 GiB; QEMU
11.1.2 TCG with the like-for-like flags (`+sha-ni`); the benchmarks of `natemu-raw/bench-20261103-01/`; wall seconds from the
START to the END line on the serial console (boot excluded), median of three runs, one emulator at a time (none contended,
every run's VERIFY line ok). Records, scripts and the full table (ranges, QEMU `thread=single` at N = 1 and 4, zstd phases,
per-vCPU breakdowns): `natemu-raw/bench-baseline-20261104-01/` (`table.md`, `runs.jsonl`, `perf/`, `dupes.json`).

| Benchmark, seconds | | N = 1 | N = 2 | N = 3 | N = 4 |
| --- | --- | --- | --- | --- | --- |
| `build` | natemu | **did not finish** (>2400 s) | 59.3 (48-121) | 73.9 (41-88) | 51.1 (35-119) |
| | natemu, chunk 11 | **21.25** | **15.84** | **13.67** | **13.93** |
| | natemu, chunk 12 | 20.39 | - | - | - |
| | natemu, chunk 13 | - | - | - | **9.26** |
| | QEMU multi | 28.3 | 15.3 | 13.5 | 12.5 |
| | native | 2.61 | 1.40 | 1.20 | 1.09 |
| `zstd` (all phases) | natemu | 2.26 | 1.74 | 1.63 | 1.71 |
| | QEMU multi | 17.5 | 11.5 | 8.65 | 7.29 |
| | native | 1.06 | 0.69 | 0.58 | 0.50 |
| `hackbench` | natemu | 53.8 | 27.9 | 19.6 | 15.2 |
| | natemu, chunk 11 | 52.7 | - | - | 15.2 |
| | natemu, chunk 12 | **30.1** | - | - | **9.57** |
| | natemu, chunk 13 | - | - | - | **9.34** |
| | QEMU multi | 16.4 | 8.87 | 6.41 | 5.08 |
| | native | 3.88 | 2.08 | 1.65 | 1.65 |
| `locks` | natemu | 16.7 | 8.90 | 6.62 | 5.81 |
| | natemu, chunk 11 | 16.3 | - | - | 6.07 |
| | natemu, chunk 12 | - | - | - | **5.35** |
| | QEMU multi | 11.0 | 33.2 | 26.1 | 27.5 |
| | native | 2.11 | 1.32 | 3.59 | 4.20 |

Speedup N = 1 to N = 4 (natemu / QEMU multi / native): `zstd` 1.33 / 2.41 / 2.10 (compress alone 1.65 → 1.00 s, 1.65x);
`hackbench` 3.54 / 3.23 / 2.36; `locks` 2.87 / 0.40 / 0.50 (natemu does not show the contention slowdown that native and
QEMU show); `build` none (no N = 1). natemu / QEMU multi time ratio: `zstd` 0.13-0.23 (natemu 4-8x faster), `hackbench`
3.0-3.3, `locks` 1.52 at N = 1 then 0.2-0.3, `build` 3.9-5.5. natemu / native: `zstd` 2.1-3.4x, `hackbench` 9-14x, `locks`
1.4-7.9x. QEMU `thread=single` at N = 4: `build` 30.2, `zstd` 20.4, `hackbench` 22.7, `locks` 11.1 s.

Rows "chunk 11": after the link-record fix ([perf-plan.md](perf-plan.md), "Measured (chunk 11)", November 4, 245700d plus
the change; `natemu-raw/linkfix-20261104-01/`, same harness and rules, median of three; `-`: not rerun). Rows "chunk 12":
after the system-instruction fast paths ([perf-plan.md](perf-plan.md), "Measured (chunk 12)", f6e8820 plus the change,
`natemu-raw/sysfast-20261104-01/`; its own before, f6e8820: `build` N = 1 20.89, `hackbench` 51.17 / 15.36, `locks` N = 4 6.42).
Rows "chunk 13": after the shared answer cache ([perf-plan.md](perf-plan.md), "Measured (chunk 13)", 18ee440 plus the change,
`natemu-raw/shared-20261104-01/`; its own before, 18ee440: `build` N = 4 12.95, `hackbench` N = 4 9.60; installer cache off
N = 1 8.65 -> 8.70, N = 4 9.17 -> 6.63).

**Findings.**
1. (Fixed by perf chunk 11: `build` now finishes, 21.3 s at N = 1 against QEMU's 28.3 s, 13.7-15.8 s at N = 2-4, run to run
   within 2 s.) `build` at N = 1 does not finish in 40 minutes, and at N = 2-4 takes 35-121 s for the same work (run to run). Cause, from the perf resident: the per-vCPU link table (64 K records) fills, and each new link
   then runs `prune` over every record (551 k reclaims in 120 s at N = 1); `cache::link` is 111.7 of 117.6 s of the vCPU
   thread's CPU (bucket `patch`). At N = 4 the bucket is 14.5-17.8 s of each vCPU's 26-29 s CPU. Fixing this comes before
   any other multi-core item: it is larger than every cost below.
2. `zstd` compress scales 1.65x at N = 4 against 2.5x natively; decompress (one thread) is 0.27-0.33 s against 0.07 s
   natively; the verify phase (0.34-0.39 s) is 4-5x native; the run is 2.1-3.4x native overall. Boot to START is 3.4-4.9 s
   (QEMU 2.1-3.0 s).
3. Per-vCPU at N = 4 (the perf runs include the 3-5 s boot): halted time is 2.2-2.4 s of 20 s wall in `hackbench`, 2.3-2.7 s
   of 10.8 s in `locks`, 3.0-3.4 s of 6.5 s in `zstd`, 7.5-9.5 s of 40 s in `build`; thread CPU 15.9-16.9 s (hackbench),
   6.3-7.6 s (locks), 1.5-2.3 s (zstd vCPUs 1-3). Biggest resident buckets: `hackbench` `link` 4.1-4.3 s and `system` 2.5 s
   per vCPU, `locks` `link` 1.7 s, `build` `patch` 14.5-17.8 s (finding 1), `zstd` almost none (the vCPUs wait). Host
   requests 33-85 k and compiles 33-68 k per vCPU in `hackbench` and `locks` (translation 0.75-1.5 s of host time each);
   vCPU 0 makes 1.5-2x the requests and compiles of vCPUs 1-3 everywhere.
4. Installer, N = 4, cache off (10.09 s with the log): 306,755 compiles; 133,439 (43%) compile a block (same RIP, bytes,
   context) that another vCPU compiled too, 172,765 (56%) by the reuse key ignoring the RIP; 174,507 placements are the
   later vCPUs' first placement of a template another vCPU already holds; the logs have no start times, so overlap in time
   is not known. vCPU thread CPU is 2.4-3.8 s of 10 s wall (halted 4.8-5.5 s). Whether threads land on efficiency cores
   cannot be seen without root (`powermetrics`); the machine has 4 performance and 6 efficiency cores, and at N = 4 the four
   vCPU threads and up to four compile servers compete for the four performance cores.

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
   Measured in perf chunk 14 ([perf-plan.md](perf-plan.md), "Measured (chunk 14)"): wakes and core placement cost little;
   CR3 loads dropping the kernel's global shadows did (fixed); vCPU threads now run at the user-interactive QoS class.

Then, as the numbers dictate:

5. **Compile ahead on spare cores, then tiering**: candidates from static successors (direct targets, both branch sides,
   return sites) and dynamic signals the resident already sees (lookup/link hotness, observed indirect targets, pages already
   translated in another address space); a bounded low-priority queue that real requests overtake; results are ordinary
   cache entries keyed by the bytes read and admitted at use; accuracy (fraction used) measured. Also fixes Linux's quick PIT
   calibration. Tiering: quick first translation, a better one for hot blocks.
6. **PCID and INVPCID on Arm ASIDs** (owner: a later phase; this is where it fits), with the shared kernel (global) shadow
   entries (built early in chunk 14, owner decision November 4; PCID reuses them) and accurate IA32_ARCH_CAPABILITIES "not vulnerable" bits (owner: both). Measured cost of
   Linux's default mitigations today: installer 11.96 → 24.93 s on natemu (2.1×) vs 6.53 → 8.92 s on QEMU (1.4×), almost all
   PTI address-space work (`natemu-raw/mitig-20261103-01/`). The ordinary benchmark case is "not vulnerable"; once PCID is
   built, a PTI-on boot joins the standing benchmarks (owner).
7. **Remaining exits and per-request costs**: fast paths for the commonest system instructions (~2.5 M entries per
   installer boot; done for the syscall path in perf chunk 12: `hackbench` N = 1 51.2 -> 30.1 s; CLI, STI, RDTSC inline at CPL 0 in
   chunk 15; SWAPGS inline at CPL 0 in chunk 17: `hackbench` N = 1 16.3 -> 13.8 s, `locks` N = 4 7.29 -> 6.54 s, [perf-plan.md](perf-plan.md),
   "Measured (chunk 17)"; SYSCALL and SYSRET remain two resident trips per system call), fexc's fixed per-request setup (argument parsing, feature table and Config per request), a SYSCALL/SYSRET fast path in the
   resident (chunk 17: two trips of about 0.2 us fixed cost each per system call, about 3.8 lookup passes per trip; going
   straight to the IA32_LSTAR block or the return RIP; estimate, unmeasured: about 0.1 us per call, about 1.6 s `hackbench`
   N = 1; measure first), a shared-memory request channel to the
   compile server in place of the pipe (keeps the process boundary; most of chunk 18's 3.5 us per compile is pipe and wake-up), an in-VM random-number generator or the Arm RNDR if the
   VM sees it (owner: after batch 1), a cheaper disk-cache key (fast hash plus stored request bytes), huge pages for the code
   area.
8. **More benchmarks** (owner, November 4: use existing implementations, not our own; timing is the orchestrator's call):
   a run-time code generator (e.g. LuaJIT with its benchmarks), heavy memory use (e.g. stress-ng or will-it-scale's
   page-fault, mmap and fork cases), floating-point/vector compute (e.g. c-ray or SciMark). Each test-run on the AMD machine
   and timed against QEMU like the existing ones; added before generated-code quality, which needs them. Per-change checks
   stay on a small subset; the full set runs at batch end.
   Chosen and staged (November 4, `natemu-raw/bench-20261104-02/`, the old four unchanged in the same initramfs): `luajit`
   (LuaJIT 2.1 openresty 20250117, 16 programs of Mike Pall's suite, N at a time, md5-checked), `mem` (stress-ng 0.19.02
   `--verify`, fixed op counts: vm, mmap, fork phases), `cray` (c-ray 1.1 `c-ray-mt`, sphfract 400x300, PPM md5). Seconds,
   N = 1 / 4: AMD reference 1.78 / 0.61, 4.65 / 1.46, 0.37 / 0.12; QEMU like-for-like 18.1 / 5.3, 16.5 / 4.9, 12.6 / 4.5;
   QEMU max 18.8 / 5.3, 27.3 / 8.0, 14.8 / 4.5. natemu runs next, after perf chunk 21.
   Extended (November 5, `natemu-raw/bench-20261105-03/`): four more existing programs — `luajit-interp` (the same LuaJIT,
   `-joff`), `7zip` (7-Zip 25.01, LZMA2 on 12 MiB of Silesia, then `7zz t`), `sqlite` (SQLite 3.46.1 `speedtest1 --memdb
   --verify --size 160`, single-process), `avx` (x264 0.164, AVX2 paths, 300 CIF frames) — and every boot now runs its
   benchmark twice, cold then warm, timed and verified separately. Resized for 3-10 s of natemu work: `zstd` -9 → -12, `cray`
   400x300 → 1280x960. QEMU like-for-like N = 1 cold / warm: luajit-interp 30.4 / 30.5, 7zip 21.1 / 15.0, sqlite 22.8 / 22.6,
   avx 45.0 / 44.9, zstd 29.8 / 29.0, cray 130.1 / 129.4 s. This is the batch-end set; per-change runs stay a subset.
   QEMU is reported twice from now on (owner, November 4): like-for-like (CPU features matched to natemu's, so the guest
   does the same work) and at its best (`-cpu max`, every feature TCG offers). QEMU does not change between our chunks, so
   both are measured once per benchmark set or QEMU version, not per change. A feature that makes QEMU faster goes on
   natemu's candidate list.
   First pass (`natemu-raw/qemumax-20261104-01/`, QEMU 11.1.2, three alternated rounds): `-cpu max` is slower than
   like-for-like in 9 of 10 cells (build +5%, zstd +9-11%, hackbench +10-22%, installer +15-21%); only `locks` N = 4 is
   faster (27.33 -> 25.29 s), and single-feature runs tie that to SMAP (like + SMAP 24.84 s). ERMS/FSRM account for much
   of the hackbench and installer slowdown. Under `max` the guest also runs 5-level paging and sees an AMD vendor. The
   faster of the two QEMU columns is the one natemu is compared against per cell.
   Dropped from standing runs (owner, November 4): `max` was slower in 9 of 10 cells and on all three new benchmarks, so
   like-for-like is the QEMU column from now on; the recorded `max` numbers stay as reference, rerun only if the QEMU version
   changes. Per-change measurement: the cells the change targets plus installer N = 1 and one canary per benchmark family,
   three interleaved runs; QEMU is never rerun per change; the full matrix only at batch end.
9. **Generated-code quality** (guest code ~2.3 s on the installer at batch-1 start): larger FEX compile regions, careful
   relaxation of precision rules where nothing observes the difference. Last, and only if steady-state benchmarks point here.

Applicability (owner, November 5): every change fixes a generic inefficiency, conditioned only on architectural state
(never on program names, guest addresses, OS symbols or benchmark binaries); each "Measured" section names the common
software patterns that hit the path beyond the benchmark that motivated it; a read-only audit for workload-specific code
runs at each batch end.

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
- The linker halts with `bad_link` when a fault or interrupt delivered at its target boundary changes CPL and the handler
  sits at the target RIP (legal x86, e.g. a CPL-3 jump to a supervisor-only page that is its own #PF handler; found in
  the chunk-17 review, older than it): skip linking when the code contexts differ, as for steps and breakpoints.

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

## Backlog (owner, November 4)

- In-process FEX: about 0.6 s installer N = 1 and 0.7 s `build` N = 1 (chunk 20 estimate), but four concurrent compiles in
  one process share FEX's global Config and the shim's globals (a lock serialises N = 4; per-thread state needs FEX patches),
  a hung compile can no longer be killed, and a FEX memory bug lands in the host process. Not now; only when necessary or
  when the owner asks.

## FEX maintenance (owner, November 4: done)

The FEX patch series moved to the owner's fork, https://github.com/ArArgon/natemu-fex, branch `natemu`: upstream 48d71752 plus
patches 0001-0033 applied with `git am`, one commit per patch, head **455698b577d343e1c1dc8c776dc39c237e312f85** (tree
`de278977...`, identical to the patched tree the old build.sh produced; the fork's `main` is untouched). `third_party/FEX`
is that commit (`.gitmodules` points at the fork, branch `natemu`); `tools/fexc/patches` is gone from natemu; the shim and
fexc front end stay. Checked: the build from the fork (`natemu-raw/fexc-native-20261026-01/series57-fork`) has `obj/fexc`
SHA-256 `a81a2f24...1fd8`, byte-identical to series56-setup-r1; `test_serve.py` (reuse, fork), `test_fexc.py`, `test_slice1.py`,
`check_sites.py --slice1` and `cargo +1.97.0 test --offline --locked` pass with it; a recorded installer N = 1 stream (126,017
requests) replays through both with 0 differences. Still to do, as the owner decided: rebase `natemu` onto upstream on a
fixed cadence with the standing suite after each rebase (moving to a newer upstream is a separate step), and offer general
fixes upstream.

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
| | natemu, chunk 14 | 20.29 | 13.08 | 10.31 | 9.23 |
| | natemu, chunk 16 pool on (not shipped) | 20.24 | - | - | 9.34 |
| | QEMU multi | 28.3 | 15.3 | 13.5 | 12.5 |
| | native | 2.61 | 1.40 | 1.20 | 1.09 |
| `zstd` (all phases) | natemu | 2.26 | 1.74 | 1.63 | 1.71 |
| | natemu, chunk 14 | 2.13 | 1.42 | 1.27 | 1.14 |
| | QEMU multi | 17.5 | 11.5 | 8.65 | 7.29 |
| | native | 1.06 | 0.69 | 0.58 | 0.50 |
| `hackbench` | natemu | 53.8 | 27.9 | 19.6 | 15.2 |
| | natemu, chunk 11 | 52.7 | - | - | 15.2 |
| | natemu, chunk 12 | **30.1** | - | - | **9.57** |
| | natemu, chunk 13 | - | - | - | **9.34** |
| | natemu, chunk 14 | **28.27** | **14.86** | **10.39** | **8.45** |
| | QEMU multi | 16.4 | 8.87 | 6.41 | 5.08 |
| | native | 3.88 | 2.08 | 1.65 | 1.65 |
| `locks` | natemu | 16.7 | 8.90 | 6.62 | 5.81 |
| | natemu, chunk 11 | 16.3 | - | - | 6.07 |
| | natemu, chunk 12 | - | - | - | **5.35** |
| | natemu, chunk 14 | 10.97 | 5.83 | 4.60 | 4.71 |
| | QEMU multi | 11.0 | 33.2 | 26.1 | 27.5 |
| | native | 2.11 | 1.32 | 3.59 | 4.20 |

Speedup N = 1 to N = 4 (natemu / QEMU multi / native): `zstd` 1.33 / 2.41 / 2.10 (compress alone 1.65 → 1.00 s, 1.65x);
`hackbench` 3.54 / 3.23 / 2.36; `locks` 2.87 / 0.40 / 0.50 (natemu does not show the contention slowdown that native and
QEMU show); `build` none (no N = 1). natemu / QEMU multi time ratio: `zstd` 0.13-0.23 (natemu 4-8x faster), `hackbench`
3.0-3.3, `locks` 1.52 at N = 1 then 0.2-0.3, `build` 3.9-5.5. natemu / native: `zstd` 2.1-3.4x, `hackbench` 9-14x, `locks`
1.4-7.9x. QEMU `thread=single` at N = 4: `build` 30.2, `zstd` 20.4, `hackbench` 22.7, `locks` 11.1 s.

Rows "chunk 14" (every cell rerun): after the kept global shadows and the vCPU QoS class ([perf-plan.md](perf-plan.md),
"Measured (chunk 14)", edfcee9 plus the change, series52-adox66, `natemu-raw/mcore-20261104-01/`; its own before, edfcee9, in
the same interleaved campaign: `build` 20.67 / 13.11 / 10.46 / 9.61, `zstd` 2.13 / 1.45 / 1.28 / 1.24, `hackbench` 30.32 / 16.17 /
11.55 / 9.41, `locks` 11.01 / 5.87 / 4.55 / 5.00). Speedup N = 1 to 4 now: `zstd` 1.87, `hackbench` 3.35, `locks` 2.33, `build` 2.20.

Rows "chunk 16": the compile-ahead pool on (`NATEMU_AHEAD=on`; [perf-plan.md](perf-plan.md), "Measured (chunk 16)", d2a3b0d
plus the change, `natemu-raw/ahead-20261104-02/`); its own before, d2a3b0d: `build` 20.01 / 9.28; installer cache off N = 1
8.10 -> 8.06, N = 4 6.54 -> 6.34. Within spread: the pool is off by default, so the shipped tree's numbers are chunk 14's.

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
5. (Perf chunk 14, measured at edfcee9 within the START-END window; [perf-plan.md](perf-plan.md), "Measured (chunk 14)".)
   Finding 3's halted times included the 3-4 s boot. In the window, vCPUs rarely halt: `hackbench` and `locks` N = 4 0.1-0.3 s
   of 5-10 s per vCPU, `zstd` compress 0.19-0.24 of 0.73 s (the guest's own time: 74% busy), decompress and verify one
   thread; a wake costs 8-9 us to the end of `wfi` and 12-14 us to the event, over a few hundred wakes per vCPU. Threads sit
   on performance cores 93-100% of their CPU time without a QoS class (`proc_pid_rusage`), 100% with user-interactive.
   `zstd`'s weaker scaling is its single-thread phases (0.44 of 1.2 s at N = 4; compress alone scales 2.34x, native 2.54x).
   `locks` gets faster with N because its N = 1 time is mostly guest system time (6.4 of 11.2 s: 3.1 M `sched_yield` calls
   at about 2 us), which parallelises; its contention shows (guest CPU 11.2 -> 21.2 s at N = 4) and its counts verify.
   `hackbench` N = 4 is CPU-bound (3.9 of 4 cores); per vCPU the costs of N = 1 (generated code 3.3 s, linker 1.8, entries
   1.0, system instructions 1.0 of 9.7 s with the perf resident) plus shadow misses after CR3 loads (0.9 s), 94% of them
   kernel (global) pages a CR3 load dropped: fixed in chunk 14.

# Performance diagnosis (measurement only, October 28, 2026)

Two questions are answered here by measurement only. First, where does each benchmark's time go: native arm64, FEX's code,
our patches, the VM, or QEMU? Second, why is natemu slower than QEMU on the 4096-byte-stride loop? Nothing in the
emulator was changed. When something was not measured, this README says "not measured".

Raw output: `/Users/liaozping/orca/natemu-raw/perf-diagnosis-20261028-01/` (`bench.json`, `sweep-ABC.json`, `sweep-C.json`,
`chase-load.json`, `chase-loadstore.json`, `qemu_sweep.json`, `nsweep.txt`, `nsweep2.txt`, `native.dis`, `natemu/results.json`,
`qemu/results.json`, `counters-manifest-sample.json`).

## Setup and machine load

- M4 (Mac16,10), macOS 26 (25G83), AC power. No thermal or performance warning was recorded. The harness calibration read 4.38-4.41 GHz.
- Compiler: the series10 `fexc` (`natemu-raw/fexc-native-20261026-01/series10/obj/fexc`). Native code: Apple clang `-O2`.
  QEMU: Homebrew `qemu-system-x86_64` 11.1.2 under system TCG, with the bare-metal stub from `tools/bench`.
- Load during the runs (14:01-14:21): load averages 1.5-2.3. The `top` snapshots showed CPU 72-94% idle. At the end of the
  natemu run, macOS's `deleted` (CacheDelete) used 89% of a core and `cloudd` 51%; the disk was 98% full. The other agent's
  VM experiments did not appear in any `ps` snapshot, but I cannot rule out short overlapping runs. VM-side timings vary more
  than host-side ones (min-max often 20-40% against under 5%), so differences under about 0.3 ns/iter in VM columns are not resolved.
- The commands ran from an agent shell, at the default QoS and without core pinning (as in S2g).

## Method

- (a) `native.c`: the six loops in C, with the same algorithm, data, footprint and stride. Times are slopes of t(2N) - t(N),
  median of 7 rounds. The final x86 registers match `tools/bench/model.c` for all six loops.
- (b) FEX's **default** profile: FEX without this project's correctness patches. The code is compiled by `fexc --profile default
  --no-tso --no-end-marker`, plus `--multiblock` for `branchy`. `--no-tso` is used because natemu runs with hardware TSO, so FEX's
  software TSO (`ldar`/`stlr`) does not belong in the comparison. The block's own loop runs natively, as S2g's `fexonly` did.
  No separate unpatched build exists: the patches act only under `--profile slice1`, and the README of `tools/fexc` says that
  default-profile output is unchanged.
- (c) The `slice1` profile: the linked blocks that natemu executes, patched as the resident patches them (`build_native.py base`).
- (b) and (c) run in two places:
  - "host" uses `tools/bench/attribution/h2.c` (S2g's harness, user mode, 16 KiB pages).
  - "VM" uses **`vmloop.c`**, a new probe. It runs the identical image in a Hypervisor.framework VM at EL1, with natemu's
    settings: TSO on, 4 KiB stage-1 pages, normal write-back inner-shareable memory, and the frame, exit and dispatcher
    layout of h2. The probe has these switches:
    - TSO on or off;
    - stage-1 mapping: 4 KiB pages, 16 KiB pages, or 2 MiB blocks;
    - stage-2 (IPA) granule: 4 KiB or 16 KiB (`hv_vm_config_set_ipa_granule`);
    - EL1 or EL0;
    - `host`: runs the same image natively in the same process, on **the same physical pages** that the VM maps.

  Before every run, the VM probe resets the data. After the 2N run, its final guest registers are compared with `model.c`
  (or with `mm.c` for the sweeps). Every comparison matched, so the VM really computes the loop.
- (d) natemu: `tools/bench/run_natemu.py`, 3 rounds, scale 0.5 (natemu's own N to 2N wall-time slope).
- (e) QEMU: `tools/bench/run_qemu.py --no-linux-user --reps 3 --mults 1,2 --scale 0.5`.
- Words per iteration:
  - (b): the default block's loop body (from the `b.ne` target to the `b.ne`). For `branchy`, the paths are weighted by the
    model's branch outcomes.
  - (c): `words.py`'s executed-word count for the linked steady state. For `call`, it adds the 12 words of `dispatch_fast` per `ret`.
  - (a): counted from `native.dis`.

```
python3 experiments/track03/perf-diagnosis/run.py build OUT && python3 .../run.py bench OUT     # (a)(b)(c), host and VM
python3 .../run.py sweep OUT [20000000 ABC]; python3 .../run.py chase OUT load|loadstore      # 4096 diagnosis
python3 .../qemu_sweep.py OUT 3                                                                # QEMU over the same sweep
tools/with-env python3 tools/bench/attribution/vmvar.py FEXC OUT 3 20000000 64:256:0 ... 4096:128:0   # natemu over the sweep
tools/with-env python3 tools/bench/run_natemu.py --fexc FEXC --out OUT --reps 3 --scale 0.5
python3 tools/bench/run_qemu.py --out OUT --reps 3 --mults 1,2 --scale 0.5 --no-linux-user
```
`run.py` reads `FEXC` (default: series10) and `NATEMU_RAW_ROOT`. OUT was the scratch directory; results were copied to the raw folder.

## 1. Time per iteration (ns), all benchmarks

| bench | x86 insns/iter | (a) native C | (b) FEX default, host | (b) in VM | (c) slice1, host | (c) in VM | (d) natemu | (e) QEMU TCG |
|---|---|---|---|---|---|---|---|---|
| int | 10 | 1.81 | 1.85 | 1.95 | 1.89 | 1.88 | 1.89 | 1.95 |
| mem64 | 8 | 0.64 | 0.64 | 0.69 | 0.81 | 0.84 | 0.76 | 2.68 |
| mem4096 | 8 | 3.58 | 3.53 | 6.49 | 4.47 | 7.07 | 7.80 | 2.91 |
| call | 11 | 0.67 | not measured¹ | not measured¹ | 1.86 | 1.89 | 1.64 | 7.87 |
| branchy | 11.75 | 0.98² / 3.55³ | 3.89 | 3.89 | 4.83 | 4.84 | 4.85 | 6.65 |
| mixed | 13 | 0.71 | 0.94 | 0.95 | 1.02 | 1.06 | 1.03 | 3.58 |

Native instructions (words) per iteration, and per x86 instruction:

| bench | (a) native C | (b) FEX default | (c) slice1 |
|---|---|---|---|
| int | 7 (0.70) | 16 (1.60) | 22 (2.20) |
| mem64, mem4096 | 10 (1.25) | 12 (1.50) | 28 (3.50) |
| call | 10 (0.91): loop 7 + leaf 3 | not measured¹ | 66 (6.00): 54 block words + 12 dispatcher words |
| branchy | 12 (1.02)² / ~8.6 (0.73)³ | 16.24 (1.38) | 33.24 (2.83) |
| mixed | 13 (1.00) | 18 (1.38) | 40 (3.08) |

¹ The default profile ends the block at the `call`: a `bl` to an exit stub plus FEX's return-stack push. It has no in-block
loop, so it cannot run in this harness. ² clang turns both branches into `csel`/`cinc`, so this version has no data-dependent
branch. ³ `branchy_br`: the same C with an empty `asm volatile` in each arm, which keeps the two branches. The x86 loop has them.

What this establishes:

- **Native against FEX** ((a) against (b)): int +0.04, mem64 0, mem4096 0, mixed +0.23 ns/iter. For branchy, the gap is
  +0.34 when both keep the branches and +2.9 against clang's branch-free code. Most of branchy's time is the branches
  themselves: 3.55 ns natively with branches against 0.98 without.
- **Our patches** ((c) against (b), on the host): int +0.03, mem64 +0.17, mem4096 +0.94 (see section 3), branchy +0.94
  (the six linked blocks against FEX's single multiblock block), mixed +0.08 ns/iter.
- **The VM** ((c) in the VM against (c) on the host): +0.04 or less on int, mem64, call, branchy and mixed. On mem4096 it is
  +2.6 (section 3). The default-profile block shows the same gap (+3.0 on mem4096), so this cost does not come from our patches.
- **natemu against the probe**: they agree within 0.25 ns on every benchmark except mem4096 (7.80 against 7.07; the natemu
  rounds ranged 28%).
- Not established: anything about `call`'s FEX default profile, and the cost of FEX's software TSO (not measured).

## 2. The integer loop: where natemu's time goes (ns/iter)

| step | ns/iter | delta | measured by |
|---|---|---|---|
| native C, 7 instructions | 1.81 | | (a) |
| FEX default block, 16 words: imul CF/OF (`smulh`, `cmp`, `ccmp`, `cset`, `rmif`) and register moves | 1.85 | +0.04 | (b) host |
| slice1 linked block, 22 words: 2 extra `mov`s, header and stop-check prologue (3 words, executed every iteration), link exit | 1.89 | +0.03-0.04 | (c) host |
| same block in the VM | 1.88 | -0.01 | (c) VM |
| natemu | 1.89 | +0.01 | (d) |
| QEMU TCG | 1.95 | +0.06 vs natemu | (e) |

- Dispatch overhead per iteration is 0. The loop is one linked self-loop block: natemu's counters show `fast` = 0, so there
  are no dispatcher hits, and the counters are identical at N and 2N.
- Address guards: 0, because `int` makes no memory access.
- Stop-check store and header: removing them changed the time by -0.02 and -0.06 ns in S2g, which is within noise there. Not re-measured here.
- Flag computation: included in the +0.04 ns of (b) against (a). It was not isolated from the register moves (not measured separately).
- In total, natemu is 0.08 ns (4%) slower than native C and 0.06 ns faster than QEMU. All three are near 1.8-1.95 ns. At the
  measured 4.4 GHz that is about 8 cycles per iteration, which matches the loop-carried chain of add, shift-xor, shift, xor
  and multiply. That match is an inference from the clock: no cycle or latency counter was read.

## 3. The 4096-byte stride

### 3a. Stride and footprint sweep (ns/iter; the mem loop with stride and wrap mask patched; N = 20M, 7 rounds)

"slice1" is the linked natemu block. The probe VM columns run at EL1 with TSO on and stage-2 16 KiB unless the column says otherwise.

| set | stride | 4 KiB pages | native C | slice1 host | slice1 VM 4K pages | VM 16K pages | VM 2M blocks | natemu | QEMU |
|---|---|---|---|---|---|---|---|---|---|
| A (1 MiB) | 64 | 256 | 0.63 | 0.79 | 0.82 | 0.79 | 2.27 | 0.76 | 2.67 |
| A | 1024 | 256 | 2.18 | 1.16 | 1.21 | 1.20 | 2.39 | 1.30 | 3.04 |
| A | 2048 | 256 | 2.50 | 2.69 | 5.98 | 5.93 | 7.18 | 6.9 / 9.7 | 2.84 |
| A | **4096** | 256 | 3.53 | 4.54 | 7.57 | 6.83 | 8.15 | 7.4 / 6.3 | 2.95 |
| A | 4160 | 256 | 0.65 | 0.91 | 1.02 | 0.89 | 2.21 | 1.04 / 1.05 | 2.79 |
| A | 8192 | 256 | 4.52 | 5.45 | 9.26 | 8.81 | 10.53 | 8.33 | 3.06 |
| A | 16384 | 256 | 5.61 | 6.57 | 9.92 | 11.52 | 10.50 | 8.9 / 8.1 | 5.86 |
| B | 4096 | 16 | 0.52 | 0.74 | 0.75 | 0.75 | 2.70 | 0.68 | 2.38 |
| B | 4096 | 32 | 0.53 | 1.44 | 0.90 | 0.86 | 2.58 | 0.75 | 2.59 |
| B | 4096 | 64 | 1.56 | 3.66 | 3.69 | 3.55 | 7.41 | 9.1 / 10.2 | 3.26 |
| B | 4096 | 128 | 2.50 | 4.51 | 6.50 | 6.08 | 8.95 | 8.4 / 6.9 | 2.95 |
| C (8 MiB) | 64 | 2048 | 0.67 | 0.81 | 0.83 | 0.80 | 2.35 | n/m | n/m |
| C | 4160 | 2048 | 0.73 | 1.04 | 1.01 | 1.06 | 2.27 | n/m | n/m |

The natemu column has two runs, `vmvar.py` with 3 and 7 rounds. Its ranges were 9-78%, so it is the noisiest column.
Set C does not fit natemu's 4 MiB guest RAM or QEMU's 4 MiB identity map, so it was not measured there ("n/m").

### 3b. VM knobs at stride 4096, 1 MiB (ns/iter; slice1 block)

| host (h2) | VM default | TSO off | stage-2 4 KiB | EL0 | 16 KiB stage-1 | 2 MiB stage-1 | 2 MiB + TSO off + EL0 |
|---|---|---|---|---|---|---|---|
| 4.54 | 7.57 | 7.84 | 7.43 | 8.45 | 6.83 | 8.15 | 9.17 |

The same knobs at stride 4160 (1 MiB and 8 MiB) and stride 64 (8 MiB) read 0.83-1.06 for every setting except the 2 MiB
blocks (2.21-2.35). Two more single runs: the probe's own in-process host mode, on the same physical pages, read 4.34. Moving
h2's frame 512 bytes off the data's low 12 address bits read 3.84 on the host (base 4.59, without the prologue 3.66). In the
VM the same frame and prologue variants read 6.8-8.9, unchanged within its noise.

### 3c. Hand-written dependent chases (ns per access)

These are two tiny hand-written arm64 loops, run in the same probe. Each access's address comes from the previous load.
`load` is `ldr x4,[x10,x4]`. `loadstore` is `ldr x5,[x10,x4]; str x5,[x10,x4]; mov x4,x5`, which is the benchmark's
load and store to the same address.

| stride | footprint | load: host | load: VM 4K | load: VM 16K | loadstore: host | loadstore: VM 4K | loadstore: VM 16K |
|---|---|---|---|---|---|---|---|
| 4160 | 64 KiB | 0.91 | 0.91 | 0.91 | 0.91 | 0.91 | 0.91 |
| 4160 | 1 MiB | 5.05 | 8.08 | 4.84 | 4.80 | 6.88 | 5.01 |
| 4160 | 8 MiB | 5.37 | 8.36 | 5.88 | 6.40 | 8.13 | 6.38 |
| 4096 | 256 KiB (64 cells) | 1.42 | 4.10 | 1.41 | 2.48 | 4.31 | 3.14 |
| **4096** | **1 MiB (256 cells)** | **1.20** | **7.68** | **1.20** | **2.06** | **6.62** | **6.63** |
| 64 | 1 MiB | 1.08 | 1.46 | 1.54 | 1.39 | 1.71 | 1.26 |
| 16384 | 1 MiB (64 cells) | 4.48 | 4.49 | 4.61 | 4.61 | 5.41 | 5.32 |

### What section 3 establishes

1. **natemu is slower than QEMU at stride 4096 for two measured reasons.**
   - Even with no VM, the identical code takes 4.5 ns natively. That is FEX's block (3.5, the same as native C) plus 0.9 that
     comes from the frame stores. The prologue and header store `[x28]` and the data share their low 12 address bits: moving
     the frame by 512 bytes recovers 0.75 of the 0.9 on the host.
   - Running inside a Hypervisor.framework VM then adds 2.6-3.0 ns per iteration, for FEX's default code as well as for ours.
   - QEMU runs the same loop in 2.9-3.0 ns, faster than even native C (3.5). The reason QEMU's code is faster here was not measured.
2. **What the VM extra does NOT depend on** (each was changed alone, and the extra stayed within the probe's noise):
   - TSO (ACTLR_EL1);
   - EL1 against EL0;
   - the stage-2 granule (4 or 16 KiB);
   - the physical pages: the probe's host mode uses the same pages and reads 4.34;
   - the frame offset;
   - 16 KiB stage-1 pages, which give 6.83 against 7.57. That is at most 0.7 of the 3.
3. **Where the VM extra appears.** It appears only at power-of-two strides of 2048 or more, and only once the loop touches
   128 or more 4 KiB pages. It does not appear at stride 64, 1024 or 4160, at 1 MiB or at 8 MiB, even though those also miss L1
   (set C). natemu follows the probe on this pattern at 1 MiB footprints.
4. **The chases separate two VM-only effects:**
   - Exposed **load** latency in the VM is +0.7 to +6.5 ns with 4 KiB stage-1 pages and disappears with 16 KiB stage-1 pages.
     This holds wherever 64 or more 4 KiB pages are touched by a chain whose latency is not hidden: +2.7 at stride 4096 over
     256 KiB, +6.5 over 1 MiB; at stride 4160, +3.0 over 1 MiB, +0.7 over 4 MiB, +3.0 over 8 MiB. So for pure loads the extra follows the **4 KiB stage-1 granule**, not the VM as such.
   - A **store to the same address** in a stride-4096 chain adds about +4.6 ns in the VM with 4 KiB **and** 16 KiB stage-1
     pages (6.62 / 6.63 against 2.06 on the host). At stride 4160 the store adds nothing VM-specific. This second effect is the
     one that remains in the benchmark under 16 KiB stage-1 pages.
5. **2 MiB stage-1 blocks** (over the 16 KiB stage-2) cost about +1.4 ns per iteration at every stride, including fully
   L1-resident ones. Larger guest mappings are therefore not a free fix. natemu's generated code uses 4 KiB shadow pages, so
   this does not touch the benchmarks. Resident-side accesses through natemu's 2 MiB guest-RAM alias were not measured.

**Not established:** the hardware mechanism of either VM-only effect. The candidates are the TLB (L1 DTLB reach or the
nested-walk cost in guest mode) for the first, and memory-disambiguation or ordering replays for the second. The PMU run
below is meant to decide. Also not established:

- why QEMU beats native C at stride 4096;
- why natemu read 9.1-10.2 at 64 pages, where the probe reads 3.7, natemu's range there was 18-51%, and S2g measured 5.3.
  natemu's layout differs from the probe's: data at VA 0x200000, frame and code at `PRIVATE`, ASIDs, and nG. Not measured.

## 4. Hardware counters (needs root): `counters.py` + `kpcrun.c`

`kpcrun` counts the Apple PMU system-wide (all CPUs, all exception levels) around one child command through the private
`kperf`/`kperfdata` frameworks. `counters.py` runs every subject at N and 2N, 3 times each, and reports per-iteration counts:

- subjects: the h2-style host run, the VM probe with 4 KiB and with 16 KiB stage-1 pages, natemu, and QEMU;
- points: the loop at stride 4096 (64, 128 and 256 pages), stride 2048 and stride 4160, plus the load and loadstore chases.

Events: cycles, instructions, `L1D_CACHE_MISS_LD`, `L1D_CACHE_MISS_ST`, `L1D_TLB_MISS`, `L1D_TLB_ACCESS`, `L2_TLB_MISS_DATA`,
`MMU_TABLE_WALK_DATA`, `ST_MEM_ORDER_VIOL_LD_NONSPEC` and `LD_UNIT_WAITING_YOUNG_L1D_CACHE_MISS`. They fit in one group:
`kpcrun --check` maps them to counters 0-9 without root.

```
cd /Users/liaozping/orca/natemu
python3 experiments/track03/perf-diagnosis/counters.py prepare /Users/liaozping/orca/natemu-raw/perf-diagnosis-20261028-01/counters
sudo python3 experiments/track03/perf-diagnosis/counters.py run /Users/liaozping/orca/natemu-raw/perf-diagnosis-20261028-01/counters
```

- `prepare` runs as you, without root. It takes about 1 minute (cargo is already built) and writes about 18 MiB: natemu's
  program files hold the 1 MiB arrays.
- `run` takes about 3-5 minutes. A dry run of every command without counters (`run OUT --dry`) took 43 s per round and passed.
- `run` writes `counters.json` and prints one row per subject and point.
- Close other work first. The counts are system-wide, so other processes add to them. N to 2N differences cancel startup, but not background noise.

What each outcome would establish:

| observation (per iteration, stride 4096, 256 pages) | establishes |
|---|---|
| VM and natemu cycles/iter about 0 while their ns/iter are about 7 | the counters do not run in guest mode; the VM columns are void, and only host and QEMU rows can be used |
| `L1D_TLB_MISS` and/or `MMU_TABLE_WALK_DATA` per iteration much higher for VM 4K than for host and VM 16K in the **load** chase | the first VM-only effect is translation: 4 KiB guest pages miss the L1 DTLB, or walk, where 16 KiB pages do not |
| `ST_MEM_ORDER_VIOL_LD_NONSPEC` much higher in the VM than on the host in the **loadstore** chase and the benchmark | the second effect is memory-ordering replays (load or store disambiguation) that happen only in guest mode |
| `L1D_CACHE_MISS_LD`/`_ST` equal on host and VM but cycles higher in the VM | the extra is per-miss latency in the guest (translation on the miss path or the walk), not more misses |
| more L1D misses per iteration in the VM than on the host | the VM changes cache behaviour (for example, the prefetcher or set indexing in guest mode) |
| QEMU with fewer L1D misses or fewer ordering violations per iteration than the host's FEX block | why QEMU beats native at stride 4096: its access sequence avoids the aliasing stall |
| natemu equal to VM 4K on every event | the probe is a faithful stand-in for natemu on this loop |
| natemu much higher than VM 4K at 64 pages | the 64-page discrepancy is natemu-specific (layout, ASIDs or nG), not the VM in general |

## Files

- `run.py`: build; (a), (b) and (c) on the host and in the VM; the sweep; the chases. `native.c`: (a).
- `vmloop.c` + `guest.S`: the VM probe; its host mode links `tools/bench/attribution/tramp2.s`. `entitlements.plist`.
- `qemu_sweep.py`: QEMU over the sweep, using `run_qemu.py`'s builder, runner and fit, checked against `mm.c`.
- `counters.py`, `kpcrun.c`: the root-only PMU run.

# 19 — Does the Design Scale to a Multi-vCPU Guest? Measured

**Date:** 2026-08-08 · **Host:** Apple M2 Max (8 P-cores + 4 E-cores), macOS 26.5.2, SDK Hypervisor 259.5.15
**Probes:** `probes/smp_scaling/smp_scaling.c` + `probes/smp_scaling/payload.h`, `probes/gic_sgi/gic_sgi.c`
(build `probes/build.sh smp_scaling/smp_scaling.c`; raw logs
`probes/smp_scaling/results/run{1,2,3}.log` and `run4-base.log`)

Every performance number in this corpus up to `16` was **single-core**. `16 §3` measured TLBI
contention with exactly **one** other vCPU; `14 §G-4` ran the TSO litmus on **two**. The actual
target is a multi-vCPU guest. This document runs the load-bearing measurements at 1–12 vCPUs.

> **Reading the labels.** `M` = measured on this machine today. `D` = derived by subtracting one
> measured quantity from another. `X` = modelled/arithmetic on an `M` number. `?` = attempted and
> **inconclusive** — reported as a failure, not smoothed over.

---

## Bottom line

### The one table that matters

Per-vCPU cost and aggregate throughput of the **shadow-fill fault path**, N vCPUs all faulting
concurrently, each on private page tables and private pages:

| N | per-vCPU p50 | vs adjacent solo | p99 | aggregate | parallel efficiency | Tag |
|---|---|---|---|---|---|---|
| 1 | 169–172 ns | — | 201–706 ns | 5.8–5.9 M faults/s | — | `M` |
| 2 | 167–174 ns | 0.97–1.00× | 198–214 ns | 11.5–12.0 M | 100–103 % | `M` |
| 4 | 169–188 ns | 1.07–1.09× | 216–224 ns | 22.1–23.8 M | 95 % | `M` |
| 6 | 167–185 ns | 1.05–1.09× | 224–487 ns | 33.1–35.9 M | 93–95 % | `M` |
| 8 | 174–182 ns | 1.09–1.12× | 492–531 ns | 44.3–46.5 M | **91–92 %** | `M` |

*(ranges across two independent full sweeps; "vs adjacent solo" is against an N = 1 run measured
immediately before, see Method)*

**Shadow-fill fault handling scales.** At 8 vCPUs each one still handles a full fault → ESR/FAR →
4-level x86 walk → descriptor synthesis → store → `ERET` in **within 11 % of its solo cost**, and
the machine sustains **~45 million shadow fills per second**. Sharing the page-table tree between
vCPUs (one `TTBR0`/ASID, shared L0/L1/L2) changes nothing measurable: 93 % efficiency at 8.

### …and the one that kills it

The same experiment with the **naive handler that issues `TLBI VAAE1IS` on every fill**:

| N | per-vCPU p50 | vs N=1 | aggregate | Tag |
|---|---|---|---|---|
| 1 | 370 / 383 ns | 1.00× | 2.70 / 2.61 M faults/s | `M` |
| 2 | 628 / 622 ns | 1.70 / 1.63× | 3.19 / 3.21 M | `M` |
| 4 | 781 / 776 ns | 2.11 / 2.03× | 5.15 / 5.17 M | `M` |
| 8 | 1214 / 1229 ns | **3.28 / 3.21×** | **6.58 / 6.56 M** | `M` |

*(two independent runs, shown as `run1 / run3`; they agree to within 2 %)*

> **`16`'s design rule — "do not issue a `TLBI` when turning an INVALID descriptor into a VALID
> one" — was worth 3.0× on one core. On eight cores it is worth 6.8×**, and the difference between
> 45 M and 6.6 M aggregate fills/s is the difference between a design that scales and one that does
> not. This is now the single highest-value rule in the corpus.

### Everything else, one line each

| # | Quantity | Result | Tag |
|---|---|---|---|
| 1 | shadow fill, per-vCPU, 8 vCPUs, private page tables | **+11 %** over solo | `D` |
| 2 | shadow fill, per-vCPU, 8 vCPUs, **shared** page tables | +9 % over solo | `D` |
| 3 | aggregate shadow fills/s, 8 vCPUs | **45 M/s** | `M` |
| 4 | shadow fill **with** per-fill `TLBI`, 8 vCPUs | 1214 ns, **3.3×** solo | `M` |
| 5 | `TLBI VAE1IS` + barriers, solo (reference) | 208–316 ns | `M` |
| 6 | …with 7 **idle** vCPUs | 359–384 ns (0.8–1.7×, noisy) | `M` |
| 7 | …with 7 vCPUs hammering **the same** pages (true shootdown) | **441–445 ns (1.6–2.0×)** | `M` |
| 8 | …with 7 vCPUs walking their own 4 KiB pages | **907–961 ns (3.7–4.5×)** | `M` |
| 8b | `TLBI VMALLE1IS` + barriers, 7 busy vCPUs | **888–1147 ns (2.3–4.6×)** | `M` |
| 8c | `TLBI VMALLE1` (**non**-broadcast), 7 busy vCPUs | **885–1152 ns (2.0–4.3×)** | `M` |
| 9 | `hv_vcpus_exit` → `hv_vcpu_run` returns | 917 / 1167 ns | `M` |
| 10 | full host-mediated IPI → guest EL1 IRQ vector | **1500 / 1542 ns** | `M` |
| 11 | GICv3 `ICC_SGI1R_EL1` → other vCPU's EL1 vector, **zero host exits** | **3.5–4.1 µs** | `M` |
| 12 | …of which the `MSR ICC_SGI1R_EL1` write alone | **1.65–1.73 µs** | `M` |
| 13 | in-VM shared-memory flag round trip (`STLR`/`LDAR`) | **68 ns** (34 ns one-way) | `M` |
| 14 | MP litmus, `EnTSO=1`, 2/4/8 vCPUs, both variants | **0 weak outcomes, always** | `M` |
| 15 | MP litmus, `EnTSO=0`, 8 vCPUs | 4.2–7.8 M weak outcomes | `M` |
| 16 | `EnTSO=1` cost, private buffers, any N | **inconclusive** (bimodal, as `16 §7`) | `?` |
| 17 | `EnTSO=1` cost, 8 vCPUs storing to the **same** cache lines | **4.6×–12.6×** | `M` |
| 18 | `EnTSO=1` cost, 8 vCPUs **loading** the same cache lines | 1.00× (no effect) | `M` |
| 19 | vCPU clock, `QOS_CLASS_USER_INTERACTIVE`, solo / 8 vCPUs | 2.83–2.89 / 2.48–2.53 GHz | `M` |
| 20 | vCPU clock, `QOS_CLASS_BACKGROUND`, 8 vCPUs | 0.79–2.66 GHz, **2.7–3.0× spread** | `M` |
| 21 | mixed QoS, 4+4 vCPUs, simultaneous | **1.4×–3.0× spread** between vCPUs | `M` |
| 22 | 12 vCPUs on 8 P+4 E cores, fault p50 / p99 | unchanged (164 / 208 ns) | `M` |
| 23 | 12 vCPUs + 8 host threads, worst fault sample | **1.27 ms** (one preemption) | `M` |

### The single most important finding

> **The in-VM fault path is not a shared resource. The TLB-maintenance path is.**
>
> Eight vCPUs taking shadow-fill faults simultaneously cost 92 % of perfect scaling, because the
> only hardware they contend for is per-core (page-table walker, L1 dTLB, exception unit) plus a
> stage-2 that HVF has already made static. Eight vCPUs issuing `TLBI`s while the other seven do
> memory work cost **1.6× to 4.6× each**, because a `TLBI`'s cost is its `DSB` completion latency,
> and a `DSB` cannot complete until the whole memory system agrees that it can.
>
> This is exactly the opposite of the `02 §6.1b` picture for *host exits*, where aggregate
> throughput saturates machine-wide at ~3 M/s. **Work kept inside the VM scales with cores. Work
> that leaves the VM, or that requires cross-core agreement, does not.**

---

## Method and its guards

* **Timing.** `CNTVCT_EL0` read at guest EL1 under our own `VBAR_EL1`; no host exit occurs inside
  any measured window. 24 MHz → 41.67 ns/tick, so every window batches many operations. Each
  shadow-fill sample covers **16 faults** (≈ 2.7 µs, ≈ 1.5 % quantisation) so that a *distribution*
  exists and p99 means something; `16` used one sample per 512 faults and could only report a
  median.
* **This probe reproduces `16` when it uses `16`'s method.** With one timed window per 512-page
  round the no-`TLBI` shadow fill measured **141.85 / 145.67 / 145.67 / 158.28 / 185.06 / 185.55 ns**
  over six independent processes against `16`'s **145.8 ns** (`16` itself reported 141.7–160.6 over
  five), and the `TLBI` variant **434.9–502.6 ns** against `16`'s **439.2 ns** (`16`: 405.8–469.3).
  Fault counts were asserted at exactly 153 600 / 153 600 every time. **`16 §2` is confirmed.**
* **The finer 16-page timing window costs something, and how much is not well determined.**
  Measured back-to-back in the same process, the 16-page-window arm read higher than the
  512-page-window arm by **+8, +9, +18, +18 and +21 ns/fault** in five paired comparisons and
  **−42 ns** in a sixth. The offset is probably real (two extra `ISB`+`MRS` pairs per 16 faults,
  plus the fencing apart of faults the machine may otherwise overlap) but the machine's own
  run-to-run variance is the same size, so **it is not a number to quote.** It does not affect
  anything in this document: every arm, every control and every solo baseline uses the same
  16-page window, so the offset cancels out of every ratio reported here. The absolute per-vCPU
  nanosecond figures in §1 and §6 are therefore ~10 ns pessimistic relative to `16`'s method.
* **Every fault number is a NET.** The faulting loop and the fault-free control loop execute
  identical instruction sequences over identical address streams; only the shadow L3 contents
  differ. The control is measured in the same process, on the same vCPU, immediately after.
* **Fault counts are asserted, not assumed.** Each run checks that exactly `pages × rounds` faults
  were taken per vCPU. Any mismatch is printed as `** FAULT COUNT MISMATCH`; none occurred.
* **The "pure scaling" arm is not secretly a memory-bandwidth measurement.** Per vCPU per round the
  working set is 512 pages × one 8-byte load = **64 KiB of distinct cache lines**, plus a 4 KiB
  shadow L3 and 16 KiB of x86 tables — **~84 KiB live**, inside the M2 Max P-core's 128 KiB L1D.
  Eight vCPUs together touch ~670 KiB, far inside the 16 MiB P-cluster L2. Nothing in this arm is
  shared between vCPUs: separate roots, separate tables, separate backing pages, separate x86
  tables. What is left to contend for is exactly the infrastructure: walkers, TLBs, stage-2, the
  exception unit, the fabric.
* **Every N is bracketed by a freshly measured N = 1.** DVFS state, thermal state and background
  load on this machine drift by 10–20 % over minutes (the same shadow fill measured 145.7 ns and
  186.4 ns in two runs an hour apart). Reporting `N` against an N = 1 taken at the top of the run
  would manufacture scaling artefacts. Every ratio in this document is against a solo run taken
  **immediately before** the multi-vCPU run it is compared to, and both absolute figures are shown.
* **Overlap witness.** Each vCPU records absolute `CNTVCT_EL0` at the open and close of its measured
  window. The `ovl` column is `(min end − max start) / (max end − min start)`: the fraction of the
  experiment during which *all* N vCPUs were simultaneously inside their measured windows. Rows
  with low `ovl` are flagged; a "scaling" number taken at `ovl ≈ 0` would not be a concurrency
  measurement at all.
* **Barriers.** Every mode begins with a bounded in-guest arrival barrier (`LDADDAL` + `LDAR` spin)
  so all N vCPUs start their measured windows together rather than staggered by thread-creation
  cost. The barrier self-traps via `HVC #10` if it fails to complete.
* **Hang safety.** Every guest spin has an explicit iteration bound and self-traps (`HVC #9`/`#10`/
  `#11`) past it; every group run is covered by a host watchdog thread that calls `hv_vcpus_exit()`
  on all vCPUs. The watchdog fired exactly once during development (the GIC probe, §3) and is
  reported there rather than hidden.
* **Trap guard.** The host run loop prints and counts any EL2 system-register trap (`EC 0x18`) or
  unexpected exit. None occurred inside any measured window in any reported run.
* **Repeats.** The shadow-fill scaling sweep and the TLBI sweep were each run **twice** in separate
  processes; the `EnTSO` cost table **four** times; the GIC probe **six** times; the single-vCPU
  cross-check against `16` **six** times. Where runs disagree that is stated rather than averaged
  away, and both figures are shown.

---

## 1 — Does in-VM fault handling scale?

512 private 4 KiB pages per vCPU per round, 300 rounds, 16 pages per timed sample; handler = read
`FAR_EL1`, walk a 4-level x86 table, synthesise the ARM descriptor, store it, `ERET` without
advancing `ELR`, **no `TLBI`**.

### 1a — Private page tables (pure infrastructure contention)

```
    N   solo ns    p50 ns     worst    p99 ns  vs solo agg faults/s    eff    ovl
    1    182.29    169.27    169.27    705.73   0.929x         5.91M   108%   1.00
    2    171.88    166.67    166.67    197.92   0.970x        12.00M   103%   1.00
    4    158.85    169.27    174.48    216.15   1.066x        23.83M    95%   0.89
    6    158.85    166.67    171.88    486.98   1.049x        35.93M    95%   0.86
    8    156.25    174.48    179.69    492.19   1.117x        46.50M    91%   0.86
                                                              (run 1)
    1    158.85    171.88    171.88    200.52   1.082x         5.82M    92%   1.00
    2    174.48    174.48    174.48    213.54   1.000x        11.46M   100%   0.99
    4    171.88    187.50    187.50    223.96   1.091x        22.13M    95%   0.95
    6    169.27    184.90    187.50    223.96   1.092x        33.10M    93%   0.93
    8    166.67    182.29    187.50    531.25   1.094x        44.31M    92%   0.78
                                                              (run 2)
```

* **Per-vCPU cost at 8 vCPUs is 1.09–1.12× solo.** Two independent runs agree to within 2 points.
* **Aggregate throughput is 44–47 M shadow fills/s at 8 vCPUs**, i.e. 7.5–7.9× the single-vCPU rate.
  Parallel efficiency **91–92 %**.
* **The spread between the fastest and the slowest of the eight vCPUs is 3 %** (174.5 vs 179.7 ns).
  No vCPU is being starved; the load is symmetric.
* The p99 rises from ~200 ns at N ≤ 4 to ~500 ns at N = 6–8. That is a real tail — see §6 — but it
  is 0.05 % of samples and does not move the median.

**Why it scales.** The fault path touches only per-core structures. The x86 walk (4 dependent loads)
and the descriptor store hit this vCPU's own L1D. The exception entry/`ERET` is a per-core pipeline
event. The stage-1 walk goes through HVF's stage-2, which is a *static* 256 MiB mapping — no
stage-2 faults, no `hv_vm_map` calls, no host involvement of any kind. Nothing in the loop requires
agreement with another core, so nothing in the loop serialises.

### 1b — Shared page tables (what a guest kernel actually does)

All N vCPUs run with the **same `TTBR0_EL1`, the same ASID and the same L0/L1/L2 tables**; each
still owns its own L3 table and its own pages (so the fault count per vCPU is unchanged and the
comparison is exact).

```
    N   solo ns    p50 ns     worst    p99 ns  vs solo agg faults/s    eff    ovl
    1    166.67    171.88    171.88    200.52   1.031x         5.82M    97%   1.00
    2    179.69    169.27    169.27    651.04   0.942x        11.82M   106%   0.95
    4    174.48    184.90    184.90    562.50   1.060x        22.28M    97%   0.91
    6    174.48    187.50    190.10    520.83   1.075x        32.80M    95%   0.88
    8    171.88    187.50    192.71    591.15   1.091x        43.31M    93%   0.78
```

**The contention delta between private and shared page tables is under 2 points of efficiency and
is inside the run-to-run noise.** Sharing the upper levels of the tree means eight walkers read the
same L0/L1/L2 cache lines — but they only *read* them, so the lines sit shared in every L1D and
cost nothing. This matters because it is the realistic configuration: a guest kernel's address
space is shared by every vCPU running kernel code.

> **The design does not need per-vCPU shadow page tables for performance.** Whatever reason there
> may be to give each vCPU its own tables, contention is not one of them.

### 1c — The same experiment with a `TLBI` on the fill path

```
    N     p50 ns     max ns     p99 ns agg faults/s
    1     369.79     369.79     895.83         2.70M   (1.00x N=1)      run 1
    2     627.60     627.60    1023.44         3.19M   (1.70x N=1)
    4     781.25     783.85    1210.94         5.15M   (2.11x N=1)
    8    1213.54    1229.17    1799.48         6.58M   (3.28x N=1)
    1     382.81     382.81     695.31         2.61M   (1.00x N=1)      run 3
    2     622.40     622.40     942.71         3.21M   (1.63x N=1)
    4     776.04     778.65    1078.12         5.17M   (2.03x N=1)
    8    1229.17    1231.77    1627.60         6.56M   (3.21x N=1)
```

Here every vCPU's handler ends with `DSB ISHST; TLBI VAAE1IS; DSB ISH; ISB`. Aggregate throughput
**saturates at 6.6 M fills/s** and per-vCPU latency degrades 3.3×. The eight vCPUs are no longer
independent: each one's `DSB` must wait for the DVM traffic generated by the other seven.

This is the same shape as `02 §6.1b`'s host-exit saturation (1.37 → 2.49 → 3.17 → 2.94 M exits/s at
1/2/4/8 vCPUs on M2 Pro), and for the same structural reason: a machine-wide serialising resource.
**The TLBI-free fill path has no such resource and therefore has no such ceiling.**

---

## 2 — Broadcast TLB maintenance at real vCPU counts

One vCPU issues the maintenance sequence in a tight loop (60 000 iterations × 15 rounds) while
N − 1 others run. Three contender behaviours. Each column is bracketed by a freshly measured solo
run; ratios are against that adjacent solo.

Two independent full sweeps were run (`results/run1.log`, `results/run3.log`). **The absolute
nanosecond figures at N = 8 reproduce to within a few per cent between them; the *ratios* move
more, because the solo baseline itself drifts by up to 50 % with machine state.** Both are given.
Where they disagree, trust the absolute figures.

### 2a — Contenders idle-spinning

```
                                       run 1                         run 3
  N (total vCPUs)                    1       4       8           1       4       8
  isb                            10.46   10.00    9.58        9.58   10.46    9.19
  dsb ishst; dsb ish; isb        18.85   18.75   17.77       17.77   18.57   17.77
  dsb nsh + TLBI VAE1           213.22  248.44  269.27      256.40  285.01  368.50
  dsb ish + TLBI VAE1IS         211.75  264.45  358.72      316.35  261.12  383.55
  dsb ish + TLBI VAAE1IS        207.07  251.72  294.74      248.27  267.94  276.51
  dsb nsh + TLBI VMALLE1        224.39  256.58  363.38      288.28  299.48  371.16
  dsb ish + TLBI VMALLE1IS      238.12  257.02  292.10      351.95  380.52  371.90
     ratio to adjacent solo, N=8       1.20 - 1.69x                0.83 - 1.44x
```

**With seven *idle* vCPUs the effect is small and does not clearly exceed the run-to-run noise.**
An invalidate costs 277–384 ns against a solo baseline of 207–352 ns. `16 §3`'s "+4 % to +18 % with
one contender" is *not* contradicted here: merely having other vCPUs exist and spin costs little.
It is what they are *doing* that matters.

### 2b — Contenders walking 4 KiB pages of their own (TLB-heavy, private VAs)

```
                                       run 1                         run 3
  N (total vCPUs)                    1       4       8           1       4       8
  dsb nsh + TLBI VAE1           209.98  378.29  998.20      243.65  374.75  937.36
  dsb ish + TLBI VAE1IS         208.23  359.10  960.64      253.48  378.43  907.42
  dsb ish + TLBI VAAE1IS        268.37  370.55  758.70      247.74  374.83  904.30
  dsb nsh + TLBI VMALLE1        299.19  850.97 1139.70      298.80  825.82 1152.40
  dsb ish + TLBI VMALLE1IS      318.05  839.65 1121.84      306.34  830.24 1147.24
     ratio, VAE1IS   solo->8          1.00 -> 4.47x               1.00 -> 4.18x
     ratio, VMALLE1IS solo->8         1.00 -> 4.57x               1.00 -> 3.86x
```

### 2c — Contenders walking **the same** pages being invalidated (the real shootdown)

```
                                       run 1                         run 3
  N (total vCPUs)                    1       4       8           1       4       8
  dsb nsh + TLBI VAE1           217.19  361.10  446.13      296.15  353.38  439.18
  dsb ish + TLBI VAE1IS         218.46  341.61  444.85      280.13  353.08  440.50
  dsb ish + TLBI VAAE1IS        217.37  352.98  443.46      285.71  353.14  442.17
  dsb nsh + TLBI VMALLE1        280.31  850.05  896.29      358.71  832.47  884.89
  dsb ish + TLBI VMALLE1IS      226.79  906.40  888.09      362.94  835.77  888.98
     ratio, VAE1IS   solo->8          1.00 -> 2.01x               1.00 -> 1.57x
     ratio, VMALLE1IS solo->8         1.00 -> 3.17x               1.00 -> 3.35x
```

The N = 8 absolute figures agree between runs to within **1 %** in the shared-VA arm
(444.85 / 440.50 for `VAE1IS`; 888.09 / 888.98 for `VMALLE1IS`) and within **5 %** in the private-VA
arm. This is the most reproducible part of the whole document.

**Four conclusions, and two of them supersede `16`.**

1. **`16 §3`'s "the multi-vCPU tax is small (+4…+18 %)" does not survive contact with real vCPU
   counts *doing real work*.** With seven other cores actively using their TLBs, a single-page
   invalidate costs **441–961 ns** instead of a 208–316 ns solo baseline — **1.6× to 4.5×**. A
   global invalidate costs **885–1152 ns** — **2.0× to 4.6×**. `16` explicitly warned that
   "the scaling to 8–12 vCPUs is not measured and should not be assumed flat", and it is not flat.

2. **`16 §3`'s other finding — that the broadcast is *not* what costs — survives, and is now
   stronger.** `TLBI VMALLE1` (non-shareable, `DSB NSH`, no DVM broadcast at all) degrades to
   **1140–1152 ns at 8 vCPUs, 3.7–4.3× its solo cost** — as badly as `VMALLE1IS`. A purely *local*
   invalidate cannot be slowed down by DVM traffic. What slows it down is the **`DSB` completion
   itself**: the barrier must wait for the memory system to quiesce, and a memory system being
   hammered by seven other cores takes much longer to quiesce. The `TLBI`'s cost is a
   *memory-system* cost, not an *interconnect-protocol* cost, and it therefore scales with how busy
   the machine is rather than with how many cores must be told.

3. **Selective invalidation is now worth ~2×, not 13 %.** `16 §3` measured `VAE1IS` and
   `VMALLE1IS` within 13 % of each other and concluded "the reason to prefer selective invalidation
   is entirely the refill it avoids". At 8 vCPUs the *instruction* costs differ by **2.0×**
   (441–445 vs 888–889 ns in the shared-VA arm, both runs) on top of the refill difference.
   **Prefer `VAE1IS`/`VAAE1IS` over `VMALLE1IS` for the instruction cost as well now.** Note the
   shape: at 8 vCPUs the VA-selective forms cluster tightly at ~441 ns and the global forms at
   ~888 ns, in both runs — a clean 2× split that only appears under contention.

4. **Caveat on 2b vs 2c.** The private-VA arm (2b) is the *harsher* number, but it is not purely a
   TLB effect: eight vCPUs walking 1024 private pages each touch 32 MiB of distinct memory and
   8192 distinct translations, so it loads the memory system harder than the shared-VA arm, which
   re-reads the same 4 MiB. The shared-VA arm (2c) is the honest analogue of an x86 TLB shootdown
   (all cores hold the entry being killed and must refill it) and gives **2.0× for VA-selective,
   3.2× for global**. Where the two arms disagree, quote 2c for shootdown cost and 2b as the
   upper bound under general memory load.

---

## 3 — Cross-vCPU signalling

Three mechanisms, measured end to end.

### 3a — Host-mediated (`hv_vcpus_exit`)

```
  (a) hv_vcpus_exit -> hv_vcpu_run returns on the target thread (the WAKE only):
      run 1  p50 1167 ns  p10 917  p90 1792  min 875   (n=40)
      run 3  p50  917 ns  p10 833  p90 1292  min 833   (n=40)
  (b) full host-mediated IPI: hv_vcpus_exit -> run returns -> set_pending_interrupt(IRQ)
      -> re-enter -> guest EL1 IRQ vector:
      wake                        p50  917 /  875 ns
      guest EL1 vector reached    p50 1542 / 1500 ns   (p10 1458/1417, p90 1875/1833)
      + trailing HVC back to host p50 2000 / 1917 ns   (whole cycle, host-timed)
```

`hv_vcpu_get_vtimer_offset` returns **0** on this host, so guest `CNTVCT_EL0` and host
`mach_absolute_time()` are the *same* 24 MHz counter; the "guest EL1 vector reached" figure is a
genuine cross-domain measurement (guest stamp minus host stamp), not an inference.

**This is 2× better than `02 §6.1` reports.** `02` measured `hv_vcpus_exit` → `hv_vcpu_run` returns
at **~3 100 ns** on M2 Pro; here the *whole* path — wake, host injects a pending IRQ, re-enter,
guest takes the exception at EL1 — is **1 500–1 542 ns**, and the wake alone is 875–917 ns. Either the M2 Max
is faster or `02`'s figure included the re-entry; either way **the planning number for a
host-mediated IPI on this machine is ~1.5 µs, not 3.1 µs**. Both figures reproduced in two
independent runs.

### 3b — Shared-memory doorbell, entirely in-VM

```
  (c) in-VM shared-memory flag round trip (STLR/LDAR, no host exit),
      64 round trips per timed window:
      run 1  round trip p50 68.4 ns  p90 69.0  p99 89.8  min 66.4
      run 3  round trip p50 68.4 ns  p90 69.0  p99 72.3  min 66.4
      one-way p50 34.2 ns   (identical to 0.1 ns across runs)
```

**A polled shared-memory flag between two vCPUs costs 34 ns one-way — 45× cheaper than the
host-mediated IPI and 100× cheaper than a GIC SGI (below).** It costs a spinning core, which is
the whole trade.

### 3c — Does the framework GIC deliver SGIs without a host exit? **Yes.**

This was the open question. `02 §3.1` established that `hv_gic_create()` works on Apple silicon and
that `hv_gic_get_ich_reg` is EL2-gated, but left SGIs explicitly **Unverified**: *"With a GIC: SGIs
are handled by the framework via the GICv3 `ICC_SGI1R_EL1` path, which should avoid the host round
trip entirely. **Unverified**."*

`probes/gic_sgi/gic_sgi.c` runs the experiment: create a GICv3, give each vCPU an `MPIDR_EL1`
affinity, map the distributor and each vCPU's own redistributor frames into guest stage-1 as
Device-nGnRnE, and let **guest EL1 itself** bring the interrupt controller up.

```
  distributor 65536 B (align 65536), redistributor region 33554432 B,
    per-vCPU 131072 B (align 65536), msi 65536 B (align 65536)
  hv_gic_create -> 0x0 OK
  vcpu0 redistributor base IPA 0x11000000
  vcpu1 redistributor base IPA 0x11020000
  GICD_CTLR (host view, before guest setup) = 0x50    GICD_TYPER = 0x478001f

  guest EL1 GIC setup, as read back inside the VM:
   vcpu0 ICC_SRE_EL1=0x7 trapped=0  GICR_WAKER 0->0  GICR_ISENABLER0=0xffff
         GICD_CTLR 50->52  ICC_IGRPEN1_EL1=0x1
   vcpu1 ICC_SRE_EL1=0x7 trapped=0  GICR_WAKER 0->0  GICR_ISENABLER0=0xffff
         GICD_CTLR 50->52  ICC_IGRPEN1_EL1=0x1
  receiver IRQs taken: 2001 / 2000   last ICC_IAR1_EL1 = 0x3
  cost of one MSR ICC_SGI1R_EL1 with no deliverable target: 1653.0 / 1713.6 / 1729.9 ns
  SGIs completed: 2000; attempts needed  1:1998-1999  2:1-2  >2:0
  hv_vcpu_run returns: vcpu0 = 1, vcpu1 = 1   (1 each = no host exit at all)

  SGI latency, ICC_SGI1R_EL1 write on vcpu0 -> EL1 IRQ vector on vcpu1:
    run A  p50 4083.3  p10 3541.7  p90 4791.7  p99 4833.3
    run B  p50 3458.3  p10 2666.7  p90 3458.3  p99 3500.0
    run C  p50 3583.3  p10 3166.7  p90 3625.0  p99 3708.3
    (n = 2000 each, three independent processes, CNTVCT granularity 41.67 ns)
```

**What is now established (`M`):**

* **A guest at EL1 can configure a GICv3 CPU interface, redistributor and distributor by itself.**
  `ICC_SRE_EL1` reads back `0x7` with **no EL2 trap**, `ICC_PMR_EL1`, `ICC_BPR1_EL1` and
  `ICC_IGRPEN1_EL1` are all writable from EL1, and `GICR_ISENABLER0` accepts `0xffff` (SGI 0–15).
  The guest set `GICD_CTLR` from `0x50` to `0x52` itself through its own Device-memory mapping.
* **`MSR ICC_SGI1R_EL1` on one vCPU delivers an IRQ to another vCPU's EL1 vector**, which reads
  `ICC_IAR1_EL1 = 0x3` (the INTID that was sent) and completes it with `ICC_EOIR1_EL1`.
* **No host exit occurs.** The host counted `hv_vcpu_run` returns: **exactly 1 per vCPU** across the
  whole 2 000-SGI run. `02 §3.1`'s "should avoid the host round trip entirely" is **confirmed**.
* **But it is slow: 3.5–4.1 µs p50.** And **1.65–1.73 µs of that is the `MSR ICC_SGI1R_EL1` write
  itself** — measured separately by aiming 4 096 SGIs at a non-existent affinity so nothing is ever
  delivered. A hardware `ICC_SGI1R_EL1` write is a handful of cycles; 1.7 µs means the register is
  **trapped and emulated**, in the kernel, without returning to userspace. The remaining
  ~1.8–2.4 µs is delivery to the target.
* **Delivery is not perfectly reliable under back-to-back use.** 1–2 SGIs per 2 000 required the
  sender to re-issue after a ~40 µs timeout (the attempt histogram is `1:1998–1999, 2:1–2, >2:0` in
  all three runs). An earlier version of the probe that panicked instead of retrying wedged after
  28–75 SGIs. **A guest IPI protocol built on this must be able to retransmit;** it cannot assume
  every `ICC_SGI1R_EL1` write lands.

> **Verdict on 3c.** `02`'s claim that the framework GIC removes interrupt-controller *exits* is
> **true and now measured**. But an SGI is the **most expensive** of the three signalling
> mechanisms — 3.5–4.1 µs, versus 1.5 µs for doing it by hand through `hv_vcpus_exit` +
> `hv_vcpu_set_pending_interrupt`, versus 68 ns for a shared-memory flag.
>
> **Design consequence:** use the GIC because it lets the *guest OS* use a standard interrupt
> controller, not because it is fast. For the emulator's own cross-vCPU traffic (translation-cache
> invalidation, shadow-table invalidation, stop-the-world requests) use a **polled shared-memory
> doorbell** and fall back to an SGI or a host wake only when the target is idle. `02 §6.1b`'s
> advice to "prefer shared-memory doorbell protocols" is quantitatively vindicated: the doorbell is
> **45–58× cheaper** than either interrupt path.

---

## 4 — TSO at scale

### 4a — Does `EnTSO` still enforce ordering at 4 and 8 vCPUs? Yes, absolutely.

MP-with-counters, exactly as `14 §G-4`: P0 stores an increasing counter to X then Y with no barrier;
readers load Y then X with no barrier. `r0(Y) > r1(X)` is forbidden under TSO and observable under
ARM's weak model. Two variants: N/2 **independent writer/reader pairs** on separate cache lines, and
**one writer with N−1 readers** on the same variables. 3 000 000 iterations per vCPU per round.

```
     N variant                           EnTSO  weak outcomes        witness
     2 N/2 independent pairs                 0        1057003         166096
     2 N/2 independent pairs                 1              0        1167224
     2 1 writer / N-1 readers                0        1019609         165528
     2 1 writer / N-1 readers                1              0        1631094
     4 N/2 independent pairs                 0        1245298         244040
     4 N/2 independent pairs                 1              0        5489620
     4 1 writer / N-1 readers                0         428241         185422
     4 1 writer / N-1 readers                1              0        1461502
     8 N/2 independent pairs                 0        4213751         542613
     8 N/2 independent pairs                 1              0        7470297
     8 1 writer / N-1 readers                0        7829111         336491
     8 1 writer / N-1 readers                1              0        4199015
                                                                       (run 1)
     2 N/2 independent pairs                 0        1106728         166830
     2 N/2 independent pairs                 1              0        2747056
     2 1 writer / N-1 readers                0        1150678         166996
     2 1 writer / N-1 readers                1              0        2593045
     4 N/2 independent pairs                 0        2047516         356934
     4 N/2 independent pairs                 1              0        4045088
     4 1 writer / N-1 readers                0         498020         286237
     4 1 writer / N-1 readers                1              0        4226872
     8 N/2 independent pairs                 0        3359558         196924
     8 N/2 independent pairs                 1              0        9306225
     8 1 writer / N-1 readers                0       11568516         274249
     8 1 writer / N-1 readers                1              0        3027600
                                                                       (run 3)
```

* **`EnTSO=1` produced zero weak outcomes in all twenty-four configurations** across two independent
  sweeps, up to 8 vCPUs, with both independent pairs and a shared variable read by seven readers.
* **The weak baseline is strongly positive everywhere** (0.4–11.6 M violations), so every `EnTSO=1`
  zero is a meaningful zero, not a failure to exercise the test.
* The concurrency witness (how often the writer advanced while a reader was looping) is 0.17–9.3 M
  in every row, so the threads genuinely overlapped.
* `ACTLR_EL1` was read back in-guest as `0x2` in every `EnTSO=1` row.

> **`14 §G-4` is extended from 2 vCPUs to 8.** Hardware TSO under Hypervisor.framework is not a
> two-core artefact. It holds for the whole machine.

### 4b — What does `EnTSO=1` cost under multi-core contention? Two answers, one of them a failure.

Four runs, each: N vCPUs, 64 KiB buffer, streaming `stp` (stores) or `ldp` (loads), buffer either
**private per vCPU** or **shared by all vCPUs** (true line-level sharing).

**Private buffers — INCONCLUSIVE, exactly as `16 §7` found.** Across four runs the N=1 ratio was
1.18, 1.36, 1.27, 1.32 and the N=8 ratio 1.20, 1.41, 0.92, 0.49. Ratios below 1.0 ("TSO made it
faster") recur, the `EnTSO=0` baseline is bimodal at ~0.19 and ~0.42 ns/8 B, and the pattern is not
monotone in N. **`16 §7`'s open item stays open. We could not stabilise it and are not reporting a
number.**

**Shared cache lines, stores — a large, reproducible effect at 8 vCPUs.**

```
  -- SHARED (all vCPUs write the same lines) buffer, streaming stp, 64 KiB
     N     run1        run2        run3        run4
     1   2.435x      1.007x      0.741x      1.525x
     2   1.505x      0.977x      3.226x      1.294x
     4   1.562x      1.830x      0.982x      1.164x
     8  10.264x     10.547x     12.631x      4.563x
```

At N = 8 the `EnTSO=0` arm is stable at **1.30–1.36 ns per 8 B** in all four runs while the
`EnTSO=1` arm goes to **6.2 / 10.3 / 13.5 / 16.5 ns per 8 B**. The direction and the order of
magnitude reproduce; the magnitude itself varies 4.6×–12.6×. At N ≤ 4 the measurement is as unstable
as the private case and nothing should be read into it.

**Shared cache lines, loads — no effect at all.** N = 1/2/4/8 ratios of 1.00–1.22 with no trend,
across all four runs. `16 §7`'s "on loads `EnTSO=1` costs nothing measurable" holds under contention.

> **What this means.** Total-store-order forbids the store buffer from reordering or coalescing
> stores. When eight cores fight over the same lines, every store must be made globally visible in
> program order, so the line ping-pongs once per store instead of once per burst. **On heavily
> write-shared lines, `EnTSO=1` costs something between 4.6× and 12.6× at 8 vCPUs.**
>
> This is a **narrow but real hazard**: it is exactly the profile of a spinlock cache line, a
> shared refcount, a producer-consumer ring head, or a false-shared per-CPU array. A translated
> x86 guest running a multithreaded workload will hit it. `01`'s "2–5 %" and `16 §7`'s "+7–8 % on
> DRAM-bound sequential stores" both describe the *uncontended* case and neither covers this.
>
> **Caveat, stated plainly:** the uncontended baseline on this machine is bimodal (§4b, `16 §7`) and
> we never found out why. The N = 8 shared-store result is reported because the *effect size* is far
> outside that instability and reproduced in four independent processes — but the exact multiplier
> is not a number to budget against.

---

## 5 — P-cores vs E-cores: the guest sees a heterogeneous machine

Clock estimated from a 16-deep dependent `ADD` chain (1 cycle ALU latency), measured in-guest.

```
  thread QoS              N     ns per ADD      est GHz     spread
  USER_INTERACTIVE        1         0.3457         2.89      1.00x
  USER_INTERACTIVE        8         0.4028         2.48      1.01x
      per-vCPU GHz: 2.47 2.48 2.48 2.48 2.48 2.48 2.48 2.48
  UTILITY                 1         0.4352         2.30      1.00x
  UTILITY                 8         0.4598         2.17      1.01x
      per-vCPU GHz: 2.17 2.17 2.17 2.18 2.17 2.18 2.17 2.17
  BACKGROUND              1         0.4563         2.19      1.00x
  BACKGROUND              8         0.8367         1.20      2.71x
      per-vCPU GHz: 2.37 2.66 1.22 1.20 1.05 0.98 1.12 1.43
  mixed QoS (4 interactive + 4 background), per-vCPU GHz: 2.80 2.80 2.81 2.78 2.04 1.52 1.65 1.64
                                                                              (run 1)
  USER_INTERACTIVE        8         0.3954         2.53      1.00x
      per-vCPU GHz: 2.53 2.53 2.53 2.53 2.53 2.53 2.53 2.53
  BACKGROUND              8         0.7487         1.34      3.01x
      per-vCPU GHz: 1.94 2.36 1.34 2.08 1.56 0.79 0.86 0.95
  mixed QoS (4 interactive + 4 background), per-vCPU GHz: 2.78 2.78 2.78 2.78 2.95 2.93 1.06 0.97
                                                                              (run 3)
```

* **`pthread_set_qos_class_self_np` on the vCPU thread does steer placement**, and the guest can
  see it: `QOS_CLASS_USER_INTERACTIVE` gives a uniform 2.48 GHz across eight vCPUs;
  `QOS_CLASS_BACKGROUND` gives 0.98–2.66 GHz with a **2.71× spread** — eight threads crowded onto
  four E-cores.
* **`QOS_CLASS_USER_INTERACTIVE` is perfectly uniform**: all eight vCPUs measured 2.48 GHz (run 1)
  and 2.53 GHz (run 3) — identical to two decimal places. Steer every vCPU thread this way and the
  guest sees a homogeneous machine.
* **The mixed configuration is the important one.** Four interactive + four background vCPUs
  running simultaneously in the same VM: **2.80 vs 1.52–2.04 GHz (run 1)** and **2.78–2.95 vs
  0.97–1.06 GHz (run 3)** — a sustained **1.4× to 3.0× spread with no way for the guest to know.**
  Note also that QoS is a *hint*: in run 3 two of the four background vCPUs landed at 2.93–2.95 GHz
  (P-cores) and two at ~1.0 GHz, so the asymmetry is not even stable within a run.
* All-core turbo costs ~13–16 %: one interactive vCPU runs at 2.83–2.89 GHz, eight at 2.48–2.53 GHz.
* Absolute clocks here are below `16`'s 3.18–3.32 GHz because this machine had ~3.1 load average
  from unrelated user applications throughout. The *ratios* are what this section is for.

> **Implication for the design, and it is not small.** A guest OS scheduler assumes homogeneous
> CPUs. Linux's load balancer, its `sched_clock`-based accounting, its spinlock backoff heuristics
> and any guest-side work-stealing all assume that a task moved from CPU 3 to CPU 7 runs at the same
> speed. On this host the emulator can silently hand the guest CPUs that differ by **1.4×–3.0×**,
> and the split is not even stable from run to run.
>
> The mitigations are (a) pin **all** vCPU threads to `QOS_CLASS_USER_INTERACTIVE` so they land on
> the same core class — measured to give a uniform 2.48–2.53 GHz across eight vCPUs — which caps
> the guest at 8 vCPUs on this machine; or (b) surface the asymmetry
> to the guest as a heterogeneous topology, which x86 guests have only understood since Alder
> Lake and most do not. **(a) is the only realistic option, and it makes "8 P-cores" a hard cap on
> useful vCPU count regardless of what `hv_vm_get_max_vcpu_count` reports (64).**

---

## 6 — Oversubscription

Shadow-fill scaling again, now at N > 8 and/or with additional host threads spinning
(`busy` = number of extra `QOS_CLASS_USER_INTERACTIVE` host threads, standing in for translator and
device-emulation threads).

```
     N   busy    p50 ns    p90 ns    p99 ns      max ns   >5xp50 agg faults/s    ovl
     8      0    200.52    234.38    257.81        5094    0.00%        40.72M   0.94
     8      4    182.29    200.52    518.23       78008    0.08%        45.02M   0.83
     8      8    156.25    177.08    203.12      349909    0.06%        51.33M   0.10
    10      0    161.46    190.10    208.33        6367    0.04%        62.19M   0.74
    12      0    164.06    190.10    208.33       40953    0.07%        74.03M   0.75
    12      4    161.46    184.90    205.73      811682    0.05%        74.99M   0.08
    12      8    158.85    179.69    197.92     1267846    0.06%        76.29M   0.00
```

* **The median does not move.** p50 stays 157–201 ns from 8 vCPUs on an idle machine to 12 vCPUs
  plus 8 competing host threads. p90 and p99 barely move either (177–234 / 198–518 ns).
* **The entire cost of oversubscription is in the extreme tail.** The worst single sample goes from
  **5.1 µs** (8 vCPUs, idle host) to **1.27 ms** (12 vCPUs + 8 host threads) — that is a macOS
  scheduler preemption of a vCPU thread mid-window, five orders of magnitude above the median.
  The rate of such events is **0.05–0.08 % of samples**.
* This shape is what you would expect and it is worth stating explicitly: **the hypervisor does not
  degrade gracefully under oversubscription, it degrades bimodally.** A vCPU is either running at
  full speed or descheduled for ~100 µs–1 ms. There is no middle. A guest that measures its own
  progress will see stalls, not slowdown.
* **The `ovl` column is the warning label.** At `busy ≥ 4` and N = 12 the overlap fraction collapses
  to 0.08 and then 0.00: the twelve vCPUs could no longer be made to run their measured windows
  simultaneously at all, because host scheduling skew exceeded the window length. **The aggregate
  throughput figures in those rows are therefore not simultaneous-throughput measurements and
  should not be quoted.** The rows with `ovl ≥ 0.74` (N ≤ 12, busy = 0) are sound, and they show
  aggregate rising to 74 M fills/s at N = 12 — the 4 E-cores are contributing real work.

---

## Does the Tier 2 design scale?

**Yes, and the ceiling is not where the corpus expected it.**

### Where the ceiling is

| Resource | Scales? | Ceiling on this machine | Evidence |
|---|---|---|---|
| In-VM stage-1 fault handling | **Yes, ~92 % efficient to 8 vCPUs** | ~45 M shadow fills/s | §1 |
| Shared guest page tables | **Yes, no measurable penalty** | — | §1b |
| Hardware TSO (`EnTSO`) correctness | **Yes, to 8 vCPUs** | — | §4a |
| Loads, private stores under TSO | **Yes, no measurable cost** | — | §4b |
| **`TLBI` + `DSB` completion** | **No** | 2–4.6× per-op degradation at 8 vCPUs | §2 |
| **Shadow fill *with* a `TLBI`** | **No** | **6.6 M fills/s, machine-wide** | §1c |
| **Stores to write-shared lines under TSO** | **No** | 4.6–12.6× at 8 vCPUs | §4b |
| Host exits (`02 §6.1b`) | **No** | ~3 M exits/s, machine-wide | `02` |
| GIC SGI / host-mediated IPI | flat but expensive | 1.5–4.1 µs each | §3 |
| Homogeneous vCPU speed | **No** | 1.4–3.0× spread, 8 P-core hard cap | §5 |

### What causes each ceiling

1. **`TLBI` does not scale because a `DSB` must wait for the memory system to quiesce, and a busy
   memory system takes longer to quiesce.** This is now measured rather than assumed: the
   *non-broadcast* `TLBI VMALLE1` degrades 4.3× at 8 vCPUs, which no DVM-broadcast explanation can
   account for. The consequence is that TLB maintenance cost is a function of **how busy the whole
   machine is**, not of how many cores must be notified — so it gets worse exactly when the guest is
   doing the most work.

2. **Host exits do not scale because they serialise in the hypervisor** (`02 §6.1b`, not re-measured
   here). Anything routed through the host is capped machine-wide at ~3 M/s no matter how many
   vCPUs you give it.

3. **TSO stores to write-shared lines do not scale because TSO is exactly the prohibition on
   coalescing them.** This is a property of the memory model, not of the implementation, and there
   is no way to optimise it away — only to avoid the sharing.

4. **vCPU speed is not homogeneous because the host is not homogeneous.** Nothing in
   Hypervisor.framework exposes core-class placement; the only lever is thread QoS.

### The design rules that follow

1. **The `16` design rule is now load-bearing, not an optimisation.** *Never issue a `TLBI` when
   turning an INVALID shadow descriptor into a VALID one.* Single-core it was worth 3.0×. At 8
   vCPUs it is worth **6.8× on aggregate throughput** (45 M vs 6.6 M fills/s). Everything else in
   this document is secondary to it.

2. **Batch, defer and coalesce every genuine invalidation.** A real invalidate (guest PTE edit,
   permission downgrade, unmap, A/D clear) costs ~210 ns on an idle machine and **441–1152 ns on a
   busy one**. `16`'s advice to batch was based on the 280 ns figure; the multi-core figure makes
   it 2–4× more urgent. Prefer `VAE1IS`/`VAAE1IS` over `VMALLE1IS` — at 8 vCPUs the *instruction*
   costs now differ by 2×, on top of the refill difference `16` identified.

3. **Prefer an ASID switch to a `TLBI` wherever the semantics allow.** `16 §5` measured an ASID
   switch at 10.3 ns and showed that a rollover costs almost nothing beyond the refill any fresh
   ASID pays. Under 8-vCPU contention the `TLBI` alternative has got 2–4.6× worse while the ASID
   switch — a local `MSR` + `ISB` — has not.

4. **Use polled shared memory for the emulator's own cross-vCPU traffic.** 68 ns round trip versus
   1.5 µs for a host-mediated IPI and 3.5–4.1 µs for a GIC SGI. Reserve interrupts for waking an idle
   vCPU, where the spin cost would otherwise be paid forever.

5. **Do create a framework GIC — for the guest, not for the emulator.** It works, the guest can
   drive it from EL1 with no host trap, and SGIs cross vCPUs with zero `hv_vcpu_run` returns. It is
   the right way to give a guest OS a standard interrupt controller. It is the *wrong* way to
   implement a fast internal IPI, and any protocol on top of it must tolerate an occasional lost
   SGI (1–2 per 2 000 observed).

6. **Cap the guest at 8 vCPUs and pin every vCPU thread to `QOS_CLASS_USER_INTERACTIVE`.**
   `hv_vm_get_max_vcpu_count` reports 64; the useful number on this machine is the P-core count.
   Beyond it the guest gets CPUs that differ by 1.4–3.0× in speed, which no x86 guest OS scheduler
   expects. Oversubscription past that point does not slow the guest down smoothly — it stalls
   individual vCPUs for up to **1.27 ms** at a time.

7. **Warn about write-shared lines under TSO.** If the translator ever gets to choose (its own
   internal data structures, its guest-visible paravirtual interfaces, its shadow-table metadata),
   pad to 128 B and avoid write sharing. On this machine 8 cores hammering one 64 KiB region cost
   **4.6–12.6× more with `EnTSO=1` than without**, and `EnTSO=1` is not optional for a
   correctness-preserving x86 emulator.

### The honest summary

> **The thesis of the Tier 2 design — handle guest MMU events inside the VM rather than exiting —
> gets *stronger* with more vCPUs, not weaker.** The in-VM path scales at 92 % efficiency to 8
> cores because it touches only per-core hardware. The exit path saturates machine-wide at ~3 M/s.
> At 1 vCPU the in-VM fault was 5.5× cheaper than a bare host exit (`16`); at 8 vCPUs the
> *aggregate* advantage is 45 M/s versus ~3 M/s, i.e. **~15×**.
>
> **The one thing that can throw that away is TLB maintenance, and it can throw away all of it.**
> A design that issues one `TLBI` per shadow fill scales to 6.6 M fills/s — barely twice the host
> exit rate — and would have made the entire in-VM argument marginal. The margin exists only
> because the invalidate is unnecessary on the invalid→valid transition, and that is now the
> load-bearing fact of the whole design.

---

## Corrections and additions to the corpus

* **`16 §3` "the multi-vCPU tax is small: +4 % to +18 %" is superseded.** That was one contender.
  With seven *busy* ones, a single-page invalidate costs **1.6–4.5×** and a global one
  **2.0–4.6×** its solo cost (441–961 ns and 885–1152 ns absolute). Seven *idle* contenders cost
  little, so it is what the other cores are doing that matters, not that they exist. `16` flagged
  this as unmeasured and warned against assuming it flat; the warning was right.
* **`16 §3` "broadcast is not the problem" is confirmed and strengthened.** The non-broadcast
  `TLBI VMALLE1` degrades as badly as `VMALLE1IS` (1140–1152 ns vs 1122–1147 ns at 8 busy vCPUs,
  both runs), which rules out DVM traffic as the cause. The cost is `DSB` completion in a busy
  memory system.
* **`16 §3` "selective vs global differs by only 13 %" is superseded for multi-core.** At 8 vCPUs
  they differ by **2.0×** in instruction cost alone.
* **`16 §2`'s design rule is promoted from "largest available optimisation" to "the thing the design
  depends on".** 3.0× at one vCPU, **6.8×** at eight.
* **`16 §7`'s open item on cache-resident `EnTSO` store cost remains open** — the private-buffer
  baseline is still bimodal and we could not stabilise it. **A new, separate result is added:** on
  *write-shared* lines at 8 vCPUs the cost is 4.6–12.6×.
* **`14 §G-4` is extended from 2 vCPUs to 8**, with both independent pairs and a shared variable.
  `EnTSO=1` produced zero weak outcomes in all twenty-four configurations across two sweeps.
* **`02 §3.1`'s "SGIs … Unverified" is now verified.** SGIs are delivered vCPU-to-vCPU with **zero**
  `hv_vcpu_run` returns; guest EL1 can bring up the GICv3 itself without a single EL2 sysreg trap.
  The cost is **3.5–4.1 µs**, of which **1.65–1.73 µs is the `MSR ICC_SGI1R_EL1` write itself** — the
  register is emulated in the kernel, not hardware-accelerated.
* **`02 §6.1`'s 3 100 ns for `hv_vcpus_exit` should be revisited.** On this M2 Max the wake is
  875–1167 ns and the *complete* IPI including guest EL1 vector entry is **1 500–1 542 ns**.
* **`02 §6.1b`'s "prefer shared-memory doorbell protocols" is quantified: 45–58× cheaper** than
  either interrupt mechanism (68.4 ns round trip vs 1.5 µs and 3.5–4.1 µs).
* **New: the useful vCPU cap on this host is 8, not 64.** `hv_vm_get_max_vcpu_count` returns 64, but
  P-core count is the real limit once you require homogeneous vCPU speed.
* **New: `hv_vcpu_get_vtimer_offset` is 0 by default**, so guest `CNTVCT_EL0` and host
  `mach_absolute_time()` are the same counter. This is a useful, cheap way to measure host↔guest
  latencies without a hypercall, and it is what makes §3a's cross-domain figure a measurement.

## What is still open

* **Everything here is one machine, one OS, one process.** M2 Max, macOS 26.5.2, ~3.1 load average
  from unrelated applications throughout. Absolute nanosecond figures carry ±20 % run-to-run; every
  scaling claim in this document is a ratio against an adjacent solo measurement for that reason.
* **Beyond 8 vCPUs the P/E boundary confounds everything.** N = 10 and N = 12 necessarily place
  vCPUs on E-cores; those rows measure a heterogeneous machine, not deeper scaling.
* **The `TLBI` contention mechanism is inferred, not isolated.** We measured that a *local* `TLBI`
  degrades as much as a broadcast one, which excludes DVM. We did **not** isolate which part of the
  memory system the `DSB` is waiting on.
* **The cache-resident `EnTSO` store baseline is still bimodal** and the cause is still unknown
  (`16 §7`). The N = 8 write-shared result is reported on effect size, not on a stable baseline.
* **SGI reliability is characterised but not explained.** 1–2 retransmits per 2 000 SGIs; an
  earlier non-retrying probe wedged after 28–75. We do not know whether this is a pending-state
  race in the framework GIC, a consequence of sending back-to-back SGIs with the same INTID, or
  something in our own EOI sequence.
* **Not measured: more than one VM.** All of this is one `hv_vm_create` with N vCPUs.
* **Not measured: stage-2 faults under concurrency.** The design assumes a static stage-2 map; a
  demand-paged one would add `hv_vm_map` calls, which are host-side and therefore in the
  non-scaling category by `02 §6.1b`.

**Reproduce:**
`cd probes && ./build.sh smp_scaling/smp_scaling.c && ./smp_scaling/bin/smp_scaling all`
(individual experiments: `pe`, `base`, `scale`, `scale2`, `tlbi`, `ipi`, `litmus`, `entso`,
`oversub`), and `./build.sh gic_sgi/gic_sgi.c && ./gic_sgi/bin/gic_sgi 2000`.

---

## Auditor's note (added on review, 2026-08-08)

**1. SGI reliability does not hold at higher volumes, and this document's figure is n-specific.**
§3 characterises delivery as "1–2 retransmits per 2 000 SGIs", measured with
`./gic_sgi/bin/gic_sgi 2000`. Re-running the *same probe on the same machine* at the binary's
default of 20 000 SGIs gives a materially worse picture:

```
receiver IRQs taken: 4610 / 20000
sender resends: 984965, gave up on: 15390
attempts needed  1:4607  2:1  3-4:2  ...  64(gave up):15390
hv_vcpu_run returns: vcpu0 = 1, vcpu1 = 1   (no host exit at all)
SGI latency p50 1791.7 ns  p90 3500  p99 4791.7  min 375  (n=4610)
```

So **77 % of SGIs were abandoned after 64 attempts** at 10× the volume. The *headline* claim — SGI
delivery to another vCPU's EL1 vector with **zero host exits** — reproduces exactly (one
`hv_vcpu_run` return per vCPU). What does not reproduce is the reliability. Delivery appears to
degrade with sustained rate, which §3 did not probe.

This **strengthens** this document's own recommendation (use shared-memory doorbells at 68 ns, not
SGIs, for emulator-internal signalling) and should be treated as a hard caveat on any guest IPI
protocol built over the GIC.

Note also that the probe's own trailing `VERDICT:` line is self-contradictory — it reports "the run
was not exit-free" while its own counter shows one `hv_vcpu_run` return per vCPU. The verdict
string keys on the give-up count, not the exit count. **Fix the probe's verdict logic before
quoting it.**

**2. The `EnTSO` same-cache-line penalty (4.6×–12.6×) is measured against the wrong alternative.**
§4 compares `EnTSO=1` against `EnTSO=0` *with no barriers*. That is not the design choice available
to us: a correct x86 emulator without hardware TSO must emit `DMB`/`LDAR`/`STLR`, which serialise
the same contended lines. The honest comparison is **TSO vs. barriers-everywhere**, not TSO vs. an
unordered baseline that no correct implementation could use. The 4.6–12.6× therefore **overstates**
the penalty relative to the real alternative by an unmeasured amount. The finding that heavy
same-line sharing is where TSO costs most is still valid and useful — the magnitude is not.

Follow-up worth doing: re-run the contended-line arm with a third configuration, `EnTSO=0` plus
`LDAR`/`STLR` on every access, to get the ratio that actually informs the design.

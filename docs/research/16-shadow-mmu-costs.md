# 16 — What Shadow Paging Actually Costs, Measured

**Date:** 2026-08-08 · **Host:** Apple M2 Max, macOS 26.5.2, SDK Hypervisor 259.5.15
**Probe:** `probes/shadow_mmu/shadow_mmu.c` + `probes/shadow_mmu/payload.h`
(build `probes/build.sh shadow_mmu/shadow_mmu.c`; raw log `probes/shadow_mmu/run-all.log`)

`14-gate-results-verified.md` proved the *mechanism* exists: 4 KB stage-1 at guest EL1, 4 KB
permission enforcement inside one 16 KB host page, hardware TSO. This document measures what the
mechanism **costs**. Every number below was taken with `CNTVCT_EL0` read **inside the VM at guest
EL1**, under our own `VBAR_EL1`, so no host exit occurs inside any measured window.

> **Reading the labels.** `M` = measured on this machine today. `D` = derived by subtracting one
> measured quantity from another. `X` = extrapolated/modelled from an `M` number — arithmetic, not
> observation. `?` = attempted and **inconclusive**.

---

## Bottom line

| # | Quantity | Cost | Cyc @3.25 GHz | Tag |
|---|---|---|---|---|
| 1 | **In-VM permission fault → our VBAR → ERET**, translation TLB-resident | **28.2 ns** | 92 | `D` |
| 2 | **In-VM translation fault → VBAR → ERET** (invalid PTE, always re-walks) | **94.4 ns** | 306 | `D` |
| 3 | In-VM permission fault, walk data cold / >L1 dTLB | 103–123 ns | 334–399 | `D` |
| 4 | **Full shadow fill: fault + ESR/FAR + 4-level x86 walk + descriptor + ERET** | **145.8 ns** | 473 | `D` |
| 5 | …the same, **plus `DSB`+`TLBI VAAE1IS`+`DSB`+`ISB`** | **439.2 ns** | 1425 | `D` |
| 6 | 4-level x86-format software walk alone (4 dependent loads, warm) | 1.14 ns | 3.7 | `M` |
| 7 | walk + x86→ARM descriptor translation + store to shadow L3 | 5.13 ns | 17 | `M` |
| 8 | **any single-page TLBI + required barriers** (`VAE1`/`VAE1IS`/`VAAE1IS`) | **279–285 ns** | ~910 | `M` |
| 9 | `TLBI VMALLE1`/`VMALLE1IS` + barriers | 305–321 ns | 990–1043 | `M` |
| 10 | the same eight TLBI forms with a second vCPU running | 295–357 ns (+6…+18 %) | — | `M` |
| 11 | `DSB ISHST; DSB ISH; ISB` with no TLBI at all | 17.6 ns | 57 | `M` |
| 12 | `ISB` alone | 9.3 ns | 30 | `M` |
| 13 | `MSR TTBR0_EL1` (new ASID) + `ISB` | 10.3 ns | 33 | `M` |
| 14 | TLB refill after moving to a fresh ASID | 20.4–22.5 ns / 4 KB page | 66–73 | `D` |
| 15 | extra cost of a **full ASID rollover** (`TLBI VMALLE1IS`) over a fresh ASID | **≈ 0.1–1.1 ns/page** | — | `D` |
| 16 | AF fault → set AF → ERET (**with** TLBI) | 441.4 ns | 1432 | `D` |
| 17 | AF fault → set AF → ERET (**no** TLBI) | 139.0 ns | 451 | `D` |
| 18 | `AT S1E1R` / `S1E1W` / `S1E1RP` / `S1E1WP` + `ISB` + read `PAR_EL1` | **26.4 ns** (all four identical) | 86 | `M` |
| 19 | 4 KB vs 16 KB stage-1 pages, TLB-bound pointer chase | 1.0× … **2.7×** (peak at 16–24 MiB) | — | `M` |
| 20 | `ACTLR_EL1.EnTSO=1` on loads / pointer chase | **0 % (1.000×)** | — | `M` |
| 21 | `EnTSO=1` on DRAM-bandwidth-bound sequential stores | **+7…8 %** | — | `M` |
| 22 | `EnTSO=1` on cache-resident sequential stores | **inconclusive** (baseline bimodal) | — | `?` |

**Reference points this must be compared against** (`02`, `12`, `14`): a bare `HVC` round trip to
the host is **808 ns**, an MMIO exit **890 ns**, and the previously measured *host-side* data-abort
handling path **11.5 µs**.

### The single most important number

> **A stage-1 fault handled entirely inside the VM costs 28–95 ns. The same fault handled by
> exiting to the host costs 808 ns at the absolute floor and 11.5 µs on the path actually
> measured.**
>
> That is **8.5× cheaper** than a bare exit for a translation fault, **29× cheaper** for a
> permission fault, and **~120×** cheaper than the measured host-side abort path.
>
> **It is not ~100× cheaper than a bare host exit. It is ~5–29×.** The thesis survives, but the
> margin is one order of magnitude, not two, and it is *destroyed* by a single TLBI (item 5) if the
> handler issues one.

---

## Method and its guards

* **Clock.** `CNTFRQ_EL0 = 24 MHz` (41.67 ns/tick), so every measurement batches ≥ 512 iterations
  into one timed window and divides. `PMCCNTR_EL0` **traps to EL2** from guest EL1 under HVF
  (`ESR` EC `0x18`, confirmed as a real host exit), so there is no in-guest cycle counter. The
  clock is estimated from a 16-deep dependent `ADD` chain (1-cycle ALU latency): **3.18–3.32 GHz**
  across runs. Cycle columns above are therefore ±5 % and are `X`, not `M`.
* **Counter-read overhead measured and accounted.** `{ISB; MRS CNTVCT_EL0}` back-to-back costs
  **9.1 ns**. It is paid twice per *round*, not per iteration, so with ≥ 512 iterations/round it
  contributes < 0.04 ns/iteration. An empty `{SUBS; B.NE}` loop iteration is **0.30 ns**, which
  bounds the loop scaffolding.
* **Every fault measurement has a matched control.** The faulting loop and the non-faulting loop
  execute *identical* instruction sequences over *identical* address streams; only the target
  region's permissions differ. The reported "NET" is the difference. The control's addresses are
  spread across cache sets (`base + page*4096 + (i&31)*128`) precisely so the control does not pay
  cache misses that the faulting variant — whose store never executes — never pays.
* **Branch-predictor / address-predictor guard.** The fault loop was run over 1, 64 and 512
  distinct faulting pages. Rotating the address changes the answer materially (see below), so the
  single-address figure is *not* the one quoted.
* **TLB-vs-cache guard.** The 4 KB/16 KB comparison uses the *same physical pages*, the *same node
  addresses*, the *same random permutation* and the *same number of chased nodes* in both arms —
  one node per 4 KiB of span in both cases. Data footprint, chain length and access order are
  therefore identical and the **only** difference is how many distinct stage-1 pages the stream
  touches. Nodes are placed at a rotating intra-page offset so they do not collide in cache sets.
* **Trap guard.** The probe counts EL2 traps (`EC 0x18`) per run and warns if more than one occurs.
  Across the whole run **zero** traps were seen inside any measured mode — in particular the
  ~1.5 M `TLBI` instructions per variant executed at EL1 without exiting. The 279 ns is a real
  in-guest cost, not a disguised hypercall.
* **Hang safety.** Every instrumented handler carries a fault counter and a limit and self-traps via
  `HVC #9` past it; every guest run is covered by a host watchdog thread that calls
  `hv_vcpus_exit()`. Neither fired in the final run.
* **Spread.** Each figure is the median of 15–41 in-guest rounds; the `[lo .. hi]` columns in the
  raw log are the 10th/90th percentile. The headline fault and shadow-fill numbers were
  additionally repeated across **5 independent process invocations** (see §1, §2).

---

## 1 — In-VM stage-1 fault round trip

Guest EL1 executes `STR XZR, [Xn]` in a counted loop against three regions: an **unmapped** one
(translation fault, DFSC `0x07`), a **read-only** one (permission fault, DFSC `0x0f`), and a
**read-write** one (no fault, control). The handler in our own `VBAR_EL1 + 0x200` is four
instructions — `MRS ELR_EL1; ADD #4; MSR ELR_EL1; ERET`.

```
  -- warm: 64 faulting pages
  loop iteration, no fault (control)              0.81 ns     2.6 cyc
  loop iteration + translation fault             95.21 ns   309.0 cyc
  loop iteration + permission fault              29.00 ns    94.1 cyc
  => NET translation-fault round trip            94.40 ns   306.4 cycles
  => NET permission-fault round trip             28.20 ns    91.5 cycles
```

Across five independent invocations the translation-fault figure was
**91.5, 91.7, 92.2, 93.9, 94.3 ns** — a 3 % spread.

**Three things fall out, and two of them were not expected:**

1. **A permission fault is 3.3× cheaper than a translation fault** (28 ns vs 94 ns). This is not
   noise; it is architectural. VMSAv8-64 permits caching only *valid* descriptors, so a translation
   fault must re-run the full 4-level stage-1 walk — **nested through HVF's stage-2** — on *every*
   occurrence, and can never be accelerated by the TLB. A permission fault finds the translation in
   the TLB and only fails the permission check, so it costs the bare exception-entry + `ERET`
   pipeline flush and nothing else. **92 cycles is the true floor of the in-VM trap mechanism.**
2. **The permission-fault path is bimodal**, and this was initially mistaken for a handler effect.
   It costs 28 ns when the walk data is cache-resident and the page count fits the L1 dTLB, and
   103–123 ns when it is cold or when 512 distinct pages are in play. Reordering the experiment
   showed the cheap mode depends on **position in the run, not on the handler**: the first
   configuration executed in a process always pays ~123 ns and every later one pays ~28 ns, and a
   512-page configuration pays ~103 ns wherever it sits. The residual ~75–95 ns is one memory
   round trip for the L3 descriptor line. Both modes are reported; **the design should assume the
   pessimistic one for cold working sets.**
3. **An out-of-line handler is free.** Putting the four handler instructions directly in the 128-byte
   vector slot versus branching to them (`B` + 4 instructions) is indistinguishable
   (94.70 vs 93.94 ns). Real handlers can be as long as they need to be.

*A methodological note worth recording:* an early version of this experiment produced a
non-reproducible 21 ns permission-fault figure. The cause was a **stale guest instruction cache** —
the host rewrites the vector table between configurations and the guest had no `IC IALLU`. The
payload now invalidates I-cache after enabling the MMU. Any future probe that rewrites guest code
between `hv_vcpu_run` calls must do the same.

---

## 2 — Full shadow-fill sequence

The realistic handler: read `FAR_EL1`, walk a 4-level **x86-format** page table in software (four
dependent loads), translate the x86 PTE into an ARM stage-1 descriptor (output address, `AF`,
`SH`, `UXN`/`PXN`, and `AP` from the x86 `R/W` bit), store it into the shadow L3 table, and `ERET`
without advancing `ELR` so the faulting access retries. 512 pages per round, 25 rounds; the probe
asserts that exactly 12 800 faults were taken, i.e. **every page faulted exactly once and the fill
was correct**.

```
  -- handler ends with dsb ishst; TLBI VAAE1IS; dsb ish; isb
  FULL: touch + fault + fill + TLBI + ERET      439.94 ns  1428 cyc
    component: bare touch loop, all mapped        0.73 ns
    component: 4-level x86 walk only              1.14 ns
    component: walk + xlate + descriptor store    5.13 ns
    component: dsb + TLBI VAAE1IS + dsb + isb   260.99 ns
  => NET per shadow-fill fault                  439.21 ns   (TLBI = 59 % of it)

  -- handler ends with dsb ishst; isb   (no TLBI)
  FULL: touch + fault + fill + ERET             146.89 ns   477 cyc
  => NET per shadow-fill fault                  145.83 ns   (walk+xlate+store = 4 % of it)
```

Five independent invocations: with TLBI **405.8, 412.7, 413.9, 414.3, 469.3 ns**; without TLBI
**141.7, 141.8, 141.9, 145.2, 160.6 ns**.

**The dominant term is not the walk. It is the TLB maintenance.** The software x86 walk that the
whole design exists to eliminate costs **1.14 ns**; translating and storing the descriptor brings
it to **5.13 ns**; together they are **3.5 %** of a shadow-fill with TLBI and **4 %** without. The
exception round trip is ~95 ns. Everything else is `TLBI` + `DSB`.

### The TLBI on the fill path is unnecessary, and dropping it is a 3× win

VMSAv8-64 does not permit a TLB to hold an entry for an invalid descriptor. A shadow fill that
turns an **invalid** L3 entry into a **valid** one therefore needs no invalidation — only a
`DSB ISHST` so the table write is visible to the table walker, and an `ISB`. The `no TLBI` arm
above tests exactly this and **the fault count is still exactly 512 per round**: no page faulted
twice, so the newly written descriptor took effect without any invalidate. This is measured
behaviour that agrees with the architecture, not an inference.

That takes the shadow-fill inner loop from **439 ns to 146 ns**. It is the single largest
optimisation available in this design and it is free.

The TLBI is still mandatory when the shadow entry is being **changed or downgraded** (guest PTE
edit, permission narrowing, unmap, A/D clearing) — and there it costs the full ~280 ns.

---

## 3 — TLB maintenance

```
                                            solo        with a 2nd vCPU running
  isb                                       9.27 ns          9.19 ns
  dsb ishst; dsb ish; isb                  17.61 ns         17.77 ns
  dsb nsh + TLBI VAE1     + dsb + isb     279.20 ns        308.30 ns   (+10 %)
  dsb ish + TLBI VAE1IS   + dsb + isb     284.89 ns        295.13 ns   (+4 %)
  dsb ish + TLBI VAAE1IS  + dsb + isb     279.77 ns        297.49 ns   (+6 %)
  dsb nsh + TLBI VMALLE1  + dsb + isb     304.67 ns        351.66 ns   (+15 %)
  dsb ish + TLBI VMALLE1IS+ dsb + isb     321.22 ns        356.59 ns   (+11 %)
```

**The broadcast/non-broadcast distinction is essentially free, and that is the surprise.**
`VAE1` (non-shareable, `DSB NSH`) and `VAE1IS` (inner-shareable broadcast, `DSB ISH`) are within
2 % of each other. Likewise `VMALLE1` and `VMALLE1IS`. Whatever the cost is, it is not the DVM
broadcast — it is the completion barrier itself. The barriers alone without any TLBI cost 17.6 ns,
so the TLBI contributes ~260 ns of *completion latency* that `DSB` must wait out.

**Consequences:**

* The multi-vCPU tax that `09` worried about is small: **+4 % to +18 %** with one contending vCPU.
  Broadcast is not the problem. This machine had only one contender; the scaling to 8–12 vCPUs is
  **not measured** and should not be assumed flat.
* Selective (`VAE1IS`) versus global (`VMALLE1IS`) invalidation differs by only **13 %** in
  instruction cost. The reason to prefer selective invalidation is entirely the **refill** it
  avoids (§5), not the instruction.
* At ~280 ns, **a TLBI costs a third of a full host exit.** Any design that issues one TLBI per
  guest PTE write has thrown away most of the benefit of not exiting.

---

## 4 — 4 KB vs 16 KB TLB reach

Identical physical working set, identical chased-node addresses, identical random permutation,
identical node count (one per 4 KiB of span in *both* arms). Only the stage-1 page size differs, so
the only variable is how many distinct pages the stream touches.

```
      span    nodes  4K pages   4K ns/acc 16K pages  16K ns/acc   4K/16K
       1 M      256       256        0.89        64        0.89    1.00x
       2 M      512       512        7.64       128        5.97    1.28x
       4 M     1024      1024        8.03       256        5.83    1.38x
       8 M     2048      2048        7.88       512        6.97    1.13x
      12 M     3072      3072        8.26       768        7.54    1.10x
      16 M     4096      4096       36.76      1024       13.72    2.68x   <-- 4K knee
      20 M     5120      5120       48.24      1280       25.34    1.90x
      24 M     6144      6144       58.24      1536       37.00    1.57x
      32 M     8192      8192       62.65      2048       40.96    1.53x
      48 M    12288     12288       56.67      3072       35.16    1.61x
      64 M    16384     16384       60.22      4096       44.83    1.34x
     128 M    32768     32768       65.94      8192       57.97    1.14x
     256 M    65536     65536       78.24     16384       66.04    1.18x
```

Three independent sweeps agree on the shape.

* **The 4 KB knee is between 12 and 16 MiB in all three runs.** 3072 entries × 4 KiB = 12 MiB.
  **`12`'s "12 MiB" half of rank 8 is confirmed exactly.**
* **The 16 KB knee is between 16 and 32 MiB, and moves between runs.** It is *not* at 48 MiB.
  Across the three sweeps the 16 KB curve had already left its floor by 16–20 MiB. The effective
  reach advantage is **~1.3–2.7×, not 4×**. **`12`'s "48 MiB" half of rank 8 is not supported by
  measurement and should be corrected.** The likely reason is that the entry budget is not a flat
  3072 for both page sizes, and that HVF's stage-2 (16 KiB IPA granule here) participates in the
  combined entry.
* **The penalty of choosing 4 KB is real but bounded and strongly size-dependent:** nil below
  2 MiB, 1.1–1.4× from 2–12 MiB, a peak of **1.9–2.7× in the 16–24 MiB band**, and 1.1–1.6× above
  32 MiB where both page sizes miss anyway.

Caveat: above ~32 MiB span the chased-node data footprint itself (span/32 bytes) begins to leave
the last-level cache, so both curves rise for reasons unrelated to the TLB. That inflates both arms
equally and therefore does not bias the **ratio**, which is the quantity of interest — but it does
mean the absolute `ns/access` numbers at large spans are a memory-system figure, not a TLB figure.

---

## 5 — ASID budget (N-11)

```
  working set 3072 pages (12 MiB), one full cold pass per round
  chase, warm, same ASID                          7.91 ns/access
  TTBR0_EL1 ASID switch (msr+isb), each          10.29 ns
  chase after switching away and back             7.92 ns/access
  chase under a BRAND NEW ASID                   28.31 ns/access
  chase right after TLBI VMALLE1IS               28.43 ns/access
  => refill penalty of a fresh ASID              20.40 ns/page  ->  62.7 us for the working set
  => refill penalty after TLBI VMALLE1IS         20.52 ns/page  ->  63.0 us for the working set
```

* **ASID tagging works and is cheap.** Switching `TTBR0_EL1` to a different ASID and back leaves
  the working set fully resident (7.92 vs 7.91 ns/access): entries are genuinely ASID-tagged and
  survive. The switch instruction pair costs **10.3 ns**.
* **A fresh ASID costs a full refill: ~20.4 ns per 4 KiB page.** This is not an ASID-shortage cost;
  it is what any address-space switch to a cold ASID costs.
* **An ASID rollover costs almost nothing *beyond* that fresh-ASID refill** — 20.52 vs
  20.40 ns/page, i.e. **0.1 ns/page or 0.6 %**. The reason is structural: the rollover flushes
  entries the incoming address space could not have used anyway.
* **The refill is capped by TLB capacity, not by working-set size.** At an 8192-page (32 MiB)
  working set the warm, fresh-ASID and post-flush passes are all ~62 ns/access: nothing was
  resident to lose. **The worst case a rollover can cost is therefore bounded at roughly
  3072 entries × 20.4 ns ≈ 63 µs**, plus one 321 ns `TLBI VMALLE1IS`.

**Model for a Linux guest (`X`, arithmetic on the measured 63 µs bound).** With 256 ASIDs, a
rollover occurs once per 256 activations of address spaces not currently holding an ASID. Taking a
busy guest at 10 000–50 000 context switches/s with a 20 % ASID-cache miss rate gives 2 000–10 000
misses/s, i.e. **8–40 rollovers/s**. At the 63 µs upper bound that is **0.5–2.5 ms of CPU per
second, or 0.05 %–0.25 % of one core.**

> **N-11 is quantitatively a non-issue.** 256 ASIDs versus 4096 PCIDs costs well under 1 % of a
> core even under pessimistic assumptions, because the flush destroys only what fits in a
> 3072-entry TLB and because the incoming address space had to refill regardless. A generation-based
> allocator is still the right implementation, but it is an ordinary engineering task, not a risk.
> `09` N-11's "precisely the storm that using the hardware MMU was supposed to avoid" is overstated.

---

## 6 — A/D bit emulation (N-12)

```
  -- handler sets AF + TLBI VAAE1IS + barriers        (12800/12800 faults, correct)
  touch page with AF=0 (fault + set AF + retry)  442.46 ns
    bare touch loop, AF already set                1.06 ns
  => NET AF-fault cost per page                  441.41 ns   (1432 cycles)

  -- handler sets AF, dsb ishst only, no TLBI        (12800/12800 faults, correct)
  => NET AF-fault cost per page                  139.00 ns    (451 cycles)

  AT S1E1R  + isb + read PAR_EL1                  26.37 ns
  AT S1E1W  + isb + read PAR_EL1                  26.37 ns
  AT S1E1RP + isb + read PAR_EL1                  26.37 ns
  AT S1E1WP + isb + read PAR_EL1                  26.37 ns
```

* **AF-fault-driven Accessed-bit emulation costs 139 ns/page without a TLBI, 441 ns/page with
  one.** Both arms took exactly 512 faults per round, so **setting `AF` without an invalidate was
  observed to work on this hardware.** ⚠️ Unlike the invalid→valid case in §2, this is *not*
  architecturally guaranteed — an implementation is permitted to cache an `AF=0` entry, and a
  design relying on the 139 ns figure must either re-validate it on every target CPU or accept the
  441 ns figure.
* **`AT S1E1R`, `S1E1W`, `S1E1RP` and `S1E1WP` all cost 26.4 ns and are indistinguishable from one
  another.** `FEAT_PAN2` costs nothing extra. `06`'s "10–20 cycles" for `AT` is **too optimistic by
  4–8×**: it is ~86 cycles including the mandatory `ISB` and `PAR_EL1` read.

**Model for a 1 GiB working set (`X`, arithmetic on the measured per-page figures; 262 144 pages):**

| Strategy | Per full A-bit epoch | As a fraction of one core, at 1 epoch/s |
|---|---|---|
| AF fault, no TLBI | **36.4 ms** | 3.6 % |
| AF fault, with TLBI | **115.7 ms** | 11.6 % |
| `AT S1E1R` sweep, fault-free | **6.9 ms** | 0.7 % |

`06`'s estimate of "0.15 s/GiB" is close to the with-TLBI figure and **4× pessimistic** if the TLBI
is elided. The `AT`-based sweep is **5.3× cheaper than the cheapest fault-driven variant** and, more
importantly, does not disturb the TLB or the pipeline at all — `06 §6`'s proposal to use `AT` for
fault-free probing is **vindicated by measurement** and should be promoted from "consolation" to
the primary A-bit strategy.

---

## 7 — Cost of `ACTLR_EL1.EnTSO = 1`

Four workloads × three cache tiers × `EnTSO ∈ {0,1}`, ten repeats per setting in ABBA order
(the reported ratio is the median of ten per-run medians). `ACTLR_EL1` read back in-guest as
`0x0` and `0x2` respectively every time.

| Workload | 32 KiB (L1) | 1 MiB (L2) | 16 MiB (DRAM) |
|---|---|---|---|
| streaming `ldp` (per 8 B) | 1.016× | 1.001× | 0.994× |
| scattered `str` (per 128 B line) | 0.990× | 1.003× | 1.002× |
| dependent pointer chase (per load) | 0.979× | 0.999× | 0.998× |
| streaming `stp` (per 8 B) | 0.828× **?** | 0.971× **?** | **1.080×** |

* **On loads, scattered stores and dependent pointer chasing, `EnTSO=1` costs nothing measurable
  — 1.000× ± 0.02 with very tight distributions.** This is the majority of what a translated x86
  workload does.
* **On DRAM-bandwidth-bound sequential stores it costs a clean, reproducible +7…8 %**
  (0.1301 → 0.1405 ns per 8 B; both distributions tight; +7.0 %, +7.4 %, +8.0 % across three
  separate runs).
* **On cache-resident sequential stores the measurement is INCONCLUSIVE and is reported as a
  failure.** The `EnTSO=1` arm is always tightly clustered (e.g. 0.0769 ± 0.003 at L1), but the
  `EnTSO=0` baseline is **bimodal**, landing at either ~0.077 or ~0.125 ns/8 B — a 1.6× spread —
  seemingly at random per run. Consecutive whole-run ratios for the L2 case were 1.560×, 1.195×,
  0.997×, 0.971×; for the L1 case 0.825×, 0.819×, 0.828×. A ratio below 1.0 means TSO "made it
  faster", which is not a physical result; it means the baseline, not the TSO arm, is unstable.
  We could not identify the cause (core/cluster placement and DVFS state are the obvious suspects)
  and we are not going to report a number we cannot reproduce.

> **Verdict on `01`'s "2–5 %".** Not confirmed as a general figure and not refuted either. For
> everything we could measure stably the cost is **0 %**; the one stable non-zero result is **+7–8 %
> on pure sequential store bandwidth**, which is *worse* than `01`'s figure but applies to a narrow
> class of code. `14 §G-4`'s open item is **partially closed**: the cost is negligible for
> latency-bound and load-bound code and is a single-digit percentage for store-bandwidth-bound code.
> The cache-resident store case remains **open**.

---

## Implications for the Tier 2 design

### Does the shadow-paging thesis survive its own costs? Yes — with one mandatory design rule.

The thesis is that handling guest MMU events inside the VM beats both software TLB emulation and
exiting to the host. All three legs check out, but not with the margins the corpus assumed.

**1. Versus exiting to the host: the win is ~5–29×, not ~100×.** A translation fault handled
in-VM is 94 ns against a bare 808 ns exit (8.5×) and against the 11.5 µs measured host abort path
(~120×). A *complete* shadow fill — fault, ESR/FAR, four-level x86 walk, descriptor synthesis,
store, `ERET` — is **146 ns**, i.e. **5.5× a bare host exit**. That is a real and decisive win but
it is one order of magnitude, and anyone budgeting on "100× cheaper" is wrong by ~20×.

**2. Versus software TLB emulation: the win is enormous and holds.** `12` rank 1 costs a softMMU
guest load at 0.466 ns versus 0.267 ns for a native `ldr`. Shadow paging makes the hot path a plain
`ldr` and pays 146 ns per *miss*. The break-even is one shadow fill per ~730 guest memory accesses;
any working set with better locality than that wins, which is essentially all of them.

**3. The dominant term is TLB maintenance, and it must be avoided rather than optimised.**

| Term in the shadow-fill path | Cost | Share (with TLBI) | Share (no TLBI) |
|---|---|---|---|
| `TLBI` + completion barriers | 261–280 ns | **59 %** | 0 % |
| exception entry + `ERET` (translation fault) | ~94 ns | 21 % | **65 %** |
| walk + descriptor synthesis + store | 5.1 ns | 1 % | 4 % |
| remainder (handler prologue, `DSB ISHST`, `ISB`, retry) | ~79 ns | 18 % | 31 % |

The software x86 walk that the entire design exists to eliminate is **1 % of the cost**. Optimising
the walk is pointless. What matters is (a) not issuing a TLBI, and (b) taking fewer faults.

> **Design rule, and it is the main deliverable of this document:**
> **the shadow-fill handler must not issue a TLBI when it is turning an INVALID descriptor into a
> VALID one.** This is architecturally sound — VMSAv8-64 forbids caching invalid entries — and it
> was verified experimentally here (exactly one fault per page, no repeats). It is worth **3.0×** on
> the shadow-fill path (439 → 146 ns) and **3.2×** on the A-bit path (441 → 139 ns). Conversely,
> every genuine invalidation — guest PTE edit, permission downgrade, unmap, A/D clear — costs
> ~280 ns and should be batched, deferred, or replaced by an ASID switch wherever the semantics
> allow.

**4. Secondary conclusions that change the corpus.**

* **`12` rank 8 splits.** The "12 MiB" 4 KB reach figure is **confirmed exactly**. The "48 MiB"
  16 KB figure is **wrong**: measured 16 KB reach is 16–32 MiB, so the loss from choosing 4 KB is
  **~2×, not 4×**, and the penalty on TLB-bound code is 1.0–2.7× depending sharply on working-set
  size (worst in the 16–24 MiB band, mild above and below).
* **`09` N-11 (ASIDs) should be downgraded to low priority.** Measured rollover cost is bounded at
  ~63 µs and models to **< 0.25 % of a core**. The extra cost of a rollover *over and above* the
  refill a fresh ASID pays anyway is **0.6 %**.
* **`09` N-12 (A/D) should be re-scoped from "fault-driven, expensive" to "use `AT`".** `AT S1E1R`
  at 26.4 ns is 5.3× cheaper than the cheapest fault-driven AF update and does not perturb the TLB.
  `06 §6`'s proposal is confirmed. But `06`'s "10–20 cycles" for `AT` is **4–8× optimistic**;
  the real figure is ~86 cycles.
* **Permission faults are 3.3× cheaper than translation faults** (28 vs 94 ns) because invalid
  descriptors can never be TLB-cached and must re-walk, nested through stage-2, every time.
  A shadow design that keeps entries *present but restricted* (for write-protection of guest page
  tables, SMC detection, dirty tracking) pays materially less than one that keeps them *invalid*.
  This is a first-class design lever that the corpus has not previously identified.
* **There is no in-guest cycle counter.** `PMCCNTR_EL0` traps to EL2. Any in-VM profiling,
  self-tuning or adaptive-recompilation trigger in the translator must be built on `CNTVCT_EL0` at
  41.67 ns granularity, or on instruction counting.

### What is still open

* Multi-vCPU TLBI scaling beyond two vCPUs (measured contention is one contender only: +4…+18 %).
* The cache-resident `EnTSO` store-throughput question (§7) — bimodal baseline, unresolved.
* Whether eliding the TLBI on `AF` updates (§6) is safe on M1/M3/M4 and on other macOS versions.
  The invalid→valid elision in §2 is architecturally guaranteed; the `AF` one is not.
* Everything here is one machine (M2 Max), one OS (macOS 26.5.2), one core type. The clock estimate
  of 3.18–3.32 GHz is below the M2 Max P-core maximum, so all cycle counts carry ~5 % uncertainty
  and the absolute nanosecond figures may improve on a fully boosted core.

**Reproduce:**
`cd probes && ./build.sh shadow_mmu/shadow_mmu.c && ./shadow_mmu/bin/shadow_mmu all`
(individual experiments: `cal`, `fault`, `shadow`, `tlbi`, `reach`, `asid`, `ad`, `tso`).

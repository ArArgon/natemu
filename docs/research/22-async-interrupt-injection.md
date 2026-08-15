# 22 — Asynchronous Interrupt Injection: Getting the Guest to a Boundary, and What Each Route Costs

**Date:** 2026-08-15 · **Host:** Apple M2 Max (Mac14,5, 8P+4E), macOS 26.5.2 (25F84), SDK Hypervisor 259.5.15
**Probe:** `probes/irq_inject/irq_inject.c` (in-VM, guest EL1, 7 modes)
**Raw output:** `probes/irq_inject/results/{poll.1-5,vtgic.1-4,vtgic.sweep,hvc,hostkick,vtnogic,protect,pollsmp.*}.log`
**Power state:** battery, 67% — see `18 §0.2`. Absolute nanoseconds in this document are
battery-state; every conclusion below is a ratio measured within one run.

A translated x86 guest cannot be interrupted between arbitrary instructions, because there is no
"between" — the JIT has fused the guest's instructions into blocks with intermediate state live in
host registers. So the runtime needs a way to (a) notice that an interrupt is pending and (b) reach
a guest-instruction boundary where the architectural state is coherent enough to deliver it. This
document measures every route to both, and finds one that is essentially free and one that is 38×
more expensive than it needs to be.

> **Reading the labels.**
> `M` — measured on this machine today by this probe. Run count given.
> `D` — derived by subtracting a control section measured in the same run.
> `I` — inferred from measured evidence; reasoning shown.
> `?` — attempted and **inconclusive**, with the closing experiment named.

---

## 0. Bottom line

### 0.1 The finding that decides the design

**Interrupt *delivery* into a translated guest is nearly free. Interrupt *acknowledgement* through
the ARM GIC costs 38× more than the delivery — and we never have to pay it.** `M`, 4 runs.

With a framework GIC (`hv_gic_create`), an ARM virtual-timer PPI delivered into the guest's own
`VBAR_EL1` vector **7,191,937 times against 2 `hv_vcpu_run` returns**, i.e. essentially zero host
involvement. The cost per IRQ round trip splits cleanly on one variable:

| Handler does | ns per IRQ round trip `M` (runs 2/3/4) |
|---|---|
| IRQ entry → `VBAR_EL1` → `ERET`, **touching no GIC register** | **48.7 / 50.2 / 50.0** |
| the same, plus `MRS ICC_IAR1_EL1` + `MSR ICC_EOIR1_EL1` | **1912.1 / 1931.0 / 1911.7** |

Every GIC CPU-interface access is a host exit priced like one: `MRS ICC_IAR1_EL1` **922–926 ns**,
`MSR ICC_EOIR1_EL1` **901–903 ns**, `ICC_PMR_EL1` **900–920 ns** either direction, and the
redistributor through Device MMIO at stage 2 **983–1003 ns**. Two of those account for the whole
1912 ns.

**The design consequence is direct: a natemu guest is an x86 guest with an x86 APIC. It has no
reason to run the ARM EOI protocol at all.** The ARM GIC is not the guest's interrupt controller —
it is a *boundary-forcing mechanism* for the runtime. Use it to get the vCPU into our own vector at
50 ns, do the x86 APIC bookkeeping in guest memory where it costs ordinary loads and stores, and
never touch `ICC_*`. Doc 10's exit budget of 1.2 M/sec/core is not the constraint here; the
constraint is that an EOI-per-interrupt design would cap out around **520 K interrupts/sec/core**
on GIC register traps alone, while the EOI-free design reaches **20 M/sec** `D`.

### 0.2 The four routes to a boundary, ranked

Every measurement is the same event — "the runtime needs the vCPU to stop what it is doing" — timed
end to end. `M`.

| # | Route | Latency | Who pays | Bounded? |
|---|---|---|---|---|
| 1 | **Pinned-register poll** (`cbnz x24`) at every translated block | **≈ 0 ns** — below this probe's resolution at ≤2 checks per 5-x86-instruction block (§1) | the translated code, continuously | yes, by block length |
| 2 | **Kill a stage-1 mapping from another vCPU** → instruction abort | **p50 83 ns**, p99 333, max 500 `M` n=500 (§4) | the target vCPU, once | yes |
| 3 | **GIC vtimer PPI** → guest `VBAR_EL1`, no `ICC_*` | **48.7–50.2 ns** per round trip; **p50 958–1000 ns** from armed deadline to handler (§3) | the target vCPU, once | yes |
| 4 | **Host thread kick** (`hv_vcpus_exit` + pending IRQ) | **p50 2042 ns**, p99 2250, max 3625 `M` n=200 (§5) | host + a full exit/entry | yes |
| — | *(reference)* bare `HVC` round trip | **1487.6 ns** guest-timed (§5) | — | — |
| — | *(reference)* local `SVC` to EL1, no host | **36.1 ns** (§5) | — | — |

Routes 1–3 are all in-guest and all sub-100 ns. Route 4 is 24–41× worse than routes 2 and 3 and is
the only one that involves the host at all. **The runtime should never use route 4 for anything on
a latency path**; it is for events that originate on the host and have no in-guest proxy.

### 0.3 The recommendation in one paragraph

**Poll a pinned register at every translated block, arm the ARM vtimer for the guest's next
deadline, take delivery in our own EL1 vector without touching a single GIC register, and force
cross-vCPU boundaries by killing a stage-1 mapping rather than by kicking the host.** The poll
costs nothing measurable up to 2 checks per 5 guest instructions; a `REP MOVSQ` stays interruptible
to **0.3 ns worst case at −1.8% to +1.2% throughput** using a register poll per element; delivery
lands in 50 ns; the cross-vCPU shootdown lands in 83 ns; and arming the next deadline costs
**4.77 ns** (`MRS CNTVCT_EL0` + `MSR CNTV_CVAL_EL0` + `MSR CNTV_CTL_EL0`). The one thing that must
never appear in the hot path is a GIC register access, which is a ~900 ns host exit wearing a
system-register encoding.

---

## 1. The per-block poll: what does checking cost?

`M`, 5 runs (`poll.1..5.log`).

**Method.** A synthetic translated loop stands in for JIT output: 1 x86 instruction = 4 ARM ops,
5 x86 instructions per group, **40% of them memory-touching** (the density `06 §2.3` establishes),
8 chained blocks per outer iteration, and **exactly 2,000,000 translated x86 instructions per timed
section**. Timing is in-guest with `CNTVCT_EL0` on either side of an `ISB`. Four variants:

| Variant | Emitted check | Frequency |
|---|---|---|
| `none` | — | control |
| `ld+cbnz` | `ldr w9, [x1]` ; `cbnz w9, exit` | once per **block** |
| `hdr-only` | the same two instructions | once per **8 blocks** (loop header) |
| `cbnz-reg` | `cbnz x24, exit` — a **pinned register**, no memory access | once per block |

### 1.1 Per-block polling is free at the difference measurement's resolution

Block length swept 5 → 80 x86 instructions. Run 5, representative `M`:

| x86/block | none | ld+cbnz | hdr-only | cbnz-reg | ld/blk% | hdr% | reg/blk% |
|---|---|---|---|---|---|---|---|
| 5 | 499 µs | 498 | 500 | 497 | −0.23% | +0.15% | −0.51% |
| 10 | 516 | 497 | 499 | 497 | −3.70% | −3.37% | −3.66% |
| 20 | 498 | 498 | 500 | 498 | −0.09% | +0.32% | −0.07% |
| 40 | 498 | 498 | 499 | 498 | +0.06% | +0.34% | +0.09% |
| 80 | 499 | 498 | 499 | 504 | −0.28% | −0.03% | +0.94% |

The baseline is **0.249–0.258 ns per translated x86 instruction** across all five runs `M` — a
stable figure worth recording on its own. Several "costs" are negative, which is the honest signal
that the effect is below the noise floor. The probe's own drift control says the same: the *same
section* run twice differs by −0.23% to −0.64% at k=5 and −0.28% to +0.31% at k=80 `M`. **A
difference measurement cannot resolve the cost of one check per block.**

### 1.2 The density sweep can resolve it, and the answer is a threshold

Instead of checked-vs-unchecked, sweep *n* checks inside every 5-x86-instruction block over 400,000
blocks and take the slope. Two runs, side by side `M`:

| n/block | `ld+cbnz` ns/check (run 2 / run 5) | `cbnz-reg` ns/check (run 2 / run 5) |
|---|---|---|
| 1 | −0.0047 / −0.0033 | −0.0071 / −0.0061 |
| 2 | −0.0001 / −0.0002 | −0.0014 / −0.0014 |
| **4** | **+0.0103 / +0.0104** | **+0.0080 / +0.0082** |
| 8 | +0.0315 / +0.0315 | +0.0322 / +0.0323 |
| 16 | +0.0729 / +0.0720 | +0.0741 / +0.0729 |
| 32 | +0.1117 / +0.1107 | +0.1104 / +0.1104 |

Baseline (n=0) measured twice within each run: drift +0.03% and +1.45% `M`.

Three readings, in order of importance:

1. **There is a threshold at n≈2–4, not a linear cost.** One or two checks per five guest
   instructions are absorbed entirely — the slope is negative, i.e. unmeasurable. From n=4 the
   slope becomes real and then grows superlinearly (0.010 → 0.032 → 0.072 → 0.111). `I`: the core
   issues the check in spare slots until the block runs out of them. A JIT emitting **one check per
   translated block** is operating comfortably inside the free region for any block longer than
   ~3 guest instructions.
2. **The memory load is not the cost.** `ld+cbnz` and `cbnz-reg` have statistically identical
   slopes at every n ≥ 4 (0.0103 vs 0.0080, 0.0315 vs 0.0322, 0.0720 vs 0.0741, 0.1107 vs 0.1104).
   The poll flag is L1-resident and the load is free; what costs is the **branch**. That is a
   useful negative — it means a pinned-register design's advantage is not about avoiding the load,
   and §2 shows where the advantage actually shows up.
3. **This reproduces to three significant figures across runs**, which is what makes it usable while
   the difference measurement in §1.1 is not.

---

## 2. Keeping a translated `REP MOVSQ` interruptible

`M`, 3 runs. 4,096 elements × 512 repeats = **2,097,152 8-byte element copies per section**. A
string instruction is the worst case for interrupt latency: architecturally interruptible between
elements, but a JIT that emits a tight copy loop has no boundary to stop at.

| Strategy | ns/elem (run 1 / 2 / 5) | Overhead | Worst-case IRQ latency `D` |
|---|---|---|---|
| no check (uninterruptible) | 0.313 / 0.310 / 0.302 | 0% | **unbounded** |
| `load+cbnz` every element | 0.329 / 0.318 / 0.320 | +2.5% … +5.8% | 0.3 ns |
| `load+cbnz` every 8 | 0.385 / 0.373 / 0.373 | **+20.4% … +23.4%** | 3.0 ns |
| `load+cbnz` every 64 | 0.324 / 0.318 / 0.311 | +2.5% … +3.6% | 19.9–20.7 ns |
| `load+cbnz` every 512 | 0.342 / 0.333 / 0.333 | +7.5% … +10.2% | 170.6–175.3 ns |
| **`cbnz` reg every element** | **0.312 / 0.304 / 0.306** | **−1.8% … +1.2%** | **0.3 ns** |
| no check (repeat — drift control) | 0.309 / 0.300 / 0.303 | −3.3% … +0.2% | — |

**A pinned-register poll on every element is free and bounds interrupt latency at 0.3 ns.** It is
within the drift control of the no-check case in all three runs. This is where the pinned register
earns its keep — not in the density sweep (§1.2), but here, where the check is in the innermost
loop and the flag load would otherwise contend with the copy's own memory traffic.

**One reproducible result I cannot explain.** Checking *every 8 elements* is the **worst** strategy
in all three runs (+20.4%, +23.2%, +23.4%) — worse than checking every single element, and worse
than every 64. Monotonicity says an intermediate frequency should give an intermediate cost. `I`:
most likely an unroll/alignment interaction — the every-8 variant is the one whose loop body
straddles whatever fetch or branch-predictor granule the others fit inside — but I have not
disassembled the emitted section to confirm. **`?` UNRESOLVED.** It does not affect the
recommendation (every-element register poll wins outright), but a JIT that chooses a check stride
by arithmetic rather than by measurement could land on exactly this cliff. Closing experiment:
dump the four generated loop bodies and compare instruction count, 64-byte alignment, and branch
density.

### 2.1 Self-patch and deadline-arming costs, measured alongside

`M`, from the same runs:

| Operation | ns |
|---|---|
| bare `STR` to the code page | 0.46 / 0.78 |
| `STR` + `DC CVAU` + `DSB ISH` + `IC IVAU` + `DSB` + `ISB` | **111.29 / 115.29 / 140.16** |
| `ISB` alone | 8.80 / 8.93 / 8.94 |
| `MRS CNTVCT_EL0` | 0.53 / 0.54 |
| **arm a deadline** (`MRS CNTVCT` + `MSR CNTV_CVAL` + `MSR CNTV_CTL`) | **4.77 / 4.79** |

Two things worth carrying forward. **Arming the next guest timer deadline costs 4.77 ns** — cheap
enough that a design can re-arm on every guest `TSC_DEADLINE` write without thinking about it, which
is what doc 10's vtimer→`TSC_DEADLINE` mapping needs. And the full code-patch maintenance sequence
at **111–140 ns** corroborates doc 21's canonical-sequence measurements from an independent probe
(doc 21 item 14 measured 490–850 ns over 4 KiB; this is a single line, and 21 item 12 puts `DC CVAU`
at 1.45–1.55 ns per 64 B line).

**Host exits during the entire poll run: 1** `M`. Everything in §1 and §2 happens without HVF
learning about it.

---

## 3. GIC delivery: the vtimer into the guest's own vector

`M`, 4 runs (`vtgic.1..4.log`) plus a deadline sweep (`vtgic.sweep.log`).

`hv_gic_create` succeeds with the distributor, redistributor and MSI region configured, and
`GICR_ISENABLER0` reads back `0xffffffff` with **bit 27 (the vtimer PPI) enabled** `M`. Then:

**`hv_vcpu_run` returns: 2. Guest IRQs taken (EL1 vector entries): 7,191,937.** `M` (run 3)

That ratio is the whole point. Once the GIC is created and the PPI enabled, the ARM virtual timer
delivers into the guest's own `VBAR_EL1` with **no host involvement at all** — `VTIMER_ACTIVATED`
exits: **0**.

### 3.1 Delivery latency from an armed deadline

Lateness = (delivery timestamp − armed deadline), n=400 per point. Deadline sweep `M`:

| Deadline ahead | p10 | p50 | p90 | p99 | min | max |
|---|---|---|---|---|---|---|
| 24 ticks (1 µs) | 958 | **958** | 958 | 1375 | 917 | 1667 |
| 240 ticks (10 µs) | 958 | **1000** | 1000 | 1208 | 958 | 17875 |
| 2400 ticks (100 µs) | 958 | **1000** | 1042 | 2792 | 917 | **22292** |
| 24000 ticks (1 ms) | 1000 | **1000** | 1292 | 4833 | 958 | 5542 |

All values in ns. Repeated at the 10 µs point across the four `vtgic` runs: p50 **1458 / 958 / 958 /
958**, max **11042 / 1625 / 1542 / 1833** `M` — run 1 is a cold outlier, runs 2–4 agree closely.

Three readings:

1. **The floor is ~958 ns and it does not depend on the horizon.** Arming 1 µs ahead and arming
   1 ms ahead both deliver ~958–1000 ns late. `I`: this is a fixed cost in the arm-and-deliver
   path, not a scheduling artifact — a scheduling artifact would scale with the horizon or with
   how long the vCPU sits idle. Note that **41.67 ns is one `CNTVCT` tick** (`18 §4`), so ~958 ns
   is ~23 ticks; the granularity of the counter explains ~4% of it, not the rest.
2. **The tail is where the horizon shows up, and it is not monotonic.** p99 runs 1375 → 1208 →
   2792 → 4833 ns across the four deadlines — flat at the two short horizons, then 2.3× and 4.0×
   at 100 µs and 1 ms — while the single worst observation, **22.3 µs**, sits at the 100 µs point
   rather than the longest one. `I`: long horizons let the host deschedule the vCPU thread and the
   tail measures the cost of getting it back, but the non-monotonicity means this is a
   scheduling-luck distribution with n=400, not a clean function of the horizon. Design to the
   **22 µs observed worst case**, not to a fitted curve.
3. **For an x86 guest this is fine, and it is worth saying why.** A guest programming
   `TSC_DEADLINE` expects sub-microsecond-ish precision but tolerates lateness; Linux's timer
   subsystem is built for it. A ~1 µs floor with a 22 µs worst case is well inside what a guest
   whose slowest instruction path is a 146 ns shadow fill will notice. It is **not** fine for a
   design that wanted the vtimer as a fine-grained profiling clock — use `CNTVCT_EL0` directly
   (0.53 ns, `18 §4`) for that.

### 3.2 The cost of the GIC registers themselves — the load-bearing table

`M`, runs 2/3/4 (run 1 was cold; its figures are 1.9–2.7× higher and are excluded, with run 1's
values shown for completeness):

| Access | runs 2/3/4 (ns) | run 1, cold (ns) |
|---|---|---|
| `MRS ICC_IAR1_EL1` (spurious, returned 1023) | 922.0 / 923.1 / 925.9 | 2501.0 |
| `MSR ICC_EOIR1_EL1` (spurious id 1023) | 902.3 / 903.1 / 901.4 | 2083.3 |
| `MSR ICC_PMR_EL1` | 899.9 / 903.6 / 919.5 | 1865.5 |
| `MRS ICC_PMR_EL1` | 903.9 / 904.3 / 910.2 | 1785.0 |
| `LDR GICR_ISENABLER0` (Device MMIO, stage 2) | 985.6 / 983.0 / 984.2 | 1717.0 |
| `STR GICR_ISENABLER0` (Device MMIO, stage 2) | 1000.2 / 1000.4 / 1002.6 | 1696.1 |

**Every one of these is a host exit.** The system-register-encoded `ICC_*` accesses cost ~900–926 ns
and the memory-mapped redistributor ~983–1003 ns — within 10% of each other, and both within a
factor of the `HVC` floor (§5). The encoding does not matter; the trap does.

This is what produces §0.1's 38× ratio: `MRS ICC_IAR1_EL1` (923) + `MSR ICC_EOIR1_EL1` (902) =
1825 ns of the measured 1912 ns storm-B round trip, leaving ~87 ns for everything else — which is
about the 50 ns of storm A plus the extra branch work `D`. **The arithmetic closes.**

### 3.3 Without a GIC, delivery does not happen at all

`M`, `vtnogic.log`, an explicit A/B against §3:

```
=== ARM virtual timer -> guest EL1, WITHOUT framework GIC ===
  hv_vcpu_run returns: 3   of which VTIMER_ACTIVATED: 1, CANCELED: 0
  guest IRQs taken (EL1 vector entries): 0
  phase IRQ counts: single-shot 0, after storm A 0, after storm B 0
```

Zero guest IRQs, and the timer expiry surfaces as a **host exit** (`VTIMER_ACTIVATED`) instead.
This is an independent confirmation of `18 §7`, taken by a different probe with a different setup,
and together the two runs settle it: **`hv_gic_create` is not an optimisation, it is the enabling
condition for in-guest interrupt delivery.** A design that skipped the framework GIC to "keep the
device model simple" would be choosing a **~1500 ns host exit per timer tick over a 50 ns vector
entry**, a 30× penalty on the most frequent asynchronous event a guest has.

---

## 4. Forcing a boundary on another vCPU by killing a stage-1 mapping

`M`, `protect.log`, n=500 rounds.

**Method.** vCPU 0 stores 0 to the L3 entry covering a code page vCPU 1 is executing in, then
`TLBI VAAE1IS` + `DSB ISH`. vCPU 1 is executing `BLR` into that page and takes an instruction abort
at EL1. Timed from vCPU 0's stamp to vCPU 1's abort handler.

| Metric | Value |
|---|---|
| Aborts taken on vCPU 1 | **500 / 500 rounds** |
| Host exits | 1 + 1 (setup only) |
| vCPU 0 stamp → vCPU 1 abort handler | **p10 83, p50 83, p90 125, p99 333, min 83, max 500 ns** |

**500 out of 500 is the important number.** This is a *reliable* cross-vCPU boundary primitive, and
`19` carries an auditor's note that its SGI-reliability claim did **not** reproduce at the probe's
default `n`. This route does reproduce, at n=500, with a p50 of 83 ns and a hard max of 500 ns.

Compared with the alternatives: **24.6× faster than a host kick** (2042 ns, §5) and comparable to
doc 19's 68 ns cross-vCPU doorbell — but unlike a doorbell it does not require the target to be
polling anything, because the abort is taken by the *fetch* itself. That makes it the right tool for
the case a poll cannot cover: a vCPU stuck in a long uninterruptible region, or one that must be
stopped for a shadow-page-table invalidation it has no reason to be watching for.

`I`, and worth stating as a design note rather than a measurement: this primitive composes exactly
with doc 20's lazy-resync design. If the runtime is already going to invalidate a shadow mapping,
the invalidation *is* the boundary-forcing signal — one mechanism, not two. The p50 83 ns is
therefore not an added cost in that path; it is the latency of a thing already being done.

---

## 5. The host-involved routes, for comparison

`M`, `hvc.log` and `hostkick.log`.

**Bare `HVC` round trip** — the host-exit floor on this machine, 20,000 exits, 20,001 `hv_vcpu_run`
returns:

| Measurement | Value |
|---|---|
| guest-timed `HVC` round trip (`CNTVCT_EL0` either side) | **1487.6 ns** |
| the same loop shape with a local `SVC` to EL1 (no host) | **36.1 ns** |
| whole process including VM setup | 259.860 ms |

The `SVC`/`HVC` ratio is **41×** `D`. That is the price of leaving the VM, measured with the same
loop, the same timer and the same code shape on both sides — the cleanest statement of the exit tax
in the corpus. (Absolute values: 1487.6 ns here on battery, 1125 ns in `18 §9.3` on battery in an
earlier session, 808 ns in `02 §6.1` on wall power. See `18 §0.2`; the ratio is the durable part.)

**Host thread → guest EL1 IRQ vector**, via `hv_vcpus_exit` + a pending interrupt, n=200:

| Metric | Value |
|---|---|
| host stamp → guest EL1 IRQ vector | **p10 2000, p50 2042, p90 2083, p99 2250, min 1750, max 3625 ns** |
| `hv_vcpu_run` returns | 402 (of which CANCELED 201) |
| `hv_vcpu_get_vtimer_offset` | **0** ⇒ guest `CNTVCT` == host `mach_absolute_time` |

The p50 of 2042 ns is ~1.37× the bare `HVC` round trip, which makes sense: a kick is an exit plus
the cancel/re-enter and the injection. **The distribution is tight** (p10 2000 → p99 2250, only
1.13×), so this route is slow but predictable — which is the right property for the job it should
actually have: host-originated events (a device completion, a signal) that have no in-guest proxy.

The `vtimer_offset = 0` result independently confirms `18 §4`'s finding that guest `CNTVCT` and host
`mach_absolute_time` share a timebase — here read straight out of the API rather than inferred from
overlapping value ranges.

---

## 6. What did not work: the SMP poll-contention measurement

`?` **INCONCLUSIVE.** `M`, 8 runs (`pollsmp.0/1.log`, `pollsmp.0/1.rep.log`).

The question was sharp and worth answering: §1 shows the poll flag is free on a quiet vCPU, but a
real runtime has *another* vCPU writing that flag to signal an interrupt. Does the poll stay free
when its cache line is being written from another core? The probe runs a second vCPU storing either
to the **same 64-byte line** as the poll flag (treatment) or to a line **4 KiB away** (control).

The measurement does not support a conclusion, and the reason is visible in the control:

| Run | arm | `no check` | repeat | `ld+cbnz` per block | Δ |
|---|---|---|---|---|---|
| 1 | same line | 698 µs | 719 | 698 | −0.13% |
| 2 | same line | 844 | 690 | 816 | −3.34% |
| 3 | same line | 1148 | 1051 | 1453 | **+26.60%** |
| 4 | same line | 897 | 936 | 1119 | **+24.82%** |
| 1 | 4 KiB away (control) | 1869 | 882 | 1330 | **−28.85%** |
| 2 | 4 KiB away | 1220 | 821 | 1136 | −6.90% |
| 3 | 4 KiB away | 842 | 1025 | 852 | +1.19% |
| 4 | 4 KiB away | 866 | 815 | 837 | −3.33% |

The **control arm swings from 842 µs to 1869 µs (2.2×) on the identical workload**, and the
same-section drift control swings by up to 41.93%. The treatment effect, whatever it is, is far
smaller than the noise. The per-check density slope in this mode is equally unusable: 0.0223 to
0.6451 ns/check across eight runs, against **0.0315 ± 0.0000** for the same n=8 point in the
single-vCPU mode (§1.2).

`I`: the two-vCPU mode runs two pthreads, and the measured thread is being descheduled or migrated
between P and E cores mid-section — which the single-vCPU mode does not suffer. This is a probe
limitation, not a property of the hardware.

**Closing experiment**, in priority order: pin both vCPU threads with
`pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE)` so they stay on P-cores; report the
per-section `CNTVCT` deltas individually rather than only the aggregate so descheduling is visible
as an outlier rather than folded into the mean; and raise the repeat count to n≥20 per arm with a
median rather than a single sample. Until then, **§1's "the poll is free" result is established only
for an uncontended flag**, and a design that signals interrupts by writing the polled line should
treat the contended case as unmeasured.

---

## 7. Cost model: what an interrupt actually costs the guest

Assembling §1–§5 into the number a cost model needs. All `D` from measurements in this document,
battery clock.

**Per interrupt, in the recommended design** (register poll, GIC delivery, no `ICC_*`):

| Component | Cost | Source |
|---|---|---|
| the poll, amortised over the block it sits in | **0 ns** (below resolution at ≤2 checks / 5 x86 insns) | §1.2 |
| IRQ entry → `VBAR_EL1` → `ERET` | **48.7–50.2 ns** | §3 |
| x86 APIC bookkeeping in guest memory | ordinary loads/stores, not measured here | — |
| re-arm the next deadline | **4.77 ns** | §2.1 |
| **total, excluding APIC emulation** | **≈ 55 ns** | |

**Per interrupt, if the design runs the ARM EOI protocol:**

| Component | Cost |
|---|---|
| everything above | ≈ 55 ns |
| `MRS ICC_IAR1_EL1` | 922–926 ns |
| `MSR ICC_EOIR1_EL1` | 901–903 ns |
| **total** | **≈ 1880–1885 ns**, i.e. **34× worse** |

**Sustained interrupt rate ceiling per core**, taking the round-trip figures directly `D`:

| Design | ns/IRQ | IRQs/sec/core |
|---|---|---|
| EOI-free (storm A) | 48.7–50.2 | **19.9–20.5 M** |
| with ARM EOI (storm B) | 1911.7–1931.0 | **518–523 K** |
| host kick per interrupt | 2042 | 490 K |

For scale: doc 10's exit budget is 1.2 M/sec/core, and a busy Linux guest under load takes on the
order of 10⁴–10⁵ interrupts/sec. **The EOI-free design has ~200× headroom over a demanding guest;
the EOI design has ~5×**, which is thin enough that an interrupt-heavy workload would feel it.

---

## 8. What this changes elsewhere in the corpus

| Document | Claim | Effect |
|---|---|---|
| `10` | GICv3 available macOS 15+; vtimer → `TSC_DEADLINE`; exit budget 1.2 M/sec/core | **Sharpened and constrained.** The GIC is now *mandatory* rather than an option (§3.3), and the exit budget is not the binding constraint — GIC register traps are (§7). Arming a deadline costs 4.77 ns, so the `TSC_DEADLINE` mapping is cheap (§2.1) |
| `18` | Without a GIC the vtimer cannot deliver into the guest's vector | **Independently confirmed** by a second probe (§3.3), and the positive case measured: with a GIC, 7.19 M deliveries against 2 host exits |
| `18` | Guest `CNTVCT` shares the host `mach_absolute_time` timebase | **Confirmed from the API**: `hv_vcpu_get_vtimer_offset = 0` (§5) |
| `19` | Cross-vCPU doorbells 68 ns; SGI reliability **did not reproduce** at default `n` | **An alternative that does reproduce**: stage-1 kill, 500/500 at n=500, p50 83 ns (§4) |
| `20` | Lazy shadow-PT resync, invalidation-driven | **Composes for free.** The shadow invalidation the design already performs *is* the cross-vCPU boundary signal (§4) |
| `21` | SMC canonical maintenance sequence | Corroborated on a single line: `STR`+`DC CVAU`+`DSB`+`IC IVAU`+`DSB`+`ISB` = 111–140 ns (§2.1) |
| `17` | Precise exceptions; two-tier fault handling | The interrupt-boundary problem has the same shape and the same answer: the common case needs no precise state, only a boundary. §2's register poll bounds it at 0.3 ns inside a string instruction |
| `06 §2.3` | Instruction-mix density (40% memory) | Used as the input to §1's translated-loop model; the resulting **0.249–0.258 ns per translated x86 instruction** is a reusable baseline |

---

## 9. Open items

| # | Item | Closing experiment |
|---|---|---|
| 1 | **SMP poll contention unmeasured** (§6) | Pin both vCPU threads to P-cores via QoS class; report per-section deltas individually; n≥20 per arm, median |
| 2 | Checking every 8 elements is worse than every element **and** every 64 (§2) | Disassemble the four generated `REP MOVSQ` bodies; compare instruction count, 64-byte alignment, branch density |
| 3 | 22.3 µs worst-case delivery at the 100 µs deadline (§3.1) | Repeat the sweep with the vCPU thread pinned and with a busy-spin instead of an idle wait, to separate descheduling from the arm-and-deliver path |
| 4 | The ~958 ns delivery floor is unexplained (§3.1) | Sweep the deadline below 1 µs and across `CNTV_TVAL` vs `CNTV_CVAL` arming; check whether the floor moves with the arming instruction or is fixed |
| 5 | Absolute timings are power-state dependent | Re-run `hvc`, `vtgic`, `hostkick`, `protect` on wall power; record both columns (`18 §0.2`, item 7) |
| 6 | x86 APIC emulation cost is not in §7's model | Measure guest-memory APIC bookkeeping once the register layout is fixed |

---

## 10. Reproduce

```sh
cd probes
./build.sh irq_inject/irq_inject.c
./irq_inject/bin/irq_inject poll        # section 1, 2  per-block poll + REP MOVSQ + self-patch
./irq_inject/bin/irq_inject vtgic       # section 3     vtimer -> guest vector WITH framework GIC
./irq_inject/bin/irq_inject vtnogic     # section 3.3   the A/B: same, without a GIC
./irq_inject/bin/irq_inject protect     # section 4     cross-vCPU boundary via stage-1 kill
./irq_inject/bin/irq_inject hvc         # section 5     bare HVC round trip, and local SVC
./irq_inject/bin/irq_inject hostkick    # section 5     hv_vcpus_exit + pending IRQ
./irq_inject/bin/irq_inject pollsmp 1   # section 6     contended poll flag  (INCONCLUSIVE)
./irq_inject/bin/irq_inject pollsmp 0   # section 6     control: line 4 KiB away
```

Requires ad-hoc signing with `com.apple.security.hypervisor` (`probes/entitlements.plist`, applied
by `build.sh`), and macOS 15+ for `hv_gic_create`. No SIP changes. **Record the power state**
(`pmset -g ps`) alongside any timing intended for cross-session comparison.

`vtgic` runs 2–4 agree closely; run 1 is a cold outlier throughout and is reported but not used.

## References

`06` x86 system semantics (§2.3 instruction-mix density, the input to §1's model); `10`
interrupt/timer/device dossier (the design this document measures); `17` precise exceptions;
`18` EL1 capability surface (the no-GIC vtimer result, the `CNTVCT` timebase, the exit catalogue);
`19` SMP scaling (cross-vCPU doorbells, and the SGI-reliability auditor's note §4 answers);
`20` shadow-PT coherence (the invalidation that doubles as a boundary signal); `21` SMC detection
(the cache-maintenance sequence corroborated in §2.1); `02` Hypervisor.framework (the 808 ns exit
floor). External: ARM DDI 0487 for GICv3 CPU-interface register semantics; Apple Hypervisor
framework `hv_gic_*` API.

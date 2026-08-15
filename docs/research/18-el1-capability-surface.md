# 18 — What a Guest at EL1 Under Hypervisor.framework Can Actually Do

**Date:** 2026-08-15 · **Host:** Apple M2 Max (Mac14,5, 8P+4E), macOS 26.5.2 (25F84), SDK Hypervisor 259.5.15
**Probe:** `probes/el1_features/el1_features.c` (2,435 lines, in-VM, guest EL1h)
**Raw output:** `probes/el1_features/results/{regs,applies,feat,time,at,tlbi,timer,exc,exits}.txt`
**Method:** every experiment runs in its **own forked VM**. The vCPU is placed in EL1h
(`CPSR=0x3c5`) before its first `MRS`, and `VBAR_EL1` points at our own vector table, so an
undefined instruction is survivable and reportable rather than fatal.

> **Reading the labels.**
> `M` — measured on this machine today by this probe, with the raw line quoted.
> `D` — derived by subtracting one measured quantity from another (loop floor, control section).
> `I` — inferred from measured evidence; the reasoning is shown. Never presented as a measurement.
> `?` — attempted and **inconclusive**, with the experiment that would close it named.

---

## 0. Bottom line

### 0.1 The three findings that matter to the design

**1. The entire MMU and cache-maintenance surface the design depends on executes at guest EL1
with zero host exits.** `M` (§9). All of `TLBI` (every variant, including `TLBIRANGE` and
`TLBIOS`), `IC IALLU`/`IALLUIS`/`IVAU`, `DC CVAU`/`CIVAC`/`ISW`/`ZVA`, `AT S1E*`, and `MSR` to
`TTBR0_EL1`/`TCR_EL1`/`SCTLR_EL1`/`VBAR_EL1`/`MAIR_EL1`/`CONTEXTIDR_EL1`/`ACTLR_EL1`/`CPACR_EL1`
run **entirely inside the VM**. Out of 48 catalogued operations, exactly **six** leave: `MRS
CNTPCT_EL0`, `MRS PMCCNTR_EL0`, `MSR OSLAR_EL1`, `WFI`, `SMC`, and `HVC`. Everything the shadow-MMU
design (16, 20) and the SMC design (21) needs is native.

**2. There is no cycle counter at guest EL1, and this is not a configuration problem.** `M` (§4).
`PMCCNTR_EL0`, every other architectural PMU register, and **every Apple IMPDEF register**
(`HID0/1/4/5/11`, `PMCR0/1`, `PMESR0`, `PMC0/1`) trap to EL2 with `EC=0x18`. A read of `PMCCNTR_EL0`
costs a host exit and returns a **delta of 0 over a 4096-iteration loop** — HVF does not virtualise
it, it just traps it. The only in-guest clock is `CNTVCT_EL0` at **24 MHz / 41.67 ns per tick**,
which is 0.44–0.61 ns to read without an `ISB` and 13.2–20.0 ns with one. **Every self-measurement
the runtime wants to do is either 41.67 ns-granular or costs an `ISB`.**

**3. Without a GIC, the virtual timer cannot deliver into the guest's own vector at all.** `M`
(§7). Armed 100 µs ahead with `PSTATE.I` unmasked, the vtimer produced **1 `hv_vcpu_run` return
(`VTIMER_ACTIVATED`) and 0 guest IRQs** — and it still produced 0 guest IRQs after the host
acknowledged with `hv_vcpu_set_vtimer_mask`. `CNTV_CTL` reads `0x5` (`ISTATUS=1`, firing) while the
guest's vector never runs. Separately, `hv_vcpu_set_pending_interrupt` with `PSTATE.I=1` delivered
**0 IRQs even after `DAIFClr`**. Interrupt delivery is therefore *not* a guest-EL1 capability on the
bare path; it is a host-side operation, which is what makes doc 22's GIC measurements the load-bearing
ones.

### 0.2 A measurement caveat — and a correction to the first version of this section

**Raw output:** `probes/el1_features/results/clock-stability.txt`

The first version of this document attributed a ~1.6× swing in this probe's absolute nanoseconds
to **host power state** — battery versus wall power — on the strength of two `el1_features` runs
that agreed with each other and one `pt_wp cal` run. **That attribution was wrong, and the
investigation that refuted it is more useful than the claim it replaced.** Re-measured on wall
power with proper sample counts:

| Probe / condition | n | Clock estimate (GHz) |
|---|---|---|
| `pt_wp cal`, **AC** | 20 | median **3.09**, range 2.68–3.33 (1.24×) |
| `pt_wp cal`, **battery** | 1 | 2.01 |
| `el1_features time`, **AC**, load avg 10.9 | 10 | 1.43 … 3.22 |
| `el1_features time`, **AC**, load avg 2.4 | 10 | 1.16 … 1.67 |
| `el1_features time`, **battery** | 2 | 2.20, 2.08 |

**The battery readings sit inside and mostly above the wall-power distribution.** Power state does
not explain the swing. Three further controls, all `M`:

* **QoS is not the cause.** `pt_wp.c:1041` and `irq_inject.c:984` both call
  `pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE, 0)`; `el1_features.c` did not. Adding
  it (kept — it is correct either way) left the distribution unchanged: 1.16 … 1.67 GHz over 10 runs.
* **Background load is not the cause.** The `el1_features` distribution is the same at load average
  10.9 and 2.4.
* **Forcing E-cores** (`taskpolicy -b`) reproduces the low readings for *both* probes (`pt_wp`
  0.85–2.27, `el1_features` 0.85–2.33), which shows E-core placement *can* produce them but does
  not establish that it does.

**What is actually established:**

1. **This probe's core-clock-bound timings carry ~2× run-to-run spread.** 15 paired samples on AC
   at low load give `ISB` alone anywhere from **10.25 to 21.74 ns** and `DSB ISH` from **5.43 to
   14.31 ns**, and the spread is **not** removed by converting to cycles (`ISB` 12.3–42.9 cyc) —
   the calibration loop is as noisy as the thing it would normalise. A single-run nanosecond from
   §4, §8 or §9 is good to about a factor of two, no better.
2. **Fabric-bound timings are stable.** `TLBI VAAE1IS` measures **203.5 ns** on battery and
   **206.2–225.1 ns** across 6 AC runs — a 1.09× spread spanning both power states. TLB and cache
   maintenance costs in §6 are trustworthy; core-issue costs in §4 are not.
3. **`pt_wp` is consistently tighter and higher than `el1_features`** (3.09 median, 1.24× spread vs
   ~1.5 median, ~3× spread) across many batches. The cause is **not established** — QoS, load and
   power state are all ruled out above. `?` UNRESOLVED.

**Consequences, stated plainly.** Nothing in 14/16/17/19/20/21 is retracted: every load-bearing
argument there is a ratio measured within one run, and §6's independent `TLBI` numbers land within
1.1× of doc 20's across power states, which is the direct check. But **two claims in the first
version of this document were wrong and are withdrawn**: that the swing is a power-state effect,
and that doc 02's 808 ns versus this session's 1125 ns `HVC` corroborates it. Re-measured, `HVC` on
AC is **1292–1458 ns** in `el1_features` (worse than the battery reading it was supposed to
explain) and **913–1200 ns** in `irq_inject`. The two probes disagree in opposite directions; there
is no clean power-state signal in either.

**Action for the corpus:** `13 §D-4`'s one-machine caveat should be extended not to *power state*
but to *sample count*. A core-clock-bound timing from this probe needs a median over n ≥ 10 with
the range reported. Where a single figure appears in §4, §8 or §9 below, read it as ±2×; §5 and §6
are firmer. This also removes the tidy explanation that was offered for the permission-fault
discrepancy in §8 — see the revised item 4 in §11.

---

## 1. The system-register surface at guest EL1

`M`, `results/regs.txt`. Reads report the value or where the access went instead; writes report
`WROTE -> READBACK -> AFTER-HOST-EXIT`, the last column being what survives HVF's context switch.

### 1.1 Directly accessible, no trap (29 registers)

`CurrentEL` (= 0x4, confirming EL1), `SCTLR_EL1` (`0x30901185`), `TCR_EL1` (`0x280903510`),
`TTBR0_EL1`, `TTBR1_EL1`, `MAIR_EL1` (`0xffff`), `AMAIR_EL1`, `VBAR_EL1`, `CONTEXTIDR_EL1`,
`TPIDR_EL0`, `TPIDRRO_EL0`, `TPIDR_EL1`, `PAR_EL1`, `ACTLR_EL1`, `CPACR_EL1`, `ESR_EL1`, `FAR_EL1`,
`ELR_EL1`, `SPSR_EL1`, `SP_EL0`, `ISR_EL1`, `DISR_EL1`, `AFSR0/1_EL1`, `MDSCR_EL1`, `DBGBVR0_EL1`,
`DBGWCR0_EL1`, `MDCCINT_EL1`, `CNTKCTL_EL1`.

This is the whole set a shadow-MMU monitor needs. Note `ISR_EL1` (pending virtual interrupt state)
and `DISR_EL1` (FEAT_RAS deferred SError) are both readable — the guest can inspect its own pending
interrupt state without an exit.

### 1.2 Correctly unreachable

`VPIDR_EL2`, `HCR_EL2`, `TTBR0_EL2`, `SCR_EL3` all raise `UNDEF` into the guest's own vector
(`ESR_EL1=0x2000000`, `EC=0x00`) `M`. **The EL2/EL3 boundary is intact** — a guest cannot reach the
hypervisor's own control state, which is the security property the whole helper-VM design assumes.
`RVBAR_EL1` also undefs.

### 1.3 Trapped to EL2 — the exclusion list

Every one of these returns `EC=0x18` (MSR/MRS trap) `M`:

| Group | Registers | Consequence |
|---|---|---|
| **Architectural PMU** | `PMCCNTR_EL0`, `PMCR_EL0`, `PMUSERENR_EL0`, `PMSELR_EL0`, `PMCNTENSET_EL0`, `PMCEID0_EL0`, `PMEVCNTR0_EL0`, `PMCCFILTR_EL0`, `PMINTENSET_EL1` | **No cycle counter, no event counters.** §4. |
| **Apple IMPDEF** | `S3_0_c15_c{0,1,4,5,13}_0` (HID0/1/4/5/11), `S3_1_c15_c{0,1,5}_0` (PMCR0/1, PMESR0), `S3_2_c15_c{0,1}_0` (PMC0/1), `S3_3_c15_c0_0`, `S3_6_c15_c2_5`, `S3_6_c15_c8_3`, `S3_4_c15_c2_0` | **No Apple chicken bits from the guest.** Whatever `ACTLR_EL1.EnTSO` does not give us, we cannot reach another way. |
| **Debug lock** | `OSLSR_EL1` (read), `OSLAR_EL1` (write) | Debug-lock manipulation exits. |
| **Physical timer/counter** | `CNTPCT_EL0`, `CNTP_CVAL_EL0`, `CNTP_CTL_EL0` | The guest gets the **virtual** timer only. §7. |
| **FEAT_ECV** | `CNTVCTSS_EL0`, `CNTPCTSS_EL0` | Self-synchronised counters are not available; the `ISB`+`MRS` idiom is the only correct clock read. |

`ACTLR_EL1` is worth calling out for the opposite reason: it is **not** trapped, and writing `0x2`
(Apple `EnTSO`) reads back `0x2` and **survives a host exit** `M`. That is the mechanism doc 01 and
doc 14 rest on, now confirmed on the register-surface side as well as behaviourally.

### 1.4 Writes that do not fully stick

`M`. Three registers accept a write and quietly drop part or all of it:

| Register | Wrote | Read back | After host exit | Reading |
|---|---|---|---|---|
| `AMAIR_EL1` | `0xffff_ffff_ffff_ffff` | `0xffff_ffff_ffff_ffff` | **`0x0`** | **HVF does not preserve `AMAIR_EL1` across its context switch.** RES0 unless IMPDEF attributes exist, so harmless here — but it is a register the guest can write, read back, and then find zeroed by an event it cannot see. Anything that caches `AMAIR_EL1` state in the monitor must re-establish it after every exit. |
| `PAR_EL1` | `0xdeadbee0` | `0xdeadbae0` | `0xdeadbae0` | Partially ignored — `PAR_EL1` is architecturally written by `AT`, not by software. Bits do not round-trip. |
| `AFSR0_EL1` | `0xffffffff` | `0x0` | `0x0` | Write ignored entirely. |
| `CPACR_EL1` | `0x3330000` (`FPEN`=`ZEN`=`SMEN`=`0b11`) | `0x300000` | `0x300000` | **Only `FPEN` survives.** `ZEN` and `SMEN` read back 0 — consistent with §3: SVE is absent. |

Registers that do round-trip cleanly and survive the exit: `TPIDR_EL1`, `TPIDR_EL0`, `TPIDRRO_EL0`,
`CONTEXTIDR_EL1`, `ACTLR_EL1`, `TTBR1_EL1`, `CNTKCTL_EL1`, `MDSCR_EL1`, `DBGBVR0_EL1`,
`CNTV_CVAL_EL0`, `VBAR_EL1` `M`.

---

## 2. Does the write *apply*, or merely stick?

A readback proves storage, not effect. §2 of the probe forces a **behavioural consequence** for
each register. `M`, `results/applies.txt` + `results/applies.run2.txt` (two runs).

| Register | Consequence forced | Verdict |
|---|---|---|
| `TTBR0_EL1` | Switch to an alternate root; read a VA that maps to a different value under each | **APPLIES** — `root1 -> 0xaaaa0000aaaa0001`, `root2 -> 0xbbbb0000bbbb0001` |
| `MAIR_EL1` | Change attr1 Normal-WB → Device-nGnRnE, then do an unaligned load | **APPLIES** — control (Normal) load succeeds, post-change load takes `EC=0x25` alignment fault. Device memory forbids unaligned access, so the *memory type actually changed* |
| `SCTLR_EL1` (beyond `.M`) | Set `SCTLR_EL1.A=1`, then do an unaligned load | **APPLIES** — readback `0x30901187`, load takes `EC=0x25` alignment fault |
| `VBAR_EL1` | Move to a second vector table, then `SVC` | **APPLIES** — the `SVC` lands in the **new** table, whose handler reports out (`ELR=0x40020010`) |
| `TCR_EL1` | Set `EPD0=1`, then load `VA_A` | probe says `?`; see §2.1 — the evidence says **APPLIES** |

### 2.1 The `TCR_EL1.EPD0` result the probe mis-scores

The probe prints *"TCR_EL1 did NOT visibly apply — inspect"*, and that verdict is wrong. Its own
evidence line is:

```
before: read 0x0 (expect MAGIC_A)   after: TIMEOUT ESR_EL1=0x86000004 EC=0x21 translation fault
```

`I`, from measured values: `ESR_EL1 = 0x86000004` decodes to `EC = 0x21` — **Instruction Abort
taken without a change in Exception level** — with `IFSC = 0b000100`, a **translation fault at
level 0**. That is not a failure to apply; it is `EPD0=1` applying so thoroughly that the guest
wedged. With `TTBR0_EL1 = 0x002a000040006000` and `VBAR_EL1 = 0x40004000`, both the code being
executed *and the vector table that would report the fault* live in the TTBR0 range. Disabling
TTBR0 table walks therefore killed the very next instruction **fetch**, and then killed the fetch
of the abort handler, so the guest fault-looped until the watchdog fired — which is exactly what
"TIMEOUT" means here.

**Verdict: `TCR_EL1` applies. Recorded as `I`, not `M`, until the confirming experiment runs.**
That experiment is one edit: relocate the payload and vectors into the TTBR1 half (the split at
bit 47 is already proven to work — `20` item 16, `T0SZ=T1SZ=17`), then set `EPD0=1` and read a
TTBR0-mapped VA. The load should take a **data** abort (`EC=0x25`) with `DFSC` level 0 while
execution continues. Until then, `TCR_EL1.EPD0` is `?`.

### 2.2 One sub-test is not stable run-to-run

The EL0 `TPIDRRO_EL0` write reported two different outcomes across two runs of the same binary `M`:

* run 1: `UNDEF_EL1`, `ESR_EL1=0x2000000`, `EC=0x00` — the write raised an undef **into the guest's
  own vector, with no host exit**
* run 2: `EXC_EL1`, `ESR_EL1=0x6234f861`, `EC=0x18` — an MSR/MRS **trap to EL2**

Both runs agree on the load-bearing part (the write does not succeed), and both agree `CurrentEL`
was `0x3c0`, i.e. the vCPU really was at EL0. They disagree on *where the refusal is generated*,
which matters if the design ever wants to emulate `TPIDRRO_EL0` writes cheaply. **Recorded as
`?`.** Everything else in §2 reproduced identically across both runs.

---

## 3. FEAT_* — the ID bit versus the executed instruction

The point of this section is that an ID register is a claim and an executed instruction is
evidence. `M`, `results/feat.txt`.

ID registers as seen at guest EL1: `ID_AA64ISAR0_EL1 = 0x0221100110212120`,
`ID_AA64ISAR1_EL1 = 0x0010111110211402`, `ID_AA64ISAR2_EL1 = 0x0`,
`ID_AA64PFR0_EL1 = 0x1101000010110011`, `ID_AA64PFR1_EL1 = 0x1`,
`ID_AA64MMFR2_EL1 = 0x1001001100001011`, `ID_AA64DFR0_EL1 = 0x10305006`, `ID_AA64ZFR0_EL1 = 0x0`.
Decoded: `Atomic=2 TS=2 CRC32=1 DP=1 API=4 GPI=1 JSCVT=1 LRCPC=2 BT=1 DIT=1 AT(LSE2)=1 PMUVer=0`,
and `SVE=0 MTE=0 SSBS=0 APA=0 GPA=0`.

`PMUVer=0` is the ID-register half of §1.3: HVF does not merely trap the PMU, it **tells the guest
there isn't one**. That is the coherent, well-behaved thing to do, and it means a guest kernel will
not try to use it.

### 3.1 Confirmed by execution

| Feature | Evidence `M` |
|---|---|
| **PAuth** | `PACIA` under key 0 → `0xc85a400000000000`; under a new key → `0x356a400000000000`. **The key applies**, and the new key **survives a host exit** (same value after) |
| **BTI** | `SCTLR_EL1.BT1` readback `0x1030901185`; a `BLR` into a guarded page at a non-landing-pad raises `ESR_EL1=0x36000002`, `EC=0x0d` (Branch Target Exception). **BTI is enforced at guest EL1** |
| **LSE** | `CASAL` old=`0x55` mem=`0x66` ✓; `LDADDAL` old=7 mem=12 ✓; `CASPAL` → `0x3333/0x4444` ✓ |
| **FlagM / FlagM2** | `CFINV`→`0x0`, `RMIF`→`0x0`, `SETF8`/`SETF16`→`0x40000000`, `AXFLAG`/`XAFLAG`→`0x20000000`. All six execute. This is the machinery doc 04 called a transferable Rosetta win |
| **JSCVT** | `FJCVTZS(1.5) = 1`, `NZCV=0x0` ✓ |
| **CRC32** | `CRC32X = 0x21193d2e` ✓, `CRC32CX = 0xe9986aa9` ✓ (both against reference) |
| **DotProd** | `UDOT` lanes `0x18/0x18` = 4×2×3 = 24 ✓ |
| **DIT / SSBS / PAN / UAO** | All four PSTATE bits set and clear correctly from guest EL1 |
| **SB, ESB/`DISR_EL1`, `LDAPR`/`LDAPUR`/`STLUR`, `DC ZVA`** | All execute, no trap |

`PAN` and `UAO` working at guest EL1 matters for doc 24 §6: the SMEP/SMAP→PAN/XN mapping is not
just architecturally available, it is reachable from where we will be running.

### 3.2 Absent, and how the absence presents

| Feature | Presentation `M` |
|---|---|
| **SVE** | `CPACR_EL1.ZEN` will not set (writes `0b11`, reads 0); `PTRUE` raises `UNDEF` (`EC=0x00`). **Absent, consistently at both the enable and the instruction level** |
| **MTE** | `TCO` will not set; `IRG`/`ADDG` and `STG`/`LDG` all `UNDEF`. Absent |
| **RNDR** | `UNDEF`. Absent — note doc 23 lists `RNDR` among the EL0-legal registers FEXCore's backend emits, so a FEXCore-derived JIT must not assume it |
| **FEAT_ECV** | `CNTVCTSS`/`CNTPCTSS` **trap to EL2** rather than undef. Not available |

### 3.3 One measured result I cannot explain, recorded as unresolved

`ID_AA64MMFR2_EL1` reports `AT=1`, i.e. **FEAT_LSE2 present**, which should permit unaligned
accesses (including atomics) to Normal memory. The executed test disagrees `M`:

```
LSE2  unaligned 8-byte loads across 8- and 16-byte boundaries: faulted (ESR_EL1=0x96000021 ALIGNMENT fault)
      8-byte-aligned CASPAL: ALIGNMENT FAULT (16-byte alignment required)
```

`ESR_EL1=0x96000021` is `EC=0x25`, `DFSC=0x21` — alignment fault. The obvious explanation,
`SCTLR_EL1.A=1`, is **ruled out**: `SCTLR_EL1` reads `0x30901185` in this configuration, and bit 1
(`A`) is 0 `M`. The other explanation — the target page being Device-typed — is not ruled out,
because `MAIR_EL1` is `0xffff` (attr0 = `0xff` = Normal WB) but the probe's `applies` mode is the
only place that establishes which attr index the test page uses.

**`?` UNRESOLVED.** This matters more than it looks: doc 23 §10 counts FEX's unaligned-atomic
backpatching among the assets we would inherit, and that machinery is designed around LSE2 being
usable. The closing experiment is small — dump the leaf descriptor's `AttrIndx` for the address
under test and re-run with an explicitly Normal-WB `AttrIndx`, and separately re-run with
`SCTLR_EL1.A` explicitly cleared in the same window. Until then, **do not assume unaligned atomics
work at guest EL1 on this host.**

---

## 4. The in-guest clock

`M`, `results/time.txt` + `results/time.run2.txt` (two runs, battery). **The two runs below agreed
with each other, which is exactly what made the first version of §0.2 wrong — 15 later runs span
1.05–3.24 GHz. Read every figure in this section as ±2×.**

| Quantity | Run 1 | Run 2 |
|---|---|---|
| `CNTFRQ_EL0` | 24,000,000 Hz (41.67 ns/tick) | same |
| Monotonic over 512 back-to-back `MRS CNTVCT_EL0` | **YES**, 0 decreases, 502 repeats, smallest nonzero step 1 tick | same |
| Timebase | **shares the host's `mach_absolute_time` range** (guest `CNTVCT` 10652616393220 inside the host's 10652616355209..10652616974070 window) | — |
| Dependent `ADD` (1 cycle) | 0.4551 ns → ~2.20 GHz | 0.4801 ns → ~2.08 GHz |
| `MRS CNTVCT_EL0`, no `ISB` | **0.61 ns** (1.3 cyc) | 0.44 ns (0.9 cyc) |
| `ISB ; MRS CNTVCT_EL0` | **13.19 ns** (29.0 cyc) | 19.99 ns (41.6 cyc) |
| `ISB` alone | 19.62 ns (43.1 cyc) | 20.00 ns (41.6 cyc) |
| `DSB ISH` alone | 9.04 ns (19.9 cyc) | 11.56 ns (24.1 cyc) |

Three consequences for the design:

1. **`CNTVCT_EL0` is the only clock, it is 41.67 ns-granular, and it is free to read without an
   `ISB`** (~1 cycle). Any timing the runtime does at finer resolution than 41.67 ns must be done
   by repetition, which is what every probe in this corpus already does.
2. **`ISB` dominates a synchronised clock read.** `ISB ; MRS` costs 13–20 ns, and `ISB` alone
   accounts for essentially all of it. A guest TSC read (doc 10 maps `vtimer`→`TSC_DEADLINE`) that
   needs ordering pays this; one that does not, does not.
3. **Guest `CNTVCT` and host `mach_absolute_time` share a timebase.** That is a gift for the
   correctness oracle (25) and for any host-side profiling: timestamps taken on both sides of an
   exit are directly comparable with no conversion.

**The cycle counters, both of them, are unavailable** `M`: `PMCCNTR_EL0` and Apple `PMC0` each cost
**2 host exits** inside the measurement window and report a **delta of 0 over a 4096-iteration
loop**. First trap `ESR_EL2=0x6230e41b` / `0x6230bc01`, both `EC=0x18`. The cycle columns in every
document in this corpus are therefore modelled from a dependent-`ADD` clock estimate, not counted —
which is exactly why §0.2's power-state finding bites.

---

## 5. `AT` — address translation instructions

`M`, `results/at.txt`. `DATA_A` at IPA `0x40011000` (RW), `DATA_B` at `0x40012000` (RO).

### 5.1 Correctness — `AT` reports exactly what the architecture says

| Instruction | Target | `PAR_EL1` | Decoded |
|---|---|---|---|
| `AT S1E1R` | `VA_A` (RW) | `0xff00000040011b80` | F=0, PA=`0x40011000`, SH=3, ATTR=`0xff` |
| `AT S1E1W` | `VA_A` (RW) | `0xff00000040011b80` | F=0, same |
| `AT S1E1W` | `VA_B` (RO) | `0x81f` | **F=1, FST=0x0f PERMISSION** |
| `AT S1E1R` | unmapped | `0x80f` | **F=1, FST=0x07 TRANSLATION** |
| `AT S1E0R` | `VA_A` (EL1-only) | `0x81f` | **F=1, FST=0x0f PERMISSION** |
| `AT S1E1RP` / `S1E1WP` | `VA_A` | `0xff00000040011b80` | F=0 |
| `AT S1E0W` | `VA_A` | `0x81f` | F=1, PERMISSION |
| `AT S1E1RP` / `S1E1R`, `PAN=1` | `VA_A` | `0xff00000040011b80` | F=0 (EL1-only page, so PAN is not engaged) |

`AT` distinguishes permission faults from translation faults, and distinguishes EL0 from EL1
access, entirely in-guest with **zero host exits** (§9). As a *diagnostic* it is exact.

### 5.2 Latency — and why doc 17's refutation holds

Median of 41 rounds × 16,384 executions, loop floor subtracted, timed in-guest with `CNTVCT_EL0`
so no host exit is inside the window. **Single run; read as ±2× — see §0.2.**

| Body | ns `D` | vs the LDR reference |
|---|---|---|
| empty loop (floor) | 0.11 | — |
| `LDR` from the same page (**reference**) | 0.12 | 1× |
| `ISB ; MRS PAR_EL1` (no `AT`) | 14.93 | 124× |
| `AT S1E1R` alone (no `ISB`, no `PAR`) | 11.31 | 94× |
| `AT S1E1R ; MRS PAR_EL1` (no `ISB`) | 21.17 | 176× |
| **`AT S1E1R ; ISB ; MRS PAR_EL1`** (the usable form) | **46.27** | **386×** |
| `AT S1E1W ; ISB ; MRS PAR_EL1` | 32.43 | 270× |
| `AT S1E1RP ; ISB ; MRS PAR_EL1` | 27.77 | 231× |
| `AT S1E1WP ; ISB ; MRS PAR_EL1` | 24.87 | 207× |
| `AT S1E0R ; ISB ; MRS PAR_EL1` | 22.53 | 188× |
| `AT S1E0W ; ISB ; MRS PAR_EL1` | 21.56 | 180× |
| `AT S1E1W` on a RO page (fails) | 20.43 | 170× |
| `AT S1E1R` on an unmapped VA (fails) | 39.57 | 330× |

Doc 17 refuted `AT`-probe-then-execute as a general answer to precise exceptions, at **24–55× a
store**. This measurement is independent, on a different probe, and lands in the **same
direction and further out**: 386× a load for the complete `AT ; ISB ; MRS PAR` sequence. The
difference in multiplier is a difference in denominator (doc 17 compared against a store through a
full addressing mode; this compares against a 0.12 ns L1 load), not a disagreement. **`AT` is a
debugging and fault-classification instrument, not a hot-path instruction** — which is exactly how
doc 17 concluded it should be used.

One useful asymmetry: **the `P` variants are cheaper than the plain ones** (`S1E1RP` 27.8 ns vs
`S1E1R` 46.3 ns), and `AT` on an unmapped VA is expensive (39.6 ns) while `AT` on a
permission-failing VA is cheap (20.4 ns) — the same shape doc 16 found for real faults, where
permission faults are 3.3× cheaper than translation faults.

---

## 6. `TLBI` at guest EL1

`M`, `results/tlbi.txt`.

### 6.1 Every variant we need actually invalidates

Method: cache a level-3 entry in the TLB, demote the leaf to read-only *behind* the TLB, invalidate
with **one** variant, then store again. A fault proves the variant really invalidated.

| Variant | Result | Reading |
|---|---|---|
| *(no TLBI — control)* | **not invalidated**, stale entry used | The control works: the TLB really was holding the entry |
| `VMALLE1`, `VMALLE1IS` | INVALIDATED | |
| `VAE1`, `VAE1IS` | INVALIDATED | |
| `VAAE1`, `VAAE1IS` | INVALIDATED | |
| `VALE1`, `VALE1IS` | INVALIDATED | |
| `ASIDE1`, `ASIDE1IS` | INVALIDATED | |
| **`ASIDE1IS` with the wrong ASID** | **not invalidated** | ASID scoping is real, not approximated |
| **`VAE1IS` with the wrong VA** | **not invalidated** | VA scoping is real |
| **`RVAE1IS`** (FEAT_TLBIRANGE) | INVALIDATED | Range invalidation is **available** |
| **`VMALLE1OS`** (FEAT_TLBIOS) | INVALIDATED | Outer-shareable is **available** |

The two negative controls are the load-bearing part. Doc 20's whole ASID design (§ items 17–21)
assumes `ASIDE1IS` is precisely scoped; this shows a wrong-ASID invalidation is a genuine no-op
rather than a conservative flush. `TLBIRANGE` availability is consistent with
`ID_AA64ISAR0_EL1[59:56] = 2` (§3) and is worth knowing: batched shadow invalidation has a
hardware primitive here.

### 6.2 Latency

Each body is `TLBI <op> ; DSB ISH ; ISB`, median of 41 rounds × 16,384, floor subtracted.
**These are the firmest numbers in the document**: `TLBI VAAE1IS` reproduces to 1.09× across 7 runs
spanning both power states (§0.2).

| Body | ns `D` |
|---|---|
| empty loop (floor) | 0.14 |
| `DSB ISH ; ISB` (**no TLBI** — the barrier-only control) | **22.50** |
| `TLBI VAE1IS` alone (no barriers) | 125.11 |
| `TLBI ASIDE1` | **167.51** ← cheapest |
| `TLBI VAAE1IS` | 203.46 |
| `TLBI VMALLE1` | 208.18 |
| `TLBI VALE1` | 210.73 |
| `TLBI VAE1` | 211.43 |
| `TLBI ASIDE1IS` | 215.41 |
| `TLBI VALE1IS` | 220.16 |
| `TLBI VAE1IS` | 225.33 |
| `TLBI VAAE1` | 246.21 |
| `TLBI VMALLE1IS` | **293.85** ← most expensive |

Three things fall out:

1. **The barriers are ~22 ns of the ~200 ns.** `DSB ISH ; ISB` alone is 22.5 ns; the cheapest full
   `TLBI` sequence is 167.5 ns. The invalidation itself dominates, not the fences — so doc 16's
   "never TLBI on invalid→valid" rule is a rule about the `TLBI`, and no amount of barrier
   cleverness substitutes for not issuing it.
2. **`ASIDE1` (non-IS) is the cheapest invalidation available, at 167.5 ns.** For a design that
   switches address spaces per guest `MOV CR3` (doc 20 measured **6.5 per `fork`+`exec`**), the
   non-shareable variant is 1.29× cheaper than `ASIDE1IS` and 1.75× cheaper than `VMALLE1IS`.
3. **`VMALLE1IS` is the worst case at 293.85 ns**, and it is exactly the instruction doc 20 §item
   14 identified as the closing cost of the unprotect-and-resync episode. That measurement
   (1030–1200 ns per episode with the closing `TLBI` vs 800–930 ns without, i.e. a ~230–270 ns
   delta at 3.22 GHz) is corroborated here by an independent probe at a different clock.

`TLBI VAAE1IS`, the variant doc 16 and doc 20 quote at **279–285 ns**, measures **203.46 ns** here.
Same direction, 1.37× apart — see §0.2; do not read this as a regression or an improvement.

---

## 7. Timers, and the absence of in-guest interrupt delivery

`M`, `results/timer.txt`. This is the most consequential negative result in the document.

**The register block is fully accessible with zero exits:** `CNTFRQ=24000000`, `CNTV_CTL=0x4`,
`CNTV_CVAL=0x0`, `CNTV_TVAL` written `0x1000` reads back `0x1000`, `CNTKCTL=0x0` — all inside one
exit-counted window with **0 host exits and 0 vtimer exits** `M`.

**But the vtimer will not deliver into the guest's own vector.** Three configurations, all with the
timer armed 2,400 ticks (100 µs) ahead:

| Configuration | `hv_vcpu_run` returns in window | Guest IRQs taken | `CNTV_CTL` at exit |
|---|---|---|---|
| `PSTATE.I` **unmasked**, host **ignores** the VTIMER exit | 1 (`VTIMER_ACTIVATED` = 1) | **0** | `0x5` (`ISTATUS=1`) |
| `PSTATE.I` **unmasked**, host **acknowledges** via `set_vtimer_mask` | 1 (`VTIMER_ACTIVATED` = 1) | **0** | `0x5` |
| `PSTATE.I` **masked** throughout | 1 (`VTIMER_ACTIVATED` = 1) | 0 | `0x5`, `ISR_EL1=0x0` |

In all three the timer *fires* — `ISTATUS=1` — and in all three the guest's vector never runs; the
event surfaces as a **host exit** instead. `ISR_EL1` reads `0x0` even while the timer is asserting,
so the guest cannot even observe the pending condition through the architectural register.

**Host-injected IRQs did not arrive either.** `hv_vcpu_set_pending_interrupt` with `PSTATE.I=1`
produced **0 IRQs during the masked spin, 0 after `DAIFClr`, and 0 window exits** `M`. `I`: the
most likely explanation is that the pending-interrupt state must be re-asserted around each
`hv_vcpu_run` re-entry rather than being latched once, which this probe does not do — but that is
inference, and the measurement as it stands is that **this probe never delivered a host-injected
IRQ into the guest at all**. Recorded as `?`; doc 22's `irq_inject` probe drives the GICv3 path and
does deliver, which is the comparison that resolves it.

**The physical timer is not the guest's.** `CNTP_*` access produced **4 host exits, 4 traps, first
`ESR_EL2=0x6230f804` `EC=0x18`**, and `CNTP_CTL` stayed `0x0` `M`.

**Design consequence.** Doc 10 places interrupts and timers on the assumption that the vtimer can
be an in-guest event source. On the bare (no-GIC) path it cannot: **every timer expiry is a host
exit at ~1125 ns (§9), not an in-guest vector entry at ~31 ns (§8).** That is a ~36× difference in
the cost of a timer tick and it is the single strongest argument in this document for the GICv3
path doc 22 measures. It does not change doc 10's conclusion; it removes the fallback doc 10
implicitly assumed was there.

---

## 8. Exceptions inside the VM

`M`, `results/exc.txt`. N exceptions taken at EL1 into **our** `VBAR_EL1`, handled and returned
from in-guest. "Host exits in window" counts `hv_vcpu_run` returns across all of them.
**Single run; see the 5-run AC re-measurement below and §0.2.**

| Exception | Cost `D` | Count | **Host exits** |
|---|---|---|---|
| loop floor (no exception) | 0.11 ns | — | — |
| **`SVC #0` from EL1 → `VBAR+0x200`** | **31.28 ns** | 671,744 | **0** |
| **undefined instruction → `VBAR`** | **33.89 ns** | 167,936 | **0** |
| **permission fault → `VBAR+0x200`** | **104.91 ns** | 167,936 | **0** |
| EL0 → EL1 transition | **TIMEOUT** (940,975,211 in-guest exceptions counted, 1 total `hv_vcpu_run` return) | — | — |

`DAIF` manipulation works exactly as architected: entry `0x3c0`, after `DAIFClr #0xf` → `0x0`,
after `DAIFSet #0xf` → `0x3c0`, after `DAIFClr #4` (A only) → `0x2c0`; `ISR_EL1=0x0`,
`DISR_EL1=0x0` `M`.

**The load-bearing number is the zero in the last column.** Hundreds of thousands of exceptions —
syscalls, undefs, and permission faults — were taken, handled, and returned from with **not one
host exit**. This is the property the entire two-tier fault design in doc 17 rests on, and it is
now measured directly rather than inferred: a shadow-MMU miss can be serviced without HVF ever
learning it happened.

**A cross-probe discrepancy to record, not to paper over — and the clock does not explain it.**
This section's single battery run measured a permission fault at **104.91 ns**, while doc 20 item 4
measures "bare permission fault + `ERET`" at **25.3–27.3 ns** and doc 16 measures an in-VM fault at
28–95 ns. The first version of this document offered the clock difference as a partial explanation.
**Re-measured on wall power with the QoS fix, over 5 runs, the gap does not close** `M`:

| Exception | battery, n=1 | **AC, n=5 (range)** | doc 20 |
|---|---|---|---|
| `SVC #0` | 31.38 ns | **20.61 – 22.41** | — |
| permission fault | 104.91 ns | **74.67 – 86.79** | **25.3 – 27.3** |
| undefined instruction | 34.00 ns | **25.97 – 27.87** | — |

Everything moves by ~1.3×, and the permission fault stays **~3× above doc 20's floor**. So this is
a real difference in what the two probes measure, not a unit problem. **`?` UNRESOLVED.** The
remaining hypothesis is the one the clock story was obscuring: doc 20's item 4 is a *bare*
fault-and-`ERET` with no handler body, while this probe's handler counts the exception and re-arms,
so the two are not the same event. Closing experiment: diff the two handler bodies instruction for
instruction and add doc 20's bare form to this probe as a fourth row. Nothing in either document's
conclusions turns on the absolute value — both argue from the ratio of fault cost to the ~900–1500 ns
host exit, and even 105 ns is 9–14× cheaper than an exit — but the corpus should not carry two
unreconciled numbers for the same event.

The EL0→EL1 timeout is a probe bug, not a finding: 940 million in-guest exceptions against 1
`hv_vcpu_run` return means the guest entered a fault loop the handler never cleared.

---

## 9. The host-exit catalogue

`M`, `results/exits.txt`. Each operation executed 8× inside a window; `exits` counts
`hv_vcpu_run` returns strictly inside that window. **0 = the operation never leaves the VM.**

### 9.1 Everything that stays inside (42 of 48)

**Barriers and cache maintenance:** `NOP`, `ISB`, `DSB SY`, `DMB SY`, `IC IALLU`, `IC IALLUIS`,
`IC IVAU`, `DC CVAU`, `DC CIVAC`, `DC ISW`, `DC ZVA` — **all 0 exits**. This is the direct
confirmation of doc 21's SMC path: the entire I-cache/D-cache maintenance sequence an SMC handler
must issue, including the **inner-shareable broadcast** `IC IALLUIS`, is native.

**TLB and translation:** `TLBI VMALLE1IS`, `AT S1E1R + PAR` — 0 exits.

**MMU control:** `MSR TTBR0_EL1` (the address-space switch), `TCR_EL1`, `SCTLR_EL1`, `VBAR_EL1`,
`MAIR_EL1`, `CONTEXTIDR_EL1`, `TPIDR_EL1`, **`ACTLR_EL1` (the TSO switch)**, `CPACR_EL1`,
`DAIFClr/Set` — 0 exits.

**Clock and ID:** `MRS CNTVCT_EL0`, `CNTFRQ_EL0`, `MSR CNTV_CVAL/CTL` (arming the vtimer),
`MIDR_EL1`, `ID_AA64MMFR0_EL1`, `CTR_EL0`, `DCZID_EL0`, `MDSCR_EL1`, `DBGBVR0_EL1` — 0 exits.

**Execution:** `LDXR/STXR`, `CASAL`, FP (`FMOV`/`FADD`), `YIELD`, `SEV`, **`WFE`** — 0 exits.

**Exceptions that land in the guest's own vector, not the host:** `BRK #0` (`guestEC=0x3c`),
undefined instruction (`guestEC=0x00`), `SVC #0` (`guestEC=0x15`), faulting load (`guestEC=0x25`,
data abort at current EL) — **0 host exits, 8 guest exceptions each** (4 for `SVC`).

### 9.2 The complete exit list — six operations

| Operation | Exits | Trap `EC` | Why it matters |
|---|---|---|---|
| `MRS CNTPCT_EL0` | 8 | `0x18` | Physical counter is the host's |
| `MRS PMCCNTR_EL0` | 8 | `0x18` | **No cycle counter** (§4) |
| `MSR OSLAR_EL1` | 8 | `0x18` | Debug lock |
| **`WFI`** | 8 | `0x01` (WFx) | **Idling exits.** `WFE` does not |
| `SMC #0` | 8 | `0x17` | Secure monitor call |
| `HVC #0x14` | 8 | — | Reference: this **must** exit |

`WFI` exiting while `WFE` does not is a design lever worth naming: a guest spin that needs to yield
without paying 1125 ns should use `WFE`+`SEV`, not `WFI`.

### 9.3 The exit itself

`HVC` round trip (guest → host → guest), timed on the **host** across `hv_vcpu_run`:
**median 1125 ns, p10 1000, p90 1292, min 917** `M` (battery, n=1).

Doc 02 §6.1 and doc 16 quote **808 ns** for the same measurement. The first version of this
document read the difference as a power-state effect. **Re-measured on wall power, it is not** `M`:

| Probe | battery | **AC (5 runs)** |
|---|---|---|
| `el1_features exits` | 1125 ns | **1292 – 1458 ns** (*slower* on AC) |
| `irq_inject hvc` (`22 §5`) | 1487.6 ns | **913 – 1200 ns** (faster on AC) |

The two probes move in **opposite directions**, so neither supports a power-state story; see §0.2.
**The design number remains 808 ns** from doc 02, which measured it with the most samples and the
tightest harness. Everything measured here is 1.1–1.8× that, and the honest reading is that this
machine delivers an exit somewhere in the **0.8–1.5 µs** band depending on conditions nobody in
this corpus has yet isolated.

---

## 10. What this changes elsewhere in the corpus

| Document | Claim | Effect of this document |
|---|---|---|
| `02` | 808 ns exit floor | **Confirmed in shape**, but this probe measures 1125–1458 ns and `22 §5` measures 913–1488 ns for the same event. Doc 02's 808 ns remains the design number; the honest band on this machine is 0.8–1.5 µs (§9.3) |
| `10` | vtimer → `TSC_DEADLINE`, exit-budget 1.2M/sec/core | **Constrained.** On the bare path a vtimer expiry is *always* a host exit; there is no in-guest delivery fallback (§7). Strengthens the case for the GICv3 path |
| `12` | Perf ranking, rank 8 "A/D + TLB" | `TLBI` costs now measured per variant (§6.2); `ASIDE1` at 167.5 ns is the cheapest lever |
| `14` | `ACTLR_EL1.EnTSO` writable at guest EL1 | **Confirmed on the register surface**: not trapped, round-trips, survives a host exit (§1.3) |
| `16`, `20` | Shadow-MMU costs, "never TLBI on invalid→valid" | **Mechanism confirmed native** (§9): zero exits for `TLBI`, `AT`, `MSR TTBR0_EL1`. §6.2 shows the barriers are only ~22 ns of ~200 ns, so the rule is about the `TLBI` itself |
| `17` | `AT` probe-then-execute refuted at 24–55× a store | **Independently corroborated** at 386× a load (§5.2) |
| `20` | `TLBI VAAE1IS` at 279–285 ns; closing `VMALLE1IS` costs ~230–270 ns | Corroborated at 203–225 ns / 294 ns, reproducing to 1.09× across 7 runs and both power states (§6.2). The residual 1.3× against doc 20 is real and unexplained |
| `21` | SMC maintenance path; `IC IVAU`/`IALLU` native | **Confirmed with an explicit exit count of 0**, including `IC IALLUIS` (§9.1) |
| `23` | FEXCore's backend emits `RNDR` among EL0-legal registers | **`RNDR` is absent on this host** (§3.2). A FEXCore-derived JIT must not emit it |
| `24` | SMEP/SMAP → PAN/XN mapping | `PAN` and `UAO` confirmed settable from guest EL1 (§3.1) |

---

## 11. Open items this document leaves

| # | Item | Closing experiment |
|---|---|---|
| 1 | `TCR_EL1.EPD0` scored `?` although the evidence says it applies (§2.1) | Relocate payload + vectors into the TTBR1 half (`T0SZ=T1SZ=17`, proven in `20` item 16), then set `EPD0=1` and read a TTBR0 VA. Expect a **data** abort, `DFSC` level 0, execution continuing |
| 2 | **LSE2 unaligned access faults despite `AT=1`** (§3.3) | Dump the leaf `AttrIndx` for the address under test; re-run with an explicitly Normal-WB attr and with `SCTLR_EL1.A` explicitly cleared in the same window |
| 3 | Host-injected IRQ never delivered (§7) | Re-assert `hv_vcpu_set_pending_interrupt` immediately before each `hv_vcpu_run` re-entry; compare against doc 22's GICv3 path |
| 4 | **Permission fault ~3× doc 20's floor, and the clock does not explain it** (§8). Re-measured on AC over 5 runs: 74.67–86.79 ns vs doc 20's 25.3–27.3 | Diff the two handler bodies instruction for instruction; add doc 20's *bare* fault-and-`ERET` form to this probe as a fourth row so the same binary measures both |
| 5 | EL0 `TPIDRRO_EL0` write: undef in one run, EL2 trap in the next (§2.2) | Repeat n≥20; if genuinely bimodal, find what differs between the two paths |
| 6 | EL0→EL1 transition test times out (§8) | Probe bug — the handler never clears the fault condition. Fix the handler, then measure |
| 7 | ~~Absolute timings are power-state dependent~~ — **refuted and replaced** (§0.2). This probe's core-clock-bound timings carry ~2× run-to-run spread that power state, QoS and background load all fail to explain; `pt_wp` is consistently tighter and higher and nobody knows why | Instrument the probe to report the core it ran on (`MPIDR_EL1` on the host side, or `sysctlbyname("hw.perflevel*")` accounting around the run) so placement stops being a hidden variable. Until then, report medians over n ≥ 10 with the range |

---

## 12. Reproduce

```sh
cd probes
./build.sh el1_features/el1_features.c
./el1_features/bin/el1_features all        # or one of:
./el1_features/bin/el1_features time       # §4  the in-guest clock
./el1_features/bin/el1_features regs       # §1  system-register surface
./el1_features/bin/el1_features applies    # §2  does the write apply
./el1_features/bin/el1_features feat       # §3  FEAT_* ID bit vs executed instruction
./el1_features/bin/el1_features at         # §5  AT instructions
./el1_features/bin/el1_features tlbi       # §6  TLBI variants and latency
./el1_features/bin/el1_features timer      # §7  timers and interrupt delivery
./el1_features/bin/el1_features exc        # §8  exceptions inside the VM
./el1_features/bin/el1_features exits      # §9  host-exit catalogue
```

Requires ad-hoc signing with `com.apple.security.hypervisor` (`probes/entitlements.plist`, applied
by `build.sh`). No SIP changes. **Record the power state** (`pmset -g ps`) alongside any timing you
intend to compare against another session — see §0.2.

`probes/feat_inventory/feat_inventory.c` holds the ID-register decode tables shared by §3.

## References

`01` Apple TSO; `02` Hypervisor.framework (the 808 ns floor); `10` interrupt/timer dossier;
`14` gate results (`ACTLR_EL1.EnTSO`, `T0SZ=16`); `16` shadow-MMU costs; `17` precise exceptions
(the `AT` refutation this corroborates); `20` shadow-PT coherence (`TLBI` costs, ASID scoping);
`21` SMC detection (the cache-maintenance path this confirms is native); `22` async interrupt
injection (the GICv3 path that answers §7); `23` FEXCore retrofit (`RNDR`); `24` boot path
(PAN/UAO). External: ARM DDI 0487 for `ESR_EL1` and `PAR_EL1` encodings.

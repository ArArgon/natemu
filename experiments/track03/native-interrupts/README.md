# Native interrupts: HVF GIC, virtual timer, SGIs and an in-VM precise stop

Status: design study, October 28, 2026. Evidence only; nothing in `crates/` or `tools/fexc` was changed.
Owner direction: make x86 exceptions and interrupts as native as possible (Arm exceptions, virtual interrupts, the GIC,
timers) instead of host round trips and polling, keeping precise x86 delivery at instruction boundaries, nothing
kernel-specific.

Labels: **measured** (this study, M4, macOS 26.6.2 build 25G83, SDK 26.4); **header** (Hypervisor.framework headers in
`/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk/System/Library/Frameworks/Hypervisor.framework/Headers`, file:line);
**Arm ARM** (Arm Architecture Reference Manual, DDI 0487); **not verified** (neither).

Files:
- `probe.c`: a standalone HVF probe, independent of natemu. It has one or two vCPUs, a 4 KiB stage 1 per vCPU with its own ASID,
  translated-style EL0 code and a resident-style EL1 vector table. The EL0 code is 16 blocks of 64 bytes. Each block starts
  with `bl pad` (the E1 stop site), checks `x2 == 3*x1` with `cmp/b.ne`, and stores progress. The pad is a per-vCPU EL0
  execute-only page holding `ret`. The modes are listed in the file header.
- `build.sh OUT` builds and signs `OUT/probe`. `run.sh OUT [load]` runs the matrix below. `load` adds 12 busy `yes`
  processes on the 10-core M4.
- Raw output is in `/private/tmp/claude-501/-Users-liaozping-orca-natemu/025ed521-9978-4fb2-aaf7-b90da95d37f3/scratchpad/nirq/`:
  - `final.txt` (unloaded) and `final-load.txt` (loaded). The two `asid` rows at their end were re-run after fixing a
    fall-through bug in the probe's `armbench asid` loop; the earlier `NOT DONE` rows above them are that bug.
  - `run7.txt`: today's host-driven stop (`stop-premise` probe, 1 vCPU, 10,000 kicks) in the same session.
  - `alt.txt`: the stop alternatives of §3a.
  - `run1.txt` to `run6.txt`: development runs. They include the SPI routing failure described in §1.

Ranges below are "unloaded / loaded" from `final*.txt` unless stated. The guest clock is CNTVCT with
`hv_vcpu_set_vtimer_offset(0)`, so CNTVCT = `mach_absolute_time()` (header `hv_vcpu.h:438-449`). Its resolution is 41.7 ns
(24 MHz). The host and the guest timestamps therefore share one clock.

## Answer in brief

- With the in-kernel GIC (`hv_gic_create`), every asynchronous source we need reaches a running vCPU with **zero exits to
  the host process**:
  - the virtual timer (deadline to EL1 handler p50 0.96 / 1.0 us);
  - an SGI from another vCPU's EL1 (`ICC_SGI1R_EL1`; send to handler p50 1.88 / 1.83 us);
  - an SPI raised by any host thread (`hv_gic_set_spi`; call to handler p50 1.38 / 1.38 us; the call itself 0.29 us).
- A vCPU in `wfi` is parked by HVF itself. No WFI exit was seen with the GIC, and the vCPU woke on a timer or an SGI.
- The IRQ arrives at EL1 from EL0 at a boundary between native instructions with the EL0 state intact. The resident then
  arms an **in-VM stop** that fires at the next `bl pad` (the next block entry). Over 4,000 IRQs per run in every variant:
  - every IRQ produced exactly one stop, and every stop was at a block entry (0 misplaced, 0 unexpected);
  - the EL0 register/flags invariant never failed;
  - there were zero host exits.

  IRQ entry to stop takes 1.7-2.3 us. About 1.8 us of that is the GIC acknowledge and end-of-interrupt (`ICC_IAR1_EL1`
  read about 0.9 us, `ICC_EOIR1_EL1` write about 0.85-0.9 us). These accesses cost close to a host round trip each but
  cause no host exit. Arming costs:
  - descriptor UXN plus TLBI by VA and ASID: 0.18-0.36 us;
  - patching the pad word to `brk` with DC CVAU/IC IVAU: 0.08-0.11 us;
  - switching TTBR0 to a "stop view" with its own ASID: about 0.02 us.
- **Recommendation:** use the GIC as a doorbell for every asynchronous x86 event: the virtual timer for the LAPIC timer,
  SGIs for IPIs, shootdowns, NMI and the mailbox, and a per-vCPU SPI for host-originated events. The resident turns each
  doorbell into the in-VM stop and applies all x86 semantics in its own LAPIC and eligibility model at the next block
  entry. Keep `hv_vm_protect` and `hv_vcpus_exit` only as a watchdog fallback. The GIC is not an x86 interrupt
  controller: it only rings the bell.

## 1. Available mechanisms

| Mechanism | What the headers say | Measured here |
|---|---|---|
| `hv_vcpu_set_pending_interrupt(v, IRQ\|FIQ, bool)` | macOS 11+. "Must be called by the owning thread." "The pending interrupts automatically cleared after hv_vcpu_run returns… expected [to] be called before every hv_vcpu_run" (`hv_vcpu.h:301-312`); `hv_interrupt_type` IRQ/FIQ (`hv_vcpu_types.h:427-433`). | A peer thread can only request it, through `hv_vcpus_exit` (`hv_vcpu.h:371-381`), and the owner must set it before re-entry. The line is **level**: a handler that returned without an exit was re-entered 5 of 5 times (`inj 1 level`). So the guest **must exit once more to drop it**: 2 host exits per IRQ (cancel plus ack `hvc`). |
| In-kernel GIC v3: `hv_gic_create`, distributor, redistributors, ICC system registers, MSI | macOS 15+ (`hv_gic.h:48`). It is created after the VM and before any vCPU; one per VM; affinity routing; vCPUs set MPIDR_EL1 (`hv_gic.h:22-49`). Redistributor and ICC accessors: "Must be called by the owning thread" (`hv_gic.h:116-170`). `hv_gic_set_spi(intid, level)`: "Setting level also causes an edge… for an edge triggered interrupt" (`hv_gic.h:51-66`); no owning-thread restriction is stated. MSI through `hv_gic_send_msi` needs an MSI region and an INTID range (`hv_gic.h:68-77`, `hv_gic_config.h:56-86`). Reserved PPIs: virtual timer 27, physical timer 30, PMU 23 (`hv_gic_types.h:38-50`). Save/restore: `hv_gic_state_*` and `hv_gic_set_state` (`hv_gic_state.h`, `hv_gic.h:250-265`). | Created on macOS 26.6.2 with the distributor 64 KiB, the redistributor region 32 MiB (128 KiB per vCPU) and SPIs 32-1019. GICD_CTLR read back `0x53` (DS=1, ARE, both groups enabled). ICC_PMR keeps 5 bits (`0xff` reads back `0xf8`, 32 priority levels). ICC_CTLR_EL1 = `0x40400`. **Undocumented quirk:** with IROUTER written before the vCPU existed, the SPI stayed pending in GICD_ISPENDR1 and was never delivered, even after `hv_vcpus_exit` and re-entry. Writing IROUTER again after vCPU creation fixed it; priority was irrelevant (`spi … noprio`). GICR_WAKER (0x14) is not accessible (`HV_BAD_ARGUMENT`) and was not needed. MSI: not tested. |
| `hv_gic_set_spi` into a running vCPU | (above) | Called from a non-owner thread with no `hv_vcpus_exit`, it was delivered 4,000/4,000 times with 0 host exits. Call 0.29 us p50; call to guest handler p50 1.38 us (p99 1.6 / 1.5 us). |
| SGI from vCPU EL1 (`msr ICC_SGI1R_EL1`) | The GIC "supports… GIC CPU system registers" (`hv_gic.h:29-30`). | Target running EL0: 4,000/4,000 delivered, 0 host exits on either vCPU, send to handler p50 1.88 / 1.83 us. The sender's `msr` plus `isb` costs 1.27 us (no exit). Target parked in `wfi`: p50 6.5 / 6.6 us, max 13 / 19 us. |
| Virtual timer (CNTV) | Without a GIC: exit `HV_EXIT_REASON_VTIMER_ACTIVATED`, which masks the timer automatically until `hv_vcpu_set_vtimer_mask(false)`, "called during a trap of the EOI" (`hv_vcpu_types.h:43-53`, `hv_vcpu.h:403-426`). With a GIC, the virtual timer is PPI 27 (`hv_gic_types.h:48`). | **No GIC:** 2 host exits per tick (VTIMER_ACTIVATED, then the ack `hvc` to unmask); deadline to handler p50 0.79-0.92 us. **GIC:** 0 host exits and no VTIMER_ACTIVATED exits; deadline to handler p50 0.96 / 1.0 us, p99 1.2 / 1.04 us. |
| WFI/WFE | The headers say nothing about WFI. | **No GIC:** every `wfi` at EL1 exits (EC 0x01); the host must advance PC and park, here with `mach_wait_until(CNTV_CVAL)`. **GIC:** 0 WFI exits over 1,000 timer and 2,000 SGI wake-ups. HVF blocks inside `hv_vcpu_run` and wakes on the virtual timer and on SGIs. Waking from an SPI was not measured. Timer wake is **slow** in both cases: deadline to handler p50 34-36 us, min 11 us, max 60 us. The cause (host idle-state exit or timer leeway) is not verified. `wfi` at EL0 traps to EL1 (EC 0x01), not to the host. |
| EL0 masking interrupts | Arm ARM: with SCTLR_EL1.UMA = 0, EL0 `MSR DAIFSet` traps to EL1. | `msr daifset,#2` at EL0 gives EC 0x18 to EL1. `msr ICC_PMR_EL1` and `msr ICC_SGI1R_EL1` at EL0 give EC 0x00 (UNDEFINED) to EL1, with or without the GIC. EL0 cannot mask or raise interrupts. This matches `el0-study` row 13. |

## 2. Measurements (comparison with host round trips)

| Path, 1 vCPU unless stated | Host exits per event | Latency p50 (p99) | Other cost |
|---|---|---|---|
| Host stop today: `hv_vm_protect` stop granule (`stop-premise`, `run7.txt`) | 1, plus the host restores the granule and injects the stop gate (not re-measured) | call to host-seen exit 1.33 us (22.8 us) | the protect call itself 1.04 us (24.8 us); 2-200 us p99 contention with several callers and ms tails when oversubscribed (`stop-premise` README) |
| Host stop fallback: protect + `hv_vcpus_exit` (`run7.txt`) | 1 | 2.54 us (21.7 us) | |
| IRQ without GIC: `hv_vcpus_exit` + `set_pending_interrupt` | 2 (cancel, ack `hvc` ≈ 0.66 us each way per `el0-study`) | call to handler 1.17 / 1.13 us (1.33 / 1.21) | needs the owner thread for every IRQ |
| SPI with GIC: `hv_gic_set_spi` from any host thread | 0 | 1.38 / 1.38 us (1.63 / 1.50) | call 0.29 us; in guest: IAR 0.95 us + EOI 0.90 us |
| Virtual timer, no GIC | 2 | deadline to handler 0.79 / 0.92 us | |
| Virtual timer, GIC | 0 | 0.96 / 1.0 us (1.21 / 1.04) | in guest: IAR 0.89-0.96 us + EOI 0.84-0.92 us |
| SGI guest to guest (2 vCPUs), target running | 0 | 1.88 / 1.83 us (2.17 / 3.25; one 541 us outlier when loaded) | sender `ICC_SGI1R_EL1` 1.27 us |
| SGI to a target in `wfi` | 0 | 6.5 / 6.6 us (11.0 / 10.8) | |
| Timer wake from `wfi`: GIC (in-kernel park) / no GIC (host `mach_wait_until`) | 0 / 2 + 1 WFI exit | 34 / 36 us, and 34 / 36 us | slow in both; cause not verified |
| GIC handler entry to `eret` (IAR + EOI + bookkeeping) | 0 | 1.8-2.0 us mean | without GIC the same handler takes 0.03-0.09 us |

The GIC removes host exits and host-thread work, not vCPU time: acknowledging and ending an interrupt costs about 1.8 us
of the target's time. That is about the same as the two exits of the no-GIC path, which are 2 × about 0.66 us plus host
work. Whether these ICC accesses trap into the macOS kernel is not verified; we see only that they cost about 0.9 us each
and never reach the host process.

## 3. Precision design: an IRQ at EL1 becomes a stop at the next block entry

The probe's mode `stop` has these steps:
1. The virtual timer fires every 20-105 us (random) while EL0 runs the blocks.
2. The EL1 IRQ handler reads IAR, **arms the stop** (one of three in-VM ways, below), writes EOI and returns.
3. EL0 continues to its next `bl pad`, which now traps to EL1. That is the stop.
4. The stop handler checks that the trap is at the pad and that LR = a block's first word + 4. It records the time,
   disarms, and resumes at LR, as if the pad had returned.

| Arm/disarm method (in the resident, no host) | Stops / IRQs | Misplaced or unexpected stops | Arm cost in handler, mean | IRQ entry to stop p50 (p99) | Arm + disarm pair, loop of 10^6 |
|---|---|---|---|---|---|
| Pad descriptor UXN=1 + `tlbi vale1` (ASID, VA) + `dsb nsh` + `isb` | 4000/4000 | 0 | 0.21 / 0.35 us | 2.08 / 2.25 us (3.75 / 2.67) | 0.24-0.58 us (varies run to run) |
| same with `tlbi vale1is` + `dsb ish` | 4000/4000 | 0 | 0.18 / 0.36 us | 2.04 / 2.29 us | 0.25-0.58 us |
| Pad word `ret` → `brk #5`, `dc cvau`, `ic ivau`, DSBs, `isb` (pad is EL1-RW, EL0 execute-only) | 4000/4000 | 0 | 0.11 / 0.08 us | 1.88 / 2.08 us (2.33 / 2.17) | 0.15-0.22 us |
| `msr ttbr0_el1` to a per-vCPU "stop view" root (own ASID, pad UXN=1) + `isb` | 4000/4000 | 0 | 0.02 / 0.03 us (at the clock's resolution) | 1.75 / 1.92 us (2.17 / 1.96) | 0.017-0.019 us |

All methods work and none needs the host. The resident **can** change its own stage-1 permission and TLBI by ASID. The
remaining latency (about 1.8 us) is the GIC acknowledge and end-of-interrupt, not the arming. A host round trip is not
needed.

Where the IRQ lands (measured, offset of ELR inside a 64-byte block, 4,000 IRQs):
- about 75% at word 0 (the `bl` or the pad's `ret`);
- about 24% at word 1;
- a few mid-block, 0-23 per run at word 13.

So the M4 takes IRQs mostly at branch targets, but not only there. Why is not verified; the design must not depend on it
(Arm ARM: an asynchronous exception is taken between instructions, with ELR = the first instruction not executed). Over
about 1.4 × 10^8 blocks per run, the `x2 == 3*x1` / `cmp`/`b.ne` check never failed, so registers and NZCV survived
every interruption. If the IRQ lands at the pad itself, the stop fires at that same block's entry, which is also a
correct boundary.

**Revisiting the earlier rejection** ("a virtual IRQ… owner-thread only, lands at any native PC", `slice3-contract.md` §1):
- "Owner-thread only" is true of `hv_vcpu_set_pending_interrupt` (header). It is not true of `hv_gic_set_spi` or of
  guest SGIs (measured).
- "Lands at any native PC" is true, and it is harmless when the IRQ only arms the existing pad stop. Precision is still
  "at the next block entry".

**x86 eligibility on top of this.** The Arm IRQ is only a doorbell; nothing x86 is decided by the GIC. At the stop, the
resident runs its eligibility check against its own x86 state:
- **IF = 0:** the resident acknowledges the doorbell (IAR/EOI) and leaves the x86 vector pending in its own state, so the
  next doorbell is not needed. Delivery is re-checked in the transactions that can set IF: STI, POPF and IRET, which are
  already resident transactions in the plan.
- **STI / MOV SS shadow:** if the stop falls inside a shadow, the resident runs one more x86 instruction through the
  step slot and re-checks. Whether FEX block boundaries can split a shadow is not verified.
- **NMI blocking:** a resident bit, cleared by IRET.
- **HLT:** when no x86 event is eligible, the resident executes `wfi` at EL1 in a loop. With the GIC it parks inside HVF
  with no host exit and wakes on a timer or SGI doorbell (measured). An SPI wake-up is not measured. The Arm ARM says a
  pending interrupt wakes WFI even while it is masked by PSTATE; with masking not measured. The timer wake-up from `wfi`
  costs about 34 us, against about 1 us when running. That matters only for idle CPUs, but it is the latency of a
  sleeping CPU's timer tick.

## 3a. Stop mechanism alternatives (the hot-path cost of the E1 `bl pad`)

The orchestrator asked this after E1 landed. E1's `bl pad` at every block entry costs about +0.45 ns per entry in
natemu: call-heavy loops +85%, mixed +45% (natemu measurement, not repeated here). Raw output:
`…/scratchpad/nirq/alt.txt`, with 4,000 virtual-timer IRQs per stop run, the GIC and EL0 blocks. The block's first word
is set by the host to `bl pad`, `nop` or `ldr x7, [x8]`.

| Option | Block-entry word | Probe ns per 16-word block (two runs) | Arm cost in IRQ handler | IRQ entry to stop p50 (p99) | Precise (stop at a block entry) | Gap or caveat |
|---|---|---|---|---|---|---|
| E1 pad, stop by descriptor (reference) | `bl pad` | 1.695, 1.693 | 0.28 us | 2.08 us (3.88) | 4000/4000 | the +0.45 ns per entry in natemu |
| **(a1) unlinking**: the IRQ handler patches the exit `b` of the block containing ELR into `brk #6` (DC CVAU / IC IVAU); if ELR is already a block's first word it stops at once | none (`nop` here) | 1.669, 1.676 | about 0.16 us per armed stop (0.0156 us mean over all 4,001 stops, of which 391 armed) | 2.04 us (3.54) for the 391 armed; the other 3,610 stopped at once in the handler | **4001/4001**, 0 misplaced | It must patch **every** exit of the current block: conditional exits and the indirect exit (`dispatch_fast` / L1) too. The probe blocks have one live exit. Blocks are shared, so a peer running the same block takes the `brk` and must just continue (one trap, about 35 ns per `el0-study`). Arm ARM allows concurrent modification only for a small set of instructions (B, BL, BRK, NOP, …; "concurrent modification and execution of instructions"); the exact list is not re-checked here. The resident needs to know whether ELR is a block start (the code-cache map). Helper calls and in-block loops: not covered. |
| (a2) remove EL0 execute from the code area (stage-1 UXN on the code page + TLBI by ASID, or the stop-view TTBR0) | none | as (a1) | 0.29 us (TLBI) / 0.02 us (TTBR0) | 2.17 / 1.92 us | **not precise: 418/4000 and 447/4000 mid-block** | Execute permission is checked at the next instruction fetched, which is ELR itself. The stop is wherever the IRQ landed (words 4, 5, 13 here), so this gives nothing beyond the IRQ itself. Making it precise needs (a1) or a step to the block end: not measured. Not recommended. |
| **(b) load check**: `ldr x7, [x8]` at the block entry from an EL0 read-only page; arming makes the page EL0-inaccessible (descriptor + TLBI, or the stop-view TTBR0) | one load | 1.686, 1.686 | 0.29 us (TLBI) / 0.012 us (TTBR0) | 2.17 us (3.88) / 1.92 us (2.50) | **4000/4000** both | **Gap:** every canonical address is a guest address, so this page must sit in the non-canonical hole. A guest load there would read zeros instead of #GP, a second documented deviation page per vCPU, next to the guest-state page. Putting the check word inside the guest-state page itself would avoid the second page. Arming would then make FEX's own guest-state accesses fault between the IRQ and the next entry; the resident would have to emulate those. Not measured. |
| (c1) stop-view TTBR0/ASID switch as the arming step for the pad or (b) | (as chosen) | — | 0.012-0.03 us | 1.75-1.96 us | 4000/4000 | It needs a second root per guest address space; x86 invalidations must cover both ASIDs. Integration is not verified. |
| (c2) pad word patched to `brk` (§3) | `bl pad` | as pad | 0.08-0.11 us | 1.88-2.08 us | 4000/4000 | It keeps the `bl` on the hot path. |

Reading:
- **The per-entry cost was not resolvable in this probe.** All three entry words are within 0.03 ns per block of each
  other (1.67-1.69 ns per 16-instruction block). This loop is limited elsewhere and has no guest call/return pattern. So
  the probe neither reproduces nor refutes natemu's +0.45 ns for `bl pad`, and the load's cost must be measured in natemu
  (the earlier study's "about 0 ns" is not re-validated in call-heavy loops). Why `bl`/`ret` costs more in natemu (for
  example, interaction with the return-address predictor during guest calls) is not verified.
- **Is anything needed on the hot path? No, with unlinking (a1).** It was precise in every case measured, and its
  latency is the same as the pad's because the GIC acknowledge dominates. Its cost moves to the stop: about 0.16 us to
  patch and restore the exit, plus covering every exit kind of the block. The indirect exit is the open part. One option
  is a per-vCPU `dispatch_fast` entry word that the resident patches the same way, so a computed exit also reaches the
  resident; not measured. Shared blocks give peers occasional harmless `brk` traps.
- If a hot-path word is kept, the load check (b) is the cheapest precise one in this probe. It costs one more
  documented non-#GP page per vCPU unless the check moves into the guest-state page, which is not measured.
- Removing execute permission from code (a2) is not precise and should not be used alone.

## 4. Proposed mapping of x86 events

| x86 event | Native mechanism (doorbell) | Where x86 semantics apply | Stays emulated, and why |
|---|---|---|---|
| Local APIC timer (one-shot, periodic; no TSC-deadline) | The resident programs CNTV_CVAL from the LAPIC count; PPI 27 fires without a host exit (0.96 us). | Resident: LVT mask, mode, divider, current count, vector into IRR at the stop. | LAPIC counting from virtual time V. CNTVCT runs at 24 MHz like the LAPIC crystal, but whether CNTV can carry V (offset, pausing) is the L5 question: not verified. |
| IPIs (fixed, NMI, shorthands) | The guest's ICR write is emulated by the resident; it sets the target's IRR atomically in shared resident memory, then `msr ICC_SGI1R_EL1` to the target (1.88 us, 0 exits). | Target resident at its next block entry: priority, TPR/PPR, ISR, EOI. | Destination modes and logical destinations are decoded in the resident. Broadcast SGI (IRM=1): not measured. |
| AP start, mailbox (no INIT/SIPI) | APs wait in resident `wfi`. The mailbox page is write-protected for guest stores; the writer's resident takes the fault, applies the store and sends an SGI to the AP. | Resident responder (snapshot, clear command, enter vector). | The protocol is the ACPI mailbox model. This composition of measured parts (EL0 fault, about 36 ns per `el0-study`; SGI to a `wfi` target 6.5 us) was not measured end to end. |
| Device interrupts (IOAPIC/PIC, in the host per the plan) | The host device thread computes the x86 vector and target, writes the target's IRR in guest memory, and calls `hv_gic_set_spi` on that vCPU's own SPI (call 0.29 us, 1.38 us to handler, 0 exits). | Resident LAPIC: priority and delivery at the stop. | IOAPIC/PIC routing and level/edge state stay in the host. A level-triggered EOI must still reach the host model (one `hvc` per level EOI, as today). |
| NMI | Same doorbell (an SGI or the per-vCPU SPI) plus an "NMI pending" bit in resident state. | Resident: one pending bit, blocking until IRET. | GIC NMI features are not used (not verified on HVF). |
| x86 exceptions (#PF, #GP, #UD, …) | Already Arm synchronous exceptions to the resident (EL0 faults to EL1, `el0-study`). | Resident delivery transaction (IDT, error code, CR2, frame). | Unchanged: no host involvement. |
| HLT | Resident `wfi` at EL1; HVF parks it (0 exits); doorbells wake it. | Resident: wake only for an eligible event, else `wfi` again. | 34 us timer wake from park (cause not verified). |
| TLB-shootdown-style kicks (the S3d shootdown wait; code-cache invalidation stops) | Guest initiator: SGI to each peer. Host initiator (DMA writes): `hv_gic_set_spi` per peer. Peers stop at their next block entry and acknowledge in shared memory; the initiator spins in its resident. | Resident drain and acknowledge. | Linux's own shootdown is guest software over x86 IPIs (row 2). The wait loop stays in the initiator. |

The LAPIC semantics stay in the resident because the GIC's do not match:
- x86 has 256 vectors with 16 priority classes, TPR/PPR, ISR nesting until EOI, and the IRR;
- the GIC has 32 priority levels here and per-INTID active state;
- SPIs are global, not per-vCPU vectors.

Mapping x86 vectors one-to-one onto INTIDs would make the GIC's active and priority rules fight the LAPIC's. A doorbell
avoids that.

## 5. Recommendation and risks

**Recommendation** (needs owner sign-off; it changes the slice-3 stop contract):
1. Create the HVF GIC for every VM. Give each vCPU a doorbell SPI routed to it, with IROUTER written after the vCPU
   exists. Reserve SGI INTIDs for IPI, NMI and kick. Use PPI 27 for the LAPIC timer.
2. The resident's IRQ handler does IAR, arms the in-VM stop, EOI and `eret`; at the stop it runs x86 eligibility and
   delivery. Arming method:
   - Patching the pad word is the simplest: about 0.1 us, no TLBI, no extra tables; but the pad page becomes EL1-writable.
   - The stop-view TTBR0/ASID switch is the cheapest (about 0.02 us), but needs a second root per address space and
     double TLB coverage for x86 invalidations; that integration is not verified.
   - Descriptor plus TLBI works but costs 0.2-0.6 us.
   - To remove the per-entry `bl pad` entirely, unlink instead: patch the current block's exits (§3a). It was precise in
     4001/4001 cases and puts nothing on the hot path. Still open: covering indirect exits and shared blocks.
3. The host uses `hv_gic_set_spi` instead of `hv_vm_protect` to kick: 0.29 us from any thread, with no protect-call
   contention. `hv_vm_protect` + `hv_vcpus_exit` remain only as watchdog fallback.
4. HLT is resident `wfi`.

Measured gain against host round trips:
- 0 host exits per interrupt, against 2 (no-GIC injection or timer) or 1 plus host restore and inject (protect stop);
- no host-thread participation for timers, IPIs and shootdowns.

Not gained:
- target vCPU time: about 1.8 us per doorbell for IAR plus EOI, comparable to the two exits it replaces;
- the 34 us wake-up from `wfi` by the timer.

Risks and unknowns:
- **macOS requirement:**
  - The GIC API needs macOS 15+ (`API_AVAILABLE(macos(15.0))`, `hv_gic.h`).
  - Tested only on macOS 26.6.2 on an M4, with 1-2 vCPUs.
  - The API is young: the IROUTER-before-vCPU quirk is undocumented, and GICR_WAKER is not reachable.
  - The `hv_gic_create` before-vCPUs rule is in the header (`hv_gic.h:35-38`).
- **ICC access cost:** about 0.9 us each (IAR, EOI) and 1.27 us for SGI1R, on every doorbell. The mechanism is not
  documented and not verified. Batching (one doorbell per stop, any number of x86 vectors) bounds the cost.
- **Wake-up from park:** 34 us timer and 6.5 us SGI, cause not verified. The SPI wake from `wfi` is not measured.
- **Load:** tested with 12 `yes` processes. Tails stayed in the µs range except one 541 us SGI outlier. Oversubscription
  with competing high-QoS threads was not tested; the vCPU thread must still be scheduled.
- **Local TLBI (`vale1`) when HVF migrates a vCPU between cores:** correctness relies on HVF (not verified). The probe
  used one ASID per vCPU and per-vCPU tables; the `is` variant costs about the same.
- **Not covered:** MSI, broadcast SGIs, more than 2 vCPUs, EL2/nested GIC registers (ICH/ICV), and the interaction with
  FEX helpers and the step slot (the "one block" bound of the existing contract is assumed unchanged).

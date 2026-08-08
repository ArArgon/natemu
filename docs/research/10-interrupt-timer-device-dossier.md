# 10 — Interrupt, Timer, and Device Virtualization Dossier

**Scope:** consolidated device strategy complementing 02 (HVF GIC/timer/IPI costs), 06 §8–10 (x86 devices/TSC/APIC), 07 §3.4/3.7/§6 (PV interrupts/timers, Tier model), 09 N-04/N-06 (TSC/TSO atomics).
**Status:** synthesis — no new measurements; all quantitative claims cite 02/06/09 or are flagged [UNVERIFIED].
**Host baseline:** M2 Pro, macOS 26.5.2, HV 259.5.15, 16 KB stage-2 granule default, 40-bit IPA max (`02 §2.1–2.2`). M2 = no EL2/nested.

---

## 1. Interrupt controller — three options and the bridge problem

### 1.1 What HVF gives you

`hv_gic_create()` (`02 §3.1`, macOS 15+, **one per VM, after `hv_vm_create` before first `hv_vcpu_create`**) furnishes an **in-kernel GICv3**: distributor, redistributors, MSI, and CPU-interface registers. Measured sizing on M2 Pro (`02 §3.1`):

| Region | Size | Alignment | Notes |
|---|---|---|---|
| Distributor | **64 KB** (`0x10000`) | 64 KB | One instance |
| Redistributor region | **32 MB** | 64 KB | 256 frames / 128 KB per vCPU (2×64 KB) — sized >> 64-vCPU max |
| Per-vCPU redistributor | **128 KB** | 64 KB | Two 64 KB frames |
| MSI region | **64 KB** | 64 KB | `hv_gic_config_set_msi_region_base` |
| SPI range | **988 SPIs, INTID 32–1019** | — | `GICD_TYPER=0x0478001f` (ITLinesNumber=31) |

Injection: `hv_gic_set_spi(intid, level)` (level/edge SPIs) and `hv_gic_send_msi(address, intid)` for MSI. Save/restore: opaque versioned blob (`hv_gic_state_*`). Reserved INTIDs 23 (PMU), 25 (GIC maintenance, EL2-only), 26 (EL2 phys timer, EL2-only), **27 (EL1 virtual timer)**, 30 (EL1 phys timer) — `02 §3.1`.

Topology constraint: each vCPU must program `MPIDR_EL1` before `hv_gic_get_redistributor_base` succeeds; **no vCPU hotplug** after `hv_vcpu_run` begins (`02 §3.1`).

`ICH_LR0–15_EL2` / `ICV_*` list registers are **EL2-gated** (`hv_gic.h:179–180`, measured `HV_UNSUPPORTED` on M2, `02 §3.1`). For a normal (non-nested) VM the framework manages list registers internally; you never touch them.

### 1.2 Fallback: userspace GIC via `hv_vcpu_set_pending_interrupt`

Before macOS 15 and on hosts without `hv_gic_create`, the only mechanism is one virtual IRQ+FIQ line per vCPU via `HCR_EL2.VI/VF` (`02 §1.5/§3.1`):

```c
hv_vcpu_set_pending_interrupt(vcpu, HV_INTERRUPT_TYPE_IRQ, true);
```

Cleared automatically on next `hv_vcpu_run` return — must be re-asserted before every run (`02 §3.1`). No distributor, no priority, no SGI/MSI, no EOI. Sufficient for correctness but you must emulate all GIC priority/masking logic yourself.

### 1.3 Inside-VM LAPIC (Tier 2 / Captive model)

An ARM64 nanokernel at guest EL1 (`06 §0.3` arch B / `07 §6.2` Tier 2) can place an **emulated x86 LAPIC+IOAPIC entirely inside the VM** alongside the JIT and shadow-page-table code. Guest APIC accesses never leave the VM and never become `hv_vcpu_run` exits. Captive's §1.5 barrier protocol is the reference: each vCPU polls a shared interrupt-pending flag at translation-block boundaries and at backward branches; inter-vCPU signalling is a shared-memory store + (if the target is in `WFI`) a host-side kick via `hv_vcpus_exit`. Direct block linking is patched through a generation counter when the pending flag is set.

This is the **lowest-exit-count placement** and the only one that avoids the 808 ns floor per interrupt.

### 1.4 The bridge: HVF has GICv3, x86 guests need LAPIC+IOAPIC+MSI

There is no hardware LAPIC. Every INTID/ICR/EOI translation is yours. Comparison:

| Option | Mechanism | Exit cost per LAPIC op | Fidelity / effort |
|---|---|---|---|
| **(a) hv_gic as backend** | Translate x86 LAPIC semantics → GIC SPIs/SGIs/MSI via `hv_gic_set_spi`/`send_msi`; forward `vTimer` INTID 27 through same path. Framework handles priority, preemption, deactivation. | **Host-process call, but in-kernel dispatch** — cheaper than a full MMIO emulated device, but guest APIC MMIO/MSR still traps to host unless the access is inside the VM. | Mismatched models: GIC has distributor+redistributor priority, LAPIC has TPR/PPR/APR/ICR/EOI/IRR/ISR/LVT/ESR/SVR+`TSC_DEADLINE`. Requires a non-trivial translation layer and still pays an exit per guest APIC access if the controller lives in the host. **Not recommended as primary Tier 2 design.** Useful if Tier 1 must accelerate host-side IRQ routing before a nanokernel exists. |
| **(b) Userspace GIC/LAPIC in host** | Emulate LAPIC/IOAPIC as host C code behind stage-2 MMIO faults or port traps; inject via `VIP`/`VFI`. | **~890 ns per APIC register access** (MMIO fault, `02 §6.1`) + ~3.1 µs IPI via `hv_vcpus_exit` (`02 §3.3`). Two APIC accesses per interrupt (ICR write on sender + EOI on receiver) → ~1.8 µs before handler runs. | Simplest to build, worst scaling. Viable for bring-up, not for I/O-heavy guests. |
| **(c) Inside-VM LAPIC** | LAPIC/IOAPIC/MSI logic runs at EL1 inside the VM; `APIC_BASE` MSR and MMIO region are mapped into the nanokernel's address space; x2APIC MSRs are intercepted by the DBT's own MSR dispatcher, not by HVF. | **~0 exits** for APIC ops (shared-memory inside VM). Virtio device queues still exit, but batched (see §3). | Faithful x86 APIC semantics with no translation loss; Captive-proven. **Recommended for Tier 2.** |

### 1.5 x2APIC advantage — decisive for either tier

- **xAPIC** (legacy): APIC at `0xFEE00000` — a 4 KB MMIO window. Every `MOV` to it is a stage-2 data abort (`EC=0x24`, `02 §1.4–1.5`), i.e. a full VM exit inside the microcoded path. Linux will also use `TPR` writes on every `local_irq_disable` / spin-lock — extremely hot.
- **x2APIC**: APIC at MSRs `0x800–0x8FF` (`IA32_X2APIC_*`). CPUID `01H:ECX[21]` advertises it. `RDMSR`/`WRMSR` on these MSRs are **handled inside the DBT's MSR emulation** — no HVF trap (x86 MSRs are not ARM sysregs; they never reach `EC=0x18`). The DBT can even inline fast paths (EOI, self-IPI, TPR) without leaving translated code. `06 §10.1` explicitly recommends x2APIC-only.

**Action:** Always advertise x2APIC; never map the `0xFEE00000` window if x2APIC is negotiated. Keep a minimal xAPIC MMIO fallback only for early boot (before CPUID) and de-prioritize it. On Tier 1 (host-side LAPIC) this converts APIC hot-path exits to near-zero even before moving the controller inside the VM.

### 1.6 Placement recommendation

- **Tier 1 (userspace process, no helper VM):** LAPIC/IOAPIC in the host process as C models behind stage-2 faults/MSR intercepts. **Require x2APIC** — this keeps the per-interrupt exit budget at ~1 (the actual IRQ injection via `hv_vcpu_set_pending_interrupt`) instead of ~3–4 (APIC MMIO + IRQ). Accept the 3.1 µs IPI floor; keep vCPU count small.
- **Tier 2 (nanokernel-in-VM):** move LAPIC+IOAPIC+MSI **inside the VM** (`07 §3.4`). Use Captive's shared-memory barrier protocol for IPIs and EOI. Reserve `hv_gic_create` for **timer INTID 27 routing (see §2) and MSI passthrough** if later wired to a real PCIe/virtio-PCIe transport; do not route LAPIC itself through GIC. If GIC is created at all on Tier 2, it can be the exclusive interrupt distributor for the VM's own EL1 devices (vtimer), while the x86 LAPIC remains a pure software construct.

---

## 2. Timers — CNTVCT, the scaling trap, and the EOI protocol

### 2.1 What traps and what doesn't (`02 §1.5/§3.2`)

| Counter/timer | Host behavior | Guest-visible? |
|---|---|---|
| `CNTVCT_EL0` / `CNTV_CTL_EL0` / `CNTV_CVAL_EL0` / `CNTV_TVAL_EL0` | **Native, no trap** — hardware virtual counter | Always (virtual timer) |
| `CNTPCT_EL0` / `CNTP_CTL_EL0` / `CNTP_CVAL_EL0` / `CNTP_TVAL_EL0` | **Trapped, `EC=0x18`** — you emulate; registers exist only **after `hv_gic_create`** (`02 §1.5/§3.1` measured `HV_UNSUPPORTED` without GIC) | Only with GIC |
| `CNTFRQ_EL0` | Not in `hv_sys_reg_t` (`02 §1.8`) | Set via ID-reg feature view or DT/ACPI; publish the 24 MHz host value and the scaled TSC freq separately |
| PMU `PMCR_EL0` / `PMCCNTR_EL0` | **Trapped** (`EC=0x18`) unless EL2 enabled and `PMUVer` non-zero (`02 §1.5/§4.2`) | Stub or framework-emulated on M3+ with EL2 |

**Rule:** the guest must be steered to `CNTVCT`/`CNTV_*` exclusively. Never let a guest poll `CNTPCT`.

### 2.2 Host counter is CNTVCT @ 24 MHz — implications (`02 §3.2`, `09 N-04`)

Apple's `CNTFRQ_EL0` is **24 MHz** (≈41.67 ns/tick, `09 N-04`). x86 `TSC` is expected at **GHz** (nominal P-frequency, 2–5 GHz), invariant, synchronized, and fine-grained. A 1:1 mapping yields zero deltas on back-to-back `RDTSC`, divides-by-zero, calibration failures, and RCU stalls (`09 N-04`, `06 §11.2`).

### 2.3 TSC synthesis (`06 §11.3`, `09 N-04` mitigation)

Standard recipe (used by KVM/QEMU, adopted in `06 §10–11`):

1. **Scale:** `TSC = (CNTVCT × mul) >> shift` with `mul/2^shift ≈ 100` to synthesize ~2.4 GHz. 64×64→128 `MUL`+`UMULH` (`06 §11.3`). ~40–60 cycles vs 20–30 for real `RDTSC`; `CNTVCT_EL0` itself is ~20–40 cycles.
2. **Advertise:** CPUID `0x15` (TSC/crystal ratio) + `0x16` (nominal freq) and **hypervisor leaf `0x40000010`** (KVM TSC kHz, honored by Linux when hypervisor bit set) so the guest **skips PIT/HPET calibration entirely** (`06 §10.1/§11.3`). This eliminates the calibration device dependency.
3. **Per-vCPU monotonic clamp:** `last_tsc = max(new_tsc, last_tsc+1)` — load/compare/`CSINC`/store (`06 §11.3`). **Per-vCPU, not shared** (shared atomic would serialize the hottest guest instruction).
4. **Pause/resume offset:** maintain a guest-visible TSC offset adjusted on host sleep / VM pause so `CNTVCT` jumps don't leak (`06 §11.4`). In Tier 2, hardware `CNTVOFF_EL2` (`02 §1.8`) is under Apple's EL2 and not writable from the nanokernel on M2 — do the offset in the scaling arithmetic.
5. **`RDTSCP aux`:** `IA32_TSC_AUX` (`0xC000_0103`) → per-vCPU value (VP index), returned in `ECX` for `RDTSCP`.
6. **`TSC_DEADLINE` (MSR `0x6E0`, CPUID `01H:ECX[24]`):** guest programs absolute TSC deadline. Translate via **inverse scale** to `CNTV_CVAL_EL0` (`06 §11.4`). Under Tier 2 this is the preferred LAPIC-timer backend.

### 2.4 kvmclock vs HPET/PIT

| Source | Fidelity | Cost | Recommendation |
|---|---|---|---|
| **kvmclock / Hyper-V reference TSC page** (PV clock, `07 §2.3`) | Correct under a slow emulator; decouples wall-clock from sluggish instruction rate. | One shared page + MSR (`MSR_KVM_SYSTEM_TIME_NEW`); guest driver is **in-box for Linux**, Hyper-V ref TSC for Windows. | **Implement early for correctness, not throughput** (`07 §2.3` "large on correctness"). Expose alongside the scaled TSC, not instead of it. Tier 1 and Tier 2 identical. |
| **HPET** (MMIO at `0xFED00000`) | Linux happy without it if CPUID `0x15`/0x40000010 present | Non-trivial MMIO device; 3 comparators with MSI routing | **Omit** (`06 §10.1`). Don't put it in ACPI. |
| **PIT 8254** (ports `0x40–0x43`) | Linux uses only for TSC calibration fallback | Tiny device (~200 lines) but on a port-trap path | **Omit or stub** — CPUID leaves above make it unnecessary (`06 §10.1`). If retained, a minimal model that enough to pass calibration is sufficient. |
| **RTC/CMOS** (`0x70/0x71`) + ACPI PM timer | Wall clock, `clocksource` fallback | Required for boot; trivial | **Keep** — minimal RTC (`06 §10.1`). |

### 2.5 The vtimer EOI protocol and 02 §3.2 step 4

`hv_vcpu_*` virtual timer protocol (`02 §3.2`):

1. Guest timer fires → `hv_vcpu_run` returns `HV_EXIT_REASON_VTIMER_ACTIVATED`.
2. Framework **auto-masks** vtimer (no repeat exit).
3. Host asserts the vTimer INTID (**27**) via its interrupt controller.
4. Host waits for guest **EOI/deactivation** of INTID 27, then clears mask: `hv_vcpu_set_vtimer_mask(vcpu, false)`.

Step 4 is awkward with `hv_gic_*` (no deactivation callback — must poll `ICC_*`/redistributor state or `EISR`/`ELRSR`). Two workable designs:

- **Tier 1 with framework GIC:** dedicate a host thread to poll `hv_gic_get_icc_reg` / `hv_gic_get_redistributor_reg` for `EOI` of INTID 27 after injection, then unmask. Accept one extra poll exit per timer tick. **Alternatively**, avoid the question entirely: don't use the HVF vtimer for the x86 guest's timer at all — synthesize `TSC_DEADLINE` via the **guest-nanokernel's own `CNTV_CVAL_EL0`** (Tier 2) or via a host `mach_absolute_time` deadline + `hv_vcpus_exit` kick (Tier 1 fallback). The HVF vtimer is an ARM virtual timer; the x86 LAPIC timer is the guest-visible device.
- **Tier 2:** manage LAPIC timer as `CNTV_*` **inside the VM** at EL1. The nanokernel programs `CNTV_CVAL_EL0` directly; on expiry it sets the in-VM LAPIC's pending flag and vectors via the in-VM IDT, with no `hv_vcpu_run` exit. Host-side `hv_vcpu_set_vtimer_*` is then used only for host-level scheduling (e.g., `WFI` wake), not per-guest-tick.

**Recommendation (Tier 2): place all periodic/one-shot timers inside the VM** (LAPIC timer + `TSC_DEADLINE` via `CNTV_CVAL_EL0`; kvmclock as shared page). Reserve HVF's `VTIMER_ACTIVATED`/`vtimer_mask` protocol for the **host-VM scheduling channel** — i.e., waking a `WFI`-blocked vCPU (`07 §3.4` "PV timers worth doing early for correctness").

---

## 3. Device models — placement, fault handling, and the exit budget

### 3.1 Fault handling for stage-2 MMIO

Unmapped IPA → `HV_EXIT_REASON_EXCEPTION`, `EC=0x24` (data abort) or `0x20` (instruction abort, `02 §1.4`). Triple `(ESR_EL2, FAR_EL2, HPFAR-derived IPA)` is delivered; dispatch on **IPA**, not VA (`02 §1.4`). `FAR==IPA` when guest MMU off, different when on.

**ISV=0 decoder requirement (`02 §1.4`):** `LDP`/`STP` and other encodings deliver **no valid syndrome** (`ISV=0`, no `SAS`/`SRT`/`WnR` width). To emulate the access you must **fetch and decode the faulting instruction** — which itself requires walking the guest's stage-1 tables from `PC` via `TTBR0/1_EL1` + `TCR_EL1` (all readable via `hv_vcpu_get_sys_reg`, `02 §1.8`). Every serious HVF consumer implements this; budget a decoder and a `TTBR` walker from day one (`02 §9.5`). `HVC`/`SMC` arrive with **PC already advanced**; data aborts/sysreg traps/`BRK` do not (`02 §1.4`).

### 3.2 Where devices live

| Placement | Mechanism | Cost per device interaction |
|---|---|---|
| **Host process behind `hv_vcpu_run` exits** (Tier 1, or Tier 2 virtio backends) | Guest touches MMIO/port → stage-2 abort → exit → host decodes and dispatches | **~890 ns** per MMIO exit (measured, `02 §6.1`), ~808 ns for `HVC` analog. |
| **Inside the VM (EL1 nanokernel)** — e.g., LAPIC/IOAPIC, virtio-mmio front-half | No exit — shared-memory ring/poll inside VM's address space. Only cross-VM queue kicks cross the boundary. | **~0 ns** (register/shared-memory operation). |

`07 §3.4` is explicit: **"put the interrupt controller model *inside* the unikernel, so LAPIC accesses never exit the VM. Only true I/O (virtio queues) exits, and virtio batches that by design."**

### 3.3 virtio vs legacy, and how virtio batches

- **Baseline: virtio-blk/net/console** (`07 §2.3`) — virtqueues (`avail`/`used` rings in normal guest RAM, not MMIO) with a single MMIO/port **notify** per batch plus an interrupt per completion batch. Guest coalesces dozens of sector transfers or packets into one notify.
- **Legacy: IDE/e1000/16550/AC97** — one MMIO/port trap **per sector / per packet / per byte** (PIO IDE is PIO-by-sector; e1000 Tx descriptors are MMIO-doorbelled per packet).

Quantitative planning at 890 ns/MMIO (`02 §6.1`):

- Single-host-thread aggregate exit ceiling ≈ **3 M exits/sec/machine** (`02 §6.1b`, negative scaling to 8 vCPUs — actually drops). Per-core floor ≈ 1.2 M/sec. These are **shared budgets** across all devices and all vCPUs (`02 §6.1b` design note).
- Budget example at 90% cap on one core (~1 M exits/sec usable):
  - Legacy PIO IDE at 4 KB sectors: 250K IOPS would already saturate one core *before any other exits*. 10K IOPS → 10K exits/sec (fine) but each 4 KB → 40 MB/s — hopeless on modern workloads.
  - Legacy e1000 at 1500 B MTU, line rate 1 Gb/s ≈ 81K pps → 81K exits/sec for Tx alone, before Rx and interrupts — workable but consumes ~8% of budget on MMIO alone.
  - **Virtio-blk with 32-entry batch (default `queue_size` 128):** 32 sectors per notify → 10K IOPS needs ~312 notifies/sec → negligible.
  - **Virtio-net with GSO/GRO and interrupt coalescing:** 64 KB supersegments batched to one notify; effective notify rate ~1–2K/sec at 10 Gb/s equivalent.

**Conclusion:** at 0.9 µs per MMIO, legacy devices are architecturally *capable* of modest I/O but waste an order of magnitude of the shared exit budget. **virtio's batching is what keeps device I/O off the exit hot path.** This reproduces `07 §2.3`'s table: virtio matters "large" when devices are behind exits (Tier 2), modest when they are C function calls in-process (Tier 1 without HVF).

**Additional device-cost lever:** add **shared-memory doorbell polling** (`02 §6.1b` recommendation): guest writes the `available` ring in normal RAM (no trap) and sets a flag; a host thread polls or sleeps on a semaphore/`hv_vcpus_exit` kick. QEMU's `ioeventfd` equivalent on ARM HVF is the undocumented `hv_vm_monitor_data_abort→Mach message` path (`02 §1.10`) — not usable publicly — so emulate it with the polling-ring + `WFI`/`hv_vcpus_exit` wake.

### 3.4 Non-negotiable implementation items for performant devices

1. **Decoder for ISV=0** and stage-1 walker (`TTBR`/`TCR`) — else `LDP`/`STP` MMIO to virtio rings is silently wrong (`02 §1.4`).
2. **Stage-2 layout with few large `hv_vm_map` regions** — `hv_vm_protect` cost is O(mappings), not O(size): 313 ns with ~5 mappings vs **27 µs with 20 000** (`02 §2.6`). Demand-paging fault is ~1.5 µs (`02 §2.7`). Keep guest RAM as a handful of large mappings; fault/batch VMAR updates at ≥2 MB.
3. **x2APIC + MSI/MSI-X preferred** — MSI/MSI-X interrupts are `hv_gic_send_msi` with an address+INTID in the GIC case, vs an IOAPIC pin. Use virtio-PCIe MSI-X (see §4).
4. **Correct MMIO ordering** — x86 UC is strongly ordered; `06 §7.4` recommends leaving MMIO regions unmapped so every access faults and is emulated at exact width/count (no merging two 32-bit stores into `STP`).

---

## 4. Exit-cost audit — reconciling the four numbers

| Source | Measured | What it is |
|---|---|---|
| `HVC` exit → re-enter, empty handler | **808 ns** (`02 §6.1`) | Pure VM-entry/exit + EL2 bounce, no emulation. **The floor.** |
| Stage-2 MMIO data abort (full dispatch: ESR+PC decode, set X0, advance PC) | **890 ns** (`02 §6.1/2.6`) | Floor + syndrome decode + a few `get/set_reg`. Dominated by the floor — `02 §6.1` notes 2×`get_reg`+`set_reg` adds ~0 measurable. |
| `hv_vcpus_exit` cross-thread kick | **~3.1 µs** (`02 §3.3/6.1`) | Sender posts mailbox, calls `hv_vcpus_exit(target)`, target thread wakes from `hv_vcpu_run`. **IPI floor.** (`hv_vcpu_set_pending_interrupt` itself is `HV_BUSY` cross-thread, `02 §3.3`.) |
| `hv_vm_protect` | **313 ns** at ~5 mappings → **27 µs** at 20 000 mappings (`02 §2.6/6.2`) | **Not an exit** — a VMAR table walk + stage-2 TLB invalidate. O(mappings). Independent of MMIO exit latency (900 ns even with 20K mappings, `02 §2.6`). |

Also: `hv_vm_map`+`unmap` pair **587 ns**; demand-paging (fault+map+unmap) **1.5 µs** (`02 §2.6–2.7`); `get/set_reg` **5–12 ns** (`02 §1.3/6.3` — free).

**What this implies for placement (concrete):**

1. **Budget exits globally, not per-vCPU** — multi-vCPU scaling saturates at ~3 M exits/sec/machine and per-exit latency balloons 731 ns (1 vCPU) → 804 ns (2) → 1.26 µs (4) → 2.72 µs (8) (`02 §6.1b`). A 4-vCPU guest cannot each independently "spend" 1 M exits/sec. Design device placement assuming the **machine-wide** budget.
2. **Trap-driven hot paths are impossible.** At 1.2 M exits/sec/core, trapping even once per 1K guest instructions (~3K instructions/µs at ~3.5 GHz) loses ~50% throughput (`02 §6.1` note). Periodic PIT/HPET ticks at 1 KHz are fine; per-instruction LAPIC polls are not.
3. **The 27 µs `hv_vm_protect` cliff dominates any per-page W^X or dirty-bit scheme.** 20K 16 KB pages = 320 MB described. Dirty-tracking a 4 GB guest by per-page protects would be ~250K mappings — extrapolating the O(n) curve is unusable. **Mitigation (`02 §2.6/§9.2`): keep guest RAM in a few large `hv_vm_map` extents; do not use `hv_vm_protect` per 4 KB for SMC or dirty bits; batch with `flags=0` tripwires installed once, or do sub-page checks inside translated code as Captive falls back to for the 16 KB-granule mismatch (`07 §3.1`).**
4. **3.1 µs IPI floor vs in-VM shared-memory IPI.** A guest kernel `smp_call_function_many` that IPIs 7 vCPUs via host `hv_vcpus_exit` pays ~22 µs serialization; via in-VM LAPIC + shared flag it pays shared-memory latency + at most one `hv_vcpus_exit` to break a `WFI` (`02 §3.3`). Host-side IPIs must be reserved for `WFI` wake only.

---

## 5. Minimal Tier 2 device set — Linux boots, Windows path

### 5.1 Phase 1 target: stock x86-64 Linux → shell (no Windows, no firmware)

Goal: minimize exits and engineering breadth; prove shadow paging + x2APIC + virtio is sufficient (`06 §10` difficulty 3 — "platform is where schedule overruns live").

| Subsystem | Phase 1 (minimal Linux) | Notes |
|---|---|---|
| **Boot** | **Direct 64-bit Linux boot protocol** (`bzImage` + `boot_params`) — no firmware | Eliminates real mode, A20, 16-bit protected, CSM (`06 §10.3`). Matches `07 §6.2` Tier 2 scope. |
| **CPU topology** | Advertised via CPUID `0x01`/`0x0B`/`0x1F`; MPIDR_EL1 set per vCPU; topology final on first `hv_vcpu_run` | No hotplug (`02 §3.1`). |
| **LAPIC** | **x2APIC only** (`CPUID.01H:ECX[21]`), MSRs `0x800–0x8FF`; ICR/EOI/TPR/PPR/LVT/`TSC_DEADLINE` via `0x6E0` | Inside VM for Tier 2; host C model for Tier 1. xAPIC MMIO at `0xFEE00000` unmapped/stubbed. |
| **IOAPIC** | Minimal virtio-adjacent IOAPIC at `0xFEC00000` (24 pins), only for 16550/legacy if retained; otherwise route virtio via MSI/MSI-X | Linux tolerates IOAPIC with no HPET/PIC (MADT declares it). |
| **PIC 8259** | Stub (report present, mask all) or ~200-line faithful model for very early boot only | Linux re-parents IRQs to IOAPIC immediately (`06 §10.1`). |
| **PCIe** | **ECAM** via ACPI **MCFG** (memory-mapped config), plus minimal legacy `0xCF8/0xCFC` ports | Firmware-in-guest not present, so ECAM is sufficient after `boot_params` → `MCFG`; keep ports for completeness. |
| **ACPI** | **RSDP → XSDT → FADT + MADT (APIC) + MCFG + minimal DSDT** | Do **not** hand-write AML — adapt QEMU's DSDT (`_SB.PCI0`, `_PRT`) as `06 §10.1` advises; Windows pickier, Linux lenient. |
| **Timers** | Scaled `TSC` (CPUID `0x15`/`0x40000010` + kvmclock ref page), in-VM LAPIC timer via `CNTV_CVAL_EL0`, minimal `RTC/CMOS` (`0x70/0x71`), `STI;HLT` idle via trapped `WFI` (`EC=0x01`, `02 §1.5`) | HPET and PIT omitted (`06 §10.1`). |
| **Storage / net / console** | **virtio-blk** (or virtio-scsi) + **virtio-net** + **virtio-console** (early `printk`) via virtio-PCIe MSI-X; linear framebuffer / virtio-gpu optional for non-headless | Virtio is `07 §2.3`'s only "large when behind exits" device win. Provide at most one legacy UART 16550 at `0x3F8`/`0x2F8` for early debug before virtio-console attaches. |
| **PS/2 / USB HID** | None (headless) | Add virtio-input later. |
| **Explicitly omitted in Phase 1** | SMM (never generate SMI; `qemu -machine smm=off` equivalent — `06 §10.2`), real mode / A20 / SeaBIOS/OVMF, HPET, `PMCCNTR_EL0`, nested EL2, AVX-512/LA57 (`06 §12–13`), hardware task switching, call gates | Saves "not hard, but enormous" breadth (`06 §12 R5`). |

With this set a `virtio-blk`-root Linux kernel configured with `CONFIG_VIRTIO_PCI`, `CONFIG_VIRTIO_BLK`, `CONFIG_VIRTIO_NET`, `CONFIG_VIRTIO_CONSOLE`, `CONFIG_X2APIC`, and `CONFIG_PV_CLOCK` (kvmclock) boots inside the design's protected region; the host process backs virtio rings behind ~hundreds of notifies/sec rather than per-sector exits.

### 5.2 Incremental path to stock Windows (deferred, no Phase-1 preclusion)

Windows validation is stricter and doubles device breadth (`07 §2.1` target (d)). Each is a separable, post-Linux-boot increment:

1. **UEFI firmware as guest code** — run OVMF (UEFI x64) as ordinary translated guest code. Requires the Phase-1-omitted subsystems: real mode, unreal mode, 32-bit protected mode, and A20 for firmware initialization; then 64-bit. No host porting — firmware *is* the early guest payload.
2. **ACPI/AML hardening** — Windows parses DSDT `_PRT`, `_HID`/`_CID`, `_STA`, and power methods strictly; retain QEMU-derived tables, add `HPET` table if H-HPET kept, plus `SRAT`/`SLIT` only if NUMA is advertised.
3. **AHCI + e1000 fallbacks** — in-box Windows drivers for `storahci` and Intel e1000/82574 before virtio drivers are installed; alternatively ship the `virtio-win` drivers on a second virtio-blk volume (the Hyper-V enlightenment path `07 §2.3` notes Windows ships `hv-*` clients in-box, and the project can follow `hv-tlbflush`/`hv-ipi` later).
4. **Graphics** — Bochs VGA linear framebuffer or virtio-gpu; Windows display driver load happens after kernel boot.
5. **TPM 2.0** for Windows 11 — CRB/FIFO virtio-TPM or TPM-IF via MMIO (`06 §10.1`); firmware-dependent.
6. **HPET** — only if Windows calibration demands it; can stay omitted if CPUID leaves `0x15/0x16/0x40000010` satisfy `hal.dll` on the tested build.
7. **USB HID** (PS/2 or `xHCI` via `virtio-input` / `qemu-xhci` model) for interactive Windows.

The CPU translator itself gains nothing new for Windows beyond devices/firmware (`07 §2.1` "SMM→device/firmware, not CPU"), so the Windows path should be tracked as a device checklist, not a translator risk.

### 5.3 What Phase 1 explicitly does *not* need to solve (and why)

- **SMM** — `06 §10.2`: without SMI generation or chipset emulation there's no SMM entry; omit `SMBASE`/`SMRAM`/SMI rendezvous; variable store is non-secure but functional (`qemu.smm=off`).
- **Real mode via direct kernel boot** — `06 §10.3`: `boot_params` entry skips everything before long mode.
- **HPET** and full PIT emulation — `06 §10.1–11.3`: CPUID leaves replace calibration.
- **AVX-512, 5-level paging, PMU precision** — `06 §12–13`: cap at `x86-64-v2`, mask CPUID, omit.

---

## 6. Open questions — remaining [UNVERIFIED] Hypervisor.framework device behaviors

Ordered by blocking power.

| # | Question | Why it matters | Current status | How to close |
|---|---|---|---|---|
| **Q1** | **Monitored-memory / `hv_data_abort_notification_t` → Mach message** (`02 §1.10`) — is there a trap/MIG path to `hv_vm_monitor_data_abort` on this OS, and does it truly avoid the `hv_vcpu_run` exit? | An in-kernel exit-free MMIO path would halve device-exit cost and give a native `ioeventfd` for virtio doorbells. `02 §1.10` notes zero exported symbols — only `hv_kern_types.h` struct `hv_vm_mem_access_msg_t` and a raw Mach trap. | **[UNVERIFIED]** — types exist, no API, TBD has no symbol, Virtualization.framework presumably uses it. Treat as **not usable** (`02 §1.10`). | Reverse the `Virtualization.framework` device backend or SPI headers on macOS 15/26; attempt the trap via `mach_msg` `vm_monitor_data_abort` name; measure MMIO latency with/without it holding all else equal. |
| **Q2** | **Multiple address spaces (private `__hv_vm_space_*` SPI, `02 §1.7`)** — can the process own two IPA spaces with different `ipa_base`/`ipa_granule`/`ipa_size` (e.g., guest RAM in one space, MMIO/ROM in another, code-cache alias in a third)? | Would isolate the `hv_vm_protect` O(n) scaling (`02 §2.6` cliff) per space and enable per-space granule (e.g., 4 KB for guest RAM on macOS 26 vs 16 KB elsewhere). `02 §1.7` lists `__hv_vm_space_create/_destroy`, `__hv_vm_map_space/_protect_space`, `__hv_vm_space_config_set_ipa_base/_granule/_size`, plus `__hv_vm_config_set_isa`. | **[UNVERIFIED]**, private, ABI unstable. `02 §1.7` Medium confidence. | Attempt SPI resolution via `dlsym`/`Hypervisor.tbd` exports on macOS 26; if present, test mapping the same host page into two spaces. Do **not** ship on it; keep as opportunistic optimization. |
| **Q3** | **Private SPI `__hv_vm_config_set_isa`** (`02 §1.7`) — does it select an x86 guest ISA path (Rosetta VMAR plumbing) and change any trap behavior? | If Rosetta's `__hv_vm_config_set_isa` changes the kernel's `HCR_EL2` trap stencil (e.g., fewer/more `EC=0x18` sysregs or different fault handling), it would be directly useful — but is strictly unsupported. | **[UNVERIFIED]** | Search Rosetta's private framework linking (`otool -L` on `TranslationKit`) for `Hypervisor` imports; corridor-try setting ISA value (e.g., `1`) on a fresh `hv_vm_config` in a test VM — observe whether `hv_gic_create` / `hv_vcpu_create` behavior changes. |
| **Q4** | **GIC SGI/SPI delivery latency with framework GIC vs `hv_vcpu_set_pending_interrupt`+`hv_vcpus_exit`** | `02 §3.3` measured IPI at **3.1 µs** via `hv_vcpus_exit`; SGI via `ICC_SGI1R_EL1` *should* be exit-free but was **not measured** (`02 §3.3` "Unverified"). Determines whether host-originated timer/IPI should ever route through `hv_gic_*`. | **[UNVERIFIED]** — exercise suggested in `02 §6.4`: measure `hv_gic_set_spi(INTID 27) → guest IRQ handler entry` vs cross-thread `set_pending_interrupt`+`hv_vcpus_exit` path. | Microbenchmark: guest spins reading a flag; host calls each path 100K times timestamped with `mach_absolute_time`. |
| **Q5** | **VTIMER EOI observability with framework GIC** (`02 §3.2` step 4) — is there any notification or query for INTID 27 deactivation, or must the host poll `GICD_ISPENDR`/`GICR_ISPENDR`/`EISR`/`ELRSR`/`ICC_IAR`? | Determines whether the spec's `vtimer_mask`/`vtimer_offset` protocol (`02 §3.2`) is usable without a poll loop per tick. If not, Tier 1 should not use the HVF vtimer for the guest timer. | **[UNVERIFIED]** for `02 §3.2`; mechanism exists only as register reads. | After injecting INTID 27 with `hv_gic_set_spi`, poll `hv_gic_get_icc_reg(ICC_IAR1_EL1)`/`EOIR` shadow via `hv_gic_get_gic_*` series and `hv_gic_get_msi_reg`; confirm which `ELRSR`/`EISR` bits reflect it without EL2. |
| **Q6** | **Private SPI `__hv_vcpu_config_set_fgt_enabled` / `__hv_vcpu_set_control_field` (FEAT_FGT / `HCR_EL2` controls)** | Would grant per-sysreg trap control (CNTPCT, PMU, `ACTLR_EL1` for TSO); `02 §1.7/§6` flags this as the escape hatch for "which sysregs trap". | **[UNVERIFIED]** private — symbol exists (`02 §1.7`), semantics inferred. | Not shippable; note as Virtualization.framework tech preview only. Close only if the project opts into reverse-engineered SPI. |
| **Q7** | **`WFE` non-trapping vs `HCR_EL2.TWI/TWE` fixity** | `02 §1.5` measured `WFI` traps (`EC=0x01`), `WFE` does not — because `HCR_EL2.TWI=1, TWE=0` is baked. Caps idle fidelity: guest `WFE`-spinning on a lock never yields; guest idle that `WFI`s does. | **Verified** (`02 §1.5` high) that `WFE` is not trapped. Open question is whether private SPI can toggle `TWE`. | Only relevant with Q6. |

No work in this dossier is gated on any Q1–Q7; the Tier 2 design is described above to be viable without them, with measurable upside if any later prove usable.

---

## 7. Recommendations (per tier)

**Tier 1 (userspace, host-side devices):**

1. Implement **x2APIC** immediately; MMIO `0xFEE00000` only as a stub. This single switch makes APIC hot paths exit-free even before any VM move.
2. Keep device set lean — **virtio-blk/net/console + minimal PCI/ACPI/RTC** — and enforce **virtio queue sizes 128+ with batching**; reject legacy-IDE queue depths 1: they burn 8× the exit budget.
3. Use **kvmclock** + scaled `TSC` (`CPUID 0x15/0x40000010`); **omit HPET/PIT**; leave `WFI` trapping as the idle hint (`hv_vcpu_run` blocks). Budget `hv_vcpus_exit` IPIs at 3.1 µs — keep vCPU-count low.

**Tier 2 (helper-VM, Captive-shaped) — the target:**

1. **Move LAPIC/IOAPIC/MSI + LAPIC timer inside the VM**; only virtio ring notifies cross the `hv_vcpu_run` boundary. Adopt Captive's block-boundary interrupt poll/barrier for IPI+EOI; `hv_vcpus_exit` only to break `WFI`.
2. Scale `TSC` at the VM edge (per-vCPU clamp); advertise `0x15/0x40000010`; map `TSC_DEADLINE` → `CNTV_CVAL_EL0` inside the VM; add kvmclock ref page for wall-clock/JDI correctness.
3. Stage-2 hygiene: **few large `hv_vm_map` extents**; no per-page `hv_vm_protect`; a decoder+`TTBR` walker for ISV=0 MMIO; `ipa_size=40` max and, if on macOS 26, explicitly select `HV_IPA_GRANULE_4KB` for guest-RAM spaces.
4. Reserve `hv_gic_create` for **INTID 27 + MSI plumbing** if desired; do not force x86 LAPIC interrupts through it. If framework GIC is not created, timer IPIs use the in-VM `CNTVCT` path above.

---

## References

- `02-hypervisor-framework.md` §1.4 (ESR/FAR/HPFAR + ISV=0/LDP-STP), §1.5 (trap table: HVC/SMC/WFI/PMU/CNTV vs CNTP), §1.7 (private `__hv_*` SPI), §1.8 (sysreg surface), §1.10 (Mach-message monitored memory), §2.6 (O(n) `hv_vm_protect` cliff), §2.7 (demand paging), §3.1–3.5 (GIC/timer/IPI/PSCI/SMMU), §6.1–6.4 (measured latency tables + scaling).
- `06-x86-system-semantics.md` §2 (shadow page cost), §7.3–7.4 (atomics/TSO/fences: `06:454–492`), §8 (SMC: `06:498–548`), §10 (devices/CPUID/MSRs/APIC/PCI/ACPI: `06:618–648`), §11 (TSC/CNTVCT scaling: `06:651–692`).
- `07-paravirtualization-design-space.md` §2.3 (virtio vs legacy quantified), §3.1–3.7 (PV grade: page-quads A, PV-EOI/IPI/SC C, PV clock/interrupts B), §6 (Tier 1 vs Tier 2 effort/perf).
- `09-architectural-nuances.md` N-04 (24 MHz `CNTVCT` mismatch), N-06 (TSO≠atomicity/`CMPXCHG16B`).
- HVF SDK: `hv_gic*.h`, `hv_vcpu.h:369`/`hv_vm.h:42`, `hv_kern_types.h:78-116`, `Hypervisor.tbd:37-52`.

*Generated 2026-08-08. Measurements to refresh on M3/M4 hardware (SGI latency, 4 KB IPA granule, EL2 availability).*

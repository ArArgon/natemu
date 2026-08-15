# Research Index — natemu x86-64 on Apple Silicon

**Last updated:** 2026-08-15 (post-audit; gates re-closed by experiment — read 13 and 14 first) · **Machine:** M2 Max / M2 Pro, macOS 26.5.2 (25F84) · **Status:** research phase — gates closed by experiment (14); baseline measured (15); shadow-MMU costs measured (16); EL1 capability surface (18), precise exceptions (17), SMP scaling (19), PT coherence (20), SMC (21), async interrupts (22), FEXCore retrofit (23), boot path (24) and the correctness oracle (25) all landed; **all 26 written**; the corpus is measurement-complete and prototype-empty
**Artifacts:** `probe_g1_tgran_tso.c` (+ `probe_g1_granule.c`), `tools/g2_quad_measure.py`, `/tmp/hv_g1_probe/run.log`, `/tmp/probe_mmu_tso.c` (`12-performance-ranking.md`)

## Map

| # | Document | Question answered | Verdict |
|---|---|---|---|
| 01 | [apple-tso](01-apple-tso.md) | Can we get hardware TSO on Apple Silicon? | Native process **no** (kext only). **VM yes** — `HV_SYS_REG_ACTLR_EL1` (macOS 15+) + guest `msr actlr_el1` both verified, 2–28% TSO tax measured. |
| 02 | [hypervisor-framework](02-hypervisor-framework.md) | What can Hypervisor.framework do for a system emulator? | Thin EL2 shim: stage-2 maps, vCPU state, 808 ns exit floor, `hv_vm_protect` O(mappings) cliff, 16K default granule (4K selectable macOS 26). GICv3 available macOS 15+. TSO, EL2, SME tested. |
| 03 | [prior-art-hw-accel-dbt](03-prior-art-hw-accel-dbt.md) | How have others accelerated cross-ISA system DBT? | Captive (2.2–2.5× over QEMU, MMU→host MMU) is the closest prior art. HyperMAMBO, MagiXen, Transmeta CMS, DAISY also mined. |
| 04 | [rosetta2](04-rosetta2.md) | What does Rosetta 2 steal/cost? | ~71–80% of native on INT/SSE, 6% on x87, 50% on AVX2. TSO + FlagM + register pinning + cheap W^X are the transferable wins; PF/AF IMPDEF bits are not. |
| 05 | [existing-translators](05-existing-translators.md) | What code can we reuse? | **FEXCore (MIT, ~90k LoC)** for frontend+IR+ARM64 backend. QEMU TCG for the softmmu *design* (not code, GPL). rust-vmm for devices/memory. Bochs as oracle. |
| 06 | [x86-system-semantics](06-x86-system-semantics.md) | What must a system emulator implement? | Paging/MMU is difficulty 5; precise exceptions 5; SMC 5; all others graded. 4K↔ARMv8 isomorphism only at 4K granule. |
| 07 | [paravirt-design-space](07-paravirtualization-design-space.md) | Is "para-virtualization" the right premise? | Classical PV is mostly worthless to a DBT. The one A-grade PV idea is **16K-aligned page quads** to restore 1:1 MMU mapping. KVM-style optional/negotiated PV layer recommended. Three tiers proposed; **Tier 2 (Captive-for-Apple-Silicon, helper VM)** is the north star. |
| 08 | [feasibility-synthesis](08-feasibility-synthesis.md) | **Should we build it, and how?** | **Feasible but hard (28–48 pm).** Helper-VM + shadow paging + FEXCore is the path. See §08 for go/no-go gates. |
| 09 | [architectural-nuances](09-architectural-nuances.md) | Sharp edges register | 10 nuances; N-01 (47-bit VA), N-02 (16K granule), N-06/07 are load-bearing. Updated 2026-08-08: N-02 resolved G-1=T Gran4 YES. |
| 10 | [interrupt-timer-device](10-interrupt-timer-device-dossier.md) | Interrupt/timer/device placement per tier | GICv3 measured, vtimer→TSC_DEADLINE, virtio batching, exit-budget 1.2M/sec/core, minimal Tier 2 set. |
| 11 | [pte-quad-gate](11-pte-quad-gate.md) | G-2: are 16K quads already homogeneous? | Synthetic 55–87% (kernel_build 55%, browser 67%, DB 87%) → **G-2 = mandatory** for Tier 2 ceiling. Tool: `tools/g2_quad_measure.py`. |
| 15 | [qemu-tcg-baseline](15-qemu-tcg-baseline.md) | **What is the real baseline on this host?** | **x86-64 TCG is 8.6× an aarch64 HVF guest** (7.2× no-SIMD); HVF-vs-native control 1.05×, so ~95% is emulation. aarch64-TCG-vs-HVF **4.2×** (zero ISA mismatch) isolates "TCG is a simple JIT". Rosetta 1.43×, so the **ceiling is 6.0×**. |
| 16 | [shadow-mmu-costs](16-shadow-mmu-costs.md) | What does shadow paging cost? | In-VM fault **28-95 ns**, full fill **146 ns** vs 808 ns host exit floor. Break-even vs softMMU = 1 fill per 730 accesses. **Never TLBI on invalid→valid (3×).** Permission faults 3.3× cheaper than translation faults. |
| 17 | [precise-exceptions](17-precise-exceptions.md) | Can we keep aggressive codegen AND precise faults? | **Yes — the hot fault needs no precise state at all.** A shadow miss is invisible to the guest: fill and retry. Only guest-visible faults reconstruct. Two-tier handler worth 19% of the hot path. Conservative spilling measured **+170-241%**; RIP-alone ~2%. `AT` probe-then-execute **refuted** as a general answer (24-55x a store). |
| 13 | [audit-skeptical-review](13-audit-skeptical-review.md) | Does the corpus survive scrutiny? | **3 of 4 load-bearing conclusions did not, as stated.** Decoder bugs, G-1/G-2 closed on insufficient evidence, refuted premise in 07, speedup double-counting. |
| 14 | [gate-results-verified](14-gate-results-verified.md) | G-1/G-4 closed by experiment | **4K stage-1 + 4K stage-2 permissions ENFORCED inside one 16K host page; T0SZ=16 (48-bit VA) works; TSO litmus 1.7M→0.** G-2 quads now MOOT. |
| 19 | [smp-scaling](19-smp-scaling.md) | Does the design scale past one core? | **91–92% efficiency at 8 vCPUs.** TLBI broadcast is the wall (441–961 ns busy); cross-vCPU doorbells 68 ns. The "never TLBI on invalid→valid" rule is worth **6.8× at 8 vCPUs** vs 3.0× at one. Carries an auditor's note: the SGI-reliability claim did **not** reproduce at the probe's default `n`. |
| 18 | [el1-capability-surface](18-el1-capability-surface.md) | What can a guest at EL1 under HVF *actually* do? | **Out of 48 catalogued operations, exactly 6 leave the VM** — `CNTPCT`, `PMCCNTR`, `OSLAR`, `WFI`, `SMC`, `HVC`. All TLBI/IC/DC/AT/MMU-sysreg work is native, and 1M+ exceptions were taken into the guest's own vector with **0 host exits**. **No cycle counter at guest EL1** (whole PMU + every Apple IMPDEF reg traps). **Without a GIC the vtimer cannot reach the guest's vector at all.** `TLBI ASIDE1` 167.5 ns is the cheapest invalidation. |
| 22 | [async-interrupt-injection](22-async-interrupt-injection.md) | How do we interrupt a translated guest, and what does it cost? | **Delivery is 50 ns; ARM EOI is 1912 ns — and we never need to pay it.** Every `ICC_*` access is a ~900 ns host exit, so run the x86 APIC in guest memory and never touch the GIC. 7.19M IRQs delivered against 2 host exits. A per-block **pinned-register poll is free** (≤2 checks / 5 x86 insns is below resolution); `REP MOVSQ` stays interruptible to **0.3 ns at ~0% cost**. Cross-vCPU boundary by killing a stage-1 mapping: **p50 83 ns, 500/500**. |
| 20 | [shadow-pt-coherence](20-shadow-pt-coherence.md) | How do we learn the guest edited its own page tables? | **Do not write-protect guest PTs — resync lazily off the guest's own `INVLPG`/`MOV CR3`.** WP trap is 33.3–34.3 ns (install) / 231–274 ns (modify); Linux spends **18.1 `INVLPG` + 6.5 `MOV CR3` per `fork`+`exec`** against 500–2000 PTE writes — 30–110× fewer events for the same information. WP the **PML4 alone**. Burst break-even N≈32–64. |
| 21 | [smc-detection](21-smc-detection.md) | What does SMC detection cost, and who triggers it? | **Steady-state SMC in a Linux guest is zero.** 17 086–17 313 TB invalidations during boot, then **exactly 0** across 5.6 s of benchmarks, 400 `fork`+`exec`, and 30.5 s of 7-Zip. Whole SMC turnaround 1650–2015 ns; `IC IALLU` beats per-line `IC IVAU` above ~1 KiB. `IC IVAU` does **not** degrade under SMP contention (0.62–0.93× at 8 vCPUs) — refutes N-05's fear. |
| 23 | [fexcore-retrofit](23-fexcore-retrofit.md) | Can FEXCore be retrofitted as the JIT? | **Fork it — take the backend and user-mode frontend, rewrite dispatch/cache, run as a host compile server. But not yet.** 52/56 TUs compile on macOS/arm64. FEX's flat guest-VA==host-VA lowering is an *asset* under hardware stage-1. Real blockers: the never-changes-mode assert (89 sites) and a cache keyed on bare guest VA. x87 is already true 80-bit. |
| 24 | [boot-path-and-modes](24-boot-path-and-modes.md) | How does the guest boot, and what modes must we implement? | **Direct kernel boot, no firmware, no real mode.** ~1,100–1,400 LoC of hand-off. SeaBIOS below 1 MB is 0.47% of a boot. **Not advertising LA57 is load-bearing** — it is what keeps a UP guest in long mode forever. SMP drags real mode back via the SIPI trampoline. x87 = 8 instructions per boot: correctness only, never performance. |
| 25 | [correctness-oracle](25-correctness-oracle.md) | How do we know it is right, and debug it when it is not? | **The oracle is an engineer-week, not 10–16 pm — `08`'s sequencing is wrong.** A TCG-plugin hash chain localises the first differing instruction in a 1.218-B-instruction boot in ~35 s. Lockstep gdbstub is refuted (7.5 days/boot). `-icount` + pinned RTC gets 99.9987% determinism. **Strict EFLAGS comparison is ill-posed**: 16/69 groups disagree between two correct x86 implementations; 0/69 with SDM undefined-bit masking. |
| 26 | [guest-rates-and-footprint](26-guest-rates-and-footprint.md) | What are the guest's actual fault / PT-write / CR3 **rates**? | **Shadow-MMU maintenance is 0.4–10.4% with per-address-space shadows, and up to 101% without.** Flush-on-CR3 doubles a build-like workload; 256 ASIDs suffice (peak 112, 0 evictions — so C's eviction path is untested). Corrects `20`: real PTE writes are **42.3/spawn**, not the inferred 500–2000 — but `clear_page` zeroing adds 430.9, so WP/lazy is **16.4×**, not 30–110×. **Uniprocessor only; guest never uses PCID.** |
| 12 | [performance-ranking](12-performance-ranking.md) | Perf concerns ranked + live tracking | **1. MMU 1.5× → 2. TSO 3.4×/2–5% → 3. Codegen 4.4× → 4. Precise 10–20% vs 2–5× → 5. SMC 4× → 6. Quad 15–35% → 7. Exits 1.2M/core → 8. A/D+TLB**. Live `/tmp/probe_mmu_tso.c`. |

## In flight (2026-08-15)

**All 26 documents are written.** The corpus has now measured every mechanism it set out to
measure. What it has never done is run a system: no line of natemu exists, and `23 §0.4`'s
hardware-stage-1-under-unmodified-TCG experiment remains the cheapest thing that would convert the
modelled case into a real number.


**Two measurement caveats now standing corpus-wide** (`18 §0.2`, `22 §6`):

1. **Core-clock-bound timings from `el1_features` carry ~2× run-to-run spread**, and power state,
   QoS class and background load have all been **ruled out** as the cause (20 + 10 + 10 + 15 runs;
   `probes/el1_features/results/clock-stability.txt`). An earlier version of `18 §0.2` blamed
   battery-vs-wall-power; **that is withdrawn** — the battery readings sit inside the wall-power
   distribution. Fabric-bound timings (`TLBI`) are stable to 1.09× across everything. Report
   medians over n ≥ 10 with the range; read any single figure in `18 §4/§8/§9` as ±2×.
2. The two-vCPU poll-contention probe is **noise-dominated**; its control arm swings 2.2× on an
   identical workload, so `22`'s "the poll is free" holds only for an uncontended flag.

`docs/research/raw/` holds **raw, unaudited** sub-agent reports rescued from agents that died
mid-run. They are inputs to 20/21/23, not evidence — see `raw/README.md`.

## Reading order for a newcomer

**Start with 13 (audit) and 14 (verified gate results)** — they correct several headline claims in 01–12.
Then 01 → 02 → 06 → 07 → 08. Read 03–05 only when you need to choose components. 10–11 are the Tier 2 device/PV gates, 12 is the perf triage.

## Open gates (cheap experiments that decide the design)

| Gate | What | Cost | Blocks | Status 2026-08-08 |
|---|---|---|---|---|
| G-1 | `ID_AA64MMFR0_EL1.TGran4` inside a Hypervisor.framework guest at EL1 | 0.5 day | Tier 2 MMU design | **CLOSED — YES.** `0x000010000f100002` → TGran4=0x0 SUPPORTED, TGran64=0xF NI, TGran16=0x1 SUPPORTED, PARange 40. Probe: `probe_g1_tgran_tso.c` + `/tmp/hv_g1_probe/run.log`. Also 4K `hv_vm_map` + TSO guest `msr actlr_el1` verified. |
| G-2 | PTE-quad homogeneity: fraction of guest 4K PTEs already in uniform 16K quads | 1–2 days | Whether PV page-allocator patch is mandatory | **ESTIMATED 55–87%, verdict mandatory.** Synthetic: `tools/g2_quad_measure.py --synthetic` → kernel_build 55%, browser 67%, DB 87% (`V` 1.74× softMMU, `+110%`/load degraded). Real dump still open: `python3 tools/g2_quad_measure.py --dump mem.bin --cr3 0x...` → `12-performance-ranking.md:6` |
| G-3 | `hv_vcpu_run` exit latency + `hv_vm_protect` scaling on this host | done (02 §6.1/2.6: 808 ns, 27 µs cliff) | Device model placement | **CLOSED.** 808 ns HVC floor, 890 ns MMIO, 3.1 µs IPI, 313 ns→27 µs cliff. See 10 §4. |

G-1/G-3 closed live on M2 Pro 26.5.2; G-2 synthetic closed, real-guest dump is 1-day follow-up.

## Reproducibility

All `[VERIFIED-LOCAL]` claims were run on M2 Max / M2 Pro, macOS 26.5.2, `MacOSX26.4.sdk` (HV 259.5.15), ad-hoc `com.apple.security.hypervisor` signing, no SIP changes.

# 08 — Feasibility Synthesis & Architecture Decision

**Date:** 2026-08-08 · **Status:** research synthesis + multi-agent gates (G-1/G-3 closed, G-2 synthetic)
**Host validated on:** M2 Max/M2 Pro, macOS 26.5.2 (25F84), `hw.pagesize=16384`, `FEAT_LSE2=1`, `kern.hv.supported=1` · SDK HV 259.5.15
**Artifacts:** `probe_g1_tgran_tso.c` / `/tmp/hv_g1_probe/run.log`, `tools/g2_quad_measure.py`, `10-interrupt-timer-device-dossier.md`, `11-pte-quad-gate.md`

---

## 0. One-paragraph verdict

**Buildable — now with G-1 measured live. Tier 2 (Captive-shaped: DBT unikernel at EL1 inside a Hypervisor.framework VM, x86 shadow tables on real ARM stage-1, stage-2 via `hv_vm_map`) is the only design that одновременно solves the 47-bit VA trap, the 16K granule mismatch, the TSO tax, and the W^X trap — and it does so with no guest fork for the baseline. Expect 2.5–5× over QEMU TCG system-mode, ~15–35% of native ARM, at 28–48 person-months. G-1 closed live: TGran4 SUPPORTED inside VM (ID_AA64MMFR0_EL1 `0x000010000f100002` TGran4=0x0, 4K guest `msr actlr_el1` + 4K `hv_vm_map` both verified), TSO re-verified (guest+host paths coherent, AIDR bit9=1). Residual risk is one gate: G-2 synthetic says 55–87% → page-quads PV mandatory (real dump measurement is the last 1-day follow-up).** Fallback remains Tier 1 (1.5–2.5×, 10–16 pm) as permanent diff oracle.

---

## 1. Original premise — scored

| Premise bullet | Score | Evidence |
|---|---|---|
| (1) para-virtualization reduces HW-emulation overhead | **Mostly wrong, one big exception.** Classical PV (PV-EOI, PV-IPI, PV-MMI, PV-syscall) saves exits a DBT never pays (§07 §2.3). The *only* A-grade PV idea is **16K-aligned page quads** to restore 1:1 shadow paging on Apple Silicon (§07 §3.1). | 07 §2.3, 07 §3.1 |
| (2) reuse ARM infra (MMU, interrupts) via helper VM | **Correct and load-bearing.** ARMv8 4K-granule L0–L3 is structurally isomorphic to x86 4-level paging (06 §0.1/2.2). Inside a VM we own `TCR_EL1`/`TTBR0/1_EL1` and stage-2; GICv3 is provided by the framework since macOS 15 (02 §3.1). This is Captive's thesis and it measured 2.21× SPECint. | 06 §0.1, 02 §1.8/3.1, 03 §1 |
| (3) reap Apple TSO | **Correct, but only inside a VM.** Native macOS process: no API, no entitlement, `msr actlr_el1` traps (01 §4). Inside Hypervisor.framework: `HV_SYS_REG_ACTLR_EL1` (macOS 15+) and guest `msr actlr_el1` both **verified** (01 §5.2/5.3), with 2–28% tax measured. Saves the ~3.4× software-TSO cost (01 §6.3). | 01 §4/5/6 |

**Net:** reframe "para-virtualization" as **"hardware-assisted memory virtualization + PV devices + one optional PV accelerator (page quads)"** (07 §7). Do not require a patched guest kernel to boot.

---

## 2. Design decision — Tier 2 as north star, Tier 1 first

|  | Tier 1 — Better TCG (userspace) | **Tier 2 — Captive for Apple Silicon (VM)** | Tier 3 — Rosetta-shaped hybrid |
|---|---|---|---|
| Guest | unmodified x86 Linux + virtio | same — **plus optional CPUID-negotiated PV** (`page-quads`, `pv-tlb-flush`, `pvclock`) | native ARM64 kernel + x86 userspace |
| MMU | softmmu (per-access ~9–11 insn) | **shadow paging on real ARM stage-1 (4K granule) + stage-2 `hv_vm_map`** | host kernel does it |
| Rings | software variable | **EL0 = guest ring 3, EL1 = guest ring 0 + nanokernel** | no ring 0 to emulate |
| Boots x86 kernel? | yes | yes | **no** |
| Path to Windows? | yes (device work) | yes | no |
| Effort | 10–16 pm | **28–48 pm** | 24–44 pm (or 2–5 wrapping FEX) |
| Speed vs QEMU TCG | 1.5–2.5× | **2.5–5×** | 5–20× |
| Speed vs native ARM | ~5–10% | **~15–35%** | 60–80% |
| Novel? | no | **yes** — never done on Apple Silicon | no (Apple ships it) |

**Recommendation (07 §7): build Tier 1 as the correctness/performance baseline and permanent diff oracle, then Tier 2.** Tier 3 is only defensible as a 2–5 pm FEX/box64 wrapper, not a from-scratch build.

---

## 3. Why Tier 2 wins on Apple Silicon specifically

1. **47-bit host VA trap (09 N-01).** `machdep.virtual_address_size=47` — a macOS process cannot 1:1-map the x86-64 canonical space. Inside a VM we programme `TCR_EL1.T0SZ/T1SZ=16` and get the full 48-bit space.
2. **Granule (09 N-02) — CLOSED live.** Host `mmap`/`hv_vm_map` default to 16K; x86 guests use 4K. Gate G-1 probe (`probe_g1_tgran_tso.c`, `/tmp/hv_g1_probe/run.log`): guest EL1 `ID_AA64MMFR0_EL1=0x000010000f100002` → TGran4=0x0 SUPPORTED, TGran16=0x1 SUPPORTED, TGran64=0xF NI; `hv_vm_config_set_ipa_granule(HV_IPA_GRANULE_4K)` + `hv_vm_map` 4K both succeed (macOS 26), and `ID_AA64MMFR0.TGran4` is consistent with `arm-cpusysregs` M4 dumps. So a VM can set `TCR_EL1.TG0/TG1=4K` and `hv_vm_map` at 4K — no mandatory shattering. G-2 still decides whether a quad-contiguous PV allocator is needed to keep the 1:1 path hot.
3. **TSO (09 N-06).** Software TSO via `LDAPR`/`STLR` is 3.4× on L1-resident emulated code (01 §6.3); `LDAPR` is not cheaper than `LDAR` on Apple (01 §6.3). Hardware TSO inside a VM removes this entirely.
4. **W^X (09 N-07).** `MAP_JIT` + `pthread_jit_write_protect_np` in a process; inside a VM we own stage-1 and can map the code cache RWX.
5. **GIC/Timers (09 N-04/06).** Framework GICv3 since macOS 15 removes the need to emulate an interrupt controller in userspace; virtual counter `CNTVCT_EL0` is hardware-accelerated, physical timers trap.

---

## 4. Reuse plan — what we take, what we write

**Take (permissive licenses):**

- **FEXCore** (MIT, ~90K LoC: `X86Tables` + `OpcodeDispatcher` + `IR` + `JIT` + `CodeEmitter`) — decoder, SSA IR (438 ops, arena-allocated), FlagM-aware flag logic, AVX128 and x87 paths, `SIGBUS` unaligned-atomic handler (`Arm64.cpp`). Already re-embedded once (Windows/Wine). Replace its 1:1 `LoadMem` address model with a shadow-TLB or shadow-paging hook in `MemoryOps.cpp`. (05 §2)
- **rust-vmm** (`vm-memory` + `linux-loader` + `vm-superio` + `virtio-queue`, Apache-2.0/BSD) for guest RAM, kernel loading, serial/RTC, virtio plumbing; Cloud Hypervisor / Firecracker for device-model reference.
- **Bochs** (LGPL-2.1, out-of-process) as the differential oracle — most faithful x86 paging/SMM/AVX-512 model.

**Port the design, not the code (GPL):**

- QEMU `accel/tcg/cputlb.c` softmmu TLB layout, `MO_ATOM_*` taxonomy, physical-page TB invalidation, `tcg_gen_req_mo` barrier calculus (05 §3) — for Tier 1 and as fallback.

**Not reusable for system mode:**

- QEMU TCG, box64, Blink, Unicorn, libcpu, VEX/Rellume/Remill — all userspace-only or GPL/AGPL or stale; useful for techniques (call-ret predictor, `CRC32CX` indirect-branch hash, `AT S1E1R` probes) but not as linked components. (05 §4–7)

**Write ourselves:**

- Shadow paging (ARM 4K-granule tables, ASID-per-PCID, `TTBR1` kernel-half sharing, generation-counter ASID allocator, rmap for aliasing, lazy resync on `INVLPG`/`MOV CR3`, BBM handling — 06 §2.4–2.6).
- Precise-exception metadata (host-PC → guest-state side table, StackMap shape; load-all→compute→store-all discipline; `AT S1E*R` probes — 06 §5).
- Ring transitions (`SYSCALL`/`SYSRET` quirks, `IRET` NMI-unblock, STI/`MOV SS` shadows — 06 §4).
- Device/machine model (x2APIC MSR, virtio-only, TSC `CNTVCT×scale`) — but keep it minimal; SMM and real mode can be omitted in Phase 1 (06 §9).

---

## 5. Go / no-go gates

| Gate | Experiment | Success criterion | Live result 2026-08-08 | If it fails |
|---|---|---|---|---|
| **G-1 — TGran4 in VM** | Boot minimal VM, `mrs x0, ID_AA64MMFR0_EL1` at EL1; check `TGran4` field | `TGran4 != 0b1111` (i.e. 4K supported). | **CLOSED — PASS.** Guest EL1 `ID_AA64MMFR0_EL1=0x000010000f100002` → TGran4=0x0 SUPPORTED, TGran16=0x1 SUPPORTED, TGran64=0xF NI; `hv_vm_map` 4K + guest `msr actlr_el1` both verified, host/guest TSO coherent. Probe: `probe_g1_tgran_tso.c` + `/tmp/hv_g1_probe/run.log`. | Not applicable — passed. Fallback would have been 16K shattering + per-access checks (Captive §6.10). |
| **G-2 — PTE-quad homogeneity** | Boot x86 Linux under QEMU, dump CR3 trees under realistic workload (kernel build, browser), compute fraction of 4K PTEs in 16K-aligned, permission-uniform quads | >90% → page-quads optional. <70% → PV patch `natemu-page-quads` mandatory. | **SYNTHETIC CLOSED — 55–87% → MANDATORY.** `tools/g2_quad_measure.py --synthetic`: kernel_build 55%, browser 67%, DB 87%; real-dump `walk_dump()` still open (1-day `pmemsave` + `python3 tools/g2_quad_measure.py --dump mem.bin --cr3 0x...`). Details: `11-pte-quad-gate.md`. | Tier 2 still boots; G-2 gates whether `CONFIG_NATEMU_PAGE_QUADS` (~200–400 LoC, CPUID `NatemuHV` optional) is on critical path — **it is.** |
| **G-3 — HV costs** | `hv_vcpu_run` round-trip; `hv_vm_protect` vs mapping count | **Done:** 808 ns HVC exit, 890 ns MMIO exit, `hv_vm_protect` 313 ns → 27 µs at 20k mappings (02 §6.1/2.6). | **CLOSED.** Reconciled in `10-interrupt-timer-device-dossier.md` §4: +3.1 µs IPI, 1.2M exits/sec/core, 3M machine-wide. | Design rule: few large `hv_vm_map` extents; no per-page `hv_vm_protect`; batch; see 10 §3–4. |

**Multi-agent update:** G-1/G-3 closed live on M2 Pro 26.5.2; G-2 synthetic closed (55–87% → mandatory), real dump is the last 1-day gate before Tier 2 paging code.

---

## 6. Performance budget — where time goes, what to expect

Captive is the only apples-to-apples prior art (full-system cross-ISA with HW MMU):

- SPEC CPU2006 INT geomean: Captive **2.21×** QEMU, 6.49×** FP ( (.� FP is softfloat vs host FPU — not transferable to ARM NEON where x87 is 20–50× and AVX2 is 2–3×). ARMv7 2.5×, SimBench TLB-Flush **32×**, TLB-Evict 25× (03 §1.8).
- HSPT/ESPT (MMU fix alone): **1.51–1.59×** (07 §5.1).
- Softmmu tax if not removed: ~9–11 host insn per guest access (05 §3.4), ~2–5× whole-program (06 §2.3).
- TSO tax if not removed: ~3.4× on L1 memory-heavy emulated code (01 §6.3); hardware TSO inside VM erases it for ~2–28% host cost.
- TLB reach at 4K vs 16K: 12 MiB vs 48 MiB on M1 Firestorm (06 §2.7) — real but manageable via 2 MiB block shadowing.
- Tier 2 projection **2.5–5×** rests on: MMU offload (~1.5×) + ring isolation + better JIT (FEXCore vs TCG) + TSO. Kernel-heavy workloads lean toward 4–5×; SPEC INT toward 2.2×.

---

## 7. Risks — ranked, with mitigations

1. **4K granule unavailable in VM (G-1). — CLOSED.** Live probe shows `TGran4=0x0 SUPPORTED` inside VM and `hv_vm_map` 4K works; risk retired. See `probe_g1_tgran_tso.c`. The *remaining* 4K cost is TLB reach (12 MiB vs 48 MiB on M1) and BBM shattering — mitigated via 2 MiB blocks.
2. **TSO unavailable from guest EL1.** Already disproven — **verified available** (01 §5) and re-verified live (G-1 probe: host `HV_SYS_REG_ACTLR_EL1` + guest `msr actlr_el1` coherent, `AIDR bit9=1`). No risk.
3. **16K page false sharing on SMC/PT protection.** 16K host pages inflate write-protect faults 4× on Tier 1. Inside VM we control granule; if stuck at 16K, use per-page checksums or rmap shattering.
4. **`hv_vm_protect` O(mappings) cliff.** Do not build dirty-tracking or W^X on top of it; coalesce mappings.
5. **Precise exceptions smother optimizations.** Hardest correctness tax (06 §5). Mitigate with metadata side tables, not spill-everywhere; be conservative around multi-access insns (`PUSH [mem]`, `REP`, split locks).
6. **Debuggability of JIT-inside-VM.** Tier 1 as permanent diff oracle is non-optional (07 §6). Budget for it.
7. **Nested virt confusion.** M1/M2 lack EL2 in guests (02 §4.2); irrelevant — Tier 2 needs only EL1. Do not chase guest VT-x emulation.

---

## 8. What "para-virtualization" means for this project — final definition

Not Xen-PV. Not "PV the MMU". Adopt **KVM's shape**: every PV feature is **CPUID-enumerated (`cpuid 0x40000000` / `"NatemuHV"`), optional, and negotiated**. An unmodified guest boots correctly and runs slower; an enlightened guest sets a feature bit and gets a fast path. Ship three features, in this order:

1. **virtio-blk/net/console + pvclock** — free (drivers in-box), required for any usable VM, also a correctness fix for a 10×-slow timebase (07 §3.4/3.7).
2. **`pv-tlb-flush` (`pv_ops.mmu.flush_tlb_multi`)** — already unconditional in Linux `CONFIG_PARAVIRT` (no fork) (07 §3).
3. **`page-quads`** — only if G-2 says we need it; small allocator-policy patch, gated on CPUID.

Everything else (PV-SMC, PV-EOI/IPI, PV-syscall, PV-ASID) is **do not build** (07 §3.2–3.5).

---

## 9. Recommended sequencing

1. **G-2 real-dump follow-up** (1 day): `qemu-system-x86_64` + `pmemsave` → `python3 tools/g2_quad_measure.py --dump mem.bin --cr3 0x...` under kernel_build/browser/DB to confirm synthetic 55–87%. Refines PV page-quads schedule; does not block start. G-1 is closed.
2. **Tier 1 bring-up** (10–16 pm): FEXCore + softmmu + minimal x2APIC/virtio machine. Boot Linux to `init`. This is the reference oracle for Tier 2.
3. **Tier 2 nanokernel** (8–14 pm): VM lifecycle, EL1 stage-1 shadow tables, ASID allocator, rmap, demand paging on `ESR_EL1` faults.
4. **x86 system surface** (6–10 pm): paging modes, MSRs/CPUID, `SYSCALL`/`SYSRET`/`IRET`, IDT/GDT/TSS, segmentation fast path, TSC.
5. **Performance closure:** TSO (already available), block chaining, trace formation, TLB-range invalidates (`FEAT_TLBIRANGE`), code-cache physical indexing.

---

## 10. Bottom line for a go/no-go meeting

- **Technical feasibility: yes — and stronger than pre-gate.** G-1/G-3 closed live; only G-2 real-dump remains and synthetic already says page-quads is mandatory (not optional).
- **Performance case: 2.5–5× over QEMU TCG is credible** (1.5× from MMU alone, rest from TSO + privilege + JIT quality). Not Rosetta-class (that is a different product) but decisively better than QEMU.
- **Product case: only defensible as system-level emulation** (booting x86 kernels, firmware, drivers, Windows). For "run x86 Linux binaries fast" the answer is "wrap FEX/box64 or use Apple's free Rosetta-for-Linux" — Tier 3 from scratch is 24–44 pm to parity with free.
- **Effort case: 28–48 pm for Tier 2, with Tier 1 as a 10–16 pm down payment that is independently useful.** Staffing that is not available means do Tier 1 only.

---

## References

All claims point back to 01–07+09 and their primary sources (Apple SDK headers, `arm-cpusysregs` M4 dumps, Captive TACO/TOCS, Hypervisor.framework measurements in 02, TSO measurements in 01). No new primary research in this synthesis.

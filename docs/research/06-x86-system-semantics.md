# 06 — System-Level x86-64 Semantics on ARMv8-A / Apple Silicon

**Status:** research reference · **Scope:** *system-level* (ring 0 + hardware) x86-64 emulation, as distinct from user-mode (Rosetta/FEX/box64) emulation · **Host target:** Apple Silicon (M1–M4), macOS

> Citation convention. Intel SDM = *Intel® 64 and IA-32 Architectures Software Developer's Manual*, cited as `SDM Vol.3A §4.5` etc. Section numbers for Vol.3A Ch. 8–11 shifted by +1 in revisions after ~2021 (Multiple-Processor Management moved 8→9, APIC 10→11, Cache Control 11→12); both are given where it matters. ARM ARM = *Arm Architecture Reference Manual for A-profile*, DDI 0487 (Issue K.a used here); the AArch64 VMSA chapter is `D8` in recent issues and was `D5` before Issue G.

---

## 0. Executive framing

User-mode emulation gets to ignore ~80% of what follows. A system emulator cannot. The difference in kind — not degree — is that a system emulator must reproduce the **x86 memory-management unit**, the **exception model**, and the **privilege model** with enough fidelity that a hostile-to-emulators artifact (the Linux or Windows kernel) cannot tell the difference. Those three, plus self-modifying-code detection, dominate the engineering cost and the performance ceiling.

### 0.1 The single most important architectural fact

**x86-64 4-level paging and ARMv8-A 4KB-granule stage-1 translation are structurally isomorphic.**

| | x86-64 4-level | ARMv8-A, 4KB granule |
|---|---|---|
| VA bits | 48 (canonical, sign-extended from bit 47) | 48 (`T0SZ`/`T1SZ` = 16) |
| Levels | 4 (PML4, PDPT, PD, PT) | 4 (L0, L1, L2, L3) |
| Index bits/level | 9 | 9 |
| Descriptor size | 8 bytes | 8 bytes |
| Table size | 4096 B / 512 entries | 4096 B / 512 entries |
| Large mappings | 1 GiB (PDPTE.PS), 2 MiB (PDE.PS) | 1 GiB L1 block, 2 MiB L2 block |
| Address-space split | canonical hole at bit 47 | `TTBR0_EL1` (low) / `TTBR1_EL1` (high) |

This is not a coincidence — both descend from the same 9-bit-index/8-byte-descriptor design point. The practical consequence is enormous: **shadow page-table construction is a per-descriptor bit permutation with no tree restructuring.** Guest PML4 index *i* maps to shadow L0 index *i*. Guest 2 MiB pages map to ARM 2 MiB L2 blocks 1:1. Guest 1 GiB pages map to ARM 1 GiB L1 blocks 1:1. The x86 canonical-address split lands exactly on the `TTBR0`/`TTBR1` boundary.

**At the 16KB granule this isomorphism is destroyed.** With a 16KB granule the level-3 page is 16 KiB, blocks are permitted only at level 2 (32 MiB), and level-1 blocks are not permitted at all (ARM ARM §D8.3). A 4 KiB x86 page has **no representation whatsoever**; a 2 MiB x86 page would need 128 separate 16 KiB descriptors and would still have wrong permission granularity at its edges.

Therefore: **4KB granule support on Apple Silicon is a hard, binary go/no-go gate for this project.** It was the first thing verified.

### 0.2 Verified Apple Silicon facts

Sourced from register dumps in [`lelegard/arm-cpusysregs`](https://github.com/lelegard/arm-cpusysregs/blob/main/docs/apple-m1-features.md) (decoded `ID_AA64*_EL1` on real hardware), cross-checked against Asahi Linux and QEMU.

| Fact | Value | Consequence |
|---|---|---|
| `FEAT_TGran4K` (stage 1) | **yes** (M4 dump) | ✅ **Project is viable.** 4KB-granule shadow tables possible. |
| `FEAT_S2TGran4K` (stage 2) | **yes** (M4 dump) | ✅ 4 KiB-granular stage-2 protection under Hypervisor.framework. |
| `FEAT_TGran16K` / `S2TGran16K` | yes | Host/native path. |
| `FEAT_TGran64K` / `S2TGran64K` | **no** | Irrelevant to us. |
| `FEAT_HAFDBS` | **no** — M1, M3, **and M4** | ❗ **No hardware A/D bits.** x86 A/D emulation must be fault-driven. See §1.6. |
| `FEAT_LPA` / `LPA2` / `LVA` / `D128` | **no** | ❗ **48-bit VA ceiling.** x86 5-level paging (LA57, 57-bit VA) cannot be shadowed. Do not advertise LA57. |
| `FEAT_ASID16` | yes | 65 536 ASIDs ≥ 4 096 x86 PCIDs. ✅ clean PCID mapping. |
| `FEAT_BBM` | **no** | ❗ Strict break-before-make required when changing block size / output address. Hurts 4 KiB↔2 MiB promotion/demotion. |
| `FEAT_TLBIRANGE`, `FEAT_TLBIOS`, `FEAT_TTL` | yes | ✅ Range invalidation for `INVLPG` bursts and `INVPCID`. |
| `FEAT_PAN` / `PAN2` / `PAN3` | yes (PAN3: M3, M4) | ✅ Direct SMAP analogue. |
| `FEAT_XNX`, `FEAT_HPDS`, `FEAT_HPDS2`, `FEAT_E0PD` | yes | ✅ NX/SMEP analogues, hierarchical permission control. |
| `FEAT_LSE` (incl. `CASP`) | yes | ✅ `CMPXCHG16B` → `CASPAL`. |
| `FEAT_LSE2` | yes | ✅ Single-copy atomicity for ordinary accesses not crossing a 16-byte boundary. |
| `FEAT_LSE128` | **no** | Minor; `CASP` covers the need. |
| `FEAT_LRCPC` / `LRCPC2` | yes | ✅ Cheap `LDAPR` acquire loads (RCpc, weaker/faster than `LDAR`). |
| `FEAT_LRCPC3` | no | Minor. |
| `FEAT_FlagM` / `FlagM2` | **yes, all of M1–M4** | ✅✅ `CFINV`, `RMIF`, `SETF8/16`, `AXFLAG`, `XAFLAG` — added to ARM *specifically* for x86 emulation. Huge. |
| `FEAT_SVE` / `SVE2` | **no** — including M4 | ❗ **128-bit NEON is the hard SIMD ceiling.** |
| `FEAT_SME` / `SME2` | yes (M4 only) | ⚠️ Useless here: `FEAT_SME_FA64` = **no**, so streaming mode disables most of A64. Not an SVE substitute. |
| `FEAT_AFP` | **no on M1/M2**, yes on M3/M4 | ⚠️ M1/M2 cannot separate DAZ from FZ in hardware (`FPCR.FIZ`/`AH` absent). |
| `FEAT_PMUv3` | **no** — M1, M3, **M4** | ❗ No architectural cycle counter. `PMCCNTR_EL0` unusable → **`CNTVCT_EL0` @ 24 MHz is the only clock.** See §10. |
| `FEAT_ECV` | M1 detailed dump: no; M3/M4: yes | Counter scaling/offset control on M3+. |
| `FEAT_SB`, `FEAT_DIT`, `FEAT_SPECRES` (M4) | yes | ✅ `LFENCE` speculation-barrier semantics mappable. |
| `FEAT_NV` / `NV2` | yes on M3/M4 | Nested virt available if ever needed. |
| `FEAT_XS`, `FEAT_ETS`/`ETS2`, `FEAT_WFxT`, `FEAT_nTLBPA` | yes on M4 only | Nice-to-have; do not depend on them (M1 baseline). |
| Apple **TSO mode** | `ACTLR_EL1` bit 1 (`EnTSO`), EL1-writable | ✅ x86-TSO in hardware; ~**8.94%** avg slowdown (Wrenger et al., ARCS'23 / JSA'24). ❗ No public userspace API. |
| M1 Firestorm TLB | L1 dTLB 256 entries, L2 TLB 3072 entries | 4 KiB pages → **12 MiB** L2 TLB reach vs **48 MiB** at 16 KiB. **4× reach loss.** |
| macOS host page size | **16 KiB** | ❗ `mprotect`/`mach_vm_protect` granularity is 16 KiB → 4× false sharing for any userspace-JIT SMC/PT write-protection scheme. |
| macOS W^X | `MAP_JIT` + `pthread_jit_write_protect_np` (**per-thread**) | Manageable; ~1.4% faster than `mprotect` churn. |
| Hypervisor.framework on Apple Silicon | **ARM guests only** (`hv_vmx_*` is Intel-only) | ❗ No hardware x86 virtualization. All x86 execution is JIT, always. |
| QEMU HVF `ipa-granule` prop | `auto`/`4k`/`16k`, added 2026 | ✅ Independent confirmation that Apple stage-2 does 4 KiB. |

### 0.3 Two candidate architectures

Everything below is evaluated against both.

**(A) Userspace JIT** — the QEMU-TCG / Rosetta shape. Whole guest runs in one macOS process at EL0. Guest MMU emulated in software (`softmmu` + software TLB), or approximated with `mach_vm_remap` games. Ships as a normal signed app.

**(B) Nanokernel-in-VM (Captive shape)** — create an **ARM64** VM via Hypervisor.framework; inside it, at EL1, run a minimal nanokernel that owns `TCR_EL1` and sets `TG0`/`TG1` = **4 KiB granule**. Translated guest code runs at EL0 (guest ring 3) and EL1 (guest ring 0). **The ARM MMU walks shadow page tables in hardware.** Stage-2 (`hv_vm_map`, 4 KiB granule) backs guest RAM and traps MMIO.

This is precisely Spink, Wagstaff & Franke's *Captive* design (USENIX ATC'19, "A Retargetable System-Level DBT Hypervisor"), which reported **2.21× over QEMU** on SPEC CPU2006 in full-system Linux by "mapping guest MMU events and operations onto host memory virtualization extensions, eliminating the need for costly software MMU emulation."

**(B) is strongly recommended.** It is the only design that turns the §0.1 isomorphism into actual hardware page walks, and it is the only way to get ring0/ring3 onto real EL1/EL0. Its costs are: the `com.apple.security.hypervisor` entitlement (no App Store, needs a Developer ID), a much harder debugging story, and JIT-inside-VM plumbing.

---

## 1. Difficulty / impact / mitigation summary

Difficulty 1–5 (5 = multi-engineer-year or research-grade). Perf impact = expected contribution to total slowdown if done naively.

| # | Item | Diff | Perf impact | Primary mitigation |
|---|---|:--:|---|---|
| 1 | **Paging / MMU** | **5** | **Dominant.** softmmu ≈ 2–5× overall; shadow-paging misses ≈ 500–2000 cyc each | Design (B): ARM 4KB-granule shadow tables, 1:1 level mapping; 16-bit ASID per guest PCID; `TTBR1` for the kernel half; rmap for aliasing |
| 2 | Segmentation | 2 | ~0 in 64-bit; 5–15% in 32-bit compat | Pin FS/GS base in ARM regs; specialize on descriptor cache; implement LDT/GDT as data only, no call gates / no HW task switch |
| 3 | Privilege & control transfer | 4 | Moderate; syscall path is hot | ring3→EL0, ring0→EL1 under (B); hand-written trampolines for `SYSCALL`/`SYSRET`/`IRET`; emulate `SYSRET` non-canonical `#GP` and `IRET` NMI-unblock exactly |
| 4 | **Precise exceptions in a JIT** | **5** | 10–30% if done with metadata; 2–5× if done by spilling | Host-PC→guest-state side tables (LLVM StackMap shape); "load-all → compute → store-all" instruction discipline; probe-before-commit for multi-access ops |
| 5 | Flags (EFLAGS) | 3 | 2–5× naive → 10–20% with lazy flags | Lazy flags + `FEAT_FlagM/FlagM2` (`CFINV`, `RMIF`, `SETF8/16`, `AXFLAG`/`XAFLAG`); shift-left trick for 8/16-bit widths; PF/AF computed on demand only |
| 6 | Atomics & memory model | 4 | 20–40% with `LDAR`/`STLR`; **~9% with Apple TSO** | Apple TSO mode if reachable from guest EL1 (**must be tested**); `CASP` for `CMPXCHG16B`; vCPU rendezvous for split locks |
| 7 | **SMC / code-cache invalidation** | **5** | Catastrophic if wrong (fault storms); 5–20% if right | Write-protect at guest-physical granularity + rmap; escalate to per-page checksum or interpret-mode after N faults; explicit handling of `text_poke_bp` INT3 protocol |
| 8 | SIMD / FP | 4 | SSE2 ~1.3×; AVX2 ~2–3×; x87 **20–50×**; AVX-512 impractical | Cap CPUID at x86-64-v2 (or v3); soft-float80 for x87; hand-code `MIN/MAXPS`, `CVTTPS2DQ`, `PCMPxSTRx`; sticky FP flags only |
| 9 | Devices / platform | 3 | ~0 with virtio; large *engineering* cost | x2APIC (MSR, no MMIO trap); virtio-only; **omit SMM entirely**; skip real mode via direct 64-bit kernel boot in Phase 1 |
| 10 | TSC | 3 | Hot path (guest vDSO) | `CNTVCT_EL0` × scale (24 MHz → ~2.4 GHz), per-vCPU monotonic clamp, advertise freq via CPUID leaf `0x40000010` |

---

## 2. Paging and the MMU

### 2.1 x86 semantics, precisely

**Modes** (SDM Vol.3A §4.1.1): 32-bit paging (`CR0.PG=1`, `CR4.PAE=0`), PAE paging (`CR4.PAE=1`, 3-level, 4 PDPTEs loaded *into the CPU* on `MOV CR3` — a genuinely weird special case), 4-level (`IA32_EFER.LME=1`), and 5-level/LA57 (`CR4.LA57=1`, SDM §4.5.4). A system emulator that boots a stock OS must implement at least: no-paging (real/unreal mode), 32-bit, PAE (for the 32-bit boot path and 32-bit guests), and 4-level.

**A/D bits** (SDM Vol.3A §4.8). The processor sets `A` in every paging-structure entry used in a successful translation, and `D` in the final entry on a write. Key rules that emulators get wrong:
- Updates are **atomic read-modify-writes** on the paging structure in memory, and are ordered as locked operations.
- `A` **may be set speculatively** — for a translation the processor caches but never actually uses. So being *over-eager* on `A` is architecturally legal.
- `D` is set **only on an actual write**. Being over-eager on `D` is *not* architecturally sanctioned; the practical damage is a spurious writeback of a clean page (performance, not correctness) — but a paranoid guest could notice.
- If the paging structure is read-only in a nested/EPT layer, the A/D update itself faults. In our design the shadow tables are ours, so this reduces to "the update must be done by our fault handler."

**Reserved-bit faults** (SDM §4.7, error-code bit 3 `RSVD`). Which bits are reserved depends on `MAXPHYADDR` (from `CPUID.80000008H:EAX[7:0]`), on `IA32_EFER.NXE` (bit 63 reserved iff NXE=0), and on `CR4.PSE`/`PS`. Guests — and, more importantly, guest *self-tests* and exploit mitigations — probe this. Our software walker must reproduce it exactly.

**Page-fault error code** (SDM Vol.3A §4.7): bit 0 `P`, 1 `W/R`, 2 `U/S`, 3 `RSVD`, 4 `I/D`, 5 `PK`, 6 `SS` (shadow stack), 15 `SGX`. `CR2` receives the **faulting linear address**, not the address of the faulting paging structure.

**TLB coherence** (SDM Vol.3A §4.10). The contract is: software must invalidate after changing a paging-structure entry, **except** that a not-present→present transition needs no invalidation, because the processor is forbidden from caching a translation for a non-present page. **This exception is what makes lazy shadow-table filling legal** — it is the load-bearing rule for the entire design. The processor may also cache intermediate *paging-structure* entries (PDE caches, §4.10.3), so the guest must invalidate on any level change; we get to inherit that obligation.

**`INVLPG`** invalidates entries for one linear address in the current PCID plus all global entries. **`INVPCID`** (SDM Vol.2) types: 0 = individual address, 1 = single context, 2 = all contexts *including* globals, 3 = all contexts *excluding* globals. **`MOV CR3`** flushes non-global entries for the current PCID (and if `CR4.PCIDE=1` and bit 63 of the new value is set, does *not* flush).

**`CR0.WP`** (SDM §4.6.1). When `WP=0`, supervisor-mode writes to read-only pages **succeed**. There is no ARM equivalent — ARM has no "privilege overrides write permission" mode.

**SMEP** (`CR4.SMEP`): supervisor instruction fetch from a user page → `#PF`. **SMAP** (`CR4.SMAP`): supervisor data access to a user page → `#PF`, *unless* `EFLAGS.AC=1` (set/cleared by `STAC`/`CLAC`). Modern Linux toggles `AC` on essentially every `copy_to_user`, so this is a hot path, not an exotic corner.

**Self-referencing page tables.** Windows maps its PML4 into itself at a (since Win10 1607, randomized) index, so every page table is reachable at a computed virtual address. Linux uses a fixmap and `__va()` of the direct map. Either way, **guest page-table pages are ordinarily mapped writable in the guest's own address space, often at multiple virtual addresses.** This is the aliasing problem that forces a reverse map (§2.4).

### 2.2 ARMv8-A comparison

| x86 concept | ARMv8-A equivalent | Fidelity |
|---|---|---|
| 4-level paging, 4 KiB | 4KB granule L0–L3 | **Exact** (§0.1) |
| 2 MiB / 1 GiB pages | L2 / L1 blocks | **Exact** |
| Canonical split @ bit 47 | `TTBR0_EL1` / `TTBR1_EL1`, `T0SZ=T1SZ=16` | **Exact** |
| 5-level / LA57 (57-bit VA) | needs `FEAT_LVA`+`LPA2` — **absent on Apple** | ❌ **Not representable.** Don't advertise. |
| NX bit (`EFER.NXE`) | `UXN` / `PXN` (+`FEAT_XNX` for stage 2) | Exact |
| SMEP | `PXN` on user pages | Exact |
| SMAP + `EFLAGS.AC` | `PSTATE.PAN` (inverted sense) + `FEAT_PAN2` | Exact; `STAC`→`MSR PAN,#0`, `CLAC`→`MSR PAN,#1` (1 instr each) |
| `CR0.WP=0` | **none** | ❌ Requires shadow rebuild (§2.6) |
| PCID (12-bit, 4096) | ASID (16-bit, 65536 — `FEAT_ASID16`) | Superset ✅ |
| Global pages (`PTE.G`, `CR4.PGE`) | `nG` bit clear / `TTBR1` half | Exact |
| `INVLPG` | `TLBI VAE1IS` | Exact |
| `INVPCID` type 0/1/2/3 | `TLBI VAE1IS` / `ASIDE1IS` / `VMALLE1IS` / `VMALLE1IS`+global handling | Close |
| `INVLPG` over a range | `TLBI RVAE1IS` (`FEAT_TLBIRANGE` ✅) | Better than x86 |
| Hardware A/D | `FEAT_HAFDBS` — **absent on Apple** | ❌ Fault-driven (§2.5) |
| PAT (8 entries) | `MAIR_EL1` (8 entries × 8 bits), `AttrIndx[2:0]` | **8-vs-8 structural match** ✅ |
| MTRRs (range-based) | none | Fold into `AttrIndx` at shadow-build time ✅ |

**Memory-type mapping** (SDM Vol.3A §11.5.2/§12.5.2 gives the MTRR∩PAT effective-type table; ARM ARM §D8.5 gives `MAIR` encodings):

| x86 type | ARM attribute | Loss |
|---|---|---|
| WB | Normal, Inner/Outer WB, R/W-allocate | none |
| WT | Normal, Inner/Outer WT | none |
| UC | Device-nGnRnE | none |
| UC− | Device-nGnRE | none |
| **WC** | Normal Non-cacheable (closest) | ⚠️ **Real loss.** ARM Normal-NC permits merging/reordering but has no true write-combining buffer semantics. Affects guest GPU/framebuffer drivers. |
| WP (write-protect) | **none** | Approximate as WT. Rare. |

The clean trick here: x86's effective memory type is `f(MTRR, PAT, CR0.CD/NW)` — all of which we know at shadow-PTE construction time. **Precompute the effective type and encode it directly into `AttrIndx`.** MTRRs then cost nothing at runtime.

### 2.3 The three implementation strategies

**(a) Direct use of x86 page tables — impossible.** The formats are irreconcilable at the bit level even though the tree shape matches: x86 puts NX at bit 63, ARM puts `UXN`/`PXN` at 54/53; x86 `R/W` is bit 1 (1 = writable), ARM `AP[2]` is bit 7 (1 = read-only); x86 `U/S` is bit 2, ARM `AP[1]` is bit 6; x86 `A`/`D` are bits 5/6, ARM `AF` is bit 10; ARM requires `AttrIndx[2:0]` at 2:4 and `SH[1:0]` at 8:9, which x86 uses for `P`/`R/W`/`U/S` and `A`. There is no `TCR` configuration that makes ARM hardware consume an x86 PTE. Shadowing is mandatory.

**(b) Software TLB (softmmu).** QEMU's approach: a direct-mapped hash table keyed on the guest virtual page, consulted inline on every guest memory access. Fast path is ~5–10 host instructions (mask, index, load tag, compare, branch, add offset, access). With ~35–45% of x86 instructions touching memory, this is roughly a **2–5× whole-program tax** before anything else. Slow path (TLB miss) runs a full software x86 page walk: 4 dependent loads, permission checks, reserved-bit checks, A/D updates — 100–300 cycles. This is the honest baseline and the fallback if design (B) proves unreachable.

**(c) Shadow page tables on real ARM hardware — recommended.** Build ARM 4KB-granule stage-1 tables that mirror the guest's x86 tables, install them in `TTBR0_EL1`/`TTBR1_EL1`, and let the ARM MMU do the walking. Guest memory accesses become **plain `LDR`/`STR` with zero inline overhead** — the entire softmmu tax disappears. This is the whole reason design (B) exists.

### 2.4 Shadow page-table maintenance — the real cost model

The classic "virtual TLB" model (pre-EPT VMware/KVM), adapted:

**Fill (lazy, on fault).** Legal because x86 forbids caching non-present translations (§2.1). On an ARM translation fault:
1. Take the abort at EL1 (`ESR_EL1.EC` = 0x20/0x21 instruction abort, 0x24/0x25 data abort; `FAR_EL1` = faulting VA; `ESR_EL1.WnR` bit 6 = write).
2. **Software-walk the guest x86 page tables** (4 dependent loads + reserved-bit + permission checks). This is unavoidable — it's how we distinguish "shadow miss" from "genuine guest `#PF`".
3. If the guest translation is valid and permits the access → populate the shadow L0–L3 chain, set A (and D if write), retry.
4. Else → synthesize an x86 `#PF`: compute the error code from `ESR_EL1` (W/R ← `WnR`, I/D ← EC class, U/S ← faulting EL, P/RSVD ← our walk), set `CR2` ← `FAR_EL1`, vector to guest IDT entry 14.

**Cost per shadow miss:** ARM abort entry/exit (~100–200 cyc) + software walk (~100–200 cyc, more on cache misses) + shadow population (~50–100 cyc) ≈ **500–2000 cycles**. Hardware x86 with EPT does a nested walk in tens of cycles. This gap is the fundamental tax of design (B), and the entire optimization strategy is *miss avoidance*.

**Miss-avoidance levers (all essential):**
- **ASID per guest CR3/PCID.** With `FEAT_ASID16` (65 536 ASIDs) we never need to flush on a context switch — just swap `TTBR0_EL1` with a new ASID. Guest `MOV CR3` becomes ~1 system-register write plus a hash lookup, instead of a full shadow rebuild. Recycle ASIDs with a generation counter (Linux's `arm64` ASID allocator is the reference design).
- **Kernel half in `TTBR1`.** The guest's high canonical half (≥ `0xFFFF_8000_0000_0000`) is nearly always process-invariant. Shadow it once into `TTBR1_EL1` with `nG=0` and never touch it on CR3 switches. This gives x86 `PTE.G`/`CR4.PGE` semantics for free and makes CR3 switches genuinely cheap. **Caveat:** the guest's high-half PML4 entries live in the *same* PML4 page as the low half, so a write there must invalidate the `TTBR1` shadow too — a special case in the write-protect handler.
- **Shadow root cache.** Keep N recent (CR3 → shadow root) pairs, LRU. Avoids rebuild churn on ping-ponging context switches.
- **Level sharing.** Because the trees are isomorphic, sub-trees for shared regions (the kernel direct map, shared libraries) can be shared between shadow roots — one shadow L1/L2 table referenced from many shadow L0s. Big memory and fill-time win. Requires refcounting.

**Guest page-table writes.** Two options:
- *Write-protect the guest PT pages* (KVM-style). Every guest PTE write faults. Guest OSes write PTEs constantly (`fork`, COW, `mmap`, demand paging) — this is expensive, and it interacts badly with 2 MiB-mapped PT arenas.
- *Leave them writable and resync lazily* (virtual-TLB style). Rely on the guest's architectural obligation to `INVLPG`/`MOV CR3`. On `INVLPG`, drop the shadow PTE + `TLBI VAE1IS`. On `MOV CR3`, either switch ASID (if the root is cached and clean) or rebuild. **This is the right default**: it exploits exactly the same contract real hardware relies on, and it eliminates the PT-write fault storm.
  - The one thing lazy resync cannot handle is a present→present *permission upgrade* without invalidation — which the architecture forbids the guest from doing, so it is safe. Present→present *downgrade* without invalidation is also forbidden.
  - Hybrid: write-protect only the *top* level (PML4), which changes rarely, to catch address-space-wide restructuring cheaply.

**Aliasing and the reverse map.** A guest-physical frame may be mapped at many guest-virtual addresses, in many address spaces. When we must revoke write access to a frame (for SMC detection, §7, or for D-bit tracking, or for a write-protected PT page), we must clear the write bit in **every shadow PTE that maps it**. That requires a `GPA → {shadow PTE*}` reverse map. KVM calls this `rmap` and it is a significant structure: typically a per-frame head pointer with an overflow chain, costing ~8–16 bytes/frame plus chain nodes. Maintaining it on every shadow fill/teardown is real time. **Budget for it explicitly; it is the single most-underestimated piece of shadow paging.**

**Break-before-make.** `FEAT_BBM` = **no** on Apple. ARM ARM §D8.16 requires: to change a valid descriptor's block size, output address, or memory type, software must (1) write an invalid descriptor, (2) `DSB`, (3) `TLBI`, (4) `DSB`, (5) write the new descriptor, (6) `DSB`, `ISB`. That's a full round trip per change. This hits whenever a guest promotes 4 KiB pages to a 2 MiB huge page (transparent huge pages do this constantly) or demotes on `mprotect`. **Mitigation:** prefer to shadow guest 2 MiB pages as 2 MiB blocks *only* when the whole region is stable and uniformly permissioned; otherwise shatter to 4 KiB and accept the TLB-reach loss. Shattering avoids BBM entirely because you never change a block's size.

**Recursive/self-map interaction.** With a lazy-resync design, Windows' self-map is harmless: the guest reads and writes its own tables through ordinary mappings, and we resync on its `INVLPG`. With a write-protect design it is a disaster: every self-map write faults. Another argument for lazy resync.

### 2.5 A/D bits without `FEAT_HAFDBS`

Apple has **no** hardware A/D management. This turns out to be a reasonable fit rather than a catastrophe, because the natural fault-driven mapping is:

| x86 | ARM mechanism | Cost |
|---|---|---|
| `A` bit | Shadow PTE built with `AF=0` → first access raises an **Access Flag fault** (`ESR_EL1` DFSC/IFSC = `0b0010xx`). Handler sets guest `A=1` and shadow `AF=1`, retries. | 1 fault per page per shadow epoch |
| `D` bit | Shadow PTE built with `AP[2]=1` (read-only) even when the guest permits writes → first write raises a **permission fault**. Handler sets guest `D=1`, flips shadow to writable, retries. | 1 additional fault per page per epoch |

So: **up to 2 extra faults per page per shadow generation**, ~500–2000 cycles each, versus zero on real x86 hardware. For a workload touching 1 GiB of 4 KiB pages that is 262 144 pages × ~2 faults × ~1000 cyc ≈ **0.5 Gcycles ≈ 0.15 s** of pure A/D overhead per full sweep. Tolerable for steady-state, painful for page-churn-heavy workloads (kernel build, container start).

**Optimizations:**
- **Skip the A-bit fault when the guest PTE already has `A=1`.** Build the shadow with `AF=1` directly. Since guest OSes clear `A` only during page reclaim, in steady state almost all A-faults vanish. Same for `D`.
- **Batch by block.** If shadowing a 2 MiB block whose guest PDE already has A=1/D=1, one descriptor covers 512 pages.
- Exploit the legality of speculative `A` setting (§2.1): when we fill a shadow entry, we may set the guest's `A` immediately without waiting for an access. Do **not** do the same for `D`.

### 2.6 `CR0.WP` and other whole-shadow invalidators

`CR0.WP=0` (supervisor may write read-only pages) has no ARM analogue. Handling: on a guest write to `CR0` that toggles `WP`, **flush all shadow roots** and rebuild with supervisor-writable permissions. Frequency: essentially zero on modern Linux (`text_poke` uses a separate `mm`, not `WP=0`) but non-zero on older kernels, some Windows paths, and rootkits/anti-cheat. Cost per toggle is a full shadow rebuild — acceptable at low frequency, pathological if a guest toggles it in a loop. Detect and, if a guest is a repeat offender, fall back to a permanently `WP=0`-shaped shadow (all supervisor mappings writable) with software permission checks on supervisor writes.

Similar whole-shadow invalidators: `CR4.PGE`, `CR4.SMEP`/`SMAP`, `CR4.PAE`, `CR4.PCIDE`, `CR4.LA57`, `EFER.NXE`, `EFER.LME`, and MTRR/PAT MSR writes. All rare; all just flush.

### 2.7 The 4 KiB TLB-reach problem

Apple M1 Firestorm: L1 dTLB 256 entries, L2 TLB 3072 entries. At the native 16 KiB granule that is 48 MiB of reach; at 4 KiB it is **12 MiB**. Running the guest at the 4 KiB granule therefore costs a **4× reduction in TLB reach on hardware whose TLBs were sized for 16 KiB**. Expect measurably higher walk rates on large-footprint guests.

**Mitigations:** (a) aggressively use 2 MiB L2 blocks wherever the guest uses 2 MiB pages — the guest kernel's direct map and .text are usually huge-page-mapped, so this recovers most of the reach for kernel-heavy work; (b) `FEAT_TTL` (present) lets `TLBI` specify the level, reducing over-invalidation; (c) share shadow sub-trees to improve walk-cache locality. This is a real, unavoidable, quantified tax — call it **5–15%** on memory-footprint-heavy guests.

---

## 3. Segmentation

### 3.1 What 64-bit mode actually keeps

In 64-bit mode (SDM Vol.3A §3.4.4): CS/DS/ES/SS bases are **forced to 0** and limits are **not checked**. Only **FS** and **GS** retain functional bases, supplied by MSRs, not by descriptors:
- `IA32_FS_BASE` (0xC000_0100), `IA32_GS_BASE` (0xC000_0101), `IA32_KERNEL_GS_BASE` (0xC000_0102).
- `SWAPGS` atomically exchanges `IA32_GS_BASE` with `IA32_KERNEL_GS_BASE`. It is valid only in ring 0. Linux and Windows both use it as the very first instruction of the syscall/interrupt entry path to get at per-CPU data.
- `WRFSBASE`/`WRGSBASE`/`RDFSBASE`/`RDGSBASE` when `CR4.FSGSBASE=1` (used by modern glibc for TLS).

**ARM mapping.** ARM has no segment-base-added addressing mode. A prefixed access `mov rax, fs:[rdi+8]` becomes `ADD tmp, x_fsbase, x_rdi` + `LDR x_rax, [tmp, #8]` — **one extra instruction**. Pin `fs_base` and `gs_base` in two callee-saved ARM registers (there are 31 GPRs; 16 x86 GPRs + RIP + flags scratch + fs/gs base + a few pointers fits comfortably). `SWAPGS` is a 2-instruction register swap against a memory slot. **Cost: negligible.** Note `TPIDR_EL0`/`TPIDRRO_EL0`/`TPIDR_EL1` exist but are just registers with no addressing-mode magic — no advantage over a pinned GPR, and `TPIDR_EL0` is needed by our own runtime.

### 3.2 What still needs real segmentation

- **32-bit compat mode** (`CS.L=0`, `CS.D=1`) is *not* optional. WoW64 on Windows and 32-bit ELF binaries on Linux are still widespread, and they use genuine segmentation: 32-bit Linux TLS uses `%fs` with a base installed via `set_thread_area` / `modify_ldt` — so **the LDT is genuinely live**.
- Descriptors carry base (32-bit), limit (20-bit + G granularity flag), type, DPL, P, D/B, L. Segment **limit checks** on every access in 32-bit mode would be brutal if done naively.
  **Mitigation:** cache the loaded descriptor per segment register (a "descriptor cache", exactly as hardware does) and *specialize the translated code* on it. If base = 0 and limit = 0xFFFFFFFF (the flat case — overwhelmingly common), emit no check and no add. Invalidate translations keyed on the segment register when it is reloaded or when the underlying GDT/LDT entry is written. This reduces 32-bit segmentation to near-zero cost for flat models and correct-but-slow for the rest.
- **16-bit real mode / unreal mode**: needed only if we boot legacy firmware. See §9.6 — we can avoid it entirely in Phase 1.

### 3.3 What modern OSes never use (implement as traps, or not at all)

- **Call gates** (SDM Vol.3A §5.8.3): unused by Linux and Windows. Implement as `#GP` or a slow interpreter path.
- **Hardware task switching / task gates** (SDM Vol.3A Ch.7): *does not exist in 64-bit mode at all*. In 32-bit mode, its only real-world user is the 32-bit Linux `#DF` handler's task gate. Do not implement the task-switch mechanism.
- **TSS**: in 64-bit mode the TSS is used **only** as a data structure holding `RSP0`–`RSP2`, `IST1`–`IST7`, and the I/O permission bitmap offset (SDM §7.7). Read those fields on ring transitions; never perform a task switch.
- **`LSL`/`LAR`/`VERR`/`VERW`**: rare but trivially implementable; `VERW` is now used for MDS mitigation buffer clearing on real hardware — emulate as a no-op after checking the operand.

**Overall difficulty: 2.** Segmentation is folklore-scary and engineering-cheap, *provided* you resist implementing the parts nobody uses.

---

## 4. Privilege and control transfer

### 4.1 Rings

x86 has rings 0–3; rings 1 and 2 are unused by every mainstream OS. Under design (B): **ring 3 → EL0, ring 0 → EL1**, with the nanokernel also at EL1 (sharing EL1 with the guest kernel but in a reserved VA region, protected by `TTBR1` split and by keeping nanokernel data out of the guest's shadow). Under design (A), privilege is a software variable and every privileged operation needs an explicit check — significantly slower and much easier to get wrong.

The EL0/EL1 mapping buys us, for free: user/supervisor page permission enforcement (`AP[1]`), `PXN` for SMEP, `PAN` for SMAP, and hardware-enforced separation of the guest's own kernel/user boundary.

### 4.2 `SYSCALL` / `SYSRET`

`SYSCALL` (SDM Vol.2B):
1. `RCX ← RIP` of the next instruction; `R11 ← RFLAGS`.
2. `RFLAGS ← RFLAGS & ~IA32_FMASK` (0xC000_0084); notably clears `IF` if the guest set that bit (Linux does).
3. `CS.Sel ← IA32_STAR[47:32]`, `SS.Sel ← IA32_STAR[47:32]+8`, both with **hardcoded** attributes — the descriptors are *not* read from the GDT.
4. `CPL ← 0`; `RIP ← IA32_LSTAR` (0xC000_0082).
5. **The stack is not switched.** The kernel does that itself after `SWAPGS`.

`SYSRET` reverses it, and has two quirks that must be reproduced bit-for-bit:
- **`SYSRET` with a non-canonical `RCX` raises `#GP` *in ring 0*, on the kernel stack** — the famous SYSRET bug (CVE-2012-0217 class). Every hardened kernel has a code path that depends on this exact behavior.
- `SYSRET` sets `SS.Sel` but leaves `SS` attributes at a fixed value; some hypervisor/OS combinations care.

`SYSENTER`/`SYSEXIT` (MSRs 0x174 `SYSENTER_CS`, 0x175 `SYSENTER_ESP`, 0x176 `SYSENTER_EIP`) are used by 32-bit code and by the Linux vDSO's `__kernel_vsyscall`. Also `INT 0x80` (legacy) and `syscall` from 32-bit compat (`IA32_CSTAR`, 0xC000_0083).

**Implementation.** These are the hottest control transfers in the system. Implement them as hand-written ARM trampolines that (a) save the guest EL0 state, (b) materialize lazy flags into `R11`, (c) look up the translation for `LSTAR` (which is constant — cache the translated entry point aggressively), (d) `ERET` to EL1 guest-kernel code. Under design (B) the EL0→EL1 transition is a real hardware exception, so the guest's own `SWAPGS`/stack-switch code runs unmodified.

### 4.3 Interrupts, the IDT, and exception classes

**IDT**: 256 entries, 16 bytes each in 64-bit mode, containing a 64-bit offset, a segment selector, a 3-bit `IST` index, a type (interrupt gate clears `IF`, trap gate does not), and a DPL. `IST[1..7]` select an unconditional stack from the TSS — Linux uses IST for `#DB`, `NMI`, `#DF`, `#MC`, and `#VC`.

**Exception classes** (SDM Vol.3A §6.5, Table 6-1) — this is the taxonomy that drives §5:

| Class | Saved `RIP` | Restartable | Examples |
|---|---|---|---|
| **Fault** | address of the **faulting instruction** | yes — instruction re-executes | `#DE`, `#UD`, `#NM`, `#PF`, `#GP`, `#SS`, `#NP`, `#TS`, `#BR`, `#AC`, `#XM` |
| **Trap** | address of the **next instruction** | n/a — instruction completed | `#BP` (INT3), `#OF` (INTO), single-step `#DB`, `INT n` |
| **Abort** | imprecise | no | `#DF`, `#MC` |

**Error codes** are pushed for `#DF`(always 0), `#TS`, `#NP`, `#SS`, `#GP`, `#PF`, `#AC`, `#CP`. Selector-style error codes carry `EXT`(0), `IDT`(1), `TI`(2) plus the index.

**`IRET`** pops `RIP`, `CS`, `RFLAGS`, `RSP`, `SS` — in 64-bit mode **always all five**, even for same-privilege returns. Its notorious property: **`IRET` unblocks NMI unconditionally, even if the `IRET` itself faults.** This is why Linux's NMI entry contains the elaborate nested-NMI trampoline. If we do not reproduce it, guest NMI handling either deadlocks or corrupts. Reproduce it exactly.

**NMI** (vector 2): delivered regardless of `EFLAGS.IF`; blocked from the moment of delivery until the next `IRET`. Model as an explicit per-vCPU latch.

**`#MC`** (vector 18): we simply never generate machine checks. Either don't advertise `CPUID.1:EDX.MCE/MCA`, or advertise them and provide read-as-zero `IA32_MCG_CAP`/`MCi_*` MSRs (Linux enumerates them at boot and copes with either). **Low risk.**

**Interrupt delivery in a JIT.** Interrupts can only be taken at x86 instruction boundaries. The standard mechanism: a per-vCPU "pending event" flag checked at every translation-block entry and at every backward branch (to bound latency inside loops). Under design (B), an incoming host interrupt/signal sets the flag and the vCPU exits at the next check. `EFLAGS.IF` gates maskable delivery; `STI` has a **one-instruction shadow** (interrupts are not recognized until *after* the instruction following `STI`) — Linux's `sti; hlt` idle idiom depends on this, and getting it wrong hangs the guest. `MOV SS, x` and `POP SS` similarly block interrupts (and `#DB`) for one instruction.

**Difficulty: 4.** Mechanically large, conceptually well-understood, but studded with quirks (SYSRET `#GP`, IRET/NMI, STI shadow, `MOV SS` shadow) where "almost right" means "guest crashes in an unreproducible way three hours into a boot."

---

## 5. Precise exceptions in a JIT

This is the constraint that shapes the entire code generator.

### 5.1 The requirement

At **every** instruction that can raise a fault, the emulator must be able to produce the complete x86 architectural state *as of the instruction boundary*: all 16 GPRs, `RIP`, `RFLAGS` (all of it, including PF and AF), all segment registers and their hidden descriptor caches, `CR0/2/3/4/8`, the x87/SSE state, and the MSRs. For **faults**, that is the state *before* the instruction. For **traps**, *after*.

And "can raise a fault" is almost every instruction: any memory access can `#PF`; division can `#DE`; any FP instruction can `#NM` (if `CR0.TS`) or `#XM`; and any instruction at all can be interrupted (though interrupts are only *taken* at boundaries, which is easier).

### 5.2 The three costs

**(1) Register allocation.** Guest registers live in ARM registers, sometimes spilled, sometimes stale in the guest context block. The naive fix — flush all guest registers to memory before every faulting instruction — costs ~16 stores per instruction and is a non-starter (easily **2–5×**).

The correct approach is **metadata, not spilling**: for each potentially-faulting site, record a compact map "at host PC *X*, guest RAX is in `x19`, RBX is at `[x28, #24]`, RIP is *V*". This is precisely LLVM's `StackMap`/`patchpoint` shape, and QEMU's `cpu_restore_state` shape. On a fault, the handler binary-searches host PC → map, reconstructs the x86 state, and delivers the guest exception.
- **Space cost:** roughly comparable to the generated code itself (0.5–1.5×). Compress aggressively (delta-encode host PCs; most sites share a "register assignment epoch" ID, so store an epoch index plus a small delta rather than a full map per site).
- **Time cost at fault:** a binary search plus reconstruction, ~100–300 cycles. Only paid on actual faults. **This is the right trade.**

**(2) RIP.** Do not materialize `RIP` before each instruction (1 store per instruction ≈ 10–20% alone). Derive it from the host PC via a sorted `host_pc → guest_rip` side table, folded into the same metadata.

**(3) Lazy flags.** If `EFLAGS` is kept lazily (as it must be — §6), then at a fault we must be able to *materialize* it. That means the lazy-flag state (operation kind, source operands, result) is part of the recoverable state and must appear in the map. Consequence: **you may defer computing EFLAGS, but you may not discard its inputs across a fault point.** Dead-flag elimination is therefore only legal between a def and a redef with *no intervening fault point* — which, given that nearly every instruction is a fault point, means classic dead-flag elimination is far weaker here than in user-mode emulators. Cost: 2–3 extra live pseudo-registers, and reduced flag-elimination opportunity.

### 5.3 The hard case: instructions that fault partway through

x86 requires that a **fault** leave *no* architectural side effects. This is violated by any naive translation of a multi-access instruction:

- `PUSH [mem]`: load from `mem` may succeed, store to `[RSP-8]` may `#PF`. `RSP` must be **unchanged** on fault. So you cannot decrement `RSP` first.
- `MOVS`/`STOS`/`CMPS`/`SCAS` with `REP`: architecturally **interruptible and restartable**; on a fault, `RSI`/`RDI`/`RCX` must reflect exactly the elements already transferred (SDM Vol.2B, `REP` description).
- `PUSHA`/`POPA`, `ENTER`, `LEAVE`: multiple stack accesses.
- `CMPXCHG16B` and any unaligned access spanning two pages: one of the two pages may be absent.
- Far transfers, `IRET`, task-related ops: many accesses.

**Two disciplines, both needed:**

**(a) Probe-then-execute.** Before an instruction with multiple or wide memory operands, probe every operand for presence and permission (read and/or write) without committing anything. QEMU does this (`probe_write`, `access_prepare`). Under design (B) with hardware shadow tables there is a cheap probe: ARM's **`AT S1E1R`/`AT S1E1W`/`AT S1E0R`/`AT S1E0W` address-translation instructions** (and `FEAT_PAN2`'s `AT S1E1RP`/`S1E1WP`, which respect `PSTATE.PAN` — present on Apple). These ask the MMU "would this access succeed?" and return the result in `PAR_EL1` without side effects. **This is a genuinely useful hardware assist** — a probe costs one `AT` + one `MRS PAR_EL1` (~10–20 cycles) instead of a software walk.

**(b) "Load-all → compute → store-all".** Structure every translated instruction so that *all* faulting accesses happen before *any* architectural register update, and so that at most one store commits after the point of no return. Most x86 instructions have at most one memory *write*, so this is achievable for the vast majority at zero cost. Combined with (a) for the multi-write minority, it makes fault precision nearly free in the common case.

**REP string ops** get special treatment: translate them as an explicit loop whose induction variables *are* the architectural `RSI`/`RDI`/`RCX`, updated at the top of each iteration. Then a fault mid-loop is automatically precise. This forbids the obvious optimization of keeping the counters in scratch registers — the cost of correctness. (An optimized variant: keep counters in scratch, but emit a metadata entry that tells the fault handler how to reconstruct the architectural values from the scratch values plus the iteration count. Recovers most of the speed.)

### 5.4 Checkpoint / replay as an alternative

Snapshot guest register state at each translation-block entry; on a fault, roll back and re-execute the block in a slow, precise interpreter up to the faulting instruction.
- **Pro:** the fast path carries *no* metadata and permits aggressive cross-instruction optimization.
- **Con:** memory writes are not rollback-able without hardware support. Transmeta's Crusoe had a gated store buffer and commit/rollback in hardware; **ARM has nothing equivalent** (`FEAT_TME` = **no** on Apple, so no transactional memory either). So replay only works if the block is guaranteed to have performed no stores before the faulting point — which forces block splitting at every store, destroying the benefit.
- **Verdict:** metadata-based recovery (§5.2) is the correct choice on this hardware. Replay is a fallback for the rare instructions where metadata is impractical.

**Difficulty: 5.** Not because any single piece is hard, but because it is a *global* constraint on the compiler: every optimization must be proven not to destroy recoverable state, and violations produce bugs that only manifest under memory pressure in the guest.

---

## 6. Flags

### 6.1 x86 `EFLAGS`

Bits: `CF`(0), reserved-1(1), `PF`(2), `AF`(4), `ZF`(6), `SF`(7), `TF`(8), `IF`(9), `DF`(10), `OF`(11), `IOPL`(12:13), `NT`(14), `RF`(16), `VM`(17), `AC`(18), `VIF`(19), `VIP`(20), `ID`(21).

The arithmetic six are `CF`, `PF`, `AF`, `ZF`, `SF`, `OF`. Nearly every ALU instruction writes some subset; the subsets differ (`INC`/`DEC` write all but `CF`; `BT`/`BTS` write only `CF`; shifts leave `AF` architecturally undefined but software observes real hardware behavior).

### 6.2 Mapping to ARM `NZCV`

| x86 | ARM | Match |
|---|---|---|
| `ZF` | `Z` | exact |
| `SF` | `N` | exact **at the right width** |
| `OF` (add/sub) | `V` | exact |
| `CF` **on ADD** | `C` | exact |
| `CF` **on SUB/CMP** | `¬C` | ❗ **inverted** — ARM `C=1` means *no borrow* |
| `AF` | — | ❌ no equivalent |
| `PF` | — | ❌ no equivalent |
| `DF`, `IF`, `TF`, `AC`, … | — | software state |

**The carry inversion is solved by hardware.** `FEAT_FlagM` (present on **all** of M1–M4) provides **`CFINV`** — a single instruction that inverts `PSTATE.C`. So `CMP`/`SUB` becomes `SUBS` + `CFINV`: **2 instructions, both single-cycle.** `FEAT_FlagM` also provides `RMIF` (rotate-mask-insert-flags — insert arbitrary bits of a GPR into `NZCV`, ideal for `POPF`/`SAHF`) and `SETF8`/`SETF16` (set `N`/`Z`/`V` from an 8- or 16-bit value — directly targets x86's sub-word operations).

`FEAT_FlagM2` provides **`AXFLAG`** and **`XAFLAG`**, which convert between ARM's FP condition-flag encoding and the x86 `ZF`/`PF`/`CF` encoding produced by `COMISD`/`UCOMISD`. **These instructions exist in the ARM architecture explicitly to make x86 emulation fast.** Apple implements all of them on every generation. This is a substantial, often-overlooked advantage.

**Sub-word widths.** ARM `ADDS`/`SUBS` operate on 32 or 64 bits only; x86 needs 8 and 16 too. The standard trick: **shift both operands left** by (32 − width) and use a 32-bit `ADDS`/`SUBS`. For an 8-bit add, `LSL #24` on both operands makes the 32-bit `N`, `Z`, `C`, `V` come out *exactly* right for the 8-bit operation. Cost: 2 extra shifts, often foldable into the operand-materialization. Alternatively `SETF8`/`SETF16` handle the `N`/`Z`/`V` cases directly.

### 6.3 `AF` — the adjust flag

`AF` = carry out of bit 3, i.e. `AF = ((a ^ b ^ result) >> 4) & 1`.

Its only *consumers* are the BCD instructions `AAA`, `AAS`, `AAM`, `AAD`, `DAA`, `DAS` — **all of which are invalid in 64-bit mode**. But `AF` remains **readable** via `PUSHF`/`PUSHFQ` and `LAHF`. So it must be *correct on demand*, not fast.

**Cost when materialized:** `EOR` + `EOR` + `UBFX` = 3 single-cycle instructions, given that `a`, `b`, and `result` are available. And they *are* available, because §5.3 already requires keeping lazy-flag inputs live. **`AF` is therefore nearly free** — the real cost is not `AF` itself but the requirement to keep operands live, which lazy flags impose anyway. Widely over-feared.

### 6.4 `PF` — the parity flag

`PF` = even parity of the **low 8 bits** of the result (`PF=1` ⟺ even number of set bits). No ARM instruction computes this.

Options, with honest Apple-M-class costs:
| Method | Instructions | Latency | Notes |
|---|---|---|---|
| 256-byte lookup table | `AND` + `LDRB` | ~4–5 cyc (L1 hit) | Costs a cache line, pollutes D-cache |
| Bit-fold: `x^=x>>4; x^=x>>2; x^=x>>1; PF=~x&1` | ~6 | ~6 cyc (serial chain) | No memory traffic — **preferred** |
| NEON `CNT` + LSB | `FMOV`→`CNT`→`FMOV` | ~8–12 cyc | GPR↔SIMD round trip on Apple is ~3–4 cyc each way. **Worse.** |

**But the important observation is where `PF` is actually consumed.** In 64-bit code, `PF` is read essentially in one place: **`JP`/`JNP` after `COMISS`/`COMISD`/`UCOMISS`/`UCOMISD`, to detect NaN.** Unordered compare sets `ZF=PF=CF=1`. This is the standard compiler idiom for `if (a != b)` on floats and is genuinely hot.

For that path, do **not** compute parity. ARM `FCMP` sets `NZCV` to `0b0011` (N=0,Z=0,C=1,V=1) on unordered. The four-way mapping ARM→x86 is a small fixed permutation, and **`AXFLAG` performs exactly this conversion in one instruction.** So the hot `PF` consumer costs *one instruction*, and the cold path (`PUSHF`, `LAHF`, integer `JP`) uses the 6-instruction bit-fold, lazily.

### 6.5 Lazy flags, partial updates, and the shift problem

**Lazy-flag design.** Keep a per-vCPU tuple `(op_kind, src1, src2, result, width)`. Materialize individual flags on demand. Because different instructions write different flag subsets ("partial flag updates" — the emulator analogue of hardware partial-flag stalls), the state must be **per-flag-group**, not monolithic: typically a separate saved `CF` (for `INC`/`DEC`, which preserve it) alongside the main tuple, plus a validity bitmask.

**The shift trap.** x86 shifts and rotates have three nasty properties:
1. `CF` = last bit shifted out.
2. `OF` is architecturally defined only for count == 1 (for `SHL`: `OF = MSB(result) ^ CF`; `SAR`: `OF = 0`; `SHR`: `OF = MSB(original)`), *and software depends on the real behavior for other counts*.
3. **A shift with count == 0 leaves all flags completely unchanged.** This is a *data-dependent conditional flag update* — for `SHL r, CL` the JIT cannot know statically whether flags are written.

Handling (3) requires either a runtime branch on `CL==0` around the flag update, or computing the new lazy-flag tuple and then conditionally selecting between old and new with `CSEL` (branchless, ~2 extra instructions, preferred). Getting this wrong is a classic source of "works for an hour then miscompares" bugs.

**`DF`** (direction) affects string ops; keep it in a register and either branch on it or generate two specialized loops with a guard (DF=0 is overwhelmingly common; specialize for it and deoptimize otherwise).

**`IF`, `AC`, `TF`, `RF`** are system state, not computed flags: `IF` gates interrupt delivery (§4.3); `AC` **is SMAP's override and is toggled on nearly every `copy_to_user`** — map to `PSTATE.PAN` (inverted); `TF` is single-step (map to ARM software-step, `MDSCR_EL1.SS`, or drop to the interpreter); `RF` suppresses instruction breakpoints for one instruction.

**`POPF`/`PUSHF`/`IRET`** must materialize/consume the *entire* `EFLAGS`, including `PF` and `AF`, and must enforce that user mode cannot change `IOPL`, `VM`, `VIF`, `VIP`, or `IF` (when `IOPL < CPL`). `RMIF` makes the `NZCV` half of this a single instruction.

**Cost estimate.** Eager, naive flag computation on every ALU instruction: **2–5× overall**. Well-implemented lazy flags with `FlagM`/`FlagM2`: **10–20%**. The `FlagM` instruction set moves this from "major problem" to "manageable tax" — it is one of the best pieces of news in this document.

---

## 7. Atomics and the memory model

### 7.1 x86 semantics

**`LOCK` prefix** (SDM Vol.3A §8.1/§9.1): legal on `ADD, ADC, AND, BTC, BTR, BTS, CMPXCHG, CMPXCHG8B, CMPXCHG16B, DEC, INC, NEG, NOT, OR, SBB, SUB, XOR, XADD`, and implicit on `XCHG` with a memory operand. A locked instruction is an atomic RMW **and** a full memory barrier, and locked operations have a single total order.

**Atomicity without `LOCK`** (SDM §8.1.1): naturally aligned 1/2/4/8-byte accesses are atomic; additionally, on P6 and later, **unaligned accesses that fit within a single cache line are atomic**.

**Split locks**: a `LOCK`ed RMW crossing a cache-line boundary. Architecturally guaranteed to work (historically via bus locking); modern CPUs can be configured to raise `#AC` (`MSR 0x33 TEST_CTRL`, bit 29) because of the enormous system-wide cost.

**`CMPXCHG16B`**: 16-byte compare-and-swap; **requires 16-byte alignment or raises `#GP`**.

**Memory model — x86-TSO** (SDM §8.2.2/§9.2.2). Permitted reordering: **a load may pass an earlier store to a different address** (store-buffer forwarding). Forbidden: load-load reordering, store-store reordering, and a store passing an earlier load. Stores from a single processor are observed in program order by all processors; there is a total order on stores.

**Fences**: `MFENCE` = full barrier. `SFENCE` = store-store. `LFENCE` = load-load, and since Spectre also a **speculation barrier**. Non-temporal stores (`MOVNTI`, `MOVNTDQ`, `MOVNTPS`) are **weakly ordered even on x86** and require an explicit `SFENCE`.

### 7.2 ARMv8-A comparison and the three options

ARMv8-A is a fully relaxed model: any reordering is permitted except where address, data, or control dependencies (plus barriers) forbid it (ARM ARM §B2.3). A naive translation of TSO code to plain `LDR`/`STR` is **unsound** on multi-vCPU guests.

**Option 1 — acquire/release everywhere.** Guest load → `LDAR`, guest store → `STLR`. This yields *sequential consistency*, which is strictly **stronger** than TSO (SC forbids the store→load reordering TSO allows), so it is correct but over-strong. Measured cost in the literature and in FEX/box64 experience: **20–40%** on memory-heavy code. `FEAT_LRCPC`'s `LDAPR` (RCpc acquire, present on all Apple M-series) is cheaper than `LDAR` and is sufficient for the load side, trimming this somewhat.

**Option 2 — Apple's TSO mode.** Apple cores implement a **hardware TSO memory model**, selected by **`ACTLR_EL1` bit 1 (`EnTSO`)**. With it set, ordinary `LDR`/`STR` obey TSO. This is what Rosetta 2 uses. Measured cost: **~8.94% average** (Wrenger et al., *TOSTING: Investigating Total Store Ordering on ARM*, ARCS'23; and *Analyzing the memory ordering models of the Apple M1*, JSA 2024) — **dramatically better than Option 1.**

**❗ The critical open question.** `ACTLR_EL1` is writable only from EL1 (and is IMPDEF). Therefore:
- **Design (A), userspace JIT: TSO mode is NOT publicly reachable.** There is no public API. `saagarjha/TSOEnabler` is a kernel extension requiring SIP to be disabled — unshippable. Rosetta gets TSO because the *kernel* sets it for translated processes.
- **Design (B), nanokernel in a Hypervisor.framework VM: our nanokernel runs at EL1 and can attempt `MSR ACTLR_EL1, x`.** Whether Apple's hypervisor (running at EL2) traps, ignores, or honors this write is **undocumented and must be tested empirically on day one.**

**This single experiment — "can guest EL1 under Hypervisor.framework set `ACTLR_EL1.EnTSO`?" — is worth roughly 20–30% of total system performance and should be the first thing prototyped.** It is also a second strong argument for design (B).

**Option 3 — selective barriers.** Emit strong accesses only where the memory may be shared. In *user-mode* emulation, escape analysis on the stack works well. In a *system* emulator, essentially all memory is potentially shared (the kernel's direct map aliases everything), so this degenerates. Only genuinely useful special case: a **single-vCPU guest needs no barriers at all**. Worth supporting as a fast configuration.

### 7.3 What TSO mode does *not* fix

**It does not fix atomicity or alignment.** TSO is an *ordering* model. Specifically:

- **Split-lock atomics are impossible on ARM.** ARM's LSE atomics (`LDADD`, `LDSET`, `SWP`, `CAS`) and the `LDXR`/`STXR` pair **require natural alignment**; an unaligned atomic raises an Alignment fault regardless of `SCTLR_EL1.A`. There is no ARM instruction that can atomically RMW across a cache-line boundary.
  **Mitigation:** detect misalignment (statically where the address is known-aligned; otherwise a runtime check, ~2 instructions) and fall back to a **vCPU rendezvous**: IPI all other vCPUs, wait for them to park, perform the RMW non-atomically, release. Cost ~10–50 µs. This is the *only* strictly correct approach, because a hashed-lock scheme cannot stop a plain aligned load on another vCPU from observing a torn intermediate value. Split locks are rare and actively discouraged in modern kernels (Linux warns on them), so the frequency is near zero — but the *correctness* argument matters.
  **Fidelity verdict: correct but slow; acceptable.**

- **`FEAT_LSE2` covers most, not all, of x86's unaligned atomicity.** With `FEAT_LSE2` (present on all Apple M-series), ordinary loads/stores and `LDP`/`STP` are single-copy atomic **provided they do not cross a 16-byte boundary** (ARM ARM §B2.2.1). x86 guarantees atomicity across the whole **cache line** (64 bytes). So an unaligned 8-byte access at offset 60 within a line is atomic on x86 but **not** on ARM. In practice compilers never emit this; a hostile or unusual guest could. **Fidelity gap: real, narrow, low-frequency.**

- **`CMPXCHG16B` maps cleanly.** `FEAT_LSE`'s **`CASP`/`CASPA`/`CASPL`/`CASPAL`** performs a 128-bit compare-and-swap on an even/odd register pair, requiring 16-byte alignment — and x86's `CMPXCHG16B` *also* requires 16-byte alignment (`#GP` otherwise). **The alignment requirements coincide exactly.** `FEAT_LSE128` is absent on Apple but is not needed for this. ✅

### 7.4 Fences and MMIO

| x86 | ARM | Note |
|---|---|---|
| `MFENCE` | `DMB ISH` | Full barrier |
| `SFENCE` | `DMB ISHST` | Store-store |
| `LFENCE` (ordering) | `DMB ISHLD` | Load-load |
| `LFENCE` (speculation barrier) | **`SB`** (`FEAT_SB` = **yes** on all M-series) or `DSB ISH; ISB` | Guests use this in hot mitigation paths; `SB` is the cheap correct answer ✅ |
| `LOCK`-prefixed op | LSE atomic with acquire-release (`LDADDAL`, `CASAL`, `SWPAL`) | Full barrier + atomic in one instruction ✅ |
| `MOVNTx` | plain `STR` (or `STNP` hint) | Lose cache-bypass; correctness fine |
| `PAUSE` | `YIELD` (or `ISB` on some designs) | Spin-loop hint |
| `SERIALIZE` / `CPUID` as serializer | `DSB ISH; ISB` | |

**MMIO ordering.** x86 UC accesses are strongly ordered, non-speculative, and must not be split, merged, or replayed. ARM Device-nGnRnE gives identical guarantees. The reliable implementation is not to rely on attributes at all: **leave MMIO regions unmapped in the shadow tables** so every access faults into software, where we emulate the exact access width and count. This is what QEMU/KVM do, it is correct by construction, and it removes any risk of the JIT merging two 32-bit MMIO stores into one `STP` (which would break device semantics). Cost: an exit per MMIO access — fine, since virtio keeps MMIO off the hot path.

---

## 8. Self-modifying code and code-cache invalidation

### 8.1 The semantic gap

**x86** (SDM Vol.3A §8.1.3/§9.1.3): a write to a location currently held in the instruction cache invalidates the affected line. For **self**-modifying code (same processor writes then executes), the SDM *recommends* a serializing instruction or a jump, but real hardware makes it work regardless, and a great deal of software depends on that. For **cross**-modifying code (one processor patches code another is executing), an explicit synchronization protocol is required.

**ARM**: the instruction cache is **not** coherent with the data cache. `CTR_EL0.{DIC,IDC}` indicate which maintenance can be skipped. The general sequence is `DC CVAU` → `DSB ISH` → `IC IVAU` → `DSB ISH` → `ISB`.

### 8.2 Problem 1 — our own code cache (easy)

We generate ARM code; we must I-cache-invalidate it. macOS: `sys_icache_invalidate()`. W^X: allocate with `mmap(..., PROT_READ|PROT_WRITE|PROT_EXEC, MAP_PRIVATE|MAP_ANON|MAP_JIT, ...)` and toggle with **`pthread_jit_write_protect_np()`**, which is **per-thread** (~1.4% faster than `mprotect` churn). The per-thread nature matters: a multithreaded translator must not have thread A executing from a region while thread B has it writable in its own view — Apple explicitly warns about this. Design the translator so each code region has a single writing thread, or use `pthread_jit_write_with_callback_np`.

Under design (B), code generation happens for code that executes *inside the VM*, so the buffer must be both writable by the host process and mapped executable into the guest IPA space via `hv_vm_map` — a solvable but fiddly double-mapping.

**Difficulty: 2.**

### 8.3 Problem 2 — guest SMC detection (very hard)

The guest writes to memory we have already translated. We must invalidate the affected translations before the guest executes them.

**Mechanism.** Write-protect, at **guest-physical** granularity, every frame containing translated code. Because a frame may be mapped at many guest-virtual addresses in many address spaces, revocation requires the **rmap** from §2.4. On a write fault:
1. Look up translation blocks overlapping the written address.
2. Invalidate them (and any blocks chained to them — direct block linking must be undone; keep a back-pointer list per block).
3. Let the write proceed: either temporarily unprotect + single-step, or (better) emulate the store in software and resume.

**False sharing is the killer.** Page granularity means one page containing both code and hot data produces a fault on *every* data write. This is not hypothetical: guest kernel `.text` is frequently 2 MiB-huge-page-mapped alongside `.rodata` and even writable data, and guest JITs (eBPF, the JVM, .NET, browsers) place code and metadata adjacent.

Worse, under **design (A)** the host page size is 16 KiB, so `mprotect` granularity is 16 KiB — a **4× amplification** of false sharing over the guest's 4 KiB view. Under **design (B)** we control the shadow tables directly and can protect at true 4 KiB granularity. **This is another significant argument for design (B).**

**Escalation ladder** (necessary — a single strategy will not work):
1. **Default:** page-granular write protection.
2. **On repeated faults to a page (> N):** unprotect it, and instead attach a **checksum/validity check** to each translation block from that page, verified on block *entry*. Too expensive for hot blocks, so...
3. **For hot, frequently-written pages:** demote to **interpretation**, or to translation with a per-block "generation counter" compared on entry (1 load + compare + branch, cheap).
4. **Sub-page tracking:** maintain a dirty bitmap at 64- or 128-byte granularity for pathological pages, updated by an instrumented store path used *only* for those pages.

### 8.4 Why guest kernels make this uniquely painful

A modern x86 kernel is one of the most aggressive self-modifiers in existence:

- **Boot-time alternatives patching** (`.altinstructions`): hundreds to thousands of sites rewritten based on CPUID, before the kernel even reaches userspace. A one-time storm, but it happens while our translation cache is cold — mostly harmless.
- **Static keys / jump labels**: NOP ↔ JMP patched at runtime whenever a tracepoint or feature toggles. Uses `text_poke`.
- **`ftrace`**: patches the 5-byte call site at the entry of *every* kernel function.
- **`kprobes`**: writes `INT3` (0xCC) into arbitrary instruction boundaries.
- **Module load/unload**: new executable pages appear; ELF relocation processing writes directly into `.text`; on unload the pages are freed and **reused for data** — so stale translations must be dropped on unmap, not just on write.
- **eBPF JIT**: the guest generates native x86 code at runtime, in guest kernel memory. This is a JIT inside a JIT.
- **Retpoline / return-thunk / IBT `ENDBR` sealing**: more boot-time and runtime patching.
- **Windows**: HotPatch, driver loading, PatchGuard's self-verification, retpoline patching.

**The `text_poke_bp` protocol deserves specific attention.** Linux patches multi-byte instructions atomically by: (1) writing `INT3` over the first byte, (2) IPI + `sync_core()` on all CPUs, (3) writing the remaining bytes, (4) IPI again, (5) writing the final first byte, (6) IPI again. Meanwhile, an `INT3` taken from a patch site is handled by a special handler that emulates the pending instruction. **If our SMC detection is not precise, the guest executes a partially-patched instruction and dies.** The good news: because we detect the *write* itself (synchronously, via a fault) rather than relying on the guest's sync, we are strictly *more* conservative than x86 requires — provided we invalidate **across all vCPUs** with a rendezvous before letting the write land. That rendezvous cost is real but the frequency is bounded.

Also note `text_poke` writes through a **separate `mm`** with an alias mapping of the target page — so the write arrives at a *different guest virtual address* than the one we translated from. **Only guest-physical-granularity tracking catches this.** Virtual-address-based SMC detection is simply wrong for Linux. This is a concrete, easy-to-get-wrong design requirement.

Finally: **emulated-device DMA** (disk reads, network RX) writing into memory that previously held code must go through the same invalidation path. Do not let device models bypass it.

**Difficulty: 5. Performance impact: catastrophic if wrong, 5–20% if right.**

---

## 9. SIMD and floating point

### 9.1 Register mapping

| x86 | Width × count | ARM NEON cost |
|---|---|---|
| SSE–SSE4.2 (XMM) | 16 × 128-bit | 16 of 32 `V` registers — **fits with room to spare** ✅ |
| AVX/AVX2 (YMM) | 16 × 256-bit | 2 `V` regs each = **32 of 32** — zero scratch, forced spilling ⚠️ |
| AVX-512 (ZMM) | 32 × 512-bit + 8 mask regs | 4 `V` regs each = **128 needed, 32 exist** ❌ |

**`FEAT_SVE` is absent on M1 through M4.** `FEAT_SME`/`SME2` exist on M4, but `FEAT_SME_FA64` is **no**, meaning streaming mode disables most of A64 — it cannot serve as a general-purpose wide-vector unit for emulation. **128-bit NEON is the hard ceiling on Apple Silicon.**

**Recommendation: advertise x86-64-v2 (SSE4.2 + POPCNT + CMPXCHG16B) as the baseline, optionally x86-64-v3 (AVX2/BMI/FMA) later, and never AVX-512.** Rationale: v2 is sufficient for essentially all distro binaries and Windows; v3 is increasingly assumed by some software and is achievable at ~2–3× cost on vector code; v4 is rare in the wild and would be a research project here. Rosetta 2 itself shipped without AVX for years, which is strong evidence that v2 is a viable product baseline.

### 9.2 Operations that do *not* map cleanly

These are the ones that bite:

- **`MINPS`/`MAXPS`/`MINSD`/`MAXSD`**: x86's semantics are deliberately non-IEEE — `MIN(a,b)` returns **`b` unconditionally** if either operand is NaN, and also returns `b` when both are zero (so `MIN(+0,-0) = -0` but `MIN(-0,+0) = +0`). ARM's `FMIN` returns NaN, and `FMINNM` implements IEEE `minNum`. **Neither matches.** Correct emulation needs an `FCMGT`-based select: ~3 instructions instead of 1. Common in graphics/media code, so the cost is real.
- **`CVTTPS2DQ` / `CVTTSD2SI`**: out-of-range results produce the "integer indefinite" value `0x8000_0000`. ARM `FCVTZS` **saturates**. Requires a compare + select fixup.
- **NaN propagation**: for 2-operand SSE ops, x86 returns the *first source* (= destination) operand's NaN, quieted. ARM's rule selects the first signaling NaN, else the first quiet NaN, in operand order — mostly but not always identical.
- **`RCPPS` / `RSQRTPS`**: approximate reciprocals with a specified relative error bound (≈1.5 × 2⁻¹²) but implementation-defined exact bits. ARM `FRECPE`/`FRSQRTE` give *different* exact bits. Bit-exact comparison will differ. Generally acceptable; occasionally breaks a game's checksum.
- **`PCMPESTRI` / `PCMPESTRM` / `PCMPISTRI` / `PCMPISTRM`** (SSE4.2 string): each requires ~20–50 ARM instructions. **glibc uses these heavily in `strcmp`/`strstr`/`strlen`.** This is a hot path in guest userspace and deserves hand-written assembly implementations.
- **Horizontal ops, `PSHUFB`, `PMADDWD`, saturating arithmetic**: mostly map fine (`TBL`, `SQADD`, etc.) but need careful lane-order handling.

### 9.3 x87 — the worst fidelity problem

x87 uses **80-bit double-extended precision**: 1 sign, 15 exponent, and 64 **explicit** mantissa bits (the integer bit is explicit, unlike IEEE binary32/64). **ARM has no 80-bit format and no hardware FP128.** `FEAT_FP` provides half, single, and double only.

Consequences:
- x87 must be implemented in **software floating point** (Berkeley SoftFloat / QEMU's `softfloat` are the reference implementations). Cost: **~50–200 cycles per operation** versus ~4 on hardware — a **20–50× slowdown on x87-heavy code**.
- Approximating with 64-bit `double` is **wrong** and will be detected: on x86-64 Linux, `long double` **is** the x87 80-bit type, glibc's `strtod`/`printf` rely on extended precision for correct rounding, and any extended-precision accumulation loop will diverge.
- Additional state: the 8-register **rotating stack** with `TOP` in the status word, the **tag word**, the control word's **precision-control field** (24/53/64-bit mantissa — software does change it), the exception status/mask bits, `FIP`/`FDP`/`FOP`, and the `FSAVE`/`FRSTOR`/`FXSAVE`/`FXRSTOR`/`XSAVE` memory layouts. **MMX aliases onto the x87 registers** and mutates the tag word, so MMX and x87 cannot be implemented independently.

**How much does it matter?** In 64-bit code, compilers use SSE for `float`/`double`; x87 appears only for `long double`. The Linux kernel uses no FP at all. So x87 is *rare but not absent* in 64-bit guests. In **32-bit** guest code it is pervasive (the 32-bit ABI returns floats in `ST(0)`).

**Verdict: implement soft-float80 correctly and accept it is slow.** A `double` fast path is only sound when precision control is 53-bit *and* no precision loss is provable — rarely provable, so don't bother. **Difficulty 4; performance impact severe but narrowly scoped.**

### 9.4 `MXCSR` ↔ `FPCR`/`FPSR`

**`MXCSR`**: `FZ`(15), `RC`(14:13), masks `PM UM OM ZM DM IM`(12:7), `DAZ`(6), sticky flags `PE UE OE ZE DE IE`(5:0).
**`FPCR`**: `AHP`, `DN`, `FZ`, `RMode`(23:22), `FZ16`, plus optional trap enables `IDE/IXE/UFE/OFE/DZE/IOE`. **`FPSR`**: `QC`, `IDC`, `IXC`, `UFC`, `OFC`, `DZC`, `IOC`.

**Rounding modes — note the swap:**

| Mode | x86 `RC` | ARM `RMode` |
|---|:--:|:--:|
| Nearest-even | 00 | 00 |
| Toward −∞ | **01** | **10** |
| Toward +∞ | **10** | **01** |
| Toward zero | 11 | 11 |

The two middle encodings are **transposed**. A 2-bit permutation — trivial, but a guaranteed bug if missed.

**`DAZ` vs `FZ`.** x86 separates *denormals-are-zero on input* (`DAZ`) from *flush-to-zero on output* (`FZ`). Base ARM `FPCR.FZ` does **both**. `FEAT_AFP` adds `FPCR.FIZ` (flush inputs to zero) and `FPCR.AH`, which separates them and matches x86 much more closely — but **`FEAT_AFP` is absent on M1/M2 and present only on M3/M4.** On M1/M2, exact `DAZ ≠ FZ` combinations require software emulation of the affected operations. ⚠️ Generation-dependent fidelity.

**FP exception fidelity — the real gap.** x86 SSE can **trap**: if a mask bit in `MXCSR` is clear and the corresponding condition occurs, the CPU raises `#XM`/`#XF` (vector 19) *precisely*. ARM's trapped FP exceptions (`FPCR.IOE` etc.) are **OPTIONAL in the architecture and are not implemented by Apple** — Apple provides untrapped operation with sticky accumulation in `FPSR` only.

- **Common case (all exceptions masked — the default for essentially all software): maps perfectly.** `FPSR.IXC`↔`MXCSR.PE`, `IOC`↔`IE`, `OFC`↔`OE`, `UFC`↔`UE`, `DZC`↔`ZE`, `IDC`↔`DE`. Read `FPSR` lazily — only when the guest executes `STMXCSR`/`FXSAVE` — so the cost is zero on the hot path.
- **Unmasked case: cannot be delivered precisely by hardware.** Mitigation: detect that the guest has cleared any `MXCSR` mask bit, and from that point translate affected FP instructions to a slow path that checks `FPSR` after each operation and synthesizes `#XM`. Very rare in practice (numerical debugging, some scientific codes, `feenableexcept`). **Fidelity loss: real but rare; mitigation is a mode switch, not a redesign.**

**Denormals:** Apple cores handle denormals in hardware without traps or major penalties, so IEEE-correct (`FZ=0`) operation is not a performance cliff — unlike some older ARM designs.

---

## 10. Devices and platform

### 10.1 The genuine minimum for a modern x86-64 OS

- **CPUID** — leaves 0, 1, 4, 7, 0xB/0x1F, 0xD, 0x15, 0x16, 0x8000_0000–0x8000_0008, plus hypervisor leaves 0x4000_0000+. Must be **internally consistent**; Linux and Windows both cross-validate (e.g. cache topology vs APIC ID layout). A large share of early bring-up bugs live here.
- **MSRs that matter**: `IA32_EFER`(0xC000_0080), `STAR`/`LSTAR`/`CSTAR`/`FMASK`, `FS_BASE`/`GS_BASE`/`KERNEL_GS_BASE`, `IA32_APIC_BASE`(0x1B), `IA32_TSC_DEADLINE`(0x6E0), `IA32_MISC_ENABLE`(0x1A0), `IA32_TSC_AUX`(0xC000_0103), `IA32_PAT`(0x277), the MTRR block (0xFE, 0x200–0x20F, 0x250/0x258/0x259/0x268–0x26F, 0x2FF), `IA32_FEATURE_CONTROL`(0x3A), `IA32_MCG_CAP`(0x179) + `MCi_*`, `SYSENTER_{CS,ESP,EIP}`(0x174–0x176).
  **High-value trick: `IA32_ARCH_CAPABILITIES` (0x10A).** Linux reads it and, if you set `RDCL_NO`, `IBRS_ALL`, `SKIP_L1DFL_VMENTRY`, `MDS_NO`, `TAA_NO`, `SSB_NO`, etc., the guest **disables all its speculative-execution mitigations**. Since we are an emulator and not vulnerable in the x86 sense, this is both honest and worth a large single-digit percentage of guest performance. Requires advertising `CPUID.7.0:EDX[29]`.
- **LAPIC — mandatory.** Prefer **x2APIC** (MSRs 0x800–0x8FF): it needs no MMIO trap at all, riding the MSR path we already have, and is dramatically simpler and faster than xAPIC's MMIO window at `0xFEE0_0000`. Required functionality: ICR (IPIs), LVT entries, TPR/PPR, EOI, the LAPIC timer (one-shot, periodic, **TSC-deadline**), ESR, spurious-interrupt vector.
- **IOAPIC** (`0xFEC0_0000`, 24 redirection entries): needed for legacy INTx routing and for the PIT/RTC/UART IRQs. Keep minimal.
- **8259 PIC**: needed only during early boot before APIC init. ~200 lines.
- **PIT (8254)** at ports 0x40–0x43: Linux uses it for TSC calibration if nothing better exists. **Avoid the need entirely by advertising CPUID leaf 0x15/0x16 and the KVM TSC-frequency leaf** (§11) — then Linux skips calibration.
- **RTC/CMOS** (ports 0x70/0x71): wall-clock time; required by both Linux and Windows boot.
- **HPET: omit.** Linux is perfectly happy without it, and it is a nontrivial MMIO device. Simply don't put it in the ACPI tables.
- **PCI/PCIe**: prefer **ECAM** (memory-mapped config, described by the ACPI **MCFG** table) over the legacy 0xCF8/0xCFC port pair; implement both since firmware uses ports early.
- **ACPI tables**: RSDP → XSDT → FADT, **MADT** (APIC topology — mandatory for SMP), MCFG, and a **DSDT containing AML bytecode**. The DSDT is the awkward one: you must ship a compiled AML blob describing the PCI root bus, interrupt link routing (`_PRT`), and power methods. Windows is far pickier than Linux. **Do not write AML by hand — adapt QEMU's generated tables, which are battle-tested against both OSes.**
- **Devices**: virtio-blk, virtio-net, virtio-console, virtio-gpu (or a simple Bochs-style linear framebuffer), and a 16550 UART for debugging. For Windows guests, either ship virtio drivers or emulate AHCI + e1000.

### 10.2 SMM — can we just not?

**Yes.** System Management Mode is entered only via an SMI, which is generated by the chipset or by firmware writing to the APM control port. If **we** never generate SMIs and **our** firmware doesn't require SMM, SMM need not exist. QEMU supports exactly this (`-machine smm=off`). The only casualty is a *secure* UEFI variable store / Secure Boot implementation, which conventionally lives in SMM; without SMM you get a functional but non-tamper-resistant variable store. Windows does not require SMM. **Omit SMM entirely. Risk: low. This removes an enormous amount of work** (SMRAM, SMBASE relocation, the SMM save-state map, SMI rendezvous across vCPUs).

### 10.3 Boot: avoiding real mode

Legacy firmware starts at the reset vector `0xFFFF_FFF0` in **16-bit real mode**, requiring real mode, unreal mode, the A20 gate, and 16-bit protected mode — a lot of rarely-exercised, easy-to-get-wrong code.

**Phase 1 recommendation: skip firmware entirely.** Implement the **Linux 64-bit boot protocol**: load a `bzImage`, populate `boot_params`, construct page tables, and enter the kernel at its 64-bit entry point with the CPU already in long mode. **No real mode, no 16-bit, no firmware, no A20.** This gets a Linux guest booting with a fraction of the work.

**Phase 2:** run **OVMF** (or SeaBIOS) as ordinary guest x86 code — no porting needed, since firmware is just x86 code we execute. That does require implementing real mode and 32-bit protected mode, but by then the rest of the system is stable. UEFI x64 is the only path for Windows guests.

**MTRRs**: Linux parses them if `CPUID.1:EDX[12]` is set. Either don't advertise MTRR (Linux copes and falls back to PAT), or advertise with a trivial configuration: default type WB, one UC range covering the MMIO hole. Cheap either way.

**Difficulty: 3** — conceptually easy, but a large volume of unglamorous, exacting work. This is where schedule overruns come from, not where the project dies.

---

## 11. The TSC

### 11.1 x86 semantics and guest expectations

`RDTSC` returns a 64-bit counter in `EDX:EAX` (zero-extending, so the upper halves of `RAX`/`RDX` are cleared). `RDTSCP` additionally returns `IA32_TSC_AUX` in `ECX` and waits for prior instructions to have executed (it is *partially* ordering, not fully serializing).

**Invariant TSC** (`CPUID.8000_0007H:EDX[8]`, SDM Vol.3B §17.17): the TSC advances at a constant rate independent of P-states and C-states. Every modern CPU has it, and **guests assume it**: Linux selects the TSC as its primary clocksource, and the vDSO `clock_gettime` implementation reads it directly from userspace. `RDTSC` is therefore on one of the hottest paths in the entire guest.

Guests require the TSC to be:
1. **Monotonic** — never going backwards. Linux runs a TSC synchronization test at boot and will mark the TSC unstable if it observes backwards motion, permanently downgrading to a far slower clocksource.
2. **Synchronized across CPUs** — within the tolerance of that test.
3. **Fine-grained** — code frequently measures short intervals and divides by the delta.

Guests determine the frequency via (a) `CPUID.15H`/`16H` (crystal ratio and nominal frequency), (b) calibration against the PIT/HPET/ACPI PM timer, or (c) **the hypervisor leaf `0x4000_0010`, which reports TSC kHz directly** and which Linux honors whenever it detects a hypervisor.

### 11.2 The Apple Silicon resolution mismatch

**`CNTVCT_EL0` runs at 24 MHz** (`CNTFRQ_EL0` = 24 000 000) — one tick every **~41.67 ns**. An x86 TSC typically runs at 2–4 GHz — one tick every ~0.3 ns. That is a **~100× resolution gap.**

**Why `PMCCNTR_EL0` cannot rescue us:** the ARM cycle counter would give true core-frequency resolution, but (a) **`FEAT_PMUv3` is `no` on M1, M3, and M4** — Apple implements an IMPDEF PMU, not the architectural one, and `PMCCNTR_EL0` is not available under Hypervisor.framework; (b) it is not invariant (it tracks DVFS); (c) it is not synchronized between P-cores and E-cores, so a migrating vCPU would see it jump **backwards** — the single thing a guest cannot tolerate. **`CNTVCT_EL0` is the only usable time source.**

### 11.3 The fix

**Step 1 — scale.** `TSC = (CNTVCT_EL0 × M) >> S`, with `M/2^S ≈ 100` to synthesize a ~2.4 GHz TSC. Use a 64×64→128 multiply (`MUL` + `UMULH`) to avoid overflow.

**Step 2 — advertise the synthesized frequency** via hypervisor leaf `0x4000_0010` (and `CPUID.16H`). The guest then never calibrates, so the PIT/HPET never enter the picture and there is no risk of a calibration mismatch.

**Step 3 — per-vCPU monotonic clamp.** Scaling makes `RDTSC` deltas jump in steps of ~100, so two back-to-back reads still return **the same value** (delta 0). Guest code that divides by the delta will fault or spin. Fix: keep a per-vCPU `last_tsc`; if the new value is ≤ `last_tsc`, return `last_tsc + 1`. Store it back. Cost: one load, compare, `CSINC`, store. **Keep it per-vCPU** — a shared counter would need an atomic and would serialize all vCPUs on the hottest instruction in the guest.

Cross-vCPU monotonicity is only architecturally required for causally ordered reads, and since all vCPUs derive from the same physical `CNTVCT`, they are inherently within ~1 tick of each other — comfortably inside Linux's tolerance.

**Cost of `RDTSC`:** `MRS CNTVCT_EL0` is ~20–40 cycles on Apple cores; plus multiply/shift plus the clamp ≈ **40–60 cycles total**, against ~20–30 for a real `RDTSC`. Acceptable — and note that guest EL0 must be permitted to read the counter (`CNTKCTL_EL1.EL0VCTEN`), which under design (B) we control.

### 11.4 Two traps

- **`TSC_DEADLINE` LAPIC timer** (`CPUID.1:ECX[24]`): the guest programs an absolute TSC value. Map it to `CNTV_CVAL_EL0` using the **inverse** scaling. Clean, and worth supporting — Linux prefers it.
- **Host sleep/suspend.** `CNTVCT` is an always-on counter and keeps advancing while the Mac sleeps or the VM is paused. On resume the guest would see a multi-hour TSC jump, likely tripping soft-lockup watchdogs or the TSC-stability check. **Maintain a guest-visible TSC offset** and adjust it across every pause/resume so the guest's TSC appears continuous. Under design (B), `CNTVOFF_EL2` is managed by Apple's hypervisor, so this must be done in our own scaling arithmetic rather than in hardware.

**Difficulty: 3. Performance impact: moderate but on a very hot path.**

---

## 12. Top 5 project-killing risks

Ordered by the product of probability and severity.

### Risk 1 — Precise exceptions constrain the optimizer to death (§5)
**Severity: fatal to performance, not to correctness.**
Every optimization the code generator wants to perform — cross-instruction scheduling, dead-flag elimination, keeping induction variables in scratch registers, register-allocating aggressively across blocks — must be proven not to destroy state that could be observed at a fault. Since almost every x86 instruction is a potential fault site, the naive-safe implementation (spill everything) costs **2–5×** and eliminates the project's reason to exist. The metadata approach recovers this, but it is a *global invariant* that every future contributor must respect. The failure mode is insidious: violations produce guest corruption that appears only under memory pressure, hours into a workload, and is nearly impossible to bisect.
**Mitigation:** design the state-map infrastructure **first**, before any optimization exists. Build a differential tester that runs every translated block against a reference interpreter and compares full architectural state at every fault site. Make "is this state recoverable?" a type-level or assertion-level property in the IR, not a convention.

### Risk 2 — Self-modifying-code detection collapses under guest kernel behavior (§8)
**Severity: fatal to performance; potentially fatal to correctness.**
A modern x86 kernel patches its own text constantly (alternatives, static keys, ftrace, kprobes, module load, eBPF JIT, retpolines), and `text_poke` writes through an **alias mapping in a separate `mm`** — so any virtual-address-based detection scheme is simply wrong for Linux. Page-granular write protection with code/data on the same page produces fault storms that can be arbitrarily bad (100×+ on a pathological page). Under design (A) the 16 KiB host page size makes this **4× worse** than the guest's own granularity.
**Mitigation:** guest-**physical**-granularity tracking with a reverse map, from day one. Build the escalation ladder (protect → checksum → generation-counter → interpret) as a first-class subsystem, not as an afterthought. Instrument fault rates per page and alert in testing. Choose design (B) so protection granularity is a true 4 KiB.

### Risk 3 — `ACTLR_EL1.EnTSO` is unreachable, forcing `LDAR`/`STLR` everywhere (§7)
**Severity: ~20–30% of total system performance.**
Apple's hardware TSO mode costs ~9%; the portable alternative (acquire/release on every guest memory access) costs 20–40%. TSO is set via an IMPDEF EL1 register with **no public API**. In a userspace JIT it is definitively unavailable (the only known enabler is an out-of-tree kext requiring SIP off — unshippable). In a Hypervisor.framework VM it *might* be settable from guest EL1, but Apple's EL2 hypervisor may trap or silently ignore the write. **This is entirely unknown and undocumented.**
**Mitigation:** **Prototype this in week one.** It is a ~50-line experiment (boot a minimal EL1 payload in an HVF VM, write `ACTLR_EL1`, read it back, then run a litmus test — e.g. the classic store-buffering test — to confirm the model actually changed; reading back the bit is *not* sufficient evidence). The answer determines both the architecture choice and the achievable performance target. Have the `LDAPR`/`STLR` fallback designed regardless, and support a single-vCPU no-barrier fast configuration.

### Risk 4 — Shadow-paging fault costs swamp the gains from hardware walks (§2)
**Severity: could invert the entire design rationale.**
Design (B) exists to eliminate the softmmu tax by using real hardware page walks. But every shadow miss costs 500–2000 cycles (ARM abort + software x86 walk + shadow fill), against tens of cycles for a hardware EPT walk on real x86. On top of that: **no `FEAT_HAFDBS`** means up to 2 extra faults per page per epoch for A/D emulation; **no `FEAT_BBM`** makes huge-page promotion expensive; the 4 KiB granule costs **4× TLB reach** on TLBs sized for 16 KiB; and the rmap for aliasing adds memory and per-fill time. If the miss rate is high enough, design (B) could end up *slower* than a well-tuned softmmu while being far more complex and requiring an entitlement.
**Mitigation:** build a **fault-rate model early** — instrument a softmmu prototype to count what the shadow miss rate *would* be for real workloads (boot, kernel build, container start) before committing to (B). Front-load the miss-avoidance levers (ASID-per-PCID, `TTBR1` for the kernel half, shadow-root cache, sub-tree sharing, building shadows with `AF`/`D` pre-set from the guest PTE). Set a measurable go/no-go threshold.

### Risk 5 — Platform/device/firmware breadth consumes the schedule (§9, §10)
**Severity: not fatal, but the most likely cause of the project never shipping.**
None of CPUID consistency, ACPI/AML, LAPIC/IOAPIC, PCI ECAM, virtio, real mode, x87 soft-float, `PCMPxSTRx`, or `MIN/MAXPS` is *hard*. All of them are exacting, individually unglamorous, and collectively enormous — and each has a long tail of "the guest boots 95% of the way and then hangs" bugs. This is where cross-architecture emulator projects historically die: not on a wall, but on a beach.
**Mitigation:** ruthless scope control. Phase 1: **Linux only, direct 64-bit `bzImage` boot (no firmware, no real mode), x2APIC only, virtio only, no SMM, no HPET, x86-64-v2 CPUID cap, soft-float80 x87.** Adapt QEMU's ACPI tables rather than authoring AML. Defer OVMF, Windows, 32-bit compat mode, and AVX2 to explicitly later phases. Treat "boots a stock distro kernel to a shell" as the only Phase-1 milestone that counts.

---

## 13. Recommendations

1. **Adopt design (B), the nanokernel-in-VM architecture** — but gate it on the two experiments below.
2. **Week-1 experiment A:** can guest EL1 under Hypervisor.framework set `ACTLR_EL1.EnTSO`, and does a litmus test confirm the memory model actually changed? (Risk 3.)
3. **Week-1 experiment B:** can guest EL1 under Hypervisor.framework set `TCR_EL1.TG0/TG1` to the **4 KiB granule** and successfully walk a 4-level table? The register dumps say the hardware supports it and QEMU's `ipa-granule` work confirms stage 2 — but confirm stage 1 under HVF end-to-end. (Risk 4, and §0.1's go/no-go gate.)
4. **Build the precise-exception state-map infrastructure before any optimizer.** (Risk 1.)
5. **Make SMC tracking guest-physical from the first line of code.** (Risk 2.)
6. **Cap CPUID at x86-64-v2** and advertise `IA32_ARCH_CAPABILITIES` with the "not vulnerable" bits set.
7. **Do not implement:** 5-level paging/LA57, AVX-512, SMM, hardware task switching, call gates, HPET. **Defer:** real mode, OVMF, Windows, 32-bit compat, AVX2.
8. **Exploit `FEAT_FlagM`/`FlagM2` (`CFINV`, `RMIF`, `SETF8/16`, `AXFLAG`, `XAFLAG`) aggressively** — ARM added them for exactly this purpose and Apple implements all of them on every generation.
9. **Use `AT S1E0R`/`S1E1R`/`S1E1WP` for fault-free memory-operand probing** in multi-access instructions. Underused hardware assist.

---

## Sources

- [lelegard/arm-cpusysregs — Arm features in Apple M1/M2/M3/M4](https://github.com/lelegard/arm-cpusysregs/blob/main/docs/apple-m1-features.md) — decoded `ID_AA64*_EL1` dumps from real hardware; the source for all per-chip feature claims in §0.2.
- [Spink, Wagstaff & Franke, *A Retargetable System-Level DBT Hypervisor*, USENIX ATC'19](https://www.usenix.org/conference/atc19/presentation/spink) — the Captive design; hardware-MMU-backed cross-ISA full-system DBT, 2.21× over QEMU.
- [Spink, *A Retargetable System-Level DBT Hypervisor* (ACM TOCS, extended)](https://research-repository.st-andrews.ac.uk/bitstream/handle/10023/24320/Spink_2020_ACM_Trans._Comput._Syst_Retargetable_System_Level_AAM.pdf)
- [Wrenger et al., *TOSTING: Investigating Total Store Ordering on ARM*, ARCS'23](https://www.sra.uni-hannover.de/Publications/2023/tosting-arcs23/wrenger_23_arcs.pdf) — `ACTLR_EL1` bit 1 `EnTSO`; TSO cost measurement.
- [Wrenger et al., *Analyzing the memory ordering models of the Apple M1*, JSA 2024](https://www.sra.uni-hannover.de/Publications/2024/wrenger_24_jsa.pdf) — the ~8.94% TSO figure.
- [saagarjha/TSOEnabler](https://github.com/saagarjha/TSOEnabler) — kext demonstrating per-thread TSO enablement; evidence that no userspace API exists.
- [QEMU patch: *hvf: map granule abstraction, configurable IPA*](https://patchew.org/QEMU/20260315034154.41986-1-lucaaamaral@gmail.com/20260315034154.41986-3-lucaaamaral@gmail.com/) — `ipa-granule` (auto/4k/16k); confirms Apple stage-2 4 KiB support independent of the 16 KiB host page size.
- [Asahi Linux — Broken Software / 16K pages](https://asahilinux.org/docs/sw/broken-software/) and [Talospace: *Asahi Linux gives hope for weird page sizes yet*](https://www.talospace.com/2022/03/asahi-linux-gives-hope-for-weird-page.html) — Apple Silicon has 4K support "chiefly to make Rosetta work"; the DART IOMMU is 16K-only; macOS itself always runs 16K.
- [Apple: `pthread_jit_write_protect_np(3)`](https://keith.github.io/xcode-man-pages/pthread_jit_write_protect_np.3.html) and [Apple Hypervisor framework docs](https://developer.apple.com/documentation/hypervisor) — W^X JIT mechanics; `hv_vmx_*` being Intel-only confirms no x86 guest support on Apple Silicon.
- [libcpucycles — Counters](https://cpucycles.cr.yp.to/counters.html) — `CNTVCT_EL0` at 24 MHz on Apple Silicon.
- Intel SDM Vol.1 (Ch.8 x87, Ch.10 SSE/`MXCSR`, Ch.14 AVX); Vol.2 (`CPUID`, `RDTSC(P)`, `CMPXCHG16B`, `INVPCID`, `SYSCALL`/`SYSRET`); Vol.3A (Ch.3 segmentation, Ch.4 paging, Ch.5 protection, Ch.6 interrupts/exceptions, Ch.7 task management, Ch.8/9 multiprocessor & memory ordering & SMC, Ch.10/11 APIC, Ch.11/12 cache control/MTRR/PAT); Vol.3B (§17.17 TSC); Vol.3C (SMM).
- Arm Architecture Reference Manual for A-profile, DDI 0487 — §B2.2.1 (single-copy atomicity, `FEAT_LSE2`), §B2.3 (memory ordering), §D1 (exception model, `ESR_EL1`), §D8.2–D8.6 (granules, `TCR`, descriptor formats, access flag, permissions, `MAIR`), §D8.13–D8.16 (TLB maintenance, break-before-make).

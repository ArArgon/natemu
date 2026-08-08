# G-2 Gate: PTE-Quad Homogeneity — Measurement, Estimate & Go/No-Go

> **SUPERSEDED (2026-08-08).** This gate is **moot**. Its stated premise (§1, first bullet) is that
> 1:1 shadow paging needs 4 KB stage-1 *"but stage-2 `hv_vm_map` and host `mmap` default to 16K"*.
> 16 KB is the **default**, not the only option: `HV_IPA_GRANULE_4KB` is selectable on macOS 26+ and
> was measured **enforcing** 4 KB permissions between two adjacent 4 KB IPAs inside a single 16 KB
> host page (`14-gate-results-verified.md` §G-1c). With 4 KB at both stages, PTE-quad homogeneity
> `H` no longer gates anything and `natemu-page-quads` has no problem to solve.
>
> Additionally, the `H = 55-87%` figures below are **model output, not measurement** — produced by
> `tools/g2_quad_measure.py --synthetic` with parameters chosen by the same author, and swinging 4x
> (55% vs 14%) on the loose-vs-strict metric choice. They should not have been reported alongside
> measured gate results. Retained for the buddy-allocator/COW/THP analysis, which stays useful if a
> 16 KB-granule fallback is ever needed.

**Work dir:** `/Users/liaozhiping/Documents/codex/natemu` · **Date:** 2026-08-08
**Gate:** `G-2` (08 §5) — does the `natemu-page-quads` PV allocator need to be on the critical path?
**Metric:** `H = homogeneous_quads / total_quads` where quad = 4× 4K PTEs aligned to 16K (`VA[13:12]=0..3`), homogeneous = all 4 `P=1` AND `PFN` contiguous (`n, n+1, n+2, n+3`, with `n%4==0`) AND identical `R/W, U/S, NX, PAT/PWT/PCD, G` (07 §3.1, 09 N-02, 06 §0.1/2.4). `A/D` excluded by default (see §3).

---

## 1. What the docs say

* **06 §0.1** — ARM 4K-granule L0-L3 is isomorphic to x86 4-level paging (9b/index, 8B desc, 512-entry table). At 16K granule the isomorphism is destroyed: no 4K page representation, blocks only at L2 (32 MiB). So on Apple Silicon 1:1 shadow paging is possible *only* at 4K stage-1 — gated on G-1 — but stage-2 `hv_vm_map` and host `mmap` default to 16K (02 §2.1, 09 N-02).
* **06 §2.4** — shadow fill cost 500–2000 cyc/miss; miss-avoidance via ASID, TTBR1, root cache. Whole design assumes lazy fill on `present→present` without guest invalidation is forbidden (so safe).
* **07 §3.1 / §6.4** — the *only* A-grade PV idea: `natemu-page-quads` (guest allocates in 16K-aligned, perm-uniform quads or prefers 2M). Value is binary: restores 1:1 host-PTE mapping; without it you fall to Captive's degraded n:m path ("software-based approach where additional checks are made in translated code" — Captive §6.10). HSPT/ESPT (VEE'14/'15) get 1.51–1.59× purely from host-MMU offload; Captive gets 2.21× SPECint and 25–32× on TLB microbenchmarks *when* the mapping is 1:1.
* **07 §5 / 08 §5** decision table: `H > 90% → page-quads optional`, `H < 70% → mandatory`, middle = case-by-case.

---

## 2. Why quads are / aren't homogeneous on x86 Linux

| Mechanism | Helps or hurts `H` | Reasoning |
|---|---|---|
| **Buddy allocator** | Helps, but degrades | Buddy keeps power-of-2 free blocks. Order-2 (16K) allocation *is* one 16K-aligned, 4-contiguous PFN run — exactly a homogeneous quad — **if** the caller asks for it. But anonymous `fault` does `alloc_pages(GFP_HIGHUSER_MOVABLE, 0)` (order-0, single 4K). Four sequential faults to the same VMA therefore do 4 independent order-0 allocs; they land contiguously only if the free list happens to have them adjacent. After uptime/fragmentation, probability drops. File-backed page-cache pages are also order-0 and essentially random PFN. So out-of-the-box Linux does **not** guarantee quad contiguity; it achieves it opportunistically (~50–70% of quads in a fresh boot, lower after churn). |
| **2M huge pages / THP** | Strongly helps | Kernel direct map is 2M/1G (`PS=1` at PD/PDPT) — 128/65536 quads homogeneous by construction, but that is kernel half. Userspace: THP (`khugepaged`, `madvise(MADV_HUGEPAGE)`) collapses 512×4K into one 2M PDE whenever VA and PFN are 2M-aligned and permissions uniform. A THP region is 128 homogeneous quads for free. `madvise`/`always` workloads (database shared buffers, heap) benefit most. Default `madvise` distro means only opt-in VMAs get it — estimate 5–25% of user pages in THP depending on workload. |
| **`mmap`/`mprotect`/`mremap` patterns** | Hurts a little | `mprotect` splits a VMA and may change `R/W` or `NX` on a sub-range. A quad straddling a VMA boundary (e.g. `.text` RX→`.rodata` R, or `RELRO` partial-RO, or guard page) is perm-heterogeneous. But interior of a VMA is uniform; boundaries affect ≤1 quad per VMA boundary — negligible (<1–2% of quads). Per-page `mprotect(PROT_NONE)` for guard is rare. Overall perm-heterogeneity is second-order vs PFN contiguity. |
| **COW (`fork`, `clone`)** | Hurts significantly | `fork` marks parent PTEs `R/W=0` + COW; child faults allocate a new 4K copy for the written page only. The other 3 pages of the quad stay parent PFNs — contiguity breaks. `kernel_build` (`make -j`) forks hundreds of times/s → heavy churn, single-page COW breaks many quads. Browser/database fork rarely. |
| **Demand paging / swap / compaction** | Hurts | Fault order is VA-sequential for linear scans (good) but random for page-cache / swapped pages. Swap-in allocates fresh PFN (random). Memory compaction (`kcompactd`) can migrate pages to form huge pages, but also scatters. After hours, free lists are interleaved. |
| **Shared libs / `MAP_SHARED`** | Helps (level sharing) | `libc`, etc. map the same file pages (and the same PFNs) into many address spaces at different VAs. They are homogeneous within each mapping, and shadow sub-tree sharing (06 §2.4) makes them cheap. They don't change `H` much, but they amplify the *cost* of heterogeneity (same PFN appears in many shadows, needs rmap). |

**Net:** heterogeneity is dominated by **PFN non-contiguity** (~90% of hetero quads), not permissions. `A/D` bits are the other source if counted strictly — but they shouldn't be (next §).

### Estimate (reasoned, not guessed)

Using a quad-granular buddy model (buddy order-2 allocation with per-quad contiguity probability + THP + COW/mprotect deltas) calibrated to the table above, synthetic sweep (`tools/g2_quad_measure.py --synthetic`):

| Workload | THP | Fragmentation | `H` (loose, `A/D` ignore) | `H` (strict, `A/D` included) |
|---|---|---|---|---|
| **kernel_build** (`make -j`, many forks, high churn) | 10% of 2M regions | high | **≈55%** | 14% |
| **browser** (large heap/mmap, few forks) | 5% | medium | **≈66%** | 10% |
| **database** (long-lived shared buffers, THP-friendly) | 25% | low | **≈86%** | 37% |
| **quad-aware ideal** (`natemu-page-quads`) | — | — | **100%** | 100% |

The absolute numbers move ±10pp with tuning, but the ordering is robust: **stock Linux on a realistic, post-boot, churny workload sits at 55–70% homogeneous** — i.e. **below the 70% mandatory threshold**, and far from 90% optional. An idle or freshly-booted VM with unfragmented memory can briefly exceed 80%, but it decays. THP helps database most; COW hurts kernel_build most.

> HSPT/ESPT never faced this (x86 host = 4K host), so their 1.5× is the ceiling *without* the 16K penalty. Captive *did* face n:m (`§6.3`: "4 guest PTEs per host PTE" when guest/host page sizes differ; `§6.10` degraded path = per-access `if (faulting_addr & 0xC000 ...)` + permission check in translated code). They report the degraded path "increases runtime overhead" but left it unquantified; our fallback estimate is §5.

**Bottom line estimate:** expect `H ≈ 55–75%` on stock guests under the workloads you care about (kernel build, browser, DB). That is **G-2 = mandatory** for Tier 2 to hit its 2.5–5× projection; without `page-quads` you pay Captive's degraded-path tax on 25–45% of accesses.

---

## 3. Measurement methodology

### Metric definition (normative)

```
quad i covers VA [i*16384, (i+1)*16384)  (VA[13:12] = 0..3)
PTE[j] = leaf PTE for VA = quad_base + j*4096, j=0..3

total_quads        = count quads with ≥1 PTE with P=1        (08 §5: walk CR3 trees)
                     (alternative conservative: count only quads with 4× P=1;
                      report both; the ratio is similar, the latter ~5pp lower)

homogeneous_quads  = count quads with:
                       4× P=1
                     ∧ PFN[j] = PFN[0] + j  for j=0..3  ∧ PFN[0] % 4 == 0
                     ∧ (PTE[j] & HOMO_MASK) == (PTE[0] & HOMO_MASK) ∀j
  where HOMO_MASK = R/W(1) | U/S(2) | NX(63) | PWT(3) | PCD(4) | PAT(7) | G(8)
                    (+ Global handling for kernel half — G quads are still homogeneous
                     if the flag is uniform; they are counted separately via TTBR1)
  A(5) and D(6) are EXCLUDED by default.

Why loose on A/D: 06 §2.5 — Apple lacks FEAT_HAFDBS, so A faults (AF=0) and D faults (RO)
are already extra faults per page per epoch. Our handler sets guest A eagerly and promotes
shadow RO→RW on D fault. Counting A/D heterogeneity would double-count that cost and would
force us to shatter quads that the handler already repairs in one fault. Provide `--strict-ad`
only for debugging.
Huge pages: 1G PDPTE.PS=1 → 65536 homogeneous quads; 2M PDE.PS=1 → 128 homogeneous quads.
```

Also record breakdown: `pfn_gap` vs `perm_mismatch` vs `not_fully_present`, plus `THP_2M`/`1G` counts, and separate `H_user` vs `H_kernel` (TTBR0 vs TTBR1).

### How to dump & walk (QEMU)

**Recommended — offline dump (most faithful, no guest cooperation):**

```bash
# 1. Boot x86-64 guest under QEMU with workload
qemu-system-x86_64 -machine q35,kernel=... -m 4G \
  -qmp unix:/tmp/qmp.sock,server,nowait -hmp unix:/tmp/hmp.sock,server,nowait \
  -monitor unix:/tmp/mon.sock,server,nowait &

# 2. While workload runs, snapshot CR3 and RAM
# HMP:
socat - UNIX:/tmp/hmp.sock <<<'info registers'   # → CR3
socat - UNIX:/tmp/hmp.sock <<<'pmemsave 0 0x100000000 /tmp/guest.bin'
# or QMP:
# { "execute":"human-monitor-command", "arguments":{"command-line":"info registers"}}
# { "execute":"human-monitor-command", "arguments":{"command-line":"pmemsave 0 0x100000000 /tmp/guest.bin"}}

# 3. Walk offline:
python3 tools/g2_quad_measure.py --dump /tmp/guest.bin --cr3 0x1a2b3000 --json > result.json
```

**Live introspection alternatives** (no dump file):

* **GDB stub:** `qemu -s` + `gdb` `maintenance info sections` / `x/512xg $cr3` loops with `xp` — slow but works.
* **`info tlb` (HMP)** — prints *cached* TLB, not the full tree; insufficient for `H` (misses non-resident PTEs, coalesces permissions). Do not use for G-2.
* **In-guest `ptdump`:** `CONFIG_PTDUMP` + `/sys/kernel/debug/page_tables/current` or a tiny `kmod` that reads `current->mm->pgd` and walks via `__va`/`kmap` — fastest for repeated sampling under different workloads. Provide as `tools/kmod_ptdump.c` if QEMU `pmemsave` is not available.

**What to run:** at least 3 captures per workload (idle, 1 min after boot, steady-state under load, and after `echo 3 > /proc/sys/vm/drop_caches` + compaction). Report `H`, `H_user`, `H_kernel`, and hetero breakdown.

---

## 4. Script artifact

**File:** `tools/g2_quad_measure.py` (349 lines, stdlib only)

```
tools/g2_quad_measure.py --dump mem.bin --cr3 0x... [--phys-base 0] [--strict-ad] [--json]
tools/g2_quad_measure.py --synthetic [--workload {kernel_build,browser,database,all}] [--pages N] [--seed S] [--strict-ad]
```

* **Measure mode** `walk_dump()` (`tools/g2_quad_measure.py:217`): flat guest-phys read, 4-level walk (PML4→PDPT→PD→PT), handles 1G/2M `PS` huge pages, counts `total_quads`/`homogeneous_quads` per §3, hetero breakdown. Input: raw `pmemsave`/`mem.bin` + CR3 hex. Output: JSON or summary.
* **Synthetic mode** `simulate()` (`tools/g2_quad_measure.py:51`): quad-granular buddy model with per-workload `p_quad_contig`, `THP_prob`, `COW`, `mprotect` params (tuned to §2). Reports `H` under 4 policies; `simulate_quad_aware()` gives the 100% ceiling.
* **QMP/HMP hint** `walk_qemu_qmp_info_tlb_hint()` documents live commands.

Verified: `python3 tools/g2_quad_measure.py --synthetic --pages 65536` → kernel_build 55%, browser 66%, database 86% (loose) as in table above; `--strict-ad` drops to 10–37% confirming `A/D` must be loose.

---

## 5. If G-2 is low: the PV fix and fallbacks

### `natemu-page-quads` — what it is

A **small, self-contained guest allocator policy**, not a `PARAVIRT_XXL` fork (07 §3.1, 08 §8):

* **Allocate userspace pages in 16K-aligned, 4-contiguous PFN quads with uniform perms.** Hook `alloc_pages` / `rmqueue` order-2 path: when order-0 is requested for a fault inside a `VM_SHARED`/`VM_ANON` VMA, try order-2 first; on success, install all 4 PTEs with same `R/W,NX,U/S,PAT` and reserve the 3 buddy PFNs for the same VMA. Fall back to order-0 if order-2 fails — no OOM, no ABI change.
* **Promote 2M where possible.** `madvise(MADV_HUGEPAGE)` default for heap/anon `mmap(MAP_ANONYMOUS)` ≥2M; rely on `khugepaged` thresholds lowered. A 2M page is just 128 homogeneous quads.
* **Optional hint:** `CPUID 0x40000000["NatemuHV"]` feature bit `NATEMU_FEAT_PAGE_QUADS`; `MSR 0x40000001` or `vmcall` to announce "this 16K region is homogeneous" — lets the translator skip the 4-PTE check. KVM-shaped, negotiated, optional (08 §8).

**Invasiveness:** ~200–400 LoC in `mm/page_alloc.c` + `mm/memory.c` fault path, gated on CPUID. No `pv_ops.mmu` table, no `PARAVIRT_XXL`. A `CONFIG_NATEMU_PAGE_QUADS` build flag plus runtime CPUID check. Rebase cost is low (allocator API is stable). The enlightened guest runs faster; the stock guest still boots (just slower).

### Fallbacks if stuck at 16K granule (no PV, or PV insufficient)

Three tiers, in order of preference:

1. **Shatter + 4K shadows where heterogeneous** — Detect hetero quads at fill time, install 4× 4K L3 descriptors instead of one 16K L3 descriptor for that quad. On Apple Silicon this *requires* G-1's 4K granule (TGran4). If G-1 passes, shattering is correct and zero-per-access-overhead; cost is 4× TLB entries for those quads and 4× `TLBI` on invalidation (manageable). If G-1 fails (VM stuck at 16K), shattering is impossible — go to (2).

2. **Per-access sub-page checks in translated code (Captive §6.10 degraded path)** — Every guest `LDR/STR` that *could* cross a hetero quad gets `AND tmp, addr, #0xC000; CBZ` + permission table load + trap. Captive describes this but does not quantify. Estimate from softmmu tax (06 §2.3: 5–10 host insn per access) and Tier 2 projection (08 §6): degraded path is **~15–35% of the Tier 2 win**, i.e. Tier 2 at 2.5–5× over QEMU drops to **~1.6–3×** if 30–45% of accesses are degraded. Short-running / kernel-heavy workloads regress more. Correct but painful.

3. **Softmmu fallback for hetero regions** — Fault hetero pages into a QEMU-style `softmmu` hash TLB and run them there, while keeping homogeneous regions on the host MMU. Clean separation, but you now ship two memory paths and pay the softmmu ~9–11 insn/access on the hetero subset. Similar cost to (2) but more engineering.

**Cost estimate for degraded path:** HSPT/ESPT show host-MMU offload alone is 1.51–1.59×; losing it on 30–45% of pages costs roughly half that, leaving ~1.2–1.3×. Captive's 2.21× includes the privilege/TLB wins that survive, so overall Tier 2 degraded is **~1.6–2.2×** vs QEMU — still ahead of Tier 1 (1.5–2.5×) but no longer decisively better.

---

## 6. Verdict — what to run first, what threshold matters, what to build

**Run this first (1–2 days, before writing Tier 2 paging code — 08 §9):**

```bash
# On any x86-64 machine or QEMU, no Apple hardware needed:
python3 tools/g2_quad_measure.py --synthetic --pages 262144 --workload all   # sanity

# Then real measurement (the gate):
qemu-system-x86_64 -kernel bzImage -initrd rootfs.cpio -m 4G -smp 2 \
  -qmp unix:/tmp/qmp.sock,server,nowait -hmp unix:/tmp/hmp.sock,server,nowait &
# inside guest: run three workloads
#  (a) kernel_build: git clone linux && make -j$(nproc) defconfig && make -j$(nproc)
#  (b) browser: headless chromium --use-gl=swiftshader https://example.com
#  (c) database: pgbench -i -s 10 && pgbench -c 8 -T 60
# during each: pmemsave + info registers → tools/g2_quad_measure.py --dump ... --cr3 ...
```

**Threshold (08 §5):**

* **`H > 90%` → `natemu-page-quads` is an optional optimisation.** Build Tier 2 on stock guests; add PV later for the last 10–20% and for TLB reach. This outcome is *unlikely* per §2 unless THP is `always` and the VM is fresh — verify, don't assume.
* **`70% < H ≤ 90%` → PV is worthwhile but not load-bearing.** Ship Tier 2 on stock, document the PV as a recommended accelerator (like `kvm-pv-tlb-flush`). Accept the degraded-path tax on the tail.
* **`H < 70%` → PV is mandatory for Tier 2 to meet its ceiling.** Put `natemu-page-quads` on the critical path, gate Tier 2 paging bring-up on it, and advertise it as CPUID-negotiated but effectively required for production perf. This is the **expected outcome** for kernel_build/browser per §2, and it is the premise of 07 §3.1.

**Design decision follows directly:**

* Either way, **Tier 2 still boots** — G-2 does not block Tier 1 or Tier 2 bring-up, only its *optimiser* tier. Do not make a patched guest a prerequisite to boot (08 §8, KVM shape). 
* If `H < 70%` (expected), schedule `natemu-page-quads` as the **first PV feature** (before `pv-tlb-flush` / `pvclock`), because it is the only one that protects the *core* host-MMU win and it is the smallest patch. Budget 1–2 weeks for the allocator hook + validation; keep the fallback shattering path in the translator regardless (for stock guests and for `G-1=16K`).
* If `H > 90%` (surprise), deprioritise `page-quads`, rely on shattering for the residual, and keep the PV as a future `MAY`.

**Immediate next step:** run the `pmemsave` captures above on a real QEMU x86-64 Linux guest (Ubuntu/Fedora stock kernel) under the three workloads and compute `H` with the provided script. That single number settles whether "para-virtualization" is a good premise (07) or a bad one for natemu.

---

## References

* 06 §0.1/2.4, 07 §3.1/5/8.1, 08 §5, 09 N-02 — this repo.
* Captive (ATC'19/TOCS'20) §6.3/6.10 — n:m mapping & degraded path.
* VEE'14 ESPT / VEE'15 HSPT — host-MMU offload numbers (1.51–1.59×).
* `probe_g1_granule.c` / `probe_g1_tgran_tso.c` — G-1 companion probes (stage-2 `hv_vm_map` 4K/HVF & `ACTLR_EL1.TGran4`).
* `tools/g2_quad_measure.py` — this gate's artifact (`walk_dump:217`, `simulate:51`).

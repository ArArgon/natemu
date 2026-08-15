# 26 — Guest Rates: the Missing Half of Every Cost Model

**Date:** 2026-08-15 · **Host:** Apple M2 Max, macOS 26.5.2 · **Guest:** Alpine 3.24.1, Linux 6.18.35 x86-64, QEMU 11.0.3 TCG (§1–§9 at `-smp 1`; §10 at 1/2/4 vCPUs)
**Instrument:** `probes/qemu_baseline/plugin_mmu.c` (TCG plugin) · `runmmu.py` (driver) · `analyze_mmu.py` (arithmetic)
**Raw:** `probes/qemu_baseline/results/mmu/snap.M3.*.txt` (§1–§9), `snap.L*.txt` (§10), `analysis.M3.txt`, `sweep.M3.log`

Every performance claim in `08`, `12`, `15`, `16` and `20` multiplies a carefully measured
*per-event cost* by an *assumed event rate*. The costs are known to three significant figures. The
rates were never measured. This document measures them, and the arithmetic changes one design
decision, confirms another, and corrects a third.

> **Labels.** `M` measured by this instrument today. `D` derived from `M` by arithmetic shown.
> `MODEL` produced by the plugin's simulation of a design that does not exist yet — the strongest
> claim available short of a prototype, and weaker than a measurement. `?` unresolved.

---

## 0. Bottom line

### 0.1 Read this before any number below

**Three limitations are individually capable of moving the headline by more than the headline
itself.** They are stated first, not buried in a closing section:

1. ~~Everything here is uniprocessor.~~ **CLOSED 2026-08-15 — see §10.** The `-smp 1`
   restriction was not "global unlocked state" as the plugin's header claimed; it was the harness
   forcing `-accel tcg,thread=single`, which corrupts the guest at `-smp > 1` under any nontrivial
   plugin. With `thread=multi` and a coarse lock the plugin runs multi-core, and the answer is
   **less bad than feared**: Policy A is 6.8–18.9% at 1–4 vCPUs, and 11.2–23.7% if every shadow
   invalidation pays doc 19's 8-vCPU broadcast cost. The predicted 25–30% did not materialise.
   **But a different limit did: the 256-ASID budget is exceeded at SMP** (520 live address spaces
   on `spawn400` at 2 vCPUs against a budget of 256).
2. **The guest never uses PCID.** `cr3w_noflush = 0` and `invpcid = 0` in **all five workloads**
   `M`. Linux has enabled PCID by default since 4.14, so every CR3 rate here comes from a
   configuration no shipping distribution runs. See §6 — this is also an unexplored design
   opportunity, not only a caveat. `?`
3. **The denominator is a synthetic microbenchmark.** §4 divides by guest instructions ×
   0.249–0.258 ns, which `22 §1.1` measured on an ALU+memory loop with no branch or I-cache
   pressure. Real translated code is slower, so the true percentages are **lower** than reported
   here by an unknown factor, plausibly ~2×. The percentages are pessimistic, not optimistic. `?`

### 0.2 What the rates say

**The shadow-MMU maintenance cost is small on every workload measured, provided the retention
policy is right — and catastrophic if it is not.** `MODEL`, five workloads:

| Workload | Ginsns | **Policy A/C** | **Policy B** | B/A fill ratio |
|---|---|---|---|---|
| boot | 1.22 | **8.7%** | 42.3% | 6.04× |
| spawn400 (`fork`+`exec` ×400) | 1.66 | **10.4%** | 72.7% | 8.41× |
| buildproxy (proc churn + page cache) | 3.71 | **5.2%** | **101.2%** | 21.90× |
| bench (ALU/memory microbench) | 45.00 | **0.7%** | 1.7% | 2.09× |
| 7-Zip LZMA | 35.18 | **0.4%** | 1.6% | 4.84× |

Policy A keeps a shadow per guest address space forever; Policy B keeps one and wipes it on every
CR3 load; Policy C keeps at most 256, LRU. **Flushing on context switch doubles the runtime of a
build-like workload.** Keeping per-address-space shadows costs 0.4–10.4%.

**Policy C is identical to Policy A on every workload, because it never evicted once** `M`. Peak
live address spaces was **112** against the 256-entry budget the host's `ASIDBits` allows. The
simple design fits in hardware — with only 2.3× headroom, and its eviction path therefore
**untested**. See §5.

**On 7-Zip — the application `15 §8a` used to put QEMU's software MMU at 54.5% of host time — the
modelled hardware-stage-1 maintenance cost is 0.4%.** The denominators differ (share of QEMU host
time versus share of modelled natemu execution), so this is not a clean 136× ratio, but it is the
strongest quantitative support yet for `23 §0.4`'s claim that the MMU half of the win is available
without touching the JIT.

### 0.3 What this corrects in doc 20

`20` recommended lazy resync over write-protection, on the argument that guest PTE writes outnumber
the guest's own `INVLPG`/`CR3` announcements by **30–110×**. It never measured PTE writes; it
inferred 500–2000 per `fork`+`exec`.

Measured, per `fork`+`exec` (spawn400 minus boot baseline) `D`:

| Quantity | Measured | `20` said |
|---|---|---|
| `INVLPG` | **19.5** | 18.1 ✓ |
| `MOV CR3` | **9.4** | 6.5 ✓ |
| guest exceptions | **59.4** | 59.3 (its own `/proc/vmstat`) ✓ |
| **actual PTE writes** | **42.3** | *inferred 500–2000* ✗ |
| `clear_page` zeroing of fresh PT pages | **430.9** | not considered |
| **total write-protect traps** | **473.1** | — |
| lazy events (`INVLPG` + `CR3`) | **28.9** | 24.6 ✓ |
| **WP / lazy event ratio** | **16.4×** | 30–110× ✗ |

Two corrections and a vindication:

* **`20`'s inferred PTE-write count was 12–47× too high.** Real page-table *entry* writes are 42.3
  per spawn, not 500–2000.
* **`20`'s conclusion is nonetheless right, for a mechanism it did not identify.** When Linux takes
  a fresh page to use *as* a page table, `clear_page()` zeroes all 4 KiB of it — 512 stores that
  land in a page-table frame and that a write-protect design must trap. That contributes 430.9 of
  the 473.1 traps per spawn. `20`'s 500–2000 guess happens to bracket 473 — the right answer by the
  wrong route.
* **The ratio is not one number.** It spans three orders of magnitude across workloads (§2): 2.11×
  on boot, 5.74× on spawn, 5.39× on buildproxy, **346×** on 7-Zip and **1395×** on the memory
  microbenchmark. `20` quoted a single 30–110× band. Lazy resync wins everywhere, but by a margin
  that varies with workload far more than `20` allowed.

---

## 1. Guest rates

`M`. Events per 10⁹ guest instructions, so workloads of different length are comparable.

| Workload | Ginsns | CR3 wr | `INVLPG` | exceptions | interrupts | PTE writes | `clear_page` zeros |
|---|---|---|---|---|---|---|---|
| boot | 1.22 | 16,437 | 32,691 | 2,209 | 14,361 | 3,058 | 66,054 |
| spawn400 | 1.66 | 14,327 | 28,685 | 15,928 | 31,550 | 12,431 | 152,326 |
| buildproxy | 3.71 | 11,240 | 12,908 | 6,970 | 23,928 | 3,694 | 65,894 |
| bench | 45.00 | **446** | **886** | 146 | 8,837 | 236,303 | 999,857 |
| 7-Zip | 35.18 | **570** | **1,138** | 130 | 8,777 | 35,347 | 358,633 |

The spread is the point. **Address-space activity varies 37× between process-churn workloads and
compute workloads** (16,437 vs 446 CR3 writes per 10⁹). Any cost model quoting a single
"CR3 switches per second" figure is describing one workload, not the guest.

Note the inversion in the last two columns: `bench` and 7-Zip have the *lowest* CR3 and `INVLPG`
rates and the *highest* PTE-write rates — they allocate and free large mappings inside one address
space rather than switching between many. This is exactly the regime where lazy resync wins by the
largest margin (§2), and it is the regime real applications spend their time in.

---

## 2. Page-table write traffic

`M`, with the heuristic caveat in §7.2.

| Workload | counted | + unwalked | = PTE total | zeroing | PTE/`INVLPG` | (PTE+zero)/`INVLPG` |
|---|---|---|---|---|---|---|
| boot | 1,148 | 2,575 | 3,723 | 80,421 | 0.09 | **2.11** |
| spawn400 | 17,003 | 3,624 | 20,627 | 252,761 | 0.43 | **5.74** |
| buildproxy | 10,675 | 3,043 | 13,718 | 244,720 | 0.29 | **5.39** |
| bench | 1,841 | 10,630,737 | 10,632,578 | 44,988,989 | 266.70 | **1395.18** |
| 7-Zip | 1,279 | 1,242,186 | 1,243,465 | 12,616,371 | 31.05 | **346.12** |

* **counted** — stores to a frame already identified as a page table.
* **unwalked** — PTE-shaped stores to a frame *before* it was linked into the hierarchy and became
  walkable. Recovering these was the instrument fix described in §7.1; without it this column reads
  zero and the whole table is wrong by up to 17.6×.
* **zeroing** — zero stores to such a frame, i.e. `clear_page()` on a freshly allocated table.

**A write-protect design traps on PTE + zeroing. A lazy design pays neither, and instead handles
`INVLPG` + CR3.** The last column is therefore the event-count ratio between the two designs, and
it never falls below 2.11×.

The `clear_page` contribution deserves naming because it is counter-intuitive: **on every workload
measured, most stores into page-table frames are zeroes, not page-table entries** — 95% on boot,
92% on spawn, 81% on bench. A write-protect scheme spends the large majority of its trap budget
watching the kernel zero memory it is about to fill in anyway.

---

## 3. Shadow-fill rates by retention policy

`MODEL`. Absolute fill counts over the whole run.

| Workload | A xlat | A perm | B xlat | B perm | C xlat | C perm | B/A | evictions |
|---|---|---|---|---|---|---|---|---|
| boot | 97,066 | 71,593 | 795,000 | 223,163 | 97,066 | 71,593 | 6.04 | **0** |
| spawn400 | 188,299 | 96,104 | 1,922,817 | 469,934 | 188,299 | 96,104 | 8.41 | **0** |
| buildproxy | 222,089 | 112,436 | 6,121,683 | 1,205,215 | 222,089 | 112,436 | **21.90** | **0** |
| bench | 413,200 | 384,197 | 1,122,019 | 547,847 | 413,200 | 384,197 | 2.09 | **0** |
| 7-Zip | 127,443 | 101,063 | 839,440 | 265,979 | 127,443 | 101,063 | 4.84 | **0** |

The B/A ratio tracks how much address-space *re-entry* a workload does. `buildproxy` — 527 retired
roots, constant process churn against a warm page cache — is the worst case at 21.9×, because every
returning process rebuilds a map it had already built. `bench`, which barely switches, is 2.09×.

---

## 4. The arithmetic

`D`, from `M` rates × costs measured elsewhere in this corpus. Every cost is traceable to the
document that measured it, and they are held in one dict in `analyze_mmu.py` rather than scattered.

| Cost | Value | Source |
|---|---|---|
| translated x86 instruction | 0.249–0.258 ns | `22 §1.1`, 5 runs |
| shadow fill, translation fault | 146 ns | `16` item 4 (full fill, no TLBI) |
| shadow fill, permission fault | 33.3–44.2 ns | `20` item 9 / `16` (146 / 3.3) |
| TLBI per page | 203–285 ns | `18 §6.2` / `20` |
| TLBI all, batched (`VMALLE1IS`) | 294 ns | `18 §6.2` |

| Workload | exec ms | A fill | A + inval | **A %** | A batched % | B + inval | **B %** | **C %** |
|---|---|---|---|---|---|---|---|---|
| boot | 309 | 16.9 | 26.8 | **8.7** | 7.4 | 130.6 | 42.3 | 8.7 |
| spawn400 | 421 | 31.2 | 43.9 | **10.4** | 9.1 | 305.9 | 72.7 | 10.4 |
| buildproxy | 941 | 36.8 | 49.1 | **5.2** | 5.2 | 952.7 | **101.2** | 5.2 |
| bench | 11,406 | 75.2 | 85.2 | **0.7** | 0.7 | 190.9 | 1.7 | 0.7 |
| 7-Zip | 8,918 | 22.5 | 32.4 | **0.4** | 0.3 | 138.8 | 1.6 | 0.4 |

**Invalidation is not free, and an earlier draft of this analysis omitted it entirely.** Filling a
shadow entry (invalid→valid) needs no TLBI — that is `16`'s "never TLBI on invalid→valid" rule,
worth 3×. But *dropping* one does, and `shadow_inval` counts exactly those: 40,246–51,841 per run
`M`. Including it moved Policy A from 3.9–7.4% to **5.2–10.4%**, a 40% increase in the headline.

**Batching helps, modestly.** Accumulating invalidations and issuing one `TLBI VMALLE1IS` at the
next context switch — legal, because `VMALLE1IS` flushes the ARM TLB without destroying our shadow
tables — takes boot from 8.7% to 7.4% and spawn from 10.4% to 9.1%. Worth having, not decisive.

---

## 5. Footprint, and the ASID budget that was never tested

`M`.

| Workload | roots live | roots retired | AS live (peak) | PT frames | code pages | phys frames | shadow invals |
|---|---|---|---|---|---|---|---|
| boot | 4 | 43 | 31 | 85 | 3,491 | 59,725 | 40,246 |
| spawn400 | 3 | 848 | **112** | 81 | 4,544 | 59,716 | 51,841 |
| buildproxy | 4 | 527 | 105 | 81 | 5,554 | 59,778 | 50,647 |
| bench | 4 | 49 | 36 | 83 | 3,705 | 191,631 | 40,994 |
| 7-Zip | 4 | 49 | 31 | 85 | 3,792 | 73,867 | 40,549 |

`root_retire` = 848 over 400 spawns is 2.1 per `fork`+`exec`, exactly as expected (one address
space for the fork, one for the exec).

**The 256-ASID budget was never exercised.** Zero evictions on every workload, peak 112 live. That
is why Policy C is numerically identical to Policy A throughout — **not evidence that C works, but
evidence that C's eviction path never ran.** Headroom is only 2.3×, and a workload with more
concurrency would cross it, at which point C degrades toward B and B is the 101% case. `?`

---

## 6. The guest is not using PCID, and nobody in this corpus has asked about it

`M`: `cr3w_noflush = 0` and `invpcid = 0` across all five workloads. Every CR3 write in this data
is a full-flush CR3 write.

Linux has enabled PCID by default since 4.14. Two consequences:

* **As a caveat:** these CR3 and `INVLPG` rates come from a guest configuration no shipping
  distribution runs. Whether that biases the rates up or down is not established.
* **As an opportunity, and this is the larger point:** x86 PCID and ARM ASID are the same idea —
  a tag that lets an address-space switch avoid flushing the TLB. `20` reasons carefully about ARM
  ASIDs (items 17–21, the global/non-global TTBR1 result) and `18 §6.1` establishes that ARM ASID
  scoping is precise. But **no document in this corpus asks what the guest's own PCID should map
  onto.** If a guest PCID can be carried into an ARM ASID, a large fraction of the shadow
  invalidation priced in §4 — and the multi-core TLBI broadcast that §0.1 item 1 warns about — may
  simply not need to happen.

That is the most promising unexplored design direction this measurement turned up, and it is
cheap to investigate: re-run with PCID enabled and compare `cr3w_noflush` against `cr3w`. `?`

---

## 7. Instrument: what was wrong with it, and why you should believe it now

### 7.1 Two bugs, found by running it rather than reading it

**The page-table write counter was missing 94% of its events.** The store handler dropped any store
to a frame not *already* identified as a page table. Linux allocates a page, populates it with
PTEs, and only then links it into the hierarchy, so for `fork`+`exec` most PTE stores happen before
the frame is walkable. `ptwrite_unwalked`, the counter that would have flagged this, was declared
and printed but **never incremented anywhere** — a stub. Measured after wiring it up: 16,539
counted against 274,161 missed, a **17.6× undercount**.

Had the sweep run on the original instrument, this document would have reported PT writes at 0.36×
the `INVLPG` rate and **inverted `20`'s central claim** on the strength of an artifact.

**The first fix was itself misleading.** Credited writes came to 685 per address space; checked
against `root_retire` that is 1,964 per retired root — **3.8 × 512**, one `clear_page()` per paging
level. Zeroing dominates, and folding it into "PT writes" overstates the PTE update rate by more
than 10×. The two are now split, because they are different events for the design (§2).

### 7.2 What is measured, what is bounded, what is modelled

* **Measured** — `INVLPG`, CR3, CR4, exceptions, interrupts, instruction counts, `clear_page`
  zeroing, live/retired roots. Direct architectural observation of the guest.
* **Bounded** — PTE writes. `ptwrite` is a lower bound; `ptwrite + ptwrite_unwalked` an upper
  bound. Which stores get *remembered* before promotion is decided by a heuristic (aligned 64-bit
  store, value zero or P=1 with an output address inside guest RAM), because probing a side table
  on all 448 M stores in a spawn run would dominate the runtime. The gate that decides which
  remembered stores get *counted* — promotion of the frame to a page table — is structural, not
  heuristic: a frame that never becomes a page table never contributes. **`?` The heuristic misses
  non-present PTE writes** (Linux swap entries have P=0 and otherwise arbitrary bits), so the
  "upper bound" is not provably one. Closing experiment: one workload with the filter disabled.
* **Modelled** — everything about the three retention policies, the fill counts, and §4's
  percentages. These are a simulation of a design that does not exist.

### 7.3 Three independent validations

| Check | Result |
|---|---|
| Modelled page walk vs QEMU's own translation | **64/64 agree, 0 mismatches**, every workload `M` |
| Exceptions per `fork`+`exec` vs `20`'s guest `/proc/vmstat` | **59.4 vs 59.3** — 0.2%, unrelated measurement paths |
| `INVLPG` per `fork`+`exec` vs `20`'s independent plugin | **19.5 vs 18.1** |
| Run-to-run reproducibility (boot × 2) | **<0.5% on every counter** |

---

## 8. Open items

| # | Item | Closing experiment |
|---|---|---|
| 1 | ~~SMP rates are entirely unmeasured~~ **CLOSED** (§10). Policy A is 6.8–18.9%, or 11.2–23.7% charging every invalidation at doc 19's broadcast cost. The predicted ~30% did not materialise | — |
| 1b | **NEW, and now the binding constraint: the 256-ASID budget is exceeded at SMP.** `spawn400` at 2 vCPUs peaks at **520** live address spaces (§10.1 finding 4), against ARM's 256. Policy C's eviction path is load-bearing and still untested | Model eviction at 256 with the measured working sets; then decide between a generation/rollover scheme and pinning a subset. This is the top open item in this document |
| 1c | The page-table walker is still single-root and segfaults at `-smp ≥ 3`, so §2's PT-write traffic is uniprocessor-only | Make `pt_ref`/`pt_unref` multi-root safe (a frame reachable from several CR3s), then re-run §2 at SMP |
| 2 | **PCID is off; PCID→ASID is unexplored** (§6) | Re-run with PCID enabled, compare `cr3w_noflush`/`cr3w`. Then ask whether a guest PCID can be carried into an ARM ASID |
| 3 | **The denominator is synthetic** (§0.1 item 3) | Derive ns-per-guest-instruction from a real execution measurement rather than `22`'s ALU loop |
| 4 | The PTE-write upper bound is not provably one (§7.2) | One workload with the value filter disabled |
| 5 | **Policy C's eviction path never ran** (§5) | A workload with >256 concurrent address spaces; characterise the cliff |
| 6 | Fills charged flat at 146 ns; building intermediate table levels is not priced | Instrument `shl1/shl2/shl3` transitions and charge them separately |
| 7 | All five workloads are initramfs toys — no real filesystem, no mmap-heavy application, no swap | A guest with a real root filesystem and a desktop-class or database workload |

---

## 9. Reproduce

```sh
cd probes/qemu_baseline
cc -O2 -shared -fPIC -o bin/plugin_mmu.so plugin_mmu.c \
   -I/opt/homebrew/include $(pkg-config --cflags glib-2.0) -undefined dynamic_lookup
./runmmu.py --task none        --label M3.boot
./runmmu.py --task spawn400    --label M3.spawn
./runmmu.py --task buildproxy  --label M3.buildproxy
./runmmu.py --task bench       --label M3.bench
./runmmu.py --task sevenzip:1  --label M3.sevenzip
./analyze_mmu.py results/mmu/snap.M3.*.txt
```

Needs `probes/qemu_baseline/work/` (fetched by that probe's `build.sh`) and QEMU 11.0.3.
Whole sweep is ~17 minutes on an M2 Max.

## 10. Multi-core (added 2026-08-15)

`M`, `-accel tcg,thread=multi`, coarse-locked plugin, `--pt 0` (the page-table walker is still
single-root only, §8 item 8). All six runs reached 123 NATEMU markers, so all six guests are
healthy — the check that invalidated an earlier attempt.

| Run | vCPU | Ginsns | CR3 wr | `INVLPG` | fill A | fill B | B/A | shadow inval | **AS live** |
|---|---|---|---|---|---|---|---|---|---|
| boot | 1 | 1.21 | 20,009 | 39,812 | 164,732 | 1,005,732 | 6.11 | 40,218 | 26 |
| boot | 2 | 1.27 | **456** | 39,866 | 181,303 | 310,585 | 1.71 | 39,866 | 45 |
| boot | 4 | 1.64 | **668** | 40,055 | 184,184 | 433,756 | 2.36 | 40,055 | 37 |
| spawn400 | 1 | 1.63 | 23,114 | 47,153 | 305,084 | 2,209,714 | 7.24 | 47,153 | 133 |
| spawn400 | 2 | 1.80 | **2,673** | 47,550 | 642,821 | 1,514,882 | 2.36 | 47,550 | **520** |
| spawn400 | 4 | 2.84 | **6,127** | 48,531 | 581,022 | 3,127,693 | 5.38 | 48,531 | **317** |

### 10.1 Four findings

**1. CR3 writes collapse 8–44× at SMP; `INVLPG` does not move at all.** Boot goes 20,009 → 456;
`INVLPG` goes 39,812 → 40,055 (+0.6%). This reproduces `20`'s independent smp4 observation
(cr3_w 20,009 → 460) on a healthy guest, so it is real guest behaviour: at one vCPU the single CPU
must reload CR3 constantly to time-slice every runnable task, while at more vCPUs tasks run
concurrently and idle CPUs sit in lazy-TLB mode. **Per-page invalidation work is unaffected by core
count — only address-space switching is.**

**2. Policy B's disadvantage largely evaporates.** B/A falls from 6.11 to 1.71–2.36 on boot and
from 7.24 to 2.36–5.38 on spawn, because B's cost is driven by CR3 loads and those collapse.
**§0.2's headline — that flushing on context switch doubles a build-like workload — is a
uniprocessor result.** The recommendation does not change (A is still better everywhere) but the
margin at one core is the worst case, not the typical one.

**3. The feared multi-core blow-up did not happen.** Charging *every* shadow invalidation at doc
19's 8-vCPU broadcast cost (700 ns, from its 441–961 ns band) instead of 244 ns:

| Run | exec ms | fill ms | inval @1 core | **A %** | inval @broadcast | **A % broadcast** |
|---|---|---|---|---|---|---|
| boot ×1 | 308 | 16.4 | 9.8 | 8.5 | 28.2 | **14.5** |
| boot ×4 | 415 | 18.6 | 9.8 | 6.8 | 28.0 | **11.2** |
| spawn ×1 | 412 | 33.9 | 11.5 | 11.0 | 33.0 | **16.2** |
| spawn ×2 | 457 | 74.9 | 11.6 | 18.9 | 33.3 | **23.7** |
| spawn ×4 | 719 | 65.4 | 11.8 | 10.8 | 34.0 | **13.8** |

§0.1 predicted 25–30% for spawn at SMP. The measured worst case is **23.7%**, and only at 2 vCPUs;
at 4 it falls to 13.8% because instruction count grows faster than invalidation count. The
prediction was pessimistic but the right order of magnitude.

**4. The new binding constraint is the ASID budget, not time.** `spawn400` at 2 vCPUs peaks at
**520 live address spaces against ARM's 256** (`ID_AA64MMFR0_EL1.ASIDBits = 0` → 8-bit ASID,
confirmed from the G-1 probe). §5 reported peak 112 at one vCPU with 2.3× headroom and flagged that
Policy C's eviction path was untested. **At SMP the budget is exceeded by 2×, so that path is now
load-bearing and still untested.** This is the most important thing this section found.

### 10.2 What is still not measured

The page-table walker (`--pt 1`) remains single-root only and segfaults at `-smp ≥ 3`, so §2's
PT-write traffic is uniprocessor-only. Fill and invalidation rates above are unaffected — they come
from the shadow model, which is now multi-core correct.

---

## References

`08` feasibility synthesis (the cost model this supplies rates for); `12` performance ranking;
`15` QEMU TCG baseline (§8a's 54.5% softmmu share, and the 7-Zip workload reused here);
`16` shadow-MMU costs (fill and TLBI costs, the never-TLBI-on-invalid→valid rule);
`18` EL1 capability surface (§6.2 TLBI costs per variant, and §0.2's sampling caveat);
`19` SMP scaling (the multi-core TLBI figures that make §0.1 item 1 the priority);
`20` shadow-PT coherence (the document this corrects and confirms);
`22` async interrupt injection (§1.1's translated-instruction cost, the denominator here);
`23` FEXCore retrofit (§0.4's "the MMU half is available without touching the JIT").

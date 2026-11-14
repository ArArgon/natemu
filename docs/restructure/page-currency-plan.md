# Page-level currency: one fetch walk per (epoch, page) instead of one per block

Status: design, not built (November 14, 2026), branch `perf-real-workload`; roadmap item 6 of the
[architecture review](architecture-review-20261113.md) (root cause A), ranked first for configure by the census
([io5-perf-notes](io5-perf-notes.md) decision 15). Line numbers are those of `bcc164f`.

## 1. The problem in one paragraph

A block is "current" when a fetch walk found its source pages at its linear address in the present TLB epoch. Every
CR3 load moves the epoch, so a new process revalidates every block it touches, each with its own walk. The census
counted 22.7 M revalidations on configure and 53.9 M on make, but only 2.54 M / 4.65 M distinct (epoch, page) pairs
among them (11.2% / 8.6%): the same page is walked about nine to eleven times per epoch, once per block. A hardware TLB
walks a page once and serves every fetch on it from the entry. This plan does the same: the result of a fetch walk is
recorded per (epoch, linear page, privilege class), and later blocks on that page are revalidated from the record
without a walk. Placement walks use the same record.

## 2. What "current" means today, and what a revalidation checks

- **Currency** (`cache::current`, `crates/runtime/src/cache.rs:555-558`): `b.checked == c.epoch`, or for a `GLOBAL`
  block `b.checked >= c.global_epoch`. `checked` is written by placement (`place`, `:752`; `copy`, `:1163`;
  `instance`; `place_system`, `:1211`) and by `lookup`'s revalidation (`:626`); `cut_one` clears it to 0 (`:2251`), the
  "never current" value, which relies on `epoch >= 1` (`reset`, `:217`).
- **The epoch** (`cache::tlb`, `:648-652`): `epoch += 1` at every CR3 load (`execution.rs:2361`, a load of the same
  value too) and every global flush, which also sets `global_epoch = epoch`: CR0.PG/WP, the paging bits of CR4
  (`CR4_PAGING`, `:2043`), EFER.NXE/LME/LMA, MTRR writes (`commit(.., flush_all)`, `:2606`), and the host's
  `tables_written` after a device wrote a page-table page (`io_checkpoint`, `:1462-1466`). Capacity flushes keep the
  epoch (`reset`, `cache.rs:217`).
- **A revalidation** (`lookup`, `cache.rs:594-644`; `mapped`, `execution.rs:1220-1225`, bucket `revalidate`;
  `revalidated`, `:1226-1237`): for each source page k of the block, `fetch_walk((rip & !4095) + 4096 k)` must return
  the block's `source_gpa[k]`. `fetch_walk` (`:1192-1216`) is `paging::walk` for `Access::Fetch` under the vCPU's
  paging controls, which include the CPL (CPL 3 needs U/S at every level; CPL 0 with SMEP faults on a user page; NXE:
  `crates/abi/src/paging.rs:199-263`), through the `CodeWrites(FetchWrites)` observer that marks each table page read
  (`table_read`) and turns the walk's A-bit writes into code-page invalidations (`:1255-1274`). The leaf flags are
  folded by `leaves` (`:1240-1243`): `GLOBAL` only if CR4.PGE and every leaf has G; `LARGE` if any leaf is above 4 KiB.
  `lookup` then cuts an instance whose walk found a global mapping (`:616-623`), else writes `checked = epoch` and the
  `GLOBAL | LARGE` flags (`:626-633`, `mark_large` for `LARGE`). A walk that fails or finds another frame counts
  `remapped` and the probe moves on (`:640`).
- **A placement** walks the same way: `fetch_first` (`execution.rs:817-825`) walks RIP's page through `fetch_page`
  (`:1173-1186`, bucket `fetch`), which also marks the frame as a code page (`mark`, `:1284`); `ask` walks the next page
  when the host asks (`:826-845`); in-VM copies and instances use the same `s.fetch` and `fetched` (`reused`, `:912`;
  `walked`, `:1246-1254`). The `fetch` bucket (13.4 M / 12.1 M events) is these walks: about one per placement or copy.
- **Cost** (`natemu-raw/chunk1b-profile-1`, sums over four vCPUs): `revalidate` 2.44 s for 23.0 M events (106 ns) on
  configure, 6.76 s for 55.0 M (123 ns) on make; `fetch` 1.86 s / 13.4 M (139 ns) and 1.71 s / 12.1 M (142 ns). The
  `lookup` bucket (L2 probe and `current`, 28-36 ns) is not a walk and is not claimed here.

## 3. Design

### 3.1 The record

A per-vCPU direct-mapped table of translations found by fetch walks, in the lookup area after `PIC_FRAMES`
(`area::PAGE_STAMPS`, after `crates/abi/src/execution.rs:340-341`; `LOOKUP_BYTES`, `:278`, grows by it): 8,192 entries of 24 bytes
(192 KiB per vCPU; section 5 sizes it).

- `key: u64` = leaf base page number `<< 2 | large << 1 | user`. The base is the 4 KiB page for a 4 KiB leaf, the first
  page of the 2 MiB region for a 2 MiB leaf. `user` is the privilege class of the walk (CPL 3 or not), because the
  fetch walk's outcome depends on it (section 2). 1 GiB leaves are not recorded (counted).
- `epoch: u64`: the `Control::epoch` of the walk. 0 means empty (never matches: `epoch >= 1`, as `checked` already
  relies on).
- `frame: u64`: the leaf's guest-physical base, with the leaf's G bit in bit 3 (`cache::GLOBAL`'s value).
- Index: `(key * 0x9e37_79b9_7f4a_7c15) >> (64 - 13)`, as `lbucket` (`cache.rs:251-254`). A collision overwrites: a
  lost record costs one walk, never a wrong answer.

### 3.2 The probe, in one place

A function `page_translation(s, va) -> Result<(gpa, flags), fault>` in `runtime/src/execution.rs` wraps `fetch_walk`
and replaces its two calls (`revalidated`, `fetch_page`). With `user = cpl == 3`:

1. Probe the 4 KiB key of `va`. A record hits when its key matches and `epoch == c.epoch`, or its G bit is set,
   CR4.PGE is set and `epoch >= c.global_epoch` (the rule of `current` for `GLOBAL` blocks: a PGE change is a global
   flush, so PGE at the probe equals PGE at the walk). Result: `(frame, G ? GLOBAL : 0)`.
2. Else probe the 2 MiB key of `va`'s region, the same test. Result: `(frame + (va & 0x1ff000), flags | LARGE)`.
3. Else `fetch_walk`. On `Ok`, record under the key of the leaf's size (4 KiB or 2 MiB; a 1 GiB leaf records nothing)
   with `epoch = c.epoch`; `fetch_walk` returns the leaf size for this. Faults and non-RAM results record nothing, as
   hardware caches no fault.

Everything above the probe is unchanged: `revalidated` still compares each page's result with `source_gpa[k]` and
folds flags with `leaves`; `lookup` still writes `checked`, the flags and `mark_large`; `fetch_page` still marks the
frame (`mark`) for a block request, since the mark is about bytes, not mappings. `cache.rs` changes only in `invlpg`.

**How blocks become current without a walk.** Currency is logically per page from the moment the page is recorded:
every block whose pages are recorded with its frames in this epoch is as current as the hardware's fetch would make
it. The block's own `checked` stamp is refreshed lazily, at its next `lookup`, from the record: one probe and one
store, on a path that has already paid a dispatcher miss and an L2 probe for that very block. No block that does not
run is touched (section 7 rejects eager stamping). So "per-block work" shrinks from a 4-5 level walk with an observer
to a 24-byte compare.

**Crossing blocks** (1.0% / 1.3% of revalidations) probe each page on its own: a block over P1 and P2 is current only
when both records hit with its two frames (or the missing one is walked and matches). A block over a 4 KiB page and a
2 MiB page uses one key of each kind. Nothing special is stored for crossing: the record is per leaf, as a TLB is.

### 3.3 Invalidation

| Event | Record | Why |
| --- | --- | --- |
| MOV CR3 (`cache::tlb(false)`) | nothing: `epoch` moved | every non-global record is stale by its epoch; global ones stay by the `global_epoch` rule, as global blocks do |
| Global flush (`tlb(true)`; controls, MTRR, `tables_written`) | nothing: `global_epoch = epoch` | every record is stale, G or not |
| INVLPG m, #PF at m (`cache::invlpg`, `cache.rs:2122-2154`, from `page_faulted` `:1368-1380` and the instruction `:2492`) | clear the 4 KiB record of m's page and the 2 MiB record of m's region, both privilege keys: four probes, `epoch = 0` on a key match | SDM Vol.3 4.10.4.1: the entries for the page, and every entry of a larger page containing it. A record built from a 2 MiB leaf is only ever under the 2 MiB key (3.2 step 3), so no sweep of the region's 512 pages is needed |
| Code write to a frame (`cache::written`, `:1995`; peers' posts) | nothing | the record maps a linear page to a frame; bytes are the frame-keyed code-page protocol's business. A new block placed at that page fetches the frame's present bytes, marked before the host copies them, as today |
| Guest PTE write without a flush (this vCPU or a peer) | nothing | a stale TLB entry is permitted until the guest invalidates it; today's `checked` has the same semantics, and natemu already keeps blocks current across such writes (`no-invlpg`, `place-global`, `invlpg-page` programs) |
| Capacity flush (`cache::reset`) | nothing | blocks go, translations do not; the epoch is kept |
| A walk's A-bit write | happens at the recording walk only | a hit sets nothing, as a TLB hit sets nothing; a later walk in the same epoch would find A set unless the guest cleared it without a flush, when hardware with the entry would also not set it |

### 3.4 Multiple vCPUs, epochs, controls

- The table is per vCPU, like `epoch`, `checked` and the shadows. Peers reach it only through memory: their PTE edits
  are visible at this vCPU's next INVLPG, #PF or CR3 load (the guest's shootdown IPI leads to one); their DMA into table
  pages ends in `tables_written`, a global flush on every resident.
- Epochs are `u64`, monotonic for the vCPU's life (`reset` keeps them); records hold the full value, so no aliasing.
  At a million events per second the counter wraps after 5.8 x 10^5 years: not handled, stated in a comment.
- Every paging control the walk reads (CR0.PG/WP, CR4.PAE/PGE/SMEP/LA57, EFER.NXE/LMA, CR3) is fixed within an epoch
  because changing any of them moves it (section 2). The CPL is not a control and does not flush; it is in the key.
  SMAP concerns data only. PAT and MTRRs affect memory type, which the record does not carry.
- Check mode: `Control::pages` = `PAGES_OFF | PAGES_ON | PAGES_CHECK`, set by the host from `--page-currency` as
  `Control::reuse` is (`REUSE_CHECK` precedent). In `PAGES_CHECK` every hit is followed by the walk; a different frame
  or flag counts `page_disagree` and, in K1 runs, halts (`stop::PAGE_CHECK`). `PAGES_OFF` is today's path, kept until
  the standing suite passes with the record on.

## 4. Correctness argument and the invariants it rests on

Claim: a block revalidated or placed from a record runs only when a CPU with a TLB entry for its page, filled at the
recording walk and not since invalidated, would fetch the same bytes.

- **I1 (recording).** A record (K, e, F) exists only if a fetch walk on this vCPU, at epoch e, under the privilege class
  in K, returned leaf base F for K's range, and no INVLPG or #PF of an address in K's range has happened on this vCPU
  since (3.3). Records are written by `page_translation` alone and cleared by `invlpg` alone.
- **I2 (granule).** A record's key granule is the leaf size the walk found, so an INVLPG anywhere in a 2 MiB leaf's
  range finds its record under the region key, and a 4 KiB record never stands for a larger leaf.
- **I3 (epoch).** Every event that may change what a walk returns for a page, other than a change the guest must
  follow with INVLPG, #PF or a CR3 load, moves `epoch` (section 2's list); a global flush also moves `global_epoch`.
  Within one epoch the paging controls and CR3 are constant.
- **I4 (hit).** A hit at epoch e' needs `e == e'`, or G set, CR4.PGE set and `e >= global_epoch`. In the first case
  the record is a TLB entry filled in this address space under these controls and not invalidated (I1, I3). In the
  second, the entry is global and was filled after the last event that invalidates global entries; CR3 loads since do
  not drop it (SDM Vol.3 4.10.4.1), INVLPG and #PF would have cleared it (I1).
- **I5 (block).** `revalidated` accepts a block only if each of its pages' results equals the block's recorded frame
  for that page, so the block's bytes are the bytes at the translation the entry gives; `leaves` folds the flags as
  before. A crossing block needs both pages (3.2). A mismatch is a `remapped` block, as today.
- **I6 (bytes).** Which bytes a frame holds is the frame-keyed code-page protocol's invariant (every live block's
  source pages carry this vCPU's mark or a write since posted the page), untouched by this plan; a placement from a
  recorded frame still marks it before the host copies.
- **I7 (A/D).** A recorded walk set the A bits hardware would set at the fill; a hit sets none, as a TLB hit (3.3).
- **I8 (independence of the rest).** `current`, `cut_one`, the L1 passes, `prune_tlb`, the link invariant and the
  relink plan's "becomes current only in `lookup`" (`relink-plan.md` section 2) all read `checked`, which this plan
  writes exactly where and when it is written today; only the answer of `mapped` changes.

Two cases where the record answers differently from a walk, both permitted: a PTE changed and not invalidated (hit
gives the old frame, the walk the new; today's `checked` gives the old one too, and `no-invlpg` asserts either), and the
mirror case where a later block's walk already recorded the new frame and an earlier block of the old frame is now
refused (a hardware fetch through the entry would run the new frame's bytes). The guest may see fewer cached
translations than hardware could hold, never one hardware could not hold.

## 5. Cost per event (estimates; the probe cost is measured in chunk 1)

| Event | Added | Removed |
| --- | --- | --- |
| Revalidation of a block on a recorded page | 1 probe (hash, 24-byte load, compare; the table is 192 KiB, so a core data-cache hit): ~10-20 ns; a crossing block 2 | the walk: 106-123 ns per `revalidate` event |
| First revalidation of a page in an epoch | 1 probe + 1 store | nothing |
| Placement or copy on a recorded page | 1 probe | the walk: 139-142 ns per `fetch` event |
| INVLPG, #PF | 4 probes, 0-2 stores: ~30-60 ns on an event of about 1-3 us (`invlpg_ticks`, `page_fault_ticks`) | nothing |
| CR3 load, global flush, capacity flush, code write | nothing | nothing |
| Memory | 192 KiB per vCPU, touched lazily | |

Table size: distinct pages per epoch average about 50 on configure (2.54 M / 51 k epochs) and 130 on make; cc1 holds
a few thousand text pages. 8,192 direct-mapped entries keep collisions rare at those loads; chunk 0 counts evictions
of live records and halves or doubles the table on the count.

## 6. Expected gain (estimate; arithmetic beside each figure)

Walks removed: revalidations x (1 - distinct share), i.e. 22.7 M x 0.888 = 20.2 M on configure, 53.9 M x 0.914 =
49.2 M on make (the census's distinct count is an upper bound of walks still needed, so these are lower bounds on
walks removed). Placement walks hit at an unmeasured rate h: a new block or copy lands on a page already recorded this
epoch; shared-library copies after an ASLR'd exec are the likely bulk. h = 0.5-0.8 assumed until chunk 0 counts it.

- configure: `revalidate` 2.44 x 0.888 = 2.17 s, less 22.7 M probes x 15 ns = 0.34 s; `fetch` 1.86 x h = 0.93-1.49 s,
  less 13.4 M x 15 ns = 0.20 s. Net **2.5-3.1 s of 117 busy vCPU-s (2.1-2.6%)**, about the same in wall time
  (configure is serial): 76.7 -> about 74 s.
- make: 6.76 x 0.914 = 6.18 less 54 M x 15 ns = 0.81; 1.71 x h = 0.86-1.37 less 0.18. Net **6.0-6.6 vCPU-s of 573
  (1.0-1.2%)**, about 1.5 s of 139 s wall.

Below the review's 3-4 s / 8-12 because the probes are now costed and `lookup` is not claimed. Not counted here, for
item 2 (fast miss path): a miss that "needs revalidation" (54.4% / 29.0% of misses: 17.4 M / 37.8 M) and hits the
record needs no walk, so no fault, no A-bit write and no host request can follow, which makes it eligible for the
fast path; at about 0.14 us saved per such miss that is another ~2.2 s on configure and ~4.8 vCPU-s on make, credited
to item 2's gate.

## 7. What was rejected and why

1. **Read-through currency** (a block's `checked` replaced by its page's record, so no per-block store at all): puts
   two dependent probes into `current`, which the CR3 load runs over every record of the CR3 link segment
   (`prune_tlb`, `cache.rs:2299-2321`) and every listed L1 entry (`:671-698`), and `sys_fast` runs in `current_at`;
   the `GLOBAL | LARGE` flags would be recomputed at every use. The lazy stamp costs one store on a path that already
   took a trap for that block.
2. **Eager stamping or L1 refill of every block on a recorded page** (walk once, then `on_linear_page` over the page's
   chain): about 9 blocks are revalidated per page-epoch, while a translated 4 KiB page holds tens to a hundred live
   blocks, most of which will not run in a short process; the linear chains are shared by hash bucket (65,536
   buckets, `area::LINEAR_BUCKETS`), so the scan is longer than the page. Lazy stamping touches only blocks that run.
3. **The data shadow tables as the record**: a fetch installs no shadow (code runs from the code area), so code pages
   rarely have one; shadows carry neither G nor the leaf size; the two privilege spaces would need two lookups; an
   install costs about 300 ns against a 24-byte store.
4. **Keeping dispatcher L1 entries across CR3 loads** so the miss itself is saved: needs a tag check on every entry
   (rejected after measurement, decision 9) or retained roots (decision 8).
5. **A 512-slot sweep at INVLPG for 2 MiB leaves** (the `large_held` pattern of `cache::invlpg`): replaced by keying
   records by leaf size, which is the TLB's own structure and makes INVLPG four probes.
6. **Recording faults** (negative entries): hardware caches none, and #PF delivery needs the walk for the error code.
7. **A 32-bit epoch in the record** (16-byte entries): aliasing after 2^32 epochs would be a silent wrong answer for
   64 KiB saved.
8. **Keying without the privilege class**: a supervisor fetch would hit a record a CPL 3 walk made and run a user page
   under SMEP, or a user fetch would run a supervisor page (`adv_setup`'s U/S cases).

## 8. Chunks, gates, tests, mutations

**Chunk 0: count (scratch perf build, no behaviour change; one Tier 2 profile scheduled by the main session).** Build
the table and the probe in shadow mode: probe, always walk, record from the walk, compare. Counters: hits by granule
and caller (revalidation / placement), walks, `page_disagree`, evictions of a live record, 1 GiB leaves met, the
largest number of live records in an epoch, and revalidations by block class (`GLOBAL`, `LARGE`, user). Gate: the
modelled net saving (hits x the measured per-event walk cost minus probes x 15 ns) is at least 2.0 s on configure or
5 vCPU-s on make, and `page_disagree` is zero in single-vCPU runs (K1; a 4-vCPU run may show a small count from PTE
edits that precede the shootdown IPI: report the rate). If the gate fails, stop; the counters stay in `io5-bins`.

**Chunk 1: build (S-M: about 150-250 lines; abi area constant and key helpers, `page_translation`, four clears in
`invlpg`, counters, the host flag; independent review before the gate).**
- Tier 0: abi unit tests for key packing and unpacking (page, large, user), the 2 MiB base and offset arithmetic, the
  hash range, "an empty record (epoch 0) never hits at any epoch", "a 2 MiB record never hits a 4 KiB key"; the
  mutations below, each run once against its test.
- Tier 1, new `run_k1.py` programs (each a few CR3 loads and calls, as the existing ones; K1 stays at its present
  length; QEMU-compared where the architecture fixes the answer, natemu-only otherwise):
  - `page-share` (QEMU): X and Y on one page V (two RIPs), A maps F1, B maps F2 with other bytes; A: X, Y; B: X, Y; A: X, Y.
    Each space's bytes every time; counts (natemu): one walk and one hit per (space, round).
  - `page-invlpg` (QEMU): A: X then Y (Y from the record); V's PTE -> F2, INVLPG V; Y runs F2's bytes.
  - `page-pf` (QEMU): as `pf-remap` with two blocks: V made not present, a load faults, the handler maps F2 with no
    INVLPG; Y runs F2's bytes (the #PF cleared the record).
  - `page-cross` (QEMU): W crosses P1 into P2, X lies on P2; B maps P1 to a third frame, P2 as A. B: X (records P2),
    then W: P1 must be walked and W replaced by the third frame's bytes.
  - `page-large` (natemu only; QEMU's jump cache fails `invlpg-large` already): X and Y in a 2 MiB page; X recorded;
    the PDE changed, INVLPG of another 4 KiB address of the page; Y runs the new frame's bytes.
  - `page-global` (natemu only): PGE on, V global and the same frame in A and B; A: X; B: Y hits across the CR3 load
    (count) with F1's bytes; then V's PTE -> F2 in B with no INVLPG and PGE toggled off and on: Y runs F2's bytes.
    Second part: PGE off, V with G set: A: X; B (V -> F2): Y runs F2's bytes.
  - `page-user` (natemu only, `adv_setup` style): V with U/S set, fetched at CPL 3 (recorded); SMEP on; a CPL 0 fetch
    of V must #PF (its own key misses, the walk faults).
  - `page-counts`: 32 blocks on one page in a fresh space: `page_walks` 1, `page_hits` 31.
  - Existing programs that must stay green, in particular `walk` (the recording walk's A-bit write still kills the
    block), `cross`, `straddle`, `cross-link`, `no-invlpg`, `global`, `place-global`, `invlpg-page`, `invlpg-large`,
    `pf-large`, `large-reval`, `local-gpa`, the `reuse-*` three, and the `adv_setup` set; all also under
    `--page-currency check` with zero disagreements.
- Tier 2 gate: `revalidate` + `fetch` down by at least 70% of chunk 0's model; `invlpg_ticks`, `page_fault_ticks` and
  `tlb` up by no more than half the gain; `remapped` within noise of the baseline. Wall time is judged in group F
  (items 6, 8, 9) per the review's rule.
- End of group: `run_io5.py run` (the CPython build and its tests) once with `--page-currency check`; the standing
  suite and benchmarks once before merge.

**Deliberate mutations, each turning its test red:**

| Mutation | Caught by |
| --- | --- |
| M1 hit ignores the frame compare in `revalidated` (any record hit makes the block current) | `page-share` (Y runs F1's bytes under B) |
| M2 record not checked against the epoch | `page-share`, `remap-cr3` |
| M3 table disabled (always walk) | `page-counts` |
| M4 `invlpg` leaves the 4 KiB record | `page-invlpg`, `page-pf`, `remap-invlpg` |
| M5 `invlpg` leaves the 2 MiB record, or a 2 MiB leaf recorded under its 4 KiB key | `page-large`, `invlpg-large`, `pf-large` |
| M6 a crossing block current on one page's hit | `page-cross`, `cross` |
| M7 global record kept across a global flush (`epoch >= 1` instead of `>= global_epoch`) | `page-global` first part |
| M8 global rule ignores CR4.PGE | `page-global` second part, `place-global` |
| M9 key without the privilege class | `page-user` |
| M10 hit skips `mark` in `fetch_page` | `reuse-write`, `dormant-write` (a write to the frame no longer posts) |
| M11 check mode not comparing flags | `large-reval` under `--page-currency check` (a 4 KiB record answering a large leaf) |

**Chunk 2 (not this plan): item 2's fast miss path takes record hits** (section 6). Designed with item 2.

## 9. Stop rule and risks

- Stop after chunk 0 if its gate fails; the counters stay. Revert chunk 1 if its Tier 2 gate fails after one round of
  diagnosis. Any K1 red, `BAD_LINK`, or a check-mode disagreement in a single-vCPU run (K1, Linux at N = 1) traced to
  the design rather than a slip stops the item for re-review of sections 3-4 (the retention plan's rule).
- Risks and open questions: the placement hit rate h is unmeasured (chunk 0); the share of revalidations that are
  kernel blocks on 2 MiB global pages after the 3.4-3.5 k global flushes per run decides how much the 2 MiB key
  matters, and whether a 1 GiB key is ever needed (chunk 0 counts both); table load on cc1-sized processes
  (evictions counted); check-mode disagreements at four vCPUs are expected to be nonzero and must be judged by rate;
  the hit test adds about ten instructions to a path the fast miss path will later want to classify without a full
  bank (item 2's design); a PTI kernel (not the measured configuration) is exact under this plan but gains less,
  since its kernel pages are non-global.

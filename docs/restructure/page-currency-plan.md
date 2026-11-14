# Page-level currency: one fetch walk per (epoch, page) instead of one per block

Status: design, not built (November 14, 2026), branch `perf-real-workload`; accepted with fixes after one independent
review (section 10). Roadmap item 6 of the [architecture review](architecture-review-20261113.md) (root cause A), ranked
first for configure by the census ([io5-perf-notes](io5-perf-notes.md) decision 15). Line numbers are those of `bcc164f`.

**Sequencing (reviewer and coordinator, November 14).** This item's own gain is about 1-2% of busy time. Its value is
that a dispatcher miss whose block needs revalidation (54.4% of configure's misses) can, once the page is recorded,
be served without a walk, so the [fast miss path](fast-miss-plan.md) (item 2) can take it. It is therefore built
immediately before item 2, and if chunk 0's corrected model misses its gate the record is folded into item 2's plan
as that path's revalidation source rather than dropped.

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
  epoch (`reset`, `cache.rs:217`). A change of CR4.PAE or LA57 is #GP (`:2365`), so the walk's shape is fixed.
- **A revalidation** (`lookup`, `cache.rs:594-644`; `mapped`, `execution.rs:1220-1225`, bucket `revalidate`;
  `revalidated`, `:1226-1237`): for each source page k of the block, `fetch_walk((rip & !4095) + 4096 k)` must return
  the block's `source_gpa[k]`. `fetch_walk` (`:1192-1216`) is `paging::walk` for `Access::Fetch` under the vCPU's
  paging controls, which include the CPL (CPL 3 needs U/S at every level; CPL 0 with SMEP faults on a user page; NXE:
  `crates/abi/src/paging.rs:199-263`; a PS bit at levels 3-4 is a reserved-bit fault, `:227`, so leaves are 4 KiB or
  2 MiB), through the `CodeWrites(FetchWrites)` observer that marks each table page read (`table_read`) and turns the
  walk's A-bit writes into code-page invalidations (`:1255-1274`); the leaf's permission check ends in an `sb` before the
  frame is returned (`paging.rs:236-241`, under `speculation_barriers`). `fetch_walk` accepts the result only if the 4 KiB
  page under `va` is guest RAM (`:1206`). The leaf flags are folded by `leaves` (`:1240-1243`): `GLOBAL` only if CR4.PGE
  and every leaf has G; `LARGE` if any leaf is 2 MiB. `lookup` then cuts an instance whose walk found a global mapping
  (`:616-623`), else writes `checked = epoch` and the `GLOBAL | LARGE` flags (`:626-633`, `mark_large` for `LARGE`). A
  walk that fails or finds another frame counts `remapped` and the probe moves on (`:640`).
- **A placement** walks the same way: `fetch_first` (`execution.rs:817-825`) walks RIP's page through `fetch_page`
  (`:1173-1186`, bucket `fetch`), which also marks the frame as a code page (`mark`, `:1284-1305`: an atomic `fetch_or`,
  a peer check, sometimes a shadow reset or shootdown); `ask` walks the next page when the host asks (`:826-845`); in-VM
  copies and instances use the same `s.fetch` and `fetched` (`reused`, `:912`; `walked`, `:1246-1254`). The `fetch`
  bucket (13.4 M / 12.0 M events) is these walks plus their marks: about one per placement or copy.
- **Cost** (`natemu-raw/group-r1-profile-2`, the current baseline, sums over four vCPUs): `revalidate` 2.43 s for
  23.1 M events (105 ns) on configure, 6.37 s for 52.2 M (122 ns) on make; `fetch` 1.85 s / 13.4 M (138 ns) and
  1.69 s / 12.0 M (141 ns). Both averages mix cold first walks (table pages not in the core's caches) with warm
  repeats; the repeats are the ones this plan removes, so the per-walk saving is below the average (chunk 0 times the
  two apart). The `lookup` bucket (L2 probe and `current`, 27-35 ns) is not a walk and is not claimed.

## 3. Design

### 3.1 The record

A per-vCPU direct-mapped table of translations found by fetch walks, in the lookup area after `PIC_FRAMES`
(`area::PAGE_STAMPS`, after `crates/abi/src/execution.rs:340-341`; `LOOKUP_BYTES`, `:278`, grows by it): 8,192 entries
of 32 bytes (256 KiB per vCPU; two entries per 64-byte line, none straddling one; section 5 sizes it).

- `key: u64` = leaf base page number `<< 2 | large << 1 | user`. The base is the 4 KiB page for a 4 KiB leaf, the first
  page of the 2 MiB region for a 2 MiB leaf. `user` is the privilege class of the walk, taken from the same
  `paging::Control` the walk is given (`c.cpl == 3`), because the walk's outcome depends on it (section 2).
- `epoch: u64`: the `Control::epoch` of the walk. 0 means empty (never matches: `epoch >= 1`, as `checked` already
  relies on).
- `frame: u64`: the leaf's guest-physical base, with the leaf's G bit in bit 3 (`cache::GLOBAL`'s value).
- `_pad: u64`.
- Index: `(key * 0x9e37_79b9_7f4a_7c15) >> (64 - 13)`, as `lbucket` (`cache.rs:251-254`). A collision overwrites: a
  lost record costs one walk, never a wrong answer.
- **RAM rule.** A 4 KiB leaf is recorded only if its page is guest RAM (what `fetch_walk` already checks). A 2 MiB leaf
  is recorded only if its whole 2 MiB range lies in one guest-RAM region (`GuestRam::range(base, 2 MiB)`; RAM is up to
  four regions with holes, `crates/abi/src/guest_memory.rs:38-44`): otherwise a record made from the RAM part would
  later answer a fetch in the non-RAM part with a frame, turning the typed stop `no_code fetch_not_ram` (host
  `execution.rs:2686-2693`) into a host error on the copy (`:2706-2711`). A split leaf records nothing (counted
  `page_2m_split`) and walks as today.

### 3.2 The probe, in one place

A function `page_translation(s, va) -> Result<(gpa, flags), fault>` in `runtime/src/execution.rs` wraps `fetch_walk`
and replaces its two calls (`revalidated`, `fetch_page`). With `control = s.cpu.control()` and `user = control.cpl == 3`:

1. Probe the 4 KiB key of `va`. A record hits when its key matches and `epoch == c.epoch`, or its G bit is set,
   CR4.PGE is set and `epoch >= c.global_epoch` (the rule of `current` for `GLOBAL` blocks: a PGE change is a global
   flush, so PGE at the probe equals PGE at the walk). Result: `(frame, G ? GLOBAL : 0)`.
2. Else probe the 2 MiB key of `va`'s region, the same test. Result: `(frame + (va & 0x1ff000), flags | LARGE)`.
3. Else `fetch_walk`. On `Ok`, record under the key of the leaf's size (3.1's RAM rule) with `epoch = c.epoch`;
   `fetch_walk` returns the leaf size for this. Faults and non-RAM results record nothing, as hardware caches no fault.
4. Under `speculation_barriers != 0`, a hit ends in the same `sb` the walk's leaf check ends in, so "every fetch
   translation the resident hands out follows a barrier" stays true to the letter. (No load is issued through the
   recorded frame on either path: a revalidation compares frame numbers, and a placement's bytes are copied by the host
   after the `hvc`, a full context switch. The probe's own load is indexed by a hash masked to the table.) Off by
   default, as the walk's barrier is.

Everything above the probe is unchanged: `revalidated` still compares each page's result with `source_gpa[k]` and
folds flags with `leaves`; `lookup` still writes `checked`, the flags and `mark_large`; `fetch_page` still marks the
frame (`mark`) for a block request, since the mark is about bytes, not mappings. `cache.rs` changes only in `invlpg`.

**How blocks become current without a walk.** Currency is logically per page from the moment the page is recorded:
every block whose pages are recorded with its frames in this epoch is as current as the hardware's fetch would make
it. The block's own `checked` stamp is refreshed lazily, at its next `lookup`, from the record: one probe and one
store, on a path that has already paid a dispatcher miss and an L2 probe for that very block. No block that does not
run is touched (section 7 rejects eager stamping). So "per-block work" shrinks from a 4-5 level walk with an observer
to a 32-byte compare.

**Crossing blocks** (1.0% / 1.3% of revalidations) probe each page on its own: a block over P1 and P2 is current only
when both records hit with its two frames (or the missing one is walked and matches). A block over a 4 KiB page and a
2 MiB page uses one key of each kind. Nothing special is stored for crossing: the record is per leaf, as a TLB is.

### 3.3 Invalidation

| Event | Record | Why |
| --- | --- | --- |
| MOV CR3 (`cache::tlb(false)`) | nothing: `epoch` moved | every non-global record is stale by its epoch; global ones stay by the `global_epoch` rule, as global blocks do |
| Global flush (`tlb(true)`; controls, MTRR, `tables_written`) | nothing: `global_epoch = epoch` | every record is stale, G or not |
| INVLPG m, #PF at m, **at any CPL** (`cache::invlpg`, `cache.rs:2122-2154`, from `page_faulted` `:1368-1380` and the instruction `:2492`) | clear the 4 KiB record of m's page and the 2 MiB record of m's region, **both privilege keys**: four probes, `epoch = 0` on a key match | SDM Vol.3 4.10.4.1: the entries for the page, and every entry of a larger page containing it, whatever privilege filled them. Linux edits user PTEs and invalidates at CPL 0 (`munmap`, `mprotect`, COW), so the CPL-0 INVLPG must clear the CPL-3 record. A record built from a 2 MiB leaf is only ever under the 2 MiB key (3.2 step 3), so no sweep of the region's 512 pages is needed |
| Code write to a frame (`cache::written`, `:1995`; peers' posts) | nothing | the record maps a linear page to a frame; bytes are the frame-keyed code-page protocol's business. A new block placed at that page fetches the frame's present bytes, marked before the host copies them, as today |
| Guest PTE write without a flush (this vCPU or a peer) | nothing | a stale TLB entry is permitted until the guest invalidates it; today's `checked` has the same semantics, and natemu already keeps blocks current across such writes (`no-invlpg`, `place-global`, `invlpg-page` programs) |
| Capacity flush (`cache::reset`) | nothing | blocks go, translations do not; the epoch is kept |
| A walk's A-bit write | happens at the recording walk only | a hit sets nothing, as a TLB hit sets nothing (SDM Vol.3 4.8: A is set when a paging-structure entry is used for a translation; a TLB hit uses none) |

### 3.4 Multiple vCPUs, epochs, controls, check mode

- The table is per vCPU, like `epoch`, `checked` and the shadows. Peers reach it only through memory: their PTE edits
  are visible at this vCPU's next INVLPG, #PF or CR3 load (the guest's shootdown IPI leads to one); their DMA into table
  pages ends in `tables_written`, a global flush on every resident.
- Epochs are `u64`, monotonic for the vCPU's life (`reset` keeps them); records hold the full value, so no aliasing.
  At a million events per second the counter wraps after 5.8 x 10^5 years: not handled, stated in a comment.
- Every paging control the walk reads (CR0.PG/WP, CR4.PAE/PGE/SMEP/LA57, EFER.NXE/LMA, CR3) is fixed within an epoch
  because changing any of them moves it (section 2). The CPL is not a control and does not flush; it is in the key.
  SMAP concerns data only. PAT and MTRRs affect memory type, which the record does not carry.
- **Check mode:** `Control::pages` = `PAGES_OFF | PAGES_ON | PAGES_CHECK | PAGES_CHECK_HALT`, set by the host from
  `--page-currency` as `Control::reuse` is (`REUSE_CHECK` precedent). In the check modes every hit is followed by the
  walk; a different frame or flag counts `page_disagree`, and `PAGES_CHECK_HALT` halts (`stop::PAGE_CHECK`). The check
  walk is a real walk: it sets A bits and its observer may kill blocks, as any walk; it never overwrites the record (the
  record is the TLB entry under test, and a disagreement is a legal divergence or a defect, decided by the program, 3.5).
  `PAGES_OFF` is today's path, kept until the standing suite passes with the record on.

### 3.5 Permitted divergences from a walk

All are what a hardware TLB may do (SDM Vol.3 4.10.2.3 "the processor may use a TLB entry until it is invalidated",
4.10.4.1), and none is new in kind: today's `checked` already keeps blocks current across un-invalidated PTE edits.

1. A PTE changed and not invalidated: the record (and a block kept by it) gives the old frame; a walk gives the new.
   Today's per-block `checked` gives the old one too (`no-invlpg`, `place-global`, `invlpg-page` assert either).
2. The mirror case: a later block's walk recorded the new frame and an earlier block of the old frame is now refused
   where a walk might have found the old PTE restored (ABA); a hardware fetch through the entry runs the new frame.
3. No A bit is set on a hit (3.3's last row).
4. A global record serves after a CR3 load (MOV CR3 keeps global entries, 4.10.4.1; `global` and `place-global`
   already assert natemu may keep the old frame).
5. A 4 KiB record serves after the guest replaced the mapping by a 2 MiB one without invalidation, or a 2 MiB record
   after a split into 4 KiB pages: 4.10.2.3 allows entries of either size to persist until invalidated (and warns the
   guest to invalidate). The frame is the same when the split preserves it (Linux's THP split), the flags differ, which
   check mode reports as a disagreement (test `page-size-check`).
6. A fetch that would have faulted on a walk (PTE made not present, no invalidation) is served from the record, as a
   TLB hit would be; `pf-fetch`'s allowed set already contains that outcome.

In check mode, a program with no un-invalidated PTE edit must show zero disagreements (and halts under
`PAGES_CHECK_HALT`); a program with such edits runs under `PAGES_CHECK` and asserts its expected `page_disagree` count
(the `counts` form). In a 4-vCPU Linux run the count is expected to be small and nonzero (a peer edits a PTE before
its shootdown IPI reaches this vCPU); the gate is a rate: at most 10 disagreements per million hits, and zero in
single-vCPU Linux runs. The zero at N = 1 is an assumption (that Linux edits a PTE and invalidates on the same CPU
before any fetch of that page); a disagreement there is traced (the address, the two frames, the preceding events)
before it is called a design failure rather than a guest pattern to add to this list.

## 4. Correctness argument and the invariants it rests on

Claim: a block revalidated or placed from a record runs only when a CPU with a TLB entry for its page, filled at the
recording walk and not since invalidated, would fetch the same bytes.

- **I1 (recording).** A record (K, e, F) exists only if a fetch walk on this vCPU, at epoch e, under the privilege class
  in K, returned leaf base F for K's range, F's whole range is guest RAM (3.1), and no INVLPG or #PF of an address in
  K's range has happened on this vCPU since (3.3). Records are written by `page_translation` alone and cleared by
  `invlpg` alone.
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
  recorded frame still marks it before the host copies (test `page-mark`).
- **I7 (A/D).** A recorded walk set the A bits hardware would set at the fill; a hit sets none, as a TLB hit (3.3).
- **I8 (independence of the rest).** `current`, `cut_one`, the L1 passes, `prune_tlb`, the link invariant and the
  relink plan's "becomes current only in `lookup`" (`relink-plan.md` section 2) all read `checked`, which this plan
  writes exactly where and when it is written today; only the answer of `mapped` changes.

The guest may see fewer cached translations than hardware could hold, never one hardware could not hold (3.5).

## 5. Cost per event (estimates; the probe cost is measured in chunk 1)

| Event | Added | Removed |
| --- | --- | --- |
| Revalidation of a block on a recorded page | 1 probe (hash, 32-byte load, compare; the table is 256 KiB, so a second-level cache hit): ~10-20 ns; a crossing block 2 | the walk: a warm repeat walk, below the 105-122 ns average |
| First revalidation of a page in an epoch | 1 probe + 1 store | nothing |
| Placement or copy on a recorded page | 1 probe | the walk part of a `fetch` event (138-141 ns including `mark`, which stays) |
| INVLPG, #PF | 4 probes, 0-2 stores: ~30-60 ns on an event of about 1-3 us (`invlpg_ticks`, `page_fault_ticks`) | nothing |
| CR3 load, global flush, capacity flush, code write | nothing | nothing |
| Memory | 256 KiB per vCPU, touched lazily | |

Table size: distinct pages per epoch average about 50 on configure (2.54 M / 51 k epochs) and 130 on make; cc1 holds
a few thousand text pages. 8,192 direct-mapped entries keep collisions rare at those loads; chunk 0 counts evictions
of live records and halves or doubles the table on the count.

## 6. Expected gain (estimate; arithmetic beside each figure; chunk 0 replaces the two unknowns)

Walks removed: revalidations x (1 - distinct share), i.e. 22.8 M x 0.888 = 20.3 M on configure, 52.1 M x 0.914 =
47.6 M on make (baseline counts; the census's distinct count is an upper bound of walks still needed, so these are
lower bounds on walks removed). Two unknowns: the cost of a *repeat* walk, w, which is below the bucket average because
the first walk of a page in an epoch is the cold one (the reviewer's estimate: 50-70% of the average); and the share of
placement walks that hit, h (a new block or copy on a page already recorded this epoch), with `mark` staying on a hit.

- configure, bucket averages (upper): `revalidate` 2.43 x 0.888 = 2.16 s less 22.8 M probes x 15 ns = 0.34 s; `fetch`'s
  walk part (about 100 of 138 ns) x 13.4 M x h = 0.67-1.07 s at h = 0.5-0.8, less 0.20 s of probes: **2.3-2.7 s**.
  With w at 50-70% of the average: **1.3-1.9 s of 109.5 busy vCPU-s (1.2-1.7%)**, about the same in wall time
  (configure is serial). Planning number: **1.3-1.9 s; chunk 0 decides.**
- make: 6.37 x 0.914 = 5.82 less 52.1 M x 15 ns = 0.78; fetch 0.1 us x 12.0 M x h = 0.6-0.96 less 0.18: upper
  **5.5-5.8 vCPU-s**; at w = 50-70%: **3.3-4.3 vCPU-s of 561.5 (0.6-0.8%)**, under 1 s of wall.

Chunk 0 measures w directly (CNTVCT around each walk, split by whether the probe would have hit) and h, and the gate
uses those. The larger effect is in item 2: a miss that "needs revalidation" (54.4% / 29.0% of misses: 17.4 M /
37.8 M) and hits the record needs no walk, so no fault, no A-bit write and no host request can follow, which makes it
eligible for the fast miss path; the [fast-miss plan](fast-miss-plan.md) counts that gain.

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
   install costs about 300 ns against a 32-byte store.
4. **Keeping dispatcher L1 entries across CR3 loads** so the miss itself is saved: needs a tag check on every entry
   (rejected after measurement, decision 9) or retained roots (decision 8).
5. **A 512-slot sweep at INVLPG for 2 MiB leaves** (the `large_held` pattern of `cache::invlpg`): replaced by keying
   records by leaf size, which is the TLB's own structure and makes INVLPG four probes.
6. **Recording faults** (negative entries): hardware caches none, and #PF delivery needs the walk for the error code.
7. **A 32-bit epoch in the record** (16-byte entries): aliasing after 2^32 epochs would be a silent wrong answer for
   128 KiB saved.
8. **Keying without the privilege class**: a supervisor fetch would hit a record a CPL 3 walk made and run a user page
   under SMEP, or a user fetch would run a supervisor page (`adv_setup`'s U/S cases).
9. **Recording 2 MiB leaves that straddle a RAM region end** from the RAM part (3.1): a frame for a non-RAM fetch.

## 8. Chunks, gates, tests, mutations

**Chunk 0: count (scratch perf build, no behaviour change; one Tier 2 profile scheduled by the main session).** Build
the table and the probe in shadow mode: probe, always walk, record from the walk, compare. Counters: hits by granule
and caller (revalidation / placement), walks, `page_disagree`, evictions of a live record, split 2 MiB leaves, the
largest number of live records in an epoch, revalidations by block class (`GLOBAL`, `LARGE`, user), and **CNTVCT ticks
of each walk split by probe outcome** (would-hit vs miss, and in `fetch_page` the walk apart from `mark`), which give w
and h. Gate: the modelled net saving (would-hit walks x their measured mean cost minus probes x 15 ns) is at least
1.5 s on configure or 3.5 vCPU-s on make, and `page_disagree` is zero in single-vCPU runs (K1 programs without
un-invalidated PTE edits; Linux at N = 1) and at most 10 per million hits at four vCPUs. If the gate fails, the record
is not dropped but folded into item 2 (see the sequencing note), since item 2 needs a walk-free revalidation whatever
this item's own saving; the counters stay in `io5-bins`.

**Chunk 1: build (S-M: about 150-250 lines; abi area constant and key helpers, `page_translation`, four clears in
`invlpg`, counters, the host flag, the runner's per-program host flags and expected-count form; independent review
before the gate).**
- Tier 0: abi unit tests for key packing and unpacking (page, large, user), the 2 MiB base and offset arithmetic, the
  hash range, "an empty record (epoch 0) never hits at any epoch", "a 2 MiB record never hits a 4 KiB key", and the
  RAM rule ("a 2 MiB leaf across a region end, or across a hole, is not recorded; one inside a region is"); the
  mutations below, each run once against its test.
- Tier 1, new `run_k1.py` programs (each a few CR3 loads and calls; K1 stays at its present length; QEMU-compared where
  the architecture fixes the answer, natemu-only otherwise; `SET_CPL` switches CPL without a flush where a CPL change
  is needed inside one epoch):
  - `page-share` (QEMU), three phases in one program: (a) X and Y on one page V, A maps F1, B maps F2 with other bytes;
    A: X, Y; B: X, Y; A: X, Y: each space's bytes every time, counts one walk and one hit per (space, round);
    (b) in A, X then Y (Y from the record), V's PTE -> F2, INVLPG V, Y runs F2's bytes; (c) V made not present, a load
    faults, the handler maps F1 again with no INVLPG (as `pf-remap`), Y runs F1's bytes with a fresh walk (the #PF
    cleared the record: counts).
  - `page-cross` (QEMU): W crosses P1 into P2, X lies on P2; B maps P1 to a third frame, P2 as A. B: X (records P2),
    then W: P1 must be walked and W replaced by the third frame's bytes.
  - `page-large` (natemu only; QEMU's jump cache fails `invlpg-large` already): X and Y in a 2 MiB page; X recorded;
    the PDE changed, INVLPG of another 4 KiB address of the page; Y runs the new frame's bytes.
  - `page-large-edge` (natemu only): guest RAM is a 2 MiB multiple (host `execution.rs:205`) and the 3-4 GiB hole's
    bounds are 2 MiB aligned (abi `execution.rs:153-154`), so the only hole a 2 MiB leaf can straddle is the legacy
    hole 0xA0000-0xFFFFF inside guest-physical 0-2 MiB (`LEGACY_HOLE`, `:173`; natemu's own boot identity map covers
    it with a 2 MiB page, host `boot.rs`). The program maps a 2 MiB leaf at guest-physical 0, X in RAM below 0xA0000
    and Y inside the hole: X runs; the fetch of Y must end in the typed stop `no_code fetch_not_ram` with the record on
    or off (the runner gains an expected-stop form for this program, as it has an either-outcome form for `pf-fetch`).
  - `page-global` (natemu only): PGE on, V global and the same frame in A and B; A: X; B: Y hits across the CR3 load
    (count) with F1's bytes; then V's PTE -> F2 in B with no INVLPG and PGE toggled off and on: Y runs F2's bytes.
    Second part: PGE off, V with G set: A: X; B (V -> F2): Y runs F2's bytes.
  - `page-user` (natemu only, `adv_setup` style, SMEP on from the start, one epoch): V a user page, S a supervisor
    page. At CPL 3, X on V runs (user record); `SET_CPL` 0; a fetch of V must #PF (SMEP; the supervisor key misses and
    the walk faults). At CPL 0, Z on S runs (supervisor record); `SET_CPL` 3; a fetch of S must #PF (U/S).
  - `page-user-invlpg` (natemu only): at CPL 3, X on V runs; `SET_CPL` 0; V's PTE -> F2 and INVLPG V at CPL 0;
    `SET_CPL` 3; Y on V runs F2's bytes. Then the same with a #PF at CPL 0 (a supervisor load from V after V is made not
    present, the handler maps F1 again, no INVLPG): Y runs F1's bytes after a fresh walk (counts).
  - `page-mark` (natemu only, one epoch): X on V runs (frame F marked); F written through its data alias (X dies); Y
    placed on V from the record; F written again; Y, called again, runs the new bytes (the second write must find the
    mark `fetch_page` set on the hit).
  - `page-size-check` (natemu only, under `--page-currency check`): LP by a 2 MiB page, X runs (2 MiB record); the PDE
    replaced by a page table of 4 KiB pages of the same frames with no INVLPG; Y on LP: `page_disagree` must be exactly
    1 (a 2 MiB record against a 4 KiB walk: same frame, other flags).
  - `page-check-keep` (natemu only, under `check`): X on V (recorded, F1); V's entry -> F2 with no invalidation; Y and
    then Z placed on V: both are placed from the F1 record (3.5 case 1) and `page_disagree` is exactly 2.
  - `page-counts`: 32 blocks on one page in a fresh space: `page_walks` 1, `page_hits` 31.
  - Existing programs that must stay green, in particular `walk` (the recording walk's A-bit write still kills the
    block), `cross`, `straddle`, `cross-link`, `no-invlpg`, `global`, `place-global`, `invlpg-page`, `invlpg-large`,
    `pf-large`, `large-reval`, `local-write`, the `reuse-*` three, and the `adv_setup` set. **`local-gpa`** asserts T
    placed from F2 after an un-invalidated PTE edit, which the record (legally, 3.5 case 1) would answer with F1, so
    it would no longer test what it guards: it runs with `--page-currency off` (the runner passes per-program host
    flags), and once more under `check` asserting `page_disagree` = 1. The same expected-count form covers
    `no-invlpg`, `pf-fetch`, `place-global`, `invlpg-page` and `global` under `check`; every other program runs under
    `check-halt`.
- Tier 2 gate: `revalidate` + `fetch` down by at least 70% of chunk 0's model; `invlpg_ticks`, `page_fault_ticks` and
  `tlb` up by no more than half the gain; `remapped` within noise of the baseline. Wall time is judged in group F
  (items 6, 8, 9) per the review's rule, or with item 2 if the two are measured together.
- End of group: `run_io5.py run` (the CPython build and its tests) once with `--page-currency check` (the rate of
  3.5); the standing suite and benchmarks once before merge.

**Deliberate mutations, each turning its test red:**

| Mutation | Caught by |
| --- | --- |
| M1 hit ignores the frame compare in `revalidated` (any record hit makes the block current) | `page-share` (a) (Y runs F1's bytes under B) |
| M2 record not checked against the epoch | `page-share` (a), `remap-cr3` |
| M3 table disabled (always walk) | `page-counts` |
| M4 `invlpg` leaves the 4 KiB record | `page-share` (b) and (c), `remap-invlpg` |
| M5 `invlpg` leaves the 2 MiB record, or a 2 MiB leaf recorded under its 4 KiB key | `page-large`, `invlpg-large`, `pf-large` |
| M6 a crossing block current on one page's hit | `page-cross`, `cross` |
| M7 global record kept across a global flush (`epoch >= 1` instead of `>= global_epoch`) | `page-global` first part |
| M8 global rule ignores CR4.PGE | `page-global` second part, `place-global` |
| M9 key without the privilege class | `page-user` (one epoch, SMEP on throughout: the second fetch hits the other class's record and runs instead of faulting) |
| M10 `invlpg` clears only the current privilege class's keys | `page-user-invlpg` (Y runs F1's bytes) |
| M11 hit skips `mark` in `fetch_page` | `page-mark` (the second write goes through a writable shadow unnoticed; Y keeps the old bytes) |
| M12 check mode compares frames only | `page-size-check` (count 0 instead of 1) |
| M13 2 MiB leaf recorded without the RAM rule | `page-large-edge` (Y's outcome is no longer the typed stop), the abi unit test |
| M14 check walk overwrites the record | `page-check-keep` (natemu only, under `check`): X on V recorded; V's entry changed to F2 with no invalidation; Y then Z placed on V: `page_disagree` must be 2 (both placements hit the F1 record against an F2 walk); under M14 the first check walk refreshes the record and the count is 1 |

**Chunk 2 (the [fast-miss plan](fast-miss-plan.md)): the fast miss path takes record hits.**

## 9. Stop rule and risks

- Chunk 0's gate failing does not stop the record: it is folded into item 2 (sequencing note). Revert chunk 1 if its
  Tier 2 gate fails after one round of diagnosis and item 2's chunk 2 does not need it. Any K1 red, `BAD_LINK`, or a
  check-mode disagreement in a single-vCPU run traced to the design rather than a slip stops the item for re-review of
  sections 3-4 (the retention plan's rule).
- Risks and open questions: w (the repeat walk's cost) and h (the placement hit rate) are unmeasured (chunk 0); the
  share of revalidations that are kernel blocks on 2 MiB global pages after the 3.4-3.5 k global flushes per run
  decides how much the 2 MiB key matters (chunk 0 counts); table load on cc1-sized processes (evictions counted);
  check-mode disagreements at four vCPUs are judged by the rate of 3.5; the hit test adds about ten instructions to a
  path the fast miss path will classify without a full bank (item 2's design); a PTI kernel (not the measured
  configuration) is exact under this plan but gains less, since its kernel pages are non-global.

## 10. Review response (independent review, November 14, 2026: accept with fixes)

| Finding | Resolution |
| --- | --- |
| B1 (blocking): a 2 MiB record made from the RAM part of a leaf that straddles a RAM region end would serve the non-RAM part, turning the typed `fetch_not_ram` stop into a host error | Fixed: the RAM rule (3.1): a 2 MiB leaf is recorded only if its whole range lies in one RAM region; I1 names it; unit test, `page-large-edge`, mutation M13 |
| B2 (blocking): `local-gpa` would turn red (T placed from the F1 record instead of F2) and stop testing its link rule; `pf-fetch` and `local-gpa` would halt in check mode, so "zero disagreements in K1" fails by design | Fixed: `local-gpa` runs with the record off and once more under `check` with an expected count; programs with un-invalidated PTE edits assert their `page_disagree` count, the rest run under `check-halt` (3.5, 8); the check walk is stated to be a real walk (A bits, observer) that never overwrites the record (3.4), mutation M14 |
| B3 (blocking): mutations M9, M10, M11 survived their tests (SMEP toggled, CR3 reloads between phases); no test that a CPL-0 INVLPG/#PF clears CPL-3 records | Fixed: `page-user` runs both directions in one epoch with SMEP on via `SET_CPL`; `page-mark` writes the frame twice in one epoch; `page-size-check` splits a 2 MiB mapping in one epoch; new rule and test `page-user-invlpg`, mutation M10 (clears only the current class) |
| 1 GiB leaves | Dropped: the walk rejects PS at levels 3-4 (`paging.rs:227`) and PAE/LA57 changes are #GP, so no 1 GiB leaf reaches the record |
| Permitted divergences | Listed (3.5), with the SDM sections and the programs that already assert them |
| Check-mode threshold at 4 vCPUs | Set: at most 10 disagreements per million hits; zero at N = 1 (3.5) |
| Gain optimistic (`fetch` includes `mark`; the average walk includes cold first walks) | Revised (6): the walk part of `fetch` only; w at 50-70% of the average until chunk 0 times would-hit and miss walks apart; planning number 1.3-1.9 s configure, 3.3-4.3 vCPU-s make; chunk 0's gate lowered to match, and a failed gate folds the record into item 2 |
| Re-review: `page-large-edge` as written (a leaf past the RAM end) cannot exist (RAM and hole bounds are 2 MiB aligned); M14 not caught by `page-share` (b) (it invalidates right after the edit); 6's arithmetic says 1.3-1.9 s; "zero disagreements at N = 1" is an assumption | Fixed: the program uses the legacy hole inside guest-physical 0-2 MiB, the only hole a 2 MiB leaf can straddle, and M13's text no longer presumes a host error; new program `page-check-keep` (two placements after an un-invalidated edit: count 2, 1 under M14); planning number 1.3-1.9 s, chunk 0 decides; the N = 1 zero is marked an assumption to trace before judging (3.5) |
| Speculation barrier | The hit path ends in the walk's `sb` under the barrier switch (3.2 step 4), with the argument why no load depends on it |
| Entry size | 32 bytes, two per line (3.1, 5) |
| Key's `user` bit | From the `paging::Control` handed to the walk (3.1, 3.2) |
| Three programs into one | `page-share` has three phases (8) |
| Sequencing | Built immediately before item 2; a failed chunk 0 folds the record into item 2 rather than stopping |

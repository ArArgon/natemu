# Root retention plan: keeping each address space's translation state across CR3 loads without PCID

Status: design, not built (November 13, 2026). Accepted after four rounds of independent review (findings and fixes in section 12).
Branch `perf-real-workload`.

The guest has no PCID. Today every MOV to CR3 makes every non-global block stop being current, empties the listed L1
entries, undoes the links into non-global blocks and drops every non-global data shadow. This plan keeps that state per
CR3 value ("root") in a few slots per vCPU, so that a load back to a recently used root costs a check of its page
tables instead of a rebuild. Section 2 settles whether that can be exact; the rest is the design, its costs, tests and
order of work.

## 1. Motivation (measured, IO5 profile, Debian 13, CPython build, 4 vCPUs, `mitigations=off`)

Evidence: `natemu-raw/io5-profile-20261113-01` (`breakdown.txt`, `*.summary.json`), `io5-cr3-20261113-02` (CR3 classes,
scratch counters `io5-bins/scratch-cr3-class.diff`), `io5-prune-20261113-01` (prune time, `scratch-prune-bucket.diff`).
Bucket sums are whole-run sums over the four vCPU threads (the step window plus about 20 s of boot).

| | `make -j4` (181 s window) | `configure` (97 s window, serial) |
| --- | --- | --- |
| CR3 loads to the same root / one of the vCPU's last 8 other roots / other | 20.6 / 66.5 / 12.9 % | 15.8 / 63.8 / 20.4 % |
| CR3 loads that kept the kernel's global shadows (`kept_resets`) | 15.5 k of 36.6 k (42%) | 16.3 k of 51.3 k (32%) |
| global flushes per vCPU (about 221 in all from device DMA into table pages) | 837-910 | similar |
| `tlb` bucket (CR3 loads and global flushes) | 14.3 s; 487 us per load on vCPU 0 | 4.9 s; 82 us per load |
| revalidations | 102 M, 9.3 s | 23.3 M, 2.6 s |
| links made / undone by events / reclaimed at capacity; `link` + `patch` | 116.6 M / 44.2 M / ~71 M (97-99 passes per vCPU); 69.8 + 29.5 s | 16.1 M / 14.3 M / -; 10.1 + 2.3 s |
| dispatcher misses | 138.6 M, 40.7 s | 32.0 M, 7.6 s |
| data aborts + shadow installs (each counted once) | 54.9 M, 19.3 + 16.5 s | 41.8 M, 12.9 + 11.2 s; 2.4 M guest #PFs |
| #PF invalidation's link scan (`prune` after `cache::invlpg`) | not split out | 76,794 scans x 34.7 us = 2.67 s |

The CR3 classes ignore page-table writes made while a root was away; section 7 turns all this into estimates.
Two findings stand apart from root retention and are cheaper: 58-68% of CR3 loads already drop the kernel's global
shadows, because some non-global shadow sits in the kept half (chunk 0b), and link capacity reclaims undo more links
than every other event together (chunk 1b).

## 2. The crux: what natemu tracks today

[PCID plan](pcid-plan.md) 3.1 rejected "keep shadows per root without PCID" because a CR3 load without PCID must flush,
and hiding that seemed to need a fault on every guest page-table write. The owner's counter-argument is that natemu
already sees writes to page-table pages. The code says it does not, except for device DMA:

1. **Code pages, not table pages.** `CODE_PAGES` holds one bit per vCPU per frame that holds translated *code*
   (`cache::mark`, `crates/runtime/src/cache.rs:2213`). Shadows of such frames are read-only (`translate`,
   `crates/runtime/src/execution.rs:2992-3001`; `install`, `:3722-3735`), so a store into one exits to `code_written`, and
   peers are posted (`cache::written`, `cache.rs:1895`; `post`, `:1923`). A page-table page gets a code bit only if code
   was translated from it, which Linux never does. Arming the protocol for a page costs a synchronous shootdown: every
   peer that may hold a writable shadow of the page drops all its shadows (`mark`, `execution.rs:1284-1305`).
2. **Table pages, read side only.** `TABLE_PAGES` holds one bit per frame ever read as a page table
   (`cache::table_read`, `cache.rs:108`; abi `execution.rs:366-391`), set before each walker read
   (`CodeWrites::before_read`, `crates/abi/src/guest_memory.rs:208-218`). Only the host reads it: a device copy into a
   marked page takes a full window and publishes `tables_written` (`crates/host/src/execution/io.rs:101`, `:618`,
   `:825-842`), after which every resident does a global flush (`io_checkpoint`, `execution.rs:1462-1467`).
3. **The walker's own A/D writes** go through the same observer and matter only for code pages (`guest_memory.rs:211-217`).
4. **Guest stores into table pages, by this vCPU or a peer**, are plain native stores through a writable shadow (the
   kernel's direct map, writable whenever the walk allows it and the frame is not a code page, `execution.rs:2992`).
   No exit, no bit, no record. A shadow is a copy of a walk's result; it is never updated when the PTE changes.

So no retained state can rely on today's tracking: a `munmap` + `mmap` at the same address by another thread of the
process while this vCPU runs another process, then a load back, would run the old code and read the old data. Linux
without PCID gives no other signal: its shootdown IPIs go only to CPUs that currently run the mm.

**But exactness does not need write tracking.** A walk is a function of the CR3 value, the paging controls and the
entries it reads. If, at the CR3 load, every entry a retained translation was built from still holds the value it was
built from (A/D bits set since excepted, 6), a walk at that instant would produce the same translation and write
nothing, so keeping it is what a TLB filled right after the load would hold. That is checked at the load by comparing
the root's page-table pages with copies taken when they were walked. No store has to trap.

## 3. Hazards and what each retained item needs

Retained items: (a) a block's currency, (b) L1 entries, (c) links, (d) data shadows. All depend only on walk results
for their pages; (b) and (c) depend on (a). Code bytes stay covered by the frame-keyed code-page protocol.

| Hazard (guest action while the root is away, or active without a flush) | Today's tracking | Table-page record (5.2) |
| --- | --- | --- |
| Page reclaimed and reused; COW (R/W cleared, later a new frame); `munmap` + `mmap` by another thread | no | yes: entry differs |
| Another vCPU running the same mm, or the kernel, writes the tables (rmap unmap, khugepaged, exit) | no | yes: memory compare |
| Root page freed and reused for a new process (same CR3 value) | no | yes: compared by content; equal paths give equal translations |
| A or D cleared by reclaim or writeback | no | yes: a cleared bit is a change |
| Same-root reload after PTE edits (Linux's full local flush) | no | yes |
| An entry changed, a walk built state from the new value, then changed again, all without a flush | no | yes: the "mixed" mark (5.2) |
| Global pages; INVLPG, #PF; device DMA into a table page | handled | unchanged rules (5.7) |
| Page tables that map themselves (Windows self-map) | n/a | a page reached at two levels or bases: the slot resets at every load |

## 4. Detecting changed entries: four ways, costed

| Way | Per CR3 load | Other costs | Decision |
| --- | --- | --- | --- |
| Write traps on the table pages of away roots | 225-405 us (estimate below) | stores into armed pages trap: 554 k #PFs per vCPU each followed by a PTE store, ~0.33 s per vCPU | rejected |
| Compare at the load, eagerly (5.2) | 8-29 us (estimate below) | a few ns per level at recorded walks | **chosen** |
| Compare lazily per 2 MiB region at first touch (L1 variant) | 1 TLBI per slot ASID + one abort and one path compare per region touched | aborts, park/reattach state | later, data shadows only (11) |
| Entry patch of stale blocks instead of unlinking (L2 variant) | n/a | a code write per cut and per restore, a trap at first entry | rejected (11) |

Write traps: the only write detection natemu has arms a page by making every peer holding a writable shadow of it drop
all its shadows, and the kernel writes PTEs through the direct map on every vCPU. Estimate: 50-90 table pages per root
re-armed per switch; if 1% of armings meet a writable peer, 0.5-0.9 peer resets per load at about 830 refills x 0.545 us
= 450 us each = 225-405 us per load. Eager compare: 50-90 pages, each read twice (copy and memory), 400-720 KiB per load at
25-50 GB/s (an unmeasured estimate for this M4) = 8-29 us, against today's 82-487 us of `tlb` alone.

## 5. Design

### 5.1 Slots keyed by the CR3 value

- Each vCPU has K slots (start K = 8). A slot holds one CR3 value (all 64 bits as loaded), the paging controls it was
  built under (CR0.PG/WP, CR4's paging bits, EFER.NXE/LME/LMA, the PAT value and an MTRR write generation), its
  table-page record (5.2), the blocks current under it (5.3), its user-context L1 tables (5.4) and its data-shadow spaces
  (5.6).
- MOV to CR3 with value V (`execution.rs:2356-2363`, today `tlb(s, false)` unconditionally):
  1. a full barrier (`dsb ish`): MOV CR3 serializes, and Linux's lazy-TLB protocol (a CPU sets its bit in `mm_cpumask`,
     loads CR3, then walks; a peer stores a PTE, fences, then reads the mask to choose IPI targets) needs every later
     table read ordered after the guest's earlier stores. Today `cache::tlb`'s L1 pass supplies one by accident
     (`cache.rs:684`, `:696`);
  2. find the slot holding V, else take the least recently used one and reset it for V;
  3. if the slot's controls differ from the current ones, reset it; else validate its record (5.2);
  4. make it current: currency, L1 binding, shadow roots and ASID (a TTBR pair, no TLBI).
  A load of the current value does 1 and 3. Other flush paths follow 5.7.
- Off switch: host flag `--root-slots K`; K = 0 is today's path, kept until the final gate (chunk 6).

### 5.2 The table-page record and its validation

- **Recorded walks:** every walk whose result is kept past the next CR3 load in a slot: block placement and
  revalidation fetch walks (`fetch_page`, `execution.rs:1173`; `revalidated`, `:1226`) and data walks that install a
  non-global shadow (`translate`, `:2950`). A data walk ending at a global leaf is not recorded; a non-global block
  records both its pages' walks. The hook is the walk observer's `read` and `wrote` (`guest_memory.rs:179-188`), in an
  observer wrapping `CodeWrites` for these walks only.
- **Record:** per slot, a set of table pages, each with a 4 KiB copy, its level (4 = PML4 .. 1 = PT), the linear base it
  was reached at, and two 512-bit masks: `used` (a recorded walk passed through this entry, so items may depend on it;
  cleared only when the entry's range is invalidated) and `mixed`. Lookup by frame through a per-slot hash, and by 2 MiB
  region for level-1 pages. Cap: 128 pages per slot (set from chunk 0); a walk past it marks the slot overflowed, and a
  page reached at a second level or base marks it aliased; either resets the slot at its next load.
- **At a recorded walk** reading entry i of page P with value v (after the walk's own A/D write):
  - P not in the set: copy P, record level and base, set copy[i] = v (other entries have no dependents yet).
  - copy[i] == v: nothing. `used[i]` clear, or v equals copy[i] with only A/D bits added: copy[i] = v.
  - otherwise (`used[i]` set, a real change while the root is active, no flush yet): copy[i] = v and set `mixed[i]`.
    Items built from the old value may stay until the next flush (x86 allows the stale entry).
  - then set `used[i]` if the walk passed through the entry (present and not faulting there).
- **INVLPG or #PF at page p in the current slot:** its blocks and shadows go (5.7), so the leaf entry of p in the
  record, if any (its PTE, or the PDE mapping its 2 MiB page), gets `used` and `mixed` cleared; a PDE pointing to a
  table never does (INVLPG does not drop everything under it). Linux changes a PTE and then INVLPGs it (COW, `mprotect` of a few pages), so
  without this almost every changed entry would end up mixed and be dropped once more at the next load.
- **Validation (at a load into the slot, after the barrier of 5.1):** pages are compared level by level from the root
  down, with a `dmb ishld` between levels (a hardware walk's order: each level read after the one that leads to it),
  in 8-byte elements (no torn entry). For each entry i whose memory value m differs from copy[i], or with `mixed[i]` set:
  - `mixed[i]` set: invalidate (below).
  - `used[i]` clear (nothing depends on it), or m equals copy[i] with only A/D bits added: copy[i] = m.
  - otherwise invalidate.
  - invalidate = drop the entry's range in this slot, then copy[i] = m, clear `used[i]` and `mixed[i]`. Level 1: its
    4 KiB page; level 2: its 2 MiB region, and its PT page leaves the set; levels 3-4: reset the slot.
  The copy is updated from the value compared, never re-read, so a store racing the compare is seen now or at the next
  walk or load.
- **Dropping a range in slot q:** blocks with bytes on it (linear-map chains, `on_linear_page`, `cache.rs:267`; 512
  chains for 2 MiB, as `cache::invlpg`'s large path) are cut (`cut_one`, all slots: more than needed, kept simple), and
  slot q's shadows of the range are unmapped in both its spaces with `TLBI VAE1` of q's ASIDs.

### 5.3 Blocks: currency per slot

Today `current(b)` is `checked == epoch`, or `GLOBAL` and `checked >= global_epoch` (`cache.rs:555-558`). New, as
[PCID plan](pcid-plan.md) 3.4 with slots for PCIDs:
- `checked` = 48-bit stamp << 16 | 16-bit slot mask. `Control` gains `now` and `flushed_at[K]`.
- A non-global block is current under the current slot q when bit q is set and stamp > `flushed_at[q]`; its live slots
  are the bits r with stamp > `flushed_at[r]`. Global blocks keep `GLOBAL` with `global_epoch`, current under every slot.
- Revalidation or placement under q: mask = old live slots + q; stamp = `now`.
- Flushing slot q: `flushed_at[q] = now; now += 1`, no per-block work. `cut_one` (`cache.rs:2089`) keeps `checked = 0`.
- K = 0: one slot, flushed at every CR3 load: today's semantics.

### 5.4 L1 tables

- CPL-0 contexts keep one table each across slots; at a switch their fill lists are visited as at today's CR3 load
  (`cache.rs:666-701`), emptying entries not current under the new slot. With `mitigations=off` they hold global blocks
  and their lists are short.
- CPL-3 contexts get one table per (context, slot): `select_l1` (`cache.rs:488`) keys tables by (context, slot) with its
  least-recently-used choice; at a switch the context's dispatcher slot is rebound to the new slot's table
  (`write_slot`, about 90 ns per perf chunk 12) or to the miss. `L1_TABLES` goes from 4 to 8 (abi `execution.rs:256`).
- A table holds only entries current under its owner: `cut_one` empties index i in every table; flushing slot q empties
  q's tables through their fill lists; a global flush clears the CPL-0 tables and the current slot's.

### 5.5 Links: one invariant for every link, and an inbound index

**Invariant:** for every patched link X -> Y (local links included), Y is current under every live slot of X; a global
X links only into a global Y. Kept by:
- (a) **link time** (`cache::link`, `cache.rs:1512`): the link is made only if Y is current under all of X's live slots
  (global Y: always); a global X never links into a non-global Y (its exit goes to the linker each time; with
  `mitigations=off` the kernel is global, so chunk 0 counts these; a PTI kernel mixes them and would need PCID plan 3.5's
  demotion, not planned). A link is local only when source and target have the same `GLOBAL` flag; any other
  qualifies only as a non-local link. A global flush ends global blocks' currency through today's `prune(TLB)` pass,
  which visits only non-local records into global targets (`cache.rs:2126-2150`). With this rule a local link into a
  global target has a global source (it stays in the LOCAL segment, `cache.rs:1605-1609`, which that pass does not
  visit); that source loses its currency at the same flush and runs again only after a revalidation, where hook (b)
  undoes the link if needed. So no link into a global block outlives a global flush in a running source.
- (b) **X gains a slot** (revalidation or placement under q): each patched point of X, local or not, into a block not
  current under q is undone (at most `MAX_LINK_POINTS` points; the record is found on the target's inbound list).
  A block revalidated or placed as global (`lookup` rewrites `GLOBAL` from the new walk, `cache.rs:620-623`) gains every
  slot: hook (b) then undoes each of its points into a non-global target, local ones included.
- (c) **Y stops being current** (`cut_one`) **or dies:** every inbound link, local ones included, is undone through
  its inbound list. A cut does not always reach a local source: `cache::invlpg`'s large path cuts only `LARGE` blocks
  (`cache.rs:2034-2040`), so a source current under r through 4 KiB pages keeps running while its target, current
  under q through a 2 MiB page of the same frames, is cut. With (c) a block entered by any link is current under the
  running slot.
- **Slot flushes and switches need nothing:** a flush removes the slot from every non-global block at once; a switch
  makes nothing current. A global flush ends every global block's currency, and its inbound links go through today's
  TLB-segment pass (837-910 per vCPU in make, rare enough).
- This replaces chunk 8's exemption of local links: with slots, a local source current under slot r could otherwise
  enter a target that was linked onward under slot q only, which today's CR3-load prune prevents (review finding 1).
- **Optional, if chunk 0 counts many local refusals and undos:** instead of refusing or undoing a local link S -> T for
  lack of slots, give T the source's live slots (T's mask = both live sets, stamp `now`). Exact by chunk 8's argument:
  T's pages are pages of S through the same frames, so wherever S's walks hold, T's do, and S's recorded walks cover them.
  Lending is a way for T to gain slots, so hook (b) runs on T for every lent slot (T's out-links into blocks not current
  under it are undone). T is marked `LARGE` and recorded by `mark_large` when S is `LARGE` (an INVLPG elsewhere in S's
  2 MiB page must reach T, and the region marks must hold T's region, not only S's). Plain local links only: a local
  link into a PIC translation names the translation, not an instance. A global S never lends to a non-global T, and
  rule (a) keeps both flags equal anyway. Built in chunk 3 only on chunk 0's counts.

**Inbound index** (replaces the scans): each `LinkRecord` (abi `execution.rs:844`) gains `prev_in` and `next_in` (16 -> 24
bytes), each block an inbound head (the unused `Block::reserved`, `:749`). Records stay in today's segments; `insert`
and `remove` (`cache.rs:1840-1864`) fix the neighbours of each record they move (at most one per later segment).
- INVLPG and #PF (`cache::invlpg`, `cache.rs:2022`): the blocks cut walk their own inbound lists; `prune(TLB)` goes.
- Death (code writes): the dead block's inbound list, local links included. Its outbound links stay patched (a dead
  block never runs; its code stays until the next flush; its records are dropped when met or at reclaim). Checked links
  (into a dispatcher slot) are on no list and go at flushes, as today.
- CR3 load without slots (chunk 1 alone): the CR3-segment pass stays. That segment holds only non-local links into
  non-global blocks, all of which such a load must undo, so its cost (424 us per load in make) is the undoing, not the
  search; slots (chunk 3) remove it.
- Capacity reclaim and `unlink_all` keep their passes; reclaim is frequent in make (chunk 1b).

**Why not a tag check on linked entry.** A check at the target (load its tag, load the vCPU's tag, compare, branch) makes
every invalidation a store but taxes every linked transfer. Linked transfers are not counted; an unmeasured estimate of
0.3-1 ns per transfer on blocks of 3-5 ns gives 5-30% of guest time on every workload. The invariant costs work only at
events. If chunk 3's gate fails because of the invariant's bookkeeping, a check-always-passes build measures the tag's
real cost first.

### 5.6 Data shadows per slot

As [PCID plan](pcid-plan.md) 3.2-3.3 and 3.6, with slots for PCIDs:
- Each slot has its own supervisor-lower, user-lower and user-upper roots (lower roots link the resident's private
  region at L0 index 256 as `LOWER_ROOT` does) and its own ASID pair. The supervisor upper half (the kept half,
  `crates/abi/src/shadow.rs:17-23`) is shared by every slot; its entries stay nG, refilled per ASID by hardware walk.
- **Shared-half maintenance reaches every ASID:** today's `Tlb::invalidate` and `invalidate_all` hit the current ASIDs
  only (`crates/runtime/src/tables.rs:26-35`). An unmap in the shared half (INVLPG or #PF of a kernel page, Linux's
  `flush_tlb_kernel_range`) uses `TLBI VAAE1` (all ASIDs, by address) and a shared-half reset `TLBI VMALLE1`; they also
  hit other vCPUs' entries on the same core, a cost only.
- ASIDs: vCPU v gets 2K from 2 + 2Kv; slot j uses +2j and +2j+1. K = 8: ASIDs 2-65 at four vCPUs; 8-bit ASIDs allow 15
  vCPUs. `tables::select` (`tables.rs:72`) compares the full ASID, not only the user bit.
- **Non-global shadows in the shared half:** today one sets `NOT_ALL_GLOBAL` and the next CR3 load is a full reset
  (`Space::install`, `shadow.rs:255-257`; `reset_cr3`, `:319`). This already happens at 58-68% of CR3 loads (section 1),
  so the data half of this plan gains little until it is fixed: chunk 0b finds the cause (one candidate: `install_ones`
  maps unassigned physical pages with `map`, never global, `tables.rs:116-117`, so a kernel probe of an unassigned page
  marks the kept half) and either fixes it or builds PCID plan 3.6's per-top-entry split (chunk 5, now required for the
  data half).
- Pool: today 288 pages in the 2 MiB window, bump-allocated from two ends, freed only whole (`shadow.rs:43-48`). It moves
  to a per-vCPU private region of 2,048 pages (8 MiB; estimate: about 100 table pages per slot x 8, the kept half and
  headroom) with a free list; each slot owns a page list. Resetting a slot: unlink its roots, `TLBI ASIDE1` of its two
  ASIDs, then free its pages. Capacity: reset the least recently used non-current slot, then everything.
  `Space::next`'s table check and `install`'s private-target check cover every slot's roots and the new region.
- INVLPG and #PF unmap in the current slot, the shared half (as above) and any slot flagged as holding a global shadow
  outside the shared half (Linux makes no user page global).
- Code-write protocol: writable bits stay per vCPU, a superset over all slots; a peer's `SHADOWS` request, the vCPU's own
  `mark`, a split lock and a pool eviction reset every slot. Retained slots keep more writable shadows, so more
  shootdowns than make's 28 per vCPU: counted from chunk 4.

### 5.7 Invalidation rules

| Event | Today | With slots |
| --- | --- | --- |
| MOV CR3 (PCIDE = 0) | non-global blocks, listed L1 entries, CR3-segment links, non-global shadows | 5.1: barrier, find or recycle, controls check, validate, switch |
| INVLPG m, #PF at m | blocks on the page (2 MiB if large), their links (scan), 2 MiB of shadows | same blocks (all slots) via inbound lists; current slot's, shared half's (all ASIDs) and flagged slots' shadows; the record's `used`/`mixed` for m cleared |
| CR0.PG/WP, CR4 paging bits, EFER paging bits, MTRR write, **PAT write** (new; flushes nothing today, `execution.rs:2567-2569`, though a slot must not carry translations built under the old PAT past a CR3 load) | global flush and full shadow reset (PAT: none) | global blocks and the shared half flushed; the current slot and every flagged slot (5.6) reset; other slots reset at their next load if their controls differ (a PGE toggle off and on leaves them valid) |
| `tables_written` (device DMA into a table page) | global flush | as the row above (global state, current and flagged slots); other slots' records see the new contents at their next load |
| Code-cache capacity flush | every block and link | the same; records and data shadows stay |
| Pool full, uncached list overflow, peer `SHADOWS`, own `mark`, split lock | full shadow reset | every slot's shadows; blocks unaffected |
| Code write (any vCPU, host) | frame-keyed death | unchanged; inbound list instead of scan |
| PIC instances and checked links | current through their walks; checked links probe L1 | unchanged: an instance is current only under slots that walked it |

Not flushing inactive slots at a global flush is exact for their non-global state only, for the reason of section 6:
it is not in the architectural TLB, and at their next load it is checked against memory under unchanged controls.
Global shadows are never recorded, so a slot flagged as holding one outside the shared half is reset by every global
flush (review finding, second round); global blocks need nothing, as `global_epoch` moves.

### 5.8 Other vCPUs

Slots, records, currency, L1 tables, link records and shadow spaces are per vCPU. Peers affect a slot only through
memory (compared at the load, after its barrier), code-page posts (frame-keyed), shadow-reset requests (every slot) and
the shared half (all-ASID maintenance). Peers' walks set A/D bits, which validation folds in.

## 6. Exactness argument (SDM Vol.3 chapter 5, edition 092)

Without PCID, a MOV to CR3 must invalidate every non-global TLB entry and paging-structure-cache entry (5.10.4.1); a
processor may cache a translation whenever it could walk it and may drop entries at any time (5.10.2.2). Claim: every
non-global translation natemu uses after a CR3 load at time T is one a walk at or after T would produce.

1. A retained item under slot q was built by a recorded walk under q's CR3 value V with q's recorded controls; the
   load checks the controls are unchanged (PAT and the MTRR generation included), else resets q.
2. Every table page the walk read is in q's record (else the slot is reset). For each entry the walk passed through,
   `used` is set and the copy holds the value the item was built from, or `mixed` is set (5.2); `used` and `mixed` are
   cleared only when the entry's range is dropped (validation, INVLPG, #PF), which removes every item built through it.
3. After the load's full barrier, validation reads each such entry top-down and finds it equal to its copy, or differing
   only by A/D bits set since, with `mixed` clear; otherwise it drops the entry's whole range, which contains the item.
4. A walk after the barrier for the item's address starts at V and reads, level by level, entries whose addresses are
   set by V and the earlier values: the same entries with the same values (A/D aside), read in the same order, so it
   yields the same frame, rights, memory-type bits and global bit, and writes nothing (the A/D bits the item relies on
   are set). A cleared A or D bit counts as a change, so the next access walks and sets it, as hardware would.
5. So each retained item equals what a walk after T would cache. From T on the existing rules apply: INVLPG and #PF
   remove at least the SDM minimum for the current root, and for global entries in every ASID; another slot's items are
   not in the architectural TLB until their own load checks them.
6. Blocks, L1 and links: a block runs only when current under the current slot or entered by a link that 5.5's
   invariant keeps right under it (entered by lookup, by an L1 table owned by the slot, or by a link from a block that
   was: induction over links, local ones included). Bytes are covered by the frame-keyed code protocol.

The guest may see fewer cached translations than hardware could hold, never one hardware could not hold.

**Check mode** (resident feature `check-roots`, off in measured runs): after each validation, re-walk with the
independent walker every non-global shadow of the slot and both pages of every block current under it or reachable from
one by links (local included), comparing frame, rights and global bit; check every L1 entry of the slot's tables and of
the CPL-0 tables, and every link out of those blocks (target current under the slot or reached by a local link). A
mismatch halts with the slot, the address and the record entry. It reads natemu's tables, not the Arm TLB; TLBI
omissions are covered by the negative controls in 8.

## 7. Expected gain (estimates; chunk 0 replaces them with a model run on counts)

Upper bound of today's cost caused by CR3 loads, summed over vCPUs (section 1):
- make, blocks side: `tlb` 14.3 + revalidation 9.3 + re-links (44.2 M x 99.3 / 116.6 M = 37.6) + re-misses (102 / 138.6 x
  40.7 = 30.0; assumes each revalidation followed a miss) = 91.2 s. Data side: (54.9 - 2.2) / 54.9 x 35.8 = 34.4 s (2.2 M =
  554 k #PFs x 4). Part of all this comes from INVLPG, #PF, global flushes and reclaims.
- configure: blocks 4.9 + 2.6 + (14.3 / 16.1 x 12.4 = 11.0) + (23.3 / 32.0 x 7.6 = 5.5) = 24.0 s; data (41.8 - 2.4) / 41.8 x
  24.1 = 22.7 s.
- Factors: slot hit 87.1% (make), 79.6% (configure); kept after validation 50-90% (unknown); global flushes reset the
  current slot about once per 10 loads (x 0.9).
- New costs: validation 8-29 us per load (make about 36.6 k loads = 0.3-1.1 s; configure 51.3 k = 0.4-1.5 s); recorded
  walks a few ns per level, about 0.1-0.2 s per vCPU.
- Blocks side net: make 91.2 x 0.871 x (0.5-0.9) x 0.9 - 1.5 = **34-63 vCPU-s, 5-9% of 725**; lower bound from `tlb` +
  revalidation alone (23.6 x 0.871 x 0.5 x 0.9 - 1.5 = 7.7, 1%). Configure 24.0 x 0.796 x (0.5-0.9) x 0.9 - 1.5 =
  **7-14 s of the 97 s window**.
- Data side: zero until chunk 0b or 5 removes the `NOT_ALL_GLOBAL` resets (they reset every slot); then make 34.4 x 0.871
  x (0.5-0.9) = 15-27 vCPU-s, configure 22.7 x 0.796 x (0.5-0.9) = 9-16 s.
- Link capacity: reclaims (97-99 passes per vCPU in make, about one per 93 CR3 loads) undo every slot's links; retained
  links fill the area sooner, so chunk 1b sizes it first.
- Chunk 1 alone: configure's #PF scan, 2.67 s, to near 0. Chunk 0b alone (kept half kept at every load, as chunk 14
  intended): an unknown part of the 34.4 / 22.7 s data side, measured directly.
- Memory per vCPU (estimate): records 4 MiB (cap) + shadow pool 8 MiB + L1 4 tables x 2.5 MiB (code, owners, lists) +
  link records +2 MiB at 256 Ki (+8 MiB at 512 Ki): about 24-30 MiB, 96-120 MiB at four vCPUs, touched lazily.

## 8. Tests

New `tools/run_k1.py` programs (its body: each CPU i has tables A and B and calls code at V; `tools/bench/smp/programs.py`),
at 1-4 vCPUs and block sizes default and 1; QEMU-compared where x86 fixes the answer, natemu-only otherwise. "Away": CPU i
has loaded B; "a peer": CPU (i + 1) % N (itself at N = 1), synchronized by a flag word.
1. `away-remap`, `away-remap-data`: V's PTE (or data page D's) in A changed by a peer while away; back in A, no INVLPG:
   the new frame's code (value).
2. `away-aba`: V's PTE changed F1 -> F2 -> F1 while away: F1.
3. `away-abit`, `away-dbit`: A (or D after a store) cleared while away; back: the access runs and sets the bit again.
4. `away-cow`: D made read-only while away; back: the store takes #PF, the handler maps a copy, the old frame is unchanged.
5. `away-pde`, `away-pde-large`, `away-pml4`: V remapped through a new PT page, a 2 MiB leaf, or a new PDPT while away.
6. `root-reuse`: A's root page rewritten as another hierarchy (same CR3 value) mapping V to F2: F2.
7. `active-peer`: a peer changes V's PTE while CPU i runs under A, no INVLPG; CPU i reloads CR3 = A: F2.
8. `mixed`: under A, V's PTE -> F2 (no INVLPG), INVLPG V and call (built from F2), PTE back to F1 (no INVLPG), CR3
   reload of A: F1. `mixed-invlpg`: the same with a final INVLPG of V before the reload: F1, and the counter shows no
   validation drop for V (the INVLPG cleared the mark).
9. `pde-fault-populate` (finding 3): V's PDE cleared with no flush, a load from another page of the region faults (the
   walk sees the PDE not present), the handler installs a new PT page mapping V to F2 with no flush, CR3 reload: F2.
10. `away-link`, `away-l1`: 1 through a direct jump from another page and through a computed jump (L1).
11. `slot-share`: X on a frame mapped at one address in A and B, Y mapped only in A at X's target; X -> Y linked under A;
    under B, Y's address maps F2: X reaches F2's code (hooks a and b).
12. `slot-local` (finding 1): W -> X local (X on W's page and frame), X -> Y linked under A (Y on another page); under B
    the same W/X page maps the same frame, Y's address another frame: W runs, X's jump reaches B's Y.
    `slot-local-large` (second round): the same with X's page mapped by a 2 MiB page under A and by 4 KiB pages of the
    same frames under B; under A an INVLPG of another 4 KiB address of the large page cuts X; under B, W runs, and X's
    jump reaches B's Y.
13. `global-link` (finding 2): a global block X jumping into a non-global Y under A; under B, Y's address maps F2: F2.
    `global-local-flush` (third round): non-global W (its second page non-global) with a local link into Y on its first,
    global page; W current under A; CR4.PGE toggled; Y revalidated under B as non-global (G cleared there) and linked
    onward to Z, current only under B; back in A, W runs: its jump must reach A's Z (Z's address maps another frame).
    `global-flip` (fourth round): W crosses from page P1 into P2, Y lies on P1 alone, W -> Y local under A, both PTEs
    with G clear; the guest sets G in both with no flush, INVLPGs P2 (cutting W, not Y), and W is revalidated as global;
    Y -> Z linked under A; under B (Z's
    address maps another frame), W runs and its jump must reach B's Z.
    `lend-out` (third round, only if lending is built): T, lent A's slot by S -> T, holds T -> Z made under B; under A,
    S -> T -> Z must reach A's Z.
    `global-flush-flagged` (second round): a global shadow of a lower-half page under A (G set, PGE on, so A is flagged);
    under B, the PTE changed and CR4.PGE toggled off and on; back in A: the new frame's value.
14. `global-invlpg-slots` (finding 4): a global kernel-half data page read under A and under B (both ASIDs cache it); its
    PTE changed and INVLPG under B; back in A: the new frame's value.
15. `pat-slot` (finding 6): PAT entry changed with no flush while away, then a CR3 load to A: the counter shows A reset.
16. `slots-rr`: K + 1 roots round robin, each remapping its V on alternate rounds; `record-full`: a root past the record
    cap; `pf-fill`: a #PF-driven map (no INVLPG) survives away and back with no second fault; `global-kept`: a global V
    remapped with no INVLPG keeps the old frame (allowed, asserted, as `place-global`).
17. `cr3-litmus` (finding 5), N = 2, many rounds: CPU 0 stores flag = 1, reloads CR3 = A, loads from D; CPU 1 stores D's
    PTE -> F2, MFENCE, loads flag. Forbidden: CPU 1 read flag 0 and CPU 0 read F1's value.
18. Existing: every `run_k1.py` program, `run_slice3_vm.py sharedpt coherence smp`, `run_m1.py`, `run_x3.py`, `run_x4.py`;
    abi `shadow.rs` tests extended for slots (free list, per-slot reset, TLBI before reuse, private-target checks).
- Host DMA: a `run_io_window_cases.py` case writing a page-table page of an inactive root by a block-device read; back in
  that root: the new mapping.
- Linux: a peer-edits-inactive-root stress (threads of one process `mmap`/`munmap`/`mprotect` one region while
  processes on the other vCPUs switch, each thread checking its data); stress-ng's `fork`, `vm`, `mmap`, `mremap`,
  `madvise`, `brk`, `stack`, `exec`, `clone` through `tools/correctness/stress-campaign.sh` at N = 1 and 4 with check mode;
  the LTP mm subset (`tools/correctness/ltp-list.py`); `tools/run_io5.py run` (the CPython build and its tests, judged).
- Mutations (scratch residents; each must turn its test red): validation skipped (1, 7); every difference taken as
  A/D-only (1); `used` ignored (9); `mixed` dropped (8); the mixed check after the `used` exemption (9); stamp compared
  with `>=` (16); dispatcher slot not rebound (10); hook a (11, 13), hook b (11), hook b skipping local points (12); a cut leaving inbound local links (`slot-local-large`); flagged slots skipped at a global flush (`global-flush-flagged`); a local link allowed across `GLOBAL` flags (`global-local-flush`); lending without hook (b) on T (`lend-out`); a block turned global keeping points into non-global targets (`global-flip`);
  `VAE1` instead of `VAAE1` in the shared half (14); PAT outside the controls (15); no barrier before validation (17,
  probabilistic, many rounds); no `TLBI ASIDE1` at slot reset (16, data side); inbound list not walked at INVLPG
  (`invlpg-page`).

## 9. Measurement gates

- Profile: `tools/run_io5.py profile`, steps `make` and `configure`, from the same snapshot disk, base and change
  interleaved, perf runtime; per bucket and counter. New counters: slot hits, recycles, controls resets, validations
  (pages, bytes, entries by class), overflows, alias resets, shadow resets by cause, `NOT_ALL_GLOBAL` setters by caller,
  global-into-non-global link refusals, link reclaims, shootdowns.
- Standing benchmark suite at the end only (installer, zstd, hackbench, locks, build at N = 1-4), three interleaved
  rounds; no cell slower beyond its spread.

## 10. Chunks

0. **Count and model** (perf build, no behaviour change): record and validate as 5.2 at each CR3 load without keeping
   anything, and run a K-slot LRU model with today's events applied as this plan treats them (a global flush resets
   the current and flagged slots only; shadow resets by cause; link reclaims): what would be kept, local links refused
   or undone for lack of slots (5.5's optional rule), table pages per root (p50, p95, max), compare bytes, changed entries by class.
   Gate: modeled make saving at least 20 vCPU-s or configure at least 8 s.
0b. **Why the kept half is lost at 58-68% of CR3 loads**: count `NOT_ALL_GLOBAL` setters by caller and address; fix the
   cause if it is natemu's (for example the all-ones page) or plan chunk 5. Ships on its own; gate on `abort` + `shadow`.
1. **Inbound index** (5.5), no slots. Gate: configure's #PF scan 2.67 s to under 0.2 s, no slower bucket. Ships alone.
1b. **Link capacity**: `MAX_LINKS` 256 Ki to 512 Ki (and 1 Mi if needed), a reclaim that prefers links of sources not
   current under any live slot. Gate: make's `link` + `patch` (reclaims undo ~71 M links today). Ships alone.
2. **Shadow mode**: the record kept and validated at every load and walk while still flushing as today; after each load,
   every block and data shadow the record would have kept is re-walked when next used and must give the same frame and
   rights (a disagreement counter). Gate: zero disagreements over make, configure, `run_k1.py` and the stress-ng mm
   stressors. It checks the record only; L1 and link errors are for check mode and the tests of chunk 3.
3. **Slots for blocks, L1 and links** (5.1, 5.3-5.5), data shadows still reset at each load. `--root-slots`. Gate:
   `tlb` + `revalidate` + `link` + `patch` + `miss` drop by at least half of chunk 0's model.
4. **Data shadows per slot** (5.6), after 0b or 5. Gate: `abort` + `shadow` drop by half the model; shootdowns counted.
5. **Per-top-entry split of the shared half** (PCID plan 3.6), if 0b cannot remove the cause.
6. **Tune K and caps**; remove the K = 0 path only after the standing suite and an IO5 `run` pass.

Each chunk: its tests from 8 and their mutations, an independent review of the diff, then its gate.

## 11. Lazy variants (owner's question)

**First-touch validation of data shadows per 2 MiB region.** At a load the slot's in-use level-2 table descriptors are
parked (a few dozen per process) and both slot ASIDs invalidated (`TLBI ASIDE1`: the Arm TLB may still hold the slot's
entries from before it left); the first access to a parked region aborts, the resident compares that path's entries
(PML4E, PDPTE, PDE) and the region's PT page with their copies (5.2's rules), drops changed entries' shadows and
reattaches the region's leaf table, all 512 retained shadows at once.
- Exact: each compare is a walk at a time after the load (after its barrier, in program order on this vCPU), which is
  as good as a walk at the load; shared-half maintenance is unaffected (the shared half is never parked).
- Parked descriptors stay part of the slot's tables for every other path: INVLPG and #PF unmaps, `descriptor()` and
  the writable-bit superset must reach shadows under a parked descriptor, and the abort path must tell a parked region
  (reattach after the compare) from a shadow miss (walk and install).
- Cost: per region touched, about 0.3 us abort + 2 x 4 KiB compare (~0.2-0.3 us) + reattach and its TLBI; at 20-40
  regions per slice about 10-25 us per load, plus the ASID-wide TLBI and Arm TLB refills; eager costs 8-29 us with no
  aborts. Lazy wins only when a slice touches few of the recorded regions.
- Blocks, L1 and links cannot be lazy: linked and L1 entries never pass through the resident, so the PT pages that carry
  current blocks must be validated eagerly; lazy applies to data-only regions (a per-page "carries blocks" bit).
- The fault-around effect (one abort per 2 MiB instead of one per page) applies only to retained shadows, which eager
  validation leaves attached with no abort at all; a fault-around for new shadows is a separate idea.
- **Adopt later**, as the data-shadow validation of chunk 4, if chunk 0 shows recorded pages per root well above regions
  touched per slice (eager compare above about 30 us per load, or above a quarter of the modeled saving).

**Entry patch instead of unlinking.** Every link enters a block at `Block::entry` (absolute links,
`cache.rs:1573`; a PIC block's full entry runs into the same word), checked links at a dispatcher slot, L1 at
`full_entry`. So an `svc` written over a stale block's entry word would catch every entry (the trap's ELR names the
block), and its revalidation would restore the word, one code write each way instead of one per inbound link.
- Without slots it is exact (one epoch: a restored block is current for everyone who links into it).
- With slots it is not enough: at a restore under q the block is current under q only, while its inbound links may
  come from blocks current under other slots; making that safe needs those links found, which is the inbound index
  anyway. It also adds a resident trap at the first entry after every cut (about 0.2-0.3 us) and IC maintenance twice.
- **Reject** for this plan; reconsider only if chunk 1's inbound unlinking shows up in the profile.

## 12. Review response

| Finding | Resolution |
| --- | --- |
| B1 local links bypass the invariant | Fixed: the invariant covers local links (5.5); hook (b) undoes local points into blocks lacking the new slot; local links lost their exemption entirely in the second round (row below). Tests `slot-local`, mutation "hook b skipping local points". |
| B2 hook (a) dropped slot bits | Fixed: a link is made only if the target is current under all the source's live slots; a global source never links into a non-global target (counted in chunk 0; PTI kernels out of scope). Tests `slot-share`, `global-link`. |
| B3 not-present exemption overrode the mark | Fixed: a per-entry `used` mask replaces "not present" as the no-dependents test, and `mixed` always drops the range. Test `pde-fault-populate`, mutations for both. |
| B4 shared half invalidated per ASID | Fixed: `TLBI VAAE1` / `VMALLE1` for shared-half maintenance (5.6). Test `global-invlpg-slots`, mutation `VAE1`. |
| B5 no barrier before validation | Fixed: a full barrier at the load, top-down compare with `dmb ishld` between levels (5.1, 5.2). Litmus `cr3-litmus`. |
| B6 PAT writes | Fixed: PAT is in each slot's controls; a PAT write resets the current slot and makes other slots reset at their next load (5.7). Test `pat-slot`. |
| B7 estimate | Revised (7): the data half counts only after chunk 0b or 5 (now required for it), the current slot is lost at global flushes but inactive slots survive them (5.7), link capacity gets chunk 1b, and chunk 0 runs a K-slot model with those events before its gate. |
| Second round, B1 not closed: the large path of `cache::invlpg` cuts a target but not its 4 KiB local source | Fixed: (c) undoes every inbound link, local ones included, at every cut, so a block entered by any link is current under the running slot (5.5). Test `slot-local-large`, mutation "a cut leaving inbound local links". |
| Second round: global shadows in a flagged slot outlived a global flush | Fixed: a global flush and `tables_written` reset every flagged slot as well as the current one (5.7); global blocks need nothing. Test `global-flush-flagged`, mutation. |
| Third round: a local link from a non-global source into a global target escaped the global flush's pass | Fixed: rule (a) makes a link local only when both flags match, so the pass over non-local records into global targets reaches every link into a global block (5.5). Test `global-local-flush`, mutation. |
| Third round: slot lending skipped hook (b) | Fixed: lending runs hook (b) on T for each lent slot; also restricted to plain local links and calls `mark_large(T)` (5.5). Test `lend-out`, mutation. |
| Fourth round: a block revalidated as global kept its local links into non-global targets | Fixed: becoming global counts as gaining every slot, so hook (b) undoes every point into a non-global target (5.5). Test `global-flip`, mutation. Wording: the global-flush note now says local links from global sources stay in the LOCAL segment and are covered by the source's own loss of currency and hook (b). |
| Non-blocking | Adopted: compare cost 8-29 us; memory recomputed (24-30 MiB per vCPU); `mixed` and `used` cleared by INVLPG/#PF of the page; `used` instead of not-present; shadow mode re-walks data shadows and states it cannot see L1/link errors; check mode covers local-link reach and CPL-0 L1; `cr3-litmus` and the peer-edits stress; mutations for B1-B6; tag-check cost labelled unmeasured; self-mapping tables reset every load. Second round: `used`/`mixed` cleared only on p's leaf entry (5.2); 5.5's
optional slot lending for local links (counted in chunk 0); the lazy variant's parked descriptors reached by every
unmap path and told apart at an abort (11); chunk 0 models the new global-flush rule. Not adopted: replacing the CR3-load link pass in chunk 1, because that segment holds only links a slot-less CR3 load must undo anyway (5.5). |

## 13. Risks and stop rule

- The kept fraction may be small if processes rewrite their tables while descheduled; chunk 0 models it first.
- The data half depends on chunk 0b or 5; more retained writable shadows mean more peer shootdowns, each resetting every
  slot; more ASIDs mean more Arm TLB refills of the resident's private (nG) region (PCID plan 3.3).
- Memory: 24-30 MiB per vCPU (estimate). Complexity in `Space` (free list, per-slot roots) and the link records.
- Stop rule: stop after chunks 0b, 1 and 1b if chunk 0's model misses its gate. Revert a chunk whose gate fails after one
  round of diagnosis. Any shadow-mode disagreement or check-mode halt traced to the record's design (not a coding slip)
  stops the work for re-review of 5.2, 5.5 and 6.

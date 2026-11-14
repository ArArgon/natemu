# Shadow fault-around: install a page's present, already-accessed neighbours at one abort

Status: design, not built (November 14, 2026), branch `perf-real-workload`. Roadmap item 8 of the
[architecture review](architecture-review-20261113.md) (root cause E's abort cost, group F), kept by the census
([io5-perf-notes](io5-perf-notes.md) decision 15: aborts per table page per epoch 26 / 101; the guest's A bit already set in
98-99.96% of installs; D set by the walk in 571 installs per run). Buckets and counts are from the current head profile
`natemu-raw/fm-on-profile-1` (page record and fast miss path on; sums over four vCPUs). Line numbers are those of `c4b783c`.
Every number marked *estimate* has its arithmetic beside it. No code or VM was touched for this plan.

## 1. The problem in one paragraph

A guest data access to a linear page with no shadow aborts from EL0 into the resident, which walks the guest tables for that
one page and installs one shadow leaf. The guest's own page tables are already populated around it: Linux maps file pages
sixteen at a time (fault-around, `fault_around_bytes` = 64 KiB) and, on x86, marks the prefaulted entries young (only arm64
asks for old prefaulted PTEs), fork copies a parent's populated tables, and the kernel's direct map and text are 2 MiB pages
with A and D set from the start (`__PAGE_KERNEL` carries both). So after a CR3 load, or on a process's first touch of a
region, natemu takes one abort per 4 KiB page for pages whose translations the guest tables already hold complete, with
A = 1: 15.2 M aborts on configure, 40.1 M on make, about 300 per CR3 load. A hardware TLB fills one entry per miss too,
but a miss costs it tens of cycles, not an exception; and real x86 prefetches translations of pages the program has not
touched (section 4). This plan installs, at one abort, the neighbouring shadows the same guest table page already
justifies, under rules that leave every guest-visible bit exactly as hardware and QEMU would.

## 2. What an abort costs today (head profile, four vCPUs summed; buckets exclusive)

| | configure (108.2 busy vCPU-s) | make -j4 (537.1 busy) | What the bucket holds |
| --- | --- | --- | --- |
| `abort` | 5.22 s, 15.16 M, **344 ns** (4.8%) | 13.81 s, 40.09 M, **345 ns** (2.6%) | `aborted` (`execution.rs:3400`): ESR/FAR/ELR, the block and site search, `take_invalidations`, the site checks, two `tables::descriptor` walks for the no-progress test, `write_record`, `resume`; minus the walk |
| `shadow` | 4.59 s, 15.17 M, **302 ns** (4.2%) | 10.67 s, 40.16 M, **266 ns** (2.0%) | `translate` (`:3312`): the four-level guest walk with the code-page observer, `memory_type`, a third `descriptor`, `install` (`:4084`: `split_wait`, `space()`, `ram.range`, `at s1e1r`, the three-level shadow walk, the store, `dsb ishst; isb`) |
| `delivery` | 2.84 s, 2.53 M, 1,124 ns (2.6%) | 3.36 s, 2.67 M, 1,256 ns (0.6%) | a guest exception delivered (mostly #PF): untouched by this plan |
| `tlb` | 3.94 s, 54 k, 73 us (3.6%) | 12.63 s, 39 k, 322 us (2.4%) | a CR3 load or global flush: L1 emptying, link undo, `reset_cr3`; shadows go here, and fault-around adds no work to it |

So a shadow-miss abort costs **0.65 us (configure) / 0.61 us (make)** inside the buckets, plus the exception entry and
return and the guest's cold caches and predictors after it, which the review charges to `guest` (unmeasured; the chunk 9
figure for a miss was about 990 cycles before its fix).

**What the 15.16 M / 40.09 M aborts are** (K1 and state counters of the same logs):
- `retries` (an access resumed after a shadow install) **11.50 M / 27.94 M**: the population this plan can shrink. Of them
  `upgrades` (a read-only shadow made writable by a later write) **1.98 M / 5.73 M**, so first installs are about
  **9.52 M / 22.21 M**.
- `deliveries` 2.53 M / 2.67 M: genuine guest #PFs. Not savable (the page has no translation yet). But each is followed by a
  retry that *is* in the population, and, for a file page, Linux has just populated its 15 neighbours young.
- The rest, 1.13 M / 9.48 M: split-store stubs (a page-crossing store traps at its check, shadows or not), SIMD/x87 checks,
  device accesses, UC steps (1,307). Not savable here.
- `page_faults` 2.39 M / 2.49 M; `invlpgs` 611 k / 431 k; `cr3_loads` 50.8 k / 35.6 k (every one a `kept_reset`: the kept
  half held only global shadows); `global_flushes` 3.3 k / 3.6 k; `shadow_resets` 4.8 k / 5.4 k; `shootdowns` 157 / 184.

The census (decision 15, `census-profile-1`, counts of the same shape): distinct (epoch, leaf table page) pairs among
aborts 549 k / 469 k, i.e. **25.9 / 100.6 aborts per table page per epoch**; the walk set the leaf's A bit in 281 k / 19 k
of 14.2 M / 47.2 M installs (**A already set in 98.0% / 99.96%**); it set D in 571 on each step (the same count on both,
so from the shared boot and resume, effectively none inside the steps): the guest sets A and D itself at its own page
faults, as Linux does on x86.

## 3. Design

### 3.1 The rule, in one sentence

At a demand install for linear page `va` (the existing path, unchanged), also install the shadows of the other pages of
`va`'s aligned window of W pages inside the same guest leaf table (or the same 2 MiB leaf) whose guest entries are present,
reserved-bit clean and **already accessed (A = 1)**; each read-only unless its entry has **D = 1** and grants the write;
never for a page that is not guest RAM, UC, held by a split lock, already shadowed, or (in the kept half) non-global;
nothing is written to the guest tables.

### 3.2 Where it hangs, and what the walk must hand over

`translate` (`execution.rs:3312-3391`) returns after `install(s, va, t.gpa, writable, global)` (`:3374`). The one new call
goes right after it: `fault_around(s, va, &t, kind)` (a new `execution/fault_around.rs`, 150-200 lines). It needs from the
walk what the walk already knows and discards: for a 4 KiB leaf, the leaf table's guest-physical base (`table` at level 1,
`paging.rs:219`) and the R/W and U/S bits accumulated over levels 4-2 (`rw`, `user` before the leaf is folded in, `:232-233`);
for a 2 MiB leaf, nothing more (one entry covers all 512 pages). `Translation` (`paging.rs:38-51`) gains `table: u64`
(0 for a 2 MiB leaf) and `parent_rw: bool`, `parent_user: bool`. Host unit tests cover the three fields.

Conditions under which `fault_around` does nothing (each counted, section 7):
- the block is a step block (`b.flags & STEP`: a UC page shadowed for one instruction, `:3353`) or the demand page was not
  shadowed (`uncached`);
- a data breakpoint is armed (`s.cpu.dr[5] & DR7_ENABLES != 0`): the debug paths (`watch.rs`) stay exactly today's;
- `Control::fault_around` is 0 (the host flag `--fault-around off|8|16|32|64|512`, as `--page-currency`, `host
  execution.rs:3295`; the default is the window chunk 0 picks).

### 3.3 Which neighbours, and what each gets

Window: the W-aligned run of W pages containing `va` inside the leaf's 2 MiB (index `i0 = (va >> 12 & 511) & !(W - 1)`).
Aligned, not centred: it is one or two cache lines of PTEs, and it is how Linux populates (64 KiB aligned), so the fifteen
entries Linux just wrote young at a file fault are exactly the window of the retry that follows. W is the same for both
leaf kinds unless chunk 0 says otherwise (a 2 MiB leaf has no PTE reads and no permission differences between its pages,
so its break-even window is wider).

For each index `j` in the window, `j != i`:
1. **Already shadowed** (the leaf descriptor is valid): skip. The fill never replaces an entry, so no break-before-make and
   no TLBI; one `dsb ishst; isb` publishes the whole batch (3.4).
2. **4 KiB leaf: the guest entry.** One plain 64-bit load of `table + 8 j` from guest RAM (`guest_ram().load64`), with no
   observer: the demand walk recorded the table page's dependency already (`table_read`, same page), and the fill writes
   nothing, so no `CodeWrites` hook can fire. Require: present (bit 0); reserved bits 51:40 clear, and bit 63 clear when
   `nxe == 0` (`paging.rs:225-226`; a reserved entry is a #PF on use, which hardware does not cache, 4.10.2.2); **A set
   (bit 5)**. Permissions: `user = parent_user && bit 2`; at CPL 3, skip unless `user`; `rw = parent_rw && bit 1`;
   `writable = bit 6 (D) && (rw || (cpl == 0 && wp == 0))` (the leaf rule of `walk_once`, `:237-238, :257`, for a write).
   `gpa = entry & PA_MASK`, `pat` from bits 7, 4, 3 (`:261`), `global = bit 8 && CR4.PGE` (`:3374`'s rule).
3. **2 MiB leaf.** `gpa = leaf_base + 4096 j`; `writable = t.dirty && t.writable` (the PDE's D and the walk's grant: one entry,
   one A, one D for all 512 pages, so the A/D question does not arise); `pat`, `global` from the PDE.
4. **RAM.** Skip unless `mmio::ram(gpa)` (`mmio.rs:30`): device pages are never shadowed (M3), unassigned pages are shadowed
   onto the all-ones page only once a read asks the host about them (`mmio::ones`, L3), and the legacy hole 0xA0000-0xFFFFF
   inside a 2 MiB leaf at guest-physical 0 is not RAM. The `Space::install` private-target check (`shadow.rs:255-261`:
   tables, state, pool, the guest-state frame page and its alias tables) is kept in the fill as defence, though guest RAM
   never reaches those IPAs.
5. **Memory type.** `kind = cpu::memory_type(msrs(), gpa, pat)` (`cpu.rs:224`), per page (MTRRs can split a 2 MiB leaf):
   UC skip (M2: UC runs one instruction at a time, shadowed only in a step block); WT or WP: read-only only (perf chunk 26:
   writes there run alone); WB, WC: as computed.
6. **Writable protocol**, exactly `translate`'s (`:3358-3365`): if `writable`, `cache::shadowed_writable(gpa)` first, then
   `cache::translated(gpa)`: if some vCPU holds translations from the frame, install read-only instead (the write then
   aborts and takes `code_written`, as today). The order (bit before marks) is what lets a peer's `mark` either see the bit
   or be seen (`mark`, `:1641`). **Supervisor space: read-only only, for now** (3.6 says why; chunk 0 counts what it costs).
7. **Split lock.** Skip a page `cache::split_held(gpa)` (no wait: the demand page's `split_wait` already set `holding()`).
8. **Kept half.** In the supervisor space's upper half a non-global shadow must be listed for the next CR3 load
   (`shadow.rs:264-274`, `LIST_MAX` 64; past it every CR3 load resets everything, decision 4's -15%). A fill there installs
   **global neighbours only**; non-global ones are skipped (`Space::fill` refuses them, so the rule is where the list is).
9. **Install** into the space of the demand access's CPL (`tables::space(user(cpl))`), `PAGE | NOT_GLOBAL | READ_ONLY?`
   with the page's IPA (`ram.range` then `native`, as `tables::install`, `tables.rs:106-107`; chunk 1 may replace the
   `at s1e1r` per page by the alias's arithmetic if `GuestRam` exposes the region's IPA, measured).

Nothing else changes: not `last_retry`, `retries`, `upgrades` (a write to a read-only prefetched page is an upgrade,
counted as today), not the `uncached` list, not the code-page marks.

### 3.4 `Space::fill` (abi `shadow.rs`, about 40 lines, unit-tested)

`fill(&mut self, region: u64, entries: &[(u8 index, u64 ipa, bool writable, bool global)]) -> Result<usize, Error>`:
`leaf(region, false)` once (never allocates: the demand install just created the table; `Ok(None)` means skip all), refuse
`kept(region) && !global` entries, refuse private targets, write only into slots whose descriptor is 0, then one
`tlb.publish()` if anything was written. Returns the count written. The invariant "a kept-half non-global shadow is listed
or `NOT_ALL_GLOBAL` is set" is unchanged because `fill` never creates one.

### 3.5 Interaction with everything that removes or relies on shadows

| Event | Prefetched shadows | Why it is unchanged |
| --- | --- | --- |
| INVLPG m (`:2844-2857`) | `tables::unmap_large` drops the whole 2 MiB region in both spaces, prefetched or not | region-granular already; SDM 4.10.4.1 lets INVLPG drop more than the page |
| #PF at CR2 (`page_faulted`, `:1725`) | `tables::unmap(cr2)` drops the faulting page only; neighbours stay | a #PF invalidates entries for the faulting address; other pages' entries may stay (4.10.4.1), as demand shadows do today |
| MOV CR3 (`tlb`, `:1701`; `reset_cr3`) | lower halves and the user space go; kept-half global ones stay; non-global kept ones were never prefetched | the TLB keeps global entries across CR3 (4.10.4.1); the list is untouched (3.3 item 8) |
| Global flush (CR0/CR4/EFER/MTRR, `tables_written`) | everything goes (`reset_shadows`) | as today |
| Capacity (`Error::Capacity` in `install`) | the fill never allocates, so never evicts | the demand install's eviction path is the only one |
| Peer PTE edit before its shootdown IPI | a stale prefetched shadow may serve until this vCPU's INVLPG/CR3 | a TLB entry may be used until invalidated (4.10.2.3); today's demand shadows have the same window |
| Peer `mark` of a frame (code placed from it) | the frame's writable bit (set at the fill) makes `mark` ask this vCPU for a reset, as for a demand writable shadow | the protocol keys on the bit, not on how the shadow came to be (`mark`, `:1641-1661`) |
| Split lock (`split_begin`) | peers reset all shadows, prefetched included | as today |
| Code write through a prefetched writable shadow | impossible for a frame marked at fill time (read-only then); a frame marked later is handled by the shootdown above; a frame this vCPU itself later fetches from takes `reset_shadows` in `mark` (`writers & own`) | `code_written` and SMC detection unchanged |
| Page record (decisions 24-26) | none | the record caches fetch walks for code; this plan installs data shadows and writes no table entry; neither reads the other |
| Fast miss path, `uncached_end`, no-progress test | none | they read `descriptor` and `shadow_generation`, which move as today |
| Watchpoints (`watch.rs`) | disabled while DR7 arms one (3.2) | the step-and-rearm protocol stays identical |
| UC pages | never prefetched | M2's "one instruction alone" holds |

### 3.6 Why the supervisor space gets read-only neighbours first

Writable shadows carry a per-frame writable bit (`WRITABLE_PAGES`), and any vCPU placing code from that frame later asks
every bit holder for a full shadow reset and waits (`mark`; decision 31 found peer kicks of this family cost milliseconds
when the drain is slow). The kernel's direct map is 2 MiB, global, RW, A = 1, D = 1: a writable fill there would set the bit
on hundreds of frames per abort, among them page-cache frames about to be executed by a new process, so every exec could
shoot down every vCPU that merely prefetched. Today's shootdowns are 157 / 184 per run. The user space has no such
exposure (its text is RW = 0 and never writable; JITs are rare in these workloads), so user neighbours are writable where
D = 1 and the entry grants it. Chunk 0 counts the supervisor write-after-read upgrades a writable fill would save and the
frames it would bit-mark that are later marked as code; if the first is large and the second near zero, chunk 1 lifts
the restriction (the switch is one boolean).

### 3.7 Chunk 1 by itself: writable on a read when D is already set

Independent of any window and worth doing first: `translate` installs read-only on a read even when the leaf has D = 1
and grants the write (`writable = access == Access::Write && t.writable && t.dirty`, `:3354`). A TLB entry filled by a read
carries the entry's permissions; a later write through it with D = 1 sets nothing (4.8). Changing the rule to
`t.writable && t.dirty && !matches!(kind, UC | WT | WP)` (the write-side memory-type rule, 3.3 item 5) saves the second
abort of every read-then-write page whose D was set already: a process's stack and heap after a context switch (D set
by its earlier writes), the kernel's direct map. Same writable protocol, same spaces, nothing else. Up to 1.98 M / 5.73 M
upgrades (13-14% of aborts); chunk 0 counts how many had D = 1 at the read.

## 4. Correctness argument against the SDM, and the real-x86 tiebreaker

Claim: with fault-around on, every guest-visible state (memory, A/D bits, faults and their error codes, memory ordering) is
one some x86 processor may produce from the same program, and QEMU TCG's result is among natemu's accepted outcomes in every
K1 program whose outcome the architecture fixes.

- **A TLB may hold any translation the tables presently permit, however it got there.** SDM Vol.3 4.10.2.3: the processor
  may cache translations required for prefetches and for accesses that are a result of speculative execution that would
  never actually occur in the executed code path, and may use a cached translation until it is invalidated. A prefetched
  shadow is a translation the tables permit at fill time (present, reserved clean, the walk's parents plus the leaf's
  bits), filled in the space whose privilege the entry grants (U/S checked at CPL 3; a supervisor entry is never put in the
  user space).
- **Nothing is cached that the processor could not cache.** 4.10.2.2: no entry is made for a not-present entry or one
  with a reserved bit set; a fault is never cached. The fill requires both, and installs no all-ones or device page.
- **A.** 4.8: whenever the processor uses an entry for a translation it sets A if clear; the processor also sets A for
  the translations it caches ahead (4.10.2.3's prefetches walk the tables). Natemu takes the stricter choice: it caches
  only entries whose A is set, so no bit is written that the program's own accesses would not have written, and A reads back
  exactly as under QEMU (which caches nothing it did not access). The census says this costs nothing: A is set already in
  98.0-99.96% of installs.
- **D.** 4.8: D is set at the first write through an entry; 4.10.2.3: a TLB entry records D, and a write through an entry
  cached with D = 0 sets D in the paging-structure entry before the write. A neighbour with D = 0 is installed read-only,
  so its first write aborts, walks with write intent, sets D once (`walk_once:249-251`) and upgrades; a neighbour with D = 1
  installed writable needs no further write to the entry, and hardware would make none. The 2 MiB case has one D for the
  leaf, set or not for all 512 pages alike.
- **Permissions.** A prefetched shadow grants at most what `walk_once` would grant the same access at the same CPL under
  the same CR0.WP (3.3 item 2 uses its leaf rule with the parents' bits); the shadow's space is the access's privilege
  class, as for every demand shadow; SMEP concerns fetches, which never use shadows; natemu implements no SMAP (none in
  `Control`), as today.
- **Invalidation.** INVLPG, #PF, MOV CR3 and the global flushes remove prefetched shadows at least as eagerly as the
  SDM's 4.10.4.1 rules remove the corresponding TLB entries (3.5): the one way natemu keeps more than hardware must
  (a stale translation after an un-invalidated PTE edit) is permitted and already relied upon (`no-invlpg`, `global`,
  `place-global`, `invlpg-page`, the page-currency plan's 3.5).
- **Memory type and ordering.** Each page's type is computed as for a demand shadow; UC is never shadowed, WT/WP never
  writable, so every native access runs with an ordering at least as strong as its type's (M2, perf chunk 26).
- **Multiple vCPUs.** A peer's PTE edit is visible at this vCPU's next INVLPG/#PF/CR3 (the guest's shootdown), a window
  the TLB rule allows; the code-page protocol keys on writable bits set before the marks are read, identical to a demand
  writable install; DMA into table pages ends in `tables_written`, a global flush.
- **Real x86 as the tiebreaker** (owner's rule): Intel cores since Ivy Bridge run a next-page prefetcher that touches the
  following page ahead of the program and so fills its translation before any instruction addresses it, and modern cores
  fill TLB entries on speculative paths; software that depends on an untouched page having no TLB entry is broken on real
  hardware. Natemu's fill is narrower (A = 1 only, same leaf table, no bit written) so it stays inside what QEMU produces
  too, which keeps the K1 comparisons exact rather than "either".

The guest may see fewer cached translations than hardware could hold, never one hardware could not hold.

## 5. Cost per event and estimated gain (estimates; chunk 0 replaces h, k, u and measures c_n)

**Per neighbour installed (c_n, estimate):** one PTE load (same 64-byte line for 8 entries; a 2 MiB leaf reads none),
bit tests, `ram.range` (a four-region compare), `memory_type` (an MTRR scan, 10-20 ns), `split_held` (three loads), the
`at s1e1r` + `isb` + `par_el1` (20-40 ns, or arithmetic), the descriptor store: **40-60 ns read-only**; writable adds the
shared-word atomic `fetch_or` and the `translated` load: **80-110 ns**. Per fill: `space()` (two `mrs`), one `leaf` walk
(three loads), one `dsb ishst; isb` (20-50 ns). So a window of k installed neighbours costs about 50 + k x (40-110) ns.

**Per abort saved:** 0.65 / 0.61 us in the buckets, plus the unmeasured entry/return and cold-state cost (0.1-0.3 us by
the chunk 9 figure); planning value **0.75 / 0.7 us**.

**Model.** N = first installs today (9.52 M / 22.21 M); h = the share of them a window of an earlier abort in the same
(epoch, table) would have covered; k = neighbours installed per remaining abort; c_n = 0.05-0.1 us.
Net = N x [h x 0.75 - (1 - h) x k x c_n] (configure; 0.7 for make).

| h | k | c_n (us) | configure net (s) | make net (vCPU-s) |
| --- | --- | --- | --- | --- |
| 0.3 | 6 | 0.08 | 9.52 x (0.225 - 0.336) = **-1.1** | 22.2 x (0.21 - 0.336) = **-2.8** |
| 0.5 | 6 | 0.08 | 9.52 x (0.375 - 0.24) = **+1.3** | 22.2 x (0.35 - 0.24) = **+2.4** |
| 0.6 | 8 | 0.06 | 9.52 x (0.45 - 0.192) = **+2.5** | 22.2 x (0.42 - 0.192) = **+5.1** |
| 0.7 | 10 | 0.08 | 9.52 x (0.525 - 0.24) = **+2.7** | 22.2 x (0.49 - 0.24) = **+5.6** |

The census's 26 / 101 aborts per table page per epoch give the bounds. Spread uniformly over 512 entries, a 16-window
around an abort holds 0.8 / 3 other aborts, and fault-around loses on configure; clustered as stacks, heaps, data
segments and Linux's 16-page populations are, h is in the upper rows. For a 2 MiB leaf the per-neighbour cost is lowest
(no PTE read, one permission) and the population per table largest (the kernel's direct map): on make, filling a whole
2 MiB leaf at 101 aborts per table costs 512 x 0.04 = 20 us against 100 x 0.7 = 70 us saved; on configure (26 per table)
17 us against 20 us, so W must come from the count, per leaf kind.

**Chunk 1 (writable on read, 3.7):** u = share of upgrades whose leaf had D = 1 at the read install (guess 0.6-0.9):
1.98 M x u x 0.75 us = **0.9-1.3 s** on configure, 5.73 M x u x 0.7 = **2.4-3.6 vCPU-s** on make, at no per-event cost.

**Planning numbers:** configure **2-4 s of 108 busy (2-4%)**, about the same in wall time (serial); make **4-9 vCPU-s of
537 (0.7-1.7%)**, 1-2 s of wall. Below the review's 3-5 s / 8-14 vCPU-s, which assumed half the installs useful and no
per-neighbour cost; the count decides, and the stop rule (section 8) drops the window while keeping chunk 1 if the model
says so.

**Memory:** none (the leaf tables exist; the pool is untouched). **`tlb` bucket:** unchanged (`reset_cr3` clears roots,
not entries). **Arm TLB:** descriptors are not TLB entries; nothing fills until used.

## 6. What was rejected and why

1. **Installing A = 0 neighbours and setting A** (the hardware prefetcher's licence): an atomic RMW per entry in the guest
   tables through the code-page observer, an A bit QEMU would not set (every A/D K1 comparison turns "either"), and the
   census says A = 0 entries are 0.04-2% of installs: nothing to gain.
2. **Whole-table fill (W = 512) as the only window:** at c_n = 0.05-0.1 us a full table costs 25-50 us per abort against
   26 x 0.75 = 20 us saved on configure. Kept as one of the counted candidates, likely right for 2 MiB leaves on make.
3. **Prefetch at the CR3 load** (rebuild the returning space's shadows eagerly): root retention, rejected by measurement
   (decision 8: short processes are new roots); and it would set no A bit either, but it walks pages nobody may touch.
4. **Keeping data shadows across CR3 loads by ASID** (PCID): the pcid plan; not built (owner).
5. **Writable neighbours in the supervisor space now** (3.6): the writable-bit footprint on the direct map would turn every
   exec into shootdowns of prefetching vCPUs. Counted first.
6. **Using the page record for data shadows:** it records fetch walks (code pages, which rarely have data shadows) and
   carries neither D nor the memory type; a data fill from it would reintroduce the per-page walk it saves.
7. **A centred or adaptive window:** two cache lines of PTEs and a guest that populates 64 KiB aligned; the count compares
   aligned windows of several sizes, which is the whole decision.
8. **Prefetching the all-ones shadows of unassigned pages:** a host request per page (`REQUEST_PAGE`) and a strict mode that
   stops on them; demand-only, as today.
9. **Observing the PTE loads** (`table_read` per neighbour): the demand walk recorded the same table page; a second mark is
   a wasted atomic.
10. **A check mode** (re-walk at each use, as the page record has): a shadow's use is a native load, nothing traps; the
    equivalent evidence is the study build's coverage model (chunk 0) and the use counter of chunk 2 (section 7).

## 7. Chunks, gates, tests, mutations

**Chunk 0: count (scratch resident feature `fault-study`, implies `perf`, never `perf` itself: decision 16's rule; no
behaviour change; one Tier 2 profile on configure and make scheduled by the main session).** At each demand install
(`translate` after `install`): the (epoch, leaf table page or 2 MiB leaf, index, access, CPL, half, leaf size, D at the
read). A per-vCPU 4-way set of (epoch, table) entries (as the census's `first`), each with a 512-bit `installed` mask and a
512-bit `covered` mask per candidate window W in {8, 16, 32, 64, 512}, plus `covered_writable`. At an install at index i:
if `covered_W[i]`: `hit_W += 1` (and `hit_needs_upgrade_W` if the access is a write and the cover would have been
read-only); then read the W-window's entries once (plain loads), count those present, A = 1, not installed, not covered,
and classify the skips (A = 0, not RAM, UC/WT/WP, `translated`, kept-half non-global, U/S at CPL 3): `candidates_W += n`,
set their `covered_W` bits. Separately: `read_d1` = read installs whose leaf had D = 1 and grants the write (chunk 1's u);
`marks_on_covered_writable` = `mark` events whose frame is in a per-vCPU 1-bit-per-frame "would be writable-prefetched"
map (1 MiB of RAM pages = 128 KiB per vCPU in the lookup area's slack; cleared at each full shadow reset), split by space
(3.6's question); CNTVCT ticks of today's `tables::install` alone (c_n's floor) and of the whole `translate`. Output per
vCPU in the perf log's `k1.fault_study`; `tools/fault_study_an.py PROFILE_DIR` prints the model of section 5 per W and leaf
kind. **Gate:** some W (per leaf kind) with modelled net >= 1.5 s on configure or >= 4 vCPU-s on make, with hit rate
`hit_W / (hit_W + installs)` >= 25%; chunk 1's own gate `read_d1 x 0.75 us` >= 0.8 s on configure. Picks W (and whether
2 MiB leaves get a wider one) and settles 3.6.

**Chunk 1: writable on read (S: one rule in `translate`, counters, independent review).** Tier 0: unit test of the rule
table (D, R/W, WP, CPL, kind -> writable). Tier 1: K1 programs `fa-ad` (the D half) and `fa-ro` below, `local-write`,
`dormant-write`, the `adv_setup` set, `pic-*`. Tier 2 gate: `upgrades` down by >= 70% of `read_d1`; `abort` + `shadow`
down accordingly; `shootdowns` not up beyond noise.

**Chunk 2: the fill (M: `Translation` fields, `Space::fill`, `fault_around.rs`, the host flag, counters; independent
review before the gate).** Counters in the perf build: fills, neighbours installed (by space, leaf kind, writable), skips by
reason; and **uses**, if the core offers it: Arm hardware access-flag update (FEAT_HAFDBS, ID_AA64MMFR1_EL1.HAFDBS,
TCR_EL1.HA) lets prefetched descriptors be installed with AF = 0 and read back AF = 1 at the next reset without any fault,
giving the production hit rate for the gate's "wasted installs"; absent HA, the study build's coverage count stands in
(a descriptor with AF = 0 and HA off would fault, so the AF = 0 form is used only when HA is confirmed on).

- Tier 0: abi unit tests for `fill` (writes only empty slots; refuses kept-half non-global and private targets; one
  publish; never allocates; count returned), `Translation`'s new fields (parents' bits with and without a 2 MiB leaf), the
  neighbour rule table (present/reserved/A/U-S/R-W/D/WP/CPL/kind/G -> skip, read-only, writable); each mutation below run
  once against its test.
- Tier 1, new `run_k1.py` programs (each a few calls and CR3 loads; the runner passes `--fault-around 16` and runs
  `fa-counts` also at 512; QEMU-compared where the architecture fixes the answer):
  - `fa-ad` (QEMU): one leaf table with 32 present user pages: indices 0-15 A = 1, 16-31 A = 0, odd ones D = 1. Read page 0
    (fills 1-15 at W = 16). Read every PTE back: A/D exactly QEMU's (page 0's A set if it was clear; nothing else moved).
    Write page 2 (A = 1, D = 0: read-only prefetched): its D now set, once; a second write leaves the PTE equal. Read page
    17 (A = 0: never prefetched) then read back: its A set. Write page 3 (D = 1: writable prefetched): PTE unchanged.
  - `fa-invlpg` (QEMU): read X (fills N -> F1); N's PTE -> F2; INVLPG N; load from N returns F2's byte. Then N -> F1 again
    with a CR3 reload instead: F1's byte.
  - `fa-stale` (natemu only, as `no-invlpg`): read X (fills N); N's PTE -> F2 with no invalidation; a load from N may return
    either; INVLPG N: F2's. QEMU returns F2 at once (it cached nothing for N): the "either" set holds both.
  - `fa-kept` (natemu only): upper-half supervisor mapping (0xffff8...; the runner gains an upper-half table helper if
    none exists), PGE on; X global, N non-global, both in one leaf table, N mapped to F1 in space A and F2 in B. Read X
    under A (N must not be filled: non-global in the kept half); CR3 B; a load from N returns F2's byte, and the K1
    stats show `kept_resets` = CR3 loads (no full reset: the list did not overflow). A second phase with 70 non-global
    neighbours checks the same.
  - `fa-mmio` (QEMU): a leaf table where index 1 maps the LAPIC page (0xfee00000) and index 0 a RAM page. Read index 0;
    read the LAPIC ID register through index 1: the APIC ID (QEMU agrees); `fault_around.skipped.not_ram` = 1.
  - `fa-ro` (QEMU): N with R/W = 0 beside X, WP = 1: read X, write N: #PF error code 3 (present, write) to a handler that
    records CR2 and the code; WP = 0 at CPL 0: the write succeeds.
  - `fa-user` (natemu only, `adv_setup` style): at CPL 3 read X whose neighbour S is a supervisor page (U/S = 0 at the
    leaf): S is not filled in the user space; a CPL 3 read of S faults with error code 5. Then at CPL 0 read S (fills its
    neighbours in the supervisor space, read-only) and write one of them: an upgrade, `upgrades` = 1.
  - `fa-smc` (QEMU): N user RW = 1, D = 1, A = 1 holding `mov eax, 1; ret`. (a) Write X (fills N writable, the bit set);
    call N (fetch marks N: `writers` holds this vCPU, `reset_shadows`); store `mov eax, 2` into N; call N: 2. (b) The
    other order: call N first (marked), write X (N filled read-only: `translated`), store into N (aborts, `code_written`,
    stepped), call N: 2. Both halves equal QEMU's registers.
  - `fa-large` (QEMU): a 2 MiB leaf, PDE D = 0: read one 4 KiB page (fills the window read-only); write another page of
    the leaf: the PDE's D set once; read back equals QEMU's. A second 2 MiB leaf with D = 1: the write changes nothing.
  - `fa-uc` (natemu only): N's PTE PCD|PWT with a PAT entry of UC beside X: read X; a store to N runs alone
    (`uncached_steps` = 1), `skipped.uncached` = 1.
  - `fa-counts` (natemu only): 32 consecutive present A = 1 pages, fresh space: touch page 0 then 1..31 in order:
    `retries` = 2 and `fault_around.installed` = 30 at W = 16; `retries` = 1 at W = 512.
  - Existing programs that must stay green: `walk`, `no-invlpg`, `pf-remap`, `invlpg-page`, `invlpg-large`, `pf-large`,
    `large-reval`, `local-write`, `dormant-write`, `global`, `place-global`, the `adv_setup` set, the `pool-*` seven, the
    I/O suites (device pages beside RAM), `--check-reuse` boots (busybox 1 and 4 vCPUs).
- Tier 2 gate: `abort` + `shadow` down by >= 70% of chunk 0's modelled low end for the chosen W; `guest`, `tlb` and
  `shootdown` not up by more than half the gain; wasted installs (installed minus used, by HA or the study) under 50%;
  `shootdowns` within noise. Wall time judged in group F's interleaved rounds (items 6, 8, 9), per the review's rule.
- End of group: the standing suite and the benchmark suite once before merge (suite-only-before-merge); the applicability
  audit (the rule is generic x86: any OS that populates tables ahead of touching pages benefits; nothing keys on Linux).

**Deliberate mutations, each turning its test red:**

| Mutation | Caught by |
| --- | --- |
| M1 fill ignores A (installs A = 0 neighbours) | `fa-ad` (page 17 read back with A = 0 where QEMU has 1) |
| M2 fill installs writable with D = 0 (or ignores WT/WP) | `fa-ad` (page 2's D never set), `fa-large` (PDE D), `fa-uc` |
| M3 fill lists nothing and installs kept-half non-global neighbours (or `map_global` for them) | `fa-kept` (F1's byte after CR3 B, or a full reset counted) |
| M4 fill skips the RAM test (maps the entry's address as IPA) | `fa-mmio` (a RAM-looking read instead of the APIC ID, or `stop::SHADOW`) |
| M5 fill sets no writable bit | `fa-smc` (a) (the stale block returns 1) |
| M6 fill skips `translated` | `fa-smc` (b) (the store never traps; 1) |
| M7 fill ignores U/S at CPL 3 | `fa-user` (the CPL 3 read of S succeeds) |
| M8 fill ignores R/W (or WP) | `fa-ro` (the write succeeds) |
| M9 fill disabled (or `--fault-around off` ignored) | `fa-counts` (`retries` 32) |
| M10 fill replaces a valid slot without break-before-make, or publishes nothing | abi unit test (`fill` on a valid slot: refused); `fa-counts` under the resident checkers (a TLB that may hold the old entry: `page-mark`-style second write) |
| M11 chunk 1's rule ignores the memory type | `fa-uc` with a read first (the store runs natively: `uncached_steps` 0) |
| M12 `Translation.parent_*` taken from the leaf instead of the parents | abi unit test (a non-user PDE over a user PTE) and `fa-user` |

## 8. Stop rule and risks

- Chunk 0: no window clears the gate (section 7) for either leaf kind -> the fill is not built; chunk 1 stands or falls
  on its own count (`read_d1`). Both counters stay in `io5-bins`.
- Chunk 2: revert after one round of diagnosis if the Tier 2 gate fails (the review's 70% rule), or if `shootdowns` or
  `guest` rise by more than half the gain (3.6's risk made real: then user-only writable, or read-only everywhere, is
  tried once before dropping). Any K1 red, `BAD_LINK`, `stop::SHADOW` or `--check-reuse` mismatch traced to the design
  rather than a slip stops the item for re-review of sections 3-4 (the retention plan's rule).
- Risks and open questions: h and k (section 5) are unknown until chunk 0 and decide everything; c_n's `at s1e1r` share
  (chunk 1 measures; the alias arithmetic is the fallback); whether the M4 under HVF exposes FEAT_HAFDBS for the use
  counter (else the study model stands in); the supervisor writable question (3.6; counted); a cc1-sized process on make
  may have much higher locality than configure's shells, so the two steps may want different W (the flag takes one value;
  per-leaf-kind is in the design, per-step is not); the `tlb` bucket's 322 us per CR3 load on make is not touched by this
  plan and stays group F's other item.

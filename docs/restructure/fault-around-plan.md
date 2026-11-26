# Shadow fault-around: install a page's present, already-accessed neighbours at one abort

Status: chunk 0 counted and chunk 1 merged (November 14, 2026); chunk 2 (the fill) built on branch `fault-around` (November 26)
after a second review for the KDE desktop (section 9.1), its K1 programs named `fa-fill-*` (tests/system/run_k1.py) and its
mutations in tests/system/run_fault_around_mutations.py. Accepted with fixes after one independent review (section 9). Roadmap item 8 of the
[architecture review](architecture-review-20261113.md) (root cause E's abort cost, group F), kept by the census
([io5-perf-notes](io5-perf-notes.md) decision 15: aborts per table page per epoch 26 / 101; the guest's A bit already set in
98-99.96% of installs; D set by the walk in 571 installs per run). Buckets and counts are from the current head profile
`natemu-raw/fm-on-profile-1` (page record and fast miss path on; sums over four vCPUs). Line numbers are those of `c4b783c`.
Every number marked *estimate* has its arithmetic beside it. No code or VM was touched for this plan.

## 1. The problem in one paragraph

A guest data access to a linear page with no shadow aborts from EL0 into the resident, which walks the guest tables for that
one page and installs one shadow leaf. The guest's own page tables are already populated around it: Linux maps file pages
sixteen at a time (fault-around, `fault_around_bytes` = 64 KiB) and, on x86, marks the prefaulted entries young (only arm64
asks for old prefaulted PTEs); the kernel's direct map and text are 2 MiB pages with A and D set from the start
(`__PAGE_KERNEL` carries both); and every page a process touched before a context switch keeps A = 1 in its tables while
natemu's shadow of it went with the CR3 load. (Fork does not help: `copy_present_pte` marks the child's copies old and
file VMAs without anonymous pages are not copied at all, so a fresh subshell's neighbours are A = 0 until it touches them;
the census's 98% describes the pages natemu installs on demand, not their neighbours.) So after a CR3 load, or at the
retry that follows a guest page fault, natemu takes one abort per 4 KiB page for pages whose translations the guest tables
already hold complete, with A = 1: 15.2 M aborts on configure, 40.1 M on make, about 300 per CR3 load. A hardware TLB fills one entry per miss too,
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
6. **Writable protocol**, for a shadow that no instruction is writing through right now (a fill, and chunk 1's read): if
   `writable`, first `cache::translated(gpa)`: if some vCPU holds translations from the frame, install read-only and set
   **no** bit (a later write aborts and takes `code_written`, as today; `code_written` itself is never called here: nothing
   is being written). Else `cache::shadowed_writable(gpa)`, then `translated` again: if a peer marked the frame in between,
   install read-only (the bit stays set, which is conservative: `mark` may ask this vCPU for a reset it does not need, the
   same superset today's `tlb` already tolerates, `:1698-1700`). Setting the bit before the second read is what lets a
   peer's `mark` either see the bit or be seen (`mark`, `:1641`); reading `translated` before setting it keeps read-only
   installs from leaving stale bits that would shoot down a vCPU holding no writable shadow (review B1). The demand
   *write* path keeps today's order and its `code_written` call: there the instruction does write the frame.
   **Supervisor space: read-only only, for now** (3.6 says why; chunk 0 counts what it costs).
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
by its earlier writes). Up to 1.98 M / 5.73 M upgrades (13-14% of aborts); chunk 0 counts how many had D = 1 at the read.

Two conditions (review B1 and 4). **The read must take 3.3 item 6's protocol, not the write path's**: today's write path
calls `code_written` when the frame holds translations (`:3362-3364`), which steps the instruction or kills every block on
the page for every vCPU (`:3166-3178`); on a read nothing is written, so a translated frame simply gets a read-only shadow
and no bit, as the fill does. (Taken literally, the write path's order on a read would have killed libc's code at every
`read()` of `libc.so.6` through the direct map and paid decision 31's drain each time.) **User space first**: a read in the
supervisor space of a direct-map 2 MiB page (RW = 1, D = 1) would set writable bits on frames that outlive kept CR3 loads
(`tlb`, `:1698-1700`) and may later be marked as code, the exposure 3.6 forbids for fills; chunk 0 counts chunk 1's
would-be bits on frames later marked (by space), and the supervisor space joins only if that count is near zero.

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
`translated` loads and the shared-word atomic `fetch_or`: **80-110 ns**. **Per fill (c_f):** `space()` (two `mrs`), one
`leaf` walk (three loads), one `dsb ishst; isb`: **about 50 ns**, paid at every remaining abort whether or not anything is
installed (at h = 0.3 on configure that alone is 9.52 M x 0.7 x 50 ns = 0.33 s against a 1.5 s gate). **Later removal:**
`unmap_large` at INVLPG issues `dsb ishst; tlbi vae1; dsb ish; isb` per valid entry (`tables.rs:26-30`); with more valid
entries per region the 611 k / 431 k INVLPGs per run pay more, so chunk 2 batches them (one `dsb ishst`, n `tlbi`, one
`dsb ish; isb`: a `Maintenance::invalidate_many`), and the model charges 10 ns per extra entry removed (c_r); `clear_own`
at a full reset is a bitmap sweep whose cost does not grow with the bits set, but the study counts the resets.

**Per abort saved:** 0.65 / 0.61 us in the buckets, plus the unmeasured entry/return and cold-state cost (0.1-0.3 us by
the chunk 9 figure); planning value **0.75 / 0.7 us**.

**Model.** N = first installs today (9.52 M / 22.21 M); h = the share of them a window of an earlier abort in the same
(space, epoch, table), still standing at the time (not removed by an INVLPG, #PF or reset in between) and not needing an
upgrade, would have covered; k = neighbours installed per remaining abort; c_n = 0.05-0.1 us, the gate uses 0.1.
Net = N x [h x 0.75 - (1 - h) x (c_f + k x c_n + k x c_r)] (configure; 0.7 for make). The table below leaves c_f and c_r
out for readability (they take 0.3-0.5 s off each configure row); chunk 0's model carries them.

| h | k | c_n (us) | configure net (s) | make net (vCPU-s) |
| --- | --- | --- | --- | --- |
| 0.3 | 6 | 0.08 | 9.52 x (0.225 - 0.336) = **-1.1** | 22.2 x (0.21 - 0.336) = **-2.8** |
| 0.5 | 6 | 0.08 | 9.52 x (0.375 - 0.24) = **+1.3** | 22.2 x (0.35 - 0.24) = **+2.4** |
| 0.6 | 8 | 0.06 | 9.52 x (0.45 - 0.192) = **+2.5** | 22.2 x (0.42 - 0.192) = **+5.1** |
| 0.7 | 10 | 0.08 | 9.52 x (0.525 - 0.24) = **+2.7** | 22.2 x (0.49 - 0.24) = **+5.6** |

The census's 26 / 101 aborts per table page per epoch give the bounds. Spread uniformly over 512 entries, a 16-window
around an abort holds 0.8 / 3 other aborts, and fault-around loses on configure; clustered as stacks, heaps, data
segments and Linux's 16-page populations are, h is in the upper rows. Two things pull h down (review): a forked subshell's
copied PTEs are old (section 1), so configure's many `sh` children offer nothing until exec, and a window's cover is
undone by any INVLPG of its region (611 k per configure run) before the neighbour is touched. The planning expectation is
therefore h = 0.3-0.5 on configure and 0.4-0.6 on make, with the 2 MiB direct-map rows carrying make. For a 2 MiB leaf the per-neighbour cost is lowest
(no PTE read, one permission) and the population per table largest (the kernel's direct map): on make, filling a whole
2 MiB leaf at 101 aborts per table costs 512 x 0.04 = 20 us against 100 x 0.7 = 70 us saved; on configure (26 per table)
17 us against 20 us, so W must come from the count, per leaf kind.

**Chunk 1 (writable on read, 3.7):** u = share of upgrades whose leaf had D = 1 at the read install (guess 0.6-0.9):
1.98 M x u x 0.75 us = **0.9-1.3 s** on configure, 5.73 M x u x 0.7 = **2.4-3.6 vCPU-s** on make, at no per-event cost.

**Planning numbers:** configure **1.5-3.5 s of 108 busy (1.4-3.2%)**, about the same in wall time (serial), of which
chunk 1 is 0.9-1.3 s and the window 0.5-2.5 s after c_f and c_r; make **4-9 vCPU-s of 537 (0.7-1.7%)**, 1-2 s of wall. Below the review's 3-5 s / 8-14 vCPU-s, which assumed half the installs useful and no
per-neighbour cost; the count decides, and the stop rule (section 8) drops the window while keeping chunk 1 if the model
says so.

**Memory:** none (the leaf tables exist; the pool is untouched). **`tlb` bucket:** unchanged (`reset_cr3` clears roots,
not entries). **Arm TLB:** descriptors are not TLB entries; nothing fills until used.

## 6. What was rejected and why

1. **Installing A = 0 neighbours and setting A** (the hardware prefetcher's licence): an atomic RMW per entry in the guest
   tables through the code-page observer, and an A bit QEMU would not set, so every A/D K1 comparison turns "either".
   It would reach the old PTEs of forked children (section 1), which the A = 1 rule cannot; chunk 0 counts the A = 0
   neighbours skipped, so the size of what is given up is known, but the exactness against QEMU is worth more than it.
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
(`translate` after `install`): the (space, epoch, leaf table page or 2 MiB leaf, index, access, CPL, half, leaf size, D at
the read). A per-vCPU 4-way set of (space, epoch, table) entries (as the census's `first`; keyed by space because a fill
serves only its privilege class), each with a 512-bit `installed` mask and a 512-bit `covered` mask per candidate window
W in {8, 16, 32, 64, 512}, plus `covered_writable`. **The masks follow the real shadows** (review 2): cleared for the whole
region at INVLPG (`unmap_large`), for the page at a #PF (`unmap`), wholly at a full reset, and at a CR3 load for every
entry but the kept half's global ones, so a cover that would have been gone counts as no cover. At an install at index i:
if `covered_W[i]` and the access is a read, or a write with `covered_writable_W[i]`: `hit_W += 1`; a write onto a
read-only cover counts `hit_needs_upgrade_W` and is **not** a hit (an upgrade abort would follow; the kernel's writes to
read-only-filled 2 MiB direct-map pages are the large case); then read the W-window's entries once (plain loads), count
those present, A = 1, not installed, not covered, and classify the skips (A = 0, not present, reserved, not RAM, UC/WT/WP,
`translated`, kept-half non-global, U/S at CPL 3, split-held): `candidates_W += n`, set their `covered_W` bits. Separately:
`read_d1` = read installs whose leaf had D = 1 and grants the write (chunk 1's u), by space; **would-be writable bits** in
the shared global area, one bit per vCPU per frame like `WRITABLE_PAGES` (`MASK_BYTES` more, study build only; the lookup
area has no slack since the page record took it, `LOOKUP_BYTES`), set where a fill or chunk 1 would set the real bit,
cleared with the real bits at a full reset; in `mark`, count the frames whose would-be bits name a **peer** (a shootdown
the change would add) and those naming **this vCPU** (`writers & own`: a full reset of its own shadows), each by the space
the bit came from (3.6's and 3.7's questions); CNTVCT ticks of today's `tables::install` alone (c_n's floor), of
`unmap_large` per valid entry (c_r) and of the whole `translate`. Output per vCPU in the perf log's `k1.fault_study`;
`tools/fault_study_an.py PROFILE_DIR` prints the model of section 5 per W, leaf kind and space, with c_f, c_n = 0.1 us and
c_r in. **Gate:** some W (per leaf kind) with modelled net >= 1.5 s on configure or >= 4 vCPU-s on make at the conservative
costs, with hit rate `hit_W / (hit_W + installs)` >= 25%; chunk 1's own gate `read_d1 x 0.75 us` >= 0.8 s on configure in
the user space alone, and supervisor would-be bits on frames later marked under 1% of marks before the supervisor space
joins. Picks W (and whether 2 MiB leaves get a wider one) and settles 3.6 and 3.7's second condition.

**Counting rules as built (chunk 0, tightened at its review):** a `mark` counts a would-be shootdown only for a frame whose
would-be bit names a vCPU whose *real* writable bit is clear (a real bit would have shot it down anyway); the masks are
cleared per `reset_cr3`'s actual result, i.e. everything when the list overflowed (`NOT_ALL_GLOBAL`) or TTBR1 is off, else
all but the kept half's global covers; `read_d1` and `covered_writable` count only what chunk 1 and the fill would really
make writable: the user space, WB or WC memory, frames not `translated` at the time (3.3 item 6, 3.7).

**Chunk 1: writable on read (S: one rule and the read-side protocol of 3.3 item 6 in `translate`, user space first,
counters, independent review).** Tier 0: unit test of the rule table (D, R/W, WP, CPL, kind, translated -> writable, bit).
Tier 1: K1 programs `fa-ad` (the D half), `fa-ro`, `fa-wt`, `fa-read-code` below, `local-write`, `dormant-write`, the
`adv_setup` set, `pic-*`. Tier 2 gate: `upgrades` down by >= 70% of the user-space `read_d1`; `abort` + `shadow` down
accordingly; `shootdowns`, `invalidations` and `steps` not up beyond noise.

**Chunk 2: the fill (M: `Translation` fields, `Space::fill`, `fault_around.rs`, the host flag, counters; independent
review before the gate).** Also in this chunk: `unmap_large` batches its TLBIs (`Maintenance::invalidate_many`: one
`dsb ishst`, the region's `tlbi vae1`s, one `dsb ish; isb`; unit-tested through the `Log` maintenance), since the fill
raises the valid entries an INVLPG removes. Counters in the perf build: fills, neighbours installed (by space, leaf kind,
writable), skips by reason; and **uses**, optionally: Arm hardware access-flag update (FEAT_HAFDBS, ID_AA64MMFR1_EL1.HAFDBS,
TCR_EL1.HA) would let prefetched descriptors be installed with AF = 0 and read back AF = 1 at the next reset without any
fault, giving the production hit rate for the gate's "wasted installs". Caveats (review 7): TCR_EL1.HA applies to the whole
EL1 regime, the resident's own descriptors included, and a hardware-updated AF breaks `Space::install`'s `old == new` test
(`shadow.rs:276-279`; the compare would mask AF); whether HVF exposes HAFDBS on the M4 is unknown. So: confirm on the M4
first, and if it is off or awkward the study build's coverage count stands in for the gate (a descriptor with AF = 0 and
HA off faults, so the AF = 0 form is never used without HA confirmed on).

- Tier 0: abi unit tests for `fill` (writes only empty slots; refuses kept-half non-global and private targets; one
  publish; never allocates; count returned), `Translation`'s new fields (parents' bits with and without a 2 MiB leaf), the
  neighbour rule table (present/reserved/A/U-S/R-W/D/WP/CPL/kind/G -> skip, read-only, writable); each mutation below run
  once against its test.
- Tier 1, new `run_k1.py` programs (each a few calls and CR3 loads; the runner passes `--fault-around 16`, runs `fa-counts`
  also at 512 and the must-stay-green list at the shipped default W; "beside X" always means inside X's W-aligned window
  at the runner's W, which the programs lay out as one 64 KiB-aligned run of pages; QEMU-compared where the architecture
  fixes the answer):
  - `fa-ad` (QEMU): one leaf table, one 16-page window of present user pages: indices 0-7 A = 1 (odd ones D = 1), indices
    8-15 present with A = 0 (odd ones D = 1). Read page 0 (fills 1-7; a mutant ignoring A fills 8-15 too). Read every PTE
    back: A/D exactly QEMU's. Write page 2 (A = 1, D = 0: read-only prefetched): its D now set, once; a second write
    leaves the PTE equal. Read page 9 then page 11 (both A = 0: demand accesses that set A under natemu and QEMU alike;
    under the mutant both were prefetched at page 0's read, so either read leaves A = 0 where QEMU sets it): read back all
    16 PTEs again. Write page 3 (D = 1: writable prefetched): PTE unchanged.
  - `fa-invlpg` (QEMU): read X (fills N -> F1); N's PTE -> F2; INVLPG N; load from N returns F2's byte. Then N -> F1 again
    with a CR3 reload instead: F1's byte.
  - `fa-reserved` (QEMU): N beside X with a reserved bit (bit 51) set, A = 1: read X (N skipped: `skipped.reserved` = 1); a
    load from N: #PF error code 9 (present, reserved) to a handler recording CR2 and the code.
  - `fa-none` (QEMU): N beside X shaped like a PROT_NONE or NUMA-hint entry: P = 0 with A = 1, D = 1 and a frame set. Read
    X (N skipped: not present); a load from N: #PF error code 0 (not present).
  - `fa-wt` (natemu only): N beside X with PWT set (PAT entry 1 = WT), RW = 1, A = 1, D = 1: read X (N filled read-only:
    `skipped.not_writable_type` = 1); a load from N runs natively; a store to N runs alone (`uncached_steps` = 1, one
    stepped instruction). Second phase for chunk 1: **INVLPG N first** (phase 1 left N shadowed by the fill, and the step
    block may have shadowed it too, so without the INVLPG the read would not abort and chunk 1's rule would never run),
    then read N (a demand install: D = 1 but WT, so read-only), then store: `uncached_steps` = 2 in total.
  - `fa-read-code` (QEMU; chunk 1; **run with `--fault-around off`**, so that no earlier data access in N's window, the
    stack or X among them, prefetches N writable before the call and leaves the read nothing to abort on): N user RW = 1,
    D = 1, A = 1 holding `mov eax, 1; ret`; call N (placed, the frame marked); read N as data (a demand install: D = 1, so
    chunk 1 would make it writable, but the frame is translated: read-only, no bit, no `code_written`): `invalidations`
    and `steps` unchanged by the read; then store `mov eax, 2` into N and call N: 2 (the SMC result equals QEMU's).
    Mutation M13 below.
  - `fa-stale` (natemu only, as `no-invlpg`): read X (fills N); N's PTE -> F2 with no invalidation; a load from N may return
    either; INVLPG N: F2's. QEMU returns F2 at once (it cached nothing for N): the "either" set holds both.
  - `fa-kept` (natemu only): upper-half supervisor mapping (0xffff8...; the runner gains an upper-half table helper if
    none exists), PGE on; X global, N non-global, both in one leaf table, N mapped to F1 in space A and F2 in B. Read X
    under A (N must not be filled: non-global in the kept half); CR3 B; a load from N returns F2's byte, and the K1
    stats show `kept_resets` = CR3 loads (no full reset: the list did not overflow). A second phase with 70 non-global
    neighbours checks the same.
  - `fa-mmio` (QEMU): a leaf table where index 1 maps the LAPIC page (0xfee00000) and index 0 a RAM page. Read index 0;
    read the LAPIC ID register through index 1: the APIC ID (QEMU agrees); `fault_around.skipped.not_ram` = 1.
  - `fa-ro` (QEMU, `adv_setup` style): N beside X with R/W = 0, **A = 1 and D = 1**, both user pages. **At CPL 3** (where
    fills may be writable, so a mutant ignoring R/W installs N writable): read X, write N: #PF error code 7 (present,
    write, user) to a handler that records CR2 and the code. At CPL 0 with WP = 1 the same program takes error code 3 and
    with WP = 0 the write succeeds (QEMU agrees), but these halves do not test M8: supervisor fills are read-only (3.6), so
    the mutant installs N read-only and the fault comes anyway; the WP half becomes a test only if 3.6's restriction is
    lifted, and the program is then rerun with it in mind.
  - `fa-user` (natemu only, `adv_setup` style, one epoch via `SET_CPL`): at CPL 3 read X whose neighbour S is a supervisor
    page (U/S = 0 at the leaf): S is not filled in the user space; a CPL 3 read of S faults with error code 5. Then at
    CPL 0 read S (fills its neighbours, among them user page U, in the supervisor space, read-only) and write one of them:
    an upgrade, `upgrades` = 1; `SET_CPL` 3: a read of a supervisor neighbour the CPL 0 fill installed faults with error
    code 5 (the fill went to the supervisor space only).
  - `fa-smc` (QEMU): N user RW = 1, D = 1, A = 1 holding `mov eax, 1; ret`. (a) Write X (fills N writable, the bit set);
    call N (fetch marks N: `writers` holds this vCPU, `reset_shadows`); store `mov eax, 2` into N; call N: 2. (b) The
    other order: call N first (marked), write X (N filled read-only: `translated`), store into N (aborts, `code_written`,
    stepped), call N: 2. Both halves equal QEMU's registers.
  - `fa-large` (QEMU): a 2 MiB leaf, PDE D = 0: read one 4 KiB page (fills the window read-only); write another page of
    the leaf: the PDE's D set once; read back equals QEMU's. A second 2 MiB leaf with D = 1: the write changes nothing.
  - `fa-uc` (natemu only): N's PTE PCD|PWT with a PAT entry of UC beside X: read X; a load from N runs alone
    (`uncached_steps` = 1), `skipped.uncached` = 1. (UC is never shadowed outside a step block, `:3353`, so the memory-type
    mutations are caught by `fa-wt`, not here.)
  - `fa-counts` (natemu only): 32 consecutive present A = 1 pages on a 64 KiB-aligned run, fresh space: **read** page 0 then
    1..31 in order (reads, so read-only fills suffice): `retries` = 2 and `fault_around.installed` = 30 at W = 16;
    `retries` = 1 and `installed` = 31 at W = 512.
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
| M1 fill ignores A (installs A = 0 neighbours) | `fa-ad` (page 9 or 11 read back with A = 0 where QEMU has 1) |
| M2 fill installs writable with D = 0, or ignores WT/WP | `fa-ad` (page 2's D never set), `fa-large` (PDE D); `fa-wt` (the store runs natively: `uncached_steps` 0) |
| M3 fill lists nothing and installs kept-half non-global neighbours (or `map_global` for them) | `fa-kept` (F1's byte after CR3 B, or a full reset counted) |
| M4 fill skips the RAM test (maps the entry's address as IPA) | `fa-mmio` (a RAM-looking read instead of the APIC ID, or `stop::SHADOW`) |
| M5 fill sets no writable bit | `fa-smc` (a) (the stale block returns 1) |
| M6 fill skips `translated` | `fa-smc` (b) (the store never traps; 1) |
| M7 fill ignores U/S at CPL 3 | `fa-user` (the CPL 3 read of S succeeds) |
| M8 fill ignores R/W (or WP) | `fa-ro` at CPL 3 (N has A = 1, D = 1 in the user space: the mutant installs it writable and the write succeeds instead of #PF 7); the WP half is untestable while supervisor fills are read-only |
| M9 fill disabled (or `--fault-around off` ignored) | `fa-counts` (`retries` 32) |
| M10 fill replaces a valid slot without break-before-make, or publishes nothing | abi unit test (`fill` on a valid slot: refused); `fa-counts` under the resident checkers (a TLB that may hold the old entry: `page-mark`-style second write) |
| M11 chunk 1's rule ignores the memory type | `fa-wt` second phase (INVLPG N, N read first, then stored: the store runs natively, `uncached_steps` 1 instead of 2) |
| M12 `Translation.parent_*` taken from the leaf instead of the parents | abi unit test (a non-user PDE over a user PTE) and `fa-user` |
| M13 chunk 1's read takes the write path's order (bit, then `code_written` on a translated frame) | `fa-read-code` under `--fault-around off` (`invalidations` or `steps` move at the read; or, with the bit set and no kill, the later store's SMC result differs) |
| M14 fill ignores the present bit or the reserved bits | `fa-none` (the load succeeds instead of #PF 0), `fa-reserved` (instead of #PF 9) |
| M15 fill installs in both spaces, or in the wrong one | `fa-user` (the CPL 3 read of the supervisor neighbour succeeds after the CPL 0 fill) |
| M16 study masks not cleared at INVLPG/#PF/reset, or not keyed by space | chunk 0 unit tests of the mask model (a cover removed by `invlpg-page`'s pattern no longer hits; a CPL 0 cover does not serve a CPL 3 access) |

## 8. Stop rule and risks

- Chunk 0: no window clears the gate (section 7) for either leaf kind -> the fill is not built; chunk 1 stands or falls
  on its own count (`read_d1`). Both counters stay in `io5-bins`.
- Chunk 2: revert after one round of diagnosis if the Tier 2 gate fails (the review's 70% rule), or if `shootdowns` or
  `guest` rise by more than half the gain (3.6's risk made real: then user-only writable, or read-only everywhere, is
  tried once before dropping). Any K1 red, `BAD_LINK`, `stop::SHADOW` or `--check-reuse` mismatch traced to the design
  rather than a slip stops the item for re-review of sections 3-4 (the retention plan's rule).
- Risks and open questions: h and k (section 5) are unknown until chunk 0 and decide everything; c_n's `at s1e1r` share
  (chunk 1 measures; the alias arithmetic is the fallback); whether the M4 under HVF exposes FEAT_HAFDBS for the use
  counter and whether TCR_EL1.HA is acceptable for the resident's own descriptors (else the study model stands in); the
  supervisor writable question (3.6, 3.7; counted); a cc1-sized process on make may have much higher locality than
  configure's shells, so the two steps may want different W (the flag takes one value; per-leaf-kind is in the design,
  per-step is not); the `tlb` bucket's 322 us per CR3 load on make is not touched by this plan and stays group F's other
  item.
- Pre-existing, noted by the review and not this plan's to fix: a WRMSR to IA32_PAT is not treated as a flush (`is_mtrr`,
  `cpu.rs:30` covers the MTRR MSRs only), although SDM Vol.3 11.12.4 asks for a TLB flush after a PAT change and cached
  memory types (shadows installed under the old PAT, prefetched or not) would otherwise outlive it. Linux writes PAT once
  at boot before any shadow matters; recorded in the correctness backlog as a gap, with the one-line fix (treat the PAT MSR
  as a global flush) left to that pass.

## 9. Review response (independent review of `d4f43b1`, November 14, 2026: accept with fixes; SDM argument sound)

| Finding | Resolution |
| --- | --- |
| B1 (blocking): chunk 1 on a *read* of a frame holding translations would, with the write path's order (`:3358-3365`), call `code_written` and kill every block on the page for every vCPU (`:3166-3178`), e.g. libc's code at every `read()` of `libc.so.6` through the direct map, each paying decision 31's drain; read-only installs would also leave stale writable bits that make a later `mark` shoot down a vCPU with no writable shadow | Fixed: 3.3 item 6 is now the read-side protocol (`translated` first: if set, read-only and no bit, never `code_written`; else bit, then `translated` again) and 3.7 says the read takes it, not the write path's; the write path keeps its order and its `code_written`. New program `fa-read-code` (invalidations and steps unchanged by the read; SMC after it equals QEMU), mutation M13 |
| B2 (blocking): the A-bit mutation passed `fa-ad` (the A = 0 pages lay outside page 0's window and page 17 was itself a demand access) | Fixed: the A = 0 entries are inside page 0's 16-page window (indices 8-15); after the demand read of page 9 the program reads page 11, which only the mutant has prefetched, and reads all PTEs back; M1 names page 11 |
| B3 (blocking): the memory-type mutations passed `fa-uc` (UC is never shadowed outside a step block, `:3353`); the R/W mutation passed `fa-ro` (with D = 0 the mutant installs read-only anyway); "beside X" undefined | Fixed: new `fa-wt` (PAT entry WT: read-only fill, the store runs alone, `uncached_steps` counted; a second phase for chunk 1's rule), M2 and M11 point at it; `fa-ro`'s N has A = 1 and D = 1; the program list states that "beside X" is inside X's W-aligned window at the runner's W on a 64 KiB-aligned run |
| 1: the study's would-be writable map was per vCPU, so a `mark` on X could not see Y's bits; the lookup area has no slack (`LOOKUP_BYTES` is sized to the page record, `:284`) | Fixed: the map is in the shared global area with one bit per vCPU per frame like `WRITABLE_PAGES` (`MASK_BYTES` more, study build only); `mark` counts peer shootdowns and own full resets (`writers & own`) by space |
| 2: hit rate overstated: covers must go where real shadows go, be keyed by space, and not count hits that still need an upgrade | Fixed: masks keyed by (space, epoch, table), cleared for the region at INVLPG, the page at #PF, everything at a full reset and all but the kept half's global entries at a CR3 load; a write onto a read-only cover is `hit_needs_upgrade_W`, not a hit (chunk 0); section 5's h definition says so |
| 3: missing costs: the fixed per-fill cost (0.3 s at h = 0.3 against a 1.5 s gate), INVLPG's per-entry `dsb/tlbi/dsb/isb` growing with valid entries, `clear_own` at resets; gate on a conservative c_n | Fixed: c_f (50 ns per remaining abort) and c_r (10 ns per extra entry removed) in the model, c_n = 0.1 us at the gate; chunk 2 batches `unmap_large`'s TLBIs (`invalidate_many`); the study times `unmap_large` per entry; `clear_own` noted as a sweep independent of the bits set, resets counted; planning numbers lowered (configure 1.5-3.5 s) |
| 4: chunk 1 would make supervisor direct-map pages writable on reads, the exposure 3.6 forbids for fills | Fixed: chunk 1 starts in the user space; the study counts chunk 1's would-be bits on frames later marked, by space; the supervisor space joins only under 1% of marks (chunk 0's gate) |
| 5: factual: fork marks the child's copied PTEs old (`copy_present_pte`) and skips file VMAs without anon pages, so forked subshells see A = 0 neighbours; the census's 98% describes demand pages, not neighbours | Fixed: section 1 and 6.1 corrected; h's planning range lowered to 0.3-0.5 on configure, 0.4-0.6 on make; the A = 0 skips are counted so the size of what the A = 1 rule gives up is known |
| 6: K1 additions | Added: `fa-reserved` (#PF 9), `fa-none` (P = 0 with A, D and a frame: #PF 0), the CPL 3 read of a supervisor neighbour after a CPL 0 fill in `fa-user` (M15), the must-stay-green list at the shipped default W, `fa-counts` as reads on a 64 KiB-aligned run; mutations M14-M16 |
| 7: FEAT_HAFDBS: TCR_EL1.HA covers the resident's own descriptors and breaks `Space::install`'s `old == new` test | Noted in chunk 2: optional, confirm on the M4 first (HVF exposure, the AF-masked compare); the study model stands in otherwise |
| Pre-existing: a WRMSR to IA32_PAT is not a flush (`is_mtrr`, `cpu.rs:30`) | Noted in section 8 as a correctness-pass gap, not this plan's |
| Re-check of `8ee56ae` (accepted; test-plan defects): `fa-ro` missed M8 (supervisor fills are read-only, so the mutant installs N read-only and the #PF comes anyway); `fa-wt` phase 2 missed M11 (N already shadowed by phase 1, so the read never aborts); `fa-read-code` may miss M13 (an earlier data access in N's window prefetches N writable before the call); `fa-ad`'s "A set by both" wrong under the mutant (page 9 was prefetched at page 0's read) | Fixed: `fa-ro`'s R/W half runs at CPL 3 expecting error code 7, the WP half stated untestable until supervisor fills become writable; `fa-wt` phase 2 begins with INVLPG N; `fa-read-code` runs under `--fault-around off`; `fa-ad`'s text corrected (the mutant is caught at page 9 or 11); the chunk-0 counting rules tightened at its review are recorded (shootdowns only for would-be bits whose real bit is clear; masks cleared per `reset_cr3`'s actual result including overflow; `read_d1` and `covered_writable` limited to the user space, WB/WC, frames not translated) |

### 9.1 Second review: the KDE desktop (November 26, 2026; independent reviewer on another model, before chunk 2 is built)

Evidence reviewed: debug case `desktop-kde-profile` (Mac mini M4, branch `desktop-profile`): a window resize at about 20
frames/s, half of each frame's natemu overhead in shadow faults; KWin's per-frame buffers cost 77 k guest #PFs and 1.26 M
shadow installs in 3 s (about 16 per #PF), with `clear_page_rep` prominent in the guest kernel. Chunk 0's failure on the
CPython build (notes item 34: hit rates 39-53% on sparse windows) need not carry over: buffer sweeps are dense.

| Finding | Resolution |
| --- | --- |
| B1 (blocking): the writable reverse map (`docs/writable-drop.md`) postdates this plan; its invariant W says every writable shadow has a live `WRITERS` entry, and a fill that writes a writable descriptor without `cache::writable_installed` lets a targeted drop (`drop_writable`) miss it, so a code write through it goes unseen. 3.5's "peer `mark` asks for a reset" row is stale (now a targeted drop) | Fixed in chunk 2: each writable neighbour takes `shadowed_writable`, the `translated` re-check (3.3 item 6), the descriptor, then `writable_installed`, all before the resident returns to the guest (so before it can serve a drop) |
| B2: a read-only supervisor fill costs more than none for pages written first: the write aborts on the read-only shadow and takes `Space::install`'s break-before-make (a full `dsb; tlbi; dsb; isb`). The kernel's direct map (2 MiB, D = 1) is written first by `clear_page_rep`. 3.6's restriction also predates the reverse map and chunk 1, which made demand reads writable in both spaces | Chunk 2 installs no supervisor neighbour whose entry has D = 1 and grants the write (3.6 would make it read-only): skipped and counted (`supervisor_read_only`). Supervisor neighbours with D = 0 or no write grant are filled read-only (they would be read-only anyway). Lifting 3.6 needs a region-granular reverse-map entry first (a writable direct-map fill would overflow the 2^17-entry map into `lossy` and full resets) |
| The 16:1 ratio does not mean 16-page groups: a 4 KiB anonymous or shmem write fault maps one page, so at most about 2 installs follow it (the direct-map clear and the user access), about 12% of the 1.26 M. The rest is most likely refills: after CR3 loads (an munmap of more than 33 pages reloads CR3 on every CPU running the mm), per vCPU, after INVLPG region drops and resets, and shmem read fault-around (16 young PTEs). Estimate: W = 16 removes about 55-75% if user space holds most installs, under 20% if direct-map writes dominate (low confidence) | The A/B decides: `--fault-around off` (NATEMU_FAULT_AROUND=off) against the default, judged by retries removed per neighbour installed, `abort` + `shadow`, frames/s. The fill's counters (`k1.fault_around`) give the split by space and leaf kind |
| Alternatives: (a) 2 MiB shadow blocks for the direct map (legal: one A, one D) need 512 RAM frames untranslated, of one type, none split-held, gpa/IPA congruent modulo 2 MiB, region-granular writable bits and drops: large, pays only if direct-map writes dominate; (b) a sequential ramp (a miss on the first index after a filled window doubles W: 16, 64, 512) for linear buffer sweeps; (c) one shadow walk per abort instead of three `tables::descriptor` walks plus `install`'s, perhaps 10-15% of an abort; (e) batch `unmap_large`'s TLBIs; PCID only removes CR3-load refills entirely | Ranked: chunk 2 as above with `unmap_large` batched (one `dsb ishst`, the region's `tlbi`s, one `dsb ish; isb`); the ramp if retries stay window-bound; (c) as its own item; (a) and lifting 3.6 only if the counters show direct-map writes large; PCID only if refills and `tlb` still dominate after chunk 2 |
| Nits on the work in progress: the abi module was not declared; `Translation::table == 0` as the 2 MiB discriminator is fragile; `pte`'s CPL 0 grant matches `walk_once` only because the walker refuses CPL 1 and 2 | Fixed: declared; the fill keys on `leaf_bytes`; the CPL note is in `pte`'s comment |

## 10. Chunk 2 as built: the A/B on the M4 (an experiment, November 26, 2026)

Built as section 3 with review 9.1's B1 and B2 (commit `5dc215bb`; the same code on the desktop deploy commit `e86dbab6` is
`a8c2c924`). The window is 16 pages by default; `--fault-around off` or `NATEMU_FAULT_AROUND=off` turns it off. One run per
side on the Mac mini M4, both natemu builds through `TESTKIT_BENCH_AT` from the desktop checkout: base `e86dbab6`, change
`a8c2c924`.

**Desktop** (debug case `desktop-kde-profile`, runs `20261126-024119-6d48` base and `20261126-024151-4352` change):

| window | frames/s | abort ms/frame | shadow ms/frame | abort + shadow vCPU-s | busy vCPU-s |
| --- | --- | --- | --- | --- | --- |
| window grow | 9.89 → 10.54 | 10.6 → 6.2 | 8.2 → 10.0 | 0.693 → 0.534 | 5.10 → 4.58 |
| window move | 9.11 → 9.14 | 7.8 → 4.2 | 5.8 → 6.4 | 0.488 → 0.380 | 2.96 → 2.83 |
| session start to idle desktop | | | | 8.02 → 5.69 | 78.3 → 80.9 |

- The fill halves the time spent in `abort` in every window and adds about a tenth to `shadow`.
- The frame rates do not move beyond one run's noise: guest time is half the busy time of a resize frame.
- The app starts (Konsole and Dolphin, first and second, the `:start` windows) are unchanged within noise. Before and after,
  in seconds: 1.80 / 1.79, 1.18 / 1.11, 1.70 / 1.68 and 1.04 / 1.12.

Over the whole boot (four vCPUs summed), retries fell from 24.8 M to 10.7 M.

- 6.7 M fills installed 46.4 M neighbours:
  - 45.5 M in the user space and 0.9 M in the supervisor space;
  - 9.0 M from 2 MiB leaves;
  - 28.2 M writable.
- That is about 3.3 neighbours installed per retry saved.
- Skipped neighbours:
  - not present: 17.7 M;
  - A = 0: 4.9 M;
  - supervisor neighbours that would have been writable (B2): 7.0 M;
  - already shadowed: 7.1 M.
- Upgrades rose from 1.6 k to 15.5 k.

**CPython build** (bench case `io5-solo32`; base `20261126-025007-a3ff`; change `20261126-025007-0dc0`, and
`20261126-030820-3ab4`, run again because its difference was within the noise). Seconds:

| | 64-bit configure | 64-bit make | i386 configure | i386 make |
| --- | ---: | ---: | ---: | ---: |
| base | 52.9 | 85.7 | 57.2 | 104.2 |
| fault-around, two runs | 50.8, 53.2 | 84.4, 85.0 | 57.4, 56.8 | 107.9, 104.1 |

No step moved beyond the spread of the change's own two runs. Section 5's 1.5-3.5 s on configure is not shown. This
agrees with chunk 0's count: its windows were sparse there.

Speedometer was not run: the owner moved measurements to the pooled checkpoint branch.

# Frame-keyed user code and a per-vCPU code TLB: design

Status: design, reviewed (section 6), November 26, 2026, branch `frame-keyed` off `main` at be88e684. Nothing is built yet. The
design is Fable's proposal P1. This page gives its mechanism, its sizes, the guard's code, how invalidation maps to the
SDM, the risks, and the measurements that decide each step.

## 1. The problem, measured

Case `desktop-kde-profile`, Mac mini M4, run `20261126-013059-46da` at 64ac467c (be88e684 plus the desktop profile's
harness). A KDE Plasma 6 session, 4 vCPUs, 8 GiB. In the window `app-konsole-2:start`, a warm Konsole start, the app
takes 1.268 s; under KVM it takes 0.18 s. Busy vCPU time is 1.725 s, split by resident bucket:

| guest | pool | host | link | miss | lookup | abort | place | enter | reuse | patch | relink | tlb | other |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 0.398 | 0.366 | 0.193 | 0.162 | 0.126 | 0.067 | 0.067 | 0.051 | 0.048 | 0.047 | 0.047 | 0.021 | 0.014 | 0.118 |

In the same window there were 363 CR3 loads, 4,853 INVLPGs and 20,349 #PFs. CR3 loads suspended 205,736 links, and
147,049 were patched again. There were 42,676 translation requests, 38,656 of them answered from the host's memory
(code the VM already had). Over the whole boot, 4.9 M links were suspended and 4.6 M patched again. Code that natemu
already holds costs about 60% of the window: copies from the pool, placement, linking, dispatcher misses, lookups and
instance records.

Three things cause this cost:

1. **Placing code again.** A process or vCPU that runs code natemu already holds still pays per block. Each block
   costs a copy from the pool, or a copy of a template, or a new instance record. The hint table finds templates by
   (frame, page offset, context), but it is lossy: 2^18 direct-mapped entries, one per hash, while up to 1 Mi blocks
   are live. When the hint is lost, the block goes to the pool or the host even though this vCPU holds its template.
2. **CR3 loads undo state.** A CR3 load ends the currency of every non-global block: it empties their L1 entries and
   suspends the links into them. Coming back costs, per block, a dispatcher miss, a revalidation and a relink.
3. **Instances.** Shared code needs an instance per linear address: a record, an L2 slot, linear chains, its own
   currency and its own revalidations.

**What the counts say (the coordinator's counting boot, Mac mini M4, warm Konsole and Dolphin starts).** Pool hits and
host requests split by why this vCPU lacked the block:

- **(a) A peer vCPU holds the block live:** 59-71% of pool and host request time. This is P2's work, a translation
  store shared by the vCPUs, and not this plan's.
- **(c) The hint table lost a template this vCPU holds:** 21-28% of requests and 0.13-0.15 vCPU-s. Step 1a fixes
  exactly this.
- **Revalidations and instance misses:** 61% of misses by count, but only 0.09-0.11 vCPU-s.
- **On 1 vCPU,** (a) becomes (b), "flushed", nearly one for one.

So step 1a, the exact frame-keyed table, comes first. Its key is built so that P2's shared table can grow from it. Step 2
stays in the plan but waits until step 1 is measured (section 7 says what evidence would justify it).

## 2. The mechanism

### 2.1 Frame-keyed table (step 1a)

The lossy hint table (2^18 direct-mapped u32, `area::HINTS`) is replaced by an exact open-addressed table of its own,
`area::FKEYS`: 2^20 entries of 8 bytes (8 MiB per vCPU), each a 32-bit tag of the key and the block number + 1. The table's
algorithm is pure functions with unit tests (`natemu_abi::fkeys`). The lookup area has no room for 2^21 entries, so the
table caps its load: templates are entered up to half the table, shared translations up to three quarters, and a
template beyond the cap is counted (`fkey_full`) and not entered. The hint table stays as a write-only shadow, read only
to count what it would have lost (`fkey_lost`); it goes once that is measured. The key is `(frame, page offset, half,
context)`: the first source page's guest-physical page, `rip & 0xfff`, `rip >> 63` and the code context. It names no
linear address and no vCPU, so a shared (cross-vCPU) table in P2 can use the same key. A tag match is only a candidate: the
record is checked as `lookup` checks its own (live, `RELOCATABLE` or `TRANSLATION`, never `SYSTEM` or `STEP`,
`source_gpa[0] == frame`, same page offset, half and context).

- Each key names at most one block. A placed [`RELOCATABLE`] block (a template, from the host, the pool or a copy) is
  entered if its key is free or names a dead block. A translation made from a template replaces the template's slot
  (the template stays valid at its own RIP). So the slot names the translation if there is one, else the oldest live
  template. A copy of an unshareable template does not replace its source.
- A block leaves the table when it dies, and only if the slot still names it (copies of an unshareable template share one
  key). Removal uses backward-shift deletion, as `l2_remove` does.
- Every block has at most one entry, and the caps keep the load below three quarters, so probes end at an empty slot
  (review finding 2). `cache::reset` empties it.

`reused` then finds every template this vCPU holds for the bytes at RIP, wherever it was placed. The lost-hint share
(case (c) below) goes to an in-VM instance or copy instead of the pool or the host.

### 2.2 No instance records (step 1b)

A user RIP that the RIP-keyed L2 does not find is looked up by frame. On the fast miss path, the frame comes from the
page record (`page_record::probe`, an exact TLB-entry answer in this epoch and class); on the full path, it comes from
the fetch walk that `reused` already makes. A translation found there is entered at that RIP, with no record filed: the
L1 entry (RIP → `full_entry`, owner = the translation's number), and the resident's own `enter`, which writes the page
base slot, as today. `count::INSTANCES` counts these entries, so the k1 cases' lower bounds keep their meaning.

The refusals `instance` applies are made at every such entry (review finding 3):

- the page within the rewrite's reach of a canonical edge (`pic::reaches`);
- a block end that is not canonical;
- 32-bit code not wholly below 4 GiB;
- a global leaf;
- a fence or stop RIP on RIP's page or the next.

A refused RIP gets an absolute copy, as today.

Three paths resolve a RIP by frame:

- `miss_fast` uses the page record only (no walk).
- `sys_fast` (SYSRET, SYSCALL, IRET and the rest) uses the page record or a walk, as its lookup's revalidation may. It
  tests fences, the stop RIP and the RIP's range before it walks (code review, finding 3).
- `find` uses the walk that `reused` makes.

What instances did, and how each is done without them:

| Instance duty | Without it |
|---|---|
| Currency per address (`checked`) | The L1 entry is the only per-address state; it is exact for the epoch (below). A resident lookup needs the page's frame, from the page record or a walk. |
| L1 entries emptied at a CR3 load | Unchanged: an owner that is a translation is never current (`checked` 0), so `tlb`'s listed scan empties it. |
| L1 entries emptied at INVLPG or #PF of the page or its 2 MiB region | A filter of linear pages with translation-owned L1 entries (2^15 bits; cleared at CR3 loads, global flushes and resets), plus a region filter for fills whose walk found a large leaf. A fill sets the bit of the first page and, for a block whose bytes cross into the next page, of that page too (review finding 1). On a hit, the user tables' L1 lists are scanned: every translation-owned entry whose bytes at its tag (tag to tag + `guest_bytes` - 1) touch the page or region is emptied. |
| L1 entries emptied at the translation's death | The 8 sets with its page offset (RIP bits 12-14 vary) in every table, both ways: 64 owner reads. |
| Its own links | Links already name translations, never instances (checked links go through the dispatcher slot; local links are translation to translation). Unchanged. |
| Linker and fault RIP | `running_rip` already reads the slot. The linker gets the target's RIP from `find` instead of `target.rip` (`link(source, target, at, to)`). |
| `REUSE_CHECK` | The comparison with the host's answer runs at each frame-keyed entry in check mode. |

L1 is still emptied at every CR3 load in step 1. Sharing stays lazy: code is rewritten the first time a second address
needs it.

### 2.3 Code TLB (step 2)

A per-vCPU table of user-class fetch translations, as an ITLB holds them: `CTLB_ENTRIES = 4096` entries of 8 bytes
(32 KiB), direct-mapped by `(va >> 12) & 4095`. Each entry is `vpn | VALID | frame << 36`, where `vpn = va >> 12` (35
bits for a user address below 2^47), `VALID` is bit 35, and the frame is a 4 KiB guest-physical page number below
2^28 (guest RAM below 1 TiB; a larger frame is not entered). An empty entry is 0, which never matches because of
`VALID`.

- **Filled** only from a fetch translation's answer at CPL 3 for a non-global leaf: a fetch walk, or a page-record hit
  (which a walk in this epoch and class made). This happens on the resident's miss and revalidation paths, and in
  `fetch_page` for a placement.
- **Emptied**, with every index it filled kept on a list (when the list overflows, the whole table is cleared):
  - at every epoch move (`cache::tlb`: CR3 load, global flush; `tables_written`, which calls `tlb(s, true)`);
  - at `cache::reset`;
  - at INVLPG and #PF: the entry of the page; and if a large leaf filled any entry of the 2 MiB region (a region
    filter, as for the page record), all 512 of the region's indices.

  These are exactly the page record's events (`page_record::invalidated`, epoch). So the code TLB is a projection of
  the page record, and its correctness argument reduces to the page record's (page-currency plan).
- **Mapped** EL0 read-only in the guest-state alias's leaf table, in the 8 pages after the guest-state page, at x28 +
  4096. The table is backed in the lookup area and written by the resident at EL1. The leaf table is shared by both
  shadow spaces, so supervisor code can read it too; it never reads it. What user code can read is the frame numbers of
  its own address space's user code pages. This is a documented deviation of the same class as the readable L1 tables
  (section 2 of [translated-code-layout](translated-code-layout.md)). To hide the frame numbers, store `frame ^ k`, with
  `k` a per-VM constant, in both the table and the guard.

### 2.4 The guarded entry (step 2)

Each user translation gets a guarded entry. It extends today's two-word PIC entry (`and x0, x0, #~0xfff`; `str x0,
[x28, #PAGE_BASE]`), and is reached with x0 = the target RIP. x1 and x2 are dead at every exit, because the dispatcher
slot already clobbers them. `K = frame << 36 | VALID`:

```
lead-8:  b     DISPATCH_MISS              // the guard's miss: x0 still the RIP
lead-4:  (guarded entry)
G0:  ubfx  x1, x0, #12, #12               // index
G1:  add   x1, x28, x1, lsl #3
G2:  ldr   x1, [x1, #4096]                // the entry
G3:  movz  x2, #K[63:48], lsl #48
G4:  movk  x2, #K[47:32], lsl #32
G5:  orr   x2, x2, x0, lsr #12            // the entry a hit needs
G6:  eor   x1, x1, x2
G7:  cbnz  x1, lead-8
     and   x0, x0, #~0xfff                // today's full entry
     str   x0, [x28, #PAGE_BASE]          // (the code header word)
     body
```

On a hit it runs 8 more instructions than today's full entry, with one load (a 32 KiB table; the hot lines stay in L1D)
and no taken branch. `PIC_LEAD` grows from 16 to 48 bytes (12 words), so FEX's code stays 16-byte aligned. A block over
two pages needs a second probe (`add x1, x1, #8` with the index wrapped, and `K'` for the second frame, about 6 more
words). The first cut leaves such blocks unguarded: they keep today's scheme, and a counter says how many there are.

Absolute (first-address, never-shared) non-global user blocks can be guarded the same way. With an absolute block, x0
equals its RIP at every entry (the L1 tag, or the link's absolute move), so the guard checks `CTLB[rip] == frame`,
which is that block's currency. These blocks are split hot/cold: their guard is an out-of-line stub (the 8 guard words
and a `b` to the hot entry), so it costs one taken branch more. They matter because the suspended links are links
between absolute blocks. Section 6 asks the reviewer which form to build first.

### 2.5 Persistent links and L1 entries (step 2)

- A cross-page link into a guarded block patches the exit's `svc` word to `b guarded entry`; the move into x0 stays, as
  for a checked link. Its record goes in a new segment, `GUARDED`, on the target's inbound list. Neither a CR3 load nor
  a global flush suspends or prunes the segment; only the death of the source or the target does, or a reclaim.
  Links can then be made eagerly (at placement, for targets already present) instead of at the first exit.
- An L1 entry for a guarded block holds its guarded entry. A CR3 load does not empty it (`tlb` skips owners flagged
  `GUARDED`), and neither does an INVLPG: the code-TLB entry goes, so the guard misses. The entry goes only at the
  block's death, when its table changes context, or at a reset.
- A guard miss enters the resident through `DISPATCH_MISS` with x0 = the RIP. `miss_fast` fills the code-TLB entry from
  the page record when it has the page; otherwise it declines to the full path, whose walk fills it. Then it enters the
  block again. So each page costs one resident trip per address space switch, instead of one per block plus one per link.
- Local links (same page, same frames) stay as they are: they do not depend on the address space.
- Kernel and global code keeps today's scheme. A page with an armed DR7 breakpoint, a stop RIP or a host fence gets no
  code-TLB entry, so every guarded entry into it misses and the resident's checks run.

### 2.6 Soundness argument

A guard hit enters block B at RIP r under CR3 c only if the code-TLB entry for r's page holds B's frame. That entry
exists only if a fetch translation of r's page at CPL 3 under c, after the last CR3 load, global flush, INVLPG or #PF
covering r, found that frame through a non-global leaf. (The page record has the same validity, and the code TLB is
emptied at each of its invalidating events.) An x86 processor may hold exactly that ITLB entry and fetch through it
(SDM Vol.3 4.10.2.3: entries are cached for user and supervisor fetches; 4.10.4.1: only the listed operations are
guaranteed to remove them). So the hit is a legal ITLB hit. B's bytes are those the frame holds now: a write to the frame
kills B (unchanged SMC handling: frames stay write-protected), and B's death removes its L1 entries and the links into
it before the frame's mark is cleared. The page base slot is written by the guarded entry from x0, as today. A forged
slot is the same guest-state deviation as now, and the resident still trusts the slot only as "a jump by this process to
that address" (pic-plan section 4).

### 2.7 SDM 4.10.4.1 events

| Event (SDM Vol.3 4.10.4.1) | natemu today | Code TLB | L1 (step 2) | Guarded links |
|---|---|---|---|---|
| MOV to CR3 (no PCID: the profile has none) | `tlb(false)`: epoch++ | all entries emptied | guarded entries stay | stay |
| MOV to CR4 changing PGE, PAE, PSE, SMEP, ...; MOV to CR0 changing PG or WP; MTRR writes | `tlb(true)` | all emptied | stay (unguarded ones emptied) | stay |
| INVLPG m | `cache::invlpg` | page's entry; region's if a large leaf filled one | stay | stay |
| #PF at CR2 (4.10.4.1: invalidates the entries for the faulting address) | `page_faulted` → `cache::invlpg` | as INVLPG | stay | stay |
| Host table-page write (`tables_written`) | `tlb(true)` | all emptied | stay | stay |
| INVPCID, PCID CR3 forms | not exposed | (would map to `tlb`) | | |
| Task switch loading CR3 | through the CR3 path | as CR3 | | |

## 3. Data structures and sizes

| Structure | Size | Step | Note |
|---|---|---|---|
| Frame-keyed table `FKEYS` | 2^20 × 8 B = 8 MiB per vCPU | 1a | Exact; tag + number; templates to 1/2, translations to 3/4 of it. |
| Hint table | 1 MiB, kept for now | 1a | A shadow for the `fkey_lost` count only; removed once measured. |
| Translation L1 page filter | 2^15 bits = 4 KiB, and a 16-word region filter | 1b | `area::TL1_PAGES`; `Control::tl1_regions`. |
| Instance records | about 216 B each, gone | 1b | The arena and the index fill more slowly. |
| Code TLB | 4096 × 8 B = 32 KiB (or 32 Ki entries, 256 KiB: review finding 8), and a fill list | 2 | Lookup area, mapped EL0 read-only at x28 + 4096. |
| Guarded lead | +32 B per guarded translation; a 5-word stub per guarded absolute block | 2 | |
| `GUARDED` link segment | no new space | 2 | One more segment boundary in `Control`. |

## 4. Steps

1. **Step 1a: the exact frame-keyed table** (2.1). Instances stay, and so does everything else. This is the smallest
   change that removes the lost-hint share. Counters: frame-keyed hits; hits the old hint would have missed (a shadow of
   the hint table kept in `perf` builds); pool hits and host requests by kind (does this vCPU hold a block of the key?).
2. **Step 1b: no instance records** (2.2). It removes instance placement and revalidation, the 0.09-0.11 vCPU-s.
3. Measure 1a and 1b together against main on the Mac mini M4: one run of each commit, the cases of section 7.
4. **Step 2, held:** the code TLB (2.3), guarded entries (2.4), and persistent links and L1 entries (2.5), with the
   review's findings 4-16 (section 6). It starts only on the evidence in section 7.

## 5. Risks

- **The guard's cost on every cross-page transfer and every L1 hit into user code (step 2).** guest-luajit-interp is
  the case that rejected per-site branches before ([indirect-cache plan](indirect-cache-plan.md)). Fallback: L1 entries
  keep the unguarded full entry and stay emptied at CR3 loads; only links go through the guard. Second fallback, which
  the review suggested: entry poisoning, where a CR3 load patches one entry word per target instead of each inbound link.
- **INVLPG and #PF cost in step 1b.** The page filter makes most #PFs (data pages) cost one bit test. A hit scans the L1
  lists, which hold the entries filled since the last CR3 load. A counter gives the scans and their length.
- **More work per miss for shared code in step 1b.** Instead of an instance hit in L2, a miss probes the page record,
  then the frame-keyed table. Each is one probe.
- **The frame-keyed table's 16 MiB per vCPU** is touched only as filled.
- **Hot/cold layout.** Translations are never split. If step 2 makes more user code shared, its fetch packing is lost.

## 6. Review (Fable, independent, November 26) and the response

The reviewer read the code and found the guard's ITLB argument and the event list of 2.7 sound. The findings:

1. BLOCKING, step 1b: the L1 page filter missed blocks that cross into the invalidated page. **Adopted** (2.2: both
   pages are filtered, and the scan tests a block's byte range).
2. BLOCKING, step 1: fkeys in L2 could fill it to load 1, where probes never end. **Adopted:** a table of their own
   (2.1). The rule that a dying block removes only a slot that names it is adopted too.
3. BLOCKING: the frame-keyed entry dropped `instance`'s per-address refusals. **Adopted** for step 1b (2.2), and for
   step 2's code-TLB fills (no entry for such pages).
4. BLOCKING, step 2: a stale guarded L1 way can shadow the refilled one forever. **Adopted for step 2:** the fill
   replaces the way whose tag equals the RIP.
5. SHOULD, step 2: a current guarded block is never refilled into the code TLB. **Adopted:** every resident entry
   into a guarded block fills it while the block is current.
6. SHOULD, step 2: guarded L1 entries stay on the CR3 list forever. **Adopted:** they are not listed.
7. SHOULD, step 2: a 5-word constant-index stub for absolute targets. **Adopted** as the absolute form.
8. SHOULD, step 2: 4096 direct-mapped entries conflict on a desktop. **Adopted:** count conflicts from the first build,
   and take 32 Ki entries (`ubfx #12, #15`, the same instruction count).
9. SHOULD: after a CR3 load the trip per page is the full path's, not `miss_fast`'s. **Adopted** in the estimate.
10. NOTE: the soundness checks the reviewer confirmed; also clear the code TLB when the host's `stop_at` changes.
    **Adopted.**
11. NOTE: a non-canonical x0 can pass the guard through a forged slot, confined to that process. **Adopted:** document
    the deviation, and extend `forge-noncanonical` to guarded links.
12. NOTE: guarded links inherit the checked-link exclusions (REP self-exits, `STI_FIRST` self-links, armed
    breakpoints). **Adopted.**
13. NOTE: a stop inside the lead needs a PC class ("RIP = x0, not entered"), and a test at every guard word.
    **Adopted.**
14. NOTE: the check and study modes never fill the code TLB; a guard check mode; `link-check`'s exemption for
    `GUARDED`; the guard under `speculation_barriers`. **Adopted.**
15. NOTE: the 8 (or 64) pages after the guest-state page need a layout assert. **Adopted.**
16. NOTE: unguarded two-page translations keep step 1b's filter; in the first cut, a global flush also clears guarded
    L1 entries. **Adopted.**

The reviewer's recommended order was counters, then the guard on absolute user blocks, then the frame-keyed table. It
rested on the open question of where pool and host time goes. The coordinator's counting boot answered that question
(section 1): lost hints are the share this plan can take now, and the switch cost waits for step 2. Two of the
reviewer's alternatives are kept:

- **Guard absolute user blocks first in step 2.** This is the population of suspended links, and it needs neither the
  frame-keyed table nor PIC.
- **PIC-form pool entries,** as the interim before P2's shared store.

The reviewer's cheaper lost-hint fix, scanning the frame's reverse-map chain, is not taken. A frame's chain also holds
every copy and instance of its blocks, so its length grows with the number of processes. It would also give P2 no table
to grow from.

### 6.1 Code review of step 1 (Fable, independent, November 26)

The reviewer read `be88e684..7041c596` and found no blocking defect: no path enters a shared translation at a RIP whose
pages do not map its frames. The findings:

1. **Cap (SHOULD).** Templates past the cap lose reuse, and a translation left out was reachable only by local links.
   **Adopted:** translations are entered up to three quarters. `fkey_full` is read in the deciding runs; if it is not 0,
   the table needs room the lookup area does not have now.
2. **Scan counters (SHOULD).** `tl1_scans`, `tl1_scanned` and `tl1_emptied` never reached the host. **Adopted:** they are
   in the k1 stats' `fkeys` block. If scans are long, test the tag's page against the filter before reading the record.
3. **`sys_fast` walked before its fence test (SHOULD).** **Adopted** (2.2).
4. **Hot-path costs (SHOULD).** The virtual instance is now built only under the check, and `suspended_guardable` counts
   in `perf` builds only. **Adopted.** The hint shadow goes after measurement.
5. **NOTE.** A translation that dies alone leaves its live template with no slot until the next placement of that key.
   This is sound and converges.
6. **NOTE.** The key ignores the second frame: two crossing blocks that differ only there share a key, and `link`'s share
   can alternate them. This is rare, and the hint table had the same weakness.
7. **NOTE.** One translation holds at most one way of an L1 set.
8. **NOTE.** Counter meanings moved: `INSTANCES` and `reuse_copies` now count entries, not records; refusals can count
   twice (fast path, then full path); `dispatch-study`'s miss classes and `page-study`'s shadow are not adjusted.
9. **NOTE.** Instance code paths are dead now (`lookup`'s global refusal, `code_of`'s arm outside the check, `cut_one`'s
   scope). They are removed when step 1 is kept.
10. **NOTE.** The region filter is 2 MiB, as `large_held` is; 1 GiB leaves would need more (inherited).

## 7. Measurements that decide

All bench cases run on the Mac mini M4, each side from its own commit (`TESTKIT_BENCH_AT`), one run of each.

- **Step 1 (1a + 1b) is kept** if the following hold:
  - `desktop-kde-profile`'s warm Konsole and Dolphin starts (`app-*-2:start`) get no slower.
  - The time in `pool`, `host`, `reuse` and `lookup` together drops beyond the case's noise; the expectation is close to
    case (c)'s 0.13-0.15 vCPU-s plus part of the instance share.
  - `guest-build` (the ASLR-heavy instance producer), `hackbench`, `speedometer-natemu` and `guest-luajit-interp` stay
    within noise.
- **Step 2 is justified** if, after step 1, the warm starts still spend at least 15% of busy vCPU time in the switch
  buckets (`link`, `relink`, `patch`, `tlb`, and the misses after CR3 loads). It also needs most suspended links to be
  guardable: absolute to absolute, user, non-global, with a one-page target (a counter step 1a adds). If it is built, it
  is kept if those buckets drop and `guest-luajit-interp` slows by no more than twice its noise; otherwise the
  fallbacks of section 5 are measured the same way.

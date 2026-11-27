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

What the tests found instances also gave (k1 at 2-4 CPUs), kept without them:

- **The reuse check compares each (translation, RIP) once,** as each instance was compared once when it was filed. A
  direct-mapped table of the pairs already compared (`area::CHECKED_AT`, 4096 entries, under the check only) holds them;
  the fast paths enter only a pair compared already.
- **`remapped` still counts** a RIP whose page maps other frames than its last shared entry (or a global leaf, or none),
  as the stale instance's revalidation counted it. It comes from a direct-mapped table of the frames each RIP was entered
  from (`area::ENTERED_AT`, 4096 entries, counting only). The k1 oracle exempts such runs from "host requests equal
  distinct RIPs".
- **A translation's L1 way is the one whose tag is the RIP,** so two of its RIPs in one set hold both ways, as two
  instances did (code review note 7).
- **A lost race does not discard the translation** (`entered_shared`); it serves every RIP of its frames.

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
- **Mapped** (as first designed; as built it is one 2 MiB block beside the data tables, 32 Ki entries: 8.4 item 15 and
  section 10) EL0 read-only in the guest-state alias's leaf table, in the 8 pages after the guest-state page, at x28 +
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

### 7.1 Checkpoint 1: the A/B (Mac mini M4, `~/de-profile`)

The owner's policy: serious measurements happen only at checkpoints. Step 1 is checkpoint 1. It runs the decision set only,
one run per side; the full set (`guest-build`, `guest-hackbench`, headless `speedometer-natemu`) runs once, at the final
checkpoint.

- **Base:** e86dbab6, the desktop-profile tip with the host's `snapc` lines, through `TESTKIT_BENCH_AT`. Its runs are
  shared with the fault-around work:
  - `desktop-kde-profile`: run 20261126-024119-6d48.
  - `desktop-kde-browser-profile` (the desktop Chromium Speedometer, which is the one that counts; base 2.42): run
    20261126-021552-6362.
  - `guest-luajit-interp`, run for this plan: run 20261126-030949-67a2.
- **Change:** `frame-keyed-measure`, which is e86dbab6 plus this branch's commits (tip eded8385, which carries the race
  fix 2d7ec278), on the same three cases: `guest-luajit-interp` in run 20261126-032144-aa00, the two desktop cases in run
  20261126-032146-6203. The branch's later commits change, outside the check and the counters, only which L1 way a
  translation's second RIP in one set takes; they are not measured separately.
- **What is read:**
  - `desktop-kde-profile`: the warm and cold Konsole and Dolphin starts (`app-*-1:start`, `app-*-2:start`), their buckets,
    and the k1 counters `fkeys.lost`, `fkeys.full`, `fkeys.suspended_guardable`, the `tl1_*` scans, and `relink.suspended`.
  - `desktop-kde-browser-profile`: Chromium's starts and its Speedometer score.
  - `guest-luajit-interp`: the hot path's regression check.

### 7.2 Step 2's decision

Step 2 stays held until checkpoint 1 is read. It is built only if both hold:

- In `desktop-kde-profile`'s warm starts, the switch buckets (`link`, `relink`, `patch`, `tlb`, and the misses after CR3
  loads) are still at least 15% of busy vCPU time.
- Most suspended links are guardable (`fkeys.suspended_guardable` / `relink.suspended` above one half).

Otherwise P2 (the store shared by the vCPUs, which the counting boot gives 59-71% of pool and host time) comes before it.
**Read at checkpoint 1 (run 20261126-032146-6203 against 20261126-024119-6d48; Mac mini M4):**

- **Guardable share:** 70-83% of suspended links per vCPU (8.66 M of 10.43 M, 4.98 of 6.77, 4.40 of 6.25, 5.01 of
  6.76).
- **Switch share of warm-start busy time:** the report's switch seconds are 20% of Konsole's and 15% of Dolphin's.
  `link + relink + patch + tlb` alone is 13-14%, about 17-19% with the misses.

**Decision: both conditions hold, so step 2 is justified,** but with two conditions:

- **It needs the link record area fixed first.** With fewer capacity flushes (2/2/2/1 per vCPU against 4/3/3/3), link
  records now fill the area: 1-2 reclaims per vCPU undid 0.28-0.77 M links, where the base had none. Persistent links
  would add to that pressure.
- **Its order against P2 is the coordinator's call.** P2 still holds the largest share (59-71% of pool and host time).

### 7.3 Checkpoint 1's numbers (Mac mini M4, one run each side)

`desktop-kde-profile`, base 20261126-024119-6d48 (e86dbab6) against change 20261126-032146-6203 (eded8385), app starts
to first window:

| window | base s | change s | base busy vCPU-s | change busy vCPU-s |
|---|---|---|---|---|
| Konsole cold (`app-konsole-1:start`) | 1.801 | 1.518 | 2.508 | 2.070 |
| Konsole warm (`app-konsole-2:start`) | 1.176 | 0.806 | 1.607 | 1.066 |
| Dolphin cold (`app-dolphin-1:start`) | 1.698 | 1.706 | 2.415 | 2.427 |
| Dolphin warm (`app-dolphin-2:start`) | 1.041 | 1.372 | 1.548 | 1.936 |

- **Konsole warm, by bucket:** `pool` 0.353 → 0.100 s, `host` 0.177 → 0.094 s, `miss` 0.114 → 0.036 s, `link` 0.152 →
  0.095 s.
- **Dolphin warm, run again** (20261126-034139-bb8f): 1.778 s, 114,603 requests. So it repeats, but the cause is
  where the capacity flushes land, not a dearer warm start:
  - In bb8f, three of the four vCPUs each make 30-40 k requests inside the 1.8 s window (base: 2-9.5 k each). That is
    the pattern of the metadata arena filling (`full[1]`) and its flush dropping each vCPU's code at about the same
    point.
  - With no instance records the arena fills more slowly (2/2/2/1 flushes per vCPU against 4/3/3/3). The change's
    second flush falls near the warm Dolphin start, while the base's fell elsewhere (its warm Konsole start has 26 k
    requests on vCPU 0).
  - The code area is at its 128 MiB maximum, so the flushes cannot be moved off the case. Single windows are confounded
    both ways; the whole run is the fair reading.
  - bb8f's other windows: Konsole cold 1.544 s, warm 1.044 s (base 1.801, 1.176); Dolphin cold 1.342 s (base 1.698).
- **Whole run** (base, change 6203, change bb8f):
  - vCPU CPU time: 147.2, 137.1 and 140.2 s (−5 to −7%).
  - The session's start to ready: 78.3, 71.3 and 73.9 busy vCPU-s.
  - The four app windows: 19.99, 18.48 and 18.74 busy vCPU-s (−7%).
  - The UI windows: 24.10, 22.83 and 22.70 busy vCPU-s (−5%).
  - Translation requests outside the session's start: 1,028,918, 931,834 and 941,015.
- **Counters per vCPU:**
  - `fkeys.lost` is 0.40-0.45 M of 1.10-1.21 M templates found: the old hint table lost about 35% of them.
  - `fkeys.full` is 0.
  - INVLPG/#PF scans: 1,254-1,799, with 6.4-23.6 M list slots read (about 5,000-16,600 per scan).
- **Others (the coordinator's reading of the same runs):**
  - `desktop-kde-browser-profile`: Chromium cold start 9.41 → 8.57 s, warm 6.61 → 5.90 s, host requests 828k → 642k
    and 643k → 494k. Speedometer 2.42 → 2.35, within its ±16% spread.
  - `guest-luajit-interp` (base 20261126-030949-67a2, change 20261126-032144-aa00): 3.563 / 3.470 s → 3.426 / 3.289 s,
    no regression.


## 8. After checkpoint 1: the link record area, then step 2 (design for review)

The coordinator's decision (November 26): fix the link record area first, then build step 2, absolute user blocks
first, in parallel with P2.

### 8.1 The link record area no longer fills

**Measured:**
- At checkpoint 1 the link record area filled 1-2 times per vCPU.
- Each reclaim undid 0.28-0.77 M links (`reclaims`); the base had none.
- With no instance records the arena fills more slowly, so a flush empties the record area less often.
- P2's regions (a larger code cache) and step 2's persistent links both raise the pressure.

**Why it can fill at all:** live link records are bounded by the link points of live blocks: at most
`MAX_BLOCKS × MAX_LINK_POINTS` = 2^20 × 4 = 4 Mi. The area holds 512 Ki (`MAX_LINKS`, 24 B each, 12 MiB in the lookup
area). Suspended records (the relink plan's) come on top, up to one per suspended link. They are dropped first at a
reclaim, with no code written (`drop_suspended`).

**Change:**
- The records move out of the lookup area into a span of their own, `LINKS_VA = PRIVATE + 0x1800_0000`, with
  `MAX_LINKS = 4 Mi` (96 MiB, backed as touched).
- That address range is free in `main`'s layout (between the code area and its write alias) and in P2's (whose code
  area moves to PRIVATE + 64 GiB, `shared-code-regions` 53e35f01).
- With this size only suspended records can fill the area. The reclaim then drops them and nothing else: the `prune`
  and `unlink_all` stages are reached only under a test cap (`NATEMU_LINK_CAP`, K1's `link-full`), which stays as it is.
- The lookup area gives up 12 MiB.

**Code:**
- **abi:** `LINKS_VA`, `LINKS_BYTES`, and a backing offset (`LINKS_OFFSET`) in the vCPU's block, before the code area.
- **host:** map the span, EL1 read-write and never executable, as the metadata arena is; read records from
  `LINKS_OFFSET` (`execution.rs`'s end-of-run checks, the program driver's record writer).
- **runtime:** `record(i)` from `LINKS_VA`.

**Checks:**
- K1's `link-full` still fills a capped area: it runs with `NATEMU_LINK_CAP`, and its counts are unchanged.
- A unit test asserts `MAX_LINKS ≥ MAX_BLOCKS × MAX_LINK_POINTS`. It is shown to fail at the old 512 Ki.
- A k1 program fills more than 512 Ki links with no reclaim: it is shown to fail on the old layout.

### 8.2 Step 2, absolute user blocks first

The review's step 2 findings (6, 4-16) are part of this design. Only what changes from 2.3-2.5 is listed.

**Which blocks get a guard:**
- Absolute (not PIC) blocks of a user (CPL 3) context, with a non-global leaf and all bytes on one linear page.
- Not the step block, nor a SYSTEM record.
- Shared translations keep step 1's scheme; their guard is the meeting point with P2 (8.3).

**The code TLB:**
- 32 Ki entries of 8 bytes (256 KiB, review finding 8), direct-mapped by `(va >> 12) & 0x7fff`. An entry is
  `vpn | VALID << 35 | frame << 36`.
- It is mapped EL0 read-only in the 64 pages after the guest-state page. `guest_state_alias_valid` gains the room
  check (finding 15).
- **Filled** at every resident entry into a guarded block that is current (finding 5): the block's `checked == epoch`
  says a walk in this epoch found its page at its frame. Also filled by each revalidation and placement of a guarded
  block.
- **Never filled** under `REUSE_CHECK`, a page-record check mode, `pic-count`, or an armed DR7 (finding 14), nor for a
  page holding a host fence or the stop RIP.
- **Emptied** at:
  - every `tlb` (CR3 load and global flush);
  - a reset;
  - a change of the host's `stop_at` (finding 10);
  - INVLPG and #PF: the page's entry, and the region's 512 entries if a large leaf filled one there.
  
  The emptied indices come from a fill list of 4096 entries; past that, the whole table is cleared.

**The guard of an absolute block B** (RIP r, frame F, constant index i and expected entry E): a stub after B's code
(finding 7):

```
G0: add  x1, x28, #(4096 + 8 i) >> 12, lsl #12
G1: ldr  x1, [x1, #(4096 + 8 i) & 0xfff]
G2: ldr  x2, E                 // the stub's 8-byte literal
G3: cmp  x1, x2
G4: b.ne miss
G5: b    B's (hot) entry
miss: movz/movk x0, #r (3 words); b GUARD_MISS   (as built, section 10)
E:   .quad vpn(r) | VALID << 35 | F << 36
```

- **Cost:** a hit runs 6 instructions, with two loads (the table and the literal) and one taken branch more than a plain
  link. Today's cross-page link runs 1, but is undone and made again at every CR3 load.
- **Registers:** x0 is not needed on a hit, so a link into B patches the move's first word to `b stub`, as a plain link
  does. x1 and x2 are dead at every exit and after the dispatcher slot (review finding 10).
- **L1:** an L1 entry for B holds the stub's address (x0 = r from the dispatcher).

**Persistence:**
- Links into a guarded block go in a new segment, `GUARDED`. Neither a CR3 load nor a global flush suspends or
  prunes it; the death of either end does.
- L1 entries owned by guarded blocks are not listed, so `tlb(false)` leaves them (finding 6). `tlb(true)` still
  clears whole tables (finding 16).
- INVLPG and #PF go on making B not current (linear chains). They leave B's guarded links and L1 entries alone: the
  code-TLB entry goes, so the guard misses.
- An L1 fill replaces the way whose tag equals the RIP, for every block (finding 4). Step 1b does this for translations
  already.
- Guarded links keep the checked links' exclusions (finding 12): no guarded link from a REP self-exit or into an
  `STI_FIRST` self-target, and none while DR7 is armed.

**PC classes:**
- A stop or interrupt at any stub word means "B not entered; RIP = r" (finding 13). `block_at` maps the stub's range to
  B through a new `Block` field, `guard` (the stub's code-area offset; 0: none).
- `interrupted` treats the stub like B's first word.
- A k1 case stops at every stub word.

**What the measurement must show** (pooled checkpoint 2):
- The switch buckets drop (`link`, `relink`, `patch`, `tlb`, misses after CR3 loads).
- `guest-luajit-interp` within twice its noise.
- Fallbacks if it fails: guard links only, L1 cleared as now (section 5); then entry poisoning.

### 8.3 Where step 2 meets P2

P2's shared code (`shared-code-plan.md` on `shared-code-regions`, section 2) links across pages only by checked links: a
`b` to the dispatcher slot, which probes the running vCPU's own L1, about 9 ns more than a direct branch. Its plan
expects this: "When P1's guarded entries land, a checked link becomes a direct branch to the target's guard".

The guard reads the running vCPU's code TLB through x28, so a guarded link is right on every vCPU and in every address
space. It is the form P2 can patch once, in shared code, for all vCPUs. For that, P2's shared translations need the
PIC form of the guard (2.4's sequence, x0-indexed), not 8.2's constant-index stub, which serves one RIP.

Order: step 2 builds the absolute stub and the code TLB, and the PIC guard after it. P2 then turns its checked links into
guarded ones. The link records' segments (`GUARDED`) and `cache::link`'s forms are this plan's; P2's chunk 2 does not
touch them.

### 8.4 Review of section 8 (Fable, independent, November 26) and the response

The reviewer found 8.1 sound with corrections, and 8.2's ITLB argument sound in every listed case (an absolute block
reached by a plain link, from L1, after a CR3 load, after INVLPG or #PF, with large leaves, in 32-bit code, alive but not
current, killed by a write). Two defects in the stub as drawn were blocking.

**Blocking, adopted:**

1. **The stub must not write NZCV**, which holds guest flags across every exit. It compares with `eor x1, x1, x2; cbnz
   x1, miss`, as the dispatcher does. A k1 case enters a guarded block with each NZCV bit set, on a hit and on a miss.
2. **The code area is EL0 execute-only,** so the stub cannot load a literal. The expected entry is built with
   `movz`/`movk`. It is packed to 48 bits: `(frame | (vpn >> 15) << 28) + 1` (the slot index gives vpn's low 15 bits;
   0 is empty), so three move words. The hit path is `add, ldr, movz, movk, movk, eor, cbnz, b`: 8 instructions, one
   load, one taken branch.

**Should, adopted:**

3. Fill the code TLB at every resident entry into a block that is `current`, global leaf or not. The entry is a TLB entry
   of this epoch either way.
4. A guard is suppressed by the plain-link rule (`at_breakpoint` of its RIP), not by "DR7 armed": data watchpoints keep
   translations. Under a mode-wide suppression (the reuse check, a page-record check mode, `pic-count`), blocks get the
   plain entry and plain CR3-segment links. A frame of 2^28 or more is decided at placement: no stub.
5. **Conflicts:** each guarded block has two candidate slots (two hashes of its vpn). On a conflict miss (B current, its
   slot holding another valid page) the resident re-points B's two index words to the other slot. Fills write B's
   current slot; INVLPG and #PF clear both candidates of the page. Conflicts are counted.
6. The reclaim continues past `drop_suspended` only while `links_full` still holds (and an unset cap is the full area).
   `MAX_LINKS = MAX_BLOCKS × MAX_LINK_POINTS + MAX_LINK_POINTS`, asserted.
7. Suspended records are bounded on their own: they are dropped (no code written) when they reach 1 Mi. Reclaims are
   counted split into suspended records dropped and live links undone.
8. **P2's kill:** P2's shared area has no inbound records, so a guarded cross-page link into a dead block would still hit
   (the code TLB says only that the page still maps the frame). A kill therefore poisons the dead block's guard: one word
   to `b miss`, a form Arm allows to change under a running core. In this plan's private cache, death also unlinks the
   inbound records and empties L1, as today. 8.3 records it, and that the shared area will have no `GUARDED` records.
   8.2's constant-index stub serves P2's absolute user blocks unchanged; only PIC translations need 2.4's form.
9. **Code the plan did not list, adopted:**
   - The records: `host/execution.rs`'s `link_record` and the program driver's writers (`program.rs` 1030, 1094).
   - `check_links`: a first word may branch to a guard stub.
   - The segment checks (`host/execution.rs` 1755, 1818, 1868).
   - `unlink_word`/`links_to`, and `check_no_dead_links`'s rule for `GUARDED`.
   - `GUARDED` sits between `TLB` and `LOCAL`, so INVLPG's `unlink_inbound(n, false)` leaves it and `resume`'s swaps stay
     valid.
   - `cut_one(n, false)` leaves a guarded block's L1 ways.
10. The fill list appends a slot only when the slot did not hold the entry already.
11. Layout asserts: the shadow pool ends below `LINKS_VA`, and `LINKS_VA + LINKS_BYTES <= CODE_WRITE_VA` (P2:
    `<= META_VA`).

**Notes, adopted:**

12. The INVLPG/#PF clear of the code TLB is unconditional: a guarded block that is not current is reachable through the
    table alone.
13. The stub enters at `entry()`: a guarded link loses the x87 run-test skip, so the test runs once more. This is
    correct.
14. The stub lies inside B's code span (after its body, before its tail), so `block_at` finds B with no new search. A
    stop at a stub word is "not entered, RIP = r", as the `pic-count` stub's precedent does.
15. **The table's mapping:** one 2 MiB block, EL0 read-only, in the user space's level 2 on the alias's free side (as DL1
    is), not 64 leaf pages. Supervisor code cannot read it, and `guest_state_alias_valid` needs no room check. The
    guard's `add x1, x28, #hi, lsl #12` reaches it.
16. With 12 MiB freed in the lookup area, `FKEYS` gets one slot per block (2^21 entries).

**Not adopted for now:** the lead form, a guard before the hot entry that falls through on a hit. It saves the taken
branch but needs room in the hot zone before each split block's hot copy, so it waits for the measurement. The baseline
to beat is P2 chunk 4's A/B of checked cross-page links in the private cache.

### 8.5 Built: the link record area (a7479c30)

- **The layout:**
  - `LINKS_VA = PRIVATE + 0x1800_0000`, `LINKS_BYTES` 98 MiB, `LINKS_OFFSET` before the code area in the backing.
  - `MAX_LINKS = MAX_BLOCKS × MAX_LINK_POINTS + MAX_LINK_POINTS`.
  - Asserts: the span lies between the code area and its write alias, and the shadow pool ends below it.
- **The reclaim:** with no test cap, a reclaim drops the suspended records and goes on only if the area is still full.
  Suspended records are also dropped at `MAX_SUSPENDED` (1 Mi) at a TLB event.
- **The counters** (`reclaimed_suspended`, `suspended_dropped`) are in the k1 stats' `fkeys` block.
- **`FKEYS`** takes the freed lookup room: 2^21 entries.
- **The checks:**
  - The unit test `link_records_hold_every_live_link_point`.
  - k1's `link-full` at the production size (`k1-full-links`): the old 512 Ki chain, 580-697 k links, must now fill
    nothing. At a test cap, `link-full` and `relink-reclaim` still exercise every reclaim stage.
  - Both are shown to fail with `MAX_LINKS` back at 512 Ki (branch `fk-broken-3`, never merged).

## 9. Step 1c: checked links by default, and step 2's new rationale

**The evidence:** P2's link-form A/B (shared-code plan chunk 4; scratch commit 650bebb2 on `shared-code-linkform`, Mac
mini M4, `~/de-linkform`). With `NATEMU_LINK_FORM=checked`, every link that is not local is a checked link through the
dispatcher slot, on today's private areas:
- desktop Speedometer: 2.30 and 2.35 without it, 3.00 and 3.06 with it (about +30% over two pairs);
- app starts: within noise;
- `guest-luajit-interp`: no regression (3.583/3.451 s against 3.509/3.415 s);
- no suspensions or relinks at CR3 loads.

**Built (step 1c):** checked is the default.
- `Control::link_form_plain`, the host's `NATEMU_LINK_FORM=plain`, is the debug key for the plain form.
- A link from another page patches its exit's `svc` word to the context's dispatcher slot. That slot probes L1 at the RIP
  the exit built, so a TLB event empties an L1 entry and undoes no link.
- The plain form's machinery (suspension, relink, the CR3 and TLB segments) stays, used only under the key.

**Tests:**
- k1's relink programs run under `NATEMU_LINK_FORM=plain`.
- `relink-many` runs once more in the default form and must suspend and relink nothing. This is shown to fail on a
  resident that makes plain links whatever the switch (`fk-broken-4`, never merged).
- The host's end-of-run link checks pass unchanged: the checked form was already one they model.

**Step 2's rationale, revised:** after 1c no link is undone at a CR3 load. What is left is the checked link's cost: an
L1 probe on every cross-page transfer (about 9 ns against a direct branch), and one L1 refill per block per address-space
switch. Step 2's guards turn hot cross-page links back into direct branches into a guard stub: 8 instructions and one load,
with no switch cost. They are measured against step 1c at the pooled checkpoint 2, not against the plain form.

**Review of step 1c (Fable, independent, November 26).** No blocking finding: the checked form is sound for every pair it
now covers (kernel and global targets, 32-bit code, split blocks' cold exits, precise stops, REP and `STI_FIRST`
self-links). Adopted:

- **S1.** The PIC counters (`CHECKED_LINKS`, the absolute-into-shared class) count what they counted before 1c, so the
  PIC tests' floors can still fail. A link checked only because it crosses pages is counted on its own
  (`links_checked_cross`, in the k1 stats' `fkeys` block).
- **S2.** The default-form `relink-many` run also checks its registers (every site reached the right V after each CR3
  load) and that at least one checked cross-page link per site was made, so it cannot pass vacuously.

Noted, not changed:

- A context with no dispatcher slot (more than 127 contexts since a flush) gets no cross-page link now, where it used to
  get plain ones.
- A context whose L1 table was given away misses on every cross-page link until it takes a table again.
- Every cross-page exit of a split block runs through its cold code.
- Three hot targets with the same low 15 RIP bits thrash through the resident.

The last two are part of what step 2's direct guarded links remove.

## 10. Step 2 built: the code TLB and guarded absolute user blocks

**What is built, and where it departs from 8.2:**

- **The code TLB:** a 2 MiB block after the data tables (`cache::CTLB`), 32 Ki 8-byte entries, page and frame packed
  to 48 bits (`ctlb_entry`), and a fill list.
  - It is mapped EL0 read-only in the user space only, at `ctlb_block` beside the data tables. x28 reaches it at
    `ctlb_delta`.
  - It is filled at every `fill_l1` of a current guarded block.
  - It is emptied at every `tlb` (CR3 load, global flush, `tables_written`) and reset. At INVLPG and #PF the page's
    slot is emptied, and every slot of the 2 MiB region of a large leaf.
- **The stub sits before the code** (48 bytes, `GUARD_LEAD`, `Block::guard_entry`), not after the body as 8.4 note 14
  said. `start()` and `block_at` cover it.
  - The stub's words come from `natemu_abi::cache::guard_words`.
  - A unit test decodes them: the add/sub reach on both sides of the alias, the slot, the entry, and the branches.
  - The host's end-of-run check recomputes every guarded block's stub.
- **Guarded links live in the LOCAL segment, not a segment of their own.** Their lifecycle is the local links': never
  undone by a TLB event, undone by either end's death, dropped first by a strict reclaim. The target gets
  `LOCAL_INBOUND`. They take the place of step 1c's checked form for links from absolute code on another page into a
  guarded block.
- **L1 entries:** a guarded block's are not listed for CR3 loads, survive INVLPG (`cut_one`), and go at death, at a
  table's change of context, or at a global flush. An L1 fill now replaces the way whose tag is the RIP, for every block.
- **Not placed** under `NATEMU_GUARDS=off`, the reuse check, a page-record check mode or the PIC tests.

**Review (Fable, independent): two blocking defects, both of which halt the vCPU.** Neither runs wrong code.

- **B1:** undoing a guarded link (a death, a flush, a reclaim) halted `BAD_LINK`. Fixed: `unlink_word` accepts the
  stub branch.
- **B2:** an interrupt at stub words 0-10 found no block. Fixed: `block_at` looks `GUARD_LEAD` ahead.

The review's SHOULD findings, all fixed:

- **S1:** the host check now takes stub entries with no x87 run-test skip.
- **S2:** the host mapping test expects the code TLB's block.
- **S3:** frames stay below 2^28 - 1, so no entry builds as 0.
- **S4:** the words builder is pure, with its decode test and the host recheck.
- **S7:** the tag rule applies to every block.
- **S5 and S6, the tests:**
  - `guard-flags` gained an OF case;
  - `guard-hits` bounds the dispatcher misses over K calls against the same run with no guards;
  - `guard-kill` rewrites the target's frame and flushes, with the guarded link live.

**Tests:**

- The k1 programs, natemu only, CPL 3, without the reuse check: `guard-cr3`, `guard-invlpg`, `guard-flags`,
  `guard-hits` and `guard-kill`. Each asserts a guarded link and code-TLB fills.
- Shown to fail when broken (branches `fk-broken-5` to `fk-broken-8`, never merged):
  - a guard that always hits fails `guard-cr3` and `guard-invlpg`;
  - a stub that writes NZCV fails `guard-flags`;
  - no INVLPG clear fails `guard-invlpg`;
  - a guard that always misses fails `guard-hits`;
  - B1 reverted fails `guard-kill`.

**Still open:**

- A k1 case that stops at every stub word. It needs a kick source; the rented M4's kicker is throttled.
- The two-candidate slots (8.4 item 5): conflicts are only counted (`ctlb_conflicts`).
- A large-leaf INVLPG case, and running the link and kill programs once more with guards on.

## 11. Step 1d, guard misses counted, and step 2's A/B

**Guard misses are counted** (d1667718). The stub's miss exits at `GUARD_MISS`, a copy of the dispatcher's `svc #MISS`
in slot 0's unused words. The resident counts it (`fkeys.guard_misses`) and serves it as a dispatcher miss. Guard hits
run wholly at EL0, so they could be counted only by a store in every stub; they are not measured. The fast miss path
enters a guarded block at its body, since it has found the block current.

**Step 1d** (989309b8):
- **The evidence:** checkpoint 2's attribution put step 1c's cost on `guest-build` at about 4-7%; the plain form was at
  or below base.
- **The reasoning:** a link into a global block was never undone by a CR3 load. It sits in the TLB segment, suspended
  only at a global flush. So checking it bought nothing and cost every kernel cross-page transfer an L1 probe.
- **The change:** by default only links into non-global blocks are checked. `NATEMU_LINK_FORM=checked` is 1c's form,
  `plain` the old one.
- **The check:** `relink-global-many` runs once more in the default form. Exactly Z's 2 links must be checked, and V's
  must be relinked after each round's global flushes.
- **Shown to fail:** with the form pinned to `checked` it reports "17 cross-page links checked, 0 relinked".

**Review of d1667718 and 989309b8** (Fable, independent): d1667718 has no defect.
- **B1, adopted as a defence; its premise rebutted for the tests.** A link's form now follows the target's `GLOBAL`,
  which a revalidation rewrites. A source with two points naming one target could have one linked checked and the
  other plain, and the linker's consistency check would halt. The check now accepts a linked point in either sound
  form.
  - But `link` links every point naming the target in one call, and every undo (suspension, prune, `unlink_all`,
    death) undoes them together. Mixed forms need a point unlinked alone: only `relink` dropping one record whose word
    changed.
  - The k1 case added for it, `link-two-forms` (`je T; jmp T` across a PGE change), passes with and without the fix.
    So it is kept as a regression case for two-exit sources across PGE changes, not as the fix's check.
- **S1, adopted:** the default-form check is exact (Z's 2 checked, RM relinked a round).
- **S2, adopted:** `relink` drops a suspended record whose target came back non-global, so the point links in the
  checked form at its next exit.
- **Notes:** stale comments fixed (the stub exits to `GUARD_MISS`). `guard_misses` sits mid-struct, harmless because
  host and resident are always built together.

**The kicker cases on the rented M4** fail the same way on `main` (be88e684) on that machine: 28 failed, all `kicks`
cases of k2 (`sysfast`, `verw`, `stifirst`, `swapgs` kicks and events, `repmovs`, `repbulk`). They pass on the M2 at
this branch's tip. The machine's host kicker is throttled.

**Step 2's A/B** (Mac mini M4, `~/de-profile`): one build, `step2-measure` 31a0bed4 (checkpoint 2's tip f6010122 plus
step 2 and step 1d). Guards on is the default; guards off is `NATEMU_GUARDS=off`.

| case | guards on | guards off |
|---|---|---|
| `guest-luajit-interp` | 20261126-053942-ce47: 3.654 / 3.504 s | 20261126-053943-6696: 3.562 / 3.449 s |
| `desktop-kde-profile` + `desktop-kde-browser-profile` | 20261126-053944-b24b | 20261126-053945-b913 |
| `guest-build` (twice a side) | 20261126-053946-0c53, -11ec | 20261126-053947-882b, -1038 |

- **luajit-interp:** the guards cost about 2%, near noise, on a single-process case.
- **The reference for `guest-build`:** checkpoint 2's plain-form numbers (second run 5.56-5.59 s; base 5.79-5.99 s;
  change 67a776e2 5.98-6.22 s).

**The results** (read November 26; one build, so on and off differ only in `NATEMU_GUARDS`):

- **`guest-build`, wall seconds (first run / second run):**

  | | run 1 | run 2 |
  |---|---|---|
  | guards on | 7.17 / 5.455 | 7.211 / 5.451 |
  | guards off | 7.97 / 6.15 | 7.96 / 6.391 |

  The guards take 11-15% off the second run. They also put it below checkpoint 2's plain form (5.56-5.59 s) and its
  base (5.79-5.99 s): they recover step 1c's cost in full and then some. Run 1038's numbers are complete
  (`bench.jsonl`), but its run folder has no `result.json`.
- **`desktop-kde-profile`** (one boot a side, wall s / busy vCPU-s):

  | window | guards on | guards off |
  |---|---|---|
  | session start to idle desktop (`session:idle-ready`) | 24.69 / 69.38 | 31.06 / 78.41 |
  | the four app `:start` windows, summed | 5.43 / 7.29 | 5.53 / 7.58 |
  | `ui-window-grow` frames/s | 10.82 | 10.46 |
  | `ui-window-move` frames/s | 9.50 | 9.52 |

  - The session start is 6.4 s (21%) shorter with guards and takes 9.0 busy vCPU-s less, beyond the 71-78 s spread of
    the earlier boots of 7.3.
  - App starts: Konsole is a little slower with guards (1.52/1.20 s against 1.42/1.02 s), Dolphin a little faster
    (1.51/1.20 against 1.78/1.31). In sum they are equal, within single windows' noise (7.3).
  - Frame rates do not move.
  - **Counters, guards on, over the boot (four vCPUs):** 2.60 M guard misses (0.45-1.12 M a vCPU) and 54.8 k code-TLB
    conflicts. Guards off: both 0.
- **`desktop-kde-browser-profile`:**
  - Speedometer 3.18 ± 0.62 with guards, 2.97 ± 0.64 without: within its ±20% spread.
  - Chromium cold start 8.16 s with guards, 7.50 s without (busy 18.8 / 16.2 vCPU-s); warm start 4.25 / 3.97 s.
  - Firefox's first start is 9.66 / 9.61 s.
  - These are single windows, with no second run.

**Chromium, run again** (`desktop-kde-browser-profile` alone, same build; guards on 20261126-150702-908f, off
20261126-151244-7d4d):

| | guards on (b24b, 908f) | guards off (b913, 7d4d) |
|---|---|---|
| Chromium cold start, s | 8.16, 8.02 | 7.50, 7.44 |
| Chromium cold start, busy vCPU-s | 18.8, 18.8 | 16.2, 16.5 |
| Chromium warm start, s | 4.25, 3.83 | 3.97, 3.56 |
| Firefox cold start, s | 9.66, 9.17 | 9.61, 9.51 |
| Speedometer | 3.18, 3.20 | 2.97, 3.11 |

The Chromium cost repeats: with guards a cold start is about 0.6 s (8%) longer and takes 14% more busy vCPU time, and a warm
start about 0.27 s longer. 908f's first vCPU alone counts 6.25 M guard misses. Firefox and Speedometer do not move.

**Decision: step 2 stays on by default.** It is a clear gain on `guest-build` and on the desktop's session start. But
Chromium's start, one of the KDE target's readings, is a repeated loss, so the cost of a guard miss in a many-process
start is the next thing to find (the counters by window, then the policies below or the two-candidate slots of 8.4
item 5).

**If the guards had recovered `guest-build` only in part,** the policies to try (both cheap, both behind the existing keys):
- **Plain links for blocks of a low CR3 rate:** count each address space's CR3 loads, and link plainly into blocks whose
  space is switched to rarely. They are suspended and relinked at most a few times.
- **Direct links only where the code TLB earns them:** keep a link checked until its target page has been filled in the
  code TLB in N epochs, then make it guarded.

## 12. L2: block_at fix, guard-stub regression, and predeclared desktop runs

**The fix (`91fe2a1b`):** `block_at` now binary-searches the extent-start table rather than failing when a guard-stub
lookup lands near zero-sized trailing blocks, with an exact guard-stop regression (`guard_stop.rs` + `guard-stub-records`).

**Verification and negative controls:**
- **Negative control (`20261127-024240-abc0`):** the old `block_at` search failed 9/41 across `two-system` and
  `system-then-code` with `bad_stop detail=11`.
- **Mutant control (`20261127-025338-ba8e`):** a skipped-bank mutant failed 38/38 stub schedules.
- **Passing regression (`20261127-025259-cf00`):** `guard-stub-records` passed 41/41.
- **Branch suite (`20261127-025619-983f`):** `run --base 320133c8` passed 102,852 checks across 13 families; `k2-stifirst`
  passed 20/20 isolated (`20261127-033658-3482`).
- **Independent review:** Codex (`gpt-6.2-astra`) review and sign-off (`task-82` + `01a21ca6`).

**Predeclared desktop runs on `91fe2a1b`** (six clean runs, zero `BAD_STOP`, `problems=[]`):
- **M4:**
  - `desktop-kde-profile` `20261127-033911-b388` (`idle_desktop` 24.48 s)
  - `desktop-kde-browser-profile` `20261127-034301-c5cc` (Chromium cold 8.57 s, warm 4.16 s; Firefox 9.10 / 5.06 s; Speedometer 3.04)
  - `desktop-kde-browser-profile` `20261127-034859-a9c8` (Chromium cold 7.94 s, warm 4.07 s; Firefox 8.99 / 3.87 s; Speedometer 3.17)
- **M2 Max:**
  - `desktop-kde-profile` `20261127-033829-5335` and `20261127-034204-e3ec`
  - `desktop-kde-browser-profile` `20261127-034738-3191`

### L4 Guard-cost decision

Measured on Mac mini M4 (November 27, 2026), comparing guard local-skip (`4d523e73` on branch `m1-l4`) against L2 (`91fe2a1b`):

- **`guest-build`** (two clean alternating pairs, 1 vCPU, `contended=[]`, no `foreign_load`; first / second run):
  - **L2 (`91fe2a1b`):** `20261127-041051-6a33` (`6.507 s` / `5.213 s`) and `20261127-041258-d877` (`6.617 s` / `5.326 s`), mean `6.562 s` / `5.270 s`.
  - **L4 (`4d523e73`):** `20261127-041117-e999` (`6.796 s` / `5.384 s`) and `20261127-041314-4559` (`6.733 s` / `5.392 s`), mean `6.765 s` / `5.388 s` (`+3.1%` / `+2.2%` slower).
- **`desktop-kde-browser-profile`** (`4d523e73`, run `20261127-041334-4d17`, `problems=[]`, 0 `BAD_STOP`):
  - Cold Chromium start `7.927 s` (within noise of L2 `91fe2a1b`: `20261127-034301-c5cc` `8.566 s` and `20261127-034859-a9c8` `7.935 s`); warm Chromium `3.988 s`, cold Firefox `9.165 s`, Speedometer `3.38 ± 0.69`.
- **Decision:** Leave guard local-skip out of `m1` (slower on `guest-build` in both pairs and within noise on cold Chromium; branch `m1-l4` preserved locally). `m1`'s code tip remains `91fe2a1b` (with L3 `ecb5717e`).

### L5 Scorecard, cold-row breakdown, and next-investment ranges

Measured on Mac mini M4 (4 vCPUs, November 26–27, 2026), comparing `m1` (`91fe2a1b` / `ecb5717e`, plus diagnostic `m1-l5-diag`) against the stored `main` base at `b168570a` (with desktop cases from `be88e684`). All runs exited `0` with `problems=[]`, `capped=False`, and `contended=[]`. The profiled runs also recorded `log.dropped=0` and `log.failed=0`. Runs after L3 (`ecb5717e`: `0183`, `0c2a`, `328d`, `e89f`, `a60b`, and plain runs `1448`, `c682`, `7796`, `b50a`) all have `ui_effects_verified=True`; earlier runs (`84bd`, `fd58`, `2ecf`, `211a`, `b388`, `c5cc`, `a9c8`) predated `driver.sh` `v=2`'s `DE State` check.

#### L5.1 Run inventory

| Group | Commit / build | Case | Runs (`short_id`) | Notes |
|---|---|---|---|---|
| Base `perf` | `b168570a` (`perf`) | `desktop-kde-profile` | `84bd`, `0183` | `0183` ran on L3 `driver v=2` |
| Base `perf` | `b168570a` (`perf`) | `desktop-kde-browser-profile` | `84bd`, `fd58` | `84bd` reused per spec 3.1 |
| Base `perf` warm | `b168570a` (`perf`) | `*-warm` (boot 0 cold / boot 1 warm) | `2ecf` (KDE), `211a` (Browser) | Boot 0 is a cache-fill reference |
| M1 `perf` | `91fe2a1b` (`perf`) | `desktop-kde-profile` | `b388` (`v=1`), `e89f` (`m1-l5-diag`, `v=2`) | `e89f` carries `desk` `peer_live` |
| M1 `perf` | `91fe2a1b` (`perf`) | `desktop-kde-browser-profile` | `c5cc`, `a9c8` (`v=1`), `a60b` (`m1-l5-diag`, `v=2`) | `a60b` carries `desk` `peer_live` |
| M1 `perf` warm | `91fe2a1b` (`perf`) | `*-warm` (boot 0 cold / boot 1 warm) | `0c2a` (KDE, `v=2`), `328d` (Browser, `v=2`) | `ui_effects_verified=True` |
| Plain confirmation | `b168570a` vs `ecb5717e` (plain) | `desktop-kde` / `desktop-kde-browser` | `c682` vs `1448` (KDE); `b50a` vs `7796` (Browser) | Ordinary resident, no request log |

#### L5.2 Scorecard summary and noise

Cache-off noise (`SD`) is computed strictly from the ordinary cache-off profile repeats: **2 base / 1 M1** on `desktop-kde-profile` (`84bd`, `0183` vs `b388`) and **2 base / 2 M1** on `desktop-kde-browser-profile` (`84bd`, `fd58` vs `c5cc`, `a9c8`). Untimed `-warm` boot-0 fills (`2ecf`, `211a`, `0c2a`, `328d`, which run with `fill=True` writing the compile cache: `0.78–0.83` vCPU-s `disk_put` in `session:shell` vs `0.005 s` cache-off) and the separately instrumented `m1-l5-diag` runs (`e89f`, `a60b`) are shown alongside as reference values, not pooled into the cache-off SDs. Because each plain build ran once, its `> 2×` check uses the cache-off profiled base SD as a proxy.

**Cold desktop and browser scorecard (`perf` cache-off means and plain confirmation, seconds; Speedometer score higher is better):**

| Row | Base `perf` mean (cache-off SD) | M1 `perf` (`b388` / `c5cc,a9c8`) | Delta `perf` | Base plain (`c682` / `b50a`) | M1 plain (`1448` / `7796`) | Delta plain | `> 2×` base SD? |
|---|---:|---:|---:|---:|---:|---:|---|
| `session:shell` (KDE) | `24.316` (`0.528`) | `22.236` (ref: `0c2a-b0` `23.804`, `e89f` `22.526`) | `-2.080 s` (`-8.6%`) | `23.945` | `20.945` | `-3.000 s` (`-12.5%`) | Yes (`3.94×` `perf`, `5.68×` plain proxy) |
| `session:idle-ready` (KDE) | `30.985` (`0.304`) | `24.480` (ref: `0c2a-b0` `30.880`, `e89f` `30.640`) | `-6.505 s` (`-21.0%`) | `30.730` | `24.850` | `-5.880 s` (`-19.1%`) | Yes (`21.39×` `perf`, `19.34×` plain; `idle` variance on `0c2a-b0`/`e89f`) |
| `app-konsole-1:start` | `1.691` (`0.084`) | `1.527` (ref: `0c2a-b0` `1.765`, `e89f` `1.680`) | `-0.164 s` (`-9.7%`) | `1.648` | `1.319` | `-0.329 s` (`-20.0%`) | Plain only (`1.94×` `perf`, `3.91×` plain proxy) |
| `app-konsole-1` (total) | `6.860` (`1.018`) | `5.770` (ref: `0c2a-b0` `5.640`, `e89f` `5.500`) | `-1.090 s` (`-15.9%`) | `5.150` | `5.240` | `+0.090 s` (`+1.7%`) | No (`1.07×` `perf`; plain total slower from `:settle`) |
| `app-konsole-2:start` (warm) | `1.096` (`0.038`) | `1.038` (ref: `0c2a-b0` `1.195`, `e89f` `1.052`) | `-0.058 s` (`-5.3%`) | `1.125` | `0.994` | `-0.131 s` (`-11.6%`) | Plain only (`1.5×` `perf`, `3.43×` plain proxy) |
| `app-dolphin-1:start` | `1.809` (`0.231`) | `1.690` (ref: `0c2a-b0` `1.624`, `e89f` `1.454`) | `-0.119 s` (`-6.6%`) | `1.724` | `1.638` | `-0.086 s` (`-5.0%`) | **Neither** (`0.52×` `perf`, `0.37×` plain) |
| `app-dolphin-1` (total) | `5.930` (`0.099`) | `5.050` (ref: `0c2a-b0` `5.180`, `e89f` `5.050`) | `-0.880 s` (`-14.8%`) | `5.570` | `5.340` | `-0.230 s` (`-4.1%`) | Yes (`8.9×` `perf`, `2.3×` plain proxy) |
| `app-dolphin-2:start` (warm) | `1.351` (`0.037`) | `1.234` (ref: `0c2a-b0` `0.874`, `e89f` `0.817`) | `-0.117 s` (`-8.7%`) | `1.203` | `0.976` | `-0.227 s` (`-18.9%`) | Yes (`3.18×` `perf`, `6.17×` plain proxy) |
| `session:shell` (Browser) | `24.236` (`0.216`) | `22.114` (`22.005`, `22.223`; ref `a60b` `21.944`) | `-2.122 s` (`-8.8%`) | `23.464` | `21.617` | `-1.847 s` (`-7.9%`) | Yes (`9.84×` `perf`, `8.56×` plain proxy) |
| `session:idle-ready` (Browser) | `31.420` (`0.325`) | `26.235` (`25.300`, `27.170`; ref `a60b` `24.250`) | `-5.185 s` (`-16.5%`) | `31.030` | `24.880` | `-6.150 s` (`-19.8%`) | Yes (`15.94×` `perf`, `18.91×` plain proxy) |
| `app-chromium-1:start` | `8.727` (`0.278`) | `8.251` (`8.566`, `7.935`; ref `a60b` `7.997`) | `-0.477 s` (`-5.5%`) | `8.376` | `7.787` | `-0.589 s` (`-7.0%`) | Plain only (`1.72×` `perf`, `2.12×` plain proxy) |
| `app-chromium-1` (total) | `18.480` (`0.113`) | `12.260` (`12.630`, `11.890`; ref `328d-b0` `17.550`, `a60b` `16.260`) | `-6.220 s` (`-33.7%`) | `18.480` | `11.460` | `-7.020 s` (`-38.0%`) | Yes (`55×` `perf`, `62×` plain proxy; `:settle` noisy on `328d-b0`/`a60b`) |
| `app-chromium-2:start` (warm) | `6.804` (`0.187`) | `4.110` (`4.155`, `4.065`; ref `a60b` `3.964`) | `-2.694 s` (`-39.6%`) | `6.261` | `3.754` | `-2.507 s` (`-40.0%`) | Yes (`14.4×` `perf`, `13.4×` plain proxy) |
| `app-firefox-1:start` | `9.954` (`0.040`) | `9.049` (`9.104`, `8.994`; ref `328d-b0` `9.962`, `a60b` `9.421`) | `-0.905 s` (`-9.1%`) | `9.332` | `8.150` | `-1.182 s` (`-12.7%`) | Yes (`22.85×` `perf`, `29.85×` plain proxy; fill `328d-b0` touches base) |
| `app-firefox-1` (total) | `42.605` (`4.462`) | `32.630` (`31.640`, `33.620`; ref `a60b` `37.750`) | `-9.975 s` (`-23.4%`) | `39.560` | `32.670` | `-6.890 s` (`-17.4%`) | **`perf` only** (`2.24×` `perf`; `1.54×` plain proxy) |
| `app-firefox-2:start` (warm) | `4.930` (`0.045`) | `4.462` (`5.056`, `3.867`; ref `328d-b0` `5.197`) | `-0.468 s` (`-9.5%`) | `4.675` | `4.030` | `-0.645 s` (`-13.8%`) | **Both** (`10.35×` `perf`, `14.25×` plain proxy; `perf` repeats disagree in direction: `c5cc` `5.056 s` slower than base, `a9c8` `3.867 s` faster) |
| `app-chromium-3:start` | `8.496` (`0.277`) | `4.603` (`4.629`, `4.577`; ref `a60b` `4.473`) | `-3.893 s` (`-45.8%`) | `7.695` | `4.062` | `-3.633 s` (`-47.2%`) | Yes (`14.0×` `perf`, `13.1×` plain proxy) |
| Speedometer 3.1 (score) | `2.360` (`2.415`, `2.305`) | `3.101` (`3.037`, `3.165`; ref `a60b` `3.102`) | `+0.741` (`+31.4%`) | `2.430 ± 0.42` | `3.308 ± 0.81` | `+0.878` (`+36.1%`) | Yes (clearly better, well above spread) |

**Warm variants (`-warm` boot 1, compile cache on; `2ecf` vs `0c2a` and `211a` vs `328d`):**
- `desktop-kde-profile-warm` boot 1: `session:shell` `22.064 → 18.933 s` (`-14.2%`), `session:idle-ready` `27.260 → 24.410 s` (`-10.5%`), `app-konsole-1:start` `1.839 → 1.239 s` (`-32.6%`), `app-konsole-1` `5.400 → 5.000 s` (`-7.4%`), `app-dolphin-1:start` `1.914 → 1.869 s` (`-2.4%`), `app-dolphin-1` `7.320 → 5.300 s` (`-27.6%`), `app-dolphin-2:start` `1.286 → 0.982 s` (`-23.6%`), `app-dolphin-2` `5.120 → 4.380 s` (`-14.5%`).
- `desktop-kde-browser-profile-warm` boot 1: `session:shell` `23.108 → 19.560 s` (`-15.4%`), `session:idle-ready` `30.870 → 21.960 s` (`-28.9%`), `app-chromium-1:start` `9.845 → 6.706 s` (`-31.9%`), `app-chromium-1` `17.520 → 15.680 s` (`-10.5%`), `app-chromium-2:start` `7.635 → 4.353 s` (`-43.0%`), `app-chromium-2` `17.500 → 11.330 s` (`-35.3%`), `app-firefox-1:start` `11.759 → 8.245 s` (`-29.9%`), `app-firefox-1` `40.850 → 35.730 s` (`-12.5%`), `app-firefox-2:start` `5.376 → 4.403 s` (`-18.1%`), `app-firefox-2` `31.720 → 27.530 s` (`-13.2%`). (On boot 1, `211a` also suffered much lower disk-cache hit rates than `328d`: e.g. `8,508` vs `412,630` `disk` answers on `app-chromium-1:start`.)

**Scorecard verdict (Spec 3.1):**
- M1 delivers clear wins on `session:idle-ready` (`-16.5%` to `-21.0%`), `session:shell` (`-7.9%` to `-12.5%`), Chromium warm launches (`-39.6%` to `-47.2%`), Speedometer 3.1 (`+31.4%` `perf`, `+36.1%` plain), and `-warm` boot 1.
- However, as the pre-store base, M1 **misses** Spec 3.1's strict *"every cold start row better beyond twice its noise"* criterion on the cold application `:start` rows: `app-dolphin-1:start` clears `2×` on neither `perf` (`0.52×` SD) nor plain (`0.37×` proxy SD), `app-konsole-1:start` (`1.94×` `perf`) and `app-chromium-1:start` (`1.72×` `perf`) clear `2×` only on the single plain pair using the profiled base SD proxy (`3.91×` and `2.12×`), and only `app-firefox-1:start` clears `2×` on both (`22.85×` / `29.85×`, though fill `328d-b0` `9.962 s` touches base). Observed `perf`-minus-plain `session:shell` differences span `0.371–1.291 s`, which reflects single-run variance as well as logging overhead. That cold `:start` gap is the exact subject of the L5 breakdown below.

#### L5.3 Every worse row (named trade-offs and open / noise accounting for the owner)

Every row across the ordinary `perf` profiles (`desktop-kde-profile`, `desktop-kde-browser-profile`), `-warm` (boot 0 fill and boot 1 warm), and plain confirmation where any M1 run or cohort mean read slower than `b168570a` is listed below (separately, `m1-l5-diag` `a60b` also had `app-firefox-2:start` `4.966 s` vs base `4.898` / `4.962 s` and `app-chromium-2:settle` `10.256 s`):

1. **`app-konsole-2:settle` and `app-konsole-2` total (`b388`, `0c2a` boot 0, plain `1448`; plus plain `app-konsole-1:settle`/`app-konsole-1`):** `b388` `3.752 s` / `5.610 s` vs base `3.044 s` / `5.150 s` (`+23.3%` / `+8.9%`); `0c2a-b0` `3.695 s` / `5.690 s` vs `2ecf-b0` `2.930 s` / `5.100 s`; plain `1448` `app-konsole-2:settle` `3.526 s` vs `3.305 s` (`+6.7%`) and `app-konsole-1:settle` `3.221 s` / total `5.240 s` vs `2.642 s` / `5.150 s` (`+21.9%` / `+1.7%`). *Observations:* `busy_s` on `app-konsole-2:settle` is lower or equal (`1.194 s` in `b388` vs `1.589` / `1.190 s`), while `stretch.idle` (low aggregate vCPU activity during `quiet` wait) is longer (`3.102 s` in `b388` vs `2.476` / `2.109 s`); `:start` improved in all three.
2. **`app-konsole-2:start` and `app-konsole-1:settle` on `-warm` boot 1 (`0c2a` vs `2ecf`):** `1.087 s` vs `1.020 s` (`+6.6%`) and `3.031 s` vs `2.641 s` (`+14.8%`). *Observations:* on `app-konsole-2:start`, `busy_s` was lower (`1.392` vs `1.455 s`) with higher translation request volume (`62,323` vs `24,051`; per-window flush counts are not separated); on `app-konsole-1:settle`, `stretch.idle` grew (`2.290` vs `1.875 s`) and **`busy_s` also rose slightly (`1.581` vs `1.551 vCPU-s`)**, while `app-konsole-1` total improved (`5.000` vs `5.400 s`). Cause remains open.
3. **`app-dolphin-1:start`, `app-dolphin-2:settle`, `boot`, `session_start_s` (`b388` single-pair) and plain `app-dolphin-1:settle` (`1448` vs `c682`):** `b388` vs `84bd` `app-dolphin-1:start` `1.690` vs `1.646 s` (`+2.7%`), `app-dolphin-2:settle` `2.706` vs `2.683 s` (`+0.9%`); `b388` vs `0183` `boot` `6.517` vs `6.482 s` (`session_start_s` `6.11` vs `6.09 s`); and plain `1448` `app-dolphin-1:settle` `2.872` vs `2.706 s` (`+6.1%`, **`+0.166 s` = `2.01×` profiled base SD `0.0827 s`**). *Observations:* `b388` differences lie within base repeat spread (`0183` had `1.973` / `2.857 s`), and `app-dolphin-1` total improved (`5.340` vs `5.570 s`), while plain `app-dolphin-1:settle` is a marginal single-pair loss with an open cause.
4. **Browser `app-chromium-2:settle` & `app-chromium-2` total (`c5cc`), plus individual slower browser rows:** `app-chromium-2:settle` mean `8.805 s` (`c5cc` `11.735 s`, `a9c8` `5.875 s`) vs base mean `7.800 s` (`+12.9%`, base range `0.085 s`); on **`c5cc` alone**, **`app-chromium-2` total was `16.820 s`** vs base `15.570` / `15.950 s` (mean `13.860` vs `15.760 s`), **`app-firefox-2:start` was `5.056 s`** vs base `4.898` / `4.962 s` (mean `4.462` vs `4.930 s`), and **`app-chromium-1:start` was `8.566 s`** vs `fd58`'s `8.531 s`. On `a9c8` alone, `boot` was `7.125 s` (`session_start_s` `6.72 s`) vs `fd58` `6.513 s` (`6.10 s`). *Observations:* `c5cc` `app-chromium-2:settle` had lower `busy_s` (`3.772` vs `4.460` / `4.592 s`) with `9.422 s` in `stretch.idle`, which records low aggregate vCPU activity and cannot identify what delayed settlement; cause remains open.
5. **`app-chromium-1:settle` and `app-chromium-1` total (`desktop-kde-browser-profile-warm` `328d` vs `211a`):** boot 0 `8.145 s` / `17.550 s` vs `6.120 s` / `16.680 s` (`+33.1%` / `+5.2%`); boot 1 `:settle` `8.054 s` vs `6.585 s` (`+22.3%`). *Observations:* `busy_s` was `17–21%` lower (`4.298` / `4.353 s` vs `5.208` / `5.519 s`), while `stretch.idle` grew (`5.785` / `5.717 s` vs `3.387` / `3.767 s`).
6. **`app-firefox-2:start`, `:settle`, and total (`328d` boot 0 vs `211a` boot 0):** `5.197 s` / `23.983 s` / `30.200 s` vs `4.626 s` / `20.974 s` / `26.870 s` (`+12.3%` / `+14.3%` / `+12.4%`). *Observations:* `:settle` had `22%` lower `busy_s` (`29.608` vs `38.049 s`) and `+4.69 s` in `stretch.idle` (`10.868` vs `6.177 s`), **but on `:start`, `busy_s` rose (`9.405` vs `8.834 vCPU-s`) and `stretch.one` rose `+0.618 s` (`2.453` vs `1.835 s`) while `stretch.idle` rose only `0.002 s`** (`translate.host_s` fell `2.164 → 1.847 s`, total requests `474,587` vs `402,472`). Cause of `:start`'s `stretch.one` rise remains open.
7. **UI rows across `driver.sh` `v=1` vs `v=2` (`0c2a` vs `2ecf`), plus `≤ 0.05 s` noise deltas:** On `0c2a` vs `2ecf`, `ui-overview-close`, `ui-desktop-next`, `ui-desktop-prev`, `ui-window-place`, `ui-window-maximize`, `ui-window-restore` (and boot 1 `ui-window-move`, `ui-window-grow`, `ui-window-switch`) read `+0.12` to `+0.50 s` slower where `0c2a` ran L3's `driver.sh` `v=2` (`ecb5717e`, which invokes `get_state` / `DE State` *inside* the `ui()` timing window; `2ecf` ran `v=1`). Its individual cost was not separately measured, though on matched `driver` versions (`0183` vs `e89f` and plain `c682` vs `1448`, both `v=2`; `84bd` vs `b388`, both `v=1`), only `≤ 0.050 s` deltas appear (`ui-overview-close` `+0.020 s`, `ui-desktop-prev` `+0.040 s`, `ui-desktop-next` `+0.020–0.040 s`, `ui-window-place` `+0.050 s`, `ui-window-restore` `+0.020 s`).
8. **Trade-off status for the owner:** All 25 slower warm-pair windows (`0c2a`/`328d` vs `2ecf`/`211a`), all 6 slower plain KDE values (`app-konsole-1:settle`, `app-konsole-1`, `app-konsole-2:settle`, `app-dolphin-1:settle`, `ui-desktop-next`, `ui-desktop-prev`; 0 slower on plain browser), and the slower profile windows/means are named above. Because `stretch.idle` measures low aggregate vCPU activity and cannot identify what delayed settlement, all `:settle` losses (`app-konsole-2:settle` across `b388`, `0c2a-b0`, and plain `1448`; plain `app-konsole-1:settle`; `c5cc` `app-chromium-2:settle`; `328d` boot 0/1 `app-chromium-1:settle` `+2.025 s`/`+1.469 s`; `328d` boot 0 `app-firefox-2:settle` `+3.009 s`; plain `app-dolphin-1:settle` `+2.01×` SD) as well as `328d-b0` `app-firefox-2:start`, `0c2a-b1` `app-konsole-2:start`, and the unisolated `DE State` overhead in `0c2a` vs `2ecf` remain explicitly open under Spec §1's trade-off rule, alongside the known non-desktop `debug/web-tooling-profile` ~4% cost noted for L6 (`kde-desktop-plan.md`).

#### L5.4 Cold-row breakdown (`m1` `perf` and `m1-l5-diag` `e89f` / `a60b`)

**1. Host time by request kind and residual host time (mean M1 `perf` vCPU-s: `b388` for KDE, `c5cc`/`a9c8` for Browser):**
On cold boots (compile cache off), `disk` answers are `0` (`disk_get + disk_put = 0.000–0.010 s` total), and `log_stats` shows `dropped = 0`, `failed = 0` on all runs. Note that `vcpu.host − translate.host_s` is **residual host time** (across the whole `desktop-kde-browser-profile` boot, `perf.host`'s recorded `device + port` buckets total at most `~0.37 s`, so per-row device I/O is unmeasured and the residual includes other host exit work).

| Cold row | Wall (s) | Busy (vCPU-s) | `vcpu.host` (s) | `first_seen` req (`host_s`, `fexc` / `admit`) | `repeat` req (`host_s`, `memory` / `fexc`) | Residual host (`vcpu.host − tr.host`) | `vcpu.pool` + `vcpu.place` (s) |
|---|---:|---:|---:|---|---|---:|---:|
| `session:shell` (KDE) | `22.236` | `63.527` | `11.491` | `568,715` (`7.756 s`, `5.818` / `0.894`) | `585,250` (`1.483 s`, `536k` / `7.9k`) | `2.252 s` (`19.6%`) | `3.556 + 1.304` |
| `session:idle-ready` (KDE) | `24.480` | `69.116` | `12.048` | `587,542` (`8.048 s`, `6.039` / `0.925`) | `638,422` (`1.608 s`, `587k` / `8.1k`) | `2.392 s` (`19.9%`) | `4.022 + 1.380` |
| `app-konsole-1:start` | `1.527` | `2.257` | `0.738` | `34,886` (`0.368 s`, `0.253` / `0.050`) | `81,620` (`0.155 s`, `78.5k` / `180`) | `0.215 s` (`29.1%`) | `0.138 + 0.114` |
| `app-dolphin-1:start` | `1.690` | `2.502` | `0.489` | `20,475` (`0.197 s`, `0.133` / `0.027`) | `67,813` (`0.132 s`, `64.3k` / `96`) | `0.160 s` (`32.7%`) | `0.303 + 0.088` |
| `session:shell` (Browser) | `22.114` | `63.129` | `11.443` | `568,778` (`7.730 s`, `5.833` / `0.882`) | `601,088` (`1.446 s`, `551k` / `9.4k`) | `2.267 s` (`19.8%`) | `3.534 + 1.296` |
| `session:idle-ready` (Browser) | `26.235` | `70.244` | `12.067` | `588,408` (`8.070 s`, `6.095` / `0.916`) | `658,933` (`1.575 s`, `607k` / `9.6k`) | `2.423 s` (`20.1%`) | `3.999 + 1.375` |
| `app-chromium-1:start` | `8.251` | `19.536` | `9.474` | `405,672` (`6.769 s`, `5.068` / `0.740`) | `386,286` (`1.119 s`, `359k` / `12.5k`) | `1.585 s` (`16.7%`) | `1.651 + 0.873` |
| `app-firefox-1:start` | `9.049` | `16.237` | `6.242` | `304,231` (`4.401 s`, `2.862` / `0.517`) | `302,787` (`0.679 s`, `286k` / `3.2k`) | `1.162 s` (`18.6%`) | `1.651 + 0.609` |

**2. D1 execute-rule `peer_live` (`m1-l5-diag`: `e89f` for KDE, `a60b` for Browser):**
`desk` instruments the slow L2-miss path starting at `desk::begin()` after `find()` fails (`execution.rs:356`; fast-path `shared_fast` L2 misses resolve before `desk::begin()` without entering `desk`), whereas a unified-store fast-path hit would also bypass `desk::begin()` (while still paying shared-table lookup/entry costs). `pool+host` sums elapsed ticks (with diagnostic overhead `DIAG_TICKS` deducted) recorded for the `POOL` and `HOST` outcomes (`run_desktop.py:537–544`; `untracked=0` across all rows), and **establishes no positive floor on ordinary-build wall savings**.

| Cold row (`diag`) | `pool+host` req (`s`) | `peer_abs` req (`s`) | `peer_pic` req (`s`) | Confirmed peer (`abs+pic`) share | `peer_unknown` req (`s`) | `peer_unknown` share | `not_peer` req (`s`) | `shared_entries` / `evictions` (evictions per shared entry) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `session:shell` (`e89f`) | `1,985,600` (`15.687 s`) | `350,810` (`1.052 s`) | `108,994` (`0.351 s`) | `23.2%` req / `8.9%` s | `1,511,342` (`14.111 s`) | `76.1%` req / `90.0%` s | `14,454` (`0.173 s`) | `1,666,920` / `1,812,512` (`1.09×`) |
| `session:idle-ready` (`e89f`) | `2,364,350` (`17.523 s`) | `431,954` (`1.329 s`) | `121,743` (`0.398 s`) | `23.4%` req / `9.9%` s | `1,796,161` (`15.624 s`) | `76.0%` req / `89.2%` s | `14,492` (`0.173 s`) | `1,829,639` / `2,187,840` (`1.20×`) |
| `app-konsole-1:start` (`e89f`) | `161,960` (`1.060 s`) | `11,781` (`0.037 s`) | `9,105` (`0.034 s`) | `12.9%` req / `6.7%` s | `141,074` (`0.989 s`) | `87.1%` req / `93.3%` s | `0` (`0.000 s`) | `64,511` / `168,866` (`2.62×`) |
| `app-dolphin-1:start` (`e89f`) | `123,653` (`0.681 s`) | `17,538` (`0.044 s`) | `12,579` (`0.038 s`) | `24.4%` req / `12.0%` s | `93,536` (`0.600 s`) | `75.6%` req / `88.0%` s | `0` (`0.000 s`) | `87,992` / `134,957` (`1.53×`) |
| `session:shell` (`a60b`) | `2,414,687` (`16.830 s`) | `437,871` (`1.312 s`) | `124,162` (`0.426 s`) | `23.3%` req / `10.3%` s | `1,837,129` (`14.914 s`) | `76.1%` req / `88.6%` s | `15,525` (`0.178 s`) | `1,521,944` / `2,282,903` (`1.50×`) |
| `session:idle-ready` (`a60b`) | `2,530,879` (`17.625 s`) | `463,588` (`1.413 s`) | `138,994` (`0.486 s`) | `23.8%` req / `10.8%` s | `1,912,772` (`15.548 s`) | `75.6%` req / `88.2%` s | `15,525` (`0.178 s`) | `1,653,292` / `2,402,578` (`1.45×`) |
| `app-chromium-1:start` (`a60b`) | `1,179,298` (`12.429 s`) | `215,434` (`0.746 s`) | `31,455` (`0.123 s`) | `20.9%` req / `7.0%` s | `931,758` (`11.546 s`) | `79.0%` req / `92.9%` s | `651` (`0.014 s`) | `277,636` / `863,294` (`3.11×`) |
| `app-firefox-1:start` (`a60b`) | `1,165,444` (`8.998 s`) | `312,075` (`0.935 s`) | `40,899` (`0.136 s`) | `30.3%` req / `11.9%` s | `810,753` (`7.902 s`) | `69.6%` req / `87.8%` s | `1,717` (`0.026 s`) | `345,524` / `907,374` (`2.63×`) |

*(In `app-dolphin-1:start` `e89f`, `peer_abs` `pool+host` is `[17,538, 0.0436 s]` = `pool [15,452, 0.0298]` + `host [2,086, 0.0138]`, and `peer_pic` `pool+host` is `[12,579, 0.0381 s]` = `pool [9,317, 0.0186]` + `host [3,262, 0.0195]`, totaling `[30,117, 0.0817 s]`.)*

Why `peer_unknown` is `88–93%` of `pool+host` time: `desk`'s presence table (`PRESENCE_SETS = 65,536 × 4` ways = `262,144` slots) records `135 k–2.40 M` live-eviction events (`1.09×–3.11×` `shared_entries`), setting `lost_vcpus` (`lost_coverage`) in `SET_LOCKS` on affected sets. Once `lost_coverage` is set, any miss in that set without a surviving peer way — as well as unresolved crossing/fenced/x87-blocked requests — is classified `PEER_UNKNOWN` rather than `NOT_PEER`. Those event counts alone do not establish how many sets lost coverage or partition `peer_unknown` between `not_peer` (`first_seen`) and evicted peer hits.

**3. Serial stretches (`stretch.one`) and `snapc` CR3 / TLB refill cost (`b168570a` → `m1`):**
On `m1`, P1's guards and checked links reduced `tlb_s` by `51–95%` (`-51.2%` on Konsole-1 to `-95.4%` on Firefox-1) and eliminated `suspended` on Dolphin-1 (`0` `suspended`, `360` `relinked` on `b388` `:start`) and both `suspended`/`relinked` on Firefox-1 (`0`/`0`). Remaining `tlb + relink` is small, while `stretch.one` also contains `0.30–1.03` vCPU-s of `refill` work whose causal link to switches is unmeasured:

| Cold row | `stretch.one` wall (`host` / `sw_s` / `tlb_s` / `refill`, M1) | `stretch.many` wall (conc.) | Total `tlb_s`: base → M1 | Total `sw_s`: base → M1 | Remaining `tlb+relink` (`vCPU-s`, `one+many/conc`) | Remaining `sw_s` breakdown (`link` / `abort` / `shadow`) |
|---|---:|---:|---:|---:|---:|---|
| `session:shell` (KDE) | `4.776 s` (`2.548` / `0.460` / `0.009` / `0.696`) | `17.350 s` (`3.39×`) | `0.713 → 0.159` (`-77.7%`) | `13.690 → 8.621` | `0.430 s` (`0.150 s`) | `3.186` / `1.868` / `2.971` |
| `session:idle-ready` (KDE) | `5.257 s` (`2.610` / `0.526` / `0.010` / `0.909`) | `18.934 s` (`3.38×`) | `0.800 → 0.166` (`-79.2%`) | `15.479 → 9.436` | `0.462 s` (`0.161 s`) | `3.457` / `2.055` / `3.284` |
| `app-konsole-1:start` | `0.938 s` (`0.273` / `0.110` / `0.003` / `0.301`) | `0.596 s` (`2.19×`) | `0.020 → 0.010` (`-51.2%`) | `0.403 → 0.324` | `0.026 s` (`0.015 s`) | `0.178` / `0.057` / `0.059` |
| `app-dolphin-1:start` | `1.136 s` (`0.261` / `0.207` / `0.001` / `0.331`) | `0.560 s` (`2.38×`) | `0.018 → 0.004` (`-77.1%`) | `0.405 → 0.411` | `0.006 s` (`0.003 s`) | `0.275` / `0.055` / `0.071` |
| `session:shell` (Browser) | `4.810 s` (`2.581` / `0.459` / `0.009` / `0.762`) | `17.201 s` (`3.40×`) | `0.657 → 0.169` (`-74.3%`) | `13.758 → 8.649` | `0.456 s` (`0.159 s`) | `3.145` / `1.879` / `3.012` |
| `session:idle-ready` (Browser) | `5.535 s` (`2.672` / `0.568` / `0.011` / `1.032`) | `19.087 s` (`3.39×`) | `0.749 → 0.178` (`-76.2%`) | `15.816 → 9.622` | `0.489 s` (`0.172 s`) | `3.462` / `2.106` / `3.394` |
| `app-chromium-1:start` | `2.850 s` (`1.517` / `0.244` / `0.001` / `0.597`) | `5.374 s` (`3.12×`) | `0.389 → 0.021` (`-94.5%`) | `3.528 → 2.436` | `0.062 s` (`0.023 s`) | `1.566` / `0.355` / `0.419` |
| `app-firefox-1:start` | `4.136 s` (`2.143` / `0.390` / `0.001` / `0.732`) | `4.893 s` (`2.48×`) | `0.318 → 0.014` (`-95.4%`) | `3.302 → 2.131` | `0.014 s` (`0.006 s`) | `1.326` / `0.359` / `0.401` |

#### L5.5 Candidate investment wall-time scenarios, overlap, and separability verdict

The figures below are **allocation scenarios**, not strict wall-time bounds: removing off-critical-path work saves `0 s` wall time, while the serial-first endpoint of each scenario places as much removed work into `stretch.one` (up to `one_host` for host-only work, or `one_host + one_refill` when `pool+place` are included) as fits before diluting the rest by `many_conc`:

- **(a) Unified code store (D1):**
  - *Confirmed-peer scenario:* `peer_abs + peer_pic` `pool+host` (`0.071–1.899` vCPU-s of `POOL`+`HOST` post-`find` miss time), mapped from `[work / conc, serial_first(work, one_host + one_refill)]`. Because an eligible shared hit still incurs lookup/entry costs and `desk` does not measure pre-miss L2 lookup, this establishes no positive ordinary-build floor.
  - *Non-`first_seen` `pool+host+place` scenario:* `Requests` globally deduplicates `first_seen` by guest-bytes hash across the boot (which does not rule out a shorter peer block at the same RIP/frame, though `first_seen` is `100%` `fexc`). Assigning `place` by `repeat` request share gives `repeat_host_s + vcpu.pool + repeat_share × vcpu.place` (`0.37–6.35` vCPU-s), mapped from `[work / conc, serial_first(work, one_host + one_refill)]` (and also shown capped at `one_host` alone in parens for Chromium: `2.05 s`), *before* subtracting D3's S2 checked-link trade-off (shared blocks losing guarded cross-page links; Step 2's A/B saw guards save `6.4 s` on `session:idle-ready`).
- **(b) Bounded first-seen translation change:**
  - `first_seen_host_s` is `0.197–8.070` vCPU-s (`40–71%` of `vcpu.host`, dominated by `fexc` + `admit`). Under a `25–50%` cut, **parallel-first** gives `[0.25 × fs / conc, 0.50 × fs / conc]` and **serial-first** gives `[serial_first(0.25 × fs, one_host), serial_first(0.50 × fs, one_host)]` (with `100%` of `first_seen_host_s` as `[fs / conc, serial_first(fs, one_host)]`).
- **(c) Process-switch / CR3 work:**
  - Direct `tlb + relink` (`one + many / conc`) is `0.003–0.172 s` wall (bundled M1 does not isolate guards alone). Counting 100% of `sw_s` (`one_sw + many_sw / conc`) gives `0.21–3.23 s`, dominated by `link` (checked-link probes/patching) and `shadow`+`abort`, while `0.30–1.03` vCPU-s of `refill` inside `stretch.one` has unmeasured causal coupling to switches.

| Cold row | (a) Store (D1) (`abs+pic` post-miss; non-`first_seen` `pool+host+place`) | (b) First-seen `25–50%` (parallel-first; serial-first; `100%` ref) | (c) Switch (`tlb+relink`; all `sw_s` ref) | Intersecting (a) vs (b) intervals |
|---|---|---|---|---|
| `session:shell` (KDE) | `0.41–1.40 s`; `1.68–3.97 s` | `0.57–1.14 s`; `1.94–2.94 s` (`2.29–4.08 s`) | `0.150 s` (`2.86 s`) | `abs+pic` ∩ par; non-`fs` ∩ serial |
| `session:idle-ready` (KDE) | `0.51–1.73 s`; `1.88–4.36 s` | `0.60–1.19 s`; `2.01–3.03 s` (`2.38–4.22 s`) | `0.161 s` (`3.16 s`) | `abs+pic` ∩ par; non-`fs` ∩ serial |
| `app-konsole-1:start` | `0.03–0.07 s`; `0.17–0.37 s` | `0.04–0.08 s`; `0.09–0.18 s` (`0.17–0.32 s`) | `0.015 s` (`0.21 s`) | `abs+pic` ∩ par; non-`fs` ∩ serial |
| `app-dolphin-1:start` | `0.03–0.08 s`; `0.21–0.50 s` | `0.02–0.04 s`; `0.05–0.10 s` (`0.08–0.20 s`) | `0.003 s` (`0.29 s`) | `abs+pic` ∩ par & serial; non-`fs` store higher |
| `session:shell` (Browser) | `0.51–1.74 s`; `1.66–4.02 s` | `0.57–1.14 s`; `1.93–2.96 s` (`2.28–4.10 s`) | `0.159 s` (`2.87 s`) | `abs+pic` ∩ par; non-`fs` ∩ serial |
| `session:idle-ready` (Browser) | `0.56–1.90 s`; `1.86–4.47 s` | `0.59–1.19 s`; `2.02–3.07 s` (`2.38–4.26 s`) | `0.172 s` (`3.23 s`) | `abs+pic` ∩ par; non-`fs` ∩ serial |
| `app-chromium-1:start` | `0.28–0.87 s`; `1.02–2.46 s` (`2.05 s` `one_h`) | `0.54–1.08 s`; `1.57–2.11 s` (`2.17–3.20 s`) | `0.023 s` (`0.95 s`) | `abs+pic` ∩ par (`abs+pic` disjoint below serial); non-`fs` ∩ both |
| `app-firefox-1:start` | `0.43–1.07 s`; `1.06–2.63 s` | `0.44–0.89 s`; `1.10–2.17 s` (`1.78–3.05 s`) | `0.006 s` (`1.09 s`) | `abs+pic` ∩ par; non-`fs` ∩ serial |

**Separability verdict (Plan L5 rule):**
1. Direct `tlb + relink` CR3-flush cost on M1 is small (`0.003–0.172 s` wall across all eight rows), supporting deprioritizing direct flush/relink work, while broader switch/refill work (`link` probes, `shadow`/`abort`, and `0.30–1.03` vCPU-s of `stretch.one` `refill`) remains unranked/unattributed without targeted measurement.
2. **The scenarios do NOT cleanly separate (a) Unified code store (D1) from (b) Bounded first-seen translation change.** On Chromium cold start, `abs+pic` (`0.28–0.87 s`) lies below the serial-first `25–50%` first-seen interval (`1.57–2.11 s`), yet intersects the parallel-first interval (`0.54–1.08 s`), and the non-`first_seen` store scenario (`1.02–2.46 s`, or `2.05 s` under `one_host` cap) overlaps both; `peer_unknown` (`87.8–93.3%` of `pool+host` vCPU-s) also cannot be partitioned into eligible peer hits from eviction counts alone (and D3's S2 checked-link trade-off remains an unmeasured offset).
3. **Consequence per Plan L5:** M1 misses Spec 3.1's cold-start bar, and these scenarios cannot separate the leading candidates on their own. Under `kde-desktop-plan.md` L5 and `kde-desktop-spec.md` §4.1, a **bounded A/B prototype** on one cold row (e.g., `app-chromium-1:start`) settles the store-vs-first-seen pairwise choice before M2 starts (with broader switch work kept explicit for the owner's disposition) unless the owner decides directly on the record above.

### L6 Standing set and regression attribution

Measured on Mac mini M4 (November 27, 2026) at `m1`'s tip `ecb5717e` (which builds the exact `91fe2a1b` host and runtime; `git diff 91fe2a1b..ecb5717e` touches only desktop tests and this file). Compared against same-day `be88e684` controls (`20261127-064830-6a7d` for 10 × 1-vCPU cases, `20261127-065252-296c` for 7 × 4-vCPU cases, `20261127-065456-379e` for `speedometer-natemu-warm`, and `20261127-064515-bbc6` for diagnostic `debug/web-tooling-profile`), and against the stored November 25 M4 runs in `basecap-ab`: `20261125-214847-89b0` (`535ca1e9`, which shares identical runtime/host sources with `be88e684` — `be88e684` only added `natemu-viewer` — though `glmark2` in `de27` ran against a rebuilt initramfs with `userland_sha256` `98e93b19...` vs `89b0`'s `1af94d90...`), and the earlier `c3f074b7` runs (`20261125-203014-0575`, `-203913-dce6`, `-204804-7ce8` for 3 × 1-vCPU boots; `20261125-205656-b78b`, `-205843-0406`, `-210031-5be0` for 3 × 4-vCPU boots; `20261125-210218-e677` for `io5-solo32`, `guest-paper`, `display-fill`, `compat-tls-natemu`, `mixed-far-natemu`). Note that `c3f074b7` predates the gather execution, host admission, and FEX (`22cd3b5`) commits that landed in `535ca1e9` / `be88e684`, so `c3f074b7` is used here for historical 3-boot sample SDs and for cases not re-run on `be88e684`/`535ca1e9`. All `ecb5717e` runs: `20261127-055730-9d89` (28 × 1-vCPU cases, 1 boot), `20261127-060736-c274` (12 × 4-vCPU `guest-*` + `io5-solo32`, 1 boot), `20261127-061630-de27` (`guest-paper` 3 boots, `glmark2-natemu*` 5 boots), `20261127-063038-da63` (`speedometer-natemu*` 3 boots), and `20261127-064224-026b` (`debug/web-tooling-profile`).

#### L6.1 Moved standing-set rows, unresolved single-boot deltas, and diagnostic Web Tooling

Where a `guest-*` SD multiple is shown below, its numerator is the **same-day `be88e684` delta** (`ecb5717e − be88e684`) divided by the sample SD of the three `c3f074b7` boots (for 1v `guest-mem`, `15.9×` if using `c3f074b7`'s median `7.399 s` instead of `6a7d`'s `7.735 s`).

| Case (vCPUs) | Baseline (`be88e684` / `535ca1e9` / `c3f074b7` / `benchmarks.md`) | `ecb5717e` (run ID) | Delta vs control (noise / spread) |
|---|---:|---:|---|
| `io5-solo32` (4v, s) | i386 `56.8` / `104.0`, x64 `54.4` / `85.7` (`e677`, `c3f074b7`; page `58.3` / `107.1`, `53.1` / `88.6`) | **`45.0` / `100.2`**, **`43.3` / `81.1`** (`c274`) | `-20.8%` / `-3.7%`, `-20.4%` / `-5.4%` vs `e677` (`-22.8%` / `-6.4%`, `-18.5%` / `-8.5%` vs `9f35eb96`) |
| `guest-mem` (1v, s) | `7.735` (`6a7d`, `be88e684`); `c3f0` `7.363–7.475` (med `7.399`, SD `0.057`; page `7.417`) | **`6.489`** (`9d89`) | **`-16.1%`** vs `6a7d` (`21.8×` SD; `-12.3%` / `15.9×` SD vs `c3f0`) |
| `guest-mem` (4v, s) | `2.518` (`296c`, `be88e684`); `c3f0` `2.489–2.525` (med `2.522`, SD `0.020`; page `2.487`) | **`2.226`** (`c274`) | **`-11.6%`** vs `296c` (`14.6×` SD) |
| `speedometer-natemu` (4v, score) | `3.10` (`89b0`, `535ca1e9`: `3.035–3.129`, med `3.101`, `geomean_ms` `330.95`) | **`3.33`** (`da63`: `3.273–3.349`, med `3.327`, `geomean_ms` `307.44`) | **`+7.3%`** (above both spreads) |
| `speedometer-natemu-warm` (4v, score) | `3.10` (`379e`, `be88e684`: `3.016–3.100`, med `3.095`, `geomean_ms` `329.60`; page `3.12`) | **`3.33`** (`da63`: `3.288–3.392`, med `3.332`, `geomean_ms` `308.31`) | **`+7.6%`** vs `379e` (`+6.8%` vs page, above both spreads) |
| `guest-locks` (1v, s) | `3.421` (`6a7d`, `be88e684`); `c3f0` `3.364–3.391` (SD `0.014`; page `3.357`) | **`3.639`** (`9d89`) | **`+6.4%`** vs `6a7d` (`15.7×` SD; `+8.4%` vs page) |
| `guest-locks` (4v, s) | `5.235` (`296c`, `be88e684`); `c3f0` `5.720–5.977` (SD `0.135`; page `5.732`) | **`7.308`** (`c274`) | **`+39.6%`** vs `296c` (`15.4×` SD; `+27.5%` vs page) |
| `guest-sqlite` (1v, s) | `5.630` (`6a7d`, `be88e684`); `c3f0` `5.515–5.539` (SD `0.012`; page `5.465`) | **`6.095`** (`9d89`) | **`+8.3%`** vs `6a7d` (`38.7×` SD; `+11.5%` vs page) |
| `guest-sqlite` (4v, s) | `5.646` (`296c`, `be88e684`); `c3f0` `5.561–5.572` (SD `0.006`; page `5.564`) | **`6.147`** (`c274`) | **`+8.9%`** vs `296c` (`85.5×` SD; `+10.5%` vs page) |
| `guest-sqlite32` (1v, s) | `7.471` (`6a7d`, `be88e684`); `c3f0` `7.236–7.279` (SD `0.024`; page `7.264`) | **`7.928`** (`9d89`) | **`+6.1%`** vs `6a7d` (`18.8×` SD; `+9.1%` vs page) |
| `guest-luajit-interp` (1v, s) | `3.464` (`6a7d`, `be88e684`); `c3f0` `3.395–3.438` (SD `0.024`; page `3.429`) | **`3.688`** (`9d89`) | **`+6.5%`** vs `6a7d` (`9.4×` SD; `+7.6%` vs page) |
| `guest-startup32` (1v, s) | `6.796` (`6a7d`, `be88e684`); `c3f0` `6.592–6.599` (SD `0.004`; page `6.617`) | **`7.055`** (`9d89`) | **`+3.8%`** vs `6a7d` (`73.7×` SD; `+6.6%` vs page) |
| `guest-paper` (4v, s) | 1st `20.88` (`20.77–21.20`), 2nd `20.14` (`19.82–20.36`, `e677`, `c3f074b7`; Done `18.65` / `18.27`) | **`21.65`** (`21.32–21.99`), **`21.35`** (`20.77–21.40`, `de27`; Done `19.40` / `19.54`) | **`+3.7%`** (1st) / **`+6.0%`** (2nd), outside `e677` spreads (no `be88e684` control) |
| `glmark2-natemu` (4v, score) | `546` (`89b0`, `535ca1e9`: `544–566`, spread `22`; `userland_sha256` `1af94d90`; page `554`) | **`527`** (`de27`: `518–556`, `userland_sha256` `98e93b19`) | **`-3.5%`** vs `89b0` (`-19` pts < `22`-pt spread; distributions overlap; input hash differs) |
| `glmark2-natemu-sse41` (4v, score) | `861` (`89b0`, `535ca1e9`: `850–880`, spread `30`; `userland_sha256` `1af94d90`; page `858`) | **`833`** (`de27`: `823–853`, `userland_sha256` `98e93b19`) | **`-3.3%`** vs `89b0` (`-28` pts < `30`-pt spread; distributions overlap; input hash differs) |
| `debug/web-tooling-profile` (4v, diagnostic) | `8.97` runs/s (`bbc6`, `be88e684`, boot `108.39 s`) | **`8.51`** runs/s (`026b`, boot `111.23 s`) | **`-5.1%`**: `tlb` `3.251 → 0.060 s` (`-98.1%`; `k1.tlb_us` `3,249,524 → 59,418 us`, `89,204 → 1,619 ns/flush`, `-98.2%`), `relink` `5.366 → 0.800 s` (`5,504,948 → 1,031,760` events), `abort` `9.182 → 3.013 s`, `link` `5.176 → 2.435 s`; `guest` `+10.368 s` (`123.790 → 134.158 s`, `+8.4%`) |

Remaining standing-set cases fall into two distinct groups (do not treat the second group as confirmed "within noise"):
- **Within `be88e684` or `c3f074b7` spread or `< 2×` SD:** `guest-hackbench` (1v `11.728` vs `6a7d` `11.458 s`, `+2.4%` / `1.7×` SD; 4v `3.107` vs `296c` `3.118 s`, `-0.4%`), `guest-7zip` (1v `3.760` vs `6a7d` `3.720 s`, `+1.1%` / `1.8×` SD), `guest-luajit-interp` 4v (`1.068 s` inside `c3f0` `1.025–1.072 s`), `guest-cray32-small` (`0.675` inside `c3f0` `0.591–0.683 s`), `guest-cray32-strict-small` (`0.591` inside `c3f0` `0.574–0.643 s`), `guest-scimark32` (`fft` `1093.5` vs `1023.1`, `sor` `1548.0` vs `1556.1`, `montecarlo` `289.0` vs `288.0`, `sparse` `1168.2` vs `1171.2`, `lu` `1906.8` vs `1884.8`), and `display-fill` (`vram_s 0.0264` vs `e677` `0.0258 s`, `ram_s 0.0262` vs `0.0266 s`, `snapshot 4.75` vs `4.75 ms`).
- **Unresolved single-boot / single-run shifts (outside `c3f074b7`/`e677` spread without a `be88e684` control, or differing across `296c` vs `c3f074b7`):** `guest-cray-strict` 4v (`1.195 s` vs `296c` `1.155 s`, `+3.5%`, `8.9×` `c3f0` SD, above `c3f0` `1.076–1.085 s`), `guest-7zip` 4v (`1.683 s` vs `296c` `1.745 s` `-3.6%`, vs `c3f0` `1.598–1.629 s`), `guest-cray` 4v (`1.109 s` vs `296c` `1.182 s` `-6.2%`, vs `c3f0` `1.012–1.065 s`), `guest-avx` 4v (`3.136 s` vs `c3f0` `3.261–3.273 s`, `-4.0%`, `10.8×` `c3f0` spread / `21.6×` SD; `3b13c1d0` `3.245 s`) and 1v (`7.913 s` vs `c3f0` `8.069–8.146 s`, `-2.8%`), `guest-hackbench32-int80` (`26.592 s` vs `c3f0` `25.868–26.090 s`, `+2.8%`, `5.7×` SD; if updated, QEMU ratio moves `0.95× → 0.92×`), `guest-cray32-sse2-small` (`0.291` vs `c3f0` `0.298–0.312 s`), `guest-scimark` (`montecarlo` `754.2` vs `6a7d` `833.2`, `-9.5%` in 1 boot; `fft` `2128.8` vs `6a7d` `3022.3` inside 1500–3200 Mflops FFT variance, `sor` `3814.1` vs `3856.4`, `lu` `6574.9` vs `6506.5`), `guest-scimark32-sse2` (`sor` `3737.1` vs `c3f0` `3801.5–3937.6` `-4.5%`, `montecarlo` `250.26` vs `255.80–256.59` `-2.2%`, `sparse` `2830.6` vs `2868.4–2887.2` `-1.4%`, `lu` `6602.6` vs `6663.3–6701.2` `-1.0%`), `mixed-far-natemu` (`1,850.8 ns`, samples `1,847.7–1,872.2` vs `e677` `1,801.9–1,812.7`, `+2.4%`, disjoint 3-sample spreads), `compat-tls-natemu` (`+1.2%` to `+2.8%` across all 7 variants vs `e677`), and `1.2–3.6%` single-boot shifts on `guest-build` (1v `7.016` vs `c3f0` `7.137–7.239`, `-2.3%`; 4v `3.724` vs `c3f0` `3.584–3.660`, `+3.6%`, matching `3b13` `3.732`), `guest-zstd` 1v (`2.707` vs `2.638–2.663`, `+2.6%`), `guest-luajit` 1v (`3.010` vs `2.959–2.986`, `+1.3%`), `guest-cray` 1v (`3.177` vs `3.032–3.106`, `+3.1%`), `guest-cray-strict` 1v (`3.340` vs `3.270–3.295`, `+1.8%`), `guest-hackbench32` (`11.379` vs `10.799–11.124`, `+3.4%`), and `guest-cray32` / `-strict` / `-sse2` (`+1.2%` to `+1.8%`). These remain at `3b13c1d0` / `670fab60` in `docs/benchmarks.md` until a 3-boot / `be88e684` check settles whether they moved.

#### L6.2 Toggle-sweep attribution (`ecb5717e` vs `be88e684`)

In `crates/runtime/src/cache.rs:2765-2780` and `crates/host/src/execution.rs:4910-4937, 4993-5008`:
- `NATEMU_GUARDS=off` sets `Control::guards_off = 1` (no stderr banner), which sets `guarded = false` so non-local transfers into non-global blocks fall through to `checked = true` (`cross_checked` defaults to `target.flags & GLOBAL == 0`). (Non-local links from shared code or into `INSTANCE | TRANSLATION` blocks stay checked even with both switches set.)
- `NATEMU_LINK_FORM=plain` (logs `natemu: NATEMU_LINK_FORM set: link forms changed (debug key)` — identical stderr text for `plain` and `checked`) sets `link_form_plain = 1` (`cross_checked = false`), which does **not** bypass `guarded` while guards are on.
- `NATEMU_FAULT_AROUND=off` disables the shadow fault-around window (no stderr banner; `NATEMU_WRITABLE_ON_READ` has a separate control).
- Because the run records in `9b1d6479`'s toggle sweep only log whether `NATEMU_LINK_FORM` was non-empty (not whether it was `plain` vs `checked`, nor `NATEMU_GUARDS` or `NATEMU_FAULT_AROUND`), the per-run toggle labels below are **inferred** from run order, the stderr notice, and the code rules above (and `cba1` cannot distinguish `NATEMU_FAULT_AROUND=off` from an M1 default repeat on `locks`/`sqlite` alone):

| Run (`short_id`) | vCPUs | `NATEMU_LINK_FORM` stderr? | `guest-locks` (`wall_s` / `wall2_s`; boot `SYSCALL` `0x23`) | `guest-sqlite` (`wall_s` / `wall2_s`) | Other cases (`wall_s` / `wall2_s`) | Inferred toggle state (unlogged env) & effect |
|---|---:|:---:|---|---|---|----|
| `296c` (`be88e684`) | 4 | No | `5.235` / `5.477` (`47,738,725`) | `5.646` / `5.474` | — | `main` baseline |
| `c274` (`ecb5717e`) | 4 | No | `7.308` / `7.285` (`66,733,910`) | `6.147` / `5.930` | — | M1 defaults (guards + checked + P3) |
| `cb1c` | 4 | No | `7.207` / `5.789` (`58,576,604`) | `6.149` / `6.004` | — | Inferred `NATEMU_GUARDS=off` alone: falls back to checked links |
| `30f1` | 4 | No | `6.688` / `6.286` (`58,778,930`) | `6.302` / `6.049` | — | Inferred `NATEMU_GUARDS=off` alone (checked links; contention spread) |
| `3204` | 4 | Yes | `7.272` / `7.298` (`66,801,768`) | `6.154` / `5.918` | — | Inferred `NATEMU_LINK_FORM=plain` (or `checked`) with guards on: guard stubs stay active |
| `bbb1` | 4 | Yes | **`5.597` / `5.553` (`49,845,807`)** | **`5.653` / `5.424`** | — | **Inferred `NATEMU_GUARDS=off NATEMU_LINK_FORM=plain`**: `sqlite` recovers (`5.653` vs `5.646`); `locks` recovers `82.5%` of `c274`'s gap (`5.597 s`, still `+6.9%` above `296c`, though below `c3f0` `5.720–5.977 s`) |
| `cba1` | 4 | No | `7.163` / `7.121` (`64,930,546`) | `6.137` / `5.986` | — | Inferred `NATEMU_FAULT_AROUND=off` or M1 default repeat: regression persists |
| `6a7d` (`be88e684`) | 1 | No | `3.421` / `3.353` (`6,251,126`) | `5.630` / `5.450` | `luajit-interp` `3.464` / `3.399`; `mem` `7.735` / `7.519` | `main` baseline |
| `9d89` (`ecb5717e`) | 1 | No | `3.639` / `3.560` (`6,251,126`) | `6.095` / `5.871` | `luajit-interp` `3.688` / `3.546`; `mem` `6.489` / `6.290` | M1 defaults |
| `a2fc` | 1 | No | `3.596` / `3.590` (`6,251,126`) | `6.170` / `5.977` | `luajit-interp` **`3.434`** / `3.414`; `mem` `6.423` / `6.242` | Inferred `NATEMU_GUARDS=off` alone: `luajit-interp` recovers; `locks`/`sqlite` stay on checked links |
| `46a1` | 1 | Yes | **`3.369` / `3.355` (`6,251,126`)** | **`5.574` / `5.400`** | `luajit-interp` **`3.369`** / `3.322`; `mem` **`6.505`** / `6.270` | **Inferred `NATEMU_GUARDS=off NATEMU_LINK_FORM=plain`**: 1v `locks`/`sqlite`/`luajit-interp` match `6a7d`, `mem` gain kept |
| `f50a` | 1 | No | `3.655` / `3.610` (`6,251,126`) | `6.131` / `5.917` | `luajit-interp` `3.753` / `3.654`; `mem` **`7.714`** / `7.504` | Inferred `NATEMU_FAULT_AROUND=off`: `mem` reverts to `6a7d`, compute regressions remain |

#### L6.3 Named M1 trade-offs for the owner (Spec §1)

The standing-set and diagnostic regressions — `guest-locks` (`+6.4%` at 1 vCPU, `+39.6%` contended at 4 vCPUs), `guest-sqlite` (`+8.3%` at 1 vCPU, `+8.9%` at 4 vCPUs), `guest-sqlite32` (`+6.1%`), `guest-luajit-interp` (`+6.5%` at 1 vCPU), `guest-startup32` (`+3.8%`), `guest-paper` (`+3.7%` first start, `+6.0%` second start), `glmark2-natemu` (`-3.5%`, overlapping spreads and rebuilt `userland_sha256` `98e93b19` vs `89b0`'s `1af94d90`, cause unresolved) and `glmark2-natemu-sse41` (`-3.3%`, overlapping spreads and rebuilt userland, cause unresolved), plus diagnostic `debug/web-tooling-profile` (`-5.1%`, `8.97 → 8.51` runs/s) — are presented for the owner's disposition against Spec §1's acceptance rule ("small, buy something larger, and cannot be fixed"):

1. **Inferred toggle attribution vs un-isolated causes:** Under the inferred toggle states above, `guest-locks` (1v, and 82.5% of the 4v gap — though `+6.9%` remains above `296c` in `bbb1`, and whole-boot `0x23` `SYSCALL` counts of `47.74M` in `296c` vs `58.58–66.80M` under M1 defaults/`NATEMU_GUARDS=off`/`NATEMU_LINK_FORM`, or `49.85–66.80M` across the full sweep including `bbb1`, do not directly measure futex hand-off latency), `guest-sqlite` (1v and 4v), and `guest-luajit-interp` (1v) align with P1 Step 1c/1d and Step 2 cross-page guard stubs and checked links (`guarded` stubs and `checked` L1 probes in EL0). In the 1-vCPU sweep (`f50a`), `guest-mem` reverts when P3 fault-around is inferred off (`6.489 → 7.714 s`), whereas 4-vCPU `guest-mem` (`-11.6%`) was not toggled and is only consistent with P3 rather than isolated at 4 vCPUs. The sweep did **not** test `guest-sqlite32`, `guest-startup32`, `guest-paper`, `glmark2-natemu*`, `debug/web-tooling-profile`, `io5-solo32`, or Speedometer, so their individual causes remain un-isolated by the sweep (on `debug/web-tooling-profile`, the `guest` bucket's `+10.37 s` / `+8.4%` rise matches EL0 stub/probe overhead).
2. **What M1 as a whole buys:** `-51.2%` to `-95.4%` `tlb_s` across the eight cold desktop rows in L5 (`-98.1%` total `tlb` bucket / `-98.2%` per flush on `debug/web-tooling-profile`), `-19%` desktop `idle_desktop_s` (`30.73 → 24.85 s` / `31.03 → 24.88 s`), `-40%` warm Chromium start (`6.261 → 3.754 s` plain, `6.804 → 4.110 s` `perf`), `+31–36%` desktop Speedometer (`2.36 → 3.10` `perf`, `2.43 → 3.31` plain), `+7.3–7.6%` standalone Speedometer (`3.10 → 3.33` cold with disk cache off and `3.10 → 3.33` warm after a cache-fill boot), `-20%` `io5-solo32` configure (`56.8 → 45.0 s` i386, `54.4 → 43.3 s` 64-bit), and `-11.6%` to `-16.1%` `guest-mem`.
3. **Why it remains in M1's as-built base (and open question for the owner):** `NATEMU_GUARDS=off` alone falls back to checked links (`cache.rs:2765-2780`), and disabling both (`NATEMU_GUARDS=off NATEMU_LINK_FORM=plain`) was not measured on the desktop or Speedometer (Step 2's earlier `6.4 s` `session:idle-ready` gain toggled guards alone, not both switches). Note that `guest-locks` 4v (`+39.6%`) is not small under Spec §1, and `sqlite32`/`startup32`/`paper`/`glmark2` lack per-switch isolation; low-CR3-rate plain links (section 11, line 848 above) remain a separate possible mitigation if the owner wants these regressions reduced before or outside M2.






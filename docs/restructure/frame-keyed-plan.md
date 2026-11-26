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

**Decision: step 2 stays on by default.** It is a clear gain on `guest-build` and on the desktop's session start, and
neutral elsewhere within noise. The two policies below are not needed for `guest-build`. A Chromium cold start
0.6 s slower is the one reading against it: repeat it at the next checkpoint before acting on it.

**If the guards had recovered `guest-build` only in part,** the policies to try (both cheap, both behind the existing keys):
- **Plain links for blocks of a low CR3 rate:** count each address space's CR3 loads, and link plainly into blocks whose
  space is switched to rarely. They are suspended and relinked at most a few times.
- **Direct links only where the code TLB earns them:** keep a link checked until its target page has been filled in the
  code TLB in N epochs, then make it guarded.


# One code copy for all vCPUs (P2): design

Status: design, not built (November 26, 2026), branch `shared-code` from `main` at `be88e684`; accepted with fixes by an
independent review (section 13; six blocking findings, all adopted in the text). It is option (c) of the
[cross-vCPU reuse plan](cross-vcpu-reuse-plan.md) ("one code area shared by all vCPUs: a redesign, not a step"), taken up
now with P1 and P3. Line numbers are `main`'s at `be88e684`. Inputs: `crates/runtime/src/cache.rs` (`link` 2370, `arm` 2245,
`disarm` 2288, `written` 2947, `rechecked` 2962, `invalidate` 3003, `mark` 3577, `drain` 3850),
`crates/runtime/src/execution.rs` (`kept` 631, `mark` 1892, `flush` 1973, `interrupted` 2581, `stopped` 2718, the I/O window
2047-2130), `crates/abi/src/execution.rs` (the `cache` areas, 281-600), and
[translated-code-layout.md](translated-code-layout.md). Every number marked *estimate* shows its arithmetic; the evidence of
sections 1 and 11 is experiments' (the Konsole profile; the counting boot on the M4, scratch branch `desktop-counting` at
`8d2ed580`), not bench cases', and chunk 8's A/B is what measures P2.

Reconciled on `m1` from `mmio-flush-return` (`adf92e20`), `shared-code-regions` (`13cd1ae1`), `shared-tail` (`73a8b354`),
and `tail-owned-recovery` (`e366e24f`). Where this background design doc differs from the M1–M3 architecture, barriers,
or step fixes in [kde-desktop-spec.md](kde-desktop-spec.md) and [kde-desktop-plan.md](kde-desktop-plan.md), those two
documents govern.

**P2 depends on one thing of P1: the exact frame-keyed L2** (P1's first step; section 2). It does not need P1's code TLB,
guarded entries or persistent cross-page links: the counting boot found revalidation and instance misses small (0.09-0.11
vCPU-s), and section 2 says how P2 stands without them and what it gains when they land. P3 (shadow fault-around) does
not touch this plan.

## Recommendation in one paragraph

Put every translated block, its record and the exact L2 in one area that every vCPU maps at the same address, beside a
private head (dispatcher, probes, step slot, a new tail slot) that stays each vCPU's own. A vCPU places blocks into chunks it
takes from the area and publishes each with one compare-and-swap into the shared L2; a peer that looks the block up runs
the same code, with the links already made. Shared code is patched only in the forms Arm allows to change under a running
core (`b`/`nop`/`svc` words), a stop no longer patches code at all (the interrupted block's tail runs from a private copy
whose exits stop), a guest write kills a page's blocks once for everyone by a per-page generation and cuts the links into
them, and nothing is freed except with every vCPU stopped at a block boundary. Edges inside a guest page are the same
for every vCPU and process, so a link made there by one vCPU's linker serves all; only transfers between pages go
through per-vCPU state. Linking stays lazy (section 4a). What a vCPU still owns is what the
hardware gives each CPU: its TLB (L1, and P1's code TLB when it lands), its dispatcher, its pending stop. Expected
(section 11): of warm Konsole's 0.55 vCPU-s in pool and host requests, the 0.33 spent on blocks a peer holds live
(experiment, counting boot), less about a tenth for the adoptions, and the linking of those blocks; fewer capacity flushes;
and one copy of the code instead of one per vCPU.

## 1. What goes wrong today

A vCPU's cache is its own: code area, records, L2, links (`cache.rs` 1-14: "Blocks and L1 are this vCPU's own (its code
area is backed by its own memory), so a patch stops no other vCPU"). Code that moves to another vCPU (a thread migrates, a
second thread of the same program, the kernel on every CPU) is fetched again there: found in the pool or asked of the
host, admitted or re-checked, copied, placed, split into hot and cold, and linked point by point as its exits are first
taken. The brief's profile of a warm Konsole start: in a 0.95 s stretch on one vCPU, `pool` 0.188, `place` 0.035, `link`
0.142 and `patch` 0.026 vCPU-s, 0.391 of 0.95 (41%), and 26-53% of the EL0 samples were of code another vCPU had run less
than a second before. The pool (cross-vCPU reuse plan) removed the host round trip from such a miss; the copy, the
placement and all the linking remain, once per vCPU and again after each vCPU's own capacity flush.

The counting boot (experiment: Mac mini M4, scratch branch `desktop-counting` at `8d2ed580`, runs `c244` at 4 vCPUs and
`772b` at 1) classed every pool and host request by why the block was not in the asking vCPU's cache:

| Class | Warm Konsole (4 vCPUs) | Warm Dolphin (4 vCPUs) | Who fixes it |
| --- | --- | --- | --- |
| (a) a peer vCPU holds the block live | 71% of requests, 59% of their time: 0.33 of 0.55 vCPU-s | 41% of requests | P2 |
| (b) the asking vCPU had it and flushed it | | (a) + (b) = 65% | P2 in part: one area for all flushes less often than N small ones |
| (c) the lossy hint table lost it | 21-28% | 21-28% | P1 step 1, the exact frame-keyed L2 |

On 1 vCPU class (a) turns into (b) almost one for one: the same blocks are asked for again, after a capacity flush instead
of after a move. So the cost is "this vCPU does not have a block that exists", and the flush rate is part of it.

## 2. What P2 depends on: the exact frame-keyed L2

- **R1, block identity (P1 step 1).** A block is named without any vCPU's state: L2 is exact and keyed by (first source
  frame and page offset, RIP, context). A lookup first finds the frame RIP is fetched from on this vCPU now (the fetch
  walk, or the fast miss path's page record), so an L2 hit is current for the asking vCPU by construction; a crossing
  block's second frame is checked against that vCPU's walk of the next page before it is used. The shared L2 *is* that
  table, published by CAS instead of written by one owner (section 3).

That is all. What P1's later steps would have given, P2 arranges itself with what exists today:

- **No per-vCPU currency in a record.** `Block::checked` (the TLB epoch of its last walk), the `GLOBAL` and `LARGE` flags of
  that walk and the linear chains (`lnext`) are one vCPU's view. With R1 the lookup proves currency, so `checked` goes; what
  a TLB event must find is *this vCPU's L1 entries* (the only place a vCPU caches "current"), and it finds them through the
  private L1 owner lists (`L1_OWNERS`, `L1_LISTS`), whose owner words take the two flags and the linear page. Chunk 6.
- **No shared word is patched for a TLB event.** Today cross-page links are undone at a CR3 load or flush and made again
  (`prune`, `suspend`, `relink`, `cache.rs` 3252-3600): one vCPU's TLB event patching code. In the shared area a link is
  one of two kinds, both independent of any vCPU's TLB:
  - *local* (today's `local`, `cache.rs` 2684: every linear page of the target is a page of the source from the same
    frame): a direct branch, valid for every vCPU that is validly in the source;
  - *checked* (today's PIC stage 2 form, `cache.rs` 2437): any other link is a `b` to the source context's dispatcher
    slot with the target RIP in x0, which probes the running vCPU's own L1. Currency is that vCPU's data, emptied by its
    own TLB events as today.
  Cost: a cross-page transfer that is a direct branch today pays the L1 probe, about 9 ns by the layout page's
  measurement of a dispatched return against a linked jump (section 7 there); chunk 4 counts those transfers and A/Bs the
  form on the private areas before anything is shared. In exchange the whole relink machinery and its `tlb` bucket work
  go. **When P1's guarded entries land, a checked link becomes a direct branch to the target's guard**, and the 9 ns go
  too; nothing else in this plan changes.
- **The entry rule.** A vCPU enters a block only (i) by its resident's `eret` after a lookup, (ii) from its own L1, or
  (iii) by a local link. L1 entries of vCPU v for a block on frame F are filled only by v's resident after it set v's
  code-page bit of every source frame (`cache::mark`), and are emptied when v drains a post for F. So *a vCPU that can enter
  code of F without its resident has its bit of F set, or a post for F pending* (used in section 6).
- PIC translations and their instances keep their forms (local or checked); they hold no vCPU's value (an instance's
  page base is read through x28). If P1 removes them, less to share.

## 3. The shared area

**Regions.** One `b` reaches 128 MiB, and every block must reach its vCPU's dispatcher, probes and tail slot with one. So
the shared code is a row of **regions** of 128 MiB of address space each: the first 2 MiB of every region is an alias of
the vCPU's one private head (the same private memory mapped again), the other 126 MiB are shared code. There is no
ceiling but memory: the number of regions is set at launch from the budget below. Regions and their write aliases
(EL1 only) go in a new window above the guest-RAM alias (today's code window, 256 MiB below the write alias, cannot hold
them); its addresses get compile-time range checks in `natemu_abi` in chunk 2.

| Part | Where | Backing | Mapped |
| --- | --- | --- | --- |
| private head: step slot, dispatcher and slots, probes, **tail slot** (new, 64 KiB) | the first 2 MiB of every region | the vCPU's own block, once | EL0 execute-only at every region; EL1 write alias once |
| shared code | the other 126 MiB of each region | one span for the VM | the same, in every vCPU, same address |
| shared records (block records, sites, chunk tables) | a span beside the regions, sized with them | one span | EL1 read-write, every vCPU |
| shared lookup (L2, generations, slot contexts, flush words, `synced`) | a 2 MiB-aligned span after the global area | one span | EL1 read-write, every vCPU |
| private lookup (Control, L1 and its owner lists, summaries, writers map, step and tail records) | `LOOKUP_VA` | own block | as today |

- *What the alias costs.* The code area is mapped in 2 MiB level-2 blocks, EL0 execute-only (`Rights::Code`, host
  `memory.rs` 331), and the private window is one level-1 table linked from every root of a vCPU (`memory.rs` 274-278),
  so the cost is per vCPU, not per shadow space: per region one level-2 entry for the head and 63 for the shared code,
  512 bytes; one 4 KiB table page per four regions, in new table-page slots of the vCPU's block (`map_private` and
  `private_l2` know only the first `PRIVATE_BYTES` today). Written once at start; never changed. There is room in that
  level-1 table above the guest-RAM alias (34 GiB at most), still at addresses no x86 access can form.
- *What in the head must not depend on where it runs.* Everything already is, or is made so in chunk 2: the dispatcher
  reaches its tables through x28 and its miss by a relative `b` (`write_slot`, `cache.rs` 720); probes return through a
  register; the tail's and the step block's branches out of themselves go only to the head (dispatcher, probes) or are
  stop words, so the same words are right at every alias. What names an absolute head address is data: an empty L1 way's
  target (`CODE_VA + DISPATCH_MISS`: any alias will do, region 0's is used) and the resident's comparisons of a PC with a
  head address, which first reduce a PC to (region, offset in region). A tail runs at the alias of its origin's region;
  the step block at region 0's. To convert in chunk 2 (follow-up review, N1): `probe::at`, `returns_to` and `rearm_at`
  (absolute `PROBES_VA`); the miss test `elr == CODE_VA + DISPATCH_MISS + 4` (`execution.rs` 2368, 2407);
  `probe::branch_word`, `relocate`, `dispatcher_for` and `branch(at, DISPATCH_MISS)`, which take offsets from region 0 and
  must take them from the block's own region; and the readers of code through the write alias by `pc - CODE_VA` (`fp.rs`
  99 and 119, `cache::word`).
- *Instruction cache.* A write to the head is synchronized at the alias it will run at (the tail: one region's), or at
  every alias when it may run at any (a dispatcher slot changing tables: 128 bytes per region, rare), unless `CTR_EL0`
  says the cache is PIPT.
- *How regions fill.* One fill word for the whole row: a chunk is the next 256 KiB, never straddling a region's end. So
  the row fills in address order and blocks placed close in time are in one region.
- *Links between regions.* A local link whose target is out of `b` reach (a block of the same page placed in another
  region) is made as a checked link; counted (`far`), expected rare, as a page's blocks are mostly placed together.
- *When freeing triggers.* When the last region's last chunk, the record span or half of L2 is taken (section 8): one
  flush empties every region. No region is freed alone: links and L1 entries cross regions.

**Budget, proportional to the guest's memory** (ceilings: every span is backed only as touched, so an unused budget
costs address space and page-table pages, not memory; the aim is few flushes, not a small footprint):

| Tier | Size | From |
| --- | --- | --- |
| shared code | guest RAM / 8, in whole regions, at least one, at most a third of the host's headroom (below); `machine.shared_code_mib` overrides | Speedometer places about 400 MiB in a 4 GiB guest (1.55 M answers at about 261 bytes), a tenth of its RAM; the eighth leaves a quarter spare |
| shared records | 2 x the code | 128 MiB of code is full at 83% of the 256 MiB arena and 78% of 1 Mi blocks (`META_BYTES`' comment): about 164 bytes of code and 265 of record a block, 1.6 to 1. The arena is what fills first today, so it is sized from the code with room, not fixed |
| L2 | 16 bytes per 128 bytes of code, the next power of two | a block per 164 bytes; load at most a half |
| chunk tables | 8 bytes per 128 bytes of code | two index words a block |
| generations | 4 bytes per guest page: 0.1% of RAM | |
| pool ring, host answer map | unchanged at first (256 MiB; min(1 GiB, host / 16)) | section 12a |

Headroom = host RAM - guest RAM - 3 GiB for macOS - the pool ring (256 MiB) - the host answer map (host / 16); the
store's tiers together are about 3.2 times its code, hence the third. Pages touched once stay resident across flushes
(the area is reused, not released), so after the first fill the ceiling is the footprint (follow-up review, N4); a host
may release the pages above a low-water mark at a flush it runs, which is not planned.

| Machine, guest | Headroom | Code | Records | L2 and tables | Ceiling together |
| --- | --- | --- | --- | --- | --- |
| M3 (8 GB), 4 GiB guest | 0.25 GiB | 1 region, 126 MiB (the minimum: this host has no room) | 252 MiB | 24 MiB | 0.4 GiB |
| Mac mini M4 (16 GB), 4 GiB guest | 7.75 GiB | 4 regions, 504 MiB | 1008 MiB | 96 MiB | 1.6 GiB |
| Mac mini M4 (16 GB), 8 GiB guest | 3.75 GiB | 8 regions, 1008 MiB | 2016 MiB | 190 MiB | 3.1 GiB |
| a 1 GiB guest on either | | 1 region, 126 MiB | 252 MiB | 24 MiB | 0.4 GiB |

The M3 row is the honest one: an 8 GB host with a 4 GiB guest has a quarter of a gigabyte to spare after macOS, the ring
and the answer map, less than today's four private areas may already take; there the store is one region and
Speedometer (about 400 MiB placed) flushes it three or four times a run, where four private 128 MiB areas flush more
often each. A smaller answer-map cap on such a host (section 12a) is what would buy a second region.

Against today's ceilings at 4 vCPUs: 4 x (128 MiB of code + 256 MiB of records) = 1.5 GiB, mostly duplicates. The numbers
that should replace the eighth are the placed-code footprint of the desktop and Chromium cases divided by their measured
duplication (counters of chunk 7: bytes placed, adoptions, flushes); until then the fraction is the Speedometer figure's.

**Same address in every vCPU is required, not a convenience.** `ic ivau` invalidates by virtual address, and on a VIPT
instruction cache is only guaranteed for the address given (Arm ARM, the cache maintenance instructions' rules for VIPT
instruction caches: aliases of the location under other addresses need not be invalidated; section number to be pinned
by the review). One vCPU's synchronization (`synchronize_code`, `cache.rs` 2126:
`dc cvau` unless IDC, `dsb`, `ic ivau` unless DIC, `dsb ish`, `isb`) must make the new words visible to every vCPU: `ic
ivau` and `dsb ish` are broadcast over the inner shareable domain, which holds every core a vCPU thread can run on, and the
address is the one every vCPU executes at.

**Chunks.** The code area is cut into 256 KiB chunks (eight 32 KiB hot/cold segments). A vCPU takes its next chunk with a
`fetch_add` on the shared fill word and fills it alone with today's `SegFill` (hot zone and code side per segment,
`cache.rs` 105); records come from 64 KiB chunks of the record span the same way. No lock, no shared
line written per block. A chunk's unused end at a flush is lost: at most 256 KiB a vCPU, 0.4% of 64 MiB each.
The chunk table (one entry per code chunk: owner, its index array of record offsets in code-address order and in
hot-address order, a count published with a release store after the record is complete) replaces the block index:
`block_at(pc)` finds the chunk by division and searches that chunk's arrays as today (`cache.rs` 3500-3570). A reader of a
peer's chunk reads the count (acquire) and only entries below it, which never change.

**Records.** A record is written whole by its placer before publication and is read-only afterwards, except one atomic
`state` word: `DEAD` (set by its placer alone, for a block it gives up before publishing) and the flags written after
filing today (`UNSHAREABLE`, `cache.rs` 1350 and 1396; `fetch_or`). It also holds the generations of its source pages
(section 6) and the stamp of its synchronization (section 4). The inbound lists, the link records and the per-page
chains of today's record are gone (third review, N2: section 6 says why nothing needs them).

**Publication by compare-and-swap.** An L2 slot is one `AtomicU64`: {record offset / 16 (28 bits: 4 GiB of records),
flush epoch (12 bits), key tag (24 bits)}; 0 is empty. (The epoch field wraps after 4,096 flushes; the flush that wraps
it clears L2, so a slot of an old epoch never reads as current.) To publish block B the placer (1) writes and synchronizes the code, (2) writes the record and the chunk index entry,
(3) **loads its own code-page bits of B's source frames (section 6): if one is clear a write got in, and it publishes
nothing**, sets `DEAD` and steps the instruction from fresh bytes, as `kept` does today (`execution.rs` 631); (4) probes
from the key's hash and CASes B into the first slot that is empty, of another epoch, or names a record that is not live.
The check is before the CAS because a published block can be adopted at once (third review, B1).
A slot value is unique in an epoch (records are never
reused before a flush), so the CAS has no ABA. A lost CAS re-reads the slot: if it now names a live block of the same key,
the placer adopts that one and its own is left dead (space lost until the flush; counted). Duplicates that slip through
(two placers taking different slots of one probe sequence) are both right translations of the same bytes and die
together; lookup returns the first. Nothing needs the key to be unique.
A lookup probes until an empty or other-epoch slot, compares the tag and epoch, then the record's full key, then
liveness. Replacing a dead slot cannot hide a live key: a live block's slot is never replaced, and every slot before it
in its probe sequence stays non-empty for the epoch (slots only go from empty to full, or from one record to another). The order (code synchronized, then
record, then slot) makes everything a slot leads to complete: the slot's CAS is the release, the reader's
load the acquire.

**Dispatcher slots.** A block's computed exit is a `b` to its context's dispatcher slot, bound per vCPU today
(`bind_slot`, `cache.rs` 775; `dispatcher_for` gives the miss when the context has no table *on this vCPU now*). Shared
code needs one meaning for slot k: the shared lookup span holds `slot_contexts[128]`, each claimed by CAS from
`NO_CONTEXT` and kept until the flush. Placement bakes the context's slot (the miss once all 127 are taken, as today). What
slot k *does* stays private: each vCPU's head holds `b` to the miss until that vCPU gives the context an L1 table, and is
rewritten in its own memory when tables change hands (`write_slot`), which stops nobody else.

## 4. Link forms that are legal while another core runs the code

The rule (Arm ARM, "Concurrent modification and execution of instructions": B2.2.5 in DDI 0487 issues from D on, B2.6.5 in
the first ARMv8 issue, which is the number the Linux comment at `aarch64_insn_hotpatch_safe` quotes): if one thread of
execution changes an instruction while another executes it, the result is the old or the new instruction's behaviour
only when *both* the instruction before and the instruction after are from the set **`B`, `BL`, `BRK`, `HVC`, `ISB`,
`NOP`, `SMC`, `SVC`** (A64); for any other pair the architecture allows any behaviour achievable at that exception level
(CONSTRAINED UNPREDICTABLE), unless the writer first makes sure no other thread can execute it. No `isb` is needed on
the executing cores for a pair inside the set; the writer still makes the word visible (`ic ivau`, `dsb ish`).

Today's forms against the rule:

| Patch (`cache.rs`) | Before, after | Legal on shared code |
| --- | --- | --- |
| plain link of a split block (`link_at` = the hot copy's exit trampoline, 233; `link_word` 238) | `b` to the cold exit, `b` to the target or `nop` when the target's hot copy follows | yes |
| plain link of a block placed whole (the link point's first word, 2497) | first word of the RIP move (`movz`), `b` | **no** |
| checked link (PIC stage 2, the point's `svc #LINK` word, 2437) | `svc`, `b` | yes |
| unlink (`unlink_word` 2921) | the reverse of each | as above |
| arm, disarm (2245, 2288) | exit words (`movz`, `b`), `brk` (`STOP_WORD`) | the `movz` ones no; and a stop is one vCPU's, so no shared word may carry it at all |
| placement, hot copy, probe returns, relocation of the computed exit (140-193, 2202-2237) | written before publication | not concurrent: no other vCPU can reach the words yet |

So in the shared area (the two kinds of link are section 2's):

- **L-b:** a *local* link from a point reached from a hot copy is made in its trampoline word: `b` cold-exit becomes `b`
  target (or `nop`). Unchanged from today for split blocks, which are the hot path.
- **L-s:** every other link is made in the point's `svc #LINK` word, after the RIP move (2-4 words, which still run): a
  local link out of a block placed whole, or out of a split block's cold exit (reached only by code resumed in the cold
  copy), becomes `b` target entry (x0 is dead there); a *checked* link becomes `b` to the context's dispatcher slot, which
  needs x0. Blocks placed whole are those with no exit to split at or malformed for the split (translated-code-layout
  section 4); chunk 4 counts their share of taken links before choosing whether to give them a trampoline too.
- The first word of a RIP move is never patched. A link word is restored only to the word it held (`b` cold-exit or
  `svc #LINK`).
- `natemu_abi::shared::patchable(old, new)` is the one predicate (both words in the set); every write to a published
  word goes through one function that halts (`stop::BAD_LINK`) when it is false. Test builds check it; so does release.

**When a peer may be sent into new words (review finding B2).** A published block's code was written into a chunk no
other vCPU can reach, and synchronized (ending in `dsb ish`) before the slot's CAS. That clears every core's instruction
cache of the old words, but the architecture promises another core the new instructions only after that core has had a
context synchronization event: words it fetched before the invalidation may still be in its pipeline. (Linux's arm64
`flush_icache_range` therefore follows the invalidation with `kick_all_cpus_sync()`, an interrupt on every CPU, except
for the hot-patchable set.) Three of the four ways into a block give the entering vCPU such an event after the block's
synchronization: its resident's `eret`; its own L1, which its resident filled before an `eret`; a checked link, which
goes through its L1. The fourth, a local link's direct branch, does not: a peer running the source could follow a freshly
patched `b` into words it has never been synchronized for. So:

- every vCPU keeps `synced[v]` in the shared lookup span: all ones while it is in its resident (stored at each entry
  from generated code), the counter (`isb; mrs cntvct_el0`) at its `eret` back to generated code otherwise;
- a block keeps `stamp`, the counter read the same way after its code's synchronization completed;
- **a local link into T is written only when `synced[p] > T.stamp` for every other vCPU p** (a vCPU in its resident, or
  away, passes: it will `eret` before it runs anything). Otherwise the linker enters T by `eret` without patching, and the
  point links at a later trip. Nobody is kicked for it: every vCPU passes its resident at its next timer interrupt at
  the latest.

Cost: two stores to a line of its own per resident trip; links into blocks placed microseconds ago are sometimes made
one trip later (counted in chunk 7). Words of the last epoch at a reused address are covered by the same rule (and every
vCPU passed through its resident at the flush).

**The lazy link, in one place** (what the linker does at an `svc #LINK` trip in shared code). It is lock-free and keeps
no record:

1. The target T is found by lookup. The link is *local* when every source frame of T is a source frame of the source S
   at the same linear page (`local`), T is in `b` reach, has code, and is live; otherwise it is *checked*.
2. Local: if any peer is behind T's stamp (`synced`), no link now. Else one aligned 32-bit store of `b` T (or `nop`) into
   the point's word, a legal pair (`patchable`: L-b or L-s), then the instruction-cache maintenance for that word (`dc
   cvau` unless IDC, `dsb`, `ic ivau` unless DIC, `dsb ish`, `isb`). Checked: the same store of `b` slot.
3. Two vCPUs linking one point at once both store a right word (the same target, or two live blocks of the same key):
   whichever lands last stays. A word other than its admitted one is left alone. A checked link, and a local one out of
   `b` reach, always goes in the `svc` word (it needs x0 from the RIP move), never in a trampoline; a context with no
   dispatcher slot gets no link.
4. **A link is never undone.** Not for a TLB event (section 2), not for capacity (there are no link records to run out
   of), and not when its target dies: a local link's target has only frames its source also has, so a write that kills
   the target kills the source with it (section 6), and a dead block's words no longer matter. Everything a link word
   ever held is gone at the flush.

So no `out[k]` claim, no lock, no inbound list and no link record exist in the shared area (the first two reviews'
drafts had all four, for a cut that the third review showed is never needed). PIC translations: a local link out of a
shared translation is made only under the same frame rule (`same_frames`, `cache.rs` 2391); chunk 7 checks each PIC
form against it and makes the others checked.

**Instruction breakpoints are one vCPU's and links are everyone's** (second review). Today arming DR7 flushes the arming
vCPU's cache and its linker refuses a target at a breakpoint address (`execution.rs` 793), so its blocks reach `find`,
which delivers the #DB. A link made by a peer would carry the armed vCPU past its breakpoint. So the shared lookup span
holds a count of vCPUs with an enabled instruction breakpoint: arming one (0 to 1 on that vCPU) raises the count and
then asks for a VM-wide flush, and while the count is not 0 no local link is made by anyone (every in-page exit enters
the resident, as on the armed vCPU today). Adoption has the same hole (third review, B3): the host cuts a block at the
asking vCPU's breakpoints, which are not in the key. So an armed vCPU, and one with a stop RIP or host fences, adopts a
block only if its guest range holds none of them (the checks `reused` makes today, `execution.rs` 1270), and otherwise
runs its own, unpublished, in its private head's step slot or a private chunk. Arming another address on a vCPU that
is already armed leaves the count alone but still empties that vCPU's L1 and tail (its adopted blocks passed the old
check), as its own flush does today. Debugging speed, by design.

## 4a. In-page edges: certain, linked lazily (eager linking evaluated and set aside)

The owner's direction (November 26): an edge between two blocks of the same guest page holds for every vCPU and every
process that runs the source; only lookup and branching *between* pages are per vCPU (now the dispatcher's L1; later
P1's guard). In the shared area that is section 2's local link: made once, by whichever vCPU first takes the exit, and
there for all. Linking them *eagerly*, at placement, was drafted here and reviewed (Opus, second review); the owner then
said lazy linking is fine, so it is off the critical path. What the review found, kept for whoever builds it:

- The claim "a link made at placement is the same link made lazily" was wrong in four places. The lazy linker and `find`
  refuse things placement does not know: a system-instruction record as target (its `code` is the fill point: a link
  would branch into the next block placed; `cache.rs` 1942), targets at an instruction breakpoint and an STI-first
  block's self-link (`execution.rs` 793-798), points already holding a checked link (`Link::Bad` would halt), and
  anything before `kept` and the reuse check have passed (`execution.rs` 355-407; the check compares the copy word for
  word). An eager link belongs after those, in `find`, filtered to absolute live blocks with code.
- The page chain holds every record filed from the frame (decodes, translations, instances, dead ones), so a walk of 64
  buys little; an index of pending edges by (frame, offset, context) would replace it.
- It removes less than it seems: the trip that places the target stays, and the linker links that source anyway.
  Outbound-only linking (up to four exact L2 probes, no walk) covers loops and joins and is what to build first, if ever.
- Its measurement is not the sampled transitions of the draft (those are nearly all linked already) but the linker's
  trips classed by what would have removed them: local with the target older than the source (outbound eager), local
  with the target newer (inbound), target placed in this trip (nothing), cross-page (nothing), not linkable (nothing),
  each charged its ticks. Block numbers rise in placement order, so the classing is free.

## 5. Stops without patching code

A doorbell (a peer's kick, a timer, the host) interrupts a block mid-way; the vCPU must reach the next instruction boundary
the resident can bank, which is the block's next exit, and stop there. Today `interrupted` (`execution.rs` 2581) stops at
once at a block's first word and otherwise arms: every exit word of the block becomes `STOP_WORD` (`cache::arm`), the block
resumes, traps at an exit, and `disarm` restores the words at the next resident entry: two patch-and-synchronize rounds on
words that, shared, every other vCPU would trap on too.

**Chosen: the private tail.** At a doorbell inside block B at native offset o (from `native_offset`, which already maps
a hot-copy PC to the code's offset), the resident copies B's code (its whole cold copy, `code_size` bytes, at most 32
KiB, about 260 bytes on average) into the tail slot of its private head, rewriting on the way:

- every exit's first word (each link point, and the computed exit's `b`) **and every link point's `svc` word**, linked
  or not (`svc_slot`): `STOP_WORD`. A doorbell inside a RIP move resumes past the first word, and in shared code that
  `svc` word may be a link's `b` (L-s): unstopped, the vCPU would leave the tail with its kick lost. `arm_words` arms
  those words today (`cache.rs` 2264) and `stopped` already takes a stop there with x0 as the RIP (2738);
- every other word that names an address outside the copy, by `natemu_abi::execution::moved_word` (the hot copy's own
  re-encoder, `cache.rs` 135): the split stubs' `b` to a probe (re-encoded from the new address; the head is in reach),
  the run-end return and the probe returns that the placement pointed into the hot copy (pointed at the same offset of
  the tail instead);
- The as-built encoder accepts the typed split-probe `adr x0` and the branch forms above. An unclassified `adr`, literal load, register branch or branch-with-link takes the in-place fallback.

It synchronizes the tail (its own memory; one round) and resumes at tail + o with the interrupted registers. When the
interrupted PC is in a probe thunk (in the head, not in B), the PC stays and the thunk's return address into B is moved
to the tail wherever the thunk holds it at that word: x0 at its first word, d0 after it, both at its last two
(`probe::words`, `probe::returns_to`). A PC in the dispatcher is a boundary already (as today). The tail has a record in the private lookup area, as
the step block has (`STEP_RECORD`): B's record with `code` = the tail slot, no hot copy, flag `TAIL`, and B's sites read
from B's record (the offsets are the same). So an abort in the tail finds its site, resumes in the tail, and a stop word
there is taken by `stopped` (`execution.rs` 2718) with the link point's target as RIP exactly as today; a second doorbell
in the tail merges (`s.merged`) and never rebuilds the tail: the PC is already in it. Nothing is restored afterwards:
the next stop overwrites the slot. What else changes with `arm` gone:

- `interrupted` (`execution.rs` 2652) and `stopped` (2725) refuse a `DEAD` block and halt (`BAD_STOP`). In shared code a
  writer's kick normally finds its peer inside a block the writer has just killed, so a dead block is a normal place to
  be interrupted and stopped in: the filters go, there and on the abort paths (the code is intact until the flush).
- `resume` (2526) and `resume_interrupted` (2542) arm again when a stop is still wanted after an abort: they resume in
  the tail if the PC is in it and build it otherwise; the PIC stage 2 test's `arm_svc` (2326) becomes a tail whose `svc`
  words alone stop.
- The step block is private and alone in its slot: its exits already enter the resident (`place_step`), so a wanted stop
  is taken there; it never needs a tail.
- Everything that reads a block's words by a PC (`probe::stub_of`, the site checks) reads the tail's words for a tail PC.

- Cost: one copy and one synchronization per armed stop, against two rounds today (`patch` 0.026 vCPU-s of the
  stretch); none on any path that takes no stop; no change to fexc, admission or the disk cache's keys.
- Soundness: the tail is B's admitted code with exits replaced by a trap and out-of-block branches re-encoded by the rule
  the hot copy already uses; it runs the same instructions on the same state from o to the first exit. Blocks only branch
  forward and no branch enters a link point after its first word (admission; `arm`'s own argument), so it ends at an exit
  or enters the resident. B dying meanwhile changes nothing: the tail is a copy, and it stops at B's next exit like any
  block already running when its page is written.
- A word `moved_word` has no form for: the stop falls back to the software step the resident already has for watched
  accesses (`execution.rs` 2382-2390, `fp::EC_STEP`), stepping until the PC is an exit word; counted, expected 0, and a
  unit test runs the tail encoder over the admission corpus (`rust-admission`) to show which forms occur.

**The alternative: a flag polled at block entry.** fexc emits, after each unit's entry words, `ldrb w16, [x28, #stop]`;
`cbnz w16, 1f` with a stub that enters the resident; the doorbell sets the flag in the vCPU's guest-state page and
resumes, and the next block entry stops (a REP string's self-link included, which gives its between-iterations stop for
free). `arm`, `disarm`, `STOP_WORD` and the tail all go. Cost (*estimate*): 8 bytes on about 261 code bytes a block
(3%), on the hot stream; one first-level-cache load and a never-taken branch at every block entry, 2 instructions on the
20-60 of a typical block, so 1-3% of translated-code time before any effect of the denser stream; a scratch register at
entry that the block must not need; a change to the FEX fork, an admission rule for the two words, the hot/cold split, the
step unit, and every disk-cache and pool key (`TRANSLATION_ARGS`). It is the simpler mechanism and the costlier one, on
every block for an event that is rare per block. If P1's guarded entries come to load a word of the vCPU's own at
every entry, the poll folds into that load for nothing and becomes the better choice; that is decided when they land.
Chunk 3 builds the tail on today's private areas, where it is an A/B of its own.

## 6. Guest writes to translated code

**Shared state per guest-physical page** (global area and shared lookup span):

- the code-page mask (as today, one word of vCPU bits): bit v now means *v may hold private references to code of this
  page* (L1 entries, a PC, a tail) and, as today, any bit set keeps every shadow of the page read-only (`translated`,
  `cache.rs` 3609);
- `gen[page]`, a new `AtomicU32`: the page's code generation. A record keeps the generations of its source pages as its
  placer read them. **A block is live iff `DEAD` is clear and each of its generations equals its page's now.**

Private, per vCPU: for each frame, a list of its own L1 entries of blocks from that frame, which is what a drain walks
(third review, B2: a post names a frame, and nothing else finds a vCPU's L1 entries of a frame short of scanning 262,144
owner words). L1 entries are reused all the time (the second way, CR3 loads, a table changing context), so the list is
not threaded through them (follow-up review, F1: a refilled entry would cut the old frame's list): it is an append-only
arena of {L1 entry, block} items chained per frame (two items for a crossing block), and a drain empties an entry only
if it still holds that block. A full arena empties all of this vCPU's L1 and starts again.

**Mark before read, in shared state.** To translate from a frame, or to adopt a block it found in L2, vCPU v does, for
*every* source frame F of the block (both of a crossing block; and it has walked its own mapping of the second page,
section 2):

1. sets its bit of F and makes sure no vCPU keeps a writable shadow of F: today's `mark` (`execution.rs` 1892, the one
   synchronous shootdown), unchanged; the `fetch_or` is skipped when a load shows the bit set;
2. loads `gen[F]` (SeqCst), after the mark;
3. reads the guest bytes and has them translated (a placer), or compares the record's generations with those just read
   (an adopter);
4. **loads its own bit of F again** (SeqCst): clear means a write got in. A placer then publishes nothing (section 3);
   an adopter does not use the block. This is `kept`'s check of today (`execution.rs` 639);
5. a placer publishes; either puts the block in its L1 and on its private list of F.

**The write.** `written(F)` (a write faulted on a read-only shadow, or the walker's A/D write, `cache.rs` 2947) becomes:

1. `bits = mask[F].fetch_and(0)`: every vCPU's bit, as today;
2. `gen[F] += 1` (SeqCst), **after** the bits are cleared: every block of F, everywhere, is dead;
3. for each peer in `bits`: post F to its record and kick it (`post`, 2975, unchanged: the kick is what stops a peer
   that is in the page's code, or about to return into it);
4. empty its own L1 entries of F (its private list); then the write goes ahead as today (its own writable install reads
   the marks again).

That is all: no walk of the page's blocks, no code word written, no synchronization. `rechecked` (after a write through
the RAM alias, 2962) does 1-3 for the peers' bits when any is set, as today. A peer's drain (`drain`, 3850) of a posted
page empties that peer's L1 entries of the page from its private list: data only.

**Why this is sound.**

- *Stores.* A store to F is made only by a vCPU whose writable install read F's mask as empty after setting its own
  writable bit, or between `written` and `rechecked` through the RAM alias (both as today).
- *No published block with current generations has stale bytes.* Let v be the placer: its bit was set and the shadows
  shot down before it loaded `gen` and read the bytes. A store after that read needs the mask emptied after v's
  `fetch_or`: some `fetch_and` X, followed by X's bump. If X came before v's last load of its bit, v sees it clear and
  publishes nothing. If after, then X's bump is after v's load of `gen`: the block's generation is behind, and it is not
  live. (The first draft bumped before clearing and had no last check: the first review's B1. The second put the check
  after the publication, where an adopter could take the block in between and keep it: the third review's B1.) Between a
  RAM-alias store and `rechecked` a block placed in between is live with the old bytes until `rechecked`'s bump: today's
  window between that store and the peers' posts, inside the writing instruction.
- *An adopter never keeps a stale block.* What it adopts was published, so by the item above it was right when its
  generations were current; the adopter compared them after its own mark. A write after that: if its `fetch_and` came
  before the adopter's last load of its bit, the adopter refuses; if after, the adopter's bit was in it, and it is posted.
- *The mask is never empty while a live block of F exists.* A placer's bit stays until a write (step 1 is followed by step
  2, which ends every block of F) or the flush. No vCPU clears its own bit alone (today's `invalidate` and `reset` do; both
  go).
- *Dead code is left, and never entered again.* The ways into a block (section 2): a lookup (compares generations: refuses
  a dead block); a vCPU's own L1 (emptied of F at its drain, and it is posted because its bit was set when it filled the
  entry, or it refused at step 4); a checked link (its L1); a local link from a block S that has every frame of the
  target, so S is dead too. So only a vCPU already in dead code of F can go on in it, through local links among dead
  blocks, and such a vCPU has its bit of F or a post pending: step 3 kicks it, the tail stops it at its block's next exit
  whatever that exit is linked to (section 5), it drains, and from then on every way in is closed to it. A linker that
  checked its target live just before the bump stores a link into a source that died with it: harmless for the same
  reason.
- *Two writers of one page:* both clear, both bump; liveness is by generation and each posts the bits it took.
- *The writer itself* empties its own entries and finds every block of F dead before its store completes, as today.

**Against x86's rules.** SDM Vol.3 "Handling Self- and Cross-Modifying Code" (9.1.3; 8.1.3 in older issues): a write to
code a processor has prefetched is seen by that processor (the write's linear address is checked against the
instructions in flight), and a processor modifying code *another* processor is executing must itself keep that
processor from executing it (the SDM's protocol: the others wait on a flag, then serialize); otherwise the other
processor may run the old or the new instructions for an unspecified time. natemu's rule is the same as before P2: the
writing vCPU never runs a translation of the old bytes after its store (step 5, and its current block is ended by the
existing code-write path); a peer runs old code at most to the end of the block it is in when the kick arrives, which
real hardware allows for unsynchronized cross-modifying code, and a guest that follows the SDM's protocol (or Linux's
`text_poke_bp`: a breakpoint byte first, then `on_each_cpu` to synchronize) gets its interrupt at that block boundary. What
changes is who does the work: one vCPU kills the page's blocks once, where today each holder cut its own copy.

## 6a. The write protocol, case by case

The hard part, so each case on its own: what happens, what it costs, what a peer can see. "Holders" are the vCPUs with
their code bit of the page set; B is the number of blocks placed from the page, L the links into them.

**1. Same-CPU self-modifying code: the next instruction must see the new bytes.** A store from translated code into a
page with code bits faults on its read-only shadow and reaches `code_written` (`execution.rs` 3646), unchanged by P2:

- the storing block was translated from that page: its instruction is run alone (`step_from`: the state banked at the
  instruction's start, a one-instruction block in the private step slot, which marks no page). The step's own store
  takes the write path of section 6 (bits cleared, generation bumped, peers posted, this vCPU's L1 entries
  of the page emptied) and completes; its exit enters the resident, and the next instruction is looked up: L2 holds no
  live block of the page, so it is translated from the bytes now there. The rest of the old block never runs. This is
  the SDM's guarantee for a write into the code the same processor is executing, kept exactly as today;
- the storing block is from another page: section 6's steps, then the store completes and the block runs on (it reads
  none of the written page's bytes).
- New in P2: the storing vCPU may be in a tail (a stop pending). The fault's site is found through the tail's record; the
  step leaves the tail, and the stop is taken at the step's exit.
- After the store the page has no code bits, so its shadow may be writable again: further stores to it cost nothing
  until some vCPU executes from it again (its mark makes every shadow read-only again, one synchronous shootdown).

**2. Cross-modifying code: a peer vCPU is executing the page.** The writer does section 6's steps and its store; it does
not wait for the peers. Each holder is posted and kicked; an interrupt at its next instruction boundary the hardware
gives (it is in generated code or in its resident), then the tail, a stop at its block's next exit, the drain (its L1
entries of the page go), and a lookup that finds nothing live.

- *What the peer can observe between the store and its kick:* it goes on executing translations of the old bytes, and
  may pass from one dead block of the page to another through local links, until the interrupt lands; then to the next
  exit of the block it is in (the tail). As *data* it reads the new bytes
  at once (the store is in RAM). So for the interrupt's latency plus the rest of one block, a peer can execute old
  instructions while a load of the same address returns new bytes. Real hardware has the same shape with a shorter
  window (the instructions already in the peer's pipeline; a snoop hit clears it), and the SDM gives no guarantee for
  unsynchronized cross-modification, only a protocol. Today's natemu has the same window (the peer drops its own blocks
  "at its next block entry", `cache.rs` 2943).
- *What stays true:* a guest that synchronizes gets what it asks for. An IPI (Linux's `text_poke_sync` after writing the
  INT3 byte, `on_each_cpu`) is delivered at a block boundary after the drain, so its handler and everything after it run
  code of the new bytes. Code patched with atomic stores and no synchronization (a JIT's inline-cache or call-target
  patch) sees the old target or the new one, the new one from the peer's next block at the latest after its kick.
- *The stricter form,* if a guest is ever found to need it: the writer waits for each holder's drain before its store
  completes (the writable-drop shootdown's wait, `execution.rs` 1892): after the storing instruction no vCPU executes
  the old bytes. Cost: the wait for k interrupts on every code write. Not proposed; `shared-xmod` can run both.

**3. A JIT writing fresh code (W^X or not).** The page has no code bits while it is written: no fault, no protocol,
nothing shared is touched, however many stores. The first execution marks it (shadows of every vCPU made read-only:
one synchronous shootdown of the vCPUs with a writable shadow of it, as today), translates, publishes. Under W^X the
guest's own `mprotect` and TLB shootdown come first; natemu's detection is by guest-physical frame and does not depend
on them (nor on which linear address the write used: Linux's `text_poke` writes through a temporary mapping).

**4. A JIT patching in place, rarely.** One event (a store into a page whose code is live on k holders):

| Step | Who | Cost (*estimate*) |
| --- | --- | --- |
| fault, `code_written`, the step if the block is from the page | writer | as today: an abort (about 0.35 us) and, for the step, a host round trip (about 12 us, translated-code-layout section 5); nothing new |
| clear, bump | writer | two atomic operations |
| post and kick k - 1 holders | writer | as today (`post`: three atomic operations and an SGI each) |
| the page's blocks and the links into them | nobody | nothing: dead by the bump. Today each of the k holders walks its chain, undoes every link into and out of its copies and synchronizes the words |
| stop, drain: its L1 entries of the page emptied from its private list | each holder | an interrupt and a stop (as today), then data only |
| the next execution from the page: its mark shoots down the writer's writable shadow | first vCPU back | one synchronous shootdown, as today |
| re-execution | first vCPU back on the page | each block it runs is fetched again: unchanged bytes come from the pool or the host's map by content, changed ones are compiled; placed and linked once |
| re-execution | every other holder | an adoption per block (an L2 probe, an L1 fill); today each fetched, placed and linked its own |
| space | the area | the dead copies stay until the flush: a page killed n times holds n generations of its executed blocks |

So a write event costs the writer less than a holder pays today (two atomic operations and the posts, even when it
holds nothing of the page), the other holders less than today, and the re-translation is paid once instead of k times.
New costs: bits last longer (adopters set them, and nothing clears them but a write or the flush), so a page that was
code and became data costs one fault at its first write, as today, but more vCPUs are posted by it; and dead copies
fill the area, so a guest that keeps rewriting executed pages now brings on VM-wide flushes instead of one vCPU's. The kill is by page, as today: a store to a page that also holds hot
code (data beside code) kills that code for every vCPU each time, and a guest that alternates writes and execution on one
page pays a re-fetch of the page's executed blocks per round; P2 does not make that worse per vCPU and makes it cheaper in
total, but it is the pattern to count (invalidations per page, chunk 7's counters) before finer granularity is thought of.

**5. Races the protocol must survive** (each a test of section 9): a placer between the writer's clear and bump (B1);
an adopter taking a block between its publication and a late check (the third review's B1: the check is before the
publication); an adopter that marked before the clear; two writers of one page; a writer and a flush; a peer whose only
references are L1 entries (its private list); a write to the second page of a crossing block; a store by the step
block itself (it only invalidates: no step of a step).

## 7. What stays per vCPU

| Per vCPU | Why |
| --- | --- |
| L1 tables, their owner lists (with each entry's global and large flags and linear page) and the dispatcher slots' contents; P1's code TLB when it lands | a CPU's TLB: which linear pages map to which frames is each vCPU's own CR3 and flush history |
| the private head: dispatcher, probes, the step slot and its record, the tail slot and its record | per-vCPU code that may be rewritten without stopping peers |
| the pending stop (`stop_wanted`), events, the invalidation record, the doorbell | an interrupt is one CPU's |
| code-page and writable bits (its bit), summaries, the writers map, shadows | which pages it may reference or write |
| staging, the host channel, `Control`'s counters, PC hints | one writer each; counters summed by the host |
| the chunks it is filling, `synced[v]`, `away[v]` | allocation without a lock; one writer each |

Shared: code, hot copies, records and sites, L2, generations, link words, slot numbering,
system-instruction decodes (records without code), the reuse hints and the PIC instances if P1 keeps them (they hold no
vCPU's value: an instance's page base is read through x28). The pool stays: it serves the same bytes at *another* frame
(configure's relinked programs), which exact L2 does not; its hits at the same frame move to L2.

## 8. Freeing only with the world stopped

Nothing in the shared area is reused while any vCPU can hold a pointer into it: a dead block's code, record and L2 slot
stay until a flush, so a vCPU in a dead block, its abort handler's `block_at`, a tail's origin record and a peer's stale L1
entry all still name intact memory. Space is recovered only by the capacity flush, VM-wide, modelled on the I/O window's
request/acknowledge/quiescent words (`execution.rs` 2047-2130):

1. The vCPU that finds the last region's last chunk, the record span or half of L2 taken is at a block boundary (it is
   placing). It CASes `flush.request` from e to e + 1; a loser takes part as a peer.
2. It posts `FLUSH` to every other vCPU and kicks it, then waits, serving shadow-drop requests meanwhile (`take_shadows`,
   as a split-lock wait does), until each peer p has `flush.ack[p] == e + 1` or `flush.away[p] != 0`.
3. A peer stops at its next block boundary (section 5), and in `flush`: empties its L1 and its private per-frame lists,
   slots, PC hints and tail, **gives up the chunks it was filling**, clears its own code-page bits by its summary (`clear_own`), moves `s.generation`
   (every resident path that holds a block pointer across a drain re-checks it, as `kept` does; a `mark` wait that
   drains a `FLUSH` abandons its placement the same way), stores its `ack`, and waits for `flush.released == e + 1`.
4. With every peer acknowledged or away, the requester resets the fill words and the slot contexts and bumps the epoch:
   L2 slots of another epoch are empty (no clear of the table, which `reset` does per vCPU today, `cache.rs` 446).
   Then `released = e + 1`.
5. **Away.** A vCPU that will run no generated code and touch no shared state until it has looked again stores `away = 1`:
   before every exit to the host (a compile, a device), and before every wait inside its resident: the I/O window's park
   (`io_checkpoint`, 2095-2130), HLT (a `wfi` loop in the resident, `lapic.rs` 334) and the AP start-up wait. When it
   comes back, it stores `away = 0` and loads `flush.request`, both SeqCst. If the epoch moved, it resets its private
   state, acknowledges the observed epoch and waits for its release before touching shared state or entering code.
   A release already observed satisfies that wait immediately. The requester reads `away`
   after publishing the request, so it either sees the peer away (and the peer sees the request on return) or gets its
   acknowledgement. A vCPU stopped for good stays away. So a requester never waits for a vCPU parked in an I/O window
   while the host waits for the requester's own acknowledgement of that window (review finding B4).
   **An away point in the middle of a block** (third review, B4): a device access makes its host exit from inside an
   abort handler (`mmio::page`, `mmio.rs` 261-284) and then resumes at the faulting PC, in a shared block or a tail
   whose record reads that block's sites; so can a park reached from an abort path. If the flush moved while it was
   away, that code may be someone else's now: it never resumes there. It banks the state at the instruction's start
   from the site (`bank_at`, as a stepped store does) before going away. After a classification-only exit, a flush
   redispatches from that RIP. At an accepted device access (`mmio::device`), keep its completion metadata before
   publishing `away`. Finish the accepted operation using its host reply, advance RIP once and then redispatch.
   A pending reply is polled on the existing operation.
6. **A flush the host asks for** (`FLUSH` posted from a host write window, a new instruction breakpoint): the host,
   holding the window with every resident parked (so away), bumps the epoch itself and resets the fill words; each
   resident does step 3's local part on return. Host tools that read a vCPU's code or records (the reuse check, the audit,
   profile symbolization) read the shared spans.

Generations are not reset (a block's generation can only match again after 2^32 bumps of one page inside one epoch, and
each bump needs a translation of the page first, which the area cannot hold).

Sizing (*estimate*): today 16 MiB of code and its records per vCPU, each flushed on its own; 64 MiB shared holds what four
vCPUs hold now if nothing were common, and more by whatever is. The counting boot's class (b) is what a lower flush rate
buys; chunk 8 measures it.

**Private-cache prerequisite.** On a synchronous abort, the resident takes pending page cuts first. If the
origin is now dead or a full flush was requested, it banks the interrupted instruction while the origin still
owns its recovery sites, then completes the flush and steps the instruction. Ordinary retries skip this bank.
On the Mac mini M4 the `system/shared-stop-flush` case replaces the retired PIC origin with an absolute
RIP of zero and no sites. Banking after reset made both vCPUs stop at the wrong RIP in `pic-xm` at both
block sizes and `pic-pfault` at the default size.
With pre-reset banking, all four program/block-size combinations passed and the case counted six
forced recoveries. `system/shared-stop` passed its 52 tail and 52 fallback runs, and
`system/shared-stop-x87` matched its QEMU reference.

The layout from `shared-flush` at `d0fb9bf5` and P1 at `0a51ba5b` both allocate the first backing byte
after per-vCPU metadata. Keep P1's link records there, place the region page tables after them, and derive
the code and vCPU stride from both reservations. P1's `GUARDED` and the private tail's `TAIL` at
`26ca38c5` also use the same block flag bit (24) and need distinct values when combined. The multi-region
guard must branch to its own region's `GUARD_MISS` alias and recognize that alias on return.

## 9. Tests and mutations

Unit (host threads, `natemu_abi::shared`, family `unit`): the chunk allocator (no two takers overlap); L2 publication
under contention (every published key found, lost CAS adopts, duplicates both live, dead and old-epoch slots reused, no
torn record seen: a checksum in the test build); the mark/write pair as a model (threads marking and adopting against
threads bumping: no adopted block whose bytes differ from the page's); the flush barrier with away/return races (no thread
touches the area between release and its own reset; a requester against a peer parked in an I/O window; chunks given
up); both B1 interleavings as fixed schedules (the first review's and the third's, with an adopter); the epoch field's
wrap; `patchable` over every word pair the resident writes; the `synced`/`stamp` rule; the tail encoder over the
admission corpus.

System (new cases in family `system`, natemu only, 2 and 4 vCPUs, both block sizes, the `pool-*` programs' bodies):

- `shared-peer`: CPU 0 runs a page, CPU 1 runs it: no host request, no pool hit, no placement and no link on CPU 1.
- `shared-smc`: CPU 1 rewrites a page CPU 0 is looping in; then CPU 2 runs it: new bytes' effects everywhere; with the
  write racing CPU 2's adoption after its mark (`pool-race`'s handshake).
- `shared-xmod`: the SDM's cross-modifying protocol and an unsynchronized writer against a two-block loop on one page
  (dead blocks linked to each other): the looping CPU leaves within one kick.
- `shared-stop`: interrupts and kicks at every native offset of a block with a probing split stub, a run test, a REP
  string and an L-s link (the tail's forms), at every word of a probe thunk, in a block killed just before, and a second
  doorbell in the tail; the stop's RIP and state equal the armed stop's of the baseline.
- `shared-link`: two CPUs taking the same unlinked exit at once; a link made by one is taken by the other without a
  resident entry; a block placed whole links in its `svc` word.
- `shared-flush`: a 2 MiB area so flushes are constant, with one CPU halted, one in a long device exit and one in a
  compile at the request: all values right, no stale entry used after release.
- `shared-ctx`: the 128th context (slots exhausted) and a table changing hands on one CPU while another runs the context.
- the standing SMP suites (`smp`, hackbench, the split-lock and writable-drop cases) and a `--check-reuse` boot.

Mutations, each red in the named test: no generation bump in `written` (`shared-smc`); bits cleared before the bump
(the unit model, `shared-smc` race); the last bit check after the publication instead of before it (the third review's
schedule); a drain that does not empty the private list (`shared-smc`: a holder keeps running old code from L1); a local
link to a target with a frame its source lacks (`shared-xmod`); an armed vCPU adopting a block over its breakpoint
(`shared-ctx`); a resume in the block after a flush taken at a device exit (`shared-flush`);
the bump before the `fetch_and` or no last bit check (the B1 schedule); a `movz` word linked (`patchable`,
`shared-link`); a local link written without the `synced` test (unit); a tail exit or a linked `svc` word left unpatched
(`shared-stop`: a stop lost); the tail's probe return left in the hot copy, or d0 not moved (`shared-stop`); a parked
vCPU not away (`shared-flush`: hangs, bounded by the case's timeout); chunks kept across a flush (unit); `away` not re-checked on return (`shared-flush`); a slot context
bound per vCPU (`shared-ctx`); a chunk index count published before the record (unit).

## 10. What it does not fix

First-ever translations (fexc, admission), the pool's hits at other frames, class (c) (P1 step 1), each vCPU's own L1
fills after its CR3 loads and the fetch walks of its lookups (P1's code TLB), shadow faults (P3), and the adoption
itself: one L2 probe, two shared loads, the bit checks and, the first time on a page, a mark.

## 11. Expected gain

From the counting boot (experiment, section 1), warm Konsole at 4 vCPUs: pool and host requests take 0.55 vCPU-s; 0.33 of
it (59%; 71% of the requests) is class (a), a block a peer holds live.

- **Requests:** class (a) becomes an adoption: an L2 probe, the marks and bit checks, an L1 fill; no pool lookup, copy,
  re-check or placement. At a tenth of a request's cost (*estimate*: the same-vCPU reuse path measured 0.21-0.25 us a
  copy against 2.9-3.4 us a host hit in the cross-vCPU reuse plan; an adoption copies nothing), 0.33 × 0.9 = **0.30
  vCPU-s** of warm Konsole. Dolphin: 41% of requests are (a); its times were not given.
- **Links:** an adopted block arrives linked. In the profile's 0.95 s stretch `link` is 0.142 vCPU-s; if links are spread
  as requests are (59-71% on peers' blocks) that is 0.08-0.10 vCPU-s of the stretch (*estimate*; the two experiments are
  different windows and are not added).
- **Flushes:** class (b) (with (a), 65% of Dolphin's requests) shrinks as far as one 64 MiB area flushes less often than
  four 16 MiB ones whose contents overlap; on 1 vCPU (a) is (b), so this is the part of the gain a single-vCPU guest
  sees. Not sized: chunk 8 measures it.
- **Stops:** one synchronization instead of two: about half of `patch` (0.026 in the stretch).
- **Costs added:** checked links where today's cross-page links are direct (about 9 ns a transfer until P1's guards;
  chunk 4 measures it alone, and it is the number that could make chunk 7 wait for P1); the RIP move of L-s links on
  blocks placed whole; local links deferred by the `synced` rule; the record lock on each link (uncontended); shared lines
  for L2 and generations (read-mostly).
- **Memory:** one code area and record arena instead of N.

## 12. Chunks, in build order

Each is testable alone; 4 and 5 are useful without the rest and are measured on the private areas. The counting the
plan's first draft put second is done (the counting boot); what is still worth counting is noted where it is used.

1. **`natemu_abi::shared` (abi only; no P1 file). Built** (section 14). The slot word and probe, the chunk allocator, generations and the
   mark/write order with the last bit check, the flush words with away, the `synced`/`stamp` rule, `patchable`. Pure code on atomics with the unit tests and mutations of section 9. New file
   `crates/abi/src/shared.rs` and one line in `lib.rs`.
   - **1a (optional, gates nothing): class the linker's trips** as section 4a's last item says, in the counting build. It
     prices eager linking and, split by `local`, says how many transfers chunk 4 turns into checked links. The sampled
     exits in `stopped` (in-page, cross-page, computed, by CPL) are the execution-weighted number chunk 4 wants.
   - (1b, eager in-page linking on the private areas: dropped from the path, section 4a.)
2. **Layout: regions.** The window of regions with the head aliased before each, the record and lookup spans, their
   mappings in every vCPU and both shadow spaces, the budget rule of section 3, the tail slot, range checks; and
   today's private code area moved behind the same aliases (several regions per vCPU, still private), so the alias, the
   head's position independence and far links are exercised and measured before anything is shared. Test: a word
   written through one vCPU's alias is executed by another; a block in the last region reaches its dispatcher.
3. **Stops by the private tail,** on the private areas: `arm`/`disarm` and `armed_words` removed, the `DEAD` filters
   lifted. The implementation and focused cases are on `shared-tail` from `main`. Section 14 records the observed coverage and private-area hackbench comparison.
4. **Legal link forms, checked cross-page links and VM-wide slot numbers,** still private: links only as L-b and L-s
   through the one patch function; `prune`/`suspend`/`relink` removed. Counts L-s and checked transfers; A/B: the cost of
   the forms alone, which decides whether chunk 7 goes ahead on step 1 of P1 or waits for its guards.
5. **The VM-wide flush barrier,** driving today's per-vCPU flushes together, with away at every wait. `shared-flush`.
6. **Per-vCPU currency out of the record** (on P1's exact L2): `checked`, the walk's flags and the linear chains move to
   the L1 owner lists; the private per-frame lists of L1 entries (section 6).
7. **The switch** (`machine.shared_code`, off by default): placement into chunks, publication, adoption, generations and
   the write of section 6, lock-free shared links with the `synced` rule, the armed count. Every system test and mutation of section 9.
8. **Measurement and default.** One A/B on the M4 (the desktop case, hackbench, the build), `docs/benchmarks.md`, the
   gate; then the per-vCPU path is deleted.

Rebase points onto `frame-keyed`: chunks 1 and 2 none (new file; new spans beside the existing ones in
`crates/abi/src/execution.rs`'s `cache` module and the host's `prepare`). Chunk 3 edits `interrupted`, `resume`,
`resume_interrupted`, `stopped` (`execution.rs`) and `arm`/`disarm` (`cache.rs` 2245-2330), which P1 does not need to
touch on the original base. On the combined branch, assign different bits to P2's `TAIL` and P1's `GUARDED`
(both independent branches use bit 24), decide explicitly which placed-block flags a tail inherits, and exercise a doorbell
at every word of a guarded entry before accepting that rebase. Chunks 4-7 edit `link`, `written`, `invalidate`, `file`, `reset`, `block_at`,
`lookup` and the TLB paths, all of which P1 rewrites: they start from P1's head.

## 12a. One store: folding the template pool and the host's answer map into the shared area (evaluated)

The owner's question: could there be one code store, in VM memory, that all vCPUs run, managed mostly by the host, as
QEMU has one buffer? Today a translation exists up to three times: in the host's answer map (`Entry`, `Shard`,
`crates/host/src/execution.rs` 2566-2836, capped at 1 GiB), in the template pool's ring (256 MiB, written by the host,
read by residents), and placed in each vCPU's code area. P2 as planned makes the third one copy. Folding goes further:

**The proposal.** The host, which admits every answer and is the pool's one writer, writes each admitted translation
straight into the shared executable area, placed (hot and cold split, probes pointed, the computed exit on its
VM-wide slot), with its record; the host's map keeps an index from request key to the placed block instead of the
object. Residents look up (exact L2), link (legal word swaps), stop (the tail), and never copy, relocate or file code.

**Answers to the questions.**

- *Absolute (non-PIC) templates.* Kernel code is absolute (global mappings are never shared as instances), and so is user
  code seen at one address so far (a template is rewritten position-independent lazily, when a second address needs it).
  What fraction of executed blocks that is has no number in the repository; the `pic` counters of a counting boot give
  it. Absolute blocks live in the same store, one per (frame, RIP, context); the same bytes at another RIP are another
  block, made by moving the first (`relocate_code`), which in this proposal is the host's work at a request.
- *Instance records.* A PIC translation is placed once; an instance is a record without code that names it, with its own
  key. Making one needs no host: a record from the resident's chunk and an L2 publication. So "residents never file"
  holds for code, not for records.
- *Publication and instruction-cache synchronization.* The host can write the bytes through its own mapping of the
  backing. Whether it can also publish depends on the cache: `ic ivau` works by virtual address and on a VIPT cache is
  only promised for the address given, and the address vCPUs execute at is the guest's, not the host's. If `CTR_EL0` says
  PIPT (believed of Apple's cores, not verified; the resident reads it at start), the host's own invalidation reaches
  the guest's lines, as the resident's write alias already relies on; its stamps for the `synced` rule would have to be
  in the guest's timebase. The form that needs no such fact: the host places into space nobody can reach and returns;
  the asking resident (it is in that exit anyway) synchronizes at the guest address and makes the L2 CAS. The
  pool's release/acquire protocol (tail, fence, slot) disappears with the ring: nothing is overwritten under a reader,
  and the `synced` rule covers links into the new block as for any block.
- *Capacity and freeing.* Easier from the host than from a resident: the flush is a host window (every resident parked or
  quiescent, the mechanism the I/O window already has), inside which the host resets fill words and its index. Section
  8's barrier then needs no requester among the residents.
- *Guest code writes.* Detected in the resident (the shadow fault) and killed there, as in section 6: generation, bits,
  chain, links are all resident-side shared state; the host is not asked. The host's index entry for those bytes stays
  right as a statement about bytes ("these bytes at this RIP under this context translate to that code"), but the block it
  names is dead for its frame; a later request with the same bytes needs a new record, and may reuse the placed code
  only if no local link was patched into it for the old frame's neighbours, which cannot be known cheaply. So in practice:
  a new placement, from the host's kept form.
- *Branch reach.* The same 128 MiB as resident-placed code, for the same reason (one `b` to the vCPU's head). Beyond it:
  several regions, each preceded by an alias of the same private head (the head's own memory mapped once per region), so
  every block reaches a dispatcher, probes and tail slot of its vCPU within its region; a link between regions is a
  checked link. This serves either design and is how the area grows past 126 MiB.
- *Trust.* Today the resident accepts a pool entry only after the position and tail checks, because nothing in the
  resident admits code again; those two exist because the ring is overwritten under readers (the prefix compare and the
  fences belong to any content index and stay). Direct placement removes the ring and those checks; containment then rests, as for every placed block today, on the host having admitted what it
  wrote, plus the placement transformations (split, relocation), which run in the host instead of the resident. The
  host is the trusted side already. Neither weaker nor stronger in principle; less code on the path that must be right.
- *What the host must keep beside the index.* (i) The admitted form with its relocations (`Kept`), to move an answer to
  another RIP or page offset without admitting again; (ii) the moved index (`find_moved`); (iii) the prefix bytes, which
  are the key; (iv) for the disk cache, nothing after the object is written at compile time. The object itself can go
  once it is placed *if* the placed block can give the admitted words back, as `cache::copy` already restores a template's
  link words when it copies one.
- *An overflow tier.* Needed. Speedometer makes 1.55 M answers, 1.9 GB as objects (`cap_bytes`' comment), a third of them
  found again; at about 261 placed bytes each that is some 400 MiB of code, three times the reach of one region. Whatever
  the store's size, a flush empties it, and what refills it must not be fexc (about 8 us a compile against under 1 us for
  a kept answer). So the host keeps a bounded map of objects (or leans on the disk cache) as the tier behind the store;
  "one copy in the whole system" holds for what is live in the store, not for what was evicted from it.

**What it buys, against resident-placed shared code.** Both make class (a) (a peer has the block) an L2 hit with no pool
lookup, no copy and no placement; `pool` and `place` for those vanish in both. They differ on an L2 *miss*:

| On a shared-L2 miss | Resident-placed (the plan) | Host-placed |
| --- | --- | --- |
| the same bytes exist elsewhere (another frame or RIP: freshly linked programs, ASLR) | pool hit in the resident, no exit: 0.5-1.0 us (cross-vCPU reuse plan, measured) | a host exit: 2.9-3.4 us (same plan, measured), less the resident's copy and placement |
| never seen | host exit, reply staged, resident places | host exit, host places: saves the staging copy and part of `place` (0.035 vCPU-s of the stretch, the whole bucket) |
| memory | host map (up to 1 GiB) + ring (256 MiB) + one store | host tier (smaller) + one store |
| resident code | `place`, `copy`, `relocate_code`, the pool reader stay | all four go; the host gains placement |

Host placement therefore gives back the pool's own gain (the pool exists because a round trip per content hit cost
configure 3.3%) in exchange for at most the `place` bucket and a simpler resident. Its strong points are memory, the
flush run from the host, and the end of the pool's reader protocol.

**Recommendation** (the third review agrees with the conclusion and corrected two premises). Not host placement:

1. *The plan as it stands:* a resident-placed shared area, with the pool's ring and the host map behind it. Class (a)
   is gone; a content hit stays in the resident.
2. *Host heap, with no resident change:* while an answer is in the ring, the host keeps it there only (its `Entry`
   keeps the key, the kept form's relocations and the ring position): up to the ring's 256 MiB less in the host.
3. *The ring is not removed yet.* Making the store the pool (the content index pointing at placed blocks, a hit being
   `cache::copy` from the live block) saves the ring and half a content hit's cost, but the ring survives a flush of the
   store and the store does not; and the disk cache cannot stand in as the tier behind it, because kernel and JIT
   answers never reach it (`cpl3` only, `runtime_code` never). First measure, after the switch, the content hits on
   entries older than the last flush; fold only if they are rare.
4. *Host placement of fresh compiles* is optional and small (at most the `place` bucket, 3.7% of the stretch), and
   worth it only if the flush moves to the host anyway.

By how much a host-managed single store would beat step 1: it does not. *Estimate* (the review's): about 41% of `pool`'s
0.188 vCPU-s is not class (a), 0.077 vCPU-s; at three to six times the cost per hit that is 0.15-0.39 vCPU-s added against
0.035 saved, a net loss of 0.12-0.35 vCPU-s per 0.95 s stretch. In memory it would save the ring and part of the host
map; step 2 gets the ring's worth without it.

## 13. Review (November 26)

Independent review by a second model (Opus), told to break the design and to propose cheaper ways to the same end.
Verdict: accept with fixes. Every blocking finding is adopted.

- **B1 (blocking), a stale block could stay live for good.** The draft bumped the generation before clearing the bits and
  judged a new block by its generation alone: a placer that marked between the two read the new generation and the old
  bytes, and nothing ever bumped again. Adopted: bits cleared first, then the bump; the placer's and the adopter's last
  step is a load of their own bit, today's `kept` (section 6, with the case analysis; the schedule is a unit test and a
  mutation).
- **B2 (blocking), a peer may be sent into words it was never synchronized for.** The instruction-cache invalidation is
  broadcast, the context synchronization event is not (Linux's `flush_icache_range` interrupts every CPU for this).
  Adopted for the one path that lacks an event, a local link's direct branch: the `synced`/`stamp` rule of section 4.
  `eret`, L1 and checked links have the event already.
- **B3 (blocking), the tail was incomplete.** Linked `svc` words not stopped; the probe thunk's return address also lives
  in d0; `interrupted` and `stopped` halt on a dead block, which becomes the normal case; `resume` and
  `resume_interrupted` arm again; stub words read by PC. All adopted (section 5).
- **B4 (blocking), deadlock with the I/O window,** and HLT is a resident loop, not a host exit. Adopted: away covers
  every wait inside the resident (section 8, step 5).
- **B5 (blocking), chunks and chain heads across a flush.** Adopted: chunks are given up in step 3; chain heads carry the
  epoch (section 8, steps 3-4).
- **B6 (blocking), an 8-bit epoch wraps.** Adopted: 15 bits, and the wrapping flush clears L2 (section 3).
- **Second review (Opus, section 4a's eager in-page linking):** accept with fixes; four blocking findings (a decode
  record as a link target, `Link::Bad` halting, linking before `kept` and the reuse check, the breakpoint and STI-first
  refusals), the measurement and the build order corrected. The owner then kept linking lazy; the findings are
  recorded in section 4a. One finding reaches the plan itself and is adopted: instruction breakpoints are per vCPU and
  shared links are not (section 4, the armed count).
- **N1** crossing blocks: both frames marked, compared and walked (section 6). **N2** flags written after filing move
  into `state` (section 3). **N3** linkers only try the lock (section 4). **N4** the host's flush has its own form, and
  host tools read the shared spans (section 8, step 6). **N5** the estimate: replaced by the counting boot's numbers
  (section 11). **N6** tail against poll: the reviewer keeps the tail (a stop costs 0.026 vCPU-s of the stretch; the poll
  costs every block) while crediting the poll with removing B3's whole class; unchanged. **N7** a third design (arm
  shared `b`/`svc` words with `brk`, legal, peers emulating the original word when they hit a stop that is not theirs):
  noted, not taken: it makes every peer trap on one vCPU's stop.
- Not verified by the review: the current DDI 0487 section number of the concurrent-modification rule (B2.6.5 in the
  first ARMv8 issue is confirmed from Linux's quotation; B2.2.5 later is believed) and whether its list names `ISB` and
  `BRK` beside `B`, `BL`, `NOP`, `SVC`, `HVC`, `SMC` (the plan uses only `B`, `NOP` and `SVC` on shared words, which are in
  both); Apple cores' `CTR_EL0` (read at run time); the VIPT rule's section number.

- **Third review (Opus; sections 6, 6a, the lazy link, 12a, and the plan's consistency):** accept with fixes.
  - **B1 (blocking):** with the last bit check after the publication, an adopter can take the block in between and keep
    it after its placer gives it up. Adopted: the check is before the CAS (sections 3 and 6); `shared.rs` and its model
    follow.
  - **B2 (blocking):** a posted peer had no way to find its L1 entries of a frame. Adopted: a private per-frame list.
  - **B3 (blocking):** an armed vCPU adopting a peer's block over its breakpoint. Adopted (section 4).
  - **B4 (blocking):** away across a host exit that resumes mid-block. Adopted (section 8).
  - **N1/N2:** the two link orders contradicted each other, and neither was needed: a local link's target has only
    frames its source has, so a kill takes both, and nothing has to be cut. Adopted in full: no chain walk, no record
    lock, no inbound lists, no link records, no `out[k]`; the writer's part of a kill is two atomic operations and the
    posts. `shared::link`, `cut` and `State`'s lock, built in chunk 1 for the earlier design, are removed.
  - **N3** kick only a peer that is running code (post always). **N4** no kick for a refused link; counters read after
    `isb`. **N5** the cost table's missing rows. **N6** `rechecked` clears peers' bits only. **N8** the trust paragraph.
    All adopted.
  - **12a:** the conclusion stands; "the host cannot publish" was too strong (it depends on `CTR_EL0`), and removing the
    ring would lose content hits across flushes that the ring survives today. The recommendation is rewritten.
  - Regions behind head aliases: judged sound (the dispatcher reaches its tables through x28; a head write must be
    synchronized at each alias unless the cache is PIPT).
- **Follow-up review (same reviewer; section 3's regions and budget, and the adoption of its findings):** accept with
  fixes, two blocking, both in how I had adopted its own findings.
  - **F1 (blocking):** the private per-frame lists could not be threaded through L1 entries, which are reused: a
    refilled entry cuts its old frame's list and a drain misses what followed. Adopted: an append-only arena of {entry,
    block} items (section 6).
  - **F2 (blocking):** skipping the kick for a peer in its resident also removed the stop the pending interrupt gave it:
    it could return into dead code (a lookup made before the post, or a resume mid-block) with nothing pending and loop
    in local links among dead blocks. Adopted the simple way: the writer kicks every holder, as today. (Skipping the
    kick is possible later if every return to generated code loads the pending word after storing `synced` and sets a
    stop when it finds a post.)
  - **N1** the head code that still names region 0, listed in section 3. **N2** the mapping cost is per vCPU, not per
    shadow space, and needs new table-page slots. **N3** records are 1.6 times the code, not 1.1: budget 2 times, L2 at
    16 bytes per 128 of code, the chunk tables budgeted. **N4** touched pages stay resident, and the clamp ignored macOS,
    the ring and the answer map: the M3 has room for one region, and the table says so. **N5** a second breakpoint on an
    armed vCPU. **N6** the lazy link's wording. All adopted.
  - Confirmed by it: the check before the CAS and section 6's argument; never-undone links against the armed count,
    far links and duplicates; `DEAD` on an unpublished record.
  - Not verified by it: Apple's `CTR_EL0.L1Ip`; whether every PIC local link meets the frame rule.

**Alternatives the review put forward, and what becomes of them.**

| Alternative | Removes | Verdict |
| --- | --- | --- |
| Link at placement: when a block is placed, link its exits whose targets are in L2, and the recent sources into it, in one synchronization (private caches) | link trips for *all* code, not only peers' | Evaluated for in-page edges (section 4a, second review) and set aside: the owner keeps linking lazy. |
| Exact peer copy (cross-vCPU reuse option (a) with a shared key index): copy a peer's placed block at the same RIP | most of class (a)'s request time | Rebutted as the end state: every vCPU still places and links its copy, memory stays N-fold, and class (b) is untouched, which the 1-vCPU run shows is the same cost in another class. It also reads peers' private records, whose every field is written with plain stores (that plan's reasons against (a) stand). It is the fallback if chunk 4's forms cost too much. |
| Copy a peer's whole page group, links inside it intact | placement and same-page links of peers' code | As above: a cheaper copy is still a copy per vCPU and per flush. |
| Share only hot code, or one area per address space | a subset | Rebutted (the reviewer agrees): the same protocol and burden for less; the kernel is shared by every address space anyway. |
| Hand a cache over with a migrating thread | | Not applicable: guest threads move inside the guest. |

The review's order (count first, build the cheap competitors, then decide) is answered by the counting boot for the
request side: class (a) is 59% of request time, not a margin. Its point that the *link* side has a cheaper competitor
stands; section 4a records what building it would take.

## 14. As built

**Chunk 1 (November 26), `crates/abi/src/shared.rs` and `shared/tests.rs`.** `patchable`; `take` (chunks); the slot word and
`Table` (find, publish with adoption and reuse of dead slots, clear at the epoch's wrap); `clear`, `bump`, `label`, `live`,
`kept`; `behind`; `Barrier`. (`State`, `link` and `cut` were built for the first design's cut of inbound links and removed
when the third review showed none is needed.) Unit tests, among them both B1 findings as fixed schedules (the plan's
order leaves no stale block; the first draft's order and the late check are each shown to leave one), the mark/write
model on threads with adopters, publishers against killers, and the flush barrier with vCPUs going away and
coming back (and its mutation, no check on return, shown to be caught). Runs: the first form of the module, unit suite `rust` on the M3 (run
20261126-023057-91d9): 916 passed, 15 skipped, with the suite's one known bug (`block-abandoned-worker-retire-timeout`,
unrelated). The form after the third review (a8ca1d6d): run 20261126-031750-c4b5 there, 916 passed, 15 skipped (the known bug passed). Nothing uses the
module yet.

**`CTR_EL0` (November 26): not read.** A user program's `mrs x0, ctr_el0` is refused on macOS (SIGILL on this M2 Max,
tried), so the host cannot learn it that way; the value that matters to the resident is the one its own read gets in the
VM (`cache::read_ctr_el0`, kept in `Control::ctr_el0` and printed nowhere). Asahi Linux reports `0x8444c004` for Apple's
cores (from memory, not checked here): L1Ip = 0b11 (PIPT), IminLine and DminLine 64 bytes, IDC and DIC clear. Chunk 2
prints the resident's copy in the run's state line, which settles it per machine; until then the plan assumes nothing
(every head write is synchronized at each alias it can run at).

**Chunk 2, first part (November 26): regions on the private code areas.** Each vCPU's code area is a row of regions of
128 MiB of address space, each beginning with a 2 MiB alias of the vCPU's one head and then holding code of its own;
nothing is shared between vCPUs yet. `--code-cache` (`Boot::code_bytes`) above one region's worth (128 MiB less the head)
makes several, up to 31 (a code-area offset stays below 4 GiB, the `u32` fields of a block record); the default is one
region, which places, links and flushes as before (the fill rule is tested placement by placement against the rule before
regions).
- *Where.* The row lies above the private window in the window's level-1 table: executable aliases from `PRIVATE` + 64
  GiB, writable ones from `PRIVATE` + 68 GiB (`cache::CODE_VA`, `CODE_WRITE_VA`; range checks in `natemu_abi`), also when
  there is one region. Its level-2 tables are pages of a new 2 MiB area of the vCPU's block of the backing
  (`cache::REGION_TABLES_OFFSET`, before the code; not window pages, which bound guest RAM), written by the host
  (`memory::map_regions`). Cost per vCPU: 8 bytes per 2 MiB mapped in each alias, 1 KiB a region, in one 4 KiB table page
  per GiB of each alias: two pages up to eight regions, eight pages at 31. The backing's code is the head's 2 MiB once and
  then each region's code (`cache::backing`, `code_at`: what every host reader of placed code goes through).
- *Offsets.* A code-area offset is linear over the row in both aliases. A block reaches the head in its own region's
  alias (`cache::head_at`: the computed exit's `b`, a probing stub's `b`, a checked link's `b`); a branch read back, and a
  PC, are reduced to the head's offset before they are compared (`cache::head_offset`, `head_pc`: the miss, the
  dispatcher's range at an interrupt, `probe::at` and with it `returns_to` and `rearm_at`, the sampler, the host's link
  check and `locate`). A PC in a head alias is resumed where it was: the dispatcher reaches its tables through x28 and
  its miss by a relative branch, a probe returns through x0, and empty L1 ways name region 0's miss. The step block runs
  at region 0's alias only.
- *Fill.* Linear, by one rule (`cache::fill_at`, in `SegFill::reserve` and `share`): what is placed lies in one region,
  after its head alias; a block that would cross its region's end begins the next region's code; a segment (a block's
  code and its hot copy) lies in one region.
- *Links.* A link whose target lies in another region is made checked (`b` to the source context's dispatcher slot in the
  source's region, which probes L1 at the address the exit built), as section 3 says, and counted (`Control::far_links`).
  A REP string's self-exit and a context without a slot are not linked, as for every checked link.
- *Instruction cache.* A write to the dispatcher, a slot or the probes is synchronized at every region's alias
  (`cache::synchronize_head`); the step slot at region 0's.
- *`CTR_EL0`.* The resident's copy is in the K1 statistics (`k1.ctr_el0`, beside `k1.regions`: the row's regions, the fill
  point's region, far links). On the M3 (system case `k1-regions`): `0x9444c004`: L1Ip = 0b11 (PIPT), IminLine and
  DminLine 64 bytes, IDC set, DIC clear. So on this machine an invalidation by one alias serves every alias, and the
  per-alias synchronization cannot be shown to be needed by a test there (mutation below).
- *Tests.* Unit: the offsets, the backing and the row's end; the fill rule (no straddle, heads skipped, a row's end, one
  region as before); a probe reached and recognised at every alias; the sampler at an alias; the tables the host writes for
  1, 2, 3, 9 and 31 regions (`natemu_abi::execution::tests`, the host's
  `the_code_area_is_a_row_of_regions_behind_head_aliases`). System: K1's `regions` (case `k1-regions`, and in every `k1`
  case): a row of two regions with a test fill of 320 KiB a region (192 KiB at block size 1; the host's
  `NATEMU_REGION_CAP`), a chain that fills region 0 and ends in region 1 with a page-crossing store and `jmp r13`, three
  passes, the tail's frame run again at a second address (a copy in region 1, under the reuse check); the counters
  required: the fill point in region 1, far links, checked links, copies, no flush. Boot: `boot-startup-regions`, the SMP
  start-up boots with a row of two regions of 4 MiB each: code placed in the second region on the BSP and the full row
  flushed at least once in each boot. (Two regions, not three: a head branch computed from the linear offset wraps by
  256 MiB, so from an odd region it lands on the head alias two regions up if that is mapped, and from an even one it is
  right; only a block in the row's last, odd region shows it.)
- *Mutations* (each one run of the system case `k1-regions` on the M3, as the case stood before the tail's second
  address was added; red unless noted): the computed exit's `b` from the linear offset (a fetch outside the row,
  `private_access`); a probing stub's `b` from the linear offset (`private_access`; not at block size 1, whose stubs are
  `brk`s); a later region's fill begun at the head's end instead of after the alias (resident stops and the host's link
  audit: region 0's blocks overwritten); a direct `b` into another region (`private_access`); the PC reduction removed
  everywhere (`unknown_abort` in the probe) and at the miss test alone (`unknown_abort`); a later region's head alias
  mapped onto its own backing (`trapped`: the alias is empty). **Not observable on PIPT hosts:** the head synchronized at
  region 0's alias only passes (`CTR_EL0` = `0x9444c004`, L1Ip = PIPT: one invalidation serves every alias); the
  per-alias invalidation stays, as the architecture requires it for other cache types. The reuse check's expected
  computed-exit word from the linear offset (the independent review's finding: the check then refuses a right copy in
  an odd region): `reuse_mismatch` at the tail's second address.
- *Runs* (M3, November 26, the tree of the commit before this paragraph's): unit suite `rust` 20261126-060037-e0dd,
  920 passed, 15 skipped (the suite's known bug passed); system case `k1-regions` 20261126-060037-8ac2, passed at 1 and
  2 vCPUs, both block sizes (`k1.regions` [2, 1, 3], [2, 1, 2] at block size 1); boot case `boot-startup-regions`
  20261126-060037-f221, passed (BSP: row of 2, peak fill point 140.5 MB, 2 flushes, 4,752 and 4,950 linker entries with a
  far target). Experiment, one pair of boots of an earlier form (three regions, runs 20261126-044830-8aac and
  20261126-045831-34e4, a loaded machine): with far exits left unlinked the BSP entered the linker for a far target
  786 thousand and 1.34 million times a boot; made checked, 4,540 and 5,328 times.
- *Review.* An independent review (Opus) of the change found that defect (blocking, fixed), the boot case unable to show
  a wrong head branch with three regions (now two), the K1 check passing without a checked far link (now required), and
  the host's link audit accepting a checked link into another region's slot alias (now refused).
- *Not in this part* (chunk 2's rest): the record and lookup spans, the budget rule, the tail slot, anything mapped in more
  than one vCPU.

**Chunk 2, second part (November 26): the store's spans, mapped in every vCPU, and the tail slot.** The store
(`cache::store`) is laid out, sized by the budget rule and mapped; nothing is placed in it, and nothing runs in it but a
test hook's three words.
- *Where.* All three parts lie together above the private row, in the window's level-1 table (so every root of a vCPU,
  the supervisor, user and compat spaces alike, maps them): the code row at `PRIVATE` + 72 GiB (16 regions' reservation,
  2 GiB), its writable alias at + 74 GiB, the records span at + 76 GiB (5 GiB reserved), the lookup span at + 81 GiB
  (1 GiB), the window ending at + 82 GiB; range checks in `natemu_abi` (level-1 entries, x86-noncanonical, each part in
  its reservation, the largest store valid, the header in its block, table pages in the region-table area). In the backing
  the store follows the last vCPU's block, before the pool: code, records, lookup (`store::Layout`).
  **Plan issue, decided:** section 3's table puts the lookup span "after the global area", in the private window; at the
  most regions its L2 alone is 256 MiB and the generations up to 36 MiB, more than that window has free beside the pool,
  so it lies with the rest of the store.
- *Code row.* Every region begins with the vCPU's own head (its code backing's first 2 MiB, EL0 execute-only, as in its
  private row) and then holds 126 MiB of the store's one code span, the same in every vCPU at the same address; offsets
  linear as the private row's (`store::region`, `code_start`, `head_at`, `backing`). The writable alias maps the code only:
  its head aliases stay unmapped, the head being written once through the private row. `head_pc` reduces a PC in the
  store row's head aliases too, and a head write is synchronized at every alias of both rows (`synchronize_head`).
- *Records span:* the records (twice the code), then the chunk tables (code / 16). *Lookup span:* a 2 MiB header block
  (`store::Header`: the size; chunk 7's fill words, flush epoch, barrier, `synced` and VM-wide slot contexts, every slot
  context written `NO_CONTEXT` by the host; the test hook's words), the L2 (code / 8 bytes, the next power of two, at
  least 2 MiB), the generations (4 bytes a guest-physical page below max(RAM's end, 4 GiB): a firmware image lies below 4
  GiB).
- *Budget* (`store::budget`, the host's `store_regions`): guest RAM / 8 in whole regions, at least one, at most a third of
  the headroom (host memory by `hw.memsize`, less guest RAM, 3 GiB, the pool ring's 256 MiB and the answer map's bound,
  `cap_bytes`), and at most **16 regions**. **Plan issue, decided:** section 3 sets no ceiling but memory, but a slot word
  names a record by offset / 16 in 28 bits, so the records span holds at most 4 GiB: twice the code, so at most 2 GiB of
  code, 16 regions (4,032 MiB of records). `machine.shared_code_mib` (`--shared-code MIB` in the drivers) overrides it in
  whole regions, rounded up (1 to 2,016 MiB). **Decided:** whole regions only for now; the 2 MiB store `shared-flush`
  wants is a test cap to add with chunk 5 or 7, as `NATEMU_REGION_CAP` is for the private row. The unit test checks the
  section 3 table's rows (M3 4 GiB: 1; M4 4 GiB: 4; M4 8 GiB: 8; 1 GiB: 1) and the spans' sizes from the code (one region:
  126, 252 and 16 + 8 MiB).
- *Tail slot:* 64 KiB of the head after the probes (`cache::TAIL`; `HEAD_BYTES` grows by it). Reserved: chunk 3 writes it;
  its record in the private lookup comes with chunk 3.
- *Test hook* (`NATEMU_STORE_TEST=1`, `Control::store_test`): each vCPU writes its own tail slot (`movk x0, #(v + 1), lsl
  #48`; `b` to its dispatcher's miss); the first vCPU at start (CAS on `Header::test_writer`) writes one `b` to the tail
  slot at the first code word of the store's last region through the store's writable alias, synchronizes it and
  publishes it (`test_ready`); then every other block entry of every vCPU (none in a shadow, a stop, a PIC test or the
  step slot) goes in there, interrupts masked, with the block's RIP in x0. The miss from the store row is counted as the
  vCPU's own (`store_entries`: from the last region's head alias, x0 the RIP entered with this vCPU's mark) or not
  (`store_foreign`), x0 restored, and the miss served as any. K1 reports `k1.store` = [regions, wrote, entries, foreign].
- *Tests.* Unit: abi `the_store_is_sized_by_the_budget_rule_and_aliases_the_head_in_every_region`; host
  `the_store_is_one_span_behind_every_vcpus_own_head` (every descriptor of every part for 1, 2 and 16 regions and the
  rule's, at 2 and 3 vCPUs: own head, one code backing, no head in the writable alias, nothing past an end; the header; the
  override's bounds); cli `shared_code_mib_key_sizes_the_store_within_its_bounds`. System: `k1-store` (run_k1.py
  `--store-test`): the `regions` program at 2 vCPUs, both block sizes, with a store of two regions (its last odd, where a
  head branch from the linear offset leaves the row) and the hook: one CPU wrote the trampoline, every CPU's entries
  reached its own head's miss in the last region, none foreign. So *a word written through one vCPU's alias is executed by
  another* (the CPU that did not write it counts entries) and *a block in the last region reaches its dispatcher* (its own
  vCPU's: the mark). On the M3: `k1.store` [2, 0, 5478, 0] and [2, 1, 5478, 0] in one block size's run, 4,135 entries a CPU in the other's.
- *Mutations* (M3, November 26; each one run, red unless noted). System case `k1-store`: the store's code backed per vCPU
  (vCPU 1's on its own memory: it runs zeros; `trapped`, no entries; run 20261126-152813-c1d4); every vCPU's head aliases
  in the store row mapped to vCPU 0's head (vCPU 1's entries all foreign, its marks vCPU 0's; 20261126-152905-55c0); the
  trampoline's branch from the linear offset (out of the row from the odd last region: `private_access`;
  20261126-153000-f59e); `head_pc` not reducing the store row (the miss is no miss: `unknown_abort`; 20261126-153059-8316).
  **Not observable on the M3** (PIPT, IDC: one invalidation serves every alias): the head not synchronized at the store
  row's aliases passes (20261126-153159-0784); the synchronization stays, as the architecture requires it. Unit suite
  `rust`, one run with three mutations at once (20261126-154057-9a00): vCPU 0's head in every store row (the host's
  mapping test red), the budget without the headroom bound and the override rounded up by one region too many (the abi
  test red; the cli test red, from the override's alone).
- *Runs* (M3, November 26): system cases `k1-store` and `k1-regions` and boot case `boot-startup-regions`
  20261126-153323-198b, passed (commit a4bf387e; the later commits change host tests only); unit suite `rust`
  20261126-153859-4ed4 at b9d6806b: 923 passed, 15 skipped (the suite's known bug passed). Before the fix of the entry
  frame's x0 (the host's check that the resident's entry frame holds only what it writes) `k1-store` failed
  (20261126-152556-dca5); the PIC tests' reset of x0 at the next resident entry now covers the hook.
- *Independent review and replication (November 26).* At `54ee1f94` the size override rounds in MiB and host headroom
  uses saturating subtraction. Before the fix, the CLI accepted 2^44 MiB and the ABI test panicked when an answer-cache
  allowance exhausted the host's headroom. The hook now uses atomic references to individual header words, releases
  its borrow of the control record before synchronizing code, and borrows the restored frame before dispatch. The reviewer
  rechecked the fixes. On the M3 the Rust unit suite, `k1-store`, `k1-regions` and `boot-startup-regions` passed.
  In a negative control, `k1-store` failed both a report containing only the writer and an empty report. An independent
  `k1-store` run on the M2 Max confirmed that CPU 1 wrote the trampoline and CPU 0 executed it. Each CPU recorded
  5,478 entries at the default block size and 4,135 at block size 1, with zero foreign entries.
- *Not in this part:* anything placed, published or looked up in the store (chunk 7), the tail's use (chunk 3), a test cap
  below one region.

**Chunk 3 (November 26), stops by the private tail; branch `shared-tail` from `main` at `be88e684`** (not on chunk 2's
regions: section 12 lets it start from `main`). No stop patches placed code any more: `cache::arm`, `arm_svc`, `disarm`,
`Control::armed`/`armed_words` and `ARMED` are gone, and so are the relink's armed check and the host link audit's
armed-word exception.
- *Where.* The tail slot is the head's 64 KiB step slot split in two (`cache::SLOT_BYTES` 32 KiB for the step block, then
  `TAIL_SLOT`, `TAIL_BYTES` 32 KiB, the largest block's size), so `HEAD_BYTES`, the dispatcher's and the probes' offsets
  and every limit derived from them are unchanged on `main` (section 3's 64 KiB tail slot belongs to chunk 2's head; 32 KiB
  is enough for either). The tail's record is `cache::TAIL_RECORD` in the lookup area, 4 KiB taken from the step record's
  span (which keeps 236 KiB for a Block and a staged object's sites). Its sites are read where its origin's are
  (`Control::tail_origin`, the origin record's address; `cache::sites` follows it), not copied; a flush clears the record.
- *The copy* (`cache::tail`). Each link point's first word and its `svc` word, and the computed exit's branch,
  become `STOP_WORD`. For other words, `tail_word` uses the hot-copy branch encoder. A branch inside the block keeps its
  displacement; a branch to the hot copy returns to the corresponding word in the tail; a branch to a shared probe still
  reaches the probe. A split stub's recorded `adr x0` computes its probe return at the new address. Unclassified address
  producers, literal loads, branch-with-link, register branches and out-of-range targets use the in-place step fallback.
  The corpus case checks the recorded probe relocation of each `adr` before allowing it to move.
- *Doorbells* (`execution::doorbell`, which `interrupted` calls after the acknowledge). As before in the dispatcher and at a
  block's first word. Dead blocks are no longer refused (the filter is gone there and in `stopped`). In the tail already:
  merged, nothing built. In the step block: the stop is wanted and the block resumes (its exits enter the resident). At an
  exit word itself (not reached through a probe): the stop is taken there at once (counted `at_exit`; the fallback needs
  it, and it saves a copy). Else the tail is built and the vCPU resumes at the same word of it, or, in a probe, where it
  was with the probe's return moved into the tail (x0 at the probe's first word, d0 after it, x0 too at its last two).
  **Found by the test, not in the plan:** at a probing stub's *second* word (`b` to its probe) the stub's `adr` has already
  put its return in x0; that x0 is moved too (`cache::stub_return`), or the probe returns into the block and the stop is
  lost (the stop test's check caught it at once, in `probe-copy` and `probe-share`). The encoder admits only this known use of a register holding a native return address.
- *Stops* (`stopped`): a `STOP_WORD` trap is a stop only in the tail at one of its exit words (anywhere else `bad_stop`, so
  `--forge-stop` still halts); it is taken whether a stop is still wanted or not (the words are the resident's own, and a
  boundary is always a legal place to stop). `pic::count::SVC_STOPS` counts a stop at an `svc` word whose origin is linked
  there, as before. A deferred stop at a PIC block's full entry performs the entry's page-base AND in the captured
  x0 and resumes at the copied store. The RIP move and its SVC identify the same REP self-exit. With RCX zero,
  the stop advances past the REP. With elements remaining, it preserves RF and marks the between-iteration boundary.
- *Resumes* (`resume`, `resume_interrupted`): a stop still wanted resumes in the tail, built now if the PC is not in it
  (counted `at_resume`); the PIC study's `svc` test enters its blocks in a tail whose `svc` words alone stop, built only
  when one of them is linked (`cache::tail(b, false)`), as `arm_svc` armed only then.
- *The fallback* (no tail: a refused word): a software step from the word, each step a doorbell's place again, until it
  is an exit word, the dispatcher, a block's first word or an entry into the resident. Expected never (no refused word in
  the corpus); it is tested by the stop test's `step` mode, which refuses every tail. A pending stop allows a watchpoint
  or FP handler to finish its own single-step before fallback resumes.
- *Fault recovery.* An abort that drains a flush or finds its placed block dead banks the interrupted guest instruction
  and re-fetches it as a step. A tail borrows its origin's sites. The handler saves its address and borrows the record after the drain, so clearing the tail descriptor cannot erase its PIC identity or x87 recovery map before the bank. The original record
  is available until the next placement. A store in a tail into its own code page still takes the existing self-write step.
  The SMP test's former armed-record tamper case was removed with that record. Its forged stop outside the tail remains.
- *Checks.* `unit/rust` checks which native words move or require the fallback, and the K1 stats formatter with zero and
  distinct nonzero tail counters. `unit/rust-admission` checks the encoder against admitted objects in the boot corpus.
  `system/shared-stop` runs 13 K1 programs at 2 and 4 vCPUs and both block sizes, in tail and forced-fallback modes.
  It rings after rotating software-step counts from 1 through 48, checks that execution stays in a tail or its probe
  until a stop, and requires the K1 final registers and memory. This samples reachable offsets; it does not establish
  coverage of every native offset or compare a trace of intermediate stops with the old arming implementation.
  `system/shared-stop-x87` rings on the 32-bit x87 run end's return from cold to hot SSE code and compares against
  the existing QEMU answer at both block sizes. `system/shared-stop-flush` flushes on an abort in a relocated PIC
  tail, checks the exact RIP banked for the guest instruction against its pre-flush address, and checks the programs'
  final states. Two different aliases can converge to the same final values, so checking that intermediate RIP matters.
  `system/shared-stop-rep` rings at the full PIC entry of a REP block. `shared-stop-rep-exit` replays the
  accepted RIP move of a real unlinked REP LODSB self-exit, or rings at its SVC. At each block size, each schedule
  checks five boundaries, three with RCX nonzero and RF preserved, two with RCX zero and RF clear.
  `shared-stop-fp` runs CVTTSS2SI on a quiet NaN in accurate mode with AFP disabled. The case requires a
  pending stop, a refused tail, and the FP handler's completed single-step, along with the x86 result.
- *Observed before the interruption* (November 26). The rented M4 passed `shared-stop` and `pic-svc`; the former built
  1,343 tails, including 43 at resume and 136 from dead origins, moved 42 probe returns and merged 542 second
  doorbells. Its fallback mode took 11,025 steps. On the M2 Max, the existing `smp/kicks`, `smp/system`, and
  `smp/contain` run finished with 733 passed on the earlier code.
- *Review and narrow verification.* The independent review accepted the private-branch code at `26ca38c5` after
  fixes to the PIC full entry, REP self-exit, FP single-step ownership and cache record borrows. At that commit the
  rented M4 passed the Rust/admission checks and the stop, PIC SVC, x87, flush, REP entry/exit and pending-FP cases
  (919 passed, 16 skipped, one known-bug-passed). `static/stack` and `static/stack-profile` passed on the Mac mini M4
  after the audit at `e3fb2921` named the doorbell resume closure and the software-step frame return.
- *Mutations* (rented M4). Seven independent changes on the initial implementation each failed `system/shared-stop`:
  omit the stop at the link SVC; omit the computed-exit stop; leave probe returns in hot code; leave d0 unmoved;
  leave x0 unmoved at the stub's second word; reject a dead block at the doorbell; omit the existing-tail merge.
  The x87-only wrong-target mutation subsequently failed `shared-stop-x87` at `bad_stop` and disagreed with QEMU.
  Banking the retired tail descriptor instead of its placed origin failed `shared-stop-flush` on the intermediate RIP.
  Three further mutations on `26ca38c5` each failed its focused check. Resuming at `TAIL_VA - 4` trapped at the full
  PIC REP entry. Losing the REP bit at the unlinked SVC yielded `bad_stop` in both schedules at both block sizes.
  Letting fallback take the FP handler's single-step gave zero completed FP reruns in `shared-stop-fp`.
- *Four-vCPU measurement.* Bench case `guest-hackbench`, Mac mini M4, November 26, 2026, four jobs, two runs on
  each build with the same kernel and guest image. First-pass/warm-pass wall seconds were 3.114/3.168 and
  3.604/3.439 on `be88e684`, 3.108/2.923 and 3.301/3.264 on the private-tail resident from `26ca38c5` (audited
  checkout `e3fb2921`). All eight passes verified without reported contention. The observed same-build spread
  covers the change in the mean for each pass, so this case establishes no timing change beyond its noise.
- *Affected run, rented M4.* The full affected selection at `26ca38c5` finished with 102,597 passed, 41 failed,
  1,215 skipped and five known-bug-passed. Counting case wrappers and their individual checks, 34 failures concerned
  host-kick volume, five a stale ERET caller list, one a fixture rename and one a historical compiler dependency.
  The corrected stack audit at `e3fb2921` passed with both binaries and its binary mutation tests on the Mac mini M4.
  A cold concurrent `fexc-precision-0` lost the shared temporary fixture rename. It passed on the original commit
  after generation, and all four cold precision shards passed together with the separate fixture repair `f5ffbf19`.
  The rented checkout's `main` at `035b243e` made `fexc-replay` select a historical FEX pin missing from its shallow
  submodule. With `76caba9a` the case fetched and replayed that declared pin, preserving the current FEX checkout.
  The original runtime then passed with the cached compiler. The M2 Max passed against its canonical `main` at `be88e684`.
- *Host kick coverage.* On the same rented M4, `smp/kicks` on both `be88e684` and the tail ended its hot and REP
  loops before the host issued 1,000 kicks per vCPU. Every issued host doorbell was acknowledged or pending, and
  both builds acknowledged all 7,000 host kicks in the longer adversarial group. In `system/k2-sysfast`, the
  experiment at `d1e2a08a` (old) and `dfb9322f` (tail) counted vector posts with the original guest and kicker
  options. The old build made 25 accepted posts on each run and delivered 16 guest interrupts. The tail made 25
  and 24 accepted posts and delivered 15 on each run. All delivered at CPL 3 with IF set. The M2 Max acknowledged
  all 7,000 kicks in each `smp/kicks` group on both builds. All seven cases that fell short of their injection
  requirements on both rented builds passed on both M2 Max builds. They were `smp/kicks` and the six `system`
  cases `k2-sysfast`, `k2-swapgs`, `k2-repbulk`, `k2-repmovs`, `k2-verw` and `k2-stifirst`.
- *Exact paired stops for one split-store block.* `system/shared-stop-pair` on `shared-tail-observer` at `0c488dcb`
  compares the pinned pre-tail resident (`be88e684`) and the resident built from the current checkout, including dirty
  inputs. It adds the same test-only observer to both. On the Mac mini M4, hardware stepping on the prewarmed
  four-byte crossing store through its unlinked literal exit found the same 29 distinct executed words on both builds:
  13 in the hot copy, four in the cold code (split-stub `adr` and branch, literal-exit move and SVC), and all 12 on
  the normal path through its shared probe. For each word a fresh VM sets an EL0 execution breakpoint and posts
  a self-SGI at that word. Both the breakpoint PC and the SGI's actual ELR must equal the requested address. The case
  compares the first banked RIP, GPRs, RFLAGS, XMM/YMMH, MXCSR, x87, GS bases and the four guest bytes written. Four
  more schedules check aligned entry offsets +0, +4, +8 and +12. All 33 comparisons passed. At the final unlinked
  SVC, the old armer has passed its patched move, so the check requires service at the next resident entry on the old
  build and a banked exit on the new build. Both report the same guest state. Moving service to a later generic entry
  in the other exit schedules is a distinct verdict. A dirty working-tree mutation flipping banked RBP reproduced the
  same 29-word discovery path and failed all 33 comparisons on GPRs on the Mac mini M4. It also failed all four
  entry and twelve probe comparisons of the narrower M3 case.
- *Separate REP comparison.* `system/shared-stop-pair-rep` on `rep-stop-pair` at `83cea621` compared the first
  iteration of non-bulk REP MOVSD against `be88e684` on the M3. Each of its two RCX seeds (3 or 1) reached 13
  unique physical words, 11 hot and two cold. The 26 stops agreed on registers, flags and captured memory, including
  the unlinked LINK SVC where the old and new service origins differ. Dropping only the REP identity of that SVC
  failed both SVC schedules: the remaining-elements stop lost RF, and the final-element stop left RIP on the REP.
- *Installed L-s branch.* `system/shared-stop-pair-linked` at `f5b23396` on the M3 exercised one checked link from
  an absolute source to a PIC target. Both residents executed the installed branch at native offset 16 under a
  hardware single-step and reached its decoded dispatcher slot with the target in x0. On the next visit, an SGI at
  that exact branch banked the target RIP before its fifth execution. The old and new state files matched byte for
  byte, with four completed target entries in registers and memory. An independent M2 Max run also matched both
  banks at offset 16. On the M3, mutation `ede97a9d` banked the source RIP and failed only the RIP comparison.
- *Open x87 run.* `system/shared-stop-pair-x87` at `2123f502` on the Mac mini M4 followed one linked 32-bit
  transition from x87 to SSE with ST(0) live. The old and new residents discovered the same 22 distinct physical
  words, 12 hot and ten cold, and the next SSE block's linked entry at +8. All 22 exact-SGI stops matched on
  their banked state. The case checks the stop origin, x87 stack, rounding control and exception bits, integer flags,
  SSE result and control register, and the 256-byte guest result area. At the 11 offsets that still execute the cold
  return after the interrupt, a hardware step checks the return lands at the third entry word in the original hot
  code or in the private tail respectively. Moving only that return target forward by one word (`d1fac0e4`) failed
  all 11 landing checks. The other 11 stops still matched.
- *Remaining section 9 evidence.* The exact sweeps cover the selected split-store, REP and open-x87 paths.
  The installed L-s check covers its linked SVC word. The other reachable offsets of that installed block still
  need paired stops. Dead-origin and second-doorbell cases have sampled coverage. The pre-tail armer rejects a
  dead origin. Compare a dead origin's architectural outcome against its live sealed equivalent and check
  dead-origin acceptance and nested-doorbell merging independently.

## Chunk 5 private-cache control

The `shared-flush` work branch starts at chunk 2's `54ee1f94`. It tests the protocol and the
private-cache workload independently. The resident still handles a capacity flush locally.

- In unit suite `rust`, `capacity_reuse_waits_for_a_returning_peer_and_catches_up_a_parked_peer` fills
  a 2 MiB area twice using the chunk allocator and the reviewed `shared::Barrier`. A return before
  the requester's scan requires an acknowledgement. A return after that scan can race a release
  that the requester already has authority to publish. Two peers that stayed away through both
  releases must observe the latest epoch before using the area again.
- System case `shared-flush-private` gives each vCPU a 2 MiB code cache. CPU 0 waits for CPU 1's
  HLT park word and runs a branch chain twice. At four vCPUs it also waits for CPU 2's first
  non-idempotent counter read to complete in the device and CPU 3's real FEX request to be posted.
  The test driver holds the two host replies until CPU 0 has incurred at least two new capacity
  flushes. FEX may finish computing while its response awaits consumption. The guest must reach
  every CPU's own endpoint, receive exactly one wake IPI, obtain counter values 0 and 1 from
  exactly two reads and use the returned translation. The report records capacity flushes and
  total local resets per CPU. On the M3 the leader incurred four capacity flushes at the default
  block size and three at block size one, at both two and four vCPUs. Each peer incurred zero
  capacity flushes and recorded only its initial private reset. In the M3 negative runs, the case
  rejected a workload that fit, a missing CPU report, a duplicated device read (three operations,
  register values 1 and 2), a peer that bypassed the held compile and a device callback that
  returned before release. At two vCPUs, requiring CPU 1 to reset after startup failed at both
  block sizes. The fixed unit schedule rejected a return that ignored the request and one that
  left its away word set.

Production wiring follows the section 12 rebase point. P1 at `8748f45d` changes the private reset,
including the frame-key, checked and entered tables, segment slots and L1 owners. Chunk 2 supplies
the store header containing the VM-wide flush words. Both are needed for the integrated reset.
In chunk 3 at `82c6e979`, the abort handler banks the private tail's original site while its
placed record still exists. Shared reuse can remove
that record as soon as a peer publishes `away`. Capture the recovery state before that publication.
Section 8 specifies the separate return paths for classification and accepted device operations.

On the composed base, run this workload with the coordinated barrier and require returning peers to
discard the old local epoch, including peers that skip intermediate releases. Keep test coverage
for the pre-access classification exit, I/O-window waits that must still serve shadow drops,
SIPI and HLT waits, in-flight compile and tail recovery, and host-originated flushes before enabling
shared reuse. Section 9's stale-code and deadlock mutations apply to that runtime implementation.

### Native MMIO return before shared placement

The `mmio-flush-return` branch starts at the private control tip `d0fb9bf5`. For a native
read requiring a host page classification, the resident saves the running instance's RIP,
flag and x87 recovery information before the request. A new local generation or a host flush
post on return requires banking that instruction, draining local invalidations and fetching
the instruction again. The guest already crossed its instruction boundary, including any
MOV SS or STI inhibition, so the resident uses one precise step. A completed device request
already has its owned completion record, linear address, data and banked guest state before
the request. It commits the returned value and advances RIP once, including after device polls.
A native atomic or swap has no device completion and is refused before the device sees it.

System case `mmio-flush-return` changes the guest instruction while its sole vCPU is stopped.
It checks a claimed and an unassigned page, an instruction protected by MOV SS, and an accepted
counter read at both block sizes. It uses the existing private host flush post. A shared placement
still requires returning through the VM-wide barrier before inspecting a native continuation
and a descriptor lifetime that permits the requester to reclaim the placement.

## Questions for the owner

1. The private tail (chosen; the review agrees) or the entry poll?
2. If chunk 4 shows checked cross-page links cost more than they save, does chunk 7 wait for P1's guarded entries?
3. The shared area's default size (64 MiB proposed) and whether `machine.code_mib` keeps its meaning.
4. (Answered November 26: linking stays lazy, section 4a.)
